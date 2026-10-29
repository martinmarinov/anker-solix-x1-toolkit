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

def _contrast(a: str, b: str) -> float:
    def lum(value: str) -> float:
        h = value.lstrip("#")
        channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4
                    for c in channels]
        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + .05) / (lo + .05)


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
        self.assertIn('@media (max-aspect-ratio: 95/100)', css)
        self.assertIn('--tile-w: 46%;', css)
        self.assertIn('bottom: .5%;', css)
        self.assertIn('--card-h:', css)
        self.assertEqual(css.count('height: var(--card-h)'), 3)
        self.assertIn('--card-h: calc(var(--u) * 25.9);', css)
        self.assertIn('transform: translateX(-50%);', css)

    def test_portrait_typography_and_icons_scale_by_half(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertIn('--type-scale: 1.5', css)
        self.assertIn('calc(var(--fs-value) * var(--type-scale))', css)
        self.assertIn('calc(calc(var(--u) * 7.8) * var(--type-scale))', css)
        self.assertIn(
            'calc(clamp(30px, calc(var(--u) * 9.2), 116px) * var(--type-scale))',
            css,
        )

    def test_power_meters_start_off_with_five_segments(self):
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

    def test_daylight_shell_and_system_font(self):
        base = (STATIC / "css/base.css").read_text(encoding="utf-8")
        self.assertIn("color-scheme: light", base)
        self.assertIn("--bg: #f4efe4", base)
        self.assertIn("--ink: #17252f", base)
        self.assertIn("system-ui", base)
        self.assertIn("Roboto", base)
        self.assertNotIn("@font-face", base)
        self.assertNotIn("http://", base)
        self.assertNotIn("https://", base)
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('<meta name="theme-color" content="#f4efe4">', html)

    def test_daylight_palette_clears_contrast_floor(self):
        base = (STATIC / "css/base.css").read_text(encoding="utf-8")
        tokens = {
            m.group(1): m.group(2)
            for m in re.finditer(r"--([\w-]+):\s*(#[0-9a-fA-F]{6});", base)
        }
        for name in ("card", "bg", "ink", "muted", "solar-ink", "grid-ink",
                     "home-ink", "battery-ink", "accent", "good", "warn", "bad"):
            self.assertIn(name, tokens)
        self.assertGreaterEqual(_contrast(tokens["ink"], tokens["card"]), 7.0)
        self.assertGreaterEqual(_contrast(tokens["muted"], tokens["bg"]), 4.5)
        for name in ("solar-ink", "grid-ink", "home-ink", "battery-ink"):
            self.assertGreaterEqual(_contrast(tokens[name], tokens["card"]), 4.5)
        for name in ("accent", "good", "warn", "bad"):
            self.assertGreaterEqual(_contrast(tokens[name], tokens["bg"]), 4.5)
        for name in ("good", "warn", "bad", "grid-ink", "muted", "slate"):
            h = tokens[name].lstrip("#")
            expected = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            m = re.search(rf"--{re.escape(name)}-rgb:\s*(\d+),\s*(\d+),\s*(\d+);", base)
            self.assertIsNotNone(m, name)
            self.assertEqual(expected, tuple(int(v) for v in m.groups()), name)

    def test_home_cards_are_light_paper_with_ink_readouts(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertIn("border-top: 4px solid var(--card-accent)", css)
        self.assertIn("background: linear-gradient(155deg, #fffdf8 0%, #fffaf0 100%)", css)
        self.assertIn(".tile-value", css)
        self.assertIn("color: var(--card-accent)", css)
        self.assertIn("color: var(--ink);", css)
        self.assertNotIn("#06111d", css)
        self.assertNotIn("#081724", css)
        self.assertNotIn("rgba(16, 32, 49", css)

    def test_battery_readout_stays_dark_over_fill_and_headroom(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for selector in (".battery-soc", ".battery-power",
                         ".battery-caption-label", ".battery-caption-value"):
            rule = re.search(re.escape(selector) + r" \{(.*?)\}", css, re.S)
            self.assertIsNotNone(rule, selector)
            self.assertIn("color: var(--ink)", rule.group(1), selector)
        self.assertNotIn("--battery-ink:", css)
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertIn("--battery-ink-rgb", js)
        self.assertIn("batteryTone", js)

    def test_card_accent_follows_the_active_meter_band(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for level, token in (("green", "good"), ("amber", "warn"),
                             ("red", "bad"), ("export", "grid-ink"),
                             ("off", "muted")):
            self.assertIn(f'.tile[data-level="{level}"]', css)
            self.assertIn(f"--card-accent: var(--{token});", css)
            self.assertIn(f'.power-bars[data-level="{level}"]', css)
        self.assertIn('.power-bars[data-level="green"] { --bar-color: var(--good); }', css)
        self.assertIn('.power-bars[data-level="amber"] { --bar-color: var(--warn); }', css)
        self.assertIn('.power-bars[data-level="red"] { --bar-color: var(--bad); }', css)
        self.assertIn('.power-bars[data-level="export"] { --bar-color: var(--grid-ink); }', css)
        self.assertIn("rgba(var(--card-accent-rgb), .18)", css)
        icon = re.search(r"\.tile-icon \{(.*?)\}", css, re.S)
        unit = re.search(r"\.tile-unit \{(.*?)\}", css, re.S)
        self.assertIsNotNone(icon)
        self.assertIsNotNone(unit)
        self.assertIn("color: var(--card-accent)", icon.group(1))
        self.assertIn("color: inherit", unit.group(1))
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertIn("function setCardLevel", js)
        self.assertEqual(js.count("setCardLevel(el, band)"), 3)

    def test_portrait_home_wire_routes_around_the_caption(self):
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertIn('matchMedia("(max-aspect-ratio: 95/100)")', js)
        self.assertNotIn('max-aspect-ratio: 0.95', js)
        self.assertIn("var capLeft = summary ? rectOf(summary).left", js)
        self.assertIn("Math.min(battery.left + battery.width * 0.12, capLeft - u)", js)
        self.assertIn("bendY = Math.max(battery.bottom + u * 3", js)
        self.assertIn("homeTile.top - u * 6", js)

    def test_house_is_a_simple_daylight_pastel(self):
        svg = (STATIC / "assets/house.svg").read_text(encoding="utf-8")
        self.assertIn("in daylight", svg)
        self.assertNotIn("at night", svg)
        for colour in ("#e3ecd3", "#b8d2ae", "#a7cde6", "#fffdf7"):
            self.assertIn(colour, svg)
        self.assertLess(svg.count("<linearGradient"), 10)

    def test_refresh_slider_is_bounded_to_sixty(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertRegex(html, r'id="refreshRange"[^>]*min="5"[^>]*max="60"')
        self.assertIn('step="5"', html)
        for element in ("refreshValue", "refreshMeta", "refreshStatus"):
            self.assertIn(f'id="{element}"', html)
        css = (STATIC / "css/drawer.css").read_text(encoding="utf-8")
        self.assertIn('.refresh-row input[type="range"]', css)
        self.assertIn("accent-color: var(--accent);", css)
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('"/api/live/interval"', js)
        self.assertIn("function saveRefresh", js)
        self.assertIn("state.liveSeq", js)

    def test_home_view_no_longer_filters_battery_locally(self):
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertNotIn("BATTERY_FILTER_MS", js)
        self.assertNotIn("batteryPowerSamples", js)
        self.assertIn("batt.hours_to_empty", js)
        self.assertIn("batt.hours_to_full", js)

    def test_drawer_about_describes_the_read_only_wake(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertNotIn("never publishes", html)
        self.assertIn("realtime wake request", html)


if __name__ == "__main__":
    unittest.main()
