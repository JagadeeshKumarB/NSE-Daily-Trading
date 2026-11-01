#!/usr/bin/env python3
"""Paper Trader for nse-paper: practice an order on real NSE prices, no real money. Standard library only.

    python3 paper.py show                                # the orders in trader/orders.json, READY or not
    python3 paper.py place 1                             # the ticket for order 1, and what a yes does
    python3 paper.py place 1 --yes                       # record order 1 as a paper trade (after the user said yes)
    python3 paper.py settle                              # replay open paper trades on Yahoo's 5-minute bars
    python3 paper.py settle --offline bundle.json.gz --now "2026-10-30 15:30"
    python3 paper.py journal                             # every paper trade, wins, losses, charges, net

place     An order is READY only when nse-risk passed THIS exact order today
          (same fingerprint in trader/state/risk.json, dated today in IST) and
          config "paper" is true. --yes records it: a paper trade in
          trader/state/paper.json and a "placed" entry in
          trader/state/broker.json, so nse-risk counts it against your trades
          per day and your daily loss. Pass --yes only after the user typed yes
          for that one order in the chat.

settle    Replays each open paper trade on the day's 5-minute bars (IST), from
          the first bar that starts at or after the time it was placed:
            entry   LIMIT: the first bar whose low reaches the limit fills at
                    the limit (or the bar's open, if it opened below it).
                    MARKET: the first bar's open. A LIMIT entry still unfilled
                    at no_entries_after (14:30) is cancelled.
            stop    a bar whose low reaches the trigger fills at the trigger
                    (or the open, on a gap down). The fill bar itself counts
                    for the stop, never for the target (the order of the
                    moves inside a bar is unknown).
            target  a bar whose high reaches the target fills at the target
                    (or the open, on a gap up).
            both    stop and target in the same bar: counted as the stop.
            flat    still open at flat_by (15:10): sold at the open of the
                    first bar at or after flat_by, else the last close.
          Profit and loss is net of Zerodha's estimated charges (the India
          block). A trade on a day still in progress stays open, marked to the
          last close.

The simulation is kinder than a real market in one way and harsher in
another: a real SL limit order can be skipped by a gap (you would still hold
the shares), and a real limit buy can be missed by a tick. Paper results are
practice, not proof.

Exit code 0 on success, 1 when nothing was done (not READY, no yes, nothing open).
"""

import argparse
import datetime
import gzip
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 india-day-trader-skill")
HOSTS = ("query2", "query1")
BAR = datetime.timedelta(minutes=5)
DEFAULTS = {"paper": True, "no_entries_after": "14:30", "flat_by": "15:10", "product": "MIS"}
NOTE = "Paper trading: no real order, no real money. Practice, not proof."


class PaperError(Exception):
    pass


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


# ---------------------------------------------------------------- shared with nse-risk (copied unchanged)
KITE_FIELDS = ("symbol", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "variety", "validity", "exits")


def fingerprint(order):
    """The exact order that was checked: sha256 of its Kite fields, first 16 hex characters.
    Copy this function (and KITE_FIELDS) unchanged into any skill that matches verdicts."""
    core = {k: order[k] for k in KITE_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- files
def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as e:
            raise PaperError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path):
    cfg = dict(DEFAULTS)
    cfg.update(read_json(path, {}) or {})
    return cfg


def parse_now(text):
    """--now in IST ("2026-10-30 09:40"), or the clock."""
    if not text:
        return to_ist(datetime.datetime.now(datetime.timezone.utc))
    t = text.strip().replace("T", " ")
    if t.upper().endswith(" IST"):
        t = t[:-4]
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            pass
    raise PaperError("--now wants IST like \"2026-10-30 09:40\", not %r" % text)


def hhmm(text):
    h, m = str(text).split(":")[:2]
    return datetime.time(int(h), int(m))


def clock(t):
    """datetime.time -> '9:40 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def money(x):
    return inr(x)


def signed(x):
    return ("+" if x > 0 else "") + inr(x)


# ---------------------------------------------------------------- orders and the risk verdict
def entry_orders(doc):
    return list((doc or {}).get("orders") or [])


def verdict_for(order, idx, risk, today):
    """(ready, why, result)."""
    if not risk:
        return False, "nse-risk has not checked these orders (run check.py)", None
    if risk.get("date") != today:
        return False, "the risk check is from %s, not today (%s): run nse-risk again" % (risk.get("date"), today), None
    fp = fingerprint(order)
    for r in risk.get("results") or []:
        if r.get("fingerprint") == fp:
            if r.get("verdict") != "PASS":
                return False, "BLOCKED by nse-risk: " + "; ".join(r.get("reasons") or []), r
            return True, "READY: nse-risk PASS today", r
    return False, "this exact order was not in today's risk check (it changed or is new): run nse-risk again", None


def is_entry(order):
    return str(order.get("transaction_type", "")).upper() == "BUY"


def ticket(order, idx, total, why):
    q = int(order.get("quantity") or 0)
    how = "at the market" if order.get("order_type") == "MARKET" else "with a limit of %s" % order.get("price")
    ex = order.get("exits") or {}
    sl, tg = ex.get("stop_loss") or {}, ex.get("target") or {}
    plan = order.get("plan") or {}
    lines = ["Order %d of %d: %s" % (idx, total, why),
             "  BUY %d %s on %s, %s (%s), %s." % (q, order.get("symbol"), order.get("exchange", "NSE"),
                                                   order.get("product", "MIS"),
                                                   "intraday" if order.get("product", "MIS") == "MIS" else "delivery",
                                                   how)]
    if sl:
        lines.append("  Stop: SL SELL, trigger %s, limit %s." % (sl.get("trigger_price"), sl.get("price")))
    if tg:
        lines.append("  Target: LIMIT SELL %s." % tg.get("price"))
    if plan.get("flat_by"):
        lines.append("  Flat by %s IST, win or lose." % plan["flat_by"])
    if order.get("max_loss_with_charges") is not None:
        lines.append("  Most it should lose at the stop: %s with charges." % money(order["max_loss_with_charges"]))
    return lines


# ---------------------------------------------------------------- bars
def yahoo_chart(sym, day, today):
    age = (today - day).days
    rng = "1d" if age == 0 else ("5d" if age < 5 else "60d")
    path = "/v8/finance/chart/%s?range=%s&interval=5m" % (urllib.parse.quote(to_yahoo(sym)), rng)
    last = "no answer"
    for host in HOSTS:
        req = urllib.request.Request("https://%s.finance.yahoo.com%s" % (host, path),
                                     headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
    raise PaperError("Yahoo did not answer for %s (%s): try again in a minute" % (sym, last))


def offline_chart(path, sym):
    data = read_json(path)
    if data is None:
        raise PaperError("no offline file %s" % path)
    if isinstance(data, dict) and "chart" not in data:
        for key in (sym, to_yahoo(sym)):
            if key in data:
                return data[key]
        raise PaperError("the offline file has no %s" % sym)
    return data


def day_bars(chart, day, now):
    """Completed 5-minute bars of one IST day: [(start, o, h, l, c)]."""
    try:
        r = chart["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise PaperError("Yahoo returned no chart")
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}
    out = []
    for i, ts in enumerate(r.get("timestamp") or []):
        c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
        if c is None:
            continue
        start = ts_to_ist(ts)
        if start.date() != day or start.second or start.minute % 5:
            continue
        if start + BAR > now:
            continue        # still forming
        o, h, lo = (q.get(k)[i] if q.get(k) and i < len(q.get(k)) else None for k in ("open", "high", "low"))
        out.append((start, float(o if o is not None else c), float(h if h is not None else c),
                    float(lo if lo is not None else c), float(c)))
    return out


# ---------------------------------------------------------------- the replay
def replay(trade, bars, now, cfg):
    """Walk the bars. Returns a dict of what happened (pure)."""
    placed = datetime.datetime.strptime(trade["date"] + " " + trade["placed_at"], "%Y-%m-%d %H:%M")
    day = placed.date()
    cutoff = datetime.datetime.combine(day, hhmm(trade.get("no_entries_after") or cfg["no_entries_after"]))
    flat = datetime.datetime.combine(day, hhmm(trade.get("flat_by") or cfg["flat_by"]))
    limit, stop, sl_limit, target = (trade.get(k) for k in ("limit", "stop", "sl_limit", "target"))
    fill = None
    notes = []
    last = None
    for start, o, h, lo, c in bars:
        if start < placed:
            continue
        last = (start, c)
        if fill is None:
            if start >= cutoff:
                return {"status": "cancelled", "how": "not filled by %s" % clock(cutoff.time()), "notes": notes}
            if limit is None:
                fill = (start, o)
            elif lo <= limit:
                fill = (start, min(limit, o))
            else:
                continue
            if lo <= stop:      # the fill bar counts for the stop only
                px = min(stop, o) if o < stop else stop
                return finish(fill, (start, px, "stop"), sl_limit, notes, "the stop was hit in the bar that filled the entry")
            continue
        if start >= flat:
            return finish(fill, (start, o, "flat"), sl_limit, notes)
        hit_stop, hit_target = lo <= stop, h >= target
        if hit_stop and hit_target:
            return finish(fill, (start, min(stop, o), "stop"), sl_limit, notes,
                          "stop and target in the same bar: counted as the stop")
        if hit_stop:
            return finish(fill, (start, o if o < stop else stop, "stop"), sl_limit, notes)
        if hit_target:
            return finish(fill, (start, o if o > target else target, "target"), sl_limit, notes)
    session_over = now >= flat
    if fill is None:
        if now >= cutoff:
            return {"status": "cancelled", "how": "not filled by %s" % clock(cutoff.time()), "notes": notes}
        return {"status": "waiting", "notes": notes}
    if session_over and last:
        return finish(fill, (last[0], last[1], "flat"), sl_limit, notes,
                      "no bar at %s in Yahoo's data: sold at the last close" % clock(flat.time()))
    return {"status": "open", "fill": fill, "last": last, "notes": notes}


def finish(fill, exit_, sl_limit, notes, note=None):
    if note:
        notes.append(note)
    if exit_[2] == "stop" and sl_limit is not None and exit_[1] < sl_limit:
        notes.append("the price gapped below your SL limit %.2f: a real SL limit order may not have filled, "
                     "and you could still be holding the shares" % sl_limit)
    return {"status": "closed", "fill": fill, "exit": exit_, "notes": notes}


def settle_one(trade, bars, now, cfg):
    res = replay(trade, bars, now, cfg)
    q = trade["quantity"]
    trade["notes"] = res["notes"]
    trade["status"] = res["status"]
    if res.get("fill"):
        t, px = res["fill"]
        px = float(to_tick(px, "nearest", trade["symbol"], cfg))
        trade["entry_fill"] = {"time": t.strftime("%H:%M"), "price": px}
    if res["status"] == "closed":
        t, px, how = res["exit"]
        px = float(to_tick(px, "nearest", trade["symbol"], cfg))
        entry = trade["entry_fill"]["price"]
        ch = charges(q * entry, q * px, trade.get("product", "MIS"), cfg)
        gross = round(q * (px - entry), 2)
        trade["exit"] = {"time": t.strftime("%H:%M"), "price": px, "how": how}
        trade.update(gross=gross, charges=ch, net=round(gross - ch["total"], 2))
    elif res["status"] == "open":
        t, c = res["last"] or (None, trade["entry_fill"]["price"])
        trade["mark"] = {"time": t.strftime("%H:%M") if t else None, "price": c,
                         "unrealised": round(q * (c - trade["entry_fill"]["price"]), 2)}
    elif res["status"] == "cancelled":
        trade["exit"] = {"how": res["how"]}
    return trade


# ---------------------------------------------------------------- commands
def cmd_show(a, cfg, now):
    orders = entry_orders(read_json(a.orders))
    if not orders:
        print("No orders in %s: run /nse-budget first." % a.orders)
        return 1
    risk = read_json(a.risk)
    today = now.date().isoformat()
    for i, o in enumerate(orders, 1):
        ready, why, _ = verdict_for(o, i, risk, today) if is_entry(o) else (False, "not a buy entry", None)
        print("\n".join(ticket(o, i, len(orders), why)))
        print()
    if not cfg.get("paper", True):
        print("Config \"paper\" is false: nse-paper only practises. Set it to true to paper trade.")
    print(NOTE)
    return 0


def cmd_place(a, cfg, now):
    if not cfg.get("paper", True):
        raise PaperError("config \"paper\" is false: nse-paper only paper trades. Real orders are a later skill")
    orders = entry_orders(read_json(a.orders))
    if not 1 <= a.index <= len(orders):
        raise PaperError("there is no order %d in %s (it has %d)" % (a.index, a.orders, len(orders)))
    order = orders[a.index - 1]
    if not is_entry(order):
        raise PaperError("order %d is not a BUY entry: nse-paper practises long entries only" % a.index)
    today = now.date().isoformat()
    if not trading_day(now.date(), cfg):
        raise PaperError("NSE is closed on %s" % today)
    ready, why, result = verdict_for(order, a.index, read_json(a.risk), today)
    print("\n".join(ticket(order, a.index, len(orders), why)))
    if not ready:
        print("Not recorded.")
        return 1
    fp = fingerprint(order)
    book = read_json(a.book, {"trades": []}) or {"trades": []}
    if any(t.get("fingerprint") == fp and t.get("date") == today for t in book["trades"]):
        print("Already recorded today as a paper trade: not recorded twice.")
        return 1
    if not a.yes:
        print("To record it, the user types yes for THIS order in the chat; then run: paper.py place %d --yes" % a.index)
        return 1
    ex = order.get("exits") or {}
    plan = order.get("plan") or {}
    tid = "P%s-%s-%s" % (now.strftime("%Y%m%d-%H%M"), order["symbol"], fp[:6])
    trade = {
        "id": tid, "date": today, "placed_at": now.strftime("%H:%M"), "symbol": order["symbol"],
        "exchange": order.get("exchange", "NSE"), "product": order.get("product", "MIS"),
        "quantity": int(order["quantity"]), "order_type": order.get("order_type", "LIMIT"),
        "limit": None if order.get("order_type") == "MARKET" else float(order["price"]),
        "stop": float(ex["stop_loss"]["trigger_price"]), "sl_limit": _f(ex["stop_loss"].get("price")),
        "target": float(ex["target"]["price"]), "flat_by": plan.get("flat_by") or cfg["flat_by"],
        "no_entries_after": cfg["no_entries_after"], "fingerprint": fp,
        "max_loss_with_charges": order.get("max_loss_with_charges"), "status": "waiting",
    }
    book["trades"].append(trade)
    write_json(a.book, book)
    broker = read_json(a.broker, {"orders": []}) or {"orders": []}
    broker.setdefault("orders", []).append({
        "id": tid, "at": ist_to_utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today,
        "status": "placed", "paper": True, "kind": "entry", "side": "buy", "transaction_type": "BUY",
        "symbol": order["symbol"], "quantity": int(order["quantity"]), "fingerprint": fp,
        "max_loss": order.get("max_loss"), "max_loss_with_charges": order.get("max_loss_with_charges"),
        "order": {k: order[k] for k in KITE_FIELDS if k in order},
    })
    write_json(a.broker, broker)
    print("Recorded paper trade %s at %s IST. Nothing was sent to Zerodha." % (tid, clock(now.time())))
    print("Next: paper.py settle (after a few bars, or after the close) replays it on real prices.")
    return 0


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def describe(t):
    s = t["status"]
    head = "%s  %s %d %s" % (t["id"], t["date"], t["quantity"], t["symbol"])
    if s == "waiting":
        return head + ": waiting for the entry to fill"
    if s == "cancelled":
        return head + ": cancelled, %s" % t["exit"]["how"]
    ef = t["entry_fill"]
    if s == "open":
        m = t["mark"]
        return head + ": open, bought %.2f at %s, last %.2f (%s): %s so far, before charges" % (
            ef["price"], clock(hhmm(ef["time"])), m["price"], m["time"], signed(m["unrealised"]))
    ex = t["exit"]
    word = {"target": "the target", "stop": "the stop", "flat": "flat by %s" % t["flat_by"]}[ex["how"]]
    return head + ": bought %.2f at %s, sold %.2f at %s (%s): %s gross, %s charges, %s net" % (
        ef["price"], clock(hhmm(ef["time"])), ex["price"], clock(hhmm(ex["time"])), word,
        signed(t["gross"]), money(t["charges"]["total"]), signed(t["net"]))


def cmd_settle(a, cfg, now):
    book = read_json(a.book, {"trades": []}) or {"trades": []}
    todo = [t for t in book["trades"] if t["status"] in ("waiting", "open")]
    if not todo:
        print("No open paper trades.")
        return 1
    broker = read_json(a.broker, {"orders": []}) or {"orders": []}
    for t in todo:
        day = datetime.date.fromisoformat(t["date"])
        chart = offline_chart(a.offline, t["symbol"]) if a.offline else yahoo_chart(t["symbol"], day, now.date())
        settle_one(t, day_bars(chart, day, now), now, cfg)
        print(describe(t))
        for n in t.get("notes") or []:
            print("    note: " + n)
        if t["status"] == "closed":
            broker.setdefault("orders", []).append({
                "id": t["id"] + "-X", "at": ist_to_utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"), "date": t["date"],
                "status": "filled", "paper": True, "kind": "exit", "side": "sell", "transaction_type": "SELL",
                "symbol": t["symbol"], "quantity": t["quantity"], "how": t["exit"]["how"],
                "price": t["exit"]["price"], "pnl_inr": t["net"],
            })
    write_json(a.book, book)
    write_json(a.broker, broker)
    print("Bars: Yahoo 5-minute, free and unofficial. " + NOTE)
    return 0


def summary(trades):
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if t["net"] > 0]
    return {"trades": len(trades), "closed": len(closed), "wins": len(wins), "losses": len(closed) - len(wins),
            "gross": round(sum(t["gross"] for t in closed), 2),
            "charges": round(sum(t["charges"]["total"] for t in closed), 2),
            "net": round(sum(t["net"] for t in closed), 2)}


def cmd_journal(a, cfg, now):
    book = read_json(a.book, {"trades": []}) or {"trades": []}
    trades = book["trades"]
    if a.json:
        print(json.dumps({"trades": trades, "summary": summary(trades)}, indent=2, ensure_ascii=False))
        return 0
    if not trades:
        print("No paper trades yet. Start with /nse-scanner, and record a trade with paper.py place N --yes.")
        return 1
    for t in trades:
        print(describe(t))
    s = summary(trades)
    print()
    if s["closed"]:
        print("%d closed: %d won, %d lost (after charges). Gross %s, charges %s, net %s." % (
            s["closed"], s["wins"], s["losses"], signed(s["gross"]), money(s["charges"]), signed(s["net"])))
        if s["charges"] and s["gross"] > 0 and s["net"] <= 0:
            print("Charges turned a gross profit into a net loss.")
    if s["closed"] < 20:
        print("Fewer than 20 closed trades: too few to tell skill from luck.")
    print(NOTE)
    return 0


def main(argv=None):
    utf8_stdout()
    p = argparse.ArgumentParser(description="Paper Trader: practise NSE orders on real prices, no real money.")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--risk", default="trader/state/risk.json")
    p.add_argument("--book", default="trader/state/paper.json")
    p.add_argument("--broker", default="trader/state/broker.json")
    p.add_argument("--now", help='pin the clock, IST: "2026-10-30 09:40"')
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    pl = sub.add_parser("place")
    pl.add_argument("index", type=int)
    pl.add_argument("--yes", action="store_true", help="the user typed yes for this order in the chat")
    st = sub.add_parser("settle")
    st.add_argument("--offline", help="a saved Yahoo chart or a {SYMBOL: chart} bundle (.gz is fine)")
    jo = sub.add_parser("journal")
    jo.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config)
        now = parse_now(a.now)
        warn = calendar_warning(now.date(), cfg)
        if warn:
            print(warn)
        return {"show": cmd_show, "place": cmd_place, "settle": cmd_settle, "journal": cmd_journal}[a.cmd](a, cfg, now)
    except PaperError as e:
        print("paper.py: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
