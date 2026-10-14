# RunneOS OCR

`runneos-ocr` recognizes text from a selected screen region: `slurp` picks the
area, `grim` captures it, `tesseract` (default `rus+eng`) recognizes it. The
text goes to the Wayland clipboard via `wl-copy` and a short `notify-send`
notification reports the character count; the full text is only in the
clipboard. Cancelling the selection (Esc in slurp) exits quietly with code 0.

- `--lang <langs>` overrides the tesseract languages (default `rus+eng`).
- An empty recognition result notifies «Текст не распознан» and does not
  overwrite the clipboard.
- No shell is used: every external program runs through `subprocess` argv.

Dependencies come from official repositories: `grim`, `slurp`, `tesseract`,
`tesseract-data-rus`, `tesseract-data-eng`, `wl-clipboard`, `libnotify`,
`python`.

Unit checks (no Wayland session required):
`python -m unittest discover -s tests -p test_ocr.py -v`.

A keybinding is reserved by the RunneOS standard but intentionally not
assigned yet; see `.agents/skills/runneos/references/bindings.md`.
