import calendar
import datetime
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

try:
    from _load import load, FIX
except ImportError:
    from tests._load import load, FIX

tm = load("nse-timemachine", "timemachine")

BUNDLE = os.path.join(FIX, "nse", "nifty50-5m-60d.json.gz")   # {"SYMBOL": Yahoo v8 chart}, 5m bars Aug 5 - Oct 30 2026
PLAN_FILE = os.path.join(FIX, "nse-timemachine", "plan.json")  # the ICICIBANK reference trade, as nse-exit writes it
with gzip.open(BUNDLE, "rt", encoding="utf-8") as _fh:
    TEXT = json.dumps(json.load(_fh)["ICICIBANK"])
with open(PLAN_FILE, encoding="utf-8") as _fh:
    PLAN = json.load(_fh)
NOW = "2026-10-30 09:35"
NOW_UTC = tm.parse_now(NOW)

# fingerprints of the seed-19 run on the fixture: any change in the draws shows up here
BANDS = "cf969057e6585de6"
PATHS = "d3c85ad49de2c91f"
TRADES = "6b73bbc4f2bc4812"
HIST = {"lo": 1367.4, "hi": 1419.34,
        "bins": [103, 97, 149, 196, 282, 333, 455, 524, 650, 685, 748, 795, 789, 731, 647, 609, 508, 425, 332, 269,
                 204, 150, 89, 68]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def stamp(y, mo, d, h, mi):
    """A UTC timestamp for an IST wall-clock time."""
    return calendar.timegm(tm.ist_to_utc(datetime.datetime(y, mo, d, h, mi)).utctimetuple())


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Reference(unittest.TestCase):
    """ICICIBANK, Friday 2026-10-30: entry 1394.50 at 9:35 AM, stop 1387.10, 1R target 1401.90."""

    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:10"))
        self.assertEqual(m["pool_sessions"], 59)
        self.assertEqual(m["bars_left"], 67)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-05", "2026-10-29"))
        self.assertEqual(m["p_target"], 0.4067)
        self.assertEqual(m["p_stop"], 0.5199)
        self.assertEqual(m["p_neither"], 0.0734)
        self.assertEqual(m["p_green"], 0.4385)
        self.assertEqual(m["median_close"], 1393.16)
        self.assertEqual(m["p5_close"], 1375.06)
        self.assertEqual(m["p95_close"], 1411.65)
        self.assertEqual(m["avg_R"], -0.119)
        self.assertEqual(m["avg_R_net"], -0.281)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)
        self.assertEqual(m["bands"][-1], [1376.42, 1385.84, 1392.99, 1400.49, 1411.48])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (58, 36))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (12, 20, 4, 13))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (-0.219, -0.408))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        self.assertEqual(h["trades"][0], {"date": "2026-08-06", "time": "11:15 AM", "entry": 1456.3, "stop": 1452.1,
                                          "target": 1460.5, "outcome": "stop", "R": -1.0, "R_net": -1.22})
        dates = [t["date"] for t in h["trades"]]
        self.assertNotIn("2026-10-15", dates)    # missing bars: left out of the history
        self.assertNotIn("2026-10-30", dates)    # today is never in the history

    def test_history_prices_sit_on_the_tick(self):
        for t in self.doc["history"]["trades"]:
            for k in ("entry", "stop", "target"):
                self.assertAlmostEqual(t[k] * 10, round(t[k] * 10), places=6, msg=t)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Leans toward the stop: target first 40.7 % vs stop first 52.0 %.")
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.439, 0.5))
        self.assertEqual(self.doc["verdict_notes"], [])

    def test_the_charges(self):
        ch = self.doc["charges"]
        self.assertEqual((ch["product"], ch["qty"], ch["qty_from"]), ("MIS", 67, "max_loss_per_trade_inr 500"))
        self.assertEqual(ch["at_target"], tm.charges(1394.5 * 67, 1401.9 * 67, "MIS")["total"])
        self.assertEqual(ch["at_stop"], tm.charges(1394.5 * 67, 1387.1 * 67, "MIS")["total"])
        self.assertEqual((ch["at_target"], ch["at_stop"], ch["R_at_target"]), (80.49, 80.2, 0.162))
        self.assertEqual((ch["avg_R_net"], ch["history_avg_R_net"]), (-0.281, -0.408))

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1394.5] * 5)
        self.assertEqual(len(m["bands"]), 68)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:35", "15:10", 68))
        self.assertTrue(all(p[0] == 1394.5 and len(p) == 68 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("ICICIBANK", 1394.5, 1387.1, 1401.9, 3, "9:35 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (7.4, 1.0, "15:10"))
        self.assertEqual((p["exchange"], p["product"], p["tick"]), ("NSE", "MIS", 0.1))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["created_by"], "nse-timemachine")
        self.assertEqual(self.doc["data"]["last_bar"], "2026-10-30 09:30 IST")
        self.assertEqual(self.doc["data"]["short_sessions_left_out"],
                         [{"date": "2026-10-15", "bars": 72, "missing": ["11:45", "11:50"]}])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("ICICIBANK buy, Friday 2026-10-30: entry 1394.50 at 9:35 AM (bar 3), stop 1387.10, "
                     "target 1401.90 (1R, risk 7.40 a share).",
                     "10,000 replays", "9:35 AM to 3:10 PM IST (67 five-minute bars)", "seed 19", "59 sessions",
                     "target first   40.7 %", "stop first     52.0 %", "neither        7.3 %    still open at 3:10 PM",
                     "green          43.9 %", "median 1393.16, 90 % of replays between 1375.06 and 1411.65",
                     "Average R: -0.119",
                     "Charges (MIS, estimate) for 67 shares (the shares that risk ₹500 at the stop): about ₹80.49 a "
                     "round trip at the target, ₹80.20 at the stop, 0.16R a trade.",
                     "Average R after charges: -0.281.",
                     "fired 36 times: 12 target, 20 stop, 4 neither. Average R -0.219, -0.408 after charges.",
                     "Leans toward the stop: target first 40.7 % vs stop first 52.0 %.",
                     "2026-10-15 (no 11:45, 11:50)", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$", "New York", " ET"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.4001, 0.5203, 1393.29))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; this pins the first draws so a
        # failure in the numbers above points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "ICICIBANK")

    def test_the_clock_and_the_empty_end_of_day_bars(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        # 127 bars without a price (Yahoo leaves 15:15 to 15:25 empty, and Oct 15 11:45, 11:50);
        # Oct 30 after 9:30 has not closed yet at 9:35
        self.assertEqual(dropped, {"no_price": 127, "off_grid": 0, "not_complete": 70, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-30", "09:30", 3))
        self.assertEqual((last["h"], last["c"]), (1394.5, 1394.5))
        # with the real clock (any time later) the data's own time (15:28:23) still marks 15:25 as in progress
        later = tm.parse_bars(self.result, tm.parse_now(None))[1]
        self.assertEqual(later[-1]["start"], "15:15")    # 15:20 and 15:25 have no price that day

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-30 09:32"))[1]
        self.assertEqual(len([b for b in bars if b["date"] == "2026-10-30"]), 3)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-30 09:32"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:25 bar", str(cm.exception))
        self.assertEqual(tm.parse_now("2026-10-30 09:35"), datetime.datetime(2026, 10, 30, 4, 5))
        self.assertEqual(tm.parse_now("2026-10-30T04:05Z"), datetime.datetime(2026, 10, 30, 4, 5))

    def test_ist_has_no_daylight_saving(self):
        stamps = [stamp(2026, 10, 30, 9, 15), stamp(2026, 11, 2, 9, 15),   # either side of the US clock change
                  stamp(2026, 11, 2, 9, 10),                                # before the open
                  stamp(2026, 11, 2, 15, 25), stamp(2026, 11, 2, 15, 30)]   # the last bar; the close
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 5, "high": [1] * 5, "low": [1] * 5, "close": [1] * 5,
                                         "volume": [0] * 5}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-11-03T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-10-30", "09:15", 0), ("2026-11-02", "09:15", 0), ("2026-11-02", "15:25", 74)])
        self.assertEqual(dropped["outside_hours"], 2)
        self.assertEqual(stamps[0] % 86400, 3 * 3600 + 45 * 60)    # 03:45 UTC
        self.assertEqual(tm.SESSION_BARS, 75)

    def test_a_gap_keeps_the_pool_but_leaves_the_history(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_sessions"], 59)
        self.assertEqual(doc["history"]["sessions"], 58)
        self.assertIn("Missing bars in 1 session(s): 2026-10-15 (no 11:45, 11:50). The replays draw those times "
                      "from the other sessions; the history leaves those days out.", "\n".join(tm.report(doc)))

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None     # 2026-08-05 09:40
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 128)
        self.assertEqual(doc["mc"]["pool_sessions"], 59)
        self.assertEqual(doc["history"]["sessions"], 57)
        self.assertEqual(doc["data"]["short_sessions_left_out"][0], {"date": "2026-08-05", "bars": 71,
                                                                     "missing": ["09:40"]})

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-29")

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "ICICIBANK.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        dates = sorted({ist(ts).date().isoformat() for ts in tm.chart_result(TEXT, "X")["timestamp"]})
        self.assertEqual((len(dates), dates[0], dates[-1]), (60, "2026-08-05", "2026-10-30"))

        def keep_from(first):
            return edited(lambda res: keep_bars(res, lambda ts: ist(ts).date().isoformat() >= first))
        with self.assertRaises(tm.TMError) as cm:
            machine(text=keep_from(dates[-20]))
        self.assertIn("only 19 past session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))
        # 20 sessions, but one of them (Oct 15) has no 11:45 bar: every bar the replay draws needs 20
        with self.assertRaises(tm.TMError) as cm:
            machine(text=keep_from(dates[-21]))
        self.assertEqual(str(cm.exception), "only 19 past session(s) of ICICIBANK have the 11:45 bar (and the one "
                                            "before it); the time machine needs at least 20")
        self.assertEqual(machine(text=keep_from(dates[-22]), replays=200)["mc"]["pool_sessions"], 21)

    def test_the_last_bars_of_the_day_are_empty(self):
        doc = machine(until="15:20", replays=500)
        self.assertEqual((doc["mc"]["until"], doc["mc"]["bars_left"]), ("15:20", 69))
        self.assertEqual(doc["history"]["sessions"], 39)   # 19 sessions have no 15:15 bar, Oct 15 has a gap
        with self.assertRaises(tm.TMError) as cm:
            machine(until="15:30", replays=10)
        self.assertIn("only 0 past session(s) of ICICIBANK have the 15:20 bar", str(cm.exception))
        self.assertIn("--until 15:10", str(cm.exception))

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="SBIN"))
        self.assertIn("data is for ICICIBANK", str(cm.exception))

    def test_the_bundle(self):
        with gzip.open(BUNDLE, "rt", encoding="utf-8") as fh:
            bundle = fh.read()
        self.assertEqual(tm.chart_result(bundle, "ICICIBANK")["meta"]["symbol"], "ICICIBANK.NS")
        self.assertEqual(tm.chart_result(bundle, "M&M")["meta"]["symbol"], "M&M.NS")
        with self.assertRaises(tm.TMError) as cm:
            tm.chart_result(bundle, "ZZZZ")
        self.assertIn("bundle has no ZZZZ (it holds 50 symbols)", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        for date, words in (("2026-10-20", "NSE is closed that day"),   # Dussehra
                            ("2026-10-31", "NSE is closed that day"),   # a Saturday
                            ("2026-11-02", "no bars for that day")):
            with self.assertRaises(tm.TMError) as cm:
                machine(plan(date=date))
            self.assertIn("not in the data", str(cm.exception))
            self.assertIn(words, str(cm.exception), date)
        with self.assertRaises(tm.TMError) as cm:      # 9:40 has not closed in this data yet
            machine(plan(entry_time="9:40 AM", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="9:37 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:30 PM", entry_bar=None), "between 9:20 AM and 3:25 PM"),
            (dict(entry_time="9:15 AM", entry_bar=None), "between 9:20 AM and 3:25 PM"),
            (dict(entry_bar=4), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1395.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Oct 30"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(tick="x"), "tick must be a number"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_symbols(self):
        self.assertEqual(plan(symbol="icicibank.ns")["symbol"], "ICICIBANK")
        self.assertEqual(plan(symbol="M&M")["symbol"], "M&M")
        self.assertEqual(plan(symbol="BAJAJ-AUTO")["symbol"], "BAJAJ-AUTO")

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "9:35 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="09:35", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="9:35am", entry_bar=None)["entry_bar"], 3)
        self.assertEqual(plan(entry_time="2:30", entry_bar=None)["entry_bar"], 62)   # a bare 2:30 is p.m.

    def test_until(self):
        doc = machine(until="14:30", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("14:30", 59, "14:30"))
        self.assertIn("still open at 2:30 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="2:30 PM", replays=500)["mc"], m)
        # no --until: the plan's flat_by, else 15:10
        self.assertEqual(machine(plan(flat_by="14:30"), replays=500)["mc"], m)
        self.assertEqual(machine(plan(flat_by=None), replays=100)["mc"]["until"], "15:10")
        for bad, words in (("9:35 AM", "nothing to replay"), ("15:35", "no later than 3:30 PM"),
                           ("15:12", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(flat_by="15:12"), replays=10)
        self.assertIn("the plan's flat_by", str(cm.exception))

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1398.0, stop=1390.0, target=1406.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stops_round_to_the_tick_away_from_the_entry(self):
        self.assertEqual(tm.tick_away(1387.15, True), 1387.1)
        self.assertEqual(tm.tick_away(1387.15, False), 1387.2)
        self.assertEqual(tm.tick_away(1383.4000244140625, True), 1383.4)    # Yahoo's float noise
        self.assertEqual(tm.tick_away(1383.3999999999999, True), 1383.4)
        self.assertEqual(tm.tick_away(245.567, True), 245.56)              # under 250: 1 paisa
        self.assertEqual(tm.tick_near(1394.5000001), 1394.5)
        self.assertEqual(tm.tick_away(1387.17, True, "ICICIBANK", {"tick_size": {"ICICIBANK": 0.05}}), 1387.15)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1383.4, target=1405.6), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1390.0, target=1403.5), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.3227, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 59)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1401.9, target=1387.1, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))
        self.assertEqual(doc["charges"]["at_stop"], tm.charges(1401.9 * 67, 1394.5 * 67)["total"])

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 3:10 PM: the price at 3:10 PM decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_charges_that_eat_the_edge_are_said(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095,
              "avg_R_net": -0.02}
        self.assertEqual(tm.verdict_notes(mc), ["After estimated charges the average R is -0.020: the charges eat "
                                                "the edge."])
        self.assertEqual(tm.verdict_notes(dict(mc, avg_R_net=0.01)), [])

    def test_2r_on_the_fixture(self):
        # the same entry and stop with a 2R target 1409.30: target first 14.5 % vs stop first 56.5 %
        doc = machine(plan(target=1409.3))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"], m["avg_R_net"]),
                         (2.0, 0.1453, 0.5647, 0.29, -0.145, -0.307))
        self.assertEqual(doc["verdict"], "Leans toward the stop: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 20 % (target first 14.5 % vs stop "
                                         "first 56.5 %).")
        self.assertEqual(doc["verdict_notes"], [])

    def test_the_quantity_for_the_charges(self):
        ch = machine(replays=200, qty=10)["charges"]
        self.assertEqual((ch["qty"], ch["qty_from"], ch["at_target"]), (10, "--qty", 14.87))
        ch = machine(replays=200, config={"max_loss_per_trade_inr": 1000})["charges"]
        self.assertEqual(ch["qty"], 135)
        ch = machine(replays=200, config={"max_loss_per_trade_inr": 5})["charges"]
        self.assertEqual(ch["qty"], 1)                                   # never 0 shares
        with self.assertRaises(tm.TMError):
            machine(replays=10, qty=0)


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("icicibank"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/ICICIBANK.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("M&M", "query1"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("BAJAJ-AUTO"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; day-trader-skill/2.0; "
                                "+https://github.com/Jakeschincariol/day-trader-skill)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("ICICIBANK"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("ICICIBANK"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("ICICIBANK"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ICICIBANK")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, err = run(["run", "--plan", p, "--now", NOW, "--replays", "300",
                                   "--config", os.path.join(d, "none.json")])
            self.assertEqual(code, 1)                    # a --config that is not there is an error
            self.assertIn("no config at", err)
            code, text, err = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", BUNDLE, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   40.7 %", text)
        self.assertIn("Leans toward the stop: target first 40.7 % vs stop first 52.0 %.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/nse-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history", "charges") if k in doc),
                         ["charges", "history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "nse-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.4067, -0.119))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (36, -0.219))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", BUNDLE, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.5199)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:21], "Leans toward the stop")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "icicibank", "--date", "2026-10-30", "--entry", "1394.50",
                               "--stop", "1387.10", "--target", "1401.90", "--entry-time", "9:35 AM",
                               "--offline", BUNDLE, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"], doc["mc"]["until"]), (0.4067, 0.4385, "15:10"))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (12, 20, -0.219))
        code, _, err = run(["run", "--symbol", "ICICIBANK", "--entry", "1394.5", "--offline", BUNDLE])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        with gzip.open(os.path.join(self.d, "ICICIBANK-60d5m.json.gz"), "wt", encoding="utf-8") as fh:
            fh.write(TEXT)
        with open(os.path.join(self.d, "trader", "config.json"), "w") as fh:
            json.dump({"max_loss_per_trade_inr": 1000}, fh)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertIn("for 135 shares (the shares that risk ₹1,000 at the stop)", text)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", BUNDLE, "--now", NOW, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", BUNDLE, "--now", NOW, "--until", "15:30"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertIn("/nse-exit", run(["run", "--plan", os.path.join(self.d, "nope.json")])[2])
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", BUNDLE, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("ICICIBANK 2026-10-30, 2,000 replay paths from the 9:35 AM entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1401.90 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1387.10 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1394.50 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("9:35", text)
        self.assertIn("3:10", text)
        self.assertEqual(lines[-1], "Leans toward the stop: target first 40.7 % vs stop first 52.0 %.")
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", BUNDLE, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
