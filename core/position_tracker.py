import asyncio
from typing import Dict, List, Optional
from utils.logger import get_logger

logger = get_logger('position_tracker')

class PositionTracker:
    """
    In-memory tracker for open positions.
    Synced with database on every trade open/close.
    Supports both paper and live mode.
    """
    
    def __init__(self, db, mode: str = 'paper'):
        self.db = db
        self.mode = mode
        self._open_positions: Dict[str, dict] = {}  # symbol -> position dict
        self._lock = asyncio.Lock()
    
    @property
    def open_count(self) -> int:
        return len(self._open_positions)  # Safe: single-threaded event loop
    
    async def has_open_position(self, symbol: str) -> bool:
        async with self._lock:
            return symbol in self._open_positions
    
    def get_position(self, symbol: str) -> Optional[dict]:
        return self._open_positions.get(symbol)
    
    def get_all_open(self) -> List[dict]:
        return list(self._open_positions.values())
    
    async def open_position(self, trade_data: dict) -> int:
        """
        Record new position opening.
        trade_data should match trades table schema.
        Returns trade_id from database.
        """
        async with self._lock:
            trade_id = await self.db.insert_trade(trade_data)
            trade_data['id'] = trade_id
            self._open_positions[trade_data['symbol']] = trade_data
            logger.info(f"Position opened: {trade_data['symbol']} {trade_data['direction']} "
                        f"size={trade_data['size_usdt']} USDT (id={trade_id})")
            return trade_id
    
    async def close_position(self, symbol: str, exit_data: dict) -> Optional[dict]:
        """
        Record position closing.
        exit_data: {exit_ts, exit_price_bybit, exit_price_gateio, gross_pnl, fee_total, net_pnl, 
                    actual_spread_pct, slippage_pct, execution_ms}
        Returns full trade record.
        """
        async with self._lock:
            pos = self._open_positions.pop(symbol, None)
            if not pos:
                logger.warning(f"Tried to close non-existent position: {symbol}")
                return None
            
            await self.db.update_trade_exit(pos['id'], exit_data)
            pos.update(exit_data)
            pos['status'] = 'closed'
            
            logger.info(f"Position closed: {symbol} net_pnl={exit_data.get('net_pnl', 0):.4f} USDT")
            return pos
    
    async def abort_position(self, symbol: str, reason: str) -> Optional[dict]:
        """Mark position as aborted (one leg failed)."""
        async with self._lock:
            pos = self._open_positions.pop(symbol, None)
            if pos:
                await self.db.update_trade_exit(pos['id'], {'status': 'aborted'})
                pos['status'] = 'aborted'
                logger.error(f"Position ABORTED: {symbol} reason={reason}")
            return pos
    
    async def load_from_db(self):
        """Load open positions from database on startup."""
        trades = await self.db.get_open_trades(self.mode)
        async with self._lock:
            for trade in trades:
                self._open_positions[trade['symbol']] = dict(trade)
        logger.info(f"Loaded {len(trades)} open positions from DB (mode={self.mode})")
    
    def get_summary(self) -> dict:
        return {
            'mode': self.mode,
            'open_count': self.open_count,
            'symbols': list(self._open_positions.keys())
        }
