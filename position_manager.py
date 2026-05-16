"""
Position manager - in-memory tracking of open positions and exit state.

Stages:
  Stage 0 - just entered, %1 SL on exchange
  Stage 1 - +%1.2 profit (peak):    SL moves to +%1 profit on entry, no CE yet
  Stage 2 - +2 ATR profit (peak):   SL moves to +0.2 ATR profit, CE 2 ATR trail starts
  Stage 3 - +6 ATR profit (peak):   CE narrows to 1 ATR trail, SL untouched

Stage transitions use extreme_price (peak profit reached), not current price.
This means a stage stays "achieved" even if price pulls back.

CE level is computed from extreme_price and the current trail multiplier.
CE hit check uses current price.
"""
from dataclasses import dataclass
from typing import Dict, Optional


# Stage definitions
STAGE_ENTRY = 0
STAGE_1_PCT = 1     # +%1.2 reached → SL to +%1
STAGE_2_ATR = 2     # +2 ATR reached → SL to +0.2 ATR, CE 2 ATR
STAGE_3_ATR = 3     # +6 ATR reached → CE 1 ATR


@dataclass
class Position:
    symbol: str
    side: str                # "Buy" (long) or "Sell" (short)
    entry_price: float
    qty: float
    stake_usdt: float        # USDT margin used
    leverage: int
    atr_at_entry: float
    open_time: float

    # Stage tracking
    stage: int = STAGE_ENTRY
    ce_level: Optional[float] = None        # Current Chandelier Exit level (price)
    current_sl: float = 0.0                 # Current SL price on the exchange
    extreme_price: float = 0.0              # Peak price for long, trough for short

    # Misc
    last_reverse_check_candle: int = 0      # candle.start of last reverse check

    # ---------- updates ----------
    def update_extreme(self, current_price: float) -> None:
        """Update peak/trough since entry."""
        if self.side == "Buy":
            if current_price > self.extreme_price:
                self.extreme_price = current_price
        else:
            if current_price < self.extreme_price:
                self.extreme_price = current_price

    # ---------- profit calculations ----------
    def profit_pct_at(self, price: float) -> float:
        """Price-based profit percentage at a given price."""
        if self.side == "Buy":
            return (price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - price) / self.entry_price

    def profit_atr_at(self, price: float) -> float:
        """Profit measured in ATR units at a given price."""
        if self.atr_at_entry <= 0:
            return 0.0
        if self.side == "Buy":
            return (price - self.entry_price) / self.atr_at_entry
        else:
            return (self.entry_price - price) / self.atr_at_entry

    # ---------- CE ----------
    def ce_trail_atr(self) -> Optional[float]:
        """
        Current CE trail multiplier based on stage.
        Returns None if CE is not active.
        """
        if self.stage == STAGE_2_ATR:
            return 2.0
        if self.stage == STAGE_3_ATR:
            return 1.0
        return None  # No CE in stages 0 and 1

    def compute_ce(self) -> Optional[float]:
        """
        Compute Chandelier Exit level based on current extreme and stage.
        Returns None if CE is not active.
        """
        trail = self.ce_trail_atr()
        if trail is None:
            return None
        offset = self.atr_at_entry * trail
        if self.side == "Buy":
            return self.extreme_price - offset
        else:
            return self.extreme_price + offset

    def ce_hit(self, current_price: float) -> bool:
        """Has current price touched/crossed the CE level?"""
        if self.ce_level is None:
            return False
        if self.side == "Buy":
            return current_price <= self.ce_level
        else:
            return current_price >= self.ce_level


class PositionManager:
    def __init__(self):
        self._positions: Dict[str, Position] = {}

    def open(self, position: Position) -> None:
        self._positions[position.symbol] = position

    def close(self, symbol: str) -> Optional[Position]:
        return self._positions.pop(symbol, None)

    def get(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def all(self) -> Dict[str, Position]:
        return dict(self._positions)

    def count(self) -> int:
        return len(self._positions)

    def has(self, symbol: str) -> bool:
        return symbol in self._positions
