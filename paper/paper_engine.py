import time
from database.models import Signal, PriceData, OrderResult, PositionRecord, Direction


class PaperEngine:
    def __init__(
        self,
        initial_balance: float,
        slippage_pct: float,
        taker_fee_bybit: float,
        taker_fee_gateio: float,
    ):
        self.balance = initial_balance
        self.slippage_pct = slippage_pct
        self.taker_fee_bybit = taker_fee_bybit
        self.taker_fee_gateio = taker_fee_gateio
        self.initial_balance = initial_balance

    async def execute_entry(
        self, signal: Signal, size_usdt: float, leverage: int
    ) -> tuple[OrderResult, OrderResult]:
        """Simulate both legs of entry. Returns (bybit_result, gateio_result)."""
        slippage = self.slippage_pct  # Already in decimal form (0.0005 = 0.05%)

        if signal.direction == Direction.LONG_BYBIT:
            # Long Bybit (buy at ask), Short Gate.io (sell at bid)
            bybit_price = signal.price_bybit.ask * (1 + slippage)
            gateio_price = signal.price_gateio.bid * (1 - slippage)
            bybit_side = "buy"
            gateio_side = "sell"
        else:
            # Long Gate.io (buy at ask), Short Bybit (sell at bid)
            gateio_price = signal.price_gateio.ask * (1 + slippage)
            bybit_price = signal.price_bybit.bid * (1 - slippage)
            bybit_side = "sell"
            gateio_side = "buy"

        # Use the LONG leg's entry price for quantity calculation
        long_price = bybit_price if signal.direction == Direction.LONG_BYBIT else gateio_price
        quantity = size_usdt / long_price

        fee_bybit = size_usdt * self.taker_fee_bybit
        fee_gateio = size_usdt * self.taker_fee_gateio
        total_fees = fee_bybit + fee_gateio

        # Margin required: size / leverage (simplified)
        margin = size_usdt / leverage
        if margin + total_fees > self.balance:
            # Not enough balance
            err = OrderResult(
                success=False, exchange="bybit", symbol=signal.symbol,
                side=bybit_side, price=0, quantity=0, fee=0,
                error="insufficient_balance",
            )
            return err, err

        self.balance -= total_fees  # Deduct fees from balance

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
        """Simulate both legs of exit. Returns (bybit_result, gateio_result)."""
        slippage = self.slippage_pct  # Already in decimal form (0.0005 = 0.05%)

        entry_qty = position.size_usdt / position.entry_price_bybit

        if position.direction == Direction.LONG_BYBIT.value:
            # Close long at bid, close short at ask
            bybit_exit_price = exit_bybit.bid * (1 - slippage)
            gateio_exit_price = exit_gateio.ask * (1 + slippage)
            bybit_side = "sell"
            gateio_side = "buy"
            # PnL: long profit on Bybit, short profit on Gate.io
            pnl_bybit = (bybit_exit_price - position.entry_price_bybit) * entry_qty
            pnl_gateio = (position.entry_price_gateio - gateio_exit_price) * entry_qty
        else:
            # Close short at ask on Bybit, close long at bid on Gate.io
            bybit_exit_price = exit_bybit.ask * (1 + slippage)
            gateio_exit_price = exit_gateio.bid * (1 - slippage)
            bybit_side = "buy"
            gateio_side = "sell"
            pnl_bybit = (position.entry_price_bybit - bybit_exit_price) * entry_qty
            pnl_gateio = (gateio_exit_price - position.entry_price_gateio) * entry_qty

        exit_size_bybit = entry_qty * bybit_exit_price
        exit_size_gateio = entry_qty * gateio_exit_price

        fee_bybit = exit_size_bybit * self.taker_fee_bybit
        fee_gateio = exit_size_gateio * self.taker_fee_gateio
        total_fees = fee_bybit + fee_gateio

        gross_pnl = pnl_bybit + pnl_gateio
        net_pnl = gross_pnl - total_fees

        # Return fees + pnl to balance
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
