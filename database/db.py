import aiosqlite
import os
import time
from typing import Optional, List


class Database:
    def __init__(self, db_path: str = 'data/arb_bot.db'):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute('PRAGMA journal_mode=WAL')
        await self.db.execute('PRAGMA synchronous=NORMAL')
        await self._create_tables()
        await self.db.commit()

    async def _create_tables(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                mode                 TEXT NOT NULL,
                symbol               TEXT NOT NULL,
                direction            TEXT NOT NULL,
                entry_ts             INTEGER NOT NULL,
                exit_ts              INTEGER,
                signal_spread_pct    REAL,
                preflight_spread_pct REAL,
                actual_spread_pct    REAL,
                slippage_pct         REAL,
                execution_ms         INTEGER,
                entry_price_bybit    REAL,
                entry_price_gateio   REAL,
                exit_price_bybit     REAL,
                exit_price_gateio    REAL,
                size_usdt            REAL,
                leverage             INTEGER,
                gross_pnl            REAL,
                fee_total            REAL,
                net_pnl              REAL,
                status               TEXT DEFAULT 'open'
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS engine_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        INTEGER NOT NULL,
                level     TEXT,
                event     TEXT,
                symbol    TEXT,
                details   TEXT
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS ws_health (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                exchange    TEXT,
                conn_index  INTEGER,
                event       TEXT,
                retry_count INTEGER,
                latency_ms  INTEGER
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS rebalance_history (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                   INTEGER NOT NULL,
                mode                 TEXT NOT NULL,
                from_exchange        TEXT NOT NULL,
                to_exchange          TEXT NOT NULL,
                amount               REAL NOT NULL,
                fee_usdt             REAL DEFAULT 0,
                network              TEXT,
                tx_hash              TEXT,
                status               TEXT DEFAULT 'pending',
                confirmed_ts         INTEGER,
                balance_bybit_before REAL,
                balance_gateio_before REAL,
                balance_bybit_after  REAL,
                balance_gateio_after  REAL
            )
        """)

    async def close(self):
        if self.db:
            await self.db.close()

    # ── trades CRUD ──────────────────────────────────────────────

    async def insert_trade(self, trade: dict) -> int:
        cursor = await self.db.execute(
            """INSERT INTO trades
               (mode, symbol, direction, entry_ts, signal_spread_pct,
                preflight_spread_pct, actual_spread_pct, slippage_pct,
                execution_ms, entry_price_bybit, entry_price_gateio,
                size_usdt, leverage, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.get("mode", "paper"),
                trade["symbol"],
                trade["direction"],
                trade["entry_ts"],
                trade.get("signal_spread_pct"),
                trade.get("preflight_spread_pct"),
                trade.get("actual_spread_pct"),
                trade.get("slippage_pct"),
                trade.get("execution_ms"),
                trade.get("entry_price_bybit"),
                trade.get("entry_price_gateio"),
                trade.get("size_usdt"),
                trade.get("leverage", 1),
                trade.get("status", "open"),
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def update_trade_exit(self, trade_id: int, exit_data: dict) -> None:
        await self.db.execute(
            """UPDATE trades SET
                exit_ts = ?,
                exit_price_bybit = ?,
                exit_price_gateio = ?,
                actual_spread_pct = ?,
                slippage_pct = ?,
                gross_pnl = ?,
                fee_total = ?,
                net_pnl = ?,
                status = ?
               WHERE id = ?""",
            (
                exit_data.get("exit_ts"),
                exit_data.get("exit_price_bybit"),
                exit_data.get("exit_price_gateio"),
                exit_data.get("actual_spread_pct"),
                exit_data.get("slippage_pct"),
                exit_data.get("gross_pnl"),
                exit_data.get("fee_total"),
                exit_data.get("net_pnl"),
                exit_data.get("status", "closed"),
                trade_id,
            ),
        )
        await self.db.commit()

    async def get_open_trades(self, mode: str) -> list:
        cursor = await self.db.execute(
            "SELECT * FROM trades WHERE mode = ? AND status = 'open' ORDER BY entry_ts DESC",
            (mode,),
        )
        return await cursor.fetchall()

    async def get_trade_history(self, mode: str, days: int = 30) -> list:
        cutoff = int((time.time() - days * 86400) * 1000)
        cursor = await self.db.execute(
            "SELECT * FROM trades WHERE mode = ? AND entry_ts >= ? ORDER BY entry_ts DESC",
            (mode, cutoff),
        )
        return await cursor.fetchall()

    async def get_trade_summary(self, mode: str, days: int) -> dict:
        cutoff = int((time.time() - days * 86400) * 1000)
        cursor = await self.db.execute(
            """SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(net_pnl) as total_pnl,
                AVG(net_pnl) as avg_pnl,
                MAX(net_pnl) as best_trade,
                MIN(net_pnl) as worst_trade
               FROM trades WHERE mode = ? AND status = 'closed' AND entry_ts >= ?""",
            (mode, cutoff),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return {
                "total_trades": row[0],
                "wins": row[1] or 0,
                "losses": row[2] or 0,
                "total_pnl": round(row[3] or 0, 2),
                "avg_pnl": round(row[4] or 0, 2),
                "best_trade": round(row[5] or 0, 2),
                "worst_trade": round(row[6] or 0, 2),
                "win_rate": round((row[1] or 0) / row[0] * 100, 1) if row[0] else 0,
            }
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0, "avg_pnl": 0, "best_trade": 0,
            "worst_trade": 0, "win_rate": 0,
        }

    # ── engine_logs ──────────────────────────────────────────────

    async def insert_engine_log(
        self, level: str, event: str, symbol: str = None, details: str = None
    ) -> None:
        ts = int(time.time() * 1000)
        await self.db.execute(
            "INSERT INTO engine_logs (ts, level, event, symbol, details) VALUES (?,?,?,?,?)",
            (ts, level, event, symbol, details),
        )
        await self.db.commit()

    # ── ws_health ────────────────────────────────────────────────

    async def insert_ws_health(
        self,
        exchange: str,
        conn_index: int,
        event: str,
        retry_count: int = 0,
        latency_ms: int = None,
    ) -> None:
        ts = int(time.time() * 1000)
        await self.db.execute(
            "INSERT INTO ws_health (ts, exchange, conn_index, event, retry_count, latency_ms) VALUES (?,?,?,?,?,?)",
            (ts, exchange, conn_index, event, retry_count, latency_ms),
        )
        await self.db.commit()

    # ── rebalance_history CRUD ───────────────────────────────────

    async def insert_rebalance_log(self, log: dict) -> int:
        cursor = await self.db.execute(
            """INSERT INTO rebalance_history
               (ts, mode, from_exchange, to_exchange, amount, fee_usdt, network,
                tx_hash, status, balance_bybit_before, balance_gateio_before,
                balance_bybit_after, balance_gateio_after)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                log.get("ts", int(time.time() * 1000)),
                log["mode"],
                log["from_exchange"],
                log["to_exchange"],
                log["amount"],
                log.get("fee_usdt", 0),
                log.get("network"),
                log.get("tx_hash"),
                log.get("status", "pending"),
                log.get("balance_bybit_before"),
                log.get("balance_gateio_before"),
                log.get("balance_bybit_after"),
                log.get("balance_gateio_after"),
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def update_rebalance_status(self, record_id: int, status: str,
                                       confirmed_ts: int = None) -> None:
        if confirmed_ts:
            await self.db.execute(
                "UPDATE rebalance_history SET status = ?, confirmed_ts = ? WHERE id = ?",
                (status, confirmed_ts, record_id),
            )
        else:
            await self.db.execute(
                "UPDATE rebalance_history SET status = ? WHERE id = ?",
                (status, record_id),
            )
        await self.db.commit()

    async def get_daily_rebalance_total(self) -> float:
        today_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, 0))) * 1000
        cursor = await self.db.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM rebalance_history
               WHERE ts >= ? AND status IN ('confirmed', 'simulated', 'pending')""",
            (today_start,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0.0
