"""nse-entry tests. Offline and deterministic.

Fixtures:
tests/fixtures/nse/nifty50-5m-60d.json.gz   real Yahoo 5-minute bars, a bundle
        {"ICICIBANK": <v8 chart>, ...} for the Nifty 50, Aug 5 to Oct 30 2026 (60 sessions).
tests/fixtures/nse-entry/DEMO-1m-synthetic.json   SYNTHETIC one-minute bars (not market
        data), Thu Oct 29 2026 from 9:15 IST: a wick above the range high, a close AT
        the high, a null bar, a live tick.

ICICIBANK-2026-10-30.json is cut from the bundle at test time: Oct 30 only, ending
with the in-progress 10:45 bar and a live tick at 10:49:45 IST, like a range=1d answer.
"""

import copy
import datetime
import gzip
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

try:
    from _load import load, FIX, ROOT
except ImportError:          # python -m unittest tests.test_entry, from the repo root
    from tests._load import load, FIX, ROOT

entry = load("nse-entry", "entry")

BUNDLE = os.path.join(FIX, "nse", "nifty50-5m-60d.json.gz")
DEMO = os.path.join(FIX, "nse-entry", "DEMO-1m-synthetic.json")
OPEN_TS = 1793331900       # 2026-10-30 09:15 IST
LIVE_TS = OPEN_TS + 94 * 60 + 45    # 10:49:45 IST
DT = datetime.datetime

with gzip.open(BUNDLE, "rt", encoding="utf-8") as _fh:
    ALL = json.load(_fh)


def one_day(sym="ICICIBANK"):
    """Oct 30 only, as Yahoo's range=1d answers at 10:49:45: bars to the in-progress
    10:45 bar plus a live tick."""
    d = copy.deepcopy(ALL[sym])
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    keep = [i for i, t in enumerate(r["timestamp"]) if OPEN_TS <= t <= OPEN_TS + 18 * 300]
    r["timestamp"] = [r["timestamp"][i] for i in keep] + [LIVE_TS]
    for k in q:
        q[k] = [q[k][i] for i in keep] + [q[k][keep[-1]]]
    m = r["meta"]
    m.pop("tradingPeriods", None)
    m.update(range="1d", regularMarketTime=LIVE_TS)
    return d


ICICI = one_day()
TMP = tempfile.mkdtemp()
ONE = os.path.join(TMP, "ICICIBANK-2026-10-30.json")
with open(ONE, "w", encoding="utf-8") as _fh:
    json.dump(ICICI, _fh)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def ist(text):
    return entry.parse_now(text)


def chart(d=None, sym="ICICIBANK"):
    return entry.parse_chart(json.dumps(d if d is not None else ALL[sym]), sym)


CH = chart()


def at(text, d=None, **kw):
    return entry.evaluate(chart(d) if d is not None else CH, ist(text), **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    """ICICIBANK, Fri Oct 30 2026: range 1383.40-1390.90, the 9:30 bar closes 1394.50 at 9:35."""

    def test_triggers_at_935_on_the_930_bar(self):
        r = at("2026-10-30 9:35")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1390.9)
        self.assertEqual(r["opening_range"]["low"], 1383.4)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "9:35 AM")
        self.assertEqual(t["bar_start"], "09:30")
        self.assertEqual(t["bar_index"], 3)
        self.assertEqual(t["price"], 1394.5)
        self.assertEqual(t["pct_over"], 0.26)
        self.assertEqual(r["level"], 1390.9)
        self.assertEqual(r["prev_close"], 1386.6)
        self.assertEqual(r["exchange"], "NSE")
        self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("09:30", "14:30"))

    def test_at_933_it_is_too_early_and_the_930_bar_is_still_forming(self):
        r = at("2026-10-30 9:33")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:25")
        self.assertEqual(r["last"]["close"], 1389.8)
        self.assertEqual(r["forming"]["bar_start"], "09:30")
        self.assertTrue(r["reason"].startswith("too early"), r["reason"])
        self.assertIn("closes at 9:35 AM", r["reason"])
        self.assertIn("₹1.10 under the range high ₹1,390.90", r["reason"])
        self.assertEqual(at("2026-10-30 09:34:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-30 09:00")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("NSE opens at 9:15", r["reason"])
        r = at("2026-10-30 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-30 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_bundle_and_a_single_day_file_agree(self):
        a = at("2026-10-30 9:35")
        b = entry.evaluate(entry.parse_chart(json.dumps(ICICI), "ICICIBANK"), ist("2026-10-30 9:35"))
        self.assertEqual(a["trigger"], b["trigger"])
        self.assertEqual(a["opening_range"], b["opening_range"])
        self.assertEqual(b["prev_close"], 1386.6)       # meta's previousClose on a one-day file

    def test_the_bundle_selects_the_day(self):
        r = at("2026-10-29 15:00")
        self.assertEqual(r["date"], "2026-10-29")
        self.assertEqual(r["opening_range"]["high"], 1376.8)
        self.assertEqual(r["prev_close"], 1377.6)       # Oct 28's last close, from the bars
        self.assertEqual(at("2026-10-23 15:00")["status"], "expired")

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = entry.parse_chart(json.dumps(ICICI), "ICICIBANK")
        self.assertEqual(c["bars"][-1]["t"], LIVE_TS)
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), setup="level", level=1410)
        self.assertEqual(r["now"], "2026-10-30 10:49:45")
        self.assertEqual(r["_bars"][-1]["slot"], 17)               # the 10:40 bar
        self.assertEqual(r["forming"]["bar_start"], "10:45")
        self.assertEqual(r["bars_closed"], 18)
        r = entry.evaluate(c, ist("2026-10-30 10:50"), setup="level", level=1410)
        self.assertEqual(r["_bars"][-1]["slot"], 17)
        self.assertIn("Yahoo's last trade is 10:49:45", r["lag_note"])
        r = entry.evaluate(c, ist("2026-10-30 10:51"), setup="level", level=1410)
        self.assertEqual(r["_bars"][-1]["slot"], 18)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-30 15:35")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "9:35 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)

    def test_35_of_the_nifty_50_broke_their_range(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "15:30"}
        n = sum(entry.evaluate(chart(sym=s), ist("2026-10-30 15:30"), cfg=cfg)["status"] == "triggered"
                for s in ALL)
        self.assertEqual(n, 35)
        n = sum(entry.evaluate(chart(sym=s), ist("2026-10-30 15:30"))["status"] == "triggered" for s in ALL)
        self.assertEqual(n, 34)                          # by the default 14:30 cutoff, as SKILL.md says


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-30 12:00", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 217542)    # the 9:15 bar shows 0 volume: left out
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("11:15 AM", 23, 1395.6))
        self.assertEqual(t["volume_x"], 2.52)
        r = at("2026-10-30 09:36", min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.83x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-30 12:00", setup="level", level=1400)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("11:50 AM", 30, 1401.0))
        # a level under the open: the first eligible close is the 9:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-30 12:00", setup="level", level=1300)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-30 12:00", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        r = at("2026-10-23 14:29")
        self.assertEqual(r["status"], "waiting")
        r = at("2026-10-23 14:30")
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:30", r["reason"])
        self.assertIn("₹1,404.50", r["reason"])
        self.assertEqual(at("2026-10-23 15:31")["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:45"}
        d = copy.deepcopy(ICICI)
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= OPEN_TS + 17 * 300]   # up to the 10:40 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = OPEN_TS + 89 * 60 + 40                       # last trade 10:44:40
        r = at("2026-10-30 10:45:10", d, setup="level", level=1410, cfg=cfg)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:44:40", r["lag_note"])
        self.assertEqual(at("2026-10-30 10:46", d, setup="level", level=1410, cfg=cfg)["status"], "expired")

    def test_config_times(self):
        r = at("2026-10-30 09:40", cfg={"no_entries_before": "09:30", "no_entries_after": "09:30"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-10-30 10:30", cfg={"no_entries_before": "10:00", "no_entries_after": "14:30"})
        self.assertEqual(r["trigger"]["time"], "10:00 AM")       # the 9:55 bar, first to close at or after 10:00
        self.assertEqual(r["trigger"]["price"], 1394.3)

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-30 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-30 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")

    def test_now_is_ist(self):
        self.assertEqual(entry.parse_now("2026-10-30 9:35"), DT(2026, 10, 30, 4, 5))
        self.assertEqual(entry.parse_now("2026-10-30 09:35"), DT(2026, 10, 30, 4, 5))
        self.assertEqual(entry.parse_now("2026-10-30 09:35 IST"), DT(2026, 10, 30, 4, 5))
        self.assertEqual(entry.parse_now("2026-10-30T04:05:00Z"), DT(2026, 10, 30, 4, 5))
        self.assertEqual(entry.parse_now("2026-10-30T09:35+05:30"), DT(2026, 10, 30, 4, 5))
        # no daylight saving: the same offset in January and in July
        self.assertEqual(entry.parse_now("2026-01-05 09:15"), DT(2026, 1, 5, 3, 45))
        self.assertEqual(entry.parse_now("2026-07-06 09:15"), DT(2026, 7, 6, 3, 45))

    def test_or_minutes_30(self):
        r = at("2026-10-30 12:00", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1395.4)
        self.assertEqual(r["trigger"]["time"], "11:10 AM")

    def test_prices_sit_on_the_tick_grid(self):
        r = at("2026-10-30 9:35")
        for x in (r["trigger"]["price"], r["level"], r["opening_range"]["low"]):
            self.assertEqual(round(x * 10) / 10, x)          # 0.10 tick between 1,000 and 5,000
        self.assertIn("₹1,394.50", "\n".join(entry.describe(r)))


class OneMinute(unittest.TestCase):
    def c(self):
        with open(DEMO) as fh:
            return entry.parse_chart(fh.read(), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-29 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-10-29 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, 9:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_weekend_and_nse_holiday(self):
        r = at("2026-10-31 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("weekend", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-02 09:15")
        r = at("2026-10-20 10:30")                                # Dussehra
        self.assertIn("NSE holiday", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        # Diwali-Balipratipada, Tue Nov 10
        self.assertEqual(entry.next_open(datetime.date(2026, 11, 9)), "2026-11-11 09:15")

    def test_config_holidays_and_an_unknown_year(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "14:30", "holidays": ["2026-10-30"]}
        r = at("2026-10-30 10:30", cfg=cfg)
        self.assertEqual(r["status"], "no-data")
        r = at("2027-03-01 10:30")
        self.assertIn("2027 holiday list is not built in", r["calendar_note"])
        self.assertIn("2027 holiday list", "\n".join(entry.describe(r)))
        self.assertIsNone(at("2026-10-30 10:30")["calendar_note"])

    def test_empty_data(self):
        empty = copy.deepcopy(ICICI)
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-30 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-02 10:15", ICICI)                         # Yahoo sent Friday's bars only
        self.assertEqual(r["status"], "no-data")

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "ICICIBANK")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        self.body = json.dumps(ICICI)

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def get(self):
        return entry.get_chart("ICICIBANK", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = self.get()
        self.assertEqual(c["symbol"], "ICICIBANK")
        self.assertEqual(len(c["bars"]), 20)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/ICICIBANK.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        self.get()
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        self.get()
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "ICICIBANK", "--now", "2026-10-30 09:35"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 30), today=datetime.date(2026, 10, 30))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_and_symbols(self):
        today = datetime.date(2026, 10, 30)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 29), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 9), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 10), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 8, 1), today, "5m")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 9), today, "1m")
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("m&m", "1d", "5m"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("BAJAJ-AUTO", "1d", "5m"))
        self.assertIn("/chart/RELIANCE.NS?", entry.chart_path("RELIANCE.NS", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None, spike=1392.0):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike above the range high, plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = spike, max(b["h"], spike)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": spike, "h": spike, "l": spike, "c": spike, "v": 0})
        return {"symbol": "ICICIBANK", "meta": {}, "bars": bars, "prev_close": 1386.6, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, opts=None, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(opts or self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def hms(self, calls):
        return [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(ICICI, calls=calls), "2026-10-30 09:32", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:35 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1394.5)
        self.assertEqual(self.hms(calls), ["09:32:00", "09:35:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:30 bar  closed ₹1,394.50  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_a_spike_inside_a_bar_is_not_a_trigger(self):
        calls = []
        opts = dict(self.OPTS, setup="level", level=1400)
        code, res, lines, fired, clock = self.loop(live_provider(ICICI, calls=calls, spike=1402.0),
                                                   "2026-10-30 09:42", opts=opts, every=60, until=DT(2026, 10, 30, 10, 0))
        self.assertEqual(code, 1)                          # every in-progress bar spikes to 1402: never counted
        self.assertEqual(fired, [])
        self.assertIn("--until 10:00", lines[-1])

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-30 09:35:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(ICICI, lag_until=lag, calls=calls),
                                                   "2026-10-30 09:32", every=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.hms(calls), ["09:32:00", "09:35:02", "09:35:32"])   # 9:35:02: Yahoo still at 9:34:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:35 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(ICICI), "2026-10-30 09:00", every=30)
        self.assertEqual(code, 0)
        self.assertIn("not open yet", lines[0])
        self.assertIn("9:20", lines[0])
        text = "\n".join(lines)
        self.assertIn(" 9:25 bar  closed ₹1,389.80  opening range set: high ₹1,390.90, low ₹1,383.40", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 4)    # 9:15 ... 9:30, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-30 09:46"))
        calls, lines = [], []
        opts = dict(self.OPTS, setup="level", level=1400)
        code, res = entry.watch_loop(live_provider(ALL["ICICIBANK"], calls=calls), clock.now, clock.sleep,
                                     lines.append, opts, every=30)
        self.assertEqual(code, 0)
        times = self.hms(calls)
        self.assertEqual(times[:2], ["09:46:00", "10:04:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:45", " 9:50", " 9:55", "10:00"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "11:50 AM")

    def test_expires_at_the_cutoff_and_until(self):
        d = ALL["ICICIBANK"]
        code, res, lines, fired, clock = self.loop(live_provider(d), "2026-10-23 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:30")
        code, res, lines, fired, clock = self.loop(live_provider(d), "2026-10-23 10:30", every=300,
                                                   until=DT(2026, 10, 23, 11, 0))
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(ICICI), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(ICICI), "2026-10-20 10:00")
        self.assertIn("NSE holiday", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-30 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "ICICIBANK", "--setup", "orb", "--offline", BUNDLE,
                             "--now", "2026-10-30 9:35", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:35 AM at ₹1,394.50", text)
        self.assertIn("Opening range 9:15-9:30: high ₹1,390.90, low ₹1,383.40", text)
        self.assertIn("/nse-exit", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "nse-entry", "symbol": "ICICIBANK", "setup": "orb", "side": "buy",
                "date": "2026-10-30", "status": "triggered", "time": "9:35 AM", "bar_start": "09:30",
                "bar_index": 3, "price": 1394.5, "level": 1390.9, "interval": "5m",
                "opening_range": {"high": 1390.9, "low": 1383.4, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-30T04:05:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)
        self.assertEqual(sorted(e), sorted(list(want) + ["reason"]))

    def test_check_at_933_waits(self):
        code, text, _ = run(["check", "ICICIBANK", "--setup", "orb", "--offline", BUNDLE, "--now", "2026-10-30 9:33"])
        self.assertEqual(code, 1)
        self.assertIn("WAITING  too early", text)
        self.assertIn("The 9:30 bar is still forming", text)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "ICICIBANK", "--offline", ONE, "--now", "2026-10-30 9:33", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "ICICIBANK", "--offline", ONE, "--now", "2026-10-30 9:33", "--write", "e.json"])
        with open("e.json", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_file_kinds(self):
        # a single v8 file, read as of its own time
        code, text, _ = run(["check", "ICICIBANK", "--offline", ONE])
        self.assertEqual(code, 0)
        self.assertIn("10:49 IST", text)
        # a folder of gzipped files, and a .NS spelling of the symbol
        os.mkdir("charts")
        with open(ONE, "rb") as src, gzip.open(os.path.join("charts", "ICICIBANK.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "icicibank.ns", "--offline", "charts", "--now", "2026-10-30 09:35"])
        self.assertEqual(code, 0)
        # the bundle without --now: as of its own last trade, Oct 30 15:28:23
        code, text, _ = run(["check", "ICICIBANK", "--offline", BUNDLE])
        self.assertEqual(code, 0)
        self.assertIn("Fri 2026-10-30 15:28 IST", text)
        code, _, err = run(["check", "RELIANCE", "--offline", ONE])
        self.assertEqual(code, 1)
        self.assertIn("holds ICICIBANK, not RELIANCE", err)
        code, _, err = run(["check", "ZZZZ", "--offline", BUNDLE])
        self.assertIn("bundle has no ZZZZ", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "ICICIBANK", "--offline", ONE, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "ICICIBANK", "--offline", ONE, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "ICICIBANK", "--offline", ONE, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:30"}, fh)
        code, text, _ = run(["check", "ICICIBANK", "--offline", BUNDLE, "--now", "2026-10-30 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"holidays": ["2026-10-30"]}, fh)
        code, text, _ = run(["check", "ICICIBANK", "--offline", BUNDLE, "--now", "2026-10-30 09:40"])
        self.assertIn("MARKET CLOSED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "ICICIBANK", "--offline", ONE])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_bundle_and_writes_the_entry(self):
        code, text, _ = run(["watch", "ICICIBANK", "--offline", BUNDLE, "--now", "2026-10-30 09:20", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:30 bar  closed ₹1,394.50  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["bar_index"], 3)
        code, text, _ = run(["watch", "ICICIBANK", "--offline", BUNDLE, "--now", "2026-10-30 09:20",
                             "--until", "09:30", "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "nse-scanner", "hits": [
                {"symbol": "ICICIBANK", "setups": ["orb"], "or_high": 1390.9, "or_low": 1383.4},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "RELIANCE", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", BUNDLE, "--now", "2026-10-30 09:35"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(re.match(r"ICICIBANK\s+triggered\s+9:35 AM\s+₹1,394.50\s+₹1,390.90", rows[1]), rows[1])
        self.assertIn("error: the offline bundle has no ZZZZ", rows[2])
        self.assertTrue(rows[3].startswith("RELIANCE"))
        self.assertIn("of 3 triggered", text)
        os.mkdir("charts")
        shutil.copy(ONE, os.path.join("charts", "ICICIBANK.json"))
        shutil.copy(DEMO, os.path.join("charts", "RELIANCE.json"))
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-30 09:33", "--json"])
        self.assertEqual(code, 1)
        out = json.loads(text)
        self.assertEqual(out[0]["result"]["status"], "waiting")
        self.assertIn("no offline file", out[1]["error"])
        self.assertIn("holds DEMO, not RELIANCE", out[2]["error"])
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /nse-scanner first", err)


class SkillDoc(unittest.TestCase):
    def text(self):
        with open(os.path.join(ROOT, "nse-entry", "SKILL.md"), encoding="utf-8") as fh:
            return fh.read()

    def test_skill_md(self):
        text = self.text()
        self.assertTrue(text.startswith("---\nname: nse-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "most people who day trade lose money", "sebi", "/nse-exit",
                     "longs only", "in the background", "/loop", "nothing was placed", "9:35 am", "₹1,394.50",
                     "₹1,390.90", "₹1,383.40", "bar 3", "ist", "on windows, `python` works too",
                     "not financial advice"):
            self.assertIn(must, low)
        for never in ("—", "tick " + "by tick", "real " + "time", "pro" + "fit"):
            self.assertNotIn(never, low)

    def test_no_us_leftovers(self):
        for path in (os.path.join(ROOT, "nse-entry", "SKILL.md"), os.path.join(ROOT, "nse-entry", "entry.py")):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            name = os.path.basename(path)
            for word in ("NYSE", "TSLA", "New York", "Alpaca", "SEC ", "trader-entry", "trader-exit",
                         "trader-scanner", "EDT", "EST", "dollar", "the cent"):
                self.assertNotIn(word, text, (name, word))
            # "$" only as in ${CLAUDE_SKILL_DIR} or a regex anchor, never a price
            self.assertIsNone(re.search(r"\bET\b|\$\d|\$%|\$\.", text), name)

    def test_the_example_numbers_come_from_the_tool(self):
        text = self.text()
        r = at("2026-10-30 9:35")
        for line in entry.describe(r)[1:]:
            self.assertIn(line, text)
        w = at("2026-10-30 9:33")
        for line in entry.describe(w)[2:]:
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
