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
