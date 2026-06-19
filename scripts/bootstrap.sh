#!/usr/bin/env bash
#
# bootstrap.sh — one-command path from "Nebius CLI configured" to a running,
# smoke-tested doc-recognition endpoint. Built for contest reproducibility:
# a judge with their own Nebius account can run this end-to-end without
# editing any code, only env vars (see CONFIG below).
#
# RUN ON A MACHINE WITH: Nebius CLI (`nebius iam whoami` works), Docker,
# the AWS CLI (`aws`, for the blueprint upload step), python3.
#
# Idempotent where the Nebius CLI makes that practical: existing
# network/subnet/registry/bucket/service-account are reused via
# get-by-name/list instead of erroring on a second run. The access key and
# the endpoint itself are NOT idempotent — re-running creates a new key and a
# new endpoint each time (cheap to clean up manually; making those two fully
# idempotent isn't worth the complexity for a one-shot bootstrap).
#
# Every CLI invocation here was verified against a live Nebius tenancy before
# being written (not guessed from docs) — see internal-docs/ for the session
# that did this verification if any flag ever needs re-checking.
#
# Live-verified 2026-06-19 end-to-end (incl. TENANT_ID path) — see CLAUDE.md
# Reproducibility section for the two rough edges that run surfaced: a fresh
# project's subnet may already exist (no async wait needed), and a federated
# CLI session can expire mid-build on a long `docker build` — this script now
# checks login before starting and again right before the final endpoint
# create, and recovers gracefully if `endpoint create` succeeded server-side
# but the client lost the session waiting for the result.
set -euo pipefail

START_TIME=$SECONDS
phase() {
  echo "[bootstrap] phase=$1 elapsed=${SECONDS}s (total since start)"
}

# Bounded check — a plain `nebius iam whoami` on an expired session will
# itself try to re-authenticate interactively and hang waiting for a browser
# that may not exist on this host. `timeout` caps that wait so we fail fast
# with clear instructions instead of hanging for minutes.
check_login() {
  if ! timeout 15 nebius iam whoami >/dev/null 2>&1; then
    echo "[bootstrap] not logged in, or your session has expired."
    echo "[bootstrap] run 'nebius iam whoami' yourself, open the printed auth link in a"
    echo "[bootstrap]   browser (use 'ssh -L <port>:localhost:<port>' to a headless host),"
    echo "[bootstrap]   then re-run this script."
    exit 1
  fi
}

phase "auth-check"
check_login

# ── Config (override via env) ────────────────────────────────────────────
# PROJECT_ID: pass an existing project (e.g. your tenant's auto-created default —
# `nebius iam project list --parent-id <tenant-id>`), OR set TENANT_ID instead and
# this script creates a dedicated project for you (find-or-create, see step 0 below).
NAME_PREFIX="${NAME_PREFIX:-docs-proc}"
PROJECT_NAME="${PROJECT_NAME:-${NAME_PREFIX}-project}"
BUCKET="${BUCKET:-${NAME_PREFIX}-blueprints}"
SA_NAME="${SA_NAME:-${NAME_PREFIX}-endpoint}"
REGISTRY_NAME="${REGISTRY_NAME:-${NAME_PREFIX}-registry}"
NETWORK_NAME="${NETWORK_NAME:-${NAME_PREFIX}-network}"
ENDPOINT_NAME="${ENDPOINT_NAME:-${NAME_PREFIX}-doc-recognition}"
IMAGE_TAG="${IMAGE_TAG:-bootstrap}"
DISK_SIZE="${DISK_SIZE:-80Gi}"
SHM_SIZE="${SHM_SIZE:-16Gi}"
S3_REGION="${S3_REGION:-eu-north1}"
S3_ENDPOINT="${S3_ENDPOINT:-https://storage.${S3_REGION}.nebius.cloud}"
REGISTRY_DOMAIN="${REGISTRY_DOMAIN:-cr.${S3_REGION}.nebius.cloud}"

# ── 0. Project (find-or-create, only if PROJECT_ID wasn't given directly) ──
# Every Nebius tenant already has an auto-created default project — list yours
# with `nebius iam project list --parent-id <tenant-id>` and pass it as
# PROJECT_ID if you'd rather reuse it. Set TENANT_ID instead to have this
# script create (or reuse, by name) a dedicated project for you. Note:
# creating a project is a TENANT-scoped write permission, which a
# project-scoped service account typically does NOT have — use an identity
# with tenant owner/admin rights for this step (the federated CLI profile
# tied to your account, not a narrowly-scoped SA) if `project create` fails
# with a permission error.
phase "project"
if [ -z "${PROJECT_ID:-}" ]; then
  TENANT_ID="${TENANT_ID:?Set PROJECT_ID to an existing project, or TENANT_ID to have this script create one (see 'nebius iam tenant list')}"
  echo "[bootstrap] no PROJECT_ID given — finding or creating project '$PROJECT_NAME' under tenant $TENANT_ID"
  PROJECT_JSON=$(nebius iam project get-by-name --name "$PROJECT_NAME" --parent-id "$TENANT_ID" --format json 2>/dev/null || true)
  if [ -z "$PROJECT_JSON" ]; then
    echo "[bootstrap] creating project '$PROJECT_NAME'"
    PROJECT_JSON=$(nebius iam project create --name "$PROJECT_NAME" --parent-id "$TENANT_ID" --format json)
  fi
  PROJECT_ID=$(echo "$PROJECT_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
fi
STORAGE_PROJECT_ID="${STORAGE_PROJECT_ID:-$PROJECT_ID}"   # bucket/SA/key project; can differ from PROJECT_ID

echo "[bootstrap] project=$PROJECT_ID storage_project=$STORAGE_PROJECT_ID"

# ── 1. Network + subnet (find-or-create) ────────────────────────────────
phase "subnet"
SUBNETS_JSON=$(nebius vpc subnet list --parent-id "$PROJECT_ID" --format json)
SUBNET_ID=$(echo "$SUBNETS_JSON" | python3 -c "
import sys, json
items = json.load(sys.stdin).get('items', [])
print(items[0]['metadata']['id'] if items else '')
")

if [ -z "$SUBNET_ID" ]; then
  echo "[bootstrap] no subnet found in $PROJECT_ID — creating a default network + subnet"
  NETWORK_ID=$(nebius vpc network create-default --parent-id "$PROJECT_ID" --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
  echo "[bootstrap] NETWORK_ID=$NETWORK_ID — re-run this script once the network finishes provisioning"
  echo "[bootstrap] (network creation is async; check 'nebius vpc subnet list --parent-id $PROJECT_ID' until a subnet appears)"
  exit 0
fi
echo "[bootstrap] SUBNET_ID=$SUBNET_ID"

# ── 2. Container registry (find-or-create) ──────────────────────────────
phase "registry"
REGISTRY_ID=$(nebius registry list --parent-id "$PROJECT_ID" --format json | python3 -c "
import sys, json
name = '$REGISTRY_NAME'
for r in json.load(sys.stdin).get('items', []):
    if r['metadata']['name'] == name:
        print(r['metadata']['id']); break
")
if [ -z "$REGISTRY_ID" ]; then
  echo "[bootstrap] creating registry '$REGISTRY_NAME'"
  REGISTRY_ID=$(nebius registry create --name "$REGISTRY_NAME" --parent-id "$PROJECT_ID" --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
fi
REGISTRY_PATH="${REGISTRY_ID#registry-}"
IMAGE="${REGISTRY_DOMAIN}/${REGISTRY_PATH}/endpoint:${IMAGE_TAG}"
echo "[bootstrap] REGISTRY_ID=$REGISTRY_ID IMAGE=$IMAGE"

# ── 3. NOS bucket for blueprints (find-or-create) ────────────────────────
phase "bucket"
BUCKET_JSON=$(nebius storage bucket get-by-name --name "$BUCKET" --parent-id "$STORAGE_PROJECT_ID" --format json 2>/dev/null || true)
if [ -z "$BUCKET_JSON" ]; then
  echo "[bootstrap] creating bucket '$BUCKET'"
  BUCKET_JSON=$(nebius storage bucket create --name "$BUCKET" --parent-id "$STORAGE_PROJECT_ID" --format json)
fi
BUCKET_ID=$(echo "$BUCKET_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[bootstrap] BUCKET_ID=$BUCKET_ID"

# ── 4. Service account scoped to the bucket (find-or-create) ────────────
phase "iam"
SA_JSON=$(nebius iam service-account get-by-name --name "$SA_NAME" --parent-id "$STORAGE_PROJECT_ID" --format json 2>/dev/null || true)
if [ -z "$SA_JSON" ]; then
  echo "[bootstrap] creating service account '$SA_NAME'"
  SA_JSON=$(nebius iam service-account create --name "$SA_NAME" --parent-id "$STORAGE_PROJECT_ID" --format json)
fi
SA_ID=$(echo "$SA_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[bootstrap] SA_ID=$SA_ID"

# Grant storage.editor on the bucket. Verified against a live tenancy: a
# service account CANNOT be the parent of an access-permit directly — the
# API requires a Group ("Parent should be one of [group]" / bucket-policy
# error "Only group can be a subject of a bucket policy rule"). So: create a
# group, add the SA as a member, grant the permit to the group.
GROUP_JSON=$(nebius iam group get-by-name --name "${SA_NAME}-group" --parent-id "$STORAGE_PROJECT_ID" --format json 2>/dev/null || true)
if [ -z "$GROUP_JSON" ]; then
  echo "[bootstrap] creating group '${SA_NAME}-group'"
  GROUP_JSON=$(nebius iam group create --name "${SA_NAME}-group" --parent-id "$STORAGE_PROJECT_ID" --format json)
fi
GROUP_ID=$(echo "$GROUP_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
echo "[bootstrap] GROUP_ID=$GROUP_ID"

nebius iam group-membership create --parent-id "$GROUP_ID" --member-id "$SA_ID" 2>&1 | grep -v "AlreadyExists" || true
nebius iam access-permit create --parent-id "$GROUP_ID" --resource-id "$BUCKET_ID" --role storage.editor \
  2>&1 | grep -v "AlreadyExists" || true

# ── 5. S3-compatible access key (created fresh each run; cheap to revoke) ─
phase "access-key"
echo "[bootstrap] issuing S3 access key"
KEY_JSON=$(nebius iam v2 access-key create \
  --account-service-account-id "$SA_ID" \
  --parent-id "$STORAGE_PROJECT_ID" \
  --description "bootstrap.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --format json)
KEY_ID=$(echo "$KEY_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['metadata']['id'])")
read -r S3_ACCESS_KEY S3_SECRET_KEY <<< "$(nebius iam v2 access-key get --id "$KEY_ID" --format json | python3 -c "
import sys, json
d = json.load(sys.stdin)['status']
print(d['aws_access_key_id'], d['secret'])
")"
echo "[bootstrap] KEY_ID=$KEY_ID (S3_ACCESS_KEY=$S3_ACCESS_KEY)"

# ── 6. Build + push (Dockerfile is self-contained as of the public-base fix) ─
phase "build-push"
echo "[bootstrap] docker build (slow first time — base image pull + model weight download, ~10-15min)"
docker build --platform linux/amd64 -t "$IMAGE" nebius-endpoint/
nebius iam get-access-token | docker login "$REGISTRY_DOMAIN" --username iam --password-stdin
docker push "$IMAGE"

# ── 7. Upload built-in blueprints to the bucket, with a _catalog.json ───────
phase "blueprint-upload"
# Verified live: blueprint_loader._load_via_catalog() takes priority over the
# directory-scan fallback the moment ANY _catalog.json exists in the bucket —
# and any blueprint create/update/delete call lazily creates one containing
# ONLY the entries it knows about. Uploading raw v1.json files without also
# writing a matching _catalog.json works at first boot (scan fallback finds
# them), but the first POST/PUT/DELETE/reload after that silently orphans
# every blueprint not listed in the catalog — they stay in the bucket as
# files but become permanently unreachable. So: write the catalog ourselves,
# upfront, alongside the raw files.
echo "[bootstrap] uploading blueprints + _catalog.json to s3://$BUCKET/blueprints/"
S3_ACCESS_KEY="$S3_ACCESS_KEY" S3_SECRET_KEY="$S3_SECRET_KEY" S3_ENDPOINT="$S3_ENDPOINT" \
  S3_REGION="$S3_REGION" BUCKET="$BUCKET" python3 -c "
import os, glob, json, boto3
from datetime import datetime, timezone

s3 = boto3.client('s3',
    aws_access_key_id=os.environ['S3_ACCESS_KEY'],
    aws_secret_access_key=os.environ['S3_SECRET_KEY'],
    endpoint_url=os.environ['S3_ENDPOINT'],
    region_name=os.environ['S3_REGION'])

now = datetime.now(timezone.utc).isoformat()
catalog = {'schema_version': '1.0', 'updated_at': now, 'blueprints': []}
for bp in sorted(glob.glob('nebius-endpoint/blueprints/*/v1.json')):
    doc_type = os.path.basename(os.path.dirname(bp))
    key = f'blueprints/{doc_type}/v1.json'
    s3.upload_file(bp, os.environ['BUCKET'], key)
    print(f'  uploaded {bp} -> s3://{os.environ[\"BUCKET\"]}/{key}')
    with open(bp) as f:
        raw = json.load(f)
    catalog['blueprints'].append({
        'id': raw.get('id', doc_type),
        'name': raw.get('name', doc_type),
        'status': raw.get('status', 'active'),
        'latest_version': 1,
        'path': key,
        'created_at': now,
        'updated_at': now,
    })

catalog_key = 'blueprints/_catalog.json'
s3.put_object(Bucket=os.environ['BUCKET'], Key=catalog_key,
               Body=json.dumps(catalog, indent=2).encode(), ContentType='application/json')
print(f'  wrote s3://{os.environ[\"BUCKET\"]}/{catalog_key} ({len(catalog[\"blueprints\"])} entries)')
"

# ── 8. Deploy the endpoint ────────────────────────────────────────────────
phase "endpoint-create"
# Re-check: the build+push+upload above can take 10-15+ minutes, long enough
# for a federated CLI session to expire mid-run (observed live 2026-06-19).
# Catch it here, before the slow-to-retry final step, rather than mid-`create`.
check_login

AUTH_TOKEN="${AUTH_TOKEN:-$(openssl rand -hex 32)}"
IAMTOK=$(nebius iam get-access-token)   # minted fresh, right before create — short-lived

echo "[bootstrap] creating endpoint '$ENDPOINT_NAME'"
# NOTE: we deliberately do NOT try to parse this call's own stdout as JSON.
# Observed live: `nebius ai ... create --format json` waits synchronously on
# the underlying operation, and for long waits it can interleave plain-text
# progress ("waiting for operation ... to complete") with the final JSON on
# stdout — breaking a naive `$(cmd) | python3 -c json.load`. It can also fail
# CLIENT-SIDE (e.g. an auth session expiring mid-wait) even though the request
# already went through SERVER-SIDE and the endpoint is provisioning/RUNNING.
# Both cases are handled the same way: ignore this call's own output/exit code
# and resolve the canonical state afterward via list-by-name + get, which are
# fast, non-blocking calls that reliably return clean JSON.
set +e
nebius ai endpoint create \
  --name "$ENDPOINT_NAME" \
  --image "$IMAGE" \
  --container-port 8080 \
  --platform gpu-h100-sxm \
  --preset 1gpu-16vcpu-200gb \
  --disk-size "$DISK_SIZE" \
  --shm-size "$SHM_SIZE" \
  --subnet-id "$SUBNET_ID" \
  --public \
  --auth none \
  --env AUTH_TOKEN="$AUTH_TOKEN" \
  --env S3_ACCESS_KEY="$S3_ACCESS_KEY" \
  --env S3_SECRET_KEY="$S3_SECRET_KEY" \
  --env S3_BUCKET="$BUCKET" \
  --env S3_REGION="$S3_REGION" \
  --env S3_ENDPOINT="$S3_ENDPOINT" \
  --registry-username iam \
  --registry-password "$IAMTOK" \
  --parent-id "$PROJECT_ID"
CREATE_EXIT=$?
set -e
if [ "$CREATE_EXIT" -ne 0 ]; then
  echo "[bootstrap] 'endpoint create' reported an error locally (exit=$CREATE_EXIT) — checking whether it"
  echo "[bootstrap]   actually succeeded server-side anyway before giving up..."
fi

ENDPOINT_ID=$(nebius ai endpoint list --parent-id "$PROJECT_ID" --format json | python3 -c "
import sys, json
name = '$ENDPOINT_NAME'
for e in json.load(sys.stdin).get('items', []):
    if e['metadata']['name'] == name:
        print(e['metadata']['id']); break
")

if [ -z "$ENDPOINT_ID" ]; then
  echo "[bootstrap] endpoint '$ENDPOINT_NAME' was not found — it really did fail."
  echo "[bootstrap] Re-run this script (build/push/upload above are already done and will be"
  echo "[bootstrap]   skipped or fast on a re-run)."
  exit 1
fi
echo "[bootstrap] ENDPOINT_ID=$ENDPOINT_ID"
ENDPOINT_JSON=$(nebius ai endpoint get "$ENDPOINT_ID" --format json)

# Public IP is often allocated at provisioning time, before the container is
# RUNNING — try to read it now so the summary below is immediately usable;
# fall back to "not yet" if the platform hasn't assigned one in this response.
PUBLIC_ENDPOINT=$(echo "$ENDPOINT_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
eps = d.get('status', {}).get('public_endpoints', [])
print(eps[0] if eps else '')
")
ENDPOINT_STATE=$(echo "$ENDPOINT_JSON" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('status', {}).get('state', 'UNKNOWN'))
")

phase "done"
echo "[bootstrap] total elapsed: ${SECONDS}s"
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[bootstrap] SUMMARY — everything needed to use or clean up this deploy"
echo "════════════════════════════════════════════════════════════════"
echo "  ENDPOINT_ID    = $ENDPOINT_ID"
echo "  ENDPOINT_STATE = $ENDPOINT_STATE"
if [ -n "$PUBLIC_ENDPOINT" ]; then
  echo "  PUBLIC_URL     = http://$PUBLIC_ENDPOINT"
else
  echo "  PUBLIC_URL     = (not yet assigned — poll: nebius ai endpoint get $ENDPOINT_ID --format json)"
fi
echo "  AUTH_TOKEN     = $AUTH_TOKEN"
echo "  PROJECT_ID     = $PROJECT_ID"
echo "  REGISTRY_ID    = $REGISTRY_ID"
echo "  BUCKET         = $BUCKET"
echo "  BUCKET_ID      = $BUCKET_ID"
echo "  SA_ID          = $SA_ID"
echo "  GROUP_ID       = $GROUP_ID"
echo "  ACCESS_KEY_ID  = $KEY_ID"
echo "  S3_ACCESS_KEY  = $S3_ACCESS_KEY"
echo "  S3_SECRET_KEY  = $S3_SECRET_KEY"
echo "  S3_ENDPOINT    = $S3_ENDPOINT"
echo "  S3_REGION      = $S3_REGION"
echo "════════════════════════════════════════════════════════════════"
echo ""
if [ -n "$PUBLIC_ENDPOINT" ] && [ "$ENDPOINT_STATE" = "RUNNING" ]; then
  echo "[bootstrap] Endpoint is RUNNING. Smoke test now:"
else
  echo "[bootstrap] Endpoint is still coming up (image pull + vLLM weight load can take"
  echo "[bootstrap]   several minutes). Poll with the command above, then smoke test:"
fi
echo "  export NEBIUS_ENDPOINT_URL=\"http://${PUBLIC_ENDPOINT:-<PUBLIC_IP>:8080}\""
echo "  export NEBIUS_ENDPOINT_TOKEN=\"$AUTH_TOKEN\""
echo "  export NEBIUS_ENDPOINT_ID=\"$ENDPOINT_ID\""
echo "  bash nebius-endpoint/smoke_test.sh"
echo ""
echo "[bootstrap] To tear everything in this run down later:"
echo "  ENDPOINT_ID=$ENDPOINT_ID BUCKET=$BUCKET BUCKET_ID=$BUCKET_ID \\"
echo "  S3_ACCESS_KEY=$S3_ACCESS_KEY S3_SECRET_KEY=$S3_SECRET_KEY \\"
echo "  S3_ENDPOINT=$S3_ENDPOINT S3_REGION=$S3_REGION \\"
echo "  ACCESS_KEY_ID=$KEY_ID SA_ID=$SA_ID GROUP_ID=$GROUP_ID REGISTRY_ID=$REGISTRY_ID \\"
echo "  PROJECT_ID=$PROJECT_ID bash scripts/cleanup-bootstrap.sh"
