"""Live Chart data: the India block (hours, holidays, ticks, charges, rupees), the clock,
NSE's market state and free Yahoo bars (live, or a saved bundle for replays). Used by live.py."""

import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

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
