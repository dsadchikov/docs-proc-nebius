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
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.auth import verify_token
from app.models import RecognizeRequest, RecognizeResponse, BlueprintCreate, BlueprintUpdate, BlueprintMeta, PresignResponse, BlueprintGenerateRequest
from app.nos_writer import write_outbound
from app.extractor import extract_document, extract_auto, extract_packet, preprocess_document, generate_blueprint_from_document
from app.blueprint_loader import BlueprintStore

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

blueprint_store: BlueprintStore = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global blueprint_store
    app.state.http_client = httpx.AsyncClient(base_url=Config.VLLM_BASE_URL, timeout=Config.VLLM_TIMEOUT)
    app.state.start_time = time.time()
    blueprint_store = BlueprintStore()
    app.state.blueprint_store = blueprint_store
    logger.info("FastAPI started | GPU:%s Mock:%s vLLM:%s", Config.GPU_ENABLED, Config.MOCK_VLLM, Config.VLLM_BASE_URL)
    yield
    await app.state.http_client.aclose()


app = FastAPI(title="Nebius Document Recognition", version="2.0.0", lifespan=lifespan)

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


@app.get("/inbound/presign", response_model=PresignResponse)
async def inbound_presign(filename: str = Query(..., description="Original filename, used to detect extension")):
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
    if body.mode == "auto":
        image_content, document_part = await preprocess_document(body.document)
        result = await extract_auto(request.app.state.http_client, image_content, document_part, body.options, blueprint_store)
    elif body.mode == "packet":
        result = await extract_packet(request.app.state.http_client, body.document, body.options, blueprint_store)
    else:
        result = await extract_document(request.app.state.http_client, body, blueprint)
    result.request_id = request_id
    asyncio.create_task(write_outbound(request_id, result.model_dump()))
    logger.info("recognize: request_id=%s mode=%s bp=%s conf=%s route=%s time=%.2fs",
                request_id, body.mode, body.blueprint_id,
                getattr(result, "document_confidence", None), result.routing, time.time()-start)
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
