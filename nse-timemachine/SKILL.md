---
name: nse-timemachine
description: >-
  Time Machine for NSE intraday trades. Replays every possible outcome of a
  trade before you risk a rupee: 10,000 simulated sessions plus every past
  time the setup fired. Takes the plan from nse-exit (entry, stop, target),
  rebuilds the rest of the day 10,000 times from the stock's own 5-minute
  bars over the last 60 days, and re-runs the same rule on every past
  session. Prints the odds of target first, stop first, or neither by your
  flat-by time, where the price sits then, the average R before and after
  Zerodha's estimated charges, the history, and one honest verdict, then
  hands off to /nse-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /nse-exit writes a plan, before /nse-budget.
argument-hint: "[path to plan.json, or NSE symbol]"
---

# nse-timemachine

The Time Machine sits between the plan and the money. `/nse-exit` has set
the entry (the price you buy at), the stop (the price where you sell to cap
the loss) and the target (the price where you sell to take the gain). Before
anything is sized or sent, this skill asks one question: if today plays out
like the stock's own last 60 days, how often does this plan hit the target
first, the stop first, or neither? It answers with 10,000 replays and with
the real record of the same rule on every past session.

It is a simulation built from the stock's own recent bars, not a prediction.
Most people who day trade lose money: SEBI's study of FY2023 found that about
7 in 10 individual intraday traders in equity cash lost money. A good-looking
replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows, `python` works too.

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 14:30      # end each replay at another time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol ICICIBANK --date 2026-10-30 \
    --entry 1394.50 --stop 1387.10 --target 1401.90 --entry-time "9:35 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--qty N` (shares for the charges estimate), `--config PATH` (default
`trader/config.json`, optional), `--offline FILE` (a saved Yahoo chart JSON,
or a bundle `{"SYMBOL": chart, ...}`, `.json` or `.json.gz`; the symbol comes
from the plan), `--now "2026-10-30 09:35"` (IST) to pin the clock.

## Words used here

- **5-minute bar**: the open, high, low and close of the price over five
  minutes. NSE's normal market runs 9:15 AM to 3:30 PM IST: 75 bars a day.
- **Opening range**: the high and the low of the first 15 minutes (the 9:15,
  9:20 and 9:25 bars). A close above the high is the breakout (ORB).
- **R**: the risk per share, entry minus stop. +1R means you made what you
  risked; -1R means you lost what you risked.
- **MIS**: Zerodha's intraday product. The position must be closed the same
  day; Zerodha squares off open MIS positions itself from about 3:20 PM, for
  a ₹50 + GST fee. That is why the replay ends at your flat-by time.
- **Circuit limit**: the most a stock may move from yesterday's close in one
  day. A stock stuck at its circuit may not let you sell. 60 days of bars may
  never show that.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from nse-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by, product, tick |
| `trader/config.json` | optional: `max_loss_per_trade_inr` (default 500, for the charges estimate), `charges`, `tick_size`, `holidays` |
| Yahoo chart API | one request: 60 days of 5-minute bars for `SYMBOL.NS` (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history, charges}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at the end |

## How it works

1. **The pool.** It takes every session before the plan's date (today is
   left out). Every bar becomes four ratios to the bar before it: open, high,
   low and close over the previous close. Yahoo sometimes leaves a bar empty
   (often 3:15 to 3:25 PM, and on 2026-10-15 at 11:45 and 11:50 for every
   stock). An empty bar is skipped for that time of day only.
2. **One replay.** Start at the entry price. For each bar left until the end
   time, pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **The end time** is the plan's `flat_by` (default 3:10 PM), or `--until`.
   After 3:10 PM Yahoo's bars are often empty, so `--until 15:30` is refused.
4. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at the end time. Green, which means
   target first or above the entry at the end time.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at the end time divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
5. **Charges.** Zerodha's charges (brokerage, STT, exchange, SEBI, GST,
   stamp duty) are estimated for each round trip, for the number of shares
   that risks your `max_loss_per_trade_inr` at the stop (or `--qty`), and
   taken off in R. On a small stop they are a big slice of R. It is an
   estimate: the contract note is the real number.
6. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, prices on NSE's tick) runs again on every past session that
   has all its bars. That is what really happened, not a simulation.
7. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
8. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither, the average R when its sign goes against the lean,
   and a line when charges turn a positive average R negative.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 past sessions (or fewer than 20 with
a bar the replay needs), when the entry bar is not in the data (wrong date,
an NSE holiday or weekend, not closed yet), or when the plan does not make
sense (a stop above a buy's entry, an entry off the 5-minute grid). A live
in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/nse-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the end time, the average R before and after
   charges, the history line, then the verdict line and any line under it
   exactly as printed. Say the seed.
4. If the verdict is a coin flip, close to break-even, or leans toward the
   stop, say so plainly. Do not dress it up. If the history and the replays
   disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news and circuit limits can do things those 60 days never did.
6. If the user still wants the trade, hand off to `/nse-budget` to size it.

## Example: ICICI Bank, Friday 2026-10-30

The 15-minute opening range is 1383.40 to 1390.90. The 9:30 bar closes at
1394.50, above the range: entry 1394.50 at 9:35 AM (bar 3). The stop is the
range midpoint rounded down to the 0.10 tick, 1387.10. Risk 7.40 a share, so
the 1R target is 1401.90. Flat by 3:10 PM.

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --now "2026-10-30 09:35"
```

```
ICICIBANK buy, Friday 2026-10-30: entry 1394.50 at 9:35 AM (bar 3), stop 1387.10, target 1401.90 (1R, risk 7.40 a share).
10,000 replays of the rest of the session, 9:35 AM to 3:10 PM IST (67 five-minute bars), seed 19.
Each bar is drawn whole from ICICIBANK's own 5-min bars at the same time of day (+-2 bars) over its last 59 sessions (2026-08-05 to 2026-10-29).

  target first   40.7 %
  stop first     52.0 %
  neither        7.3 %    still open at 3:10 PM
  green          43.9 %   target first, or above the entry at 3:10 PM

  Price at 3:10 PM: median 1393.16, 90 % of replays between 1375.06 and 1411.65.
  Average R: -0.119 (R = the 7.40 risk: +1R at the target, -1R at the stop).
  Charges (MIS, estimate) for 67 shares (the shares that risk ₹500 at the stop): about ₹80.49 a round trip at the target, ₹80.20 at the stop, 0.16R a trade.
  Average R after charges: -0.281.

History: the same rule (15-min opening range, stop at the opening-range midpoint, 1R target) on each of those 58 real sessions fired 36 times: 12 target, 20 stop, 4 neither. Average R -0.219, -0.408 after charges.

Leans toward the stop: target first 40.7 % vs stop first 52.0 %.

Missing bars in 1 session(s): 2026-10-15 (no 11:45, 11:50). The replays draw those times from the other sessions; the history leaves those days out.
A simulation built from ICICIBANK's own last 59 sessions. It is not a prediction: results, news and circuit limits can do things those sessions never did. Same seed, same numbers. Repeat this run with --seed 19.
Next: /nse-budget sizes it to your budget and your max loss per trade.
```

What this says, in plain words. At 1R the target has to come first in half
of the replays that hit one side. It came first in 43.9 % of them. So the
replays lean toward the stop. The real history of the same rule on ICICIBANK
agrees: 12 targets against 20 stops in 36 trades. Charges make it worse: at
67 shares, about ₹80 a round trip is 0.16R taken off every trade, so the
average moves from -0.119R to -0.281R. On the real day this trade did reach
its target first; one good day does not change the odds above. That is what
the user should hear before `/nse-budget` sizes anything.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Charges are an estimate at Zerodha's published rates; say so.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.
- Muhurat trading (the one-hour Diwali session) is not supported.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
