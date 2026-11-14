# SPEC-0014: Redis resilience

Status: reviewed
Requirements: R-0006, R-0013, R-0026
Design: [design.md](../design/design.md)

## Goal
Keep the service working when a Redis connection drops or goes stale, without
reconnect storms, and keep the per-run Redis cost small and independent of the
keyspace size. It applies in every environment; the cloud deployment (SPEC-0013)
depends on it, as a Lambda freezes between requests and its connections go stale.

## Inputs / Outputs
### App settings added
| Setting | Default | Meaning |
|---------|---------|---------|
| `REDIS_HEALTH_CHECK_SECONDS` | `30` | Ping an idle connection before reuse after this long. |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `5` | Connect and read timeout. |
| `REDIS_RETRIES` | `3` | Retries with exponential backoff and jitter on connection errors. |
| `REDIS_MAX_CONNECTIONS` | `5` | Per process, for the one pool shared by the checkpointer and the lock client (B-0001). Where several processes share one database, processes × this must stay within the database's connection limit. |

No API change.

## Behavior
- B-0001: The Redis client is built from `REDIS_URL` (TLS with `rediss://`) with a
  connect and read timeout, a health check on connections idle longer than
  `REDIS_HEALTH_CHECK_SECONDS`, TCP keepalive, and one connection pool per process
  capped at `REDIS_MAX_CONNECTIONS`. The checkpointer and the thread lock share that
  client (today each opens its own). A connection that went stale while idle (or
  while a Lambda was frozen) is detected and replaced before use.
- B-0002: Idempotent Redis operations (checkpoint reads, TTL refresh, lock release,
  pings) are retried up to `REDIS_RETRIES` times with exponential backoff and
  jitter on connection and timeout errors. Lock acquisition is not retried;
  a failure there is a 503. After the last retry the request gets 503 as today
  (SPEC-0002 B-0005).
- B-0003: If the checkpointer's connection fails, the next request rebuilds the
  checkpointer and graph instead of reusing the broken one. A rebuild after a
  failure happens at most once per `REDIS_HEALTH_CHECK_SECONDS`, so a Redis outage
  doesn't cause a reconnect storm.
- B-0004: The checkpoint TTL refresh (SPEC-0004 B-0003) no longer scans the
  keyspace. It refreshes only the thread's keys, found without a `SCAN`
  ([#14](https://github.com/darrenpaine/harnessd/issues/14)), so a refresh costs a
  few operations however many keys Redis holds.

## Acceptance tests
- AT-0001 (B-0001, B-0003): A Redis connection killed between requests (`CLIENT
  KILL`) → the next request succeeds. -> `tests/test_redis_resilience.py::test_stale_connection_replaced` (redis)
- AT-0002 (B-0002): A fake Redis failing twice then succeeding → the read succeeds;
  failing past the retry limit → 503; lock acquisition is tried once.
  -> `tests/test_redis_resilience.py::test_retries`
- AT-0003 (B-0004): A refresh touches only the thread's keys and runs no SCAN.
  -> `tests/test_checkpoint.py::test_ttl_refresh_without_scan` (redis)
- AT-0004 (B-0001): The checkpointer and the lock use the same Redis client, whose
  pool is capped at REDIS_MAX_CONNECTIONS.
  -> `tests/test_redis_resilience.py::test_shared_bounded_pool`

## Non-goals
Redis Cluster or Sentinel, failover between databases, retrying non-idempotent
writes (checkpoint writes, lock acquisition, the day's spend), a circuit breaker
beyond B-0003's rebuild limit.

## Open questions
None. Moved from SPEC-0013 (B-0003..B-0006 and AT-0002..AT-0004, AT-0009 there),
which was reviewed; the only changes are the ids and B-0004 no longer naming the
mechanism (a per-thread set, or the checkpointer's own per-thread index), which is
chosen at implementation.
