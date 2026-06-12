# Implementation Plan: docs-proc-nebius — Nebius Document Recognition Pipeline

## Overview

**Architecture:** standalone-first, no AWS dependencies. Endpoint operates without AWS dependencies. Two document delivery modes: `base64` (inline) and `nebius_object` (via NOS). lity integration is an optional client.

**Workstreams:**
1. `nebius-endpoint` — FastAPI + vLLM container (main product)
2. `nebius-job` — batch container
3. `lity-backend` — optional integration (after main product)

---

## Implementation Status: Completed vs. Spec

### ✅ Fully implemented

| Component | What's done |
|---|---|
| `app/router.py` | `clamp_confidence` + `get_routing`, no heavy dependencies |
| `app/extractor.py` | All modes: `raw`, `blueprint`, `auto`, `double_check`; clamping via router |
| `app/main.py` | `/recognize`, `/health`, `/blueprints` CRUD, `/blueprints/reload` |
| `app/blueprint_loader.py` | BlueprintStore with S3 CRUD, in-memory cache |
| `app/models.py` | Pydantic models for all requests/responses |
| `app/auth.py` | Bearer token verify (AUTH_TOKEN) |
| `app/config.py` | All env vars |
| `app/pdf_converter.py` | PDF → JPEG via poppler |
| `app/mock_vllm.py` | CPU mock for testing |
| `tests/test_router.py` | P1, P2 — Hypothesis PBT, 2/2 tests pass |
| `tests/conftest.py` | Fixtures rewritten for new architecture |
| `Dockerfile`, `Dockerfile.base`, `Dockerfile.cpu` | Two-layer architecture |
| `docker-compose.cpu.yml` | CPU test stack |
| `start.sh`, `nginx.conf` | Three-process container |
| `nebius-job/job.py` | Batch job — manifest, retry, exponential backoff, summary |
| `nebius-job/Dockerfile` | Job container |
| `lity-backend/nebius-adapter.js` | SSM cache, presigned URL, fetch /recognize, DynamoDB write |
| `lity-backend/documents.js` | `handleRecognizeDocument` + route dispatch |
| `.github/workflows/nebius-build-push.yml` | CI/CD build + push |
| `.github/workflows/nebius-deploy-endpoint.yml` | Deploy endpoint |
| `.github/workflows/nebius-run-job.yml` | Run batch job |
| Nebius infrastructure | Account, SA, registry, endpoint v17 deployed |

### ❌ Not implemented (new spec requirements)

| What's needed | Requirement |
|---|---|
| Rich Blueprint Format (`sections` + `inferenceType`) in files | Req 5.4 |
| `BlueprintStore._normalize()` — flatten sections → fields[] | Req 6.3 |
| `_catalog.json` — loading via catalog, not glob | Req 6.2 |
| Blueprint files in NOS format: `default/v1.json`, `passport/v1.json`, `residence_permit_ltu_front/v1.json` | Req 4.1 |
| `GET /inbound/presign` — NOS presigned PUT URL for clients | Req 1.2 |
| `document.type: "nebius_object"` in Endpoint (fetch from NOS) | Req 9.1 |
| NOS write: `outbound/YYYY/MM/DD/HH/mm/<id>.json` after recognition | Req 9.4 |
| NOS write: `logs/YYYY/MM/DD/HH/mm/req_<id>.json` after recognition | Req 12.2 |
| `POST /blueprints/generate` — blueprint generation from sample | Req 10.1 |
| Tests P3, P4, P5 (`test_extractor.py`) | Design P3–P5 |
| Tests P6, P7 (`nebius-job/tests/test_job.py`) | Design P6–P7 |
| Tests P8, P9, P10 (`nebius-adapter.test.js`) | Design P8–P10 |
| Test `test_endpoint.py` — HTTP API examples | Design P11 |
| `nebius-adapter.js` NOS upload (instead of presigned URL) + fallback | Req 7.1 |
| SSM parameters for NOS in `nebius-adapter.js` | Req 7.4 |

### ⚠️ Partially implemented / discrepancies

| Component | Issue |
|---|---|
| `blueprint_loader.py` | Loads flat JSON (`blueprints/*.json`), not Rich Format + `_catalog.json` |
| `job.py` | Uses `presigned_url` as document type, not `nebius_object` |
| `nebius-adapter.js` | Uses AWS presigned GET URL, not NOS upload |
| Blueprint files | `app/blueprints/` directory does not exist — no blueprints at all |
| `documents-recognize.test.js` | Does not exist |

---

## Tasks

### Workstream 0: Infrastructure ✅ DONE

- [x] 0.1 Nebius account, project, quotas
- [x] 0.2 Nebius CLI + Docker credentials helper
- [x] 0.3 Service Account for CI/CD
- [x] 0.4 Container Registry
- [x] 0.5 GitHub Secrets (basic)
  - [ ] 0.5a Add NOS secrets: `NEBIUS_S3_ACCESS_KEY`, `NEBIUS_S3_SECRET_KEY`, `NEBIUS_S3_BUCKET`
    - _Requirements: 11.2, 5.5_

- [x] 1. Scaffold: directories, .gitignore, LICENSE

---

### Workstream 1: Nebius Endpoint

#### 1A. Blueprints — Rich Format (new)

- [ ] 2. Create blueprint files in Rich Format
  - [x] 2.1 Create `nebius-endpoint/blueprints/default/v1.json`
    - Catch-all blueprint: all fields optional, sections DOCUMENT_METADATA + PERSONAL_INFO + DOCUMENT_DETAILS
    - Extraction prompt: "Extract ALL visible structured information from this document"
    - _Requirements: 4.2, 5.4_

  - [x] 2.2 Create `nebius-endpoint/blueprints/passport/v1.json`
    - Rich Format with sections: DOCUMENT_METADATA (document_type, document_number, issuing_country), PERSONAL_INFO (surname, given_names, sex, nationality, date_of_birth), DOCUMENT_DETAILS (date_of_expiry, date_of_issue, mrz_line_1, mrz_line_2)
    - inferenceType: explicit for verbatim fields, inferred for dates (YYYY-MM-DD) and sex (M/F)
    - Based on `ltu-prp-scheme-front.json` pattern
    - _Requirements: 4.3, 5.4_

  - [x] 2.3 Create `nebius-endpoint/blueprints/residence_permit_ltu_front/v1.json`
    - Take fields directly from `specs-actual/ideas/doc-management/doc-recognition/ltu-prp-scheme-front.json`
    - Sections: DOCUMENT_METADATA, PERSONAL_INFO, PERMIT_DETAILS, ADDITIONAL_INFO
    - _Requirements: 4.4, 5.4_

  - [x] 2.4 Create `nebius-endpoint/blueprints/_catalog.json`
    - List all three blueprints with status: "active", latest_version: 1, path
    - _Requirements: 6.2_

- [ ] 3. Update `BlueprintStore` for Rich Format
  - [x] 3.1 Add `_normalize(rich: dict) -> dict` — flatten sections → fields[]
    - For each field in sections extract: name, instruction, inferenceType, required, _section
    - Result stored in `normalized["fields"]` — compatible with current Extractor
    - _Requirements: 6.3_

  - [x] 3.2 Update `_load_all()` — read via `_catalog.json`
    - First try reading `blueprints/_catalog.json`
    - Load only `status: "active"` entries by `path`
    - Fallback (if catalog missing): current glob over `blueprints/*/` for highest version
    - **Added:** `_load_from_local()` — loads from filesystem when S3 credentials are absent
    - _Requirements: 6.2_

  - [x] 3.3 Update `create()` / `update()` — write to `blueprints/<id>/vN.json`
    - On create: `blueprints/<id>/v1.json`
    - On update: compute new N = latest_version + 1, write `vN.json`, old version preserved
    - After each operation update `_catalog.json`
    - DELETE: no file deletion, only set status → "deprecated" in `_catalog.json`
    - _Requirements: 6.1_

- [x] 4. Implement `GET /inbound/presign` (new)
  - Accepts query param `filename`
  - Returns `{ presigned_put_url, nos_key, expires_in: 300 }`
  - `nos_key = inbound/YYYY/MM/DD/HH/mm/<uuid>.<ext>`
  - Generates NOS presigned PUT URL via boto3 `generate_presigned_url("put_object")`
  - If NOS not configured → 503
  - Add to `nginx.conf` location `/inbound/presign → FastAPI`
  - _Requirements: 1.2_

- [x] 5. Implement `document.type: "nebius_object"` in Extractor
  - In `preprocess_document()`: for type `"nebius_object"` — `s3_client.get_object(Bucket, Key=value)` → bytes
  - If NOS not configured → raise → HTTP 503
  - _Requirements: 9.1_

- [ ] 6. NOS write: outbound + logs (best-effort, non-blocking)
  - [x] 6.1 After each `/recognize` request — write `outbound/YYYY/MM/DD/HH/mm/<request_id>.json`
    - Generate `request_id = uuid4()`
    - Add `request_id` to RecognizeResponse
    - Write via `asyncio.create_task()` — does not block HTTP response
    - _Requirements: 9.4_

  - [ ] 6.2 Write `logs/YYYY/MM/DD/HH/mm/req_<request_id>.json`
    - Contents: `request_id`, `timestamp`, `blueprint_id`, `mode`, `document_confidence`, `routing`, `latency_ms`, `model`, `endpoint_version`
    - _Requirements: 12.2_

- [x] 7. `POST /blueprints/generate` — blueprint generation from sample (new)
  - Pass 1: `mode=raw` VLM inference → free-text description of document fields
  - Pass 2: VLM prompt → generate Rich Blueprint JSON (sections + inferenceType + instruction)
  - Save as `blueprints/<id>/v1.json` with `status: "draft"`, update `_catalog.json`
  - Return HTTP 201 with generated blueprint
  - Blueprint is NOT loaded into active cache until `PUT /blueprints/{id}` with `status: "active"`
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

#### 1B. Already implemented, verify/fix

- [x] 8. `app/router.py` — clamp_confidence + get_routing ✅
- [x] 9. `app/extractor.py` — all modes ✅
- [x] 10. `app/main.py` — /recognize, /health, /blueprints CRUD ✅
  - [ ] 10.1 Update `main.py` — add `/inbound/presign`, `/blueprints/generate`
    - _Requirements: 1.2, 10.1_
- [x] 11. `tests/test_router.py` — P1, P2 ✅ (2/2 pass)
- [x] 12. `tests/conftest.py` ✅

#### 1C. Tests (some optional)

- [x] 13. Write `tests/test_extractor.py` — P3, P4, P5
  - **P3:** Required fields never omitted — Hypothesis + mock VLM
  - **P4:** Confidence scores — integers in [0, 100]
  - **P5:** Fields schema-valid against blueprint
  - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**

- [x] 14. Write `tests/test_endpoint.py` — HTTP API examples
  - 422 on unknown blueprint_id, 422 on missing blueprint_id
  - 503 on nebius_object without NOS credentials
  - Health response shape (P10)
  - **Validates: Requirements 1.1, 1.2, 1.10, 12.4**

- [x] 15. Checkpoint Endpoint
  - `python -m pytest nebius-endpoint/tests/ --tb=short` — all tests green
  - Sync to srv55: `rsync -avz --exclude='.git' --exclude='__pycache__' /Users/ds/lity/nebius-endpoint/ srv55:/home/lity-nebius/nebius-endpoint/`
  - Rebuild CPU image: `docker compose -f docker-compose.cpu.yml up --build -d`
  - Smoke test: `curl -H "Authorization: Bearer test-token-123" http://192.168.10.55:8080/health`

---

### Workstream 2: Nebius Inference Job

- [x] 16. `nebius-job/job.py` ✅ — manifest, retry, exponential backoff, summary
  - [x] 16.1 Update `job.py` — use `document.type: "nebius_object"` instead of `presigned_url`
    - Manifest format: `{ document_id, blueprint_id, nos_key, mime_type }` (instead of presigned_url)
    - `ENDPOINT_TOKEN` env var — add `Authorization: Bearer` header
    - Add `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` env vars
    - _Requirements: 8.4_

  - [ ] 16.2 Job summary → write to `logs/YYYY/MM/DD/HH/mm/job_<job_id>.json` in NOS
    - _Requirements: 12.3_

- [x] 17. `nebius-job/Dockerfile` ✅

- [x] 18. Write `nebius-job/tests/test_job.py` — P6, P7
  - **P6:** Exactly N output files for N manifest items
  - **P7:** Exit 0 regardless of individual failures
  - **Validates: Requirements 8.3, 8.4, 8.5, 8.6, 8.7**

- [x] 19. Checkpoint Job
  - `python -m pytest nebius-job/tests/ --tb=short`

---

### Workstream 3: lity-backend (optional integration)

- [x] 20. `lity-backend/nebius-adapter.js` ✅ (basic implementation with presigned URL)
  - [ ] 20.1 Update: NOS upload before `/recognize`
    - Step 1: `s3.GetObject` from `lity-poc-docs` → document bytes
    - Step 2: Generate NOS presigned PUT URL via `@aws-sdk/client-s3` (endpoint: NOS)
    - Step 3: PUT bytes to NOS → `inbound/YYYY/MM/DD/HH/mm/<documentId>.<ext>`
    - Step 4: POST `/recognize` with `document.type: "nebius_object"`
    - Fallback on NOS error: POST with `document.type: "base64"` (encode document bytes)
    - _Requirements: 7.1_

  - [ ] 20.2 Add SSM parameters for NOS
    - `/lity/nebius-s3-access-key` (SecureString)
    - `/lity/nebius-s3-secret-key` (SecureString)
    - `/lity/nebius-s3-bucket` (String)
    - _Requirements: 7.4_

- [x] 21. `lity-backend/documents.js` — `handleRecognizeDocument` ✅

- [ ] 22. lity-backend tests
  - [ ] 22.1 `__tests__/nebius-adapter.test.js` — P8, P9, P10
    - **P8:** DDB written iff HTTP 200 (fast-check)
    - **P9:** Handler never throws (fast-check)
    - **P10:** Log entries contain required fields (Jest)
    - **Validates: Requirements 7.2, 7.3**

  - [ ] 22.2 `__tests__/documents-recognize.test.js`
    - Missing blueprintId → 400; case not found → 404; adapter error → 502
    - _Requirements: 7.1_

- [ ] 23. Checkpoint lity-backend
  - `npm test --runInBand` in `lity-backend/`

---

### Workstream 4: NOS Setup and Blueprint Upload

- [x] 24. Create NOS bucket and static key
  - `nebius object-storage bucket create --name your-nos-bucket --parent-id $PROJECT_ID`
  - `nebius iam service-account static-key create --service-account-id <SA_ID>`
  - Save key_id + secret → GitHub Secrets + AWS SSM
  - _Requirements: 5.5, 11.1_

- [ ] 25. Upload blueprints to NOS
  - Prerequisite: task 2 must be completed (Rich Format blueprints created)
  - Upload `_catalog.json` and three blueprint files at path `blueprints/*/v1.json`
  - ```bash
    for bp in default passport residence_permit_ltu_front; do
      aws s3 cp nebius-endpoint/blueprints/${bp}/v1.json \
        s3://your-nos-bucket/blueprints/${bp}/v1.json \
        --endpoint-url https://storage.eu-north1.nebius.cloud
    done
    aws s3 cp nebius-endpoint/blueprints/_catalog.json \
      s3://your-nos-bucket/blueprints/_catalog.json \
      --endpoint-url https://storage.eu-north1.nebius.cloud
    ```
  - _Requirements: 4.1, 6.2_

---

### Workstream 5: Final Deployment and Contest Artifacts

- [x] 26. Rebuild and deploy GPU endpoint (v18; current — v21)
  - rsync → srv55 → docker build → push → `nebius ai endpoint create`
  - Pass `--env S3_ACCESS_KEY=... --env S3_SECRET_KEY=... --env S3_BUCKET=your-nos-bucket`
  - Update SSM: `/lity/nebius-endpoint-url`, `/lity/nebius-endpoint-token`
  - _Requirements: 1.9, 5.5_

- [x] 27. Smoke tests on GPU endpoint
  - ✅ `smoke_test.sh` — 33/33 tests passed on **v22** (2026-06-12): health/auth, vLLM direct, validation, blueprints CRUD, NOS presign, GPU inference all modes, confidence modes, logprobs working (confidence_source: "logprobs")
  - ⚠️ Recognition_Result samples for submission must be recreated on MIDV-2020 (current ones are from real documents, PII — task 31)
  - _Requirements: 1.1–1.5, 4.3, 4.4, 11.6_

---

### Workstream 6: Contest v2 — Winning Improvements

#### 6A. P0 — Compliance ✅

- [x] 31. PII cleanup and public dataset MIDV-2020
  - [x] 31.1 Real documents removed, `.gitignore` updated
  - [x] 31.2 `prepare_midv2020.sh` + `build_midv_manifest.py` — 60 documents (esp_id, grc_passport, srb_passport) uploaded to NOS `eval/midv2020/`
  - [x] 31.3 Blueprint `id_card` created, uploaded to NOS, `_catalog.json` updated
  - [ ] 31.4 Recognition_Result samples must be recreated on MIDV-2020 — screenshots from real documents in the dataset needed

- [x] 32. Public repository sanitization
  - [x] 32.1 `scripts/export_public.sh` — rsync whitelist + placeholder substitution + secret scan gate
  - [x] 32.2 `smoke_test.sh` — all secrets replaced with `${NEBIUS_ENDPOINT_URL:?}` etc.
  - [ ] 32.3 Create public repo `docs-proc-nebius`: fresh git history, MIT LICENSE, `.env.example`

#### 6B. P0 — Real Confidence (Logprobs) ✅

- [x] 33. Logprob-based confidence — implemented, working on GPU (smoke T19: confidence_source="logprobs")

#### 6C. P1 — Guided JSON + Evaluation Job ✅

- [x] 34. Guided JSON decoding — `blueprint_to_guided_schema`, retry fallback, test P12
- [x] 35. Evaluation Job (MIDV-2020)
  - [x] 35.1–35.4 `job.py` eval mode, `eval_metrics.py`, summary report, test P14
  - [x] 35.5 Eval run on GPU v22: 60 docs, job_id=20260612_214444_acc95d
    - document_number 100%, srb_passport 77%, esp_id 68%, grc_passport 25%
    - Latency p50=1.84s p95=2.34s, Cost=$0.001/doc, Calibration gap=5.5pp

#### 6D. P2 — Packet splitting + Demo UI ✅

- [x] 36. `mode="packet"` — pdf_converter, extract_packet, PacketResponse, test P13
- [x] 37. Demo UI — `app/static/demo.html`, `/demo` + `/static` in FastAPI and nginx

#### 6E. Final Stretch

- [x] 38. Deploy v22 — `<YOUR-ENDPOINT-ID>`, IP `<YOUR-ENDPOINT-IP>:8080`, 33/33 smoke pass (2026-06-12)
- [ ] 39. Video walkthrough (3–10 min)
- [ ] 28. README.md ← **in progress**
- [ ] 29. Technical blog post (≥600 words, `#NebiusServerlessChallenge`)
- [ ] 30. Contest submission (by 30.06.2026)

---

## Priorities (what to do first)

Workstreams 0–5 are largely complete (v21 deployed, 33/33 smoke). Contest priority is Workstream 6, deadline June 30 2026:

```
1. Task 33 (logprob confidence)   ← core product, affects all modes and eval
2. Task 34 (guided_json)          ← done together with 33 (one pass through extractor.py)
3. Task 31 (MIDV-2020 + PII)      ← parallel with 33/34; blocks 35, 37.2, samples
4. Task 35 (Evaluation Job)       ← after 31+33; provides metrics for README/blog
5. Task 36 (packet mode)          ← independent, after 33/34
6. Task 37 (Demo UI)              ← after 31 (samples needed)
7. Task 32 (public repo)          ← after code stabilization
8. Tasks 38, 28, 29, 39, 30       ← deploy → README → blog → video → submission
```

---

## Bugs / Backlog

| # | Severity | Description | Location | Discovered |
|---|---|---|---|---|
| BUG-1 | Medium | `mode=auto` on empty/unrecognized image: classification returns `document_type="default", blueprint_id=null, confidence=0` → `extract_auto` logs `"Blueprint not found: default"` even though the default blueprint exists. Likely cause: lookup uses `blueprint_id` from classification (null) instead of `document_type`. Routing correctly set to `escalate_to_operator`, but `raw_text` contains error message instead of null. | `extractor.py` → `extract_auto()` | smoke_test T23, v22, 2026-06-12 |

---

## Notes

- Tasks 13, 14, 18, 22 — optional tests, can be skipped for fast MVP
- Task 7 (`/blueprints/generate`) — optional feature
- Workstream 3 (lity-backend) — low priority, done after standalone is ready
- lity-backend uses CommonJS (`require`/`module.exports`), not ESM
- Docker builds only on Linux (srv55), not on Mac
- After each `nebius ai endpoint create` — update SSM parameters url + token

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["33.1", "33.2", "31.1", "31.2", "31.3"], "note": "logprobs core + dataset — in parallel" },
    { "id": 1, "tasks": ["33.3", "33.4", "34.1", "34.2"] },
    { "id": 2, "tasks": ["33.5", "34.3", "34.4", "35.1", "35.2"] },
    { "id": 3, "tasks": ["35.3", "35.4", "36.1", "36.2", "37.1"] },
    { "id": 4, "tasks": ["36.3", "36.4", "37.2", "37.3", "31.4"] },
    { "id": 5, "tasks": ["38"], "note": "deploy v22 with all improvements" },
    { "id": 6, "tasks": ["35.5", "27-rerun", "32.1", "32.2"], "note": "eval on GPU + sanitization" },
    { "id": 7, "tasks": ["32.3", "28"], "note": "public repo + README" },
    { "id": 8, "tasks": ["29", "39"], "note": "blog + video" },
    { "id": 9, "tasks": ["30"], "note": "submission by June 30" }
  ]
}
```
