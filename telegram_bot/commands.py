import time
from telegram import Update
from telegram.ext import ContextTypes
from utils.logger import get_logger
from utils.telegram_escape import esc, fmt_price, fmt_pnl, fmt_pct, fmt_duration, fmt_usdt

logger = get_logger('commands')

SEP = "━━━━━━━━━━━━━━━━"


class CommandHandler:
    def __init__(self, engine, authorized_user_id: int):
        self.engine = engine
        self.authorized_user_id = authorized_user_id

    def _check_auth(self, update: Update) -> bool:
        return update.effective_user.id == self.authorized_user_id

    # ── /start ────────────────────────────────────────────────────────
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /start — Show WS connection status, not just 'Ready'. """
        if not self._check_auth(update):
            return

        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized", parse_mode="MarkdownV2")
            return

        s = self.engine.settings
        ws_pool = self.engine.ws_pool

        if ws_pool:
            ws_bybit = "🟢 Connected" if any(c.status == 'connected' for c in ws_pool.bybit_connections) else "🔴 Disconnected"
            ws_gateio = "🟢 Connected" if any(c.status == 'connected' for c in ws_pool.gateio_connections) else "🔴 Disconnected"
        else:
            ws_bybit = ws_gateio = "⚪ N/A"

        text = (
            f"🤖 *Arbitrage Bot* \\(Bybit × Gate\\.io\\)\n"
            f"{SEP}\n"
            f"Mode       `{esc(s.trading_mode.upper())}`\n"
            f"Engine     `{'🟢 Running' if self.engine.running else '⛔ Stopped'}`\n"
            f"Bybit WS   {ws_bybit}\n"
            f"Gate\\.io WS {ws_gateio}\n"
            f"{SEP}\n"
            f"Use /help to see all commands"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")

    # ── /help ────────────────────────────────────────────────────────
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /help — Group commands by function. """
        if not self._check_auth(update):
            return

        text = (
            f"📋 *Available Commands*\n"
            f"{SEP}\n"
            f"*Automation*\n"
            f"▶️ /auto on — Start automation engine\n"
            f"⏹️ /auto off — Stop engine \\(closes all positions\\)\n\n"
            f"*Info*\n"
            f"📊 /status — Engine, WS, latency, uptime\n"
            f"🔥 /top — Top 5 spread pairs \\+ direction\n"
            f"💰 /portfolio — Balances \\& open positions\n"
            f"📈 /history — Trade summary \\(1D/7D/30D\\)\n"
            f"📈 /history detail — Last 10 trades\n\n"
            f"*Config*\n"
            f"🔄 /mode paper — Switch to paper mode\n"
            f"🔄 /mode live — Switch to live mode \\(auto=off required\\)\n"
            f"❌ /cancel — Cancel all pending orders\n"
            f"{SEP}\n"
            f"📄 /mode — show current mode\n"
            f"⚙️ /auto — show engine status"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")

    # ── /auto ────────────────────────────────────────────────────────
    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /auto on | /auto off | /auto — Show engine status. """
        if not self._check_auth(update):
            return

        args = context.args

        # Show status if no args
        if not args:
            running = self.engine and self.engine.running
            status = "🟢 Running" if running else "⛔ Stopped"
            pos_count = len(self.engine.position_tracker.get_all_open()) if self.engine and self.engine.position_tracker else 0
            mode = self.engine.settings.trading_mode if self.engine else "paper"
            text = (
                f"⚙️ *Engine Status*\n"
                f"{SEP}\n"
                f"Status      {status}\n"
                f"Mode        `{esc(mode)}`\n"
                f"Open pos    `{pos_count}`\n"
                f"{SEP}\n"
                f"Use `/auto on` or `/auto off`"
            )
            await update.message.reply_text(text, parse_mode="MarkdownV2")
            return

        action = args[0]
        if action == "on":
            if self.engine and self.engine.running:
                await update.message.reply_text("⏳ Engine already running", parse_mode="MarkdownV2")
                return
            if self.engine:
                await self.engine.start()
                pairs = len(self.engine.scanner.common_symbols) if self.engine.scanner else 0
                await update.message.reply_text(
                    f"🟢 *Engine STARTED*\n"
                    f"⚙️ Mode: `{esc(self.engine.settings.trading_mode)}`\n"
                    f"📊 Monitoring: `{pairs}` pairs",
                    parse_mode="MarkdownV2"
                )
        elif action == "off":
            if self.engine:
                await self.engine.stop()
                ec = self.engine.executor._execution_count if self.engine.executor else 0
                await update.message.reply_text(
                    f"🔴 *Engine STOPPED*\n"
                    f"📊 Session trades: `{ec}`",
                    parse_mode="MarkdownV2"
                )

    # ── /status ──────────────────────────────────────────────────────
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /status — Comprehensive engine + WS health. """
        if not self._check_auth(update):
            return

        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized", parse_mode="MarkdownV2")
            return

        s = self.engine.settings
        running = self.engine.running
        uptime = int(time.time() - self.engine.start_time) if self.engine.start_time and running else 0
        uptime_str = fmt_duration(uptime) if running else "—"
        pairs = len(self.engine.scanner.common_symbols)
        open_pos = self.engine.position_tracker.open_count if self.engine.position_tracker else 0
        mode = esc(s.trading_mode.upper())
        sig_count = getattr(self.engine.spread_engine, '_signal_count', 0) if self.engine.spread_engine else 0
        rej_count = getattr(self.engine.spread_engine, '_rejected_count', 0) if self.engine.spread_engine else 0

        # WS pool status
        ws_lines = []
        ws_pool = self.engine.ws_pool
        if ws_pool:
            for exchange, conns in [("bybit", getattr(ws_pool, "bybit_connections", [])),
                                     ("gateio", getattr(ws_pool, "gateio_connections", []))]:
                for conn in conns:
                    icon = "🟢" if conn.status == "connected" else "🔴"
                    lag = f"{conn.last_latency_ms}ms" if conn.last_latency_ms else "—"
                    ws_lines.append(
                        f"  {icon} {esc(exchange)} WS\\#{conn.index}  "
                        f"`{esc(str(len(conn.symbols)))} pairs`  `{esc(lag)}`"
                    )

        text = (
            f"⚙️ *Engine Status*\n"
            f"{SEP}\n"
            f"Status      `{'🟢 Running' if running else '⛔ Stopped'}`\n"
            f"Mode        `{mode}`\n"
            f"Uptime      `{esc(uptime_str)}`\n"
            f"Pairs       `{pairs}`\n"
            f"Open pos    `{open_pos}/{s.max_open_positions}`\n"
            f"Signals     `{sig_count}`  Rejected `{rej_count}`\n"
        )
        if ws_lines:
            text += f"{SEP}\n*WebSocket Connections*\n" + "\n".join(ws_lines) + "\n"
        text += (
            f"{SEP}\n"
            f"Threshold   `{fmt_pct(s.internal_threshold)}` gross\n"
            f"Target net  `{fmt_pct(s.spread_entry_threshold)}`"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")

    # ── /mode ────────────────────────────────────────────────────────
    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /mode paper | /mode live | /mode — Show current mode. """
        if not self._check_auth(update):
            return

        args = context.args

        # Show mode if no args
        if not args:
            current = self.engine.settings.trading_mode if self.engine else "paper"
            emoji = "📄" if current == "paper" else "💰"
            other = "live" if current == "paper" else "paper"
            text = (
                f"{emoji} *Current Mode: {esc(current.upper())}*\n"
                f"{SEP}\n"
                f"To switch: `/mode {other}`\n"
                f"_Note: engine must be stopped to switch modes_"
            )
            await update.message.reply_text(text, parse_mode="MarkdownV2")
            return

        new_mode = args[0]
        if new_mode not in ("paper", "live"):
            await update.message.reply_text("Usage: /mode paper | /mode live", parse_mode="MarkdownV2")
            return

        if self.engine and self.engine.running:
            await update.message.reply_text("⏳ Stop engine first: /auto off", parse_mode="MarkdownV2")
            return

        if self.engine:
            from dataclasses import replace
            self.engine.settings = replace(self.engine.settings, trading_mode=new_mode)
            self.engine.executor.mode = new_mode
            self.engine.position_tracker.mode = new_mode

        emoji = "📝" if new_mode == "paper" else "🔴"
        await update.message.reply_text(
            f"{emoji} Mode switched to `{esc(new_mode)}`",
            parse_mode="MarkdownV2"
        )

    # ── /portfolio ──────────────────────────────────────────────────
    async def cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /portfolio — Balances & open positions detail. """
        if not self._check_auth(update):
            return

        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized", parse_mode="MarkdownV2")
            return

        s = self.engine.settings
        mode = s.trading_mode
        mode_label = esc(mode.capitalize())
        positions = self.engine.position_tracker.get_all_open() if self.engine.position_tracker else []
        paper_balance = self.engine.paper_engine.get_balance() if self.engine.paper_engine else 0

        text = f"💼 *Portfolio*  •  {mode_label}\n{SEP}\n"

        if mode == "paper":
            text += f"*Balances*\nBybit     `{fmt_usdt(paper_balance / 2)}`\nGate\\.io  `{fmt_usdt(paper_balance / 2)}`\nTotal     `{fmt_usdt(paper_balance)}`\n"
        else:
            text += "*Balances*\n💵 Fetching live balances\\.\\.\\.\n"

        if positions:
            text += f"{SEP}\n*Open Positions \\({len(positions)}\\)*\n"
            for pos in positions:
                symbol = esc(pos.get("symbol", "?"))
                direction = pos.get("direction", "")
                if direction == "long_bybit":
                    long_ex = "Bybit"
                    short_ex = "Gate\\.io"
                    long_price = pos.get("entry_price_bybit", 0)
                    short_price = pos.get("entry_price_gateio", 0)
                else:
                    long_ex = "Gate\\.io"
                    short_ex = "Bybit"
                    long_price = pos.get("entry_price_gateio", 0)
                    short_price = pos.get("entry_price_bybit", 0)

                size = pos.get("size_usdt", 0)
                entry_ts = pos.get("entry_ts", 0)
                now_ms = int(time.time() * 1000)
                duration = max(0, (now_ms - entry_ts) // 1000) if entry_ts else 0

                # Calculate current spread
                symbol_raw = pos.get("symbol", "")
                current_spread = self.engine.price_cache.calc_spread(symbol_raw) if self.engine.price_cache else None
                spread_str = f"{fmt_pct(current_spread)}" if current_spread is not None else "—"

                text += (
                    f"🟢 *{symbol}*\n"
                    f"   📈 Long {long_ex} @ `{fmt_price(long_price)}`  Size `{esc(f'{size:.0f}')} USDT`\n"
                    f"   📉 Short {short_ex} @ `{fmt_price(short_price)}`\n"
                    f"   📐 Spread now: `{spread_str}` · Open `{esc(fmt_duration(duration))}`\n"
                )
        else:
            text += "No open positions\n"

        # Session PnL
        try:
            summary = await self.engine.db.get_trade_summary(mode, days=1)
            total_pnl = summary.get("total_pnl", 0) if summary else 0
            pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
            text += f"{SEP}\nSession PnL  `{fmt_pnl(total_pnl)}` USDT {pnl_emoji}"
        except Exception:
            pass

        await update.message.reply_text(text, parse_mode="MarkdownV2")

    # ── /history ─────────────────────────────────────────────────────
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /history — Trade summary (24h/7d/30d) with mode filter and UTC timestamps.
            /history detail — Last 10 trades with full context. """
        if not self._check_auth(update):
            return

        if not self.engine:
            await update.message.reply_text("❌ Engine not initialized", parse_mode="MarkdownV2")
            return

        args = context.args
        mode = self.engine.settings.trading_mode
        mode_label = esc(mode.capitalize())
        db = self.engine.db

        # ── detail mode ──
        if args and args[0] == "detail":
            trades = await db.get_trade_history(mode, days=30)
            lines = [f"📋 *Last 10 Trades*  •  {mode_label}\n{SEP}"]
            total_pnl = 0

            for idx, t in enumerate(trades[:10], 1):
                net_pnl = t.get("net_pnl", 0) or 0
                total_pnl += net_pnl
                pnl_emoji = "✅" if net_pnl >= 0 else "❌"
                symbol = t.get("symbol", "?")
                direction = t.get("direction", "")
                signal_spread = t.get("signal_spread_pct") or 0
                actual_spread = t.get("actual_spread_pct") or 0
                entry_ts = t.get("entry_ts", 0)
                exit_ts = t.get("exit_ts", 0)
                duration = max(0, (exit_ts - entry_ts) // 1000) if entry_ts and exit_ts else 0

                if direction == "long_bybit":
                    dir_label = "Long Bybit · Short Gate\\.io"
                else:
                    dir_label = "Long Gate\\.io · Short Bybit"

                lines.append(
                    f"{idx}\\. *{esc(symbol)}*  {pnl_emoji} {fmt_pnl(net_pnl)} USDT\n"
                    f"   Spread: `{fmt_pct(signal_spread)}` → `{fmt_pct(actual_spread)}`  \\|  `{esc(fmt_duration(duration))}`\n"
                    f"   {dir_label}"
                )

            total_emoji = "🟢" if total_pnl >= 0 else "🔴"
            lines.append(f"{SEP}\nTotal: {fmt_pnl(total_pnl)} USDT {total_emoji}")
            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
            return

        # ── summary mode (fixed bug: UTC timestamps + mode filter + status='closed')
        # Use existing get_trade_summary which already queries status='closed'
        period_map = {"24h": 1, "7d": 7, "30d": 30}
        lines = [f"📈 *Trade Summary*  •  {mode_label}\n{SEP}"]

        for label, days in period_map.items():
            summary = await db.get_trade_summary(mode, days)
            cnt = summary.get("total_trades", 0) if summary else 0
            pnl = summary.get("total_pnl", 0.0) if summary else 0.0
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{label}:  `{cnt}` trades,  net PnL: `{fmt_pnl(pnl)}` USDT {pnl_emoji}")

        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    # ── /top ─────────────────────────────────────────────────────────
    async def cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /top — Top 5 spread pairs with direction + threshold status. """
        if not self._check_auth(update):
            return

        if not self.engine or not self.engine.price_cache:
            await update.message.reply_text("❌ Engine not running", parse_mode="MarkdownV2")
            return

        s = self.engine.settings
        threshold = s.internal_threshold
        mode_label = esc(s.trading_mode.capitalize())

        spreads = []
        for symbol in self.engine.scanner.common_symbols:
            spread = self.engine.price_cache.calc_spread(symbol)
            if spread is not None:
                spreads.append((symbol, spread))

        spreads.sort(key=lambda x: abs(x[1]), reverse=True)

        lines = [f"🔥 *Top Spread Pairs*  •  {mode_label}\n{SEP}"]

        if not spreads:
            lines.append("_Waiting for price data\\.\\.\\. try again in a few seconds_")
        else:
            for sym, spread in spreads[:5]:
                above = abs(spread) >= threshold
                status = "✅" if above else "⚠️"
                direction = (
                    "Short Bybit / Long Gate\\.io" if spread > 0
                    else "Long Bybit / Short Gate\\.io"
                )
                lines.append(
                    f"{status} `{esc(sym)}`  *{fmt_pct(spread)}*\n"
                    f"    └ _{direction}_"
                )
            lines.append(f"{SEP}\n✅ = above threshold ({fmt_pct(threshold)})")

        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    # ── /cancel ──────────────────────────────────────────────────────
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """ /cancel — Cancel pending orders (market orders only, usually none). """
        if not self._check_auth(update):
            return
        await update.message.reply_text("ℹ️ No pending orders to cancel (market orders only)", parse_mode="MarkdownV2")