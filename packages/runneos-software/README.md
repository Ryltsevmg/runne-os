# Программы RunneOS

## Shared operation engine (0.1.1)

GUI installs/removals/updates and `runneos-update --apply` use the same
`OperationRunner` and validated plans. `runneos-update` defaults to `--plan`;
`--check` only checks sources; `--status` reads the last operation. There is no
implicit upgrade or snapshot bypass flag in the CLI.

The runner uses a per-user interprocess flock and a private, fsynced atomic
status journal at `$XDG_STATE_HOME/runneos/software/operation.json` (normally
`~/.local/state/runneos/software/operation.json`). It records step counts,
executable basename, PID, exit status and snapshot number; it does not persist
raw stdout, arguments, credentials or environment. Native package-manager
system-wide locks still govern transactions started outside RunneOS or by other
users. This is a last-operation journal, not a complete historical audit log.

A successful snapshot command must return exactly one positive snapshot number
before the package update can start. Failure stops the remaining sequence.
CLI SIGINT/SIGTERM requests stop **after the current step**, without forwarding
the signal to its separate process group. A lost GUI output callback does not
abort the transaction. Hard crashes/power loss cannot be made safe by flock:
an unfinished journal blocks replay and requires diagnosis/reconciliation.

For unfinished operations, inspect `runneos-update --status`, active package
manager processes, native locks and `/var/log/pacman.log` before recovery. Do
not delete pacman's lock while a transaction is running and do not repeat an
uncertain AUR/Flatpak step blindly. After manual reconciliation, preserve the
old journal in a private backup and explicitly clear/archive it before starting
a new operation. Automated reconciliation/resume and boot rollback are not yet
implemented; an interrupted result is never labelled success.

Native GTK4/libadwaita software center. Launch `runneos-software` or search «Программы RunneOS» in DMS. Catalog / Installed / Updates; Arch AppStream, Flatpak catalogs, on-demand AUR, user local desktop apps and npm tools. Colours follow DMS's generated palette.

Dependencies are declared in PKGBUILD. `yay` is built from its reviewed official AUR recipe; do not enable third-party distribution repositories. Build/install with makepkg and pacman. ISO integration remains separate.

Operations use argv arrays and polkit, never run the GUI as root. AUR builds run as the user; the privilege adapter accepts only pacman invocations. One transaction at a time, exit failures stop the sequence. Arch uninstall uses `-R`, preserving configuration and avoiding recursive dependency deletion. Flatpak uninstall omits `--delete-data`. Essential environment packages are protected.

Updates create a root Snapper snapshot with the `number` cleanup algorithm before full Arch/AUR/Flatpak updates. A root Snapper config must exist; failures stop updates. Configure `NUMBER_CLEANUP` and enable `snapper-cleanup.timer` separately: the GUI does not silently change system policy. Root snapshots do not include separate /boot or /home: no whole-machine recovery guarantee. Updates checks use checkupdates, not pacman -Sy. Empty `yay -Qua` exit 1 means no AUR updates; diagnostic output still counts as an error. The center does not migrate or automatically uninstall arbitrary manual installations or npm tools; those remain visible with instructions. GUI AUR install has a community-source confirmation and links to the recipe.

Run `runneos-software-pin` in an active DMS session to add the packaged launcher widget to the first enabled bar. It preserves other widgets/plugins, is repeatable, waits for asynchronous plugin discovery, and saves settings backups under `~/.local/state/runneos/backups/software-panel-*`. No DMS restart or changes to keyboard shortcuts are needed. For a new user this remains an explicit setup step, not an automatic ISO installer action.

`runneos-software --page installed|updates|catalog` switches the existing instance. `--status` prints its state without presenting the window. Invalid page arguments return an error.

Tests: `python3 -m unittest discover -s tests -p test_software.py -v` from repository root. `python3 tests/test_software_gui.py -v` runs real GTK/CLI tests in a private D-Bus/runtime session; isolated portals may warn about unavailable Mutter, PipeWire or RealtimeKit. `runneos-software --smoke-test` loads real catalogs, presents GTK and verifies mapped search, adaptive grids and Installed rendering. Load/layout errors and timeouts return nonzero; `--smoke-timeout 0` exercises failure. No system upgrade is run as a smoke test.

Validated machine/package results and remaining limitations: `tests/results-software-2026-10-10.md` in the repository. Graphical polkit authorization, fresh-user setup, real AUR builds and full recovery still need acceptance testing.
