"""
Nebius Inference Job — MIDV-2020 evaluation harness.

This container runs the recognition pipeline over a labeled MIDV-2020 subset,
calls the Endpoint for each document, scores the result against ground truth,
and writes per-document results plus a summary report to Nebius Object Storage
(Requirement 15).

Batch re-recognition (the former JOB_MODE=batch path) is out of scope for the
current submission and has been removed — see Requirement 8 (de-scoped).

Environment variables:
  MANIFEST_PATH       — Nebius Object Storage path to the eval Manifest JSON
                        (e.g. s3://my-bucket/eval/midv2020/manifest.json)
  OUTPUT_PATH         — Nebius Object Storage prefix for results + report
                        (e.g. s3://my-bucket/eval/)
  ENDPOINT_URL        — Full URL of the Nebius Endpoint
                        (e.g. https://...nebius.ai)
  ENDPOINT_TOKEN      — Bearer token for Authorization header on /recognize calls
  REQUEST_TIMEOUT_S   — Per-request timeout in seconds (default: 60)
  GPU_HOURLY_USD      — H100 SXM hourly price for the cost estimate (default: 2.80)
  S3_ENDPOINT         — Nebius Object Storage S3-compatible endpoint URL
                        (default: https://storage.eu-north1.nebius.cloud)
  S3_BUCKET           — Nebius Object Storage bucket name (set via env)
  S3_ACCESS_KEY       — Nebius Object Storage static key ID
  S3_SECRET_KEY       — Nebius Object Storage static secret key

Eval manifest format (JSON):
  {
    "documents": [
      {
        "document_id": "esp_id_00",
        "doc_type": "esp_id",
        "blueprint_id": "id_card",
        "nos_key": "eval/midv2020/images/esp_id/00.jpg",
        "mime_type": "image/jpeg",
        "ground_truth": { "document_number": "...", "surname": "...", ... }
      }
    ]
  }

Requirements: 15.1, 15.4, 15.5
"""

import json
import os
import sys
import time

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "")
ENDPOINT_URL = os.environ.get("ENDPOINT_URL", "").rstrip("/")
ENDPOINT_TOKEN = os.environ.get("ENDPOINT_TOKEN", "")
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "60"))

GPU_HOURLY_USD = float(os.environ.get("GPU_HOURLY_USD", "2.80"))

# Nebius Object Storage (NOS) configuration
S3_ENDPOINT = os.environ.get(
    "S3_ENDPOINT", "https://storage.eu-north1.nebius.cloud"
)
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _make_s3_client():
    """Create a boto3 S3 client configured for Nebius Object Storage."""
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY or None,
        aws_secret_access_key=S3_SECRET_KEY or None,
    )


def _parse_s3_path(s3_path):
    """
    Parse an 's3://bucket/key' path into (bucket, key).

    >>> _parse_s3_path("s3://my-bucket/path/to/file.json")
    ('my-bucket', 'path/to/file.json')
    >>> _parse_s3_path("s3://my-bucket/prefix/")
    ('my-bucket', 'prefix/')
    """
    if not s3_path.startswith("s3://"):
        raise ValueError(f"Invalid S3 path (must start with s3://): {s3_path!r}")
    without_scheme = s3_path[len("s3://"):]
    parts = without_scheme.split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    return bucket, key


def _read_manifest(s3_client, manifest_path):
    """
    Fetch and parse the eval Manifest JSON from Nebius Object Storage.

    Returns the parsed dict on success.
    Logs to stdout and calls sys.exit(1) on any failure.
    """
    try:
        bucket, key = _parse_s3_path(manifest_path)
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
        return json.loads(body)
    except (BotoCoreError, ClientError) as exc:
        print(json.dumps({"event": "manifest_read_error", "error": str(exc)}), flush=True)
        sys.exit(1)
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"event": "manifest_read_error", "error": str(exc)}), flush=True)
        sys.exit(1)


def _write_result(s3_client, output_path, document_id, record):
    """
    Write a per-document result JSON to OUTPUT_PATH/<document_id>.json
    in Nebius Object Storage.
    """
    prefix = output_path.rstrip("/")
    bucket, key_prefix = _parse_s3_path(prefix)
    # Strip leading slash from key_prefix to avoid double slashes
    key_prefix = key_prefix.strip("/")
    output_key = f"{key_prefix}/{document_id}.json" if key_prefix else f"{document_id}.json"

    s3_client.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def _write_nos_json(s3_client, bucket, key, payload):
    """Best-effort JSON write to NOS — logs but never raises."""
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "nos_write_error", "key": key, "error": str(exc)}), flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _build_auth_headers():
    """
    Build HTTP headers for /recognize calls.
    Includes Authorization: Bearer <ENDPOINT_TOKEN> if token is configured.
    """
    headers = {}
    if ENDPOINT_TOKEN:
        headers["Authorization"] = f"Bearer {ENDPOINT_TOKEN}"
    return headers


def _call_endpoint(http_client, endpoint_url, doc, timeout_s):
    """
    POST /recognize to the Endpoint for a single document entry.

    Uses document.type: "nebius_object" with the nos_key from the manifest
    entry. Sends Authorization: Bearer header if ENDPOINT_TOKEN is configured.

    Returns (response_or_none, status_code_or_none, elapsed_ms, exception_or_none).
    """
    url = f"{endpoint_url}/recognize"
    payload = {
        "document": {
            "type": "nebius_object",
            "value": doc["nos_key"],
            "mime_type": doc["mime_type"],
        },
        "mode": "blueprint",
        "blueprint_id": doc["blueprint_id"],
    }

    headers = _build_auth_headers()

    t0 = time.monotonic()
    try:
        resp = http_client.post(url, json=payload, headers=headers, timeout=timeout_s)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return resp, resp.status_code, elapsed_ms, None
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return None, None, elapsed_ms, exc


# ---------------------------------------------------------------------------
# Evaluation run — Req 15
# ---------------------------------------------------------------------------

def main_eval():
    """Evaluation run: recognize each labeled document, score against ground
    truth, write per-document results and the summary report (Req 15.4, 15.5)."""
    import uuid

    from eval_metrics import evaluate_document, summarize

    job_start = time.monotonic()
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]

    s3_client = _make_s3_client()
    manifest = _read_manifest(s3_client, MANIFEST_PATH)
    documents = manifest.get("documents", [])

    doc_results = []
    with httpx.Client() as http_client:
        for doc in documents:
            ground_truth = doc.get("ground_truth") or {}
            if not ground_truth:
                print(json.dumps({"event": "eval_skip_no_ground_truth",
                                  "document_id": doc.get("document_id")}), flush=True)
                continue
            resp, status_code, elapsed_ms, exc = _call_endpoint(
                http_client, ENDPOINT_URL, doc, REQUEST_TIMEOUT_S
            )
            fields = {}
            if status_code == 200:
                try:
                    fields = resp.json().get("fields") or {}
                except Exception:  # noqa: BLE001
                    fields = {}
            entry = {
                "document_id": doc["document_id"],
                "doc_type": doc.get("doc_type"),
                "blueprint_id": doc.get("blueprint_id"),
                "endpoint_http_status": status_code,
                "latency_ms": elapsed_ms,
                # P14 — every ground-truth field is scored even on total failure
                "fields": evaluate_document(fields, ground_truth),
            }
            if exc is not None:
                entry["error"] = str(exc)
            doc_results.append(entry)
            print(json.dumps({"event": "eval_document", "document_id": doc["document_id"],
                              "endpoint_http_status": status_code, "elapsed_ms": elapsed_ms}), flush=True)
            _write_result(s3_client, OUTPUT_PATH.rstrip("/") + f"/results/{job_id}",
                          doc["document_id"], entry)

    report = summarize(doc_results, gpu_hourly_usd=GPU_HOURLY_USD)
    report["job_id"] = job_id
    report["elapsed_seconds"] = round(time.monotonic() - job_start, 3)

    bucket, base_key = _parse_s3_path(OUTPUT_PATH.rstrip("/"))
    report_key = f"{base_key.strip('/')}/reports/{job_id}.json" if base_key.strip("/") else f"reports/{job_id}.json"
    _write_nos_json(s3_client, bucket, report_key, report)

    print(json.dumps({"event": "eval_summary", **report}, ensure_ascii=False), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main_eval()
