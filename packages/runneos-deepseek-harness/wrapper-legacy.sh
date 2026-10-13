#!/usr/bin/env bash
# RunneOS shell for the DeepSeek Harness web UI (dsh).
# Starts the local server on demand, then opens it in a Chrome app window
# in the user's default Chrome profile (user decision 2026-10-11: no
# separate profile). No sandbox/TLS flags are changed.
#
# The dsh web UI is protected by its own access token (printed to the unit
# journal at each start). A browser that visits the token URL once receives
# a pairing cookie that survives server restarts. First launch uses the
# token URL; later launches use the plain URL from the paired profile.
# `--pair` re-runs the token pairing (e.g. after clearing site data).
set -euo pipefail

SERVICE=runneos-deepseek-harness.service
URL=http://127.0.0.1:3080/
PORT=3080
WM_CLASS=chrome-127.0.0.1__-Default
# Marker: the default profile has visited the token URL once (pairing
# cookie survives server restarts).
MARKER_DIR="$HOME/.var/app/com.google.Chrome/config/google-chrome"
MARKER="$MARKER_DIR/.runneos-deepseek-harness-paired"

force_pair=0
if [[ "${1:-}" == "--pair" ]]; then
	force_pair=1
fi

if ! [[ -x "$HOME/.local/opt/dsh/node_modules/.bin/dsh" ]]; then
	notify-send -a "DeepSeek Harness" "dsh не найден в ~/.local/opt/dsh" 2>/dev/null ||
		echo "dsh not found in ~/.local/opt/dsh" >&2
	exit 1
fi

# Launch/focus contract: re-running the entry focuses the existing window.
existing="$(niri msg --json windows 2>/dev/null |
	python3 -c 'import json,sys; ws=json.load(sys.stdin); print(next((str(w["id"]) for w in ws if w.get("app_id")==sys.argv[1]), ""))' "$WM_CLASS" 2>/dev/null || true)"
if [[ -n "$existing" && "$force_pair" != 1 ]]; then
	exec niri msg action focus-window --id "$existing"
fi

if ! systemctl --user is-active --quiet "$SERVICE"; then
	systemctl --user start "$SERVICE"
fi

ready=0
for _ in $(seq 1 150); do
	if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
		exec 3>&- 3<&- 2>/dev/null || true
		ready=1
		break
	fi
	sleep 0.2
done

if [[ "$ready" != 1 ]]; then
	notify-send -a "DeepSeek Harness" "Локальный сервер не запустился: journalctl --user -u $SERVICE" 2>/dev/null ||
		echo "server did not start; see journalctl --user -u $SERVICE" >&2
	exit 1
fi

open_url="$URL"
if [[ -n "${1:-}" && "${1:-}" != "--pair" ]]; then
	open_url="$1"
elif [[ "$force_pair" == 1 || ! -f "$MARKER" ]]; then
	token="$(journalctl --user -u "$SERVICE" --no-pager -n 50 2>/dev/null |
		sed -n 's/.*token=\([^ ]*\).*/\1/p' | tail -1 || true)"
	if [[ -n "$token" ]]; then
		open_url="$URL?token=$token"
	fi
fi

mkdir -p "$MARKER_DIR"
touch "$MARKER"

exec flatpak run com.google.Chrome \
	--app="$open_url" \
	--no-first-run \
	--no-default-browser-check
