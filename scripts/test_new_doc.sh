#!/usr/bin/env bash
# =============================================================
# New Document Walkthrough Test
#
# Scenario:
#   1. Submit an unknown document → get blueprint-not-found 422
#   2. Ask the system to generate a blueprint from the sample
#   3. Download the generated blueprint JSON
#   4. Re-submit the same document using the new blueprint
#
# Usage:
#   export NEBIUS_ENDPOINT_URL="http://<ip>:8080"
#   export NEBIUS_ENDPOINT_TOKEN="<bearer-token>"
#
#   # Option A — base64 image file:
#   export NEW_DOC_PATH="/path/to/document.jpg"
#
#   # Option B — already in NOS (inbound/ key):
#   export NEW_DOC_NOS_KEY="inbound/my-folder/document.jpg"
#
#   # Blueprint ID to create (alphanumeric + _ -)
#   export NEW_BLUEPRINT_ID="my_new_document"     # default: new_doc_test
#   export NEW_BLUEPRINT_NAME="My New Document"   # default: New Document Type
#
#   bash scripts/test_new_doc.sh
# =============================================================

BASE_URL="${NEBIUS_ENDPOINT_URL:?Set NEBIUS_ENDPOINT_URL}"
TOKEN="${NEBIUS_ENDPOINT_TOKEN:?Set NEBIUS_ENDPOINT_TOKEN}"
BLUEPRINT_ID="${NEW_BLUEPRINT_ID:-new_doc_test}"
BLUEPRINT_NAME="${NEW_BLUEPRINT_NAME:-New Document Type}"
DOWNLOAD_PATH="/tmp/blueprint_${BLUEPRINT_ID}.json"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf "${GREEN}[OK]${NC}   %s\n" "$1"; }
err()  { printf "${RED}[FAIL]${NC} %s\n" "$1"; }
info() { printf "${YELLOW}[INFO]${NC} %s\n" "$1"; }
hdr()  { printf "\n${CYAN}══ %s ══${NC}\n" "$1"; }

# ── Build document payload ────────────────────────────────────
if [ -n "$NEW_DOC_PATH" ]; then
    if [ ! -f "$NEW_DOC_PATH" ]; then
        echo "File not found: $NEW_DOC_PATH"; exit 1
    fi
    EXT="${NEW_DOC_PATH##*.}"
    case "${EXT,,}" in
        jpg|jpeg) MIME="image/jpeg" ;;
        png)      MIME="image/png"  ;;
        pdf)      MIME="application/pdf" ;;
        *)        MIME="image/jpeg" ;;
    esac
    B64=$(base64 < "$NEW_DOC_PATH" | tr -d '\n')
    DOC_JSON="{\"type\":\"base64\",\"value\":\"$B64\",\"mime_type\":\"$MIME\"}"
    info "Using local file: $NEW_DOC_PATH ($MIME)"

elif [ -n "$NEW_DOC_NOS_KEY" ]; then
    DOC_JSON="{\"type\":\"nebius_object\",\"value\":\"$NEW_DOC_NOS_KEY\",\"mime_type\":\"image/jpeg\"}"
    info "Using NOS key: $NEW_DOC_NOS_KEY"

else
    err "Set either NEW_DOC_PATH or NEW_DOC_NOS_KEY"
    exit 1
fi

# ── Step 1: Submit with unknown blueprint_id ──────────────────
hdr "Step 1: Submit unknown document (expect 422 — blueprint not found)"

HTTP=$(curl -s -o /tmp/nd_step1.json -w "%{http_code}" --max-time 30 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"document\":$DOC_JSON,\"mode\":\"blueprint\",\"blueprint_id\":\"$BLUEPRINT_ID\"}")

if [ "$HTTP" = "422" ]; then
    MSG=$(python3 -c "import json; d=json.load(open('/tmp/nd_step1.json')); print(d.get('detail','?'))" 2>/dev/null)
    ok "Got 422 — blueprint not found: $MSG"
elif [ "$HTTP" = "200" ]; then
    info "Blueprint '$BLUEPRINT_ID' already exists — skipping generate step"
    info "Jumping straight to Step 4 (re-recognize)"
    SKIP_GENERATE=1
else
    err "Unexpected HTTP $HTTP (expected 422)"
    python3 -m json.tool /tmp/nd_step1.json 2>/dev/null || cat /tmp/nd_step1.json
    exit 1
fi

# ── Step 2: Generate blueprint from the document ──────────────
hdr "Step 2: Generate blueprint (POST /blueprints/generate, GPU pass, ~30s)"

if [ -z "$SKIP_GENERATE" ]; then
    info "Sending to /blueprints/generate — this calls VLM twice, please wait..."
    HTTP=$(curl -s -o /tmp/nd_step2.json -w "%{http_code}" --max-time 120 \
      -X POST "$BASE_URL/blueprints/generate" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{
        \"document\": $DOC_JSON,
        \"blueprint_id\": \"$BLUEPRINT_ID\",
        \"name\": \"$BLUEPRINT_NAME\",
        \"description\": \"Auto-generated from sample document\"
      }")

    if [ "$HTTP" = "201" ]; then
        FIELDS=$(python3 -c "import json; d=json.load(open('/tmp/nd_step2.json')); print(len(d.get('fields',[])))" 2>/dev/null)
        STATUS=$(python3 -c "import json; d=json.load(open('/tmp/nd_step2.json')); print(d.get('status','?'))" 2>/dev/null)
        ok "Blueprint generated: id=$BLUEPRINT_ID fields=$FIELDS status=$STATUS"
    elif [ "$HTTP" = "409" ]; then
        info "Blueprint '$BLUEPRINT_ID' already existed (409) — continuing"
    else
        err "Generate failed: HTTP $HTTP"
        python3 -m json.tool /tmp/nd_step2.json 2>/dev/null || cat /tmp/nd_step2.json
        exit 1
    fi
fi

# ── Step 3: Download the blueprint ───────────────────────────
hdr "Step 3: Download blueprint JSON (GET /blueprints/$BLUEPRINT_ID)"

HTTP=$(curl -s -o "$DOWNLOAD_PATH" -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/blueprints/$BLUEPRINT_ID")

if [ "$HTTP" = "200" ]; then
    FIELDS=$(python3 -c "import json; d=json.load(open('$DOWNLOAD_PATH')); print(len(d.get('fields',[])))" 2>/dev/null)
    ok "Blueprint downloaded → $DOWNLOAD_PATH ($FIELDS fields)"
    echo ""
    python3 -m json.tool "$DOWNLOAD_PATH"
else
    err "Download failed: HTTP $HTTP"
    cat "$DOWNLOAD_PATH"
    exit 1
fi

# ── Step 4: Re-recognize with the new blueprint ───────────────
hdr "Step 4: Re-recognize document using blueprint '$BLUEPRINT_ID' (GPU pass)"

info "Sending to /recognize with mode=blueprint — please wait..."
HTTP=$(curl -s -o /tmp/nd_step4.json -w "%{http_code}" --max-time 120 \
  -X POST "$BASE_URL/recognize" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"document\": $DOC_JSON,
    \"mode\": \"blueprint\",
    \"blueprint_id\": \"$BLUEPRINT_ID\",
    \"options\": {\"include_confidence\": true, \"confidence_mode\": \"both\"}
  }")

if [ "$HTTP" = "200" ]; then
    python3 -c "
import json
d = json.load(open('/tmp/nd_step4.json'))
print(f'  routing           : {d.get(\"routing\",\"?\")}')
print(f'  document_confidence: {d.get(\"document_confidence\",\"?\"):.3f}' if isinstance(d.get('document_confidence'), float) else f'  document_confidence: {d.get(\"document_confidence\",\"?\")}')
print(f'  fields extracted  : {len(d.get(\"fields\",{}))}')
print()
for name, val in (d.get('fields') or {}).items():
    v = val.get('value','?') if isinstance(val,dict) else val
    c = val.get('confidence') if isinstance(val,dict) else None
    conf_str = f'  conf={c:.3f}' if isinstance(c, float) else ''
    print(f'    {name}: {v}{conf_str}')
" 2>/dev/null
    ok "Recognition complete"
    echo ""
    info "Full response → /tmp/nd_step4.json"
else
    err "Recognition failed: HTTP $HTTP"
    python3 -m json.tool /tmp/nd_step4.json 2>/dev/null || cat /tmp/nd_step4.json
    exit 1
fi

echo ""
echo "══════════════════════════════════════════════"
printf "  ${GREEN}All 4 steps passed.${NC}\n"
printf "  Blueprint saved: ${CYAN}%s${NC}\n" "$DOWNLOAD_PATH"
echo "══════════════════════════════════════════════"
