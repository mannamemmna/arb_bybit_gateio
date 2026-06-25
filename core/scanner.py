import asyncio
import time
from typing import List, Optional
from utils.logger import get_logger

logger = get_logger('scanner')


class SymbolScanner:
    """
    Discovers common USDT-margined perpetual pairs between Bybit and Gate.io.

    - Fetch all USDT perp pairs from both exchanges via REST on startup
    - Calculate intersection (only pairs available on BOTH exchanges)
    - Refresh every 6 hours (pairs can list/delisting anytime)
    - Distribute common_symbols to WS pool

    Estimation: ~250-300 common pairs from ~450 Bybit + ~320 Gate.io
    """

    def __init__(self, bybit_client, gateio_client, refresh_interval_sec: int = 21600):
        self.bybit_client = bybit_client
        self.gateio_client = gateio_client
        self.refresh_interval = refresh_interval_sec
        self.common_symbols: List[str] = []
        self._bybit_symbols: List[str] = []
        self._gateio_symbols: List[str] = []
        self._last_scan_ts: float = 0
        self._running = False

    async def scan(self) -> List[str]:
        """Fetch symbols from both exchanges, return intersection."""
        logger.info("Scanning for common perpetual pairs...")

        try:
            # Fetch from both exchanges concurrently
            bybit_result, gateio_result = await asyncio.gather(
                self.bybit_client.fetch_tickers(),
                self.gateio_client.fetch_tickers()
            )

            # Extract USDT perpetual symbols
            self._bybit_symbols = self._extract_bybit_symbols(bybit_result)
            self._gateio_symbols = self._extract_gateio_symbols(gateio_result)

            # Find intersection (normalize format first!)
            # Bybit: "BTCUSDT", Gate.io: "BTC_USDT" -> normalize to "BTCUSDT"
            bybit_set = set(self._bybit_symbols)
            gateio_normalized = {
                s.replace('_USDT', 'USDT').replace('_usdt', 'USDT')
                for s in self._gateio_symbols
            }

            common = sorted(bybit_set & gateio_normalized)

            new_symbols = [s for s in common if s not in self.common_symbols]
            self.common_symbols = common
            self._last_scan_ts = time.time()

            logger.info(
                f"Scan complete: {len(self._bybit_symbols)} Bybit + "
                f"{len(self._gateio_symbols)} Gate.io = {len(common)} common"
            )

            if new_symbols:
                logger.info(f"New symbols detected: {new_symbols}")

            return common

        except Exception as e:
            logger.error(f"Symbol scan failed: {e}")
            return self.common_symbols  # Return cached

    def _extract_bybit_symbols(self, data) -> List[str]:
        """Extract USDT perp symbols from Bybit tickers (processed dict format).

        fetch_tickers() returns {symbol: {bid, ask, ts}}. Bybit's
        /v5/market/tickers?category=linear only returns linear perpetuals.
        """
        symbols: List[str] = []
        if isinstance(data, dict):
            for sym in data.keys():
                if sym.endswith('USDT'):
                    symbols.append(sym)
        return symbols

    def _extract_gateio_symbols(self, data) -> List[str]:
        """Extract USDT perp symbols from Gate.io tickers (processed dict format).

        fetch_tickers() returns {contract: {bid, ask, ts}}. Gate.io's
        /api/v4/futures/usdt/tickers only returns USDT futures.
        """
        symbols: List[str] = []
        if isinstance(data, dict):
            for contract in data.keys():
                if contract.endswith('_USDT'):
                    symbols.append(contract)
        return symbols

    def get_gateio_symbol(self, normalized: str) -> str:
        """Convert normalized symbol (BTCUSDT) back to Gate.io format (BTC_USDT)."""
        return normalized.replace('USDT', '_USDT')

    def get_bybit_symbol(self, normalized: str) -> str:
        """Convert normalized symbol to Bybit format (already BTCUSDT)."""
        return normalized  # Bybit uses BTCUSDT directly

    async def run_periodic(self, on_update=None):
        """Run periodic scan every refresh_interval. on_update(new_symbols) called on change."""
        self._running = True
        while self._running:
            old = set(self.common_symbols)
            await self.scan()
            new = set(self.common_symbols)

            if old != new and on_update:
                await on_update(list(new))

            await asyncio.sleep(self.refresh_interval)

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        return {
            'common_symbols': len(self.common_symbols),
            'bybit_symbols': len(self._bybit_symbols),
            'gateio_symbols': len(self._gateio_symbols),
            'last_scan_ts': self._last_scan_ts,
            'next_scan_in': max(0, self.refresh_interval - (time.time() - self._last_scan_ts))
        }
