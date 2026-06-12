# Implementation Plan: docs-proc-nebius — Nebius Document Recognition Pipeline

## Overview

**Архитектура:** standalone-first. Endpoint работает без зависимостей от AWS. Два режима доставки документа: `base64` (inline) и `nebius_object` (через NOS). lity-интеграция — опциональный клиент.

**Workstreams:**
1. `nebius-endpoint` — FastAPI + vLLM контейнер (основной продукт)
2. `nebius-job` — batch контейнер
3. `lity-backend` — опциональная интеграция (после основного продукта)

---

## Сопоставление: реализовано vs. спека

### ✅ Полностью реализовано

| Компонент | Что сделано |
|---|---|
| `app/router.py` | `clamp_confidence` + `get_routing`, без тяжёлых зависимостей |
| `app/extractor.py` | Все режимы: `raw`, `blueprint`, `auto`, `double_check`; clamping через router |
| `app/main.py` | `/recognize`, `/health`, `/blueprints` CRUD, `/blueprints/reload` |
| `app/blueprint_loader.py` | BlueprintStore с S3 CRUD, in-memory cache |
| `app/models.py` | Pydantic модели для всех запросов/ответов |
| `app/auth.py` | Bearer token verify (AUTH_TOKEN) |
| `app/config.py` | Все env vars |
| `app/pdf_converter.py` | PDF → JPEG через poppler |
| `app/mock_vllm.py` | CPU-мок для тестирования |
| `tests/test_router.py` | P1, P2 — Hypothesis PBT, 2/2 тестов проходят |
| `tests/conftest.py` | Fixtures переписаны под новую архитектуру |
| `Dockerfile`, `Dockerfile.base`, `Dockerfile.cpu` | Двухслойная архитектура |
| `docker-compose.cpu.yml` | CPU тест стек |
| `start.sh`, `nginx.conf` | Трёхпроцессный контейнер |
| `nebius-job/job.py` | Batch job — manifest, retry, exponential backoff, summary |
| `nebius-job/Dockerfile` | Контейнер job |
| `lity-backend/nebius-adapter.js` | SSM cache, presigned URL, fetch /recognize, DynamoDB write |
| `lity-backend/documents.js` | `handleRecognizeDocument` + route dispatch |
| `.github/workflows/nebius-build-push.yml` | CI/CD build + push |
| `.github/workflows/nebius-deploy-endpoint.yml` | Deploy endpoint |
| `.github/workflows/nebius-run-job.yml` | Run batch job |
| Nebius инфраструктура | Аккаунт, SA, registry, endpoint v17 задеплоен |

### ❌ Не реализовано (новые требования из спеки)

| Что нужно | Requirement |
|---|---|
| Rich Blueprint Format (`sections` + `inferenceType`) в файлах | Req 5.4 |
| `BlueprintStore._normalize()` — flatten sections → fields[] | Req 6.3 |
| `_catalog.json` — загрузка через каталог, не glob | Req 6.2 |
| Blueprint файлы в NOS-формате: `default/v1.json`, `passport/v1.json`, `residence_permit_ltu_front/v1.json` | Req 4.1 |
| `GET /inbound/presign` — NOS presigned PUT URL для клиентов | Req 1.2 |
| `document.type: "nebius_object"` в Endpoint (fetch из NOS) | Req 9.1 |
| NOS write: `outbound/YYYY/MM/DD/HH/mm/<id>.json` после recognition | Req 9.4 |
| NOS write: `logs/YYYY/MM/DD/HH/mm/req_<id>.json` после recognition | Req 12.2 |
| `POST /blueprints/generate` — генерация blueprint по образцу | Req 10.1 |
| Тесты P3, P4, P5 (`test_extractor.py`) | Design P3–P5 |
| Тесты P6, P7 (`nebius-job/tests/test_job.py`) | Design P6–P7 |
| Тесты P8, P9, P10 (`nebius-adapter.test.js`) | Design P8–P10 |
| Тест `test_endpoint.py` — HTTP API примеры | Design P11 |
| `nebius-adapter.js` NOS upload (вместо presigned URL) + fallback | Req 7.1 |
| SSM параметры для NOS в `nebius-adapter.js` | Req 7.4 |

### ⚠️ Частично реализовано / расхождения

| Компонент | Проблема |
|---|---|
| `blueprint_loader.py` | Загружает flat JSON (`blueprints/*.json`), не Rich Format + `_catalog.json` |
| `job.py` | Использует `presigned_url` как document type, не `nebius_object` |
| `nebius-adapter.js` | Использует AWS presigned GET URL, не NOS upload |
| Blueprint файлы | `app/blueprints/` директория не существует — blueprint-ов нет вообще |
| `documents-recognize.test.js` | Не существует |

---

## Tasks

### Workstream 0: Инфраструктура ✅ DONE

- [x] 0.1 Nebius аккаунт, проект, квоты
- [x] 0.2 Nebius CLI + Docker credentials helper
- [x] 0.3 Service Account для CI/CD
- [x] 0.4 Container Registry
- [x] 0.5 GitHub Secrets (базовые)
  - [ ] 0.5a Добавить NOS secrets: `NEBIUS_S3_ACCESS_KEY`, `NEBIUS_S3_SECRET_KEY`, `NEBIUS_S3_BUCKET`
    - _Requirements: 11.2, 5.5_

- [x] 1. Scaffold: директории, .gitignore, LICENSE

---

### Workstream 1: Nebius Endpoint

#### 1A. Blueprints — Rich Format (новое)

- [ ] 2. Создать blueprint файлы в Rich Format
  - [x] 2.1 Создать `nebius-endpoint/blueprints/default/v1.json`
    - Catch-all blueprint: все поля optional, секции DOCUMENT_METADATA + PERSONAL_INFO + DOCUMENT_DETAILS
    - Extraction prompt: "Extract ALL visible structured information from this document"
    - _Requirements: 4.2, 5.4_

  - [x] 2.2 Создать `nebius-endpoint/blueprints/passport/v1.json`
    - Rich Format с секциями: DOCUMENT_METADATA (document_type, document_number, issuing_country), PERSONAL_INFO (surname, given_names, sex, nationality, date_of_birth), DOCUMENT_DETAILS (date_of_expiry, date_of_issue, mrz_line_1, mrz_line_2)
    - inferenceType: explicit для verbatim полей, inferred для дат (YYYY-MM-DD) и sex (M/F)
    - Основа: `ltu-prp-scheme-front.json` паттерн
    - _Requirements: 4.3, 5.4_

  - [x] 2.3 Создать `nebius-endpoint/blueprints/residence_permit_ltu_front/v1.json`
    - Взять поля напрямую из `specs-actual/ideas/doc-management/doc-recognition/ltu-prp-scheme-front.json`
    - Секции: DOCUMENT_METADATA, PERSONAL_INFO, PERMIT_DETAILS, ADDITIONAL_INFO
    - _Requirements: 4.4, 5.4_

  - [x] 2.4 Создать `nebius-endpoint/blueprints/_catalog.json`
    - Перечислить все три blueprint с status: "active", latest_version: 1, path
    - _Requirements: 6.2_

- [ ] 3. Обновить `BlueprintStore` для Rich Format
  - [x] 3.1 Добавить `_normalize(rich: dict) -> dict` — flatten sections → fields[]
    - Для каждого поля в sections извлечь: name, instruction, inferenceType, required, _section
    - Результат кладём в `normalized["fields"]` — совместимо с текущим Extractor
    - _Requirements: 6.3_

  - [x] 3.2 Обновить `_load_all()` — читать через `_catalog.json`
    - Сначала пытаемся прочитать `blueprints/_catalog.json`
    - Загружаем только `status: "active"` entries по `path`
    - Fallback (если каталог отсутствует): текущий glob по `blueprints/*/` для высшей версии
    - **Добавлено:** `_load_from_local()` — загрузка из файловой системы когда нет S3 credentials
    - _Requirements: 6.2_

  - [x] 3.3 Обновить `create()` / `update()` — писать в `blueprints/<id>/vN.json`
    - При создании: `blueprints/<id>/v1.json`
    - При update: вычислить новый N = latest_version + 1, писать `vN.json`, старая версия остаётся
    - После каждой операции обновить `_catalog.json`
    - DELETE: не удалять файл, только менять status → "deprecated" в `_catalog.json`
    - _Requirements: 6.1_

- [x] 4. Реализовать `GET /inbound/presign` (новое)
  - Принимает query param `filename`
  - Возвращает `{ presigned_put_url, nos_key, expires_in: 300 }`
  - `nos_key = inbound/YYYY/MM/DD/HH/mm/<uuid>.<ext>`
  - Генерирует NOS presigned PUT URL через boto3 `generate_presigned_url("put_object")`
  - Если NOS не настроен → 503
  - Добавить в `nginx.conf` location `/inbound/presign → FastAPI`
  - _Requirements: 1.2_

- [x] 5. Реализовать `document.type: "nebius_object"` в Extractor
  - В `preprocess_document()`: для type `"nebius_object"` — `s3_client.get_object(Bucket, Key=value)` → bytes
  - Если NOS не настроен → raise → HTTP 503
  - _Requirements: 9.1_

- [ ] 6. NOS write: outbound + logs (best-effort, non-blocking)
  - [x] 6.1 После каждого `/recognize` запроса — писать `outbound/YYYY/MM/DD/HH/mm/<request_id>.json`
    - Генерировать `request_id = uuid4()`
    - Добавить `request_id` в RecognizeResponse
    - Запись через `asyncio.create_task()` — не блокирует HTTP ответ
    - _Requirements: 9.4_

  - [ ] 6.2 Писать `logs/YYYY/MM/DD/HH/mm/req_<request_id>.json`
    - Содержимое: `request_id`, `timestamp`, `blueprint_id`, `mode`, `document_confidence`, `routing`, `latency_ms`, `model`, `endpoint_version`
    - _Requirements: 12.2_

- [x] 7. `POST /blueprints/generate` — генерация blueprint по образцу (новое)
  - Pass 1: `mode=raw` VLM inference → free-text описание полей документа
  - Pass 2: VLM промпт → сгенерировать Rich Blueprint JSON (sections + inferenceType + instruction)
  - Сохранить как `blueprints/<id>/v1.json` с `status: "draft"`, обновить `_catalog.json`
  - Вернуть HTTP 201 с сгенерированным blueprint
  - Blueprint НЕ загружается в активный кэш пока не `PUT /blueprints/{id}` с `status: "active"`
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

#### 1B. Уже реализовано, проверить/починить

- [x] 8. `app/router.py` — clamp_confidence + get_routing ✅
- [x] 9. `app/extractor.py` — все режимы ✅
- [x] 10. `app/main.py` — /recognize, /health, /blueprints CRUD ✅
  - [ ] 10.1 Обновить `main.py` — добавить `/inbound/presign`, `/blueprints/generate`
    - _Requirements: 1.2, 10.1_
- [x] 11. `tests/test_router.py` — P1, P2 ✅ (2/2 pass)
- [x] 12. `tests/conftest.py` ✅

#### 1C. Тесты (часть опциональные)

- [x] 13. Написать `tests/test_extractor.py` — P3, P4, P5
  - **P3:** Required fields never omitted — Hypothesis + mock VLM
  - **P4:** Confidence scores — integers in [0, 100]
  - **P5:** Fields schema-valid against blueprint
  - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**

- [x] 14. Написать `tests/test_endpoint.py` — HTTP API примеры
  - 422 на unknown blueprint_id, 422 на missing blueprint_id
  - 503 на nebius_object без NOS credentials
  - Health response shape (P10)
  - **Validates: Requirements 1.1, 1.2, 1.10, 12.4**

- [x] 15. Checkpoint Endpoint
  - `python -m pytest nebius-endpoint/tests/ --tb=short` — все тесты зелёные
  - Синхронизировать на srv55: `rsync -avz --exclude='.git' --exclude='__pycache__' /Users/ds/lity/nebius-endpoint/ srv55:/home/lity-nebius/nebius-endpoint/`
  - Пересобрать CPU образ: `docker compose -f docker-compose.cpu.yml up --build -d`
  - Smoke test: `curl -H "Authorization: Bearer test-token-123" http://192.168.10.55:8080/health`

---

### Workstream 2: Nebius Inference Job

- [x] 16. `nebius-job/job.py` ✅ — manifest, retry, exponential backoff, summary
  - [x] 16.1 Обновить `job.py` — использовать `document.type: "nebius_object"` вместо `presigned_url`
    - Manifest формат: `{ document_id, blueprint_id, nos_key, mime_type }` (вместо presigned_url)
    - `ENDPOINT_TOKEN` env var — добавить `Authorization: Bearer` header
    - Добавить `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` env vars
    - _Requirements: 8.4_

  - [ ] 16.2 Job summary → писать в `logs/YYYY/MM/DD/HH/mm/job_<job_id>.json` в NOS
    - _Requirements: 12.3_

- [x] 17. `nebius-job/Dockerfile` ✅

- [x] 18. Написать `nebius-job/tests/test_job.py` — P6, P7
  - **P6:** Exactly N output files for N manifest items
  - **P7:** Exit 0 regardless of individual failures
  - **Validates: Requirements 8.3, 8.4, 8.5, 8.6, 8.7**

- [x] 19. Checkpoint Job
  - `python -m pytest nebius-job/tests/ --tb=short`

---

### Workstream 3: lity-backend (опциональная интеграция)

- [x] 20. `lity-backend/nebius-adapter.js` ✅ (базовая реализация с presigned URL)
  - [ ] 20.1 Обновить: NOS upload перед `/recognize`
    - Шаг 1: `s3.GetObject` из `lity-poc-docs` → document bytes
    - Шаг 2: Генерировать NOS presigned PUT URL через `@aws-sdk/client-s3` (endpoint: NOS)
    - Шаг 3: PUT bytes в NOS → `inbound/YYYY/MM/DD/HH/mm/<documentId>.<ext>`
    - Шаг 4: POST `/recognize` с `document.type: "nebius_object"`
    - Fallback при ошибке NOS: POST с `document.type: "base64"` (encode document bytes)
    - _Requirements: 7.1_

  - [ ] 20.2 Добавить SSM параметры для NOS
    - `/lity/nebius-s3-access-key` (SecureString)
    - `/lity/nebius-s3-secret-key` (SecureString)
    - `/lity/nebius-s3-bucket` (String)
    - _Requirements: 7.4_

- [x] 21. `lity-backend/documents.js` — `handleRecognizeDocument` ✅

- [ ] 22. Тесты lity-backend
  - [ ] 22.1 `__tests__/nebius-adapter.test.js` — P8, P9, P10
    - **P8:** DDB written iff HTTP 200 (fast-check)
    - **P9:** Handler never throws (fast-check)
    - **P10:** Log entries contain required fields (Jest)
    - **Validates: Requirements 7.2, 7.3**

  - [ ] 22.2 `__tests__/documents-recognize.test.js`
    - Missing blueprintId → 400; case not found → 404; adapter error → 502
    - _Requirements: 7.1_

- [ ] 23. Checkpoint lity-backend
  - `npm test --runInBand` в `lity-backend/`

---

### Workstream 4: NOS Setup и Upload blueprints

- [x] 24. Создать NOS бакет и static key
  - `nebius object-storage bucket create --name your-nos-bucket --parent-id $PROJECT_ID`
  - `nebius iam service-account static-key create --service-account-id <SA_ID>`
  - Сохранить key_id + secret → GitHub Secrets + AWS SSM
  - _Requirements: 5.5, 11.1_

- [ ] 25. Загрузить blueprints в NOS
  - Предварительно: задача 2 должна быть выполнена (Rich Format blueprints созданы)
  - Загрузить `_catalog.json` и три blueprint файла по пути `blueprints/*/v1.json`
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

### Workstream 5: Финальный деплой и contest artifacts

- [x] 26. Пересобрать и задеплоить GPU endpoint (v18; актуальный — v21)
  - rsync → srv55 → docker build → push → `nebius ai endpoint create`
  - Передать `--env S3_ACCESS_KEY=... --env S3_SECRET_KEY=... --env S3_BUCKET=your-nos-bucket`
  - Обновить SSM: `/lity/nebius-endpoint-url`, `/lity/nebius-endpoint-token`
  - _Requirements: 1.9, 5.5_

- [x] 27. Smoke tests на GPU endpoint
  - ✅ `smoke_test.sh` — 33/33 тестов прошли на **v22** (2026-06-12): health/auth, vLLM direct, validation, blueprints CRUD, NOS presign, GPU inference все режимы, confidence modes, logprobs работают (confidence_source: "logprobs")
  - ⚠️ Recognition_Result samples для сабмишна пересоздать на MIDV-2020 (текущие — с реальных документов, PII — задача 31)
  - _Requirements: 1.1–1.5, 4.3, 4.4, 11.6_

---

### Workstream 6: Contest v2 — выигрышные доработки

#### 6A. P0 — Compliance ✅

- [x] 31. PII-чистка и публичный датасет MIDV-2020
  - [x] 31.1 Реальные документы удалены, `.gitignore` обновлён
  - [x] 31.2 `prepare_midv2020.sh` + `build_midv_manifest.py` — 60 документов (esp_id, grc_passport, srb_passport) загружены в NOS `eval/midv2020/`
  - [x] 31.3 Blueprint `id_card` создан, загружен в NOS, `_catalog.json` обновлён
  - [ ] 31.4 Recognition_Result samples пересоздать на MIDV-2020 — нужны скриншоты с реальных документов из датасета

- [x] 32. Санитизация публичного репозитория
  - [x] 32.1 `scripts/export_public.sh` — rsync whitelist + placeholder substitution + secret scan gate
  - [x] 32.2 `smoke_test.sh` — все секреты заменены на `${NEBIUS_ENDPOINT_URL:?}` и т.д.
  - [ ] 32.3 Создать публичный repo `docs-proc-nebius`: fresh git history, MIT LICENSE, `.env.example`

#### 6B. P0 — Реальный confidence ✅

- [x] 33. Logprob-based confidence — реализован, работает на GPU (smoke T19: confidence_source="logprobs")

#### 6C. P1 — Guided JSON + Evaluation Job ✅

- [x] 34. Guided JSON decoding — `blueprint_to_guided_schema`, retry fallback, тест P12
- [x] 35. Evaluation Job (MIDV-2020)
  - [x] 35.1–35.4 `job.py` eval mode, `eval_metrics.py`, summary report, тест P14
  - [x] 35.5 Eval прогон на GPU v22: 60 docs, job_id=20260612_214444_acc95d
    - document_number 100%, srb_passport 77%, esp_id 68%, grc_passport 25%
    - Latency p50=1.84s p95=2.34s, Cost=$0.001/doc, Calibration gap=5.5pp

#### 6D. P2 — Packet splitting + Demo UI ✅

- [x] 36. `mode="packet"` — pdf_converter, extract_packet, PacketResponse, тест P13
- [x] 37. Demo UI — `app/static/demo.html`, `/demo` + `/static` в FastAPI и nginx

#### 6E. Финал

- [x] 38. Деплой v22 — `<YOUR-ENDPOINT-ID>`, IP `<YOUR-ENDPOINT-IP>:8080`, 33/33 smoke pass (2026-06-12)
- [ ] 39. Видео walkthrough (3–10 мин)
- [ ] 28. README.md ← **в работе**
- [ ] 29. Технический blog post (≥600 слов, `#NebiusServerlessChallenge`)
- [ ] 30. Contest submission (до 30.06.2026)

---

## Приоритеты (что делать в первую очередь)

Workstreams 0–5 в основном закрыты (v21 задеплоен, 33/33 smoke). Контест-приоритет — Workstream 6, дедлайн 30 июня 2026:

```
1. Задача 33 (logprob confidence)   ← ядро продукта, влияет на все режимы и eval
2. Задача 34 (guided_json)          ← делается вместе с 33 (один проход по extractor.py)
3. Задача 31 (MIDV-2020 + PII)      ← параллельно с 33/34; блокирует 35, 37.2, samples
4. Задача 35 (Evaluation Job)       ← после 31+33; даёт метрики для README/blog
5. Задача 36 (packet mode)          ← независимая, после 33/34
6. Задача 37 (Demo UI)              ← после 31 (нужны сэмплы)
7. Задача 32 (публичный repo)       ← после стабилизации кода
8. Задачи 38, 28, 29, 39, 30        ← деплой → README → blog → видео → submission
```

---

## Баги / Беклог

| # | Severity | Описание | Где | Выявлено |
|---|---|---|---|---|
| BUG-1 | Medium | `mode=auto` на пустом/нераспознанном изображении: classification возвращает `document_type="default", blueprint_id=null, confidence=0` → `extract_auto` логирует `"Blueprint not found: default"` хотя blueprint default существует. Вероятная причина: lookup идёт по `blueprint_id` из classification (null), а не по `document_type`. Routing правильно ставится `escalate_to_operator`, но `raw_text` содержит ошибочное сообщение вместо null. | `extractor.py` → `extract_auto()` | smoke_test T23, v22, 2026-06-12 |

---

## Notes

- Задачи 13, 14, 18, 22 — опциональные тесты, можно пропустить для быстрого MVP
- Задача 7 (`/blueprints/generate`) — опциональная feature
- Workstream 3 (lity-backend) — низкий приоритет, делается после standalone готовности
- lity-backend использует CommonJS (`require`/`module.exports`), не ESM
- Сборка Docker только на Linux (srv55), не на Mac
- После каждого `nebius ai endpoint create` — обновить SSM параметры url + token

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["33.1", "33.2", "31.1", "31.2", "31.3"], "note": "logprobs core + датасет — параллельно" },
    { "id": 1, "tasks": ["33.3", "33.4", "34.1", "34.2"] },
    { "id": 2, "tasks": ["33.5", "34.3", "34.4", "35.1", "35.2"] },
    { "id": 3, "tasks": ["35.3", "35.4", "36.1", "36.2", "37.1"] },
    { "id": 4, "tasks": ["36.3", "36.4", "37.2", "37.3", "31.4"] },
    { "id": 5, "tasks": ["38"], "note": "деплой v22 со всеми доработками" },
    { "id": 6, "tasks": ["35.5", "27-rerun", "32.1", "32.2"], "note": "eval на GPU + санитизация" },
    { "id": 7, "tasks": ["32.3", "28"], "note": "публичный repo + README" },
    { "id": 8, "tasks": ["29", "39"], "note": "blog + видео" },
    { "id": 9, "tasks": ["30"], "note": "submission до 30 июня" }
  ]
}
```

