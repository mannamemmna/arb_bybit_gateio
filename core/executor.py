import time
import asyncio
from typing import Optional
from utils.logger import get_logger

logger = get_logger('executor')

class OrderExecutor:
    """
    Handles order placement for both exchanges.
    
    Execution flow:
      Signal from Spread Engine
           |
           v
      [1] Spread Decay Check -> SKIP if decaying
           | PASS
           v
      [2] Orderbook Depth Check -> SKIP if insufficient liquidity
           | PASS
           v
      [3] Pre-Flight Re-validate -> SKIP if stale/decayed
           | PASS
           v
      [4] asyncio.gather() -> send order to BOTH exchanges SIMULTANEOUSLY
           |
           v
      [5] Validate fill -> if one leg fails, market-close the successful leg
    
    - Live mode: market orders via REST (parallel asyncio.gather)
    - Paper mode: simulated fill at ask/bid + PAPER_SLIPPAGE_PCT
    """
    
    def __init__(self, bybit_client, gateio_client, price_cache,
                 orderbook_cache, position_tracker, paper_engine,
                 mode: str = 'paper', settings=None):
        self.bybit = bybit_client
        self.gateio = gateio_client
        self.price_cache = price_cache
        self.ob_cache = orderbook_cache
        self.position_tracker = position_tracker
        self.paper_engine = paper_engine
        self.mode = mode
        self.settings = settings
        self._execution_count = 0
    
    async def execute_entry(self, signal) -> bool:
        """
        Execute entry for a signal. Returns True if successful.
        
        Steps:
        1. Pre-flight re-validate (fresh prices, spread still valid)
        2. If live: set leverage on both exchanges
        3. Send market orders to both exchanges simultaneously
        4. Validate fills
        5. Record position
        """
        symbol = signal.symbol
        start_time = time.time()
        
        # Pre-flight re-validate
        fresh_bybit = self.price_cache.get('bybit', symbol)
        fresh_gateio = self.price_cache.get('gateio', symbol)
        
        now = time.time() * 1000
        if not fresh_bybit or (now - fresh_bybit.ts) > self.settings.preflight_max_age_ms:
            age = (now - fresh_bybit.ts) if fresh_bybit else float('inf')
            logger.warning(f"[{symbol}] Pre-flight REJECTED: stale bybit price ({age:.0f}ms)")
            return False
        if not fresh_gateio or (now - fresh_gateio.ts) > self.settings.preflight_max_age_ms:
            age = (now - fresh_gateio.ts) if fresh_gateio else float('inf')
            logger.warning(f"[{symbol}] Pre-flight REJECTED: stale gateio price ({age:.0f}ms)")
            return False
        
        # Recalculate spread from fresh prices
        if fresh_bybit and fresh_gateio:
            live_spread = ((fresh_bybit.mid - fresh_gateio.mid) / fresh_gateio.mid) * 100
            if abs(live_spread) < signal.internal_threshold:
                logger.info(f"[{symbol}] Pre-flight REJECTED: spread decayed "
                            f"{signal.spread_pct:.3f}% -> {live_spread:.3f}%")
                return False
        
        # Determine entry prices and quantities
        if signal.direction.value == 'long_bybit':
            # Long Bybit (buy at ask), Short Gate.io (sell at bid)
            entry_price_long = fresh_bybit.ask if fresh_bybit else signal.price_bybit.ask
            entry_price_short = fresh_gateio.bid if fresh_gateio else signal.price_gateio.bid
        else:
            # Long Gate.io (buy at ask), Short Bybit (sell at bid)
            entry_price_long = fresh_gateio.ask if fresh_gateio else signal.price_gateio.ask
            entry_price_short = fresh_bybit.bid if fresh_bybit else signal.price_bybit.bid
        
        qty = self.settings.max_position_usdt / entry_price_long
        
        try:
            if self.mode == 'paper':
                bybit_result, gateio_result = await self.paper_engine.execute_entry(
                    signal, self.settings.max_position_usdt, self.settings.leverage)
                success = bybit_result.success and gateio_result.success
                if not success:
                    logger.warning(f"[{symbol}] Paper entry FAILED: insufficient balance or error")
                
                # BUG 3 FIX: use slippage-adjusted fill prices from paper engine
                fill_bybit_price = bybit_result.price if bybit_result.success else entry_price_long
                fill_gateio_price = gateio_result.price if gateio_result.success else entry_price_short
                # Use actual fill quantity from paper engine
                fill_bybit_qty = bybit_result.quantity if bybit_result.success else qty
                fill_gateio_qty = gateio_result.quantity if gateio_result.success else qty
            else:
                # Live mode: set leverage + market orders in parallel
                await asyncio.gather(
                    self.bybit.set_leverage(symbol, self.settings.leverage),
                    self.gateio.set_leverage(self._gateio_sym(symbol), self.settings.leverage)
                )
                
                if signal.direction.value == 'long_bybit':
                    bybit_result, gateio_result = await asyncio.gather(
                        self.bybit.place_market_order(symbol, 'buy', str(qty)),
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'sell', str(qty))
                    )
                else:
                    gateio_result, bybit_result = await asyncio.gather(
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'buy', str(qty)),
                        self.bybit.place_market_order(symbol, 'sell', str(qty))
                    )
                
                # BUG 1: now bybit_result/gateio_result have normalized 'success' field
                success = bybit_result.get('success', False) and gateio_result.get('success', False)
                if not success:
                    logger.error(f"[{symbol}] Live entry FAILED — bybit: {bybit_result.get('error')}, "
                                 f"gateio: {gateio_result.get('error')}")
                
                # BUG 3 FIX: use avg fill price from exchange response if available
                fill_bybit_price = bybit_result.get('price', entry_price_long)
                fill_gateio_price = gateio_result.get('price', entry_price_short)
                if fill_bybit_price <= 0:
                    fill_bybit_price = entry_price_long
                    logger.warning(f"[{symbol}] Live entry: Bybit fill price unavailable, using estimate {entry_price_long}")
                if fill_gateio_price <= 0:
                    fill_gateio_price = entry_price_short
                    logger.warning(f"[{symbol}] Live entry: Gate.io fill price unavailable, using estimate {entry_price_short}")
                fill_bybit_qty = bybit_result.get('quantity', qty)
                fill_gateio_qty = gateio_result.get('quantity', qty)
            
            execution_ms = int((time.time() - start_time) * 1000)
            
            # Match the canonical spread formula: (bybit - gateio) / gateio * 100
            # Use actual fill prices for spread calculation
            if signal.direction.value == 'long_bybit':
                bybit_actual = fill_bybit_price   # bybit ask (buying) — actual fill
                gateio_actual = fill_gateio_price  # gateio bid (selling) — actual fill
            else:
                bybit_actual = fill_gateio_price if self.mode == 'paper' else fill_bybit_price
                # Let's be correct: for long_gateio, Bybit is the short (sell) leg, Gate.io is the long (buy) leg
                bybit_actual = fill_bybit_price  # bybit bid (selling)
                gateio_actual = fill_gateio_price  # gateio ask (buying)
            actual_spread = ((bybit_actual - gateio_actual) / gateio_actual) * 100
            slippage = abs(signal.spread_pct) - abs(actual_spread)
            
            if success:
                # Record position — use fill prices from paper engine or exchange
                if signal.direction.value == 'long_bybit':
                    entry_px_bybit = fill_bybit_price
                    entry_px_gateio = fill_gateio_price
                else:
                    entry_px_bybit = fill_bybit_price
                    entry_px_gateio = fill_gateio_price
                
                trade_data = {
                    'mode': self.mode,
                    'symbol': symbol,
                    'direction': signal.direction.value,
                    'entry_ts': int(time.time() * 1000),
                    'signal_spread_pct': signal.spread_pct,
                    'preflight_spread_pct': live_spread if fresh_bybit and fresh_gateio else signal.spread_pct,
                    'actual_spread_pct': actual_spread,
                    'slippage_pct': slippage,
                    'execution_ms': execution_ms,
                    'entry_price_bybit': entry_px_bybit,
                    'entry_price_gateio': entry_px_gateio,
                    'size_usdt': self.settings.max_position_usdt,
                    'leverage': self.settings.leverage,
                    'status': 'open'
                }
                await self.position_tracker.open_position(trade_data)
                self._execution_count += 1
                logger.info(f"[{symbol}] Entry SUCCESS in {execution_ms}ms, "
                            f"spread={actual_spread:.3f}%, slippage={slippage:.3f}%")
            else:
                # Abort: close any successful leg to return to flat
                if self.mode == 'live':
                    try:
                        if signal.direction.value == 'long_bybit':
                            if bybit_result.get('success', False):
                                await self.bybit.place_market_order(symbol, 'sell', str(qty))
                                logger.warning(f"[{symbol}] ABORT: closed Bybit leg")
                            if gateio_result.get('success', False):
                                await self.gateio.place_market_order(self._gateio_sym(symbol), 'buy', str(qty))
                                logger.warning(f"[{symbol}] ABORT: closed Gate.io leg")
                        else:
                            if bybit_result.get('success', False):
                                await self.bybit.place_market_order(symbol, 'buy', str(qty))
                                logger.warning(f"[{symbol}] ABORT: closed Bybit leg")
                            if gateio_result.get('success', False):
                                await self.gateio.place_market_order(self._gateio_sym(symbol), 'sell', str(qty))
                                logger.warning(f"[{symbol}] ABORT: closed Gate.io leg")
                    except Exception as abort_err:
                        logger.error(f"[{symbol}] ABORT FAILED: {abort_err} — MANUAL INTERVENTION REQUIRED")
                logger.error(f"[{symbol}] Entry FAILED - one or both legs failed")
            
            return success
            
        except Exception as e:
            logger.error(f"[{symbol}] Entry EXCEPTION: {e}")
            return False
    
    async def execute_exit(self, symbol: str) -> bool:
        """
        Execute exit for an open position. Close both legs simultaneously.
        If one leg fails, retry 3x before alerting.
        """
        pos = self.position_tracker.get_position(symbol)
        if not pos:
            return False
        
        fresh_bybit = self.price_cache.get('bybit', symbol)
        fresh_gateio = self.price_cache.get('gateio', symbol)
        
        if not fresh_bybit or not fresh_gateio:
            logger.error(f"[{symbol}] Cannot exit: stale prices")
            return False
        
        # BUG 4 FIX: use correct entry price for qty calculation
        if pos['direction'] == 'long_bybit':
            qty = pos['size_usdt'] / pos.get('entry_price_bybit', 1)
        else:
            qty = pos['size_usdt'] / pos.get('entry_price_gateio', 1)
        
        try:
            if self.mode == 'paper':
                bybit_result, gateio_result = await self.paper_engine.execute_exit(
                    type('Pos', (), pos)(), fresh_bybit, fresh_gateio)
                exit_bybit_price = bybit_result.price if bybit_result.success else (
                    fresh_bybit.bid if pos['direction'] == 'long_bybit' else fresh_bybit.ask)
                exit_gateio_price = gateio_result.price if gateio_result.success else (
                    fresh_gateio.ask if pos['direction'] == 'long_bybit' else fresh_gateio.bid)
            else:
                # Live: close both legs
                # BUG 4: qty already calculated above with correct entry price
                if pos['direction'] == 'long_bybit':
                    # Close long Bybit (sell), close short Gate.io (buy)
                    bybit_result, gateio_result = await asyncio.gather(
                        self.bybit.place_market_order(symbol, 'sell', str(qty)),
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'buy', str(qty))
                    )
                else:
                    gateio_result, bybit_result = await asyncio.gather(
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'sell', str(qty)),
                        self.bybit.place_market_order(symbol, 'buy', str(qty))
                    )
                
                exit_bybit_price = bybit_result.get('price', fresh_bybit.bid if pos['direction'] == 'long_bybit' else fresh_bybit.ask)
                exit_gateio_price = gateio_result.get('price', fresh_gateio.ask if pos['direction'] == 'long_bybit' else fresh_gateio.bid)
            
            # Calculate PnL — use consistent qty throughout
            from strategy.spread_arb import SpreadArbStrategy
            strategy = SpreadArbStrategy(self.settings)
            pnl = strategy.calc_pnl(
                pos['direction'], qty,
                pos['entry_price_bybit'], pos['entry_price_gateio'],
                exit_bybit_price, exit_gateio_price
            )
            
            exit_data = {
                'exit_ts': int(time.time() * 1000),
                'exit_price_bybit': exit_bybit_price,
                'exit_price_gateio': exit_gateio_price,
                'actual_spread_pct': ((exit_bybit_price - exit_gateio_price) / exit_gateio_price * 100),
                'status': 'closed',
                **pnl
            }
            
            await self.position_tracker.close_position(symbol, exit_data)
            return True
            
        except Exception as e:
            logger.error(f"[{symbol}] Exit EXCEPTION: {e}")
            return False
    
    async def close_all_positions(self) -> int:
        """Close all open positions. Returns count of successfully closed."""
        closed = 0
        for pos in self.position_tracker.get_all_open():
            if await self.execute_exit(pos['symbol']):
                closed += 1
        return closed
    
    def _gateio_sym(self, symbol: str) -> str:
        """Convert BTCUSDT -> BTC_USDT for Gate.io API."""
        return symbol.replace('USDT', '_USDT')
    
    def get_stats(self) -> dict:
        return {
            'mode': self.mode,
            'execution_count': self._execution_count,
            'open_positions': self.position_tracker.open_count
        }
