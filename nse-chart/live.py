#!/usr/bin/env python3
"""Live Chart (nse-chart): a live 5-minute chart of the Nifty 50 and its stocks.

Serves one page on this computer (http://127.0.0.1:8050) that refreshes itself
during the session: candles, the opening range, the nse-entry trigger and the
nse-exit stop and target, plus a bias read (bullish, bearish or neutral) from
five plain signals, with how often that read was right on the last 60 sessions.

    python3 live.py                          # serve, open the browser
    python3 live.py serve --port 8060 --no-browser
    python3 live.py bias NIFTY               # the read right now, in the terminal
    python3 live.py backtest ICICIBANK       # how often the read was right, 60 sessions
    python3 live.py serve --offline tests/fixtures/nse/nifty50-5m-60d.json.gz --now "2026-10-30 10:00"
    python3 live.py levels                   # today's buy-above, stop and target for the Nifty 50
    python3 live.py alert-test               # send one test message to your phone
    python3 live.py serve --alerts --until 15:45 --no-browser   # the scheduled day: page + phone alerts

A bias is a description of the chart so far, not a forecast. Standard library
only, Python 3.8+. Free Yahoo 5-minute bars: unofficial, can lag.
"""

import argparse
import gzip
import json
import os
import smtplib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datetime
import sys
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

# ---------------------------------------------------------------- the India block
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")  # no daylight saving
OPEN = datetime.time(9, 15)    # normal market, after the 9:00-9:08 pre-open auction
CLOSE = datetime.time(15, 30)
NSE_HOLIDAYS = {
    datetime.date(2026, 1, 15),   # Municipal Corporation elections, Maharashtra
    datetime.date(2026, 1, 26),   # Republic Day
    datetime.date(2026, 3, 3),    # Holi
    datetime.date(2026, 3, 26),   # Shri Ram Navami
    datetime.date(2026, 3, 31),   # Shri Mahavir Jayanti
    datetime.date(2026, 4, 3),    # Good Friday
    datetime.date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    datetime.date(2026, 5, 1),    # Maharashtra Day
    datetime.date(2026, 5, 28),   # Bakri Id
    datetime.date(2026, 6, 26),   # Muharram
    datetime.date(2026, 9, 14),   # Ganesh Chaturthi
    datetime.date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    datetime.date(2026, 10, 20),  # Dussehra
    datetime.date(2026, 11, 10),  # Diwali-Balipratipada
    datetime.date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    datetime.date(2026, 12, 25),  # Christmas
}
CALENDAR_YEARS = (2026,)  # a year NSE has published; others: weekdays count as open, with a warning
# Muhurat trading (Sun Nov 8 2026, Diwali) is a special one-hour session: not supported.


def extra_holidays(config):
    """`holidays` in trader/config.json: ["2027-01-26", ...] for a year not built in."""
    out = set()
    for s in (config or {}).get("holidays", []) or []:
        out.add(datetime.date.fromisoformat(str(s)))
    return out


def trading_day(d, config=None):
    return d.weekday() < 5 and d not in NSE_HOLIDAYS and d not in extra_holidays(config)


def calendar_warning(d, config=None):
    """One plain line when the holiday list for d's year is not known, else None."""
    if d.year in CALENDAR_YEARS or (config or {}).get("holidays"):
        return None
    return (f"NSE's {d.year} holiday list is not built in: every weekday counts as open. "
            f"Add \"holidays\": [\"{d.year}-01-26\", ...] to trader/config.json.")


def to_ist(utc):
    """An aware or naive-UTC datetime -> naive IST."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=datetime.timezone.utc)
    return utc.astimezone(IST).replace(tzinfo=None)


def ist_to_utc(local):
    return local.replace(tzinfo=IST).astimezone(datetime.timezone.utc)


def ts_to_ist(ts):
    return to_ist(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc))


def to_yahoo(sym):
    """NSE symbol -> Yahoo: RELIANCE -> RELIANCE.NS. URL-quote it (M&M.NS) when building a URL."""
    sym = sym.strip().upper()
    return sym if sym.endswith(".NS") or sym.endswith(".BO") else sym + ".NS"


def from_yahoo(sym):
    return sym[:-3] if sym.upper().endswith((".NS", ".BO")) else sym


# Tick size by price (NSE, from April 15 2025). NSE sets each stock's tick from
# its price band at the periodic review, not tick by tick; config "tick_size"
# overrides for one stock: {"tick_size": {"MRF": 5}}.
TICKS = (
    (Decimal("250"), Decimal("0.01")),
    (Decimal("1000"), Decimal("0.05")),
    (Decimal("5000"), Decimal("0.10")),
    (Decimal("10000"), Decimal("0.50")),
    (Decimal("20000"), Decimal("1.00")),
)


def tick_size(price, symbol=None, config=None):
    over = ((config or {}).get("tick_size") or {}).get((symbol or "").upper())
    if over:
        return Decimal(str(over))
    p = Decimal(str(price))
    for limit, tick in TICKS:
        if p < limit:
            return tick
    return Decimal("5.00")


def to_tick(price, mode="nearest", symbol=None, config=None):
    """Round a price onto NSE's tick grid. mode: nearest | down | up. Returns Decimal."""
    p = Decimal(str(price))
    t = tick_size(p, symbol, config)
    rnd = {"nearest": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_UP}[mode]
    return ((p / t).to_integral_value(rounding=rnd) * t).quantize(Decimal("0.01"))


# Zerodha's equity charges (zerodha.com/charges, read 2026-10-30). Override any
# rate in trader/config.json under "charges". Rates are fractions, not percents.
CHARGES = {
    "brokerage_pct": 0.0003,        # intraday: 0.03 % or Rs 20 an executed order, whichever is lower
    "brokerage_cap": 20.0,          # delivery (CNC): zero brokerage
    "stt_intraday_sell": 0.00025,   # 0.025 % on the sell side
    "stt_delivery": 0.001,          # 0.1 % on buy and on sell
    "exchange_nse": 0.0000307,      # NSE transaction charge, 0.00307 %
    "sebi": 0.000001,               # Rs 10 a crore
    "gst": 0.18,                    # on brokerage + exchange + SEBI
    "stamp_intraday_buy": 0.00003,  # 0.003 % on the buy side
    "stamp_delivery_buy": 0.00015,  # 0.015 % on the buy side
    "dp_per_sell": 15.34,           # delivery sell only, per scrip per day
}


def charges(buy_value, sell_value, product="MIS", config=None):
    """Estimated charges for one round trip (one buy order, one sell order), in rupees.

    Returns {"brokerage", "stt", "exchange", "sebi", "gst", "stamp", "dp", "total"}
    rounded to the paisa. An estimate: the contract note is the real number.
    """
    c = dict(CHARGES)
    c.update(((config or {}).get("charges") or {}))
    b, s = float(buy_value), float(sell_value)
    if product.upper() == "CNC":
        brokerage = 0.0
        stt = (b + s) * c["stt_delivery"]
        stamp = b * c["stamp_delivery_buy"]
        dp = c["dp_per_sell"] if s > 0 else 0.0
    else:
        brokerage = sum(min(v * c["brokerage_pct"], c["brokerage_cap"]) for v in (b, s) if v > 0)
        stt = s * c["stt_intraday_sell"]
        stamp = b * c["stamp_intraday_buy"]
        dp = 0.0
    exchange = (b + s) * c["exchange_nse"]
    sebi = (b + s) * c["sebi"]
    gst = (brokerage + exchange + sebi) * c["gst"]
    out = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
           "gst": gst, "stamp": stamp, "dp": dp}
    out = {k: float(Decimal(str(v)).quantize(Decimal("0.01"), ROUND_HALF_UP)) for k, v in out.items()}
    out["total"] = round(sum(out.values()), 2)
    return out


def inr(x, decimals=2):
    """Rupees with Indian grouping: 123456.5 -> '₹1,23,456.50'."""
    q = Decimal(str(x)).quantize(Decimal(1).scaleb(-decimals), ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    whole, _, frac = f"{abs(q):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}" + (f".{frac}" if decimals else "")


def utf8_stdout():
    """Windows pipes default to cp1252, which cannot print ₹. Call first thing in main()."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
# ---------------------------------------------------------------- end of the India block

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NIFTY50_CSV = "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv"
INDEX = "NIFTY 50"
INDEX_YAHOO = "^NSEI"
PAUSE = 0.3          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds before the one retry after a 429
CHART_TTL = 30       # seconds a live chart answer is reused
BOARD_EVERY = 300    # seconds between full board refreshes during the session
BAR = datetime.timedelta(minutes=5)
OR_BARS = 3          # 9:15, 9:20, 9:25: the 15-minute opening range
FIRST_READ = datetime.time(9, 45)
LAST_READ = datetime.time(14, 30)
FLAT_BY = datetime.time(15, 10)
HORIZON = 6          # bars: the backtest checks the close 30 minutes later
HIST_TTL = 900       # seconds a history answer (earlier sessions) is reused
# timeframe -> (Yahoo interval, Yahoo range) for the earlier sessions; 5m and 15m come from the 5m bars
TIMEFRAMES = {"5m": ("5m", "60d"), "15m": ("5m", "60d"), "1h": ("1h", "1y"), "1d": ("1d", "2y")}
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")


class ChartError(Exception):
    pass


# ---------------------------------------------------------------- clock and data

class Clock:
    """Real IST time, or a pinned pretend time (--now) for replays."""

    def __init__(self, now=None):
        self.fixed = datetime.datetime.strptime(now, "%Y-%m-%d %H:%M") if now else None

    def now(self):
        return self.fixed or to_ist(datetime.datetime.now(datetime.timezone.utc))


def market_state(now, config=None):
    d = now.date()
    if not trading_day(d, config):
        return "closed", "NSE is closed today (weekend or holiday)."
    if now.time() < OPEN:
        return "pre", "Before the 9:15 AM open."
    if now.time() >= CLOSE:
        return "closed", "After the 3:30 PM close."
    return "open", "Market open."


def http_json(path):
    last = None
    for host in YAHOO_HOSTS:
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(host + path, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                raise ChartError("Yahoo answered %s for %s" % (e.code, path))
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
                break
    raise ChartError("Yahoo did not answer (%s): check the connection and try again" % last)


def yahoo_symbol(sym):
    return INDEX_YAHOO if sym.upper() in (INDEX, "NIFTY", "NIFTY50", INDEX_YAHOO) else to_yahoo(sym)


def nifty50_symbols():
    """The Nifty 50 list from niftyindices.com, else trader/universe.txt when it is that short."""
    try:
        req = urllib.request.Request(NIFTY50_CSV, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = r.read().decode("utf-8", "replace").splitlines()
        syms = [ln.split(",")[2].strip() for ln in rows[1:] if ln.count(",") >= 3]
        if len(syms) >= 40:
            return syms
    except (urllib.error.URLError, OSError, IndexError):
        pass
    path = os.path.join("trader", "universe.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            syms = [ln.split("|")[0] for ln in fh if ln.strip()]
        if 0 < len(syms) <= 100:
            return syms
    raise ChartError("Could not read the Nifty 50 list from niftyindices.com. "
                     "Run /nse-scanner universe --index nifty50 first, or pass --symbols.")


def parse_chart(chart, interval="5m"):
    """A Yahoo v8 chart -> (bars, meta). Bars: [start (naive IST), o, h, l, c, v] on the 5-minute grid.
    Daily bars start at 9:15 on their date, whatever time Yahoo stamps them with."""
    try:
        res = chart["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise ChartError("Yahoo sent no chart")
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        start = ts_to_ist(ts)
        if interval == "1d":
            start = datetime.datetime.combine(start.date(), OPEN)
        elif ts % 300 or start.time() >= CLOSE:
            continue                               # the live tick, or the 15:30 closing print: not a bar
        vals = [(q.get(k) or [None] * (i + 1))[i] for k in ("open", "high", "low", "close", "volume")]
        if None in vals[:4]:
            continue
        if bars and bars[-1][0] == start:
            bars.pop()                             # Yahoo repeats the live day: keep the later one
        bars.append([start] + [round(float(x), 2) for x in vals[:4]] + [int(vals[4] or 0)])
    return bars, res.get("meta", {})


class Source:
    """Yahoo live, or a saved {SYMBOL: chart} bundle (--offline) cut at the pretend clock."""

    def __init__(self, clock, offline=None):
        self.clock = clock
        self.bundle = None
        self.parsed = {}
        if offline:
            opener = gzip.open if offline.endswith(".gz") else open
            with opener(offline, "rt", encoding="utf-8") as fh:
                self.bundle = json.load(fh)

    def symbols(self):
        if self.bundle is not None:
            return sorted(k for k in self.bundle if k != INDEX)
        return nifty50_symbols()

    def chart(self, sym, rng="5d", interval="5m"):
        if self.bundle is not None:
            key = INDEX if yahoo_symbol(sym) == INDEX_YAHOO else from_yahoo(sym).upper()
            if key not in self.bundle:
                raise ChartError("%s is not in the saved file" % key)
            if key not in self.parsed:     # a replay asks for the same chart every 5 minutes: parse it once
                self.parsed[key] = parse_chart(self.bundle[key])
            bars, meta = self.parsed[key]
        else:
            path = "/v8/finance/chart/%s?range=%s&interval=%s" % (urllib.parse.quote(yahoo_symbol(sym)), rng, interval)
            bars, meta = parse_chart(http_json(path), interval)
            time.sleep(PAUSE)
        now = self.clock.now()
        return [b for b in bars if b[0] < now], meta


# ---------------------------------------------------------------- indicators

def ema(values, n):
    out, k, e = [], 2.0 / (n + 1), None
    for v in values:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def rsi(values, n=14):
    """Wilder's RSI; None until n changes are in."""
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(values)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def heikin_ashi_closes(bars):
    """Heikin Ashi close of each bar: (open + high + low + close) / 4."""
    return [(b[1] + b[2] + b[3] + b[4]) / 4 for b in bars]


def ut_bot(bars, key=2.0, period=10, heikin=False):
    """UT Bot Alerts (the TradingView script): an ATR trailing stop that flips sides with the close.
    key: the sensitivity (stop = key × ATR from the close); period: ATR length (Wilder, like ta.atr).
    heikin: follow the Heikin Ashi close instead of the close (the script's option; the ATR stays on
    the real bars). TradingView's default is key 1; 2 halves the signals on 5-minute bars.
    Returns (stops, signals): stops[i] is None until the ATR is in; signals is [(i, "buy"|"sell")]."""
    src = heikin_ashi_closes(bars) if heikin else [b[4] for b in bars]
    stops, signals, atr, prev = [None] * len(bars), [], None, None
    for i, b in enumerate(bars):
        tr = b[2] - b[3] if i == 0 else max(b[2] - b[3], abs(b[2] - bars[i - 1][4]), abs(b[3] - bars[i - 1][4]))
        if i < period - 1:
            atr = (atr or 0) + tr
            continue
        atr = (atr + tr) / period if i == period - 1 else (atr * (period - 1) + tr) / period
        c, loss, ps = src[i], key * atr, prev or 0.0
        pc = src[i - 1] if i else c
        if c > ps and pc > ps:
            s = max(ps, c - loss)
        elif c < ps and pc < ps:
            s = min(ps, c + loss)
        else:
            s = c - loss if c > ps else c + loss
        if prev is not None:
            if c > s and pc <= prev:
                signals.append((i, "buy"))
            elif c < s and pc >= prev:
                signals.append((i, "sell"))
        stops[i] = prev = s
    return stops, signals


def closed_bars(bars, now):
    return [b for b in bars if b[0] + BAR <= now]


class Series:
    """Indicators over closed bars of several sessions, computed once."""

    def __init__(self, bars):
        self.bars = bars
        closes = [b[4] for b in bars]
        self.ema9, self.ema21, self.rsi = ema(closes, 9), ema(closes, 21), rsi(closes)
        self.day_start, self.prev_close, self.or_hl, self.vwap = [], [], [], []
        start, prev, last_close, rng, pv, vol = 0, None, None, None, 0.0, 0
        for i, b in enumerate(bars):
            if i == 0 or b[0].date() != bars[i - 1][0].date():
                start, prev, rng, pv, vol = i, last_close, None, 0.0, 0
            if rng is None and i - start + 1 == OR_BARS and bars[start][0].time() == OPEN:
                part = bars[start:i + 1]
                rng = (max(x[2] for x in part), min(x[3] for x in part))
            pv += (b[2] + b[3] + b[4]) / 3 * b[5]
            vol += b[5]
            self.day_start.append(start)
            self.prev_close.append(prev)
            self.or_hl.append(rng)
            self.vwap.append(pv / vol if vol else None)
            last_close = b[4]


def label(score):
    if score >= 3:
        return "Bullish"
    if score <= -3:
        return "Bearish"
    if score > 0:
        return "Leaning bullish"
    if score < 0:
        return "Leaning bearish"
    return "Neutral"


def read_at(s, i, breadth=None):
    """The five signals at closed bar i -> {score, label, signals[]}. breadth: share of stocks above VWAP."""
    b, c = s.bars[i], s.bars[i][4]
    sig = []

    def add(name, v, detail):
        sig.append({"name": name, "vote": v, "detail": detail})

    rng = s.or_hl[i]
    if rng is None:
        add("Opening range", 0, "Still forming (9:15 to 9:30)")
    elif c > rng[0]:
        add("Opening range", 1, "Above the 9:15–9:30 high %s" % inr(rng[0]))
    elif c < rng[1]:
        add("Opening range", -1, "Below the 9:15–9:30 low %s" % inr(rng[1]))
    else:
        add("Opening range", 0, "Inside the range %s to %s" % (inr(rng[1]), inr(rng[0])))
    e9, e21 = s.ema9[i], s.ema21[i]
    add("Trend (EMA 9 vs 21)", 1 if e9 > e21 else -1 if e9 < e21 else 0,
        "EMA 9 %s %s EMA 21 %s" % (inr(e9), "above" if e9 >= e21 else "below", inr(e21)))
    pc = s.prev_close[i]
    if pc:
        ch = (c - pc) / pc * 100
        add("Vs previous close", 1 if ch > 0 else -1 if ch < 0 else 0, "%+.2f %% vs %s" % (ch, inr(pc)))
    else:
        add("Vs previous close", 0, "No previous session in the data")
    r = s.rsi[i]
    if r is None:
        add("Momentum (RSI 14)", 0, "Not enough bars yet")
    else:
        add("Momentum (RSI 14)", 1 if r > 55 else -1 if r < 45 else 0,
            "%.0f (%s)" % (r, "over 55" if r > 55 else "under 45" if r < 45 else "45 to 55"))
    if s.vwap[i] is not None:
        v = s.vwap[i]
        add("VWAP", 1 if c > v else -1 if c < v else 0,
            "%s VWAP %s" % ("Above" if c >= v else "Below", inr(v)))
    elif breadth is not None:
        pct = breadth * 100
        add("Breadth", 1 if breadth >= 0.6 else -1 if breadth <= 0.4 else 0,
            "%.0f %% of Nifty 50 stocks above their VWAP" % pct)
    score = sum(x["vote"] for x in sig)
    return {"score": score, "max": len(sig), "label": label(score), "signals": sig,
            "at": (b[0] + BAR).strftime("%H:%M"), "close": c}


def backtest(bars):
    """Every closed bar 9:45-14:30: when the read was Bullish or Bearish, was the close higher or lower
    30 minutes later, and at 15:10? Compared with how often it was higher anyway."""
    s = Series(bars)
    out = {"sessions": len({b[0].date() for b in bars}), "bars": 0, "base_up": 0,
           "bull": {"calls": 0, "right30": 0, "right_flat": 0, "days": set()},
           "bear": {"calls": 0, "right30": 0, "right_flat": 0, "days": set()}}
    flat_close = {}
    for b in bars:
        if b[0].time() < FLAT_BY:
            flat_close[b[0].date()] = b[4]
    for i, b in enumerate(bars):
        end = b[0] + BAR
        if not (FIRST_READ <= end.time() <= LAST_READ) or s.prev_close[i] is None:
            continue
        j = i + HORIZON
        if j >= len(bars) or bars[j][0].date() != b[0].date():
            continue
        later, c = bars[j][4], b[4]
        out["bars"] += 1
        out["base_up"] += later > c
        r = read_at(s, i)
        side = "bull" if r["score"] >= 3 else "bear" if r["score"] <= -3 else None
        if not side:
            continue
        k, up = out[side], side == "bull"
        k["calls"] += 1
        k["days"].add(b[0].date())
        k["right30"] += (later > c) if up else (later < c)
        fc = flat_close.get(b[0].date())
        if fc is not None:
            k["right_flat"] += (fc > c) if up else (fc < c)
    for side in ("bull", "bear"):
        out[side]["days"] = len(out[side]["days"])
    return out


def describe_backtest(bt):
    def pct(a, n):
        return "%.0f %%" % (100.0 * a / n) if n else "–"
    lines = ["%d sessions, %d readings from 9:45 AM to 2:30 PM." % (bt["sessions"], bt["bars"]),
             "Any time: the close 30 minutes later was higher %s of the time." % pct(bt["base_up"], bt["bars"])]
    for side, word, dir_ in (("bull", "Bullish", "higher"), ("bear", "Bearish", "lower")):
        k = bt[side]
        lines.append("%s (%d readings on %d days): %s 30 minutes later %s, %s at 15:10 %s."
                     % (word, k["calls"], k["days"], dir_, pct(k["right30"], k["calls"]),
                        dir_, pct(k["right_flat"], k["calls"])))
    return lines


def resample(bars, tf):
    """5-minute bars -> 15m, 1h (buckets from 9:15, like NSE's session) or 1d candles."""
    if tf == "5m":
        return [list(b) for b in bars]
    size = {"15m": 15, "1h": 60}.get(tf)
    out = []
    for b in bars:
        day = datetime.datetime.combine(b[0].date(), OPEN)
        if size:
            m = int((b[0] - day).total_seconds() // 60)
            key = day + datetime.timedelta(minutes=m // size * size)
        else:
            key = day
        if out and out[-1][0] == key:
            c = out[-1]
            c[2], c[3], c[4], c[5] = max(c[2], b[2]), min(c[3], b[3]), b[4], c[5] + b[5]
        else:
            out.append([key, b[1], b[2], b[3], b[4], b[5]])
    return out


def utc_ts(d):
    return int(ist_to_utc(d).timestamp())


# ---------------------------------------------------------------- the trade plan (nse-entry, nse-exit, nse-paper)

OR_END = datetime.time(9, 30)
NO_ENTRIES_AFTER = datetime.time(14, 30)
INDEX_TICK = {"tick_size": {INDEX: 0.05}}    # the index moves in 5 paise steps, whatever its level


def snap(price, tick, mode):
    """Round onto one tick grid (one tick for the whole plan, from the entry price)."""
    p, t = Decimal(str(price)), Decimal(str(tick))
    rnd = {"nearest": ROUND_HALF_UP, "down": ROUND_DOWN}[mode]
    return float(((p / t).to_integral_value(rounding=rnd) * t).quantize(Decimal("0.01")))


def orb_plan(sym, today, now):
    """The day's levels on closed bars, both sides, fixed once the 9:15-9:30 opening range is in:
    BUY on a 5-minute CLOSE above the range high, SL the range low; SELL on a CLOSE below the range low,
    SL the range high; T1, T2, T3 at 1, 2 and 3 range widths past the entry. Entries until 2:30 PM, out
    by 15:10. The range edge beat the range middle as the stop on the saved Nifty 50 bars (60 sessions):
    fewer stops before T1 (28 % against 43 % on the buy side) and less lost after charges.
    Each side: status waiting | triggered | none, and once triggered which targets were hit and when,
    until the SL, T3 or 15:10 (a bar that touches the SL and a target counts as the SL)."""
    cfg = INDEX_TICK if sym == INDEX else None
    bars = [b for b in today if b[0] + BAR <= now]
    rng = [b for b in bars if OPEN <= b[0].time() < OR_END]
    if len(rng) < OR_BARS:
        return {"status": "forming"}
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    tick = float(tick_size(hi, sym, cfg))
    width = round(hi - lo, 2)
    out = {"status": "ready", "or_high": hi, "or_low": lo, "width": width, "tick": tick}
    if width < 5 * tick - 1e-9:
        out.update(status="refused", why="opening range under 5 ticks wide: no levels today")
        return out
    late = bars[-1][0] + BAR >= datetime.datetime.combine(bars[-1][0].date(), NO_ENTRIES_AFTER)
    for side, entry, stop, k in (("buy", hi, lo, 1), ("sell", lo, hi, -1)):
        s = {"entry": entry, "stop": stop, "risk": width,
             "targets": [snap(round(entry + k * n * width, 2), tick, "nearest") for n in (1, 2, 3)],
             "status": "none" if late else "waiting", "hits": [], "exit": None}
        out[side] = s
        trig = None
        for i, b in enumerate(bars):
            if b[0].time() < OR_END:
                continue
            if (b[0] + BAR).time() > NO_ENTRIES_AFTER:
                break
            if (b[4] > hi) if k > 0 else (b[4] < lo):
                trig = i
                break
        if trig is None:
            continue
        s.update(status="triggered", entry_time=(bars[trig][0] + BAR).strftime("%H:%M"), fill=bars[trig][4])
        for b in bars[trig + 1:]:
            end = (b[0] + BAR).strftime("%H:%M")
            if b[0].time() >= FLAT_BY:
                s["exit"] = {"how": "flat", "price": b[1], "time": b[0].strftime("%H:%M")}
                break
            if (b[3] <= stop) if k > 0 else (b[2] >= stop):
                s["exit"] = {"how": "stop", "price": min(stop, b[1]) if k > 0 else max(stop, b[1]), "time": end}
                break
            while len(s["hits"]) < 3 and ((b[2] >= s["targets"][len(s["hits"])]) if k > 0
                                          else (b[3] <= s["targets"][len(s["hits"])])):
                s["hits"].append(end)
            if len(s["hits"]) == 3:
                s["exit"] = {"how": "t3", "price": s["targets"][2], "time": end}
                break
    return out


# ---------------------------------------------------------------- phone alerts (free: email, WhatsApp, ntfy)

ALERT_ENV = {
    "email": ("NSE_SMTP_USER", "NSE_SMTP_PASSWORD", "NSE_ALERT_EMAIL_TO"),
    "whatsapp": ("NSE_WHATSAPP_PHONE", "NSE_WHATSAPP_APIKEY"),
    "ntfy": ("NSE_NTFY_TOPIC",),
}
CHUNK = 1500         # characters per WhatsApp or ntfy message


def chunks(text, size=CHUNK):
    out, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


class Notifier:
    """Sends to every channel whose environment variables are set. Secrets stay in the environment:
    they are never printed, logged or written to a file."""

    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        self.channels = [c for c, keys in ALERT_ENV.items() if all(self.env.get(k) for k in keys)]

    def send(self, subject, text):
        results = []
        for c in self.channels:
            try:
                getattr(self, "_" + c)(subject, text)
                results.append((c, True, ""))
            except Exception as e:   # one channel failing must not stop the others
                results.append((c, False, "%s: %s" % (type(e).__name__, str(e).split("?")[0][:120])))
        return results

    def _email(self, subject, text):
        e = self.env
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, e["NSE_SMTP_USER"], e["NSE_ALERT_EMAIL_TO"]
        msg.set_content(text)
        host, port = e.get("NSE_SMTP_HOST", "smtp.gmail.com"), int(e.get("NSE_SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(e["NSE_SMTP_USER"], e["NSE_SMTP_PASSWORD"])
            s.send_message(msg)

    def _whatsapp(self, subject, text):
        for i, part in enumerate(chunks(subject + "\n" + text)):
            if i:
                time.sleep(3)        # CallMeBot is free and slow: space the parts out
            q = urllib.parse.urlencode({"phone": self.env["NSE_WHATSAPP_PHONE"], "text": part,
                                        "apikey": self.env["NSE_WHATSAPP_APIKEY"]})
            with urllib.request.urlopen("https://api.callmebot.com/whatsapp.php?" + q, timeout=30) as r:
                body = r.read().decode("utf-8", "replace").lower()
            if "error" in body or "not" in body and "sent" not in body:
                raise ChartError("CallMeBot did not send it: check the phone number and API key")

    def _ntfy(self, subject, text):
        for part in chunks(text):
            req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(self.env["NSE_NTFY_TOPIC"]),
                                         data=part.encode("utf-8"), headers={"Title": subject})
            urllib.request.urlopen(req, timeout=30).close()


def money(x):
    return inr(x)[1:]              # 1,394.50: the rupee sign is noise in a list of 50


def side_levels(s):
    return "%s SL %s T1 %s T2 %s T3 %s" % (money(s["entry"]), money(s["stop"]), *(money(t) for t in s["targets"]))


def level_lines(plans):
    """Two lines a symbol (BUY, SELL): the index first, then the stocks, alphabetical."""
    lines = []
    for sym in sorted(plans, key=lambda s: (s != INDEX, s)):
        p = plans[sym]
        if p["status"] == "forming":
            lines.append("%-11s range still forming" % sym)
        elif p["status"] == "refused":
            lines.append("%-11s no levels: %s" % (sym, p["why"]))
        else:
            for side, word in (("buy", "BUY  >"), ("sell", "SELL <")):
                s = p[side]
                tag = "  (IN %s)" % s["entry_time"] if s["status"] == "triggered" else ""
                lines.append("%-11s %s %s%s" % (sym if side == "buy" else "", word, side_levels(s), tag))
    return lines


def morning_text(day, plans):
    return "\n".join([
        "NSE opening-range levels, %s (IST). Paper first. Not financial advice." % day.strftime("%a %d %b %Y"),
        "BUY only if a 5-minute bar CLOSES above the range high, SELL (intraday short) only if one closes below the low, by 2:30 PM.",
        "SL = the other side of the range. T1, T2, T3 = 1, 2, 3 range widths. Out by 15:10.",
        "IN = already triggered (time).",
        ""] + level_lines(plans))


def side_events(sym, side, s, sent):
    """New events for one side, in order: the trigger, each target, the exit. Marks them in `sent`."""
    word, out = side.upper(), []
    if s.get("status") != "triggered":
        return out
    key = "%s:%s" % (side, sym)
    if key not in sent:
        sent.add(key)
        out.append("%s %s %s: %s %s. Nothing placed." % (word, sym, s["entry_time"],
                                                         "above" if side == "buy" else "below", side_levels(s)))
    for n, t in enumerate(s["hits"], 1):
        if "%s:T%d" % (key, n) not in sent:
            sent.add("%s:T%d" % (key, n))
            out.append("T%d hit %s %s %s at %s." % (n, word, sym, t, money(s["targets"][n - 1])))
    x = s["exit"]
    if x and x["how"] != "t3" and key + ":exit" not in sent:
        sent.add(key + ":exit")
        out.append("%s %s %s %s at %s (entry %s, %d of 3 targets)." % (
            "SL hit" if x["how"] == "stop" else "OUT at 15:10", word, sym, x["time"], money(x["price"]),
            money(s["entry"]), len(s["hits"])))
    return out


def summary_text(day, plans):
    lines, counts = [], []
    for side in ("buy", "sell"):
        trig = [p[side] for p in plans.values() if p.get(side, {}).get("status") == "triggered"]
        n = lambda f: sum(1 for s in trig if f(s))  # noqa: E731
        counts.append("%s %d: T1 %d, T2 %d, T3 %d, SL before T1 %d" % (
            side.upper(), len(trig), n(lambda s: len(s["hits"]) >= 1), n(lambda s: len(s["hits"]) >= 2),
            n(lambda s: len(s["hits"]) == 3), n(lambda s: not s["hits"] and (s["exit"] or {}).get("how") == "stop")))
    lines.append("NSE day summary, %s, %d symbols. %s." % (day.strftime("%a %d %b %Y"), len(plans), "; ".join(counts)))
    lines += ["Before charges, on the opening-range levels. One day is one data point.", ""]
    for sym in sorted(plans, key=lambda s: (s != INDEX, s)):
        for side in ("buy", "sell"):
            s = plans[sym].get(side)
            if not s or s["status"] != "triggered":
                continue
            x = s["exit"] or {"how": "open", "price": s["entry"], "time": "-"}
            how = {"t3": "T3", "stop": "SL", "flat": "out 15:10", "open": "open"}[x["how"]]
            lines.append("%-11s %-4s in %s  %s  hit %d/3 -> %s %s" % (sym, side.upper(), s["entry_time"], side_levels(s),
                                                                     len(s["hits"]), how, money(x["price"])))
    return "\n".join(lines)


class Alerter:
    """After each list refresh: the morning levels once, each new breakout and exit, the summary after 15:30.
    Remembers what it sent in trader/alerts-state.json, so a restart never sends the same alert twice."""

    def __init__(self, store, notifier, folder="trader", echo=print, mode="all"):
        self.store, self.notifier, self.folder, self.echo = store, notifier, folder, echo
        self.mode = mode           # all: levels, each breakout and exit, summary; daily: levels and summary

    def _state(self, day):
        try:
            with open(os.path.join(self.folder, "alerts-state.json"), encoding="utf-8") as fh:
                s = json.load(fh)
            if s.get("date") == day.isoformat():
                return s
        except (OSError, ValueError):
            pass
        return {"date": day.isoformat(), "sent": []}

    def _save(self, state, subject, text, results):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "alerts-state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        stamp = self.store.clock.now().strftime("%H:%M:%S")
        sent = ", ".join("%s %s" % (c, "ok" if ok else "FAILED (%s)" % err) for c, ok, err in results) or "no phone channel set"
        with open(os.path.join(self.folder, "alerts-%s.txt" % state["date"]), "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n%s\n-> %s\n\n" % (stamp, subject, text, sent))
        self.echo("[%s] %s -> %s" % (stamp, subject, sent))

    def plans(self):
        now = self.store.clock.now()
        with self.store.lock:
            today = dict(self.store.today)
        return {s: orb_plan(s, bars, now) for s, bars in today.items() if bars and bars[0][0].date() == now.date()}

    def tick(self):
        now = self.store.clock.now()
        if not trading_day(now.date()) or now.time() < OR_END:
            return None
        try:
            self.store.chart(INDEX)
        except ChartError:
            pass
        plans = self.plans()
        if not plans:
            return None
        state = self._state(now.date())
        sent = set(state["sent"])
        parts, subject = [], None
        if now.time() >= CLOSE:
            if "summary" not in sent:
                subject, parts = "NSE summary %s" % now.strftime("%d %b"), [summary_text(now.date(), plans)]
                sent.add("summary")
        else:
            ready = sum(1 for p in plans.values() if p["status"] != "forming")
            if "morning" not in sent and (ready >= 0.8 * len(plans) or now.time() >= datetime.time(9, 40)):
                subject, parts = "NSE levels %s" % now.strftime("%d %b"), [morning_text(now.date(), plans)]
                sent.add("morning")
                sent.update("%s:%s" % (side, s) for s, p in plans.items() for side in ("buy", "sell")
                            if p.get(side, {}).get("status") == "triggered")   # the list already says IN
            events = []
            for s in sorted(plans, key=lambda s: (s != INDEX, s)) if self.mode == "all" else []:
                for side in ("buy", "sell"):
                    if side in plans[s]:
                        events += side_events(s, side, plans[s][side], sent)
            if events:
                parts.append("\n".join(events))
                subject = subject or "NSE alert %s: %d new" % (now.strftime("%H:%M"), len(events))
        if not parts:
            return None
        text = "\n\n".join(parts)
        results = self.notifier.send(subject, text)
        state["sent"] = sorted(sent)
        self._save(state, subject, text, results)
        return subject, text, results


# ---------------------------------------------------------------- the store

class Store:
    def __init__(self, source, clock, symbols=None):
        self.source, self.clock = source, clock
        self.lock = threading.Lock()
        self.charts = {}      # sym -> (fetched_at, payload)
        self.today = {}       # sym -> the session's 5-minute bars (naive IST)
        self.hist = {}        # (sym, timeframe) -> (fetched_at, candles)
        self.tests = {}       # sym -> (date, payload)
        self.board = {"stocks": [], "breadth": None, "updated": None, "error": None}
        self.symbols = symbols

    def _payload(self, sym, bars, meta, breadth=None):
        now = self.clock.now()
        done = closed_bars(bars, now)
        if not bars:
            raise ChartError("No bars for %s" % sym)
        day = bars[-1][0].date()
        today = [b for b in bars if b[0].date() == day]
        prev = [b for b in bars if b[0].date() < day]
        read = None
        if done and done[-1][0].date() == day:
            read = read_at(Series(done), len(done) - 1, breadth)
        with self.lock:
            self.today[sym] = today
        utc = utc_ts
        return {"symbol": sym, "name": INDEX if sym == INDEX else (meta.get("longName") or meta.get("shortName") or sym),
                "is_index": sym == INDEX, "session": day.isoformat(),
                "prev_close": prev[-1][4] if prev else meta.get("chartPreviousClose"),
                "bars": [[utc(b[0])] + b[1:] for b in today],
                "closed": sum(1 for b in today if b[0] + BAR <= now),
                "bias": read}

    def chart(self, sym, tf=None):
        """Today's 5-minute payload (bias, trade plan); with tf, plus `candles`: earlier sessions and today."""
        sym = INDEX if yahoo_symbol(sym) == INDEX_YAHOO else from_yahoo(sym).upper()
        with self.lock:
            hit = self.charts.get(sym)
        live = market_state(self.clock.now())[0] == "open" and self.source.bundle is None
        if hit and (not live or time.time() - hit[0] < CHART_TTL):
            p = hit[1]
        else:
            bars, meta = self.source.chart(sym)
            with self.lock:
                breadth = self.board["breadth"]
            p = self._payload(sym, bars, meta, breadth if sym == INDEX else None)
            with self.lock:
                self.charts[sym] = (time.time(), p)
        if not tf:
            return p
        if tf not in TIMEFRAMES:
            raise ChartError("Timeframe %s: use 5m, 15m, 1h or 1d" % tf)
        session = datetime.date.fromisoformat(p["session"])
        with self.lock:
            today = self.today.get(sym, [])
        earlier = [c for c in self.history(sym, tf) if c[0].date() < session]
        out = dict(p)
        out["tf"] = tf
        out["candles"] = [[utc_ts(c[0])] + c[1:] for c in earlier + resample(today, tf)]
        return out

    def history(self, sym, tf):
        """Earlier sessions' candles for one timeframe, fetched at most every HIST_TTL seconds."""
        with self.lock:
            hit = self.hist.get((sym, tf))
        if hit and time.time() - hit[0] < HIST_TTL:
            return hit[1]
        interval, rng = TIMEFRAMES[tf]
        if interval == "5m" or self.source.bundle is not None:
            bars, _ = self.source.chart(sym, "60d")
            candles = resample(bars, tf)
        else:
            candles, _ = self.source.chart(sym, rng, interval)
        with self.lock:
            self.hist[(sym, tf)] = (time.time(), candles)
        return candles

    def refresh_board(self):
        syms = self.symbols or self.source.symbols()
        rows, above, counted, failed = [], 0, 0, []
        for sym in syms:
            try:
                bars, meta = self.source.chart(sym)
                p = self._payload(sym, bars, meta)
            except ChartError:
                failed.append(sym)
                continue
            with self.lock:
                self.charts[sym] = (time.time(), p)
            b = p["bias"]
            if b:
                v = next((x for x in b["signals"] if x["name"] == "VWAP"), None)
                if v and v["vote"]:
                    counted += 1
                    above += v["vote"] > 0
            last = p["bars"][-1][4] if p["bars"] else None
            with self.lock:
                today = self.today.get(sym, [])
            plan = orb_plan(sym, today, self.clock.now()) if today and today[0][0].date() == self.clock.now().date() else None
            rows.append({"symbol": sym, "last": last, "prev_close": p["prev_close"],
                         "score": b["score"] if b else None, "label": b["label"] if b else None, "plan": plan})
        with self.lock:
            self.board = {"stocks": rows, "breadth": (above / counted) if counted else None,
                          "updated": self.clock.now().strftime("%H:%M:%S"), "failed": failed, "error": None}
            self.charts.pop(INDEX, None)   # the index read uses the new breadth

    def backtest(self, sym):
        sym = INDEX if yahoo_symbol(sym) == INDEX_YAHOO else from_yahoo(sym).upper()
        today = self.clock.now().date()
        with self.lock:
            hit = self.tests.get(sym)
        if hit and hit[0] == today:
            return hit[1]
        bars, _ = self.source.chart(sym, "60d")
        bt = backtest(closed_bars(bars, self.clock.now()))
        bt["lines"] = describe_backtest(bt)
        with self.lock:
            self.tests[sym] = (today, bt)
        return bt

    def status(self):
        state, words = market_state(self.clock.now())
        try:
            idx = self.chart(INDEX)          # cached CHART_TTL seconds: the watchlist's NIFTY 50 row and its plan
        except ChartError:
            idx = None
        with self.lock:
            board = dict(self.board)
            today = self.today.get(INDEX, [])
        board["index_plan"] = orb_plan(INDEX, today, self.clock.now()) if today and today[0][0].date() == self.clock.now().date() else None
        board["index_last"] = idx["bars"][-1][4] if idx and idx["bars"] else None
        board["index_prev_close"] = idx["prev_close"] if idx else None
        return {"now": self.clock.now().strftime("%Y-%m-%d %H:%M:%S"), "market": state, "words": words,
                "offline": self.source.bundle is not None, "board": board}


def board_loop(store, stop, alerter=None):
    while not stop.is_set():
        try:
            store.refresh_board()
        except ChartError as e:
            with store.lock:
                store.board["error"] = str(e)
        if alerter:
            try:
                alerter.tick()
            except Exception as e:     # an alert problem must never stop the chart
                print("Alert failed: %s" % e)
        if store.source.bundle is not None:
            return
        now = store.clock.now()
        if market_state(now)[0] == "open":
            # just after the next 5-minute bar closes, so the read moves with the chart
            nxt = (now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=5 - now.minute % 5))
            wait = max(20.0, (nxt - now).total_seconds() + 15)
        else:
            wait = 600.0
        stop.wait(min(wait, BOARD_EVERY + 30))


# ---------------------------------------------------------------- the web page

def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            sym = (q.get("sym") or [INDEX])[0]
            tf = (q.get("tf") or ["5m"])[0]
            try:
                if url.path == "/":
                    with open(PAGE, "rb") as fh:
                        return self.send(200, fh.read(), "text/html; charset=utf-8")
                if url.path == "/api/status":
                    return self.send(200, json.dumps(store.status()))
                if url.path == "/api/chart":
                    return self.send(200, json.dumps(store.chart(sym, tf)))
                if url.path == "/api/backtest":
                    return self.send(200, json.dumps(store.backtest(sym)))
                self.send(404, json.dumps({"error": "not found"}))
            except ChartError as e:
                self.send(502, json.dumps({"error": str(e)}))
    return Handler


# ---------------------------------------------------------------- commands

def cmd_serve(a):
    clock = Clock(a.now)
    if a.until and not trading_day(clock.now().date()):
        print("NSE is closed today (%s): nothing to watch." % clock.now().strftime("%a %Y-%m-%d"))
        return 0
    source = Source(clock, a.offline)
    syms = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else None
    store = Store(source, clock, syms)
    stop = threading.Event()
    alerter = None
    if a.alerts:
        notifier = Notifier()
        alerter = Alerter(store, notifier, mode=a.alerts)
        print("Phone alerts (%s): %s." % (a.alerts, ", ".join(notifier.channels) or
                                     "no channel set (see SKILL.md): alerts go to trader/alerts-<date>.txt only"))
    threading.Thread(target=board_loop, args=(store, stop, alerter), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(store))
    url = "http://127.0.0.1:%d/" % a.port
    state, words = market_state(clock.now())
    print("Live Chart on %s  (%s IST: %s)" % (url, clock.now().strftime("%a %Y-%m-%d %H:%M"), words))
    print("Prices refresh every %ds for the chart you look at, every 5 minutes for the list. Ctrl+C stops it."
          % CHART_TTL)
    print("A bias is a description of the chart so far, not a forecast. Not financial advice.")
    if not a.no_browser:
        webbrowser.open(url)
    if a.until:
        end = datetime.datetime.strptime(a.until, "%H:%M").time()

        def watch():
            while not stop.wait(20):
                if clock.now().time() >= end:
                    print("It is %s: stopping for the day." % a.until)
                    server.shutdown()
                    return
        threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        stop.set()
        server.server_close()
    return 0


def cmd_bias(a):
    clock = Clock(a.now)
    store = Store(Source(clock, a.offline), clock)
    p = store.chart(a.symbol)
    if a.json:
        print(json.dumps(p["bias"], indent=2))
        return 0 if p["bias"] else 1
    b = p["bias"]
    print("%s  %s IST, session %s, 5-minute bars (Yahoo, free, can lag)" % (p["symbol"], clock.now().strftime("%H:%M"), p["session"]))
    if not b:
        print("No closed bar today yet: no read.")
        return 1
    print("%s  (score %+d of %d) at the %s close %s" % (b["label"].upper(), b["score"], b["max"], b["at"], inr(b["close"])))
    for s in b["signals"]:
        print("  %s %-22s %s" % ({1: "+", -1: "-", 0: "·"}[s["vote"]], s["name"], s["detail"]))
    print("A description of the chart so far, not a forecast. Check the record: live.py backtest %s" % a.symbol)
    return 0


def cmd_backtest(a):
    clock = Clock(a.now)
    store = Store(Source(clock, a.offline), clock)
    bt = store.backtest(a.symbol)
    if a.json:
        print(json.dumps(bt, indent=2))
        return 0
    print("Bias record, %s, 5-minute bars (Yahoo):" % a.symbol.upper())
    for ln in bt["lines"]:
        print("  " + ln)
    print("Readings overlap (one every 5 minutes), so they are not independent. Past hit rates do not promise future ones.")
    return 0


def cmd_levels(a):
    clock = Clock(a.now)
    store = Store(Source(clock, a.offline), clock)
    store.refresh_board()
    try:
        store.chart(INDEX)
    except ChartError:
        pass
    plans = Alerter(store, Notifier({})).plans()
    if not plans:
        print("No session today yet (%s IST): levels come after the 9:15 to 9:30 range." % clock.now().strftime("%a %H:%M"))
        return 1
    text = morning_text(clock.now().date(), plans)
    print(text)
    if a.send:
        n = Notifier()
        for c, ok, err in n.send("NSE levels %s" % clock.now().strftime("%d %b"), text) or [("none", False, "no channel set")]:
            print("%s: %s" % (c, "sent" if ok else "not sent (%s)" % err))
    return 0


def cmd_alert_test(a):
    n = Notifier()
    if not n.channels:
        print("No alert channel is set. Set the environment variables in nse-chart/SKILL.md (Phone alerts), "
              "open a new terminal, and try again.")
        return 1
    ok_all = True
    for c, ok, err in n.send("NSE Live Chart test", "Test from nse-chart: phone alerts work. Not financial advice."):
        print("%-9s %s" % (c, "sent: check your phone" if ok else "FAILED: %s" % err))
        ok_all = ok_all and ok
    return 0 if ok_all else 1


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Live Chart (nse-chart): a live NSE chart with entry, stop, target and a bias read.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-30 10:00"')
        p.add_argument("--offline", metavar="FILE", help="a saved {SYMBOL: Yahoo chart} bundle (.json or .json.gz)")

    p = sub.add_parser("serve", help="the live page on this computer (the default)")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--symbols", help="comma-separated NSE symbols instead of the Nifty 50")
    p.add_argument("--alerts", nargs="?", const="all", choices=["all", "daily"],
                   help="send to your phone: all (levels, each breakout and exit, summary) or daily (levels and summary)")
    p.add_argument("--until", metavar="HH:MM", help="stop at this IST time (and do nothing on a closed day)")
    common(p)
    p = sub.add_parser("levels", help="today's buy-above, stop and target for the index and the Nifty 50")
    p.add_argument("--send", action="store_true", help="also send the list to your phone")
    common(p)
    sub.add_parser("alert-test", help="send one test message to every alert channel set")
    for name, hlp in (("bias", "the bias read right now"), ("backtest", "how often the read was right, 60 sessions")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("symbol", help="NIFTY for the index, or an NSE symbol like ICICIBANK")
        p.add_argument("--json", action="store_true")
        common(p)
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("serve", "bias", "backtest", "levels", "alert-test", "-h", "--help"):
        argv = ["serve"] + argv             # plain `live.py --port 8060` serves too
    a = ap.parse_args(argv)
    try:
        return {"serve": cmd_serve, "bias": cmd_bias, "backtest": cmd_backtest, "levels": cmd_levels,
                "alert-test": cmd_alert_test}[a.cmd](a)
    except ChartError as e:
        print(str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
