"""
Telegram notifications via Bot API.
Uses simple HTTP POST with `requests`, no async dependency.
All send_* functions silently swallow errors (logged to stdout) so that
Telegram outages never crash the trading loop.
"""
import requests
from typing import List

import config


_TELEGRAM_URL = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"


def _send(text: str) -> None:
    """Send a message; never raises."""
    try:
        resp = requests.post(
            _TELEGRAM_URL,
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[TG-ERR] {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[TG-ERR] {e}")


# ============================================================
# MESSAGE FORMATTERS
# ============================================================
def send_bot_start(balance: float, stake: float, leverage: int, symbols: List[str]) -> None:
    msg = (
        f"🚀 <b>BOT BAŞLATILDI</b>\n\n"
        f"💰 Toplam Bakiye: <b>{balance:.2f} USDT</b>\n"
        f"🎯 Stake (%20): <b>{stake:.2f} USDT</b>\n"
        f"⚡ Kaldıraç: <b>{leverage}x ISOLATED</b>\n"
        f"📊 Coin Sayısı: <b>{len(symbols)}</b>\n"
        f"⏱ Giriş Tarama: 5 dk / Çıkış Tarama: 60 sn\n\n"
        f"Strateji: EMA7 + EMA100(H/L) Kesişim + Kanal Genişliği Filtresi"
    )
    _send(msg)


def send_scan_summary(scanned: int, signals: List[str], active_count: int, max_pos: int,
                      crossover_count: int = 0, filtered_count: int = 0) -> None:
    free_slots = max_pos - active_count
    sig_text = ", ".join(signals) if signals else "—"
    msg = (
        f"📡 <b>5 DK TARAMA</b>\n"
        f"Taranan: {scanned} | Crossover: {crossover_count} | Kanal Filtresinde Takılan: {filtered_count}\n"
        f"Sinyal: {sig_text}\n"
        f"Açık İşlem: {active_count}/{max_pos} (Boş slot: {free_slots})"
    )
    _send(msg)


def send_entry(symbol: str, side: str, price: float, qty: float, stake: float,
               leverage: int, sl_price: float, atr_value: float) -> None:
    arrow = "🟢" if side == "Buy" else "🔴"
    direction = "LONG" if side == "Buy" else "SHORT"
    notional = qty * price
    msg = (
        f"{arrow} <b>{direction} AÇILDI</b> — <code>{symbol}</code>\n\n"
        f"💵 Giriş: <b>{price}</b>\n"
        f"📦 Miktar: {qty} ({notional:.2f} USDT hacim)\n"
        f"🎯 Stake: {stake:.2f} USDT × {leverage}x\n"
        f"🛑 SL (%1): {sl_price}\n"
        f"📏 ATR: {atr_value:.6f}"
    )
    _send(msg)


def send_stage1(symbol: str, side: str, price: float, new_sl: float, ce_level: float,
                atr_value: float) -> None:
    """Stage 1: +2 ATR peak → SL to +0.5% profit, CE 4 ATR active."""
    msg = (
        f"⚙️ <b>AŞAMA 1 — CE AKTİF (4 ATR)</b>\n"
        f"<code>{symbol}</code> ({'LONG' if side == 'Buy' else 'SHORT'})\n"
        f"Fiyat: {price} (peak +2 ATR)\n"
        f"Yeni SL: <b>{new_sl}</b> (+%0.5 kârda)\n"
        f"CE: <b>{ce_level}</b> (4 ATR geriden)\n"
        f"ATR: {atr_value:.6f}"
    )
    _send(msg)


def send_stage2(symbol: str, side: str, price: float, new_sl: float, ce_level: float,
                atr_value: float) -> None:
    """Stage 2: +6 ATR peak → SL to +0.2 ATR profit, CE narrows to 3 ATR."""
    msg = (
        f"🎯 <b>AŞAMA 2 — CE DARALDI (3 ATR)</b>\n"
        f"<code>{symbol}</code> ({'LONG' if side == 'Buy' else 'SHORT'})\n"
        f"Fiyat: {price} (peak +6 ATR)\n"
        f"Yeni SL: <b>{new_sl}</b> (+0.2 ATR kârda)\n"
        f"CE: <b>{ce_level}</b> (3 ATR geriden)\n"
        f"ATR: {atr_value:.6f}"
    )
    _send(msg)


def send_exit(symbol: str, side: str, entry_price: float, exit_price: float,
              pnl_usdt: float, pnl_pct: float, reason: str) -> None:
    icon = "✅" if pnl_usdt >= 0 else "❌"
    direction = "LONG" if side == "Buy" else "SHORT"
    msg = (
        f"{icon} <b>İŞLEM KAPANDI</b> — <code>{symbol}</code> ({direction})\n\n"
        f"Giriş: {entry_price} → Çıkış: {exit_price}\n"
        f"PnL: <b>{pnl_usdt:+.2f} USDT</b> ({pnl_pct:+.2f}%)\n"
        f"Sebep: <i>{reason}</i>"
    )
    _send(msg)


def send_insufficient_balance(symbol: str, side: str) -> None:
    """Signal generated but couldn't open due to insufficient balance."""
    direction = "LONG" if side == "Buy" else "SHORT"
    msg = (
        f"⚠️ <b>Sinyal: {symbol} {direction}</b>\n"
        f"Yetersiz bakiye, işlem açılamadı."
    )
    _send(msg)


def send_leverage_rejected(symbol: str, side: str, leverage: int) -> None:
    """Signal generated but coin doesn't allow requested leverage."""
    direction = "LONG" if side == "Buy" else "SHORT"
    msg = (
        f"⚠️ <b>Sinyal: {symbol} {direction}</b>\n"
        f"Bu coinde {leverage}x kaldıraca izin verilmiyor, işlem atlandı."
    )
    _send(msg)


def send_error(context: str, error: str) -> None:
    msg = f"🚨 <b>HATA</b>\n{context}\n<code>{error[:300]}</code>"
    _send(msg)


def send_daily_summary(total_pnl: float, trade_count: int, win_count: int) -> None:
    win_rate = (win_count / trade_count * 100) if trade_count else 0
    icon = "📈" if total_pnl >= 0 else "📉"
    msg = (
        f"{icon} <b>GÜNLÜK ÖZET</b>\n\n"
        f"Toplam PnL: <b>{total_pnl:+.2f} USDT</b>\n"
        f"İşlem Sayısı: {trade_count}\n"
        f"Kazanma Oranı: %{win_rate:.1f} ({win_count}/{trade_count})"
    )
    _send(msg)


def send_info(text: str) -> None:
    """Generic info message."""
    _send(f"ℹ️ {text}")
