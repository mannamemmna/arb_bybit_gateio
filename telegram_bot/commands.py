import time
from telegram import Update
from telegram.ext import ContextTypes
from utils.logger import get_logger
from utils.formatter import *

logger = get_logger('commands')

def _esc(text: str) -> str:
    """Escape MarkdownV2 special chars in dynamic values."""
    for ch in '._~`>#+-=|{}()!':
        text = text.replace(ch, '\\' + ch)
    return text

class CommandHandler:
    def __init__(self, engine, authorized_user_id: int):
        self.engine = engine
        self.authorized_user_id = authorized_user_id
    
    def _check_auth(self, update: Update) -> bool:
        return update.effective_user.id == self.authorized_user_id
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /start - Introduction and connection status. """
        if not self._check_auth(update): return
        mode = self.engine.settings.trading_mode if self.engine else 'unknown'
        await update.message.reply_text(
            f"🤖 *Arbitrage Bot* \\(Bybit × Gate\\.io\\)\n\n"
            f"⚙️ Mode: `{_esc(mode)}`\n"
            f"📊 Status: Ready\n\n"
            f"Use /help to see all commands",
            parse_mode='MarkdownV2'
        )
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /help - List all commands. """
        if not self._check_auth(update): return
        text = (
            "📋 *Available Commands*\n\n"
            "▶️ /auto on \\- Start automation\n"
            "⏹️ /auto off \\- Stop automation\n"
            "📊 /status \\- Engine status\n"
            "🔄 /mode paper \\- Switch to paper mode\n"
            "🔄 /mode live \\- Switch to live mode\n"
            "💰 /portfolio \\- Balances & positions\n"
            "📈 /history \\- Trade history & PnL\n"
            "📈 /history detail \\- Last 10 trades\n"
            "🔥 /top \\- Top spread pairs\n"
            "❌ /cancel \\- Cancel pending orders"
        )
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    
    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /auto on | /auto off - Start/stop automation engine. """
        if not self._check_auth(update): return
        
        args = context.args
        if not args or args[0] not in ('on', 'off'):
            await update.message.reply_text("Usage: /auto on | /auto off")
            return
        
        action = args[0]
        if action == 'on':
            if self.engine and self.engine.running:
                await update.message.reply_text("⚠️ Engine already running")
                return
            if self.engine:
                await self.engine.start()
                pairs = len(self.engine.scanner.common_symbols) if self.engine.scanner else 0
                await update.message.reply_text(
                    f"🟢 *Engine STARTED*\n"
                    f"⚙️ Mode: `{_esc(self.engine.settings.trading_mode)}`\n"
                    f"📊 Monitoring: `{pairs}` pairs",
                    parse_mode='MarkdownV2'
                )
        else:
            if self.engine:
                await self.engine.stop()
                ec = self.engine.executor._execution_count if self.engine.executor else 0
                await update.message.reply_text(
                    f"🔴 *Engine STOPPED*\n"
                    f"📊 Session trades: `{ec}`",
                    parse_mode='MarkdownV2'
                )
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /status - Engine status, WS connections, latency, uptime. """
        if not self._check_auth(update): return
        
        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized")
            return
        
        ws_status = self.engine.ws_pool.get_status() if self.engine.ws_pool else {}
        uptime = time.time() - self.engine.start_time if self.engine.start_time else 0
        uptime_str = f"{int(uptime//3600)}h {int((uptime%3600)//60)}m"
        
        text = f"📊 *Engine Status*\n\n"
        text += f"⚙️ Running: `{'Yes' if self.engine.running else 'No'}`\n"
        text += f"🔄 Mode: `{_esc(self.engine.settings.trading_mode)}`\n"
        text += f"⏱ Uptime: `{_esc(uptime_str)}`\n"
        text += f"📈 Pairs: `{len(self.engine.scanner.common_symbols)}`\n"
        text += f"🎯 Open positions: `{self.engine.position_tracker.open_count}`\n"
        text += f"📊 Signals: `{self.engine.spread_engine._signal_count}`\n"
        text += f"🚫 Rejected: `{self.engine.spread_engine._rejected_count}`\n\n"
        
        # WS status
        text += "*WebSocket Connections*\n"
        for exchange in ['bybit', 'gateio']:
            conns = ws_status.get(exchange, [])
            for conn in conns:
                emoji = '🟢' if conn['status'] == 'connected' else '🔴'
                text += f"{emoji} {exchange} \\#{conn['index']}: `{_esc(conn['status'])}` ({conn['symbols']} pairs)\n"
        
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /mode paper | /mode live - Switch trading mode. Only when auto=off. """
        if not self._check_auth(update): return
        
        args = context.args
        if not args or args[0] not in ('paper', 'live'):
            await update.message.reply_text("Usage: /mode paper | /mode live")
            return
        
        if self.engine and self.engine.running:
            await update.message.reply_text("⚠️ Stop engine first: /auto off")
            return
        
        new_mode = args[0]
        if self.engine:
            from dataclasses import replace
            self.engine.settings = replace(self.engine.settings, trading_mode=new_mode)
            self.engine.executor.mode = new_mode
            self.engine.position_tracker.mode = new_mode
        
        emoji = '📝' if new_mode == 'paper' else '🔴'
        await update.message.reply_text(
            f"{emoji} Mode switched to `{_esc(new_mode)}`",
            parse_mode='MarkdownV2'
        )
    
    async def cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /portfolio - Account balances & open positions. """
        if not self._check_auth(update): return
        
        mode = self.engine.settings.trading_mode if self.engine else 'paper'
        positions = self.engine.position_tracker.get_all_open() if self.engine else []
        
        text = f"💰 *Portfolio* ({_esc(mode)})\n\n"
        
        if mode == 'paper':
            balance = self.engine.paper_engine.get_balance() if self.engine.paper_engine else 0
            text += f"💵 Balance: `{balance:.2f}` USDT\n"
        else:
            text += "💵 Fetching live balances\\.\\.\\.\n"
        
        text += f"📊 Open: `{len(positions)}` positions\n\n"
        
        if positions:
            for pos in positions:
                esc_size = str(pos['size_usdt']).replace('.', '\\.')
                text += f"• `{pos['symbol']}` {pos['direction']} ({esc_size} USDT)\n"
        
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /history - Trade summary. /history detail - Last 10 trades. """
        if not self._check_auth(update): return
        
        args = context.args
        mode = self.engine.settings.trading_mode if self.engine else 'paper'
        
        if args and args[0] == 'detail':
            trades = await self.engine.db.get_trade_history(mode, days=30) if self.engine else []
            text = "📈 *Last 10 Trades*\n\n"
            for t in trades[:10]:
                net = t.get('net_pnl', 0) or 0
                emoji = '🟢' if net >= 0 else '🔴'
                esc_net = f"{net:.4f}".replace('.', '\\.')
                text += f"{emoji} `{t['symbol']}` {t.get('direction','')} net=`{esc_net}`\n"
        else:
            text = "📈 *Trade Summary*\n\n"
            for days, label in [(1, '24h'), (7, '7d'), (30, '30d')]:
                if self.engine:
                    summary = await self.engine.db.get_trade_summary(mode, days)
                    text += f"{label}: `{summary.get('count', 0)}` trades, net PnL: `{summary.get('net_pnl', 0):.4f}` USDT\n"
        
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    
    async def cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /top - Top 5 pair spread tertinggi saat ini. """
        if not self._check_auth(update): return
        
        if not self.engine or not self.engine.price_cache:
            await update.message.reply_text("❌ Engine not running")
            return
        
        spreads = []
        for symbol in self.engine.scanner.common_symbols:
            s = self.engine.price_cache.calc_spread(symbol)
            if s is not None:
                spreads.append((symbol, s))
        
        spreads.sort(key=lambda x: abs(x[1]), reverse=True)
        
        text = "🔥 *Top Spread Pairs*\n\n"
        if not spreads:
            text += "_Waiting for price data\\.\\.\\. try again in a few seconds_"
        else:
            for sym, spread in spreads[:5]:
                direction = "📈" if spread > 0 else "📉"
                esc_spread = f"{spread:.3f}%".replace('.', '\\.')
                text += f"{direction} `{sym}`: `{esc_spread}`\n"
        
        await update.message.reply_text(text, parse_mode='MarkdownV2')
    
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /cancel - Cancel all pending orders. """
        if not self._check_auth(update): return
        await update.message.reply_text("ℹ️ No pending orders to cancel (market orders only)")