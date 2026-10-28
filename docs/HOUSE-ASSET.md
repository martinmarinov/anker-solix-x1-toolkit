# House artwork

`app/static/assets/house.svg` is a hand-built vector illustration (no AI image, no third-party clip art). It is
used by the Home stage as a transparent `<img>` so it scales at any DPI and stays swappable for a raster later without changing layout.

- **viewBox:** `0 90 1200 590` (cropped top/bottom to remove transparent padding).
- **Palette:** off-white walls, slate roof, deep-blue solar array, warm window glow, dark
  teal trees/ground; it intentionally reuses the dashboard's night palette.
- **Construction:** layered gradients and polygons — ground platform, lawn, trees, side wall,
  front gable wall, two roof planes, solar grid, windows/door, bushes, walkway. No SVG blur
  filters; the only shadow is a CSS `drop-shadow` on the `<img>` in `home.css`.
- **Iteration:** rendered at 1672×941, 1280×800, 1024×600, 800×480, 1024×768, 1376×1550
  and 2560×1080; compared against the supplied mockup, then the roof pitch, array perspective,
  house scale and crop were adjusted.
- **Final geometry:** front gable with a receding left side wall, shorter back wall and
  sloped plinth; the solar array is inset inside the left roof plane; the far roof is a narrow
  fascia rather than a second large plane; bushes sit on the plinth with contact shadows. The final composition uses `width: min(60.5%, 101 * --u)` with the
  house cropped so the roof/solar array read from a distance.
- **No account data or secrets** are embedded. If a raster replacement is introduced, add
  AVIF/WebP/JPEG sources and keep this SVG as the fallback.
