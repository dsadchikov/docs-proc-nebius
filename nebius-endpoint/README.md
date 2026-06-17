# nebius-endpoint

The serverless document-recognition service image: **FastAPI/uvicorn (`:8080`, PID 1) + vLLM
serving `Qwen2.5-VL-7B-Instruct`**, with Nebius Object Storage (NOS) for blueprints, inbound
uploads, and results.

This directory is the build context for the endpoint container (`Dockerfile`), the app code
(`app/`), the built-in blueprints (`blueprints/`), the test suite (`tests/`), and the 35-test
end-to-end smoke suite (`smoke_test.sh`).

**Full documentation lives in the root [`../README.md`](../README.md)** — architecture, deploy to a
Nebius GPU endpoint, API reference, blueprint format, environment variables, and the MIDV-2020
evaluation. See also [`../WELL-ARCHITECTED.md`](../WELL-ARCHITECTED.md) for the pillar-by-pillar
design review.

## Local development (CPU / mock mode)

No GPU needed — `MOCK_VLLM=1` returns deterministic fixtures:

```bash
cp ../.env.example .env          # set AUTH_TOKEN; optionally S3_* for NOS features
docker compose -f docker-compose.cpu.yml up --build
# app on http://localhost:8080

# tests
pip install -r requirements.txt pytest hypothesis httpx
pytest tests/ -q
```
