"""
test_router.py — Property-based tests for routing logic.

Validates Requirements 3.1, 3.2, 3.3, 3.4, 3.5 using Hypothesis.
"""

import math

import pytest
from hypothesis import given, settings
import hypothesis.strategies as st

from app.router import clamp_confidence, get_routing

# The complete set of valid routing values (Req 3.1–3.3, 3.5)
VALID_ROUTING_VALUES = {"auto_classified", "review_required", "escalate_to_operator"}


# Feature: docs-proc-nebius, Property 1: Routing is always a valid, unique value
@given(st.integers(min_value=0, max_value=100))
@settings(max_examples=200)
def test_routing_always_valid_and_unique(confidence: int) -> None:
    """Property 1: For any integer confidence in [0, 100], get_routing returns exactly
    one value from the set of valid routing strings.

    Also verifies the three bands are mutually exclusive — exactly one condition matches.

    Validates: Requirements 3.1, 3.2, 3.3, 3.5
    """
    result = get_routing(confidence)

    # The result must be one of the three valid routing values
    assert result in VALID_ROUTING_VALUES, (
        f"get_routing({confidence!r}) returned {result!r}, "
        f"which is not in {VALID_ROUTING_VALUES}"
    )

    # Verify mutual exclusivity: exactly one band matches this confidence value
    matches_auto = confidence >= 85
    matches_review = 50 <= confidence < 85
    matches_escalate = confidence < 50

    matched_bands = sum([matches_auto, matches_review, matches_escalate])
    assert matched_bands == 1, (
        f"Confidence {confidence} matched {matched_bands} bands "
        f"(expected exactly 1): auto={matches_auto}, "
        f"review={matches_review}, escalate={matches_escalate}"
    )

    # Cross-check: routing result is consistent with the matched band
    if matches_auto:
        assert result == "auto_classified"
    elif matches_review:
        assert result == "review_required"
    else:
        assert result == "escalate_to_operator"


# Feature: docs-proc-nebius, Property 2: Out-of-range confidence is clamped then routed
@given(st.floats(allow_nan=False))
@settings(max_examples=500)
def test_clamp_then_route_for_any_float(raw: float) -> None:
    """Property 2: For any finite (non-NaN) float, clamp_confidence returns an int in
    [0, 100] and get_routing on that clamped value returns a valid routing string.

    Validates: Requirements 3.4, 3.5
    """
    # clamp_confidence must return a Python int
    clamped = clamp_confidence(raw)
    assert isinstance(clamped, int), (
        f"clamp_confidence({raw!r}) returned {clamped!r} (type {type(clamped).__name__}), "
        f"expected int"
    )

    # The clamped value must be in [0, 100]
    assert 0 <= clamped <= 100, (
        f"clamp_confidence({raw!r}) = {clamped}, which is outside [0, 100]"
    )

    # get_routing on the clamped value must return a valid routing string
    routing = get_routing(clamped)
    assert routing in VALID_ROUTING_VALUES, (
        f"get_routing({clamped!r}) returned {routing!r} "
        f"(raw input was {raw!r}), which is not in {VALID_ROUTING_VALUES}"
    )
