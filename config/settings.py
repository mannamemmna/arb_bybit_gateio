"""
Configuration loader for the Perpetual Arbitrage Bot.
Loads all settings from .env, validates required keys, computes derived values,
and exposes a Settings dataclass.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    # Telegram
    telegram_bot_token: str
    telegram_user_id: int

    # Bybit
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool

    # Gate.io
    gateio_api_key: str
    gateio_api_secret: str

    # Trading
    trading_mode: str  # 'paper' | 'live'

    # Strategy
    spread_entry_threshold: float
    spread_exit_threshold: float
    max_position_usdt: float
    leverage: int
    max_open_positions: int

    # Engine
    ws_reconnect_delay_sec: int
    ws_max_retries: int
    ws_heartbeat_sec: int
    price_staleness_ms: int
    rest_fallback_interval_sec: float

    # WS Pool
    bybit_ws_url: str
    gateio_ws_url: str
    ws_max_subs_per_conn: int

    # Fees
    taker_fee_bybit: float
    taker_fee_gateio: float

    # Slippage & Execution Guard
    slippage_buffer: float
    preflight_max_age_ms: int
    preflight_spread_decay: float
    use_orderbook_depth_check: bool
    orderbook_depth: int

    # Paper Mode
    paper_initial_balance_usdt: float
    paper_slippage_pct: float

    # Logging
    log_level: str
    log_file: str

    # Derived (computed at load time)
    internal_threshold: float
    total_round_trip_fee: float


# ---------------------------------------------------------------------------
# Helper loaders
# ---------------------------------------------------------------------------

def _require_str(key: str) -> str:
    """Return a required string env var or raise ValueError."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        raise ValueError(f"Missing required environment variable: {key}")
    return val.strip()


def _get_str(key: str, default: str) -> str:
    val = os.getenv(key)
    return val.strip() if val is not None else default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"Environment variable {key} must be an integer, got: {val!r}")


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        raise ValueError(f"Environment variable {key} must be a float, got: {val!r}")


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_settings(env_file: str | None = None) -> Settings:
    """
    Load settings from environment / .env file.

    Parameters
    ----------
    env_file : str | None
        Explicit path to a .env file.  When *None*, python-dotenv searches
        the current working directory and parents automatically.

    Returns
    -------
    Settings
        A frozen dataclass with every configuration value.

    Raises
    ------
    ValueError
        If any required key is missing or a value has an invalid type.
    """
    load_dotenv(dotenv_path=env_file, override=False)

    # --- Required keys (will raise on missing) ---
    telegram_bot_token = _require_str("TELEGRAM_BOT_TOKEN")
    telegram_user_id = int(_require_str("TELEGRAM_USER_ID"))
    bybit_api_key = _require_str("BYBIT_API_KEY")
    bybit_api_secret = _require_str("BYBIT_API_SECRET")
    gateio_api_key = _require_str("GATEIO_API_KEY")
    gateio_api_secret = _require_str("GATEIO_API_SECRET")

    # --- Typed with defaults ---
    bybit_testnet = _get_bool("BYBIT_TESTNET", False)
    trading_mode = _get_str("TRADING_MODE", "paper").lower()
    if trading_mode not in ("paper", "live"):
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got: {trading_mode!r}")

    spread_entry_threshold = _get_float("SPREAD_ENTRY_THRESHOLD", 0.5)
    spread_exit_threshold = _get_float("SPREAD_EXIT_THRESHOLD", 0.05)
    max_position_usdt = _get_float("MAX_POSITION_USDT", 50.0)
    leverage = _get_int("LEVERAGE", 5)
    max_open_positions = _get_int("MAX_OPEN_POSITIONS", 5)

    ws_reconnect_delay_sec = _get_int("WS_RECONNECT_DELAY_SEC", 5)
    ws_max_retries = _get_int("WS_MAX_RETRIES", 10)
    ws_heartbeat_sec = _get_int("WS_HEARTBEAT_SEC", 20)
    price_staleness_ms = _get_int("PRICE_STALENESS_MS", 500)
    rest_fallback_interval_sec = _get_float("REST_FALLBACK_INTERVAL_SEC", 1.0)

    bybit_ws_url = _get_str("BYBIT_WS_URL", "wss://stream.bybit.com/v5/public/linear")
    gateio_ws_url = _get_str("GATEIO_WS_URL", "wss://fx-ws.gateio.ws/v4/ws/usdt")
    ws_max_subs_per_conn = _get_int("WS_MAX_SUBS_PER_CONN", 250)

    taker_fee_bybit = _get_float("TAKER_FEE_BYBIT", 0.0006)
    taker_fee_gateio = _get_float("TAKER_FEE_GATEIO", 0.0005)

    slippage_buffer = _get_float("SLIPPAGE_BUFFER", 0.001)
    preflight_max_age_ms = _get_int("PREFLIGHT_MAX_AGE_MS", 500)
    preflight_spread_decay = _get_float("PREFLIGHT_SPREAD_DECAY", 0.30)
    use_orderbook_depth_check = _get_bool("USE_ORDERBOOK_DEPTH_CHECK", True)
    orderbook_depth = _get_int("ORDERBOOK_DEPTH", 5)

    paper_initial_balance_usdt = _get_float("PAPER_INITIAL_BALANCE_USDT", 1000.0)
    paper_slippage_pct = _get_float("PAPER_SLIPPAGE_PCT", 0.0005)

    log_level = _get_str("LOG_LEVEL", "INFO").upper()
    log_file = _get_str("LOG_FILE", "logs/arb_bot.log")

    # --- Derived values ---
    total_round_trip_fee = (taker_fee_bybit + taker_fee_gateio) * 2
    internal_threshold = spread_entry_threshold + total_round_trip_fee + slippage_buffer

    return Settings(
        telegram_bot_token=telegram_bot_token,
        telegram_user_id=telegram_user_id,
        bybit_api_key=bybit_api_key,
        bybit_api_secret=bybit_api_secret,
        bybit_testnet=bybit_testnet,
        gateio_api_key=gateio_api_key,
        gateio_api_secret=gateio_api_secret,
        trading_mode=trading_mode,
        spread_entry_threshold=spread_entry_threshold,
        spread_exit_threshold=spread_exit_threshold,
        max_position_usdt=max_position_usdt,
        leverage=leverage,
        max_open_positions=max_open_positions,
        ws_reconnect_delay_sec=ws_reconnect_delay_sec,
        ws_max_retries=ws_max_retries,
        ws_heartbeat_sec=ws_heartbeat_sec,
        price_staleness_ms=price_staleness_ms,
        rest_fallback_interval_sec=rest_fallback_interval_sec,
        bybit_ws_url=bybit_ws_url,
        gateio_ws_url=gateio_ws_url,
        ws_max_subs_per_conn=ws_max_subs_per_conn,
        taker_fee_bybit=taker_fee_bybit,
        taker_fee_gateio=taker_fee_gateio,
        slippage_buffer=slippage_buffer,
        preflight_max_age_ms=preflight_max_age_ms,
        preflight_spread_decay=preflight_spread_decay,
        use_orderbook_depth_check=use_orderbook_depth_check,
        orderbook_depth=orderbook_depth,
        paper_initial_balance_usdt=paper_initial_balance_usdt,
        paper_slippage_pct=paper_slippage_pct,
        log_level=log_level,
        log_file=log_file,
        internal_threshold=internal_threshold,
        total_round_trip_fee=total_round_trip_fee,
    )


# ---------------------------------------------------------------------------
# Module-level convenience: load once on import so `settings.X` works.
# If .env is missing or keys are absent the import will raise immediately,
# which is the desired fail-fast behaviour.
# ---------------------------------------------------------------------------
try:
    settings = load_settings()
except ValueError as exc:
    # Fail fast in production. Only allow None during test/doc discovery.
    if os.getenv("HERMES_TEST_MODE") or os.getenv("PYTEST_CURRENT_TEST"):
        print(f"[config.settings] WARNING – could not load settings: {exc}", file=sys.stderr)
        settings = None  # type: ignore[assignment]
    else:
        raise
