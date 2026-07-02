import json
import time
import asyncio
import hmac
import hashlib
import aiohttp
import websockets
from typing import Optional
from exchanges.base import ExchangeClient
from utils.rate_limiter import gateio_private_limiter, gateio_public_limiter
from utils.logger import get_logger

logger = get_logger('gateio')

GATEIO_REST_BASE = 'https://api.gateio.ws'
GATEIO_WS_URL = 'wss://fx-ws.gateio.ws/v4/ws/usdt'


class GateioClient(ExchangeClient):
    def __init__(self, api_key: str, api_secret: str,
                 ws_url: str = GATEIO_WS_URL):
        super().__init__('gateio', api_key, api_secret)
        self.ws_url = ws_url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._subscriptions: list[str] = []
        self._reconnect_count = 0
        self._max_retries = 10
        self._reconnect_delay = 5
        self._heartbeat_sec = 20
        self._stop_event = asyncio.Event()
        self._symbol_map: dict[str, str] = {}  # BTC_USDT -> BTCUSDT etc.
        self._session: Optional[aiohttp.ClientSession] = None

    # ── WebSocket ────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def connect_ws(self, symbols: list[str], url: str = None,
                         heartbeat_sec: int = 20):
        if url:
            self.ws_url = url
        self._heartbeat_sec = heartbeat_sec
        self._subscriptions = [s for s in symbols]
        self._stop_event.clear()
        self._ws_task = asyncio.create_task(self._ws_loop(symbols, heartbeat_sec))
        logger.info("Gate.io WS task started for %d symbols", len(symbols))

    async def disconnect_ws(self):
        self._stop_event.set()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._connected = False
        logger.info("Gate.io WS disconnected")

    async def _ws_loop(self, symbols: list[str], heartbeat_sec: int):
        backoff = self._reconnect_delay
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_count = 0
                    backoff = self._reconnect_delay
                    logger.info("Gate.io WS connected to %s", self.ws_url)

                    # Subscribe to tickers for all symbols
                    sub_msg = json.dumps({
                        "time": int(time.time()),
                        "channel": "futures.tickers",
                        "event": "subscribe",
                        "payload": symbols,
                    })
                    await ws.send(sub_msg)
                    logger.info("Gate.io WS subscribed: %s", symbols)

                    # Heartbeat + listen
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(ws, heartbeat_sec))
                    try:
                        async for raw in ws:
                            if self._stop_event.is_set():
                                break
                            try:
                                msg = json.loads(raw)
                                channel = msg.get('channel', '')
                                event = msg.get('event', '')
                                if channel == 'futures.tickers' and event == 'update':
                                    await self._handle_message(msg)
                                elif channel == 'futures.ping':
                                    # Reply with pong
                                    pong = json.dumps({
                                        "time": int(time.time()),
                                        "channel": "futures.pong",
                                    })
                                    await ws.send(pong)
                            except Exception as e:
                                logger.error("Gate.io WS message parse error: %s", e)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._reconnect_count += 1
                if self._reconnect_count > self._max_retries:
                    logger.error("Gate.io WS max retries exceeded")
                    break
                logger.warning("Gate.io WS error (%s), reconnecting in %ds (attempt %d/%d)",
                               e, backoff, self._reconnect_count, self._max_retries)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        self._connected = False

    async def _heartbeat_loop(self, ws, interval: int):
        """Send periodic ping to keep connection alive."""
        while True:
            await asyncio.sleep(interval)
            try:
                ping_msg = json.dumps({
                    "time": int(time.time()),
                    "channel": "futures.ping",
                })
                await ws.send(ping_msg)
                logger.debug("Gate.io WS heartbeat sent")
            except Exception:
                break

    async def _handle_message(self, msg: dict):
        try:
            # Gate.io result is a LIST of tickers, not a single dict
            result = msg.get('result', [])
            if not isinstance(result, list):
                result = [result]
            for item in result:
                contract = item.get('contract', '')
                if not contract or not contract.endswith('_USDT'):
                    continue
                # WS futures.tickers has 'last' but not best_bid/best_ask
                # Use best_bid/ask if available, otherwise approximate from last
                last = float(item.get('last', 0))
                bid = float(item.get('best_bid', item.get('highest_bid', 0)))
                ask = float(item.get('best_ask', item.get('lowest_ask', 0)))
                if bid <= 0:
                    bid = last * 0.9999  # approximate
                if ask <= 0:
                    ask = last * 1.0001
                if bid <= 0 or ask <= 0:
                    continue
                ts = float(item.get('time', time.time())) * 1000  # s → ms
                if self._price_callback:
                    await self._price_callback(self.name, contract, bid, ask, ts)
        except Exception as e:
            logger.error("Gate.io handle_message error: %s | msg=%s", e, msg)

    # ── REST helpers ─────────────────────────────────────────────

    def _sign(self, method: str, url: str, query_string: str = '',
              body: str = '') -> dict:
        """Generate Gate.io HMAC-SHA512 auth headers."""
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        sign_str = f"{method}\n{url}\n{query_string}\n{body_hash}\n{timestamp}"
        signature = hmac.new(self.api_secret.encode(), sign_str.encode(),
                             hashlib.sha512).hexdigest()
        return {
            "KEY": self.api_key,
            "SIGN": signature,
            "Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    async def _rest_get(self, path: str, params: dict = None,
                        private: bool = False) -> any:
        params = params or {}
        query = '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
        url = f"{GATEIO_REST_BASE}{path}"
        full_url = f"{url}?{query}" if query else url

        headers = {}
        if private:
            headers = self._sign("GET", path, query)

        limiter = gateio_private_limiter if private else gateio_public_limiter
        session = await self._get_session()
        async with limiter:
            async with session.get(full_url, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.error("Gate.io REST GET error: %s", data)
                return data

    async def _rest_post(self, path: str, params: dict = None,
                         private: bool = True) -> any:
        params = params or {}
        body = json.dumps(params)
        url = f"{GATEIO_REST_BASE}{path}"

        headers = self._sign("POST", path, body=body)

        session = await self._get_session()
        async with gateio_private_limiter:
            async with session.post(url, headers=headers, data=body) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    logger.error("Gate.io REST POST error: %s", data)
                return data

    # ── Public API ───────────────────────────────────────────────

    async def fetch_tickers(self) -> dict:
        data = await self._rest_get('/api/v4/futures/usdt/tickers')
        tickers = {}
        for item in data:
            contract = item.get('contract', '')
            tickers[contract] = {
                'bid': float(item.get('highest_bid', item.get('best_bid', 0))),
                'ask': float(item.get('lowest_ask', item.get('best_ask', 0))),
                'ts': float(item.get('time', 0))
                if item.get('time', 0) > 1e12 else float(item.get('time', 0)) * 1000,
            }
        return tickers

    async def fetch_orderbook(self, symbol: str, depth: int = 20) -> dict:
        data = await self._rest_get('/api/v4/futures/usdt/order_book', {
            'contract': symbol, 'limit': str(depth),
        })
        return {
            'bids': [[float(entry['p']), float(entry['s'])]
                     for entry in data.get('bids', [])],
            'asks': [[float(entry['p']), float(entry['s'])]
                     for entry in data.get('asks', [])],
        }

    async def place_market_order(self, symbol: str, side: str, qty: str) -> dict:
        size_val = float(qty)
        # Gate.io: positive = buy, negative = sell
        params = {
            'contract': symbol,
            'size': size_val if side.lower() == 'buy' else -size_val,
            'price': '0',  # market
            'tif': 'ioc',
            'text': 't-arb-bot',
        }
        logger.info("Gate.io place_market_order: %s %s %s", symbol, side, qty)

        body = json.dumps(params)
        url = f"{GATEIO_REST_BASE}/api/v4/futures/usdt/orders"
        headers = self._sign("POST", "/api/v4/futures/usdt/orders", body=body)

        session = await self._get_session()
        async with gateio_private_limiter:
            async with session.post(url, headers=headers, data=body) as resp:
                data = await resp.json()

        if resp.status in (200, 201) and 'id' in data:
            # Parse fill info
            order_id = data.get('id', '')
            fill_price = float(data.get('fill_price', 0))
            # For IOC market orders, 'size' in response could be the filled qty
            filled_qty = abs(float(data.get('fill_total_quantity', 0)))
            if filled_qty <= 0:
                filled_qty = size_val  # fallback to request qty

            return {
                "success": True,
                "exchange": "gateio",
                "symbol": symbol,
                "side": side,
                "price": fill_price,
                "quantity": filled_qty,
                "order_id": order_id,
                "error": None,
            }
        else:
            err_msg = data.get('label', str(data)) if isinstance(data, dict) else str(data)
            logger.error("Gate.io place_market_order FAILED: %s", err_msg)
            return {
                "success": False,
                "exchange": "gateio",
                "symbol": symbol,
                "side": side,
                "price": 0,
                "quantity": 0,
                "order_id": "",
                "error": err_msg,
            }

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            path = f'/api/v4/futures/usdt/positions/{symbol}/leverage'
            query = f'leverage={leverage}'
            headers = self._sign("POST", f"{path}?{query}", query_string=query)
            url = f"{GATEIO_REST_BASE}{path}"
            session = await self._get_session()
            async with gateio_private_limiter:
                async with session.post(url, headers=headers,
                                        params={'leverage': str(leverage)}) as resp:
                        data = await resp.json()
                        if resp.status != 200:
                            logger.error("Gate.io set_leverage error: %s", data)
                            return False
            logger.info("Gate.io leverage set: %s -> %dx", symbol, leverage)
            return True
        except Exception as e:
            logger.error("Gate.io set_leverage error: %s", e)
            return False

    async def get_wallet_balance(self) -> float:
        data = await self._rest_get('/api/v4/futures/usdt/accounts', private=True)
        return float(data.get('available', 0))

    async def get_positions(self) -> list:
        data = await self._rest_get('/api/v4/futures/usdt/positions', private=True)
        positions = []
        if isinstance(data, list):
            for p in data:
                if float(p.get('size', 0)) != 0:
                    positions.append({
                        'symbol': p.get('contract', ''),
                        'side': 'Long' if int(p.get('size', 0)) > 0 else 'Short',
                        'size': abs(int(p.get('size', 0))),
                        'entry_price': float(p.get('entry_price', 0)),
                        'unrealised_pnl': float(p.get('unrealised_pnl', 0)),
                        'leverage': float(p.get('leverage', 1)),
                    })
        return positions
