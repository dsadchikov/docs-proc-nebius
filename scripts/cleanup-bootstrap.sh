#!/usr/bin/env bash
#
# cleanup-bootstrap.sh — tear down everything scripts/bootstrap.sh created for
# a given run. Resolves every resource ID itself, by the same NAME_PREFIX
# naming convention bootstrap.sh uses (find-by-name/list, same as
# bootstrap.sh) — you only need to know PROJECT_ID and NAME_PREFIX, not hunt
# down individual resource IDs after the fact.
#
# RUN ON THE SAME MACHINE/PROFILE used for the bootstrap.sh run (needs delete
# rights on all these resources).
#
# Required:
#   PROJECT_ID     — project-...   (the one bootstrap.sh deployed into)
#
# Optional (defaults match bootstrap.sh's own defaults):
#   NAME_PREFIX    — default: docs-proc
#   BUCKET, SA_NAME, REGISTRY_NAME, ENDPOINT_NAME — override individual
#                    resource names if you customized them in the bootstrap.sh run
#   S3_REGION      — default: eu-north1
#   S3_ENDPOINT    — default: https://storage.$S3_REGION.nebius.cloud
#   DELETE_PROJECT — 1 to also delete PROJECT_ID once everything inside it is
#                    gone (default 0 — off, since PROJECT_ID may be a
#                    pre-existing project you want to keep; the dedicated
#                    project a TENANT_ID bootstrap run creates IS safe to
#                    delete). NOTE: as of CLI v0.12.223, `nebius iam project`
#                    has no `delete` subcommand at all — this is a best-effort
#                    attempt that falls back to telling you to use
#                    console.nebius.com if the CLI doesn't support it.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to the project bootstrap.sh deployed into}"
NAME_PREFIX="${NAME_PREFIX:-docs-proc}"
BUCKET="${BUCKET:-${NAME_PREFIX}-blueprints}"
SA_NAME="${SA_NAME:-${NAME_PREFIX}-endpoint}"
REGISTRY_NAME="${REGISTRY_NAME:-${NAME_PREFIX}-registry}"
ENDPOINT_NAME="${ENDPOINT_NAME:-${NAME_PREFIX}-doc-recognition}"
S3_REGION="${S3_REGION:-eu-north1}"
S3_ENDPOINT="${S3_ENDPOINT:-https://storage.${S3_REGION}.nebius.cloud}"
DELETE_PROJECT="${DELETE_PROJECT:-0}"

echo "[cleanup] resolving resource IDs under project $PROJECT_ID (prefix '$NAME_PREFIX')..."

ENDPOINT_ID=$(nebius ai endpoint list --parent-id "$PROJECT_ID" --format json | python3 -c "
import sys, json
name = '$ENDPOINT_NAME'
for e in json.load(sys.stdin).get('items', []):
    if e['metadata']['name'] == name:
        print(e['metadata']['id']); break
")
BUCKET_ID=$(nebius storage bucket get-by-name --name "$BUCKET" --parent-id "$PROJECT_ID" --format json 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['metadata']['id'])
except Exception:
    pass
" || true)
SA_ID=$(nebius iam service-account get-by-name --name "$SA_NAME" --parent-id "$PROJECT_ID" --format json 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['metadata']['id'])
except Exception:
    pass
" || true)
GROUP_ID=$(nebius iam group get-by-name --name "${SA_NAME}-group" --parent-id "$PROJECT_ID" --format json 2>/dev/null | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin)['metadata']['id'])
except Exception:
    pass
" || true)
REGISTRY_ID=$(nebius registry list --parent-id "$PROJECT_ID" --format json | python3 -c "
import sys, json
name = '$REGISTRY_NAME'
for r in json.load(sys.stdin).get('items', []):
    if r['metadata']['name'] == name:
        print(r['metadata']['id']); break
")

# bootstrap.sh issues a NEW access key on every run (not idempotent) — there
# may be more than one left over for this SA. Collect all of them, and grab
# one valid access/secret pair (the list response already includes 'secret',
# no extra `get` call needed) to empty the bucket before revoking any of them.
ACCESS_KEYS_JSON=""
if [ -n "$SA_ID" ]; then
  ACCESS_KEYS_JSON=$(nebius iam v2 access-key list --parent-id "$PROJECT_ID" --format json | python3 -c "
import sys, json
sa_id = '$SA_ID'
items = [k for k in json.load(sys.stdin).get('items', [])
         if k.get('spec', {}).get('account', {}).get('service_account', {}).get('id') == sa_id]
print(json.dumps(items))
")
fi
ACCESS_KEY_IDS=$(echo "${ACCESS_KEYS_JSON:-[]}" | python3 -c "
import sys, json
for k in json.load(sys.stdin):
    print(k['metadata']['id'])
")
S3_ACCESS_KEY=$(echo "${ACCESS_KEYS_JSON:-[]}" | python3 -c "
import sys, json
items = json.load(sys.stdin)
print(items[0]['status']['aws_access_key_id'] if items else '')
")
S3_SECRET_KEY=$(echo "${ACCESS_KEYS_JSON:-[]}" | python3 -c "
import sys, json
items = json.load(sys.stdin)
print(items[0]['status']['secret'] if items else '')
")

echo "[cleanup] resolved:"
echo "  ENDPOINT_ID    = ${ENDPOINT_ID:-<not found>}"
echo "  BUCKET_ID      = ${BUCKET_ID:-<not found>}  (BUCKET=$BUCKET)"
echo "  SA_ID          = ${SA_ID:-<not found>}"
echo "  GROUP_ID       = ${GROUP_ID:-<not found>}"
echo "  REGISTRY_ID    = ${REGISTRY_ID:-<not found>}"
echo "  ACCESS_KEY_IDS = $(echo "$ACCESS_KEY_IDS" | tr '\n' ' ')"
echo ""

# 1. Endpoint first — it's the billable GPU resource, stop that clock immediately.
if [ -n "$ENDPOINT_ID" ]; then
  echo "[cleanup] deleting endpoint $ENDPOINT_ID"
  nebius ai endpoint delete --id "$ENDPOINT_ID"
else
  echo "[cleanup] no endpoint named '$ENDPOINT_NAME' found — skipping"
fi

# 2. Empty + delete the bucket WHILE an access key can still reach it
#    (must happen before step 3 revokes them).
if [ -n "$BUCKET_ID" ]; then
  if [ -n "$S3_ACCESS_KEY" ]; then
    echo "[cleanup] emptying bucket s3://$BUCKET"
    S3_ACCESS_KEY="$S3_ACCESS_KEY" S3_SECRET_KEY="$S3_SECRET_KEY" S3_ENDPOINT="$S3_ENDPOINT" \
      S3_REGION="$S3_REGION" BUCKET="$BUCKET" python3 -c "
import os, boto3
s3 = boto3.client('s3',
    aws_access_key_id=os.environ['S3_ACCESS_KEY'],
    aws_secret_access_key=os.environ['S3_SECRET_KEY'],
    endpoint_url=os.environ['S3_ENDPOINT'],
    region_name=os.environ['S3_REGION'])
bucket = os.environ['BUCKET']
paginator = s3.get_paginator('list_objects_v2')
keys = [obj['Key'] for page in paginator.paginate(Bucket=bucket) for obj in page.get('Contents', [])]
for i in range(0, len(keys), 1000):
    batch = keys[i:i+1000]
    s3.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': k} for k in batch]})
    print(f'  deleted {len(batch)} object(s)')
print(f'  bucket s3://{bucket} emptied ({len(keys)} object(s) total)')
"
  else
    echo "[cleanup] WARNING: no usable access key found to empty s3://$BUCKET — bucket delete will likely fail if non-empty"
  fi
  echo "[cleanup] deleting bucket $BUCKET_ID"
  nebius storage bucket delete --id "$BUCKET_ID"
else
  echo "[cleanup] no bucket named '$BUCKET' found — skipping"
fi

# 3. Access key(s) — now safe to revoke.
if [ -n "$ACCESS_KEY_IDS" ]; then
  while IFS= read -r key_id; do
    echo "[cleanup] deleting access key $key_id"
    nebius iam v2 access-key delete --id "$key_id"
  done <<< "$ACCESS_KEY_IDS"
else
  echo "[cleanup] no access keys found for SA — skipping"
fi

# 4. Service account + group.
if [ -n "$SA_ID" ]; then
  echo "[cleanup] deleting service account $SA_ID"
  nebius iam service-account delete --id "$SA_ID"
else
  echo "[cleanup] no service account named '$SA_NAME' found — skipping"
fi
if [ -n "$GROUP_ID" ]; then
  echo "[cleanup] deleting group $GROUP_ID"
  nebius iam group delete --id "$GROUP_ID"
else
  echo "[cleanup] no group named '${SA_NAME}-group' found — skipping"
fi

# 5. Registry — must delete every image/artifact inside it first (verified
#    live: `registry delete` fails with "Please remove all artifacts from
#    registry!" otherwise). `registry image list` items use a flat `id`
#    field, NOT `metadata.id` (verified live — different shape than most
#    other Nebius resources).
if [ -n "$REGISTRY_ID" ]; then
  echo "[cleanup] deleting images in registry $REGISTRY_ID"
  IMAGE_IDS=$(nebius registry image list --parent-id "$REGISTRY_ID" --format json | python3 -c "
import sys, json
for img in json.load(sys.stdin).get('items', []):
    print(img['id'])
")
  if [ -n "$IMAGE_IDS" ]; then
    while IFS= read -r image_id; do
      echo "[cleanup]   deleting image $image_id"
      nebius registry image delete --id "$image_id"
    done <<< "$IMAGE_IDS"
  else
    echo "[cleanup]   no images found"
  fi
  echo "[cleanup] deleting registry $REGISTRY_ID"
  nebius registry delete --id "$REGISTRY_ID"
else
  echo "[cleanup] no registry named '$REGISTRY_NAME' found — skipping"
fi

# 6. Project last, only if explicitly requested.
# NOTE: as of CLI v0.12.223, `nebius iam project` has NO `delete` subcommand
# at all (verified live: --help lists only create/get/list/update/edit) — this
# is a best-effort attempt for forward-compat, not a guaranteed path. If it
# fails, project deletion isn't available via this CLI; either delete it
# through console.nebius.com, or leave it (an empty project with nothing
# inside it isn't billed).
if [ "$DELETE_PROJECT" = "1" ]; then
  echo "[cleanup] attempting to delete project $PROJECT_ID (may not be supported by this CLI — see note above)"
  if ! nebius iam project delete --id "$PROJECT_ID" 2>&1; then
    echo "[cleanup]   project delete not available via CLI — delete manually via console.nebius.com if needed,"
    echo "[cleanup]   or leave it (empty projects aren't billed)."
  fi
fi

echo "[cleanup] done."
