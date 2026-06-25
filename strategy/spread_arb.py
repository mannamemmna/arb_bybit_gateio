from utils.logger import get_logger

logger = get_logger('strategy')

class SpreadArbStrategy:
    """
    Spread arbitrage strategy logic.
    
    Entry conditions (ALL must be met):
      1. |spread_pct| >= internal_threshold
      2. Spread not decaying > PREFLIGHT_SPREAD_DECAY from recent peak
      3. Orderbook depth check PASS (if enabled)
      4. Pre-flight: fresh prices (< PREFLIGHT_MAX_AGE_MS), spread still valid
      5. Pair has no open position
      6. Open positions < MAX_OPEN_POSITIONS
    
    Exit conditions:
      |spread_pct| <= SPREAD_EXIT_THRESHOLD (spread converged)
    
    Position sizing:
      size_usdt = MAX_POSITION_USDT
      quantity = size_usdt / entry_price  (per leg)
      leverage = LEVERAGE
    
    PnL calculation (per pair):
      long_pnl = (exit_price_long - entry_price_long) * qty
      short_pnl = (entry_price_short - exit_price_short) * qty
      gross_pnl = long_pnl + short_pnl
      fee_total = sum of all legs * respective taker fee
      net_pnl = gross_pnl - fee_total
    """
    
    def __init__(self, settings):
        self.settings = settings
    
    def calc_position_size(self, entry_price: float) -> float:
        """Calculate quantity from USDT size."""
        return self.settings.max_position_usdt / entry_price
    
    def calc_pnl(self, direction: str, qty: float,
                 entry_bybit: float, entry_gateio: float,
                 exit_bybit: float, exit_gateio: float) -> dict:
        """Calculate PnL for a closed position."""
        if direction == 'long_bybit':
            # Long Bybit, Short Gate.io
            long_pnl = (exit_bybit - entry_bybit) * qty
            short_pnl = (entry_gateio - exit_gateio) * qty
        else:
            # Long Gate.io, Short Bybit
            long_pnl = (exit_gateio - entry_gateio) * qty
            short_pnl = (entry_bybit - exit_bybit) * qty
        
        gross_pnl = long_pnl + short_pnl
        
        # Fees: each leg pays taker fee on open AND close
        bybit_fee = (entry_bybit + exit_bybit) * qty * self.settings.taker_fee_bybit
        gateio_fee = (entry_gateio + exit_gateio) * qty * self.settings.taker_fee_gateio
        fee_total = bybit_fee + gateio_fee
        
        net_pnl = gross_pnl - fee_total
        
        return {
            'gross_pnl': gross_pnl,
            'fee_total': fee_total,
            'net_pnl': net_pnl,
            'bybit_fee': bybit_fee,
            'gateio_fee': gateio_fee
        }
    
    def calc_slippage(self, signal_spread: float, actual_spread: float) -> float:
        """Calculate slippage: difference between signal spread and actual fill spread."""
        return signal_spread - actual_spread
