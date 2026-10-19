import json
import unittest
from pathlib import Path

import session_panel as sp

FX = Path(__file__).resolve().parent.parent / "fixtures"


class Parse(unittest.TestCase):
    # Expected totals are what agentplay 0.2.1 reports for fixtures/session.jsonl (agentplay export).
    def test_totals_match_agentplay(self):
        t = sp.parse(FX / "session.jsonl")["totals"]
        self.assertEqual((t["calls"], t["errors"], t["requests"], t["tokens"], t["outputTokens"], t["retries"],
                          t["durationMs"], t["userTurns"]), (8, 1, 9, 311408, 2820, 1, 75000, 1))


class Panel(unittest.TestCase):
    def setUp(self):
        self.cfg = sp.panel_config(sp.parse(FX / "session.jsonl"))

    def test_one_cycle_value_per_step_in_order(self):
        steps = self.cfg["machines"]["steps"]
        self.assertEqual(steps["type"], "cycle")
        self.assertEqual(len(steps["values"]), 8)
        self.assertTrue(steps["values"][5]["m"].startswith("FAILED"))
        self.assertEqual(steps["values"][5]["g"], "fail")

    def test_a_tool_box_per_kind_used(self):
        titles = [e["lines"][0]["t"] for e in self.cfg["elements"] if e["type"] == "box"]
        self.assertEqual(titles, ["you", "agent", "files", "search", "tests"])

    def test_render_upstream_minimal_config(self):
        html = sp.render(json.loads((FX / "minimal.json").read_text(encoding="utf-8")))
        self.assertIn("USER -> AGENT -> TOOLS", html)
        self.assertNotIn("<script src", html)

    def test_render_escapes_closing_script_tags(self):
        self.assertNotIn("</script><b>", sp.render({"elements": [{"type": "text", "x": 0, "y": 9, "t": "</script><b>"}]}))


if __name__ == "__main__":
    unittest.main()
