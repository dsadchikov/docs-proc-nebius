"""Evaluation metrics for the MIDV-2020 benchmark (Req 15.4, 15.5).

Pure functions — no I/O, no network — so they are property-testable (P14).
"""


_MONTH_MAP = {
    # English
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    # Spanish
    "ene": "01", "abr": "04", "ago": "08", "dic": "12",
    # Greek (transliterated)
    "ιαν": "01", "φεβ": "02", "μαρ": "03", "απρ": "04", "μαϊ": "05", "ιουν": "06",
    "ιουλ": "07", "αυγ": "08", "σεπ": "09", "οκτ": "10", "νοε": "11", "δεκ": "12",
    # Serbian/Croatian
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "maj": "05", "jun": "06",
    "jul": "07", "avg": "08", "sep": "09", "okt": "10", "nov": "11", "dec": "12",
}


def _normalize_date(s: str) -> str:
    """Collapse common date formats to YYYYMMDD for comparison.

    Handles: YYYY-MM-DD, DD.MM.YYYY, DD MMM YYYY (with month names), YYYYMMDD.
    Returns the original string if no pattern matches.
    """
    import re
    s = s.strip()
    # YYYY-MM-DD or YYYY/MM/DD
    m = re.fullmatch(r"(\d{4})[-/](\d{2})[-/](\d{2})", s)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    # DD.MM.YYYY or DD/MM/YYYY or DD-MM-YYYY
    m = re.fullmatch(r"(\d{2})[-./](\d{2})[-./](\d{4})", s)
    if m:
        return m.group(3) + m.group(2) + m.group(1)
    # DD MMM YYYY or DD-MMM-YYYY or DD/MMM/YYYY (e.g. 20 ENE 1990, 15 JAN 2005)
    m = re.fullmatch(r"(\d{1,2})[\s\-/]([a-zα-ωά-ώ]{3,4})[\s\-/](\d{4})", s, re.IGNORECASE)
    if m:
        month_str = m.group(2).lower()
        month_num = _MONTH_MAP.get(month_str)
        if month_num:
            return m.group(3) + month_num + m.group(1).zfill(2)
    # YYYYMMDD already
    if re.fullmatch(r"\d{8}", s):
        return s
    return s


def _normalize_id_number(s: str) -> str:
    """Strip separators from ID/personal numbers: spaces, hyphens, dots."""
    import re
    return re.sub(r"[\s\-.]", "", s)


def normalize(value) -> str:
    """Whitespace/case normalization applied before comparison (Req 15.4).

    Dates are collapsed to YYYYMMDD; ID numbers have separators stripped.
    """
    if value is None:
        return ""
    import re
    s = " ".join(str(value).split()).casefold()
    # Date normalization
    if re.search(r"\d{4}[-/]\d{2}[-/]\d{2}"
                 r"|\d{2}[-./]\d{2}[-./]\d{4}"
                 r"|\d{1,2}[\s\-/][a-z]{3}[\s\-/]\d{4}"
                 r"|\b\d{8}\b", s, re.IGNORECASE):
        return _normalize_date(s)
    # Personal/document number: strip separators if looks like an ID
    if re.search(r"\d{5,}", s):
        return _normalize_id_number(s)
    return s


def exact_match(extracted, expected) -> int:
    return int(normalize(extracted) == normalize(expected))


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def levenshtein_sim(extracted, expected) -> float:
    """Normalized similarity in [0, 1]: 1 − dist / max(len). Both empty → 1.0."""
    a, b = normalize(extracted), normalize(expected)
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - levenshtein(a, b) / longest


def evaluate_document(fields: dict, ground_truth: dict) -> list:
    """Per-field comparison for one document (Req 15.4).

    `fields` is recognition_result["fields"] ({name: {value, confidence, ...}}),
    `ground_truth` is {name: expected}. Returns one entry per ground-truth
    field — extraction failures appear as exact_match=0, never as missing (P14).
    """
    results = []
    fields = fields or {}
    for name, expected in ground_truth.items():
        fr = fields.get(name) or {}
        extracted = fr.get("value") if isinstance(fr, dict) else None
        results.append({
            "field": name,
            "extracted": extracted,
            "expected": expected,
            "exact_match": exact_match(extracted, expected),
            "levenshtein_sim": round(levenshtein_sim(extracted, expected), 4),
            "confidence": fr.get("confidence") if isinstance(fr, dict) else None,
            "confidence_source": fr.get("confidence_source") if isinstance(fr, dict) else None,
        })
    return results


def percentile(values: list, p: float):
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(doc_results: list, gpu_hourly_usd: float = 2.80) -> dict:
    """Aggregate per-document eval results into the summary report (Req 15.5).

    `doc_results`: [{document_id, doc_type, blueprint_id, latency_ms, fields: [entry]}]
    """
    field_hits, type_hits = {}, {}
    conf_correct, conf_incorrect = [], []
    latencies = []

    for doc in doc_results:
        if doc.get("latency_ms") is not None:
            latencies.append(doc["latency_ms"])
        doc_type = doc.get("doc_type") or doc.get("blueprint_id") or "unknown"
        for entry in doc.get("fields", []):
            field_hits.setdefault(entry["field"], []).append(entry["exact_match"])
            type_hits.setdefault(doc_type, []).append(entry["exact_match"])
            if entry.get("confidence") is not None:
                (conf_correct if entry["exact_match"] else conf_incorrect).append(entry["confidence"])

    def _mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    total_s = sum(latencies) / 1000 if latencies else 0
    return {
        "documents": len(doc_results),
        "doc_types": sorted({d.get("doc_type") or d.get("blueprint_id") or "unknown" for d in doc_results}),
        "per_field_accuracy": {k: _mean(v) for k, v in sorted(field_hits.items())},
        "per_type_accuracy": {k: _mean(v) for k, v in sorted(type_hits.items())},
        "calibration": {
            "mean_conf_correct": _mean(conf_correct),
            "mean_conf_incorrect": _mean(conf_incorrect),
            "n_correct": len(conf_correct),
            "n_incorrect": len(conf_incorrect),
        },
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "gpu_cost_estimate_usd": round(total_s / 3600 * gpu_hourly_usd, 4),
    }
