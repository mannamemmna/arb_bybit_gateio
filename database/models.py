from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Direction(str, Enum):
    LONG_BYBIT = "long_bybit"    # Long Bybit, Short Gate.io
    LONG_GATEIO = "long_gateio"  # Long Gate.io, Short Bybit


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ABORTED = "aborted"


@dataclass
class PriceData:
    exchange: str
    symbol: str
    bid: float
    ask: float
    ts: float  # ms timestamp

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class Signal:
    symbol: str
    direction: Direction
    spread_pct: float
    price_bybit: PriceData
    price_gateio: PriceData
    ts: float
    internal_threshold: float


@dataclass
class OrderResult:
    success: bool
    exchange: str
    symbol: str
    side: str  # "buy" | "sell"
    price: float
    quantity: float
    fee: float
    order_id: Optional[str] = None
    error: Optional[str] = None
    latency_ms: Optional[int] = None


@dataclass
class PositionRecord:
    id: Optional[int] = None
    mode: str = "paper"
    symbol: str = ""
    direction: str = ""
    entry_ts: int = 0
    exit_ts: Optional[int] = None
    signal_spread_pct: float = 0.0
    preflight_spread_pct: float = 0.0
    actual_spread_pct: float = 0.0
    slippage_pct: float = 0.0
    execution_ms: int = 0
    entry_price_bybit: float = 0.0
    entry_price_gateio: float = 0.0
    exit_price_bybit: Optional[float] = None
    exit_price_gateio: Optional[float] = None
    size_usdt: float = 0.0
    leverage: int = 1
    gross_pnl: float = 0.0
    fee_total: float = 0.0
    net_pnl: float = 0.0
    status: str = "open"


@dataclass
class OrderbookLevel:
    price: float
    quantity: float


@dataclass
class Orderbook:
    exchange: str
    symbol: str
    bids: List[OrderbookLevel] = field(default_factory=list)  # sorted desc by price
    asks: List[OrderbookLevel] = field(default_factory=list)  # sorted asc by price
    ts: float = 0.0
