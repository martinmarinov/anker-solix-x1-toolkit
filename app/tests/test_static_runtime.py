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

    def test_battery_shimmer_is_directional_and_motion_safe(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertIn('[data-battery="discharging"] .battery-fill::after', css)
        self.assertIn('animation: battery-shimmer-down', css)
        self.assertIn('[data-battery="charging"] .battery-fill::after', css)
        self.assertIn('animation: battery-shimmer-up', css)
        self.assertIn('animation: none !important', css)
        self.assertIn('--battery-rgb', css)
        self.assertIn('--battery-color', css)

    def test_flow_dots_walk_towards_the_arrow(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertEqual(html.count('class="flow-dots"'), 3)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertIn('.flow-group.active .flow-dots', css)
        self.assertIn('animation: flow-dot-walk 1.4s linear infinite', css)
        self.assertIn('stroke-dashoffset: -18.1px', css)
        self.assertIn('.flow-dots { animation: none !important; }', css)

    def test_indicator_cards_share_one_width_token(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertIn('--tile-w:', css)
        self.assertEqual(css.count('width: var(--tile-w)'), 3)
        self.assertIn('grid-template-columns: minmax(0, 1fr);', css)

    def test_power_meters_start_dark_with_five_segments(self):
        html = INDEX.read_text(encoding="utf-8")
        home = re.search(r'id="homeBars".*?</div>', html, re.S)
        solar = re.search(r'id="solarBars".*?</div>', html, re.S)
        grid = re.search(r'id="gridBars".*?</div>', html, re.S)
        self.assertIsNotNone(home)
        self.assertIsNotNone(solar)
        self.assertIsNotNone(grid)
        for meter in (home, solar, grid):
            self.assertEqual(meter.group(0).count('class="power-bar"'), 5)
        self.assertRegex(html, r'id="homeBars"[^>]*role="meter"')
        self.assertRegex(html, r'id="solarBars"[^>]*role="meter"')
        self.assertRegex(html, r'id="gridBars"[^>]*role="meter"')
        for meter in ("homeBars", "solarBars", "gridBars"):
            self.assertRegex(html, rf'id="{meter}"[^>]*aria-valuemax="5"')
        self.assertNotIn('class="power-bar is-on"', html)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for level in ("green", "amber", "red", "export"):
            self.assertIn(f'.power-bars[data-level="{level}"]', css)
        self.assertIn('.power-bar.is-on', css)

    def test_solar_icon_has_sun_cloud_and_moon_variants(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="solarIcon"', html)
        self.assertIn('data-icon="sun"', html)
        for icon in ("sun", "moon", "cloud"):
            self.assertIn(f'class="solar-icon__{icon}"', html)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for icon in ("sun", "moon", "cloud"):
            self.assertIn(f'.solar-icon[data-icon="{icon}"]', css)

    def test_live_cards_start_empty_and_plan_controls_disabled(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="batteryHero"', html)
        self.assertIn('id="batteryFill"', html)
        self.assertIn('id="batteryShell"', html)
        self.assertRegex(html, r'id="batteryHero"[^>]*style="--soc:0"')
        self.assertRegex(html, r'id="batterySoc">—</span>')
        self.assertRegex(html, r'id="batterySummary">—</span>')
        self.assertRegex(html, r'id="batterySocUnit"[^>]*hidden')
        self.assertIn('id="liveStatus"', html)
        self.assertIn('id="clockTime"', html)
        self.assertRegex(html, r'id="reserveSoc"[^>]*disabled')
        self.assertRegex(html, r'id="gridCharging"[^>]*disabled')
        for value_id in ("vSolar", "vGrid", "vBatt", "vHome"):
            self.assertRegex(html, rf'id="{value_id}">—</span>')
        self.assertNotIn('id="siteName"', html)
        self.assertIn('data-flow="solar"', html)
        self.assertIn('data-flow="grid"', html)
        self.assertIn('data-flow="home"', html)
        self.assertNotIn('data-flow="battery"', html)


if __name__ == "__main__":
    unittest.main()
