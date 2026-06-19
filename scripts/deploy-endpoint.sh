#!/usr/bin/env bash
#
# deploy-endpoint.sh — re-deploy onto ALREADY-PROVISIONED infra (registry,
# bucket, SA, Mysterybox secret) using Nebius-native secret references.
# RUN ON srv55 (the build VM where the Nebius CLI + Docker are installed).
#
# For a from-scratch deploy (no existing bucket/SA/registry/Mysterybox setup),
# use `scripts/bootstrap.sh` instead — it provisions everything and takes
# plaintext env vars, no Mysterybox dependency.
#
# Improves deploy repeatability (Operational Excellence) and adopts the
# Nebius-native secrets path: secrets are passed by REFERENCE from Mysterybox
# via `--env-secret`, not as plaintext `--env` values.
#
# Usage:
#   ./scripts/deploy-endpoint.sh <image-tag>
#   TAG=v29 ./scripts/deploy-endpoint.sh
#
set -euo pipefail

# ── Config (override via env) ────────────────────────────────────────────
TAG="${1:-${TAG:?Set an image tag, e.g. ./deploy-endpoint.sh v29}}"
REGISTRY="${REGISTRY:-cr.eu-north1.nebius.cloud/e00kh1yd3svet2htq0}"
IMAGE="${IMAGE:-$REGISTRY/endpoint:$TAG}"
NAME="${NAME:-lity-doc-recognition}"
SUBNET_ID="${SUBNET_ID:-vpcsubnet-e00sqjf7njsth9q7n3}"
PARENT_ID="${PARENT_ID:-project-e00g5my4en10vmy2fbmhs9}"   # MUST own SUBNET_ID
DISK_SIZE="${DISK_SIZE:-80Gi}"                              # right-sized: model ~20GB + headroom
SHM_SIZE="${SHM_SIZE:-16Gi}"
# Mysterybox secret holding S3_* (and, recommended, AUTH_TOKEN) payload keys.
MB_SECRET="${MB_SECRET:-mbsec-e00bkegre2hbv41e4n}"

echo "[deploy] image=$IMAGE parent=$PARENT_ID disk=$DISK_SIZE secret=$MB_SECRET"

# ⚠️⚠️ PREREQUISITE: $MB_SECRET must contain an AUTH_TOKEN payload key (plus the
#     S3_* keys). It does NOT by default — add it first, e.g.:
#       nebius mysterybox payload ... (add key AUTH_TOKEN=<generated-token>)
#     Otherwise the --env-secret AUTH_TOKEN line below fails. See fallback at EOF.

# ── Registry login ───────────────────────────────────────────────────────
nebius iam get-access-token | docker login "$REGISTRY" --username iam --password-stdin

# ── Create endpoint ──────────────────────────────────────────────────────
# Auth model: `--auth none` (ingress open so the browser /demo works) + an
# app-level AUTH_TOKEN enforced by verify_token. AUTH_TOKEN and S3_* are loaded
# from Mysterybox by reference (no plaintext in the spec or shell history).
#
# ⚠️ VERIFY once against your CLI version: `--env-secret KEY=<selector>` loads
#    env var KEY from the payload key KEY inside <selector>. Our Mysterybox keys
#    (S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, S3_ENDPOINT, S3_REGION) match the
#    env var names. Add an AUTH_TOKEN payload key to $MB_SECRET (recommended).
#
# `--registry-username`/`--registry-password` are MANDATORY as of the v31 incident
# (2026-06-18): Nebius's implicit same-project registry auth stopped working —
# see the "Deploy incident log" in CLAUDE.md. Mint a fresh IAM token right before
# create (it's short-lived).
IAMTOK=$(nebius iam get-access-token)
nebius ai endpoint create \
  --name "$NAME" \
  --image "$IMAGE" \
  --container-port 8080 \
  --platform gpu-h100-sxm \
  --preset 1gpu-16vcpu-200gb \
  --disk-size "$DISK_SIZE" \
  --shm-size "$SHM_SIZE" \
  --subnet-id "$SUBNET_ID" \
  --public \
  --auth none \
  --env-secret AUTH_TOKEN="$MB_SECRET" \
  --env-secret S3_ACCESS_KEY="$MB_SECRET" \
  --env-secret S3_SECRET_KEY="$MB_SECRET" \
  --env-secret S3_BUCKET="$MB_SECRET" \
  --env-secret S3_ENDPOINT="$MB_SECRET" \
  --env-secret S3_REGION="$MB_SECRET" \
  --env METRICS_ENABLED=1 \
  --registry-username iam \
  --registry-password "$IAMTOK" \
  --parent-id "$PARENT_ID"

echo "[deploy] done. Recover AUTH_TOKEN / URL with:"
echo "  nebius ai endpoint get <ID> --format json"
echo "[deploy] then: NEBIUS_ENDPOINT_URL=... NEBIUS_ENDPOINT_TOKEN=... bash nebius-endpoint/smoke_test.sh"

# ── Fallback (plaintext env, if --env-secret is unavailable) ─────────────
# source .secrets.env && nebius ai endpoint create ... \
#   --env AUTH_TOKEN="$AUTH_TOKEN" --env S3_ACCESS_KEY="$S3_ACCESS_KEY" ...
