#!/usr/bin/env python3
"""Time Machine for nse-timemachine. Standard library only.

Replays a planned NSE day trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:10            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol ICICIBANK --date 2026-10-30 --entry 1394.50 \\
        --stop 1387.10 --target 1401.90 --entry-time "9:35 AM"
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work:
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, SYMBOL.NS, range=60d
     interval=5m; one request). Keep the sessions before the plan's date. An
     NSE session is 9:15 to 15:30 IST: 75 five-minute bars. Today is left out.
  2. The pool: for every bar slot 1..last of the day (last = the bar that
     closes at --until), each past session's bar as ratios to the previous
     bar's close: (open, high, low, close) / prev close. A session that is
     missing a bar (Yahoo leaves gaps) gives nothing for that slot only.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..last, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default: the plan's flat_by,
     else 15:10, well before Zerodha's MIS auto square-off from about 15:20).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every past session that
     has all its bars up to --until, first close beyond the range = entry,
     same stop-first rule. Stops round down to NSE's tick (up for a sell),
     entries and targets to the nearest tick.
  7. Charges: an estimate of Zerodha's charges for each round trip (the India
     block's charges()), turned into R for the quantity that risks your
     max_loss_per_trade_inr (trader/config.json, default 500) at the stop, or
     --qty. "Average R after charges" subtracts them from every replay and
     every history trade. The verdict itself is on the prices only.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), the average R when its sign goes against the lean, and
a note when charges turn a positive average R negative.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 past sessions (or fewer than 20 with a
bar at some time the replay needs: Yahoo often leaves 15:15 to 15:25 empty) or
the entry bar is not in the data. Same plan, same data and same --seed give the
same numbers on any machine. A simulation built from the stock's own recent
bars is not a prediction: results, news and circuit limits can do what the last
60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON, or a bundle {"SYMBOL": chart, ...}
(.json or .json.gz), instead. Times are IST; --now "2026-10-30 09:35" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
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

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
OPEN_MIN = OPEN.hour * 60 + OPEN.minute        # 9:15 IST
CLOSE_MIN = CLOSE.hour * 60 + CLOSE.minute     # 15:30 IST
SESSION_BARS = (CLOSE_MIN - OPEN_MIN) // 5     # 75
DEFAULT_UNTIL = "15:10"                        # config flat_by default
DEFAULT_MAX_LOSS = 500                         # config max_loss_per_trade_inr default
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class TMError(Exception):
    pass


# ---------------------------------------------------------------- time

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-10-30 09:35\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'9:35 AM', '9:35am', '15:10' or '3:10' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an IST time like 9:35 AM or 15:10, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(to_yahoo(sym), safe=""))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the NSE symbol" % sym.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_offline(path, sym):
    """A saved Yahoo chart file or bundle (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym.upper(), "%s-60d5m.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    """Chart JSON text, or a bundle {"SYMBOL": chart, ...}, -> the plan symbol's chart result."""
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    if isinstance(data, dict) and "chart" not in data:
        key = next((k for k in (sym.upper(), to_yahoo(sym)) if k in data), None)
        if key is None:
            raise TMError("the offline bundle has no %s (it holds %d symbols)" % (sym.upper(), len(data)))
        data = data[key]
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown symbol"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price (Yahoo often leaves 15:15 to
    15:25 IST empty), bars off the 5-minute grid (the live tick), bars not complete
    yet (now < start + 5 min, where now is the earlier of --now and the data's own
    regularMarketTime), and bars outside 9:15 to 15:30 IST."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_ist(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def past_sessions(days, before, last):
    """Sessions strictly before the plan's date, oldest first: (all, full, short).
    all: every past session as (date, {slot: bar}), for the replay pool.
    full: the ones with every bar 0..last, as (date, [bars]), for the history.
    short: the ones missing a bar before --until, with the missing bar times."""
    alls, full, short = [], [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        alls.append((d, s))
        missing = [i for i in range(last + 1) if i not in s]
        if missing:
            short.append({"date": d, "bars": len(s), "missing": [clock24(OPEN_MIN + 5 * i) for i in missing]})
        else:
            full.append((d, [s[i] for i in range(last + 1)]))
    return alls, full, short


# ---------------------------------------------------------------- the simulation

def build_pool(sessions, last):
    """pool[i] (i = 1..last) = every past session's bar i as (o, h, l, c) / bar i-1's close,
    in date order. A session missing bar i or bar i-1 gives nothing for slot i."""
    pool = [[] for _ in range(last + 1)]
    for _, D in sessions:
        for i in range(1, last + 1):
            if i in D and i - 1 in D:
                pc = D[i - 1]["c"]
                pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy", cost_r=None):
    """Monte Carlo replays of bars k+1..last. cost_r(exit_price) -> charges in R (optional)."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(last, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    out = {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }
    if cost_r is not None:
        exits = [target if o == "target" else stop if o == "stop" else p for o, p in zip(outcomes, closes)]
        out["avg_R_net"] = round(statistics.mean(r - cost_r(x) for r, x in zip(Rs, exits)), 3)
    return out


# ---------------------------------------------------------------- ticks, stop rules and the history replay

def tick_away(x, long, symbol=None, config=None):
    """Round a stop onto NSE's tick, away from the entry: down for a buy, up for a sell.
    The float noise of Yahoo's prices (1383.4000244) is cut first."""
    return float(to_tick(round(x, 6), "down" if long else "up", symbol, config))


def tick_near(x, symbol=None, config=None):
    return float(to_tick(round(x, 6), "nearest", symbol, config))


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long, sym=None, cfg=None):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, long, sym, cfg)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, long, sym, cfg)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long, sym, cfg)


def history_replay(sessions, rule, last, side="buy", sym=None, cfg=None, cost=None):
    """The plan's rule on every past session that has all its bars up to --until: entry =
    the first bar after the opening range to close beyond it (up to the --until bar),
    then stop first, then target, bar by bar, else the close at --until.
    cost(entry, exit, risk) -> charges in R (optional)."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = tick_near(D[kk]["c"], sym, cfg)
        s = rule_stop(rule, oh, ol, e, long, sym, cfg)
        risk = round((e - s) if long else (s - e), 6)
        if risk <= 0:
            continue
        t = tick_near(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk, sym, cfg)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        tr = {"date": d, "time": clock12(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
              "outcome": oc or "close", "R": round(R, 2)}
        if cost is not None:
            x = t if oc == "target" else s if oc == "stop" else cl
            tr["R_net"] = round(R - cost(e, x, risk), 2)
        trades.append(tr)
    out = {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }
    if cost is not None:
        out["avg_R_net"] = round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None
    return out


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or to_ist(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /nse-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = from_yahoo(str(raw.get("symbol") or "").strip().upper())
    if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", sym):
        raise TMError("the plan needs an NSE symbol like ICICIBANK, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-30, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 9:35 AM)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 9:35 AM) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 9:20 AM and 3:25 PM to leave bars to replay "
                      "(got %s)" % clock12(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= SESSION_BARS * 5:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    tick = raw.get("tick")
    if tick is not None:
        try:
            tick = float(tick)
        except (TypeError, ValueError):
            raise TMError("tick must be a number like 0.1, got %r" % raw.get("tick"))
        if not tick > 0:
            raise TMError("tick must be positive, got %r" % raw.get("tick"))
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "exchange": str(raw.get("exchange") or "NSE").upper(), "product": str(raw.get("product") or "MIS").upper(),
        "tick": tick, "flat_by": raw.get("flat_by"), "from": src,
    }


def tick_config(plan, config=None):
    """The config for to_tick: config.json's tick_size, with the plan's own tick for its symbol on top."""
    ts = dict(((config or {}).get("tick_size") or {}))
    if plan.get("tick"):
        ts[plan["symbol"]] = plan["tick"]
    return {"tick_size": ts}


def resolve_rule(plan, entry_day, cfg=None):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    sym = plan["symbol"]
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(tick_away((oh + ol) / 2, long, sym, cfg) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, long, sym, cfg) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- charges

def max_loss_of(config):
    v = (config or {}).get("max_loss_per_trade_inr", DEFAULT_MAX_LOSS)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise TMError("max_loss_per_trade_inr in the config must be a number, got %r" % v)
    return v


def cost_model(plan, config=None, qty=None):
    """(summary, cost) for the charges. cost(entry, exit, risk) -> one round trip's
    estimated charges in R. The quantity is --qty, or the whole shares that risk
    max_loss_per_trade_inr at the stop (at least 1)."""
    long = plan["side"] == "buy"
    product = plan.get("product") or "MIS"
    max_loss = max_loss_of(config)
    if qty is not None and qty < 1:
        raise TMError("--qty must be at least 1 share")

    def q_for(risk):
        return qty if qty is not None else max(1, int(max_loss // risk)) if risk > 0 else 1

    cache = {}

    def cost(entry, exit_, risk):
        q = q_for(risk)
        key = (round(entry, 2), round(exit_, 2), q)
        if key not in cache:
            buy, sell = (entry, exit_) if long else (exit_, entry)
            cache[key] = charges(buy * q, sell * q, product, config)["total"]
        return cache[key] / (risk * q)

    e, s, t, risk = plan["entry"], plan["stop"], plan["target"], plan["entry"] - plan["stop"] if long else \
        plan["stop"] - plan["entry"]
    q = q_for(risk)
    buy_t, sell_t = (e, t) if long else (t, e)
    buy_s, sell_s = (e, s) if long else (s, e)
    at_t = charges(buy_t * q, sell_t * q, product, config)["total"]
    at_s = charges(buy_s * q, sell_s * q, product, config)["total"]
    summary = {"product": product, "qty": q,
               "qty_from": "--qty" if qty is not None else "max_loss_per_trade_inr %g" % max_loss,
               "at_target": at_t, "at_stop": at_s,
               "R_at_target": round(at_t / (risk * q), 3), "R_at_stop": round(at_s / (risk * q), 3),
               "note": "Estimated with Zerodha's equity rates; the contract note is the real number."}
    return summary, cost


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or DEFAULT_UNTIL
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    net = mc.get("avg_R_net")
    if avg is not None and net is not None and avg > 0 >= net:
        notes.append("After estimated charges the average R is %+.3f: the charges eat the edge." % net)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo",
                 config=None, qty=None):
    """Chart JSON text (or a bundle) + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = from_yahoo(str(meta.get("symbol") or "").upper())
    if msym and msym != sym:
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    what = "--until"
    if until is None:
        until, what = (plan.get("flat_by"), "the plan's flat_by") if plan.get("flat_by") else (DEFAULT_UNTIL, "--until")
    until_min = parse_clock(until, what)
    if (until_min - OPEN_MIN) % 5 or until_min > CLOSE_MIN:
        raise TMError("%s must be on the 5-minute grid and no later than 3:30 PM (the close), got %s"
                      % (what, until))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("%s %s leaves nothing to replay after the %s entry" % (what, clock12(until_min),
                                                                             plan["entry_time"]))
    day = datetime.date.fromisoformat(plan["date"])
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        if not entry_day:
            reason = ("NSE is closed that day (a weekend or a holiday)" if not trading_day(day, config)
                      else "no bars for that day")
        else:
            reason = "the data stops at the %s bar" % entry_day[max(entry_day)]["start"]
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    cw = calendar_warning(day, config)
    if cw:
        warn.append(cw.rstrip("."))
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    alls, sessions, short = past_sessions(days, plan["date"], last)
    if len(alls) < MIN_SESSIONS:
        raise TMError("only %d past session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(alls), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(alls, last)
    for j in range(max(1, k + 1 - window), last + 1):
        if len(pool[j]) < MIN_SESSIONS:
            late = OPEN_MIN + 5 * j >= parse_clock(DEFAULT_UNTIL, "--until")
            raise TMError("only %d past session(s) of %s have the %s bar (and the one before it); the time "
                          "machine needs at least %d%s"
                          % (len(pool[j]), sym, clock24(OPEN_MIN + 5 * j), MIN_SESSIONS,
                             ". Yahoo often leaves the last bars of the day empty: end the replay earlier, "
                             "e.g. --until %s" % DEFAULT_UNTIL if late else ""))
    cfg = tick_config(plan, config)
    ch, cost = cost_model(plan, config, qty)
    risk = plan["entry"] - plan["stop"] if plan["side"] == "buy" else plan["stop"] - plan["entry"]
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  cost_r=lambda x: cost(plan["entry"], x, risk))
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(alls), pool_from=alls[0][0], pool_to=alls[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day, cfg)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], sym, cfg, cost)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    ch = dict(ch, avg_R_net=mc["avg_R_net"], history_avg_R_net=history.get("avg_R_net"))
    last_bar = bars[-1]
    return {
        "created_by": "nse-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or sym, "exchange": "NSE", "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "charges": ch,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d sessions. It is not a prediction: results, "
                 "news and circuit limits can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(alls))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"]), ),
    ]
    ch = doc.get("charges")
    if ch:
        lo_r, hi_r = sorted(("%.2f" % ch["R_at_target"], "%.2f" % ch["R_at_stop"]), key=float)
        who = ("" if ch["qty_from"] == "--qty" else
               " (the shares that risk %s at the stop)" % inr(float(ch["qty_from"].split()[-1]), 0))
        lines.append("  Charges (%s, estimate) for %d shares%s: about %s a round trip at the target, %s at the "
                     "stop, %sR a trade." % (ch["product"], ch["qty"], who, inr(ch["at_target"]), inr(ch["at_stop"]),
                                              lo_r if lo_r == hi_r else lo_r + " to " + hi_r))
        lines.append("  Average R after charges: %+.3f." % ch["avg_R_net"])
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f%s."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"],
                            ", %+.3f after charges" % h["avg_R_net"] if h.get("avg_R_net") is not None else ""))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    short = doc["data"]["short_sessions_left_out"]
    if short:
        lines.append("Missing bars in %d session(s): %s. The replays draw those times from the other sessions; "
                     "the history leaves those days out."
                     % (len(short), "; ".join("%s (no %s)" % (x["date"], ", ".join(x["missing"][:4]) +
                                                               (" ..." if len(x["missing"]) > 4 else ""))
                                              for x in short)))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        t = times[j]
        hh, mm = int(t[:2]), t[3:]
        lab = "%d:%s" % (hh % 12 or 12, mm)
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def read_config(path, required=False):
    """trader/config.json, or {} when it is not there. Only max_loss_per_trade_inr,
    charges, tick_size and holidays are read."""
    if not path or not os.path.exists(path):
        if required:
            raise TMError("no config at %s" % path)
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise TMError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(cfg, dict):
        raise TMError("%s is not a config" % path)
    return cfg


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    config = read_config(a.config, required=a.config != "trader/config.json")
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, config, a.qty)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2, ensure_ascii=False))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /nse-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned NSE day trade (nse-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from nse-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol, e.g. ICICIBANK")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "9:35 AM"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this IST time (default: the plan's flat_by, else 15:10)")
        p.add_argument("--qty", type=int, help="shares for the charges estimate (default: the shares that risk "
                                               "max_loss_per_trade_inr at the stop)")
        p.add_argument("--config", default="trader/config.json", help="read max_loss_per_trade_inr, charges, "
                                                                      "tick_size, holidays (optional)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON or a bundle {SYMBOL: chart} "
                                                         "(.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-30 09:35" IST, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
