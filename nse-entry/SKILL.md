---
name: nse-entry
description: >-
  Entry Timer for NSE stocks (India, intraday, longs only). Watches a setup
  bar by bar and tells you the moment to get in. Reads free Yahoo 1-minute or
  5-minute bars, builds the opening range (the first 15 minutes after the 9:15
  IST open by default) and calls the entry on the first completed bar that
  CLOSES above the range high, or above a price you give it (like a 20-day
  high from the scanner), with an optional volume check. Says waiting and how
  far, triggered with the time and the price in rupees, expired, or not open
  yet, and writes trader/entry.json for /nse-exit. It never places an order.
  Use when the user says "tell me when to get in", "watch ICICIBANK", "has it
  broken out", "did it break the opening range", "entry signal", "time my
  entry", "check my watchlist", or after /nse-scanner, before /nse-exit.
argument-hint: "[SYMBOL] [--setup orb|level] [--interval 5m|1m]"
---

# nse-entry

The Entry Timer turns a setup into one moment: the bar that confirms it.

A **bar** is one slice of the chart: the open, high, low and close of a
stock over 5 minutes (or 1 minute). The tool reads bars and waits for each
one to finish. A price that pokes above the high in the middle of a bar is
not an entry. A bar that CLOSES above it is. Then it says the time and the
price, writes them down for the Exit Planner (`/nse-exit`), and stops. It
never buys anything.

Most people who day trade lose money. SEBI's study of FY2023 found that about
7 in 10 individual intraday traders in equity cash lost money. A clean signal
does not change that. Day trading loses most people money; treat every signal
as a question, not an answer.

All times are IST (India Standard Time). NSE's normal market runs 9:15 to
15:30, Monday to Friday, except NSE holidays.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows, `python` works too.

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ICICIBANK               # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ICICIBANK --write       # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ICICIBANK --setup level --level 1400
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ICICIBANK --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch ICICIBANK --until 14:30 # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                     # every hit in trader/watchlist.json
```

Use the plain NSE symbol (`ICICIBANK`, `RELIANCE`, `M&M`); the tool adds
Yahoo's `.NS` itself. Other flags: `--or-minutes N` (default 15), `--json`,
`--now "2026-10-30 09:35"` (IST) to replay a day, and `--offline PATH` to read
saved bars instead of Yahoo: one saved Yahoo chart (`.json` or `.json.gz`), a
bundle of many symbols like `{"ICICIBANK": <chart>, ...}` (the tool picks the
symbol and the `--now` day out of it), or a folder of `<SYMBOL>.json` files.
`watch` with `--now` or `--offline` runs on a pretend clock and replays the
day bar by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30), `no_entries_after` (14:30), IST, and `holidays` (extra NSE holidays). Optional |
| Yahoo chart API | today's bars, `ICICIBANK.NS`, `range=1d&interval=5m` (or `1m`): free, unofficial, can lag |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes of the
  day: the 9:15, 9:20 and 9:25 five-minute bars (or fifteen 1-minute bars).
  The range is set at 9:30. The level to beat is the range high. "orb" is
  short for opening-range breakout: the price breaking out above that range.
- **Level.** A price you give, like a 20-day high from `/nse-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick (a quick poke) above is not a trigger. A close
  exactly at the level is not above it.
- **The time** is that bar's close time (the 9:30 bar closes at 9:35 AM).
  The price is its close in rupees, on NSE's price steps (the tick: 10 paise
  for a stock between ₹1,000 and ₹5,000).
- **Too early.** The first bar after the range is the 9:30 bar, and it
  closes at 9:35. Before then an orb check says "too early" in one line.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:49:45, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the average of the range bars. Yahoo shows no
  volume for NSE's 9:15 bar on most days, so the average uses the range bars
  that have volume.
- **Time limits.** No entries before 9:30 or after 14:30 (both in config).
  A late entry leaves little room before intraday positions must be closed
  (Zerodha squares off MIS, intraday, positions itself from about 15:20).
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:30: the
  range is still forming. Weekends and NSE holidays: the market is closed,
  with the next open. For a year whose NSE holiday list is not built in, it
  says so and every weekday counts as open until you add `holidays` to the
  config. The special Muhurat session at Diwali is not supported.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/nse-scanner`. Check the news on the stock yourself first (a news skill
   is coming later).
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 14:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/nse-exit` to set the stop (the price where you get out if
   you are wrong) and the target BEFORE anything is bought.

## Example: ICICI Bank, Friday October 30 2026

ICICIBANK on 5-minute bars, real Yahoo data. The 9:15, 9:20 and 9:25 bars
make the opening range: high ₹1,390.90, low ₹1,383.40.

At 9:33 the 9:30 bar is still open, so it waits:

```
python3 entry.py check ICICIBANK --setup orb --now "2026-10-30 9:33"
```

```
WAITING  too early: the first bar after the opening range, the 9:30 bar, closes at 9:35 AM; last close ₹1,389.80 (9:25 bar), ₹1.10 under the range high ₹1,390.90 (-0.08%).
The 9:30 bar is still forming (₹1,394.50 so far, closes 9:35 AM); it does not count until it closes.
```

(The "so far" price comes from the saved day. On a live morning it would be
whatever the bar shows at 9:33, and it can still change.)

At 9:35 the bar has closed, above the range high:

```
python3 entry.py check ICICIBANK --setup orb --now "2026-10-30 9:35" --write
```

```
ICICIBANK  Fri 2026-10-30 09:35 IST  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag)
Opening range 9:15-9:30: high ₹1,390.90, low ₹1,383.40 (3 of 3 bars).
TRIGGERED 9:35 AM at ₹1,394.50: the 9:30 bar (bar 3) closed above the range high ₹1,390.90 (+0.26%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /nse-exit (exit.py plan --from trader/entry.json).
```

Bars are counted from 0, so the 9:30 bar is bar 3. `trader/entry.json` now
holds `"time": "9:35 AM"`, `"price": 1394.5`, `"level": 1390.9` and the
opening range, for `/nse-exit`.

`watch` replays the same morning one bar at a time:

```
 9:25 bar  closed ₹1,389.80  opening range set: high ₹1,390.90, low ₹1,383.40
 9:30 bar  closed ₹1,394.50  TRIGGERED: closed above ₹1,390.90 (+0.26%). Get in now if you take it: 9:35 AM, ₹1,394.50
```

With `--min-volume-x 1` the 9:30 bar is too thin (0.83x the range's volume),
and the trigger moves to 11:15 AM at ₹1,395.60 (the 11:10 bar, 2.52x).

That day 34 of the 50 Nifty 50 stocks closed a 5-minute bar above their
opening range before the 14:30 cutoff. A breakout is common. It is not a
promise that the price keeps going.

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can lag. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Longs only: this pack plans buys, not short sales. If the user wants a
  short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.
- Show the plain NSE symbol and rupees (₹1,394.50) to the user.

## What it never does

It never places an order, never sizes a position (that is `/nse-budget`),
never sets the stop (that is `/nse-exit`), never moves the level after the
fact, and never counts a bar that has not closed. It never places a trade
from a background watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
