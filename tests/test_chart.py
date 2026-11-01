"""nse-chart: the bias read, the record and the page's data, on the saved Nifty 50 bars."""

import datetime
import gzip
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import FIX, load  # noqa: E402

live = load("nse-chart", "live")
BUNDLE = os.path.join(FIX, "nse", "nifty50-5m-60d.json.gz")


def store(now):
    clock = live.Clock(now)
    return live.Store(live.Source(clock, BUNDLE), clock)


class Bias(unittest.TestCase):
    def test_icicibank_at_ten_reads_bullish_on_all_five(self):
        p = store("2026-10-30 10:00").chart("ICICIBANK")
        b = p["bias"]
        self.assertEqual((b["label"], b["score"], b["max"], b["at"], b["close"]), ("Bullish", 5, 5, "10:00", 1394.3))
        self.assertEqual([s["vote"] for s in b["signals"]], [1, 1, 1, 1, 1])
        self.assertEqual(p["prev_close"], 1386.6)
        self.assertEqual((len(p["bars"]), p["closed"]), (9, 9))

    def test_before_the_range_is_set_the_range_votes_zero(self):
        b = store("2026-10-30 09:25").chart("ICICIBANK")["bias"]
        self.assertEqual(b["signals"][0], {"name": "Opening range", "vote": 0, "detail": "Still forming (9:15 to 9:30)"})

    def test_labels(self):
        self.assertEqual([live.label(s) for s in (5, 3, 2, 0, -1, -3)],
                         ["Bullish", "Bullish", "Leaning bullish", "Neutral", "Leaning bearish", "Bearish"])

    def test_index_uses_breadth_when_there_is_no_volume(self):
        t = datetime.datetime(2026, 10, 30, 9, 15)
        bars = [[t + i * live.BAR, 100 + i, 101 + i, 99 + i, 100.5 + i, 0] for i in range(10)]
        r = live.read_at(live.Series(bars), 9, breadth=0.7)
        self.assertEqual(r["signals"][-1]["name"], "Breadth")
        self.assertEqual(r["signals"][-1]["vote"], 1)


class Record(unittest.TestCase):
    def test_sixty_sessions_of_icicibank(self):
        bt = store("2026-10-30 16:00").backtest("ICICIBANK")
        self.assertEqual((bt["sessions"], bt["bars"]), (60, 3420))
        self.assertEqual((bt["bull"]["calls"], bt["bear"]["calls"]), (1028, 1409))
        self.assertIn("Any time: the close 30 minutes later was higher 48 % of the time.", bt["lines"])


class Plan(unittest.TestCase):
    def test_icicibank_buy_side_hit_t1_and_went_out_at_1510(self):
        s = store("2026-10-30 15:40")
        s.refresh_board()
        p = live.orb_plan("ICICIBANK", s.today["ICICIBANK"], s.clock.now())
        self.assertEqual((p["or_high"], p["or_low"], p["width"]), (1390.9, 1383.4, 7.5))
        b = p["buy"]
        self.assertEqual((b["status"], b["entry_time"], b["fill"], b["entry"], b["stop"], b["targets"]),
                         ("triggered", "09:35", 1394.5, 1390.9, 1383.4, [1398.4, 1405.9, 1413.4]))
        self.assertEqual((b["hits"], b["exit"]), (["11:10"], {"how": "flat", "price": 1395.0, "time": "15:10"}))
        self.assertEqual((p["sell"]["status"], p["sell"]["entry"], p["sell"]["stop"], p["sell"]["targets"]),
                         ("none", 1383.4, 1390.9, [1375.9, 1368.4, 1360.9]))

    def test_bhartiartl_both_sides_triggered(self):
        s = store("2026-10-30 15:40")
        s.refresh_board()
        p = live.orb_plan("BHARTIARTL", s.today["BHARTIARTL"], s.clock.now())
        self.assertEqual((p["sell"]["entry_time"], p["sell"]["hits"], p["sell"]["exit"]),
                         ("09:35", ["10:20"], {"how": "stop", "price": 1979.2, "time": "11:50"}))
        self.assertEqual((p["buy"]["entry_time"], p["buy"]["hits"], p["buy"]["exit"]),
                         ("11:50", [], {"how": "stop", "price": 1968.9, "time": "13:55"}))

    def test_the_levels_are_set_at_930_before_any_trigger(self):
        s = store("2026-10-30 09:32")
        s.refresh_board()
        p = live.orb_plan("ICICIBANK", s.today["ICICIBANK"], s.clock.now())
        self.assertEqual((p["status"], p["buy"]["status"], p["sell"]["status"]), ("ready", "waiting", "waiting"))
        self.assertEqual(live.orb_plan("ICICIBANK", s.today["ICICIBANK"], datetime.datetime(2026, 10, 30, 9, 25)),
                         {"status": "forming"})

    def test_targets_in_order_and_t3_ends_it(self):
        t = datetime.datetime(2026, 10, 30, 9, 15)
        ohlc = [(100, 101, 99, 100), (100, 100.5, 99.5, 100), (100, 100.8, 99.2, 100.5),   # range 99 to 101, width 2
                (100.5, 101.6, 100.4, 101.5),                                                # closes over 101: BUY
                (101.5, 103.2, 101.4, 103.0),                                                # T1 103
                (103.0, 107.5, 102.9, 107.0)]                                                # T2 105, T3 107
        bars = [[t + i * live.BAR, o, h, lo, c, 1000] for i, (o, h, lo, c) in enumerate(ohlc)]
        p = live.orb_plan("TEST", bars, datetime.datetime(2026, 10, 30, 10, 0))
        b = p["buy"]
        self.assertEqual((b["targets"], b["hits"]), ([103.0, 105.0, 107.0], ["09:40", "09:45", "09:45"]))
        self.assertEqual(b["exit"], {"how": "t3", "price": 107.0, "time": "09:45"})


class UTBot(unittest.TestCase):
    def test_icicibank_signals_on_october_30(self):
        clock = live.Clock("2026-10-30 15:40")
        bars, _ = live.Source(clock, BUNDLE).chart("ICICIBANK", "60d")
        stops, sig = live.ut_bot(bars, key=1)           # TradingView's default
        self.assertEqual(stops[:9], [None] * 9)
        day = [(bars[i][0].strftime("%H:%M"), k) for i, k in sig if bars[i][0].date() == datetime.date(2026, 10, 30)]
        self.assertEqual(day[:4], [("09:15", "sell"), ("09:20", "buy"), ("09:35", "sell"), ("10:40", "buy")])
        self.assertEqual(len(day), 14)

    def test_a_bigger_key_and_heikin_ashi_give_fewer_signals(self):
        bars, _ = live.Source(live.Clock("2026-10-30 15:40"), BUNDLE).chart("ICICIBANK", "60d")
        on_30 = lambda sig: sum(1 for i, _ in sig if bars[i][0].date() == datetime.date(2026, 10, 30))  # noqa: E731
        counts = [on_30(live.ut_bot(bars, k, 10, ha)[1]) for k, ha in ((1, False), (2, False), (2, True), (3, True))]
        self.assertEqual(counts, [14, 4, 2, 1])
        self.assertEqual(live.ut_bot(bars)[1], live.ut_bot(bars, 2, 10, False)[1])   # the default is key 2

    def test_signals_alternate_and_sit_on_the_right_side_of_the_stop(self):
        t = datetime.datetime(2026, 10, 30, 9, 15)
        closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 104, 98, 95, 99, 104, 110]
        bars = [[t + i * live.BAR, c, c + 1, c - 1, c, 1000] for i, c in enumerate(closes)]
        stops, sig = live.ut_bot(bars, key=1, period=3)
        self.assertEqual([k for _, k in sig], ["sell", "buy"])
        for i, k in sig:
            self.assertTrue(bars[i][4] < stops[i] if k == "sell" else bars[i][4] > stops[i])

    def test_the_page_carries_the_same_calculation(self):
        with open(live.PAGE, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("function utBot(bars, key, period, heikin)", page)
        self.assertIn("candles.attachPrimitive(utLabels)", page)   # filled Buy / Sell labels on the candles
        self.assertIn("function dayLevels(bars, isIndex)", page)    # the same levels as orb_plan
        self.assertIn("candles.attachPrimitive(levelLines)", page)


class FakeNotifier:
    channels = ["fake"]

    def __init__(self):
        self.msgs = []

    def send(self, subject, text):
        self.msgs.append((subject, text))
        return [("fake", True, "")]


class Alerts(unittest.TestCase):
    def run_day(self, mode):
        import tempfile
        s = store("2026-10-30 09:20")
        fake = FakeNotifier()
        al = live.Alerter(s, fake, folder=tempfile.mkdtemp(), echo=lambda *a: None, mode=mode)
        t = datetime.datetime(2026, 10, 30, 9, 20, 15)
        while t.time() < datetime.time(15, 40):
            s.clock.fixed = t
            s.refresh_board()
            al.tick()
            t += datetime.timedelta(minutes=5)
        al.tick()
        return fake.msgs

    def test_a_whole_day_once_each(self):
        msgs = self.run_day("all")
        subjects = [m[0] for m in msgs]
        self.assertEqual((subjects[0], subjects[-1], len(msgs)), ("NSE levels 30 Oct", "NSE summary 30 Oct", 47))
        self.assertIn("ICICIBANK   BUY  > 1,390.90 SL 1,383.40 T1 1,398.40 T2 1,405.90 T3 1,413.40", msgs[0][1])
        self.assertIn("            SELL < 1,383.40 SL 1,390.90 T1 1,375.90 T2 1,368.40 T3 1,360.90", msgs[0][1])
        text = "\n".join(m[1] for m in msgs[1:-1])
        self.assertEqual(text.count("BUY ICICIBANK 09:35"), 1)
        self.assertIn("BUY ICICIBANK 09:35: above 1,390.90 SL 1,383.40 T1 1,398.40 T2 1,405.90 T3 1,413.40. Nothing placed.", text)
        self.assertIn("T1 hit BUY ICICIBANK 11:10 at 1,398.40.", text)
        self.assertIn("OUT at 15:10 BUY ICICIBANK 15:10 at 1,395.00 (entry 1,390.90, 1 of 3 targets).", text)
        self.assertIn("BUY 34: T1 17, T2 7, T3 2, SL before T1 4; SELL 25: T1 4, T2 0, T3 0, SL before T1 15.", msgs[-1][1])

    def test_daily_mode_sends_the_levels_and_the_summary_only(self):
        self.assertEqual([m[0] for m in self.run_day("daily")], ["NSE levels 30 Oct", "NSE summary 30 Oct"])

    def test_channels_come_from_the_environment_and_secrets_stay_out_of_errors(self):
        n = live.Notifier({"NSE_WHATSAPP_PHONE": "+910000000000", "NSE_WHATSAPP_APIKEY": "secret123", "NSE_NTFY_TOPIC": ""})
        self.assertEqual(n.channels, ["whatsapp"])

        def boom(subject, text):
            raise OSError("https://api.callmebot.com/whatsapp.php?phone=x&apikey=secret123 failed")
        n._whatsapp = boom
        (c, ok, err), = n.send("s", "t")
        self.assertFalse(ok)
        self.assertNotIn("secret123", err)

    def test_long_lists_are_split_on_lines(self):
        parts = live.chunks("\n".join("line %03d %s" % (i, "x" * 40) for i in range(100)), 500)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual(sum(p.count("\n") for p in parts), 100)


class Data(unittest.TestCase):
    def test_parse_drops_the_live_tick_and_the_closing_print(self):
        with gzip.open(BUNDLE, "rt", encoding="utf-8") as fh:
            chart = json.load(fh)["ICICIBANK"]
        res = chart["chart"]["result"][0]
        ts = res["timestamp"][-1]
        res["timestamp"] = res["timestamp"] + [ts + 7, ts + 300 * 6]
        for k in res["indicators"]["quote"][0]:
            res["indicators"]["quote"][0][k] = res["indicators"]["quote"][0][k] + [1.0, 1.0]
        bars, _ = live.parse_chart(chart)
        self.assertTrue(all(b[0].minute % 5 == 0 and b[0].second == 0 for b in bars))
        self.assertTrue(all(b[0].time() < live.CLOSE for b in bars))

    def test_timeframes_carry_the_earlier_sessions(self):
        s = store("2026-10-30 10:00")
        days = lambda cs: len({live.ts_to_ist(c[0]).date() for c in cs})  # noqa: E731
        five, fifteen, hour, daily = (s.chart("ICICIBANK", tf)["candles"] for tf in ("5m", "15m", "1h", "1d"))
        self.assertEqual([days(five), days(fifteen), days(hour), len(daily)], [60, 60, 60, 60])
        last = live.ts_to_ist(fifteen[-1][0])
        self.assertEqual((last.date(), last.time()), (datetime.date(2026, 10, 30), datetime.time(9, 45)))
        self.assertEqual([live.ts_to_ist(c[0]).minute for c in hour[-7:]], [15] * 7)
        today = daily[-1]   # 9:15 to 10:00 so far: open of the first bar, close of the last
        self.assertEqual((today[1], today[4]), (1390.0, 1394.3))

    def test_resample_keeps_high_low_and_volume(self):
        t = datetime.datetime(2026, 10, 30, 9, 15)
        bars = [[t + i * live.BAR, 10 + i, 12 + i, 9 + i, 11 + i, 100] for i in range(4)]
        self.assertEqual([c[1:] for c in live.resample(bars, "15m")], [[10, 14, 9, 13, 300], [13, 15, 12, 14, 100]])

    def test_market_state(self):
        st = lambda s: live.market_state(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M"))[0]  # noqa: E731
        self.assertEqual([st("2026-11-02 09:00"), st("2026-11-02 11:00"), st("2026-11-02 15:30"),
                          st("2026-10-31 11:00"), st("2026-11-10 11:00")], ["pre", "open", "closed", "closed", "closed"])


if __name__ == "__main__":
    unittest.main()
