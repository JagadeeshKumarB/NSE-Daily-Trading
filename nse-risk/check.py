#!/usr/bin/env python3
"""Pre-trade risk check for nse-risk (NSE cash equities, intraday). Standard library only, no network.

Reads the proposed orders (trader/orders.json, Kite-shaped, from nse-budget)
and checks every one against your rules (trader/config.json), your account
(trader/portfolio.json), prices (trader/prices.json), the stock list
(trader/universe.txt, from nse-scanner) and what was placed today
(trader/state/broker.json). Each order gets PASS or BLOCK with the reasons.
Orders are checked in file order as if each PASS had filled, so buys that are
fine one by one but break a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-30 09:35"        # pin the clock (India time, IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a BUY, or a SELL
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        no symbol, transaction_type not BUY or SELL, quantity not a
                positive whole number, no exchange or product, an unknown
                order_type / variety / validity, a LIMIT without a price, an
                SL without a trigger_price, a price off NSE's tick grid; exits
                that miss the stop_loss or the target leg, or have a leg on
                the wrong side
  asset         options unless "allow_options": true; futures, other
                exchanges, crypto, Yahoo symbols (RELIANCE.NS)
  short         selling more than you hold, unless "allow_short": true
                (and never for delivery, CNC)

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  series        an MIS (intraday) order on a stock that is not in the EQ
                series (BE / BZ are trade-for-trade: delivery only). Unknown
                series (no trader/universe.txt) is a warning
  flat_by       an MIS trade whose flat_by is 15:20 or later (Zerodha's
                own square-off starts then)
  circuit       with a previous close and a numeric band: an entry within 1 %
                of today's upper circuit, or above it
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
                (MIS leverage) while "allow_margin" is false
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (exits.stop_loss or the
                order's plan stop); a stop on the wrong side of the entry
  loss          quantity x (entry - stop), plus the estimated round-trip
                charges when "count_charges_in_max_loss" is true, over
                max_loss_per_trade_inr. The entry is the price of a LIMIT or
                SL order, else the HIGHER of the plan's entry and a fresh
                price (under 15 minutes old)
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses of today's entries plus this one over
                max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST),
                or the market is closed (weekend, NSE holiday, outside
                09:15 to 15:30)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so the paper/broker step only shows an order as ready if THIS exact order
passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
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

DEFAULTS = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 100, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3, "max_orders_per_day": 10, "min_reward_risk": 1.0,
    "count_charges_in_max_loss": True, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:10", "product": "MIS",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "holidays": [], "tick_size": {}, "charges": {},
}
# The fields that make an order this exact order (Kite Connect's place_order, plus
# the two exit legs). Anything else (why, plan, notes) can change without a recheck.
KITE_FIELDS = ("symbol", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "variety", "validity", "exits")
ORDER_TYPES = {"MARKET", "LIMIT", "SL", "SL-M"}
PRODUCTS = {"MIS", "CNC"}
VARIETIES = {"regular"}
VALIDITIES = {"DAY", "IOC", "TTL"}
DERIVATIVE_EXCHANGES = {"NFO", "BFO", "MCX", "CDS", "BCD"}
SERIES_SUFFIX = {"EQ", "BE", "BZ", "BL", "SM", "ST", "IL"}   # Kite: ABAN-BE is ABAN in the BE series
SQUARE_OFF = datetime.time(15, 20)   # Zerodha squares off MIS equity positions itself from about 15:20
OPTION = re.compile(r"^[A-Z&\-]+\d{2}[A-Z0-9]{1,3}\d+(\.\d+)?(CE|PE)$")
FUTURE = re.compile(r"^[A-Z&\-]+\d{2}[A-Z]{3}FUT$")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA = 0.005
FRESH_MINUTES = 15
CIRCUIT_NEAR_PCT = 1.0
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as India time (IST)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-30 09:35\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:   # a stamp without an offset in a trader/ file is IST
        dt = dt.replace(tzinfo=IST)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an India time (IST) like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_ist, c):
    """Reasons an entry cannot be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    w = calendar_warning(d, c)
    if w:
        warns.append(w)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if d.weekday() >= 5:
        return ["window: the market is closed today (weekend, %s IST)" % stamp], warns
    if not trading_day(d, c):
        return ["window: the market is closed today (NSE holiday, %s IST)" % stamp], warns
    if t < OPEN or now_ist.time() >= CLOSE:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), CLOSE.strftime("%H:%M"))], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """The exact order that was checked: sha256 of its Kite fields, first 16 hex characters.
    Copy this function (and KITE_FIELDS) unchanged into any skill that matches verdicts."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    """₹1,23,456.50; whole rupees without the .00."""
    s = inr(x)
    return s[:-3] if s.endswith(".00") else s


def paise(x):
    return int((Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP) * 100))


def _p(x):
    return "%.2f" % float(x)


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def split_symbol(ts):
    """Kite tradingsymbol -> (NSE symbol, series from a suffix or None): ABAN-BE -> (ABAN, BE)."""
    head, sep, tail = ts.rpartition("-")
    if sep and head and tail in SERIES_SUFFIX:
        return head, tail
    return ts, None


def account(cfg, portfolio, prices, budget_given):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        if not budget_given:
            warnings.append("no trader/portfolio.json and no budget in trader/config.json: checked against "
                            "the default %s budget" % money(budget))
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number (rupees)")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = from_yahoo(str(p.get("ticker") or p.get("symbol") or "").upper())
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty", p.get("quantity"))))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_side(rec):
    return str(rec.get("side") or rec.get("transaction_type") or "").lower()


def rec_is_entry(rec):
    """The broker/paper log keeps the kind nse-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return rec_side(rec) == "buy"


def placed_today(state, today):
    """Orders recorded as placed today in trader/state/broker.json."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def rec_loss(rec, with_charges):
    """The max loss a placed entry was logged with: max_loss_with_charges when charges count, else max_loss."""
    if with_charges and _f(rec.get("max_loss_with_charges")) is not None:
        return _f(rec["max_loss_with_charges"])
    return _f(rec.get("max_loss"))


def load_universe(path):
    """trader/universe.txt (nse-scanner): SYMBOL|Name|Series|Band a line -> {SYM: (series, band)}.
    None when there is no file."""
    if not path or not os.path.exists(path):
        return None
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split("|")]
            sym = parts[0].upper()
            series = (parts[2].upper() if len(parts) > 2 else "") or None
            band = (parts[3] if len(parts) > 3 else "") or None
            out[sym] = (series, band)
    return out


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time, naive UTC or None}, {SYM: previous close}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of. The previous close is detail.prev_close."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}
    out, times, prev = {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = {from_yahoo(str(k).upper()): v for k, v in (pdoc.get("detail") or {}).items() if isinstance(v, dict)}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = from_yahoo(str(s).upper())
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    for s, d in detail.items():
        pc = _f(d.get("prev_close", d.get("previous_close")))
        if pc:
            prev[s] = pc
    return out, times, prev


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          universe=None, prev_close=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    universe     {SYM: (series, band)} from trader/universe.txt; None = no file
    prev_close   {SYM: previous close in rupees}, for the circuit check"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {from_yahoo(str(k).upper()): float(v) for k, v in (prices or {}).items() if _f(v)}
    prev_close = {from_yahoo(str(k).upper()): float(v) for k, v in (prev_close or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices, "budget" in (cfg or {}))
    blocked = {from_yahoo(str(b).upper()) for b in c.get("blocked") or []}
    with_charges = bool(c.get("count_charges_in_max_loss", True))
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = rec_loss(r, with_charges)
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (rec_side(r), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(from_yahoo(str(r.get("symbol", "")).upper()), rec_side(r)) for r in done_today}
    win_reasons, win_warns = window_check(now_ist, c)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    def tick_of(sym, price):
        return float(tick_size(price, sym, c))

    def on_tick(sym, price):
        return abs(float(to_tick(price, "nearest", sym, c)) - price) < 1e-6

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        if not isinstance(o, dict):
            o = {}
            reasons.append("format: an order must be a JSON object")
        raw_sym = str(o.get("symbol") or "").strip().upper()
        s_sym = from_yahoo(raw_sym)
        ts = str(o.get("tradingsymbol") or "").strip().upper()
        ts_base, ts_series = split_symbol(from_yahoo(ts)) if ts else ("", None)
        sym = ts_base or s_sym
        tt = str(o.get("transaction_type") or "").strip().upper()
        side = tt.lower()
        exch = str(o.get("exchange") or "").strip().upper()
        product = str(o.get("product") or "").strip().upper()
        typ = str(o.get("order_type") or "").strip().upper()
        variety = str(o.get("variety") or "regular").strip().lower()
        validity = str(o.get("validity") or "DAY").strip().upper()
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        exits = o.get("exits")
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol (give symbol and tradingsymbol, e.g. \"ICICIBANK\")")
        if s_sym and ts_base and s_sym != ts_base:
            reasons.append("format: symbol %s and tradingsymbol %s do not match" % (s_sym, ts))
        if tt not in ("BUY", "SELL"):
            reasons.append("format: transaction_type must be BUY or SELL, got %r" % o.get("transaction_type"))
        qv = o.get("quantity")
        q = None
        if qv is None or isinstance(qv, bool):
            reasons.append("format: no quantity (a whole number of shares)")
        else:
            qf = _f(qv)
            if qf is None or qf <= 0:
                reasons.append("format: quantity must be a positive whole number, got %r" % qv)
            elif abs(qf - round(qf)) > 1e-9:
                reasons.append("format: whole shares only (no fractional shares in India), got quantity %r" % qv)
            else:
                q = int(round(qf))
                if not isinstance(qv, int):
                    warns.append("send quantity as a whole number (%d), the way Kite's place_order types it" % q)
        if not exch:
            reasons.append("format: no exchange (\"NSE\")")
        if not product:
            reasons.append("format: no product (\"MIS\" for intraday, \"CNC\" for delivery)")
        elif product not in PRODUCTS and product != "NRML":
            reasons.append("format: unknown product %r (MIS for intraday, CNC for delivery)" % product)
        if typ not in ORDER_TYPES:
            reasons.append("format: unknown order_type %r (MARKET, LIMIT, SL or SL-M)" % o.get("order_type"))
        if variety not in VARIETIES:
            reasons.append("format: variety %r is not used here: only \"regular\" orders (Zerodha has no "
                           "bracket orders; the exits are separate orders)" % variety)
        if validity not in VALIDITIES:
            reasons.append("format: unknown validity %r (DAY, IOC or TTL)" % validity)
        elif validity == "TTL" and not o.get("validity_ttl"):
            reasons.append("format: validity TTL needs validity_ttl (minutes)")
        for k in ("price", "trigger_price"):
            if o.get(k) is None:
                continue
            v = _f(o.get(k))
            if v is None or v <= 0:
                reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                continue
            if not isinstance(o.get(k), str):
                warns.append("send %s as a string (\"%s\"), the way orders.json keeps prices" % (k, _p(v)))
            if typ == "MARKET" and k == "price":
                warns.append("a MARKET order ignores its price %s" % _p(v))
            elif sym and not on_tick(sym, v):
                reasons.append("format: %s %s is not on NSE's %s tick for %s (the exchange rejects it); "
                               "nearest is %s" % (k, _p(v), _num(tick_of(sym, v)), sym,
                                                  _p(to_tick(v, "nearest", sym, c))))
        if typ == "LIMIT" and o.get("price") is None:
            reasons.append("format: a LIMIT order needs a price")
        if typ in ("SL", "SL-M") and o.get("trigger_price") is None:
            reasons.append("format: an %s order needs a trigger_price" % typ)
        if typ == "SL" and o.get("price") is None:
            reasons.append("format: an SL order needs a price (the limit) as well as a trigger_price")
        if typ == "SL" and _f(o.get("price")) and _f(o.get("trigger_price")):
            pr, tg = _f(o["price"]), _f(o["trigger_price"])
            if (tt == "BUY" and pr < tg - PAISA) or (tt == "SELL" and pr > tg + PAISA):
                reasons.append("format: an SL %s's price %s must be %s its trigger %s"
                               % (tt, _p(pr), "at or above" if tt == "BUY" else "at or below", _p(tg)))

        # the exit legs (Zerodha has no bracket order: separate SL and LIMIT orders after the fill)
        leg_stop = leg_stop_limit = leg_target = None
        if exits is not None:
            other = {"BUY": "SELL", "SELL": "BUY"}.get(tt)
            if not isinstance(exits, dict):
                reasons.append("format: exits must be an object with stop_loss and target")
                exits = {}
            sl, tg = exits.get("stop_loss"), exits.get("target")
            if not isinstance(sl, dict) or not isinstance(tg, dict):
                reasons.append("format: exits needs both a stop_loss and a target leg")
            if isinstance(sl, dict):
                slt = str(sl.get("order_type") or "").upper()
                if other and str(sl.get("transaction_type") or "").upper() != other:
                    reasons.append("format: the stop_loss leg must be a %s for a %s entry" % (other, tt))
                if slt not in ("SL", "SL-M"):
                    reasons.append("format: the stop_loss leg must be an SL or SL-M order, got %r"
                                   % sl.get("order_type"))
                leg_stop, leg_stop_limit = _f(sl.get("trigger_price")), _f(sl.get("price"))
                if not leg_stop or leg_stop <= 0:
                    reasons.append("format: the stop_loss leg needs a positive trigger_price")
                    leg_stop = None
                if slt == "SL" and not leg_stop_limit:
                    reasons.append("format: the stop_loss leg is an SL order: it needs a price (the limit)")
                if leg_stop and leg_stop_limit and other and (
                        (other == "SELL" and leg_stop_limit > leg_stop + PAISA) or
                        (other == "BUY" and leg_stop_limit < leg_stop - PAISA)):
                    reasons.append("format: the stop_loss leg's price %s must be %s its trigger %s"
                                   % (_p(leg_stop_limit), "at or below" if other == "SELL" else "at or above",
                                      _p(leg_stop)))
            if isinstance(tg, dict):
                if other and str(tg.get("transaction_type") or "").upper() != other:
                    reasons.append("format: the target leg must be a %s for a %s entry" % (other, tt))
                if str(tg.get("order_type") or "").upper() != "LIMIT":
                    reasons.append("format: the target leg must be a LIMIT order, got %r" % tg.get("order_type"))
                leg_target = _f(tg.get("price"))
                if not leg_target or leg_target <= 0:
                    reasons.append("format: the target leg needs a positive price")
                    leg_target = None
            for name, leg in (("stop_loss", sl), ("target", tg)):
                if not isinstance(leg, dict):
                    continue
                lq = leg.get("quantity")
                if lq is not None and q is not None and _f(lq) != q:
                    reasons.append("format: the %s leg's quantity %s is not the entry's %d" % (name, lq, q))
                for k in ("price", "trigger_price"):
                    v = _f(leg.get(k))
                    if leg.get(k) is None or v is None or v <= 0:
                        continue
                    if not isinstance(leg.get(k), str):
                        warns.append("send the %s leg's %s as a string (\"%s\")" % (name, k, _p(v)))
                    if sym and not on_tick(sym, v):
                        reasons.append("format: the %s leg's %s %s is not on NSE's %s tick for %s; nearest is %s"
                                       % (name, k, _p(v), _num(tick_of(sym, v)), sym,
                                          _p(to_tick(v, "nearest", sym, c))))

        # asset: NSE cash equities only
        option = bool(sym) and bool(OPTION.match(sym))
        if option:
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option contract; options are off (\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100%% of what you pay")
        elif sym and FUTURE.match(sym):
            reasons.append("asset: %s is a futures contract; this pack trades NSE stocks and ETFs only" % sym)
        elif raw_sym.endswith((".NS", ".BO")) or ts.endswith((".NS", ".BO")):
            reasons.append("asset: use the plain NSE symbol %s, not Yahoo's %s" % (sym, raw_sym or ts))
        elif sym and ("/" in sym or not SYMBOL.match(sym)):
            reasons.append("asset: %s does not look like an NSE stock symbol; this pack trades NSE stocks "
                           "and ETFs only" % sym)
        if exch in DERIVATIVE_EXCHANGES and not (option and c.get("allow_options")):
            reasons.append("asset: exchange %s is for futures and options; this pack trades NSE cash "
                           "equities only" % exch)
        elif exch and exch not in DERIVATIVE_EXCHANGES and exch != "NSE":
            reasons.append("asset: exchange %s: this pack trades on NSE only" % exch)
        if product == "NRML" and not (option and c.get("allow_options")):
            reasons.append("asset: product NRML is for futures and options; use MIS (intraday) or CNC (delivery)")

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("LIMIT", "SL"):
            ref, ref_from = _f(o.get("price")), "limit"
        elif typ == "SL-M":
            ref, ref_from = _f(o.get("trigger_price")), "trigger"
        if ref is not None and ref <= 0:
            ref = None
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                pick = max if tt != "SELL" else min
                ref, ref_from = pick(cands, key=lambda x: x[0])
                ref = float(to_tick(ref, "up" if tt != "SELL" else "down", sym, c))
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(live), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "MARKET":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if tt == "SELL" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif tt == "BUY" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %d-share order; give the order a price, or add "
                         "%s to trader/prices.json" % (sym, q, sym))

        # blocked list, series, flat_by, size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        series, band = (universe or {}).get(sym, (None, None))
        if ts_series:
            series = ts_series
        if product == "MIS" and sym:
            if series is None:
                later_warn = ("series: %s's series and circuit band are unknown (%s): check on NSE that it is "
                              "EQ before an MIS trade; BE and BZ stocks cannot be traded intraday"
                              % (sym, "not in trader/universe.txt" if universe is not None
                                 else "no trader/universe.txt; nse-scanner writes it"))
                if kind == "entry":
                    warns.append(later_warn)
            elif series != "EQ":
                later.append("series: %s is in the %s series (trade-for-trade): every trade must be delivered, "
                             "so no MIS intraday. Only EQ stocks can be day traded" % (sym, series))
            fb_raw = plan.get("flat_by") or c.get("flat_by")
            try:
                fb = hhmm(fb_raw, "flat_by")
            except RiskError as e:
                fb = None
                later.append("flat_by: " + str(e))
            if fb is not None and fb >= SQUARE_OFF:
                later.append("flat_by: %s is at or after 15:20, when Zerodha starts squaring off MIS "
                             "positions itself (with a %s + GST charge); be flat by 15:10"
                             % (fb.strftime("%H:%M"), money(50)))
        elif product == "CNC" and kind == "entry":
            warns.append("product CNC is a delivery buy, not a day trade: the shares go to your demat "
                         "and the charges are delivery's")
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and tt == "BUY" and kind == "entry":
            after_cash = cash - value
            if after_cash < -PAISA and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (MIS leverage, money "
                             "borrowed from the broker) and \"allow_margin\" is false" % (money(value), money(cash)))
            elif after_cash < -PAISA:
                warns.append("margin: needs %s but only %s cash; %s of it is MIS leverage (\"allow_margin\": "
                             "true): a loss can be bigger than your cash" % (money(value), money(cash),
                                                                             money(-after_cash)))
            elif after_cash < buffer_inr - PAISA:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if tt == "SELL" and sym and kind == "entry":
            over = (q > held + 1e-9) if q is not None else held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first" % (sym, _num(held)))
            elif over and product == "CNC":
                reasons.append("short: a delivery (CNC) sell needs the shares in your demat; in India you can "
                               "only short sell intraday (MIS)")

        # day-trade checks: stop, loss (with charges), reward:risk, daily loss, trades, window, circuit
        max_loss = risk_ps = rr = stop = target = ch_stop = ch_target = ml_c = None
        daily = cap_t = None
        if kind == "entry" and tt in ("BUY", "SELL"):
            long = tt == "BUY"
            plan_stop = _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = leg_target or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA:
                warns.append("the stop_loss trigger %s differs from the plan's stop %s: the order's is the one "
                             "that gets placed" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only: there is no stop_loss order with this entry. "
                             "Place an SL %s right after the fill" % (_p(stop), "SELL" if long else "BUY"))
            prod = product if product in PRODUCTS else "MIS"
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): an "
                                 "exits.stop_loss leg, from nse-exit and nse-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), tt))
                else:
                    risk_p = abs(paise(ref) - paise(stop))
                    risk_ps = risk_p / 100.0
                    shares = q or 0
                    max_loss = round(shares * risk_p / 100.0, 2)
                    if long:
                        ch_stop = charges(shares * ref, shares * stop, prod, c)["total"]
                    else:
                        ch_stop = charges(shares * stop, shares * ref, prod, c)["total"]
                    ml_c = round(max_loss + ch_stop, 2)
                    counted = ml_c if with_charges else max_loss
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and counted > cap + PAISA:
                        if with_charges:
                            later.append("loss: %d shares x %s risk a share = %s at the stop, plus %s estimated "
                                         "charges = %s, over your %s max loss per trade"
                                         % (shares, _p(risk_ps), money(max_loss), money(ch_stop), money(ml_c),
                                            money(cap)))
                        else:
                            later.append("loss: %d shares x %s risk a share = %s at the stop, over your %s max "
                                         "loss per trade" % (shares, _p(risk_ps), money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(abs(paise(target) - paise(ref)) / float(risk_p), 2)
                            if long:
                                ch_target = charges(shares * ref, shares * target, prod, c)["total"]
                            else:
                                ch_target = charges(shares * target, shares * ref, prod, c)["total"]
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            counted = (ml_c if with_charges else max_loss) if max_loss is not None else None
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and counted is not None:
                used = loss_today + batch_loss
                if used + counted > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(counted), money(used + counted),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)

            # circuit limits: a stock may move only its band % from the previous close in a day
            num_band = _f(str(band).rstrip("%")) if band else None
            pc = prev_close.get(sym)
            if num_band and pc and ref:
                upper = float(to_tick(pc * (1 + num_band / 100.0), "nearest", sym, c))
                lower = float(to_tick(pc * (1 - num_band / 100.0), "nearest", sym, c))
                near = upper * (1 - CIRCUIT_NEAR_PCT / 100.0)
                if long and ref > upper + PAISA:
                    later.append("circuit: the entry %s is above today's upper circuit %s (%s%% band from the "
                                 "previous close %s): NSE rejects it" % (_p(ref), _p(upper), _num(num_band),
                                                                         _p(pc)))
                elif long and ref >= near - PAISA:
                    later.append("circuit: the entry %s is within %s%% of today's upper circuit %s (%s%% band "
                                 "from the previous close %s). A stock at its upper circuit can lock: few or no "
                                 "sellers, and a sharp fall when it opens up" % (_p(ref), _num(CIRCUIT_NEAR_PCT),
                                                                                  _p(upper), _num(num_band), _p(pc)))
                if long and stop and stop < lower - PAISA:
                    warns.append("the stop %s is below today's lower circuit %s: it cannot trigger there"
                                 % (_p(stop), _p(lower)))
                info.append("circuit: %s%% band from the previous close %s: lower %s, upper %s"
                            % (_num(num_band), _p(pc), _p(lower), _p(upper)))
            elif num_band and ref:
                warns.append("circuit: %s has a %s%% band but no previous close (prices.json detail.%s.prev_close): "
                             "the circuit check did not run" % (sym, _num(num_band), sym))
            elif band and str(band).strip().lower() == "no band":
                info.append("circuit: no fixed band (an F&O stock: NSE moves its band during the day); not checked")

            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                lim = _f(c.get("max_loss_per_trade_inr"))
                info.append("max loss %s + %s charges = %s%s, reward:risk %s%s"
                            % (money(max_loss), money(ch_stop), money(ml_c),
                               (" (limit %s%s)" % (money(lim), "" if with_charges else ", charges not counted"))
                               if lim else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
                if rr is not None and ch_target is not None:
                    gain = round((q or 0) * abs(paise(target) - paise(ref)) / 100.0, 2)
                    net = round(gain - ch_target, 2)
                    info.append("at the target: +%s - %s charges = %s%s"
                                % (money(gain), money(ch_target), "+" if net >= 0 else "", money(net)))
            if leg_stop and leg_target:
                info.append("after the entry fills: place the SL %s (trigger %s%s) and the LIMIT %s %s; cancel the "
                            "other the moment one fills (Zerodha has no bracket order for this)"
                            % ("SELL" if long else "BUY", _p(leg_stop),
                               (", limit %s" % _p(leg_stop_limit)) if leg_stop_limit else "",
                               "SELL" if long else "BUY", _p(leg_target)))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (tt, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (tt, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and tt in ("BUY", "SELL"):
                batch_entries += 1
                if max_loss is not None:
                    batch_loss += ml_c if with_charges else max_loss
            if q is not None:
                if tt == "BUY":
                    if value is not None:
                        acct["cash"] -= value
                        acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                    acct["shares"][sym] = held + q
                elif tt == "SELL":
                    if value is not None:
                        acct["cash"] += value
                        acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                    acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "transaction_type": tt, "product": product,
            "kind": kind, "quantity": q, "amount": "%s shares" % (q if q is not None else qv),
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "charges_at_stop": ch_stop, "max_loss_with_charges": ml_c, "charges_at_target": ch_target,
            "reward_risk": rr, "series": series, "band": band, "fingerprint": fingerprint(o),
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s%s, %s" % (r["verdict"], r["index"], r["transaction_type"] or "?",
                                                  r["symbol"], r["amount"],
                                                  (" " + r["product"]) if r["product"] else "", r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. Nothing is placed until you say yes to each one.")
    lines.append("Charges are estimates (Zerodha's rates); the contract note has the real numbers.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (nse-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--universe", default="trader/universe.txt", help="SYMBOL|Name|Series|Band (nse-scanner)")
    ap.add_argument("--state", default="trader/state/broker.json", help="what was placed today")
    ap.add_argument("--now", help="pretend it is this India time (IST), e.g. \"2026-10-30 09:35\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-30")
        doc = read_json(a.orders, "orders file (run nse-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, prev = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        universe = load_universe(a.universe)
        try:
            res = check(orders, cfg, portfolio, prices, state, a.today, now, times, universe, prev)
        except ValueError as e:   # e.g. a bad date in config "holidays"
            raise RiskError("config: %s" % e)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
