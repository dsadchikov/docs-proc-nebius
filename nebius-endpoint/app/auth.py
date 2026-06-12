from fastapi import Request, HTTPException
from app.config import Config


async def verify_token(request: Request):
    """Verify Bearer token from Authorization header."""
    if not Config.AUTH_TOKEN:
        return
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth_header[7:]
    if token != Config.AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
