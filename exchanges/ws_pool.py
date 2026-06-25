"""
WebSocket Connection Pool Manager.

Manages multiple WebSocket connections per exchange to handle subscription limits:
- Bybit: max 300 topics/connection
- Gate.io: ~200 topics/connection
- Default WS_MAX_SUBS_PER_CONN=250
"""

import asyncio
import time
from typing import Optional, Callable
from dataclasses import dataclass, field
from utils.logger import get_logger

logger = get_logger('ws_pool')


@dataclass
class WSConnection:
    """Represents a single WebSocket connection with its assigned symbols."""
    exchange: str
    index: int  # connection index (0, 1, 2, ...)
    symbols: list[str]
    status: str = 'disconnected'  # 'connected' | 'disconnected' | 'reconnecting'
    retry_count: int = 0
    last_latency_ms: int = 0
    ws_task: Optional[asyncio.Task] = None
    connected_at: Optional[float] = None
    last_message_at: Optional[float] = None


class WebSocketPool:
    """
    Manages multiple WebSocket connections for each exchange.
    Distributes symbols across connections based on WS_MAX_SUBS_PER_CONN.

    Architecture:
      Bybit  WS #0  -> pair 0-249
      Bybit  WS #1  -> pair 250-499
      Gateio WS #0  -> pair 0-249
      Gateio WS #1  -> pair 250-499

    Each connection:
      - Subscribes to all assigned symbols on connect
      - Heartbeat ping every heartbeat_sec
      - Auto-reconnect with exponential backoff on disconnect
      - Every message -> price_callback -> PriceCache -> spread check
    """

    def __init__(self,
                 bybit_client,
                 gateio_client,
                 max_subs_per_conn: int = 250,
                 heartbeat_sec: int = 20,
                 reconnect_delay: int = 5,
                 max_retries: int = 10,
                 on_price_update: Callable = None,
                 on_ws_event: Callable = None):
        self.bybit_client = bybit_client
        self.gateio_client = gateio_client
        self.max_subs_per_conn = max_subs_per_conn
        self.heartbeat_sec = heartbeat_sec
        self.reconnect_delay = reconnect_delay
        self.max_retries = max_retries
        self.on_price_update = on_price_update
        self.on_ws_event = on_ws_event  # callback(exchange, conn_index, event, retry_count, latency_ms)

        self.bybit_connections: list[WSConnection] = []
        self.gateio_connections: list[WSConnection] = []
        self._running = False

    def _split_symbols(self, symbols: list[str]) -> list[list[str]]:
        """Split symbol list into chunks of max_subs_per_conn."""
        return [symbols[i:i + self.max_subs_per_conn] for i in range(0, len(symbols), self.max_subs_per_conn)]

    async def start(self, common_symbols: list[str]):
        """Start all WS connections for both exchanges."""
        self._running = True

        # Wire price callback to both exchange clients
        if self.on_price_update:
            self.bybit_client.on_price_update(self.on_price_update)
            self.gateio_client.on_price_update(self.on_price_update)

        # Split symbols into chunks
        bybit_chunks = self._split_symbols(common_symbols)
        # Gate.io uses BTC_USDT format while common_symbols are in BTCUSDT format
        gateio_symbols = [s.replace('USDT', '_USDT') for s in common_symbols]
        gateio_chunks = self._split_symbols(gateio_symbols)

        # Create connection descriptors
        self.bybit_connections = [
            WSConnection(exchange='bybit', index=i, symbols=chunk)
            for i, chunk in enumerate(bybit_chunks)
        ]
        self.gateio_connections = [
            WSConnection(exchange='gateio', index=i, symbols=chunk)
            for i, chunk in enumerate(gateio_chunks)
        ]

        # Start all connections concurrently
        tasks = []
        for conn in self.bybit_connections:
            tasks.append(asyncio.create_task(self._run_connection(self.bybit_client, conn)))
        for conn in self.gateio_connections:
            tasks.append(asyncio.create_task(self._run_connection(self.gateio_client, conn)))

        logger.info(
            f"WS Pool started: {len(self.bybit_connections)} Bybit + "
            f"{len(self.gateio_connections)} Gate.io connections"
        )

        # Wait for all (they reconnect on failure)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_connection(self, client, conn: WSConnection):
        """Manage a single WS connection with reconnect logic."""
        while self._running:
            try:
                conn.status = 'connecting'
                # Connect to WS
                await client.connect_ws(
                    symbols=conn.symbols,
                    heartbeat_sec=self.heartbeat_sec
                )
                conn.status = 'connected'
                conn.connected_at = time.time()
                conn.retry_count = 0

                if self.on_ws_event:
                    await self.on_ws_event(conn.exchange, conn.index, 'connected', 0, 0)

                # Client handles reconnect internally via _ws_loop.
                # We just monitor the task and wait for it to finish.
                if hasattr(client, '_ws_task') and client._ws_task:
                    conn.ws_task = client._ws_task
                    try:
                        await conn.ws_task
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        pass  # Client's _ws_loop handles reconnect

                # If we get here, the client's WS loop exited
                if not self._running:
                    break

                conn.status = 'disconnected'
                if self.on_ws_event:
                    await self.on_ws_event(
                        conn.exchange, conn.index, 'disconnected',
                        conn.retry_count, 0
                    )

                if conn.retry_count >= self.max_retries:
                    logger.error(f"WS {conn.exchange} #{conn.index} max retries reached!")
                    if self.on_ws_event:
                        await self.on_ws_event(
                            conn.exchange, conn.index, 'failed',
                            conn.retry_count, 0
                        )
                    break

                conn.retry_count += 1
                delay = min(self.reconnect_delay * (2 ** (conn.retry_count - 1)), 60)
                logger.info(f"WS {conn.exchange} #{conn.index} reconnecting in {delay}s...")
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                conn.status = 'error'
                conn.retry_count += 1
                logger.error(f"WS {conn.exchange} #{conn.index} error: {e}")
                if self.on_ws_event:
                    await self.on_ws_event(
                        conn.exchange, conn.index, 'disconnected',
                        conn.retry_count, 0
                    )
                await asyncio.sleep(self.reconnect_delay)

    async def stop(self):
        """Stop all connections gracefully."""
        self._running = False
        # Cancel all tasks
        for conn in self.bybit_connections + self.gateio_connections:
            if conn.ws_task and not conn.ws_task.done():
                conn.ws_task.cancel()
                try:
                    await conn.ws_task
                except asyncio.CancelledError:
                    pass
        # Disconnect clients
        await self.bybit_client.disconnect_ws()
        await self.gateio_client.disconnect_ws()
        logger.info("WS Pool stopped")

    async def update_symbols(self, new_symbols: list[str]):
        """Update symbol list (requires reconnect). Called when scanner finds new pairs."""
        logger.info(f"Updating WS subscriptions: {len(new_symbols)} symbols")
        await self.stop()
        await asyncio.sleep(1)
        await self.start(new_symbols)

    def get_status(self) -> dict:
        """Return status of all connections for /status command."""
        def conn_status(conns):
            return [{
                'index': c.index,
                'status': c.status,
                'symbols': len(c.symbols),
                'retry_count': c.retry_count,
                'latency_ms': c.last_latency_ms,
                'uptime': time.time() - c.connected_at if c.connected_at else 0
            } for c in conns]

        return {
            'bybit': conn_status(self.bybit_connections),
            'gateio': conn_status(self.gateio_connections),
            'total_symbols': sum(len(c.symbols) for c in self.bybit_connections)
        }
