#!/usr/bin/env python3
"""Chart Scanner for nse-scanner: scan NSE charts for setups. Standard library only.

    python3 scan.py universe                          # NSE's lists -> trader/universe.txt
    python3 scan.py universe --index nifty500         # or only the Nifty 50 / 100 / 500
    python3 scan.py daily                             # after the close or before the open -> trader/watchlist.json
    python3 scan.py daily --symbols "RELIANCE,TCS,M&M"  # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # from 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 10:00 --or-minutes 15    # cut the clock at 10:00 AM IST
    python3 scan.py orb --offline nifty50-5m-60d.json.gz --now "2026-10-30 10:00"

universe  Downloads NSE's list of listed equities (EQUITY_L.csv) and the
          security list with price bands (sec_list.csv), both from
          nsearchives.nseindia.com. Keeps series EQ (the only series you can
          trade intraday); --include-be keeps BE (trade-for-trade, delivery
          only) too. Each stock gets its price band (circuit limit: 2, 5, 10,
          20 or "No Band" for F&O stocks) from sec_list.csv, matched on symbol
          and series; a stock missing from it gets an empty band.
          --index nifty50|nifty100|nifty500 takes the index's constituent
          list from niftyindices.com instead, still joined with the band.
          Writes one SYMBOL|Name|Series|Band a line.

daily     Scan of daily CLOSES (Yahoo spark: 20 symbols a request, 3 months
          of 1-day bars, closes only, symbols as RELIANCE.NS). Liquidity from
          the spark meta: regularMarketPrice >= --min-price (Rs 50) and
          regularMarketVolume >= --min-volume (1,00,000 shares). Setups, from
          closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 3:30 PM close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session: the 9:15, 9:20 and 9:25 bars. The
          first bar after it closes at 9:35 AM; before that the scan says it is
          too early. Stage 1: spark closes for every symbol (1 day of 5-minute
          bars, 20 a request); a chart goes on only if a completed bar after
          the range closed at or above the highest close of the range (a
          necessary condition). Stage 2: each candidate's v8 chart (real highs
          and lows): range high = the highest high of the range bars, a hit =
          the first completed bar to CLOSE above it, at that bar's close time.
          Stage 2 fetches at most --max-confirm (200) charts, the most traded
          candidates first (price x volume so far), and says so when it stops
          short. A chart is usable only when every 5-minute bar from 9:15 to
          the cutoff is there (a missing bar is a halt or no trades). After
          no_entries_after (trader/config.json, default 14:30) a breakout is
          expired and the scan says so. Floors: --min-price 50, --min-volume 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are India time (IST, UTC+5:30).
--now pins the clock ("2026-10-30 10:00", IST; ...Z for UTC). The NSE
holiday list is built in for 2026; for another year every weekday counts as
open (one warning line) unless trader/config.json lists "holidays".

Symbols: users see the plain NSE symbol (M&M); Yahoo wants M&M.NS, and the
symbol is URL-quoted (M%26M.NS). Unquoted, the & ends the symbol list: Yahoo
then answers for "M" (a US stock) and drops the rest of that request. A
spark answer for a symbol that is not one we asked for (or, offline, not an
.NS / .BO symbol) is ignored and said.

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with EQUITY_L.csv and sec_list.csv (or, with --index nifty500,
ind_nifty500list.csv or nifty500.csv; .csv.gz is fine); daily and orb take a
folder of <SYMBOL>.json v8 chart files or spark answers, or one file that is
a spark answer, a v8 chart, or a bundle {"RELIANCE": <v8 json>, ...}.
.json.gz is fine. An offline orb bundle may hold many days: the day is the
one of --now (or the last day in the file, with --at).

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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

UA = "Mozilla/5.0 (compatible; india-day-trader-skill/1.0)"
NSE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Safari/537.36")  # NSE's archives refuse plain script user agents
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NSE_ARCHIVES = "https://nsearchives.nseindia.com/content/equities/"
EQUITY_FILE = "EQUITY_L.csv"
SECLIST_FILE = "sec_list.csv"
NIFTY_URL = "https://niftyindices.com/IndexConstituent/"
INDEXES = {"nifty50": "ind_nifty50list.csv", "nifty100": "ind_nifty100list.csv",
           "nifty500": "ind_nifty500list.csv"}
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
TAIL = datetime.time(15, 15)  # from here to the 3:30 close, Yahoo's NSE 5-minute bars are often missing
DAILY_MIN_PRICE = 50.0      # rupees: below this the tick (Rs 0.01 to 0.05) is a big slice of the move
DAILY_MIN_VOLUME = 100000   # shares in the last session (1 lakh): low enough for a Rs 10,000 stock
ORB_MIN_PRICE = 50.0
NO_ENTRIES_AFTER = "14:30"
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time (IST)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-30 10:00" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:00" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def market_day(d, config=None):
    """(is_trading_day, reason). Weekends, NSE's holidays and config "holidays"."""
    if d.weekday() >= 5:
        return False, "weekend"
    if not trading_day(d, config):
        return False, "an NSE holiday"
    return True, ""


def session(d, config=None):
    """(open_time, close_time) in IST, or None if closed all day. NSE has no half days."""
    ok, _ = market_day(d, config)
    return (OPEN, CLOSE) if ok else None


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


def ist_stamp(now_utc):
    return to_ist(now_utc).strftime("%Y-%m-%dT%H:%M:%S+05:30")


# ---------------------------------------------------------------- network

def http_get(url, timeout=20, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8-sig", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def yq(sym):
    """NSE symbol -> URL-quoted Yahoo symbol: M&M -> M%26M.NS."""
    return urllib.parse.quote(to_yahoo(sym), safe=".-")


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(yq(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yq(sym), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def _indian(ysym):
    return ysym.upper().endswith((".NS", ".BO"))


def parse_spark(text_or_doc, wanted=None, skipped=None):
    """Spark JSON -> {SYM: series}, keyed by the plain NSE symbol. Symbols Yahoo does not
    know are simply missing from the answer. `wanted` maps Yahoo symbols (M&M.NS) back to
    ours; an answer for anything else (or, without `wanted`, anything that is not .NS /
    .BO) is ignored and its "SYMBOL (name, exchange)" appended to `skipped`."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        meta = resp[0].get("meta") or {}
        ysym = (item.get("symbol") or meta.get("symbol") or "").upper()
        if wanted is not None:
            sym = wanted.get(ysym)
        else:
            sym = from_yahoo(ysym) if _indian(ysym) else None
        if not sym:
            if skipped is not None:
                skipped.append("%s (%s, %s)" % (ysym, meta.get("longName") or meta.get("shortName") or "?",
                                                meta.get("exchangeName") or "?"))
            continue
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors, skipped):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc, None, skipped))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = from_yahoo(sym.upper())
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors, skipped)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path, skipped=None):
    """A folder of <SYMBOL>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    skipped = [] if skipped is None else skipped
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors, skipped)
            else:
                _absorb(doc, "", data, errors, skipped)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors, skipped)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def read_csv_rows(text, need, what):
    """CSV text -> list of dicts with stripped keys and values. `need`: columns that must be there."""
    text = text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ScanError("%s is empty" % what)
    hdr = [h.strip() for h in rows[0]]
    missing = [c for c in need if c not in hdr]
    if missing:
        raise ScanError("%s does not look like NSE's file (no %s column; it starts %r)"
                        % (what, ", ".join(missing), text[:60]))
    out = []
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        out.append({h: (r[i].strip() if i < len(r) else "") for i, h in enumerate(hdr)})
    return out


def parse_seclist(text):
    """sec_list.csv -> {(SYMBOL, SERIES): band} plus {SYMBOL: band} for the first series seen."""
    bands = {}
    for r in read_csv_rows(text, ("Symbol", "Series", "Band"), SECLIST_FILE):
        sym, ser, band = r["Symbol"].upper(), r["Series"].upper(), r["Band"]
        bands[(sym, ser)] = band
        bands.setdefault(sym, band)
    return bands


def band_of(bands, sym, series):
    return bands.get((sym, series), bands.get(sym, "")) if bands else ""


def _clean(name):
    return " ".join((name or "").replace("|", "/").split())


def build_universe(equity_text, seclist_text, include_be=False):
    """EQUITY_L.csv + sec_list.csv -> (kept [(SYM, name, series, band)], counts).
    counts: rows, kept, no_band, and one "series XX" per dropped series."""
    rows = read_csv_rows(equity_text, ("SYMBOL", "NAME OF COMPANY", "SERIES"), EQUITY_FILE)
    bands = parse_seclist(seclist_text) if seclist_text else {}
    keep = {"EQ", "BE"} if include_be else {"EQ"}
    kept, counts = {}, {"rows": len(rows), "no_band": 0}
    for r in rows:
        sym, ser = r["SYMBOL"].upper(), r["SERIES"].upper()
        if ser not in keep:
            k = "series " + (ser or "blank")
            counts[k] = counts.get(k, 0) + 1
            continue
        band = band_of(bands, sym, ser)
        if not band:
            counts["no_band"] += 1
        kept[sym] = (sym, _clean(r["NAME OF COMPANY"]), ser, band)
    counts["kept"] = len(kept)
    return [kept[k] for k in sorted(kept)], counts


def build_index_universe(index_text, seclist_text, index_name):
    """A niftyindices constituent CSV + sec_list.csv -> (kept, counts)."""
    rows = read_csv_rows(index_text, ("Company Name", "Symbol", "Series"), INDEXES[index_name])
    bands = parse_seclist(seclist_text) if seclist_text else {}
    kept, counts = {}, {"rows": len(rows), "no_band": 0}
    for r in rows:
        sym, ser = r["Symbol"].upper(), (r["Series"] or "EQ").upper()
        band = band_of(bands, sym, ser)
        if not band:
            counts["no_band"] += 1
        kept[sym] = (sym, _clean(r["Company Name"]), ser, band)
    counts["kept"] = len(kept)
    return [kept[k] for k in sorted(kept)], counts


def offline_csv(folder, names):
    """The first of `names` (each also tried with .gz) found in folder -> text, else None."""
    for n in names:
        for p in (os.path.join(folder, n), os.path.join(folder, n + ".gz")):
            if os.path.exists(p):
                return _read_maybe_gz(p)
    return None


def nse_get(url, what):
    try:
        return http_get(url, timeout=60, ua=NSE_UA)
    except urllib.error.HTTPError as e:
        raise ScanError("%s answered HTTP %d for %s" % (urllib.parse.urlparse(url).netloc, e.code, what))
    except (urllib.error.URLError, OSError) as e:
        raise ScanError("could not reach %s: %s" % (urllib.parse.urlparse(url).netloc, getattr(e, "reason", e)))


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc, config=None):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the 3:30 PM close, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today, config)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume, config=None):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc, config)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST), on the grid,
    with a close, inside 9:15 to 3:30. Sorted, one per slot."""
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        it = ts_to_ist(ts)
        if it.date() != day or it.second or it.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if it.time() < OPEN or it.time() >= CLOSE:
            continue  # pre-open, or the closing session after 3:30
        if it + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[it] = {"start": it, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:15 to the cutoff is there, and at least the range. The last
    15 minutes (from 3:15 PM) are not required: Yahoo's NSE charts never have a 3:20 bar
    and often lack 3:15 and 3:25 (the closing-price window); bars there are used when present."""
    done = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    want = min(done, int((datetime.datetime.combine(day, TAIL) - slot(day, 0)).total_seconds()
                         // (BAR_MIN * 60)))
    if done < n_range or len(bars) < want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars[:want]))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, config=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = market_day(day, config)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, CLOSE)
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:00."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("Too early: it is %s IST; the %d-minute opening range runs 9:15 to %s and the first bar "
                        "after it closes at %s. Run orb again at %s or later."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb on the next trading day after %s, "
                        "or replay today with --at 10:00." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                               clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None, skipped=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted, skipped)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [from_yahoo(s) for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [from_yahoo(str(x.get("symbol") if isinstance(x, dict) else x).upper()) for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(from_yahoo(ln.split("|")[0].strip().upper()))
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _c(n):
    """Indian digit grouping for a count: 200000 -> 2,00,000."""
    return inr(n, 0)[1:]


def _names(syms, n=12):
    syms = list(syms)
    return ", ".join(syms[:n]) + (" and %d more" % (len(syms) - n) if len(syms) > n else "")


def warn_calendar(day, cfg):
    w = calendar_warning(day, cfg)
    if w:
        print("Note: " + w, file=sys.stderr)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    if a.index:
        fname = INDEXES[a.index]
        if a.offline:
            main_text = offline_csv(a.offline, (fname, a.index + ".csv"))
            if main_text is None:
                raise ScanError("no %s (or %s.csv) in %s" % (fname, a.index, a.offline))
        else:
            main_text = nse_get(NIFTY_URL + fname, fname)
    else:
        fname = EQUITY_FILE
        if a.offline:
            main_text = offline_csv(a.offline, (EQUITY_FILE,))
            if main_text is None:
                raise ScanError("no %s in %s" % (EQUITY_FILE, a.offline))
        else:
            main_text = nse_get(NSE_ARCHIVES + EQUITY_FILE, EQUITY_FILE)
    band_note = None
    if a.offline:
        sec_text = offline_csv(a.offline, (SECLIST_FILE,))
        if sec_text is None:
            band_note = "no %s in %s: the price bands are left empty" % (SECLIST_FILE, a.offline)
    else:
        if a.pause:
            SLEEP(a.pause)
        try:
            sec_text = nse_get(NSE_ARCHIVES + SECLIST_FILE, SECLIST_FILE)
        except ScanError as e:
            sec_text, band_note = None, "%s; the price bands are left empty" % e
    if a.index:
        kept, counts = build_index_universe(main_text, sec_text, a.index)
    else:
        kept, counts = build_universe(main_text, sec_text, a.include_be)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for row in kept:
                fh.write("|".join(row) + "\n")
    dropped = {k: v for k, v in counts.items() if k.startswith("series ")}
    series = {}
    for row in kept:
        series[row[2]] = series.get(row[2], 0) + 1
    bands = {}
    for row in kept:
        bands[row[3] or "unknown"] = bands.get(row[3] or "unknown", 0) + 1
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts["rows"], "source": a.index or "EQUITY_L",
                          "series": series, "bands": bands, "dropped": dropped, "band_note": band_note,
                          "out": None if a.no_write else a.out, "seconds": round(time.time() - t0, 1)},
                         indent=2, ensure_ascii=False))
        return 0 if kept else 1
    if a.index:
        print("%s constituents (niftyindices.com): %s rows." % (a.index.replace("nifty", "Nifty "), _c(counts["rows"])))
        print("Kept %s stocks." % _c(len(kept)))
    else:
        print("NSE's list of equities (%s): %s rows." % (EQUITY_FILE, _c(counts["rows"])))
        print("Kept %s stocks (%s)." % (_c(len(kept)), ", ".join("%s %s" % (_c(v), k)
                                                                 for k, v in sorted(series.items()))))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % (_c(v), k) for k, v in
                                          sorted(dropped.items(), key=lambda kv: -kv[1])) +
                  (" (trade-for-trade: delivery only, no intraday)" if "series BE" in dropped
                   or "series BZ" in dropped else "") + ".")
    print("Price bands: " + ", ".join("%s %s" % (_c(v), ("No Band (F&O)" if k == "No Band" else
                                                       k if k == "unknown" else k + " %"))
                                      for k, v in sorted(bands.items(), key=lambda kv: -kv[1])) + ".")
    if band_note:
        print("Note: " + band_note + ".")
    if not a.no_write:
        print("Wrote %s (one SYMBOL|Name|Series|Band a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    cfg = read_config(a.config)
    warn_calendar(to_ist(now).date(), cfg)
    offline_syms = None
    skipped = []
    if a.offline:
        data, failed = load_offline(a.offline, skipped)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % (_c(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a, skipped)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume, cfg)
    missing = [s for s in symbols if s not in data]
    nfailed = len(missing)
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "nse-scanner", "created_at": ist_stamp(now), "mode": "daily",
           "exchange": "NSE", "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"],
           "failed": nfailed, "source": "yahoo",
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if missing:
        doc["no_data"] = missing
    if skipped:
        doc["ignored"] = skipped
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s+ and %s+ shares)%s; "
          "%s scanned." % (_c(counts["fetched"]), _c(len(symbols)),
                           (" (%s failed or had no data)" % _c(nfailed)) if nfailed else "",
                           _c(counts["fetched"] - counts["illiquid"]), inr(a.min_price, 0),
                           _c(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           _c(counts["scanned"])))
    if missing:
        print("PARTIAL: no data for %s." % _names(missing))
    if skipped:
        print("Ignored %d answer%s for a symbol that is not one asked for: %s."
              % (len(skipped), "" if len(skipped) == 1 else "s", _names(skipped, 5)))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-10s %12s  RSI %5.1f  %s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                               ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-entry SYMBOL after 9:35 AM.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or NO_ENTRIES_AFTER, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    warn_calendar(explicit_day or to_ist(now).date(), cfg)
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, cfg)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("this scan fetches only the last few days of 5-minute bars: replay %s with --offline"
                            % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % (_c(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed or s not in data])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "nse-scanner", "created_at": ist_stamp(now), "mode": "orb", "exchange": "NSE",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/\\"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2, ensure_ascii=False))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % (_c(counts["fetched"]), _c(len(symbols)),
             (" (%s failed or had no data)" % _c(nfailed)) if nfailed else "",
             _c(counts["scanned"]),
             (" (%s under the price / volume floor)" % _c(counts["illiquid"])) if counts["illiquid"] else "",
             _c(counts["candidates"]), _c(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % (_c(counts["unconfirmed"]), _c(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % _c(counts["confirmed"])) if counts["unconfirmed"]
                 else _c(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-10s %12s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], inr(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /nse-entry SYMBOL.")
    return 0 if hits else 1


def _p(x):
    return inr(x)[1:]


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Chart Scanner (nse-scanner): daily setups and opening-range "
                                             "breakouts on NSE stocks.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's lists -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--index", choices=sorted(INDEXES), help="only this index's stocks (niftyindices.com)")
    p.add_argument("--include-be", action="store_true", help="keep series BE (trade-for-trade) too")
    p.add_argument("--offline", metavar="DIR", help="a folder with EQUITY_L.csv and sec_list.csv (.gz is fine)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name|Series|Band a line (default %s)"
                       % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (%s)" % _n(min_price))
        p.add_argument("--min-volume", type=int, default=min_volume, help="shares (%d)" % min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-30 10:00" IST, or ...Z)')
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="setups from daily closes")
    common(p, DAILY_MIN_PRICE, DAILY_MIN_VOLUME)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, ORB_MIN_PRICE, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, %s)"
                   % NO_ENTRIES_AFTER)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


if __name__ == "__main__":
    sys.exit(main())
