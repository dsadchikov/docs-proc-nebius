FROM cr.eu-north1.nebius.cloud/e00h8fpjyqj1jdednb/base:v2

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx poppler-utils && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Download model weights at build time — avoids HF download on every cold start.
# HF_HUB_DISABLE_XET=1 uses plain HTTPS instead of the US-based xet CDN
# (which has SSL issues from Nebius EU-North1).
ARG HF_TOKEN=""
RUN HF_HUB_DISABLE_XET=1 HF_TOKEN=${HF_TOKEN} python3 -c "\
import os; \
from huggingface_hub import snapshot_download; \
snapshot_download( \
    'Qwen/Qwen2.5-VL-7B-Instruct', \
    local_dir='/models/Qwen2.5-VL-7B-Instruct', \
    token=os.environ.get('HF_TOKEN') or None, \
    ignore_patterns=['*.msgpack','*.h5','flax_model*','tf_model*'] \
)"

# Runtime env (placed BELOW the cached model layer so it doesn't trigger a
# 16GB re-download): unbuffered stdout for live logs; offline HF so vLLM never
# phones the hub at runtime (the model is already baked into the image — the
# suspected source of the idle-GPU startup hang).
ENV PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY app/ /app/app/
COPY blueprints/ /app/blueprints/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["/app/start.sh"]
