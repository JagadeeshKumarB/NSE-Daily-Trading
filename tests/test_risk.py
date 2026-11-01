import copy
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # `python -m unittest tests.test_risk` from the root
from _load import load, FIX  # noqa: E402

check = load("nse-risk", "check")

D = os.path.join(FIX, "nse-risk")
CFG_PATH = os.path.join(D, "config.json")
ORDERS_PATH = os.path.join(D, "orders.json")
BLOCKS_PATH = os.path.join(D, "orders-blocks.json")
UNIVERSE_PATH = os.path.join(D, "universe.txt")
PRICES_PATH = os.path.join(D, "prices.json")
NOW = "2026-10-30 09:35"
TODAY = "2026-10-30"
UNIVERSE = check.load_universe(UNIVERSE_PATH)
QUOTE_TIME = datetime.datetime(2026, 10, 30, 4, 20)   # 09:50 IST: ICICIBANK 1395.00 (the 09:50 bar's open)


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def icici(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def blocks():
    with open(BLOCKS_PATH, encoding="utf-8") as fh:
        return json.load(fh)["orders"]


def market(**over):
    o = icici(order_type="MARKET", **over)
    o.pop("price")
    return o


def order(sym, qty, entry, stop, target, **over):
    o = {"symbol": sym, "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY", "quantity": qty,
         "product": "MIS", "order_type": "LIMIT", "price": "%.2f" % entry, "variety": "regular", "validity": "DAY",
         "exits": {"stop_loss": {"transaction_type": "SELL", "order_type": "SL", "trigger_price": "%.2f" % stop,
                                 "price": "%.2f" % stop},
                   "target": {"transaction_type": "SELL", "order_type": "LIMIT", "price": "%.2f" % target}},
         "plan": {"entry": entry, "stop": stop, "target": target}}
    o.update(over)
    return o


def sell(sym="ICICIBANK", qty=53, **over):
    o = {"symbol": sym, "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL", "quantity": qty,
         "product": "MIS", "order_type": "MARKET", "variety": "regular", "validity": "DAY"}
    o.update(over)
    return o


def placed(sym, side="buy", day=TODAY, max_loss=392.2, with_charges=465.52, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:40:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if with_charges is not None:
        r["max_loss_with_charges"] = with_charges
    if kind:
        r["kind"] = kind
    return r


def run(orders, now=NOW, universe=UNIVERSE, **kw):
    c = kw.pop("config", None) or cfg()
    return check.check(orders if isinstance(orders, list) else [orders], c, now=at(now), universe=universe, **kw)


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


def info(res, i=0):
    return " | ".join(res["results"][i]["info"])


class TheReel(unittest.TestCase):
    """ICICIBANK, Friday 2026-10-30, the opening-range breakout at 09:35 IST."""

    def test_the_icicibank_order_passes_every_check(self):
        res = run(icici())
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["entry_ref_from"], r["stop"], r["target"]),
                         ("entry", 1394.5, "limit", 1387.1, 1401.9))
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["charges_at_stop"], r["max_loss_with_charges"]),
                         (7.4, 392.2, 73.32, 465.52))
        self.assertEqual((r["charges_at_target"], r["reward_risk"]), (73.55, 1.0))
        self.assertEqual((r["series"], r["band"], r["quantity"]), ("EQ", "No Band", 53))
        self.assertEqual(r["value_inr"], 73908.5)
        self.assertEqual(res["cash_after"], 26091.5)
        self.assertEqual(res["date"], TODAY)
        self.assertEqual(res["now_ist"], "2026-10-30 09:35")
        self.assertIs(res["paper"], True)
        self.assertEqual(res["warnings"], [])

    def test_the_numbers_match_what_nse_budget_wrote(self):
        o = icici()
        r = run(o)["results"][0]
        self.assertEqual(r["max_loss"], o["max_loss"])
        self.assertEqual(r["max_loss_with_charges"], o["max_loss_with_charges"])
        self.assertEqual(r["charges_at_stop"], o["charges_estimate"]["at_stop"])
        self.assertEqual(r["charges_at_target"], o["charges_estimate"]["at_target"])

    def test_the_report_lines(self):
        text = check.report(run(icici()))
        for line in ("PASS  #1 BUY ICICIBANK 53 shares MIS, entry",
                     "entry 1394.50 (the limit price), stop 1387.10, target 1401.90: risk 7.40 a share",
                     "max loss ₹392.20 + ₹73.32 charges = ₹465.52 (limit ₹500), reward:risk 1.00 (min 1)",
                     "at the target: +₹392.20 - ₹73.55 charges = +₹318.65",
                     "place the SL SELL (trigger 1387.10, limit 1385.70) and the LIMIT SELL 1401.90",
                     "today before this: 0 of 3 trades, ₹0 of ₹1,500 daily loss",
                     "1 passed, 0 blocked at 2026-10-30 09:35 IST. Cash after the passed orders: ₹26,091.50",
                     "not financial advice"):
            self.assertIn(line, text)

    def test_54_shares_is_over_the_max_order(self):
        res = run(icici(quantity=54))
        self.assertEqual(res["results"][0]["reasons"],
                         ["size: ₹75,303 is over your ₹75,000 max order",
                          "position: ICICIBANK would be ₹75,303, 75.3% of the ₹1,00,000 account, over your 75% cap"])

    def test_charges_count_in_the_max_loss(self):
        roomy = dict(max_order_inr=200000, max_position_pct=100, cash_buffer_pct=0)
        res = run(icici(quantity=59), config=cfg(**roomy))
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 59 shares x 7.40 risk a share = ₹436.60 at the stop, plus ₹76.27 estimated charges "
                          "= ₹512.87, over your ₹500 max loss per trade"])
        res = run(icici(quantity=59), config=cfg(count_charges_in_max_loss=False, **roomy))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("(limit ₹500, charges not counted)", info(res))
        res = run(icici(quantity=68), config=cfg(count_charges_in_max_loss=False, **roomy))
        self.assertIn("loss: 68 shares x 7.40 risk a share = ₹503.20 at the stop, over your ₹500", reasons(res))

    def test_fingerprint(self):
        o = icici()
        fp = check.fingerprint(o)
        self.assertEqual(len(fp), 16)
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={}, oco_note="x")), fp)
        self.assertNotEqual(check.fingerprint(dict(o, quantity=54)), fp)
        self.assertNotEqual(check.fingerprint(dict(o, price="1394.40")), fp)
        moved = copy.deepcopy(o)
        moved["exits"]["stop_loss"]["trigger_price"] = "1386.00"
        self.assertNotEqual(check.fingerprint(moved), fp)
        self.assertEqual(check.KITE_FIELDS, ("symbol", "exchange", "tradingsymbol", "transaction_type", "quantity",
                                             "product", "order_type", "price", "trigger_price", "variety",
                                             "validity", "exits"))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--universe", UNIVERSE_PATH, "--state", none,
                                   "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 BUY ICICIBANK 53 shares MIS, entry", text)
            self.assertIn("max loss ₹392.20 + ₹73.32 charges = ₹465.52 (limit ₹500)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-30 09:35 IST", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["max_loss_with_charges"], 465.52)
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(icici()))
            self.assertEqual(rec["orders_file"], ORDERS_PATH)


class IndiaChecks(unittest.TestCase):
    def test_the_blocks_fixture(self):
        with open(PRICES_PATH, encoding="utf-8") as fh:
            prices, times, prev = check.load_prices(json.load(fh))
        self.assertEqual(prev, {"ICICIBANK": 1386.6, "MTARTECH": 2000.0, "ABAN": 34.2})
        res = run(blocks(), prev_close=prev)
        self.assertEqual(verdicts(res), ["BLOCK", "BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["circuit: the entry 2085.00 is within 1% of today's upper circuit 2100.00 (5% band from the "
                          "previous close 2000.00). A stock at its upper circuit can lock: few or no sellers, and a "
                          "sharp fall when it opens up"])
        self.assertEqual(res["results"][1]["reasons"],
                         ["series: ABAN is in the BE series (trade-for-trade): every trade must be delivered, so no "
                          "MIS intraday. Only EQ stocks can be day traded"])

    def test_series_unknown_is_a_warning(self):
        res = run(icici(), universe=None)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("series: ICICIBANK's series and circuit band are unknown (no trader/universe.txt", warnings(res))
        res = run(order("TCS", 10, 3000.0, 2990.0, 3010.0))
        self.assertIn("not in trader/universe.txt", warnings(res))

    def test_series_from_a_kite_tradingsymbol(self):
        o = blocks()[1]
        o["tradingsymbol"] = "ABAN-BE"
        res = run(o, universe=None)
        self.assertIn("series: ABAN is in the BE series", reasons(res))
        res = run(dict(o, symbol="ABAN", tradingsymbol="ABANX"), universe=None)
        self.assertIn("do not match", reasons(res))

    def test_be_stock_for_delivery_is_not_blocked_by_the_series(self):
        o = dict(blocks()[1], product="CNC")
        res = run(o)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("CNC is a delivery buy", warnings(res))

    def test_flat_by_before_zerodhas_square_off(self):
        o = icici()
        o["plan"] = dict(o["plan"], flat_by="15:20")
        self.assertIn("flat_by: 15:20 is at or after 15:20, when Zerodha starts squaring off", reasons(run(o)))
        o["plan"]["flat_by"] = "15:19"
        self.assertEqual(verdicts(run(o)), ["PASS"])
        o["plan"].pop("flat_by")
        self.assertIn("flat_by: 15:25", reasons(run(o, config=cfg(flat_by="15:25"))))
        o["plan"]["flat_by"] = "3pm"
        self.assertIn("flat_by: flat_by must be an India time", reasons(run(o)))

    def test_circuit(self):
        prev = {"MTARTECH": 2000.0}
        res = run(order("MTARTECH", 25, 2101.0, 2090.0, 2115.0), prev_close=prev)
        self.assertIn("circuit: the entry 2101.00 is above today's upper circuit 2100.00", reasons(res))
        res = run(order("MTARTECH", 25, 2075.0, 2060.0, 2090.0), prev_close=prev)
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("circuit: 5% band from the previous close 2000.00: lower 1900.00, upper 2100.00", info(res))
        res = run(order("MTARTECH", 25, 2079.0, 2064.0, 2094.0), prev_close=prev)
        self.assertIn("within 1% of today's upper circuit", reasons(res))
        res = run(order("MTARTECH", 25, 2085.0, 2070.0, 2100.0))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no previous close", warnings(res))
        res = run(order("MTARTECH", 1, 1910.0, 1890.0, 1930.0), prev_close=prev)
        self.assertIn("below today's lower circuit 1900.00", warnings(res))
        self.assertIn("no fixed band", info(run(icici(), prev_close={"ICICIBANK": 1386.6})))

    def test_prices_must_be_on_the_tick(self):
        res = run(icici(price="1394.53"))
        self.assertIn("format: price 1394.53 is not on NSE's 0.1 tick for ICICIBANK (the exchange rejects it); "
                      "nearest is 1394.50", reasons(res))
        o = icici()
        o["exits"]["target"]["price"] = "1401.95"
        self.assertIn("the target leg's price 1401.95 is not on NSE's 0.1 tick", reasons(run(o)))
        self.assertEqual(verdicts(run(order("ABAN", 1000, 34.51, 34.21, 34.81, product="CNC"))), ["PASS"])

    def test_no_margin(self):
        pf = {"cash": 50000, "positions": []}
        res = run(icici(), portfolio=pf)
        self.assertIn("cash: needs ₹73,908.50 but only ₹50,000 cash; that would be margin", reasons(res))
        res = run(icici(), portfolio=pf, config=cfg(allow_margin=True, max_position_pct=200))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("margin: needs ₹73,908.50 but only ₹50,000 cash", warnings(res))

    def test_no_short_selling(self):
        res = run(sell(qty=10))
        self.assertIn("short: selling more ICICIBANK than you hold (0 shares); short selling is off", reasons(res))
        short = sell(qty=10, order_type="LIMIT", price="1394.50",
                     exits={"stop_loss": {"transaction_type": "BUY", "order_type": "SL", "trigger_price": "1401.90",
                                          "price": "1408.90"},
                            "target": {"transaction_type": "BUY", "order_type": "LIMIT", "price": "1387.10"}})
        res = run(short, config=cfg(allow_short=True))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 74.0, 1.0))
        res = run(dict(short, product="CNC"), config=cfg(allow_short=True))
        self.assertIn("only short sell intraday (MIS)", reasons(res))

    def test_no_options_or_futures(self):
        opt = order("NIFTY26NOV25000CE", 75, 120.0, 100.0, 140.0, exchange="NFO", product="NRML")
        why = reasons(run(opt))
        self.assertIn("option contract; options are off", why)
        self.assertIn("exchange NFO is for futures and options", why)
        fut = order("RELIANCE26NOVFUT", 500, 1450.0, 1440.0, 1460.0, exchange="NFO")
        self.assertIn("futures contract", reasons(run(fut)))

    def test_nse_symbols_only(self):
        self.assertIn("use the plain NSE symbol ICICIBANK, not Yahoo's ICICIBANK.NS",
                      reasons(run(icici(symbol="ICICIBANK.NS", tradingsymbol=None))))
        self.assertIn("this pack trades on NSE only", reasons(run(icici(exchange="BSE"))))
        self.assertIn("does not look like an NSE stock symbol",
                      reasons(run(order("BTC/INR", 1, 100.0, 90.0, 110.0))))
        ok = run(order("M&M", 20, 3000.0, 2990.0, 3010.0))
        self.assertNotIn("asset:", reasons(ok))


class Format(unittest.TestCase):
    def test_quantity(self):
        res = run(icici(quantity="53"))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("send quantity as a whole number (53)", warnings(res))
        self.assertIn("whole shares only", reasons(run(icici(quantity=8.5))))
        self.assertIn("no quantity", reasons(run(icici(quantity=None))))
        self.assertIn("positive whole number", reasons(run(icici(quantity=0))))

    def test_order_fields(self):
        self.assertIn("transaction_type must be BUY or SELL", reasons(run(icici(transaction_type="HOLD"))))
        self.assertIn("no product", reasons(run(icici(product=None))))
        self.assertIn("no exchange", reasons(run(icici(exchange=None))))
        self.assertIn("unknown order_type", reasons(run(icici(order_type="STOP"))))
        self.assertIn("a LIMIT order needs a price", reasons(run(icici(price=None))))
        self.assertIn("only \"regular\" orders", reasons(run(icici(variety="co"))))
        self.assertIn("unknown validity", reasons(run(icici(validity="GTC"))))
        self.assertIn("as a string", warnings(run(icici(price=1394.5))))

    def test_sl_entry(self):
        o = icici(order_type="SL", trigger_price="1394.50", price="1395.50")
        r = run(o)["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["max_loss"]), (1395.5, "limit", 445.2))
        self.assertIn("loss: 53 shares x 8.40 risk a share = ₹445.20 at the stop", " ".join(r["reasons"]))
        self.assertIn("price 1393.50 must be at or above its trigger", reasons(run(dict(o, price="1393.50"))))
        self.assertIn("needs a trigger_price", reasons(run(icici(order_type="SL-M"))))

    def test_exit_legs(self):
        o = icici()
        o["exits"].pop("target")
        self.assertIn("exits needs both a stop_loss and a target leg", reasons(run(o)))
        o = icici()
        o["exits"]["stop_loss"]["price"] = "1390.00"
        self.assertIn("the stop_loss leg's price 1390.00 must be at or below its trigger 1387.10", reasons(run(o)))
        o = icici()
        o["exits"]["stop_loss"]["transaction_type"] = "BUY"
        self.assertIn("the stop_loss leg must be a SELL for a BUY entry", reasons(run(o)))
        o = icici()
        o["exits"]["target"]["quantity"] = 50
        self.assertIn("the target leg's quantity 50 is not the entry's 53", reasons(run(o)))
        o = icici()
        o["exits"]["stop_loss"] = {"transaction_type": "SELL", "order_type": "SL-M", "trigger_price": "1387.10"}
        self.assertEqual(verdicts(run(o)), ["PASS"])


class DayTradeChecks(unittest.TestCase):
    def test_a_stop_is_required(self):
        o = icici()
        o.pop("exits")
        o["plan"] = {"entry": 1394.5}
        self.assertIn("stop: no stop", reasons(run(o)))
        res = run(o, config=cfg(require_stop=False, min_reward_risk=0))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = icici()
        o.pop("exits")
        res = run(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss_with_charges"], 465.52)

    def test_stop_and_target_on_the_right_side(self):
        o = icici()
        o["exits"]["stop_loss"].update(trigger_price="1395.00", price="1390.00")
        self.assertIn("must be below the entry 1394.50", reasons(run(o)))
        o = icici()
        o["exits"]["target"]["price"] = "1394.00"
        self.assertIn("target: the target 1394.00 must be above the entry 1394.50", reasons(run(o)))

    def test_reward_risk(self):
        o = icici()
        o["exits"]["target"]["price"] = "1398.00"
        self.assertIn("reward:risk: 0.47", reasons(run(o)))
        self.assertEqual(verdicts(run(o, config=cfg(min_reward_risk=0.4))), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry_of_a_market_order(self):
        res = run(market(), now="2026-10-30 09:52", prices={"ICICIBANK": 1395.0},
                  price_times={"ICICIBANK": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1395.0, "price", 7.9))
        self.assertEqual(r["reasons"], ["reward:risk: 0.87 (target 1401.90, entry 1395.00, stop 1387.10) is under "
                                        "your minimum 1"])

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-10-30 10:10", "2026-10-30 09:35"):
            res = run(market(), now=now, prices={"ICICIBANK": 1395.0}, price_times={"ICICIBANK": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 1394.5)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = run(market(), prices={"ICICIBANK": 1391.6})
        self.assertEqual(res["results"][0]["entry_ref"], 1394.5)

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("HDFCBANK"), placed("SBIN")]}
        res = run(icici(), state=two)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, ₹931.04 of ₹1,500 daily loss", info(res))
        three = {"orders": [placed("HDFCBANK"), placed("SBIN"), placed("INFY")]}
        why = reasons(run(icici(), state=three))
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: ₹1,396.56 already at risk today plus ₹465.52 for this one = ₹1,862.08, over "
                      "your ₹1,500 daily loss limit", why)
        old = {"orders": [placed("HDFCBANK", with_charges=None), placed("SBIN", with_charges=None)]}
        self.assertIn("₹784.40 of ₹1,500 daily loss", info(run(icici(), state=old)))
        yesterday = {"orders": [placed(s, day="2026-10-29") for s in ("HDFCBANK", "SBIN", "INFY")]}
        self.assertEqual(verdicts(run(icici(), state=yesterday)), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("HDFCBANK"), placed("HDFCBANK", side="sell", kind="exit", max_loss=None,
                                                    with_charges=None),
                         placed("SBIN", status="declined"), placed("INFY", status="failed")]}
        res = run(icici(), state=st)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"], {"orders_placed": 2, "entries_placed": 1, "loss_at_risk": 465.52})

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [order(s, 10, 1000.0, 960.0, 1040.0) for s in ("HDFCBANK", "SBIN", "INFY", "RELIANCE")]
        res = run(orders)
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: ₹1,231.11 already at risk today plus ₹410.37 for this one = ₹1,641.48", why)

    def test_entry_window_india_time(self):
        cases = [("2026-10-30 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-10-30 14:31", "no new entries after 14:30 IST (it is 14:31)"),
                 ("2026-10-30 09:10", "the market is closed (Fri 2026-10-30 09:10 IST; the session is 09:15 to 15:30)"),
                 ("2026-10-30 15:31", "the market is closed"),
                 ("2026-10-31 10:15", "the market is closed today (weekend"),
                 ("2026-11-10 10:15", "the market is closed today (NSE holiday"),
                 ("2026-10-30T03:59:00Z", "no new entries before 09:30 IST (it is 09:29)")]
        for now, want in cases:
            self.assertIn("window: " + want, reasons(run(icici(), now=now)), now)
        for now in ("2026-10-30 09:30", "2026-10-30 14:30", "2026-10-30T04:05:00Z", "2026-10-30T09:35:00+05:30"):
            self.assertEqual(verdicts(run(icici(), now=now)), ["PASS"], now)
        res = run(icici(), now="2026-10-31 10:15", config=cfg(no_entries_before=None, no_entries_after=None))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("NSE holiday", reasons(run(icici(), config=cfg(holidays=["2026-10-30"]))))
        res = run(icici(), now="2027-01-05 10:00")
        self.assertIn("NSE's 2027 holiday list is not built in", " ".join(res["warnings"]))
        with self.assertRaises(check.RiskError):
            run(icici(), config=cfg(no_entries_before="9.30am"))

    def test_the_window_uses_india_time_not_the_machine(self):
        self.assertEqual(at("2026-10-30 09:35"), datetime.datetime(2026, 10, 30, 4, 5))
        self.assertEqual(check.to_ist(at("2026-10-30 09:35")), datetime.datetime(2026, 10, 30, 9, 35))
        self.assertEqual(at("2026-12-01 09:35"), datetime.datetime(2026, 12, 1, 4, 5))   # no daylight saving


class Exits(unittest.TestCase):
    PF = {"cash": 26091.5, "positions": [{"ticker": "ICICIBANK", "shares": 53,
                                           "lots": [{"date": TODAY, "shares": 53, "cost": 73908.5}]}]}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("ICICIBANK"), placed("HDFCBANK"), placed("SBIN")] +
                          [placed("T%d" % i, max_loss=1, with_charges=1) for i in range(6)] +
                          [placed("ICICIBANK", side="sell", kind="exit", max_loss=None, with_charges=None)]}
        c = cfg(blocked=["ICICIBANK"], max_order_inr=1000, max_daily_loss_inr=50)
        res = run(sell(), config=c, portfolio=self.PF, state=full, now="2026-10-30 15:12")
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("series:", why)
        res = run(sell(), portfolio=self.PF, now="2026-10-31 12:00")
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = sell(qty=27)
        res = run([half, half], portfolio=self.PF, prices={"ICICIBANK": 1401.9})
        self.assertEqual(verdicts(res), ["PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 1))
        res = run([sell(qty=26), half], portfolio=self.PF, prices={"ICICIBANK": 1401.9})
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))

    def test_format_and_short_still_apply_to_a_sell(self):
        self.assertIn("a LIMIT order needs a price", reasons(run(sell(order_type="LIMIT"), portfolio=self.PF)))
        res = run(sell(qty=54), portfolio=self.PF)
        self.assertIn("short: selling more ICICIBANK than you hold (53 shares)", reasons(res))
        self.assertIn("record the fill first", reasons(run(sell())))

    def test_buy_then_sell_in_one_batch(self):
        res = run([icici(), sell()])
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class Basics(unittest.TestCase):
    def test_max_and_min_order(self):
        self.assertIn("under your ₹100 minimum order", reasons(run(order("ABAN", 2, 34.5, 34.2, 34.8, product="CNC"))))

    def test_blocked_list(self):
        res = run(icici(), config=cfg(blocked=["icicibank"]))
        self.assertEqual(res["results"][0]["reasons"], ["blocked: ICICIBANK is on your blocked list"])

    def test_cash_buffer(self):
        res = run(icici(), portfolio={"cash": 80000, "positions": []})
        self.assertIn("cash: leaves ₹6,091.50 cash, under your 10% buffer of ₹8,000", reasons(res))

    def test_daily_order_limit_and_already_placed_today(self):
        st = {"orders": [placed("T%d" % i, side="sell", kind="exit") for i in range(10)]}
        self.assertIn("daily limit: 10 placed today", reasons(run(icici(), state=st)))
        st = {"orders": [placed("ICICIBANK", status="placed")]}
        self.assertIn("a BUY ICICIBANK was already placed today", reasons(run(icici(), state=st)))
        self.assertIn("duplicate: a second BUY ICICIBANK", reasons(run([icici(), icici()]), 1))

    def test_live_account_and_default_budget_warn(self):
        self.assertIn("LIVE", run(icici(), config=cfg(paper=False))["warnings"][0])
        res = check.check([icici()], {}, now=at(NOW), universe=UNIVERSE)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("the default ₹1,00,000 budget", res["warnings"][0])

    def test_no_budget_at_all_is_an_error(self):
        with self.assertRaises(check.RiskError):
            run(icici(), config=cfg(budget=None))


class Cli(unittest.TestCase):
    def test_the_blocks_fixture_and_prices_file(self):
        with tempfile.TemporaryDirectory() as d:
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", BLOCKS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", PRICES_PATH, "--universe", UNIVERSE_PATH, "--state", none,
                                   "--now", NOW, "--no-write"])
            self.assertEqual(code, 1)
            text = buf.getvalue()
            self.assertIn("BLOCK #1 BUY MTARTECH 25 shares MIS, entry", text)
            self.assertIn("BLOCK #2 BUY ABAN 1000 shares MIS, entry", text)
            self.assertIn("0 passed, 2 blocked", text)

    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            mk = os.path.join(d, "orders.json")
            with open(mk, "w", encoding="utf-8") as fh:
                json.dump({"orders": [market()]}, fh)
            none = os.path.join(d, "none")
            base = ["--orders", mk, "--config", CFG_PATH, "--portfolio", none, "--prices", PRICES_PATH,
                    "--universe", UNIVERSE_PATH, "--state", none, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-10-30 09:52"]), 1)
            self.assertIn("reward:risk: 0.87", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-10-30 10:10"]), 0)

    def test_errors_are_one_line(self):
        for args, want in ((["--orders", os.path.join(D, "nonexistent.json")], "no orders file"),
                           (["--orders", ORDERS_PATH, "--now", "noon"], "--now wants"),
                           (["--orders", ORDERS_PATH, "--today", "friday"], "--today wants")):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(check.main(args + ["--no-write"]), 1)
            self.assertIn(want, err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
