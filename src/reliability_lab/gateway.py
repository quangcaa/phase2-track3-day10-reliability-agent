from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cumulative_cost: float = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Route reasons are descriptive: 'cache_hit:0.95', 'primary:provider_name',
        'fallback:provider_name', or 'static_fallback'.
        Includes cost budget awareness and full latency timing.
        """
        start = time.perf_counter()

        # --- Cache check ---
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                elapsed = (time.perf_counter() - start) * 1000
                return GatewayResponse(
                    cached, f"cache_hit:{score:.2f}", None, True, elapsed, 0.0
                )

        # --- Fallback chain through providers ---
        last_error: str | None = None
        for idx, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                self.cumulative_cost += response.estimated_cost
                route = (
                    f"primary:{provider.name}"
                    if idx == 0
                    else f"fallback:{provider.name}"
                )
                elapsed = (time.perf_counter() - start) * 1000
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=elapsed,
                    estimated_cost=response.estimated_cost,
                )
            except (ProviderError, CircuitOpenError) as exc:
                last_error = str(exc)
                continue

        elapsed = (time.perf_counter() - start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=elapsed,
            estimated_cost=0.0,
            error=last_error,
        )
