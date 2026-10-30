# House artwork

`app/static/assets/house.svg` is a hand-built vector illustration (no AI image, no third-party clip art). It is
used by the Home stage as a transparent `<img>` so it scales at any DPI and stays swappable for a raster later without changing layout.

- **viewBox:** `0 90 1200 590` (cropped top/bottom to remove transparent padding).
- **Palette:** cream walls, pale blue roof, soft blue solar array, warm windows and sage
  trees/lawn. The pastel set is deliberately low-contrast: the house sits behind the battery
  and is scenery, not a data element.
- **Construction:** a simplified set of flat shapes and a handful of soft gradients —
  garden platform, two trees, side wall, front gable, two roof planes, solar grid, one warm
  window, door, patio glass, bushes and path. No SVG blur filters or glow layers; the only
  shadow is a CSS `drop-shadow` on the `<img>` in `home.css`.
- **Iteration:** rendered at 1672×941, 1280×800, 1024×768, 806×1008 (Skylight pane) and
  2560×1080. The daylight version stays the default; a colour-only night counterpart was
  added for the evening crossfade without reintroducing a competing illustration.
- **Final geometry:** front gable with a receding left side wall, shorter back wall and
  sloped plinth; the solar array stays inset in the left roof plane; the far roof is a narrow
  fascia rather than a second large plane; bushes sit on the plinth. The composition uses
  `width: min(64%, 107 * --u)` (86% in the Skylight portrait pane), centred behind the
  battery so the walls, windows and trees frame the gauge.
- **No account data or secrets** are embedded. If a raster replacement is introduced, add
  AVIF/WebP/JPEG sources and keep this SVG as the fallback.

## Night counterpart

`house-night.svg` is the same file with the title and colour attributes changed. Every path,
coordinate, opacity and gradient id is identical; `test_static_runtime.py` strips the colours
and title and asserts the two documents are byte-identical. That is what lets the Home stage
stack the two images and crossfade them without the walls moving or the drop shadow doubling
into a visible offset.

| Element | Day | Night |
|---|---|---|
| front / side wall | `#fffdf7 → #f5e9d8` / `#f0e1ce → #e2ceb5` | `#dfe9ea → #c8d6d9` / `#c3d2d5 → #aebfc4` |
| roof (left / right) | `#c5d9e2 → #aac4d1` / `#b3cad6 → #96b2c1` | `#5f7f93 → #4c6b80` / `#547487 → #436175` |
| solar array | `#a7cde6 → #7fadcb` | `#6e93ad → #507593` |
| windows | `#fdf0c6 → #f7dda0` | `#ffe6ad → #ffc978` |
| patio glass | `#dceef6 → #bcdbe8` | `#f7dca6 → #e5b877` |
| lawn / trees | `#e3ecd3 → #cfdec0` / `#b8d2ae` | `#8fa78a → #a7b99a` / `#6f8b69` |

The night house keeps the warm windows and warms the patio glass so the interior reads as lit;
the walls are cool moonlit cream, the roof and solar array are desaturated slate blue, and the
lawn/trees are a darker sage. The CSS shadow changes from warm `rgba(92, 75, 48, .14)` to cool
`rgba(4, 12, 24, .30)`. The garden platform is intentionally a shade darker than the scene
hills, so the house still sits in its own pool of moonlight.

The surrounding sky lives in `app/static/index.html`. A shared cloud library feeds two
parallax banks in both scene layers: far wisps move on a 300 s track, the larger near clouds on
a 180 s track, and each track loops by exactly one 1600-unit viewBox. The night layer reuses
the day cloud banks and hill paths exactly, adds stars, swaps the warm sun glow for `#cfe2ff`
moonlight, and uses sky `#192a44 → #263b55 → #425868` over hills `#a7b8a1 → #c5d2bd` (far) and
`#b3c3a8 → #93a78d` (near). Day clouds are paper white; night clouds use `#3e536b`. The hill
band is deliberately kept moonlit rather than near-black: the battery caption is unchanged
page ink and sits over that band, so both hill gradients clear 4.5:1 contrast (enforced by
`test_night_hills_keep_the_ink_caption_legible`).
