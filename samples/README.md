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
| `recognize_srb_passport.json` | `POST /recognize` for a Serbian passport, `mode=auto` (distinct type 1) |
| `recognize_esp_id.json` | `POST /recognize` for a Spanish ID, `mode=auto` (distinct type 2) |
| `recognize_srb_passport_blueprint.json` | Same image, `mode=blueprint`/`blueprint_id=passport` — full field set (MRZ, surname/given_names) |
| `recognize_esp_id_blueprint.json` | Same image, `mode=blueprint`/`blueprint_id=id_card` — full field set |
| `eval_report.json` | The eval Job summary report (`eval/reports/<job_id>.json`) — serves as job logs |

> **Why both `auto` and `blueprint` variants (captured 2026-06-18, v31):** `mode=auto`'s
> classification step correctly identifies `classification.document_type` (`passport`/`id_card`)
> but the 7B VLM tends to also emit `blueprint_id: "default"` instead of the specific catalog ID —
> a model-classification limitation, not a code bug (`extract_auto` in `extractor.py` just trusts
> whatever `blueprint_id` the model returns). The `auto` files show this real, end-to-end
> behavior (still produces correct field values, just via the generic blueprint). The
> `_blueprint` files show the same documents through `mode=blueprint` with the matching catalog
> ID forced explicitly — this is also the mode the MIDV-2020 eval job uses (`nebius-job/job.py`),
> so these are the files that are methodologically consistent with the accuracy table in the
> main README.

## How to capture

```bash
# Live endpoint (set after deploy — see scripts/deploy-endpoint.sh)
export NEBIUS_ENDPOINT_URL="http://<PUBLIC_IP>:8080"
export NEBIUS_ENDPOINT_TOKEN="<AUTH_TOKEN>"

# 0. Before capturing: make sure the blueprint catalog has no stale drafts.
#    Only id_card/default/passport/residence_permit_ltu_front should be listed — leftover
#    demo_gen_* drafts from manual /demo testing pollute mode=auto classification (happened
#    once during v31 capture, see CLAUDE.md "Deploy incident log"). DELETE any stragglers first:
#    curl -X DELETE "$NEBIUS_ENDPOINT_URL/blueprints/<id>" -H "Authorization: Bearer $NEBIUS_ENDPOINT_TOKEN"
curl -s "$NEBIUS_ENDPOINT_URL/blueprints" -H "Authorization: Bearer $NEBIUS_ENDPOINT_TOKEN" | python3 -m json.tool

# 1. Two recognition results from distinct document types, mode=auto
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

# 2. Same documents, mode=blueprint with the matching catalog ID forced — full field set,
#    methodologically consistent with the eval job (nebius-job/job.py also uses mode=blueprint)
for f in srb_passport:nebius-endpoint/app/static/samples/49.jpg:passport \
         esp_id:nebius-endpoint/app/static/samples/11.jpg:id_card; do
  type="${f%%:*}"; rest="${f#*:}"; img="${rest%%:*}"; bp="${rest##*:}"
  b64=$(base64 -w0 "$img")
  curl -s "$NEBIUS_ENDPOINT_URL/recognize" \
    -H "Authorization: Bearer $NEBIUS_ENDPOINT_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"document\":{\"type\":\"base64\",\"value\":\"$b64\",\"mime_type\":\"image/jpeg\"},\"mode\":\"blueprint\",\"blueprint_id\":\"$bp\",\"options\":{\"include_confidence\":true}}" \
    > "samples/recognize_${type}_blueprint.json"
done

# 3. Eval summary report (after the Job finishes — see README "Running the eval job yourself")
aws s3 cp "s3://<YOUR_NOS_BUCKET>/eval/reports/<job_id>.json" samples/eval_report.json \
  --endpoint-url https://storage.eu-north1.nebius.cloud
```

Pick sample image numbers that match each type; the IDs above are illustrative —
verify against the rendered images in `nebius-endpoint/app/static/samples/`.
