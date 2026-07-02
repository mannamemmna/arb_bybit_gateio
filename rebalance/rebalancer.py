"""
RebalanceManager — monitors USDT balance across Bybit and Gate.io and triggers
on-chain transfers when imbalance exceeds configured thresholds.

Modes:
  demo — fetch real balances, simulate transfer, update virtual ledger
  live — fetch real balances, execute real withdrawal via exchange withdrawal API

Flow:
  1. Fetch current USDT balance from both exchanges
  2. Check if imbalance >= REBALANCE_THRESHOLD_USDT or >= REBALANCE_THRESHOLD_PCT
  3. Calculate transfer amount: abs(current - target), clamped to [MIN_TRANSFER, MAX_TRANSFER]
  4a. Demo: log + simulate delay + update virtual balance + notify
  4b. Live: get deposit address → call withdrawal API → get tx id → monitor
"""
import asyncio
import time
from typing import Optional
from utils.logger import get_logger
from rebalance.tx_monitor import TxMonitor

logger = get_logger('rebalancer')


class RebalanceManager:
    """
    Monitors balance across both exchanges and triggers transfers when needed.
    """

    def __init__(self, bybit_client, gateio_client, settings, db, notifier):
        self.bybit = bybit_client
        self.gateio = gateio_client
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.mode = settings.rebalance_mode  # 'demo' | 'live'

        # Virtual ledger for demo mode — initialised 50/50
        self._demo_bybit_balance = settings.paper_initial_balance_usdt / 2
        self._demo_gateio_balance = settings.paper_initial_balance_usdt / 2

        # State
        self._pending_tx: Optional[str] = None
        self._pending_tx_info: Optional[dict] = None
        self._last_check_ts: float = 0
        self._is_running: bool = False
        self._auto_enabled: bool = settings.rebalance_auto_enabled  # mutable runtime override

        # For safety checks
        self._open_count_getter = None  # set externally: lambda: position_tracker.open_count
        self._is_executing_getter = None  # set externally: lambda: executor.is_executing
        self._on_trade_callback = None  # called by paper engine on trade close

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self):
        """Start periodic balance check loop."""
        if not self.settings.rebalance_enabled:
            logger.info("Rebalance disabled via config")
            return
        self._is_running = True
        asyncio.create_task(self._check_loop())
        logger.info("Rebalance manager started (mode=%s, interval=%ds)",
                     self.mode, self.settings.rebalance_check_interval_sec)

    async def stop(self):
        self._is_running = False

    def set_open_count_getter(self, fn):
        self._open_count_getter = fn

    def set_is_executing_getter(self, fn):
        self._is_executing_getter = fn

    def on_trade_closed(self, mode: str, bybit_balance_change: float, gateio_balance_change: float):
        """
        Called by paper engine when a trade closes so rebalancer's virtual
        balance stays in sync with paper account.
        """
        if mode == 'demo':
            self._demo_bybit_balance += bybit_balance_change
            self._demo_gateio_balance += gateio_balance_change
            logger.debug("Rebalance virtual ledger updated: bybit=%.2f gateio=%.2f",
                         self._demo_bybit_balance, self._demo_gateio_balance)

    # ── Periodic check loop ─────────────────────────────────────

    async def _check_loop(self):
        while self._is_running:
            await asyncio.sleep(self.settings.rebalance_check_interval_sec)
            if self._auto_enabled and not self._pending_tx:
                await self.check_and_rebalance(force=False)

    # ── Balance fetching ─────────────────────────────────────────

    async def get_balances(self) -> dict:
        """
        Fetch current USDT balances.

        Demo mode: return virtual ledger values
        Live mode: call exchange REST APIs
        """
        if self.mode == 'demo':
            return {
                'bybit': self._demo_bybit_balance,
                'gateio': self._demo_gateio_balance,
                'total': self._demo_bybit_balance + self._demo_gateio_balance,
                'source': 'virtual',
            }
        else:
            bybit_bal, gateio_bal = await asyncio.gather(
                self.bybit.get_wallet_balance(),
                self.gateio.get_wallet_balance(),
            )
            return {
                'bybit': bybit_bal,
                'gateio': gateio_bal,
                'total': bybit_bal + gateio_bal,
                'source': 'live',
            }

    # ── Imbalance calculation ────────────────────────────────────

    def _calc_imbalance(self, balances: dict) -> dict:
        """
        Calculate imbalance and required transfer.

        Returns dict with keys:
          needs_rebalance, from_exchange, to_exchange, transfer_amount,
          current_ratio, diff_usdt, diff_pct
        """
        bybit_bal = balances['bybit']
        gateio_bal = balances['gateio']
        total = balances['total']

        if total <= 0:
            return {'needs_rebalance': False}

        target_bybit = total * self.settings.rebalance_target_ratio
        diff_usdt = abs(bybit_bal - target_bybit)
        diff_pct = (diff_usdt / total) * 100 if total > 0 else 0

        # Trigger if either condition is met
        threshold_usdt_met = diff_usdt >= self.settings.rebalance_threshold_usdt
        threshold_pct_met = diff_pct >= self.settings.rebalance_threshold_pct
        needs_rebalance = threshold_usdt_met or threshold_pct_met

        # Clamp transfer amount
        raw_amount = diff_usdt
        safe_amount = max(
            self.settings.rebalance_min_transfer,
            min(raw_amount, self.settings.rebalance_max_transfer),
        )

        if bybit_bal > target_bybit:
            from_exchange = 'bybit'
            to_exchange = 'gateio'
        else:
            from_exchange = 'gateio'
            to_exchange = 'bybit'

        return {
            'needs_rebalance': needs_rebalance,
            'from_exchange': from_exchange,
            'to_exchange': to_exchange,
            'transfer_amount': safe_amount,
            'current_ratio': bybit_bal / total if total > 0 else 0.5,
            'diff_usdt': diff_usdt,
            'diff_pct': diff_pct,
        }

    # ── Main entry point ─────────────────────────────────────────

    async def check_and_rebalance(self, force: bool = False) -> dict:
        """
        Main entry point. Called by periodic loop or /rebalance command.

        force=True skips the imbalance check (for manual /rebalance trigger).
        Returns status dict for Telegram response.
        """
        # Safety: pending tx must finish first
        if self._pending_tx is not None:
            return {'status': 'pending_exists', 'tx_id': self._pending_tx}

        # Safety: check if positions are open
        if self._open_count_getter and self._open_count_getter() > 0 and not force:
            return {'status': 'positions_open', 'open_count': self._open_count_getter()}

        # Safety: check daily limit
        daily_total = await self.db.get_daily_rebalance_total()
        balances = await self.get_balances()
        imbalance = self._calc_imbalance(balances)
        amount = imbalance['transfer_amount']

        if daily_total + amount > self.settings.rebalance_max_daily_usdt:
            return {'status': 'daily_limit', 'daily_total': daily_total, 'max': self.settings.rebalance_max_daily_usdt}

        if not force and not imbalance['needs_rebalance']:
            return {'status': 'balanced', 'balances': balances, 'imbalance': imbalance}

        # If REQUIRE_CONFIRM=true and this isn't a manual force → send alert
        if self.settings.rebalance_require_confirm and not force:
            await self.notifier.notify_rebalance_needed(balances, imbalance)
            return {'status': 'pending_confirm', 'balances': balances, 'imbalance': imbalance}

        # Execute
        if self.mode == 'demo':
            return await self._execute_demo(balances, imbalance)
        else:
            return await self._execute_live(balances, imbalance)

    # ── Demo execution ───────────────────────────────────────────

    async def _execute_demo(self, balances: dict, imbalance: dict) -> dict:
        """Simulate transfer without real money movement."""
        amount = imbalance['transfer_amount']
        from_ex = imbalance['from_exchange']
        to_ex = imbalance['to_exchange']

        # Update virtual ledger
        if from_ex == 'bybit':
            self._demo_bybit_balance -= amount
            self._demo_gateio_balance += amount
        else:
            self._demo_gateio_balance -= amount
            self._demo_bybit_balance += amount

        new_balances = await self.get_balances()

        # Record to DB
        await self.db.insert_rebalance_log({
            'mode': 'demo',
            'from_exchange': from_ex,
            'to_exchange': to_ex,
            'amount': amount,
            'network': self.settings.rebalance_network,
            'status': 'simulated',
            'tx_hash': None,
            'fee_usdt': 0.0,
            'ts': int(time.time() * 1000),
            'balance_bybit_before': balances['bybit'],
            'balance_gateio_before': balances['gateio'],
            'balance_bybit_after': new_balances['bybit'],
            'balance_gateio_after': new_balances['gateio'],
        })

        await self.notifier.notify_rebalance_executed(
            mode='demo',
            from_exchange=from_ex,
            to_exchange=to_ex,
            amount=amount,
            status='simulated',
            balances_before=balances,
            balances_after=new_balances,
        )

        return {'status': 'simulated', 'amount': amount, 'balances_after': new_balances}

    # ── Live execution ───────────────────────────────────────────

    async def _execute_live(self, balances: dict, imbalance: dict) -> dict:
        """Execute real on-chain transfer via exchange withdrawal API."""
        amount = imbalance['transfer_amount']
        from_ex = imbalance['from_exchange']
        to_ex = imbalance['to_exchange']
        network = self.settings.rebalance_network

        # 1. Get deposit address for receiving exchange
        if to_ex == 'bybit':
            deposit_address = self.settings.bybit_deposit_address
        else:
            deposit_address = self.settings.gateio_deposit_address

        if not deposit_address:
            raise ValueError(
                f"{to_ex.upper()}_DEPOSIT_ADDRESS not set in .env — "
                f"cannot proceed with live transfer"
            )

        try:
            # 2. Initiate withdrawal
            if from_ex == 'bybit':
                result = await self.bybit.withdraw_usdt(
                    address=deposit_address, amount=amount, network=network,
                )
            else:
                result = await self.gateio.withdraw_usdt(
                    address=deposit_address, amount=amount, network=network,
                )

            tx_id = result.get('tx_id') or result.get('id', '')
            fee_usdt = result.get('fee', 1.0)

            # 3. Record to DB with status 'pending'
            record_id = await self.db.insert_rebalance_log({
                'mode': 'live',
                'from_exchange': from_ex,
                'to_exchange': to_ex,
                'amount': amount,
                'network': network,
                'status': 'pending',
                'tx_hash': tx_id,
                'fee_usdt': fee_usdt,
                'ts': int(time.time() * 1000),
                'balance_bybit_before': balances['bybit'],
                'balance_gateio_before': balances['gateio'],
                'balance_bybit_after': None,
                'balance_gateio_after': None,
            })

            # 4. Start background monitor
            self._pending_tx = tx_id
            self._pending_tx_info = {'id': record_id, 'amount': amount, 'fee': fee_usdt,
                                      'from': from_ex, 'to': to_ex}
            asyncio.create_task(
                self._monitor_tx(record_id, tx_id, from_ex, to_ex, amount, fee_usdt)
            )

            # 5. Notify initiated
            await self.notifier.notify_rebalance_initiated(
                from_exchange=from_ex, to_exchange=to_ex,
                amount=amount, fee=fee_usdt, network=network, tx_id=tx_id,
            )

            return {'status': 'pending', 'tx_id': tx_id, 'amount': amount}

        except Exception as e:
            logger.error("Live rebalance failed: %s", e)
            await self.notifier.notify_rebalance_failed(from_ex, to_ex, amount, str(e))
            raise

    # ── Tx Monitor loop ──────────────────────────────────────────

    async def _monitor_tx(
        self, record_id: int, tx_id: str,
        from_ex: str, to_ex: str, amount: float, fee: float,
    ):
        """Poll exchange API every 30s until tx confirmed or timeout (30 min)."""
        monitor = TxMonitor(self.bybit, self.gateio)
        timeout_sec = 1800  # 30 min
        poll_sec = 30
        elapsed = 0

        while elapsed < timeout_sec:
            await asyncio.sleep(poll_sec)
            elapsed += poll_sec

            try:
                status = await monitor.check_status(tx_id, from_ex)

                if status == 'confirmed':
                    await self.db.update_rebalance_status(record_id, 'confirmed',
                                                          int(time.time() * 1000))
                    self._pending_tx = None
                    self._pending_tx_info = None

                    new_balances = await self.get_balances()
                    await self.notifier.notify_rebalance_confirmed(
                        from_exchange=from_ex, to_exchange=to_ex,
                        amount=amount, fee=fee, tx_id=tx_id,
                        elapsed_sec=elapsed, balances_after=new_balances,
                    )
                    return

                elif status == 'failed':
                    await self.db.update_rebalance_status(record_id, 'failed')
                    self._pending_tx = None
                    self._pending_tx_info = None
                    await self.notifier.notify_rebalance_failed(
                        from_ex, to_ex, amount, f"tx {tx_id} failed on-chain"
                    )
                    return

            except Exception as e:
                logger.warning("Tx monitor poll error: %s", e)

        # Timeout
        await self.db.update_rebalance_status(record_id, 'timeout')
        self._pending_tx = None
        self._pending_tx_info = None
        await self.notifier.notify_rebalance_timeout(tx_id, elapsed)

    # ── Command helpers ──────────────────────────────────────────

    async def get_status(self) -> dict:
        """Return current rebalance status for /rebalance status command."""
        return {
            'enabled': self.settings.rebalance_enabled,
            'mode': self.mode,
            'auto': self._auto_enabled,
            'pending_tx': self._pending_tx,
            'check_interval_sec': self.settings.rebalance_check_interval_sec,
            'threshold_usdt': self.settings.rebalance_threshold_usdt,
            'threshold_pct': self.settings.rebalance_threshold_pct,
        }

    def set_auto(self, enabled: bool):
        self._auto_enabled = enabled

    def get_pending_tx(self) -> Optional[dict]:
        if self._pending_tx_info:
            return {**self._pending_tx_info, 'tx_id': self._pending_tx}
        return None
