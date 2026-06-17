"""
test_extractor.py — Property-based tests for parse_model_response and related functions.

Validates: Requirements 2.1, 2.2, 2.3, 2.4

Properties covered:
  P3 — Required fields are never omitted from the result
  P4 — Confidence scores are always integers in [0, 100]
  P5 — Result contains exactly the fields declared in the blueprint (no extras, no missing)
"""

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
import hypothesis.strategies as st

from app.extractor import parse_model_response, calculate_document_confidence
from app.blueprint_loader import _normalize
from app.models import RecognizeOptions, FieldResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BLUEPRINTS_DIR = Path(__file__).parent.parent / "blueprints"


def _load_blueprint(blueprint_id: str) -> dict:
    """Load and normalise a real blueprint from disk."""
    path = BLUEPRINTS_DIR / blueprint_id / "v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _normalize(raw)


def _make_blueprint(field_specs: list[dict]) -> dict:
    """Build a minimal flat blueprint dict from a list of field specs."""
    return {
        "id": "test_bp",
        "fields": field_specs,
        "extraction_prompt": "Extract fields.",
    }


def _make_field_spec(name: str, required: bool) -> dict:
    return {
        "name": name,
        "description": "",
        "instruction": "",
        "inferenceType": "explicit",
        "required": required,
    }


# Hypothesis strategy: generate a non-empty list of unique ASCII word field names
field_name_strategy = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("Ll", "Lu")),
)

unique_field_names = st.lists(
    field_name_strategy,
    min_size=1,
    max_size=6,
    unique=True,
)

default_options = RecognizeOptions(include_confidence=True, confidence_mode="both")


# ===========================================================================
# P3 — Required fields never omitted
# ===========================================================================

# Feature: docs-proc-nebius, Property 3: Required fields are never omitted
# Validates: Requirements 2.1, 2.2

@given(
    field_names=unique_field_names,
    required_flags=st.lists(st.booleans(), min_size=1, max_size=6),
    present_indices=st.lists(st.integers(min_value=0, max_value=5), min_size=0, max_size=6, unique=True),
)
@settings(max_examples=200)
def test_p3_required_fields_never_omitted(
    field_names: list[str],
    required_flags: list[bool],
    present_indices: list[int],
) -> None:
    """P3: For any blueprint with some required and some optional fields,
    parse_model_response always returns every required field as a key in the
    result dict — whether or not the VLM included it in its JSON response.

    Validates: Requirements 2.1, 2.2
    """
    # Align lengths
    n = min(len(field_names), len(required_flags))
    if n == 0:
        return
    names = field_names[:n]
    flags = required_flags[:n]

    # Build blueprint field specs
    field_specs = [_make_field_spec(name, req) for name, req in zip(names, flags)]
    blueprint = _make_blueprint(field_specs)

    # Build VLM response JSON with a subset of fields
    present = {names[i]: "TEST_VALUE" for i in present_indices if i < n}
    raw_json = json.dumps(present)

    result = parse_model_response(raw_json, blueprint, default_options)

    # Every required field must be present in the result
    for spec in field_specs:
        if spec["required"]:
            assert spec["name"] in result, (
                f"Required field '{spec['name']}' is missing from result. "
                f"Blueprint fields: {[s['name'] for s in field_specs]}, "
                f"VLM returned: {list(present.keys())}"
            )


@pytest.mark.parametrize("blueprint_id", ["passport", "residence_permit_ltu_front"])
def test_p3_empty_vlm_response_all_fields_present(blueprint_id: str) -> None:
    """P3 (concrete): When VLM returns an empty JSON {}, all blueprint fields
    appear in the result — required fields with value=None, confidence=0.

    Validates: Requirements 2.1, 2.2
    """
    blueprint = _load_blueprint(blueprint_id)
    options = RecognizeOptions(include_confidence=True, confidence_mode="both")

    result = parse_model_response("{}", blueprint, options)

    for field in blueprint["fields"]:
        name = field["name"]
        assert name in result, (
            f"Field '{name}' (required={field['required']}) missing from result "
            f"for blueprint '{blueprint_id}' with empty VLM response"
        )
        fr = result[name]
        assert fr.value is None, f"Expected value=None for missing field '{name}', got {fr.value!r}"
        assert fr.confidence == 0, f"Expected confidence=0 for missing field '{name}', got {fr.confidence}"


# ===========================================================================
# P4 — Confidence scores are integers in [0, 100]
# ===========================================================================

# Feature: docs-proc-nebius, Property 4: Confidence scores are integers in [0, 100]
# Validates: Requirements 2.3, 2.4

@given(
    field_names=unique_field_names,
    raw_confidences=st.lists(
        st.one_of(
            st.integers(min_value=-200, max_value=200),
            st.floats(min_value=-5.0, max_value=150.0, allow_nan=False),
        ),
        min_size=1,
        max_size=6,
    ),
)
@settings(max_examples=200)
def test_p4_confidence_always_int_in_range(
    field_names: list[str],
    raw_confidences: list,
) -> None:
    """P4: For any numeric confidence value a VLM might return (including
    out-of-range values like -5, 150, 0.7, 99.9), parse_model_response always
    produces an integer confidence in [0, 100] for each field.

    Validates: Requirements 2.3, 2.4
    """
    n = min(len(field_names), len(raw_confidences))
    if n == 0:
        return
    names = field_names[:n]
    confs = raw_confidences[:n]

    # Build blueprint
    field_specs = [_make_field_spec(name, True) for name in names]
    blueprint = _make_blueprint(field_specs)

    # Build VLM response with raw (possibly out-of-range) confidence values
    vlm_response = {name: {"value": "TEST", "confidence": conf} for name, conf in zip(names, confs)}
    raw_json = json.dumps(vlm_response)

    options = RecognizeOptions(include_confidence=True, confidence_mode="both")
    result = parse_model_response(raw_json, blueprint, options)

    for name in names:
        assert name in result
        fr = result[name]
        assert fr.confidence is not None, f"Field '{name}' has no confidence set"
        assert isinstance(fr.confidence, int), (
            f"Field '{name}' confidence is {type(fr.confidence).__name__}, expected int"
        )
        assert 0 <= fr.confidence <= 100, (
            f"Field '{name}' confidence={fr.confidence} is outside [0, 100]"
        )


@given(
    field_names=unique_field_names,
    confidences=st.lists(
        st.integers(min_value=0, max_value=100),
        min_size=1,
        max_size=6,
    ),
)
@settings(max_examples=200)
def test_p4_calculate_document_confidence_in_range(
    field_names: list[str],
    confidences: list[int],
) -> None:
    """P4 (calculate_document_confidence): Given any dict of FieldResults with
    valid per-field confidences, calculate_document_confidence returns an int
    in [0, 100].

    Validates: Requirement 2.4
    """
    n = min(len(field_names), len(confidences))
    if n == 0:
        return
    fields = {
        name: FieldResult(value="TEST", confidence=conf)
        for name, conf in zip(field_names[:n], confidences[:n])
    }

    doc_conf = calculate_document_confidence(fields)

    assert isinstance(doc_conf, int), (
        f"calculate_document_confidence returned {type(doc_conf).__name__}, expected int"
    )
    assert 0 <= doc_conf <= 100, (
        f"calculate_document_confidence returned {doc_conf}, outside [0, 100]"
    )


def test_p4_out_of_range_confidences_clamped() -> None:
    """P4 (concrete): VLM returns confidences of -10, 150, 0.7, 99.9 — all
    must be clamped to valid integers in [0, 100].

    Validates: Requirements 2.3, 2.4
    """
    blueprint = _make_blueprint([
        _make_field_spec("field_negative", True),
        _make_field_spec("field_over_100", True),
        _make_field_spec("field_float_low", True),
        _make_field_spec("field_float_high", True),
    ])
    vlm_response = json.dumps({
        "field_negative":  {"value": "A", "confidence": -10},
        "field_over_100":  {"value": "B", "confidence": 150},
        "field_float_low": {"value": "C", "confidence": 0.7},
        "field_float_high": {"value": "D", "confidence": 99.9},
    })

    options = RecognizeOptions(include_confidence=True, confidence_mode="both")
    result = parse_model_response(vlm_response, blueprint, options)

    for name, fr in result.items():
        assert isinstance(fr.confidence, int), f"{name}: expected int confidence"
        assert 0 <= fr.confidence <= 100, f"{name}: confidence {fr.confidence} out of range"

    # Specific expected clamps
    assert result["field_negative"].confidence == 0
    assert result["field_over_100"].confidence == 100
    assert result["field_float_low"].confidence == 1   # round(0.7) = 1
    assert result["field_float_high"].confidence == 100  # round(99.9) = 100


# ===========================================================================
# P5 — Fields are schema-valid against the blueprint
# ===========================================================================

# Feature: docs-proc-nebius, Property 5: Result contains exactly the blueprint fields
# Validates: Requirements 2.1, 2.2, 2.3

@given(
    field_names=unique_field_names,
    extra_names=st.lists(
        st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Ll", "Lu"))),
        min_size=0,
        max_size=3,
    ),
    present_count=st.integers(min_value=0, max_value=6),
)
@settings(max_examples=200)
def test_p5_result_contains_exactly_blueprint_fields(
    field_names: list[str],
    extra_names: list[str],
    present_count: int,
) -> None:
    """P5: The result of parse_model_response contains exactly the fields
    declared in the blueprint — no extras (even if VLM hallucinates unknown
    fields), no missing (even if VLM omits some fields).

    Validates: Requirements 2.1, 2.2, 2.3
    """
    if not field_names:
        return

    # Ensure extra_names don't accidentally overlap with field_names
    extra_names_clean = [n for n in extra_names if n not in field_names]

    field_specs = [_make_field_spec(name, True) for name in field_names]
    blueprint = _make_blueprint(field_specs)

    # VLM returns: a subset of blueprint fields + hallucinated extra fields
    n_present = min(present_count, len(field_names))
    present_fields = {name: "VALUE" for name in field_names[:n_present]}
    hallucinated = {name: "HALLUCINATED" for name in extra_names_clean}
    vlm_response = json.dumps({**present_fields, **hallucinated})

    options = RecognizeOptions(include_confidence=True, confidence_mode="both")
    result = parse_model_response(vlm_response, blueprint, options)

    expected_keys = {f["name"] for f in field_specs}

    # Exactly blueprint fields — no more, no less
    assert set(result.keys()) == expected_keys, (
        f"Result keys {set(result.keys())} != blueprint fields {expected_keys}. "
        f"VLM had present={list(present_fields.keys())}, hallucinated={list(hallucinated.keys())}"
    )

    # All values are FieldResult instances
    for name, fr in result.items():
        assert isinstance(fr, FieldResult), (
            f"result['{name}'] is {type(fr).__name__}, expected FieldResult"
        )


def test_p5_hallucinated_fields_excluded_missing_fields_present() -> None:
    """P5 (concrete): Blueprint has 5 known fields. VLM returns 3 of them plus
    2 unknown hallucinated fields. Result must have exactly 5 keys matching the
    blueprint (hallucinated excluded, missing fields present with value=None).

    Validates: Requirements 2.1, 2.2, 2.3
    """
    blueprint_fields = ["surname", "given_names", "date_of_birth", "document_number", "issuing_country"]
    blueprint = _make_blueprint([_make_field_spec(name, True) for name in blueprint_fields])

    # VLM returns 3 blueprint fields + 2 hallucinated extras
    vlm_response = json.dumps({
        "surname":          {"value": "SMITH",    "confidence": 90},
        "given_names":      {"value": "JOHN",     "confidence": 85},
        "date_of_birth":    {"value": "1985-01-01","confidence": 88},
        "hallucinated_one": {"value": "UNKNOWN1", "confidence": 70},
        "hallucinated_two": {"value": "UNKNOWN2", "confidence": 60},
    })

    options = RecognizeOptions(include_confidence=True, confidence_mode="both")
    result = parse_model_response(vlm_response, blueprint, options)

    # Exactly blueprint fields
    assert set(result.keys()) == set(blueprint_fields), (
        f"Expected exactly {blueprint_fields}, got {list(result.keys())}"
    )

    # Present fields have real values
    assert result["surname"].value == "SMITH"
    assert result["given_names"].value == "JOHN"
    assert result["date_of_birth"].value == "1985-01-01"

    # Missing blueprint fields present with value=None
    assert result["document_number"].value is None
    assert result["issuing_country"].value is None

    # Hallucinated fields not present
    assert "hallucinated_one" not in result
    assert "hallucinated_two" not in result


@pytest.mark.parametrize("blueprint_id", ["passport", "residence_permit_ltu_front"])
def test_p5_real_blueprint_field_set_exact(blueprint_id: str) -> None:
    """P5 (concrete, real blueprints): When called with a real normalised
    blueprint, the result has exactly the blueprint's declared fields.

    Validates: Requirements 2.1, 2.2
    """
    blueprint = _load_blueprint(blueprint_id)
    expected_keys = {f["name"] for f in blueprint["fields"]}

    # VLM returns only half the fields (simulate partial response)
    half = list(expected_keys)[: len(expected_keys) // 2]
    vlm_response = json.dumps({name: {"value": "X", "confidence": 80} for name in half})

    options = RecognizeOptions(include_confidence=True, confidence_mode="both")
    result = parse_model_response(vlm_response, blueprint, options)

    assert set(result.keys()) == expected_keys, (
        f"[{blueprint_id}] Result keys mismatch: "
        f"extra={set(result.keys()) - expected_keys}, "
        f"missing={expected_keys - set(result.keys())}"
    )
    for name, fr in result.items():
        assert isinstance(fr, FieldResult), f"[{blueprint_id}] result['{name}'] is not FieldResult"


# ---------------------------------------------------------------------------
# P11 — Logprob confidence is well-defined for any token sequence
#
# Validates: Requirements 13.2, 13.3 (Design P11)
# ---------------------------------------------------------------------------

from app.extractor import logprob_confidence, blueprint_to_guided_schema  # noqa: E402

_token_entry = st.fixed_dictionaries(
    {
        "token": st.text(min_size=0, max_size=8),
        "logprob": st.floats(min_value=-50.0, max_value=0.0, allow_nan=False),
    }
)


@given(
    value=st.one_of(st.none(), st.text(max_size=40)),
    logprobs_content=st.one_of(st.none(), st.lists(_token_entry, max_size=60)),
    field_name=st.text(max_size=15),
)
@settings(max_examples=300)
def test_p11_logprob_confidence_bounded_never_raises(value, logprobs_content, field_name):
    """P11: For any (value, logprobs, field_name), logprob_confidence returns
    an int in [0, 100] (or None) and a valid source — never an exception.

    Validates: Requirements 13.2, 13.3
    """
    conf, source = logprob_confidence(value, logprobs_content, field_name)
    assert source in ("logprobs", "response_mean", "model_reported")
    if conf is not None:
        assert isinstance(conf, int)
        assert 0 <= conf <= 100
    else:
        assert source == "model_reported"


def test_p11_known_span_yields_logprobs_source():
    """P11 (concrete): a value present verbatim in the token stream is scored
    from its own tokens with source='logprobs'; exp(-0.05) ≈ 0.951 → 95.
    """
    text = '{"surname": "SMITH"}'
    lp = [{"token": text[i:i+2], "logprob": -0.05} for i in range(0, len(text), 2)]
    conf, source = logprob_confidence("SMITH", lp, "surname")
    assert source == "logprobs"
    assert conf == 95


def test_p11_missing_span_falls_back_to_response_mean():
    """P11 (concrete): value absent from the stream → whole-response mean."""
    lp = [{"token": "abc", "logprob": -1.0}, {"token": "def", "logprob": -1.0}]
    conf, source = logprob_confidence("ZZZ_NOT_THERE", lp, "x")
    assert source == "response_mean"
    assert conf == 37  # round(100 * exp(-1.0))


def test_p11_no_logprobs_is_model_reported():
    conf, source = logprob_confidence("anything", None, "f")
    assert (conf, source) == (None, "model_reported")


def test_p11_bpe_space_tokens_yield_logprobs_not_response_mean():
    """Regression (v29 bug, confirmed live): vLLM emits GPT-2 byte-level BPE
    tokens where a space is `Ġ` (U+0120) and a newline is `Ċ` (U+010A), NOT
    literal whitespace. The old "".join(tokens) substring search missed every
    value containing a space (multi-word names, spaced dates) and fell back to
    response_mean. logprob_confidence must decode the byte-level pieces and find
    the span → source='logprobs'.

    Validates: Requirements 13.2, 13.3
    """
    # Real capture shape: {"full_name": "ROBERTO CORONADO VALVERDE", ...}
    tokens = ['{Ċ', 'Ġ', 'Ġ"', 'full', '_name', '":', 'Ġ"',
              'RO', 'BERT', 'O', 'ĠCOR', 'ON', 'ADO', 'ĠVAL', 'VER', 'DE', '"']
    lp = [{"token": t, "logprob": -0.05} for t in tokens]

    conf, source = logprob_confidence("ROBERTO CORONADO VALVERDE", lp, "full_name")
    assert source == "logprobs", "spaced value must resolve via decoded byte span"
    assert conf == 95  # exp(-0.05) ≈ 0.951

    # A spaced date likewise resolves (no longer response_mean)
    date_tokens = ['"', 'date', '":', 'Ġ"', '27', 'Ġ01', 'Ġ', '1978', '"']
    lp2 = [{"token": t, "logprob": -0.1} for t in date_tokens]
    conf2, source2 = logprob_confidence("27 01 1978", lp2, "date")
    assert source2 == "logprobs"


# ---------------------------------------------------------------------------
# P12 — Guided schema matches blueprint exactly
#
# Validates: Requirements 14.1, 14.3 (Design P12)
# ---------------------------------------------------------------------------

_field_name = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)


@given(names=st.lists(_field_name, min_size=0, max_size=25, unique=True))
@settings(max_examples=200)
def test_p12_guided_schema_matches_blueprint_fields(names):
    """P12: schema properties == blueprint field names exactly, all required,
    additionalProperties forbidden, every property nullable string.

    Validates: Requirements 14.1, 14.3
    """
    blueprint = _make_blueprint([_make_field_spec(n, required=True) for n in names])
    schema = blueprint_to_guided_schema(blueprint)
    assert set(schema["properties"].keys()) == set(names)
    assert set(schema["required"]) == set(names)
    assert schema["additionalProperties"] is False
    for prop in schema["properties"].values():
        assert prop == {"type": ["string", "null"]}


@pytest.mark.parametrize("blueprint_id", ["passport", "residence_permit_ltu_front", "default"])
def test_p12_real_blueprints_produce_valid_schema(blueprint_id):
    """P12 (concrete): real blueprints produce schemas keyed exactly by their fields."""
    blueprint = _load_blueprint(blueprint_id)
    schema = blueprint_to_guided_schema(blueprint)
    assert set(schema["properties"].keys()) == {f["name"] for f in blueprint["fields"]}


# ---------------------------------------------------------------------------
# P13 — Packet grouping is a partition of pages
#
# Validates: Requirements 16.1, 16.2, 16.3 (Design P13)
# ---------------------------------------------------------------------------

from app.extractor import group_consecutive_pages, most_conservative_routing  # noqa: E402

_bp_id = st.one_of(st.none(), st.sampled_from(["passport", "id_card", "default", "x"]))


@given(page_blueprints=st.lists(_bp_id, max_size=50))
@settings(max_examples=300)
def test_p13_grouping_is_partition(page_blueprints):
    """P13: groups are non-empty, disjoint, consecutive, ascending, and jointly
    cover every page exactly once.

    Validates: Requirements 16.1, 16.2
    """
    groups = group_consecutive_pages(page_blueprints)
    all_pages = [p for g in groups for p in g["pages"]]
    assert all_pages == list(range(1, len(page_blueprints) + 1))  # exact cover, in order
    for g in groups:
        assert g["pages"], "empty group"
        assert g["pages"] == list(range(g["pages"][0], g["pages"][-1] + 1)), "non-consecutive group"
        # every page in the group has the group's blueprint_id
        for p in g["pages"]:
            assert page_blueprints[p - 1] == g["blueprint_id"]
    # adjacent groups differ — otherwise they would have been merged
    for a, b in zip(groups, groups[1:]):
        assert a["blueprint_id"] != b["blueprint_id"]


_routing = st.one_of(st.none(), st.sampled_from(["auto_classified", "review_required", "escalate_to_operator"]))


@given(routings=st.lists(_routing, max_size=20))
@settings(max_examples=200)
def test_p13_top_routing_is_most_conservative(routings):
    """P13: top-level routing equals the worst per-document routing.

    Validates: Requirements 16.3
    """
    top = most_conservative_routing(routings)
    known = [r for r in routings if r is not None]
    if not known:
        assert top is None
    else:
        severity = {"auto_classified": 0, "review_required": 1, "escalate_to_operator": 2}
        assert severity[top] == max(severity[r] for r in known)
