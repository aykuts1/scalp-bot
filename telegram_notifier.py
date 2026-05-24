"""
SMARTBOT REDBLUE — telegram_notifier.py
Telegram bildirim sablonlari ve gonderme.
"""

import os
import requests
from datetime import datetime
from typing import Optional


class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else None

    def send(self, message: str) -> bool:
        """Telegram mesaj gonder. Hata olursa False doner."""
        if not self.base_url or not self.chat_id:
            print(f"[TELEGRAM DEVRE DISI] {message}")
            return False
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            print(f"[TELEGRAM HATA] {e}")
            return False


def fmt_time(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def fmt_duration(seconds: float) -> str:
    """Saniye -> '1sa 24dk' formati."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    if h > 0:
        return f"{h}sa {m}dk"
    return f"{m}dk"


def fmt_color_side(color: str, side: str) -> str:
    """color: 'kirmizi'/'mavi', side: 'long'/'short' -> emoji + isim."""
    emoji = "🔴" if color == "kirmizi" else "🔵"
    side_name = "Long" if side == "long" else "Short"
    color_name = "Kirmizi" if color == "kirmizi" else "Mavi"
    return f"{emoji} {color_name} {side_name}"


def fmt_pnl(pnl_usdt: float, volume: float, price_diff: float, atr: float) -> str:
    """
    K/Z'yi USDT / % / ATR formatinda.
    - % bazli: hacim uzerinden (pnl / volume * 100)
    - ATR bazli: fiyat farkinin ATR cinsinden (price_diff / atr)
    """
    pct = (pnl_usdt / volume * 100) if volume > 0 else 0
    atr_val = (price_diff / atr) if atr > 0 else 0
    sign = "+" if pnl_usdt >= 0 else ""
    return f"{sign}{pnl_usdt:.2f} USDT / %{pct:.2f} / {atr_val:.2f} ATR"


def calc_pnl_components(pos: dict, current_price: float, commission: float = 0.0):
    """
    Pozisyon ve guncel fiyattan brut/net K/Z hesapla.
    Donus: (gross_pnl, net_pnl, price_diff, volume, atr)
    """
    entry = pos["entry_price"]
    side = pos["side"]
    qty = pos["qty"]
    if side == "long":
        price_diff = current_price - entry
    else:
        price_diff = entry - current_price
    gross_pnl = price_diff * qty
    net_pnl = gross_pnl - commission
    return gross_pnl, net_pnl, price_diff, pos["volume"], pos["atr_at_entry"]


# ──────────────────────────────────────────
# BILDIRIM SABLONLARI
# ──────────────────────────────────────────

def msg_bot_started(config: dict, balance: float, stake: float) -> str:
    band = config["band"]
    flag = config["flag"]
    order = config["order"]
    slot = config["slot"]
    commission = config["commission"]
    coins = config["coins"]
    report = config["report"]
    coin_list = ", ".join(coins["list"])

    return f"""🟢 BOT BAŞLATILDI
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time()}

💰 HESAP
├ Toplam Bakiye: {balance:.2f} USDT
├ Stake: {stake:.2f} USDT (%{config["account"]["stake_percent"]})
└ Kaldıraç: {order["leverage"]}x

📊 BANT AYARLARI
├ Zaman Dilimi: {band["timeframe"]}
├ EMA Periyodu: {band["ema_period"]}
├ ATR Periyodu: {band["atr_period"]}
├ Kırmızı Çarpan: {band["red_outer_multiplier"]}
├ Mavi Çarpan: {band["blue_outer_multiplier"]}
├ Tampon Çarpanı: {band["outer_buffer_multiplier"]}
├ CE1 Çarpanı: {band["level1_multiplier"]}
├ CE2 Çarpanı: {band["level2_multiplier"]}
└ Chandelier Çarpanı: {band["chandelier_multiplier"]}

🚦 FLAG AYARLARI
├ Fiyat Hafızası: {flag["price_memory_seconds"]} sn
└ Lookback: {flag["crossover_lookback_count"]}

🎰 POZİSYON
├ Max Slot: {slot["max_open_positions"]}
├ Emir Tipi: {order["type"]}
└ SL: %{order["sl_percent"]}

💸 KOMİSYON: %{commission["rate"] * 100}

📋 COİN LİSTESİ ({coins["count"]} coin)
{coin_list}

⏱ RAPORLAMA
├ Kısa Rapor: {report["short_interval_minutes"]} dk
├ Orta Rapor: {report["medium_interval_hours"]} saat
├ Vardiya: {report["shift_interval_hours"]} saat
└ Gün Sonu: {report["daily_interval_hours"]} saat
━━━━━━━━━━━━━━━━━━━━"""


def msg_trade_opened(pos: dict, slot_used: int, slot_max: int) -> str:
    title = f"{fmt_color_side(pos['color'], pos['side'])} AÇILDI".upper()
    sl_distance = abs(pos["entry_price"] - pos["sl_price"])
    sl_pct = sl_distance / pos["entry_price"] * 100
    sl_atr = sl_distance / pos["atr_at_entry"] if pos["atr_at_entry"] > 0 else 0
    return f"""{title}
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time(pos['entry_time'])}
📌 Coin: {pos['coin']}
📊 Zaman Dilimi: {pos.get('timeframe', '15m')}

💰 POZİSYON
├ Giriş Fiyatı: {pos['entry_price']:.6f} USDT
├ Stake: {pos['stake']:.2f} USDT
├ İşlem Hacmi: {pos['volume']:.2f} USDT ({pos['leverage']}x)
└ Miktar: {pos['qty']}

🛡 STOP-LOSS
├ SL Fiyatı: {pos['sl_price']:.6f} USDT
└ SL Mesafesi: {sl_distance:.6f} USDT / %{sl_pct:.2f} / {sl_atr:.2f} ATR

🎰 SLOT: {slot_used}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


def msg_trade_closed(pos: dict, exit_data: dict, slot_used: int, slot_max: int) -> str:
    title = f"{fmt_color_side(pos['color'], pos['side'])} KAPANDI".upper()
    duration = (exit_data["exit_time"] - pos["entry_time"]).total_seconds()
    volume = pos["volume"]
    atr = pos["atr_at_entry"]
    # Fiyat farki (long: exit-entry, short: entry-exit)
    if pos["side"] == "long":
        price_diff = exit_data["exit_price"] - pos["entry_price"]
    else:
        price_diff = pos["entry_price"] - exit_data["exit_price"]
    return f"""{title}
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time(exit_data['exit_time'])}
📌 Coin: {pos['coin']}
📊 Zaman Dilimi: {pos.get('timeframe', '15m')}

📍 ÇIKIŞ TİPİ: {exit_data['exit_type']}

💰 POZİSYON
├ Giriş Fiyatı: {pos['entry_price']:.6f} USDT
├ Çıkış Fiyatı: {exit_data['exit_price']:.6f} USDT
├ İşlem Hacmi: {pos['volume']:.2f} USDT ({pos['leverage']}x)
└ Süre: {fmt_duration(duration)}

📈 SONUÇ
├ Brüt K/Z: {fmt_pnl(exit_data['gross_pnl'], volume, price_diff, atr)}
├ Komisyon: -{exit_data['commission']:.2f} USDT
└ Net K/Z: {fmt_pnl(exit_data['net_pnl'], volume, price_diff, atr)}

📊 ULAŞILAN SEVİYE: {pos.get('level', 'ENTRY')}

🎰 SLOT: {slot_used}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


def msg_level_transition(pos: dict, old_level: str, new_level: str, current_price: float, level_price: float, gross_pnl: float, net_pnl: float, slot_used: int, slot_max: int) -> str:
    title = f"{fmt_color_side(pos['color'], pos['side'])} — SEVİYE GEÇİŞİ".upper()
    volume = pos["volume"]
    atr = pos["atr_at_entry"]
    if pos["side"] == "long":
        price_diff = current_price - pos["entry_price"]
    else:
        price_diff = pos["entry_price"] - current_price
    return f"""{title}
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time()}
📌 Coin: {pos['coin']}
📊 Zaman Dilimi: {pos.get('timeframe', '15m')}

📍 {old_level} → {new_level}

💰 POZİSYON
├ Giriş Fiyatı: {pos['entry_price']:.6f} USDT
├ Güncel Fiyat: {current_price:.6f} USDT
└ Seviye Fiyatı: {level_price:.6f} USDT

📈 ANLIK DURUM
├ Brüt K/Z: {fmt_pnl(gross_pnl, volume, price_diff, atr)}
└ Net K/Z: {fmt_pnl(net_pnl, volume, price_diff, atr)}

🎰 SLOT: {slot_used}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


def msg_error(thread_name: str, coin: str, error_type: str, detail: str, attempt: int = 1, max_attempts: int = 3) -> str:
    return f"""⚠️ HATA
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time()}
📌 Coin: {coin}
🔧 Thread: {thread_name}

❌ Hata Tipi: {error_type}
📝 Detay: {detail}

🔄 Deneme: {attempt}/{max_attempts}
━━━━━━━━━━━━━━━━━━━━"""


def msg_insufficient_balance(coin: str, color: str, side: str, balance: float, needed: float, slot_used: int, slot_max: int) -> str:
    return f"""💸 YETERSİZ BAKİYE
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time()}
📌 Coin: {coin}
📊 Yön: {fmt_color_side(color, side)}

💰 DURUM
├ Mevcut Bakiye: {balance:.2f} USDT
├ Gerekli Stake: {needed:.2f} USDT
└ Fark: {needed - balance:.2f} USDT

❌ İşlem açılamadı
🎰 SLOT: {slot_used}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


def msg_slot_full(coin: str, color: str, side: str, slot_max: int) -> str:
    return f"""🔒 SLOT DOLU
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time()}
📌 Coin: {coin}
📊 Yön: {fmt_color_side(color, side)}

❌ İşlem açılamadı
🎰 SLOT: {slot_max}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


def msg_coin_busy(coin: str, color: str, side: str, existing_pos: dict, slot_used: int, slot_max: int) -> str:
    return f"""🚫 COİNDE AÇIK İŞLEM VAR
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time()}
📌 Coin: {coin}
📊 Yön: {fmt_color_side(color, side)}

⛔ Mevcut Açık İşlem
├ Tip: {fmt_color_side(existing_pos['color'], existing_pos['side'])}
├ Giriş Fiyatı: {existing_pos['entry_price']:.6f} USDT
└ Giriş Zamanı: {fmt_time(existing_pos['entry_time'])}

❌ İşlem açılamadı
🎰 SLOT: {slot_used}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


def msg_external_close(pos: dict, exit_data: dict, reason: str, slot_used: int, slot_max: int) -> str:
    duration = (exit_data["exit_time"] - pos["entry_time"]).total_seconds()
    volume = pos["volume"]
    atr = pos["atr_at_entry"]
    if pos["side"] == "long":
        price_diff = exit_data["exit_price"] - pos["entry_price"]
    else:
        price_diff = pos["entry_price"] - exit_data["exit_price"]
    return f"""⚡ İŞLEM DIŞ KAYNAKTAN KAPATILDI
━━━━━━━━━━━━━━━━━━━━
🕐 Zaman: {fmt_time(exit_data['exit_time'])}
📌 Coin: {pos['coin']}
📊 Yön: {fmt_color_side(pos['color'], pos['side'])}
📊 Zaman Dilimi: {pos.get('timeframe', '15m')}

📍 KAPANIŞ NEDENİ: {reason}

💰 POZİSYON
├ Giriş Fiyatı: {pos['entry_price']:.6f} USDT
├ Çıkış Fiyatı: {exit_data['exit_price']:.6f} USDT
├ İşlem Hacmi: {pos['volume']:.2f} USDT ({pos['leverage']}x)
└ Süre: {fmt_duration(duration)}

📈 SONUÇ
├ Brüt K/Z: {fmt_pnl(exit_data['gross_pnl'], volume, price_diff, atr)}
├ Komisyon: -{exit_data['commission']:.2f} USDT
└ Net K/Z: {fmt_pnl(exit_data['net_pnl'], volume, price_diff, atr)}

📊 ULAŞILAN SEVİYE: {pos.get('level', 'ENTRY')}

🎰 SLOT: {slot_used}/{slot_max}
━━━━━━━━━━━━━━━━━━━━"""


# Singleton
notifier = TelegramNotifier()
