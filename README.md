# 🤖 Arbitrage Bot — Bybit × Gate.io

Sistem arbitrase perpetual futures otomatis antara Bybit dan Gate.io.

## Fitur

- WebSocket-first real-time price feed dengan connection pool
- Event-driven spread detection (bukan polling)
- Pre-flight guard & spread decay detector
- Paper mode & Live mode
- Telegram bot interface
- Slippage tracking & analysis
- Orderbook depth check (VWAP simulation)

## Instalasi

### 1. Clone & Setup

```bash
cd arb_bybit_gateio
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Konfigurasi

```bash
cp .env.example .env
```

Edit `.env`:

- Isi Telegram Bot Token (dari @BotFather)
- Isi Bybit API Key & Secret
- Isi Gate.io API Key & Secret

### 3. Mendapatkan API Key

**Bybit:**

1. Login ke [Bybit](https://www.bybit.com)
2. My Account → API Management
3. Create API Key dengan permission: Contract Trade
4. Copy API Key & Secret ke `.env`

**Gate.io:**

1. Login ke [Gate.io](https://www.gate.io)
2. API Management → Create API Key
3. Permission: Futures Trade
4. Copy API Key & Secret ke `.env`

### 4. Jalankan

**Paper mode:**

```bash
python main.py
```

Kontrol via Telegram: `/auto on`

**Live mode:**

Edit `.env`: `TRADING_MODE=live`

```bash
python main.py
```

## Telegram Commands

| Command | Fungsi |
|---------|--------|
| `/start` | Perkenalan |
| `/help` | Daftar command |
| `/auto on` / `off` | Start/stop automation |
| `/status` | Status engine & WS |
| `/mode paper` / `live` | Ganti mode |
| `/portfolio` | Saldo & posisi |
| `/history` | Ringkasan PnL |
| `/history detail` | 10 trade terakhir |
| `/top` | Top 5 spread pair |
| `/cancel` | Batalkan pending order |

## Penjelasan Config `.env`

### Strategy Parameters

- `SPREAD_ENTRY_THRESHOLD`: Minimum spread % untuk entry (default 0.5%)
- `SPREAD_EXIT_THRESHOLD`: Spread % untuk exit/konvergen (default 0.05%)
- `MAX_POSITION_USDT`: Ukuran posisi per leg (default 50 USDT)
- `LEVERAGE`: Leverage untuk kedua exchange (default 5x)
- `MAX_OPEN_POSITIONS`: Maksimum posisi simultan (default 5)

### Fee & Slippage

- `TAKER_FEE_BYBIT`: 0.06% per side
- `TAKER_FEE_GATEIO`: 0.05% per side
- `SLIPPAGE_BUFFER`: Safety margin 0.10%

### Internal Threshold

Bot menggunakan threshold internal yang sudah termasuk fee:

```
internal_threshold = SPREAD_ENTRY_THRESHOLD + (TAKER_FEE_BYBIT + TAKER_FEE_GATEIO) × 2 + SLIPPAGE_BUFFER
                   = 0.5% + 0.22% + 0.10% = 0.82%
```

Artinya: bot hanya entry jika gross spread ≥ 0.82%, sehingga net profit ≥ 0.5%

## Contoh Telegram Notification

```
🟢 Trade OPEN

📊 Pair: BTCUSDT
📈 Long: Bybit @ 67234.50
📉 Short: Gate.io @ 66890.20
📐 Spread: 0.512%
💵 Size: 50 USDT
```

```
🔴 Trade CLOSE

📊 Pair: BTCUSDT
💰 Gross PnL: 0.0234 USDT
💸 Fee: 0.0156 USDT
🟢 Net PnL: 0.0078 USDT
⏱ Duration: 45s
```

## Arsitektur

```
[WS Pool] → [Price Cache] → [Spread Engine] → [Executor] → [Position Tracker]
    ↑                          ↑                                    ↓
[Bybit]                     [Strategy]                           [Database]
[Gate.io]                                                   [Telegram Notifier]
```

## Troubleshooting

- **API Key Error**: Cek `.env`, pastikan key & secret benar
- **WS Disconnect**: Bot auto-reconnect. Cek `/status`
- **Database locked**: Pastikan WAL mode aktif (otomatis)
- **Rate limit**: Bot punya rate limiter built-in
