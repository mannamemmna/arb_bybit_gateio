import time
from telegram import Bot
from utils.logger import get_logger
from utils.telegram_escape import esc, fmt_price, fmt_pnl, fmt_pct, fmt_duration, fmt_usdt

logger = get_logger("notifier")

SEP = "━━━━━━━━━━━━━━━━"


def _ts() -> str:
    """Return current UTC timestamp in Telegram-escaped format."""
    return esc(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))


class Notifier:
    """Sends push notifications to the authorized user."""

    def __init__(self, bot_token: str, user_id: int):
        self.bot = Bot(token=bot_token)
        self.user_id = int(user_id)

    async def send(self, text: str, parse_mode: str = "MarkdownV2"):
        try:
            await self.bot.send_message(
                chat_id=self.user_id, text=text, parse_mode=parse_mode
            )
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    # ── Engine START (spec section 3) ───────────────────────────────
    async def notify_engine_start(self, mode: str, pair_count: int):
        ws_conns = max(1, (pair_count // 250) + 1) * 2  # 2 exchanges
        text = (
            f"⚙️ *Engine Started*  •  {esc(mode.upper())}\n"
            f"{SEP}\n"
            f"Pairs monitored   `{pair_count}`\n"
            f"WS connections    `{ws_conns}`\n"
            f"Entry threshold   `{fmt_pct(0.5)}`\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    # ── Engine STOP (spec section 4) ────────────────────────────────
    async def notify_engine_stop(self, reason: str, trade_count: int, net_pnl: float,
                                 duration_sec: int = 0):
        text = (
            f"⛔ *Engine Stopped*\n"
            f"{SEP}\n"
            f"Session trades    `{trade_count}`\n"
            f"Session net PnL   `{fmt_pnl(net_pnl)}` USDT\n"
            f"Duration          `{fmt_duration(duration_sec)}`\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    # ── Trade OPEN (spec section 1) ─────────────────────────────────
    async def notify_trade_open(
        self,
        symbol: str,
        direction: str,
        bybit_price: float,
        gateio_price: float,
        spread_pct: float,
        size_usdt: float,
    ):
        mode = "Paper"
        if direction == "long_bybit":
            long_exchange = "Bybit"
            short_exchange = "Gate\\.io"
            long_price = bybit_price
            short_price = gateio_price
        else:
            long_exchange = "Gate\\.io"
            short_exchange = "Bybit"
            long_price = gateio_price
            short_price = bybit_price

        # Est net PnL: (|spread| - fee_round_trip) * size / 100
        fee_round_trip = 0.44  # 0.44% default
        est_net = abs(spread_pct) - fee_round_trip
        est_net_pnl = max(est_net, 0.0) * size_usdt / 100

        text = (
            f"⚡ TRADE OPEN  •  {mode}\n"
            f"{SEP}\n"
            f"Pair      *{esc(symbol)}*\n"
            f"🟢 Long   {long_exchange}  `{fmt_price(long_price)}`\n"
            f"🔴 Short  {short_exchange}  `{fmt_price(short_price)}`\n"
            f"{SEP}\n"
            f"Spread    `{fmt_pct(spread_pct)}`  \\(threshold {fmt_pct(0.5)}\\)\n"
            f"Size      `{fmt_price(size_usdt, decimals=2)}` USDT × 5×\n"
            f"Est\\.net  ~`{fmt_pnl(est_net_pnl)}` USDT\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    # ── Trade CLOSE (spec section 2) ────────────────────────────────
    async def notify_trade_close(
        self,
        symbol: str,
        direction: str,
        exit_bybit: float,
        exit_gateio: float,
        gross_pnl: float,
        fee: float,
        net_pnl: float,
        duration_sec: int,
        entry_price_bybit: float = None,
        entry_price_gateio: float = None,
        signal_spread_pct: float = None,
        slippage_pct: float = None,
    ):
        """Entry prices are optional (defaults to '—' when not passed)."""
        result_emoji = "✅" if net_pnl >= 0 else "❌"
        result_text = "🟢 Profit" if net_pnl >= 0 else "🔴 Loss"
        net_emoji = "🟢" if net_pnl >= 0 else "🔴"

        if direction == "long_bybit":
            long_ex = "Bybit"
            short_ex = "Gate\\.io"
            entry_long = fmt_price(entry_price_bybit) if entry_price_bybit is not None else "—"
            exit_long = fmt_price(exit_bybit)
            entry_short = fmt_price(entry_price_gateio) if entry_price_gateio is not None else "—"
            exit_short = fmt_price(exit_gateio)
        else:
            long_ex = "Gate\\.io"
            short_ex = "Bybit"
            entry_long = fmt_price(entry_price_gateio) if entry_price_gateio is not None else "—"
            exit_long = fmt_price(exit_gateio)
            entry_short = fmt_price(entry_price_bybit) if entry_price_bybit is not None else "—"
            exit_short = fmt_price(exit_bybit)

        spread_open = fmt_pct(signal_spread_pct) if signal_spread_pct is not None else "—"
        slip_str = fmt_pct(slippage_pct) if slippage_pct is not None else "—"

        text = (
            f"{result_emoji} TRADE CLOSE  •  {result_text}\n"
            f"{SEP}\n"
            f"Pair      *{esc(symbol)}*  \\({esc(fmt_duration(duration_sec))}\\)\n"
            f"🟢 Long   {long_ex}  `{esc(entry_long)}` → `{exit_long}`\n"
            f"🔴 Short  {short_ex}  `{esc(entry_short)}` → `{exit_short}`\n"
            f"{SEP}\n"
            f"Spread    `{spread_open}` → `—`\n"
            f"Slippage  `{slip_str}`\n"
            f"{SEP}\n"
            f"Gross     `{fmt_pnl(gross_pnl)}` USDT\n"
            f"Fee       {esc(f'-{fee:.4f}')} USDT\n"
            f"Net       `{fmt_pnl(net_pnl)}` USDT {net_emoji}"
        )
        await self.send(text)

    # ── WS Disconnect (spec section 5) ──────────────────────────────
    async def notify_ws_disconnect(self, exchange: str):
        text = (
            f"🟡 *WS Disconnected*\n"
            f"{SEP}\n"
            f"Exchange    {esc(exchange)}\n"
            f"Connection  WS \\#0\n"
            f"Retrying\\.\\.\\.  attempt 1/10\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    # ── WS Reconnect Failed (spec section 6) ────────────────────────
    async def notify_ws_reconnect_failed(self, exchange: str, retries: int):
        text = (
            f"🔴 *WS Failed — Manual Check Required*\n"
            f"{SEP}\n"
            f"Exchange    {esc(exchange)}\n"
            f"Connection  WS \\#0\n"
            f"Retried     {esc(str(retries))}× — giving up\n"
            f"Action      Engine auto\\-paused\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    # ── Rate limit warning (existing) ───────────────────────────────
    async def notify_rate_limit_warning(self, exchange: str, percentage: float):
        await self.send(
            f"🟡 *Rate Limit Warning*\n"
            f"{SEP}\n"
            f"Exchange: {esc(exchange)}\n"
            f"Usage: `{esc(f'{percentage:.0f}')}%` of limit reached"
        )

    async def notify_api_key_error(self, exchange: str, error: str):
        await self.send(
            f"🔴 *API Key Error*\n"
            f"{SEP}\n"
            f"Exchange: {esc(exchange)}\n"
            f"Error: {esc(error)}\n"
            f"Check your \\.env file"
        )

    async def notify_new_pairs(self, new_symbols: list):
        lines = [f"ℹ️ *New Pairs Detected*\n{SEP}"]
        for s in new_symbols[:10]:
            lines.append(f"• `{esc(s)}`")
        await self.send("\n".join(lines))

    # ── Rebalance notifications ─────────────────────────────────

    async def notify_rebalance_needed(self, balances: dict, imbalance: dict) -> None:
        bybit_pct = (balances['bybit'] / balances['total'] * 100) if balances['total'] > 0 else 0
        gateio_pct = (balances['gateio'] / balances['total'] * 100) if balances['total'] > 0 else 0
        text = (
            f"⚖️ *Rebalance Needed*\n"
            f"{SEP}\n"
            f"Bybit     `{fmt_usdt(balances['bybit'])}`  \\({esc(f'{bybit_pct:.1f}')}%\\)\n"
            f"Gate\\.io  `{fmt_usdt(balances['gateio'])}`  \\({esc(f'{gateio_pct:.1f}')}%\\)\n"
            f"Diff      `{fmt_usdt(imbalance['diff_usdt'])}`\n"
            f"{SEP}\n"
            f"Suggested transfer:\n"
            f"  `{fmt_usdt(imbalance['transfer_amount'])}` "
            f"{esc(imbalance['from_exchange'].capitalize())} → {esc(imbalance['to_exchange'].capitalize())}\n"
            f"  Network: {esc(imbalance.get('network', 'TRC20'))}\n"
            f"{SEP}\n"
            f"Reply `/rebalance` to execute"
        )
        await self.send(text)

    async def notify_rebalance_initiated(
        self, from_exchange: str, to_exchange: str, amount: float,
        fee: float, network: str, tx_id: str,
    ) -> None:
        text = (
            f"🔄 *Transfer Initiated*\n"
            f"{SEP}\n"
            f"From    {esc(from_exchange.capitalize())}\n"
            f"To      {esc(to_exchange.capitalize())}\n"
            f"Amount  `{fmt_usdt(amount)}`\n"
            f"Fee     `\\-{esc(f'{fee:.2f}')}` USDT\n"
            f"Network {esc(network)}\n"
            f"TX      `{esc(tx_id)}`\n"
            f"{SEP}\n"
            f"Waiting for confirmation\\.\\.\\.\n"
            f"\\(usually 1\\-3 minutes\\)\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    async def notify_rebalance_confirmed(
        self, from_exchange: str, to_exchange: str, amount: float,
        fee: float, tx_id: str, elapsed_sec: int, balances_after: dict,
    ) -> None:
        bybit_pct = (balances_after['bybit'] / balances_after['total'] * 100) if balances_after['total'] > 0 else 0
        gateio_pct = (balances_after['gateio'] / balances_after['total'] * 100) if balances_after['total'] > 0 else 0
        text = (
            f"✅ *Transfer Confirmed*\n"
            f"{SEP}\n"
            f"{esc(from_exchange.capitalize())} → {esc(to_exchange.capitalize())}  `{fmt_usdt(amount)}`\n"
            f"Fee     `\\-{esc(f'{fee:.2f}')}` USDT\n"
            f"Time    `{esc(fmt_duration(elapsed_sec))}`\n"
            f"{SEP}\n"
            f"*New Balances*\n"
            f"Bybit     `{fmt_usdt(balances_after['bybit'])}`  \\({esc(f'{bybit_pct:.1f}')}%\\)\n"
            f"Gate\\.io  `{fmt_usdt(balances_after['gateio'])}`  \\({esc(f'{gateio_pct:.1f}')}%\\)\n"
            f"{SEP}\n"
            f"TX `{esc(tx_id)}`\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    async def notify_rebalance_executed(
        self, mode: str, from_exchange: str, to_exchange: str, amount: float,
        status: str, balances_before: dict, balances_after: dict,
    ) -> None:
        tag = f" [{mode.upper()}]" if mode == "demo" else ""
        bybit_pct = (balances_after['bybit'] / balances_after['total'] * 100) if balances_after['total'] > 0 else 0
        gateio_pct = (balances_after['gateio'] / balances_after['total'] * 100) if balances_after['total'] > 0 else 0
        text = (
            f"🔄 *Transfer Simulated*{esc(tag)}\n"
            f"{SEP}\n"
            f"{esc(from_exchange.capitalize())} → {esc(to_exchange.capitalize())}  `{fmt_usdt(amount)}`\n"
            f"Fee     `\\-0\\.00` USDT \\(simulated\\)\n"
            f"{SEP}\n"
            f"*Virtual Balances After*\n"
            f"Bybit     `{fmt_usdt(balances_after['bybit'])}`  \\({esc(f'{bybit_pct:.1f}')}%\\)\n"
            f"Gate\\.io  `{fmt_usdt(balances_after['gateio'])}`  \\({esc(f'{gateio_pct:.1f}')}%\\)\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    async def notify_rebalance_failed(
        self, from_exchange: str, to_exchange: str, amount: float, error: str,
    ) -> None:
        text = (
            f"🔴 *Transfer Failed — Manual Check Required*\n"
            f"{SEP}\n"
            f"From    {esc(from_exchange.capitalize())}\n"
            f"To      {esc(to_exchange.capitalize())}\n"
            f"Amount  `{fmt_usdt(amount)}`\n"
            f"Error   {esc(error)}\n"
            f"{SEP}\n"
            f"Check exchange withdrawal history manually\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)

    async def notify_rebalance_timeout(self, tx_id: str, elapsed_sec: int) -> None:
        text = (
            f"🔴 *Transfer Monitoring Timeout*\n"
            f"{SEP}\n"
            f"TX      `{esc(tx_id)}`\n"
            f"Elapsed `{esc(fmt_duration(elapsed_sec))}`\n"
            f"Action  Check exchange withdrawal history manually\n"
            f"{SEP}\n"
            f"🕐 {_ts()}"
        )
        await self.send(text)