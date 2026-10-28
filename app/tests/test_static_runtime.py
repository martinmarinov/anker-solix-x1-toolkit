"""Guards for the runtime static tree: no fixture UI and no unused files."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
INDEX = STATIC / "index.html"

EXPECTED = {
    "index.html",
    "app.js",
    "home-view.js",
    "css/base.css",
    "css/home.css",
    "css/drawer.css",
    "assets/house.svg",
}

class StaticRuntimeTests(unittest.TestCase):
    def test_no_preview_fixture_files(self):
        self.assertFalse((STATIC / "preview.html").exists())
        self.assertFalse((STATIC / "preview.js").exists())

    def test_static_tree_is_exactly_the_runtime_set(self):
        actual = {
            p.relative_to(STATIC).as_posix()
            for p in STATIC.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        self.assertEqual(actual, EXPECTED)

    def test_every_static_asset_is_referenced(self):
        html = INDEX.read_text(encoding="utf-8")
        refs = set()
        for value in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
            path = value[len("/static/"):].split("?", 1)[0]
            refs.add(path)
        self.assertEqual(refs, EXPECTED - {"index.html"})

    def test_live_cards_start_empty_and_plan_controls_disabled(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="batteryFill"', html)
        self.assertRegex(html, r'id="batteryFill"[^>]*height="0"')
        self.assertRegex(html, r'id="reserveSoc"[^>]*disabled')
        self.assertRegex(html, r'id="gridCharging"[^>]*disabled')
        for value_id in ("vSolar", "vGrid", "vBatt", "vHome"):
            self.assertRegex(html, rf'id="{value_id}">—</span>')


if __name__ == "__main__":
    unittest.main()
