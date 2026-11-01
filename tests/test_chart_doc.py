"""nse-chart: what SKILL.md says the page shows is what page.html shows, so the two cannot drift apart again."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


DOC = read("nse-chart", "SKILL.md")
PAGE = read("nse-chart", "page.html")


def doc_row(name):
    """The 'What the page shows' table row for one part of the page."""
    m = re.search(r"^\| %s \| (.*) \|$" % re.escape(name), DOC, re.M)
    if not m:
        raise AssertionError("SKILL.md has no '%s' row in What the page shows" % name)
    return m.group(1)


def buttons(group_id):
    m = re.search(r'id="%s"[^>]*>(.*?)</div>' % group_id, PAGE, re.S)
    return re.findall(r"<button[^>]*>([^<]+)</button>", m.group(1))


class PageMatchesDoc(unittest.TestCase):
    def test_timeframes(self):
        self.assertEqual(buttons("tfSeg"), ["5m", "15m", "1H", "1D"])
        self.assertIn("(5m, 15m, 1H, 1D)", doc_row("Toolbar"))

    def test_watchlist_filters_and_their_thresholds(self):
        m = re.search(r'class="filters"[^>]*>(.*?)</div>', PAGE, re.S)
        self.assertEqual(re.findall(r">([^<]+)</button>", m.group(1)), ["All", "Broke out", "Bullish", "Bearish"])
        self.assertIn("Broke out, Bullish (+3 or more), Bearish (−3 or less)", doc_row("Watchlist"))
        self.assertIn("state.filter === 'bull' && r.score >= 3", PAGE)
        self.assertIn("state.filter === 'bear' && r.score <= -3", PAGE)

    def test_indicator_switches(self):
        chips = re.findall(r'class="chip" data-ind="\w+"[^>]*><i[^>]*></i>([^<]+)</button>', PAGE)
        self.assertEqual(chips, ["UT Bot", "Day levels"])
        self.assertIn("Indicator switches over the chart: UT Bot and Day levels", doc_row("Chart"))

    def test_ut_bot_presets_and_the_calm_default(self):
        presets = re.findall(r'<button type="button" data-k="([\d.]+)" data-a="(\d+)" data-h="([01])">(\w+)</button>', PAGE)
        self.assertEqual(presets, [("1", "10", "0", "Sensitive"), ("2", "10", "0", "Balanced"), ("3", "10", "1", "Calm")])
        row = doc_row("UT Bot Alerts")
        for words in ("Sensitive (key 1", "Balanced (key 2)", "Calm (key 3, Heikin Ashi, the default here)"):
            self.assertIn(words, row)
        self.assertIn("ut: { on: true, key: 3, atr: 10, ha: true }", PAGE)

    def test_ut_bot_flip_rates_quoted_on_the_page_match_the_doc_table(self):
        page = {(k, h): r for k, h, r in re.findall(r"'(\d)\|10\|([01])': '[^']*?(\d+) % flipped back", PAGE)}
        doc = {}
        for setting, pct in re.findall(r"^\| (.*?) \| [\d.]+ \| (\d+) % \|$", DOC, re.M):
            key = re.search(r"key (\d)", setting).group(1)
            doc[(key, "1" if "Heikin Ashi" in setting else "0")] = pct
        self.assertEqual(len(doc), 5, "the UT Bot table in SKILL.md changed shape")
        for setting, pct in doc.items():
            self.assertEqual(page.get(setting), pct, "UT Bot key %s, Heikin Ashi %s" % setting)

    def test_refresh_every_30_seconds(self):
        self.assertIn("The chart you look at refreshes every 30 seconds", DOC)
        self.assertIn("setInterval(loadChart, 30000)", PAGE)

    def test_address_opens_a_chart(self):
        self.assertIn("`?sym=ICICIBANK&tf=15m` in the address opens that chart", doc_row("Chart"))
        self.assertIn("u.get('sym')", PAGE)
        self.assertIn("u.get('tf')", PAGE)

    def test_the_bias_record_is_not_claimed_for_the_page(self):
        # the page shows each stock's score; the full read and its record are live.py bias and backtest
        self.assertIn("not on the page", doc_row("Bias"))
        self.assertNotIn("/api/backtest", PAGE)
        for claim in ("page shows the record", "the page, under the bias", "next to how often that read was right"):
            self.assertFalse(claim in DOC, "SKILL.md says: " + claim)

    def test_alerts_switch(self):
        self.assertIn('"Alerts" in the toolbar', DOC)
        self.assertIn("state.alertsOn ? 'Alerts on' : 'Alerts off'", PAGE)


if __name__ == "__main__":
    unittest.main()
