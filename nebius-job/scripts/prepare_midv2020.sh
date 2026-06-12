#!/bin/bash
# prepare_midv2020.sh — download MIDV-2020 templates subset, build eval
# manifest, upload images + manifest to NOS (Req 15.2, 15.3).
#
# MIDV-2020 public dataset (no real PII): ftp://smartengines.com/midv-2020/
# Single archive: dataset/templates.tar  (863 MB)
# Structure inside:
#   images/<CODE>/00.jpg ... 99.jpg
#   annotations/<CODE>.json  (VIA 2.x format)
#
# Usage:
#   export S3_ACCESS_KEY=... S3_SECRET_KEY=... S3_BUCKET=... S3_ENDPOINT=...
#   bash prepare_midv2020.sh [--types "esp_id grc_passport srb_passport"] [--per-type 20]
#
# Requires: curl, tar, ${PYTHON:-python3}, pip3 install boto3
set -euo pipefail

MIDV_FTP="${MIDV_FTP:-ftp://smartengines.com/midv-2020/dataset/templates.tar}"
S3_ENDPOINT="${S3_ENDPOINT:-https://storage.eu-north1.nebius.cloud}"
S3_BUCKET="${S3_BUCKET:?S3_BUCKET is required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"

TYPES="esp_id grc_passport srb_passport"   # ≥3 distinct doc types (Req 15.3)
PER_TYPE=20                                 # 3×20 = 60 ≥ 50 documents
WORK_DIR="${WORK_DIR:-/tmp/midv2020}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --types)    TYPES="$2"; shift 2 ;;
    --per-type) PER_TYPE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$WORK_DIR"

# ---- 1. Download templates.tar -----------------------------------------------
ARCHIVE="$WORK_DIR/templates.tar"
if [[ ! -f "$ARCHIVE" ]]; then
  echo ">> downloading templates.tar (~863 MB) from $MIDV_FTP"
  curl -fSL --retry 3 --progress-bar -o "$ARCHIVE" "$MIDV_FTP"
else
  echo ">> templates.tar already cached at $ARCHIVE"
fi

# ---- 2. Extract only needed types (avoids full 863MB unpack) -----------------
echo ">> extracting types: $TYPES"
EXTRACT_PATHS=""
for t in $TYPES; do
  EXTRACT_PATHS="$EXTRACT_PATHS images/$t annotations/${t}.json"
done
tar -xf "$ARCHIVE" -C "$WORK_DIR" $EXTRACT_PATHS 2>/dev/null \
  || tar -xf "$ARCHIVE" -C "$WORK_DIR"   # fallback: extract all if selective fails

# ---- 3. Build eval manifest --------------------------------------------------
echo ">> building manifest"
${PYTHON:-python3} "$SCRIPT_DIR/build_midv_manifest.py" \
  --work-dir "$WORK_DIR" \
  --types "$TYPES" \
  --per-type "$PER_TYPE" \
  --out "$WORK_DIR/manifest.json"

# ---- 4. Upload images + manifest to NOS (boto3, no aws CLI needed) -----------
echo ">> uploading to s3://$S3_BUCKET/eval/midv2020/"
${PYTHON:-python3} - <<PYEOF
import boto3, sys
from pathlib import Path

s3 = boto3.client(
    "s3",
    endpoint_url="${S3_ENDPOINT}",
    aws_access_key_id="${S3_ACCESS_KEY}",
    aws_secret_access_key="${S3_SECRET_KEY}",
)
bucket = "${S3_BUCKET}"
upload_root = Path("${WORK_DIR}/upload/images")
files = [f for f in upload_root.rglob("*") if f.is_file()]
total = len(files)
if total == 0:
    print("ERROR: no images in upload dir — check manifest build step", file=sys.stderr)
    sys.exit(1)
for i, f in enumerate(files, 1):
    key = "eval/midv2020/images/" + str(f.relative_to(upload_root))
    s3.upload_file(str(f), bucket, key)
    if i % 10 == 0 or i == total:
        print(f"  images {i}/{total}", flush=True)

s3.upload_file("${WORK_DIR}/manifest.json", bucket, "eval/midv2020/manifest.json")
print("  manifest uploaded -> eval/midv2020/manifest.json")
PYEOF

n_docs=$(${PYTHON:-python3} -c "import json; print(len(json.load(open('$WORK_DIR/manifest.json'))['documents']))")
echo ">> done: $n_docs documents"
echo ">> eval: JOB_MODE=eval MANIFEST_PATH=eval/midv2020/manifest.json"
