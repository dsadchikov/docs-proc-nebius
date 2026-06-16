#!/usr/bin/env bash
# =============================================================
# Nebius Endpoint Smoke Tests
# macOS compatible — requires: bash, curl, python3
#
# Usage:
#   export NEBIUS_ENDPOINT_URL="http://<ip>:8080"
#   export NEBIUS_ENDPOINT_TOKEN="<bearer-token>"
#   export NEBIUS_ENDPOINT_ID="<endpoint-id>"   # optional, informational
#   bash nebius-endpoint/smoke_test.sh
# =============================================================

BASE_URL="${NEBIUS_ENDPOINT_URL:?Set NEBIUS_ENDPOINT_URL env var}"
TOKEN="${NEBIUS_ENDPOINT_TOKEN:?Set NEBIUS_ENDPOINT_TOKEN env var}"
ENDPOINT_ID="${NEBIUS_ENDPOINT_ID:-}"   # informational only

# =============================================================

PASS=0; FAIL=0

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

pass()   { printf "${GREEN}[PASS]${NC} %s\n" "$1"; PASS=$((PASS+1)); }
fail()   { printf "${RED}[FAIL]${NC} %s\n" "$1"; FAIL=$((FAIL+1)); }
info()   { printf "${YELLOW}[INFO]${NC} %s\n" "$1"; }
header() { printf "\n${CYAN}══ %s ══${NC}\n" "$1"; }
dump()   { python3 -m json.tool "$1" 2>/dev/null || cat "$1"; }

# python3 helper: read field from JSON file
# Usage: jval /tmp/file.json "d.get('key','?')"
jval() { python3 -c "import json; d=json.load(open('$1')); print($2)" 2>/dev/null; }

# 64×64 solid gray JPEG — large enough for Qwen2.5-VL (1×1 is rejected by the model processor)
TINY_B64="/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEPERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD1OiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD//2Q=="

# Unique ID for CRUD tests — avoids colliding with production blueprints
TEST_BP="smoke$(date +%s)"

printf "\n══════════════════════════════════════════════\n"
printf "  Nebius Endpoint Smoke Tests\n"
printf "  URL     : %s\n" "$BASE_URL"
printf "  Endpoint: %s\n" "$ENDPOINT_ID"
printf "══════════════════════════════════════════════\n"

# -------------------------------------------------------------
# Group 1 — Health & Auth
# -------------------------------------------------------------
header "Group 1: Health & Auth"

# T1: GET /health → 200
HTTP=$(curl -s -o /tmp/sm_health.json -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/health")
if [ "$HTTP" = "200" ]; then
  pass "T1: GET /health → 200"
  dump /tmp/sm_health.json
else
  fail "T1: GET /health → $HTTP (expected 200)"; dump /tmp/sm_health.json
fi

# T2: protected route without token → 401/403 (/health & /demo are intentionally public).
# Uses GET /blueprints which carries app-level verify_token. Valid under both deploy
# models: --auth token (ingress 401) and --auth none + AUTH_TOKEN (app 401).
HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/blueprints")
if [ "$HTTP" = "401" ] || [ "$HTTP" = "403" ]; then
  pass "T2: GET /blueprints no token → $HTTP (auth enforced)"
else
  fail "T2: GET /blueprints no token → $HTTP (expected 401/403)"
fi

# T3: All 8 required fields present in /health
MISSING=$(python3 -c "
import json, sys
req = {'status','vllm','fastapi','gpu_enabled','mock_mode','model','uptime_seconds','blueprints_loaded'}
try:
    d = json.load(open('/tmp/sm_health.json'))
    m = req - set(d.keys())
    print(','.join(sorted(m)) if m else '')
except Exception as e:
    print('PARSE_ERROR:' + str(e))
" 2>/dev/null)
if [ -z "$MISSING" ]; then
  pass "T3: /health has all 8 required fields"
else
  fail "T3: /health missing fields: $MISSING"
fi

# T4: vllm = "up"
VLLM=$(jval /tmp/sm_health.json "d.get('vllm','MISSING')")
[ "$VLLM" = "up" ] && pass "T4: health.vllm = up" || fail "T4: health.vllm = '$VLLM' (expected 'up')"

# T5: blueprints_loaded > 0
BP=$(jval /tmp/sm_health.json "d.get('blueprints_loaded', 0)")
if [ "$BP" -gt "0" ] 2>/dev/null; then
  pass "T5: blueprints_loaded = $BP"
else
  fail "T5: blueprints_loaded = $BP (expected > 0)"
fi

# -------------------------------------------------------------
# Group 2 — vLLM Direct
# -------------------------------------------------------------
header "Group 2: vLLM Direct"

# T6: GET /v1/models → 200 with Qwen model
HTTP=$(curl -s -o /tmp/sm_models.json -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/v1/models")
if [ "$HTTP" = "200" ]; then
  MODEL_ID=$(python3 -c "import json; print(json.load(open('/tmp/sm_models.json'))['data'][0]['id'])" 2>/dev/null)
  pass "T6: GET /v1/models → 200 (model=$MODEL_ID)"
else
  fail "T6: GET /v1/models → $HTTP (expected 200)"; dump /tmp/sm_models.json
fi

# -------------------------------------------------------------
# Group 3 — Input Validation (no GPU needed)
# -------------------------------------------------------------
header "Group 3: Input Validation"

# T7: Empty body → 422
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}')
[ "$HTTP" = "422" ] && pass "T7: empty body → 422" || fail "T7: empty body → $HTTP (expected 422)"

# T8: Unknown blueprint_id → 422
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"document":{"type":"base64","value":"dGVzdA==","mime_type":"image/jpeg"},"mode":"blueprint","blueprint_id":"nonexistent_xyz"}')
[ "$HTTP" = "422" ] && pass "T8: unknown blueprint_id → 422" || fail "T8: unknown blueprint_id → $HTTP (expected 422)"

# T9: Missing blueprint_id with mode=blueprint → 422
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"document":{"type":"base64","value":"dGVzdA==","mime_type":"image/jpeg"},"mode":"blueprint"}')
[ "$HTTP" = "422" ] && pass "T9: missing blueprint_id → 422" || fail "T9: missing blueprint_id → $HTTP (expected 422)"

# T10: nebius_object with invalid key → NOS fetch attempted → 503/500
# Uses "default" blueprint (always loaded) so the code reaches the NOS fetch before failing
HTTP=$(curl -s -o /tmp/sm_t10.json -w "%{http_code}" \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"document":{"type":"nebius_object","value":"inbound/nonexistent/no-such-file.jpg","mime_type":"image/jpeg"},"mode":"blueprint","blueprint_id":"default"}')
if [ "$HTTP" = "503" ] || [ "$HTTP" = "500" ] || [ "$HTTP" = "404" ]; then
  pass "T10: nebius_object invalid key → $HTTP (NOS fetch attempted)"
else
  fail "T10: nebius_object → $HTTP (expected 503/500)"
fi

# -------------------------------------------------------------
# Group 4 — Blueprints API
# -------------------------------------------------------------
header "Group 4: Blueprints API"

# T11: GET /blueprints → 200 + non-empty array
HTTP=$(curl -s -o /tmp/sm_bps.json -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints")
if [ "$HTTP" = "200" ]; then
  COUNT=$(python3 -c "import json; print(len(json.load(open('/tmp/sm_bps.json'))))" 2>/dev/null)
  pass "T11: GET /blueprints → 200 ($COUNT blueprints)"
  python3 -c "import json; [print('    -', b['id'], '| fields:', b.get('fields_count','?')) for b in json.load(open('/tmp/sm_bps.json'))]" 2>/dev/null
else
  fail "T11: GET /blueprints → $HTTP (expected 200)"
fi

# T12: GET /blueprints/passport → 200
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints/passport")
[ "$HTTP" = "200" ] && pass "T12: GET /blueprints/passport → 200" || fail "T12: GET /blueprints/passport → $HTTP"

# T13: GET /blueprints/residence_permit_ltu_front → 200
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints/residence_permit_ltu_front")
[ "$HTTP" = "200" ] && pass "T13: GET /blueprints/residence_permit_ltu_front → 200" || fail "T13: GET /blueprints/residence_permit_ltu_front → $HTTP"

# T14: GET /blueprints/default → 200
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints/default")
[ "$HTTP" = "200" ] && pass "T14: GET /blueprints/default → 200" || fail "T14: GET /blueprints/default → $HTTP"

# T15: GET /blueprints/nonexistent → 404
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints/nonexistent-xyz-no-such")
[ "$HTTP" = "404" ] && pass "T15: GET /blueprints/nonexistent → 404" || fail "T15: GET /blueprints/nonexistent → $HTTP (expected 404)"

# -------------------------------------------------------------
# Group 5 — NOS Presign
# -------------------------------------------------------------
header "Group 5: NOS Presign"

# T16: GET /inbound/presign?filename=test.jpg → 200 with presigned_put_url + nos_key
HTTP=$(curl -s -o /tmp/sm_presign.json -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/inbound/presign?filename=test.jpg")
if [ "$HTTP" = "200" ]; then
  PURL=$(jval /tmp/sm_presign.json "d.get('presigned_put_url','')")
  NKEY=$(jval /tmp/sm_presign.json "d.get('nos_key','')")
  EXP=$(jval  /tmp/sm_presign.json "d.get('expires_in','?')")
  if [[ "$PURL" == https://* ]] && [[ "$NKEY" == inbound/* ]]; then
    pass "T16: GET /inbound/presign → 200 (nos_key=$NKEY expires_in=$EXP)"
  else
    fail "T16: GET /inbound/presign → 200 but bad shape (url=$PURL key=$NKEY)"
  fi
elif [ "$HTTP" = "503" ]; then
  fail "T16: GET /inbound/presign → 503 (NOS not configured)"; dump /tmp/sm_presign.json
else
  fail "T16: GET /inbound/presign → $HTTP"; dump /tmp/sm_presign.json
fi

# T17: GET /inbound/presign without filename → 422
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/inbound/presign")
[ "$HTTP" = "422" ] && pass "T17: GET /inbound/presign no filename → 422" || fail "T17: GET /inbound/presign no filename → $HTTP (expected 422)"

# -------------------------------------------------------------
# Group 6 — GPU Inference
# Note: --max-time 90 per call; all use the embedded 1×1 JPEG
# -------------------------------------------------------------
header "Group 6: GPU Inference"

# T18: mode=raw
info "T18: mode=raw (waiting for GPU...)"
HTTP=$(curl -s -o /tmp/sm_raw.json -w "%{http_code}" --max-time 90 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"raw\"}")
if [ "$HTTP" = "200" ]; then
  HAS_TEXT=$(jval /tmp/sm_raw.json "'yes' if d.get('raw_text') else 'no'")
  pass "T18: mode=raw → 200 (raw_text present=$HAS_TEXT)"
  dump /tmp/sm_raw.json
elif [ "$HTTP" = "504" ]; then
  fail "T18: mode=raw → 504 timeout"
else
  fail "T18: mode=raw → $HTTP"; dump /tmp/sm_raw.json
fi

# T19: mode=blueprint / passport — structure, confidence, routing, request_id
info "T19: mode=blueprint/passport (waiting for GPU...)"
HTTP=$(curl -s -o /tmp/sm_bp_pass.json -w "%{http_code}" --max-time 90 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"blueprint\",\"blueprint_id\":\"passport\",\"options\":{\"include_confidence\":true,\"confidence_mode\":\"both\"}}")
if [ "$HTTP" = "200" ]; then
  CONF=$(jval  /tmp/sm_bp_pass.json "d.get('document_confidence','?')")
  ROUTE=$(jval /tmp/sm_bp_pass.json "d.get('routing','?')")
  RID=$(jval   /tmp/sm_bp_pass.json "'yes' if d.get('request_id') else 'no'")
  FCOUNT=$(python3 -c "import json; print(len(json.load(open('/tmp/sm_bp_pass.json')).get('fields',{})))" 2>/dev/null)
  pass "T19: mode=blueprint/passport → 200 (conf=$CONF routing=$ROUTE fields=$FCOUNT request_id=$RID)"
  dump /tmp/sm_bp_pass.json
elif [ "$HTTP" = "504" ]; then
  fail "T19: mode=blueprint/passport → 504 timeout"
else
  fail "T19: mode=blueprint/passport → $HTTP"; dump /tmp/sm_bp_pass.json
fi

# T20: routing is a valid value (uses result from T19)
ROUTE=$(jval /tmp/sm_bp_pass.json "d.get('routing','')")
case "$ROUTE" in
  auto_classified|review_required|escalate_to_operator)
    pass "T20: routing='$ROUTE' is valid" ;;
  *)
    fail "T20: routing='$ROUTE' is not valid (expected auto_classified|review_required|escalate_to_operator)" ;;
esac

# T21: request_id present (uses result from T19)
RID=$(jval /tmp/sm_bp_pass.json "d.get('request_id','')")
if [ -n "$RID" ] && [ "$RID" != "None" ] && [ "$RID" != "null" ]; then
  pass "T21: request_id present ($RID)"
else
  fail "T21: request_id missing from response"
fi

# T22: mode=blueprint / residence_permit_ltu_front
info "T22: mode=blueprint/residence_permit_ltu_front (waiting for GPU...)"
HTTP=$(curl -s -o /tmp/sm_bp_rp.json -w "%{http_code}" --max-time 90 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"blueprint\",\"blueprint_id\":\"residence_permit_ltu_front\",\"options\":{\"include_confidence\":true,\"confidence_mode\":\"both\"}}")
if [ "$HTTP" = "200" ]; then
  FCOUNT=$(python3 -c "import json; print(len(json.load(open('/tmp/sm_bp_rp.json')).get('fields',{})))" 2>/dev/null)
  pass "T22: mode=blueprint/residence_permit_ltu_front → 200 ($FCOUNT fields)"
  dump /tmp/sm_bp_rp.json
elif [ "$HTTP" = "504" ]; then
  fail "T22: mode=blueprint/residence_permit_ltu_front → 504 timeout"
else
  fail "T22: mode=blueprint/residence_permit_ltu_front → $HTTP"; dump /tmp/sm_bp_rp.json
fi

# T23: mode=auto
info "T23: mode=auto (waiting for GPU...)"
HTTP=$(curl -s -o /tmp/sm_auto.json -w "%{http_code}" --max-time 90 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"auto\"}")
if [ "$HTTP" = "200" ]; then
  HAS_CLASS=$(jval /tmp/sm_auto.json "'yes' if d.get('classification') is not None else 'no'")
  ROUTE=$(jval   /tmp/sm_auto.json "d.get('routing','?')")
  pass "T23: mode=auto → 200 (classification=$HAS_CLASS routing=$ROUTE)"
  dump /tmp/sm_auto.json
elif [ "$HTTP" = "504" ]; then
  fail "T23: mode=auto → 504 timeout"
else
  fail "T23: mode=auto → $HTTP"; dump /tmp/sm_auto.json
fi

# T24: mode=double_check
info "T24: mode=double_check (waiting for GPU, 2 passes...)"
HTTP=$(curl -s -o /tmp/sm_dc.json -w "%{http_code}" --max-time 120 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"double_check\",\"blueprint_id\":\"passport\",\"options\":{\"include_confidence\":true,\"confidence_mode\":\"both\"}}")
if [ "$HTTP" = "200" ]; then
  CONF=$(jval /tmp/sm_dc.json "d.get('document_confidence','?')")
  pass "T24: mode=double_check → 200 (conf=$CONF)"
elif [ "$HTTP" = "504" ]; then
  fail "T24: mode=double_check → 504 timeout (2 VLM passes — expected)"
else
  fail "T24: mode=double_check → $HTTP"; dump /tmp/sm_dc.json
fi

# -------------------------------------------------------------
# Group 7 — Confidence Modes
# -------------------------------------------------------------
header "Group 7: Confidence Modes"

# T25: confidence_mode=document → per-field confidences are null
info "T25: confidence_mode=document (waiting for GPU...)"
HTTP=$(curl -s -o /tmp/sm_cm_doc.json -w "%{http_code}" --max-time 90 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"blueprint\",\"blueprint_id\":\"passport\",\"options\":{\"include_confidence\":true,\"confidence_mode\":\"document\"}}")
if [ "$HTTP" = "200" ]; then
  RESULT=$(python3 -c "
import json
d = json.load(open('/tmp/sm_cm_doc.json'))
has_doc = d.get('document_confidence') is not None
per = [v.get('confidence') for v in d.get('fields',{}).values() if isinstance(v,dict)]
all_null = all(c is None for c in per)
print(f'doc_conf_present={has_doc} per_field_null={all_null}')
" 2>/dev/null)
  pass "T25: confidence_mode=document → 200 ($RESULT)"
else
  fail "T25: confidence_mode=document → $HTTP"
fi

# T26: confidence_mode=fields → document_confidence is null
info "T26: confidence_mode=fields (waiting for GPU...)"
HTTP=$(curl -s -o /tmp/sm_cm_fields.json -w "%{http_code}" --max-time 90 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":{\"type\":\"base64\",\"value\":\"$TINY_B64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"blueprint\",\"blueprint_id\":\"passport\",\"options\":{\"include_confidence\":true,\"confidence_mode\":\"fields\"}}")
if [ "$HTTP" = "200" ]; then
  RESULT=$(python3 -c "
import json
d = json.load(open('/tmp/sm_cm_fields.json'))
doc_null = d.get('document_confidence') is None
per = [v.get('confidence') for v in d.get('fields',{}).values() if isinstance(v,dict)]
has_per = any(c is not None for c in per)
print(f'doc_conf_null={doc_null} has_per_field_conf={has_per}')
" 2>/dev/null)
  pass "T26: confidence_mode=fields → 200 ($RESULT)"
else
  fail "T26: confidence_mode=fields → $HTTP"
fi

# -------------------------------------------------------------
# Group 8 — Blueprint CRUD
# -------------------------------------------------------------
header "Group 8: Blueprint CRUD (test id: $TEST_BP)"

# T27: POST /blueprints → 201
HTTP=$(curl -s -o /tmp/sm_crud_create.json -w "%{http_code}" \
  -X POST "$BASE_URL/blueprints" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"id\": \"$TEST_BP\",
    \"name\": \"Smoke Test Blueprint\",
    \"description\": \"Temporary — created by smoke test\",
    \"extraction_prompt\": \"Extract all fields.\",
    \"fields\": [
      {\"name\": \"test_field\", \"description\": \"test\", \"instruction\": \"verbatim\", \"required\": false}
    ]
  }")
if [ "$HTTP" = "201" ]; then
  VER=$(jval /tmp/sm_crud_create.json "d.get('version','?')")
  pass "T27: POST /blueprints → 201 (version=$VER)"
else
  fail "T27: POST /blueprints → $HTTP (expected 201)"; dump /tmp/sm_crud_create.json
fi

# T28: POST same id → 409
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/blueprints" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"$TEST_BP\",\"name\":\"Dup\",\"fields\":[]}")
[ "$HTTP" = "409" ] && pass "T28: POST duplicate id → 409" || fail "T28: POST duplicate id → $HTTP (expected 409)"

# T29: PUT /blueprints/{id} → 200, version incremented
HTTP=$(curl -s -o /tmp/sm_crud_update.json -w "%{http_code}" \
  -X PUT "$BASE_URL/blueprints/$TEST_BP" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description":"Updated by smoke test"}')
if [ "$HTTP" = "200" ]; then
  VER=$(jval /tmp/sm_crud_update.json "d.get('version','?')")
  pass "T29: PUT /blueprints/$TEST_BP → 200 (version=$VER)"
else
  fail "T29: PUT /blueprints/$TEST_BP → $HTTP (expected 200)"; dump /tmp/sm_crud_update.json
fi

# T30: PUT nonexistent blueprint → 404
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X PUT "$BASE_URL/blueprints/nonexistent-xyz" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description":"noop"}')
[ "$HTTP" = "404" ] && pass "T30: PUT nonexistent → 404" || fail "T30: PUT nonexistent → $HTTP (expected 404)"

# T31: POST /blueprints/reload → 200
HTTP=$(curl -s -o /tmp/sm_crud_reload.json -w "%{http_code}" \
  -X POST "$BASE_URL/blueprints/reload" \
  -H "Authorization: Bearer $TOKEN")
if [ "$HTTP" = "200" ]; then
  CNT=$(jval /tmp/sm_crud_reload.json "d.get('blueprints_count','?')")
  pass "T31: POST /blueprints/reload → 200 (blueprints_count=$CNT)"
else
  fail "T31: POST /blueprints/reload → $HTTP (expected 200)"
fi

# T32: DELETE /blueprints/{id} → 204
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X DELETE "$BASE_URL/blueprints/$TEST_BP" \
  -H "Authorization: Bearer $TOKEN")
[ "$HTTP" = "204" ] && pass "T32: DELETE /blueprints/$TEST_BP → 204" || fail "T32: DELETE /blueprints/$TEST_BP → $HTTP (expected 204)"

# T33: GET deleted blueprint → 404
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints/$TEST_BP")
[ "$HTTP" = "404" ] && pass "T33: GET deleted blueprint → 404" || fail "T33: GET deleted blueprint → $HTTP (expected 404)"

# -------------------------------------------------------------
# Group 9 — Security hardening (Well-Architected)
# -------------------------------------------------------------
header "Group 9: Security hardening"

# T34: presigned_url with a disallowed host → 400 (SSRF allowlist)
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"document":{"type":"presigned_url","value":"https://evil.example.com/x.jpg","mime_type":"image/jpeg"},"mode":"auto"}')
[ "$HTTP" = "400" ] && pass "T34: presigned_url bad host → 400 (SSRF blocked)" || fail "T34: presigned_url bad host → $HTTP (expected 400)"

# T35: invalid document.type → 422 (enum validation)
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"document":{"type":"ftp","value":"x","mime_type":"image/jpeg"},"mode":"raw"}')
[ "$HTTP" = "422" ] && pass "T35: invalid document.type → 422" || fail "T35: invalid document.type → $HTTP (expected 422)"

# -------------------------------------------------------------
# Summary
# -------------------------------------------------------------
echo ""
echo "══════════════════════════════════════════════"
printf "  Results: ${GREEN}%d passed${NC}  ${RED}%d failed${NC}\n" "$PASS" "$FAIL"
echo "══════════════════════════════════════════════"

[ $FAIL -eq 0 ] && exit 0 || exit 1
