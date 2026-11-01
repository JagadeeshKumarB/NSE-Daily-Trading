---
name: nse-paper
description: >-
  Paper Trader. Practise a day trade on real NSE prices without real money.
  Shows the orders /nse-budget proposed and whether /nse-risk passed each one
  today, records a paper trade only after the user types yes for that exact
  order, then replays it on Yahoo's 5-minute bars (entry fill, stop, target,
  flat by 15:10 IST) and keeps a journal of every paper trade with Zerodha's
  charges taken off. Paper trades count against the trades-per-day and
  daily-loss limits in /nse-risk. Use when the user says "paper trade this",
  "practice trade", "place it on paper", "did my trade work", "how did my
  paper trade do", "settle my trades", "show my journal", "my paper P&L", or
  after /nse-risk passes an order.
argument-hint: "[show | place N | settle | journal]"
---

# nse-paper

The Paper Trader is the last step of a practice trade. Indian brokers have no
practice account, so this skill plays the broker: it takes the order that
`/nse-risk` passed, waits for your yes, writes it down, and later replays it on
the real 5-minute prices of that day to see what would have happened. No order
goes to Zerodha and no money moves.

Practise first. SEBI's study of FY2023 found that about 7 in 10 individual
intraday traders in equity cash lost money. A journal of 20 or more paper
trades, after charges, tells you more about a setup than any single good day.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows, `python` works too.

```bash
python3 "${CLAUDE_SKILL_DIR}/paper.py" show            # the proposed orders, READY or not
python3 "${CLAUDE_SKILL_DIR}/paper.py" place 1         # the ticket for order 1 (records nothing)
python3 "${CLAUDE_SKILL_DIR}/paper.py" place 1 --yes   # record it, ONLY after the user typed yes
python3 "${CLAUDE_SKILL_DIR}/paper.py" settle          # replay open paper trades on real bars
python3 "${CLAUDE_SKILL_DIR}/paper.py" journal         # every paper trade and the totals
```

`--now "2026-10-30 09:40"` pins the clock (IST). `settle --offline FILE`
reads a saved Yahoo chart or a `{SYMBOL: chart}` bundle instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the orders from /nse-budget (entry, SL stop leg, LIMIT target leg) |
| `trader/state/risk.json` | /nse-risk's verdicts; only a PASS from today for this exact order is READY |
| `trader/config.json` | `paper` (must be true), `no_entries_after` (14:30), `flat_by` (15:10), `charges` |
| `trader/state/paper.json` | the paper trades: placed, filled, closed, with profit or loss after charges |
| `trader/state/broker.json` | a `placed` record for each paper entry and a `filled` record for each exit, so /nse-risk counts them |

## How a paper trade is replayed

On the day's 5-minute bars (IST), from the first bar that starts at or after
the moment you placed it:

| event | rule |
| --- | --- |
| entry, LIMIT | the first bar whose low reaches your limit fills at the limit, or at the bar's open if it opened lower |
| entry, MARKET | the first bar's open |
| not filled | a LIMIT entry still waiting at 14:30 (`no_entries_after`) is cancelled |
| stop | a bar whose low reaches the trigger fills at the trigger, or at the open on a gap down. The bar that filled the entry counts for the stop only |
| target | a bar whose high reaches the target fills at the target, or at the open on a gap up |
| both in one bar | counted as the stop (the order inside a bar is unknown, so assume the worse) |
| flat by | still open at 15:10: sold at the open of the first bar at or after 15:10 |

Profit and loss is after Zerodha's estimated charges: brokerage, STT,
exchange fee, SEBI fee, GST and stamp duty. A trade on a day still in
progress stays open and shows its price so far.

## Steps

1. After `/nse-risk` passes, run `paper.py show`. Read the ticket to the user
   in plain words: what it buys, the stop, the target, the most it should lose.
2. Ask: "Paper trade this exact order? (yes / no)". Only an explicit yes for
   that one order counts. Then run `paper.py place N --yes`. Never pass
   `--yes` on your own, for a different order, or for "all of them".
3. Later in the day or after 15:30, run `paper.py settle` and tell the user
   what happened, with the charges.
4. Every few days, run `paper.py journal`. Say how many trades closed, won and
   lost, the net after charges, and that under 20 trades is too few to judge.

## Example: ICICIBANK, Friday October 30 2026

The order `/nse-budget` wrote and `/nse-risk` passed at 9:40 AM: buy 53
ICICIBANK, limit 1394.50, SL trigger 1387.10 (limit 1385.70), target 1401.90.

```
python3 paper.py --now "2026-10-30 09:40" place 1 --yes
Order 1 of 1: READY: nse-risk PASS today
  BUY 53 ICICIBANK on NSE, MIS (intraday), with a limit of 1394.50.
  Stop: SL SELL, trigger 1387.10, limit 1385.70.
  Target: LIMIT SELL 1401.90.
  Flat by 15:10 IST, win or lose.
  Most it should lose at the stop: ₹465.52 with charges.
Recorded paper trade P20261030-0940-ICICIBANK-80e2c3 at 9:40 AM IST. Nothing was sent to Zerodha.
```

At 11:00 AM, `settle` says it is open: bought 1392.10 at 9:40 AM (the 9:40
bar opened under the limit), last 1394.10, +₹106.00 so far before charges.
After the close:

```
python3 paper.py --now "2026-10-30 15:30" settle
... 53 ICICIBANK: bought 1392.10 at 9:40 AM, sold 1401.90 at 11:50 AM (the target):
    +₹519.40 gross, ₹73.53 charges, +₹445.87 net
```

A second ICICIBANK buy the same day is then blocked by `/nse-risk` as a
duplicate, and the day's count shows 1 of 3 trades and ₹465.52 of the ₹1,500
daily loss used. One winning day is one data point: `/nse-timemachine` found
this setup hit the stop first more often than the target over the previous 58
sessions.

## Rules

- Never record a paper trade without the user's yes for that exact order.
- Say "paper" every time: no real order, no real money.
- Always give profit and loss after charges, and the number of trades behind any total.
- Never call a paper result proof that a setup works.

## What it never does

It never sends an order to Zerodha or any broker, never records an order
that `/nse-risk` did not pass today, and never records the same order twice
in a day.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
