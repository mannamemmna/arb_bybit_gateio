"""
Auto-Rebalance CEX to CEX (Bybit ↔ Gate.io).

Detects USDT balance imbalance between exchanges and triggers on-chain transfers
to maintain target ratio. Supports demo mode (virtual ledger) and live mode
(real withdrawal API).
"""
from rebalance.rebalancer import RebalanceManager
from rebalance.tx_monitor import TxMonitor

__all__ = ["RebalanceManager", "TxMonitor"]
