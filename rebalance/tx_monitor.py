"""
TxMonitor — checks withdrawal/transfer status via exchange API.

Does NOT use blockchain explorer directly — uses exchange's own withdrawal
history API which is more reliable for CEX-to-CEX transfers.
"""
from utils.logger import get_logger

logger = get_logger('tx_monitor')


class TxMonitor:
    """
    Polls exchange withdrawal history to determine if a transfer has completed.

    Bybit: GET /v5/asset/withdraw/query-record  (status: 0=pending, 4=success, 10=failed)
    Gate.io: GET /api/v4/withdrawals/{id}        (status: pend=pending, done=confirmed, fail=failed)
    """

    def __init__(self, bybit_client, gateio_client):
        self.bybit = bybit_client
        self.gateio = gateio_client

    async def check_status(self, tx_id: str, from_exchange: str) -> str:
        """
        Returns: 'pending' | 'confirmed' | 'failed'

        Parameters
        ----------
        tx_id : str
            Withdrawal ID (not blockchain tx hash) returned by the exchange's withdrawal API.
        from_exchange : str
            Which exchange initiated the withdrawal ('bybit' or 'gateio').
        """
        if from_exchange == 'bybit':
            return await self._check_bybit(tx_id)
        else:
            return await self._check_gateio(tx_id)

    async def _check_bybit(self, withdraw_id: str) -> str:
        try:
            result = await self.bybit._rest_get('/v5/asset/withdraw/query-record', {
                'withdrawID': withdraw_id,
            }, private=True)
            rows = result.get('rows', [])
            if not rows:
                return 'pending'
            status_code = rows[0].get('status', 0)
            if status_code == 4:
                return 'confirmed'
            if status_code == 10:
                return 'failed'
            return 'pending'
        except Exception as e:
            logger.warning("TxMonitor Bybit check failed: %s", e)
            return 'pending'

    async def _check_gateio(self, withdraw_id: str) -> str:
        try:
            result = await self.gateio._rest_get(
                f'/api/v4/withdrawals/{withdraw_id}', private=True,
            )
            status = result.get('status', 'pend')
            if status == 'done':
                return 'confirmed'
            if status == 'fail':
                return 'failed'
            return 'pending'
        except Exception as e:
            logger.warning("TxMonitor Gate.io check failed: %s", e)
            return 'pending'
