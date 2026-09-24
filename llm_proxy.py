"""Wake-aware reverse proxy for LiteLLM that auto-starts/stops RunPod GPU pods.

Sits between the public /llm/* path and the local LiteLLM proxy. Inspects each
chat completion request to figure out which RunPod pod it targets, and:

  - On every request, marks the pod as "active" (delays the auto-stop timer).
  - If the pod is asleep, calls RunPod podResume and waits for /health = 200.
  - For streaming requests, sends `reasoning_content` SSE chunks during cold
    start so the client (Cursor/pi) sees live progress and doesn't time out.
  - For non-streaming requests, just blocks until the pod is ready, then forwards.

Public endpoints exposed by `make_status_routes`:
  - GET  /llm/status         JSON snapshot of all pods
  - POST /llm/wake?pod=ID    Manually wake a pod
  - POST /llm/sleep?pod=ID   Manually stop a pod
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from pod_manager import PodConfig, PodManager, PodRegistry, PodTemplate

log = logging.getLogger("llm_proxy")

# ── Pod configuration ────────────────────────────────────────────────────────
#
# Pods and the model names routed to them are loaded from a JSON file
# (LLM_PODS_CONFIG, default /app/llm-pods.json). See llm-pods.example.json.
# If the file is missing, the proxy runs with no pods and simply forwards
# everything to LiteLLM.
#
# Each pod entry:
#   slot_key        stable routing identity (survives auto-respawn onto a new pod_id)
#   pod_id          the currently live RunPod pod id
#   name            human-friendly label
#   api_base        e.g. https://<pod_id>-8080.proxy.runpod.net/v1
#   health_url      e.g. https://<pod_id>-8080.proxy.runpod.net/health
#   models          LiteLLM model names that should wake this pod
#   idle_timeout_s  auto-stop after this many idle seconds (default 3600)
#   max_respawns    auto-respawn budget (default from pod_manager)
#   template        optional PodTemplate fields for auto-respawn. If it has an
#                   "entrypoint_path", that script is base64-wrapped into
#                   docker_args. Env values of the form "${VAR}" are read from
#                   the gateway's environment at load time.
#
# When a slot respawns, the entrypoint script and env must describe the same
# server as the pod it replaces.

import base64 as _b64
import os as _os
from pathlib import Path as _Path

_PODS_CONFIG_PATH = _Path(_os.environ.get("LLM_PODS_CONFIG", "/app/llm-pods.json"))
_ENV_REF_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def _expand_env(value: Any) -> str:
    value = str(value)
    m = _ENV_REF_RE.match(value)
    if m:
        return _os.environ.get(m.group(1), "")
    return value


def _build_template(raw: Optional[dict], pod_name: str) -> Optional[PodTemplate]:
    """Build a PodTemplate from config. Returns None (auto-respawn disabled)
    if there's no template or its entrypoint script can't be read — we'd
    rather lose auto-respawn than crash the gateway on boot."""
    if not raw:
        return None
    raw = dict(raw)
    entrypoint_path = raw.pop("entrypoint_path", None)
    if entrypoint_path and "docker_args" not in raw:
        try:
            script = _Path(entrypoint_path).read_text()
        except Exception as e:
            log.warning("auto-respawn disabled for %s: can't read %s (%s)", pod_name, entrypoint_path, e)
            return None
        b64 = _b64.b64encode(script.encode()).decode()
        raw["docker_args"] = (
            f"bash -lc 'echo {b64} | base64 -d > /entrypoint.sh "
            f"&& chmod +x /entrypoint.sh && exec /entrypoint.sh'"
        )
    raw["env"] = {k: _expand_env(v) for k, v in (raw.get("env") or {}).items()}
    try:
        return PodTemplate(**raw)
    except TypeError as e:
        log.warning("auto-respawn disabled for %s: bad template (%s)", pod_name, e)
        return None


def _load_pod_configs() -> tuple[list[PodConfig], dict[str, str]]:
    if not _PODS_CONFIG_PATH.exists():
        log.info("No LLM pod config at %s; wake-aware routing disabled", _PODS_CONFIG_PATH)
        return [], {}
    try:
        data = json.loads(_PODS_CONFIG_PATH.read_text())
    except Exception as e:
        log.warning("Failed to parse %s: %s; wake-aware routing disabled", _PODS_CONFIG_PATH, e)
        return [], {}

    configs: list[PodConfig] = []
    model_to_pod: dict[str, str] = {}
    for entry in data.get("pods", []):
        slot_key = entry.get("slot_key") or entry["pod_id"]
        kwargs = dict(
            pod_id=entry["pod_id"],
            slot_key=slot_key,
            name=entry.get("name", slot_key),
            api_base=entry["api_base"],
            health_url=entry["health_url"],
            gpu_count=int(entry.get("gpu_count", 1)),
            idle_timeout_s=float(entry.get("idle_timeout_s", 3600.0)),
            template=_build_template(entry.get("template"), entry.get("name", slot_key)),
        )
        if "max_respawns" in entry:
            kwargs["max_respawns"] = int(entry["max_respawns"])
        configs.append(PodConfig(**kwargs))
        for model in entry.get("models", []):
            model_to_pod[model] = slot_key
    return configs, model_to_pod


# Model name (as exposed via LiteLLM) → SLOT KEY (not pod_id). Slot keys are
# stable across auto-respawns, so this mapping never changes when pods rotate.
POD_CONFIGS, MODEL_TO_POD = _load_pod_configs()

# Hop-by-hop headers we don't forward
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

_CHATCMPL_ID_RE = re.compile(rb'"id"\s*:\s*"chatcmpl-[^"]*"')


def build_pod_registry(
    api_key: str,
    state_path: Optional[str] = None,
) -> PodRegistry:
    """Construct a PodRegistry seeded with the configured pods.

    `state_path` enables persistence of slot→pod_id mappings, which lets
    auto-respawned pod_ids survive a gateway restart instead of reverting to
    the (now-dead) original pod_id from the pod config file.

    On auto-respawn we ALSO need to keep LiteLLM's `litellm-config.yaml` in
    sync — LiteLLM holds api_base statically, so without rewriting it the
    pod gets respawned to a new URL but LiteLLM keeps forwarding to the dead
    one. `_litellm_post_respawn_hook` handles that rewrite + restart.
    """
    reg = PodRegistry(
        api_key,
        state_path=state_path,
        post_respawn_hook=_litellm_post_respawn_hook,
    )
    for cfg in POD_CONFIGS:
        # Each config carries its own slot_key (a stable identity). MODEL_TO_POD
        # uses these slot keys, so routing survives auto-respawns that change
        # the underlying pod_id.
        reg.register(cfg, slot_key=cfg.slot_key or cfg.pod_id)
    return reg


# ── LiteLLM config sync on respawn ───────────────────────────────────────────
#
# After PodRegistry rotates a slot to a new pod_id, the in-memory routing in
# the wake-aware proxy uses the new URL — but LiteLLM (the downstream OpenAI
# server we forward chat completions to) loaded its `api_base` from
# litellm-config.yaml at startup and won't update on its own. So we:
#   1. Rewrite the YAML in place (only model entries belonging to this slot)
#   2. Restart LiteLLM via supervisorctl so it picks up the new URL
#
# Both paths/cmds are env-overridable for testability and to no-op outside
# the supervisord container.

import shutil as _shutil
import subprocess as _subprocess


def _slot_to_model_names(slot_key: str) -> list[str]:
    return [name for name, slot in MODEL_TO_POD.items() if slot == slot_key]


def _litellm_post_respawn_hook(mgr) -> None:  # PodManager
    """Rewrite litellm-config.yaml api_base for this slot and restart LiteLLM.

    Best-effort. Failure to rewrite the config is logged but doesn't unwind
    the respawn — worst case the gateway still routes correctly internally,
    LiteLLM 502s for a bit, and the next deploy fixes the static config.
    """
    cfg_path = _Path(_os.environ.get("LITELLM_CONFIG_PATH", "/app/litellm-config.yaml"))
    if not cfg_path.exists():
        log.info("LiteLLM config not found at %s; skipping respawn rewrite", cfg_path)
        return

    slot_key = mgr.config.slot_key
    new_api_base = mgr.config.api_base
    target_models = set(_slot_to_model_names(slot_key))
    if not target_models:
        log.info("Slot %s has no LiteLLM models to update; skipping rewrite", slot_key)
        return

    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("PyYAML missing; can't hot-rewrite LiteLLM config on respawn")
        return

    try:
        data = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception as e:
        log.warning("Failed to parse %s for respawn rewrite: %s", cfg_path, e)
        return

    changed = 0
    for entry in data.get("model_list", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("model_name") in target_models:
            params = entry.setdefault("litellm_params", {})
            if params.get("api_base") != new_api_base:
                params["api_base"] = new_api_base
                changed += 1

    if changed == 0:
        log.info("LiteLLM config already up-to-date for slot %s", slot_key)
        return

    try:
        tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
        # default_flow_style=False = block style (readable diffs in git)
        tmp.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
        tmp.replace(cfg_path)
    except Exception as e:
        log.warning("Failed to write updated LiteLLM config: %s", e)
        return

    log.info(
        "Rewrote %s: %d model entry(s) in slot %s now point at %s",
        cfg_path, changed, slot_key, new_api_base,
    )

    restart_cmd = _os.environ.get(
        "LITELLM_RESTART_CMD",
        "supervisorctl restart litellm",
    )
    if not restart_cmd or restart_cmd.lower() == "none":
        log.info("LITELLM_RESTART_CMD disabled; skipping reload")
        return

    binary = restart_cmd.split()[0]
    if not _shutil.which(binary):
        log.info("Restart binary %s not on PATH; skipping reload (config still rewritten)", binary)
        return

    try:
        result = _subprocess.run(
            restart_cmd, shell=True, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning(
                "LiteLLM restart failed (exit %s): stdout=%s stderr=%s",
                result.returncode, result.stdout.strip(), result.stderr.strip(),
            )
        else:
            log.info("LiteLLM restarted to pick up new api_base for slot %s", slot_key)
    except Exception as e:
        log.warning("LiteLLM restart errored: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _peek_request_body(body: bytes) -> tuple[Optional[str], bool]:
    """Best-effort: extract `model` and `stream` from a JSON chat-completions body.

    Returns (model_name, is_streaming). On parse failure: (None, False).
    """
    if not body:
        return None, False
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    model = data.get("model") if isinstance(data.get("model"), str) else None
    stream = bool(data.get("stream"))
    return model, stream


def _sse_chunk(msg_id: str, model: str, *, reasoning: str = "", content: str = "") -> bytes:
    """Build a single SSE `data: {...}\\n\\n` line in OpenAI chunk format."""
    delta: dict[str, Any] = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    payload = {
        "id": msg_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def _sse_done(msg_id: str, model: str, finish_reason: str = "stop") -> bytes:
    payload = {
        "id": msg_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode("utf-8")


def _strip_request_headers(scope: Scope) -> dict[str, str]:
    return {
        k.decode("latin-1"): v.decode("latin-1")
        for k, v in scope.get("headers", [])
        if k.decode("latin-1").lower() not in _HOP_BY_HOP
    }


# ── Wake-aware proxy ─────────────────────────────────────────────────────────


def make_proxy_asgi(
    registry: PodRegistry,
    litellm_proxy_url: str,
) -> Callable[[Scope, Receive, Send], Any]:
    """Build an ASGI app that proxies /llm/* to the local LiteLLM, with wake-up logic."""

    litellm_proxy_url = litellm_proxy_url.rstrip("/")
    client = httpx.AsyncClient(
        base_url=litellm_proxy_url,
        timeout=httpx.Timeout(600.0, connect=10.0),
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await JSONResponse({"error": "unsupported scope"}, status_code=400)(scope, receive, send)
            return

        # Normalize the path: Starlette's Mount strips /llm in some versions but
        # not all — handle both cases defensively.
        path = scope.get("path", "/") or "/"
        if path.startswith("/llm/"):
            path = path[len("/llm"):]
        elif path == "/llm":
            path = "/"
        if not path.startswith("/"):
            path = "/" + path

        raw_qs = scope.get("query_string", b"") or b""
        url = path + (("?" + raw_qs.decode("latin-1")) if raw_qs else "")

        upstream_headers = _strip_request_headers(scope)

        # Buffer the request body fully so we can peek at the model field.
        # For chat completions this is small (<1MB) — fine to hold in memory.
        body_chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"") or b""
                if chunk:
                    body_chunks.append(chunk)
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                return
        body = b"".join(body_chunks)

        # Only wake-on-demand for chat/completions endpoints. Status/health
        # passthroughs forward immediately without waking anything.
        is_chat_completion = (
            path.endswith("/chat/completions")
            or path.endswith("/completions")
            or path.endswith("/embeddings")
        )

        target_pod: Optional[PodManager] = None
        is_stream = False
        model_name: Optional[str] = None

        if is_chat_completion:
            model_name, is_stream = _peek_request_body(body)
            if model_name:
                pod_id = MODEL_TO_POD.get(model_name)
                if pod_id:
                    target_pod = registry.get(pod_id)
            # Always update activity even if we couldn't pin the pod.
            if target_pod:
                target_pod.mark_activity()

        # If the target pod is asleep / not ready, kick off wake-up.
        needs_warmup = bool(target_pod) and not (
            target_pod.state.server_ready and target_pod.state.desired_status == "RUNNING"
        )
        # Cheap optimisation: if the pod *thinks* it's running but we just haven't
        # confirmed health yet, do a fast probe before triggering the warmup UI.
        # This avoids showing "Cold-starting" banners when the pod is already up.
        if needs_warmup and target_pod and target_pod.state.desired_status == "RUNNING":
            try:
                if await target_pod.probe_health(timeout=2.0):
                    needs_warmup = False
            except Exception:
                pass

        if needs_warmup and is_stream and model_name:
            await _stream_warmup_then_forward(
                receive, send, target_pod, model_name, client, scope["method"], url,
                upstream_headers, body,
            )
            return

        if needs_warmup and target_pod:
            try:
                await target_pod.ensure_running()
            except TimeoutError as e:
                await JSONResponse(
                    {"error": "cold_start_timeout", "detail": str(e)},
                    status_code=504,
                )(scope, receive, send)
                return
            except Exception as e:
                await JSONResponse(
                    {"error": "wake_failed", "detail": str(e)},
                    status_code=502,
                )(scope, receive, send)
                return

        # Pod ready (or this isn't a chat request) — forward through.
        await _forward(client, receive, send, scope["method"], url, upstream_headers, body)

    return app


async def _watch_disconnect(receive: Receive, disconnected: asyncio.Event) -> None:
    """Background task: drain ASGI receive() and flip the event on disconnect.

    Without this, the framework's `http.disconnect` message just sits in the
    receive queue forever and we only notice the cancellation on the next
    `await send(...)` (which can be many seconds during slow upstream streams).
    """
    try:
        while not disconnected.is_set():
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                disconnected.set()
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        # If receive raises, treat as disconnect — better safe than sorry.
        disconnected.set()


async def _forward(
    client: httpx.AsyncClient,
    receive: Receive,
    send: Send,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Forward a request to LiteLLM and stream the response back (no rewriting).

    Actively monitors `receive()` for `http.disconnect` so client cancellation
    propagates immediately to the upstream connection (and from there to
    LiteLLM → llama-server's slot which checks for client disconnect between
    decode batches).
    """
    req = client.build_request(method=method, url=url, headers=headers, content=body or None)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await _send_error(send, 502, "upstream unreachable", str(exc))
        return

    disconnected = asyncio.Event()
    watch_task = asyncio.create_task(_watch_disconnect(receive, disconnected))
    try:
        resp_headers = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP
        ]
        await send({"type": "http.response.start", "status": upstream.status_code, "headers": resp_headers})
        async for chunk in upstream.aiter_raw():
            if disconnected.is_set():
                log.info("Client disconnected mid-stream; aborting upstream forward (%s)", url)
                break
            try:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            except Exception:
                disconnected.set()
                break
        if not disconnected.is_set():
            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            except Exception:
                pass
    finally:
        watch_task.cancel()
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):
            pass
        # aclose() actively tears down the upstream HTTP connection — this is
        # what eventually causes llama-server to detect the disconnect and
        # release its slot.
        await upstream.aclose()


async def _stream_warmup_then_forward(
    receive: Receive,
    send: Send,
    pod: PodManager,
    model_name: str,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Open SSE response immediately, stream warmup chunks, then forward upstream."""
    msg_id = f"chatcmpl-warm-{uuid.uuid4().hex[:12]}"

    # Send response headers right away — this is what keeps the client connected.
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"cache-control", b"no-cache"),
            (b"connection", b"keep-alive"),
            (b"x-accel-buffering", b"no"),
        ],
    })

    started = time.monotonic()
    disconnected = asyncio.Event()
    watch_task = asyncio.create_task(_watch_disconnect(receive, disconnected))

    async def emit(text: str) -> None:
        await send({
            "type": "http.response.body",
            "body": _sse_chunk(msg_id, model_name, reasoning=text),
            "more_body": True,
        })

    # Kick off the wake in the background so we can keep emitting progress.
    wake_task = asyncio.create_task(pod.ensure_running())

    await emit(f"⏳ Cold-starting **{pod.config.name}** GPU on RunPod (pod {pod.config.pod_id})…\n")

    last_status: Optional[str] = None
    last_msg_at = time.monotonic()
    failed = False
    failure_msg = ""

    try:
        while not wake_task.done():
            if disconnected.is_set():
                log.info("Client disconnected during warmup; cancelling wake task")
                wake_task.cancel()
                watch_task.cancel()
                return
            await asyncio.sleep(2.0)
            elapsed = int(time.monotonic() - started)
            status = pod.state.desired_status
            if status != last_status:
                if status == "STARTING":
                    await emit(f"🟢 RunPod accepted resume; machine booting ({elapsed}s)…\n")
                elif status == "RUNNING" and not pod.state.server_ready:
                    await emit(f"🚀 Machine up; waiting for llama-server ({elapsed}s)…\n")
                last_status = status
                last_msg_at = time.monotonic()
            elif time.monotonic() - last_msg_at > 8:
                # Periodic heartbeat so client never sees silence
                await emit(f"⏳ Still warming… ({elapsed}s elapsed)\n")
                last_msg_at = time.monotonic()

        # wake_task complete — check outcome
        try:
            wake_task.result()
        except TimeoutError as e:
            failed = True
            failure_msg = f"Cold start timed out after {int(time.monotonic() - started)}s: {e}"
        except Exception as e:
            failed = True
            failure_msg = f"Wake-up failed: {e}"
    except asyncio.CancelledError:
        wake_task.cancel()
        watch_task.cancel()
        raise

    if failed:
        # Surface the failure as an assistant message so the user sees it
        await send({
            "type": "http.response.body",
            "body": _sse_chunk(msg_id, model_name, content=f"\n\n❌ {failure_msg}"),
            "more_body": True,
        })
        await send({
            "type": "http.response.body",
            "body": _sse_done(msg_id, model_name, finish_reason="stop"),
            "more_body": False,
        })
        return

    elapsed = int(time.monotonic() - started)
    await emit(f"✅ Ready in {elapsed}s — forwarding your request now.\n\n")

    # Now make the upstream call, rewriting chatcmpl ids in each chunk so the
    # client sees one continuous response.
    msg_id_bytes = f'"id":"{msg_id}"'.encode("utf-8")
    req = client.build_request(method=method, url=url, headers=headers, content=body or None)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await send({
            "type": "http.response.body",
            "body": _sse_chunk(msg_id, model_name, content=f"\n\n❌ Upstream unreachable: {exc}"),
            "more_body": True,
        })
        await send({"type": "http.response.body", "body": _sse_done(msg_id, model_name), "more_body": False})
        return

    try:
        if upstream.status_code != 200:
            err_body = b""
            async for c in upstream.aiter_raw():
                err_body += c
                if len(err_body) > 4096:
                    break
            await send({
                "type": "http.response.body",
                "body": _sse_chunk(msg_id, model_name, content=f"\n\n❌ Upstream returned {upstream.status_code}: {err_body.decode(errors='replace')[:1000]}"),
                "more_body": True,
            })
            await send({"type": "http.response.body", "body": _sse_done(msg_id, model_name), "more_body": False})
            return

        async for chunk in upstream.aiter_raw():
            if disconnected.is_set():
                log.info("Client disconnected mid-stream; aborting warmup forward")
                break
            rewritten = _CHATCMPL_ID_RE.sub(msg_id_bytes, chunk)
            try:
                await send({"type": "http.response.body", "body": rewritten, "more_body": True})
            except Exception:
                disconnected.set()
                break
        if not disconnected.is_set():
            try:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            except Exception:
                pass
    finally:
        watch_task.cancel()
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):
            pass
        await upstream.aclose()


async def _send_error(send: Send, status: int, code: str, detail: str) -> None:
    body = json.dumps({"error": code, "detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


# ── Status / wake / sleep endpoints ──────────────────────────────────────────


def make_status_routes(registry: PodRegistry) -> list[Route]:

    async def status(request: Request) -> Response:
        pods = []
        for mgr in registry.all():
            # Cheap check: only re-poll status if it's stale (>30s)
            if time.monotonic() - mgr.state.last_status_check_at > 30:
                try:
                    await mgr.fetch_status()
                except Exception:
                    pass
            pods.append(mgr.snapshot())
        return JSONResponse({"pods": pods})

    async def wake(request: Request) -> Response:
        pod_id = request.query_params.get("pod") or request.query_params.get("pod_id")
        if not pod_id:
            return JSONResponse({"error": "missing ?pod=<id>"}, status_code=400)
        mgr = registry.get(pod_id)
        if not mgr:
            return JSONResponse({"error": f"unknown pod_id {pod_id}"}, status_code=404)
        wait = (request.query_params.get("wait", "true").lower() != "false")
        try:
            if wait:
                await mgr.ensure_running()
            else:
                # Fire-and-forget wake; don't block the request
                asyncio.create_task(mgr.ensure_running())
        except Exception as e:
            return JSONResponse({"pod": mgr.snapshot(), "error": str(e)}, status_code=502)
        return JSONResponse({"pod": mgr.snapshot(), "ok": True})

    async def sleep(request: Request) -> Response:
        pod_id = request.query_params.get("pod") or request.query_params.get("pod_id")
        if not pod_id:
            return JSONResponse({"error": "missing ?pod=<id>"}, status_code=400)
        mgr = registry.get(pod_id)
        if not mgr:
            return JSONResponse({"error": f"unknown pod_id {pod_id}"}, status_code=404)
        try:
            new_status = await mgr.stop(force=True)
        except Exception as e:
            return JSONResponse({"pod": mgr.snapshot(), "error": str(e)}, status_code=502)
        return JSONResponse({"pod": mgr.snapshot(), "new_status": new_status})

    async def abort(request: Request) -> Response:
        """Emergency: full pod stop+resume cycle to drain a stuck request queue.

        Use when llama-server has a stuck slot or a pile of orphaned requests
        from cancelled-but-not-aborted streams. Returns immediately after the
        stop completes; the resume runs in the background and the next chat
        request will trigger the warmup-stream cold-start UI as usual.
        """
        pod_id = request.query_params.get("pod") or request.query_params.get("pod_id")
        if not pod_id:
            return JSONResponse({"error": "missing ?pod=<id>"}, status_code=400)
        mgr = registry.get(pod_id)
        if not mgr:
            return JSONResponse({"error": f"unknown pod_id {pod_id}"}, status_code=404)
        wait_resume = (request.query_params.get("wait", "false").lower() == "true")
        try:
            await mgr.stop(force=True)
        except Exception as e:
            return JSONResponse({"pod": mgr.snapshot(), "error": f"stop failed: {e}"}, status_code=502)
        # Kick off resume — don't block the abort call unless ?wait=true.
        if wait_resume:
            try:
                await mgr.ensure_running()
            except Exception as e:
                return JSONResponse({"pod": mgr.snapshot(), "error": f"resume failed: {e}"}, status_code=502)
        else:
            asyncio.create_task(mgr.ensure_running())
        return JSONResponse({
            "pod": mgr.snapshot(),
            "ok": True,
            "note": "queue drained; pod resume in progress (next chat will warm-stream)",
        })

    async def respawn(request: Request) -> Response:
        """Force-respawn a slot onto a fresh pod.

        Use when the current host is stuck (RunPod won't resume) and you don't
        want to wait for the next inbound request to trigger the auto-respawn
        path. Caller can pass ?reason=... for the audit trail. Returns
        immediately after RunPod accepts the deploy; the new pod still needs
        ~5-7 min for first-boot model download. Hit /llm/status to track.
        """
        pod_id = request.query_params.get("pod") or request.query_params.get("pod_id")
        if not pod_id:
            return JSONResponse({"error": "missing ?pod=<slot_or_pod_id>"}, status_code=400)
        mgr = registry.get(pod_id)
        if not mgr:
            return JSONResponse({"error": f"unknown pod {pod_id}"}, status_code=404)
        if mgr.config.template is None:
            return JSONResponse({
                "pod": mgr.snapshot(),
                "error": "this slot has no respawn template (not respawnable)",
            }, status_code=400)
        if mgr.state.respawn_count >= mgr.config.max_respawns:
            return JSONResponse({
                "pod": mgr.snapshot(),
                "error": f"respawn cap ({mgr.config.max_respawns}) reached; bump max_respawns to override",
            }, status_code=409)
        reason = request.query_params.get("reason", "manual /llm/respawn")
        # Run respawn under the manager's lock so it can't race with a concurrent
        # ensure_running().
        try:
            async with mgr._lock:
                await mgr._respawn(reason=reason)
        except Exception as e:
            return JSONResponse({"pod": mgr.snapshot(), "error": f"respawn failed: {e}"}, status_code=502)
        return JSONResponse({
            "pod": mgr.snapshot(),
            "ok": True,
            "note": "new pod provisioning; ~5-7 min cold start. Old pod is being terminated.",
        })

    return [
        Route("/llm/status",  status,  methods=["GET"]),
        Route("/llm/wake",    wake,    methods=["POST", "GET"]),
        Route("/llm/sleep",   sleep,   methods=["POST"]),
        Route("/llm/abort",   abort,   methods=["POST"]),
        Route("/llm/respawn", respawn, methods=["POST"]),
    ]
