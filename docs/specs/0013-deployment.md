# SPEC-0013: Deployment

Status: reviewed
Requirements: R-0017
Design: [design.md](../design/design.md)

## Goal
Run the service in the cloud for a few dollars a month or less, entirely built by
Terraform across two providers (AWS for compute and Redis Cloud for state), deployed
from GitHub Actions without stored cloud keys, protected by an API key and budget
alarms, and hardened for Redis connections that drop or go stale.

## Decisions
- **AWS Lambda (container image) + Function URL.** It scales to zero, so a
  permanent demo URL costs about $0-1 a month (inside Lambda's free tier at demo
  traffic, plus ECR storage). It runs the existing container unchanged through the
  AWS Lambda Web Adapter (uvicorn as usual). A function can run up to 900 s, so
  slow model runs fit; a Function URL avoids API Gateway's 29 s limit. arm64 for
  lower cost.
- **Redis Cloud (free 30 MB database) for state.** The checkpointer needs Redis's
  JSON and Search capabilities, which AWS ElastiCache does not reliably provide.
  Redis Cloud provides them and has a Terraform provider, so one `terraform apply`
  builds both the AWS side and the database.
- **Hosted model in the cloud.** Ollama needs a GPU, which serverless doesn't have;
  the cloud config uses a low-cost hosted model (Gemini Flash-Lite by default; Claude
  Haiku as the alternative). Spend is capped by SPEC-0012's daily limit.
- **Secrets in SSM Parameter Store** (SecureString, free), read at cold start through
  the AWS Parameters and Secrets Lambda Extension over localhost HTTP, so the app
  needs no AWS SDK.
- **GitHub Actions with OIDC.** Workflows assume an AWS role with short-lived
  credentials; no AWS keys are stored in GitHub.

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
| Secrets Manager | $0.40/secret/mo | Rotation features we don't need. |
| Env vars set by Terraform | $0 | Secrets would sit in Lambda config and Terraform state in plain view. |

## Inputs / Outputs
### Architecture
```
GitHub Actions ──OIDC──▶ AWS (deploy role) ──▶ ECR image, Lambda, SSM, Budgets
            └── Terraform ──▶ Redis Cloud (subscription + database)
Client ──HTTPS──▶ Lambda Function URL ──▶ Web Adapter ──▶ uvicorn/FastAPI
                                             ├──▶ Redis Cloud (TLS)
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
  deploy.yml      manual and on release tags: build arm64 image → ECR → terraform apply → smoke test
```
Provider versions are pinned in `versions.tf` and `.terraform.lock.hcl` is committed.

### Lambda settings (Terraform)
- Image from ECR, arm64, 1024 MB, reserved concurrency 5 (a cost cap). With the
  pool in B-0003 capped at `REDIS_MAX_CONNECTIONS=5`, the stack opens at most
  5 × 5 = 25 Redis connections, under Redis Cloud's free limit of 30.
- Timeout = `RUN_TIMEOUT_SECONDS` + 30, at most 900. Log group retention 14 days.
- Function URL with auth type NONE; the app checks the API key (B-0001).

### App settings added
| Setting | Default | Meaning |
|---------|---------|---------|
| `API_KEY` | none | When set, `/v1/*` requires `X-API-Key`. Required in the cloud. |
| `SSM_PARAMETER_PATH` | none | e.g. `/harnessd/demo/`; when set, secrets are loaded from SSM at startup. |
| `NOTES_BACKEND` | `file` | `file` (SPEC-0003) or `redis` (a capped Redis list; used on Lambda, whose disk is temporary). |
| `REDIS_HEALTH_CHECK_SECONDS` | `30` | Ping an idle connection before reuse after this long. |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `5` | Connect and read timeout. |
| `REDIS_RETRIES` | `3` | Retries with exponential backoff and jitter on connection errors. |
| `REDIS_MAX_CONNECTIONS` | `5` | Per process, for the one pool shared by the checkpointer and the lock client (B-0003). Reserved concurrency × this must stay within the database's connection limit. |

### Secrets (SSM, under `SSM_PARAMETER_PATH`)
`GOOGLE_API_KEY` or `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `API_KEY` and
`REDIS_URL`. Terraform creates the parameter names. Values are set outside
Terraform (`aws ssm put-parameter`), except `REDIS_URL`, which Terraform writes
from the database it creates. The state bucket is encrypted and readable only by
the deploy roles.

### Budget alarms
- AWS Budgets: monthly cost budget (default $5) with email alerts at 50%, 80% and
  100% actual and 100% forecast, via SNS.
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

### Redis hardening (all environments)
- B-0003: The Redis client is built from `REDIS_URL` (TLS with `rediss://`) with a
  connect and read timeout, a health check on connections idle longer than
  `REDIS_HEALTH_CHECK_SECONDS`, TCP keepalive, and one connection pool per process
  capped at `REDIS_MAX_CONNECTIONS`. The checkpointer and the thread lock share that
  client (today each opens its own). A connection that went stale while the Lambda
  was frozen is detected and replaced before use.
- B-0004: Idempotent Redis operations (checkpoint reads, TTL refresh, lock release,
  pings) are retried up to `REDIS_RETRIES` times with exponential backoff and
  jitter on connection and timeout errors. Lock acquisition is not retried;
  a failure there is a 503. After the last retry the request gets 503 as today
  (SPEC-0002 B-0005).
- B-0005: If the checkpointer's connection fails, the next request rebuilds the
  checkpointer and graph instead of reusing the broken one. A rebuild after a
  failure happens at most once per `REDIS_HEALTH_CHECK_SECONDS`, so a Redis outage
  doesn't cause a reconnect storm.
- B-0006: The checkpoint TTL refresh (SPEC-0004 B-0003) no longer scans the
  keyspace. It refreshes only the thread's keys, tracked in a per-thread set
  (issue #14), so a refresh costs a few operations, within the free plan's
  100 ops/sec.
- B-0007: With `NOTES_BACKEND=redis`, add_note appends to the Redis list
  `harnessd:notes`, capped at 1000 entries, with the same line format and
  confirmation as the file backend.

### Secrets and startup
- B-0008: With `SSM_PARAMETER_PATH` set, settings load the listed secrets from SSM
  through the Lambda extension at startup; environment variables still take
  precedence. A missing parameter fails startup with its name in the error, never
  its value.

### Infrastructure
- B-0009: `terraform plan` on a clean checkout shows the full stack: AWS and Redis
  Cloud resources together. `terraform destroy` removes everything except the
  bootstrap state bucket and roles.
- B-0010: CI on infra changes runs `terraform fmt -check`, `validate`, `tflint`,
  `trivy config` (no high findings) and `terraform test`, without cloud
  credentials.
- B-0011: `deploy.yml` builds the arm64 image, pushes it tagged with the git sha,
  applies Terraform with that tag, then smoke-tests: `/health` → 200, a request
  without the key → 401, and one agent run with the key → 200.

## Acceptance tests
- AT-0001 (B-0001, B-0002): With API_KEY set: no header, wrong key → 401 and no
  graph built; right key → 200; /health and /docs open. Unset → no header needed.
  -> `tests/test_auth.py::test_api_key`
- AT-0002 (B-0003, B-0005): A Redis connection killed between requests (`CLIENT
  KILL`) → the next request succeeds. -> `tests/test_redis_resilience.py::test_stale_connection_replaced` (redis)
- AT-0003 (B-0004): A fake Redis failing twice then succeeding → the read succeeds;
  failing past the retry limit → 503; lock acquisition is tried once.
  -> `tests/test_redis_resilience.py::test_retries`
- AT-0004 (B-0006): A refresh touches only the thread's keys and runs no SCAN.
  -> `tests/test_checkpoint.py::test_ttl_refresh_without_scan` (redis)
- AT-0005 (B-0007): With NOTES_BACKEND=redis, a confirmed note is in the list and
  the list stays capped. -> `tests/tools/test_add_note.py::test_redis_notes_backend` (redis)
- AT-0006 (B-0008): A mock extension serving parameters → settings hold them; a
  missing one fails with its name only. -> `tests/test_settings.py::test_ssm_parameters`
- AT-0007 (B-0009, B-0010): `terraform test` with mock providers asserts: arm64,
  reserved concurrency ≤ 5, log retention 14 days, Function URL auth NONE only with
  an API_KEY parameter, budget alerts at 50/80/100%, Redis database TLS on.
  -> `infra/app/tests/main.tftest.hcl`
- AT-0008 (B-0011): The deploy workflow's smoke-test step fails the deploy on any
  of its three checks. -> `.github/workflows/deploy.yml` (smoke-test job)
- AT-0009 (B-0003): The checkpointer and the lock use the same Redis client, whose
  pool is capped at REDIS_MAX_CONNECTIONS.
  -> `tests/test_redis_resilience.py::test_shared_bounded_pool`

## Non-goals
Multiple environments (one `demo` stack), custom domain, WAF, high availability,
autoscaling tuning, Langfuse in the cloud (tracing stays local; SPEC-0010),
Ollama in the cloud, blue/green deploys, MCP.

## Open questions
- Redis Cloud's free plan: the docs list its size and limits, but whether it
  includes Search and JSON, and whether the Terraform provider can create a free
  database, is unconfirmed. This is checked first at implementation (docs, then the
  checkpointer tests against a hand-made free database), and only then is the
  database plan chosen (free, or the smallest paid Essentials plan).

Settled in review: Gemini Flash-Lite as the cloud model; a $5 monthly AWS budget;
`deploy.yml` runs manually and on `v*` tags. The alert email is a Terraform
variable supplied from a GitHub secret, never committed.
