"""
test_hardening.py — Well-Architected security / reliability hardening tests.

Covers:
  - 413 on oversized request body (MAX_UPLOAD_BYTES guard)
  - 400 on disallowed presigned_url host (SSRF allowlist)
  - 422 on invalid document.type (enum validation)
  - 422 on PDF exceeding PDF_MAX_PAGES in single-page mode
  - 504 on per-request deadline exceeded (Req 1.6)
  - constant-time auth still accepts/rejects correctly
  - /metrics returns Prometheus text
"""
import asyncio
import base64
import os

os.environ.setdefault("MOCK_VLLM", "1")
os.environ.pop("S3_ACCESS_KEY", None)
os.environ.pop("S3_SECRET_KEY", None)

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.config import Config

MINIMAL_JPEG_B64 = base64.b64encode(
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9'
).decode()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _doc(type_="base64", value=MINIMAL_JPEG_B64, mime="image/jpeg"):
    return {"type": type_, "value": value, "mime_type": mime}


# --- 413 oversized body --------------------------------------------------
def test_oversized_body_returns_413(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_UPLOAD_BYTES", 100)
    big = base64.b64encode(b"x" * 500).decode()
    r = client.post("/recognize", json={"document": _doc(value=big), "mode": "raw"})
    assert r.status_code == 413, r.text


# --- 400 SSRF allowlist --------------------------------------------------
def test_presigned_url_disallowed_host_returns_400(client, monkeypatch):
    monkeypatch.setattr(Config, "FETCH_URL_ALLOWLIST", ["storage.eu-north1.nebius.cloud"])
    payload = {"document": _doc(type_="presigned_url", value="https://evil.example.com/x.jpg"), "mode": "auto"}
    r = client.post("/recognize", json=payload)
    assert r.status_code == 400, r.text
    assert "not allowed" in r.json()["detail"].lower()


def test_presigned_url_http_scheme_returns_400(client):
    payload = {"document": _doc(type_="presigned_url", value="http://storage.eu-north1.nebius.cloud/x.jpg"), "mode": "auto"}
    r = client.post("/recognize", json=payload)
    assert r.status_code == 400, r.text


# --- 422 invalid document.type ------------------------------------------
def test_invalid_document_type_returns_422(client):
    payload = {"document": {"type": "ftp", "value": "x", "mime_type": "image/jpeg"}, "mode": "raw"}
    r = client.post("/recognize", json=payload)
    assert r.status_code == 422, r.text


# --- 422 PDF over page limit (single-page mode) --------------------------
def test_pdf_over_page_limit_returns_422(client, monkeypatch):
    monkeypatch.setattr(main.Config, "PDF_MAX_PAGES", 5)
    # get_pdf_page_count is imported into the extractor module namespace.
    import app.extractor as extractor
    monkeypatch.setattr(extractor, "get_pdf_page_count", lambda b: 9999)
    pdf_b64 = base64.b64encode(b"%PDF-1.4 fake").decode()
    payload = {"document": _doc(type_="base64", value=pdf_b64, mime="application/pdf"), "mode": "auto"}
    r = client.post("/recognize", json=payload)
    assert r.status_code == 422, r.text
    assert "PDF_MAX_PAGES" in r.json()["detail"]


# --- 504 per-request deadline (Req 1.6) ---------------------------------
def test_recognize_timeout_returns_504(client, monkeypatch):
    async def _slow(*a, **k):
        await asyncio.sleep(2)

    monkeypatch.setattr(main.Config, "REQUEST_TIMEOUT", 0.1)
    monkeypatch.setattr(main, "extract_document", _slow)
    payload = {"document": _doc(), "mode": "blueprint", "blueprint_id": "passport"}
    r = client.post("/recognize", json=payload)
    assert r.status_code == 504, r.text


# --- constant-time auth still validates ----------------------------------
def test_auth_accepts_and_rejects(client, monkeypatch):
    monkeypatch.setattr(Config, "AUTH_TOKEN", "s3cr3t-token")
    assert client.get("/blueprints").status_code == 401
    assert client.get("/blueprints", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/blueprints", headers={"Authorization": "Bearer s3cr3t-token"})
    assert ok.status_code == 200, ok.text


# --- /metrics exposition -------------------------------------------------
def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    assert "docproc_vllm_up" in r.text
    assert r.headers["content-type"].startswith("text/plain")
