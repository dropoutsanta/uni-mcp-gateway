"""Pod lifecycle manager for RunPod GPU instances.

Tracks last-activity per pod, auto-stops after configurable idle period,
and supports on-demand wake with concurrency-safe deduplication.

Also supports *auto-respawn*: if `podResume` keeps failing with "no free GPUs
on the host machine" (the host that owns our pod is fully booked), and the
pod has a `PodTemplate` attached, we provision a new pod elsewhere via
`podFindAndDeployOnDemand`, switch routing to it, persist the new pod_id to
disk, and terminate the stuck old pod.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("pod_manager")

RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"

# Minimum time we wait between RunPod status polls (avoid hammering API)
STATUS_POLL_INTERVAL_S = 3.0
# How long to wait for a cold start before giving up
COLD_START_TIMEOUT_S = 240.0
# How long to wait for a cold start of a freshly-respawned pod (longer because
# it has to download the model, build llama.cpp, then load weights). 12 min.
RESPAWN_COLD_START_TIMEOUT_S = 720.0
# How often the idle watcher checks all pods
IDLE_WATCHER_INTERVAL_S = 60.0
# Hard cap on how many times a pod is allowed to respawn itself (defense
# against a buggy template causing infinite loop = $$$).
DEFAULT_MAX_RESPAWNS = 2
# Substring we look for in RunPod GraphQL errors to identify "host is full"
HOST_FULL_ERR = "free GPUs on the host machine"


@dataclass
class PodTemplate:
    """Everything needed to spawn a fresh pod with `podFindAndDeployOnDemand`.

    Stored on a `PodConfig` so the manager can self-heal when the bound host
    runs out of GPUs. We deliberately don't reuse `runpod.create_pod()` here —
    we go straight to GraphQL so failure modes (e.g. host full) are inspectable.
    """
    image_name: str
    gpu_type_id: str                 # e.g. "NVIDIA A100 80GB PCIe"
    container_disk_in_gb: int
    volume_in_gb: int
    volume_mount_path: str
    ports: str                       # e.g. "8080/http,22/tcp"
    docker_args: str                 # base64-wrapped entrypoint
    env: dict[str, str]              # MODEL_REPO, CTX_SIZE, REASONING_FORMAT, etc.
    cloud_type: str = "SECURE"
    gpu_count: int = 1
    name_prefix: str = "respawn"     # New pod will be named "{name_prefix}-{ts}"


@dataclass
class PodConfig:
    """Static configuration for a single managed pod.

    `pod_id` may MUTATE at runtime when a pod is auto-respawned (the original
    value still lives as the registry key, so MODEL_TO_POD lookups stay valid).
    `api_base` and `health_url` are derived from `pod_id` and rebuilt on respawn.
    """
    pod_id: str
    name: str                        # human friendly label, e.g. "primary"
    api_base: str                    # e.g. "https://<pod_id>-8080.proxy.runpod.net/v1"
    health_url: str                  # e.g. "https://<pod_id>-8080.proxy.runpod.net/health"
    gpu_count: int = 1
    idle_timeout_s: float = 3600.0   # 1h
    # If set, this pod will auto-respawn on "host full" errors instead of
    # giving up. Leave None for pods you want to manage manually (e.g. a pod you provisioned by hand).
    template: Optional[PodTemplate] = None
    max_respawns: int = DEFAULT_MAX_RESPAWNS
    # Stable identity used as the dict key in PodRegistry (never mutates,
    # even after respawn). Lets external callers (model→pod routing,
    # /llm/abort?pod=…) keep using a fixed handle.
    slot_key: str = ""               # filled in by registry on register()


@dataclass
class PodState:
    """Mutable runtime state for a pod."""
    desired_status: str = "UNKNOWN"           # RunPod-reported: RUNNING / EXITED / STARTING / STOPPING / UNKNOWN
    last_activity_at: float = field(default_factory=time.monotonic)
    last_status_check_at: float = 0.0
    server_ready: bool = False                # /health returned 200 recently
    cold_start_started_at: Optional[float] = None
    respawn_count: int = 0                    # how many times this slot has rotated to a new pod_id
    last_respawn_at: Optional[float] = None
    last_respawn_reason: Optional[str] = None


class PodManager:
    """Manages a single RunPod pod's lifecycle (start/stop/health)."""

    def __init__(
        self,
        config: PodConfig,
        api_key: str,
        http: httpx.AsyncClient,
        on_respawn: Optional["Callable[[PodManager], None]"] = None,  # type: ignore[name-defined]
    ):
        self.config = config
        self.state = PodState()
        self._api_key = api_key
        self._http = http
        self._lock = asyncio.Lock()                # serializes state-changing API calls
        self._ready_event = asyncio.Event()        # set when server_ready transitions to True
        # Called by _respawn after pod_id swap so registry can persist state
        # and rebuild secondary indexes (api_base → manager).
        self._on_respawn = on_respawn

    # ── RunPod API ────────────────────────────────────────────────────────────

    async def _gql(self, query: str, variables: dict) -> dict:
        try:
            r = await self._http.post(
                RUNPOD_GRAPHQL,
                params={"api_key": self._api_key},
                json={"query": query, "variables": variables},
                timeout=20.0,
            )
            r.raise_for_status()
            data = r.json()
            if "errors" in data and data["errors"]:
                raise RuntimeError(f"RunPod GraphQL error: {data['errors']}")
            return data.get("data") or {}
        except httpx.HTTPError as e:
            raise RuntimeError(f"RunPod API call failed: {e}") from e

    async def fetch_status(self) -> str:
        """Hit RunPod API for current pod desiredStatus and update local state."""
        q = """
        query Pod($input: PodFilter!) {
          pod(input: $input) {
            id desiredStatus
            runtime { uptimeInSeconds }
          }
        }
        """
        d = await self._gql(q, {"input": {"podId": self.config.pod_id}})
        pod = (d or {}).get("pod") or {}
        status = pod.get("desiredStatus") or "UNKNOWN"
        self.state.desired_status = status
        self.state.last_status_check_at = time.monotonic()
        # Server readiness can only be confirmed via /health, not RunPod API
        if status != "RUNNING":
            self.state.server_ready = False
            self._ready_event.clear()
        return status

    async def _runpod_resume(self) -> None:
        """Resume the pod. On 'host full' errors with a template attached,
        triggers an auto-respawn onto a different host instead of bubbling
        the error up to the request."""
        q = """
        mutation Resume($input: PodResumeInput!) {
          podResume(input: $input) { id desiredStatus }
        }
        """
        try:
            await self._gql(q, {"input": {"podId": self.config.pod_id, "gpuCount": self.config.gpu_count}})
        except RuntimeError as e:
            err = str(e)
            if HOST_FULL_ERR in err and self.config.template is not None:
                if self.state.respawn_count >= self.config.max_respawns:
                    log.error(
                        "Pod %s host is full and respawn cap (%d) reached; refusing further respawn",
                        self.config.name, self.config.max_respawns,
                    )
                    raise RuntimeError(
                        f"Pod {self.config.name}: host has no free GPUs and "
                        f"already auto-respawned {self.state.respawn_count}× — "
                        f"giving up. Manually intervene or raise max_respawns."
                    ) from e
                log.warning(
                    "Pod %s host out of GPUs; auto-respawning (attempt %d/%d). reason=%r",
                    self.config.name, self.state.respawn_count + 1,
                    self.config.max_respawns, err[:200],
                )
                await self._respawn(reason=err[:200])
                return
            raise

    async def _runpod_stop(self) -> None:
        q = """
        mutation Stop($input: PodStopInput!) {
          podStop(input: $input) { id desiredStatus }
        }
        """
        await self._gql(q, {"input": {"podId": self.config.pod_id}})

    async def _runpod_terminate(self, pod_id: str) -> None:
        """Permanently destroy a pod (+ its volume). Used on the dead pod
        after a successful respawn — we don't want to keep paying storage
        for a host that can't allocate GPUs."""
        q = """
        mutation Terminate($input: PodTerminateInput!) {
          podTerminate(input: $input)
        }
        """
        try:
            await self._gql(q, {"input": {"podId": pod_id}})
            log.info("Terminated old pod %s", pod_id)
        except Exception as e:
            log.warning("Failed to terminate old pod %s: %s", pod_id, e)

    async def _respawn(self, reason: str) -> None:
        """Spin up a fresh pod using `self.config.template`, swap our pod_id
        to the new one, persist state, and terminate the dead pod.

        Caller MUST hold `self._lock` (which `_runpod_resume` does, since it's
        only called from within `ensure_running`'s critical section).
        """
        tmpl = self.config.template
        if tmpl is None:
            raise RuntimeError(f"Pod {self.config.name} has no respawn template")

        old_pod_id = self.config.pod_id
        new_name = f"{tmpl.name_prefix}-{int(time.time())}"
        env_input = [{"key": k, "value": v} for k, v in tmpl.env.items()]

        log.info(
            "Respawning %s (slot=%s, old=%s) on a new host. name=%s gpu=%s cloud=%s",
            self.config.name, self.config.slot_key, old_pod_id,
            new_name, tmpl.gpu_type_id, tmpl.cloud_type,
        )

        q = """
        mutation Deploy($input: PodFindAndDeployOnDemandInput!) {
          podFindAndDeployOnDemand(input: $input) {
            id imageName desiredStatus
            machine { dataCenterId gpuTypeId }
          }
        }
        """
        d = await self._gql(q, {"input": {
            "name": new_name,
            "imageName": tmpl.image_name,
            "gpuTypeId": tmpl.gpu_type_id,
            "gpuCount": tmpl.gpu_count,
            "cloudType": tmpl.cloud_type,
            "containerDiskInGb": tmpl.container_disk_in_gb,
            "volumeInGb": tmpl.volume_in_gb,
            "volumeMountPath": tmpl.volume_mount_path,
            "ports": tmpl.ports,
            "dockerArgs": tmpl.docker_args,
            "env": env_input,
        }})
        pod = (d or {}).get("podFindAndDeployOnDemand")
        if not pod or not pod.get("id"):
            raise RuntimeError(
                f"podFindAndDeployOnDemand returned null — RunPod has no "
                f"capacity for {tmpl.gpu_type_id} on {tmpl.cloud_type} either"
            )

        new_pod_id = pod["id"]
        dc = (pod.get("machine") or {}).get("dataCenterId", "?")
        log.info(
            "Respawn OK: %s slot=%s | old_pod=%s -> new_pod=%s (dc=%s)",
            self.config.name, self.config.slot_key, old_pod_id, new_pod_id, dc,
        )

        # Mutate config to point at the new pod. URLs follow RunPod's standard
        # `<pod_id>-<port>.proxy.runpod.net` scheme — derive them from the new id.
        self.config.pod_id = new_pod_id
        self.config.api_base = _swap_pod_id_in_url(self.config.api_base, old_pod_id, new_pod_id)
        self.config.health_url = _swap_pod_id_in_url(self.config.health_url, old_pod_id, new_pod_id)

        # Reset readiness state — new pod has to do full cold start (image
        # pull + model download + llama.cpp build + weight load = ~5-7 min).
        self.state.server_ready = False
        self._ready_event.clear()
        self.state.cold_start_started_at = time.monotonic()
        self.state.respawn_count += 1
        self.state.last_respawn_at = time.time()
        self.state.last_respawn_reason = reason
        self.state.desired_status = pod.get("desiredStatus", "STARTING")

        # Hand back to the registry so it can re-index by api_base + persist.
        if self._on_respawn is not None:
            try:
                self._on_respawn(self)
            except Exception as e:
                log.warning("on_respawn callback failed: %s", e)

        # Best-effort terminate the stuck old pod. Don't let this fail the
        # respawn — the user's request still has a healthy pod waiting to come
        # up. Cleanup runs in background.
        asyncio.create_task(self._runpod_terminate(old_pod_id))

    # ── Health probing ────────────────────────────────────────────────────────

    async def probe_health(self, timeout: float = 5.0) -> bool:
        """Check upstream /health. Updates server_ready and the ready event."""
        try:
            r = await self._http.get(self.config.health_url, timeout=timeout)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        self.state.server_ready = ok
        if ok:
            self._ready_event.set()
        else:
            self._ready_event.clear()
        return ok

    # ── Activity tracking ─────────────────────────────────────────────────────

    def mark_activity(self) -> None:
        self.state.last_activity_at = time.monotonic()

    def idle_for_s(self) -> float:
        return time.monotonic() - self.state.last_activity_at

    # ── Public lifecycle operations ───────────────────────────────────────────

    async def ensure_running(self) -> None:
        """Idempotent: if pod is not RUNNING + server_ready, start it and wait.

        Multiple concurrent callers will share a single resume operation.
        """
        # Fast path: already known ready
        if self.state.server_ready and self.state.desired_status == "RUNNING":
            return

        async with self._lock:
            # Re-check inside the lock — another coro may have done the work
            if self.state.server_ready and self.state.desired_status == "RUNNING":
                return

            # Refresh status
            status = await self.fetch_status()

            if status == "RUNNING":
                # Pod claims running but we haven't confirmed health — probe
                if await self.probe_health():
                    return
                # Otherwise, server is still spinning up inside the pod — wait for /health

            elif status in ("EXITED", "STOPPED", "PAUSED", "TERMINATED"):
                log.info("Pod %s is %s; calling podResume", self.config.name, status)
                self.state.cold_start_started_at = time.monotonic()
                await self._runpod_resume()

            elif status == "STARTING":
                log.info("Pod %s already STARTING; waiting", self.config.name)
                if self.state.cold_start_started_at is None:
                    self.state.cold_start_started_at = time.monotonic()

            else:
                # UNKNOWN or weird transient state — try a resume defensively
                log.warning("Pod %s in unexpected state %s; attempting podResume", self.config.name, status)
                self.state.cold_start_started_at = time.monotonic()
                try:
                    await self._runpod_resume()
                except Exception as e:
                    log.warning("podResume on %s failed (state=%s): %s", self.config.name, status, e)

            # Poll until /health returns 200 OR we time out. A freshly-
            # respawned pod gets a much longer budget because it has to
            # download the model and build llama.cpp from scratch (~5-7 min).
            started = self.state.cold_start_started_at or time.monotonic()
            self._ready_event.clear()
            timeout_s = (
                RESPAWN_COLD_START_TIMEOUT_S
                if self.state.last_respawn_at is not None
                and (time.time() - self.state.last_respawn_at) < RESPAWN_COLD_START_TIMEOUT_S
                else COLD_START_TIMEOUT_S
            )
            while True:
                elapsed = time.monotonic() - started
                if elapsed > timeout_s:
                    raise TimeoutError(f"Cold-start of {self.config.name} exceeded {timeout_s}s")

                # Refresh RunPod state every 30s while waiting
                if time.monotonic() - self.state.last_status_check_at > 30:
                    await self.fetch_status()

                if await self.probe_health(timeout=3.0):
                    self.state.cold_start_started_at = None
                    log.info("Pod %s ready after %.1fs", self.config.name, elapsed)
                    return

                await asyncio.sleep(STATUS_POLL_INTERVAL_S)

    async def stop(self, *, force: bool = False) -> str:
        """Stop the pod via RunPod API. Returns the resulting desiredStatus."""
        async with self._lock:
            status = await self.fetch_status()
            if status in ("EXITED", "STOPPED", "PAUSED") and not force:
                return status
            log.info("Stopping pod %s (current status=%s, idle=%.0fs)",
                     self.config.name, status, self.idle_for_s())
            await self._runpod_stop()
            self.state.server_ready = False
            self._ready_event.clear()
            await asyncio.sleep(1.0)
            return await self.fetch_status()

    # ── Status snapshot for /status endpoint ──────────────────────────────────

    def snapshot(self) -> dict:
        cold_start_elapsed = (
            time.monotonic() - self.state.cold_start_started_at
            if self.state.cold_start_started_at
            else None
        )
        return {
            "pod_id": self.config.pod_id,
            "slot_key": self.config.slot_key,
            "name": self.config.name,
            "desired_status": self.state.desired_status,
            "server_ready": self.state.server_ready,
            "idle_for_s": int(self.idle_for_s()),
            "idle_timeout_s": int(self.config.idle_timeout_s),
            "cold_start_elapsed_s": int(cold_start_elapsed) if cold_start_elapsed is not None else None,
            "api_base": self.config.api_base,
            "respawn_count": self.state.respawn_count,
            "max_respawns": self.config.max_respawns,
            "last_respawn_at": self.state.last_respawn_at,
            "last_respawn_reason": self.state.last_respawn_reason,
            "respawnable": self.config.template is not None,
        }


class PodRegistry:
    """Holds all managed pods and exposes group operations.

    Pods are indexed by `slot_key` (a stable identity that doesn't change
    when the pod gets respawned to a new pod_id). For backward compat we
    accept either slot_key or current pod_id in `get()`.

    If `state_path` is provided, slot→pod_id mappings are persisted to disk
    so a gateway restart picks up the latest pod_id (e.g. after auto-respawn).
    """

    def __init__(
        self,
        api_key: str,
        state_path: Optional[str] = None,
        post_respawn_hook: Optional["Callable[[PodManager], None]"] = None,  # type: ignore[name-defined]
    ):
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._pods: dict[str, PodManager] = {}                 # by slot_key
        self._by_pod_id: dict[str, PodManager] = {}            # by current pod_id
        self._by_api_base: dict[str, PodManager] = {}          # by upstream origin
        self._state_path: Optional[Path] = Path(state_path) if state_path else None
        self._state: dict[str, dict] = self._load_state()      # {slot_key: {pod_id, respawn_count, ...}}
        # Caller-supplied hook fired AFTER built-in re-indexing/persistence on
        # respawn. Use it to keep external state (e.g. LiteLLM config file)
        # in sync with the new pod_id.
        self._post_respawn_hook = post_respawn_hook

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, dict]:
        if not self._state_path or not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text())
            slots = data.get("slots", {})
            log.info("Loaded pod state for %d slot(s) from %s", len(slots), self._state_path)
            return slots
        except Exception as e:
            log.warning("Failed to load pod state from %s: %s (starting fresh)", self._state_path, e)
            return {}

    def _persist_state(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"slots": {}}
            for slot_key, mgr in self._pods.items():
                data["slots"][slot_key] = {
                    "pod_id": mgr.config.pod_id,
                    "respawn_count": mgr.state.respawn_count,
                    "last_respawn_at": mgr.state.last_respawn_at,
                    "last_respawn_reason": mgr.state.last_respawn_reason,
                }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(self._state_path)
        except Exception as e:
            log.warning("Failed to persist pod state to %s: %s", self._state_path, e)

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, config: PodConfig, slot_key: Optional[str] = None) -> PodManager:
        """Register a pod under a stable slot key.

        If a saved state file has a different (newer) pod_id for this slot —
        e.g. the previous gateway run auto-respawned — apply it now so we
        route to the live pod, not the original-but-dead one.
        """
        slot_key = slot_key or config.pod_id   # default: original pod_id is the slot
        config.slot_key = slot_key

        # Apply persisted overrides (auto-respawn from previous run).
        saved = self._state.get(slot_key)
        if saved and saved.get("pod_id") and saved["pod_id"] != config.pod_id:
            old = config.pod_id
            new = saved["pod_id"]
            log.info(
                "Slot %s: applying persisted pod_id override %s -> %s (respawn_count=%s)",
                slot_key, old, new, saved.get("respawn_count"),
            )
            config.api_base = _swap_pod_id_in_url(config.api_base, old, new)
            config.health_url = _swap_pod_id_in_url(config.health_url, old, new)
            config.pod_id = new

        mgr = PodManager(
            config, self._api_key, self._http,
            on_respawn=self._on_pod_respawn,
        )
        # Restore counters so the cap survives gateway restarts.
        if saved:
            mgr.state.respawn_count = int(saved.get("respawn_count") or 0)
            mgr.state.last_respawn_at = saved.get("last_respawn_at")
            mgr.state.last_respawn_reason = saved.get("last_respawn_reason")

        self._pods[slot_key] = mgr
        self._by_pod_id[config.pod_id] = mgr
        self._by_api_base[_origin(config.api_base)] = mgr
        # Persist on every registration so the state file reflects current
        # slot→pod_id even before any respawn happens. Makes the file usable
        # as a debugging snapshot ("which pod_id is the gateway currently
        # routing this slot to?").
        self._persist_state()
        return mgr

    def _on_pod_respawn(self, mgr: PodManager) -> None:
        """Called from PodManager._respawn after pod_id has been swapped.
        Re-index secondary lookups and persist new state."""
        # Drop any stale by-pod_id / by-api_base entries that pointed at this manager.
        self._by_pod_id = {pid: m for pid, m in self._by_pod_id.items() if m is not mgr}
        self._by_api_base = {ob: m for ob, m in self._by_api_base.items() if m is not mgr}
        # Re-add under the new pod_id / new api_base.
        self._by_pod_id[mgr.config.pod_id] = mgr
        self._by_api_base[_origin(mgr.config.api_base)] = mgr
        self._persist_state()
        # External hook (e.g. rewrite + reload LiteLLM config). Failures
        # logged but never propagated — we always want re-indexing to succeed.
        if self._post_respawn_hook is not None:
            try:
                self._post_respawn_hook(mgr)
            except Exception as e:
                log.warning("post_respawn_hook failed: %s", e)

    # ── Lookups ──────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[PodManager]:
        """Look up a manager by slot_key (preferred) or by current pod_id."""
        return self._pods.get(key) or self._by_pod_id.get(key)

    def get_by_url(self, url: str) -> Optional[PodManager]:
        return self._by_api_base.get(_origin(url))

    def all(self) -> list[PodManager]:
        return list(self._pods.values())

    async def aclose(self) -> None:
        await self._http.aclose()


def _origin(url: str) -> str:
    """Return scheme://host[:port] for index lookup."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}"


def _swap_pod_id_in_url(url: str, old_pod_id: str, new_pod_id: str) -> str:
    """RunPod proxy URLs are of the form `https://<pod_id>-<port>.proxy.runpod.net/...`.
    When we respawn a pod onto a new id, the URL host changes accordingly.
    Use a strict replace of `old_pod_id-` to avoid accidentally substituting
    a substring that happens to match elsewhere in the URL (e.g. in a path)."""
    return url.replace(f"{old_pod_id}-", f"{new_pod_id}-", 1)


async def idle_watcher(registry: PodRegistry) -> None:
    """Background task: stops pods that have been idle past their threshold.

    Runs forever. Catches/logs any exception per pod so a single failure
    doesn't kill the loop.
    """
    log.info("Idle watcher started (interval=%ds)", IDLE_WATCHER_INTERVAL_S)
    while True:
        try:
            for pod in registry.all():
                try:
                    # Refresh RunPod status + /health on every tick so the
                    # /llm/status endpoint and the wake-up fast-path always have
                    # accurate state without needing an inbound request first.
                    status = await pod.fetch_status()
                    if status == "RUNNING":
                        await pod.probe_health(timeout=3.0)

                    idle = pod.idle_for_s()
                    if idle < pod.config.idle_timeout_s:
                        continue
                    if status != "RUNNING":
                        continue
                    log.info("Pod %s idle for %.0fs (>%.0fs); auto-stopping",
                             pod.config.name, idle, pod.config.idle_timeout_s)
                    await pod.stop()
                except Exception as e:
                    log.warning("Idle watcher: failed checking pod %s: %s",
                                pod.config.name, e)
        except Exception as e:
            log.exception("Idle watcher loop error: %s", e)
        await asyncio.sleep(IDLE_WATCHER_INTERVAL_S)
