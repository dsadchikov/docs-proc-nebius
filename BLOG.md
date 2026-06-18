# How I Built a Serverless Document-Recognition API on Nebius GPU Endpoints

*#NebiusServerlessChallenge*

---

Identity documents are everywhere — passports, national ID cards, residence permits — yet reliably extracting structured data from them is still hard. OCR gives you characters, but not meaning. A "date" field could be `DD.MM.YYYY`, `DD MMM YYYY`, or `YYYYMMDD` depending on the issuing country. The surname printed on the data page might differ from the MRZ. A confidence score of "high" is useless if you can't trust it.

For the Nebius Serverless AI Builders Challenge, I built **docs-proc-nebius**: a production-grade document-recognition API that extracts structured fields from identity documents, attaches per-field confidence scores derived from vLLM token log-probabilities, and enforces output structure via guided JSON decoding — all running on a single H100 SXM endpoint.

## The Stack

The service runs as a Docker container deployed to a **Nebius GPU Endpoint** (1× H100 SXM, 16 vCPU, 200 GB RAM). Inside the container:

- **FastAPI** handles HTTP routing, auth, NOS integration, and the demo UI
- **vLLM** serves `Qwen2.5-VL-7B-Instruct` — a vision-language model that takes an image plus a prompt and returns structured JSON
- **uvicorn** runs FastAPI directly on port 8080 as PID 1; vLLM warms up in the background. The Nebius endpoint ingress handles TLS and public routing, so there's no in-container reverse proxy to manage

The architecture is "standalone-first": all config comes from environment variables, blueprints can be served from the local filesystem or synced from Nebius Object Storage (NOS), and the same image runs CPU-only with `MOCK_VLLM=1` for local development without a GPU.

## Blueprints: Declarative Field Schemas

The core abstraction is a **blueprint** — a JSON document that describes what fields to extract from a given document type, with per-field instructions and inference hints. Here's a fragment of the passport blueprint:

```json
{
  "id": "passport",
  "extraction_prompt": "Extract all personal identification and travel document fields...",
  "sections": {
    "PERSONAL_INFO": {
      "surname": {
        "inferenceType": "explicit",
        "instruction": "Surname / last name in uppercase as printed on the document",
        "required": true
      },
      "date_of_birth": {
        "inferenceType": "inferred",
        "instruction": "Date of birth converted to YYYY-MM-DD format from any printed date format",
        "required": true
      }
    }
  }
}
```

Blueprints live in NOS and are hot-reloaded at runtime via `POST /blueprints/reload` — no container restart needed. There's also a `POST /blueprints/generate` endpoint that uses a two-pass VLM workflow to draft a blueprint from a sample document image.

## Guided JSON Decoding

Free-form text generation from a VLM occasionally produces malformed JSON — a missing quote, a trailing comma, or a hallucinated field name. The fix: **guided JSON decoding**.

Before each extraction call, the service converts the blueprint's field schema into a JSON Schema object and passes it to vLLM as the `guided_json` parameter. vLLM uses constrained decoding to guarantee the output conforms to the schema — no post-processing, no regex hackery. If the backend doesn't support guided JSON (e.g., a fallback CPU endpoint), the service logs a `guided_json_fallback` event and retries without the constraint.

## Per-Field Confidence from Log-Probabilities

Most document-recognition systems report a single document-level confidence score, which is too coarse to be useful. A passport extraction might be 95% confident on `document_number` but only 62% confident on a handwritten `place_of_birth`. You want to route those differently.

The service computes **per-field confidence** from vLLM's token log-probabilities:

1. Request `logprobs=true` from vLLM alongside the text generation
2. For each extracted field value, locate the token span in the output that encodes that value
3. Compute `confidence = round(100 × exp(mean(logprob)))` over the span tokens
4. Fall back to the mean over the whole response if span mapping fails

The result is a calibrated `confidence` integer (0–100) per field, tagged with `confidence_source: "logprobs"`. In our MIDV-2020 evaluation, the mean confidence on correctly-extracted fields was 98.2 vs 92.7 on incorrect ones — a 5.5 pp calibration gap. The model is slightly overconfident on errors (expected for a 7B VLM), but the signal is directionally correct and useful for triage routing.

## Recognition Modes

The API exposes five `mode` values on `POST /recognize`:

- **`blueprint`** — extract exactly the fields defined in a named blueprint
- **`auto`** — classify the document type first, pick the best blueprint, then extract
- **`raw`** — return the VLM's free-text output without parsing
- **`double_check`** — extract twice with different prompts, cross-validate, lower confidence on disagreements
- **`packet`** — for multi-page PDFs: classify each page, group consecutive same-type pages, extract per logical document

The routing field on every response maps to a confidence band: `auto_classified` (85–100), `review_required` (50–84), `escalate_to_operator` (0–49). This lets downstream systems make straight-through / review / escalate decisions without inspecting individual field scores.

## Evaluating on MIDV-2020

Evaluating document recognition honestly requires a public, labeled dataset. Real user documents can't appear in a public repo or demo. The [MIDV-2020](https://smartengines.com/midv-2020/) dataset from Smart Engines solves this: 1 000 synthetic identity documents across 10 types, with VIA 2.x polygon annotations for all text fields.

We picked three types — `esp_id` (Spanish national ID), `grc_passport` (Greek passport), `srb_passport` (Serbian passport) — and ran 60 documents through the GPU endpoint. Results with `mode=blueprint`:

| Field | Accuracy |
|---|---|
| document_number | 100% |
| nationality / sex | 67% |
| surname | 65% |
| date_of_issue | 55% |
| given_names | 53% |
| personal_number | 45% |
| date_of_birth / date_of_expiry | 33% |

The dates look low, but the issue is format normalization: MIDV stores dates as `DD.MM.YYYY`; the model outputs `YYYY-MM-DD`. After normalizing both to `YYYYMMDD` at the metric layer (the right fix — not prompting the model more aggressively, which caused regressions in other fields), dates jumped from 0% to 33%.

Greek-script fields (`grc_passport`) dragged the overall number down to 25% — Qwen2.5-VL-7B produces Latin/MRZ approximations for polytonic Greek characters. That's a known 7B limitation; a larger model resolves it.

## Endpoint *and* Job: Both Halves of Nebius Serverless

"Serverless AI" on Nebius is two products — a **Serverless Endpoint** for live inference
and **Serverless Jobs** for batch work — and this submission uses both. The `/recognize`
API is the Endpoint. The MIDV-2020 evaluation runs as a **Nebius Serverless Job**
(`nebius-job/`) that calls the live endpoint over 60 documents and writes results plus a
summary report to NOS. Crucially, the Job and the Endpoint **share one Docker image and
code path** — there's no second artifact to build, version, or pay for. The same container
serves a single request interactively and grinds through a labeled dataset as a batch Job.

## Performance and Cost on Nebius

60 documents, `mode=blueprint`, H100 SXM:

- **p50 latency:** 1.84 s/doc
- **p95 latency:** 2.34 s/doc
- **Total:** 124 s for 60 documents
- **Cost:** ~$0.001/document ($0.07 for the whole batch)

Nebius Serverless GPU Endpoints are billed per second of active compute with no idle charge. For a document-processing workload that bursts during business hours and idles overnight, this is a significant cost advantage over a reserved GPU instance.

The H100 SXM handles `Qwen2.5-VL-7B-Instruct` comfortably: model loads in under 2 minutes, inference is ~1.5s/doc at full resolution, and the endpoint scales horizontally if needed.

## What We Learned 

**Blueprint prompt engineering is more fragile than it looks.** We tried adding "STRICTLY return YYYY-MM-DD or null" to date instructions and "use Latin script only" for surname. The first caused the model to return null for any date it wasn't certain about (date_of_issue dropped from 55% to 37%). The second caused it to prefer MRZ-format surnames over printed names (surname dropped from 65% to 50%). The lesson: for 7B VLMs, conservative prompts outperform precise ones. More instruction → more constraint →  more ways to go wrong.

**Normalize at the metric layer, not the prompt layer.** Format variation (date formats, separator characters in personal numbers) should be handled in your evaluation code, not by making your prompts more prescriptive. The model already knows dates; it just formats them consistently in its own way.

**Logprobs are a practical confidence signal even at 7B.** The 5.5 pp calibration gap is small but directionally correct — wrong extractions do score lower on average. For a triage routing system (auto-approve high-confidence, queue low-confidence for review), this is good enough.

## Hardening It for Production

A demo that only handles the happy path isn't a serious submission. The service ships with
the cheap, high-value hardening you'd want behind a real endpoint: **structured JSON logging**
(one object per line with `request_id`, mode, routing, and latency), a Prometheus **`/metrics`**
endpoint for Nebius Managed Prometheus to scrape, and a `/health` probe that actively checks
vLLM so the readiness signal is truthful. On the security side: secrets load by reference from
**Nebius Mysterybox** (no plaintext in the deploy spec), a body-size limit returns **HTTP 413**
and a per-request deadline returns **HTTP 504** as DoS guards, and `presigned_url` fetches are
restricted by an **SSRF allowlist**. For reliability, `start.sh` supervises vLLM in a
restart-on-crash loop while uvicorn stays PID 1 so the port — and `/health` — stay up through a
backend blip. The full pillar-by-pillar rationale is in [WELL-ARCHITECTED.md](WELL-ARCHITECTED.md).

## Try It

The full source, blueprints, eval scripts, and smoke tests are on GitHub: [docs-proc-nebius](https://github.com/dsadchikov/docs-proc-nebius).

To run locally in mock mode: `docker compose -f docker-compose.cpu.yml up --build` — no GPU required.

📹 **Video walkthrough:** <!-- TODO: add public 3–10 min video link before submission -->

**See it in action:** the [Proof of Execution](https://github.com/dsadchikov/docs-proc-nebius#proof-of-execution) section in the README links the live endpoint, sample recognition results, and the eval report.

<!-- TODO: capture a real /demo screenshot/GIF before submission (commit with git add -f images/demo-screenshot.png) -->
![Demo UI](images/demo-screenshot.png)

---

*Built for the Nebius Serverless AI Builders Challenge (June 2026). #NebiusServerlessChallenge*
