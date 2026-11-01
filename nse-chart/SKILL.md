---
name: nse-chart
description: >-
  Live Chart for the Nifty 50 index and its 50 stocks. Runs a page on this
  computer (http://127.0.0.1:8050) that refreshes itself during the NSE
  session: 5-minute candles, the 9:15 to 9:30 opening range, the nse-entry
  trigger and the nse-exit stop and target drawn on the chart, VWAP and EMA 9
  and 21, UT Bot Alerts (Buy and Sell labels on an ATR trailing stop), and a
  bias read (Bullish, Leaning, Neutral, Bearish) from five plain signals (the score in the watchlist; the full read and how often it was right on the last 60 sessions from `bias` and `backtest`). Free
  Yahoo bars, times in IST. Use when the user says "show me the chart", "live
  chart", "Nifty chart", "is the market bullish or bearish", "bias", "which
  stocks are bullish", "open the chart", "send alerts to my phone", "WhatsApp
  me the levels", "email me the entry, stop and target", "today's levels", or
  wants to watch the session. Phone alerts by free email, WhatsApp
  (CallMeBot) or ntfy, every trading day by Task Scheduler.
argument-hint: "[serve | bias SYMBOL | backtest SYMBOL]"
---

# nse-chart

The Live Chart puts the other skills on one screen. It draws today's 5-minute
chart of the Nifty 50 index or any of its stocks, marks the opening range, and
draws the entry, stop and target the way `/nse-entry` and `/nse-exit` would set
them. It also gives a **bias**: a count of five plain signals that says whether
the chart so far looks bullish or bearish.

A bias is a description of the chart so far, not a forecast. Nothing here can
say what a price will do next. `live.py backtest` gives the record of the read
on the last 60 sessions, so you can see how little it knows: on the Nifty
50 index, after a Bullish read the close 30 minutes later was higher 49 % of
the time, against 47 % for any moment. Most people who day trade lose money:
SEBI's study of FY2023 found that about 7 in 10 individual intraday traders in
equity cash lost money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
On Windows, `python` works too.

```bash
python3 "${CLAUDE_SKILL_DIR}/live.py"                      # serve the page, open the browser
python3 "${CLAUDE_SKILL_DIR}/live.py" serve --port 8060 --no-browser
python3 "${CLAUDE_SKILL_DIR}/live.py" serve --symbols "ICICIBANK,TCS,M&M"   # instead of the Nifty 50
python3 "${CLAUDE_SKILL_DIR}/live.py" bias NIFTY           # the read right now, in the terminal
python3 "${CLAUDE_SKILL_DIR}/live.py" bias ICICIBANK --json
python3 "${CLAUDE_SKILL_DIR}/live.py" backtest NIFTY       # how often the read was right, 60 sessions
python3 "${CLAUDE_SKILL_DIR}/live.py" levels               # today's buy-above, SL and target, all 50 + index
python3 "${CLAUDE_SKILL_DIR}/live.py" alert-test           # one test message to your phone
python3 "${CLAUDE_SKILL_DIR}/live.py" serve --alerts --until 15:45 --no-browser   # a whole day, with phone alerts
```

`--now "2026-10-30 10:00"` (IST) pins the clock and `--offline FILE` reads a
saved `{SYMBOL: Yahoo chart}` bundle instead of Yahoo, to replay a day:
`serve --offline tests/fixtures/nse/nifty50-5m-60d.json.gz --now "2026-10-30 10:00"`.
The saved bundle has the 50 stocks, not the index. Exit code 0 on success, 1
when there is no read or Yahoo did not answer (the reason is one plain line).

## What the page shows

| part | what |
| --- | --- |
| Toolbar | the symbol, last price and change, the timeframe (5m, 15m, 1H, 1D), Live (green dot) during 9:15 to 15:30 IST on NSE trading days (otherwise the last session, with the reason), the alerts switch and a light / dark theme switch. Under it, a market strip: how many stocks read Bullish, Neutral or Bearish, breadth above VWAP, breakouts |
| Watchlist | NIFTY 50 pinned on top, then the 50 stocks: bias score, last price, change on the previous close and the plan in one line. Search, and filters: Broke out, Bullish (+3 or more), Bearish (−3 or less) |
| Chart | candles on 5m, 15m, 1H or 1D, opening on the last few sessions; drag or scroll left for earlier days. Indicator switches over the chart: UT Bot and Day levels (the shaded opening range, the BUY and SELL lines, T1 to T3 each side, and where each side went in and out). Top left, like TradingView: open, high, low, close and the UT Bot stop at the candle under the mouse. EMA and VWAP are not drawn; the bias still counts them. `?sym=ICICIBANK&tf=15m` in the address opens that chart |
| UT Bot Alerts | TradingView's UT Bot: a dotted ATR trailing stop (green under price, red over it) and **Buy** / **Sell** labels where the close crosses it. On by default. Settings icon next to its switch: presets Sensitive (key 1, TradingView's default), Balanced (key 2), Calm (key 3, Heikin Ashi, the default here), or your own key, ATR period and Heikin Ashi switch |
| Bias | not on the page: the watchlist shows each stock's score (and filters Bullish / Bearish); `live.py bias SYMBOL` gives the full read and `live.py backtest SYMBOL` its 60-session record |
| Day levels | see **The day's levels** below: set at 9:30 AM every day and drawn from then on, with today's also on the price scale and in the legend top left (✓ on each target hit). Only the present trading day is drawn; earlier days stay clean candles. The watchlist line: `buy > 1,390.90 · sell < 1,383.40` until a side triggers, then `▲ 09:35 → T1 ✓` (▲ buy, ▼ sell) |

How far back each timeframe goes is Yahoo's free limit: 5m and 15m, the last
60 sessions (15m is built from the 5-minute bars, from 9:15); 1H, about a year;
1D, two years. Today's candle on every timeframe is built from today's
5-minute bars, so it moves with the live chart. The bias and the trade plan
always use today's 5-minute bars, whatever the timeframe shown.

The chart you look at refreshes every 30 seconds; the list every 5 minutes,
just after each bar closes (50 Yahoo requests, about 25 seconds). Only closed
5-minute bars count for the bias and the trade plan; the forming bar is drawn
but never used.

## The bias

Five votes, each +1, 0 or −1, on closed 5-minute bars:

| signal | +1 | −1 |
| --- | --- | --- |
| Opening range | closed above the 9:15–9:30 high | closed below the low |
| Trend | EMA 9 above EMA 21 (5-minute closes, previous sessions included) | below |
| Vs previous close | above the previous session's last 5-minute close | below |
| Momentum | RSI 14 (Wilder) over 55 | under 45 |
| VWAP (stocks) | above today's VWAP | below |
| Breadth (index) | 60 % or more of the Nifty 50 stocks above their VWAP | 40 % or less |

+3 or more reads **Bullish**, −3 or less **Bearish**, +1 or +2 **Leaning
bullish**, −1 or −2 **Leaning bearish**, 0 **Neutral**. The index has no
volume on Yahoo, so its fifth vote is breadth, and breadth needs the list to
have loaded once.

## UT Bot Alerts

The same maths as the TradingView script (`ut_bot` in `live.py`, `utBot` in
the page): a trailing stop Key value × ATR (Wilder, the ATR period) away from
the close. While the close stays above it the stop only rises; below it, it
only falls. **Buy** when the close crosses above the stop, **Sell** when it
crosses below. "Signals from Heikin Ashi candles" follows the Heikin Ashi
close, (open + high + low + close) / 4, instead; the ATR stays on the real bars,
as in the script.

TradingView's default, key 1, flips too often on 5-minute bars, so the page
starts on Calm. On the saved Nifty 50 bars (50 stocks, 60 sessions, ATR 10):

| setting | signals a day, a stock | flipped back within 30 minutes |
| --- | --- | --- |
| Sensitive: key 1 (TradingView default) | 9.2 | 55 % |
| Balanced: key 2 | 4.0 | 25 % |
| key 3 | 2.3 | 14 % |
| key 2, Heikin Ashi | 3.1 | 17 % |
| Calm: key 3, Heikin Ashi (default here) | 1.9 | 9 % |

On 15-minute candles, a quarter of key 2's signals flipped back within 6
candles, against 7 % on Calm (0.7 signals a day a stock). Waiting a candle to
confirm a signal cut that only to 20 %, and makes every signal later.

Fewer signals come later in the move: fewer whipsaws, not a better forecast.
On ICICI Bank on October 30 2026: 14 signals at key 1, 4 at key 2, 1 on Calm.

It runs on the timeframe shown (5m, 15m, 1H, 1D), across all the sessions
loaded, and on closed candles only, so a label never appears and then
vanishes. A longer timeframe also means fewer signals. A new signal on the
chart you look at shows a note on the page; with Alerts on, also a sound and a
browser notification. It is a chart signal, not a forecast, and not the
SELL level: the day's levels follow their own rule (below).

## The record (backtest)

`backtest SYMBOL` reads the last 60 sessions of
5-minute bars. At every closed bar from 9:45 AM to 2:30 PM it takes the read,
then checks the close 30 minutes later and at 15:10. It prints how often a
Bullish read was followed by a higher close, a Bearish read by a lower one, and
how often the price was higher 30 minutes later anyway, for comparison.

On the saved Nifty 50 bars to October 30 2026 (`tests/fixtures/nse/`):

```
python3 live.py backtest ICICIBANK --offline tests/fixtures/nse/nifty50-5m-60d.json.gz --now "2026-10-30 16:00"
Bias record, ICICIBANK, 5-minute bars (Yahoo):
  60 sessions, 3420 readings from 9:45 AM to 2:30 PM.
  Any time: the close 30 minutes later was higher 48 % of the time.
  Bullish (1028 readings on 45 days): higher 30 minutes later 51 %, higher at 15:10 58 %.
  Bearish (1409 readings on 49 days): lower 30 minutes later 52 %, lower at 15:10 56 %.
```

Near 50 % is a coin flip. The readings overlap (one every 5 minutes, the same
move counted many times), so they are not independent, and 60 sessions is a
short record. Say so every time a record is quoted.

## Example: ICICI Bank at 10:00 AM, Friday October 30 2026

```
python3 live.py bias ICICIBANK --offline tests/fixtures/nse/nifty50-5m-60d.json.gz --now "2026-10-30 10:00"
ICICIBANK  10:00 IST, session 2026-10-30, 5-minute bars (Yahoo, free, can lag)
BULLISH  (score +5 of 5) at the 10:00 close ₹1,394.30
  + Opening range          Above the 9:15–9:30 high ₹1,390.90
  + Trend (EMA 9 vs 21)    EMA 9 ₹1,391.72 above EMA 21 ₹1,388.90
  + Vs previous close      +0.56 % vs ₹1,386.60
  + Momentum (RSI 14)      68 (over 55)
  + VWAP                   Above VWAP ₹1,391.16
```

The page draws the same day's levels: BUY above ₹1,390.90 (triggered at
9:35 AM, the 9:30 bar closed at ₹1,394.50), SL ₹1,383.40, T1 ₹1,398.40 (hit at
11:10), T2 ₹1,405.90, T3 ₹1,413.40; out at 15:10 at ₹1,395.00. SELL below
₹1,383.40 never triggered.

## The day's levels

Set once the 9:15 to 9:30 opening range is in (9:30 AM), fixed for the day:

| line | buy side | sell side (intraday short) |
| --- | --- | --- |
| Entry | **BUY** when a 5-minute bar **closes above** the range high | **SELL** when a 5-minute bar **closes below** the range low |
| Stop-loss | the range low (the SELL line) | the range high (the BUY line) |
| T1, T2, T3 | 1, 2 and 3 range widths above the range high | 1, 2 and 3 range widths below the range low |

Entries until 2:30 PM; out by 15:10 either way. Both sides can trigger on one
day. A bar that touches the stop and a target counts as the stop. A range under
5 ticks wide gets no levels.

Why the stop is at the other side of the range and not its middle: on the
saved Nifty 50 bars (50 stocks, 60 sessions to October 30 2026), the range
edge was stopped before T1 on 28 % of buys against 43 % at the middle, and
lost less after charges (about −0.12R a buy against −0.19R; sells +0.01R
against −0.03R). Neither made money worth the name: T1 was reached on about a
quarter of trades, T3 on 2 %. Levels are a plan to check, not a forecast.

## Phone alerts (free)

`serve --alerts` sends to your phone, on top of the page:

| when | message |
| --- | --- |
| about 9:30 AM, once | the day's levels for the index and the 50 stocks: `BUY > ` entry, SL, T1, T2, T3 and `SELL < ` the same |
| each 5-minute bar after a trigger | `BUY ICICIBANK 09:35: above 1,390.90 SL 1,383.40 T1 1,398.40 T2 1,405.90 T3 1,413.40. Nothing placed.` |
| when a target, the SL or 15:10 is reached | `T1 hit BUY ICICIBANK 11:10 at 1,398.40.`, `SL hit ...`, `OUT at 15:10 ...` |
| after 3:30 PM, once | the day's summary: every breakout, its levels and how it ended |

`--alerts daily` sends only the morning levels and the summary (on October 30
2026, `--alerts all` sent 47 messages: 34 buys and 25 sells triggered). A restart
never sends the same alert twice (`trader/alerts-state.json`); every message
and whether it went out is kept in `trader/alerts-<date>.txt`. The page has
its own switch, "Alerts" in the toolbar: a sound and a browser notification
for each breakout and each new UT Bot signal, while the page is open.

Three free channels; set one or more as environment variables. The user types
these in their own terminal (`setx NAME "value"` on Windows, then a new
terminal), never in the chat:

| channel | variables | setup |
| --- | --- | --- |
| Email (Gmail) | `NSE_SMTP_USER` (your Gmail), `NSE_SMTP_PASSWORD` (a Gmail **app password**, not your password), `NSE_ALERT_EMAIL_TO` | Google account, 2-Step Verification on, then myaccount.google.com/apppasswords. Other mail: `NSE_SMTP_HOST`, `NSE_SMTP_PORT` (SSL) |
| WhatsApp (CallMeBot) | `NSE_WHATSAPP_PHONE` (+91...), `NSE_WHATSAPP_APIKEY` | free for personal use: follow callmebot.com's WhatsApp page (send their number the activation message, receive the key). Unofficial and sometimes slow; long lists arrive in parts |
| ntfy | `NSE_NTFY_TOPIC` (a long random name) | the free ntfy app; subscribe to the same topic. Anyone who guesses the topic can read it |

`live.py alert-test` sends one test message to every channel set and says
which worked. `live.py levels` prints the morning list now; `--send` also
sends it.

## Every trading day, by itself (Windows)

Task Scheduler starts the page with alerts at 9:05 AM, Monday to Friday, and
stops it at 15:45. On an NSE holiday it prints one line and exits. The
computer must be on and you logged in; set Windows not to sleep during market
hours.

```powershell
schtasks /Create /TN "NSE Live Chart" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:05 /F /TR "cmd /c cd /d C:\path\to\your\trading-folder && python C:\path\to\nse-chart\live.py serve --alerts --until 15:45 --no-browser >> trader\live.log 2>&1"
schtasks /Run /TN "NSE Live Chart"      # try it now
schtasks /Delete /TN "NSE Live Chart" /F # remove it
```

## Steps

1. Run `live.py` in the background (it serves until stopped) and give the
   user the address, http://127.0.0.1:8050. Keep the computer awake.
2. When the user asks "bullish or bearish?", run `live.py bias SYMBOL` and
   read back the label, the score and each vote, then the record from
   `live.py backtest SYMBOL`, with the base rate beside it.
3. For a trade, hand on as usual: `/nse-entry`, `/nse-exit`,
   `/nse-timemachine`, `/nse-budget`, `/nse-risk`, `/nse-paper`.

## Rules

- Say "the chart reads bullish", never "it will go up". A bias is not a
  prediction, a signal to buy, or a recommendation.
- Quote a record only with its base rate, its count and the overlap warning.
- A Bearish read is not a SELL: the SELL level needs its own 5-minute close
  under the range low. A sell is an intraday short (MIS): it must be bought back
  the same day. `/nse-budget`, `/nse-risk` and `/nse-paper` are still long only.
- The index cannot be bought in the cash market; its levels describe the
  market. Nifty futures, options and ETFs are outside this pack.
- Free Yahoo bars, unofficial and can lag. Say so when a minute matters.

## What it never does

It never places, sizes or checks an order, never calls a bias a forecast,
never listens beyond this computer (127.0.0.1 only; the copy in `cloudflare/`
serves the same page on the web, behind its own sign-in), never asks for or
prints an alert password or key (they live in environment variables), and never
sends an alert that reads like a buy instruction: every one says nothing was placed.

## Safety

- Education and tooling, not financial advice. A PASS from `/nse-risk` is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). `/nse-paper` records practice trades on real prices; nothing
  in this pack sends an order to Zerodha. Going live is the user's own decision, in their own config.
- A broker skill on Zerodha's Kite MCP may come later. Even then it will place an order only after
  `/nse-risk` passed that exact order today and the user typed an explicit yes for it in the chat, with
  the order tool set to "Needs approval", never "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys and Kite access tokens live in environment variables or the Claude Desktop config, never in the chat.
