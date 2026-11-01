---
name: nse-budget
description: >-
  Budget Manager for NSE intraday trades. Sizes every position to your budget
  and your max loss per trade, in rupees and whole shares. Reads the trade
  plan from nse-exit (entry, stop, target), finds the number of shares that
  fits inside four limits (max loss per trade with Zerodha's charges counted,
  position cap, max order, cash after your buffer), says which limit decided
  it, and writes ONE proposed trade to trader/orders.json in the shape of
  Kite Connect's place_order: a LIMIT entry plus an SL stop-loss and a LIMIT
  target, because Zerodha has no bracket order for this. A stop too far away
  for your max loss is a plain refusal, not a smaller order. Nothing is
  placed. Use when the user says "how many shares", "size this trade", "how
  much should I buy", "I have ₹1,00,000", "I only want to risk ₹500 a trade",
  "set my rules", "keep 10% in cash", or after /nse-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# nse-budget

The Budget Manager turns a plan ("buy ICICIBANK at 1394.50, stop 1387.10")
into an exact order ("buy 53 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. It writes
a proposal only. `/nse-risk` checks it and the user says yes or no.

Be honest with the user: most people who day trade lose money. SEBI's study
of FY2023 found that about 7 in 10 individual intraday traders in equity cash
lost money. Sizing to a max loss keeps each loss small; it does not make a
trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows, `python` works too.

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                         # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                 # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1393.00 --json  # a LIMIT entry at 1393.00
python3 "${CLAUDE_SKILL_DIR}/size.py" --market --write        # a MARKET entry instead of LIMIT
python3 "${CLAUDE_SKILL_DIR}/size.py" --budget 50000          # try another budget once
```

`--now "2026-10-30 09:35"` pins the clock (India time, IST). A plan made for
another day is refused: make a new one with `/nse-exit`.

## Words, once

- **Stop (stop-loss)**: the price where you get out if the trade goes wrong.
- **Risk a share**: entry minus stop. **R** is that risk; a **1R target** is
  the entry plus one times the risk.
- **MIS**: Zerodha's intraday product. The position must be closed the same
  day; Zerodha closes it itself from about 15:20 and charges ₹50 + GST for that.
- **Charges**: brokerage, STT, exchange fees, SEBI fee, GST and stamp duty.
  On a small trade they are real money, so this skill counts them.
- **Tick**: the smallest price step. ICICIBANK moves in steps of 0.10.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (`/nse-exit` writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk for a MARKET order |
| `trader/portfolio.json` | optional: its `cash` caps the size |
| `trader/orders.json` | the one proposed trade it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees (default ₹1,00,000). Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose at the stop (default ₹500).
  With **count_charges_in_max_loss** true (the default) the charges count inside it.
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (₹75,000) and the smallest order (₹100).
- The day limits `/nse-risk` enforces: **max_daily_loss_inr** (₹1,500), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:30") and **flat_by** ("15:10"), all IST.
- **sl_limit_buffer_pct** (0.1): how far under the stop the SL order's limit price sits.
- **blocked**: NSE symbols they never want to trade.
- Keep **paper** true until they have watched it work without real money.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: India has no fractional shares. The size is the smallest
of four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade_inr` / risk a share; with charges counted, loss at the stop plus round-trip charges must fit |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` |

No leverage: Zerodha lends money for MIS trades, but this skill never sizes
on margin. The order value always fits the cash after the buffer.

The entry it sizes at: by default a LIMIT order at the plan's entry (or at
`--limit PRICE`). A LIMIT buy fills only at that price or lower, so it may
not fill at all. With `--market`, it sizes at the plan's entry, or at the
price in `trader/prices.json` if that is fresh (under 15 minutes old) and
higher: a price that ran up since the plan means a bigger loss at the same
stop, so fewer shares. Prices are put on NSE's tick grid: the stop rounds
down, the entry and the target to the nearest tick.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the symbol is blocked, the plan is a short, or the
product is not MIS. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees, the
loss at the stop and the charges. Say plainly that a stop is an order, not a
promise: in a fast market it can fill below the stop. Then hand over to
`/nse-risk`.

## Example: ICICI Bank, Friday October 30 2026

Opening-range breakout. The opening range (the high and low of the first 15
minutes, 09:15 to 09:30) was 1383.40 to 1390.90. The 09:30 bar closed at
1394.50 at 9:35 AM: the breakout. Stop 1387.10 (the opening-range midpoint,
rounded down to the tick), target 1401.90 (1R). Risk 7.40 a share. The
example config: ₹1,00,000 budget, 10% cash buffer, ₹500 max loss per trade.

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --now "2026-10-30 09:35" --write
```

| limit | the math | shares |
| --- | --- | --- |
| max loss | ₹500 / 7.40, charges counted | 57 |
| position cap | 75% of ₹1,00,000 = ₹75,000 / 1394.50 | **53 (decides it)** |
| max order | ₹75,000 / 1394.50 | 53 (the same) |
| cash | ₹1,00,000 minus ₹10,000 = ₹90,000 / 1394.50 | 64 |

53 shares = ₹73,908.50. At the stop: 53 x 7.40 = ₹392.20, plus about ₹73.32
in charges = ₹465.52, inside the ₹500 limit. Charges at the target: about
₹73.55. The ₹73.32 is: brokerage ₹40 (₹20 on each order), STT ₹18.38,
exchange ₹4.53, SEBI ₹0.15, GST ₹8.04, stamp duty ₹2.22. These are Zerodha's
published rates; the contract note is the real number.

The SL order's limit price is 1385.70 (0.1% under the 1387.10 trigger), so it
fills in a fast market without being a market order. A fill as low as
1385.70 would lose ₹466.40 plus charges (about ₹539): a little over the ₹500 plan. Tell the
user.

It writes (in `trader/orders.json`, under `orders`):

```json
{"symbol": "ICICIBANK", "exchange": "NSE", "tradingsymbol": "ICICIBANK",
 "transaction_type": "BUY", "quantity": 53, "product": "MIS",
 "order_type": "LIMIT", "price": "1394.50", "variety": "regular", "validity": "DAY",
 "exits": {"stop_loss": {"transaction_type": "SELL", "order_type": "SL", "trigger_price": "1387.10", "price": "1385.70"},
           "target": {"transaction_type": "SELL", "order_type": "LIMIT", "price": "1401.90"}},
 "oco_note": "Zerodha has no bracket order for this: place the stop and the target after the entry fills, and cancel the other one the moment one fills.",
 "charges_estimate": {"at_stop": 73.32, "at_target": 73.55},
 "max_loss": 392.2, "max_loss_with_charges": 465.52,
 "why": "opening-range breakout on ICICIBANK: 53 shares, bound by your position cap (53 x 7.40 = ₹392.20 at the stop, ₹465.52 with charges)",
 "plan": {"entry": 1394.5, "stop": 1387.1, "target": 1401.9, "risk_per_share": 7.4, "max_loss": 392.2,
          "setup": "orb", "entry_time": "9:35 AM", "flat_by": "15:10"}}
```

That is three orders, not one: the BUY first; after it fills, the SL stop
and the LIMIT target. When one of the two exits fills, cancel the other at
once, or a second fill could open a new position. `exits`, `oco_note`,
`charges_estimate`, `why` and `plan` are notes for `/nse-risk` and for the
user; they are not Kite fields.

Other answers from the same plan:

- "I only want to risk ₹300 a trade" (`"max_loss_per_trade_inr": 300`): the
  max loss decides it. 33 shares = ₹46,018.50; at the stop 33 x 7.40 = ₹244.20
  plus about ₹48.75 in charges = ₹292.95.
- "I have ₹50,000" (`--budget 50000`): the position cap decides it. 75% of
  ₹50,000 = ₹37,500 / 1394.50 = 26 shares = ₹36,257; at the stop ₹192.40,
  plus about ₹38.42 in charges = ₹230.82.
- A MARKET order when the price has already run to 1396.20 (a made-up quote,
  to show the rule): risk becomes 9.10 a share, the max loss decides 47
  shares, and reward:risk falls to 0.63, so it refuses. The move it was
  waiting for has mostly happened.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The exchange's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes delivery (CNC),
options, margin (MIS leverage) or short positions.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
