#!/usr/bin/env bash
# Fetch secrets from Nebius Mysterybox and write .secrets.env (gitignored).
# Usage: bash scripts/fetch-secrets.sh
#
# Prerequisites: nebius CLI authenticated (nebius profile activate lity-nebius)

set -euo pipefail

SECRET_ID="mbsec-e00bkegre2hbv41e4n"
OUT=".secrets.env"

echo "Fetching from Nebius Mysterybox ($SECRET_ID)..."

RAW=$(nebius mysterybox payload get --secret-id "$SECRET_ID" 2>/dev/null)

if [ -z "$RAW" ]; then
  echo "ERROR: empty response from mysterybox. Are you authenticated?" >&2
  exit 1
fi

{
  echo "# !! DO NOT COMMIT !! Added to .gitignore"
  echo "# Source of truth: Nebius Mysterybox $SECRET_ID"
  echo "# Updated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""
  echo "$RAW" | python3 -c "
import sys, re
key = None
for line in sys.stdin:
    m = re.match(r'\s+-?\s*key:\s+(\S+)', line)
    if m:
        key = m.group(1)
        continue
    m = re.match(r'\s+string_value:\s+(.+)', line)
    if m and key:
        print(f'{key}={m.group(1).strip()}')
        key = None
"
} > "$OUT"

chmod 600 "$OUT"
echo "Written $OUT (chmod 600)"
echo ""
echo "Keys stored:"
grep -v '^#' "$OUT" | grep '=' | sed 's/=.*/=***/'
