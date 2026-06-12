import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from app.config import Config

logger = logging.getLogger("app.blueprints")

# ---------------------------------------------------------------------------
# Normalisation: Rich Format (sections) → flat fields[]
# ---------------------------------------------------------------------------

def _normalize(rich: dict) -> dict:
    """Flatten Rich Blueprint Format sections → fields[] for the Extractor.

    Rich format stores fields nested under named sections:
        { "sections": { "SECTION_NAME": { "field_name": { inferenceType, instruction, required } } } }

    Extractor expects a flat list:
        { "fields": [ { "name", "description", "instruction", "inferenceType", "required", "_section" } ] }

    If the blueprint already has a flat "fields" list (legacy format) it is returned unchanged.
    If the blueprint has both, sections take precedence.
    """
    if "sections" not in rich:
        # Already flat format or empty — return as-is
        return rich

    fields = []
    for section_name, section_fields in rich["sections"].items():
        if not isinstance(section_fields, dict):
            continue
        for field_name, field_meta in section_fields.items():
            if not isinstance(field_meta, dict):
                continue
            fields.append({
                "name": field_name,
                "description": field_meta.get("instruction", ""),
                "instruction": field_meta.get("instruction", ""),
                "inferenceType": field_meta.get("inferenceType", "explicit"),
                "required": field_meta.get("required", True),
                "_section": section_name,
            })

    normalized = dict(rich)
    normalized["fields"] = fields
    return normalized


# ---------------------------------------------------------------------------
# BlueprintStore
# ---------------------------------------------------------------------------

class BlueprintStore:
    """In-memory blueprint cache backed by Nebius Object Storage (S3-compatible).

    Startup load order:
    1. Try to read blueprints/_catalog.json — load only status="active" entries.
    2. Fallback (catalog missing): scan blueprints/*/ for the highest vN.json per blueprint.
    3. If no S3 credentials — local-only mode (empty cache, no S3 operations).

    All blueprints are normalised via _normalize() before being cached, so the
    Extractor always works with a flat fields[] list regardless of on-disk format.
    """

    CATALOG_KEY = "blueprints/_catalog.json"

    def __init__(self):
        self._cache: dict[str, dict] = {}     # normalised blueprints
        self._raw_cache: dict[str, dict] = {} # rich format (original, for API responses)
        self._s3_client = None
        self._init_s3()
        self._load_all()

    # ------------------------------------------------------------------
    # S3 client
    # ------------------------------------------------------------------

    def _init_s3(self):
        if not Config.S3_ACCESS_KEY or not Config.S3_SECRET_KEY:
            logger.warning("S3 credentials not set - local-only mode")
            return
        try:
            self._s3_client = boto3.client(
                "s3",
                endpoint_url=Config.S3_ENDPOINT,
                aws_access_key_id=Config.S3_ACCESS_KEY,
                aws_secret_access_key=Config.S3_SECRET_KEY,
                region_name=Config.S3_REGION,
                config=BotoConfig(signature_version="s3v4"),
            )
            logger.info("S3 client initialized: %s / bucket=%s", Config.S3_ENDPOINT, Config.S3_BUCKET)
        except Exception as e:
            logger.error("Failed to init S3: %s", e)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_all(self):
        if not self._s3_client:
            logger.info("No S3 credentials — trying local blueprints/ directory")
            self._load_from_local()
            return

        # 1. Try catalog-based loading
        if self._load_via_catalog():
            return

        # 2. Fallback: scan for highest vN.json per blueprint directory
        logger.warning("_catalog.json not found — falling back to directory scan")
        self._load_via_scan()

    def _load_from_local(self):
        """Load blueprints from local filesystem (blueprints/<id>/vN.json or _catalog.json).

        Used when S3 credentials are not configured. Looks for blueprints/ relative to:
        1. /app/blueprints/  (inside container, copied from repo)
        2. <project_root>/nebius-endpoint/blueprints/  (local dev)
        """
        import os
        candidates = [
            Path("/app/blueprints"),
            Path(__file__).parent.parent / "blueprints",
        ]
        bp_dir = None
        for c in candidates:
            if c.exists():
                bp_dir = c
                break

        if not bp_dir:
            logger.info("No local blueprints/ directory found — empty cache")
            return

        # Try _catalog.json first
        catalog_path = bp_dir / "_catalog.json"
        if catalog_path.exists():
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                loaded = 0
                for entry in catalog.get("blueprints", []):
                    if entry.get("status") != "active":
                        continue
                    # path in catalog is relative to bucket root, map to local dir parent
                    rel = entry.get("path", "")
                    local_path = bp_dir.parent / rel if rel else None
                    if not local_path or not local_path.exists():
                        # Try relative to bp_dir
                        parts = Path(rel).parts  # e.g. blueprints/passport/v1.json
                        local_path = bp_dir / Path(*parts[1:]) if len(parts) > 1 else None
                    if local_path and local_path.exists():
                        raw = json.loads(local_path.read_text(encoding="utf-8"))
                        bp_id = raw.get("id", local_path.parent.name)
                        self._raw_cache[bp_id] = raw
                        self._cache[bp_id] = _normalize(raw)
                        loaded += 1
                    else:
                        logger.warning("Local blueprint file not found: %s", rel)
                logger.info("Loaded %d blueprints from local _catalog.json (%s)", loaded, bp_dir)
                return
            except Exception as e:
                logger.warning("Failed to load local _catalog.json: %s", e)

        # Fallback: scan subdirectories for highest vN.json
        loaded = 0
        for sub in sorted(bp_dir.iterdir()):
            if not sub.is_dir():
                continue
            versions = []
            for f in sub.glob("v*.json"):
                try:
                    n = int(f.stem[1:])
                    versions.append((n, f))
                except ValueError:
                    pass
            if versions:
                _, best = max(versions, key=lambda x: x[0])
                try:
                    raw = json.loads(best.read_text(encoding="utf-8"))
                    bp_id = raw.get("id", sub.name)
                    self._raw_cache[bp_id] = raw
                    self._cache[bp_id] = _normalize(raw)
                    loaded += 1
                except Exception as e:
                    logger.error("Failed to load local %s: %s", best, e)
        logger.info("Loaded %d blueprints from local directory scan (%s)", loaded, bp_dir)

    def _load_via_catalog(self) -> bool:
        """Read _catalog.json, load active blueprints. Returns True on success."""
        try:
            resp = self._s3_client.get_object(Bucket=Config.S3_BUCKET, Key=self.CATALOG_KEY)
            catalog = json.loads(resp["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return False
            logger.error("Failed to read _catalog.json: %s", e)
            return False
        except Exception as e:
            logger.error("Failed to parse _catalog.json: %s", e)
            return False

        loaded = 0
        for entry in catalog.get("blueprints", []):
            if entry.get("status") != "active":
                continue
            path = entry.get("path")
            if not path:
                continue
            if self._load_one(path):
                loaded += 1

        logger.info("Loaded %d active blueprints via _catalog.json", loaded)
        return True

    def _load_via_scan(self):
        """Fallback: list blueprints/*/ and load the highest version file per directory."""
        try:
            resp = self._s3_client.list_objects_v2(
                Bucket=Config.S3_BUCKET, Prefix="blueprints/", Delimiter="/"
            )
        except ClientError as e:
            logger.error("Failed to list blueprints/: %s", e)
            return

        for prefix_obj in resp.get("CommonPrefixes", []):
            prefix = prefix_obj.get("Prefix", "")
            if prefix in ("blueprints/", "blueprints/_"):
                continue
            # List files in this subdir and find highest vN.json
            try:
                sub_resp = self._s3_client.list_objects_v2(
                    Bucket=Config.S3_BUCKET, Prefix=prefix
                )
                versions = []
                for obj in sub_resp.get("Contents", []):
                    key = obj["Key"]
                    fname = Path(key).name
                    if fname.startswith("v") and fname.endswith(".json"):
                        try:
                            n = int(fname[1:-5])
                            versions.append((n, key))
                        except ValueError:
                            pass
                if versions:
                    _, best_key = max(versions, key=lambda x: x[0])
                    self._load_one(best_key)
            except ClientError as e:
                logger.error("Failed to scan %s: %s", prefix, e)

        logger.info("Loaded %d blueprints via directory scan", len(self._cache))

    def _load_one(self, key: str) -> bool:
        """Fetch one blueprint file from S3, normalize, cache. Returns True on success."""
        try:
            resp = self._s3_client.get_object(Bucket=Config.S3_BUCKET, Key=key)
            raw = json.loads(resp["Body"].read().decode("utf-8"))
            bp_id = raw.get("id", Path(key).parent.name)
            self._raw_cache[bp_id] = raw
            self._cache[bp_id] = _normalize(raw)
            return True
        except Exception as e:
            logger.error("Failed to load blueprint %s: %s", key, e)
            return False

    def reload(self) -> int:
        self._cache.clear()
        self._raw_cache.clear()
        self._load_all()
        return len(self._cache)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, blueprint_id: str) -> Optional[dict]:
        """Return normalised blueprint (flat fields[]) or None."""
        return self._cache.get(blueprint_id)

    def get_raw(self, blueprint_id: str) -> Optional[dict]:
        """Return original Rich Format blueprint (for API GET responses)."""
        return self._raw_cache.get(blueprint_id)

    def list_all(self) -> list[dict]:
        result = []
        for bp_id, bp in self._raw_cache.items():
            # Count fields: prefer sections count, fall back to flat fields
            if "sections" in bp:
                count = sum(
                    len(v) for v in bp["sections"].values() if isinstance(v, dict)
                )
            else:
                count = len(bp.get("fields", []))
            result.append({
                "id": bp.get("id", bp_id),
                "name": bp.get("name", ""),
                "version": bp.get("version", 1),
                "status": bp.get("status", "active"),
                "description": bp.get("description", ""),
                "fields_count": count,
                "created_at": bp.get("created_at"),
                "updated_at": bp.get("updated_at"),
            })
        return result

    # ------------------------------------------------------------------
    # Write (CRUD)
    # ------------------------------------------------------------------

    def create(self, data: dict) -> dict:
        bp_id = data["id"]
        if bp_id in self._cache:
            raise ValueError(f"Blueprint '{bp_id}' already exists")
        now = datetime.now(timezone.utc).isoformat()
        data["version"] = 1
        data["status"] = data.get("status", "active")
        data["created_at"] = now
        data["updated_at"] = now
        key = f"blueprints/{bp_id}/v1.json"
        self._save_to_s3(key, data)
        self._update_catalog(bp_id, data["name"], data["status"], 1, key)
        self._raw_cache[bp_id] = data
        self._cache[bp_id] = _normalize(data)
        return data

    def create_draft(self, data: dict) -> dict:
        """Save a generated blueprint as status='draft'.

        Persists to NOS at blueprints/<id>/v1.json and updates _catalog.json
        with status='draft'. Stores in _raw_cache (so GET /blueprints/{id}
        works) but NOT in _cache — draft blueprints are not available for
        extraction until explicitly activated via PUT /blueprints/{id}.

        Raises ValueError if the blueprint_id already exists in either cache.
        """
        bp_id = data["id"]
        if bp_id in self._cache or bp_id in self._raw_cache:
            raise ValueError(f"Blueprint '{bp_id}' already exists")
        key = f"blueprints/{bp_id}/v1.json"
        self._save_to_s3(key, data)
        self._update_catalog(bp_id, data.get("name", ""), "draft", 1, key)
        # Store in raw cache only — NOT in active _cache (draft not used for extraction)
        self._raw_cache[bp_id] = data
        return data

    def update(self, bp_id: str, updates: dict) -> dict:
        existing_raw = self._raw_cache.get(bp_id)
        if not existing_raw:
            raise KeyError(f"Blueprint '{bp_id}' not found")
        for k, v in updates.items():
            if v is not None:
                existing_raw[k] = v
        new_version = existing_raw.get("version", 1) + 1
        existing_raw["version"] = new_version
        existing_raw["updated_at"] = datetime.now(timezone.utc).isoformat()
        key = f"blueprints/{bp_id}/v{new_version}.json"
        self._save_to_s3(key, existing_raw)
        self._update_catalog(bp_id, existing_raw.get("name", ""), existing_raw.get("status", "active"), new_version, key)
        self._raw_cache[bp_id] = existing_raw
        self._cache[bp_id] = _normalize(existing_raw)
        return existing_raw

    def delete(self, bp_id: str) -> bool:
        """Mark blueprint as deprecated in catalog. Does NOT delete S3 files."""
        if bp_id not in self._cache:
            raise KeyError(f"Blueprint '{bp_id}' not found")
        # Update catalog status to deprecated (best-effort)
        if self._s3_client:
            try:
                resp = self._s3_client.get_object(Bucket=Config.S3_BUCKET, Key=self.CATALOG_KEY)
                catalog = json.loads(resp["Body"].read().decode("utf-8"))
                for entry in catalog.get("blueprints", []):
                    if entry.get("id") == bp_id:
                        entry["status"] = "deprecated"
                        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._save_to_s3(self.CATALOG_KEY, catalog)
            except Exception as e:
                logger.error("Failed to update catalog on delete: %s", e)
        del self._cache[bp_id]
        del self._raw_cache[bp_id]
        return True

    # ------------------------------------------------------------------
    # Catalog helpers
    # ------------------------------------------------------------------

    def _update_catalog(self, bp_id: str, name: str, status: str, version: int, path: str):
        """Update or insert entry in _catalog.json."""
        if not self._s3_client:
            return
        try:
            try:
                resp = self._s3_client.get_object(Bucket=Config.S3_BUCKET, Key=self.CATALOG_KEY)
                catalog = json.loads(resp["Body"].read().decode("utf-8"))
            except ClientError:
                catalog = {"schema_version": "1.0", "blueprints": []}

            now = datetime.now(timezone.utc).isoformat()
            catalog["updated_at"] = now

            # Find existing entry or create new
            for entry in catalog["blueprints"]:
                if entry.get("id") == bp_id:
                    entry["name"] = name
                    entry["status"] = status
                    entry["latest_version"] = version
                    entry["path"] = path
                    entry["updated_at"] = now
                    break
            else:
                catalog["blueprints"].append({
                    "id": bp_id,
                    "name": name,
                    "status": status,
                    "latest_version": version,
                    "path": path,
                    "created_at": now,
                    "updated_at": now,
                })
            self._save_to_s3(self.CATALOG_KEY, catalog)
        except Exception as e:
            logger.error("Failed to update _catalog.json: %s", e)

    # ------------------------------------------------------------------
    # S3 write helper
    # ------------------------------------------------------------------

    def _save_to_s3(self, key: str, data: dict):
        if not self._s3_client:
            return
        try:
            self._s3_client.put_object(
                Bucket=Config.S3_BUCKET,
                Key=key,
                Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        except ClientError as e:
            logger.error("Failed to save %s: %s", key, e)
