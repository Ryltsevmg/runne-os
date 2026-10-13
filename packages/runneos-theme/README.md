# RunneOS Carbon / Cloud — original Runne identity

The user approved applying the Runne direction on 2026-10-10. The package stores
theme data under `/usr/share/runneos/theme` and provides an explicit
`runneos-theme carbon|cloud` command plus `runneos-appearance` GTK selector.
Installation itself does not rewrite settings. Applying saves private backups,
changes the DMS custom theme through its file watcher, selects a matching original
wallpaper and a validated Ghostty palette fragment. Other Ghostty settings and
key bindings are preserved; no terminal/session is restarted.

`files/tokens.json` is the source of truth. Runne core colors are transcribed
from the inspected `design/runne-ui.pen` at pinned commit
`b5bfb915c978f3b575f74df6ac6540055a0804aa`. Safe text colors are adapted for
contrast. Original Primary Logo, light/dark Route Glyph and canonical idle
mascot are exported from the owner-provided design through Pencil MCP. Asset
node IDs, hashes and derivations are in `files/brand/provenance.json`.
No access cookies/signed URLs, Pen application binaries or third-party font
binaries are bundled. The former hand-made R proposal is no longer used.

The installed wordmark now uses the user-approved **Runne.OS** variants
(`AZziy` Carbon / `Blo6R` Cloud): original Runne lettering, raised small blue
separator and Inter Tight 700 OS suffix. Transparent exports preserve the
approved geometry and theme-specific ink/dot colors; the presentation-board
background is omitted through separate Pen export instances, not color-keying.
These locally revised nodes postdate the archive commit; per-asset provenance
records that distinction. Inter Tight is rasterized by Pen, not installed as
a system font. The compact launcher glyph remains the original Route Glyph.

`files/generate.py --output DIRECTORY` validates and generates:

- CSS semantic variables;
- a two-mode DMS custom theme;
- Ghostty palette-only fragments, without commands/includes/key bindings.

DMS uses accessible primary colors for its foreground/background contract;
brand Cobalt stays a separate token. Runtime import into DMS has been tested.
Carbon and Cloud have been applied to the workstation; Carbon is the final
selection. DMS generates its own GTK/Qt palette. The generated Ghostty fragments
are validated before replacing its theme directive.

`ui_style.py` is the shared native GTK adapter used by Programs, AI, Hub and
Appearance. It follows DMS's dark/light mode and applies the packaged Runne
tokens, not arbitrary third-party theme palettes. Canonical wordmarks select
the matching ink variant; the raster's natural dimensions cannot expand the
sidebar. Controls use Cobalt/white and 6–8px radii, flat account/card surfaces
use 12px, secondary text is opaque and keyboard focus is visible. No global
GTK stylesheet or privileged password dialog is replaced.

Launcher glyph and wallpaper selection track `runneos-theme carbon|cloud`
and the Appearance UI. Direct DMS mode changes outside this entry point are
not yet guaranteed to switch the packaged launcher-image path automatically.

## Review prototype

From the repository root:

```sh
python design/runneos/build-preview.py
xdg-open design/runneos/preview.html
```

The self-contained preview supports Carbon/Cloud, overview/programs/settings,
AI account/detail/connect screens, popup and eight data states. All values are
synthetic and marked DEMO. There is no network access to providers, no key
input, no credential storage and no OS action. The active desktop is unchanged.

Space Grotesk is not installed or qualified for Cyrillic on this machine; the
preview/system body text uses installed Inter (with Noto Sans fallback).
Numbers use JetBrains Mono. The canonical logo is vector geometry exported to
PNG and does not depend on installing Space Grotesk. Full font qualification
and accessibility remain separate acceptance work.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p test_theme.py -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p test_ui_style.py -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p test_ai_model_v2.py -v
```

`tests/design_preview_check.mjs` checks mouse clicks, dialogs/Escape, eight
scenarios, viewport widths 375/640/1024/1440/1920, bounds, JS exceptions and
external requests through a **dedicated local Chromium CDP endpoint**. Never
point it at a personal browser profile. Example using the existing optional
Chrome Flatpak (not a new distribution dependency):

```sh
python -m http.server 8876 --bind 127.0.0.1 --directory design/runneos
# In another terminal, with a fresh temporary profile:
flatpak run --filesystem=/tmp/opencode com.google.Chrome --headless --disable-gpu \
  --no-first-run --user-data-dir=/tmp/opencode/runneos-design-chrome \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9225 about:blank
# Then:
node tests/design_preview_check.mjs
```

Browser screenshots/results are temporary verification artifacts, not live
account data. Theme/AI tests are foundation checks, not real provider or ISO
acceptance.
