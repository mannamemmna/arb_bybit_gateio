from abc import ABC, abstractmethod
from typing import Callable, Optional
import asyncio


class ExchangeClient(ABC):
    """Abstract base for exchange WebSocket + REST clients."""

    def __init__(self, name: str, api_key: str, api_secret: str):
        self.name = name  # "bybit" | "gateio"
        self.api_key = api_key
        self.api_secret = api_secret
        self._price_callback: Optional[Callable] = None
        self._connected = False

    def on_price_update(self, callback: Callable):
        """Register callback: async def callback(exchange: str, symbol: str, bid: float, ask: float, ts: float)"""
        self._price_callback = callback

    @abstractmethod
    async def connect_ws(self, symbols: list[str], url: str, heartbeat_sec: int):
        """Start WebSocket connection for given symbols."""
        pass

    @abstractmethod
    async def disconnect_ws(self):
        pass

    @abstractmethod
    async def fetch_tickers(self) -> dict:
        """REST: fetch all tickers. Returns {symbol: {bid, ask, ts}}"""
        pass

    @abstractmethod
    async def fetch_orderbook(self, symbol: str, depth: int) -> dict:
        """REST: fetch L2 orderbook. Returns {bids: [[price,qty],...], asks: [[price,qty],...]}"""
        pass

    @abstractmethod
    async def place_market_order(self, symbol: str, side: str, qty: str) -> dict:
        """Place market order. side='buy'|'sell'. Returns order result dict."""
        pass

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        pass

    @abstractmethod
    async def get_wallet_balance(self) -> float:
        """Return available USDT balance."""
        pass

    @abstractmethod
    async def get_positions(self) -> list:
        """Return list of open positions."""
        pass

    @property
    def is_connected(self) -> bool:
        return self._connected
