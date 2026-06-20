# Deployment Guide

Step-by-step instructions to deploy this service to a fresh Nebius account, from
nothing (no project, no registry, no bucket) to a running, smoke-tested GPU
endpoint, and to tear it all down again afterward.

This guide documents the exact sequence used during live verification on
2026-06-19. The full cycle (login → deploy → smoke test → teardown) takes
**about an hour** end to end — see [Timing](#timing) for the breakdown.

---

## Prerequisites

On the machine you'll run the deploy from:

- **Nebius CLI** installed and able to log in (`nebius profile create`, or an
  existing profile). [Install docs](https://docs.nebius.com/cli/)
- **Docker**, able to build `linux/amd64` images (works natively on Linux x86_64;
  on Mac/ARM it works too, just slower, via QEMU emulation)
- **Python 3** with `boto3` installed (`pip install boto3`) — used for the NOS
  (S3-compatible) blueprint upload step, no AWS CLI required
- **A Nebius account.** Either:
  - an existing **project** you control (`PROJECT_ID`) — every tenant already
    has one auto-created at signup; find yours with
    `nebius iam project list --parent-id <tenant-id>`, or
  - just your **tenant ID** (`TENANT_ID`, find it with `nebius iam tenant list`)
    — the deploy script will create a dedicated project for you

### Disk space

**At least 80 GB free**, ideally 100 GB. Breakdown:

| Component | Size |
|---|---|
| Base image (`vllm/vllm-openai:v0.9.1`, unpacked) | ~25–30 GB |
| Model weights (`Qwen2.5-VL-7B-Instruct`, downloaded at build time) | ~16 GB |
| App layer (apt packages, pip deps, code) | ~1–2 GB |
| **Final image** | **~45–50 GB** |

The build process briefly holds both the pulled base layers and the assembled
final image at once, plus Docker's build cache, so peak usage during the build
runs higher than the final image size — hence the 80 GB+ recommendation, not
just "the image is 50 GB so 50 GB is enough."

---

## Before you start: browser authorization

`nebius profile create` (or re-activating an existing profile whose session has
expired) opens an OAuth flow that needs a browser. If you're deploying from a
**headless** machine (no display — e.g. a remote build VM over SSH), the
printed auth link points at `http://127.0.0.1:<port>` **on that machine**, which
your local browser can't reach directly. Use an SSH local port-forward:

1. On the headless machine, run the command that needs auth (e.g.
   `nebius iam whoami`, or `nebius profile activate <profile>`). It prints:
   ```
   To complete the authentication process, open the following link in your browser:
   https://auth.nebius.com/oauth2/authorize?...&redirect_uri=http%3A%2F%2F127.0.0.1%3A<PORT>&...
   ```
   Note the `<PORT>` in the `redirect_uri` — it's different every time.
2. **From your laptop** (with a browser), open a second terminal and tunnel that
   exact port to the headless machine:
   ```bash
   ssh -L <PORT>:localhost:<PORT> user@headless-host
   ```
   Leave this running; it doesn't need any further input.
3. **On your laptop**, open the full link printed in step 1 in a browser and log
   in. The OAuth redirect to `http://127.0.0.1:<PORT>/...` travels through the
   tunnel back to the waiting CLI process on the headless machine, which
   completes automatically.
4. Verify: `nebius iam whoami --format json` should return a `user_profile`
   (not a `service_account_profile`) if you need tenant-owner rights (see next
   section).

**Why this matters for `TENANT_ID`:** creating a project is a tenant-scoped
write. If your CLI is authenticated as a narrowly-scoped service account (CI
automation, or an invited member with a limited role), `nebius iam project
create` will fail with a permission error even though reads (`project list`)
work fine. Log in as yourself (the tenant owner) for this path, not a service
account.

**Sessions can expire mid-deploy.** The `docker build` step (model weight
download) takes 10-15+ minutes — long enough to outlast a short federated
session in some cases. `scripts/bootstrap.sh` checks login both at the start
and again right before the final endpoint-creation step, and will tell you
clearly if you need to repeat the browser step above before continuing.

---

## Deploy

From a clean clone of this repository:

```bash
git clone https://github.com/dsadchikov/docs-proc-nebius.git
cd docs-proc-nebius
```

**Option A — you already have a project:**
```bash
PROJECT_ID=<your-project-id> ./scripts/bootstrap.sh
```

**Option B — you only have a tenant ID, no project yet:**
```bash
TENANT_ID=<your-tenant-id> ./scripts/bootstrap.sh
```

`scripts/bootstrap.sh` is idempotent for provisioning (safe to re-run; existing
resources are reused by name) and does, in order: find-or-create project (if
using `TENANT_ID`) → find-or-create subnet → find-or-create registry →
find-or-create NOS bucket → find-or-create service account + IAM group + bucket
permit → issue an S3 access key → `docker build` + push → upload blueprints +
catalog → create the GPU endpoint.

If no subnet exists yet in a brand-new project, the script creates a default
network and **exits** (subnet creation is asynchronous) — just re-run the same
command once `nebius vpc subnet list --parent-id <PROJECT_ID>` shows a subnet.

On success, the script prints a summary block with everything you need:

```
════════════════════════════════════════════════════════════════
[bootstrap] SUMMARY — everything needed to use or clean up this deploy
════════════════════════════════════════════════════════════════
  ENDPOINT_ID    = aiendpoint-...
  ENDPOINT_STATE = RUNNING
  PUBLIC_URL     = http://<ip>:8080
  AUTH_TOKEN     = ...
  PROJECT_ID     = ...
  REGISTRY_ID    = ...
  BUCKET         = ...
  BUCKET_ID      = ...
  SA_ID          = ...
  GROUP_ID       = ...
  ACCESS_KEY_ID  = ...
  S3_ACCESS_KEY  = ...
  S3_SECRET_KEY  = ...
  S3_ENDPOINT    = ...
  S3_REGION      = ...
════════════════════════════════════════════════════════════════
```

Treat this block as sensitive — it contains live credentials (`AUTH_TOKEN`,
`S3_SECRET_KEY`). Don't paste it into a public channel.

If `nebius ai endpoint create` reports a local error, the script doesn't give
up immediately — it looks the endpoint up by name afterward, since the
request can succeed server-side even when the CLI's own wait/output parsing
fails client-side (observed live: long synchronous waits can interleave
progress text with JSON on stdout, and login sessions can expire mid-wait).

### Smoke test

Copy the export lines from the summary block above, then:

```bash
bash nebius-endpoint/smoke_test.sh
```

Expect `35 passed  0 failed`. If the endpoint wasn't `RUNNING` yet when
`bootstrap.sh` finished, poll with the `nebius ai endpoint get <ENDPOINT_ID>
--format json` command it prints (image pull + vLLM weight load can take a
few minutes) and re-check `status.state`.

---

## Tear down

`scripts/cleanup-bootstrap.sh` removes everything a `bootstrap.sh` run
created. It resolves every resource by name itself — you only need to know
`PROJECT_ID` and `NAME_PREFIX` (default `docs-proc`; pass whatever you used,
if anything):

```bash
PROJECT_ID=<project-id> \
NAME_PREFIX=<the NAME_PREFIX you used> \
DELETE_PROJECT=1 \
bash scripts/cleanup-bootstrap.sh
```

Deletion order (endpoint first to stop GPU billing immediately, project
last): endpoint → bucket contents + bucket → every access key issued for the
service account (re-running `bootstrap.sh` issues a new one each time, so
there may be more than one) → service account → IAM group → every image in
the registry + the registry itself (`registry delete` fails if any image is
left) → project (best-effort).

As of Nebius CLI v0.12.223, `nebius iam project` has **no `delete`
subcommand at all**. If `DELETE_PROJECT=1` can't remove the project on your
CLI version either, delete it via [console.nebius.com](https://console.nebius.com)
instead, or just leave it — an empty project with nothing inside it isn't
billed.

---

## Timing

Live-measured end to end on 2026-06-19, srv55 (Linux x86_64, native Docker
build, no QEMU emulation):

| Phase | Typical duration |
|---|---|
| Browser login / re-auth (manual step) | a few minutes, depends on how fast you click the link |
| Project, subnet, registry, bucket, IAM, access key (steps 0–5) | well under a minute combined |
| `docker build` + push (base image pull + model weight download) | ~10–15 minutes |
| Blueprint upload | a few seconds |
| Endpoint create + wait for `RUNNING` (cold pull of the ~45–50 GB image + vLLM weight load on the GPU VM) | ~15–30 minutes |
| Smoke test (35 tests) | under a minute |
| Teardown | under a minute |

**Total observed full cycle (deploy + smoke test + teardown): about an
hour**, including the manual browser-login steps and normal back-and-forth
of a first-time run. A subsequent run with a warm Docker build cache and an
already-authenticated session is significantly faster — the `docker build`
step in particular drops to well under a minute once layers are cached.
