"""
conftest.py — Shared pytest fixtures for the nebius-endpoint test suite.

Provides:
  - blueprint_dir          (session)  — path to tests/fixtures/blueprints/
  - blueprint_passport     (function) — loaded passport blueprint dict
  - blueprint_cv           (function) — loaded cv blueprint dict
  - blueprint_criminal_record (function) — loaded criminal_record_certificate blueprint dict
  - blueprint_residence_permit_ltu (function) — loaded residence_permit_ltu_front blueprint dict
  - all_blueprint_ids      (function) — list of all four supported blueprint ID strings
  - mock_vllm_client       (function) — AsyncMock mimicking httpx.AsyncClient with a default
                                        passport extraction response
  - mock_vllm_client_for_passport (function) — same but with full realistic passport fields
  - sample_document_bytes  (function) — minimal 1×1 JPEG bytes for use in tests
  - sample_document_url    (function) — a sample presigned-URL-style string
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

# conftest.py lives in nebius-endpoint/tests/
# Blueprint JSON fixtures live in nebius-endpoint/tests/fixtures/blueprints/
# (copied from nebius-endpoint/app/blueprints/ or loaded dynamically below)
_TESTS_DIR = Path(__file__).parent
_FIXTURES_BLUEPRINTS_DIR = (_TESTS_DIR / "fixtures" / "blueprints").resolve()

# Fallback: if the fixtures directory doesn't exist yet, fall back to app/blueprints/
_APP_BLUEPRINTS_DIR = (_TESTS_DIR / ".." / "app" / "blueprints").resolve()


def _resolve_blueprints_dir() -> Path:
    if _FIXTURES_BLUEPRINTS_DIR.exists():
        return _FIXTURES_BLUEPRINTS_DIR
    if _APP_BLUEPRINTS_DIR.exists():
        return _APP_BLUEPRINTS_DIR
    raise FileNotFoundError(
        f"No blueprints directory found. "
        f"Checked: {_FIXTURES_BLUEPRINTS_DIR}, {_APP_BLUEPRINTS_DIR}"
    )


def _load_blueprint_json(blueprint_id: str) -> dict:
    """Load a blueprint JSON file from the blueprints directory."""
    bp_dir = _resolve_blueprints_dir()
    bp_path = bp_dir / f"{blueprint_id}.json"
    if not bp_path.exists():
        raise FileNotFoundError(f"Blueprint fixture not found: {bp_path}")
    with open(bp_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Blueprint fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def blueprint_dir() -> str:
    """Return the path to the blueprints fixtures directory.

    Session-scoped so the path is resolved once for the whole test run.
    """
    return str(_resolve_blueprints_dir())


@pytest.fixture
def blueprint_passport(blueprint_dir):
    """Return the loaded passport blueprint dict."""
    return _load_blueprint_json("passport")


@pytest.fixture
def blueprint_cv(blueprint_dir):
    """Return the loaded cv blueprint dict."""
    return _load_blueprint_json("cv")


@pytest.fixture
def blueprint_criminal_record(blueprint_dir):
    """Return the loaded criminal_record_certificate blueprint dict."""
    return _load_blueprint_json("criminal_record_certificate")


@pytest.fixture
def blueprint_residence_permit_ltu(blueprint_dir):
    """Return the loaded residence_permit_ltu_front blueprint dict."""
    return _load_blueprint_json("residence_permit_ltu_front")


@pytest.fixture
def all_blueprint_ids():
    """Return a list of all four supported blueprint ID strings."""
    return [
        "passport",
        "cv",
        "criminal_record_certificate",
        "residence_permit_ltu_front",
    ]


# ---------------------------------------------------------------------------
# Mock vLLM client helpers
# ---------------------------------------------------------------------------

def _make_mock_httpx_response(content: str, status_code: int = 200) -> MagicMock:
    """Build a MagicMock that looks like an httpx Response from vLLM.

    The mock has the shape returned by vLLM's /v1/chat/completions:
        response.status_code == status_code
        response.json() == {"choices": [{"message": {"content": content}}]}
    """
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return response


# Default passport extraction JSON (all fields, value "TEST", confidence 90)
_DEFAULT_PASSPORT_FIELDS = {
    "document_type": {"value": "PASSPORT", "confidence": 90},
    "document_number": {"value": "TEST123456", "confidence": 90},
    "issuing_country": {"value": "GBR", "confidence": 90},
    "surname": {"value": "TEST", "confidence": 90},
    "given_names": {"value": "TEST", "confidence": 90},
    "sex": {"value": "M", "confidence": 90},
    "nationality": {"value": "GBR", "confidence": 90},
    "date_of_birth": {"value": "1990-01-01", "confidence": 90},
    "mrz_line_1": {"value": "P<GBRTESTTEST<<<<<<<<<<<<<<<<<<<<<<<<<<<", "confidence": 90},
    "mrz_line_2": {"value": "TEST1234569GBR9001011M2501010<<<<<<<<<<<<<<<6", "confidence": 90},
}

_DEFAULT_MOCK_RESPONSE_JSON = json.dumps(_DEFAULT_PASSPORT_FIELDS)


@pytest.fixture
def mock_vllm_client():
    """Return an AsyncMock mimicking httpx.AsyncClient pointing at vLLM.

    The mock's .post() method returns a default passport extraction response.
    Override per-test by reassigning mock_vllm_client.post.return_value.
    """
    client = AsyncMock()
    client.post.return_value = _make_mock_httpx_response(_DEFAULT_MOCK_RESPONSE_JSON)
    return client


# Full passport fields response (realistic sample values)
_FULL_PASSPORT_FIELDS = {
    "document_type": {"value": "PASSPORT", "confidence": 90},
    "document_number": {"value": "AB1234567", "confidence": 90},
    "issuing_country": {"value": "GBR", "confidence": 90},
    "surname": {"value": "SMITH", "confidence": 90},
    "given_names": {"value": "JOHN WILLIAM", "confidence": 90},
    "sex": {"value": "M", "confidence": 90},
    "nationality": {"value": "GBR", "confidence": 90},
    "date_of_birth": {"value": "1985-06-15", "confidence": 90},
    "mrz_line_1": {"value": "P<GBRSMITH<<JOHN<WILLIAM<<<<<<<<<<<<<<<<<<<<", "confidence": 90},
    "mrz_line_2": {"value": "AB12345679GBR8506154M3006151<<<<<<<<<<<<<<<4", "confidence": 90},
}

_FULL_PASSPORT_RESPONSE_JSON = json.dumps(_FULL_PASSPORT_FIELDS)


@pytest.fixture
def mock_vllm_client_for_passport():
    """Return an AsyncMock configured with a full realistic passport fields response."""
    client = AsyncMock()
    client.post.return_value = _make_mock_httpx_response(_FULL_PASSPORT_RESPONSE_JSON)
    return client


# ---------------------------------------------------------------------------
# Sample document fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_document_bytes() -> bytes:
    """Return a minimal valid 1×1 pixel JPEG bytes payload.

    Hardcoded JFIF-compliant JPEG (SOI + APP0 + EOI) usable anywhere raw
    document bytes are expected without needing real images.
    """
    return (
        b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
        b'\xff\xd9'
    )


@pytest.fixture
def sample_document_url() -> str:
    """Return a sample presigned-URL-style string for testing URL-based document input."""
    return (
        "https://example-bucket.s3.amazonaws.com/test-doc.jpg"
        "?X-Amz-Signature=abc123"
    )
