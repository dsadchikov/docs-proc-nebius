import asyncio
import time
import uuid
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import boto3
from botocore.config import Config as BotoConfig
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.responses import JSONResponse, FileResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import Config
from app.auth import verify_token
from app.models import RecognizeRequest, RecognizeResponse, BlueprintCreate, BlueprintUpdate, BlueprintMeta, PresignResponse, BlueprintGenerateRequest
from app.nos_writer import write_outbound
from app.extractor import extract_document, extract_auto, extract_packet, preprocess_document, generate_blueprint_from_document
from app.blueprint_loader import BlueprintStore
from app.logging_config import configure_logging
from app import metrics

configure_logging()
logger = logging.getLogger("app")

blueprint_store: BlueprintStore = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global blueprint_store
    # Re-assert JSON logging after uvicorn has installed its own handlers.
    configure_logging()
    app.state.http_client = httpx.AsyncClient(base_url=Config.VLLM_BASE_URL, timeout=Config.VLLM_TIMEOUT)
    app.state.start_time = time.time()
    blueprint_store = BlueprintStore()
    app.state.blueprint_store = blueprint_store
    logger.info("FastAPI started | GPU:%s Mock:%s vLLM:%s", Config.GPU_ENABLED, Config.MOCK_VLLM, Config.VLLM_BASE_URL)
    if not Config.AUTH_TOKEN and Config.GPU_ENABLED and not Config.MOCK_VLLM:
        logger.warning(
            "AUTH_TOKEN is not set on a GPU (non-mock) deployment — the API is OPEN. "
            "Set AUTH_TOKEN to protect /recognize and the blueprint APIs."
        )
    yield
    await app.state.http_client.aclose()


app = FastAPI(title="Nebius Document Recognition", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized requests early (base64 memory-DoS guard)."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > Config.MAX_UPLOAD_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body exceeds MAX_UPLOAD_BYTES={Config.MAX_UPLOAD_BYTES}"},
                )
        except ValueError:
            pass
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.CORS_ALLOW_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

_STATIC_DIR = Path(__file__).parent / "static"
(_STATIC_DIR / "samples").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/demo")
async def demo_page():
    """Zero-install browser demo (Req 17). Token is always user-supplied."""
    return FileResponse(_STATIC_DIR / "demo.html")


@app.get("/health")
async def health(request: Request):
    vllm_ok = Config.MOCK_VLLM
    if not vllm_ok:
        try:
            resp = await request.app.state.http_client.get("/health")
            vllm_ok = resp.status_code == 200
        except Exception:
            pass
    uptime = time.time() - request.app.state.start_time
    bp_count = len(blueprint_store.list_all()) if blueprint_store else 0
    return JSONResponse(status_code=200 if vllm_ok else 503, content={
        "status": "healthy" if vllm_ok else "degraded", "vllm": "up" if vllm_ok else "down",
        "fastapi": "up", "gpu_enabled": Config.GPU_ENABLED, "mock_mode": Config.MOCK_VLLM,
        "model": Config.VLLM_MODEL_NAME, "uptime_seconds": round(uptime, 1), "blueprints_loaded": bp_count})


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    """Prometheus text exposition (aggregate-only). Disable via METRICS_ENABLED=0."""
    if not Config.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="metrics disabled")
    vllm_up = Config.MOCK_VLLM
    if not vllm_up:
        try:
            resp = await request.app.state.http_client.get("/health")
            vllm_up = resp.status_code == 200
        except Exception:
            vllm_up = False
    return PlainTextResponse(metrics.render(vllm_up))


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def vllm_passthrough(path: str, request: Request, _=Depends(verify_token)):
    """Transparent proxy to vLLM's OpenAI-compatible API.

    Replaces the nginx `location /v1/` passthrough now that uvicorn serves
    port 8080 directly. Preserves "direct model access" (README) and smoke
    test T6 (GET /v1/models). Protected by app-level verify_token so the model
    is not directly reachable when deployed with `--auth none` at the ingress.
    """
    client = request.app.state.http_client
    body = await request.body()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() == "content-type"}
    upstream = await client.request(
        request.method,
        f"/v1/{path}",
        content=body,
        headers=fwd_headers,
        params=request.query_params,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@app.get("/inbound/presign", response_model=PresignResponse)
async def inbound_presign(filename: str = Query(..., description="Original filename, used to detect extension"), _=Depends(verify_token)):
    """Generate a NOS presigned PUT URL for direct client upload (standalone Variant B).

    Validates: Requirements 1.2
    """
    if not Config.S3_ACCESS_KEY or not Config.S3_SECRET_KEY:
        raise HTTPException(503, detail="Object storage not configured")

    # Determine file extension from the provided filename (default to .bin)
    ext = os.path.splitext(filename)[1].lstrip(".")
    if not ext:
        ext = "bin"

    # Build time-partitioned NOS key: inbound/YYYY/MM/DD/HH/mm/<uuid>.<ext>
    now = datetime.now(timezone.utc)
    file_uuid = str(uuid.uuid4()).replace("-", "")[:8] + str(uuid.uuid4()).replace("-", "")[:8]
    nos_key = now.strftime(f"inbound/%Y/%m/%d/%H/%M/{file_uuid}.{ext}")

    # Build S3 client using the same config as blueprint_loader
    s3_client = boto3.client(
        "s3",
        endpoint_url=Config.S3_ENDPOINT,
        aws_access_key_id=Config.S3_ACCESS_KEY,
        aws_secret_access_key=Config.S3_SECRET_KEY,
        region_name=Config.S3_REGION,
        config=BotoConfig(signature_version="s3v4"),
    )

    expires_in = 300
    presigned_put_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": Config.S3_BUCKET, "Key": nos_key},
        ExpiresIn=expires_in,
    )

    logger.info("presign: nos_key=%s expires_in=%d", nos_key, expires_in)
    return PresignResponse(presigned_put_url=presigned_put_url, nos_key=nos_key, expires_in=expires_in)


@app.post("/recognize", response_model=None)
async def recognize(request: Request, body: RecognizeRequest, _=Depends(verify_token)):
    start = time.time()
    request_id = str(uuid.uuid4())
    blueprint = None
    if body.mode == "blueprint":
        if not body.blueprint_id:
            raise HTTPException(422, "blueprint_id required for mode='blueprint'")
        blueprint = blueprint_store.get(body.blueprint_id)
        if not blueprint:
            raise HTTPException(422, f"Blueprint not found: '{body.blueprint_id}'")
    elif body.mode == "double_check" and body.blueprint_id:
        blueprint = blueprint_store.get(body.blueprint_id)
        if not blueprint:
            raise HTTPException(422, f"Blueprint not found: '{body.blueprint_id}'")
    async def _run():
        if body.mode == "auto":
            image_content, document_part = await preprocess_document(body.document)
            return await extract_auto(request.app.state.http_client, image_content, document_part, body.options, blueprint_store)
        elif body.mode == "packet":
            return await extract_packet(request.app.state.http_client, body.document, body.options, blueprint_store)
        else:
            return await extract_document(request.app.state.http_client, body, blueprint)

    # Per-request deadline (Req 1.6). Packet mode gets a larger budget.
    deadline = Config.PACKET_TIMEOUT if body.mode == "packet" else Config.REQUEST_TIMEOUT
    try:
        result = await asyncio.wait_for(_run(), timeout=deadline)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        metrics.record_request(body.mode, "timeout", time.time() - start)
        logger.warning("recognize timeout", extra={"request_id": request_id, "mode": body.mode, "deadline_s": deadline})
        raise HTTPException(status_code=504, detail=f"Recognition exceeded {deadline}s")

    result.request_id = request_id
    asyncio.create_task(write_outbound(request_id, result.model_dump()))
    duration = time.time() - start
    routing = getattr(result, "routing", None)
    metrics.record_request(body.mode, routing, duration)
    logger.info(
        "recognize complete",
        extra={
            "request_id": request_id, "mode": body.mode, "blueprint_id": body.blueprint_id,
            "document_confidence": getattr(result, "document_confidence", None),
            "routing": routing, "duration_s": round(duration, 3),
        },
    )
    return result


@app.get("/blueprints", response_model=list[BlueprintMeta])
async def list_blueprints(_=Depends(verify_token)):
    return blueprint_store.list_all()


@app.post("/blueprints/generate", status_code=201)
async def generate_blueprint(request: Request, body: BlueprintGenerateRequest, _=Depends(verify_token)):
    """Generate a draft blueprint from a sample document image (two-pass VLM workflow).

    Returns HTTP 201 with the generated blueprint JSON.
    Returns HTTP 409 if blueprint_id already exists.
    """
    # Check for existing blueprint_id (in either active or raw/draft cache)
    if blueprint_store.get(body.blueprint_id) or blueprint_store.get_raw(body.blueprint_id):
        raise HTTPException(409, f"Blueprint '{body.blueprint_id}' already exists")

    generated = await generate_blueprint_from_document(
        request.app.state.http_client,
        body.document,
        body.blueprint_id,
        body.name,
        body.description,
    )

    try:
        result = blueprint_store.create_draft(generated)
    except ValueError as e:
        raise HTTPException(409, str(e))

    logger.info("generate_blueprint: id=%s name=%s status=draft", body.blueprint_id, body.name)
    return result


@app.get("/blueprints/{blueprint_id}")
async def get_blueprint(blueprint_id: str, _=Depends(verify_token)):
    bp = blueprint_store.get_raw(blueprint_id)
    if not bp:
        raise HTTPException(404, f"Blueprint not found: '{blueprint_id}'")
    return bp


@app.post("/blueprints", status_code=201)
async def create_blueprint(body: BlueprintCreate, _=Depends(verify_token)):
    try:
        return blueprint_store.create(body.model_dump())
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.put("/blueprints/{blueprint_id}")
async def update_blueprint(blueprint_id: str, body: BlueprintUpdate, _=Depends(verify_token)):
    try:
        return blueprint_store.update(blueprint_id, body.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/blueprints/{blueprint_id}", status_code=204)
async def delete_blueprint(blueprint_id: str, _=Depends(verify_token)):
    try:
        blueprint_store.delete(blueprint_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/blueprints/reload")
async def reload_blueprints(_=Depends(verify_token)):
    count = blueprint_store.reload()
    return {"status": "reloaded", "blueprints_count": count}
