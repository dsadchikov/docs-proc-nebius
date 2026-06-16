"""Minimal in-process metrics with Prometheus text exposition (no dependency).

Aggregate-only (no per-document or PII data), safe to expose like /health.
Scrapeable by Nebius Managed Prometheus.
"""
import threading

_lock = threading.Lock()
# requests_total keyed by (mode, routing)
_requests_total: dict = {}
# request_duration_seconds — running sum + count (a simple average/total view)
_duration_sum = 0.0
_duration_count = 0


def record_request(mode: str, routing: str, duration_s: float) -> None:
    global _duration_sum, _duration_count
    key = (mode or "unknown", routing or "unknown")
    with _lock:
        _requests_total[key] = _requests_total.get(key, 0) + 1
        _duration_sum += duration_s
        _duration_count += 1


def render(vllm_up: bool) -> str:
    """Render current metrics in Prometheus text exposition format."""
    lines = [
        "# HELP docproc_requests_total Total /recognize requests by mode and routing.",
        "# TYPE docproc_requests_total counter",
    ]
    with _lock:
        items = sorted(_requests_total.items())
        d_sum, d_count = _duration_sum, _duration_count
    for (mode, routing), count in items:
        lines.append(
            f'docproc_requests_total{{mode="{mode}",routing="{routing}"}} {count}'
        )
    lines += [
        "# HELP docproc_request_duration_seconds_sum Cumulative /recognize wall time.",
        "# TYPE docproc_request_duration_seconds_sum counter",
        f"docproc_request_duration_seconds_sum {d_sum:.3f}",
        "# HELP docproc_request_duration_seconds_count Completed /recognize requests.",
        "# TYPE docproc_request_duration_seconds_count counter",
        f"docproc_request_duration_seconds_count {d_count}",
        "# HELP docproc_vllm_up vLLM backend reachable (1) or not (0).",
        "# TYPE docproc_vllm_up gauge",
        f"docproc_vllm_up {1 if vllm_up else 0}",
    ]
    return "\n".join(lines) + "\n"
