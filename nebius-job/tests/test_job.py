"""
Property-based tests for nebius-job/job.py.

P6 — Exactly N output files for N manifest items (Requirements 8.3, 8.4, 8.5)
P7 — Job exits 0 regardless of individual failures   (Requirements 8.6, 8.7)

Run with:
    cd /Users/ds/lity
    python -m pytest nebius-job/tests/test_job.py --tb=short -v
"""

import json
import sys
import os
from io import BytesIO
from unittest.mock import MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Path setup — job.py lives one directory up from this tests/ folder
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import job  # noqa: E402 (module under test)

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

# A valid document entry as it appears in the manifest
_doc_entry_strategy = st.fixed_dictionaries(
    {
        "document_id": st.uuids().map(str),
        "blueprint_id": st.sampled_from(["passport", "default", "residence_permit_ltu_front"]),
        "nos_key": st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/-_."),
            min_size=1,
            max_size=60,
        ).map(lambda s: f"inbound/2026/06/09/{s}.jpg"),
        "mime_type": st.sampled_from(["image/jpeg", "image/png", "application/pdf"]),
    }
)


def _make_mock_http_response(status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response that looks like a real 200 or error response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"fields": {}, "document_confidence": 80, "routing": "auto_classified"}
    return resp


# ---------------------------------------------------------------------------
# P6 — Exactly N output files (put_object calls) for N manifest items
#
# Validates: Requirements 8.3, 8.4, 8.5
# ---------------------------------------------------------------------------

@given(documents=st.lists(_doc_entry_strategy, min_size=0, max_size=20))
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_p6_exactly_n_output_files_for_n_manifest_items(documents):
    """
    **Validates: Requirements 8.3, 8.4, 8.5**

    For every list of N manifest documents, exactly N result files must be
    written to NOS — one per document — regardless of whether individual
    HTTP calls succeed (200) or fail (500).
    """
    mock_s3 = MagicMock()
    mock_http = MagicMock()

    # Alternate between success (200) and failure (500) to exercise both
    # the success and the error-record paths in _process_document.
    def _side_effect(*args, **kwargs):
        idx = mock_http.post.call_count - 1
        status = 200 if idx % 2 == 0 else 500
        return _make_mock_http_response(status)

    mock_http.post.side_effect = _side_effect

    with patch.object(job, "OUTPUT_PATH", "s3://test-bucket/results/"):
        for doc in documents:
            job._process_document(
                s3_client=mock_s3,
                http_client=mock_http,
                doc=doc,
                endpoint_url="http://fake-endpoint",
                max_retries=0,   # no sleeps → fast tests
                timeout_s=5.0,
            )

    assert mock_s3.put_object.call_count == len(documents), (
        f"Expected {len(documents)} put_object calls, "
        f"got {mock_s3.put_object.call_count}"
    )


@given(documents=st.lists(_doc_entry_strategy, min_size=0, max_size=20))
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_p6_output_keys_match_document_ids(documents):
    """
    **Validates: Requirements 8.3, 8.5**

    Each put_object call must write a key of the form
    ``<prefix>/<document_id>.json``, so that callers can find results
    deterministically by document_id.
    """
    mock_s3 = MagicMock()
    mock_http = MagicMock()
    mock_http.post.return_value = _make_mock_http_response(200)

    with patch.object(job, "OUTPUT_PATH", "s3://test-bucket/results/batch/"):
        for doc in documents:
            job._process_document(
                s3_client=mock_s3,
                http_client=mock_http,
                doc=doc,
                endpoint_url="http://fake-endpoint",
                max_retries=0,
                timeout_s=5.0,
            )

    expected_ids = {doc["document_id"] for doc in documents}
    # Collect the Key kwarg from every put_object call
    written_keys = {kw["Key"] for _, kw in (c for c in mock_s3.put_object.call_args_list)}
    for doc in documents:
        expected_suffix = f"{doc['document_id']}.json"
        matching = [k for k in written_keys if k.endswith(expected_suffix)]
        assert len(matching) == 1, (
            f"Expected exactly one output key ending with {expected_suffix!r}, "
            f"found {matching}"
        )


# ---------------------------------------------------------------------------
# P7 — Job exits 0 regardless of individual failures
#
# Validates: Requirements 8.6, 8.7
# ---------------------------------------------------------------------------

@given(documents=st.lists(_doc_entry_strategy, min_size=1, max_size=15))
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_p7_process_document_never_raises_on_network_error(documents):
    """
    **Validates: Requirements 8.6, 8.7**

    _process_document must never raise an exception — even when every HTTP
    call raises a network-level exception (worst case).  It should return
    False (failure) and write an error record to NOS, but not propagate the
    exception upward.
    """
    mock_s3 = MagicMock()
    mock_http = MagicMock()
    # Simulate complete network failure for every call
    mock_http.post.side_effect = ConnectionError("simulated network failure")

    with patch.object(job, "OUTPUT_PATH", "s3://test-bucket/results/"):
        for doc in documents:
            try:
                result = job._process_document(
                    s3_client=mock_s3,
                    http_client=mock_http,
                    doc=doc,
                    endpoint_url="http://fake-endpoint",
                    max_retries=0,   # avoid sleeps
                    timeout_s=5.0,
                )
            except Exception as exc:
                pytest.fail(
                    f"_process_document raised {type(exc).__name__}: {exc} "
                    f"for doc {doc['document_id']!r}"
                )
            assert result is False, (
                f"Expected False (failure) but got {result!r} "
                f"for doc {doc['document_id']!r}"
            )

    # One error record written per document even under total network failure
    assert mock_s3.put_object.call_count == len(documents)


@given(documents=st.lists(_doc_entry_strategy, min_size=1, max_size=15))
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_p7_main_exits_0_regardless_of_failures(documents):
    """
    **Validates: Requirements 8.6, 8.7**

    main() must always sys.exit(0) — even when every document call fails
    with an HTTP 500 and the HTTP client raises exceptions for some docs.
    """
    manifest = {"documents": documents}
    manifest_bytes = json.dumps(manifest).encode()

    mock_s3 = MagicMock()
    # S3 get_object returns the manifest
    mock_s3.get_object.return_value = {"Body": BytesIO(manifest_bytes)}

    mock_http_instance = MagicMock()
    # All requests return HTTP 500 (failure, but not a network exception)
    mock_http_instance.post.return_value = _make_mock_http_response(500)

    with (
        patch.object(job, "MANIFEST_PATH", "s3://test-bucket/manifest.json"),
        patch.object(job, "OUTPUT_PATH", "s3://test-bucket/results/"),
        patch.object(job, "ENDPOINT_URL", "http://fake-endpoint"),
        patch.object(job, "MAX_RETRIES", 0),
        patch.object(job, "REQUEST_TIMEOUT_S", 5.0),
        patch.object(job, "_make_s3_client", return_value=mock_s3),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_http_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit) as exc_info:
            job.main()

    assert exc_info.value.code == 0, (
        f"Expected sys.exit(0) but got sys.exit({exc_info.value.code})"
    )


@given(documents=st.lists(_doc_entry_strategy, min_size=1, max_size=15))
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_p7_main_exits_0_on_all_network_exceptions(documents):
    """
    **Validates: Requirements 8.6, 8.7**

    main() must always sys.exit(0) — even when every HTTP call raises a
    network-level exception (no response at all).
    """
    manifest = {"documents": documents}
    manifest_bytes = json.dumps(manifest).encode()

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": BytesIO(manifest_bytes)}

    mock_http_instance = MagicMock()
    mock_http_instance.post.side_effect = ConnectionError("network down")

    with (
        patch.object(job, "MANIFEST_PATH", "s3://test-bucket/manifest.json"),
        patch.object(job, "OUTPUT_PATH", "s3://test-bucket/results/"),
        patch.object(job, "ENDPOINT_URL", "http://fake-endpoint"),
        patch.object(job, "MAX_RETRIES", 0),
        patch.object(job, "REQUEST_TIMEOUT_S", 5.0),
        patch.object(job, "_make_s3_client", return_value=mock_s3),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_http_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit) as exc_info:
            job.main()

    assert exc_info.value.code == 0, (
        f"Expected sys.exit(0) but got sys.exit({exc_info.value.code})"
    )


# ---------------------------------------------------------------------------
# Edge case: N=0 manifest (Requirement 8.7)
# ---------------------------------------------------------------------------

def test_p6_zero_documents_no_put_object_calls():
    """
    **Validates: Requirement 8.7**

    When the manifest contains zero documents, put_object is never called
    and main() exits 0 without ever contacting the Endpoint.
    """
    manifest = {"documents": []}
    manifest_bytes = json.dumps(manifest).encode()

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": BytesIO(manifest_bytes)}

    mock_http_instance = MagicMock()

    with (
        patch.object(job, "MANIFEST_PATH", "s3://test-bucket/manifest.json"),
        patch.object(job, "OUTPUT_PATH", "s3://test-bucket/results/"),
        patch.object(job, "ENDPOINT_URL", "http://fake-endpoint"),
        patch.object(job, "MAX_RETRIES", 0),
        patch.object(job, "REQUEST_TIMEOUT_S", 5.0),
        patch.object(job, "_make_s3_client", return_value=mock_s3),
        patch("httpx.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_http_instance)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit) as exc_info:
            job.main()

    assert exc_info.value.code == 0
    mock_s3.put_object.assert_not_called()
    mock_http_instance.post.assert_not_called()
