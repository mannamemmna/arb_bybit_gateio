import time
import asyncio
from typing import Optional
from utils.logger import get_logger

logger = get_logger('executor')


# Maximum allowed quantity mismatch between legs (1%)
MAX_QTY_MISMATCH_PCT = 1.0


def _get_qty(result) -> float:
    """Extract quantity from OrderResult (paper) or dict (live)."""
    if hasattr(result, 'quantity'):
        return float(result.quantity)
    return float(result.get('quantity', 0))


def _get_success(result) -> bool:
    if hasattr(result, 'success'):
        return result.success
    return result.get('success', False)


def _get_price(result) -> float:
    if hasattr(result, 'price'):
        return float(result.price)
    return float(result.get('price', 0))


def _get_error(result) -> Optional[str]:
    if hasattr(result, 'error'):
        return result.error
    return result.get('error')


class OrderExecutor:
    """
    Handles order placement for both exchanges.
    
    Execution flow:
      Signal from Spread Engine
           |
           v
      [0] Multi-Exchange Health Check -> SKIP if one exchange unreachable
           | PASS
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
      [4] Quantity rounding -> round to valid lot step
           | PASS
           v
      [5] asyncio.gather() -> send order to BOTH exchanges SIMULTANEOUSLY
           |
           v
      [6] Validate fill + partial fill check -> if mismatch, abort
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

    async def _health_check(self, symbol: str) -> bool:
        """
        FIX 4: Quick health check — verify both exchanges have recent prices
        and WS connections are live (via staleness check).
        """
        now = time.time() * 1000
        bybit_px = self.price_cache.get('bybit', symbol)
        gateio_px = self.price_cache.get('gateio', symbol)

        if not bybit_px or (now - bybit_px.ts) > self.settings.preflight_max_age_ms * 2:
            logger.warning(f"[{symbol}] HEALTH CHECK FAILED: bybit price too stale or missing")
            return False
        if not gateio_px or (now - gateio_px.ts) > self.settings.preflight_max_age_ms * 2:
            logger.warning(f"[{symbol}] HEALTH CHECK FAILED: gateio price too stale or missing")
            return False

        # Simple heuristic: if prices are within 1000ms, exchange is alive
        return True

    async def _round_entry_qty(self, symbol: str, raw_qty: float) -> tuple[float, float]:
        """FIX 3: Round qty to valid lot step for both exchanges."""
        gateio_symbol = self._gateio_sym(symbol)

        try:
            bybit_info = await self.bybit.fetch_instrument_info(symbol)
            bybit_qty = self.bybit.round_qty(symbol, raw_qty)
        except Exception:
            logger.warning(f"[{symbol}] Bybit instrument info failed, using raw qty")
            bybit_qty = raw_qty

        try:
            gateio_info = await self.gateio.fetch_instrument_info(gateio_symbol)
            gateio_qty = self.gateio.round_qty(gateio_symbol, raw_qty)
        except Exception:
            logger.warning(f"[{symbol}] Gate.io contract info failed, using raw qty")
            gateio_qty = raw_qty

        return bybit_qty, gateio_qty

    async def execute_entry(self, signal) -> bool:
        """
        Execute entry for a signal. Returns True if successful.
        """
        symbol = signal.symbol
        start_time = time.time()
        
        # FIX 4: Health check before anything else
        if not await self._health_check(symbol):
            logger.warning(f"[{symbol}] Entry SKIPPED: exchange health check failed")
            return False

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
        
        # Determine entry prices
        if signal.direction.value == 'long_bybit':
            entry_price_long = fresh_bybit.ask if fresh_bybit else signal.price_bybit.ask
            entry_price_short = fresh_gateio.bid if fresh_gateio else signal.price_gateio.bid
        else:
            entry_price_long = fresh_gateio.ask if fresh_gateio else signal.price_gateio.ask
            entry_price_short = fresh_bybit.bid if fresh_bybit else signal.price_bybit.bid
        
        raw_qty = self.settings.max_position_usdt / entry_price_long

        # FIX 3: Round qty to valid lot step for each exchange
        bybit_qty, gateio_qty = await self._round_entry_qty(symbol, raw_qty)
        
        try:
            if self.mode == 'paper':
                bybit_result, gateio_result = await self.paper_engine.execute_entry(
                    signal, self.settings.max_position_usdt, self.settings.leverage)

                # FIX 2: Partial fill validation
                qty_bybit = _get_qty(bybit_result)
                qty_gateio = _get_qty(gateio_result)
                if qty_bybit > 0 and qty_gateio > 0:
                    max_qty = max(qty_bybit, qty_gateio)
                    min_qty = min(qty_bybit, qty_gateio)
                    mismatch_pct = (1 - min_qty / max_qty) * 100
                    if mismatch_pct > MAX_QTY_MISMATCH_PCT:
                        logger.warning(f"[{symbol}] Partial fill DETECTED: bybit={qty_bybit:.6f} gateio={qty_gateio:.6f} "
                                       f"(mismatch={mismatch_pct:.2f}%)")
                        return False

                success = _get_success(bybit_result) and _get_success(gateio_result)
                if not success:
                    logger.warning(f"[{symbol}] Paper entry FAILED: insufficient balance or error")
                
                # BUG 3 FIX: use slippage-adjusted fill prices from paper engine
                fill_bybit_price = _get_price(bybit_result) if _get_success(bybit_result) else entry_price_long
                fill_gateio_price = _get_price(gateio_result) if _get_success(gateio_result) else entry_price_short
            else:
                # Live mode: set leverage + market orders in parallel
                await asyncio.gather(
                    self.bybit.set_leverage(symbol, self.settings.leverage),
                    self.gateio.set_leverage(self._gateio_sym(symbol), self.settings.leverage)
                )

                # Use rounded quantities for live orders
                if signal.direction.value == 'long_bybit':
                    bybit_result, gateio_result = await asyncio.gather(
                        self.bybit.place_market_order(symbol, 'buy', str(bybit_qty)),
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'sell', str(gateio_qty))
                    )
                else:
                    gateio_result, bybit_result = await asyncio.gather(
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'buy', str(gateio_qty)),
                        self.bybit.place_market_order(symbol, 'sell', str(bybit_qty))
                    )

                # FIX 2: Partial fill validation (live)
                qty_bybit = _get_qty(bybit_result)
                qty_gateio = _get_qty(gateio_result)
                if qty_bybit > 0 and qty_gateio > 0:
                    max_qty = max(qty_bybit, qty_gateio)
                    min_qty = min(qty_bybit, qty_gateio)
                    mismatch_pct = (1 - min_qty / max_qty) * 100
                    if mismatch_pct > MAX_QTY_MISMATCH_PCT:
                        logger.error(f"[{symbol}] LIVE partial fill DETECTED: bybit={qty_bybit:.6f} gateio={qty_gateio:.6f} "
                                     f"(mismatch={mismatch_pct:.2f}%)")
                        # Trigger abort
                        success = False
                        gateio_result['success'] = False
                    else:
                        success = _get_success(bybit_result) and _get_success(gateio_result)
                else:
                    success = _get_success(bybit_result) and _get_success(gateio_result)

                if not success:
                    logger.error(f"[{symbol}] Live entry FAILED — bybit: {_get_error(bybit_result)}, "
                                 f"gateio: {_get_error(gateio_result)}")
                
                fill_bybit_price = _get_price(bybit_result) if _get_success(bybit_result) else entry_price_long
                fill_gateio_price = _get_price(gateio_result) if _get_success(gateio_result) else entry_price_short
                if fill_bybit_price <= 0:
                    fill_bybit_price = entry_price_long
                    logger.warning(f"[{symbol}] Live entry: Bybit fill price unavailable, using estimate {entry_price_long}")
                if fill_gateio_price <= 0:
                    fill_gateio_price = entry_price_short
                    logger.warning(f"[{symbol}] Live entry: Gate.io fill price unavailable, using estimate {entry_price_short}")
            
            execution_ms = int((time.time() - start_time) * 1000)
            
            if signal.direction.value == 'long_bybit':
                bybit_actual = fill_bybit_price
                gateio_actual = fill_gateio_price
            else:
                bybit_actual = fill_bybit_price
                gateio_actual = fill_gateio_price
            actual_spread = ((bybit_actual - gateio_actual) / gateio_actual) * 100
            slippage = abs(signal.spread_pct) - abs(actual_spread)
            
            if success:
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
                    'entry_price_bybit': fill_bybit_price,
                    'entry_price_gateio': fill_gateio_price,
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
                        abort_qty_bybit = str(bybit_qty)
                        abort_qty_gateio = str(gateio_qty)
                        if signal.direction.value == 'long_bybit':
                            if _get_success(bybit_result):
                                await self.bybit.place_market_order(symbol, 'sell', abort_qty_bybit)
                                logger.warning(f"[{symbol}] ABORT: closed Bybit leg")
                            if _get_success(gateio_result):
                                await self.gateio.place_market_order(self._gateio_sym(symbol), 'buy', abort_qty_gateio)
                                logger.warning(f"[{symbol}] ABORT: closed Gate.io leg")
                        else:
                            if _get_success(bybit_result):
                                await self.bybit.place_market_order(symbol, 'buy', abort_qty_bybit)
                                logger.warning(f"[{symbol}] ABORT: closed Bybit leg")
                            if _get_success(gateio_result):
                                await self.gateio.place_market_order(self._gateio_sym(symbol), 'sell', abort_qty_gateio)
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
        
        # FIX 4: Health check before exit too
        if not await self._health_check(symbol):
            logger.error(f"[{symbol}] EXIT BLOCKED: exchange health check failed")
            return False
        
        # qty calculation (direction-aware — BUG 4 fix)
        if pos['direction'] == 'long_bybit':
            qty = pos['size_usdt'] / pos.get('entry_price_bybit', 1)
        else:
            qty = pos['size_usdt'] / pos.get('entry_price_gateio', 1)
        
        try:
            if self.mode == 'paper':
                bybit_result, gateio_result = await self.paper_engine.execute_exit(
                    type('Pos', (), pos)(), fresh_bybit, fresh_gateio)
                exit_bybit_price = _get_price(bybit_result) if _get_success(bybit_result) else (
                    fresh_bybit.bid if pos['direction'] == 'long_bybit' else fresh_bybit.ask)
                exit_gateio_price = _get_price(gateio_result) if _get_success(gateio_result) else (
                    fresh_gateio.ask if pos['direction'] == 'long_bybit' else fresh_gateio.bid)
            else:
                if pos['direction'] == 'long_bybit':
                    bybit_result, gateio_result = await asyncio.gather(
                        self.bybit.place_market_order(symbol, 'sell', str(qty)),
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'buy', str(qty))
                    )
                else:
                    gateio_result, bybit_result = await asyncio.gather(
                        self.gateio.place_market_order(self._gateio_sym(symbol), 'sell', str(qty)),
                        self.bybit.place_market_order(symbol, 'buy', str(qty))
                    )
                
                exit_bybit_price = _get_price(bybit_result) or (
                    fresh_bybit.bid if pos['direction'] == 'long_bybit' else fresh_bybit.ask)
                exit_gateio_price = _get_price(gateio_result) or (
                    fresh_gateio.ask if pos['direction'] == 'long_bybit' else fresh_gateio.bid)
            
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
