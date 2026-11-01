import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # `python -m unittest tests.test_budget` from the root
from _load import load, ROOT, FIX  # noqa: E402

size = load("nse-budget", "size")

CFG = os.path.join(ROOT, "nse-budget", "config.example.json")
PLAN = os.path.join(FIX, "nse-budget", "plan-icicibank.json")
ORDERS = os.path.join(FIX, "nse-budget", "orders-icicibank.json")
NOW = "2026-10-30 09:35"
TODAY = "2026-10-30"
# the contract's config (ref/PORTING.md), key for key
CONTRACT_CFG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 100, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "count_charges_in_max_loss": True, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:30", "flat_by": "15:10", "product": "MIS",
    "sl_limit_buffer_pct": 0.1, "broker": "zerodha", "paper": True, "blocked": [], "allow_margin": False,
    "allow_short": False, "allow_options": False, "holidays": [], "tick_size": {}, "charges": {},
}


def cfg(**over):
    with open(CFG, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN, encoding="utf-8") as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def write_json(d, name, doc):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return p


def prices_doc(price, quote_time="2026-10-30T04:10:00Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"ICICIBANK": price},
            "detail": {"ICICIBANK": {"time": quote_time, "source": "yahoo"}}}


class TheExample(unittest.TestCase):
    """₹1,00,000, 10% buffer, ₹500 max loss with charges, ICICIBANK risk 7.40 -> 53 shares (position cap)."""

    def test_config_example_is_the_contract(self):
        with open(CFG, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)
        self.assertEqual(size.DEFAULTS, CONTRACT_CFG)

    def test_icicibank_is_53_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 53)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual(s["bound_all"], ["position cap", "max order"])
        self.assertEqual((s["entry"], s["stop"], s["target"], s["risk_per_share"]), (1394.5, 1387.1, 1401.9, 7.4))
        self.assertEqual(s["cost"], 73908.5)
        self.assertEqual(s["max_loss"], 392.2)
        self.assertEqual(s["charges_at_stop"]["total"], 73.32)
        self.assertEqual(s["charges_at_target"]["total"], 73.55)
        self.assertEqual(s["max_loss_with_charges"], 465.52)
        self.assertEqual(s["reward_risk"], 1.0)
        self.assertEqual(s["cash_buffer"], 10000)
        self.assertEqual(s["sl_price"], 1385.7)
        self.assertEqual(s["loss_at_sl_price"], 466.4)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 57), ("position cap", 53), ("max order", 53), ("cash", 64)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["position cap"])
        self.assertIsNone(s["refusal"])

    def test_the_kite_order(self):
        o = size.kite_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "ICICIBANK", "exchange": "NSE", "tradingsymbol": "ICICIBANK", "transaction_type": "BUY",
            "quantity": 53, "product": "MIS", "order_type": "LIMIT", "price": "1394.50", "variety": "regular",
            "validity": "DAY",
            "exits": {"stop_loss": {"transaction_type": "SELL", "order_type": "SL", "trigger_price": "1387.10",
                                    "price": "1385.70"},
                      "target": {"transaction_type": "SELL", "order_type": "LIMIT", "price": "1401.90"}},
            "oco_note": size.OCO_NOTE,
            "charges_estimate": {"at_stop": 73.32, "at_target": 73.55},
            "max_loss": 392.2, "max_loss_with_charges": 465.52,
            "plan": {"entry": 1394.5, "stop": 1387.1, "target": 1401.9, "risk_per_share": 7.4,
                     "max_loss": 392.2, "setup": "orb", "entry_time": "9:35 AM", "flat_by": "15:10"}})
        self.assertIn("Zerodha has no bracket order", o["oco_note"])
        self.assertIn("bound by your position cap", why)
        self.assertIn("₹392.20 at the stop, ₹465.52 with charges", why)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(ORDERS, encoding="utf-8") as fh:
            fixture = json.load(fh)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, _, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                              "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), fixture)

    def test_cli_report(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 53 shares of ICICIBANK = ₹73,908.50 at 1394.50, decided by your position cap", text)
            self.assertIn("53 x 7.40 = ₹392.20, plus about ₹73.32 in charges = ₹465.52 (your limit ₹500)", text)
            self.assertIn("<- binds", text)
            self.assertIn("position cap and max order give the same number", text)
            self.assertIn("SL, trigger 1387.10, limit 1385.70", text)
            self.assertIn("Flat by 15:10 IST", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual((doc["created_by"], doc["status"], doc["paper"]), ("nse-budget", "proposed", True))
            self.assertEqual(doc["created_at"], "2026-10-30T04:05:00Z")
            self.assertEqual(doc["source"], "trader/plan.json (ICICIBANK orb 2026-10-30)")
            self.assertEqual(len(doc["orders"]), 1)

    def test_cli_json_and_market(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--market", "--json"])
        self.assertEqual(code, 0)
        o = json.loads(text)["orders"][0]
        self.assertEqual((o["order_type"], o["price"], o["quantity"]), ("MARKET", None, 53))
        self.assertEqual(o["exits"]["stop_loss"]["trigger_price"], "1387.10")

    def test_a_limit_order_at_your_price(self):
        s = size.size_trade(plan(), cfg(), limit=1393.03, today=TODAY)      # rounds to the 0.10 tick
        self.assertEqual((s["entry"], s["risk_per_share"], s["qty"], s["max_loss"]), (1393.0, 5.9, 53, 312.7))
        self.assertEqual(size.kite_order(s)["price"], "1393.00")
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--market", "--limit", "1393"])
        self.assertEqual(code, 1)
        self.assertIn("do not go together", err)


class Limits(unittest.TestCase):
    def test_charges_count_in_the_max_loss(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=300), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss"], s["max_loss_with_charges"]),
                         (33, "max loss", 244.2, 292.95))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=300, count_charges_in_max_loss=False), today=TODAY)
        self.assertEqual((s["qty"], s["max_loss"], s["max_loss_with_charges"]), (40, 296.0, 355.09))
        s = size.size_trade(plan(), cfg(count_charges_in_max_loss=False), today=TODAY)
        self.assertEqual(s["limits"][0]["shares"], 67)

    def test_each_limit_can_bind(self):
        s = size.size_trade(plan(), cfg(max_order_inr=50000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (35, "max order"))
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (7, "cash", 10000))
        s = size.size_trade(plan(), cfg(), budget=50000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (26, "position cap"))

    def test_zero_shares_is_a_plain_refusal(self):
        far = plan(stop=800.0, target=2000.0)
        s = size.size_trade(far, cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹500 max loss per trade", s["refusal"])
        self.assertIn("in charges", s["refusal"])
        self.assertIsNone(size.kite_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = write_json(d, "plan.json", far)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="ABC", entry=40.0, stop=39.0, target=41.0, tick=None)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=2, count_charges_in_max_loss=False), today=TODAY)
        self.assertEqual(s["qty"], 2)
        self.assertIn("under your ₹100 minimum order", s["refusal"])
        self.assertIsNone(size.kite_order(s))

    def test_prices_snap_to_the_tick(self):
        s = size.size_trade(plan(stop=1387.17, target=1401.93), cfg(), today=TODAY)
        self.assertEqual((s["stop"], s["target"]), (1387.1, 1401.9))         # stop down, target nearest
        s = size.size_trade(plan(entry=95.13, stop=94.07, target=96.21, tick=None), cfg(), today=TODAY)
        self.assertEqual((s["tick"], s["stop"], s["sl_price"]), (0.01, 94.07, 93.97))
        s = size.size_trade(plan(), cfg(tick_size={"ICICIBANK": 0.5}), today=TODAY)
        self.assertEqual((s["stop"], s["target"]), (1387.0, 1402.0))         # the config overrides the plan


class Prices(unittest.TestCase):
    def test_a_fresh_higher_price_means_a_bigger_risk_for_a_market_order(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), market=True, price=1396.2, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1396.2, "price", 9.1))
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss_with_charges"], s["reward_risk"]),
                         (47, "max loss", 497.17, 0.63))
        s = size.size_trade(plan(), cfg(), market=True, price=1396.2, today=TODAY)
        self.assertIn("reward:risk 0.63 (target 1401.90, entry 1396.20, stop 1387.10) is under your minimum 1",
                      s["refusal"])
        self.assertIsNone(size.kite_order(s))
        lower = size.size_trade(plan(), cfg(), market=True, price=1390.0, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1394.5, 53))

    def test_prices_file(self):
        with tempfile.TemporaryDirectory() as d:
            pp = write_json(d, "prices.json", prices_doc(1396.2))
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            code, text, _ = run(base + ["--market", "--now", "2026-10-30 09:45"])
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["qty"], out["orders"]), (1, 47, []))
            self.assertIn("above the plan's entry", out["notes"][0])
            out = json.loads(run(base + ["--market", "--now", "2026-10-30 10:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 53)
            self.assertIn("not used", out["notes"][0])
            code, text, _ = run(base + ["--now", "2026-10-30 09:45"])            # LIMIT: sized at the limit
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["qty"]), (0, 53))
            self.assertIn("above your LIMIT 1394.50", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = write_json(d, "portfolio.json", {"cash": 20000, "positions": []})
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (7, "cash"))


class Refusals(unittest.TestCase):
    def test_bad_plans(self):
        for p, word in ((plan(stop=1395.0), "not below the entry"),
                        (plan(target=1390.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol"),
                        (plan(product="CNC"), "intraday (MIS) trades only"),
                        (plan(exchange="BSE"), "NSE only")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), limit=1405.0, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["icicibank"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))

    def test_a_plan_for_another_day_is_refused_on_the_ist_clock(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-11-02")
        self.assertIn("make a new plan", str(e.exception))
        # 00:30 IST on Oct 31 is still Oct 30 in UTC: the IST date decides
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent",
                            "--now", "2026-10-31 00:30"])
        self.assertEqual(code, 1)
        self.assertIn("today is 2026-10-31 in India (IST)", err)
        code, _, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent",
                          "--now", "2026-10-30T18:00:00Z"])                   # 23:30 IST, same day
        self.assertEqual(code, 0)

    def test_missing_plan_and_bad_flags(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertIn("IST", err)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--limit", "abc"])
        self.assertEqual(code, 1)
        self.assertIn("--limit wants a price", err)


class NoUSLeftovers(unittest.TestCase):
    def test_no_us_words_in_the_skill(self):
        import re
        bad = re.compile(r"\$[0-9]|dollar|usd|alpaca|tsla|new york|trader-budget|13F|(?-i:\bET\b)",
                         re.IGNORECASE)
        for name in ("size.py", "SKILL.md", "config.example.json"):
            with open(os.path.join(ROOT, "nse-budget", name), encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    self.assertIsNone(bad.search(line), "%s:%d: %s" % (name, i, line.strip()))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "nse-budget", "targets.example.json")))


if __name__ == "__main__":
    unittest.main()
