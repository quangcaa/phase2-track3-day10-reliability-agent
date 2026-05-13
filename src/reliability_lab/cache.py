from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory cache with improved similarity, TTL, and false-hit guardrails.

    Uses character n-gram overlap (n=3) plus exact-match fast path for similarity.
    Applies privacy and false-hit checks before returning cached results.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        # Privacy guardrail: never serve cached results for sensitive queries
        if _is_uncacheable(query):
            return None, 0.0

        best_value: str | None = None
        best_key: str = ""
        best_score = 0.0
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        if best_score >= self.similarity_threshold:
            # False-hit guardrail: reject if year/ID differs
            if _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        # Privacy guardrail: never cache sensitive queries
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Improved similarity using exact-match fast path + character n-gram overlap.

        Character 3-gram overlap is more robust than token-based Jaccard for
        detecting small but semantically important differences (e.g. years, IDs).
        """
        a_norm = a.lower().strip()
        b_norm = b.lower().strip()
        # Exact match fast path
        if a_norm == b_norm:
            return 1.0
        if not a_norm or not b_norm:
            return 0.0
        # Character 3-gram overlap (Jaccard on n-grams)
        n = 3
        ngrams_a = Counter(a_norm[i : i + n] for i in range(len(a_norm) - n + 1))
        ngrams_b = Counter(b_norm[i : i + n] for i in range(len(b_norm) - n + 1))
        intersection = sum((ngrams_a & ngrams_b).values())
        union = sum((ngrams_a | ngrams_b).values())
        if union == 0:
            return 0.0
        return intersection / union


# ---------------------------------------------------------------------------
# Redis shared cache
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model:
        Key   = "{prefix}{query_hash}"
        Value = Redis Hash with fields: "query", "response"
        TTL   = Redis EXPIRE (automatic cleanup)

    Supports exact-match lookup, similarity-based fuzzy lookup,
    privacy guardrails, and false-hit detection.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Steps:
        1. Privacy check — skip cache for sensitive queries
        2. Exact match via hash key
        3. Similarity scan across all cached entries
        4. False-hit detection before returning
        """
        # 1. Privacy guardrail
        if _is_uncacheable(query):
            return None, 0.0

        try:
            # 2. Exact-match lookup
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if exact_response is not None:
                return exact_response, 1.0

            # 3. Similarity scan
            best_score = 0.0
            best_response: str | None = None
            best_cached_query: str = ""

            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_response = self._redis.hget(key, "response")
                    best_cached_query = cached_query

            if best_score >= self.similarity_threshold and best_response is not None:
                # 4. False-hit guardrail
                if _looks_like_false_hit(query, best_cached_query):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_query": best_cached_query,
                            "score": best_score,
                            "reason": "different_year_or_id",
                        }
                    )
                    return None, best_score
                return best_response, best_score

            return None, best_score

        except Exception:
            # Graceful degradation: if Redis is down, return cache miss
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        Skips storage for privacy-sensitive queries.
        """
        # Privacy guardrail
        if _is_uncacheable(query):
            return

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            # Graceful degradation: if Redis is down, silently skip
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
