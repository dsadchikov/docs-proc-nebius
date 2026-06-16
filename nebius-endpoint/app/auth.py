import hmac

from fastapi import Request, HTTPException
from app.config import Config


async def verify_token(request: Request):
    """Verify Bearer token from Authorization header.

    Note: when Config.AUTH_TOKEN is empty the endpoint is intentionally open
    (local / MOCK_VLLM development). main.py emits a startup warning if this
    happens on a real (GPU, non-mock) deployment — a fail-open guard.
    """
    if not Config.AUTH_TOKEN:
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header[7:]
    # Constant-time comparison to avoid leaking the token via timing.
    if not hmac.compare_digest(token, Config.AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")
