#!/bin/bash
set -e

echo "[start.sh] Starting Nebius Document Recognition..."
echo "[start.sh] GPU_ENABLED=${GPU_ENABLED:-1}"
echo "[start.sh] MOCK_VLLM=${MOCK_VLLM:-0}"

if [ "${GPU_ENABLED:-1}" != "0" ]; then
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader || true
fi

if [ "${GPU_ENABLED:-1}" != "0" ] && [ "${MOCK_VLLM:-0}" != "1" ]; then
    echo "[start.sh] Starting vLLM on :8000..."
    python3 -m vllm.entrypoints.openai.api_server \
        --model /models/Qwen2.5-VL-7B-Instruct \
        --served-model-name "Qwen2.5-VL-7B-Instruct" \
        --host 127.0.0.1 --port 8000 \
        --dtype bfloat16 --max-model-len 32768 \
        --limit-mm-per-prompt image=1,video=0 \
        --gpu-memory-utilization 0.85 \
        --trust-remote-code &
    VLLM_PID=$!
    echo "[start.sh] Waiting for vLLM..."
    WAIT=0
    while ! curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; do
        sleep 2; WAIT=$((WAIT+1))
        if ! kill -0 $VLLM_PID 2>/dev/null; then echo "[start.sh] vLLM died!"; exit 1; fi
        if [ $WAIT -gt 450 ]; then echo "[start.sh] vLLM timeout!"; exit 1; fi
    done
    echo "[start.sh] vLLM ready! (${WAIT}x2s)"
else
    echo "[start.sh] Skipping vLLM (GPU disabled or mock)"
fi

echo "[start.sh] Starting FastAPI on :8081..."
uvicorn app.main:app --host 127.0.0.1 --port 8081 &
sleep 2

echo "[start.sh] Starting nginx on :8080..."
exec nginx -g 'daemon off;'
