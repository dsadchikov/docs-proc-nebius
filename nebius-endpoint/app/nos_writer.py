"""Best-effort, non-blocking NOS (Nebius Object Storage) write helpers.

Called via asyncio.create_task() — failures are logged but never propagate
to the caller, and never affect the HTTP response.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import Config

logger = logging.getLogger("app.nos_writer")


def _get_s3_client():
    """Return boto3 S3 client for NOS, or None if not configured."""
    if not Config.S3_ACCESS_KEY or not Config.S3_SECRET_KEY:
        return None
    return boto3.client(
        "s3",
        endpoint_url=Config.S3_ENDPOINT,
        aws_access_key_id=Config.S3_ACCESS_KEY,
        aws_secret_access_key=Config.S3_SECRET_KEY,
        region_name=Config.S3_REGION,
        config=BotoConfig(signature_version="s3v4"),
    )


def _time_partitioned_key(prefix: str, filename: str) -> str:
    """Build NOS key: prefix/YYYY/MM/DD/HH/mm/filename"""
    now = datetime.now(timezone.utc)
    return now.strftime(f"{prefix}/%Y/%m/%d/%H/%M/{filename}")


async def write_outbound(request_id: str, result: dict) -> None:
    """Write Recognition_Result JSON to outbound/YYYY/MM/DD/HH/mm/<request_id>.json.

    Best-effort: logs errors but never raises. Call via asyncio.create_task().
    Requirement 9.4
    """
    s3 = _get_s3_client()
    if not s3:
        logger.debug("NOS not configured — skipping outbound write for request_id=%s", request_id)
        return
    key = _time_partitioned_key("outbound", f"{request_id}.json")
    try:
        body = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
        s3.put_object(
            Bucket=Config.S3_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        logger.info("nos_writer: outbound written key=%s", key)
    except ClientError as exc:
        logger.error("nos_writer: failed to write outbound key=%s error=%s", key, exc)
    except Exception as exc:
        logger.error("nos_writer: unexpected error writing outbound key=%s error=%s", key, exc)
