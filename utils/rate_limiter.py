"""
Async token-bucket rate limiter for exchange API calls.

The limiter is fully asynchronous and will never block the event loop.  When
no tokens are available it ``await``s just long enough for a token to be
refilled.

Pre-configured instances for Bybit and Gate.io are exported at module level.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token-bucket implementation
# ---------------------------------------------------------------------------


class RateLimiter:
    """
    Async token-bucket rate limiter.

    Parameters
    ----------
    max_tokens : int
        Maximum burst capacity of the bucket.
    refill_rate : float
        Tokens added per second.
    warning_threshold : float
        Fraction (0–1) of capacity at which the warning callback fires.
    on_warning : callback | None
        Called (synchronously) once when usage exceeds *warning_threshold*.
        Receives ``(bucket_name, current_tokens, max_tokens)``.
    name : str
        Human-readable label used in log messages and the warning callback.
    """

    def __init__(
        self,
        max_tokens: int,
        refill_rate: float,
        warning_threshold: float = 0.8,
        on_warning: Optional[Callable[[str, float, int], None]] = None,
        name: str = "unnamed",
    ) -> None:
        self.max_tokens = max_tokens
        self.refill_rate = refill_rate
        self.warning_threshold = warning_threshold
        self.on_warning = on_warning
        self.name = name

        self._tokens = float(max_tokens)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._warning_fired = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self.max_tokens,
                self._tokens + elapsed * self.refill_rate,
            )
            self._last_refill = now

    def _check_warning(self) -> None:
        """Fire warning callback once when usage exceeds threshold."""
        usage_ratio = 1.0 - (self._tokens / self.max_tokens)
        if usage_ratio >= self.warning_threshold and not self._warning_fired:
            self._warning_fired = True
            if self.on_warning:
                try:
                    self.on_warning(self.name, self._tokens, self.max_tokens)
                except Exception:
                    logger.exception("RateLimiter warning callback failed")
            logger.warning(
                "Rate limiter '%s' usage at %.0f%%  (%.1f/%d tokens remaining)",
                self.name,
                usage_ratio * 100,
                self._tokens,
                self.max_tokens,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self) -> None:
        """
        Wait until at least one token is available, then consume it.

        This method is fully non-blocking; it uses ``asyncio.sleep`` when
        the bucket is empty.
        """
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    # Reset warning flag once we drop below threshold again
                    usage_ratio = 1.0 - (self._tokens / self.max_tokens)
                    if usage_ratio < self.warning_threshold:
                        self._warning_fired = False
                    return
                # Calculate how long until 1 token is available
                wait_time = (1.0 - self._tokens) / self.refill_rate

            # Release lock before sleeping so others can check too
            self._check_warning()
            await asyncio.sleep(wait_time)

    @property
    def available_tokens(self) -> float:
        """Return the current token count (after refill)."""
        self._refill()
        return self._tokens

    def __repr__(self) -> str:
        return (
            f"RateLimiter(name={self.name!r}, max_tokens={self.max_tokens}, "
            f"refill_rate={self.refill_rate}/s, available={self.available_tokens:.1f})"
        )

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Warning callback
# ---------------------------------------------------------------------------

def _default_warning(name: str, current: float, maximum: int) -> None:
    """Log a warning when rate limiter usage is high."""
    logger.warning(
        "⚠️  Rate limiter '%s' approaching limit: %.1f / %d tokens remaining",
        name,
        current,
        maximum,
    )


# ---------------------------------------------------------------------------
# Pre-configured instances
# ---------------------------------------------------------------------------

bybit_public_limiter = RateLimiter(
    max_tokens=600,
    refill_rate=10,  # 600 tokens/min → 10/s
    name="bybit_public",
    on_warning=_default_warning,
)

bybit_private_limiter = RateLimiter(
    max_tokens=120,
    refill_rate=2,  # 120 tokens/min → 2/s
    name="bybit_private",
    on_warning=_default_warning,
)

gateio_private_limiter = RateLimiter(
    max_tokens=200,
    refill_rate=20,  # 200 tokens / 10s → 20/s
    name="gateio_private",
    on_warning=_default_warning,
)

gateio_public_limiter = RateLimiter(
    max_tokens=200,
    refill_rate=20,
    name="gateio_public",
    on_warning=_default_warning,
)
