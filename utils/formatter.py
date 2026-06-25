"""
Telegram MarkdownV2 message formatters for the Perpetual Arbitrage Bot.

All public functions return strings that are safe to send via the Telegram
Bot API with ``parse_mode="MarkdownV2"``.

Emoji legend:
    🟢  profit / long / connected
    🔴  loss / short / disconnected
    🟡  warning / pending
    ⚙️  system / config
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# MarkdownV2 escaping helper
# ---------------------------------------------------------------------------

# Characters that Telegram requires to be escaped in MarkdownV2 mode.
_TELEGRAM_MDV2_SPECIALS = r"\_*[]()~`>#+-=|{ }.!"


def _esc(value: str) -> str:
    """Escape a string for Telegram MarkdownV2."""
    out: list[str] = []
    for ch in str(value):
        if ch in _TELEGRAM_MDV2_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _fmt_pct(value: float) -> str:
    """Format a percentage with sign, e.g. ``+1.23%`` or ``-0.45%``."""
    sign = "+" if value >= 0 else ""
    return _esc(f"{sign}{value:.2f}%")


def _fmt_usdt(value: float) -> str:
    """Format a USDT amount."""
    sign = "+" if value >= 0 else ""
    return _esc(f"{sign}{value:.2f} USDT")


def _fmt_price(value: float) -> str:
    """Format a price with 4 decimal places."""
    return _esc(f"{value:.4f}")


def _fmt_duration(seconds: float) -> str:
    """Format duration as human-readable string."""
    if seconds < 60:
        return _esc(f"{seconds:.0f}s")
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return _esc(f"{m}m {s}s")
    else:
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        return _esc(f"{h}h {m}m {s}s")


def _pnl_emoji(value: float) -> str:
    return "🟢" if value >= 0 else "🔴"


def _direction_emoji(direction: str) -> str:
    d = direction.lower()
    if d in ("long", "buy"):
        return "🟢"
    elif d in ("short", "sell"):
        return "🔴"
    return "🟡"


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_trade_open(
    symbol: str,
    direction: str,
    bybit_price: float,
    gateio_price: float,
    spread_pct: float,
    size_usdt: float,
) -> str:
    """
    Format a *trade opened* notification for Telegram MarkdownV2.
    """
    d_emoji = _direction_emoji(direction)
    lines = [
        f"{d_emoji} *{_esc('TRADE OPENED')}*",
        "",
        f"*Symbol:* {_esc(symbol)}",
        f"*Direction:* {_esc(direction.upper())}  {d_emoji}",
        f"*Bybit Price:* {_fmt_price(bybit_price)}",
        f"*Gate\\.io Price:* {_fmt_price(gateio_price)}",
        f"*Spread:* {_fmt_pct(spread_pct)}",
        f"*Size:* {_esc(f'{size_usdt:.2f}')} USDT",
    ]
    return "\n".join(lines)


def format_trade_close(
    symbol: str,
    direction: str,
    exit_bybit: float,
    exit_gateio: float,
    gross_pnl: float,
    fee: float,
    net_pnl: float,
    duration_sec: float,
) -> str:
    """
    Format a *trade closed* notification for Telegram MarkdownV2.
    """
    p_emoji = _pnl_emoji(net_pnl)
    lines = [
        f"{p_emoji} *{_esc('TRADE CLOSED')}*",
        "",
        f"*Symbol:* {_esc(symbol)}",
        f"*Direction:* {_esc(direction.upper())}",
        f"*Exit Bybit:* {_fmt_price(exit_bybit)}",
        f"*Exit Gate\\.io:* {_fmt_price(exit_gateio)}",
        f"*Gross PnL:* {_fmt_usdt(gross_pnl)}",
        f"*Fees:* {_esc(f'-{fee:.2f}')} USDT",
        f"*Net PnL:* {_fmt_usdt(net_pnl)} {p_emoji}",
        f"*Duration:* {_fmt_duration(duration_sec)}",
    ]
    return "\n".join(lines)


def format_status(
    engine_on: bool,
    mode: str,
    ws_status: Dict[str, bool],
    uptime: float,
    latency: Dict[str, Optional[float]],
) -> str:
    """
    Format an engine status overview for Telegram MarkdownV2.

    Parameters
    ----------
    engine_on : bool
    mode : str
        ``'paper'`` or ``'live'``.
    ws_status : dict
        Mapping of exchange name to connected boolean, e.g.
        ``{"bybit": True, "gateio": False}``.
    uptime : float
        Seconds since engine start.
    latency : dict
        Mapping of exchange name to latency in ms (or *None*).
    """
    engine_emoji = "🟢" if engine_on else "🔴"
    mode_emoji = "⚙️" if mode == "live" else "🟡"

    lines = [
        f"⚙️ *{_esc('ENGINE STATUS')}*",
        "",
        f"*Engine:* {'ON' if engine_on else 'OFF'} {engine_emoji}",
        f"*Mode:* {_esc(mode.upper())} {mode_emoji}",
        f"*Uptime:* {_fmt_duration(uptime)}",
        "",
        "*WebSocket Status:*",
    ]

    for exchange, connected in ws_status.items():
        ws_emoji = "🟢" if connected else "🔴"
        lat = latency.get(exchange)
        lat_str = f"{lat:.1f} ms" if lat is not None else "N/A"
        lines.append(
            f"  {_esc(exchange.capitalize())}: {'Connected' if connected else 'Disconnected'} "
            f"{ws_emoji}  \\({_esc(lat_str)}\\)"
        )

    return "\n".join(lines)


def format_portfolio(
    balances: Dict[str, float],
    positions: List[Dict[str, Any]],
) -> str:
    """
    Format portfolio summary for Telegram MarkdownV2.

    Parameters
    ----------
    balances : dict
        ``{"USDT": 1234.56, ...}``
    positions : list of dict
        Each dict should have keys: ``symbol``, ``direction``, ``size``,
        ``entry_price``, ``unrealised_pnl``.
    """
    lines = [
        f"⚙️ *{_esc('PORTFOLIO')}*",
        "",
        "*Balances:*",
    ]

    for asset, amount in balances.items():
        lines.append(f"  {_esc(asset)}: {_esc(f'{amount:.4f}')}")

    lines.append("")

    if positions:
        lines.append("*Open Positions:*")
        for pos in positions:
            pnl = pos.get("unrealised_pnl", 0.0)
            p_emoji = _pnl_emoji(pnl)
            sym = _esc(pos.get("symbol", "?"))
            direction = _esc(pos.get("direction", "?").upper())
            size_str = _esc("{:.4f}".format(pos.get("size", 0)))
            entry = _fmt_price(pos.get("entry_price", 0))
            upnl = _fmt_usdt(pnl)
            lines.append(
                f"  {p_emoji} {sym} "
                f"\\| {direction} "
                f"\\| Size: {size_str} "
                f"\\| Entry: {entry} "
                f"\\| uPnL: {upnl}"
            )
    else:
        lines.append("_No open positions_")

    return "\n".join(lines)


def format_history(
    trades: Sequence[Dict[str, Any]],
    period: str = "today",
) -> str:
    """
    Format trade history summary for Telegram MarkdownV2.

    Parameters
    ----------
    trades : sequence of dict
        Each dict should have: ``symbol``, ``direction``, ``net_pnl``,
        ``duration_sec``, ``opened_at``.
    period : str
        Human-readable period label (e.g. ``'today'``, ``'7d'``, ``'all'``).
    """
    total_pnl = sum(t.get("net_pnl", 0.0) for t in trades)
    wins = sum(1 for t in trades if t.get("net_pnl", 0.0) > 0)
    losses = sum(1 for t in trades if t.get("net_pnl", 0.0) <= 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    header_emoji = _pnl_emoji(total_pnl)

    lines = [
        f"{header_emoji} *{_esc('TRADE HISTORY')}* \\({_esc(period)}\\)",
        "",
        f"*Total Trades:* {_esc(str(len(trades)))}",
        f"*Wins / Losses:* {_esc(str(wins))} / {_esc(str(losses))}",
        f"*Win Rate:* {_esc(f'{win_rate:.1f}')}%",
        f"*Total PnL:* {_fmt_usdt(total_pnl)} {header_emoji}",
        "",
    ]

    # Show last 10 trades
    recent = list(trades)[-10:]
    if recent:
        lines.append("*Recent trades:*")
        for t in recent:
            pnl = t.get("net_pnl", 0.0)
            e = _pnl_emoji(pnl)
            lines.append(
                f"  {e} {_esc(t.get('symbol', '?'))} "
                f"{_fmt_usdt(pnl)} "
                f"\\({_fmt_duration(t.get('duration_sec', 0))}\\)"
            )

    return "\n".join(lines)
