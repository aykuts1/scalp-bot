"""
Data Manager
Tüm Bybit API çağrılarını yapan merkezi sınıf.
- 15dk mum kapanışında mum verisi çeker
- Her 5sn anlık fiyat çeker
- Göstergeleri hesaplar (Donchian, EMA)
- Thread'ler buradan okur, API'ye dokunmaz.
"""
import threading
import time
import logging

from pybit.unified_trading import HTTP

from indicators import donchian_history, ema

log = logging.getLogger("DataManager")


class DataManager:
    def __init__(self, config, telegram_notifier=None):
        self.cfg = config
        self.tg = telegram_notifier

        # Bybit client
        self.client = HTTP(
            api_key=config.bybit_api_key,
            api_secret=config.bybit_api_secret,
            testnet=False,
        )

        # Data store
        self.lock = threading.Lock()
        # symbol -> dict { candles, donchian_upper_history, donchian_lower_history, ema, last_price, prev_price }
        self.data = {}
        for s in config.symbols:
            self.data[s] = {
                "candles": [],
                "donchian_upper_history": [],
                "donchian_lower_history": [],
                "ema": None,
                "last_price": None,
                "prev_price": None,
                "last_candle_close_ts": None,
            }

        self._stop = threading.Event()
        self._paused_coins = set()

        # Bakiye
        self.balance = 0.0
        self.balance_lock = threading.Lock()

        # ----------------------------------------------------------
        # POZİSYON ÖNBELLEĞİ
        # Scheduler her 1sn'de bir Bybit'ten tüm açık pozisyonları çeker
        # ve buraya yazar. Mavi/Sarı thread'ler buradan okur.
        # ----------------------------------------------------------
        self.positions_lock = threading.Lock()
        # set of (symbol, position_idx) tuple — Bybit'te açık olan pozisyonlar
        self._open_positions_set = set()
        # symbol -> {"long": avg_price|None, "short": avg_price|None}
        self._open_positions_detail = {}
        self._positions_last_sync_ts = 0.0
        self._positions_sync_ok = False

        # Margin mode'u tüm coinlere ayarla (leverage'tan ÖNCE)
        self._setup_margin_mode()
        # Leverage'ı tüm coinlere ayarla
        self._setup_leverage()

        # İlk mum verilerini çek
        self.fetch_all_candles_initial()
        # İlk bakiye
        self.update_balance()

    # ------------------------------------------------------------------
    # SETUP
    # ------------------------------------------------------------------
    def _setup_margin_mode(self):
        """Tüm coinlere margin mode'u ayarla (CROSS veya ISOLATED)."""
        # Bybit tradeMode: 0 = cross, 1 = isolated
        trade_mode = 0 if self.cfg.margin_mode == "CROSS" else 1
        for symbol in self.cfg.symbols:
            try:
                self.client.switch_margin_mode(
                    category="linear",
                    symbol=symbol,
                    tradeMode=trade_mode,
                    buyLeverage=str(self.cfg.leverage),
                    sellLeverage=str(self.cfg.leverage),
                )
            except Exception as e:
                err = str(e).lower()
                # 100028 = UTA hesabında bu endpoint yasak (margin mode hesap seviyesinde,
                # default cross). 110026 / "not modified" = zaten istenen modda.
                if "100028" in err or "unified account is forbidden" in err:
                    log.info("UTA hesabı tespit edildi — margin mode setup atlanıyor "
                             "(UTA varsayılan: cross).")
                    return
                if "not modified" not in err and "110026" not in err:
                    log.warning(f"Margin mode ayarlanamadı {symbol}: {e}")

    def _setup_leverage(self):
        """Tüm coinlere kaldıracı ayarla."""
        for symbol in self.cfg.symbols:
            try:
                self.client.set_leverage(
                    category="linear",
                    symbol=symbol,
                    buyLeverage=str(self.cfg.leverage),
                    sellLeverage=str(self.cfg.leverage),
                )
            except Exception as e:
                # Genelde "leverage not modified" hatası verir, sorun değil
                err = str(e).lower()
                if "not modified" not in err and "110043" not in err:
                    log.warning(f"Leverage ayarlanamadı {symbol}: {e}")
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # BALANCE
    # ------------------------------------------------------------------
    def update_balance(self):
        """USDT bakiyesini günceller (wallet balance)."""
        try:
            r = self.client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            coins = r.get("result", {}).get("list", [{}])[0].get("coin", [])
            for c in coins:
                if c.get("coin") == "USDT":
                    bal = float(c.get("walletBalance", 0.0))
                    with self.balance_lock:
                        self.balance = bal
                    return bal
        except Exception as e:
            log.error(f"Bakiye okunamadı: {e}")
            if self.tg:
                self.tg.notify_error("Bakiye okunamadı", "-", "DataManager", str(e))
        return self.balance

    def get_balance(self):
        with self.balance_lock:
            return self.balance

    # ------------------------------------------------------------------
    # CANDLE FETCH
    # ------------------------------------------------------------------
    def fetch_candles(self, symbol):
        """Tek bir coin için mum verisi çek."""
        try:
            r = self.client.get_kline(
                category="linear",
                symbol=symbol,
                interval=self.cfg.timeframe,
                limit=min(self.cfg.candle_count, 1000),
            )
            klines = r.get("result", {}).get("list", [])
            # Bybit en yeni en başta dönüyor, ters çevir
            klines = list(reversed(klines))

            candles = []
            for k in klines:
                candles.append({
                    "ts": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                })

            # Göstergeleri hesapla
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            closes = [c["close"] for c in candles]

            upper_hist, lower_hist = donchian_history(highs, lows, self.cfg.donchian_period)
            ema_val = ema(closes, self.cfg.ema_period)

            with self.lock:
                self.data[symbol]["candles"] = candles
                self.data[symbol]["donchian_upper_history"] = upper_hist
                self.data[symbol]["donchian_lower_history"] = lower_hist
                self.data[symbol]["ema"] = ema_val
                if candles:
                    self.data[symbol]["last_candle_close_ts"] = candles[-1]["ts"]

            return True
        except Exception as e:
            log.error(f"Mum çekme hatası {symbol}: {e}")
            if self.tg:
                self.tg.notify_error("Mum verisi çekilemedi", symbol, "DataManager", str(e))
            return False

    def fetch_all_candles_initial(self):
        """Bot başlangıcında tüm coinlerin mumlarını çek."""
        for symbol in self.cfg.symbols:
            if symbol in self._paused_coins:
                continue
            self.fetch_candles(symbol)
            time.sleep(self.cfg.candle_fetch_delay_sec)

    def fetch_all_candles(self):
        """15dk kapanışta tüm coinler için mum verisini güncelle."""
        for symbol in self.cfg.symbols:
            if symbol in self._paused_coins:
                continue
            if self._stop.is_set():
                break
            self.fetch_candles(symbol)
            time.sleep(self.cfg.candle_fetch_delay_sec)

    # ------------------------------------------------------------------
    # PRICE TICKER
    # ------------------------------------------------------------------
    def fetch_price(self, symbol):
        """Tek coin için anlık fiyat (last price)."""
        try:
            r = self.client.get_tickers(category="linear", symbol=symbol)
            lst = r.get("result", {}).get("list", [])
            if not lst:
                return None
            p = float(lst[0].get("lastPrice", 0.0))
            return p
        except Exception as e:
            log.error(f"Fiyat çekme hatası {symbol}: {e}")
            if self.tg:
                self.tg.notify_error("Fiyat çekilemedi", symbol, "DataManager", str(e))
            return None

    def fetch_all_prices(self):
        """Tüm coinler için anlık fiyat (tek API çağrısı)."""
        try:
            r = self.client.get_tickers(category="linear")
            lst = r.get("result", {}).get("list", [])
            price_map = {item["symbol"]: float(item["lastPrice"]) for item in lst
                         if "symbol" in item and "lastPrice" in item}

            with self.lock:
                for symbol in self.cfg.symbols:
                    if symbol in self._paused_coins:
                        continue
                    p = price_map.get(symbol)
                    if p is None:
                        continue
                    self.data[symbol]["prev_price"] = self.data[symbol]["last_price"]
                    self.data[symbol]["last_price"] = p
            return True
        except Exception as e:
            log.error(f"Fiyat ticker hatası: {e}")
            if self.tg:
                self.tg.notify_error("Toplu fiyat çekilemedi", "-", "DataManager", str(e))
            return False

    # ------------------------------------------------------------------
    # READ HELPERS (thread-safe)
    # ------------------------------------------------------------------
    def get_snapshot(self, symbol):
        """Bir coinin tüm verisini snapshot olarak döner."""
        with self.lock:
            d = self.data.get(symbol)
            if not d:
                return None
            return {
                "candles": list(d["candles"]),
                "donchian_upper_history": list(d["donchian_upper_history"]),
                "donchian_lower_history": list(d["donchian_lower_history"]),
                "ema": d["ema"],
                "last_price": d["last_price"],
                "prev_price": d["prev_price"],
                "last_candle_close_ts": d["last_candle_close_ts"],
            }

    def get_last_price(self, symbol):
        with self.lock:
            d = self.data.get(symbol)
            return d["last_price"] if d else None

    def get_prev_price(self, symbol):
        with self.lock:
            d = self.data.get(symbol)
            return d["prev_price"] if d else None

    def get_price_pair(self, symbol):
        """
        Atomik olarak (prev_price, last_price) ikilisini döndürür.
        Cross hesaplarında bu fonksiyon kullanılmalı — get_prev + get_last
        ayrı çağrıları arasındaki race condition'ı engeller.
        """
        with self.lock:
            d = self.data.get(symbol)
            if not d:
                return (None, None)
            return (d["prev_price"], d["last_price"])

    def get_donchian_current(self, symbol):
        """En son kapanmış mumdaki Donchian üst/alt."""
        with self.lock:
            d = self.data.get(symbol)
            if not d or not d["donchian_upper_history"]:
                return None, None
            up = d["donchian_upper_history"][-1]
            lo = d["donchian_lower_history"][-1]
            return up, lo

    def get_donchian_previous(self, symbol):
        """Bir önceki kapanmış mumdaki Donchian üst/alt."""
        with self.lock:
            d = self.data.get(symbol)
            if not d or len(d["donchian_upper_history"]) < 2:
                return None, None
            up = d["donchian_upper_history"][-2]
            lo = d["donchian_lower_history"][-2]
            return up, lo

    def get_ema(self, symbol):
        with self.lock:
            d = self.data.get(symbol)
            return d["ema"] if d else None

    # ------------------------------------------------------------------
    # PAUSE / RESUME
    # ------------------------------------------------------------------
    def pause_coin(self, symbol):
        self._paused_coins.add(symbol)

    def resume_coin(self, symbol):
        self._paused_coins.discard(symbol)

    def is_paused(self, symbol):
        return symbol in self._paused_coins

    def get_paused_coins(self):
        return list(self._paused_coins)

    # ------------------------------------------------------------------
    # ORDER / POSITION HELPERS (trade manager kullanır)
    # ------------------------------------------------------------------
    def get_open_positions(self, symbol=None):
        """Borsadaki açık pozisyonları döner."""
        try:
            if symbol:
                r = self.client.get_positions(category="linear", symbol=symbol)
            else:
                r = self.client.get_positions(category="linear", settleCoin="USDT")
            return r.get("result", {}).get("list", [])
        except Exception as e:
            log.error(f"Pozisyon okuma hatası: {e}")
            return []

    # ------------------------------------------------------------------
    # POZİSYON ÖNBELLEĞİ — her 1sn'de scheduler tarafından güncellenir
    # Mavi/Sarı thread'ler buradan okur (Bybit'e direkt sormaz).
    # ------------------------------------------------------------------
    def sync_open_positions(self):
        """
        Tek API çağrısıyla TÜM açık pozisyonları çeker ve önbelleğe yazar.
        Hatada eski önbellek korunur (yanlış pozitif kapatma yapmasın diye).
        """
        try:
            r = self.client.get_positions(category="linear", settleCoin="USDT")
            lst = r.get("result", {}).get("list", [])
        except Exception as e:
            log.error(f"Pozisyon senkron hatası: {e}")
            with self.positions_lock:
                self._positions_sync_ok = False
            return False

        new_set = set()
        new_detail = {}
        for p in lst:
            try:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                symbol = p.get("symbol")
                pidx = int(p.get("positionIdx", 0))
                avg = float(p.get("avgPrice", 0)) or None
                new_set.add((symbol, pidx))
                detail = new_detail.setdefault(symbol, {"long": None, "short": None})
                if pidx == 1:
                    detail["long"] = avg
                elif pidx == 2:
                    detail["short"] = avg
            except (ValueError, TypeError):
                continue

        with self.positions_lock:
            self._open_positions_set = new_set
            self._open_positions_detail = new_detail
            self._positions_last_sync_ts = time.time()
            self._positions_sync_ok = True
        return True

    def is_position_open(self, symbol, position_idx):
        """
        Önbellekten oku. Senkronizasyon hiç yapılmamış veya son senkron
        başarısız olduysa True döndürür (yanlış pozitif kapatma riskini engelle).
        """
        with self.positions_lock:
            if not self._positions_sync_ok:
                return True
            return (symbol, position_idx) in self._open_positions_set

    def positions_synced(self):
        """En az bir başarılı senkronizasyon yapıldı mı?"""
        with self.positions_lock:
            return self._positions_sync_ok

    def get_position_avg_from_cache(self, symbol, position_idx):
        """Önbellekten ortalama açılış fiyatı (yoksa None)."""
        with self.positions_lock:
            d = self._open_positions_detail.get(symbol)
            if not d:
                return None
            if position_idx == 1:
                return d.get("long")
            if position_idx == 2:
                return d.get("short")
            return None

    def get_instrument_info(self, symbol):
        """Coin için minQty, qtyStep, tickSize bilgisi."""
        try:
            r = self.client.get_instruments_info(category="linear", symbol=symbol)
            lst = r.get("result", {}).get("list", [])
            if not lst:
                return None
            inst = lst[0]
            lot = inst.get("lotSizeFilter", {})
            price = inst.get("priceFilter", {})
            return {
                "minOrderQty": float(lot.get("minOrderQty", 0)),
                "qtyStep": float(lot.get("qtyStep", 0.001)),
                "tickSize": float(price.get("tickSize", 0.01)),
            }
        except Exception as e:
            log.error(f"Instrument info hatası {symbol}: {e}")
            return None

    def place_market_order(self, symbol, side, qty, position_idx, stop_loss=None):
        """
        Market order aç. Hedge mode için position_idx önemli:
        - 1: Buy/Long
        - 2: Sell/Short
        side: "Buy" veya "Sell"
        """
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(qty),
                "positionIdx": position_idx,
            }
            if stop_loss is not None:
                params["stopLoss"] = str(stop_loss)
            r = self.client.place_order(**params)
            return r
        except Exception as e:
            log.error(f"Order açma hatası {symbol} {side}: {e}")
            raise

    def close_position_market(self, symbol, side_to_close, qty, position_idx):
        """
        Açık pozisyonu market order ile kapat.
        side_to_close: kapatmak için gerekli ters yön ("Buy" Short kapatır, "Sell" Long kapatır)
        """
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "side": side_to_close,
                "orderType": "Market",
                "qty": str(qty),
                "positionIdx": position_idx,
                "reduceOnly": True,
            }
            r = self.client.place_order(**params)
            return r
        except Exception as e:
            log.error(f"Close order hatası {symbol}: {e}")
            raise

    def get_position_avg_price(self, symbol, position_idx):
        """
        Borsadaki açık pozisyonun ortalama fiyatını döner.
        Açılış/kapanış doğrulamasında ve gerçek dolum fiyatı için kullanılır.
        """
        try:
            positions = self.get_open_positions(symbol)
            for p in positions:
                pidx = int(p.get("positionIdx", 0))
                size = float(p.get("size", 0))
                if pidx == position_idx and size > 0:
                    avg = float(p.get("avgPrice", 0))
                    return avg if avg > 0 else None
            return None
        except Exception as e:
            log.error(f"Pozisyon fiyat okuma hatası {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # STOP
    # ------------------------------------------------------------------
    def stop(self):
        self._stop.set()

    def is_stopping(self):
        return self._stop.is_set()
