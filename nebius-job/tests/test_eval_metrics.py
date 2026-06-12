"""
Property-based tests for eval_metrics.py.

P14 — Evaluation metrics are bounded and complete (Requirements 15.4, 15.5)

Run with:
    cd /Users/ds/lity
    python -m pytest nebius-job/tests/test_eval_metrics.py --tb=short -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hypothesis import given, settings
from hypothesis import strategies as st

from eval_metrics import (  # noqa: E402
    evaluate_document,
    exact_match,
    levenshtein_sim,
    normalize,
    percentile,
    summarize,
)

_value = st.one_of(st.none(), st.text(max_size=40))
_field_name = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)


# ---------------------------------------------------------------------------
# P14 — bounds
# ---------------------------------------------------------------------------

@given(extracted=_value, expected=_value)
@settings(max_examples=300)
def test_p14_exact_match_is_binary_and_sim_bounded(extracted, expected):
    """**Validates: Requirements 15.4** — exact_match ∈ {0,1}, sim ∈ [0,1]."""
    em = exact_match(extracted, expected)
    sim = levenshtein_sim(extracted, expected)
    assert em in (0, 1)
    assert 0.0 <= sim <= 1.0
    if em == 1:
        assert sim == 1.0  # exact match implies maximal similarity


@given(extracted=_value, expected=_value)
@settings(max_examples=200)
def test_p14_metrics_symmetric_under_normalization(extracted, expected):
    """Normalization is idempotent: scoring normalized inputs changes nothing."""
    assert exact_match(normalize(extracted), normalize(expected)) == exact_match(extracted, expected)


# ---------------------------------------------------------------------------
# P14 — completeness: every ground-truth field appears in the result
# ---------------------------------------------------------------------------

@given(
    ground_truth=st.dictionaries(_field_name, st.text(max_size=30), max_size=15),
    fields=st.dictionaries(
        _field_name,
        st.fixed_dictionaries({"value": _value, "confidence": st.one_of(st.none(), st.integers(0, 100))}),
        max_size=15,
    ),
)
@settings(max_examples=200)
def test_p14_every_ground_truth_field_scored(ground_truth, fields):
    """**Validates: Requirements 15.4** — one entry per ground-truth field,
    even when extraction returned nothing for it."""
    results = evaluate_document(fields, ground_truth)
    assert {r["field"] for r in results} == set(ground_truth.keys())
    assert len(results) == len(ground_truth)
    for r in results:
        assert r["exact_match"] in (0, 1)
        assert 0.0 <= r["levenshtein_sim"] <= 1.0


def test_p14_total_extraction_failure_scores_all_zero():
    """Concrete: empty fields → every ground-truth field present with exact_match=0."""
    gt = {"surname": "GARCIA", "date_of_birth": "1990-01-15"}
    results = evaluate_document({}, gt)
    assert len(results) == 2
    assert all(r["exact_match"] == 0 for r in results)
    assert all(r["extracted"] is None for r in results)


# ---------------------------------------------------------------------------
# P14 — summary aggregation
# ---------------------------------------------------------------------------

_doc_result = st.fixed_dictionaries(
    {
        "document_id": st.uuids().map(str),
        "doc_type": st.sampled_from(["esp_id", "grc_passport", "srb_passport"]),
        "blueprint_id": st.sampled_from(["id_card", "passport"]),
        "latency_ms": st.one_of(st.none(), st.integers(1, 60000)),
        "fields": st.lists(
            st.fixed_dictionaries(
                {
                    "field": _field_name,
                    "exact_match": st.integers(0, 1),
                    "levenshtein_sim": st.floats(0, 1),
                    "confidence": st.one_of(st.none(), st.integers(0, 100)),
                }
            ),
            max_size=10,
        ),
    }
)


@given(doc_results=st.lists(_doc_result, max_size=20))
@settings(max_examples=100)
def test_p14_summary_bounded_and_complete(doc_results):
    """**Validates: Requirements 15.5** — all accuracies in [0,1], calibration
    means in [0,100], cost non-negative, every seen field/type present."""
    report = summarize(doc_results)
    assert report["documents"] == len(doc_results)
    for acc in list(report["per_field_accuracy"].values()) + list(report["per_type_accuracy"].values()):
        assert acc is None or 0.0 <= acc <= 1.0
    cal = report["calibration"]
    for key in ("mean_conf_correct", "mean_conf_incorrect"):
        assert cal[key] is None or 0 <= cal[key] <= 100
    assert report["gpu_cost_estimate_usd"] >= 0
    seen_fields = {e["field"] for d in doc_results for e in d["fields"]}
    assert set(report["per_field_accuracy"].keys()) == seen_fields


def test_p14_percentile_edge_cases():
    assert percentile([], 50) is None
    assert percentile([7], 50) == 7
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
