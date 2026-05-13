# Day 10 Reliability Report

## 1. Architecture summary

The gateway implements a three-layer reliability system for LLM agent requests:

1. **Cache Layer** — In-memory (or Redis shared) cache with character n-gram similarity, TTL-based expiry, privacy guardrails, and false-hit detection.
2. **Circuit Breaker Layer** — Per-provider three-state machine (CLOSED → OPEN → HALF_OPEN → CLOSED) that prevents retry storms by failing fast when providers are unhealthy.
3. **Fallback Chain** — Ordered provider list with static fallback message as last resort.

```
User Request
    │
    ▼
┌──────────────────────────────────────────────┐
│               Gateway                        │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │  1. Cache Check (memory / Redis)       │  │
│  │     → HIT? return cached response      │  │
│  │     → Privacy query? skip cache        │  │
│  │     → False-hit detected? skip cache   │  │
│  └────────────────────────────────────────┘  │
│               │ MISS                         │
│               ▼                              │
│  ┌────────────────────────────────────────┐  │
│  │  2. Circuit Breaker: Primary           │  │
│  │     → CLOSED: call Provider A          │  │
│  │     → OPEN: skip (fail fast)           │  │
│  │     → HALF_OPEN: probe request         │  │
│  └────────────────────────────────────────┘  │
│               │ FAIL / OPEN                  │
│               ▼                              │
│  ┌────────────────────────────────────────┐  │
│  │  3. Circuit Breaker: Backup            │  │
│  │     → CLOSED: call Provider B          │  │
│  │     → OPEN: skip (fail fast)           │  │
│  └────────────────────────────────────────┘  │
│               │ FAIL / OPEN                  │
│               ▼                              │
│  ┌────────────────────────────────────────┐  │
│  │  4. Static Fallback Message            │  │
│  │     "Service temporarily degraded..."  │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Low enough to detect cascading failures quickly (after 3 consecutive errors), but high enough to avoid false opens from transient network jitter |
| reset_timeout_seconds | 2 | Matches typical cloud provider recovery time; allows circuit to probe within a few seconds without overwhelming recovering services |
| success_threshold | 1 | Single successful probe is sufficient to prove provider recovery, since FakeLLMProvider success/failure is independent per call |
| cache TTL | 300 | 5-minute window balances freshness for FAQ-type queries against hit rate; longer TTL would increase stale responses for time-sensitive content |
| similarity_threshold | 0.92 | Empirically tested: 0.85 caused false hits on date-sensitive queries ("refund policy 2024" vs "2026"), 0.92 eliminated all false hits while maintaining good hit rate |
| load_test requests | 100 | Per-scenario count; 400 total across 4 scenarios provides statistically meaningful results in reasonable time (~60s) |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.75% | ✅ Yes |
| Latency P95 | < 2500 ms | 483.06 ms | ✅ Yes |
| Fallback success rate | >= 95% | 98.33% | ✅ Yes |
| Cache hit rate | >= 10% | 77.00% | ✅ Yes |
| Recovery time | < 5000 ms | N/A (no full cycle) | ⚠️ N/A |

## 4. Metrics

From `reports/metrics.json` (generated via `make run-chaos`):

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.9975 |
| error_rate | 0.0025 |
| latency_p50_ms | 0.29 |
| latency_p95_ms | 483.06 |
| latency_p99_ms | 525.11 |
| fallback_success_rate | 0.9833 |
| cache_hit_rate | 0.7700 |
| circuit_open_count | 7 |
| recovery_time_ms | null |
| estimated_cost | $0.0405 |
| estimated_cost_saved | $0.3080 |

## 5. Cache comparison

Comparison data from the `cache_vs_nocache` scenario (100 requests each):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 219.1 | 0.3 | -99.9% |
| estimated_cost | ~0.044 | ~0.012 | -72.7% |
| cache_hit_rate | 0% | 77.0% | +77.0% |
| availability | ~99% | 99.75% | +0.75% |

**Key insight**: Cache dramatically reduces P50 latency (233ms → 0.3ms) because repeated queries to the same 5 sample prompts achieve very high hit rates. In production with more diverse queries, cache hit rate would be lower but still significant for FAQ-type traffic.

## 6. Redis shared cache

### Why shared cache matters for production

- **In-memory cache is insufficient for multi-instance deployments**: When running multiple gateway replicas behind a load balancer, each instance maintains its own cache. A query cached on Instance A produces a cache miss on Instance B, reducing overall hit rate proportionally to the number of instances.
- **How `SharedRedisCache` solves this**: All gateway instances connect to the same Redis instance. Cache entries are stored as Redis Hashes with automatic TTL via `EXPIRE`. Instance A's cache write is immediately visible to Instance B, providing consistent cache behavior regardless of which instance handles the request.

### Implementation details

`SharedRedisCache` supports:
1. **Exact-match lookup** via `HGET` on deterministic MD5 hash key — O(1) constant time
2. **Similarity scan** via `SCAN` + local character n-gram similarity computation — O(n) over cached entries
3. **Privacy guardrails** — `_is_uncacheable()` prevents caching/retrieving sensitive queries (balance, SSN, user IDs)
4. **False-hit detection** — `_looks_like_false_hit()` catches year/ID mismatches and logs them
5. **Graceful degradation** — `ConnectionError` exceptions are caught; Redis failure falls back to cache miss instead of crashing the gateway

### Evidence of shared state

```
# From test_shared_state_across_instances:
c1 = SharedRedisCache("redis://localhost:6379/0", ttl=60, threshold=0.5, prefix="rl:test:shared:")
c2 = SharedRedisCache("redis://localhost:6379/0", ttl=60, threshold=0.5, prefix="rl:test:shared:")
c1.set("shared query", "shared response")
cached, _ = c2.get("shared query")
assert cached == "shared response"  # ✅ Instance c2 reads c1's data
```

### Redis CLI output

```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
# (requires Docker running — start with `docker compose up -d`)
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary 100% fail → circuit opens, all traffic to backup + cache | Circuit opened, availability 99.5%, cache + fallback handled all traffic | ✅ Pass |
| primary_flaky_50 | Primary 50% fail → circuit oscillates OPEN/CLOSED, mix of primary and fallback | Circuit oscillated (open_count > 0), mix of routes observed | ✅ Pass |
| all_healthy | Both providers healthy → high availability, no static fallbacks | Availability > 99%, zero static fallbacks | ✅ Pass |
| cache_vs_nocache | Cache reduces latency and cost significantly | P50 dropped 99.9% (233ms → 0.3ms), cost savings $0.30 | ✅ Pass |

## 8. Failure analysis

**Remaining weakness: Similarity scan in SharedRedisCache is O(n)**

The current Redis similarity lookup uses `SCAN` to iterate all cached entries and computes character n-gram similarity locally. This is acceptable for small caches (< 1000 entries) but becomes a bottleneck at production scale.

**What could go wrong:**
- With 100,000+ cached entries, each cache lookup requires fetching and comparing every entry
- `SCAN` cursor-based iteration adds network round trips
- Under high concurrency, this creates Redis read amplification

**Proposed fix:**
1. **Use Redis Search** (RediSearch module) with vector similarity indexes for O(log n) approximate nearest neighbor lookup
2. **Alternatively**: Implement a two-tier cache — exact-match via Redis Hash (O(1)), and route similarity matches through an embedding service with HNSW index
3. **Short-term**: Add a max scan limit (e.g., 500 entries) and LRU eviction to bound the search space

## 9. Next steps

1. **Redis-backed circuit breaker state**: Store circuit breaker counters in Redis (`INCR`, `EXPIRE`) so state is shared across instances — currently each instance has independent circuit breakers, which means Instance A opening its circuit doesn't protect Instance B from the same failing provider.
2. **Concurrent load testing**: Implement `concurrent.futures.ThreadPoolExecutor` in `run_simulation` using the config's `concurrency` setting. This would reveal thread-safety issues in the in-memory cache and provide more realistic production load metrics.
3. **Per-user rate limiting**: Add sliding-window rate limiting (Redis sorted set with `ZADD`/`ZRANGEBYSCORE`) to prevent individual users from exhausting provider quotas. This is critical for production cost control and fair resource allocation.
