"""Live Chart maths: EMA, RSI, UT Bot, the five-vote bias, its 60-session record, longer
candles from 5-minute bars, and the day's levels (orb_plan). Used by live.py; page.html repeats
ut_bot and orb_plan in JavaScript (cloudflare/test-page-levels.mjs keeps them equal)."""

import datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from chart_data import *  # noqa: F401,F403

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
