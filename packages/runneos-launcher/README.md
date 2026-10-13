# RunneOS focus-or-launch

`runneos-launch --desktop org.runneos.AI.desktop` focuses an exact matching
Niri window, or delegates cold launch to GioUnix.DesktopAppInfo. The currently
focused match wins, otherwise the most recently focused one. Window titles and
case-insensitive substring guesses are never used as identity.

- `--desktop-file /absolute/path/app.desktop` supports manual installations.
- `--new-window` only invokes an explicitly advertised `new-window` desktop
  action; it does not invent application-specific arguments.
- `--flatpak org.example.App --scope user|system` checks the requested
  installation and passes that scope to `flatpak run`. Existing-window focus
  requires matching `.flatpak-info` scope from its PID. If the process metadata
  cannot be read, the helper requests launch in the selected scope instead of
  claiming it identified that window. Multi-installation/window behavior still
  needs live Flatpak acceptance.
- Per-application runtime flock and a short pending-launch marker reduce duplicate
  requests while the first window maps. This is bounded best-effort desktop
  activation, not a transaction or a promise that every third-party app exposes
  a stable app_id.

Integrated callers: RunneOS Hub and the Programs center's Open action. An explicit
New Window button appears in Programs only when the desktop action exists.
DMS's own launcher, arbitrary application panel pins and existing terminal
hotkeys are **not globally intercepted or rewritten** by this package.

Unit checks: `python -m unittest discover -s tests -p test_launcher.py -v`.
Live check: `python tests/check_launcher_live.py` creates a disposable unique
GTK app/desktop entry; checks cold launch, three repeated opens keeping one
window, explicit second window; closes only its own test windows and restores
the earlier focus. No real app's unsaved windows are closed.
