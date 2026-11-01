---
name: nse-exit
description: >-
  Exit Planner for NSE intraday trades. Sets your stop and your target before
  you enter, so you know when to get out. Reads the entry from nse-entry (or
  a price you give), puts the stop at the opening-range midpoint by default
  (or the range low, your price, a percent, or a multiple of the ATR), rounds
  it down onto NSE's tick grid, and sets the target at 2R (or 1R, or your
  price). Refuses a stop at or above the entry, a target at or below it, or a
  stop too tight to survive the spread, and says why. Keeps you out before
  Zerodha's 15:20 MIS square-off. Writes trader/plan.json for the Time
  Machine and the Budget Manager. Long side only. Use when the user says
  "where's my stop", "set my stop and target", "plan the exit", "when do I get
  out", "stop loss", "what's my R", "risk reward", or right after /nse-entry
  triggers, before /nse-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# nse-exit

The Exit Planner decides how a trade ends before it starts. The **stop** (or
stop loss) is the price where the setup has failed and you sell to keep the
loss small. The **target** is the price where you take what you came for.
Both are set BEFORE the entry, written down and handed on, so nobody has to
decide anything in the middle of a fast move.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Most people who day trade lose money. SEBI's study of FY2023 found that about
7 in 10 individual intraday traders in equity cash lost money. A neat plan
does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol ICICIBANK --entry 1394.50 --or-high 1390.90 --or-low 1383.40
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

On Windows, `python` works too.

Other flags: `--flat-by 15:10`, `--json`, `--now "2026-10-30 09:35"` (India
time, IST), `--entry PRICE` with a waiting entry.json to plan ahead, `--setup
level`, `--offline FILE` (a saved Yahoo chart JSON, or a bundle of them
`{"ICICIBANK": <chart>, ...}`, `.gz` is fine, for an ATR stop). Use the plain
NSE symbol (ICICIBANK, not ICICIBANK.NS).

## Words used here

- **Opening range**: the high and the low of the first 15 minutes, the
  09:15, 09:20 and 09:25 five-minute bars.
- **R**: your risk on one share, entry minus stop. A 1R target is one risk
  above the entry, 2R is two.
- **Tick**: the smallest step a price can move on NSE. It depends on the
  price: ₹0.01 under ₹250, ₹0.05 up to ₹1,000, ₹0.10 up to ₹5,000, and so
  on. ICICIBANK at ₹1,394.50 moves in ₹0.10 steps.
- **MIS**: Zerodha's intraday product. You must be out the same day. From
  about 15:20 Zerodha closes ("squares off") any MIS position still open, at
  whatever price it gets, and charges about ₹50 + GST for it.
- **ATR**: average true range, how far a bar typically moves.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from nse-entry: symbol, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:10), `product` (MIS), `min_reward_risk` (1.0: a note here, nse-risk enforces it), `holidays`, `tick_size` |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, plus `"exchange": "NSE"`, `"product": "MIS"` and `"tick"`. nse-timemachine and nse-budget read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Rounding.** Every price sits on NSE's tick grid, one tick for the whole
  plan (from the entry price). The stop rounds DOWN to the tick, away from
  the entry. Risk = entry minus stop. Target = entry + R x risk, rounded to
  the nearest tick. `2R` by default; `1R`, `1.5R` or `price:X` also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A stop fewer than 5 ticks under the entry (₹0.50 on a ₹0.10
  tick): too tight to survive the spread, normal wiggle would stop you out.
  An entry.json that has not triggered (unless you give `--entry`). A short:
  it plans longs only.
- **Flat by.** 15:10 IST by default (or `flat_by` in the config). Out by then,
  win or lose, no overnight hold. For MIS a flat-by at or after 15:20 is
  refused, because Zerodha starts squaring off MIS positions itself then. An
  entry at or after the flat-by time is refused too.
- **Charges.** The plan is per share, so it has no charges in it. Brokerage,
  STT, GST and stamp duty are real money on a small trade: `/nse-budget`
  counts them when it sizes the trade.
- **Holidays.** NSE's 2026 holidays are built in. On a holiday it says NSE
  is closed; for a year not built in it says so and asks for `holidays` in
  the config.

## Steps

1. Run it right after `/nse-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target, an earlier flat-by). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/nse-timemachine` to replay the plan, then `/nse-budget` to
   size it to the max loss per trade, charges included.

## Example: ICICI Bank, Friday October 30 2026

ICICIBANK's opening range was ₹1,383.40 to ₹1,390.90. The 09:30 bar closed
at ₹1,394.50, above the range high: the breakout, at 9:35 AM (bar 3).
`exit.py plan --stop or-mid --target 1R --write`:

```
Exit plan: ICICIBANK long on NSE (MIS, intraday), Fri 2026-10-30, opening-range breakout on 5-minute bars
  Entry         ₹1,394.50 at 9:35 AM (bar 3)
  Stop          ₹1,387.10  opening-range midpoint: (₹1,390.90 + ₹1,383.40) / 2 = ₹1,387.15, rounded down to the ₹0.10 tick
  Target        ₹1,401.90  1R (one times the risk)
  Risk a share  ₹7.40 (0.53% of the entry)
  Reward:risk   1.0
  Flat by       15:10 IST, win or lose: no overnight hold (Zerodha squares off MIS from 15:20)
  Tick          ₹0.10 (prices move in steps of this size)
Set before the entry. If price hits the stop, you are out; never move it further away.
A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Charges (brokerage, STT, GST, stamp duty) are not in this per-share plan: /nse-budget counts them.
```

The midpoint ₹1,387.15 is not on the ₹0.10 grid, so the stop goes down to
₹1,387.10. The default 2R target would be 1,394.50 + 2 x 7.40 = ₹1,409.30.
An `atr:1.5` stop on the same bars: the ATR over the 4 bars to the entry is
₹5.00, so the stop is ₹1,387.00, risk ₹7.50, 1R target ₹1,402.00.
`/nse-budget` then turns the risk a share into a number of shares that fits
your max loss per trade, after charges.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only: it plans longs only. MIS by default; CNC (delivery) is not
  a day trade and it says so.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
