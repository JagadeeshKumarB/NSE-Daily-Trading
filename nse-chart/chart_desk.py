"""The trading desk on the Live Chart: size a trade, lock the day, paper-trade from the chart, read the mood.

    size_trade(side, entry, stop, targets, settings, ...)   shares that fit your rules, charges counted
    day_book(trades, bars_by_sym, now, settings)             today's paper trades replayed, P&L and the lock
    mood(board_rows, now, book)                               Trade / Trade small / Sit out, and why

Pure functions over plain data; live.py keeps the trades in trader/state/chart-desk.json and the Worker in D1
(cloudflare/src/desk.js repeats them in JavaScript; cloudflare/test-desk.mjs keeps the two equal). Paper only:
nothing here sends an order anywhere.

Evidence (the saved Nifty 50 bars, 50 stocks, 59 sessions, every BUY and SELL that triggered, exit at T1):
  - charges cost about 0.13R a trade at a Rs 500 risk;
  - after charges, breakouts from 9:35 to 10:00 made +0.06R (T1 49 %), 10:05 to 11:00 -0.04R (T1 39 %),
    later +0.03R (T1 34 % then 22 %);
  - once 60 % or more of the day's resolved breakouts had hit their stop, the next ones made +0.06R before
    charges against +0.13 to +0.15R otherwise (118 trades: a small sample);
  - nothing known at 9:30 (stock gaps, opening-range width, yesterday's chop, India VIX) told a bad day from
    a good one: 7 bad days in 59, spread evenly.
"""

import datetime

from chart_data import charges, BAR, OPEN

DESK_DEFAULTS = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "count_charges_in_max_loss": True, "no_entries_after": "14:30", "flat_by": "15:10",
    "allow_short": False, "product": "MIS",
}
EDITABLE = ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "allow_short")
CHOP_SHARE, CHOP_MIN = 0.6, 5        # 60 % of at least 5 resolved breakouts stopped: a choppy day


def settings_from(config=None, overrides=None):
    s = dict(DESK_DEFAULTS)
    for src in (config or {}, overrides or {}):
        for k in DESK_DEFAULTS:
            if k in src and src[k] is not None:
                s[k] = src[k]
    return s


def clean_settings(data):
    """Only the settings the page may change, checked: positive amounts, a whole number of trades."""
    out = {}
    for k in EDITABLE:
        if k not in data:
            continue
        v = data[k]
        if k == "allow_short":
            out[k] = bool(v)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % k)
        if k == "max_trades_per_day":
            if v != int(v) or not 1 <= v <= 50:
                raise ValueError("max_trades_per_day must be a whole number from 1 to 50")
            v = int(v)
        elif not 0 < v <= 1e9:
            raise ValueError("%s must be above 0" % k)
        out[k] = v
    return out


def paise(x):
    return int(round(float(x) * 100))


def shares_for(amount, per_share):
    u, p = paise(amount), paise(per_share)
    return u // p if p > 0 and u > 0 else 0


def trip(side, qty, entry, exit_):
    """Charges for one intraday round trip: a BUY buys first, a SELL sells first and buys back."""
    if side == "buy":
        return charges(qty * entry, qty * exit_, "MIS")["total"]
    return charges(qty * exit_, qty * entry, "MIS")["total"]


def size_trade(side, entry, stop, targets, s, realized_today=0.0, factor=1.0, in_use=0.0):
    """Whole shares for one trade: the fewest that any of your limits allows.
    max loss   (entry - stop) a share, plus the round trip's charges at the stop, within your max loss per trade
               (halved on a "Trade small" day: factor 0.5) and within what is left of today's loss limit
    position   max_position_pct of the budget; max order; the budget less its cash buffer and less the money
               already in today's open and waiting paper trades (in_use)
    Returns the plan with P&L at each target after charges, or a refusal with the reason."""
    k = 1 if side == "buy" else -1
    risk_p = (paise(entry) - paise(stop)) * k
    if risk_p <= 0:
        return {"qty": 0, "refusal": "the stop is on the wrong side of the entry"}
    risk = risk_p / 100.0
    per_trade = float(s["max_loss_per_trade_inr"]) * factor
    left = float(s["max_daily_loss_inr"]) + min(realized_today, 0.0)       # realized losses use up the day
    max_loss = min(per_trade, left)
    limits = []
    if max_loss <= 0:
        limits.append(("loss left today", 0))
    else:
        q = shares_for(max_loss, risk)
        if s.get("count_charges_in_max_loss", True):
            while q > 0 and q * risk_p + paise(trip(side, q, entry, stop)) > paise(max_loss):
                q -= 1
        limits.append(("max loss" if max_loss == per_trade else "loss left today", q))
    budget = float(s["budget"])
    limits.append(("position cap", shares_for(budget * float(s["max_position_pct"]) / 100.0, entry)))
    limits.append(("max order", shares_for(float(s["max_order_inr"]), entry)))
    limits.append(("cash", shares_for(max(budget * (1 - float(s["cash_buffer_pct"]) / 100.0) - in_use, 0), entry)))
    qty = min(q for _, q in limits)
    bound = next(n for n, q in limits if q == qty)
    out = {"side": side, "entry": entry, "stop": stop, "qty": qty, "bound_by": bound, "factor": factor,
           "risk_per_share": risk, "cost": round(qty * entry, 2), "max_loss_used": round(max_loss, 2)}
    if qty == 0:
        out["refusal"] = ("no loss left for today" if bound == "loss left today" and max_loss <= 0 else
                          "one share risks %.2f plus charges, more than the %.2f allowed" % (risk, max_loss)
                          if bound in ("max loss", "loss left today") else "one share is more than your %s" % bound)
        return out
    at_stop = trip(side, qty, entry, stop)
    out["loss_at_stop"] = round(qty * risk + at_stop, 2)
    out["targets"] = []
    for t in targets:
        gross = round(qty * (t - entry) * k, 2)
        out["targets"].append({"price": t, "gross": gross, "net": round(gross - trip(side, qty, entry, t), 2)})
    return out


# ---------------------------------------------------------------- paper trades from the chart

def hm(t):
    return datetime.datetime.strptime(t, "%H:%M").time()


def replay(trade, bars, now, s):
    """One paper trade on its day's 5-minute bars (naive IST), from the moment it was placed.
    entry  'market': the last close when placed (stored with the trade). 'armed': waits for the level's
           trigger, a 5-minute close beyond it, and fills at that close; cancelled if none by no_entries_after.
    exits  from the bar after the fill: the stop (or the open, on a gap through it), else the target (or the
           open, on a gap through it), else out at the open of the flat_by bar; the stop and the target in one
           bar count as the stop. 'exit' on the trade (Exit now): that price and time.
    Returns status open | waiting | closed | cancelled, fill, exit, and P&L (net of charges; an open trade is
    marked to the last closed bar)."""
    side, k = trade["side"], (1 if trade["side"] == "buy" else -1)
    placed = datetime.datetime.strptime(trade["placed"], "%Y-%m-%d %H:%M")
    done = [b for b in bars if b[0] + BAR <= now and b[0].date() == placed.date()]
    after = [b for b in done if b[0] >= placed]          # only what happened after the click
    r = {"id": trade["id"], "symbol": trade["symbol"], "side": side, "qty": trade["qty"], "stop": trade["stop"],
         "target": trade["target"], "target_name": trade.get("target_name", ""), "placed": trade["placed"][11:],
         "mode": trade["mode"], "fill": None, "exit": None, "status": "waiting", "pnl": 0.0, "charges": 0.0}
    i0 = None
    manual = trade.get("exit")
    manual_at = datetime.datetime.strptime(trade["placed"][:11] + manual["time"], "%Y-%m-%d %H:%M") if manual else None
    if trade["mode"] == "market":
        r["fill"] = {"price": trade["entry"], "time": trade["placed"][11:]}
        i0 = 0
    else:
        level, cutoff = trade["entry"], hm(s["no_entries_after"])
        for i, b in enumerate(after):
            if (b[0] + BAR).time() > cutoff or (manual_at and b[0] >= manual_at):
                break
            if (b[4] > level) if k > 0 else (b[4] < level):
                r["fill"] = {"price": b[4], "time": (b[0] + BAR).strftime("%H:%M")}
                i0 = i + 1
                break
        if r["fill"] is None:
            if manual:
                r["status"] = "cancelled"
                return r
            last_end = (done[-1][0] + BAR).time() if done else OPEN
            if last_end >= cutoff or now.time() >= cutoff:
                r["status"] = "cancelled"
            return r
    fill = r["fill"]["price"]
    flat = hm(s["flat_by"])
    for b in after[i0:]:
        if manual_at and b[0] >= manual_at:
            break
        if b[0].time() >= flat:
            r["exit"] = {"how": "flat", "price": b[1], "time": b[0].strftime("%H:%M")}
            break
        end = (b[0] + BAR).strftime("%H:%M")
        if (b[3] <= trade["stop"]) if k > 0 else (b[2] >= trade["stop"]):
            r["exit"] = {"how": "stop", "price": min(trade["stop"], b[1]) if k > 0 else max(trade["stop"], b[1]), "time": end}
            break
        if (b[2] >= trade["target"]) if k > 0 else (b[3] <= trade["target"]):
            r["exit"] = {"how": "target", "price": max(trade["target"], b[1]) if k > 0 else min(trade["target"], b[1]), "time": end}
            break
    if r["exit"] is None and manual:
        r["exit"] = {"how": "manual", "price": manual["price"], "time": manual["time"]}
    if r["exit"] is None and done and done[-1][0].time() >= flat:
        r["exit"] = {"how": "flat", "price": done[-1][4], "time": (done[-1][0] + BAR).strftime("%H:%M")}
    out = r["exit"]["price"] if r["exit"] else (done[-1][4] if done else fill)
    q = trade["qty"]
    r["charges"] = round(trip(side, q, fill, out), 2)
    r["pnl"] = round(q * (out - fill) * k - r["charges"], 2)
    r["status"] = "closed" if r["exit"] else "open"
    return r


def day_book(trades, bars_by_sym, now, s):
    """Today's paper trades replayed, the day's P&L (closed and open) and the lock."""
    today = now.strftime("%Y-%m-%d")
    rows = [replay(t, bars_by_sym.get(t["symbol"], []), now, s) for t in trades if t["placed"][:10] == today]
    realized = round(sum(r["pnl"] for r in rows if r["status"] == "closed"), 2)
    open_pnl = round(sum(r["pnl"] for r in rows if r["status"] == "open"), 2)
    taken = sum(1 for r in rows if r["status"] != "cancelled")
    in_use = round(sum(r["qty"] * (r["fill"]["price"] if r["fill"] else t["entry"])
                       for r, t in zip(rows, [t for t in trades if t["placed"][:10] == today]) if r["status"] in ("open", "waiting")), 2)
    lock = None
    left = float(s["max_daily_loss_inr"]) + min(realized, 0.0)
    if left < 0.25 * float(s["max_loss_per_trade_inr"]):      # not even a quarter of one trade's risk left
        lock = "Daily loss limit reached (%.2f of %.2f lost). Stop for today." % (-realized, float(s["max_daily_loss_inr"]))
    elif taken >= int(s["max_trades_per_day"]):
        lock = "Trade limit reached (%d of %d). Stop for today." % (taken, int(s["max_trades_per_day"]))
    elif now.strftime("%H:%M") >= s["no_entries_after"]:
        lock = "No new trades after %s." % s["no_entries_after"]
    return {"trades": rows, "realized": realized, "open": open_pnl, "net": round(realized + open_pnl, 2),
            "taken": taken, "in_use": in_use, "max_trades": int(s["max_trades_per_day"]), "max_daily_loss": float(s["max_daily_loss_inr"]),
            "lock": lock}


# ---------------------------------------------------------------- the mood

def resolved_today(board_rows, now):
    """Of today's BUY and SELL breakouts across the list, how many reached T1 and how many hit the stop first,
    by now."""
    t1 = stop = 0
    clock = now.strftime("%H:%M")
    for row in board_rows:
        p = row.get("plan") or {}
        for side in ("buy", "sell"):
            x = p.get(side) or {}
            if x.get("status") != "triggered":
                continue
            if x.get("hits") and x["hits"][0] <= clock:
                t1 += 1
            elif not x.get("hits") and (x.get("exit") or {}).get("how") == "stop" and x["exit"]["time"] <= clock:
                stop += 1
    return t1, stop


def mood(board_rows, now, book, market_open=True):
    """Trade / Trade small / Sit out, with the reasons and the size factor for the calculator."""
    t1, stop = resolved_today(board_rows, now)
    n, clock = t1 + stop, now.strftime("%H:%M")       # minutes, as the Worker compares them
    reasons, level, factor = [], "trade", 1.0
    if not market_open:
        return {"level": "closed", "label": "Market closed", "factor": 1.0, "t1": t1, "stop": stop,
                "reasons": ["NSE is closed: the desk opens at 9:15 AM on a trading day."]}
    if book.get("lock"):
        return {"level": "sit", "label": "Sit out", "factor": 0.0, "t1": t1, "stop": stop, "reasons": [book["lock"]]}
    if clock < "09:30":
        level = "wait"
        reasons.append("The opening range (9:15 to 9:30) is still forming: levels at 9:30 AM.")
    if n >= CHOP_MIN and stop / n >= CHOP_SHARE:
        level, factor = "small", 0.5
        reasons.append("Choppy: %d of %d breakouts today hit their stop before T1. On 59 past sessions, breakouts after "
                       "a day turned like this made +0.06R before charges against +0.13R: half size." % (stop, n))
    elif n:
        reasons.append("%d of %d of today's breakouts reached T1 before the stop." % (t1, n))
    if "09:30" <= clock <= "10:00":
        reasons.append("9:35 to 10:00 has been the best window: T1 49 % of the time, +0.06R after charges.")
    elif clock > "10:00" and level == "trade":
        reasons.append("After 10:00, breakouts reached T1 less often (39 % falling to 22 %): be picky.")
    label = {"trade": "Trade", "small": "Trade small", "wait": "Wait for 9:30"}[level]
    return {"level": level, "label": label, "factor": factor, "t1": t1, "stop": stop, "reasons": reasons}


# ---------------------------------------------------------------- a new paper trade, checked

class DeskError(Exception):
    pass


def new_trade(sym, plan, side, target_n, last, now, s, book, md, trade_id):
    """A paper trade from the chart, or DeskError with the reason. plan: orb_plan for today; last: the last
    closed 5-minute bar [start, o, h, l, c, v] (naive IST); md: the mood (its factor sizes the trade).
    Not yet triggered: armed at the level, fills on its trigger. Triggered: at the last close, if the stop has
    not been hit and the target not passed. Sized by size_trade with what is left of today's loss limit."""
    if side not in ("buy", "sell"):
        raise DeskError("side must be buy or sell")
    if side == "sell" and not s.get("allow_short"):
        raise DeskError("Short selling is off: turn on 'Allow SELL (short)' in the desk settings first. "
                        "A short is intraday only (MIS): it must be bought back the same day.")
    if md.get("level") == "closed":                       # a closed day says so before any limit does
        raise DeskError(md["reasons"][0])
    if book.get("lock"):
        raise DeskError(book["lock"])
    if md.get("level") in ("closed", "wait", "sit"):
        raise DeskError(md["reasons"][0] if md.get("reasons") else "Not now.")
    if not plan or plan.get("status") != "ready":
        raise DeskError("No levels for %s today yet." % sym)
    if any(r["symbol"] == sym and r["status"] in ("open", "waiting") for r in book.get("trades", [])):
        raise DeskError("You already have a paper trade on %s: exit or cancel it first." % sym)
    x = plan[side]
    if target_n not in (1, 2, 3):
        raise DeskError("Pick T1, T2 or T3.")
    target, k = x["targets"][target_n - 1], (1 if side == "buy" else -1)
    if x["status"] == "none":
        raise DeskError("The %s side did not trigger by %s: no entry today." % (side.upper(), s["no_entries_after"]))
    if x["status"] == "triggered":
        if x.get("exit") or x.get("hits") and len(x["hits"]) >= target_n:
            raise DeskError("This %s already %s: too late to enter." % (side.upper(), "reached T%d" % target_n if not x.get("exit") else "ended (%s)" % x["exit"]["how"]))
        if not last:
            raise DeskError("No closed bar yet.")
        entry, mode = last[4], "market"
        if (entry - target) * k >= 0:
            raise DeskError("The price %.2f is already past T%d %.2f." % (entry, target_n, target))
        if (entry - x["stop"]) * k <= 0:
            raise DeskError("The price %.2f is already past the stop %.2f." % (entry, x["stop"]))
    else:
        entry, mode = x["entry"], "armed"
    sz = size_trade(side, entry, x["stop"], x["targets"], s, book.get("realized", 0.0), md.get("factor", 1.0), book.get("in_use", 0.0))
    if not sz["qty"]:
        raise DeskError("No trade: %s." % sz.get("refusal", "0 shares"))
    return {"id": trade_id, "symbol": sym, "side": side, "qty": sz["qty"], "mode": mode, "entry": entry,
            "stop": x["stop"], "target": target, "target_name": "T%d" % target_n,
            "placed": now.strftime("%Y-%m-%d %H:%M"), "size": sz}
