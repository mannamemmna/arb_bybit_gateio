"""
Telegram MarkdownV2 escape helpers for the Perpetual Arbitrage Bot.

WAJIB digunakan untuk SEMUA dynamic value yang dikirim ke Telegram.
Karakter yang di-escape: _ * [ ] ( ) ~ ` > # + - = | { } . !
"""
import re

__all__ = ["esc", "fmt_price", "fmt_pnl", "fmt_pct", "fmt_duration", "fmt_usdt"]


def esc(text: str) -> str:
    """Escape semua special characters untuk Telegram MarkdownV2."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def fmt_price(price: float, decimals: int = 5) -> str:
    """Format harga dengan escape otomatis. Contoh: 0.03690"""
    return esc(f"{price:.{decimals}f}")


def fmt_pnl(pnl: float) -> str:
    """Format PnL dengan tanda + untuk profit. Contoh: +0.0328"""
    sign = "+" if pnl >= 0 else ""
    return esc(f"{sign}{pnl:.4f}")


def fmt_pct(pct: float) -> str:
    """Format persentase dengan tanda. Contoh: -0.552%"""
    sign = "+" if pct >= 0 else ""
    return esc(f"{sign}{pct:.3f}%")


def fmt_duration(seconds: int) -> str:
    """Format durasi ke bentuk human-readable."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    else:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m}m"


def fmt_usdt(amount: float) -> str:
    """Format USDT amount dengan thousands separator. Contoh: '1,234.56 USDT'"""
    return esc(f"{amount:,.2f} USDT")