# SPEC-0013: Deployment

Status: reviewed
Requirements: R-0017
Design: [design.md](../design/design.md)

## Goal
Run the service in the cloud as a permanent demo URL, for the operator and the
people they give the API key to, for a few dollars a month or less. It is built
entirely by Terraform across two providers (AWS for compute and Redis Cloud for
state), deployed from GitHub Actions without stored cloud keys, and protected by
an API key and budget alarms. It depends on SPEC-0014 for Redis connections that
drop or go stale.

## Decisions
- **AWS Lambda (container image) + Function URL.** It scales to zero, so a
  permanent demo URL costs about $0-1 a month (inside Lambda's free tier at demo
  traffic, plus ECR storage). It runs the existing container unchanged through the
  AWS Lambda Web Adapter (uvicorn as usual). A function can run up to 900 s, so
  slow model runs fit; a Function URL avoids API Gateway's 29 s limit. arm64 for
  lower cost.
- **Redis Cloud free database (30 MB) for state.** The checkpointer needs Redis's
  JSON and Search capabilities, which AWS ElastiCache does not reliably provide.
  Redis Cloud has a Terraform provider, so one `terraform apply` builds both the
  AWS side and the database. The free plan has no TLS and deletes a database after
  14 days with no commands; both are accepted for a demo (see Redis Cloud free plan
  below).
- **Hosted model in the cloud.** Ollama needs a GPU, which serverless doesn't have.
  The cloud default is `gemini-3.5-flash-lite`; any model in the SPEC-0012 price
  table can be chosen in settings (an unpriced one fails startup, as
  `DAILY_COST_LIMIT_USD` is required in the cloud).
- **Injection screen on in the cloud.** `INJECTION_SCREEN=chat` with
  `INJECTION_SCREEN_MODEL=gemini-3.5-flash-lite` and
  `INJECTION_SCREEN_ON_ERROR=withhold` (SPEC-0012 B-0005, B-0006). The
  `ollama-systemone` screen needs Ollama, so it cannot run there. Each tool result
  costs one extra model call, counted against the daily limit.
- **Key holders are one trusted group.** One shared API key; everyone holding it is
  trusted like the operator. The key is changed to cut someone off. Per-user
  identity waits for R-0018.
- **Secrets in SSM Parameter Store** (SecureString, free), read at cold start through
  the AWS Parameters and Secrets Lambda Extension over localhost HTTP, so the app
  needs no AWS SDK.
- **GitHub Actions with OIDC.** Workflows assume an AWS role with short-lived
  credentials; no AWS keys are stored in GitHub.
- **Tracing off in the cloud.** Langfuse in the cloud is a non-goal, so no trace
  content leaves the function. Logs go to CloudWatch and hold no message content
  (SPEC-0008).

### Alternatives considered
| Option | Idle cost | Why not chosen |
|--------|-----------|----------------|
| GCP Cloud Run + Redis Cloud | ~$0 | Equally cheap and simpler (no adapter, 60 min timeout). AWS chosen for recognition and its Budgets/OIDC story; Cloud Run is the fallback. |
| Azure Container Apps + Redis Cloud | ~$0 | Also scales to zero; less common in AI portfolios. |
| AWS EC2 + docker compose | ~$12/mo | Same as local, but billed while idle, and Redis must be run and backed up by us. |
| AWS ECS Fargate + ALB | ~$30+/mo | The most production-shaped, but the ALB alone costs more than the whole Lambda design; the documented scale-up path. |
| Lambda behind API Gateway | ~$0 | 29 s integration timeout is too short for model runs. |
| ElastiCache (Valkey) | ~$6+/mo | No reliable JSON + Search support for the checkpointer. |
| Upstash Redis | ~$0 | No Search module. |
| Redis Cloud paid Essentials (250 MB) | ~$5/mo | TLS, backups and no idle deletion, but not needed while the demo holds no sensitive data; the upgrade path. |
| Secrets Manager | $0.40/secret/mo | Rotation features we don't need. |
| Env vars set by Terraform | $0 | Secrets would sit in Lambda config and Terraform state in plain view. |

## Inputs / Outputs
### Architecture
```
GitHub Actions ──OIDC──▶ AWS (deploy role) ──▶ ECR image, Lambda, SSM, Budgets
            └── Terraform ──▶ Redis Cloud (subscription + database)
Client ──HTTPS──▶ Lambda Function URL ──▶ Web Adapter ──▶ uvicorn/FastAPI
                                             ├──▶ Redis Cloud (same AWS region)
                                             ├──▶ LLM provider, Tavily
                                             └──▶ SSM via the extension (cold start)
```

### Repository layout
```
infra/
  bootstrap/      once, by hand: S3 state bucket (native lockfile), GitHub OIDC
                  provider, deploy role and read-only plan role
  app/            ECR, Lambda + Function URL, IAM role, log group, SSM parameters,
                  AWS Budgets + SNS email, Redis Cloud subscription + database
  app/tests/      `terraform test` files using mock providers
.github/workflows/
  infra-ci.yml    on PRs touching infra/: fmt, validate, tflint, trivy config, terraform test
  deploy.yml      manual and on `v*` tags: build arm64 image → ECR → terraform apply → smoke test
```
Provider versions are pinned in `versions.tf` and `.terraform.lock.hcl` is committed.

### Lambda settings (Terraform)
- Image from ECR, arm64, 1024 MB, reserved concurrency 5 (a cost cap). With the
  pool in SPEC-0014 B-0001 capped at `REDIS_MAX_CONNECTIONS=5`, the stack opens at
  most 5 × 5 = 25 Redis connections, under the free plan's limit of 30.
- Timeout = `RUN_TIMEOUT_SECONDS` + 30, at most 900. Log group retention 14 days.
- Function URL with auth type NONE; the app checks the API key (B-0001).
- Environment: `TRACING_ENABLED=false`, the injection screen settings above,
  `DAILY_COST_LIMIT_USD`, `NOTES_BACKEND=redis`, `SSM_PARAMETER_PATH`.

### Redis Cloud free plan
- The database is created in the same AWS region as the Lambda, so its traffic
  stays inside AWS's network.
- Terraform generates a long random password and writes `REDIS_URL` to SSM.
- No TLS: `REDIS_URL` is `redis://`. The code keeps `rediss://` support
  (SPEC-0014 B-0001) for a plan that offers it. Accepted for demo data only; key
  holders are told not to put anything sensitive in a thread. Real data, or
  users outside the trusted group, means moving to the paid plan with `rediss://`.
- The free database is deleted after 14 days with no commands. Keeping it in use is
  the operator's job, outside this repository.
- The free plan allows 30 connections and 100 operations a second; one free
  database per account.

### App settings added
| Setting | Default | Meaning |
|---------|---------|---------|
| `API_KEY` | none | When set, `/v1/*` requires `X-API-Key` (B-0001) and unknown thread ids are refused (B-0003). Required in the cloud. |
| `SSM_PARAMETER_PATH` | none | e.g. `/harnessd/demo/`; when set, secrets are loaded from SSM at startup. |
| `NOTES_BACKEND` | `file` | `file` (SPEC-0003) or `redis` (a capped Redis list; used on Lambda, whose disk is temporary). |

### Secrets (SSM, under `SSM_PARAMETER_PATH`)
The chosen provider's key (`GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`), `TAVILY_API_KEY`, `API_KEY` and `REDIS_URL`. Terraform creates the
parameter names. Values are set outside Terraform (`aws ssm put-parameter`),
except `REDIS_URL`, which Terraform writes from the database it creates. The state
bucket is encrypted and readable only by the deploy roles.

### Budget alarms
- AWS Budgets: monthly cost budget (default $5) with email alerts at 50%, 80% and
  100% actual and 100% forecast, via SNS. The alert email is a Terraform variable
  supplied from a GitHub secret, never committed.
- Model spend: SPEC-0012 `DAILY_COST_LIMIT_USD` is required in the cloud config
  (default $1), plus the provider console's own spend limit (documented, manual).
- Redis Cloud free cannot bill. The Lambda's reserved concurrency caps the number
  of parallel runs.

## Behavior
### Access
- B-0001: With `API_KEY` set, every `/v1/*` request without a matching `X-API-Key`
  (compared in constant time) gets 401 `{"error": {"code": "unauthorized", ...}}`
  and makes no Redis or LLM call. `/health`, `/docs`, `/redoc` and `/openapi.json`
  stay open. OpenAPI declares the header as a security scheme.
- B-0002: With `API_KEY` unset (local), behavior is unchanged.
- B-0003: With `API_KEY` set, a run request whose `thread_id` is unknown or expired
  gets 404 `{"error": {"code": "thread_not_found", ...}}` instead of starting a new
  thread (an exception to SPEC-0004 B-0002), and makes no LLM call. A new thread
  is started with `thread_id: null`, and the server makes its id (uuid4), so ids
  can't be chosen or guessed. With `API_KEY` unset, SPEC-0004 B-0002 applies.

### Notes
- B-0004: With `NOTES_BACKEND=redis`, add_note appends to the Redis list
  `harnessd:notes`, capped at 1000 entries, with the same line format and
  confirmation as the file backend.

### Secrets and startup
- B-0005: With `SSM_PARAMETER_PATH` set, settings load the listed secrets from SSM
  through the Lambda extension at startup; environment variables still take
  precedence. A missing parameter fails startup with its name in the error, never
  its value.

### Infrastructure
- B-0006: `terraform plan` on a clean checkout shows the full stack: AWS and Redis
  Cloud resources together. `terraform destroy` removes everything except the
  bootstrap state bucket and roles.
- B-0007: CI on infra changes runs `terraform fmt -check`, `validate`, `tflint`,
  `trivy config` (no high findings) and `terraform test`, without cloud
  credentials.
- B-0008: `deploy.yml` builds the arm64 image, pushes it tagged with the git sha,
  applies Terraform with that tag, then smoke-tests: `/health` → 200, a request
  without the key → 401, and one agent run with the key → 200.

## Acceptance tests
- AT-0001 (B-0001, B-0002): With API_KEY set: no header, wrong key → 401 and no
  graph built; right key → 200; /health and /docs open. Unset → no header needed.
  -> `tests/test_auth.py::test_api_key`
- AT-0002 (B-0003): With API_KEY set: a run with an unknown thread_id → 404
  `thread_not_found` and no model call; `thread_id: null` → 200 with a new uuid4
  id. Unset → an unknown id starts a thread with that id.
  -> `tests/test_auth.py::test_unknown_thread_refused`
- AT-0003 (B-0004): With NOTES_BACKEND=redis, a confirmed note is in the list and
  the list stays capped. -> `tests/tools/test_add_note.py::test_redis_notes_backend` (redis)
- AT-0004 (B-0005): A mock extension serving parameters → settings hold them; a
  missing one fails with its name only. -> `tests/test_settings.py::test_ssm_parameters`
- AT-0005 (B-0006, B-0007): `terraform test` with mock providers asserts: arm64,
  reserved concurrency ≤ 5, log retention 14 days, Function URL auth NONE only with
  an API_KEY parameter, budget alerts at 50/80/100%, the database in the Lambda's
  region, TLS on when the plan offers it, tracing off.
  -> `infra/app/tests/main.tftest.hcl`
- AT-0006 (B-0008): The deploy workflow's smoke-test step fails the deploy on any
  of its three checks. -> `.github/workflows/deploy.yml` (smoke-test job)

## Non-goals
Multiple environments (one `demo` stack), custom domain, WAF, high availability,
autoscaling tuning, Langfuse in the cloud (tracing stays local; SPEC-0010),
Ollama in the cloud, blue/green deploys, MCP, per-user identity (R-0018), TLS to
Redis on the free plan.

## Open questions
- Checked first at implementation, with a hand-made free database: that it
  includes JSON and Search (`MODULE LIST`, then the checkpointer tests against it),
  whether it offers TLS, and whether the Terraform provider can create a free
  database (`rediscloud_essentials_subscription` with the free plan from
  `data "rediscloud_essentials_plan"`). If JSON and Search are missing, the paid
  Essentials plan is used and the Goal's cost changes.

Settled in the first review: a $5 monthly AWS budget; `deploy.yml` runs manually
and on `v*` tags. Settled in the second review (2026-11-14): the Redis resilience
behaviors moved to SPEC-0014; the free Redis Cloud plan without TLS; the
injection screen on and any priced model in the cloud; the unknown thread id rule
(B-0003); key holders as one trusted group.
