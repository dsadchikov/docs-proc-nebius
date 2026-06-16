"""Structured JSON logging (stdlib only, no dependencies).

Emits one JSON object per log line so Nebius log ingestion / any aggregator can
parse fields directly. Extra context (e.g. request_id) passed via
`logger.info(..., extra={"request_id": rid})` is included automatically.
"""
import json
import logging
import time

# Attributes present on every LogRecord — anything else is treated as "extra".
_RESERVED = set(logging.makeLogRecord({}).__dict__.keys()) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root handler (idempotent).

    Also redirects uvicorn's own loggers (which ship their own handlers with
    propagate=False) to the root JSON handler, so server/access lines are JSON
    too — not just application logs.
    """
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
