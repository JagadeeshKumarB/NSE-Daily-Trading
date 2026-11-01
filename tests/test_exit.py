"""nse-exit tests. Bars for the ATR stop come from the shared NSE bundle
tests/fixtures/nse/nifty50-5m-60d.json.gz (real Yahoo 5-minute bars of the
Nifty 50, Aug 5 to Oct 30 2026). tests/fixtures/nse-exit/entry-icicibank.json
is trader/entry.json as nse-entry writes it for the reference example."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

try:
    from _load import load, FIX, ROOT
except ImportError:                      # python -m unittest tests.test_exit from the repo root
    from tests._load import load, FIX, ROOT

exitp = load("nse-exit", "exit")

BUNDLE = os.path.join(FIX, "nse", "nifty50-5m-60d.json.gz")
ENTRY_FILE = os.path.join(FIX, "nse-exit", "entry-icicibank.json")
with open(ENTRY_FILE, encoding="utf-8") as _fh:
    ICICI_ENTRY = json.load(_fh)

# trader/plan.json for the reference example, exactly as the contract writes it
ICICI_PLAN = {"created_by": "nse-exit", "symbol": "ICICIBANK", "exchange": "NSE", "product": "MIS", "side": "buy",
              "date": "2026-10-30", "setup": "orb", "entry": 1394.5, "entry_time": "9:35 AM", "entry_bar": 3,
              "interval": "5m", "opening_range": {"high": 1390.9, "low": 1383.4, "minutes": 15},
              "stop": 1387.1, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
              "target": 1401.9, "target_r": 1.0, "target_label": "1R (one times the risk)",
              "risk_per_share": 7.4, "reward_risk": 1.0, "flat_by": "15:10", "tick": 0.1}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1390.9, ol=1383.4, atr=None, n=None, symbol="ICICIBANK", config=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, symbol, config)


class Math(unittest.TestCase):
    def test_the_example_or_mid_1r(self):
        p = c(1394.5, "or-mid", "1R")
        self.assertEqual(p["tick"], Decimal("0.10"))
        self.assertEqual(p["stop"], Decimal("1387.10"))          # 1387.15 rounded down on the 0.10 tick
        self.assertEqual(p["risk"], Decimal("7.40"))
        self.assertEqual(p["target"], Decimal("1401.90"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertIn("rounded down to the ₹0.10 tick", p["stop_detail"])
        self.assertIn("₹1,387.15", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(1394.5, "or-mid")
        self.assertEqual(p["target"], Decimal("1409.30"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_ticks(self):
        p = c(1394.5, "or-mid", "1R", 1390.9000244140625, 1383.4000244140625)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1394.50"), Decimal("1387.10"), Decimal("1401.90")))
        p = c(1394.4999, "or-mid", "1R")
        self.assertEqual(p["entry"], Decimal("1394.50"))

    def test_other_stops(self):
        p = c(1394.5, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1383.40"), Decimal("11.10"), Decimal("1416.70")))
        p = c(1394.5, "price:1387.19")
        self.assertEqual(p["stop"], Decimal("1387.10"))             # rounds down, away from the entry
        p = c(1394.5, "price:₹1,387.10")
        self.assertEqual(p["stop"], Decimal("1387.10"))
        p = c(1394.5, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1380.50"), Decimal("14.00"), Decimal("1422.50")))
        self.assertIn("₹1,380.555", p["stop_detail"])
        p = c(1394.5, "atr:1.5", atr=4.999969482421875, n=4)
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1387.00"), Decimal("7.50"), Decimal("1409.50")))
        self.assertEqual(p["stop_label"], "1.5 x ATR(4) under the entry")
        self.assertIn("fewer than 14", p["stop_detail"])

    def test_tick_grid_by_price(self):
        # under 250: 0.01; 250-1000: 0.05; 1000-5000: 0.10. One tick for the whole plan, from the entry.
        p = c(240.37, "price:238.004", "1R", None, None, symbol="X")
        self.assertEqual((p["tick"], p["stop"], p["target"]), (Decimal("0.01"), Decimal("238.00"), Decimal("242.74")))
        p = c(612.35, "price:607.12", "1.5R", None, None, symbol="X")
        self.assertEqual((p["tick"], p["stop"]), (Decimal("0.05"), Decimal("607.10")))
        self.assertEqual(p["target"], Decimal("620.25"))            # 612.35 + 1.5 x 5.25 = 620.225 -> 620.25
        p = c(1001.0, "price:998.12", "1R", None, None, symbol="X")
        self.assertEqual((p["tick"], p["stop"]), (Decimal("0.10"), Decimal("998.10")))   # stays on the entry's grid
        p = c(1001.0, "price:972", "1R", None, None, symbol="MRF", config={"tick_size": {"MRF": 5}})
        self.assertEqual((p["tick"], p["entry"], p["stop"], p["target"]),
                         (Decimal("5"), Decimal("1000.00"), Decimal("970.00"), Decimal("1030.00")))

    def test_target_rounds_to_the_nearest_tick_and_price_targets(self):
        p = c(1394.5, "price:1387.1", "1.25R", None, None)
        self.assertEqual(p["target"], Decimal("1403.80"))           # 1394.5 + 9.25 = 1403.75 -> half up
        self.assertEqual(p["target_label"], "1.25R (1.25 times the risk)")
        p = c(1394.5, "or-mid", "price:1404.32")
        self.assertEqual(p["target"], Decimal("1404.30"))
        self.assertEqual(p["reward_risk"], Decimal("1.32"))
        self.assertEqual(p["target_label"], "your price (1.32R)")
        self.assertEqual(p["target_r"], Decimal("1.32"))

    def test_refusals_say_why(self):
        cases = [("price:1394.5", "2R", "at or above the entry"),
                 ("price:1400", "2R", "at or above the entry"),
                 ("or-mid", "price:1394.5", "at or below the entry"),
                 ("price:1394.10", "2R", "too tight to survive the spread"),
                 ("pct:0.02", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1394.5, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # five ticks of 0.10: a 0.50 risk is allowed, 0.40 is not
        self.assertEqual(c(1394.5, "price:1394.00")["risk"], Decimal("0.50"))
        with self.assertRaises(exitp.PlanError) as cm:
            c(1394.5, "price:1394.10")
        self.assertIn("fewer than 5 ticks", str(cm.exception))
        with self.assertRaises(exitp.PlanError):
            c(1386.0, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1394.5, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1394.5, "or-mid", oh=1383.4, ol=1390.9)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("ICICIBANK", "5m", None, BUNDLE)
        day = exitp.datetime.date(2026, 10, 30)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=3)
        self.assertEqual(len(bars), 4)                              # 9:15, 9:20, 9:25 and the 9:30 entry bar
        self.assertEqual(exitp.ts_to_ist(bars[0]["t"]).strftime("%H:%M"), "09:15")
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1390.8 - 1383.4, places=3)   # the first bar: no overnight gap
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 4)
        self.assertAlmostEqual(atr, sum(trs) / 4, places=9)
        self.assertAlmostEqual(atr, 5.0, places=3)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertGreater(len(all_bars), 14)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        # a holiday has no session; Yahoo symbols and a symbol not in the bundle
        self.assertEqual(exitp.session_bars(ch, exitp.datetime.date(2026, 10, 20), "5m"), [])
        self.assertEqual(len(exitp.load_chart("ICICIBANK.NS", "5m", None, BUNDLE)["bars"]), len(ch["bars"]))
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("NOSUCH", "5m", None, BUNDLE)
        self.assertIn("has no NOSUCH", str(cm.exception))

    def test_ist_clock(self):
        utc = exitp.parse_now("2026-10-30 09:35")
        self.assertEqual(utc, exitp.datetime.datetime(2026, 10, 30, 4, 5))
        self.assertEqual(exitp.parse_now("2026-10-30 09:35 IST"), utc)
        self.assertEqual(exitp.parse_now("2026-10-30T04:05:00Z"), utc)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-30 09:36"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        shutil.copy(ENTRY_FILE, os.path.join("trader", "entry.json"))
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in ICICI_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Exit plan: ICICIBANK long on NSE (MIS, intraday), Fri 2026-10-30", text)
        self.assertIn("Entry         ₹1,394.50 at 9:35 AM (bar 3)", text)
        self.assertIn("Stop          ₹1,387.10  opening-range midpoint: (₹1,390.90 + ₹1,383.40) / 2 = ₹1,387.15, "
                      "rounded down to the ₹0.10 tick", text)
        self.assertIn("Target        ₹1,401.90  1R (one times the risk)", text)
        self.assertIn("Risk a share  ₹7.40 (0.53% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:10 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/nse-budget counts them", text)
        self.assertIn("/nse-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1409.3)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-30 09:40"])
        self.assertEqual(code, 0)
        self.assertIn("₹1,409.30", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-02 09:40"])
        self.assertIn("not today", text)
        code, text, _ = run(exitp, ["show", "--json"])
        self.assertEqual(json.loads(text)["exchange"], "NSE")

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "icicibank", "--entry", "1394.5", "--or-high", "1390.9",
                                  "--or-low", "1383.4", "--time", "9:35", "--entry-bar", "3", "--date", "2026-10-30",
                                  "--target", "1R", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in ICICI_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        code, text, _ = self.plan("--symbol", "ICICIBANK", "--entry", "1394.47", "--stop", "price:1387.1", "--json")
        p = json.loads(text)
        self.assertEqual(p["entry"], 1394.5)
        self.assertIn("not on the ₹0.10 tick", p["warnings"][0])

    def test_atr_stop_reads_the_bundle(self):
        shutil.copy(ENTRY_FILE, os.path.join("trader", "entry.json"))
        code, text, err = self.plan("--stop", "atr:1.5", "--target", "1R", "--offline", BUNDLE, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1387.0, 7.5, 1402.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 4))
        self.assertAlmostEqual(p["atr"], 5.0, places=3)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", BUNDLE, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(ICICI_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("ICICIBANK is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1391.0", "--json")    # plan ahead just over the range high
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1391.0, 1387.1, None))

        self.write(os.path.join("trader", "entry.json"), dict(ICICI_ENTRY, setup="level", level=1390.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("plans longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(ICICI_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        self.assertIn("/nse-entry", err)
        code, _, err = self.plan("--symbol", "ICICIBANK")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "ICICIBANK", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "ICICIBANK", "--entry", "1394.5", "--or-high", "1390.9")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_mis_square_off_and_late_entries(self):
        shutil.copy(ENTRY_FILE, os.path.join("trader", "entry.json"))
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])

        # MIS: Zerodha squares off from 15:20, so a flat-by at or after it is refused
        for t in ("15:20", "15:25"):
            code, _, err = self.plan("--flat-by", t)
            self.assertEqual(code, 1, t)
            self.assertIn("Zerodha starts squaring off", err)
        self.assertEqual(json.loads(self.plan("--flat-by", "15:19", "--json")[1])["flat_by"], "15:19")
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:20"})
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("flat_by in the config 15:20", err)
        # CNC (delivery) is not held to 15:20, only to the 15:30 close, and it says it is not a day trade
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:25", "product": "cnc"})
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual((p["product"], p["flat_by"]), ("CNC", "15:25"))
        self.assertIn("not a day trade", " ".join(p["warnings"]))
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertIn("after the 3:30 PM close", err)
        self.write(os.path.join("trader", "config.json"), {"product": "NRML"})
        self.assertIn("MIS (intraday) or CNC", self.plan()[2])
        os.remove(os.path.join("trader", "config.json"))

        self.write(os.path.join("trader", "entry.json"), dict(ICICI_ENTRY, time="3:15 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_holiday_and_unknown_year(self):
        self.write(os.path.join("trader", "entry.json"), dict(ICICI_ENTRY, date="2026-11-10"))
        p = json.loads(run(exitp, ["plan", "--now", "2026-11-10 09:36", "--json"])[1])
        self.assertIn("NSE is closed on 2026-11-10", " ".join(p["warnings"]))
        self.write(os.path.join("trader", "entry.json"), dict(ICICI_ENTRY, date="2027-02-01"))
        p = json.loads(run(exitp, ["plan", "--now", "2027-02-01 09:36", "--json"])[1])
        self.assertIn("2027 holiday list is not built in", " ".join(p["warnings"]))

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "nse-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: nse-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "most people who day trade lose money",
                     "sebi's study of fy2023", "/nse-timemachine", "/nse-budget", "longs only", "₹1,394.50",
                     "₹1,387.10", "₹1,401.90", "₹7.40", "1,409.30", "too tight to survive the spread", "15:10",
                     "15:20", "mis", "tick", "not financial advice", "on windows, `python` works too"):
            self.assertIn(must, low)
        for never in ("—","tick " + "by tick", "real " + "time", "pro" + "fit", "nyse", "tsla",
                      "new york", " et ", "alpaca", "trader-exit", "/trader-"):
            self.assertNotIn(never, low)
        self.assertNotRegex(low, r"\$\d|\bcents?\b")     # no dollars, no cents (${CLAUDE_SKILL_DIR} is fine)


if __name__ == "__main__":
    unittest.main()
