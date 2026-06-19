#!/usr/bin/env bash
#
# cleanup-bootstrap.sh — tear down everything scripts/bootstrap.sh created for
# a test/experiment run. Verifies every required ID/credential is set BEFORE
# deleting anything, then deletes in dependency order (endpoint first — it's
# the billable GPU resource — project last, since everything else lives under it).
#
# RUN ON THE SAME MACHINE/PROFILE used for the bootstrap.sh run (needs delete
# rights on all these resources).
#
# Required env vars (all of them — this script refuses to run with any unset):
#   ENDPOINT_ID    — aiendpoint-...     (from bootstrap.sh output / `nebius ai endpoint list`)
#   BUCKET         — bucket NAME (not ID) — needed to empty it via S3 API before deleting
#   BUCKET_ID      — storagebucket-...
#   S3_ACCESS_KEY  — used to empty the bucket (must still be valid — do this BEFORE deleting it below)
#   S3_SECRET_KEY
#   S3_ENDPOINT    — e.g. https://storage.eu-north1.nebius.cloud
#   S3_REGION      — e.g. eu-north1
#   ACCESS_KEY_ID  — accesskey-...      (the Nebius access-key resource, deleted AFTER emptying the bucket)
#   SA_ID          — serviceaccount-...
#   GROUP_ID       — group-...
#   REGISTRY_ID    — registry-...
#   PROJECT_ID     — project-...        (only deleted if DELETE_PROJECT=1 — see below)
#
# Optional:
#   DELETE_PROJECT=1   — also delete PROJECT_ID itself once everything inside it is gone.
#                         Defaults to 0 (off) since PROJECT_ID may be a pre-existing project
#                         you want to keep (the TENANT_ID bootstrap path creates a dedicated
#                         one, which IS safe to delete; an existing PROJECT_ID you passed in
#                         yourself usually is NOT).
set -euo pipefail

require() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "[cleanup] missing required env var: $name" >&2
    MISSING=1
  fi
}

MISSING=0
require ENDPOINT_ID
require BUCKET
require BUCKET_ID
require S3_ACCESS_KEY
require S3_SECRET_KEY
require S3_ENDPOINT
require S3_REGION
require ACCESS_KEY_ID
require SA_ID
require GROUP_ID
require REGISTRY_ID
require PROJECT_ID

if [ "$MISSING" -ne 0 ]; then
  echo "[cleanup] aborting — set the missing variable(s) above and re-run. Nothing was deleted." >&2
  exit 1
fi

DELETE_PROJECT="${DELETE_PROJECT:-0}"

echo "[cleanup] about to delete:"
echo "  endpoint:        $ENDPOINT_ID"
echo "  bucket contents + bucket: $BUCKET ($BUCKET_ID)"
echo "  access key:      $ACCESS_KEY_ID"
echo "  service account: $SA_ID"
echo "  group:           $GROUP_ID"
echo "  registry:        $REGISTRY_ID"
if [ "$DELETE_PROJECT" = "1" ]; then
  echo "  project:         $PROJECT_ID  (DELETE_PROJECT=1)"
else
  echo "  project:         $PROJECT_ID  — KEPT (set DELETE_PROJECT=1 to also remove it)"
fi

# 1. Endpoint first — it's the billable GPU resource, stop that clock immediately.
echo "[cleanup] deleting endpoint $ENDPOINT_ID"
nebius ai endpoint delete --id "$ENDPOINT_ID"

# 2. Empty + delete the bucket WHILE the access key can still reach it
#    (must happen before step 3 revokes that key).
echo "[cleanup] emptying bucket s3://$BUCKET"
AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY" \
  aws s3 rm "s3://$BUCKET" --recursive --endpoint-url "$S3_ENDPOINT" --region "$S3_REGION"
echo "[cleanup] deleting bucket $BUCKET_ID"
nebius storage bucket delete --id "$BUCKET_ID"

# 3. Access key — now safe to revoke.
echo "[cleanup] deleting access key $ACCESS_KEY_ID"
nebius iam v2 access-key delete --id "$ACCESS_KEY_ID"

# 4. Service account + group.
echo "[cleanup] deleting service account $SA_ID"
nebius iam service-account delete --id "$SA_ID"
echo "[cleanup] deleting group $GROUP_ID"
nebius iam group delete --id "$GROUP_ID"

# 5. Registry (and the image(s) inside it).
echo "[cleanup] deleting registry $REGISTRY_ID"
nebius registry delete --id "$REGISTRY_ID"

# 6. Project last, only if explicitly requested.
if [ "$DELETE_PROJECT" = "1" ]; then
  echo "[cleanup] deleting project $PROJECT_ID"
  nebius iam project delete --id "$PROJECT_ID"
fi

echo "[cleanup] done."
