"""
router.py — Canonical routing and confidence-clamping logic.

These are pure functions with no dependencies, making them easy to test in
isolation (no GPU, no httpx, no pdf2image required).

extractor.py imports from here to avoid duplication.
test_router.py imports from here for property-based testing.
"""


def clamp_confidence(raw: float) -> int:
    """Clamp any numeric value to an integer in [0, 100].

    For any finite float input, returns a Python int in the closed range
    [0, 100]. Non-integer values are rounded before clamping.
    Infinity and values beyond int range are handled safely via min/max.

    Validates: Requirement 3.4
    """
    if raw != raw:  # NaN guard (though Hypothesis excludes NaN per allow_nan=False)
        return 0
    # Clamp to float bounds before rounding to avoid OverflowError on inf/-inf
    clamped_float = max(0.0, min(100.0, float(raw)))
    return int(round(clamped_float))


def get_routing(confidence: int) -> str:
    """Map a clamped confidence integer to a routing string.

    Bands (exhaustive and mutually exclusive over [0, 100]):
      [85, 100] → "auto_classified"
      [50,  84] → "review_required"
      [ 0,  49] → "escalate_to_operator"

    Validates: Requirements 3.1, 3.2, 3.3, 3.5
    """
    if confidence >= 85:
        return "auto_classified"
    if confidence >= 50:
        return "review_required"
    return "escalate_to_operator"


__all__ = ["clamp_confidence", "get_routing"]
