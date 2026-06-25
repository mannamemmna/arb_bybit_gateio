"""In-memory price store with spread history."""

from collections import deque
from dataclasses import dataclass
from typing import Optional, Dict

import time

from utils.logger import get_logger

logger = get_logger('price_cache')


@dataclass
class PriceData:
    exchange: str        # "bybit" | "gateio"
    symbol: str          # e.g. "BTCUSDT"
    bid: float           # best bid price
    ask: float           # best ask price
    ts: float            # timestamp in ms (time.time() * 1000)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class PriceCache:
    """
    In-memory store for latest prices from both exchanges.
    Event-driven: spread check is triggered on every price update.

    Structure:
        _prices[exchange][symbol] = PriceData
        spread_history[symbol] = deque(maxlen=10)  # recent spread % values
    """

    def __init__(self, staleness_ms: int = 500) -> None:
        self._prices: Dict[str, Dict[str, PriceData]] = {
            'bybit': {},
            'gateio': {},
        }
        self.spread_history: Dict[str, deque[float]] = {}  # symbol -> deque of recent spread%
        self.staleness_ms: int = staleness_ms
        self._update_count: int = 0
        self._last_update_ts: Dict[str, float] = {'bybit': 0.0, 'gateio': 0.0}

    @staticmethod
    def _normalize_symbol(exchange: str, symbol: str) -> str:
        """Store all symbols in Bybit format (BTCUSDT) for cross-exchange lookup."""
        if exchange == 'gateio':
            return symbol.replace('_USDT', 'USDT').replace('_usdt', 'USDT')
        return symbol  # bybit already uses BTCUSDT

    def update(self, exchange: str, symbol: str, bid: float, ask: float, ts: float) -> None:
        """Update price from WS. Called on every tickers update."""
        norm = self._normalize_symbol(exchange, symbol)
        self._prices[exchange][norm] = PriceData(
            exchange=exchange, symbol=norm, bid=bid, ask=ask, ts=ts,
        )
        self._update_count += 1
        self._last_update_ts[exchange] = ts

    def get(self, exchange: str, symbol: str) -> Optional[PriceData]:
        """Get latest price for a symbol on an exchange."""
        return self._prices.get(exchange, {}).get(symbol)

    def is_fresh(self, exchange: str, symbol: str) -> bool:
        """Check if price is not stale (within staleness_ms)."""
        price = self.get(exchange, symbol)
        if not price:
            return False
        return (time.time() * 1000 - price.ts) < self.staleness_ms

    def calc_spread(self, symbol: str) -> Optional[float]:
        """Calculate spread %: ((bybit_mid - gateio_mid) / gateio_mid) * 100.

        Returns positive if Bybit is more expensive, negative otherwise.
        """
        bybit = self.get('bybit', symbol)
        gateio = self.get('gateio', symbol)
        if not bybit or not gateio:
            return None

        spread_pct = ((bybit.mid - gateio.mid) / gateio.mid) * 100

        if symbol not in self.spread_history:
            self.spread_history[symbol] = deque(maxlen=10)
        self.spread_history[symbol].append(spread_pct)

        return spread_pct

    def is_spread_stable(self, symbol: str, decay_threshold: float = 0.30) -> bool:
        """Check if spread is not decaying from recent peak.

        Returns False if current spread has dropped > decay_threshold from recent peak.
        Example: peak 0.8%, current 0.5% => ratio 0.625 < 0.70 => unstable.
        """
        history = self.spread_history.get(symbol, deque())
        if len(history) < 3:
            return True  # Not enough data, allow

        recent_max = max(abs(s) for s in list(history)[-5:])
        current = abs(history[-1])

        if recent_max > 0 and (current / recent_max) < (1 - decay_threshold):
            return False
        return True

    def get_spread_history(self, symbol: str) -> list[float]:
        return list(self.spread_history.get(symbol, []))

    def get_stats(self) -> dict:
        """Return cache stats for monitoring."""
        return {
            'total_updates': self._update_count,
            'bybit_symbols': len(self._prices['bybit']),
            'gateio_symbols': len(self._prices['gateio']),
            'spread_history_symbols': len(self.spread_history),
            'last_bybit_update': self._last_update_ts['bybit'],
            'last_gateio_update': self._last_update_ts['gateio'],
        }
