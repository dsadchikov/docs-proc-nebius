#!/bin/bash
set -e

echo "[start.sh] Starting Nebius Document Recognition (minimal: uvicorn on :8080, vLLM in background)..."
echo "[start.sh] GPU_ENABLED=${GPU_ENABLED:-1} MOCK_VLLM=${MOCK_VLLM:-0}"

if [ "${GPU_ENABLED:-1}" != "0" ]; then
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader || true
fi

# Launch vLLM in the BACKGROUND so it does NOT block the container port.
# The readiness probe hits FastAPI on :8080, which comes up in seconds; vLLM
# warms up in parallel and its status is reported by /health. This decouples
# port-readiness from model-load — the cause of the v24 STARTING/code=13 hang.
if [ "${GPU_ENABLED:-1}" != "0" ] && [ "${MOCK_VLLM:-0}" != "1" ]; then
    echo "[start.sh] Launching vLLM on :8000 in background with restart-on-crash supervision..."
    # Self-healing loop: if vLLM exits (OOM, crash), restart it after a short
    # backoff instead of leaving /recognize permanently degraded. uvicorn stays
    # PID 1; /health reports vLLM down during the gap.
    (
        while true; do
            python3 -m vllm.entrypoints.openai.api_server \
                --model /models/Qwen2.5-VL-7B-Instruct \
                --served-model-name "Qwen2.5-VL-7B-Instruct" \
                --host 127.0.0.1 --port 8000 \
                --dtype bfloat16 --max-model-len 32768 \
                --limit-mm-per-prompt image=1,video=0 \
                --gpu-memory-utilization 0.85 \
                --trust-remote-code
            echo "[start.sh] vLLM exited (code $?). Restarting in 5s..."
            sleep 5
        done
    ) &
    echo "[start.sh] vLLM supervisor PID=$! (background)"
else
    echo "[start.sh] Skipping vLLM (GPU disabled or mock)"
fi

# FastAPI on 0.0.0.0:8080 as the foreground/PID-1 process — opens the container
# port immediately so the Nebius readiness probe passes and logs start flowing.
echo "[start.sh] Starting FastAPI (uvicorn) on 0.0.0.0:8080..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8080
