import json
import time
import asyncio
import hmac
import hashlib
import aiohttp
import websockets
from typing import Optional
from exchanges.base import ExchangeClient
from utils.rate_limiter import bybit_public_limiter, bybit_private_limiter
from utils.logger import get_logger

logger = get_logger('bybit')

BYBIT_REST_BASE = 'https://api.bybit.com'
BYBIT_WS_URL = 'wss://stream.bybit.com/v5/public/linear'


class BybitClient(ExchangeClient):
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False,
                 ws_url: str = BYBIT_WS_URL):
        super().__init__('bybit', api_key, api_secret)
        self.testnet = testnet
        self.ws_url = ws_url
        self._rest_base = 'https://api-testnet.bybit.com' if testnet else BYBIT_REST_BASE
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._subscriptions: list[str] = []
        self._reconnect_count = 0
        self._max_retries = 10
        self._reconnect_delay = 5
        self._heartbeat_sec = 20
        self._stop_event = asyncio.Event()
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
        self._subscriptions = [f"tickers.{s}" for s in symbols]
        self._stop_event.clear()
        self._ws_task = asyncio.create_task(self._ws_loop(symbols, heartbeat_sec))
        logger.info("Bybit WS task started for %d symbols", len(symbols))

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
        logger.info("Bybit WS disconnected")

    async def _ws_loop(self, symbols: list[str], heartbeat_sec: int):
        backoff = self._reconnect_delay
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_count = 0
                    backoff = self._reconnect_delay
                    logger.info("Bybit WS connected to %s", self.ws_url)

                    # Subscribe
                    sub_msg = json.dumps({"op": "subscribe", "args": self._subscriptions})
                    await ws.send(sub_msg)
                    logger.info("Bybit WS subscribed: %s", self._subscriptions)

                    # Listen
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                            if msg.get('op') == 'pong' or msg.get('success'):
                                continue
                            if 'topic' in msg and msg['topic'].startswith('tickers.'):
                                await self._handle_message(msg)
                        except Exception as e:
                            logger.error("Bybit WS message parse error: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._reconnect_count += 1
                if self._reconnect_count > self._max_retries:
                    logger.error("Bybit WS max retries exceeded")
                    break
                logger.warning("Bybit WS error (%s), reconnecting in %ds (attempt %d/%d)",
                               e, backoff, self._reconnect_count, self._max_retries)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        self._connected = False

    async def _handle_message(self, msg: dict):
        try:
            topic: str = msg['topic']
            symbol = topic.split('.', 1)[1]
            data = msg['data']
            # Delta updates may only have partial fields — skip if bid/ask missing
            bid = data.get('bid1Price')
            ask = data.get('ask1Price')
            if not bid or not ask:
                return
            bid = float(bid)
            ask = float(ask)
            ts = float(data.get('ts', time.time() * 1000))  # already in ms
            if self._price_callback:
                await self._price_callback(self.name, symbol, bid, ask, ts)
        except Exception as e:
            logger.error("Bybit handle_message error: %s | msg=%s", e, msg)

    # ── REST helpers ─────────────────────────────────────────────

    async def _sign_request(self, method: str, path: str, params: dict) -> dict:
        """Sign a private REST request."""
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        if method == "GET":
            query = '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
            sign_str = f"{timestamp}{self.api_key}{recv_window}{query}"
            url = f"{self._rest_base}{path}"
            if query:
                url += f"?{query}"
        else:
            body = json.dumps(params)
            sign_str = f"{timestamp}{self.api_key}{recv_window}{body}"
            url = f"{self._rest_base}{path}"

        signature = hmac.new(self.api_secret.encode(), sign_str.encode(),
                             hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }
        return {"url": url, "headers": headers, "body": body if method != "GET" else None}

    async def _rest_get(self, path: str, params: dict = None, private: bool = False) -> dict:
        params = params or {}
        limiter = bybit_private_limiter if private else bybit_public_limiter
        async with limiter:
            session = await self._get_session()
            if private:
                req = await self._sign_request("GET", path, params)
                async with session.get(req["url"], headers=req["headers"]) as resp:
                    data = await resp.json()
            else:
                url = f"{self._rest_base}{path}"
                async with session.get(url, params=params) as resp:
                    data = await resp.json()
        if data.get('retCode') != 0:
            logger.error("Bybit REST error: %s", data.get('retMsg'))
        return data.get('result', data)

    async def _rest_post(self, path: str, params: dict, private: bool = True) -> dict:
        limiter = bybit_private_limiter if private else bybit_public_limiter
        async with limiter:
            session = await self._get_session()
            req = await self._sign_request("POST", path, params)
            async with session.post(req["url"], headers=req["headers"],
                                    data=req["body"]) as resp:
                data = await resp.json()
        if data.get('retCode') != 0:
            logger.error("Bybit REST POST error: %s", data.get('retMsg'))
        return data.get('result', data)

    # ── Public API ───────────────────────────────────────────────

    async def fetch_tickers(self) -> dict:
        result = await self._rest_get('/v5/market/tickers', {'category': 'linear'})
        tickers = {}
        for item in result.get('list', []):
            tickers[item['symbol']] = {
                'bid': float(item['bid1Price']),
                'ask': float(item['ask1Price']),
                'ts': float(item.get('time', 0)),  # Bybit returns ms
            }
        return tickers

    async def fetch_orderbook(self, symbol: str, depth: int = 25) -> dict:
        result = await self._rest_get('/v5/market/orderbook', {
            'category': 'linear', 'symbol': symbol, 'limit': depth,
        })
        return {
            'bids': [[float(p), float(q)] for p, q in result.get('b', [])],
            'asks': [[float(p), float(q)] for p, q in result.get('a', [])],
        }

    async def place_market_order(self, symbol: str, side: str, qty: str) -> dict:
        params = {
            'category': 'linear',
            'symbol': symbol,
            'side': side.capitalize(),
            'orderType': 'Market',
            'qty': qty,
        }
        logger.info("Bybit place_market_order: %s %s %s", symbol, side, qty)

        # Send request directly (not via _rest_post) to access raw response
        async with bybit_private_limiter:
            session = await self._get_session()
            req = await self._sign_request("POST", "/v5/order/create", params)
            async with session.post(req["url"], headers=req["headers"],
                                    data=req["body"]) as resp:
                raw = await resp.json()

        ret_code = raw.get('retCode', -1)
        ret_msg = raw.get('retMsg', 'unknown')
        result = raw.get('result', {})

        if ret_code == 0:
            order_id = result.get('orderId', '')
            # Try to derive avg fill price from cumExecValue / cumExecQty
            avg_price = 0.0
            cum_exec_qty = float(result.get('cumExecQty', '0'))
            cum_exec_value = float(result.get('cumExecValue', '0'))
            if cum_exec_qty > 0 and cum_exec_value > 0:
                avg_price = cum_exec_value / cum_exec_qty

            # If avg_price still 0 and order was filled, fetch order detail
            # (one extra REST call) to get actual fill price.
            if avg_price <= 0 and order_id:
                try:
                    detail = await self._rest_get(
                        '/v5/order/realtime',
                        {'category': 'linear', 'symbol': symbol, 'orderId': order_id},
                    )
                    detail_list = detail.get('list', [])
                    if detail_list:
                        avg_price = float(detail_list[0].get('avgPrice', 0))
                except Exception as e:
                    logger.warning("Bybit fetch avgPrice failed: %s", e)

            return {
                "success": True,
                "exchange": "bybit",
                "symbol": symbol,
                "side": side,
                "price": avg_price,
                "quantity": float(qty),
                "order_id": order_id,
                "error": None,
            }
        else:
            logger.error("Bybit place_market_order FAILED: %s", ret_msg)
            return {
                "success": False,
                "exchange": "bybit",
                "symbol": symbol,
                "side": side,
                "price": 0,
                "quantity": 0,
                "order_id": "",
                "error": ret_msg,
            }

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        params = {
            'category': 'linear',
            'symbol': symbol,
            'buyLeverage': str(leverage),
            'sellLeverage': str(leverage),
        }
        try:
            await self._rest_post('/v5/position/set-leverage', params)
            logger.info("Bybit leverage set: %s -> %dx", symbol, leverage)
            return True
        except Exception as e:
            logger.error("Bybit set_leverage error: %s", e)
            return False

    async def get_wallet_balance(self) -> float:
        result = await self._rest_get('/v5/account/wallet-balance', {
            'accountType': 'UNIFIED',
        }, private=True)
        accounts = result.get('list', [])
        if accounts:
            for coin in accounts[0].get('coin', []):
                if coin['coin'] == 'USDT':
                    return float(coin.get('availableToWithdraw', 0))
        return 0.0

    async def get_positions(self) -> list:
        result = await self._rest_get('/v5/position/list', {
            'category': 'linear',
            'settleCoin': 'USDT',
        }, private=True)
        positions = []
        for p in result.get('list', []):
            if float(p.get('size', 0)) != 0:
                positions.append({
                    'symbol': p['symbol'],
                    'side': p['side'],
                    'size': float(p['size']),
                    'entry_price': float(p.get('avgPrice', 0)),
                    'unrealised_pnl': float(p.get('unrealisedPnl', 0)),
                    'leverage': float(p.get('leverage', 1)),
                })
        return positions

    async def withdraw_usdt(self, address: str, amount: float, network: str = 'TRC20') -> dict:
        """
        Initiate USDT withdrawal from Bybit.

        API: POST /v5/asset/withdraw/create
        Docs: https://bybit-exchange.github.io/docs/v5/asset/withdraw

        PENTING: address harus sudah di-whitelist di Bybit account settings.
        """
        params = {
            'coin': 'USDT',
            'chain': network,
            'address': address,
            'amount': str(round(amount, 2)),
            'accountType': 'FUND',
            'timestamp': str(int(time.time() * 1000)),
        }
        logger.info("Bybit withdraw_usdt: %.2f USDT -> %s (%s)", amount, address, network)
        result = await self._rest_post('/v5/asset/withdraw/create', params)
        return {
            'tx_id': result.get('id', ''),
            'fee': None,
        }

    # ── Instrument info cache (for quantity rounding) ────────────
    _instrument_info: dict[str, dict] = {}

    async def fetch_instrument_info(self, symbol: str) -> dict:
        """Fetch lot size info for a perpetual symbol. Cached 1h."""
        if symbol in self._instrument_info:
            return self._instrument_info[symbol]
        result = await self._rest_get('/v5/market/instruments-info', {
            'category': 'linear', 'symbol': symbol,
        })
        items = result.get('list', [])
        info = {}
        if items:
            ls = items[0].get('lotSizeFilter', {})
            info = {
                'lot_step': float(ls.get('lotStep', 0.001)),
                'min_order_qty': float(ls.get('minOrderQty', 0.001)),
                'max_order_qty': float(ls.get('maxOrderQty', 1000000)),
                'qty_step': float(ls.get('qtyStep', 0.001)),
            }
        self._instrument_info[symbol] = info
        return info

    def round_qty(self, symbol: str, qty: float) -> float:
        """Round qty to valid lot step for the symbol."""
        info = self._instrument_info.get(symbol, {})
        step = info.get('lot_step', 0.001)
        if step <= 0:
            step = 0.001
        rounded = round(qty / step) * step
        # Clamp to min/max
        min_q = info.get('min_order_qty', 0.001)
        max_q = info.get('max_order_qty', 1000000)
        if rounded < min_q:
            return min_q
        if rounded > max_q:
            return max_q
        return rounded
