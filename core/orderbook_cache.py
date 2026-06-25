"""Level-2 orderbook store for liquidity checks."""

import time
from dataclasses import dataclass
from typing import Optional, Dict, List

from utils.logger import get_logger

logger = get_logger('orderbook_cache')


@dataclass
class OrderbookLevel:
    price: float
    quantity: float


@dataclass
class Orderbook:
    exchange: str
    symbol: str
    bids: List[OrderbookLevel]  # sorted descending by price
    asks: List[OrderbookLevel]  # sorted ascending by price
    ts: float


class OrderbookCache:
    """
    Stores Level-2 orderbook snapshots for VWAP fill simulation.
    Updated via REST fetch (not WebSocket) — one-shot per signal.
    """

    def __init__(self) -> None:
        self._books: Dict[str, Dict[str, Orderbook]] = {
            'bybit': {},
            'gateio': {},
        }

    def update(
        self,
        exchange: str,
        symbol: str,
        bids: list,
        asks: list,
        ts: Optional[float] = None,
    ) -> None:
        """Update orderbook snapshot.

        bids/asks format: [[price_str, qty_str], ...]
        """
        ts = ts or (time.time() * 1000)

        book = Orderbook(
            exchange=exchange,
            symbol=symbol,
            bids=[OrderbookLevel(float(p), float(q)) for p, q in bids],
            asks=[OrderbookLevel(float(p), float(q)) for p, q in asks],
            ts=ts,
        )

        # Sort: bids descending, asks ascending
        book.bids.sort(key=lambda x: x.price, reverse=True)
        book.asks.sort(key=lambda x: x.price)

        self._books[exchange][symbol] = book

    def get(self, exchange: str, symbol: str) -> Optional[Orderbook]:
        return self._books.get(exchange, {}).get(symbol)

    def vwap_fill(
        self, exchange: str, symbol: str, size_usdt: float, side: str,
    ) -> Optional[float]:
        """Simulate VWAP fill for a given USDT size.

        side='ask' for buying (long), side='bid' for selling (short).

        Returns average fill price, or None if insufficient liquidity.
        """
        book = self.get(exchange, symbol)
        if not book:
            return None

        levels = book.asks if side == 'ask' else book.bids
        if not levels:
            return None

        remaining_usdt: float = size_usdt
        total_qty: float = 0.0
        total_cost: float = 0.0

        for level in levels:
            if remaining_usdt <= 0:
                break

            level_value: float = level.price * level.quantity
            fill_value: float = min(remaining_usdt, level_value)
            fill_qty: float = fill_value / level.price

            total_cost += fill_value
            total_qty += fill_qty
            remaining_usdt -= fill_value

        if total_qty == 0:
            return None

        return total_cost / total_qty

    def check_liquidity(
        self,
        symbol: str,
        size_usdt: float,
        depth: int = 5,
        internal_threshold: float = 0.82,
    ) -> tuple[bool, float]:
        """Check if both exchanges have enough liquidity at expected fill price.

        Returns (is_ok, actual_spread_pct).
        """
        bybit_book = self.get('bybit', symbol)
        gateio_book = self.get('gateio', symbol)

        if not bybit_book or not gateio_book:
            return False, 0.0

        bybit_mid: float = (
            (bybit_book.bids[0].price + bybit_book.asks[0].price) / 2
            if bybit_book.bids and bybit_book.asks
            else 0.0
        )
        gateio_mid: float = (
            (gateio_book.bids[0].price + gateio_book.asks[0].price) / 2
            if gateio_book.bids and gateio_book.asks
            else 0.0
        )

        if bybit_mid >= gateio_mid:
            # Bybit more expensive -> short Bybit (bids), long Gate.io (asks)
            vwap_short: Optional[float] = self.vwap_fill('bybit', symbol, size_usdt, 'bid')
            vwap_long: Optional[float] = self.vwap_fill('gateio', symbol, size_usdt, 'ask')
        else:
            # Gate.io more expensive -> short Gate.io (bids), long Bybit (asks)
            vwap_short = self.vwap_fill('gateio', symbol, size_usdt, 'bid')
            vwap_long = self.vwap_fill('bybit', symbol, size_usdt, 'ask')

        if not vwap_short or not vwap_long:
            return False, 0.0

        actual_spread: float = ((vwap_short - vwap_long) / vwap_long) * 100
        return abs(actual_spread) >= internal_threshold, actual_spread
