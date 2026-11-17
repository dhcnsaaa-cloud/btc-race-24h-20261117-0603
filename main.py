"""Dip-buying mean-reversion bot for cbBTC (CBBTC/USDC), 1-minute bars.

Strategy, in one line: buy the dip below a slow average, sell the recovery
above it, cut the loss if the dip keeps falling.

Rules, all in integer arithmetic so two runs on the same bars are identical:
- Keep the last 240 closes and their sum (a 4-hour simple moving average).
- FLAT and close is 80 bps or more below the average -> buy with all cash.
- LONG and close is 50 bps or more above the average -> sell everything.
- LONG and close is 2% or more below the position's average cost -> sell
  everything (stop-loss; the average cost comes from the account the harness
  sends, fees included).
- Never trade during warm-up batches, never trade before 240 closes exist,
  never order when the batch carries no CBBTC bar.
- One order per reply at most, max_slippage_bps 200 (2%): a fill that would
  land more than 2% away from the close we decided on is refused instead.
- No clock, no network, no files, no randomness: the orders are a pure
  function of the bars the harness sent.

Why this shape: on 1-minute bars the price is noise (about 6 bps per bar)
around slow multi-hour swings of roughly 1%. Chasing the noise with fast
crossovers pays the ~30 bps round-trip cost on every whipsaw and loses.
Waiting for a real dip below the 4-hour average and selling the recovery
above it catches the swing and pays the cost only a few times a day.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
WINDOW = 240          # bars in the slow average (4 hours of 1-minute bars)
DIP_BPS = 80           # buy when close is this far below the average
RISE_BPS = 50          # sell when close is this far above the average
STOP_BPS = 200         # sell when close is this far below our average cost
COOLDOWN_BARS = 30     # bars to stay out after a sale before buying again
MAX_SLIP_BPS = 200     # refuse a fill more than 2% from the decided close
BPS = 10_000


class DipBot:
    """Rolling 240-bar average plus the position's average cost."""

    def __init__(self):
        self.closes = deque(maxlen=WINDOW)
        self.total = 0
        self.cooldown_until = -1

    def note_bar(self, bar):
        close = int(bar["c"])
        if len(self.closes) == WINDOW:
            self.total -= self.closes[0]
        self.closes.append(close)
        self.total += close

    def ready(self):
        return len(self.closes) == WINDOW

    def below_average(self, close):
        # close < average * (1 - DIP_BPS/BPS), cross-multiplied, no floats.
        return close * WINDOW * BPS < self.total * (BPS - DIP_BPS)

    def above_average(self, close):
        # close > average * (1 + RISE_BPS/BPS), cross-multiplied, no floats.
        return close * WINDOW * BPS > self.total * (BPS + RISE_BPS)

    def entry_price(self, state):
        """Average cost per unit, at the symbol's price scale, fees included."""
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return None
        return holding.cost * 10 ** 7 // holding.qty

    def decide(self, state):
        if state.warming_up or not self.ready():
            return []
        close = state.close(SYMBOL)
        if close is None:
            return []
        holding = state.positions.get(SYMBOL)
        in_position = holding is not None and holding.qty > 0
        if not in_position:
            # After a sale, stay out for a while: a dip that is still there
            # right after we sold (in particular after a stop-loss) is a
            # falling market, not a fresh buying opportunity.
            if state.seq < self.cooldown_until:
                return []
            if self.below_average(close) and state.cash >= 1_000_000:
                return [{
                    "side": "buy",
                    "symbol": SYMBOL,
                    "notional_quote_units": state.cash,
                    "max_slippage_bps": MAX_SLIP_BPS,
                }]
            return []
        sell = None
        if self.above_average(close):
            sell = "recovery"
        else:
            cost = self.entry_price(state)
            if cost is not None and close * BPS <= cost * (BPS - STOP_BPS):
                sell = "stop"
        if sell is not None:
            self.cooldown_until = state.seq + COOLDOWN_BARS
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": holding.qty,
                "max_slippage_bps": MAX_SLIP_BPS,
            }]
        return []


def main():
    bot = DipBot()

    def on_batch(state, batch):
        for bar in batch.get("bars", []):
            if bar.get("symbol") == SYMBOL:
                bot.note_bar(bar)
        return bot.decide(state)

    makefun_bars.run(on_batch)


if __name__ == "__main__":
    main()
