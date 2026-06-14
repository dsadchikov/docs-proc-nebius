# docs-proc-nebius — Claude context

## What this is

Nebius Serverless AI Builders Challenge submission (deadline **30 June 2026**).  
Standalone document recognition pipeline: nginx + FastAPI + vLLM on H100 SXM.

Public repo: https://github.com/dsadchikov/docs-proc-nebius  
Local working dir (public repo clone): `/Users/ds/projects/docs-proc-nebius/`  
Lity monorepo (private, legacy source): `/Users/ds/lity/`

## Infrastructure

| Resource | Value |
|---|---|
| Project ID (deploy / subnet owner) | `project-e00g5my4en10vmy2fbmhs9` |
| ⚠️ Project ID (был в доке, НЕ владеет subnet) | `project-e00cnd42pr00608v7k0qjz` — деплой с ним виснет в provisioning |
| Registry | `cr.eu-north1.nebius.cloud/e00kh1yd3svet2htq0` |
| Subnet | `vpcsubnet-e00sqjf7njsth9q7n3` |
| NOS bucket | `lity-blueprints` |
| NOS endpoint | `https://storage.eu-north1.nebius.cloud` |
| NOS region | `eu-north1` |
| SA credentials | `~/.nebius/sa-lity-docs-proc-ci.json` |
| Nebius CLI profile | `lity-nebius` (federation, для ручных операций) |

## Build & deploy workflow

```
Mac                                    srv55 (192.168.10.55)
────────────────────────────────────   ──────────────────────────────
1. rsync → /home/ds/docs-proc-nebius/
   scp .secrets.env → /home/ds/docs-proc-nebius/.secrets.env
2. ssh →
                                        3. docker build
                                        4. docker push → Registry
                                        5. source .secrets.env
                                        6. nebius ai endpoint create
                                        7. smoke_test.sh
```

**Правило:** Nebius CLI установлен на srv55. Деплой запускается с srv55.  
`.secrets.env` копируется через scp (gitignored, rsync его исключает).

### Rsync (Mac → srv55)

```bash
rsync -avz \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude '.kiro/' \
  --exclude '.claude/' \
  --exclude '.hypothesis/' \
  --exclude '.pytest_cache/' \
  --exclude '.secrets.env' \
  /Users/ds/projects/docs-proc-nebius/ \
  ds@192.168.10.55:/home/ds/docs-proc-nebius/
```

### Docker build & push (на srv55)

```bash
cd /home/ds/docs-proc-nebius

docker build \
  --platform linux/amd64 \
  -t cr.eu-north1.nebius.cloud/e00kh1yd3svet2htq0/endpoint:<tag> \
  nebius-endpoint/

docker push cr.eu-north1.nebius.cloud/e00kh1yd3svet2htq0/endpoint:<tag>
```

### Deploy endpoint (с srv55)

```bash
# Загрузить секреты
source .secrets.env

nebius ai endpoint create \
  --name lity-doc-recognition \
  --image cr.eu-north1.nebius.cloud/e00kh1yd3svet2htq0/endpoint:<tag> \
  --container-port 8080 \
  --platform gpu-h100-sxm \
  --preset 1gpu-16vcpu-200gb \
  --disk-size 250Gi \
  --shm-size 16Gi \
  --subnet-id vpcsubnet-e00sqjf7njsth9q7n3 \
  --public \
  --auth token \
  --env S3_ACCESS_KEY="$S3_ACCESS_KEY" \
  --env S3_SECRET_KEY="$S3_SECRET_KEY" \
  --env S3_BUCKET="$S3_BUCKET" \
  --env S3_REGION="$S3_REGION" \
  --env S3_ENDPOINT="$S3_ENDPOINT" \
  --parent-id project-e00g5my4en10vmy2fbmhs9   # ДОЛЖЕН совпадать с владельцем --subnet-id
```

**Critical:** `--container-port 8080`, `--subnet-id`, `--shm-size` — обязательны.  
**НЕ передавать `--ssh-key`** — он даёт `rpc NotFound` и роняет операцию create (`code=13`, endpoint виснет в STARTING). Ключ всё равно не прокидывается (SSH в VM недоступен). Убран начиная с v25.  
**`--parent-id` ДОЛЖЕН совпадать с владельцем `--subnet-id`** — иначе endpoint виснет в provisioning без логов.

### После деплоя

```bash
ENDPOINT_ID="<из вывода create>"

# Получить токен и URL
nebius ai endpoint get "$ENDPOINT_ID" --format json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('Token:', d['spec']['auth_token'])
print('URL:  ', d['status'].get('endpoints', [{}])[0].get('url', '?'))
"

# Smoke test
export NEBIUS_ENDPOINT_URL="http://<IP>:8080"
export NEBIUS_ENDPOINT_TOKEN="<token>"
bash nebius-endpoint/smoke_test.sh
```

## Текущий endpoint

| Field | Value |
|---|---|
| ID | `aiendpoint-e00m7v3n0make6t394` |
| Name | `lity-doc-recognition-v25` |
| Image tag | `v25` |
| State | **RUNNING** (2026-06-14) |
| Last known IP | `89.169.102.181:8080` |
| Last smoke | 33/33 PASS (2026-06-14) |
| Project | `project-e00g5my4en10vmy2fbmhs9` (владелец subnet) |

### Архитектура контейнера (с v25 — минимальная)

- **nginx убран.** uvicorn слушает `0.0.0.0:8080` напрямую (Nebius даёт ингресс).
- **start.sh: uvicorn стартует первым** (foreground/`exec`), vLLM — в **фоне** без блокирующего ожидания. Порт 8080 открывается за секунды → readiness Nebius проходит → логи текут. (До v25 nginx стартовал последним, после ожидания vLLM до 900с → порт не открывался → `code=13` + STARTING навсегда + пустые логи.)
- `/v1/*` проксируется в vLLM роутом FastAPI (`app/main.py`, `vllm_passthrough`) — замена старого nginx-роута.
- Dockerfile: `ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` (ниже model-слоя, кэш модели не сбрасывается). vLLM офлайн — модель вшита.
- **Демо/CORS за токен-гейтом (важно):** Nebius `--auth token` ставит впереди свой ингресс-nginx (`nginx/1.31.1`), который требует Bearer на **ВСЕХ** путях. Поэтому `GET /demo` в браузере → **401** (токен в навигации по URL не подставить), а кросс-origin preflight `OPTIONS` → 401 (браузер не шлёт Authorization в preflight). Итог: **браузерный демо за `--auth token` не работает вообще** — ни загрузка страницы, ни fetch. Наш FastAPI-CORS тут бессилен (запрос не доходит). Варианты: (a) отдельный endpoint с `--auth none` для демо; (b) демо только через curl/CLI с токеном.

## Secrets management

**Источник истины: Nebius Mysterybox** `mbsec-e00bkegre2hbv41e4n`

| Место | Назначение | Как получить |
|---|---|---|
| Mysterybox `mbsec-e00bkegre2hbv41e4n` | единый источник, versioned, KMS | `nebius mysterybox payload get --secret-id mbsec-e00bkegre2hbv41e4n` |
| `.secrets.env` (gitignored, chmod 600) | локальная разработка на Mac | `bash scripts/fetch-secrets.sh` |

Ключи: `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_ENDPOINT`, `S3_REGION`, `HF_TOKEN`

**НЕ используем AWS SSM** — это независимый продукт от lity.

**NOS static key:** `accesskey-e00cztea3tty7kvpj3` / `NAKI7RYB9RNHU6BGK8PH` (создан 2026-06-13)

**GitHub Secrets** (`dsadchikov/litypoc`) ✅: `NEBIUS_S3_ACCESS_KEY`, `NEBIUS_S3_SECRET_KEY`, `NEBIUS_S3_BUCKET`, `NEBIUS_PROJECT_ID`, `NEBIUS_REGISTRY`, `NEBIUS_SA_CREDENTIALS`

## srv55 (build VM)

| | |
|---|---|
| SSH | `ds@192.168.10.55` |
| Рабочая директория | `/home/ds/docs-proc-nebius/` |
| Docker registry login | `nebius iam get-access-token \| docker login cr.eu-north1.nebius.cloud/e00kh1yd3svet2htq0 --username iam --password-stdin` |
| Старые директории | `/home/lity-nebius/`, `/home/ds/nebius-endpoint/` — не трогаем |

## Open tasks (contest deadline 30.06.2026)

- [x] Тестовый деплой: v25 RUNNING на `e00g5...`, 33/33 PASS (2026-06-14)
- [ ] 31.4 — MIDV-2020 Recognition_Result samples for submission
- [ ] 28 — README.md
- [ ] 29 — Blog post ≥600 words tagged `#NebiusServerlessChallenge`
- [ ] 39 — Video walkthrough 3–10 min
- [ ] 30 — Submit at Nebius Academy

### Backlog / known issues (после деплоя v25)

- **BUG-1** — `mode=auto` на нераспознанном изображении: lookup идёт по `null` вместо `"default"` (`raw_text: "Blueprint not found: default"` в T23; тест проходит, routing=escalate). Не блокер.
- **Браузерный демо за токен-гейтом НЕ работает** — Nebius `--auth token` ингресс (`nginx/1.31.1`) требует Bearer на всех путях: `GET /demo` → 401 (токен в URL-навигации не подставить), preflight `OPTIONS` → 401. Решение для контеста: отдельный демо-endpoint с `--auth none`, либо демонстрация через curl/CLI. **Решить до видео-walkthrough (задача 39).**
- **`--ssh-key` / SSH в VM** — ключ не прокидывается, SSH в VM endpoint'а недоступен; диагностика только через `nebius ai endpoint logs` / serial console. Флаг убран из деплой-команды.

### Лог инцидента деплоя (2026-06-13/14) — три наслоённых дефекта
1. `--parent-id` не совпадал с владельцем `--subnet-id` → provisioning-hang без логов. Fix: проект `e00g5my4en10vmy2fbmhs9`.
2. `--ssh-key` → `rpc NotFound` → операция create падала `code=13`, endpoint STARTING навсегда. Fix: убрать флаг.
3. Порядок старта: nginx (порт 8080) стартовал после ожидания vLLM до 900с → readiness не проходил. Fix: uvicorn на 8080 первым, vLLM в фоне (v25, nginx убран). Изолировано через min-test образ.

## Key constraints

- **No PII** — all samples must come from MIDV-2020 synthetic dataset
- Model: `Qwen2.5-VL-7B-Instruct` (7B, fits one H100 SXM; 72B требует multi-GPU)
- BUG-1: `mode=auto` на нераспознанном изображении → lookup идёт по null вместо "default"
- GitHub Workflows в `dsadchikov/litypoc` (не в публичном репо)
