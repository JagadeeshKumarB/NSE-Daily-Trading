# The India Day Trader skill

8 Claude skills for intraday trading on India's NSE: find a setup, time the
entry, plan the exit, replay the trade 10,000 times, size it with Zerodha's
charges, check it against your limits, practise it on paper before you risk a
rupee, and watch it all on a live chart.

Free, MIT, no API key. Plain Python that reads free public data: Yahoo
Finance's 5-minute charts (`RELIANCE.NS`) and NSE's own lists of stocks and
price bands. Ported from
[github.com/Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(the US version) to NSE hours, rupees, NSE tick sizes, SEBI rules and
Zerodha's charges.

**Nothing in this pack sends an order to a broker.** Paper trading is the
default and, for now, the only mode.

## Install

You need Claude Code and Python 3.8 or newer (nothing to pip install).

```bash
# copy the skills where Claude Code finds them
cp -r nse-* ~/.claude/skills/
```

On Windows (PowerShell):

```powershell
Copy-Item -Recurse nse-* "$env:USERPROFILE\.claude\skills\"
```

Then open Claude Code in an empty folder for your trading notes and type
`/nse-scanner`. Each skill writes into a `trader/` folder there. Copy
`nse-budget/config.example.json` to `trader/config.json` and set your own
budget and limits first.

## The eight

| command | job | what it does |
| --- | --- | --- |
| `/nse-scanner` | Chart Scanner | Lists NSE stocks that match a rule: opening-range breakouts during the day, 20-day breakouts, flags and oversold stocks before the open. |
| `/nse-entry` | Entry Timer | Watches one stock bar by bar and says the moment a breakout triggers. |
| `/nse-exit` | Exit Planner | Sets the stop and the target before you buy, on NSE's tick grid. |
| `/nse-timemachine` | Time Machine | Replays the rest of the day 10,000 times from the stock's own last 59 sessions, plus every past day the same rule fired. |
| `/nse-budget` | Budget Manager | Works out how many shares fit your budget and your max loss, charges included, and writes Zerodha-shaped orders. |
| `/nse-risk` | Risk Manager | PASS or BLOCK against your limits: max loss, daily loss, trades a day, timing, EQ series, circuit limits. |
| `/nse-paper` | Paper Trader | Records the trade only after you say yes, replays it on real prices and keeps a journal after charges. |
| `/nse-chart` | Live Chart | A live page on your computer: the Nifty 50 index and its stocks on 5-minute candles, with the entry, stop and target drawn, and a bullish or bearish read with its 60-session record. |

## How a trade flows

```
scanner -> entry -> exit -> timemachine -> budget -> risk -> paper (your yes) -> paper settle -> journal
```

A real example: ICICI Bank, Friday October 30 2026, real data.

1. **`/nse-scanner`** at 9:40 AM: 10 of the Nifty 50 had closed a 5-minute bar
   above their opening range (the 9:15 to 9:30 high). ICICIBANK is one: range
   ₹1,383.40 to ₹1,390.90.
2. **`/nse-entry`**: triggered at 9:35 AM, the 9:30 bar closed at ₹1,394.50.
3. **`/nse-exit`**: stop at the middle of the range, ₹1,387.10 (₹7.40 a share
   at risk); target one times that risk, ₹1,401.90; out by 15:10 either way.
4. **`/nse-timemachine`**: target first 40.7 %, stop first 52.0 % in 10,000
   replays; on the 58 real sessions before, the rule fired 36 times: 12
   targets, 20 stops. After charges, the average trade lost money. That is
   what looking before you risk a rupee is for.
5. **`/nse-budget`** with ₹1,00,000 and a ₹500 max loss: 53 shares
   (₹73,908.50). At the stop: ₹392.20 plus ₹73.32 charges = ₹465.52.
6. **`/nse-risk`**: PASS.
7. **`/nse-paper`**: you type yes, it records a paper trade. After the close,
   `settle` says: bought 1392.10, sold at the target 1401.90 at 11:50 AM,
   +₹519.40 gross, ₹73.53 charges, +₹445.87 net. One good day is one data
   point; keep a journal of 20 or more before you judge a setup.

## The live chart

```bash
python nse-chart/live.py          # opens http://127.0.0.1:8050
```

It refreshes itself from 9:15 to 15:30 IST. The bias adds five plain votes
(opening range, EMA 9 against 21, previous close, RSI, VWAP or breadth) and
shows how often that read was right on the last 60 sessions. On the Nifty 50
index to October 30 2026, after a Bullish read the close 30 minutes later was
higher 49 % of the time, against 47 % at any moment: close to a coin flip. A
bias describes the chart so far. It is not a forecast.

## Tests

```bash
python -m unittest discover -s tests -v
```

Offline, on real data saved in `tests/fixtures/nse/`: 60 sessions of 5-minute
bars for the Nifty 50 to October 30 2026, NSE's equity list and price bands.
They reproduce every number above.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It cannot see the future. SEBI's study of FY2023 found that about
7 in 10 individual intraday traders in equity cash lost money. Talk to a
SEBI-registered adviser if you need advice.

**Paper first.** `"paper": true` is the default. Practise until you have a
journal of at least 20 paper trades after charges.

**Charges matter.** On a ₹74,000 intraday round trip Zerodha's brokerage,
STT, exchange fee, GST and stamp duty come to about ₹73. On small trades that
can be most of the profit. The numbers here are estimates; your contract note
is the real one.

**MIS and square-off.** Intraday (MIS) positions are squared off by Zerodha
from about 15:20, with a charge. The default `flat_by` is 15:10.

**A stop is a plan, not a guarantee.** A gap or a fast market can fill past
it, and a stock locked at its circuit limit may not trade at all.

**The data is free and unofficial.** Yahoo's charts can lag or miss bars
(NSE's 3:20 PM bar is usually missing). Check anything that matters on Kite.

**Taxes.** Intraday equity profit is speculative business income in India.
Nothing here is tax advice.

## Files

```
nse-scanner/      SKILL.md, scan.py
nse-entry/        SKILL.md, entry.py
nse-exit/         SKILL.md, exit.py
nse-timemachine/  SKILL.md, timemachine.py
nse-budget/       SKILL.md, size.py, config.example.json
nse-risk/         SKILL.md, check.py
nse-paper/        SKILL.md, paper.py
nse-chart/        SKILL.md, live.py (server, commands), chart_data.py, chart_signals.py, chart_alerts.py, page.html
ref/              PORTING.md (the spec), india_common.py (the India block), SAFETY.md
tests/            unittest suite + offline NSE fixtures
```

MIT. The original US skills are by Jake Schincariol (see LICENSE).
