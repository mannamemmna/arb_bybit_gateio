# Patch Notes — arb_bybit_gateio

7 file diubah. Semua sudah lolos `py_compile` (syntax check), TAPI **belum dites end-to-end**
terhadap API Bybit/Gate.io asli — sebelum live, jalankan dulu di paper mode minimal
beberapa hari untuk memastikan tidak ada regresi.

## File yang diubah

1. `exchanges/bybit.py`
2. `exchanges/gateio.py`
3. `core/executor.py`
4. `core/spread_engine.py`
5. `paper/paper_engine.py`
6. `telegram_bot/notifier.py`
7. `main.py`

## Ringkasan per bug

### BUG 1 (CRITICAL) — `exchanges/bybit.py`, `exchanges/gateio.py`, `core/executor.py`
- `place_market_order()` di kedua exchange sekarang mengembalikan
  dict ternormalisasi: `{success, exchange, symbol, side, price, quantity, order_id, error}`.
- Bybit: `success` ditentukan dari `retCode == 0`. Avg fill price diambil dari
  `cumExecValue / cumExecQty` response awal, fallback 1 call ke `/v5/order/realtime`
  (best-effort, tidak menggagalkan order kalau gagal fetch).
- Gate.io: `success` ditentukan dari HTTP 200/201 DAN adanya field `id` di response.
  Fill price diambil dari field `fill_price` response.
- `core/executor.py` — error log jelas setiap kali `success=False` (incl. `error` message).

⚠️ **Belum divalidasi 100% terhadap struktur response asli API** — field seperti
`avgPrice`, `cumExecQty` (Bybit) dan `fill_price` (Gate.io) diasumsikan dari dokumentasi
umum. **Sebelum live, cek 1-2 order riil di testnet/kecil dan pastikan field-field ini
benar-benar terisi**, kalau tidak sesuaikan nama field-nya.

### BUG 2 (CRITICAL) — `core/spread_engine.py`, `main.py`
- `SpreadEngine` sekarang menerima `bybit_client`, `gateio_client`, `max_position_usdt`.
- Method baru `_refresh_orderbook(symbol)` fetch L2 orderbook dari kedua exchange lalu
  simpan ke `OrderbookCache`.
- Step 5 di `_validate_signal()` yang tadinya `pass` sekarang benar-benar memanggil
  `_refresh_orderbook()` lalu `ob_cache.check_liquidity()` — signal akan DITOLAK kalau
  VWAP simulasi menunjukkan spread aktual di bawah `internal_threshold`.
- `main.py` diupdate untuk mengoper `bybit_client`, `gateio_client`, `max_position_usdt`
  saat instansiasi `SpreadEngine`.

⚠️ **Ini akan mengubah jumlah signal yang lolos** — expect PnL/jumlah trade paper mode
berubah dibanding hasil minggu lalu, karena sekarang ada REST call tambahan (fetch
orderbook, 2x per exchange) tiap kali ada kandidat signal sebelum entry. Pantau juga
rate limiter usage (`/status` command) supaya tidak kena warning.

### BUG 3 (MAJOR) — `core/executor.py`, `execute_entry()`
- Entry price yang disimpan ke `trade_data` sekarang diambil dari **harga fill aktual**
  (`bybit_result`/`gateio_result` — sudah termasuk slippage di paper mode, avg fill
  price di live mode), bukan dari price-cache mentah sebelum eksekusi.
- Kalau fill price live tidak tersedia (fallback avgPrice fetch gagal), baru fallback
  ke estimasi pre-trade dengan warning log.
- `actual_spread_pct` dan `slippage_pct` yang tersimpan sekarang dihitung dari harga
  fill final ini juga, supaya konsisten dengan angka PnL yang muncul di `/history`.

### BUG 4 (MAJOR) — `core/executor.py` (`execute_exit`) dan `paper/paper_engine.py` (`execute_exit`)
- Qty per leg sekarang direction-aware:
  - `long_bybit` → qty dihitung dari `entry_price_bybit`
  - `long_gateio` → qty dihitung dari `entry_price_gateio`
- Bug yang sama juga ditemukan di `paper/paper_engine.py` (`entry_qty` selalu pakai
  `entry_price_bybit`) — ikut diperbaiki karena ini yang dipakai di paper mode kamu
  selama testing minggu lalu.

### BUG 5 (MINOR) — `telegram_bot/notifier.py`, `main.py`
- `notify_engine_stop()` sekarang menerima parameter `duration_sec` dan menampilkannya
  via `fmt_duration()`, bukan hardcoded `—`.
- `main.py` `stop()` menghitung `duration_sec = time.time() - self.start_time` sebelum
  memanggil notifier.

## Cara terapkan

1. Backup dulu project asli kamu (`git commit` atau copy folder).
2. Jalankan ulang di **paper mode** dulu (`TRADING_MODE=paper`) minimal 2-3 hari.
3. Cek `/status` di Telegram — pastikan `Signals` vs `Rejected` count terlihat wajar
   (rejected harusnya naik karena orderbook check sekarang aktif).
4. Cek log untuk baris `"Live entry: Bybit fill price unavailable"` / `"Live entry: Gate.io fill price unavailable"` — kalau sering muncul di live mode nanti, berarti field
   avgPrice/fill_price dari API perlu disesuaikan.
5. Baru pertimbangkan live mode dengan `MAX_POSITION_USDT` kecil dulu.
