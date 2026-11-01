import datetime
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # `python -m unittest tests.test_scanner` from the root
from _load import load, FIX  # noqa: E402

scan = load("nse-scanner", "scan")

NSE = os.path.join(FIX, "nse")
BUNDLE = os.path.join(NSE, "nifty50-5m-60d.json.gz")      # 50 real v8 charts, 5-min bars, Aug 5 to Oct 30 2026
DAILY = os.path.join(NSE, "nifty50-daily-spark.json.gz")  # a real spark answer, 3 months of daily closes
DT = datetime.datetime
D = datetime.date
OCT30 = D(2026, 10, 30)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def gz_text(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    fmt = "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2 else "%Y-%m-%d %H:%M"
    return int(scan.ist_to_utc(DT.strptime(ist_text, fmt)).timestamp())


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


class Time(unittest.TestCase):
    def test_ist_and_clock(self):
        self.assertEqual(scan.ts_to_ist(1793331900), DT(2026, 10, 30, 9, 15))   # the first 5-min bar on Oct 30
        self.assertEqual(scan.parse_now("2026-10-30 10:00"), DT(2026, 10, 30, 4, 30))   # naive UTC, no DST ever
        self.assertEqual(scan.parse_now("2026-01-05 10:00"), DT(2026, 1, 5, 4, 30))
        self.assertEqual(scan.parse_now("2026-10-30T04:30:00Z"), DT(2026, 10, 30, 4, 30))
        self.assertEqual(scan.parse_now("2026-10-30T10:00:00+05:30"), DT(2026, 10, 30, 4, 30))
        self.assertEqual(scan.ist_stamp(DT(2026, 10, 30, 4, 30)), "2026-10-30T10:00:00+05:30")
        self.assertEqual(scan.clock_label(DT(2026, 10, 30, 9, 35)), "9:35 AM")
        self.assertEqual(scan.clock_label(datetime.time(12, 5)), "12:05 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_nse_calendar(self):
        self.assertEqual(scan.market_day(OCT30), (True, ""))
        self.assertEqual(scan.market_day(D(2026, 10, 31)), (False, "weekend"))
        self.assertEqual(scan.market_day(D(2026, 10, 2)), (False, "an NSE holiday"))    # Gandhi Jayanti
        self.assertEqual(scan.market_day(D(2026, 11, 10)), (False, "an NSE holiday"))   # Diwali-Balipratipada
        self.assertEqual(scan.market_day(D(2026, 11, 11), {"holidays": ["2026-11-11"]}),
                         (False, "an NSE holiday"))
        self.assertEqual(scan.session(OCT30), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(scan.session(D(2026, 10, 20)))                               # Dussehra
        self.assertIsNone(scan.calendar_warning(OCT30))
        self.assertIn("2027 holiday list is not built in", scan.calendar_warning(D(2027, 1, 4)))


class Universe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.equity = gz_text(os.path.join(NSE, "EQUITY_L.csv.gz"))
        cls.seclist = gz_text(os.path.join(NSE, "sec_list.csv.gz"))

    def test_eq_only_with_bands(self):
        kept, counts = scan.build_universe(self.equity, self.seclist)
        rows = {r[0]: r for r in kept}
        self.assertEqual((counts["rows"], counts["kept"], len(kept)), (2620, 2357, 2357))
        self.assertEqual((counts["series BE"], counts["series BZ"]), (230, 33))
        self.assertEqual(rows["RELIANCE"], ("RELIANCE", "Reliance Industries Limited", "EQ", "No Band"))
        self.assertEqual(rows["ICICIBANK"], ("ICICIBANK", "ICICI Bank Limited", "EQ", "No Band"))
        self.assertEqual(rows["M&M"], ("M&M", "Mahindra & Mahindra Limited", "EQ", "No Band"))
        self.assertEqual(rows["20MICRONS"][3], "20")
        self.assertEqual(rows["21STCENMGM"][3], "5")
        self.assertEqual(rows["ABCOTS"][3], "")              # not in sec_list.csv: band unknown
        self.assertEqual(counts["no_band"], 1)
        self.assertEqual(rows["ELECTCAST"][3], "20")         # listed twice in sec_list (EQ and W1)
        self.assertNotIn("3IINFOLTD", rows)                  # series BE
        self.assertEqual([r[0] for r in kept], sorted(r[0] for r in kept))

    def test_include_be(self):
        kept, counts = scan.build_universe(self.equity, self.seclist, include_be=True)
        rows = {r[0]: r for r in kept}
        self.assertEqual(len(kept), 2587)
        self.assertEqual(rows["3IINFOLTD"][2], "BE")
        self.assertNotIn("series BE", counts)
        self.assertEqual(counts["series BZ"], 33)

    def test_band_matches_the_series(self):
        sec = "Symbol,Series,Security Name,Band,Remarks\nXYZ,BE,XYZ LTD,5,-\nXYZ,EQ,XYZ LTD,20,-\n"
        bands = scan.parse_seclist(sec)
        self.assertEqual(scan.band_of(bands, "XYZ", "EQ"), "20")
        self.assertEqual(scan.band_of(bands, "XYZ", "BE"), "5")
        self.assertEqual(scan.band_of(bands, "XYZ", "BZ"), "5")    # unknown series: the first one seen
        self.assertEqual(scan.band_of(bands, "NOPE", "EQ"), "")
        self.assertEqual(scan.band_of({}, "XYZ", "EQ"), "")

    def test_index_lists(self):
        with open(os.path.join(NSE, "nifty50.csv"), encoding="utf-8") as fh:
            kept, counts = scan.build_index_universe(fh.read(), self.seclist, "nifty50")
        self.assertEqual(len(kept), 50)
        self.assertIn(("ICICIBANK", "ICICI Bank Ltd.", "EQ", "No Band"), kept)
        self.assertTrue(all(r[3] == "No Band" for r in kept))     # all F&O stocks
        kept, counts = scan.build_index_universe(gz_text(os.path.join(NSE, "nifty500.csv.gz")), self.seclist,
                                                 "nifty500")
        self.assertEqual(len(kept), 501)

    def test_not_an_nse_file(self):
        with self.assertRaises(scan.ScanError) as cm:
            scan.build_universe("<html>Access Denied</html>", self.seclist)
        self.assertIn("does not look like NSE's file", str(cm.exception))


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        skipped = []
        data = scan.parse_spark(gz_text(DAILY), None, skipped)
        self.assertEqual(len(data), 40)            # 41 answers, one is not an NSE stock
        self.assertEqual(skipped, ["M (Macy's, Inc., NYQ)"])
        self.assertIn("BAJAJ-AUTO", data)          # keyed by the plain NSE symbol
        t = data["ICICIBANK"]
        self.assertEqual(len(t["t"]), 64)
        self.assertIsNone(t["h"])                  # spark is closes only
        self.assertEqual(t["meta"]["regularMarketVolume"], 10522673)
        closes = scan.daily_closes(t, scan.parse_now("2026-10-30 15:30"))
        self.assertEqual(closes[-1][0], OCT30)
        closes = scan.daily_closes(t, scan.parse_now("2026-10-30 15:29"))   # Friday's bar still in progress
        self.assertEqual(closes[-1][0], D(2026, 10, 29))

    def test_why_ten_nifty_stocks_were_missing(self):
        """The fixture was fetched with M&M.NS unquoted in the URL. The & ended the symbol
        list: Yahoo saw '...,LT.NS,M' and answered Macy's (M, a US stock). M&M and the 9 symbols
        after it in that request of 20 never reached Yahoo."""
        batch = ["HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK",
                 "LT", "M&M", "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC", "POWERGRID", "RELIANCE",
                 "SBILIFE", "SHRIRAMFIN"]
        unquoted = "symbols=" + ",".join(s + ".NS" for s in batch) + "&range=3mo&interval=1d"
        self.assertTrue(urllib.parse.parse_qs(unquoted)["symbols"][0].endswith(",LT.NS,M"))
        path = scan.spark_path(batch, "3mo", "1d")
        self.assertIn("LT.NS,M%26M.NS,MARUTI.NS", path)
        got = urllib.parse.parse_qs(path.split("?", 1)[1])["symbols"][0].split(",")
        self.assertEqual(got, [s + ".NS" for s in batch])
        # with `wanted`, the stray Macy's answer is ignored, not mistaken for M&M
        skipped = []
        data = scan.parse_spark(gz_text(DAILY), {scan.to_yahoo(s): s for s in batch}, skipped)
        self.assertEqual(sorted(data), sorted(batch[:10]))
        self.assertIn("M (Macy's, Inc., NYQ)", skipped)

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls(self):
        self.assertEqual(scan.spark_path(["BAJAJ-AUTO", "reliance", "J&KBANK"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=BAJAJ-AUTO.NS,RELIANCE.NS,J%26KBANK.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("M&M", "1d", "5m"), "/v8/finance/chart/M%26M.NS?range=1d&interval=5m")
        self.assertEqual(scan.chart_path("ICICIBANK.NS", "1d", "5m"),
                         "/v8/finance/chart/ICICIBANK.NS?range=1d&interval=5m")


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        self.body = gz_text(DAILY)

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20, ua=None):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=ICICIBANK.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/ICICIBANK.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE", "BAJAJ-AUTO", "BAJFINANCE",
                "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
                "GRASIM", "HCLTECH", "HDFCBANK", "NOSUCHSTOCK"] + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["NOSUCHSTOCK"], "no data from Yahoo")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-10-30 16:00"), 50, 100000)
        self.assertEqual(counts["requested"], 40)
        self.assertEqual(counts["fetched"], 19)

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # After the close on Fri Oct 30 2026: closes through Oct 30.
    EVENING = {
        "INFY": ["breakout_20d"], "TRENT": ["breakout_20d"], "ETERNAL": ["breakout_20d"],
        "BSE": ["breakout_20d"], "KOTAKBANK": ["breakout_20d"], "SUNPHARMA": ["reclaim_ma20"],
        "INDIGO": ["reclaim_ma20"], "TCS": ["bull_flag"], "LT": ["breakout_20d"],
        "HCLTECH": ["breakout_20d", "bull_flag"], "TECHM": ["bull_flag"], "APOLLOHOSP": ["breakout_20d"],
        "DRREDDY": ["breakout_20d"], "CIPLA": ["breakout_20d"], "TATACONSUM": ["breakout_20d"],
    }
    # Before the open on Oct 30: closes through Thu Oct 29.
    MORNING = {
        "INFY": ["bull_flag"], "ETERNAL": ["breakout_20d"], "ADANIPORTS": ["breakout_20d"],
        "BSE": ["breakout_20d"], "KOTAKBANK": ["breakout_20d"], "TCS": ["bull_flag"], "SBIN": ["reclaim_ma20"],
        "LT": ["breakout_20d"], "HCLTECH": ["bull_flag"], "TECHM": ["bull_flag"], "TITAN": ["reclaim_ma20"],
        "ITC": ["reclaim_ma20"], "HINDUNILVR": ["reclaim_ma20"], "DRREDDY": ["breakout_20d"],
        "GRASIM": ["reclaim_ma20"], "TATACONSUM": ["breakout_20d"], "TMPV": ["oversold_bounce"],
    }

    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(DAILY)
        cls.syms = sorted(cls.data)

    def test_setups_after_the_close(self):
        self.assertEqual(len(self.syms), 40)
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 16:00"), 50, 100000)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EVENING)
        self.assertEqual(last, OCT30)
        self.assertEqual(counts, {"requested": 40, "fetched": 40, "illiquid": 0, "short": 0, "scanned": 40,
                                  "hits": 15})
        self.assertEqual(hits[0], {"symbol": "INFY", "setups": ["breakout_20d"], "price": 1185.0,
                                   "close_date": "2026-10-30", "rsi": 64.3, "volume": 13029753})
        closes = [c for _, c in scan.daily_closes(self.data["INFY"], scan.parse_now("2026-10-30 16:00"))]
        self.assertEqual(max(closes[-21:-1]), 1168.0)   # 1185.00 closed over the best close of the 20 before

    def test_setups_before_the_open(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 50, 100000)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.MORNING)
        self.assertEqual(last, D(2026, 10, 29))
        # TMPV: RSI 28.1 on Tuesday (Yahoo has no Oct 28 close for it), up Thursday 261.20 -> 266.00, RSI 34.3
        tmpv = [h for h in hits if h["symbol"] == "TMPV"][0]
        self.assertEqual((tmpv["price"], tmpv["rsi"]), (266.0, 34.3))

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 15:29"), 0, 0)
        self.assertEqual(before, during)
        bars = scan.daily_closes(self.data["INFY"], scan.parse_now("2026-10-30 15:29"))
        self.assertEqual(bars[-1], (D(2026, 10, 29), 1146.5))
        bars = scan.daily_closes(self.data["INFY"], scan.parse_now("2026-10-30 15:30"))
        self.assertEqual(bars[-1], (OCT30, 1185.0))

    def test_liquidity_filter(self):
        # a 2-lakh share floor drops ULTRACEMCO (1,99,529 shares at Rs 11,045: Rs 220 crore traded)
        _, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 16:00"), 50, 200000)
        self.assertEqual(counts["illiquid"], 1)
        # a Rs 300 price floor drops TATASTEEL, JIOFIN, TMPV and ITC (the last price, Rs 167.60 to 265.60)
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-30 09:00"), 300, 100000)
        self.assertEqual(counts["illiquid"], 4)
        self.assertNotIn("TMPV", [h["symbol"] for h in hits])
        self.assertNotIn("ITC", [h["symbol"] for h in hits])

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(5, 15)], "c": [100.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-10-30 09:00"), 50, 100000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi_is_wilders(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(BUNDLE)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=0, syms=None, or_minutes=15, until=datetime.time(14, 30)):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, until)
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_icicibank_at_935(self):
        hits, counts, failed = self.scan_at("2026-10-30 09:35")
        self.assertEqual((len(self.syms), counts["scanned"], self.errors), (50, 50, {}))
        icici = [h for h in hits if h["symbol"] == "ICICIBANK"][0]
        self.assertEqual(icici, {"symbol": "ICICIBANK", "setups": ["orb"], "price": 1394.5, "at": "9:35 AM",
                                 "bar_start": "09:30", "bar_index": 3, "or_high": 1390.9, "or_low": 1383.4,
                                 "pct_over": 0.26})
        self.assertEqual([h["symbol"] for h in hits], ["BAJAJFINSV", "BAJFINANCE", "HCLTECH", "ICICIBANK", "INDIGO",
                                                       "JSWSTEEL", "KOTAKBANK", "TCS"])

    def test_the_10am_scan(self):
        hits, counts, _ = self.scan_at("2026-10-30 10:00")
        self.assertEqual((len(hits), counts["candidates"]), (13, 32))
        self.assertEqual([h["at"] for h in hits].count("9:35 AM"), 8)
        self.assertEqual([h["symbol"] for h in hits[8:]], ["APOLLOHOSP", "ETERNAL", "AXISBANK", "BAJAJ-AUTO",
                                                           "SBILIFE"])   # by time, then symbol

    def test_35_of_50_by_the_close(self):
        hits, counts, _ = self.scan_at("2026-10-30 15:29", until=datetime.time(15, 30))
        self.assertEqual((len(hits), counts["scanned"]), (35, 50))
        hits, _, _ = self.scan_at("2026-10-30 14:30")
        self.assertEqual(len(hits), 34)

    def test_other_days_in_the_bundle(self):
        # the bundle holds 60 sessions: the clock picks the day
        hits, counts, _ = self.scan_at("2026-10-29 10:00")
        self.assertEqual((counts["scanned"], len(hits)), (50, 22))
        self.assertEqual((hits[0]["symbol"], hits[0]["price"], hits[0]["or_high"]), ("ADANIENT", 2505.7, 2505.2))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-10-30 10:00", min_price=1000)
        self.assertNotIn("ETERNAL", [h["symbol"] for h in hits])   # Rs 342.30
        self.assertIn("ICICIBANK", [h["symbol"] for h in hits])

    def test_in_progress_bar(self):
        hits, _, _ = self.scan_at("2026-10-30 09:39", syms=["ICICIBANK"])
        self.assertEqual(hits[0]["at"], "9:35 AM")
        day, cut = scan.orb_window(scan.parse_now("2026-10-30 09:39"), None, 15, None)
        bars = scan.session_bars(self.data["ICICIBANK"], day, cut)
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars], ["09:15", "09:20", "09:25", "09:30"])

    def test_yahoos_missing_closing_bars(self):
        # Yahoo's NSE charts never have a 3:20 bar (and often no 3:15 or 3:25): still usable
        s = self.data["ICICIBANK"]
        cut = DT(2026, 10, 30, 15, 30)
        bars = scan.session_bars(s, OCT30, cut)
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars[-2:]], ["15:15", "15:25"])
        self.assertTrue(scan.usable(bars, OCT30, cut, 3))

    def test_live_tick_and_closing_print_are_dropped(self):
        closes = [100, 100.2, 100.1, 100.5, 100.6]
        s = bars_series("2026-10-30", closes, extra=[("2026-10-30 09:41:17", 101.0), ("2026-10-30 15:30", 99.0)])
        bars = scan.session_bars(s, OCT30, DT(2026, 10, 30, 9, 42))
        self.assertEqual(len(bars), 5)                     # 9:15 to 9:35; the 9:41:17 tick dropped
        self.assertTrue(scan.usable(bars, OCT30, DT(2026, 10, 30, 9, 42), 3))
        self.assertEqual(scan.orb_hit(bars, 3)["at"], "9:35 AM")   # 9:30 bar closed 100.5 over 100.3

    def test_stage_one_is_a_necessary_condition(self):
        for cut in (DT(2026, 10, 30, 10, 0), DT(2026, 10, 30, 14, 30)):
            for sym, s in self.data.items():
                bars = scan.session_bars(s, OCT30, cut)
                if scan.usable(bars, OCT30, cut, 3) and scan.orb_hit(bars, 3):
                    self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}  # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, OCT30, DT(2026, 10, 30, 10, 0), 15, 0, 0,
                                             confirm)
        self.assertEqual(len(hits), 13)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 50)                    # stage 1 skipped some charts

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.NS" % sym)
        hits, counts, failed = scan.scan_orb(spark_like, ["ICICIBANK", "TCS"], OCT30, DT(2026, 10, 30, 10, 0), 15,
                                             0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["ICICIBANK"])

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        cut = DT(2026, 10, 30, 10, 0)
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, OCT30, cut, 15, 0, 0, confirm, max_confirm=10)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (10, counts["candidates"] - 10))
        self.assertLessEqual(len(hits), 10)
        hits_all, _, _ = scan.scan_orb(spark_like, self.syms, OCT30, cut, 15, 0, 0, confirm, max_confirm=0)
        self.assertEqual(len(hits_all), 13)
        for h in hits:
            self.assertIn(h, hits_all)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-10-30", [100, 100.2, 100.1, None, 100.5, 100.6, 100.7, 100.8, 100.9])
        bars = scan.session_bars(s, OCT30, DT(2026, 10, 30, 10, 0))
        self.assertFalse(scan.usable(bars, OCT30, DT(2026, 10, 30, 10, 0), 3))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-10-30", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], OCT30, DT(2026, 10, 30, 12, 0), 30, 0, 0)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(hits[0]["at"], "9:50 AM")          # range 9:15 to 9:45 (high 101.4), 9:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)
        with self.assertRaises(scan.ScanError) as cm:
            scan.orb_window(scan.parse_now("2026-10-30 09:45"), None, 30, None)
        self.assertIn("9:50 AM", str(cm.exception))

    def test_window_messages(self):
        until = datetime.time(14, 30)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-31 10:00"))
        self.assertIn("an NSE holiday", msg("2026-11-10 10:00"))
        self.assertIn("Too early", msg("2026-10-30 09:20"))
        self.assertIn("Run orb again at 9:35 AM or later", msg("2026-10-30 09:34"))
        self.assertIn("expired", msg("2026-10-30 14:31"))
        self.assertIn("expired", msg("2026-10-30 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 3:30 PM", msg("2026-10-30 15:45", until=None))
        self.assertIn("later than now", msg("2026-10-30 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-10-30 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-30 16:30"), datetime.time(10, 0), 15, until),
                         (OCT30, DT(2026, 10, 30, 10, 0)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-30 09:35"), None, 15, until)[1],
                         DT(2026, 10, 30, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-30 10:15"), None, 7, until)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.cfg = os.path.join(self.t, "config.json")   # not there: defaults

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", NSE, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 2,357 stocks (2,357 EQ).", text)
        self.assertIn("Dropped: 230 series BE, 33 series BZ (trade-for-trade", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2357)
        self.assertIn("RELIANCE|Reliance Industries Limited|EQ|No Band", lines)
        self.assertIn("ABCOTS|A B Cotspin India Limited|EQ|", lines)

        n50 = os.path.join(self.t, "nifty50.txt")
        code, text, _ = run(["universe", "--offline", NSE, "--index", "nifty50", "--out", n50])
        self.assertEqual(code, 0)
        self.assertIn("Kept 50 stocks.", text)
        self.assertIn("Price bands: 50 No Band (F&O).", text)
        self.assertEqual(scan.symbols_from_file(n50)[:2], ["ADANIENT", "ADANIPORTS"])

        # daily over the Nifty 50 with the spark fixture: 10 never came back, so partial
        code, text, _ = run(["daily", "--universe", n50, "--offline", DAILY, "--now", "2026-10-30 16:00",
                             "--out", self.wl, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Fetched 40 of 50 charts (10 failed or had no data). 40 passed the liquidity filter "
                      "(₹50+ and 1,00,000+ shares); 40 scanned.", text)
        self.assertIn("PARTIAL: no data for M&M, MARUTI, MAXHEALTH, NESTLEIND, NTPC, ONGC, POWERGRID, RELIANCE, "
                      "SBILIFE, SHRIRAMFIN.", text)
        self.assertIn("Ignored 1 answer for a symbol that is not one asked for: M (Macy's, Inc., NYQ).", text)
        self.assertIn("  INFY          ₹1,185.00  RSI  64.3  20-day closing-high breakout", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"], doc["as_of"]),
                         ("daily", 50, 40, 10, "2026-10-30 close"))
        self.assertEqual(len(doc["no_data"]), 10)
        self.assertEqual(doc["hits"][0], {"symbol": "INFY", "setups": ["breakout_20d"], "price": 1185.0,
                                          "close_date": "2026-10-30", "rsi": 64.3, "volume": 13029753})

        # the 10:00 AM opening-range scan over the Nifty 50
        code, text, _ = run(["orb", "--universe", n50, "--offline", BUNDLE, "--now", "2026-10-30 10:00",
                             "--out", self.wl, "--config", self.cfg])
        self.assertEqual(code, 0)
        self.assertIn("Opening-range breakout scan, Fri 2026-10-30 10:00 AM IST: 15-minute range "
                      "(9:15 to 9:30 AM)", text)
        self.assertIn("Fetched 50 of 50 charts; 50 usable; 32 candidates, 32 confirmed", text)
        self.assertIn("13 of 50 charts closed above their opening-range high by 10:00 AM", text)
        self.assertIn("   9:35 AM  ICICIBANK     ₹1,394.50  range 1,383.40 to 1,390.90  +0.26% over", text)
        self.assertIn("not a list of stocks to buy", text)
        self.assertIn("/nse-entry SYMBOL", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "exchange", "as_of", "universe",
                                              "scanned", "source")},
                         {"created_by": "nse-scanner", "created_at": "2026-10-30T10:00:00+05:30", "mode": "orb",
                          "exchange": "NSE", "as_of": "2026-10-30 10:00 IST", "universe": 50, "scanned": 50,
                          "source": "yahoo"})
        self.assertEqual(len(doc["hits"]), 13)
        self.assertIn({"symbol": "ICICIBANK", "setups": ["orb"], "price": 1394.5, "at": "9:35 AM",
                       "bar_start": "09:30", "bar_index": 3, "or_high": 1390.9, "or_low": 1383.4,
                       "pct_over": 0.26}, doc["hits"])

        # the hits read back as a watchlist, with --at instead of --now (the last day in the file)
        code, text, _ = run(["orb", "--offline", BUNDLE, "--at", "10:00", "--watchlist", self.wl, "--json",
                             "--no-write", "--config", self.cfg])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (13, 13, 13))

    def test_universe_messages(self):
        code, _, err = run(["universe", "--offline", NSE, "--index", "nifty100", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("no ind_nifty100list.csv (or nifty100.csv)", err)
        bare = os.path.join(self.d, "bare")
        os.makedirs(bare)
        shutil.copy(os.path.join(NSE, "EQUITY_L.csv.gz"), bare)
        code, text, _ = run(["universe", "--offline", bare, "--json", "--no-write"])
        out = json.loads(text)
        self.assertEqual((code, out["count"], out["bands"]), (0, 2357, {"unknown": 2357}))
        self.assertIn("price bands are left empty", out["band_note"])
        code, _, err = run(["universe", "--offline", self.d])
        self.assertIn("no EQUITY_L.csv", err)

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the Oct 30 bundle."""
        bundle = json.loads(gz_text(BUNDLE))
        calls = []

        def plain(y):
            return scan.from_yahoo(urllib.parse.unquote(y))

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&")[0].split(","):
                    r = ((bundle.get(plain(y)) or {}).get("chart") or {}).get("result")
                    if r:
                        closes = r[0]["indicators"]["quote"][0].get("close", [])
                        res.append({"symbol": urllib.parse.unquote(y),
                                    "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            return json.dumps(bundle[plain(path.split("/chart/")[1].split("?")[0])])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-10-30 10:05")  # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-10-30 10:00", "--max-confirm", "10",
                                 "--no-write", "--config", self.cfg])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("50 usable; 32 candidates, 10 confirmed", text)
        self.assertIn("PARTIAL: 22 more candidates were not checked", text)
        self.assertIn("of the 10 checked charts closed above their opening-range high by 10:00 AM", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 10)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 3)    # 50 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        self.assertTrue(any("M%26M.NS" in c for c in calls))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", BUNDLE, "--now", "2026-10-31 10:00", "--out", self.wl,
                            "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", BUNDLE, "--now", "2026-10-30 09:30", "--config", self.cfg])
        self.assertIn("Too early", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        code, _, err = run(["orb", "--offline", BUNDLE, "--now", "2026-10-30 14:45", "--config", self.cfg])
        self.assertIn("expired", err)                      # default no_entries_after 14:30
        os.makedirs(self.t)
        with open(self.cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", BUNDLE, "--now", "2026-10-30 10:15", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("after 10:00", err)
        code, _, err = run(["orb", "--offline", BUNDLE])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", BUNDLE, "--now", "2026-10-30 09:35", "--symbols", "RELIANCE",
                             "--out", self.wl, "--config", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 9:35 AM", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)
        code, _, err = run(["daily", "--offline", DAILY, "--now", "2027-01-04 09:00", "--no-write"])
        self.assertIn("2027 holiday list is not built in", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(gz_text(BUNDLE))
        for sym in ("ICICIBANK", "M&M", "RELIANCE"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        with open(os.path.join(folder, "GONE.json"), "w") as fh:
            json.dump({"chart": {"result": None, "error": {"description": "No data found, symbol may be delisted"}}},
                      fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-10-30 10:00", "--no-write", "--json",
                             "--config", self.cfg])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["ICICIBANK"])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("# my list\nINFY|Infosys Limited|EQ|No Band\ntcs.ns\n\nINFY\n")
        self.assertEqual(scan.symbols_from_file(txt), ["INFY", "TCS", "INFY"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "infy"}, {"symbol": "TCS"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["INFY", "TCS"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-10-30 16:00",
                             "--json", "--no-write", "--config", self.cfg])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-10-30 16:00", "--json",
                             "--no-write", "--config", self.cfg])
        self.assertEqual(json.loads(text)["universe"], 3)
        code, text, _ = run(["daily", "--offline", DAILY, "--symbols", "M&M,INFY", "--now", "2026-10-30 16:00",
                             "--json", "--no-write", "--config", self.cfg])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["failed"], out["no_data"]), (2, 1, ["M&M"]))

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-30 16:00", "--json", "--no-write",
                             "--config", self.cfg])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 15)
        self.assertEqual(out["filters"], {"min_price": 50.0, "min_volume": 100000})
        self.assertEqual(out["ignored"], ["M (Macy's, Inc., NYQ)"])
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")


if __name__ == "__main__":
    unittest.main()
