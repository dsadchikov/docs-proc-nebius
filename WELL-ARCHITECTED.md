# Well-Architected Review — docs-proc-nebius

A pillar-by-pillar review of the document-recognition service, mapped to **Nebius
infrastructure**. It records the design decisions behind the solution, how each Well-Architected
pillar is addressed using Nebius-native services, and what is intentionally deferred.

**Scope & framing.** This is a contest submission running on a **single H100 SXM Serverless
Endpoint** — a reproducible reference deployment, not a multi-region production SLA service. Cheap,
high-value hardening is *implemented*; production-scale concerns (multi-replica failover,
per-tenant isolation) are called out as **future work** with the Nebius path shown. All data is
synthetic [MIDV-2020](https://smartengines.com/midv-2020/) — no personal data.

**Architecture in one line.** Client → Nebius Serverless Endpoint ingress → FastAPI/uvicorn
(`:8080`, PID 1) → vLLM (`Qwen2.5-VL-7B-Instruct`, `127.0.0.1:8000`, model baked in, offline),
with Nebius Object Storage (NOS) for blueprints, inbound uploads, and results.

---

## 1. Operational Excellence

**How it's addressed on Nebius**
- **Single-command deploy** — `scripts/deploy-endpoint.sh` wraps `nebius ai endpoint create` with
  the correct flags and Mysterybox secret references, replacing an error-prone manual sequence.
- **Structured JSON logging** — every log line is one JSON object (`app/logging_config.py`) with a
  `request_id`, mode, routing, and latency, ready for ingestion/aggregation.
- **Metrics endpoint** — `GET /metrics` emits Prometheus text exposition
  (`docproc_requests_total`, `docproc_request_duration_seconds`, `docproc_vllm_up`) for **Nebius
  Managed Prometheus / Grafana** to scrape.
- **Health probe** — `GET /health` actively probes vLLM and returns `503/"vllm":"down"` when the
  model backend is unavailable, so the Nebius readiness signal is truthful.
- **Reproducible verification** — a 35-check `smoke_test.sh` and a `pytest` suite (incl. property
  tests and hardening tests) gate every change; `MOCK_VLLM=1` runs the full pipeline on CPU.

**Deferred / future work**
- CI/CD lives in a private repo today; publishing a build→push→deploy pipeline (e.g. Nebius
  Container Registry + GitHub Actions OIDC) would remove the last manual steps.
- Centralized dashboards/alerts on the `/metrics` series.

## 2. Security

**How it's addressed on Nebius**
- **Secrets by reference, not value** — `--env-secret`/`--token-secret` load credentials from
  **Nebius Mysterybox** (`mbsec-…`) at deploy time; no plaintext secrets in the endpoint spec or
  shell history. Mysterybox is the single, versioned, KMS-backed source of truth.
- **App-level auth** — `verify_token` (constant-time `hmac.compare_digest`) protects `/recognize`,
  the blueprint APIs, `/inbound/presign`, and the `/v1/*` model passthrough. Only `/health`,
  `/demo`, `/static`, `/metrics` are public. A **fail-open guard** warns loudly if a GPU
  deployment starts without a token set.
- **DoS guards** (restored after the nginx layer was removed) — a request **body-size limit**
  (`MAX_UPLOAD_BYTES` → HTTP 413) and a **per-request deadline** (`REQUEST_TIMEOUT` → HTTP 504,
  satisfying Req 1.6).
- **SSRF allowlist** — `document.type=presigned_url` is restricted to `https` URLs whose host is in
  an allowlist (default: the NOS endpoint host), for both server-side fetch and the URL handed to
  vLLM.
- **Input validation** — `document.type` is an enum; `PDF_MAX_PAGES` is enforced in single-page as
  well as packet mode; blueprint IDs are pattern-checked.
- **Least-privilege identity** — `scripts/setup-iam.sh` provisions a dedicated Service Account
  scoped to the blueprints bucket instead of a broad shared key.
- **Encryption in transit** — all transport is HTTPS (Nebius ingress; S3v4-signed NOS calls). NOS
  supports server-side encryption at rest — enabling/confirming it on the bucket is recommended.

**Deferred / future work**
- Per-client identities, token rotation, and scopes (single shared token is acceptable for the
  demo). The Nebius path: Mysterybox-versioned tokens + `--token-secret`.
- **Object-level access control** in NOS — today any token holder can address any key. The
  production path is per-tenant prefixes enforced by scoped SA credentials and bucket policies.
- Tightening CORS from configurable to a pinned demo origin (`CORS_ALLOW_ORIGINS`).

## 3. Reliability

**How it's addressed on Nebius**
- **vLLM self-healing** — `start.sh` supervises vLLM in a restart-on-crash loop; uvicorn stays
  PID 1 so the container/port stays up and `/health` reports the gap.
- **Decoupled readiness** — uvicorn opens `:8080` in seconds while the model warms up in the
  background, so the Nebius readiness probe passes instead of timing out (root cause of an earlier
  deploy hang).
- **Graceful timeouts** — vLLM call timeouts and the per-request deadline map to **HTTP 504**
  rather than an opaque 500; the guided-JSON path retries once without the constraint on backend
  error.
- **Durable, recoverable state** — blueprints live in NOS; on restart the `BlueprintStore` reloads
  from `_catalog.json` with a directory-scan fallback, skipping any single corrupt file.
- **Non-blocking side effects** — outbound result/log writes to NOS are best-effort and never block
  or fail the API response.

**Deferred / future work**
- **Single GPU / single replica** is intentional for the contest. The production path is
  multi-replica with autoscaling and an SLA — available on **Nebius dedicated endpoints / Token
  Factory**, not the single-instance Serverless Endpoint CLI used here.
- Retry-with-backoff and a bounded retry budget on NOS writes.

## 4. Performance Efficiency

**How it's addressed on Nebius**
- **Right model on right hardware** — `Qwen2.5-VL-7B-Instruct` in `bfloat16` fits one H100 SXM at
  `--gpu-memory-utilization 0.85`; no multi-GPU tensor parallelism needed.
- **Model baked into the image** with `HF_HUB_OFFLINE=1` — no cold-start download; vLLM loads
  offline in ~2 min while the API already serves.
- **Guided JSON + logprobs in one pass** — schema-locked output removes post-hoc parsing/retries;
  per-field confidence comes from token logprobs rather than extra model calls.
- Measured: p50 **1.84 s/doc**, p95 **2.34 s/doc** on the eval set.

**Deferred / future work**
- Tune `--max-num-seqs` and add request batching for higher throughput under concurrency.
- Optional image downscaling before inference for very large uploads.

## 5. Cost Optimization

**How it's addressed on Nebius**
- **Per-second serverless billing** — no charge while the endpoint is stopped; ~**$0.001/doc**,
  ~$1 per 1,000 docs at the measured latency.
- **Right-sized disk** — `--disk-size 80Gi` (model ~20 GB + headroom) instead of the 250Gi default,
  cutting idle storage cost.
- **Same image, live and batch** — the endpoint and the MIDV-2020 evaluation **Job** share one
  image and code path, avoiding a second artifact to build and pay for.
- **Stop-when-idle** — the endpoint is stopped between sessions; per-second billing means no idle
  compute charge.

**Deferred / future work**
- Scale-to-zero / scheduled scaling (a dedicated-endpoint capability) for bursty production
  traffic.
- Result caching/dedup for repeated identical documents.

## 6. Sustainability

**How it's addressed on Nebius**
- A **7B** model (not 72B) on a single GPU minimizes energy per document while meeting accuracy
  targets.
- Per-second billing + stop-when-idle + right-sized disk mean compute and storage are consumed only
  when doing useful work.
- Offline, baked-in weights avoid repeated network transfer of a ~16 GB model on every cold start.

**Deferred / future work**
- Consolidate bursty workloads onto shared autoscaling capacity to raise average utilization.

---

*Companion documents: `README.md` (setup & API), the C4 model in `internal-docs/`, and the
requirements spec under `.kiro/specs/`. Hardening changes referenced above are implemented in
`nebius-endpoint/app/` and covered by `nebius-endpoint/tests/test_hardening.py`.*
