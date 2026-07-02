import time
import asyncio
from database.models import Signal, PriceData, OrderResult, PositionRecord, Direction
from core.orderbook_cache import OrderbookCache

SLIPPAGE_FALLBACK_PCT = 0.0005  # 0.05% fallback if no orderbook data


class PaperEngine:
    def __init__(
        self,
        initial_balance: float,
        slippage_pct: float,
        taker_fee_bybit: float,
        taker_fee_gateio: float,
    ):
        self.balance = initial_balance
        self.slippage_pct = slippage_pct  # flat fallback
        self.taker_fee_bybit = taker_fee_bybit
        self.taker_fee_gateio = taker_fee_gateio
        self.initial_balance = initial_balance
        self.ob_cache: OrderbookCache = None  # set externally by executor

    async def _vwap_fill_price(
        self, exchange: str, symbol: str, side: str, size_usdt: float,
    ) -> float:
        """
        Calculate realistic fill price using VWAP from orderbook cache.
        Falls back to flat slippage if no orderbook data.
        """
        if self.ob_cache:
            vwap = self.ob_cache.vwap_fill(exchange, symbol, size_usdt, side)
            if vwap is not None and vwap > 0:
                return vwap
        return 0.0  # signal to use fallback

    async def execute_entry(
        self, signal: Signal, size_usdt: float, leverage: int,
    ) -> tuple[OrderResult, OrderResult]:
        """Simulate both legs of entry with realistic VWAP slippage."""
        slip_flat = self.slippage_pct

        if signal.direction == Direction.LONG_BYBIT:
            # Long Bybit (buy at ask), Short Gate.io (sell at bid)
            bybit_base = signal.price_bybit.ask
            gateio_base = signal.price_gateio.bid
            bybit_side = "buy"
            gateio_side = "sell"
            long_exchange = "bybit"
            short_exchange = "gateio"
        else:
            # Long Gate.io (buy at ask), Short Bybit (sell at bid)
            gateio_base = signal.price_gateio.ask
            bybit_base = signal.price_bybit.bid
            bybit_side = "sell"
            gateio_side = "buy"
            long_exchange = "gateio"
            short_exchange = "bybit"

        # --- VWAP-based fill (realistic) ---
        # Long leg: buy at ask side, short leg: sell at bid side
        if long_exchange == "bybit":
            bybit_price = await self._vwap_fill_price("bybit", signal.symbol, "ask", size_usdt)
            gateio_price = await self._vwap_fill_price("gateio", signal.symbol, "bid", size_usdt)
        else:
            gateio_price = await self._vwap_fill_price("gateio", signal.symbol, "ask", size_usdt)
            bybit_price = await self._vwap_fill_price("bybit", signal.symbol, "bid", size_usdt)

        # Fallback to flat slippage if VWAP unavailable
        if bybit_price <= 0:
            bybit_price = bybit_base * (1 + slip_flat) if bybit_side == "buy" else bybit_base * (1 - slip_flat)
        if gateio_price <= 0:
            gateio_price = gateio_base * (1 + slip_flat) if gateio_side == "buy" else gateio_base * (1 - slip_flat)

        # Use the LONG leg's entry price for quantity calculation
        long_price = bybit_price if long_exchange == "bybit" else gateio_price
        quantity = size_usdt / long_price

        fee_bybit = size_usdt * self.taker_fee_bybit
        fee_gateio = size_usdt * self.taker_fee_gateio
        total_fees = fee_bybit + fee_gateio

        # Margin required: size / leverage (simplified)
        margin = size_usdt / leverage
        if margin + total_fees > self.balance:
            err = OrderResult(
                success=False, exchange="bybit", symbol=signal.symbol,
                side=bybit_side, price=0, quantity=0, fee=0,
                error="insufficient_balance",
            )
            return err, err

        self.balance -= total_fees

        bybit_result = OrderResult(
            success=True,
            exchange="bybit",
            symbol=signal.symbol,
            side=bybit_side,
            price=bybit_price,
            quantity=quantity,
            fee=fee_bybit,
            order_id=f"paper_bybit_{int(time.time()*1000)}",
            latency_ms=0,
        )
        gateio_result = OrderResult(
            success=True,
            exchange="gateio",
            symbol=signal.symbol,
            side=gateio_side,
            price=gateio_price,
            quantity=quantity,
            fee=fee_gateio,
            order_id=f"paper_gateio_{int(time.time()*1000)}",
            latency_ms=0,
        )
        return bybit_result, gateio_result

    async def execute_exit(
        self,
        position: PositionRecord,
        exit_bybit: PriceData,
        exit_gateio: PriceData,
    ) -> tuple[OrderResult, OrderResult]:
        """Simulate exit with realistic VWAP slippage."""
        slip_flat = self.slippage_pct

        if position.direction == Direction.LONG_BYBIT.value:
            entry_qty = position.size_usdt / position.entry_price_bybit
        else:
            entry_qty = position.size_usdt / position.entry_price_gateio

        exit_size_usdt = entry_qty * (
            exit_bybit.bid if position.direction == Direction.LONG_BYBIT.value else exit_bybit.ask
        )

        if position.direction == Direction.LONG_BYBIT.value:
            # Close long at bid, close short at ask
            bybit_exit_price = await self._vwap_fill_price("bybit", position.symbol, "bid", exit_size_usdt)
            gateio_exit_price = await self._vwap_fill_price("gateio", position.symbol, "ask", exit_size_usdt)
            bybit_side = "sell"
            gateio_side = "buy"
            pnl_bybit = (bybit_exit_price - position.entry_price_bybit) * entry_qty
            pnl_gateio = (position.entry_price_gateio - gateio_exit_price) * entry_qty
        else:
            # Close short at ask on Bybit, close long at bid on Gate.io
            bybit_exit_price = await self._vwap_fill_price("bybit", position.symbol, "ask", exit_size_usdt)
            gateio_exit_price = await self._vwap_fill_price("gateio", position.symbol, "bid", exit_size_usdt)
            bybit_side = "buy"
            gateio_side = "sell"
            pnl_bybit = (position.entry_price_bybit - bybit_exit_price) * entry_qty
            pnl_gateio = (gateio_exit_price - position.entry_price_gateio) * entry_qty

        # Fallback to flat slippage if VWAP unavailable
        if bybit_exit_price <= 0:
            bybit_exit_price = (
                exit_bybit.bid * (1 - slip_flat)
                if position.direction == Direction.LONG_BYBIT.value
                else exit_bybit.ask * (1 + slip_flat)
            )
        if gateio_exit_price <= 0:
            gateio_exit_price = (
                exit_gateio.ask * (1 + slip_flat)
                if position.direction == Direction.LONG_BYBIT.value
                else exit_gateio.bid * (1 - slip_flat)
            )

        exit_size_bybit = entry_qty * bybit_exit_price
        exit_size_gateio = entry_qty * gateio_exit_price

        fee_bybit = exit_size_bybit * self.taker_fee_bybit
        fee_gateio = exit_size_gateio * self.taker_fee_gateio
        total_fees = fee_bybit + fee_gateio

        gross_pnl = pnl_bybit + pnl_gateio
        net_pnl = gross_pnl - total_fees
        self.balance += net_pnl

        bybit_result = OrderResult(
            success=True,
            exchange="bybit",
            symbol=position.symbol,
            side=bybit_side,
            price=bybit_exit_price,
            quantity=entry_qty,
            fee=fee_bybit,
            order_id=f"paper_bybit_exit_{int(time.time()*1000)}",
            latency_ms=0,
        )
        gateio_result = OrderResult(
            success=True,
            exchange="gateio",
            symbol=position.symbol,
            side=gateio_side,
            price=gateio_exit_price,
            quantity=entry_qty,
            fee=fee_gateio,
            order_id=f"paper_gateio_exit_{int(time.time()*1000)}",
            latency_ms=0,
        )
        return bybit_result, gateio_result

    def get_balance(self) -> float:
        return self.balance
