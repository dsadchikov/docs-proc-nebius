"""
test_endpoint.py — HTTP API integration tests for the nebius-endpoint FastAPI app.

Tests:
  - 422 on unknown blueprint_id (Req 1.10)
  - 422 on missing blueprint_id when mode=blueprint (Req 1.10)
  - 503 on nebius_object without NOS credentials (Req 1.2, 9.3)
  - Health response shape: all required fields present (Req 12.4)

Validates: Requirements 1.1, 1.2, 1.10, 12.4
"""

import base64
import os

# Set MOCK_VLLM=1 BEFORE importing app.main so Config picks it up at module load time.
os.environ.setdefault("MOCK_VLLM", "1")
# Clear S3 credentials so nebius_object → 503 test works predictably.
os.environ.pop("S3_ACCESS_KEY", None)
os.environ.pop("S3_SECRET_KEY", None)

import pytest
from fastapi.testclient import TestClient
from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal 1×1 JPEG (JFIF SOI + APP0 + EOI) — valid enough for the extractor
MINIMAL_JPEG_B64 = base64.b64encode(
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9'
).decode()

# Required fields in the /health response (Req 12.4)
HEALTH_REQUIRED_FIELDS = {
    "status",
    "vllm",
    "fastapi",
    "gpu_enabled",
    "mock_mode",
    "model",
    "uptime_seconds",
    "blueprints_loaded",
}


@pytest.fixture(scope="module")
def http_client():
    """Yield a TestClient with the full lifespan (startup/shutdown) executed.

    Using the context manager form ensures the lifespan coroutine runs so
    that app.state.start_time is set and blueprint_store is initialised.
    """
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Test 1: 422 on unknown blueprint_id  (Req 1.10)
# ---------------------------------------------------------------------------

def test_recognize_unknown_blueprint_id_returns_422(http_client):
    """POST /recognize with a blueprint_id that does not exist must return HTTP 422.

    Validates: Requirements 1.10
    """
    payload = {
        "document": {
            "type": "base64",
            "value": MINIMAL_JPEG_B64,
            "mime_type": "image/jpeg",
        },
        "mode": "blueprint",
        "blueprint_id": "unknown_xyz_blueprint_that_does_not_exist",
    }
    response = http_client.post("/recognize", json=payload)
    assert response.status_code == 422, (
        f"Expected 422 for unknown blueprint_id, got {response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# Test 2: 422 on missing blueprint_id when mode=blueprint  (Req 1.10)
# ---------------------------------------------------------------------------

def test_recognize_missing_blueprint_id_with_blueprint_mode_returns_422(http_client):
    """POST /recognize with mode='blueprint' and no blueprint_id must return HTTP 422.

    Validates: Requirements 1.10
    """
    payload = {
        "document": {
            "type": "base64",
            "value": MINIMAL_JPEG_B64,
            "mime_type": "image/jpeg",
        },
        "mode": "blueprint",
        # blueprint_id intentionally omitted
    }
    response = http_client.post("/recognize", json=payload)
    assert response.status_code == 422, (
        f"Expected 422 for missing blueprint_id in blueprint mode, got {response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# Test 3: 503 on nebius_object when S3 credentials are absent  (Req 1.2)
# ---------------------------------------------------------------------------

def test_recognize_nebius_object_without_nos_credentials_returns_503(http_client):
    """POST /recognize with document.type='nebius_object' when S3_ACCESS_KEY is empty
    must return HTTP 503 with a message indicating object storage is not configured.

    Validates: Requirements 1.2
    """
    payload = {
        "document": {
            "type": "nebius_object",
            "value": "inbound/2026/06/10/some-object-key.jpg",
            "mime_type": "image/jpeg",
        },
        "mode": "blueprint",
        "blueprint_id": "passport",
    }
    response = http_client.post("/recognize", json=payload)
    assert response.status_code == 503, (
        f"Expected 503 when NOS not configured, got {response.status_code}: {response.text}"
    )
    body = response.json()
    # FastAPI wraps HTTPException detail in {"detail": ...}
    detail = body.get("detail", "")
    assert "not configured" in detail.lower() or "object storage" in detail.lower(), (
        f"Expected 'not configured' / 'object storage' in error detail, got: {detail!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: Health response shape  (Req 12.4)
# ---------------------------------------------------------------------------

def test_health_returns_expected_shape(http_client):
    """GET /health must return HTTP 200 (healthy with MOCK_VLLM=1) and include
    all required response fields: status, vllm, fastapi, gpu_enabled, mock_mode,
    model, uptime_seconds, blueprints_loaded.

    Validates: Requirements 12.4
    """
    response = http_client.get("/health")
    # With MOCK_VLLM=1 the health check short-circuits to healthy
    assert response.status_code in (200, 503), (
        f"Expected 200 or 503 from /health, got {response.status_code}"
    )
    body = response.json()
    missing = HEALTH_REQUIRED_FIELDS - set(body.keys())
    assert not missing, (
        f"/health response is missing required fields: {missing}. Got keys: {set(body.keys())}"
    )
    # With MOCK_VLLM=1 we expect 200 + healthy status
    assert response.status_code == 200, (
        f"Expected 200 with MOCK_VLLM=1, got {response.status_code}. Body: {body}"
    )
    assert body["status"] == "healthy", (
        f"Expected status='healthy' with mock vLLM, got {body['status']!r}"
    )
    assert body["vllm"] == "up", (
        f"Expected vllm='up' with mock vLLM, got {body['vllm']!r}"
    )
    assert body["fastapi"] == "up", (
        f"Expected fastapi='up', got {body['fastapi']!r}"
    )
    assert isinstance(body["uptime_seconds"], (int, float)), (
        f"uptime_seconds must be numeric, got {body['uptime_seconds']!r}"
    )
    assert isinstance(body["blueprints_loaded"], int), (
        f"blueprints_loaded must be an int, got {body['blueprints_loaded']!r}"
    )
    # With local blueprints/ present we expect at least one blueprint loaded
    assert body["blueprints_loaded"] >= 1, (
        f"Expected at least 1 blueprint loaded from local blueprints/, got {body['blueprints_loaded']}"
    )
