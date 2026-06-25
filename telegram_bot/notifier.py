from telegram import Bot
from utils.logger import get_logger
from utils.formatter import *

logger = get_logger('notifier')

class Notifier:
    """
    Sends push notifications to the authorized user.
    Used for trade events, engine status changes, errors, etc.
    """
    
    def __init__(self, bot_token: str, user_id: int):
        self.bot = Bot(token=bot_token)
        self.user_id = int(user_id)
    
    async def send(self, text: str, parse_mode: str = 'MarkdownV2'):
        """Send message to authorized user."""
        try:
            await self.bot.send_message(
                chat_id=self.user_id,
                text=text,
                parse_mode=parse_mode
            )
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
    
    async def notify_engine_start(self, mode: str, pair_count: int):
        text = (
            f"🟢 *Engine Started*\n\n"
            f"⚙️ Mode: `{mode}`\n"
            f"📊 Pairs: `{pair_count}`"
        )
        await self.send(text)
    
    async def notify_engine_stop(self, reason: str, trade_count: int, net_pnl: float):
        text = (
            f"🔴 *Engine Stopped*\n\n"
            f"📝 Reason: {reason}\n"
            f"📊 Trades: `{trade_count}`\n"
            f"💰 Net PnL: `{net_pnl:.4f}` USDT"
        )
        await self.send(text)
    
    async def notify_trade_open(self, symbol: str, direction: str,
                                bybit_price: float, gateio_price: float,
                                spread_pct: float, size_usdt: float):
        long_exchange = "Bybit" if direction == "long_bybit" else "Gate\\.io"
        short_exchange = "Gate\\.io" if direction == "long_bybit" else "Bybit"
        if direction == "long_bybit":
            long_price = bybit_price
            short_price = gateio_price
        else:
            long_price = gateio_price
            short_price = bybit_price
        text = (
            f"🟢 *Trade OPEN*\n\n"
            f"📊 Pair: `{symbol}`\n"
            f"📈 Long: {long_exchange} @ `{long_price:.4f}`\n"
            f"📉 Short: {short_exchange} @ `{short_price:.4f}`\n"
            f"📐 Spread: `{spread_pct:.3f}%`\n"
            f"💵 Size: `{size_usdt}` USDT"
        )
        await self.send(text)
    
    async def notify_trade_close(self, symbol: str, direction: str,
                                 exit_bybit: float, exit_gateio: float,
                                 gross_pnl: float, fee: float, net_pnl: float,
                                 duration_sec: int):
        emoji = '🟢' if net_pnl >= 0 else '🔴'
        text = (
            f"{emoji} *Trade CLOSE*\n\n"
            f"📊 Pair: `{symbol}`\n"
            f"💰 Gross PnL: `{gross_pnl:.4f}` USDT\n"
            f"💸 Fee: `{fee:.4f}` USDT\n"
            f"{'🟢' if net_pnl >= 0 else '🔴'} Net PnL: `{net_pnl:.4f}` USDT\n"
            f"⏱ Duration: `{duration_sec}s`"
        )
        await self.send(text)
    
    async def notify_ws_disconnect(self, exchange: str):
        await self.send(f"🟡 *WS Disconnected*\n{exchange}\.\.\.reconnecting\.\.\.")
    
    async def notify_ws_reconnect_failed(self, exchange: str, retries: int):
        await self.send(f"🔴 *WS Reconnect Failed*\n{exchange} after `{retries}` retries\! Manual check needed")
    
    async def notify_rate_limit_warning(self, exchange: str, percentage: float):
        await self.send(f"🟡 *Rate Limit Warning*\n{exchange}: `{percentage:.0f}%` of limit reached")
    
    async def notify_api_key_error(self, exchange: str, error: str):
        await self.send(
            f"🔴 *API Key Error*\n"
            f"Exchange: {exchange}\n"
            f"Error: {error}\n"
            f"Check your \.env file"
        )
    
    async def notify_new_pairs(self, new_symbols: list):
        text = f"ℹ️ *New Pairs Detected*\n"
        for s in new_symbols[:10]:
            text += f"• `{s}`\n"
        await self.send(text)
