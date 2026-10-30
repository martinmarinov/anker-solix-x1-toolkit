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
    "assets/house-night.svg",
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
        value = re.search(r"\.tile-value \{(.*?)\}", css, re.S)
        icon = re.search(r"\.tile-icon \{(.*?)\}", css, re.S)
        self.assertIsNotNone(value)
        self.assertIsNotNone(icon)
        self.assertIn("color: var(--ink)", value.group(1))
        self.assertIn("color: var(--ink)", icon.group(1))
        self.assertNotIn("color: var(--card-accent)", value.group(1))
        self.assertNotIn("color: var(--card-accent)", icon.group(1))
        self.assertNotIn("-webkit-text-stroke", css)
        self.assertNotIn("#06111d", css)
        self.assertNotIn("#081724", css)
        self.assertNotIn("rgba(16, 32, 49", css)

    def test_battery_readout_stays_dark_over_fill_and_headroom(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for selector in (".battery-soc", ".battery-power",
                         ".battery-caption-value"):
            rule = re.search(re.escape(selector) + r" \{(.*?)\}", css, re.S)
            self.assertIsNotNone(rule, selector)
            self.assertIn("color: var(--ink)", rule.group(1), selector)
        self.assertNotIn("--battery-ink:", css)
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertIn("--battery-ink-rgb", js)
        self.assertIn("batteryTone", js)

    def test_battery_caption_has_no_redundant_label(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertNotIn("battery-caption-label", html)
        self.assertNotIn(">Battery</span>", html)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertNotIn(".battery-caption-label", css)
        # The shell still names the region for assistive technology.
        self.assertIn('aria-label="Battery state"', html)

    def test_card_accent_matches_the_active_meter_fill(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for level, token, rgb in (("green", "bar-green", "bar-green-rgb"),
                                  ("amber", "bar-amber", "bar-amber-rgb"),
                                  ("red", "bad", "bad-rgb"),
                                  ("export", "bar-blue", "bar-blue-rgb"),
                                  ("off", "muted", "muted-rgb")):
            self.assertIn(f'.tile[data-level="{level}"]', css)
            self.assertIn(f"--card-accent: var(--{token});", css)
            self.assertIn(f"--card-accent-rgb: var(--{rgb});", css)
            self.assertIn(f'.power-bars[data-level="{level}"]', css)
        self.assertIn("rgba(var(--card-accent-rgb), .18)", css)
        icon = re.search(r"\.tile-icon \{(.*?)\}", css, re.S)
        value = re.search(r"\.tile-value \{(.*?)\}", css, re.S)
        unit = re.search(r"\.tile-unit \{(.*?)\}", css, re.S)
        self.assertIsNotNone(icon)
        self.assertIsNotNone(value)
        self.assertIsNotNone(unit)
        self.assertIn("color: var(--ink)", icon.group(1))
        self.assertIn("color: var(--ink)", value.group(1))
        self.assertIn("color: inherit", unit.group(1))
        self.assertIn("rgba(var(--card-accent-rgb)", value.group(1))
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertIn("function setCardLevel", js)
        self.assertEqual(js.count("setCardLevel(el, band)"), 3)

    def test_meter_bars_use_the_skylight_palette(self):
        base = (STATIC / "css/base.css").read_text(encoding="utf-8")
        for token, value, rgb in (("bar-green", "#7acc70", "122, 204, 112"),
                                  ("bar-amber", "#e6be4a", "230, 190, 74"),
                                  ("bar-blue", "#447cd6", "68, 124, 214")):
            self.assertIn(f"--{token}: {value};", base)
            self.assertIn(f"--{token}-rgb: {rgb};", base)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for level, token in (("green", "bar-green"),
                             ("amber", "bar-amber"),
                             ("red", "bad"),
                             ("export", "bar-blue"),
                             ("off", "muted")):
            self.assertIn(
                f'.power-bars[data-level="{level}"] '
                f'{{ --bar-color: var(--{token}); }}',
                css,
            )
        # The card accent reads these same tokens; the rim below keeps the
        # pale bar silhouettes above the 3:1 non-text floor.

    def test_solar_icon_keeps_the_ink_fill_for_every_variant(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertNotRegex(
            css,
            r'\.solar-icon\[data-icon="(?:moon|cloud)"\][^{]*\{[^}]*color:',
        )
        icon = re.search(r"\.tile-icon \{(.*?)\}", css, re.S)
        self.assertIsNotNone(icon)
        self.assertIn("color: var(--ink)", icon.group(1))

    def test_filled_bars_have_a_dark_rim(self):
        base = (STATIC / "css/base.css").read_text(encoding="utf-8")
        self.assertIn("--bar-edge: rgba(23, 37, 47, .40);", base)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        lit = re.search(r"\.power-bar\.is-on \{(.*?)\}", css, re.S)
        self.assertIsNotNone(lit)
        self.assertIn("border-color: var(--bar-edge)", lit.group(1))
        self.assertIn("background: var(--bar-color)", lit.group(1))
        track = re.search(r"\.power-bar \{(.*?)\}", css, re.S)
        self.assertIsNotNone(track)
        self.assertIn("border: 1px solid rgba(122, 108, 88, .18)",
                      track.group(1))

    def test_card_readout_and_icon_glow_with_the_meter_colour(self):
        base = (STATIC / "css/base.css").read_text(encoding="utf-8")
        for token, value in (("edge-green", "#5f9661"),
                             ("edge-amber", "#a18c48"),
                             ("edge-blue", "#3561a4"),
                             ("edge-red", "#862c30"),
                             ("edge-off", "#4b565e")):
            self.assertIn(f"--{token}: {value};", base)
        self.assertIn("--card-edge: var(--edge-off);", base)
        self.assertNotIn("rgba(23, 37, 47, .65)", base)
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        for level, edge in (("green", "edge-green"),
                            ("amber", "edge-amber"),
                            ("red", "edge-red"),
                            ("export", "edge-blue"),
                            ("off", "edge-off")):
            self.assertIn(f"--card-edge: var(--{edge});", css)
        self.assertIn("--glow-ring: calc(calc(var(--u) * .30) * var(--type-scale));", css)
        self.assertIn("--glow-bloom: calc(calc(var(--u) * .68) * var(--type-scale));", css)
        self.assertIn("--glow-wide: calc(calc(var(--u) * 1.05) * var(--type-scale));", css)
        self.assertNotIn("-webkit-text-stroke", css)
        self.assertNotIn("drop-shadow(1px 0 0", css)
        readout = re.search(r"\.tile-value \{(.*?)\}", css, re.S)
        self.assertIsNotNone(readout)
        for shadow in (
            "0 0 1px var(--card-edge)",
            "0 0 var(--glow-ring) var(--card-accent)",
            "0 0 var(--glow-bloom) rgba(var(--card-accent-rgb), .50)",
            "0 0 var(--glow-wide) rgba(var(--card-accent-rgb), .22)",
        ):
            self.assertIn(shadow, readout.group(1))
        icon = re.search(r"\.tile-icon \{(.*?)\}", css, re.S)
        self.assertIsNotNone(icon)
        self.assertEqual(icon.group(1).count("drop-shadow("), 4)
        for shadow in (
            "0 0 1px var(--card-edge)",
            "0 0 var(--glow-ring) var(--card-accent)",
            "0 0 var(--glow-bloom) rgba(var(--card-accent-rgb), .50)",
            "0 0 var(--glow-wide) rgba(var(--card-accent-rgb), .22)",
        ):
            self.assertIn(f"drop-shadow({shadow})", icon.group(1))

    def test_unavailable_cards_drop_the_glow(self):
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        value = re.search(
            r'\.home-stage\[data-available="false"\] \.tile-value \{(.*?)\}',
            css, re.S,
        )
        icon = re.search(
            r'\.home-stage\[data-available="false"\] \.tile-icon \{(.*?)\}',
            css, re.S,
        )
        self.assertIsNotNone(value)
        self.assertIsNotNone(icon)
        self.assertIn("color: var(--slate)", value.group(1))
        self.assertIn("text-shadow: none", value.group(1))
        self.assertIn("color: var(--slate)", icon.group(1))
        self.assertIn("opacity: .62", icon.group(1))
        self.assertIn("filter: none", icon.group(1))

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

    def test_night_house_keeps_the_day_geometry(self):
        day = (STATIC / "assets/house.svg").read_text(encoding="utf-8")
        night = (STATIC / "assets/house-night.svg").read_text(encoding="utf-8")

        def geometry(svg: str) -> str:
            svg = re.sub(r"<title.*?</title>", "", svg, flags=re.S)
            return re.sub(r'(?:fill|stroke|stop-color)="[^"]*"', "", svg)

        # Only the title and the palette may differ, so the two layers stay
        # pixel-aligned when the clock crossfades them.
        self.assertEqual(geometry(day), geometry(night))
        self.assertIn("at night", night)
        for warm in ("#ffe6ad", "#ffc978"):
            self.assertIn(warm, night)
        for daylight_colour in ("#e3ecd3", "#b8d2ae", "#a7cde6", "#fffdf7"):
            self.assertNotIn(daylight_colour, night)

    def test_scene_has_two_pixel_aligned_sky_layers(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="sceneDay"', html)
        self.assertIn('id="sceneNight"', html)
        self.assertEqual(html.count('class="scene-layer '), 2)
        day, night = re.findall(
            r'<svg class="scene-bg"[^>]*>(.*?)</svg>', html, re.S)
        # Same clouds and hills, same coordinates; the stars are the only new
        # geometry, otherwise the crossfade would ghost.
        self.assertEqual(
            re.findall(r'\sd="([^"]+)"', day),
            re.findall(r'\sd="([^"]+)"', night),
        )
        for radius in ("240", "54"):
            self.assertIn(f'<circle cx="800" cy="140" r="{radius}"', day)
            self.assertIn(f'<circle cx="800" cy="140" r="{radius}"', night)
        self.assertIn('id="moonGlow"', night)
        self.assertIn('fill="#f5f0dc"', night)
        self.assertGreater(night.count("<circle"), 40)
        # Duplicate inline SVG ids would make one layer use the other layer's
        # gradients.
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)))

    def test_night_hills_keep_the_ink_caption_legible(self):
        base = (STATIC / "css/base.css").read_text(encoding="utf-8")
        ink = re.search(r"--ink:\s*(#[0-9a-fA-F]{6});", base).group(1)
        html = INDEX.read_text(encoding="utf-8")
        for gradient in ("hillFarNight", "hillNearNight"):
            block = re.search(
                rf'<linearGradient id="{gradient}".*?</linearGradient>',
                html, re.S)
            self.assertIsNotNone(block, gradient)
            colours = re.findall(
                r'stop-color="(#[0-9a-fA-F]{6})"', block.group(0))
            self.assertGreaterEqual(len(colours), 2)
            for colour in colours:
                self.assertGreaterEqual(
                    _contrast(ink, colour), 4.5, (gradient, colour))

    def test_backdrop_fades_between_day_and_night_on_the_clock(self):
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        for line in ("var DAWN_START = 6 * 60;", "var DAWN_END = 7 * 60;",
                     "var DUSK_START = 19 * 60;", "var DUSK_END = 20 * 60;"):
            self.assertIn(line, js)
        self.assertIn("function nightAmount(minutes)", js)
        self.assertIn("return smoothstep((minutes - DUSK_START) / 60);", js)
        self.assertIn(
            "return 1 - smoothstep((minutes - DAWN_START) / 60);", js)
        self.assertIn("sceneDay.style.opacity = String(1 - night);", js)
        self.assertIn("sceneNight.style.opacity = String(night);", js)
        # The sky piggybacks the existing one-second clock; no extra timer.
        self.assertEqual(js.count("setInterval"), 1)
        changed = re.search(r"if \(t !== lastTime\) \{(.*?)\n    \}", js, re.S)
        self.assertIsNotNone(changed)
        self.assertIn("applySky(t);", changed.group(1))
        css = (STATIC / "css/home.css").read_text(encoding="utf-8")
        self.assertIn(".scene-layer {", css)
        self.assertIn(".scene-layer--night { opacity: 0; }", css)
        self.assertIn(".scene-glow--night {", css)
        self.assertIn(".house--night {", css)

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

    def test_refresh_poll_is_adaptive_with_a_two_second_floor(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("function pollDelayMs", js)
        self.assertIn("Math.max(2000", js)
        self.assertIn("interval * 1000 / 3", js)
        self.assertIn("function applyPollInterval", js)
        self.assertIn("applyPollInterval();", js)
        # The old fixed two-second poll must not come back.
        self.assertNotIn("}, 2000);", js)
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("one third of the MQTT interval", html)
        self.assertNotIn("polls this server every 2 s", html)

    def test_home_view_no_longer_filters_battery_locally(self):
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertNotIn("BATTERY_FILTER_MS", js)
        self.assertNotIn("batteryPowerSamples", js)
        self.assertIn("batt.hours_to_empty", js)
        self.assertIn("batt.hours_to_full", js)

    def test_battery_eta_renders_the_server_labelled_estimate(self):
        js = (STATIC / "home-view.js").read_text(encoding="utf-8")
        self.assertIn("batt.estimate", js)
        self.assertIn("root.dataset.estimate", js)
        self.assertIn("function batteryReadout", js)
        for fragment in ("eta.kind", "eta.label", '"to_empty"', '"to_full"',
                         '"to_soc"', '"at_full"', '"at_empty"',
                         "eta.soc", "eta.at", '"% by "', "charge_limit_kw"):
            self.assertIn(fragment, js)
        self.assertIn("batt.hours_to_empty", js)
        self.assertIn("batt.hours_to_full", js)
        # The calendar rule lives on the server; there is no second formatter
        # and no forced third line between the duration and the clock.
        self.assertNotIn("clockAfter", js)
        self.assertNotIn("toLocaleDateString", js)
        readout = js.split("function batteryReadout", 1)[1].split(
            "/* Battery time", 1)[0]
        self.assertNotIn("\\n", readout)
        self.assertNotIn('est.mode === "full"', js)
        self.assertNotIn('est.mode === "solar"', js)
        self.assertIn("root.dataset.battery = (view.battery && view.battery.state)",
                      js)

    def test_drawer_about_describes_the_read_only_wake(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertNotIn("never publishes", html)
        self.assertIn("realtime wake request", html)


if __name__ == "__main__":
    unittest.main()
