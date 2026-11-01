"""The repo: eight nse-* skills, the plugin manifest, the README and the safety defaults."""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT  # noqa: E402

SKILLS = ["nse-scanner", "nse-entry", "nse-exit", "nse-timemachine", "nse-budget", "nse-risk", "nse-paper", "nse-chart"]
INDIA_BLOCK = ("# ---------------------------------------------------------------- the India block",
               "# ---------------------------------------------------------------- end of the India block")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def frontmatter(path):
    text = read(path)
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def scripts():
    for s in SKILLS:
        for f in os.listdir(os.path.join(ROOT, s)):
            if f.endswith(".py"):
                yield s, f, read(s, f)


class Repo(unittest.TestCase):
    def test_eight_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)

    def test_the_same_safety_section_everywhere(self):
        safety = read("ref", "SAFETY.md").strip()
        for s in SKILLS:
            text = read(s, "SKILL.md")
            self.assertIn(safety, text, s)
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)

    def test_every_script_carries_the_same_india_block(self):
        src = read("ref", "india_common.py")
        block = src[src.index(INDIA_BLOCK[0]):src.index(INDIA_BLOCK[1])]
        for s, f, text in scripts():
            if INDIA_BLOCK[0] in text:
                self.assertIn(block, text, "%s/%s: re-paste ref/india_common.py" % (s, f))

    def test_no_us_leftovers(self):
        for s, f, text in scripts():
            for bad in ("Alpaca", "NYSE", "New York", "pdt_guard", "_usd"):
                self.assertNotIn(bad, text, "%s/%s: %s" % (s, f, bad))
        for s in SKILLS:
            text = read(s, "SKILL.md")
            for bad in ("Alpaca", "NYSE", "TSLA", "/trader-"):
                self.assertNotIn(bad, text, "%s/SKILL.md: %s" % (s, bad))

    def test_plugin_manifest_points_at_the_root_skills(self):
        plugin = json.loads(read(".claude-plugin", "plugin.json"))
        self.assertEqual((plugin["name"], plugin["skills"]), ("india-day-trader-skill", "./"))
        market = json.loads(read(".claude-plugin", "marketplace.json"))
        self.assertEqual(market["plugins"][0]["name"], "india-day-trader-skill")

    def test_readme_names_every_skill_and_the_fine_print(self):
        readme = read("README.md")
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)
        for line in ("## Fine print", "Not financial advice", "Jakeschincariol/day-trader-skill"):
            self.assertIn(line, readme)

    def test_no_personal_email_anywhere(self):
        for base, dirs, files in os.walk(ROOT):
            # .wrangler (Wrangler's own cache), node_modules and trader/ are git-ignored: never shipped
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".tmp", "fixtures", ".wrangler", "node_modules", "trader")]
            for f in files:
                if f.endswith((".md", ".py", ".json")):
                    with open(os.path.join(base, f), encoding="utf-8", errors="replace") as fh:
                        self.assertNotRegex(fh.read(), r"[\w.]+@gmail\.com", f)

    def test_every_tool_runs_on_python_3_8(self):
        for s, f, src in scripts():
            for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix("):
                self.assertNotIn(bad, src, "%s/%s" % (s, f))
            self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
            for g in ("list[", "dict[", "tuple["):
                self.assertNotRegex(src, r"(?<![\w.\"'])%s" % re.escape(g), "%s/%s" % (s, f))

    def test_safe_defaults_ship_in_the_example_config(self):
        cfg = json.loads(read("nse-budget", "config.example.json"))
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "min_reward_risk",
                  "require_stop", "no_entries_after", "flat_by", "product"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg["require_stop"], True)
        for k in ("allow_short", "allow_margin", "allow_options"):
            self.assertIs(cfg[k], False, k)
        self.assertLess(cfg["flat_by"], "15:20")


if __name__ == "__main__":
    unittest.main()
