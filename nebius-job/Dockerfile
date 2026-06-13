# nebius-job/Dockerfile
# MIDV-2020 evaluation harness — runs against a Nebius Endpoint (Req 15).
#
# Required environment variables (pass via `docker run -e` or Kubernetes secrets):
#
#   MANIFEST_PATH        — Nebius Object Storage path to the eval Manifest JSON
#                          e.g. s3://my-bucket/eval/midv2020/manifest.json
#
#   OUTPUT_PATH          — Nebius Object Storage prefix for results + report
#                          e.g. s3://my-bucket/eval/
#
#   ENDPOINT_URL         — Full URL of the Nebius Inference Endpoint (no trailing slash)
#                          e.g. https://<endpoint-id>.api.nebius.ai
#
#   REQUEST_TIMEOUT_S    — Per-request HTTP timeout in seconds (default: 60)
#
# Credentials (injected at runtime — never embed in the image):
#
#   AWS_ACCESS_KEY_ID        — Nebius Object Storage access key ID
#   AWS_SECRET_ACCESS_KEY    — Nebius Object Storage secret access key
#   NEBIUS_ENDPOINT_URL      — Nebius S3-compatible storage endpoint
#                              (default: https://storage.eu-north1.nebius.cloud)

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY job.py eval_metrics.py requirements.txt /app/

CMD ["python", "/app/job.py"]
