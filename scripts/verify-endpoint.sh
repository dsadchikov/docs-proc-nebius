#!/usr/bin/env bash
#
# verify-endpoint.sh — quick post-deploy verification of a LIVE endpoint.
# Lightweight complement to nebius-endpoint/smoke_test.sh (the full 35-check suite).
# Focuses on the Well-Architected hardening added in v29.
#
# Usage:
#   NEBIUS_ENDPOINT_URL=http://<IP>:8080 NEBIUS_ENDPOINT_TOKEN=<AUTH_TOKEN> \
#     ./scripts/verify-endpoint.sh
#
set -uo pipefail

U="${NEBIUS_ENDPOINT_URL:?set NEBIUS_ENDPOINT_URL, e.g. http://<IP>:8080}"
T="${NEBIUS_ENDPOINT_TOKEN:?set NEBIUS_ENDPOINT_TOKEN (the AUTH_TOKEN)}"

PASS=0; FAIL=0
ok()   { printf "  [PASS] %s\n" "$1"; PASS=$((PASS+1)); }
no()   { printf "  [FAIL] %s\n" "$1"; FAIL=$((FAIL+1)); }
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

echo "== verify $U =="

# 1. health: 200 + vllm up
H=$(curl -s "$U/health")
echo "$H" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['vllm']=='up'" 2>/dev/null \
  && ok "health vllm=up" || no "health vllm!=up (warming up?) -> $H"

# 2. /metrics public + Prometheus text (new)
M=$(curl -s "$U/metrics")
echo "$M" | grep -q "docproc_vllm_up" && ok "/metrics exposes docproc_* " || no "/metrics missing docproc_*"

# 3. SSRF allowlist on presigned_url -> 400 (new)
C=$(code -X POST "$U/recognize" -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"document":{"type":"presigned_url","value":"https://evil.example.com/x.jpg"},"mode":"auto"}')
[ "$C" = "400" ] && ok "SSRF presigned_url bad host -> 400" || no "SSRF -> $C (expected 400)"

# 4. document.type enum -> 422 (new)
C=$(code -X POST "$U/recognize" -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"document":{"type":"ftp","value":"x"},"mode":"raw"}')
[ "$C" = "422" ] && ok "invalid document.type -> 422" || no "enum -> $C (expected 422)"

# 5. auth enforced on protected route (no token -> 401/403)
C=$(code "$U/blueprints")
{ [ "$C" = "401" ] || [ "$C" = "403" ]; } && ok "protected route without token -> $C" || no "auth -> $C (expected 401/403)"

echo "== results: $PASS passed, $FAIL failed =="
echo "Full suite: NEBIUS_ENDPOINT_URL=$U NEBIUS_ENDPOINT_TOKEN=*** bash nebius-endpoint/smoke_test.sh"
echo "JSON logs (srv55): nebius ai endpoint logs <ID> | tail -20   # every line should be a JSON object"
[ $FAIL -eq 0 ] && exit 0 || exit 1
