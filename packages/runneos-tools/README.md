# RunneOS Hub / Doctor

`runneos-hub` is a native navigation window, not another owner of system settings.
It opens the existing Programs, AI, Appearance, DMS, Bitwarden and Seahorse UIs.
The diagnostic button runs `runneos-doctor --json` off the GTK thread.

`runneos-doctor` checks selected system/user services, failed units, Niri/Ghostty
configuration, pacman consistency, disk space and mouse-button states. Commands
are fixed argv with timeouts; no repairs, sudo, raw journal exports or keyboard
text capture. Inaccessible mouse devices are reported unknown. An active portal
is not proof of a successful screen-share call.

The executable Borg fixture in `tests/check_backup_restore.py` verifies a
disposable encrypted local repository. It is not a configured backup service or
a replacement for off-device storage and a recovery-key plan.
