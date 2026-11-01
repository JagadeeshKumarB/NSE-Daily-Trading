---
name: nse-risk
description: >-
  Risk Manager for intraday trading on NSE (India). Checks every proposed
  order against your limits and stops anything over them, before it can
  reach the broker. For a day trade: a stop is required, the loss at the stop
  PLUS the estimated charges must fit your max loss per trade, reward:risk
  must meet your minimum, the day's total risk must fit your daily loss
  limit, no more than your trades per day, and no new entries outside your
  entry window (09:30 to 14:30 IST) or when NSE is closed. India rules too:
  only EQ-series stocks can be traded intraday (MIS), the flat-by time must
  be before Zerodha's 15:20 auto square-off, no entry within 1 % of a
  stock's upper circuit, prices on NSE's tick grid. It also keeps the
  basics: blocked symbols, order size, position cap, cash buffer, no margin,
  no short selling, no options or futures, malformed Kite-style orders,
  duplicates and a daily order limit. Getting out is never blocked by a day
  limit. Prints PASS or BLOCK with the reason and records the verdicts. Use
  when the user says "check my order", "is this safe", "risk check", "does
  this break my rules", "can I take this trade", "how much have I risked
  today", or before any order is placed, always.
argument-hint: "[path to orders.json]"
---

# nse-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by /nse-budget passes through it. A BLOCK is final for that order:
it does not go to the user for a yes, and it does not go to the broker.

Most people who day trade lose money. SEBI's study of FY2023 found that
about 7 in 10 individual intraday traders in equity cash lost money. These
limits keep each loss and each day's losses small. They do not make a trade
win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-30 09:35"  # pin the clock (India time, IST)
```

On Windows, `python` works too.

## A few words first

- **Stop** (stop-loss): the price where you get out if the trade goes
  wrong. At Zerodha it is a separate SL (stop-loss) order.
- **Target**: the price where you take the profit, a LIMIT sell.
- **R** (risk): entry minus stop, a share. A 1R target is as far above the
  entry as the stop is below it. **Reward:risk** is (target minus entry)
  divided by (entry minus stop).
- **MIS**: Zerodha's intraday product. The position must be closed the same
  day; Zerodha closes it for you from about 15:20, with a ₹50 + GST charge.
  **CNC** is delivery: the shares go to your demat account. Not a day trade.
- **Series**: NSE puts each stock in a series. Only **EQ** stocks can be
  traded intraday. **BE** and **BZ** stocks are "trade-for-trade": every
  trade must be delivered, so no MIS.
- **Circuit limit** (price band): a stock may move only a set percent (2, 5,
  10 or 20 %) from yesterday's close in one day. At its upper circuit there
  may be only buyers and no sellers: the price is "locked", and you may not
  be able to get out. Stocks with futures and options show "No Band": NSE
  moves their band during the day.
- **Charges**: brokerage, STT, exchange fees, SEBI fee, GST and stamp duty.
  On a small intraday trade they are real money, so they count in your max
  loss.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders, shaped like Kite's place_order (from /nse-budget) |
| `trader/config.json` | the limits (/nse-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings in rupees; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry of a MARKET order; `detail.SYMBOL.prev_close` feeds the circuit check |
| `trader/universe.txt` | `SYMBOL\|Name\|Series\|Band` a line (from /nse-scanner): the series and the circuit band |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a BUY, or a SELL that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, series, size, duplicates) turns into a warning for an exit, never a
BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, `transaction_type` not BUY or SELL, `quantity` not a positive whole number, no `exchange` or `product`, an unknown `order_type` / `variety` / `validity`, a LIMIT without a price, an SL without a `trigger_price`, a price off NSE's tick grid (ICICIBANK moves in 0.10 steps); `exits` missing the `stop_loss` or the `target` leg, or a leg on the wrong side |
| asset | an option while `allow_options` is false; futures, NFO / BSE / other exchanges, crypto, Yahoo symbols like `ICICIBANK.NS` |
| short | a SELL of more than you hold (`allow_short` false); never a short for delivery (CNC) |
| blocked | (entries) the symbol is on the `blocked` list |
| series | (entries, MIS) the stock is not in the EQ series. If `trader/universe.txt` is missing or does not list it, the series is unknown: a warning, not a BLOCK. Check it before you trade |
| flat_by | (entries, MIS) the plan's `flat_by` (else the config's) is 15:20 or later |
| circuit | (entries) with a previous close and a numeric band: the entry is within 1 % of today's upper circuit, or above it |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin (MIS leverage) while `allow_margin` is false |
| stop | (entries) no stop while `require_stop` is true (the `exits.stop_loss` trigger, or the plan's stop); a stop on the wrong side of the entry |
| loss | (entries) quantity x (entry minus stop), plus the estimated round-trip charges, over `max_loss_per_trade_inr` |
| reward:risk | (entries) under `min_reward_risk` |
| daily loss | (entries) the max losses of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after`, or NSE is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the price of a LIMIT or SL order. For a
  MARKET order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). An older price is not used,
  and it says so.
- **Charges** use Zerodha's published equity rates (MIS: 0.03 % or ₹20 an
  order, whichever is lower, STT on the sell side, exchange and SEBI fees,
  GST, stamp duty). They are estimates; the contract note has the real
  numbers. Set `"count_charges_in_max_loss": false` to leave them out of the
  limit (it still shows them).
- **Daily loss** counts the full max loss (with charges) of every entry
  placed today, win or lose, as the log recorded it. Three trades like the
  one below use ₹1,396.56 of a ₹1,500 day.
- **The window** is India time (IST, no daylight saving), worked out in the
  tool, never from the computer's time zone. Weekends and NSE holidays are
  closed (2026 is built in; for another year add `"holidays"` to
  `trader/config.json`, and it warns you until you do). Muhurat trading is
  not supported. Set both `no_entries_*` to null to switch the window off.
- **The stop is a separate order.** Zerodha has no bracket order for this.
  After the entry fills, place the SL sell and the target LIMIT sell, and
  cancel the other one the moment one fills. The SL's limit price sits a
  little under its trigger so it fills in a fast market, but in a gap it
  can fill lower, or not at all.

Warnings do not block, but say them to the user: a LIVE account, an unknown
series, a missing previous close for a stock with a band, a stop that lives
only in the plan, a price too old to use, a number sent where a string is
expected, a holiday list that is not built in.

## Steps

1. If the price may have moved since the plan and the order is a MARKET
   order, refresh `trader/prices.json` first.
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least
   one was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with charges, reward:risk, what the target nets after charges,
   and how much of today's limits it uses. For a BLOCK, give the reason and
   the fix (fewer shares, a closer stop from /nse-exit, wait for the window,
   or skip the trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. Each order is
   matched to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: ICICI Bank, Friday 30 October 2026

The opening-range breakout from /nse-budget at 09:35 IST: 53 shares of
ICICIBANK, a LIMIT buy at 1394.50, stop 1387.10, target 1401.90, MIS, on a
fresh ₹1,00,000 paper account with the default config:

```
PASS  #1 BUY ICICIBANK 53 shares MIS, entry
          circuit: no fixed band (an F&O stock: NSE moves its band during the day); not checked
          entry 1394.50 (the limit price), stop 1387.10, target 1401.90: risk 7.40 a share
          max loss ₹392.20 + ₹73.32 charges = ₹465.52 (limit ₹500), reward:risk 1.00 (min 1)
          at the target: +₹392.20 - ₹73.55 charges = +₹318.65
          after the entry fills: place the SL SELL (trigger 1387.10, limit 1385.70) and the LIMIT SELL 1401.90; cancel the other the moment one fills (Zerodha has no bracket order for this)
          today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss

1 passed, 0 blocked at 2026-10-30 09:35 IST. Cash after the passed orders: ₹26,091.50 (paper account).
```

Notice the charges: a 1R win of ₹392.20 nets ₹318.65, and a loss at the
stop costs ₹465.52. Small trades pay a lot of charges.

One more share and it is blocked (53 x 1394.50 = ₹73,908.50 fits the
₹75,000 max order; 54 shares does not):

```
BLOCK #1 BUY ICICIBANK 54 shares MIS, entry
          ...
        - size: ₹75,303 is over your ₹75,000 max order
        - position: ICICIBANK would be ₹75,303, 75.3% of the ₹1,00,000 account, over your 75% cap
```

The same 53 shares as a MARKET order at 09:52, with the 09:50 price of
1395.00 in `trader/prices.json`, are blocked by reward:risk: the risk is now
7.90 a share and the target is only 6.90 away.

```
BLOCK #1 BUY ICICIBANK 53 shares MIS, entry
          ICICIBANK last 1395.00 is above the plan's entry 1394.50: the risk is measured from 1395.00
          ...
          max loss ₹418.70 + ₹73.32 charges = ₹492.02 (limit ₹500), reward:risk 0.87 (min 1)
        - reward:risk: 0.87 (target 1401.90, entry 1395.00, stop 1387.10) is under your minimum 1
```

At 09:29 the LIMIT order is blocked by the window (`no new entries before
09:30 IST (it is 09:29)`). The 15:12 MARKET sell of the 53 shares passes,
even though it is after the 14:30 entry cutoff: it is an exit.

## Example: two India-only BLOCKs

The test fixture `orders-blocks.json` has two made-up orders (illustrative
prices, not market data) on stocks from `trader/universe.txt`. MTARTECH has
a 5 % band and a previous close of 2000.00; ABAN is a BE-series stock.

```
BLOCK #1 BUY MTARTECH 25 shares MIS, entry
          circuit: 5% band from the previous close 2000.00: lower 1900.00, upper 2100.00
          ...
        - circuit: the entry 2085.00 is within 1% of today's upper circuit 2100.00 (5% band from the previous close 2000.00). A stock at its upper circuit can lock: few or no sellers, and a sharp fall when it opens up
BLOCK #2 BUY ABAN 1000 shares MIS, entry
          ...
        - series: ABAN is in the BE series (trade-for-trade): every trade must be delivered, so no MIS intraday. Only EQ stocks can be day traded
```

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices, the universe file or the log.
- Free Yahoo prices are unofficial and can lag; the exchange price is the one that fills, and a stop can
  fill worse than its trigger in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
