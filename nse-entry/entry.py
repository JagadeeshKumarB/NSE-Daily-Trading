#!/usr/bin/env python3
"""Entry Timer for nse-entry: watches a setup bar by bar on NSE and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check ICICIBANK                     # opening-range breakout, 5-minute bars
    python3 entry.py check ICICIBANK --now "2026-10-30 09:35" --offline nifty50-5m-60d.json.gz
    python3 entry.py check ICICIBANK --setup level --level 1400    # first close above a price
    python3 entry.py check ICICIBANK --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch ICICIBANK --every 30 --until 14:30  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups (longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:15, 9:20 and 9:25 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from nse-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars. Yahoo shows no volume for NSE's
         9:15 bar on most days, so the average uses the range bars that have
         volume.

All times are IST (India, UTC+5:30, no daylight saving). NSE's normal market
is 9:15 to 15:30; the 9:00-9:08 pre-open auction is ignored.

A trigger's time is the trigger bar's CLOSE time (the 9:30 bar closes at
9:35 AM) and its price is that close, on NSE's tick grid. Only bars on the
interval grid count (Yahoo's live tick, e.g. 10:49:45, is dropped) and a bar
counts only once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, too early, or
no close above the level yet); expired (no trigger by no_entries_after,
default 14:30); no-data (NSE is closed today or Yahoo sent no bars).

trader/config.json (optional): "no_entries_before" (09:30), "no_entries_after"
(14:30), IST, and "holidays" (extra NSE holidays, "YYYY-MM-DD").

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/ICICIBANK.NS?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart instead: one v8 chart JSON (.json or
  .json.gz), a bundle {"ICICIBANK": <v8 chart>, ...} (the symbol is picked out
  and the day selected by --now), or PATH/<SYMBOL>.json when PATH is a folder.
  Without --now, an offline file is read as of its own time
  (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 india-day-trader-skill")
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:30"}
EPOCH = datetime.datetime(1970, 1, 1)

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


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time

def ist_utc(local):
    """Naive IST -> naive UTC."""
    return ist_to_utc(local).replace(tzinfo=None)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-30 09:35" (or 9:35, ...T09:35, or just 9:35 for
    today) is IST; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?$", t)
    if not m:
        raise EntryError('--now wants an IST time like "2026-10-30 09:35" (or ...Z for UTC)')
    try:
        d = datetime.date.fromisoformat(m.group(1))
        dt = datetime.datetime.combine(d, datetime.time(int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)))
    except ValueError:
        raise EntryError('--now wants an IST time like "2026-10-30 09:35", not %r' % text)
    tz = m.group(5)
    if not tz:
        return ist_utc(dt)
    if tz == "Z":
        return dt
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    off = datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return dt - sign * off


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an IST time like 14:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """09:35, 14:30: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:35, 14:30: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_status(d, cfg=None):
    """(is_trading_day, reason). Weekends, NSE's holiday list and config "holidays"."""
    if d.weekday() >= 5:
        return False, "weekend"
    if not trading_day(d, cfg):
        return False, "NSE holiday"
    return True, ""


def session(d, cfg=None):
    """(open_time, close_time) in IST, or None if closed all day. NSE has no half days."""
    return (OPEN, CLOSE) if day_status(d, cfg)[0] else None


def next_open(d, cfg=None):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd, cfg):
            return "%s 09:15" % nd.isoformat()
        nd += datetime.timedelta(days=1)
    return None


def price(x, symbol=None, cfg=None):
    """A Yahoo float on NSE's tick grid, as a float for JSON."""
    return None if x is None else float(to_tick(x, "nearest", symbol, cfg))


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    return to_yahoo(sym)


def nse_symbol(sym):
    return from_yahoo(str(sym).strip().upper())


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (
        urllib.parse.quote(yahoo_symbol(sym), safe=".-"), rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown symbol
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 (text or an already-parsed dict) -> {symbol, meta,
    bars, prev_close, data_time}. Bars with a null close are dropped here;
    off-grid ticks later. symbol is the plain NSE symbol (ICICIBANK)."""
    try:
        data = json.loads(text) if isinstance(text, str) else text
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown symbol"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": nse_symbol(meta.get("symbol") or sym), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for base in (sym, yahoo_symbol(sym)):
            for ext in (".json", ".json.gz"):
                p = os.path.join(path, base + ext)
                if os.path.exists(p):
                    return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def pick_from_bundle(data, sym):
    """A bundle {"ICICIBANK": <v8 chart>, ...} -> that symbol's chart. A plain v8 chart passes through."""
    if not isinstance(data, dict) or "chart" in data:
        return data
    for key in (sym, yahoo_symbol(sym)):
        if key in data:
            return data[key]
    for key in data:
        if nse_symbol(key) == sym:
            return data[key]
    raise EntryError("the offline bundle has no %s (it holds %d symbols, like %s)" % (
        sym, len(data), ", ".join(sorted(data)[:3])))


def get_chart(sym, interval, day=None, offline=None, today=None):
    """Bars for one symbol, from Yahoo (range=1d for today) or a saved file."""
    sym = nse_symbol(sym)
    if offline:
        f = offline_file(offline, sym)
        try:
            data = json.loads(read_text(f))
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
        data = pick_from_bundle(data, sym)
    else:
        today = today or to_ist(utc_now()).date()
        data = yahoo_get(chart_path(sym, chart_range(day or today, today, interval), interval))
    ch = parse_chart(data, sym)
    if ch["symbol"] != sym:
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
        for k in ("holidays", "tick_size"):
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    try:
        extra_holidays(cfg)
    except (TypeError, ValueError):
        raise EntryError("config \"holidays\" wants dates like \"2027-01-26\"")
    return cfg


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None):
    """Pure function: the setup's status at now_utc from one session's bars.
    The chart may hold many days (a 60-day file): the day is now_utc's IST date."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    sym = chart["symbol"]
    n_or = or_minutes * 60 // step
    local = to_ist(now_utc)
    day = local.date()
    res = {"symbol": sym, "setup": setup, "side": "buy", "date": day.isoformat(), "exchange": "NSE",
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "calendar_note": calendar_warning(day, cfg), "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = to_ist(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = day_status(day, cfg)
    if not ok:
        res.update(status="no-data", phase="closed", reason="NSE is closed today (%s)" % why,
                   next_open=next_open(day, cfg))
        return res
    o, c = session(day, cfg)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = min(datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after")), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot, earlier = {}, None
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        ist = to_ist(utc_of(b["t"]))
        if ist.date() < day and (earlier is None or b["t"] > earlier["t"]):
            earlier = b
        if ist.date() != day or not (open_dt <= ist < close_dt):
            continue
        slot = int((ist - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=ist, end=ist + datetime.timedelta(seconds=step), slot=slot)
    if earlier is not None:                # a many-day file: the previous session's last close
        res["prev_close"] = r2(earlier["c"])
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": price(b["c"], sym, cfg)}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (to_ist(utc_of(dt)).strftime("%H:%M:%S"),
                                                                       short(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": price(b["c"], sym, cfg)}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        res.update(phase="pre-open", reason="NSE opens at 9:15%s" % (
            " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    vol_bars = [b for b in or_bars if b["v"] > 0]    # Yahoo often shows 0 for NSE's 9:15 bar
    avg_vol = (sum(b["v"] for b in vol_bars) / float(len(vol_bars))) if vol_bars else None
    if or_bars:
        res["opening_range"] = {"high": price(max(b["h"] for b in or_bars), sym, cfg),
                                "low": price(min(b["l"] for b in or_bars), sym, cfg),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end,
                                "avg_volume": int(round(avg_vol)) if avg_vol is not None else None}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
        res["level"] = price(lvl, sym, cfg)
    else:
        lvl = float(level)
        res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": price(last["c"], sym, cfg),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (short(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": price(trig["c"], sym, cfg), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < ist_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a tick or two"
                                % to_ist(utc_of(dt)).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    shown = res["level"]
    what = "close above the range high %s" % inr(shown) if setup == "orb" else "close above %s" % inr(shown)
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry" % (what, short(cutoff)))
        return res
    res["phase"] = "open"
    gap = lvl - last["c"]
    far = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
        inr(price(last["c"], sym, cfg)), short(last["et"]), inr(abs(gap)), "under" if gap >= 0 else "over",
        "the range high %s" % inr(shown) if setup == "orb" else "the level %s" % inr(shown),
        100.0 * (last["c"] / lvl - 1))
    first_after = open_dt + datetime.timedelta(seconds=first_slot * step)
    if setup == "orb" and last["slot"] < n_or:
        res["reason"] = "too early: the first bar after the opening range, the %s bar, closes at %s; %s" % (
            short(first_after), ampm(first_after + datetime.timedelta(seconds=step)), far)
    elif last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), inr(price(last["c"], sym, cfg)), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            short(last["et"]), inr(price(last["c"], sym, cfg)), short(before))
    else:
        res["reason"] = far
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "nse-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % inr(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s IST  %s on %s bars (Yahoo, free, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            short(start), short(end), inr(orr["high"]), inr(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed above %s %s (%+.2f%%%s)." % (
            t["time"], inr(t["price"]), short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", inr(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], inr(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /nse-exit "
                   "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), inr(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("calendar_note"):
        out.append(res["calendar_note"])
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (inr(price(min(x["l"] for x in seen))), inr(price(max(x["h"] for x in seen))))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (inr(orr["high"]), inr(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s, %s" % (
            inr(lvl), t["pct_over"], t["time"], inr(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % inr(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (inr(lvl - b["c"]), inr(lvl))
    return "%5s bar  closed %s  %s" % (start, inr(price(b["c"])), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    cfg = opts.get("cfg") or dict(DEFAULTS)
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_ist(now)
        day = local.date()
        s = session(day, cfg)
        if not s:
            emit("%s: NSE is closed today (%s). Nothing to watch." % (day.isoformat(), day_status(day, cfg)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = ist_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or DEFAULTS["no_entries_after"], "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("this pack plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_ist(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = nse_symbol(a.symbol)
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2, ensure_ascii=False))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = nse_symbol(a.symbol)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_ist(utc_now()).date()
    cache = {}

    def provider(now):
        if a.offline:                      # a saved file does not change: read it once
            if "chart" not in cache:
                cache["chart"] = get_chart(sym, a.interval, offline=a.offline)
            return cache["chart"]
        return get_chart(sym, a.interval, to_ist(now).date(), None, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_ist(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /nse-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = nse_symbol(h["symbol"])
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows],
                         indent=2, ensure_ascii=False))
    else:
        w = max([10] + [len(s) for s, _, _ in rows])
        print("%-*s  %-9s  %-8s  %-10s  %-10s  %-10s  %s" % (w, "SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-*s  error: %s" % (w, s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-*s  %-9s  %-8s  %-10s  %-10s  %-10s  %s" % (
                w, s, status[:9], t.get("time") or "-", inr(t["price"]) if t else "-",
                inr(r["level"]) if r.get("level") is not None else "-",
                inr(last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /nse-exit." % (n, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Entry Timer (nse-entry): watches a setup bar by bar on NSE.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="buy only")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-30 09:35"')
        p.add_argument("--offline", metavar="PATH",
                       help="saved Yahoo chart JSON (.json/.json.gz), a {SYMBOL: chart} bundle, or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
