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
  2560×1080; the flat pastel version replaced a detailed night illustration so the artwork
  stops competing with the battery and the cards.
- **Final geometry:** front gable with a receding left side wall, shorter back wall and
  sloped plinth; the solar array stays inset in the left roof plane; the far roof is a narrow
  fascia rather than a second large plane; bushes sit on the plinth. The composition uses
  `width: min(64%, 107 * --u)` (86% in the Skylight portrait pane), centred behind the
  battery so the walls, windows and trees frame the gauge.
- **No account data or secrets** are embedded. If a raster replacement is introduced, add
  AVIF/WebP/JPEG sources and keep this SVG as the fallback.
