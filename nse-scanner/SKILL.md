---
name: nse-scanner
description: >-
  Chart Scanner for NSE stocks. Scans hundreds of Indian stock charts for
  setups: opening-range breakouts, flags, oversold bounces. Builds the list
  of tradable NSE stocks (series EQ, with each stock's price band) from NSE's
  own files, or just the Nifty 50 / 100 / 500, runs a scan of daily closes
  (20-day closing-high breakout, bull flag, reclaimed the 20-day average,
  oversold, oversold bounce) and an intraday opening-range breakout scan on
  5-minute bars, then writes trader/watchlist.json for nse-entry. Free Yahoo
  data (RELIANCE.NS), bar by bar, times in IST. Use when the user says "scan
  the market", "scan the Nifty", "find me setups", "what is breaking out",
  "opening range breakouts", "scan for bull flags", "oversold stocks", "build
  my watchlist", "what should I look at today", or at the start of a trading
  day.
argument-hint: "[universe | daily | orb] [--index nifty50] [--at HH:MM]"
---

# nse-scanner

The Chart Scanner is the first step of a day trade. It reads free charts of
NSE stocks and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money:
SEBI's study of FY2023 found that about 7 in 10 individual intraday traders
in the equity cash market lost money. The scanner does not change that. It
only saves you from flipping through 2,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows, `python` works too.

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                        # all EQ stocks -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --index nifty50        # or nifty100, nifty500
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                           # after 3:30 PM or before 9:15 AM -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols "RELIANCE,TCS,M&M"  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                             # from 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:00                  # cut the clock at 10:00 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price` (rupees, default 50) and `--min-volume` (shares; daily
1,00,000, orb 0), `--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--include-be` (universe), `--now "2026-10-30 10:00"` (IST) to pin the clock,
`--offline PATH` to read saved files, `--no-write`, `--show N`, `--json`.
Exit code 0 when the scan found something, 1 when it found nothing or could
not run (the reason is one plain line).

## Words used here

- **Symbol**: the NSE trading symbol, like `ICICIBANK` or `M&M`. Yahoo calls
  it `ICICIBANK.NS`; the scanner adds and removes the `.NS` for you.
- **Series**: NSE's group for a stock. Only **EQ** stocks can be bought and
  sold the same day (intraday, Zerodha's product **MIS**). **BE** and **BZ**
  are trade-for-trade: every buy must be taken as delivery, so no day trades.
- **Price band (circuit limit)**: how far a stock may move from yesterday's
  close in one day: 2, 5, 10 or 20 %. Stocks with futures and options show
  "No Band": NSE sets their limits on the fly. A stock stuck at its upper
  circuit often has no sellers, so you cannot buy it normally.
- **Opening range**: the high and the low of the first 15 minutes, 9:15 to
  9:30 AM: the 9:15, 9:20 and 9:25 five-minute bars.
- **Opening-range breakout (ORB)**: a later 5-minute bar *closes* above the
  opening-range high.
- **RSI**: a 0-to-100 score of recent ups versus downs (14 days). Under 30
  is called oversold.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name\|Series\|Band` a line, like `RELIANCE\|Reliance Industries Limited\|EQ\|No Band`. Read by `daily` and `orb`, and by `/nse-risk` (series and band checks) |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of` (IST), `universe`, `scanned`, `failed`, `hits[]`. `/nse-entry` reads `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:30); by both: `holidays` |

## The universe

`universe` downloads two files from NSE (nsearchives.nseindia.com):

1. `EQUITY_L.csv`, every listed equity with its series. It keeps series EQ
   only; `--include-be` keeps BE too (for a delivery watchlist; still no
   intraday).
2. `sec_list.csv`, each stock's price band. A stock missing from it gets an
   empty band ("unknown": check it yourself before you trade it).

`--index nifty50`, `nifty100` or `nifty500` takes the index's list from
niftyindices.com instead, still with the bands. Start with the Nifty 50 or
Nifty 500: they are the most traded stocks, and a beginner has no reason to
day trade a thin small-cap.

On NSE's files of Oct 30 2026 (in `tests/fixtures/nse/`):

```
python3 scan.py universe --offline tests/fixtures/nse
NSE's list of equities (EQUITY_L.csv): 2,620 rows.
Kept 2,357 stocks (2,357 EQ).
Dropped: 230 series BE, 33 series BZ (trade-for-trade: delivery only, no intraday).
Price bands: 1,884 20 %, 218 No Band (F&O), 171 10 %, 83 5 %, 1 unknown.
```

The Nifty 50 list keeps 50 stocks, all "No Band" (they all have futures and
options); the Nifty 500 file of that day has 501 rows.

## The daily scan (after the close, or before the open)

Yahoo's spark endpoint returns daily closes for 20 symbols a request, so the
Nifty 500 takes 26 requests and all 2,357 EQ stocks take 118. Liquidity
comes from the same answer: the last price must be at least `--min-price`
(₹50: under that the tick, ₹0.01 to ₹0.05, is a big slice of a small move)
and the last session's volume at least `--min-volume` (1,00,000 shares; it
counts shares, not rupees, so a 2-lakh floor would drop ULTRACEMCO, a
₹11,045 stock that traded ₹220 crore that day). Run it after the 3:30 PM
close or before 9:15 AM: during the session the volume is the day's so far,
and today's daily bar is left out until 3:30 PM.

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (price times volume).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session, 9:15 to
9:30 AM. A hit is the first completed 5-minute bar to CLOSE above the range
high, at that bar's close time. The first possible hit is the 9:30 bar,
which closes at 9:35 AM; before 9:35 the scan says "Too early" in one line.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. Stage 2 checks at most `--max-confirm` (200) charts, the
   most traded first (price times volume so far), and prints PARTIAL with the
   count it skipped. `--max-confirm 0` checks them all.

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt or no trades. The last 15 minutes (from
3:15 PM) are the exception: Yahoo's NSE charts never have a 3:20 bar and often
miss 3:15 and 3:25 (the window NSE uses to compute the closing price), so
those bars are used when present but not required. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:49:45) is dropped, and a
bar counts only once it is complete (start + 5 minutes).

It prints one plain line and writes nothing before 9:35 AM, on a weekend or
an NSE holiday, after the 3:30 PM close, or after `no_entries_after` (14:30
by default: a breakout found later is expired, and Zerodha squares off MIS
positions from about 3:20 PM anyway). NSE's 2026 holidays are built in; for
2027 and later it says the list is missing and counts every weekday as open
until you add `"holidays"` to trader/config.json. Muhurat trading (the
one-hour Diwali session) is not supported.

## Example: Friday October 30 2026

All numbers below come from running the tool on the real Yahoo data saved in
`tests/fixtures/nse/`.

**The evening before, or that morning: the daily scan of the Nifty 50.**

```
python3 scan.py universe --offline tests/fixtures/nse --index nifty50
python3 scan.py daily --universe trader/universe.txt --offline tests/fixtures/nse/nifty50-daily-spark.json.gz --now "2026-10-30 16:00"
Daily scan, Fri 2026-10-30 16:00 IST, closes through 2026-10-30 (Yahoo spark, daily closes only).
Fetched 40 of 50 charts (10 failed or had no data). 40 passed the liquidity filter (₹50+ and 1,00,000+ shares); 40 scanned.
PARTIAL: no data for M&M, MARUTI, MAXHEALTH, NESTLEIND, NTPC, ONGC, POWERGRID, RELIANCE, SBILIFE, SHRIRAMFIN.
Ignored 1 answer for a symbol that is not one asked for: M (Macy's, Inc., NYQ).
15 charts matched a setup: 11 20-day closing-high breakout, 3 bull flag, 2 reclaimed the 20-day average.
Most traded first (price x volume):
  INFY          ₹1,185.00  RSI  64.3  20-day closing-high breakout
  TRENT         ₹3,017.30  RSI  61.9  20-day closing-high breakout
  ETERNAL         ₹343.65  RSI  62.1  20-day closing-high breakout
  ...
  HCLTECH       ₹1,428.10  RSI  69.6  20-day closing-high breakout, bull flag
```

This saved answer is partial, and the scanner says so. It was fetched with
`M&M.NS` written into the web address as is. In a web address `&` starts a
new field, so Yahoo read the list as ending in `M`, answered for Macy's (a
US store chain), and never saw M&M or the nine symbols after it in that
request of 20. The scanner now writes `M%26M.NS` (the `&` encoded), ignores
any answer for a symbol it did not ask for, and lists the symbols with no
data. A live run of the Nifty 50 the same evening got all 50 back.

INFY closed at ₹1,185.00, above ₹1,168.00, its best close of the 20 sessions
before: a 20-day closing-high breakout. That is a chart to look at tomorrow,
not a buy.

**During the session: the opening-range scan at 10:00 AM.**

```
python3 scan.py orb --offline tests/fixtures/nse/nifty50-5m-60d.json.gz --now "2026-10-30 10:00"
Opening-range breakout scan, Fri 2026-10-30 10:00 AM IST: 15-minute range (9:15 to 9:30 AM), 5-minute bars, Yahoo.
Fetched 50 of 50 charts; 50 usable; 32 candidates, 32 confirmed with real highs and lows.
13 of 50 charts closed above their opening-range high by 10:00 AM:
   9:35 AM  BAJAJFINSV    ₹1,666.90  range 1,658.00 to 1,666.40  +0.03% over
   9:35 AM  BAJFINANCE      ₹940.50  range 932.05 to 937.70  +0.30% over
   9:35 AM  HCLTECH       ₹1,428.50  range 1,415.60 to 1,427.40  +0.08% over
   9:35 AM  ICICIBANK     ₹1,394.50  range 1,383.40 to 1,390.90  +0.26% over
   9:35 AM  INDIGO        ₹4,788.80  range 4,638.00 to 4,766.70  +0.46% over
   9:35 AM  JSWSTEEL      ₹1,255.70  range 1,247.20 to 1,253.80  +0.15% over
   9:35 AM  KOTAKBANK       ₹460.85  range 457.30 to 460.80  +0.01% over
   9:35 AM  TCS           ₹2,521.30  range 2,500.80 to 2,520.20  +0.04% over
   9:40 AM  APOLLOHOSP    ₹8,282.50  range 8,181.50 to 8,250.00  +0.39% over
   9:40 AM  ETERNAL         ₹342.30  range 335.30 to 341.70  +0.18% over
   9:45 AM  AXISBANK      ₹1,223.90  range 1,218.60 to 1,223.80  +0.01% over
   9:45 AM  BAJAJ-AUTO   ₹10,402.00  range 10,314.00 to 10,386.00  +0.15% over
   9:45 AM  SBILIFE       ₹1,657.30  range 1,647.90 to 1,655.70  +0.10% over
```

ICICI Bank's opening range (9:15 to 9:30) had a high of ₹1,390.90 and a low
of ₹1,383.40. The 9:30 bar closed at ₹1,394.50, 0.26 % over the high, at
9:35 AM: the breakout. At 9:35 AM itself the same scan lists 8 hits; by 2:30
PM, 34; by the close, 35 of the 50 had broken their range at some point (a
breakout is common, not special). Run at 9:30 AM the scan only says it is too
early. The user picks ICICIBANK and Claude hands it to `/nse-entry
ICICIBANK`.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429. A polite 0.3 s pause
  between requests.
- Partial results are said as partial: "Fetched 40 of 50 charts (10 failed or
  had no data)", followed by the symbols. Never round a partial scan up to
  "the whole market".
- NSE's archive sometimes refuses scripts; the scanner sends a browser-like
  user agent. If `sec_list.csv` cannot be read, the universe is still written
  with empty bands and a note.
- NSE holidays for 2026 (NSE circular, read 2026-10-30). Times are IST, which
  has no daylight saving.

## Steps

1. Once a week or so: `scan.py universe --index nifty500` (or plain
   `universe` for every EQ stock).
2. After the close or before the open: `scan.py daily`. Tell the user the
   counts (fetched, liquid, scanned, hits), any symbols with no data, and the
   top few by setup.
3. From 9:35 AM IST (10:00 is a calmer first look): `scan.py orb`. Tell the
   user how many usable charts broke their range and when, and that a later
   hit is closer to the price now.
4. The user picks one chart. Hand it on: `/nse-entry SYMBOL` (the entry, bar
   by bar). Never pick for them.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a profit and loss, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.
- A stock with an empty band or a series other than EQ needs a check before
  any intraday trade: `/nse-risk` does it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
