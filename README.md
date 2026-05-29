# Bybit Multi-Thread Trading Bot

## Kurulum (Railway)

1. Bu repo'yu GitHub'a yükle.
2. Railway'de yeni proje oluştur → GitHub'dan deploy et.
3. Environment Variables ekle:
   - `BYBIT_API_KEY`
   - `BYBIT_API_SECRET`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Deploy et.

## Telegram Komutları

- `/status` — anlık durum
- `/report` — 1 saatlik rapor
- `/start` — botu başlat
- `/stop` — botu durdur
- `/pause SYMBOL` — coin durdur
- `/resume SYMBOL` — coin devam ettir
- `/help` — komut listesi

## Yapı

```
main.py            — ana giriş, scheduler
config.json        — ayarlar
config_loader.py   — config + env vars
data_manager.py    — tüm API çağrıları, mum/fiyat/göstergeler
trade_manager.py   — slot yönetimi, işlem aç/kapa
red_thread.py      — 🔴 Kırmızı (ana strateji)
blue_thread.py     — 🔵 Mavi (hedge)
yellow_thread.py   — 🟡 Sarı (kar maksimize)
telegram_thread.py — bildirimler, raporlar, komutlar
indicators.py      — Donchian, EMA
utils.py           — cross kontrolü, ortak fonksiyonlar
```

## Önemli Notlar

- **Hedge mode** açık olmalı (Bybit ayarlarından).
- **Testnet kullanılmaz**, gerçek API anahtarları gerekir.
- Bot yeniden başlatıldığında borsadaki açık pozisyonlar **slot olarak sayılır**, yönetilmez.
- Tüm işlemlerde borsaya **%2 hard SL** konulur.
- Stake = bakiye × %2, her 12 saatte bir güncellenir.
- Kaldıraç: 50x.
