# Proof-of-Execution Samples

Committed artifacts from a **live run** of the deployed endpoint and the MIDV-2020
evaluation Job, referenced from the README [Proof of Execution](../README.md#proof-of-execution)
section. All inputs are synthetic [MIDV-2020](https://smartengines.com/midv-2020/) documents —
no personal data.

> These files are produced against a live Nebius GPU endpoint and committed before
> submission. Capture them with the commands below.

## Expected files

| File | What it is |
|---|---|
| `recognize_srb_passport.json` | `POST /recognize` result for a Serbian passport (distinct type 1) |
| `recognize_esp_id.json` | `POST /recognize` result for a Spanish ID (distinct type 2) |
| `eval_report.json` | The eval Job summary report (`eval/reports/<job_id>.json`) — serves as job logs |

## How to capture

```bash
# Live endpoint (set after deploy — see scripts/deploy-endpoint.sh)
export NEBIUS_ENDPOINT_URL="http://<PUBLIC_IP>:8080"
export NEBIUS_ENDPOINT_TOKEN="<AUTH_TOKEN>"

# 1. Two recognition results from distinct document types
for f in srb_passport:nebius-endpoint/app/static/samples/49.jpg \
         esp_id:nebius-endpoint/app/static/samples/11.jpg; do
  type="${f%%:*}"; img="${f##*:}"
  b64=$(base64 -w0 "$img")
  curl -s "$NEBIUS_ENDPOINT_URL/recognize" \
    -H "Authorization: Bearer $NEBIUS_ENDPOINT_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"document\":{\"type\":\"base64\",\"value\":\"$b64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"auto\",\"options\":{\"include_confidence\":true}}" \
    > "samples/recognize_${type}.json"
done

# 2. Eval summary report (after the Job finishes — see README "Running the eval job yourself")
aws s3 cp "s3://<YOUR_NOS_BUCKET>/eval/reports/<job_id>.json" samples/eval_report.json \
  --endpoint-url https://storage.eu-north1.nebius.cloud
```

Pick sample image numbers that match each type; the IDs above are illustrative —
verify against the rendered images in `nebius-endpoint/app/static/samples/`.
