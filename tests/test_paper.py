"""nse-paper: the yes gate, the risk link and the replay, on the real ICICIBANK day (Oct 30 2026)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, ROOT, load  # noqa: E402

paper = load("nse-paper", "paper")
risk = load("nse-risk", "check")
BUNDLE = os.path.join(FIX, "nse", "nifty50-5m-60d.json.gz")
ORDERS = os.path.join(FIX, "nse-budget", "orders-icicibank.json")


def rd(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def rj(path):
    return json.loads(rd(path))


def wj(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


class Paper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.getcwd()
        os.chdir(self.dir)
        os.makedirs("trader/state")
        shutil.copy(ORDERS, "trader/orders.json")
        shutil.copy(os.path.join(ROOT, "nse-budget", "config.example.json"), "trader/config.json")
        self.order = rj("trader/orders.json")["orders"][0]

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.dir)

    def run_paper(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = paper.main(list(args))
        return code, out.getvalue()

    def passed(self, date="2026-10-30", verdict="PASS"):
        doc = {"date": date, "results": [{"index": 1, "fingerprint": paper.fingerprint(self.order),
                                          "verdict": verdict, "reasons": ["size: too big"]}]}
        wj("trader/state/risk.json", doc)

    def test_fingerprint_matches_nse_risk(self):
        self.assertEqual(paper.fingerprint(self.order), risk.fingerprint(self.order))
        self.assertEqual(paper.KITE_FIELDS, risk.KITE_FIELDS)

    def test_india_block_is_verbatim(self):
        src = rd(os.path.join(ROOT, "ref", "india_common.py"))
        start = src.index("# ---------------------------------------------------------------- the India block")
        end = src.index("# ---------------------------------------------------------------- end of the India block")
        mine = rd(os.path.join(ROOT, "nse-paper", "paper.py"))
        self.assertIn(src[start:end], mine)

    def test_no_risk_check_is_not_ready(self):
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("nse-risk has not checked", out)
        self.assertFalse(os.path.exists("trader/state/paper.json"))

    def test_blocked_or_stale_is_not_ready(self):
        self.passed(verdict="BLOCK")
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by nse-risk: size: too big", out)
        self.passed(date="2026-10-29")
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertIn("the risk check is from 2026-10-29", out)

    def test_changed_order_is_not_ready(self):
        self.passed()
        doc = rj("trader/orders.json")
        doc["orders"][0]["quantity"] = 60
        wj("trader/orders.json", doc)
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("not in today's risk check", out)

    def test_no_yes_records_nothing(self):
        self.passed()
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1")
        self.assertEqual(code, 1)
        self.assertIn("the user types yes for THIS order", out)
        self.assertFalse(os.path.exists("trader/state/paper.json"))

    def test_paper_false_refuses(self):
        self.passed()
        cfg = rj("trader/config.json")
        cfg["paper"] = False
        wj("trader/config.json", cfg)
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("only paper trades", out)

    def test_the_icicibank_day(self):
        self.passed()
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing was sent to Zerodha", out)
        code, out = self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        self.assertIn("not recorded twice", out)
        broker = rj("trader/state/broker.json")["orders"]
        self.assertEqual([(r["status"], r["kind"], r["max_loss_with_charges"]) for r in broker],
                         [("placed", "entry", 465.52)])

        code, out = self.run_paper("--now", "2026-10-30 11:00", "settle", "--offline", BUNDLE)
        self.assertIn("open, bought 1392.10 at 9:40 AM, last 1394.10 (10:55): +₹106.00 so far", out)

        code, out = self.run_paper("--now", "2026-10-30 15:30", "settle", "--offline", BUNDLE)
        self.assertIn("bought 1392.10 at 9:40 AM, sold 1401.90 at 11:50 AM (the target): "
                      "+₹519.40 gross, ₹73.53 charges, +₹445.87 net", out)
        t = rj("trader/state/paper.json")["trades"][0]
        self.assertEqual((t["status"], t["exit"]["how"], t["net"]), ("closed", "target", 445.87))
        broker = rj("trader/state/broker.json")["orders"]
        self.assertEqual(broker[-1]["kind"], "exit")

        code, out = self.run_paper("journal")
        self.assertIn("1 closed: 1 won, 0 lost", out)
        self.assertIn("too few to tell skill from luck", out)
        code, out = self.run_paper("settle")
        self.assertEqual(code, 1)

    def test_nse_risk_counts_the_paper_trade(self):
        self.passed()
        self.run_paper("--now", "2026-10-30 09:40", "place", "1", "--yes")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = risk.main(["--now", "2026-10-30 09:45"])
        self.assertEqual(code, 1)
        self.assertIn("duplicate: a BUY ICICIBANK was already placed today", out.getvalue())
        self.assertIn("1 of 3 trades, ₹465.52 of ₹1,500 daily loss", out.getvalue())


class Replay(unittest.TestCase):
    """The rules, on hand-made bars."""
    T = {"date": "2026-10-30", "placed_at": "10:00", "symbol": "X", "quantity": 10, "product": "MIS",
         "limit": 100.0, "stop": 98.0, "sl_limit": 97.9, "target": 102.0, "flat_by": "15:10",
         "no_entries_after": "14:30"}
    CFG = dict(paper.DEFAULTS)

    def bars(self, *rows):
        import datetime
        out, t = [], datetime.datetime(2026, 10, 30, 10, 0)
        for o, h, lo, c in rows:
            out.append((t, o, h, lo, c))
            t += paper.BAR
        return out

    def now(self, hh=16):
        import datetime
        return datetime.datetime(2026, 10, 30, hh, 0)

    def test_both_in_one_bar_counts_as_the_stop(self):
        r = paper.replay(self.T, self.bars((100, 100.5, 99.5, 100), (100, 103, 97.5, 101)), self.now(), self.CFG)
        self.assertEqual(r["exit"][2], "stop")
        self.assertIn("same bar", r["notes"][0])

    def test_fill_bar_counts_for_the_stop_only(self):
        r = paper.replay(self.T, self.bars((100.5, 103, 97.9, 101), (101, 101, 100.5, 101)), self.now(), self.CFG)
        self.assertEqual(r["exit"][2], "stop")

    def test_gap_below_the_sl_limit_is_said(self):
        r = paper.replay(self.T, self.bars((100, 100.5, 99.5, 100), (96, 96.5, 95, 96)), self.now(), self.CFG)
        self.assertEqual((r["exit"][1], r["exit"][2]), (96, "stop"))
        self.assertIn("may not have filled", r["notes"][-1])

    def test_unfilled_limit_is_cancelled(self):
        r = paper.replay(self.T, self.bars((101, 102, 100.5, 101)), self.now(), self.CFG)
        self.assertEqual(r["status"], "cancelled")

    def test_open_while_the_day_runs(self):
        r = paper.replay(self.T, self.bars((100, 100.5, 99.5, 100.2)), self.now(11), self.CFG)
        self.assertEqual(r["status"], "open")


if __name__ == "__main__":
    unittest.main()
