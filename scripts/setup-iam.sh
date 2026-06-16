#!/usr/bin/env bash
#
# setup-iam.sh — provision a LEAST-PRIVILEGE service account for the endpoint.
# RUN ON srv55 (Nebius CLI installed). Run once; then store the static key in
# Mysterybox and reference it from deploy-endpoint.sh.
#
# Security pillar: today a single static key with broad project access is reused
# everywhere. This scopes a dedicated SA to ONLY the blueprints bucket.
#
# ⚠️ VERIFY role IDs / binding syntax against your tenancy — Nebius role names
#    differ per environment. Docs: https://docs.nebius.com/iam/service-accounts
#                                   https://docs.nebius.com/iam/roles
set -euo pipefail

PARENT_ID="${PARENT_ID:-project-e00g5my4en10vmy2fbmhs9}"
SA_NAME="${SA_NAME:-docs-proc-endpoint}"
BUCKET="${BUCKET:-lity-blueprints}"

echo "[iam] creating service account '$SA_NAME' in $PARENT_ID"
SA_ID=$(nebius iam service-account create \
  --name "$SA_NAME" \
  --parent-id "$PARENT_ID" \
  --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[iam] SA_ID=$SA_ID"

# ── Grant ONLY storage access (not project-wide admin) ───────────────────
# ⚠️ VERIFY the exact role for object read/write on a single bucket. Prefer a
#    bucket-scoped binding over a project-wide one. Example shape:
# nebius iam access-binding create \
#   --resource-id "$BUCKET" \
#   --role storage.objectAdmin \
#   --service-account-id "$SA_ID"
echo "[iam] TODO: bind a storage object role to SA_ID=$SA_ID on bucket=$BUCKET (see comments)."

# ── Static access key for S3-compatible access ───────────────────────────
echo "[iam] creating static key (store the secret in Mysterybox, do not commit)"
nebius iam service-account static-key create \
  --service-account-id "$SA_ID" \
  --format json

echo "[iam] Next: put S3_ACCESS_KEY / S3_SECRET_KEY into Mysterybox (mbsec-...)"
echo "      so deploy-endpoint.sh can load them via --env-secret."
