# Tunnel Bot

Bybit USDT-Perp uzerinde EMA + ATR bant sistemi ile calisan otomatik trade botu.

## Dosya Yapisi

```
main.py           - Ana giris, iki thread (entry & exit), raporlama
config.py         - config.json okuma ve dogrulama
config.json       - Tum ayarlar (acilamali)
bybit.py          - Bybit Unified Trading API wrapper
order.py          - Post-only limit emir sistemi (50 deneme)
bands.py          - EMA, ATR, 7 cizgi hesaplama
price_history.py  - 300 saniyelik fiyat gecmisi
strategy.py       - Giris kosullari (3 kontrol)
position.py       - Pozisyon state, CE seviyeleri, cikis kontrolu
state.py          - Thread-safe pozisyon yoneticisi
telegram.py       - Tum bildirimler
reports.py        - Z raporlari formatlama
```

## Environment Variables (Railway)

```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Calistirma 

```bash
pip install -r requirements.txt
python main.py
```

## Strateji Ozeti

**Bantlar:**
- Asiri Ust = Dis Ust + 0.5 ATR
- Dis Ust   = EMA + 3 ATR
- Ic Ust    = EMA + 2 ATR
- EMA (orta cizgi)
- Ic Alt   = EMA - 2 ATR
- Dis Alt  = EMA - 3 ATR
- Asiri Alt = Dis Alt - 0.5 ATR

**Giris (Long, 3 kosul):**
1. Fiyat dis ust bandin ustunde
2. Asiri ust cizgiyi gecmemis
3. Son 300 sn'de fiyat hic dis ust ustunde olmamis

Short simetrik.

**BE Cizgisi:** Dis Bant × (1 ± maker_oran) -- komisyon karsilamasi..

**Cikis Seviyeleri:**
- Seviye 1: Giris (CE pasif)
- Seviye 2: CE Stage 1 (kar 0.5 ATR → 0.5 ATR takip)
- Seviye 3: CE Stage 2 (kar 2 ATR → 1 ATR takip)
- Seviye 4: Winrate (kar 5 ATR → 0.5 ATR takip)

**Cikis Tipleri:** Lose / CE1 / CE2 / Winrate / Stoploss Exit

**Emir Sistemi:** Her zaman post-only limit, max 50 deneme, market emir yok.
