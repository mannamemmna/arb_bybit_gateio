#!/usr/bin/env python3
"""
Perpetual Arbitrage Bot — Bybit × Gate.io

Entry point. Wires all modules together and manages lifecycle.
"""

import asyncio
import signal
import sys
import time
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import load_settings
from database.db import Database
from exchanges.bybit import BybitClient
from exchanges.gateio import GateioClient
from exchanges.ws_pool import WebSocketPool
from core.price_cache import PriceCache
from core.orderbook_cache import OrderbookCache
from core.scanner import SymbolScanner
from core.spread_engine import SpreadEngine
from core.position_tracker import PositionTracker
from core.executor import OrderExecutor
from strategy.spread_arb import SpreadArbStrategy
from paper.paper_engine import PaperEngine
from telegram_bot.bot import ArbitrageBot
from telegram_bot.notifier import Notifier
from rebalance.rebalancer import RebalanceManager
from utils.logger import setup_logger, get_logger

logger = get_logger('main')


class ArbitrageEngine:
    """
    Main engine that coordinates all components.

    Lifecycle:
    1. Load config
    2. Init database
    3. Init exchange clients
    4. Scan common symbols
    5. Start WS pool (feeds prices)
    6. Start spread engine (generates signals)
    7. Start executor (places orders)
    8. Start Telegram bot
    9. Run until shutdown
    """

    def __init__(self):
        self.settings = load_settings()
        setup_logger(self.settings.log_level, self.settings.log_file)

        # Core components
        self.db = Database()
        self.price_cache = PriceCache(staleness_ms=self.settings.price_staleness_ms)
        self.ob_cache = OrderbookCache()

        # Exchange clients
        self.bybit_client = BybitClient(
            api_key=self.settings.bybit_api_key,
            api_secret=self.settings.bybit_api_secret,
            testnet=self.settings.bybit_testnet,
            ws_url=self.settings.bybit_ws_url,
        )
        self.gateio_client = GateioClient(
            api_key=self.settings.gateio_api_key,
            api_secret=self.settings.gateio_api_secret,
            ws_url=self.settings.gateio_ws_url,
        )

        # Scanner
        self.scanner = SymbolScanner(self.bybit_client, self.gateio_client)

        # Position tracker
        self.position_tracker = None  # Init after db

        # Paper engine
        self.paper_engine = PaperEngine(
            initial_balance=self.settings.paper_initial_balance_usdt,
            slippage_pct=self.settings.paper_slippage_pct,
            taker_fee_bybit=self.settings.taker_fee_bybit,
            taker_fee_gateio=self.settings.taker_fee_gateio,
        )

        # Executor
        self.executor = None  # Init after position_tracker

        # Spread engine
        self.spread_engine = None  # Init after executor

        # WS Pool
        self.ws_pool = WebSocketPool(
            bybit_client=self.bybit_client,
            gateio_client=self.gateio_client,
            max_subs_per_conn=self.settings.ws_max_subs_per_conn,
            heartbeat_sec=self.settings.ws_heartbeat_sec,
            reconnect_delay=self.settings.ws_reconnect_delay_sec,
            max_retries=self.settings.ws_max_retries,
        )

        # Telegram
        self.bot = ArbitrageBot(
            token=self.settings.telegram_bot_token,
            user_id=self.settings.telegram_user_id,
            engine=self,
        )
        self.notifier = Notifier(
            bot_token=self.settings.telegram_bot_token,
            user_id=self.settings.telegram_user_id,
        )

        # Rebalance manager
        self.rebalancer = RebalanceManager(
            bybit_client=self.bybit_client,
            gateio_client=self.gateio_client,
            settings=self.settings,
            db=self.db,
            notifier=self.notifier,
        )

        self.running = False
        self.start_time = None

    async def init(self):
        """Initialize all components."""
        logger.info("Initializing Arbitrage Engine...")

        # Database
        await self.db.init()

        # Position tracker
        self.position_tracker = PositionTracker(self.db, self.settings.trading_mode)
        await self.position_tracker.load_from_db()

        # Executor
        self.executor = OrderExecutor(
            bybit_client=self.bybit_client,
            gateio_client=self.gateio_client,
            price_cache=self.price_cache,
            orderbook_cache=self.ob_cache,
            position_tracker=self.position_tracker,
            paper_engine=self.paper_engine,
            mode=self.settings.trading_mode,
            settings=self.settings,
        )
        # Wire orderbook cache to paper engine for realistic VWAP slippage
        self.paper_engine.ob_cache = self.ob_cache

        # Spread engine
        self.spread_engine = SpreadEngine(
            price_cache=self.price_cache,
            orderbook_cache=self.ob_cache,
            entry_threshold=self.settings.spread_entry_threshold,
            exit_threshold=self.settings.spread_exit_threshold,
            round_trip_fee=self.settings.total_round_trip_fee,
            slippage_buffer=self.settings.slippage_buffer,
            preflight_spread_decay=self.settings.preflight_spread_decay,
            use_orderbook_check=self.settings.use_orderbook_depth_check,
            orderbook_depth=self.settings.orderbook_depth,
            max_position_usdt=self.settings.max_position_usdt,
            position_tracker=self.position_tracker,
            on_signal=self._on_signal,
            on_exit=self._on_exit,
            bybit_client=self.bybit_client,
            gateio_client=self.gateio_client,
        )

        # Wire WS pool callbacks
        self.ws_pool.on_price_update = self._on_price_update
        self.ws_pool.on_ws_event = self._on_ws_event

        # Scan symbols
        await self.scanner.scan()
        logger.info(f"Found {len(self.scanner.common_symbols)} common pairs")

        # Setup Telegram bot
        await self.bot.setup()
        await self.bot.start()

        # Setup rebalance manager
        if self.settings.rebalance_enabled:
            self.rebalancer.set_open_count_getter(lambda: self.position_tracker.open_count)
            self.rebalancer.set_is_executing_getter(lambda: False)  # simplified
            await self.rebalancer.start()
            logger.info("Rebalance manager started")

        logger.info("Engine initialized successfully")

    async def _on_price_update(
        self, exchange: str, symbol: str, bid: float, ask: float, ts: float
    ):
        """Called on every WS price update. Updates cache and triggers spread check."""
        self.price_cache.update(exchange, symbol, bid, ask, ts)
        await self.spread_engine.on_price_update(exchange, symbol)

    async def _on_ws_event(
        self, exchange: str, conn_index: int, event: str, retry_count: int, latency_ms: int
    ):
        """Called on WS connection events."""
        await self.db.insert_ws_health(exchange, conn_index, event, retry_count, latency_ms)

        if event == "disconnected" and retry_count == 1:
            await self.notifier.notify_ws_disconnect(exchange)
        elif event == "failed":
            await self.notifier.notify_ws_reconnect_failed(exchange, retry_count)

    async def _on_signal(self, signal):
        """Called when spread engine generates an entry signal."""
        success = await self.executor.execute_entry(signal)
        if success:
            pos = self.position_tracker.get_position(signal.symbol)
            if pos:
                await self.notifier.notify_trade_open(
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    bybit_price=pos.get("entry_price_bybit", 0),
                    gateio_price=pos.get("entry_price_gateio", 0),
                    spread_pct=signal.spread_pct,
                    size_usdt=self.settings.max_position_usdt,
                )

    async def _on_exit(self, symbol: str, spread_pct: float):
        """Called when spread converges — close the position."""
        pos = self.position_tracker.get_position(symbol)
        if not pos:
            return

        success = await self.executor.execute_exit(symbol)
        if success:
            updated_pos = dict(pos)  # pos already updated by executor
            entry_ts = updated_pos.get("entry_ts", 0)
            exit_ts = updated_pos.get("exit_ts", int(time.time() * 1000))
            duration = int((exit_ts - entry_ts) / 1000)

            await self.notifier.notify_trade_close(
                symbol=symbol,
                direction=updated_pos.get("direction", ""),
                exit_bybit=updated_pos.get("exit_price_bybit", 0),
                exit_gateio=updated_pos.get("exit_price_gateio", 0),
                gross_pnl=updated_pos.get("gross_pnl", 0),
                fee=updated_pos.get("fee_total", 0),
                net_pnl=updated_pos.get("net_pnl", 0),
                duration_sec=duration,
                entry_price_bybit=updated_pos.get("entry_price_bybit"),
                entry_price_gateio=updated_pos.get("entry_price_gateio"),
                signal_spread_pct=updated_pos.get("signal_spread_pct"),
                slippage_pct=updated_pos.get("slippage_pct"),
            )

    async def start(self):
        """Start the engine — begin monitoring and trading."""
        if self.running:
            return

        self.running = True
        self.start_time = time.time()

        # Start WS pool in background
        asyncio.create_task(self.ws_pool.start(self.scanner.common_symbols))

        # Start periodic scanner refresh
        asyncio.create_task(self.scanner.run_periodic(on_update=self._on_new_symbols))

        await self.notifier.notify_engine_start(
            mode=self.settings.trading_mode,
            pair_count=len(self.scanner.common_symbols),
        )

        logger.info("Engine started — monitoring spreads")

    async def stop(self, reason: str = "manual"):
        """Stop engine. Close all positions first if auto=on."""
        if not self.running:
            return

        logger.info(f"Stopping engine: {reason}")

        # Close all positions
        closed = await self.executor.close_all_positions()

        # Stop components
        await self.ws_pool.stop()
        self.scanner.stop()

        self.running = False

        # Calculate session stats
        summary = await self.db.get_trade_summary(self.settings.trading_mode, days=1)

        await self.notifier.notify_engine_stop(
            reason=reason,
            trade_count=summary.get("total_trades", 0),
            net_pnl=summary.get("total_pnl", 0),
            duration_sec=int(time.time() - self.start_time) if self.start_time else 0,
        )

        logger.info(f"Engine stopped. Closed {closed} positions.")

    async def _on_new_symbols(self, new_symbols: list):
        """Called when scanner finds new pairs."""
        if self.running:
            await self.ws_pool.update_symbols(new_symbols)
            await self.notifier.notify_new_pairs(new_symbols)

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down...")
        if self.running:
            await self.stop("shutdown")
        if self.rebalancer:
            await self.rebalancer.stop()
        await self.bot.stop()
        await self.db.close()
        logger.info("Shutdown complete")


async def main():
    engine = ArbitrageEngine()

    # Handle SIGINT/SIGTERM
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(engine.shutdown()))

    try:
        await engine.init()

        # Keep running until shutdown
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        await engine.shutdown()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        await engine.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
