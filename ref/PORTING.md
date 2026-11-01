# Porting the day-trader skills to the Indian market (NSE)

This repo is a port of github.com/Jakeschincariol/day-trader-skill (MIT) to
NSE cash equities, intraday, long only. The user is a beginner in India who
trades through Zerodha. Six core skills are ported first:

| folder | was | script |
| --- | --- | --- |
| `nse-scanner/` | trader-scanner | scan.py |
| `nse-entry/` | trader-entry | entry.py |
| `nse-exit/` | trader-exit | exit.py |
| `nse-timemachine/` | trader-timemachine | timemachine.py |
| `nse-budget/` | trader-budget | size.py |
| `nse-risk/` | trader-risk | check.py |

`ref/trader-broker/` is the original broker skill, for reference only (a
later nse-broker and nse-paper will be built on Zerodha's Kite MCP).
`ref/india_common.py` is THE India block. Every script that needs time,
calendar, ticks, charges or rupees pastes that block verbatim (scripts stay
standard-library only and self-contained: no imports between skills).

## Ground rules

- Keep each script's design, flags, exit codes, messages and safety rules.
  Change what is US-specific, nothing else. Delete US-only features (PDT
  guard, SEC user agent, NYSE early closes, Alpaca shapes, fractional shares,
  `--weights` 13F mode in size.py may be dropped if it is US-only).
- Standard library only, Python 3.8+. Call `utf8_stdout()` first in `main()`
  (Windows pipes are cp1252 and cannot print ₹).
- Commands in SKILL.md stay `python3 ...`; add one line: "On Windows, `python` works too."
- Skill names: frontmatter `name: nse-scanner` etc. Every `/trader-foo`
  reference becomes `/nse-foo`. Skills not yet ported (news, alerts, broker,
  analyst, pelosi) are referred to as "coming later" or simply not mentioned.
- The working folder stays `trader/` (trader/config.json, trader/plan.json, ...).
- Only edit your own skill folder and your own test file
  `tests/test_<name>.py`. Shared fixtures in `tests/fixtures/nse/` are
  read-only; add skill-specific fixtures under `tests/fixtures/nse-<name>/`.
  Do not touch `tests/fixtures/trader-*` (old US fixtures; deleted later),
  other skills, or `ref/`.
- Tests: unittest, offline, deterministic; `python -m unittest tests.test_<name> -v`
  from the repo root must pass. `tests/_load.py` has `load(folder, name)` and
  `FIX`. Rewrite the US tests against the Indian fixtures; do not keep tests
  for removed features. Never invent numbers in SKILL.md: every number in an
  example must come from running the tool on the fixtures.
- Write for a beginner: plain English, short sentences, say what a term means
  the first time (opening range, R, stop, MIS, circuit limit). Keep "Most
  people who day trade lose money" honesty. SEBI's own study (FY2023) found
  about 7 in 10 individual intraday traders in equity cash lost money; you may
  cite it as "SEBI's study of FY2023".

## Market facts (all in the India block)

- Time is IST, UTC+5:30, no daylight saving. Normal market 09:15 to 15:30.
  The pre-open auction (09:00-09:08) is ignored.
- 5-minute bars start at 09:15. The default opening range is 15 minutes: the
  09:15, 09:20 and 09:25 bars. The first bar after it closes at 09:35, so an
  ORB scan / entry check before 09:35 says "too early" in one line.
- Holidays: `NSE_HOLIDAYS` (2026). For a year not built in, use
  `calendar_warning()` and print the line; config `holidays` extends the list.
  Muhurat trading is not supported (say so if the user asks).
- Yahoo symbols: `RELIANCE` <-> `RELIANCE.NS` via `to_yahoo` / `from_yahoo`;
  URL-quote symbols (`M&M.NS` -> `M%26M.NS`, `BAJAJ-AUTO.NS` is fine). Show
  users the plain NSE symbol. Yahoo's 5-minute bars go back 60 days.
- Prices are rupees on NSE's tick grid: `to_tick(price, "down"|"up"|"nearest")`.
  A long's stop rounds DOWN to the tick, a target rounds to the nearest tick,
  where the US code used cents. Show money with `inr()` (₹1,23,456.50).
- Quantities are whole shares (no fractional shares in India).
- Charges are real money on small trades: `charges(buy_value, sell_value, product)`
  (Zerodha's equity rates). Product `MIS` = intraday (Zerodha squares off MIS
  equity positions itself from about 15:20, with a ₹50 + GST charge), `CNC` =
  delivery (not a day trade).
- Series: only `EQ` stocks can be traded intraday. `BE`/`BZ` (trade-for-trade)
  must be delivered: no MIS.
- Circuit limits (price bands): each stock may move only a set percent (2, 5,
  10 or 20) from the previous close in a day; F&O stocks show "No Band" and
  have dynamic bands. A stock locked at its upper circuit often cannot be sold
  or bought normally.

## Shared files (the contract between skills)

Field names are unchanged from the US version except: every `*_usd` key
becomes `*_inr`, `"dollars"` -> `"rupees"`, `value_usd` -> `value_inr`. Prices
in JSON are numbers in rupees (plan.json) or strings (orders.json, like Kite
wants them... see below).

`trader/universe.txt` (scanner writes, risk reads): one line a stock,
`SYMBOL|Name|Series|Band`, e.g. `RELIANCE|Reliance Industries Limited|EQ|No Band`.
Built from NSE's `EQUITY_L.csv` (keep series EQ by default; `--include-be`
keeps BE too) joined with `sec_list.csv` for the band (missing -> empty).
`--index nifty50|nifty100|nifty500` builds it from niftyindices.com
constituent CSVs (`https://niftyindices.com/IndexConstituent/ind_nifty500list.csv`)
instead, still joined with the band. URLs:
`https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`,
`https://nsearchives.nseindia.com/content/equities/sec_list.csv`
(send a browser-like User-Agent).

`trader/watchlist.json`, `trader/entry.json`, `trader/timemachine.json`:
same shape as before (times are IST, labels like "9:35 AM").

`trader/plan.json` (exit writes): same fields as before, plus
`"exchange": "NSE"`, `"product": "MIS"`, `"tick": 0.1`. `flat_by` default "15:10".

`trader/orders.json` (budget writes, risk reads): same envelope
(`created_by`, `created_at`, `status`, `paper`, `note`, `source`, `orders[]`).
Each order is shaped like Kite Connect's place_order, because Zerodha has NO
bracket orders: the entry and the two exits are separate orders.

```json
{
  "symbol": "ICICIBANK",
  "exchange": "NSE",
  "tradingsymbol": "ICICIBANK",
  "transaction_type": "BUY",
  "quantity": 53,
  "product": "MIS",
  "order_type": "LIMIT",
  "price": "1394.50",
  "variety": "regular",
  "validity": "DAY",
  "exits": {
    "stop_loss": {"transaction_type": "SELL", "order_type": "SL", "trigger_price": "1387.10", "price": "1385.70"},
    "target": {"transaction_type": "SELL", "order_type": "LIMIT", "price": "1401.90"}
  },
  "oco_note": "Zerodha has no bracket order for this: place the stop and the target after the entry fills, and cancel the other one the moment one fills.",
  "charges_estimate": {"at_stop": 73.32, "at_target": 73.9},
  "max_loss": 392.2,
  "max_loss_with_charges": 465.52,
  "why": "...",
  "plan": {"entry": 1394.5, "stop": 1387.1, "target": 1401.9, "risk_per_share": 7.4,
           "max_loss": 392.2, "setup": "orb", "entry_time": "9:35 AM", "flat_by": "15:10"}
}
```

(The numbers above illustrate the shape; compute the real ones.) Default
`order_type` is `LIMIT` at the plan's entry (Kite's API needs market
protection for MARKET orders; `--market` makes it `MARKET` with `price` null).
The stop leg is an `SL` order: trigger at the stop, limit price
`sl_limit_buffer_pct` (0.1 %) under it, rounded down to the tick, so it fills
in a fast market without being a market order.

`trader/config.json` defaults (budget's `config.example.json` holds them all;
each script reads only the keys it needs, with these defaults):

```json
{
  "budget": 100000,
  "cash_buffer_pct": 10,
  "max_position_pct": 75,
  "max_order_inr": 75000,
  "min_order_inr": 100,
  "max_loss_per_trade_inr": 500,
  "max_daily_loss_inr": 1500,
  "max_trades_per_day": 3,
  "max_orders_per_day": 10,
  "min_reward_risk": 1.0,
  "count_charges_in_max_loss": true,
  "require_stop": true,
  "no_entries_before": "09:30",
  "no_entries_after": "14:30",
  "flat_by": "15:10",
  "product": "MIS",
  "sl_limit_buffer_pct": 0.1,
  "broker": "zerodha",
  "paper": true,
  "blocked": [],
  "allow_margin": false,
  "allow_short": false,
  "allow_options": false,
  "holidays": [],
  "tick_size": {},
  "charges": {}
}
```

- Sizing (budget): whole shares inside every limit; with
  `count_charges_in_max_loss`, loss at the stop PLUS estimated round-trip
  charges must fit `max_loss_per_trade_inr`. Say which limit decided the size.
  `allow_margin: false` means no MIS leverage: order value <= cash after buffer.
- Risk adds India checks to the US list (drop the PDT check): series must be
  EQ for MIS (from universe.txt when present, else "unknown, check it"),
  `flat_by` must be before 15:20 for MIS (Zerodha's auto square-off), the
  circuit check (when a previous close and a numeric band are known: block an
  entry within 1 % of the upper circuit), charges counted in max loss.

## The reference example (the "reel" for India)

ICICI Bank (ICICIBANK), Friday October 30 2026, real Yahoo data in
`tests/fixtures/nse/nifty50-5m-60d.json.gz` (a bundle `{"ICICIBANK": <v8 chart>, ...}`
for all 50 Nifty 50 stocks, 5-minute bars Aug 5 to Oct 30 2026, 60 sessions).
Opening range 09:15-09:30: high 1390.90, low 1383.40. The 09:30 bar closed
1394.50 at 09:35 AM: the breakout. Midpoint 1387.15 -> stop 1387.10 (tick
0.10, rounded down); risk 7.40; 1R target 1401.90. The price reached the
target before the stop (around 11:50). On that day 35 of the 50 broke their
opening range at some point. Use this trade through every skill's example,
`--now "2026-10-30 09:35"` (or later) to pin the clock.

Other fixtures in `tests/fixtures/nse/`:
- `nifty50-daily-spark.json.gz`: Yahoo spark, 3 months of daily closes
  (only 41 of 50 symbols came back from spark: report partial honestly).
- `EQUITY_L.csv.gz`, `sec_list.csv.gz`, `nifty500.csv.gz`, `nifty50.csv`: NSE lists, Oct 30 2026.
