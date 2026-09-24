#!/usr/bin/env bash
# Generic RunPod entrypoint for serving a GGUF model with llama-server.
#
# Required env: MODEL_REPO, MODEL_FILE (Hugging Face repo + GGUF filename)
# Optional env: CHAT_TEMPLATE_REPO / CHAT_TEMPLATE_FILE, MODEL_ALIAS, CTX_SIZE,
#               PORT, LLAMA_API_KEY, USE_YARN (+ YARN_SCALE, YARN_ORIG_CTX),
#               REASONING_FORMAT, LLAMA_TEMP / LLAMA_TOP_P / LLAMA_TOP_K,
#               PARALLEL, CACHE_TYPE_K / CACHE_TYPE_V, PUBLIC_KEY (ssh)
#
# Protective entrypoint: sshd first, bootstrap in background with tee'd logs,
# sleep-forever on failure so the container never crash-loops. SSH in and
# tail $LOG to watch progress or diagnose.
set -u

: "${MODEL_CACHE:=/cache}"
LOG="$MODEL_CACHE/boot.log"
mkdir -p "$MODEL_CACHE"

# nvcc lives outside default PATH on runpod/pytorch images
export PATH="/usr/local/cuda/bin:$PATH"

log() { echo "[wrapper $(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# -- SSH (always, first) --------------------------------------------------
if [[ -n "${PUBLIC_KEY:-}" ]]; then
    log "ssh: writing authorized_keys"
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

export DEBIAN_FRONTEND=noninteractive
log "apt: installing openssh-server"
apt-get update -qq >>"$LOG" 2>&1 || log "apt-get update failed"
apt-get install -y -qq --no-install-recommends openssh-server >>"$LOG" 2>&1 || log "apt install ssh failed"
mkdir -p /run/sshd
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config || true
/usr/sbin/sshd && log "sshd started" || log "sshd failed to start"

# -- Pause-for-debug marker ------------------------------------------------
if [[ -f "$MODEL_CACHE/pause" ]]; then
    log "PAUSE marker present — sleeping, no bootstrap"
    exec sleep infinity
fi

# -- Bootstrap in background ----------------------------------------------
log "starting bootstrap (tail -f $LOG to watch)"
(
    set -euo pipefail

    : "${MODEL_REPO:?MODEL_REPO is required (Hugging Face repo id)}"
    : "${MODEL_FILE:?MODEL_FILE is required (GGUF filename in MODEL_REPO)}"
    : "${CHAT_TEMPLATE_REPO:=}"
    : "${CHAT_TEMPLATE_FILE:=chat_template.jinja}"
    : "${MODEL_ALIAS:=${MODEL_FILE%.gguf}}"
    : "${CTX_SIZE:=16384}"
    : "${PORT:=8080}"
    # Forced rebuild key — bump to discard any stale cached llama.cpp build.
    # v3 adds the server-context.cpp slot-cap patch (required for YaRN
    # extension above n_ctx_train).
    : "${LLAMA_BUILD_TAG:=v3-cuda12-sm-auto-slotpatch}"

    LLAMA_DIR="$MODEL_CACHE/llama.cpp"
    BUILD_TAG_FILE="$LLAMA_DIR/.build-tag"
    MODEL_PATH="$MODEL_CACHE/model/$MODEL_FILE"
    CHAT_TEMPLATE="$MODEL_CACHE/model/$CHAT_TEMPLATE_FILE"
    LLAMA_SERVER="$LLAMA_DIR/build/bin/llama-server"

    mkdir -p "$MODEL_CACHE/model"

    sub() { echo "[boot $(date +%H:%M:%S)] $*"; }

    export HF_HUB_ENABLE_HF_TRANSFER=1
    export PYTHONUNBUFFERED=1

    sub "apt: installing build deps"
    apt-get install -y -qq --no-install-recommends \
        build-essential cmake git wget curl ca-certificates jq libcurl4-openssl-dev

    sub "pip: installing hf deps (PEP 668 bypass for system python)"
    pip install --quiet --break-system-packages --no-cache-dir \
        "huggingface_hub>=0.27" "hf_transfer>=0.1.9"

    # Detect GPU compute capability for explicit CUDA arch (avoid sm_75 default).
    # Examples: L40S=89, A100=80, H100=90, RTX 4090=89, RTX 5090=120.
    if command -v nvidia-smi >/dev/null; then
        CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader,nounits | head -1 | tr -d '. ')
    fi
    : "${CC:=89}"
    sub "GPU compute capability: $CC -> CMAKE_CUDA_ARCHITECTURES=$CC"

    # llama.cpp build (cached, but invalidated when LLAMA_BUILD_TAG changes)
    BUILT_TAG=""
    [[ -f "$BUILD_TAG_FILE" ]] && BUILT_TAG=$(cat "$BUILD_TAG_FILE")
    if [[ ! -x "$LLAMA_SERVER" || "$BUILT_TAG" != "$LLAMA_BUILD_TAG" ]]; then
        sub "Building llama.cpp with CUDA + sm_$CC (one-time, ~6 min)"
        rm -rf "$LLAMA_DIR"
        git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"

        # Patch out the hard "slot context > training context" cap in
        # server-context.cpp. We pass YaRN scaling args at startup to extend
        # the slot ctx beyond the model's native trained context, but
        # llama-server unconditionally clamps the slot to n_ctx_train. Removing
        # the clamp lets YaRN do its job. Idempotent (no-op if upstream
        # changes the line, in which case we fall back to capped behavior).
        sub "Patching server-context.cpp to remove slot ctx cap"
        python3 - "$LLAMA_DIR/tools/server/server-context.cpp" <<'PY' || sub "(slot-cap patch failed — slot ctx will be capped to n_ctx_train)"
import sys
from pathlib import Path
p = Path(sys.argv[1])
s = p.read_text()
old = '''        int n_ctx_slot = llama_n_ctx_seq(ctx);
        if (n_ctx_slot > n_ctx_train) {
            SRV_WRN("the slot context (%d) exceeds the training context of the model (%d) - capping\\n", n_ctx_slot, n_ctx_train);
            n_ctx_slot = n_ctx_train;
        }'''
new = '''        int n_ctx_slot = llama_n_ctx_seq(ctx);
        if (n_ctx_slot > n_ctx_train) {
            SRV_WRN("the slot context (%d) exceeds the training context of the model (%d) - keeping (cap removed by patch, expecting YaRN scaling)\\n", n_ctx_slot, n_ctx_train);
        }'''
if old in s:
    p.write_text(s.replace(old, new))
    print("[boot] patched server-context.cpp (removed slot ctx cap)")
else:
    print("[boot] server-context.cpp cap line not found — leaving as-is")
PY

        cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
            -DGGML_CUDA=ON \
            -DLLAMA_CURL=ON \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_CUDA_ARCHITECTURES="$CC"
        cmake --build "$LLAMA_DIR/build" --config Release -j "$(nproc)" --target llama-server
        echo "$LLAMA_BUILD_TAG" > "$BUILD_TAG_FILE"
        sub "llama-server built ($(du -h "$LLAMA_SERVER" | cut -f1))"
    else
        sub "llama-server cache hit (tag=$BUILT_TAG)"
    fi

    # Verify CUDA backend is actually usable before we bother loading the model
    if ! "$LLAMA_SERVER" --version 2>&1 | grep -qv "failed to initialize CUDA"; then
        : # fall through; --version always prints version even on cuda init fail
    fi
    "$LLAMA_SERVER" --version 2>&1 | head -5 || true

    # GGUF model. Cached.
    if [[ ! -f "$MODEL_PATH" ]]; then
        sub "Downloading GGUF $MODEL_REPO/$MODEL_FILE (one-time)"
        python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='$MODEL_REPO', filename='$MODEL_FILE', local_dir='$MODEL_CACHE/model')
"
    else
        sub "GGUF cache hit ($(du -h "$MODEL_PATH" | cut -f1))"
    fi

    # Optional chat template override. Cached.
    if [[ -n "$CHAT_TEMPLATE_REPO" && ! -f "$CHAT_TEMPLATE" ]]; then
        sub "Downloading chat template"
        python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='$CHAT_TEMPLATE_REPO', filename='$CHAT_TEMPLATE_FILE', local_dir='$MODEL_CACHE/model')
" || sub "(chat template download failed — falling back to GGUF-embedded template)"
    elif [[ -f "$CHAT_TEMPLATE" ]]; then
        sub "Chat template cache hit"
    fi

    touch "$MODEL_CACHE/.bootstrapped-v3"

    ARGS=(
        --model "$MODEL_PATH"
        --host 0.0.0.0
        --port "$PORT"
        --ctx-size "$CTX_SIZE"
        --n-gpu-layers 999
        --flash-attn on
        --jinja
        # Surface `<think>…</think>` as `message.reasoning_content` instead of
        # leaking it into `message.content`. Critical for clean roundtripping
        # with agent clients (Cursor, pi) that re-feed assistant history.
        --reasoning-format "${REASONING_FORMAT:-deepseek}"
        # Sampler defaults (env-overridable).
        --temp "${LLAMA_TEMP:-0.6}"
        --top-p "${LLAMA_TOP_P:-0.95}"
        --top-k "${LLAMA_TOP_K:-20}"
        # Single slot per pod — we want the full ctx-size for one big request,
        # not 4× smaller slots. Concurrency is achieved via multiple pods, not
        # multiple slots.
        --parallel "${PARALLEL:-1}"
        # KV cache quantisation — lets long contexts fit in VRAM.
        --cache-type-k "${CACHE_TYPE_K:-q8_0}"
        --cache-type-v "${CACHE_TYPE_V:-q8_0}"
        --metrics
        --alias "$MODEL_ALIAS"
        --fit off
    )

    # Optional YaRN context extension. Set USE_YARN=1 to extend ctx beyond
    # the model's native trained context; YARN_ORIG_CTX must be the model's
    # n_ctx_train. Quality degrades past ~1.5x — keep YARN_SCALE conservative.
    # NOTE: requires the server-context.cpp slot-cap patch (applied above).
    if [[ "${USE_YARN:-0}" == "1" ]]; then
        : "${YARN_SCALE:=1.25}"
        : "${YARN_ORIG_CTX:?YARN_ORIG_CTX is required when USE_YARN=1}"
        : "${YARN_EXT_FACTOR:=1.0}"
        sub "Enabling YaRN: scale=$YARN_SCALE orig_ctx=$YARN_ORIG_CTX ext_factor=$YARN_EXT_FACTOR (effective ctx=$CTX_SIZE)"
        ARGS+=(--rope-scaling yarn --rope-scale "$YARN_SCALE" --yarn-orig-ctx "$YARN_ORIG_CTX" --yarn-ext-factor "$YARN_EXT_FACTOR")
    fi

    [[ -f "$CHAT_TEMPLATE" ]] && ARGS+=(--chat-template-file "$CHAT_TEMPLATE")
    [[ -n "${LLAMA_API_KEY:-}" ]] && ARGS+=(--api-key "$LLAMA_API_KEY")

    sub "Starting llama-server"
    exec "$LLAMA_SERVER" "${ARGS[@]}"

) >> "$LOG" 2>&1 &

BOOT_PID=$!
log "bootstrap PID $BOOT_PID"

# Supervisor: keep container alive no matter what
wait "$BOOT_PID" || true
log "bootstrap exited — sleeping forever so you can SSH in and inspect $LOG"
exec sleep infinity
