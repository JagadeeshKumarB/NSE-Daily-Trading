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
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datetime
import sys

# the rest of the Live Chart sits next to this file: data, maths and alerts
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chart_data import *  # noqa: E402,F401,F403
from chart_signals import *  # noqa: E402,F401,F403
from chart_alerts import *  # noqa: E402,F401,F403
import chart_desk  # noqa: E402

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
                         "score": b["score"] if b else None, "label": b["label"] if b else None, "plan": plan,
                         "surge": volume_surge(bars, self.clock.now())})
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


# ---------------------------------------------------------------- the desk (chart_desk.py): paper trades, the lock, the mood

class Desk:
    """Your desk settings and today's paper trades, kept in trader/state/chart-desk.json. The settings start from
    trader/config.json (the same limits nse-budget and nse-risk use); what you change on the page is kept here."""

    def __init__(self, store, folder="trader"):
        self.store, self.folder = store, folder
        self.path = os.path.join(folder, "state", "chart-desk.json")
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"settings": {}, "trades": []}

    def _save(self, doc):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, self.path)

    def settings(self, doc):
        try:
            with open(os.path.join(self.folder, "config.json"), encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = {}
        return chart_desk.settings_from(cfg, doc.get("settings"))

    def _today_bars(self, sym):
        self.store.chart(sym)
        with self.store.lock:
            return list(self.store.today.get(sym, []))

    def _state(self, doc):
        now = self.store.clock.now()
        s = self.settings(doc)
        syms = {t["symbol"] for t in doc["trades"] if t["placed"][:10] == now.strftime("%Y-%m-%d")}
        book = chart_desk.day_book(doc["trades"], {x: self._today_bars(x) for x in syms}, now, s)
        with self.store.lock:
            rows = list(self.store.board.get("stocks") or [])
        md = chart_desk.mood(rows, now, book, market_state(now)[0] == "open")
        return now, s, book, md

    def view(self, sym=None):
        with self.lock:
            doc = self._load()
        now, s, book, md = self._state(doc)
        out = {"settings": s, "book": book, "mood": md, "sizes": None}
        if sym:
            sym = INDEX if yahoo_symbol(sym) == INDEX_YAHOO else from_yahoo(sym).upper()
            today = self._today_bars(sym)
            plan = orb_plan(sym, today, now) if today and today[0][0].date() == now.date() else None
            if plan and plan.get("status") == "ready" and sym != INDEX:
                out["sizes"] = {side: chart_desk.size_trade(side, plan[side]["entry"], plan[side]["stop"], plan[side]["targets"],
                                                            s, book["realized"], md.get("factor", 1.0) or 1.0, book["in_use"]) for side in ("buy", "sell")}
        return out

    def place(self, data):
        sym = from_yahoo(str(data.get("sym") or "")).upper()
        if not sym or sym == INDEX or yahoo_symbol(sym) == INDEX_YAHOO:
            raise chart_desk.DeskError("Pick a stock: the index cannot be bought or sold in the cash market.")
        with self.lock:
            doc = self._load()
            now, s, book, md = self._state(doc)
            today = self._today_bars(sym)
            done = [b for b in today if b[0] + BAR <= now and b[0].date() == now.date()]
            plan = orb_plan(sym, today, now) if done else None
            t = chart_desk.new_trade(sym, plan, str(data.get("side")), int(data.get("target") or 1), done[-1] if done else None,
                                     now, s, book, md, max([x["id"] for x in doc["trades"]] or [0]) + 1)
            doc["trades"].append(t)
            self._save(doc)
        return self.view(sym)

    def exit(self, data):
        with self.lock:
            doc = self._load()
            now = self.store.clock.now()
            t = next((x for x in doc["trades"] if x["id"] == data.get("id")), None)
            if not t or t["placed"][:10] != now.strftime("%Y-%m-%d"):
                raise chart_desk.DeskError("No such trade today.")
            done = [b for b in self._today_bars(t["symbol"]) if b[0] + BAR <= now]
            if not done:
                raise chart_desk.DeskError("No closed bar yet.")
            t["exit"] = {"price": done[-1][4], "time": (done[-1][0] + BAR).strftime("%H:%M")}
            self._save(doc)
        return self.view(t["symbol"])

    def set(self, data):
        with self.lock:
            doc = self._load()
            doc["settings"] = dict(doc.get("settings") or {}, **chart_desk.clean_settings(data))
            self._save(doc)
        return self.view(data.get("sym"))


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

def make_handler(store, desk=None):
    desk = desk or Desk(store)

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
                if url.path == "/api/desk":
                    return self.send(200, json.dumps(desk.view((q.get("sym") or [None])[0])))
                self.send(404, json.dumps({"error": "not found"}))
            except ChartError as e:
                self.send(502, json.dumps({"error": str(e)}))

        def do_POST(self):
            """The desk's changes: JSON from this page only (a page on another site cannot send JSON here)."""
            url = urllib.parse.urlparse(self.path)
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
                return self.send(403, json.dumps({"error": "not from this page"}))
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.send(400, json.dumps({"error": "JSON only"}))
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10000)) or b"{}")
                route = {"/api/desk/trade": desk.place, "/api/desk/exit": desk.exit, "/api/desk/settings": desk.set}.get(url.path)
                if not route:
                    return self.send(404, json.dumps({"error": "not found"}))
                return self.send(200, json.dumps(route(data)))
            except (chart_desk.DeskError, ValueError) as e:
                return self.send(400, json.dumps({"error": str(e)}))
            except ChartError as e:
                return self.send(502, json.dumps({"error": str(e)}))
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
