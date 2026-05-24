# SMARTBOT REDBLUE

Bybit Futures botu. EMA + ATR bantli, kirmizi/mavi flag mantigi, 4 seviye cikis (ENTRY/BE/CE1/CE2), chandelier ve winrate cikisli, Telegram bildirimli.

## DOSYALAR

```
smartbot/
├── main.py                    # Ana orkestrator
├── state.py                   # Merkezi durum (flag, pozisyon, istatistik)
├── bybit_client.py            # Bybit V5 API
├── band.py                    # EMA, ATR, bant hesabi
├── flag.py                    # Flag acma/silme + giris kosullari
├── entry.py                   # Kirmizi & Mavi giris threadi
├── exit.py                    # Kirmizi & Mavi cikis threadi + seviye gecisleri
├── telegram_notifier.py       # Telegram mesaj sablonlari
├── report.py                  # Periyodik raporlar (10dk/1sa/8sa/24sa)
├── config.json                # Tum ayarlar
├── requirements.txt           # Python paketleri
├── render.yaml                # Render deploy ayarlari
└── .gitignore
```

## KURULUM

### 1. Telegram bot ve chat ID
- @BotFather'a git, `/newbot` ile yeni bot olustur. Token al.
- Botla bir mesaj at, sonra `https://api.telegram.org/bot<TOKEN>/getUpdates` adresinden chat_id'yi al.

### 2. Bybit API key
- Bybit Hesap > API > Yeni API olustur.
- Yetkiler: "Contract Trading", "Wallet" (read) ve "Position" (read+trade).
- IP whitelist Render IP'lerine ayarla veya bos birak.

### 3. GitHub'a yukle
```bash
cd smartbot
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin <REPO_URL>
git push -u origin main
```

### 4. Render'a deploy
- Render dashboard > New > Background Worker.
- GitHub repo'yu sec.
- Build: `pip install -r requirements.txt`
- Start: `python main.py`
- Environment variables ekle:
  - `BYBIT_API_KEY`
  - `BYBIT_API_SECRET`
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`

## CONFIG DEGISIKLIGI

`config.json`'da degisiklik yapmak istersen:
1. Dosyayi guncelle
2. GitHub'a push
3. Render otomatik yeniden deploy eder
4. Bot baslarken yeni ayarlarla calismaya devam eder

**ONEMLI:** Stake ve bakiye, bot her baslatildiginda yeniden hesaplanir (bakiye x %10).

## CALISMA MANTIGI

### Threadler
- **Kirmizi Giris**: Her 5 sn'de tum coinleri tarar. Flag acar/siler, kirmizi giris kosullari saglandiginda islem acar.
- **Mavi Giris**: Aynisi mavi icin.
- **Kirmizi Cikis**: Acik kirmizi islemleri tarar. Seviye gecisi, chandelier, ENTRY/BE/CE1/CE2 cikislari.
- **Mavi Cikis**: Aynisi mavi icin. Winrate cikisi her seviyede aktif.
- **Rapor**: 10dk/1sa/8sa/24sa raporlari otomatik gonderir.

### Cikis Tipleri
- `ENTRY EXIT` / `BE EXIT` / `CE1 EXIT` / `CE2 EXIT` — seviye cikislari
- `CHANDELIER EXIT` — CE1/CE2'de 1 ATR geri cekilme
- `WINRATE EXIT` — mavi islemlerde karsi tarafa gecis
- `MANUEL EXIT` — Bybit panelinden kapatma
- `SL EXIT` — stop-loss tetiklenmesi
- `TASFIYE` — likidasyon

## TEST
Bot calistirmadan once `config.json`'da `env.testnet: true` yaparak Bybit testnet'inde dene.

## SORUN GIDERME
- Bot mesaj atmiyor → Telegram token ve chat ID dogru mu kontrol et.
- Islem acmiyor → Bybit API keyin "Trade" yetkisi var mi?
- Hatali fiyat → Bybit'in `linear` (USDT perpetual) kategori desteklemeyen coin olabilir, listeden cikar.
