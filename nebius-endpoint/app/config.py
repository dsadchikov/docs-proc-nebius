import os


class Config:
    """Application configuration from environment variables."""

    VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000")
    VLLM_MODEL_NAME = os.getenv("VLLM_MODEL_NAME", "Qwen2.5-VL-7B-Instruct")
    GPU_ENABLED = os.getenv("GPU_ENABLED", "1").lower() not in ("0", "false", "no")
    MOCK_VLLM = os.getenv("MOCK_VLLM", "0").lower() in ("1", "true", "yes")
    S3_ENDPOINT = os.getenv("S3_ENDPOINT", "https://storage.eu-north1.nebius.cloud")
    S3_BUCKET = os.getenv("S3_BUCKET", "")
    S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
    S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
    S3_REGION = os.getenv("S3_REGION", "eu-north1")
    AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
    PDF_DPI = int(os.getenv("PDF_DPI", "200"))
    PDF_MAX_PAGES = int(os.getenv("PDF_MAX_PAGES", "50"))
    VLLM_TIMEOUT = int(os.getenv("VLLM_TIMEOUT", "120"))
    FETCH_TIMEOUT = int(os.getenv("FETCH_TIMEOUT", "30"))

    # --- Well-Architected hardening (security / reliability / observability) ---
    # Per-request deadline (Req 1.6: /recognize over budget -> HTTP 504).
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
    # Packet mode legitimately runs longer (one extraction per logical document).
    PACKET_TIMEOUT = int(os.getenv("PACKET_TIMEOUT", "180"))
    # Max inbound request body (bytes). Guards base64 memory-DoS. Default 25 MiB.
    MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    # CORS origins (comma-separated). Empty = no cross-origin (same-origin /demo still works).
    CORS_ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    # SSRF allowlist for document.type=presigned_url hosts (comma-separated).
    # Empty -> derived from the S3_ENDPOINT host at use time.
    FETCH_URL_ALLOWLIST = [h.strip() for h in os.getenv("FETCH_URL_ALLOWLIST", "").split(",") if h.strip()]
    # Expose /metrics (Prometheus text exposition) for Nebius Managed Prometheus.
    METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1").lower() not in ("0", "false", "no")
