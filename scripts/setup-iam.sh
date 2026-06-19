#!/usr/bin/env bash
#
# setup-iam.sh — provision a LEAST-PRIVILEGE service account for the endpoint.
# RUN ON srv55 (Nebius CLI installed). Run once; then store the access key in
# Mysterybox and reference it from deploy-endpoint.sh.
#
# Security pillar: today a single static key with broad project access is reused
# everywhere. This scopes a dedicated SA to ONLY the blueprints bucket.
#
# Commands below were verified against a live tenancy (not guessed from docs):
# `nebius iam v2 access-key` is the correct family for S3-compatible NOS keys
# (`nebius iam service-account static-key` does not produce this resource type).
#
# NOTE: the bucket/SA/access-key live under the STORAGE project, which may differ
# from the project that owns your compute subnet (see PARENT_ID below vs. the
# project used by deploy-endpoint.sh for --subnet-id/--parent-id).
set -euo pipefail

PARENT_ID="${PARENT_ID:?Set PARENT_ID to the project that owns BUCKET}"
SA_NAME="${SA_NAME:-docs-proc-endpoint}"
BUCKET="${BUCKET:?Set BUCKET to your NOS bucket name}"

echo "[iam] creating service account '$SA_NAME' in $PARENT_ID"
SA_ID=$(nebius iam service-account create \
  --name "$SA_NAME" \
  --parent-id "$PARENT_ID" \
  --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[iam] SA_ID=$SA_ID"

# ── Resolve the bucket's resource ID (access-permit needs the ID, not the name) ──
BUCKET_ID=$(nebius storage bucket get-by-name \
  --name "$BUCKET" \
  --parent-id "$PARENT_ID" \
  --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[iam] BUCKET_ID=$BUCKET_ID"

# ── Grant ONLY storage access on this one bucket (not project-wide admin) ───
# Verified against a live tenancy: an access-permit's parent must be a Group,
# not a service account directly (API: "Parent should be one of [group]").
# So: create a group, add the SA as a member, grant the permit to the group.
GROUP_ID=$(nebius iam group create \
  --name "${SA_NAME}-group" \
  --parent-id "$PARENT_ID" \
  --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[iam] GROUP_ID=$GROUP_ID"

nebius iam group-membership create --parent-id "$GROUP_ID" --member-id "$SA_ID"

echo "[iam] granting storage.editor on $BUCKET_ID to GROUP_ID=$GROUP_ID"
nebius iam access-permit create \
  --parent-id "$GROUP_ID" \
  --resource-id "$BUCKET_ID" \
  --role storage.editor

# ── S3-compatible access key for the service account ────────────────────────
echo "[iam] creating access key"
KEY_ID=$(nebius iam v2 access-key create \
  --account-service-account-id "$SA_ID" \
  --parent-id "$PARENT_ID" \
  --description "$BUCKET NOS access ($(date +%Y-%m))" \
  --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[iam] KEY_ID=$KEY_ID"

nebius iam v2 access-key get --id "$KEY_ID" --format json | python3 -c "
import sys, json
d = json.load(sys.stdin)['status']
print('S3_ACCESS_KEY=' + d['aws_access_key_id'])
print('S3_SECRET_KEY=' + d['secret'])
"

echo "[iam] Next: put S3_ACCESS_KEY / S3_SECRET_KEY into Mysterybox (mbsec-...)"
echo "      so deploy-endpoint.sh can load them via --env-secret. Do not commit them."
