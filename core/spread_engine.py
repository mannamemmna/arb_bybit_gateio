import time
import asyncio
from typing import Optional, Callable, Tuple
from dataclasses import dataclass
from enum import Enum
from utils.logger import get_logger

logger = get_logger('spread_engine')


class Direction(Enum):
    LONG_BYBIT = "long_bybit"    # Gate.io expensive -> Long Bybit (cheap), Short Gate.io
    LONG_GATEIO = "long_gateio"  # Bybit expensive -> Long Gate.io (cheap), Short Bybit


@dataclass
class Signal:
    symbol: str
    direction: Direction
    spread_pct: float
    price_bybit: object  # PriceData
    price_gateio: object  # PriceData
    ts: float
    internal_threshold: float


class SpreadEngine:
    """
    Event-driven spread calculator.

    NOT timer-based. Triggered on every price update from WS.
    Uses fee-aware thresholds:
      internal_threshold = SPREAD_ENTRY_THRESHOLD + round_trip_fee + SLIPPAGE_BUFFER
      Default: 0.5% + 0.22% + 0.10% = 0.82%

    Entry signals:
      spread_pct >= +threshold -> Bybit more expensive: Short Bybit, Long Gate.io
      spread_pct <= -threshold -> Gate.io more expensive: Short Gate.io, Long Bybit

    Exit signal:
      |spread_pct| <= SPREAD_EXIT_THRESHOLD -> spread converged, close position
    """

    def __init__(
        self,
        price_cache,
        orderbook_cache,
        entry_threshold: float,
        exit_threshold: float,
        round_trip_fee: float,
        slippage_buffer: float,
        preflight_spread_decay: float,
        use_orderbook_check: bool,
        orderbook_depth: int,
        max_position_usdt: float = 50.0,
        position_tracker=None,
        on_signal: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
        max_open_positions: int = 5,
        bybit_client=None,
        gateio_client=None,
    ):
        self.price_cache = price_cache
        self.ob_cache = orderbook_cache
        self.entry_threshold = entry_threshold  # SPREAD_ENTRY_THRESHOLD from .env
        self.exit_threshold = exit_threshold    # SPREAD_EXIT_THRESHOLD from .env
        self.round_trip_fee = round_trip_fee
        self.slippage_buffer = slippage_buffer
        self.preflight_spread_decay = preflight_spread_decay
        self.use_orderbook_check = use_orderbook_check
        self.orderbook_depth = orderbook_depth
        self.max_position_usdt = max_position_usdt
        self.position_tracker = position_tracker
        self.max_open_positions = max_open_positions
        self.on_signal = on_signal  # async callback(signal: Signal)
        self.on_exit = on_exit      # async callback(symbol: str, spread_pct: float)
        self.bybit_client = bybit_client
        self.gateio_client = gateio_client

        # Derived
        self.internal_threshold = entry_threshold + round_trip_fee + slippage_buffer

        self._signal_count: int = 0
        self._rejected_count: int = 0

    async def _refresh_orderbook(self, symbol: str) -> None:
        """Fetch orderbook snapshot from both exchanges and update cache."""
        if not self.bybit_client or not self.gateio_client:
            return

        gateio_symbol = symbol.replace('USDT', '_USDT')
        try:
            bybit_ob, gateio_ob = await asyncio.gather(
                self.bybit_client.fetch_orderbook(symbol, depth=self.orderbook_depth),
                self.gateio_client.fetch_orderbook(gateio_symbol, depth=self.orderbook_depth),
            )
            self.ob_cache.update('bybit', symbol, bybit_ob.get('bids', []), bybit_ob.get('asks', []))
            self.ob_cache.update('gateio', symbol, gateio_ob.get('bids', []), gateio_ob.get('asks', []))
        except Exception as e:
            logger.warning(f"[{symbol}] Orderbook fetch failed: {e}")

    async def on_price_update(self, exchange: str, symbol: str) -> None:
        """
        Called on every price update from WS.
        Calculates spread, checks conditions, generates signals.
        """
        # Calculate spread
        spread_pct = self.price_cache.calc_spread(symbol)
        if spread_pct is None:
            return

        # Check exit condition first (for open positions)
        if abs(spread_pct) <= self.exit_threshold:
            if self.on_exit:
                await self.on_exit(symbol, spread_pct)
            return

        # Check entry condition
        if abs(spread_pct) >= self.internal_threshold:
            # Determine direction
            if spread_pct > 0:
                direction = Direction.LONG_GATEIO  # Bybit expensive -> long cheap (Gate.io)
            else:
                direction = Direction.LONG_BYBIT   # Gate.io expensive -> long cheap (Bybit)

            # Create signal
            bybit_price = self.price_cache.get('bybit', symbol)
            gateio_price = self.price_cache.get('gateio', symbol)

            if not bybit_price or not gateio_price:
                return

            signal = Signal(
                symbol=symbol,
                direction=direction,
                spread_pct=spread_pct,
                price_bybit=bybit_price,
                price_gateio=gateio_price,
                ts=time.time() * 1000,
                internal_threshold=self.internal_threshold,
            )

            # Validate signal
            valid, reason = await self._validate_signal(signal)
            if not valid:
                self._rejected_count += 1
                logger.debug(f"[{symbol}] Signal rejected: {reason}")
                return

            self._signal_count += 1
            logger.info(
                f"[{symbol}] SIGNAL: {direction.value} spread={spread_pct:.3f}% "
                f"(threshold={self.internal_threshold:.3f}%)"
            )

            if self.on_signal:
                await self.on_signal(signal)

    async def _validate_signal(self, signal: Signal) -> Tuple[bool, str]:
        """
        Multi-step validation before signal is emitted:
        1. Price freshness check
        2. Spread decay check
        3. Position duplicate check
        4. Max positions check
        5. Orderbook depth check (optional)
        """
        symbol = signal.symbol

        # 1. Price freshness
        bybit_price = self.price_cache.get('bybit', symbol)
        gateio_price = self.price_cache.get('gateio', symbol)
        now = time.time() * 1000

        if bybit_price and (now - bybit_price.ts) > 500:
            return False, f"bybit price stale ({(now - bybit_price.ts):.0f}ms)"
        if gateio_price and (now - gateio_price.ts) > 500:
            return False, f"gateio price stale ({(now - gateio_price.ts):.0f}ms)"

        # 2. Spread decay check
        if not self.price_cache.is_spread_stable(symbol, self.preflight_spread_decay):
            return False, "spread decaying from recent peak"

        # 3. Position duplicate check
        if self.position_tracker:
            if await self.position_tracker.has_open_position(symbol):
                return False, "pair already has open position"

            # 4. Max positions check
            if self.position_tracker.open_count >= self.max_open_positions:
                return False, "max open positions reached"

        # 5. Orderbook depth check
        if self.use_orderbook_check:
            await self._refresh_orderbook(symbol)
            is_ok, actual_spread = self.ob_cache.check_liquidity(
                symbol,
                size_usdt=self.max_position_usdt,
                depth=self.orderbook_depth,
                internal_threshold=self.internal_threshold,
            )
            if not is_ok:
                return False, f"insufficient orderbook liquidity (actual_spread={actual_spread:.3f}%)"

        return True, "ok"

    def get_stats(self) -> dict:
        return {
            'signal_count': self._signal_count,
            'rejected_count': self._rejected_count,
            'internal_threshold': self.internal_threshold,
            'entry_threshold': self.entry_threshold,
            'exit_threshold': self.exit_threshold,
        }
