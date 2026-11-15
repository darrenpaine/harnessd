# SPEC-0013: Deployment

Status: draft
Requirements: R-0017
Design: [design.md](../design/design.md)

## Goal
Run the service in the cloud as a permanent demo URL, for the operator and the
people they give the API key to, for a few dollars a month or less. It is built
entirely by Terraform across two providers (AWS for compute and Redis Cloud for
state), deployed from GitHub Actions without stored AWS keys, and protected by
an API key, budget alarms and a kill switch. It depends on SPEC-0014 for Redis
connections that drop or go stale.

## Decisions
- **AWS Lambda (container image) + Function URL.** It scales to zero, so a
  permanent demo URL costs about $0-1 a month (inside Lambda's free tier at demo
  traffic, plus ECR storage). It runs the same app through the AWS Lambda Web
  Adapter (uvicorn as usual). A function can run up to 900 s, so slow model runs
  fit; a Function URL avoids API Gateway's 29 s limit. arm64 for lower cost.
- **A separate `lambda` build target.** The Dockerfile gets a `lambda` target that
  adds the Web Adapter and the Parameters and Secrets extension to `/opt/extensions`.
  Lambda layers can't be attached to a container-image function, so both are copied
  into the image. The default target, used by compose, is unchanged.
- **Region `eu-north-1`.** The Lambda and the database are in the same AWS region,
  set by one Terraform variable.
- **Redis Cloud free database (30 MB) for state.** The checkpointer needs Redis's
  JSON and Search capabilities, which AWS ElastiCache does not reliably provide.
  Redis Cloud has a Terraform provider, so one `terraform apply` builds both the
  AWS side and the database. The free plan has no TLS, no persistence, and deletes
  a database after 14 days with no commands; all three are accepted for a demo
  (see Redis Cloud free plan below).
- **Hosted model in the cloud.** Ollama needs a GPU, which serverless doesn't have.
  The cloud default is `gemini-3.5-flash-lite`; any model in the SPEC-0012 price
  table can be chosen in settings (an unpriced one fails startup, as
  `DAILY_COST_LIMIT_USD` is required in the cloud, B-0009).
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
- **GitHub Actions with OIDC.** Workflows assume one AWS deploy role with
  short-lived credentials; no AWS keys are stored in GitHub. The role trusts only
  this repository's `demo` GitHub environment, which needs the operator's approval
  for each run and allows only `main` and `v*` tags. Redis Cloud has no OIDC, so
  its two API keys are secrets of that environment.
- **A kill switch for request floods.** The Function URL is public, and a request
  without the key is still billed. An alarm on invocations sets the function's
  reserved concurrency to 0 (B-0011), as AWS Budgets only sends email and its
  figures lag by up to a day.
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
| Redis Cloud paid Essentials (250 MB) | ~$5/mo | TLS and no idle deletion ($6 with persistence), but not needed while the demo holds no sensitive data; the upgrade path. |
| Secrets Manager | $0.40/secret/mo | Rotation features we don't need. |
| Env vars set by Terraform | $0 | Secrets would sit in Lambda config and Terraform state in plain view. |
| Secret parameters created by Terraform | $0 | Terraform can't create a parameter without a value, and a placeholder is not "missing", so B-0005's startup check would pass with a junk key. |
| Image built and pushed by Terraform | $0 | One apply from an empty account, but every apply then needs Docker, and a failed build shows as a failed apply. |
| A read-only plan role for pull requests | $0 | The repository is public; a role a pull request can assume could read the state, which holds the Redis password. |
| AWS SDK (`boto3`) to read SSM | $0 | A new dependency in the app; the extension needs none. |

## Inputs / Outputs
### Architecture
```
GitHub Actions ──OIDC──▶ AWS (deploy role) ──▶ ECR image, Lambda, SSM, Budgets
            └── Terraform ──▶ Redis Cloud (subscription + database)
Client ──HTTPS──▶ Lambda Function URL ──▶ Web Adapter ──▶ uvicorn/FastAPI
                                             ├──▶ Redis Cloud (same AWS region)
                                             ├──▶ LLM provider, Tavily
                                             └──▶ SSM via the extension (cold start)
CloudWatch alarm (invocations) ──▶ SNS ──▶ kill switch Lambda ──▶ concurrency 0
```

### Repository layout
```
infra/
  bootstrap/        once, by hand: S3 state bucket (native lockfile), GitHub OIDC
                    provider, the deploy role, the ECR repository
  bootstrap/tests/  `terraform test` files using mock providers
  app/              Lambda + Function URL, IAM role, log group, the REDIS_URL
                    parameter, AWS Budgets + SNS email, the kill switch (alarm +
                    function), Redis Cloud subscription + database
  app/kill_switch/  the kill switch function (Python, standard runtime)
  app/tests/        `terraform test` files using mock providers
.github/workflows/
  infra-ci.yml      on PRs touching infra/: fmt, validate, tflint, trivy config, terraform test
  deploy.yml        manual and on `v*` tags: build arm64 image → ECR → plan →
                    apply → smoke test
  destroy.yml       manual: terraform destroy of infra/app
  ci.yaml           also builds the `lambda` image target (B-0012)
```
Provider versions are pinned in `versions.tf` and `.terraform.lock.hcl` is committed.

### Bootstrap (Terraform, run once by the operator)
- State bucket: encrypted, versioned, readable only by the deploy role and the
  operator; `force_destroy = true`, so it can be removed once the app stack is gone.
- ECR repository: immutable tags (one image per git sha), scan on push, a lifecycle
  rule keeping the last 5 images, `force_delete = true`. It is in bootstrap because
  the Lambda can't be created before an image exists, and the image can't be pushed
  before the repository exists.
- Deploy role: assumable only through GitHub OIDC from this repository's `demo`
  environment. It can push images, manage the app stack, and read the `API_KEY`
  parameter (for the smoke test, B-0008).

### Terraform variables (app stack)
| Variable | Default | Meaning |
|----------|---------|---------|
| `region` | `eu-north-1` | AWS region of the Lambda and the database. |
| `image_tag` | none | The git sha `deploy.yml` pushed. |
| `llm_provider`, `llm_model` | `google`, `gemini-3.5-flash-lite` | The chat model; the injection screen uses the same model. |
| `reserved_concurrency` | 5 | Parallel runs; 1 on a free-tier model key. |
| `run_timeout_seconds` | 60 | `RUN_TIMEOUT_SECONDS`; 180 on a free-tier model key. |
| `llm_requests_per_minute` | unset | `LLM_REQUESTS_PER_MINUTE`; half the model's quota on a free-tier key. |
| `daily_cost_limit_usd` | 1 | `DAILY_COST_LIMIT_USD`. |
| `monthly_budget_usd` | 5 | The AWS budget. |
| `alert_email` | none | Budget and kill switch emails; from a GitHub secret, never committed. |
| `kill_switch_invocations_per_hour` | 2000 | The kill switch threshold (B-0011). |

### Lambda settings (Terraform)
- Image from ECR, arm64, 1024 MB, reserved concurrency `reserved_concurrency` (a
  cost cap, at most 5). With the pool in SPEC-0014 B-0001 capped at
  `REDIS_MAX_CONNECTIONS=5`, the stack opens at most 5 × 5 = 25 Redis connections,
  under the free plan's limit of 30.
- Timeout = `RUN_TIMEOUT_SECONDS` + 30, at most 900. Log group retention 14 days.
- Function URL with auth type NONE; the app checks the API key (B-0001).
- Environment: `TRACING_ENABLED=false`, the injection screen settings above,
  `LLM_PROVIDER`, `LLM_MODEL`, `DAILY_COST_LIMIT_USD`, `RUN_TIMEOUT_SECONDS`,
  `LLM_REQUESTS_PER_MINUTE` when set, `NOTES_BACKEND=redis`, `SSM_PARAMETER_PATH`,
  and the Web Adapter's port (5999).

### Free-tier model key
`LLM_REQUESTS_PER_MINUTE` paces each model in each process on its own (SPEC-0001
B-0008). In the cloud the chat model and the injection screen are the same model,
and each Lambda instance is a process, so the real rate can reach
2 × `reserved_concurrency` × the setting. On a free-tier key the operator sets
`reserved_concurrency = 1`, `llm_requests_per_minute` to half the model's free
quota, and `run_timeout_seconds = 180`, as paced calls are several seconds apart
and the wait counts towards the run timeout. A second caller gets 429 while a run
is in progress. On a paid key the three variables go back to their defaults.

### Redis Cloud free plan
Checked against a hand-made free database on 2026-11-15: Redis 8.8 with JSON and
Search, no TLS, 30 connections, and 18 of the 19 `redis` tests pass (the other
sets a 0.5 s run timeout, which the 34 ms round trip from outside AWS exceeds).
- The database is created in the same AWS region as the Lambda, so its traffic
  stays inside AWS's network.
- Terraform generates a long random password and writes `REDIS_URL` to SSM.
- No TLS: `REDIS_URL` is `redis://`. The code keeps `rediss://` support
  (SPEC-0014 B-0001) for a plan that offers it. Accepted for demo data only; key
  holders are told not to put anything sensitive in a thread. Real data, or
  users outside the trusted group, means moving to the paid plan with `rediss://`.
- No persistence: if Redis Cloud restarts the database, every thread, pending
  confirmation, note and the day's spend is lost, so the daily cost limit starts
  again from zero that day. Accepted for a demo; the provider's own spend limit
  still applies.
- The free database is deleted after 14 days with no commands. Keeping it in use is
  the operator's job, outside this repository.
- The free plan allows 30 connections and 100 operations a second; one free
  database per account, so a hand-made one is deleted before the first apply.

### App settings added
| Setting | Default | Meaning |
|---------|---------|---------|
| `API_KEY` | none | When set, `/v1/*` requires `X-API-Key` (B-0001) and unknown thread ids are refused (B-0003). At least 32 characters (B-0010). Required in the cloud (B-0009). |
| `SSM_PARAMETER_PATH` | none | e.g. `/harnessd/demo/`; when set, secrets are loaded from SSM at startup, and the app is "in the cloud" for B-0009. |
| `NOTES_BACKEND` | `file` | `file` (SPEC-0003) or `redis` (a capped Redis list; used on Lambda, whose disk is temporary). |

### Secrets
In SSM, under `SSM_PARAMETER_PATH`: the chosen provider's key (`GOOGLE_API_KEY`,
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`), `TAVILY_API_KEY`, `API_KEY` and
`REDIS_URL`. The operator creates the first three by hand
(`aws ssm put-parameter`); Terraform creates only `REDIS_URL`, from the database
it creates, and gives the Lambda read access to the path. The state holds the
Redis password; the state bucket is encrypted and readable only by the deploy
role and the operator.

In GitHub, as secrets of the `demo` environment: the Redis Cloud API account key
and secret key, and the alert email.

The API key is made with `openssl rand -hex 32`. Secrets are read once at cold
start, so the key is changed by putting the new value in SSM and re-running
`deploy.yml`, which replaces the running instances.

### Budget alarms and the kill switch
- AWS Budgets: monthly cost budget (default $5) with email alerts at 50%, 80% and
  100% actual and 100% forecast, via SNS.
- Kill switch: a CloudWatch alarm on the function's invocations (B-0011), sent
  through the same SNS topic.
- Model spend: SPEC-0012 `DAILY_COST_LIMIT_USD` is required in the cloud config
  (default $1), plus the provider console's own spend limit (documented, manual).
- Redis Cloud free cannot bill. The Lambda's reserved concurrency caps the number
  of parallel runs.

### Steps done by hand
The README has a runbook for the steps Terraform and the workflows don't do:
bootstrap (with the operator's own AWS credentials), the GitHub `demo`
environment with its approval rule and secrets, the three SSM parameters, key
rotation, resetting the kill switch, and taking everything down.

### Taking it down
1. `destroy.yml`, or `terraform destroy` in `infra/app`: removes everything that
   runs or can cost money, including the Redis Cloud subscription and database.
2. `terraform destroy` in `infra/bootstrap`, only after step 1: removes the ECR
   repository with its images, the deploy role, the OIDC provider and the state
   bucket.
3. By hand: the three SSM parameters, the GitHub `demo` environment, and the Redis
   Cloud account.

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
- B-0009: With `SSM_PARAMETER_PATH` set, startup fails unless both `API_KEY` and
  `DAILY_COST_LIMIT_USD` are set; the error names the missing setting.
- B-0010: An `API_KEY` shorter than 32 characters fails startup, wherever the app
  runs; the error never shows the key.

### Infrastructure
- B-0006: `terraform plan` of `infra/app` on a clean checkout shows the full stack:
  AWS and Redis Cloud resources together. `terraform destroy` there removes
  everything except what bootstrap made: the state bucket, the deploy role, the
  OIDC provider and the image repository.
- B-0007: CI on infra changes runs `terraform fmt -check`, `validate`, `tflint`,
  `trivy config` (no high findings) and `terraform test`, without cloud
  credentials.
- B-0008: `deploy.yml` runs in the `demo` environment, so it waits for the
  operator's approval. It builds the arm64 `lambda` image, pushes it tagged with
  the git sha, shows the Terraform plan, applies it with that tag, then
  smoke-tests: `/health` → 200, a request without the key → 401, and one agent run
  with the key → 200. It reads the key from SSM, masked in the log, and deletes
  the thread the run made. A failed check fails the workflow and leaves the new
  version live; there is no automatic rollback.
- B-0011: When the function is invoked more than
  `kill_switch_invocations_per_hour` times in one hour, the kill switch sets its
  reserved concurrency to 0 and the operator is emailed. Callers then get 429
  from the Function URL, and throttled requests are not billed. Re-running
  `deploy.yml` puts the concurrency back.
- B-0012: CI builds the `lambda` image target, without pushing it, on pull requests
  that change the Dockerfile or `uv.lock`.
- B-0013: `destroy.yml` runs manually in the `demo` environment and destroys the
  app stack.

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
- AT-0005 (B-0006, B-0007, B-0011): `terraform test` with mock providers asserts:
  arm64, reserved concurrency ≤ 5, log retention 14 days, Function URL auth NONE,
  the Lambda's read access limited to the parameter path, budget alerts at
  50/80/100%, the database in the Lambda's region, TLS on when the plan offers
  it, tracing off, and the alarm on invocations with its threshold wired to the
  kill switch. -> `infra/app/tests/main.tftest.hcl`
- AT-0006 (B-0008): The deploy workflow's smoke-test step fails the deploy on any
  of its three checks. -> `.github/workflows/deploy.yml` (smoke-test job)
- AT-0007 (B-0009, B-0010): With SSM_PARAMETER_PATH set, settings without API_KEY,
  or without DAILY_COST_LIMIT_USD, fail naming the setting. An API_KEY of 31
  characters fails without showing it; 32 passes.
  -> `tests/test_settings.py::test_cloud_requirements`
- AT-0008 (B-0006): `terraform test` with mock providers asserts: the repository
  has immutable tags, scan on push and a keep-5 lifecycle rule; the state bucket
  is encrypted and versioned; the deploy role's trust policy accepts only this
  repository's `demo` environment. -> `infra/bootstrap/tests/main.tftest.hcl`
- AT-0009 (B-0011): Given an alarm message, the kill switch function sets the
  named function's reserved concurrency to 0, using a fake AWS client.
  -> `tests/test_kill_switch.py::test_sets_concurrency_to_zero`
- AT-0010 (B-0012): The `lambda-image` job builds the target and fails the pull
  request when the build fails. -> `.github/workflows/ci.yaml` (lambda-image job)
- AT-0011 (B-0013): -> `.github/workflows/destroy.yml`

## Non-goals
Multiple environments (one `demo` stack), custom domain, WAF, high availability,
autoscaling tuning, Langfuse in the cloud (tracing stays local; SPEC-0010),
Ollama in the cloud, blue/green deploys, automatic rollback, MCP, per-user
identity (R-0018), TLS to Redis and persistence on the free plan, GitHub settings
managed by Terraform, a per-client request limit.

## Open questions
- Whether the Terraform provider can create a free database
  (`rediscloud_essentials_subscription` with the free plan from
  `data "rediscloud_essentials_plan"`). The plan is listed by the API the provider
  uses, at price 0 in `eu-north-1`; it is proven only by the first apply. If it
  fails, the database is made by hand and imported.
- The free quota of `gemini-3.5-flash-lite`, which sets `llm_requests_per_minute`;
  the operator supplies it.

Settled in the first review: a $5 monthly AWS budget; `deploy.yml` runs manually
and on `v*` tags. Settled in the second review (2026-11-14): the Redis resilience
behaviors moved to SPEC-0014; the free Redis Cloud plan without TLS; the
injection screen on and any priced model in the cloud; the unknown thread id rule
(B-0003); key holders as one trusted group. Settled in the third review
(2026-11-15): the free plan has JSON and Search and no TLS; `eu-north-1`; the
image repository in bootstrap; the `lambda` build target; secret parameters made
by hand; Redis Cloud keys as GitHub secrets; one deploy role tied to the `demo`
environment and no plan role; the kill switch; startup rules for the cloud
(B-0009, B-0010); no persistence accepted; free-tier model settings; no
automatic rollback; `destroy.yml`.
