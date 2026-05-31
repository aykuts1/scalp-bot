# 🤖 Bybit 6-Thread Trading Bot

Bybit Futures üzerinde **12 coin**, **50x kaldıraç**, **hedge mode** ile çalışan otomatik strateji botu.

İki bağımsız grup, toplam **6 paralel thread**:

### 🔴 KIRMIZI Grubu
- 🔴 **KIRMIZI** — Ana strateji, Donchian50 yön değişimine göre işlem açar
- 🔵 **MAVİ** — Hedge, Kırmızı'nın tersi yönünde koruma açar
- 🟡 **SARI** — Trend pekiştirici, Kırmızı'nın aynı yönünde kârı maksimize eder

### ⚪️ BEYAZ Grubu (bağımsız)
- ⚪️ **BEYAZ** — Anlık Donchian değme bazlı ikinci ana strateji
- 🟣 **MOR** — Beyaz'ın hedge thread'i (Mavi mantığı ile aynı)
- 🟠 **TURUNCU** — Beyaz'ın trend pekiştirici thread'i (Sarı mantığı ile aynı)

İki grup tamamen bağımsız çalışır. Aynı coinde aynı anda 1 Kırmızı + 1 Beyaz açık olabilir.

---

## 📋 İçindekiler
1. [Kurulum](#-kurulum)
2. [Genel Akış](#-genel-akış)
3. [🔴 Kırmızı Thread](#-kırmızı-thread)
4. [🔵 Mavi Thread](#-mavi-thread)
5. [🟡 Sarı Thread](#-sarı-thread)
6. [⚪️ Beyaz Thread](#%EF%B8%8F-beyaz-thread)
7. [🟣 Mor Thread](#-mor-thread)
8. [🟠 Turuncu Thread](#-turuncu-thread)
9. [Slot Kuralları](#-slot-kuralları)
10. [Hard SL Çakışma Kuralı](#-hard-sl-çakışma-kuralı)
11. [Telegram Komutları](#-telegram-komutları)
12. [Raporlar](#-raporlar)

---

## 🚀 Kurulum

### Gerekli environment variables
```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

### Kurulum komutları
```bash
pip install -r requirements.txt
python main.py
```

### Railway için
`Procfile` ve `runtime.txt` mevcut, doğrudan deploy edilebilir.

### Coin Listesi (12)
BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, TRXUSDT, HYPEUSDT, DOGEUSDT, AVAXUSDT, NEARUSDT, ADAUSDT, ATOMUSDT

---

## 🔁 Genel Akış

1. **Bot başlar** → bakiye, mum verileri, EMA, Donchian çekilir
2. **Başlangıçta otomatik Kırmızı flag taraması** yapılır (15dk boundary beklemez)
   - Beyaz için başlangıç taraması YOKTUR (anlık fiyat hareketine bağlı çalışır)
3. **Scheduler döngüsü:**
   - Her **1 sn** → tüm coinlerin anlık fiyatı çekilir
   - Her **1 sn** → pozisyon önbelleği yenilenir
   - Her **15 dk** kapanışta → mum çekilir, EMA + Donchian güncellenir, Kırmızı flag taraması yapılır
   - Her **12 saat** → stake güncellenir
4. **6 thread paralel çalışır:**
   - 🔴 Kırmızı: 15dk kapanışta yön değişimi tespiti, anlık değme + cross ile açılış
   - 🔵 Mavi: Kırmızı tablosu kurulduktan sonra konum bazlı flag + ST1 cross
   - 🟡 Sarı: Kırmızı tablosu kurulduktan sonra konum bazlı flag + chandelier
   - ⚪️ Beyaz: Donchian'a anlık değme + cross + EMA (15dk beklemez)
   - 🟣 Mor: Beyaz tablosu kurulduktan sonra konum bazlı flag + ST1 cross
   - 🟠 Turuncu: Beyaz tablosu kurulduktan sonra konum bazlı flag + chandelier
5. **Çıkışlar tek aktiftir:**
   - LOSE/WINRATE/Chandelier seviyeleri kontrol edilir, ETKİLİ exit ne ise tetiklenir
6. **Bağımlı kapanış:**
   - Kırmızı kapandığında bağlı Mavi+Sarı otomatik kapanır
   - Beyaz kapandığında bağlı Mor+Turuncu otomatik kapanır

---

## 🔴 Kırmızı Thread

**Ana strateji thread'i. 15 dk grafiğine bakar.**

### Flag mantığı
- 15dk mum kapanışı bekler
- Donchian50 üst/alt çizgide yön değişimi tespit edilirse → flag açılır
- Fiyat ilgili Donchian çizgisine değdiğinde → giriş çizgisi kaydedilir
- Aynı anda hem değme hem cross olamaz (cross için sonraki tarama beklenir)

### İşlem açılışı
- Giriş çizgisini cross + EMA800 koşulu sağlanırsa açılır:
  - SHORT: aşağı cross + fiyat < EMA800
  - LONG: yukarı cross + fiyat > EMA800

### Tablo (açılış anında sabitlenir)
- LOSE = Donchian (max %2 sınırlı — entry'den max %2 uzakta)
- WINRATE = entry ± (entry − LOSE) × 3 (3:1 risk/reward)
- ENTRY ile WINRATE arası 6 eşit parça: ENTRY, ST1, ST2, ST3, ST4, ST5

### Seviye geçişi (tek yönlü)
- Yeni seviyenin giriş çizgisi cross edilirse seviye ilerler
- Seviye geri gitmez

### Çıkış haritası (mevcut seviyeye göre)
| Mevcut Seviye | Çıkış Çizgisi |
|---|---|
| ENTRY | LOSE |
| ST1 | LOSE |
| ST2 | ENTRY |
| ST3 | ST1 |
| ST4 | ST2 |
| ST5 | ST3 |

WINRATE cross olursa kâr ile kapanış (her seviyede aktif).

---

## 🔵 Mavi Thread

**Hedge thread'i. Kırmızı'ya bağlı. Kırmızı tersi yön.**
- Kırmızı SHORT → Mavi LONG
- Kırmızı LONG → Mavi SHORT

### Tablo
- Kırmızı giriş çizgisi ↔ Kırmızı LOSE arası **5 eşit parça**
- Bölgeler: **FLAG**, ST1, ST2, ST3, ST4
- 6 çizgi: Kırmızı giriş, ST1, ST2, ST3, ST4, Kırmızı LOSE

### Flag mantığı (KONUM BAZLI)
- Her taramada fiyatın bölgesi tespit edilir
- Fiyat **FLAG bölgesindeyse** flag açıktır
- Fiyat FLAG bölgesinden çıkarsa flag kapanır (rapor için DELETED/CONVERTED kaydı)

### İşlem açılışı
- Fiyat **ST1 giriş çizgisini cross** ederse → işlem açılır, seviye = ST1
- Tablo kurulurken fiyat zaten ST1+ bölgesindeyse → otomatik açılır

### Seviye geçişi (iki yönlü)
- Fiyat hangi bölgedeyse seviye o olur
- Sadece telemetri amaçlı, **çıkışı etkilemez**
- Her geçişte Telegram bildirimi atılır

### Çıkış (3 yol)
1. **Mavi kendi çıkışı**: Fiyat Kırmızı giriş çizgisini ters yöne cross ederse Mavi kapanır
2. **Mavi WINRATE'i**: Fiyat Kırmızı LOSE'u Mavi'nin kâr yönüne cross ederse → Kırmızı kapanır (zincir Mavi'yi de kapatır)
3. **Zincir**: Kırmızı herhangi bir sebepten kapandığında Mavi de otomatik kapanır

### Yeniden giriş
- Kırmızı yaşadığı sürece sınırsız reentry
- Mavi kapanınca tablo silinmez, flag state sıfırlanır

---

## 🟡 Sarı Thread

**Trend pekiştirici thread'i. Kırmızı'ya bağlı. Kırmızı ile aynı yön.**

### Tablo
- Kırmızı giriş çizgisi ↔ Kırmızı WINRATE arası **6 eşit parça**
- Bölgeler: **FLAG**, ST1, ST2, ST3, ST4, ST5

### Chandelier mesafesi
- `|Kırmızı LOSE − Kırmızı giriş| / 2`
- Açılış sonrası en iyi fiyatı takip eder, ters cross olursa kapanır

### Flag mantığı (KONUM BAZLI)
- Aynı Mavi gibi, fiyat FLAG bölgesindeyse flag açık

### İşlem açılışı
- Fiyat **ST1 cross** → işlem açılır, chandelier devreye girer
- Tablo kurulurken fiyat zaten ST1+ bölgesindeyse → otomatik açılır

### Çıkışlar
1. **Chandelier ters cross** → Sarı kapanır, reentry_line set edilir
2. **Kırmızı WINRATE** cross → Kırmızı kapanır, zincir Sarı'yı da kapatır
3. **Zincir** → Kırmızı herhangi bir sebepten kapanırsa Sarı da kapanır

### Yeniden giriş
- Chandelier exit sonrası reentry_line aktif
- Fiyat reentry_line'ı kâr yönüne tekrar cross ederse Sarı yeniden açılır
- Kırmızı yaşadığı sürece sınırsız

---

## ⚪️ Beyaz Thread

**Anlık Donchian değme bazlı ikinci ana strateji. Kırmızı'dan bağımsız.**

### Flag mantığı (KIRMIZIDAN FARKLI)
- **15dk mum kapanışı BEKLEMEZ** — anlık fiyat hareketine bakar
- Fiyat Donchian üst/alt çizgiye **değdiği anda**:
  - Flag açılır
  - Giriş çizgisi kaydedilir: Donchian'ın **1/4 içinde**
- Mesafe = `(Donchian üst − Donchian alt) / 4`
  - SHORT giriş çizgisi = Donchian üst − mesafe
  - LONG giriş çizgisi = Donchian alt + mesafe
- Fiyat üste her tekrar değdiğinde flag **silinmez**, sadece giriş çizgisi **güncellenir**

### İşlem açılışı
- Kaydedilen giriş çizgisini cross + EMA800 koşulu sağlanırsa açılır:
  - SHORT: aşağı cross + fiyat < EMA800
  - LONG: yukarı cross + fiyat > EMA800
- Açılınca flag VE giriş çizgisi silinir

### Tablo (açılış anındaki Donchian değerleriyle sabitlenir)
- LOSE = doğrudan Donchian (KIRMIZIDAN FARK: **max %2 sınırı YOK**)
- WINRATE = doğrudan ters Donchian
- ENTRY ↔ WINRATE arası 6 eşit parça: ENTRY, ST1, ST2, ST3, ST4, ST5

### Seviye geçişi (tek yönlü, Kırmızı ile aynı)
### Çıkış haritası (Kırmızı ile aynı)
| Mevcut Seviye | Çıkış Çizgisi |
|---|---|
| ENTRY | LOSE |
| ST1 | LOSE |
| ST2 | ENTRY |
| ST3 | ST1 |
| ST4 | ST2 |
| ST5 | ST3 |

Kapanırken bağlı Mor + Turuncu otomatik kapanır.

---

## 🟣 Mor Thread

**Beyaz'ın hedge thread'i. Yeni Mavi mantığı ile birebir aynı.**

- Beyaz SHORT → Mor LONG
- Beyaz LONG → Mor SHORT

### Tablo
- Beyaz giriş ↔ Beyaz LOSE arası **5 eşit parça**
- Bölgeler: **FLAG**, ST1, ST2, ST3, ST4

### Mantık
- Konum bazlı flag
- ST1 cross → açılış (tablo kurulurken otomatik açılış mümkün)
- İki yönlü seviye telemetrisi
- 3 yollu kapanış:
  1. Beyaz giriş ters cross → Mor kapanır
  2. Beyaz LOSE Mor kâr yönü cross → Beyaz kapanır (zincir)
  3. Beyaz herhangi bir sebepten kapandı → Mor da kapanır
- Beyaz yaşadığı sürece sınırsız reentry

---

## 🟠 Turuncu Thread

**Beyaz'ın trend pekiştirici thread'i. Sarı mantığı ile birebir aynı.**

- Beyaz ile **aynı yön**

### Tablo
- Beyaz giriş ↔ Beyaz WINRATE arası **6 eşit parça**
- Bölgeler: **FLAG**, ST1, ST2, ST3, ST4, ST5

### Chandelier mesafesi
- `|Beyaz LOSE − Beyaz giriş| / 2`

### Mantık
- Konum bazlı flag
- ST1 cross → açılış, chandelier devreye girer
- Chandelier ters cross → Turuncu kapanır, reentry_line set
- Beyaz WINRATE cross → Beyaz kapanır (zincir)
- Reentry sınırsız, Beyaz yaşadığı sürece
- Beyaz kapanırsa Turuncu da kapanır

---

## 🎰 Slot Kuralları

### Coin başına slot
- Her coin için max **1 Kırmızı** (yön farketmez)
- Her coin için max **1 Beyaz** (yön farketmez)
- Aynı coinde **1 Kırmızı + 1 Beyaz** aynı anda açık olabilir (bağımsız gruplar)

### Bağlı thread'ler
- Her Kırmızı için max 1 Mavi + 1 Sarı (mantık gereği aynı anda olmaz)
- Her Beyaz için max 1 Mor + 1 Turuncu (mantık gereği aynı anda olmaz)

### Maksimum eş zamanlı işlem
- 12 Kırmızı + 12 Mavi/Sarı + 12 Beyaz + 12 Mor/Turuncu = **48 işlem**

### Stake hesabı
- Stake = bakiyenin %2'si
- 48 işlem × %2 × 50x kaldıraç = kontrol edilebilir risk

---

## 🛡 Hard SL Çakışma Kuralı

Bybit hedge mode'da **her kutu (symbol+side) için tek hard SL** tutulur.
Aynı coin+yön için iki ayrı grup (örn. Kırmızı SHORT + Beyaz SHORT) işlem açtığında çakışma olur.

### Çözüm: "Daha geniş" SL kullanılır
- **SHORT**: en **yüksek** SL kazanır (entry'den en uzak yukarıda)
- **LONG**: en **düşük** SL kazanır (entry'den en uzak aşağıda)

### Neden?
- Hard SL emniyet kemeridir, "çok geç olunca" devreye girer
- Her trade kendi LOSE çizgisinde **yazılım kapanışı** yapar (Bybit'in SL'inden bağımsız)
- Bu yüzden Bybit'te tek tutulan SL "her iki trade'i de yakalayacak" geniş olan olmalı

### Örnek
1. Kırmızı SHORT açılır, hard_sl = 102$
2. Beyaz SHORT aynı coinde açılır, hard_sl = 104$
3. 104 > 102 → Bybit'e 104 gönderilir, eski 102 override edilir
4. Her trade kendi LOSE'una geldiğinde yazılım tarafında kapanır

---

## 💬 Telegram Komutları

| Komut | Açıklama |
|---|---|
| `/start` | Trading başlat |
| `/stop` | Trading durdur |
| `/status` | Anlık durum (bakiye, açık işlemler, flag'ler) |
| `/report` | Hourly raporu zorla |
| `/pause SEMBOL` | Coin için yeni işlem açılmasını duraklat |
| `/resume SEMBOL` | Duraklatılan coini devam ettir |
| `/help` | Komut listesi |

---

## 📊 Raporlar

### Anlık bildirimler
- 🔴🔵🟡⚪️🟣🟠 İşlem açıldı / kapandı
- 📍 Seviye değişimi
- 🚨 Hata / yetersiz bakiye / slot dolu
- 💱 Stake güncellendi

**Flag açılışı / kapanışı Telegram'a atılmaz** — sadece raporlarda gösterilir.

### Saatlik rapor (hourly)
- Anlık durum (bakiye, stake, 6 thread açık sayıları, açık işlemler, açık flag'ler)
- Son 1 saatte kapanan işlemler ve net PnL

### 12 saatlik Z raporu
- Performans (winrate, profit factor, net PnL)
- 6 thread kırılımı
- Çıkış tipi dağılımı (WINRATE, LOSE, CHANDELIER, BAĞIMLI, GİRİŞ EXIT)
- Yön dağılımı (LONG/SHORT)
- En kârlı / en zararlı coinler

### 24 saatlik X raporu
- Genel performans
- En uzun kazanma/kaybetme serisi
- Saat bazlı dağılım
- 6 thread detayı
- Coin başına detay
- Flag istatistikleri (açılan, dönüşen, silinen — 6 thread)
- Chandelier (Sarı + Turuncu) özel istatistik
- Uyarılar (hata sayısı, yetersiz bakiye, slot dolu)

---

## ⚙️ Parametreler (config.json)

| Parametre | Değer | Açıklama |
|---|---|---|
| `leverage` | 50 | Kaldıraç |
| `stake_pct` | 2.0 | Her işlem bakiyenin %2'si |
| `hard_sl_pct` | 2.0 | Borsa SL %2 (çakışmada en geniş kullanılır) |
| `max_lose_pct` | 2.0 | Kırmızı LOSE max %2 (Beyaz için SINIR YOK) |
| `risk_reward` | 3 | Kırmızı WINRATE çarpanı |
| `donchian_period` | 50 | Donchian periyodu |
| `ema_period` | 800 | EMA periyodu |
| `timeframe` | 15 | 15 dakikalık mumlar |
| `candle_count` | 900 | Bot başlangıcında çekilen mum sayısı |
| `price_update_interval_sec` | 1 | Fiyat çekme aralığı |
| `thread_scan_interval_sec` | 1 | Thread tarama aralığı |
| `position_sync_interval_sec` | 1 | Pozisyon senkron aralığı |
| `stake_update_interval_hours` | 12 | Stake güncelleme periyodu |
