"""nse-chart's paper desk (chart_desk.py): sizing, paper trades, the daily lock and the mood, on the saved bars."""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

live = load("nse-chart", "live")
import chart_desk as desk  # noqa: E402  (live.py put nse-chart on the path)

BUNDLE = os.path.join(FIX, "nse", "nifty50-5m-60d.json.gz")
DAY = datetime.date(2026, 10, 30)


def bars(sym, now="2026-10-30 15:40"):
    clock = live.Clock(now)
    return live.Source(clock, BUNDLE).chart(sym, "60d")[0], clock.now()


def plan(sym, now="2026-10-30 15:40"):
    b, n = bars(sym, now)
    return live.orb_plan(sym, [x for x in b if x[0].date() == DAY], n), b, n


class Size(unittest.TestCase):
    def test_icicibank_matches_nse_budget(self):
        p, _, _ = plan("ICICIBANK")
        z = desk.size_trade("buy", p["buy"]["entry"], p["buy"]["stop"], p["buy"]["targets"], desk.settings_from())
        self.assertEqual((z["qty"], z["bound_by"], z["cost"], z["loss_at_stop"]), (53, "position cap", 73717.7, 470.74))
        self.assertEqual([t["net"] for t in z["targets"]], [324.03, 721.41, 1118.8])

    def test_a_trade_small_day_halves_the_risk_and_a_loss_leaves_less(self):
        p, _, _ = plan("TITAN")
        s, b = desk.settings_from(), p["buy"]
        full = desk.size_trade("buy", b["entry"], b["stop"], b["targets"], s)
        half = desk.size_trade("buy", b["entry"], b["stop"], b["targets"], s, factor=0.5)
        after = desk.size_trade("buy", b["entry"], b["stop"], b["targets"], s, realized_today=-1300)
        self.assertEqual((full["qty"], half["qty"], after["qty"]), (8, 4, 3))
        self.assertLessEqual(full["loss_at_stop"], 500)
        self.assertLessEqual(after["loss_at_stop"], 200)
        self.assertEqual(after["bound_by"], "loss left today")

    def test_money_in_open_trades_is_not_spent_twice(self):
        p, _, _ = plan("ICICIBANK")
        b = p["buy"]
        z = desk.size_trade("buy", b["entry"], b["stop"], b["targets"], desk.settings_from(), in_use=70000)
        self.assertEqual((z["qty"], z["bound_by"]), (14, "cash"))


class PaperTrades(unittest.TestCase):
    def trade(self, **kw):
        p, _, _ = plan("ICICIBANK")
        t = {"id": 1, "symbol": "ICICIBANK", "side": "buy", "qty": 53, "mode": "armed", "entry": p["buy"]["entry"],
             "stop": p["buy"]["stop"], "target": p["buy"]["targets"][0], "target_name": "T1", "placed": "2026-10-30 09:31"}
        t.update(kw)
        return t

    def test_armed_fills_on_the_next_close_beyond_and_reaches_t1(self):
        b, now = bars("ICICIBANK")
        r = desk.replay(self.trade(), b, now, desk.settings_from())
        self.assertEqual((r["status"], r["fill"], r["exit"]), ("closed", {"price": 1391.6, "time": "09:40"},
                                                               {"how": "target", "price": 1398.4, "time": "11:10"}))
        self.assertEqual((r["charges"], r["pnl"]), (73.47, 286.93))

    def test_a_cancel_before_the_trigger_and_an_exit_by_hand(self):
        b, now = bars("ICICIBANK")
        s = desk.settings_from()
        p, _, _ = plan("ICICIBANK")                        # the SELL side never triggered that day
        armed = self.trade(side="sell", entry=p["sell"]["entry"], stop=p["sell"]["stop"], target=p["sell"]["targets"][0],
                           placed="2026-10-30 10:00", exit={"price": 1, "time": "10:10"})
        self.assertEqual(desk.replay(armed, b, now, s)["status"], "cancelled")
        r = desk.replay(self.trade(mode="market", entry=1394.5, placed="2026-10-30 09:36", exit={"price": 1393.0, "time": "10:00"}), b, now, s)
        self.assertEqual(r["exit"], {"how": "manual", "price": 1393.0, "time": "10:00"})

    def test_the_lock_comes_when_under_a_quarter_of_a_trade_is_left(self):
        b, now = bars("AXISBANK")
        s = desk.settings_from(overrides={"max_daily_loss_inr": 100})
        t = {"id": 1, "symbol": "AXISBANK", "side": "buy", "qty": 14, "mode": "market", "entry": 1224.4, "stop": 1218.6,
             "target": 1230.0, "target_name": "T1", "placed": "2026-10-30 09:50"}
        book = desk.day_book([t], {"AXISBANK": b}, now, s)
        self.assertEqual(book["realized"], -99.36)
        self.assertIn("Daily loss limit reached", book["lock"])
        self.assertEqual(desk.mood([], now.replace(hour=12), book)["label"], "Sit out")
        with self.assertRaises(desk.DeskError):
            p, _, _ = plan("TITAN")
            desk.new_trade("TITAN", p, "buy", 1, b[-1], now.replace(hour=12), s, book, {"level": "sit", "reasons": [book["lock"]]}, 2)

    def test_a_sell_needs_allow_short(self):
        p, b, now = plan("BHARTIARTL", "2026-10-30 09:40")
        md = {"level": "trade", "factor": 1.0, "reasons": []}
        with self.assertRaises(desk.DeskError):
            desk.new_trade("BHARTIARTL", p, "sell", 1, b[-1], now, desk.settings_from(), {"trades": []}, md, 1)
        t = desk.new_trade("BHARTIARTL", p, "sell", 1, b[-1], now, desk.settings_from(overrides={"allow_short": True}), {"trades": []}, md, 1)
        self.assertEqual((t["mode"], t["side"]), ("market", "sell"))


class Mood(unittest.TestCase):
    def test_choppy_days_halve_the_size(self):
        rows = [{"plan": {"buy": {"status": "triggered", "hits": [], "exit": {"how": "stop", "time": "10:00"}}}}] * 4 + \
               [{"plan": {"buy": {"status": "triggered", "hits": ["10:00"], "exit": None}}}]
        md = desk.mood(rows, datetime.datetime(2026, 10, 30, 10, 30), {"lock": None})
        self.assertEqual((md["label"], md["factor"]), ("Trade small", 0.5))

    def test_the_early_window_and_the_closed_market(self):
        md = desk.mood([], datetime.datetime(2026, 10, 30, 9, 40), {"lock": None})
        self.assertEqual(md["label"], "Trade")
        self.assertIn("best window", md["reasons"][0])
        self.assertEqual(desk.mood([], datetime.datetime(2026, 11, 1, 11), {"lock": None}, market_open=False)["label"], "Market closed")


if __name__ == "__main__":
    unittest.main()
