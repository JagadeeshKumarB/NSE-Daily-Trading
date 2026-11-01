#!/usr/bin/env python3
"""Position sizing for nse-budget. Standard library only, no network.

Sizes ONE intraday (MIS) day trade on NSE from trader/plan.json (written by
nse-exit) to your budget and your max loss per trade, then writes it to
trader/orders.json in the shape of Kite Connect's place_order: an entry
order plus two exit orders (an SL stop-loss and a LIMIT target), because
Zerodha has no bracket order for this. It never places anything: nse-risk
checks the order and you say yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit 1393.00 --write          # a LIMIT entry at 1393.00 (default: the plan's entry)
    python3 size.py --market                         # a MARKET entry instead of LIMIT
    python3 size.py --now "2026-10-30 09:35" --json  # pin the clock (IST)

How many shares. Whole shares only (India has no fractional shares).

    shares = the smallest of
      max loss       max_loss_per_trade_inr / risk per share (entry minus stop);
                     with count_charges_in_max_loss (default) the loss at the
                     stop PLUS Zerodha's round-trip charges must fit
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one).
                     No MIS leverage: this tool never sizes on margin.

The entry it sizes at: the LIMIT price (the plan's entry, or --limit PRICE).
With --market: the plan's entry, or the price in trader/prices.json if that
is fresh (under 15 minutes old) and higher, because a price that ran up since
the plan means a bigger loss at the same stop. Zero shares (the stop is too
far away for your max loss) is a plain refusal: no order is written. So is a
reward:risk under min_reward_risk (the price ran toward the target).

Prices sit on NSE's tick grid: the entry and the target round to the nearest
tick, the stop rounds DOWN. The stop-loss leg is an SL order: trigger at the
stop, limit sl_limit_buffer_pct (0.1 %) under it, rounded down to the tick.
Exit codes: 0 = an order, 1 = no order (refused) or an error.
"""

import argparse
import datetime
import json
import os
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

# The contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
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
    "count_charges_in_max_loss": True,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:30",
    "flat_by": "15:10",
    "product": "MIS",
    "sl_limit_buffer_pct": 0.1,
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_margin": False,
    "allow_short": False,
    "allow_options": False,
    "holidays": [],
    "tick_size": {},
    "charges": {},
}
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "level": "close above a level"}
OCO_NOTE = ("Zerodha has no bracket order for this: place the stop and the target after the entry fills, "
            "and cancel the other one the moment one fills.")


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise SizeError("--now wants a time like \"2026-10-30 09:35\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-30T04:05:00Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def paise(x):
    return int(Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP) * 100)


def shares_for(inr_amount, per_share):
    """Whole shares that fit in inr_amount at per_share, in exact integer paise."""
    u, p = paise(inr_amount), paise(per_share)
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """1394.5 -> "1394.50": prices as strings, the way Kite takes them."""
    return "%.2f" % float(Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _num(x):
    """1 -> "1", 0.5 -> "0.5", 1.25 -> "1.25"."""
    return ("%.2f" % round(float(x), 2)).rstrip("0").rstrip(".")


def money(x):
    """Whole rupees without paise, else with: ₹1,00,000 / ₹392.20."""
    x = round(float(x), 2)
    return inr(x, 0) if abs(x - round(x)) < 0.005 else inr(x)


def tick_config(sym, plan, cfg):
    """The config, plus the plan's own tick for this symbol unless the config already sets one."""
    c = dict(cfg or {})
    ticks = dict(c.get("tick_size") or {})
    if sym not in {str(k).upper() for k in ticks} and _f((plan or {}).get("tick")):
        ticks[sym] = _f(plan["tick"])
    c["tick_size"] = ticks
    return c


def trip_charges(qty, buy, sell, product, cfg):
    return charges(qty * buy, qty * sell, product, cfg)


# ---------------------------------------------------------------- sizing

def size_trade(plan, cfg, budget=None, limit=None, market=False, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit   the LIMIT price (None = the plan's entry)
    market  a MARKET entry instead of LIMIT
    price   a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash    the cash in trader/portfolio.json, if there is one
    today   the IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = from_yahoo(str(plan.get("symbol") or "").strip().upper())
    if not sym:
        raise SizeError("the plan has no symbol (run nse-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side == "long":
        side = "buy"
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    exchange = str(plan.get("exchange") or "NSE").strip().upper()
    if exchange != "NSE":
        raise SizeError("this pack trades NSE only; the plan says %r" % exchange)
    product = str(plan.get("product") or c.get("product") or "MIS").strip().upper()
    if product != "MIS":
        raise SizeError("this pack sizes intraday (MIS) trades only; the plan says %r. CNC is delivery, "
                        "not a day trade" % product)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India (IST): make a new plan with nse-exit"
                        % (plan["date"], today))
    if limit is not None and market:
        raise SizeError("--limit and --market do not go together: pick one")
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run nse-exit): the order needs an entry, "
                            "a stop and a target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    tc = tick_config(sym, plan, c)
    tick = float(tick_size(entry_plan, sym, tc))
    entry_plan = float(to_tick(entry_plan, "nearest", sym, tc))
    if market:
        if price is not None and float(to_tick(price, "nearest", sym, tc)) > entry_plan:
            ref, ref_from = float(to_tick(price, "nearest", sym, tc)), "price"
        else:
            ref, ref_from = entry_plan, "plan"
    elif limit is not None:
        ref, ref_from = float(to_tick(limit, "nearest", sym, tc)), "limit"
    else:
        ref, ref_from = entry_plan, "plan_limit"
    stop = float(to_tick(stop, "down", sym, tc))
    target = float(to_tick(target, "nearest", sym, tc))
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with nse-exit" % (_price(target), _price(ref)))
    risk_p = paise(ref - stop)
    reward_p = paise(target - ref)
    risk = risk_p / 100.0
    buf = _f(c.get("sl_limit_buffer_pct"))
    buf = 0.1 if buf is None else buf
    if not 0 < buf < 5:
        raise SizeError("sl_limit_buffer_pct must be above 0 and under 5 (it is %s): the SL limit must sit just under the trigger" % buf)
    sl_price = float(to_tick(stop * (1 - buf / 100.0), "down", sym, tc))

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    with_charges = bool(c.get("count_charges_in_max_loss", True))
    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        q = shares_for(max_loss, risk)
        how = "%s max loss / %s risk a share" % (money(max_loss), _price(risk))
        if with_charges:
            while q > 0 and q * risk_p + paise(trip_charges(q, ref, stop, product, c)["total"]) > paise(max_loss):
                q -= 1
            how += ", charges counted"
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": q, "how": how})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "inr": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss = round(qty * risk_p / 100.0, 2)
    at_stop = trip_charges(qty, ref, stop, product, c) if qty else charges(0, 0, product, c)
    at_target = trip_charges(qty, ref, target, product, c) if qty else charges(0, 0, product, c)
    loss_c = round(loss + at_stop["total"], 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            one = charges(ref, stop, product, c)["total"]
            refusal = ("one share risks %s (entry %s minus stop %s)%s, more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with nse-exit."
                       % (money(risk), _price(ref), _price(stop),
                          (" plus about %s in charges" % money(one)) if with_charges else "", money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_p < _f(c["min_reward_risk"]) * risk_p - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so nse-risk "
                   "would block it. No order. Skip it, or make a new plan with nse-exit."
                   % (reward_p / float(risk_p), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_inr"])))
    return {
        "symbol": sym, "exchange": exchange, "product": product, "side": "buy", "setup": plan.get("setup", ""),
        "date": plan.get("date"), "entry_time": plan.get("entry_time", ""), "entry_plan": entry_plan,
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target, "tick": tick,
        "sl_trigger": stop, "sl_price": sl_price, "sl_limit_buffer_pct": buf,
        "risk_per_share": risk, "reward_risk": round(reward_p / float(risk_p), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_inr": max_loss, "count_charges_in_max_loss": with_charges,
        "limits": limits, "qty": qty, "bound_by": bound[0], "bound_all": bound,
        "cost": cost, "max_loss": loss, "charges_at_stop": at_stop, "charges_at_target": at_target,
        "max_loss_with_charges": loss_c, "loss_at_sl_price": round(qty * paise(ref - sl_price) / 100.0, 2),
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "MARKET" if market else "LIMIT", "refusal": refusal,
    }


def bound_words(s):
    return {"max loss": "%s max loss per trade%s" % (money(s["max_loss_per_trade_inr"] or 0),
                                                     " (charges counted)" if s["count_charges_in_max_loss"] else ""),
            "position cap": "position cap", "max order": "max order",
            "cash": "cash after the buffer"}[s["bound_by"]]


def kite_order(s):
    """The one proposed trade, Kite place_order fields (prices as strings) plus the exits and notes."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop, %s with charges)"
           % (setup, s["symbol"], s["qty"], bound_words(s), s["qty"], _price(s["risk_per_share"]),
              money(s["max_loss"]), money(s["max_loss_with_charges"])))
    return {
        "symbol": s["symbol"],
        "exchange": s["exchange"],
        "tradingsymbol": s["symbol"],
        "transaction_type": "BUY",
        "quantity": int(s["qty"]),
        "product": s["product"],
        "order_type": s["order_type"],
        "price": None if s["order_type"] == "MARKET" else _price(s["entry"]),
        "variety": "regular",
        "validity": "DAY",
        "exits": {
            "stop_loss": {"transaction_type": "SELL", "order_type": "SL",
                          "trigger_price": _price(s["sl_trigger"]), "price": _price(s["sl_price"])},
            "target": {"transaction_type": "SELL", "order_type": "LIMIT", "price": _price(s["target"])},
        },
        "oco_note": OCO_NOTE,
        "charges_estimate": {"at_stop": s["charges_at_stop"]["total"], "at_target": s["charges_at_target"]["total"]},
        "max_loss": s["max_loss"],
        "max_loss_with_charges": s["max_loss_with_charges"],
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    }


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy (NSE, %s intraday), %s, entry %s%s" % (
        s["symbol"], s["product"], setup, _price(s["entry_plan"]),
        (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan_limit": "a LIMIT order at the plan's entry", "plan": "a MARKET order, at the plan's entry",
           "price": "a MARKET order, at the latest price, above the plan's entry",
           "limit": "a LIMIT order at your price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-66s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    if len(s["bound_all"]) > 1:
        lines.append("  (%s give the same number; the first one is named)" % " and ".join(s["bound_all"]))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s, decided by your %s."
                 % (s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]), bound_words(s)))
    lines.append("Max loss at the stop: %d x %s = %s, plus about %s in charges = %s (your limit %s)."
                 % (s["qty"], _price(s["risk_per_share"]), money(s["max_loss"]), money(s["charges_at_stop"]["total"]),
                    money(s["max_loss_with_charges"]), money(s["max_loss_per_trade_inr"] or 0)))
    lines.append("Charges at the target: about %s. Zerodha's rates; the contract note is the real number."
                 % money(s["charges_at_target"]["total"]))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s. The SL order's limit "
                 "is %s (%s%% under); a fill there loses %s plus charges."
                 % (_price(s["stop"]), _price(s["sl_price"]), _num(s["sl_limit_buffer_pct"]),
                    money(s["loss_at_sl_price"])))
    lines.append("")
    lines.append("Proposed orders (Kite place_order arguments, nothing has been placed):")
    how = "MARKET" if order["order_type"] == "MARKET" else "LIMIT %s" % order["price"]
    ex = order["exits"]
    lines.append("  1. entry   BUY %d %s, NSE, %s, %s, DAY" % (order["quantity"], order["symbol"], order["product"], how))
    leg2 = "SELL %d, SL, trigger %s, limit %s" % (order["quantity"], ex["stop_loss"]["trigger_price"],
                                                  ex["stop_loss"]["price"])
    leg3 = "SELL %d, LIMIT %s" % (order["quantity"], ex["target"]["price"])
    w = max(len(leg2), len(leg3)) + 3
    lines.append("  2. stop    %s(after the entry fills)" % leg2.ljust(w))
    lines.append("  3. target  %s(after the entry fills)" % leg3.ljust(w))
    lines.append(OCO_NOTE)
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed, win or lose. Zerodha squares off MIS "
                     "positions itself from about 15:20 and charges %s + GST for it." % (s["flat_by"], money(50)))
    lines.append("")
    lines.append("Next: nse-risk checks it, then you say yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "nse-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until nse-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run nse-exit first)")
    limit = None
    if a.limit is not None and a.limit != "plan":
        limit = _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 1393.00")
    sym = str((plan or {}).get("symbol") or "").upper()
    entry = _f((plan or {}).get("entry")) or 0
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        lim = limit if limit is not None else entry
        if p is not None and fresh:
            if a.market:
                price = p
                if p > entry + 0.005:
                    notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk is "
                                 "bigger: sized at %s." % (sym, _price(p), to_ist(t).strftime("%H:%M"),
                                                           _price(entry), _price(p)))
            elif p > lim + 0.005:
                notes.append("%s last %s (as of %s IST) is above your LIMIT %s: the order waits and fills only "
                             "at %s or better, maybe never." % (sym, _price(p), to_ist(t).strftime("%H:%M"),
                                                               _price(lim), _price(lim)))
        elif p is not None and a.market:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, a.market, price, cash, today)
    order = kite_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes},
                         indent=2, ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order (entry + stop + target) to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(description="Size an NSE intraday trade to your budget and max loss (nse-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config, in rupees")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="the LIMIT entry price (default: the plan's entry; LIMIT is the default order type)")
    ap.add_argument("--market", action="store_true", help="a MARKET entry instead of LIMIT")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-30 09:35\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        if a.market and a.limit is not None:
            raise SizeError("--limit and --market do not go together: pick one")
        cfg = load_config(a.config)
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
