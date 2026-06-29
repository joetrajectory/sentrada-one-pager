# Wiring card.py into the runner as step 8b

`card/card.py` is the fourth procedural layout engine. It follows the same
house pattern as `email/email.py`: a standalone script that shells out cleanly,
renders a print-ready PNG at 360 DPI, and exposes a `--check` fit-validation
mode plus the crossword-style `--bleed-mm` / `--crop-marks` options.

**The runner session owns `runner/sentrada_runner.py`; this note is the spec,
not a change. I have not touched the runner.**

## Where it slots in

Step 8b (companion card) runs after Prompt 7, alongside the artefact render. It
writes to the same per-piece directory the runner already uses, so ship-check
and the deliverables staging pick it up with no further change:

    runner/pieces/<slug>/<slug>-card.png

## CLI contract

    python3.12 card/card.py \
        --text   runner/pieces/<slug>/<slug>-card.txt \
        --output runner/pieces/<slug>/<slug>-card.png \
        --bleed-mm 3 --crop-marks

- `--text` is a plain-text file holding ONE card: a first-name salutation line,
  then body paragraphs (blank-line separated), then a two-line sign-off
  (`Joe Chapman` / `Sentrada`). The copy-agent (step 8b) already produces this
  shape; write it to `<slug>-card.txt` and pass it straight in. Nothing is
  hardcoded per recipient.
- `--bleed-mm 3 --crop-marks` is the Birch print path (1573 x 2183 px). Drop
  both for a trim-only proof (1488 x 2098 px).
- Exit code is 0 on success, non-zero on overflow or bad input, matching the
  other engines.

## Suggested step 8b shape (mirrors the email step)

    # after the companion-card copy is written to <slug>-card.txt
    card_txt = piece_dir / f"{slug}-card.txt"
    card_png = piece_dir / f"{slug}-card.png"

    # 1. fail fast: does the copy fit A6 before we commit to a render?
    check = subprocess.run(
        ["python3.12", "card/card.py", "--text", str(card_txt), "--check"],
        capture_output=True, text=True)
    if check.returncode != 0:
        # too long for A6 even at the 8.5pt floor -> flag for copy revision,
        # do NOT ship a clipped card. check.stdout names the overflow in mm.
        raise PipelineError(f"Companion card overflows A6:\n{check.stdout}")

    # 2. render the Birch print-ready card
    subprocess.run(
        ["python3.12", "card/card.py",
         "--text", str(card_txt), "--output", str(card_png),
         "--bleed-mm", "3", "--crop-marks"],
        check=True)

Running `--check` first is optional but matches the pipeline's "flag, don't
ship something weak" stance: a card that will not fit A6 is caught before the
render and routed back to the copy step rather than clipped.

## Skipping (per CLAUDE.md step 8b)

Step 8b is skipped when the sender supplied custom card copy. In that case the
runner either renders the sender's copy through this same engine (if it is in
the salutation/body/sign-off shape) or passes their finished card through
untouched. The engine itself does not need to know about the skip.

## Dependencies

The engine renders with Pango/Cairo via PyGObject, same stack as the other
engines, and bundles its own fonts (`card/fonts/`, Fraunces + Inter, OFL) so it
does not depend on the host having them installed.

Because the system gobject-introspection bindings in this environment are built
for CPython 3.12, the engine must run under **python3.12**, not 3.11. Required
system packages (install once in the runner image / SessionStart):

    apt-get install -y gir1.2-pango-1.0 gir1.2-pangocairo-1.0 python3-gi-cairo
    python3.12 -m pip install --break-system-packages pycairo

`fc-cache` is not required; the engine registers `card/fonts/` with fontconfig
at runtime via a generated `FONTCONFIG_FILE`.

## Orientation note

The render defaults to **portrait** A6 (105 x 148 mm), matching the print spec.
A `--landscape` flag flips it to 148 x 105 mm. The Sentrada Canva template is
the source of truth; if it is landscape, pass `--landscape` from the runner (or
flip the default constant). Layout, type and the wordmark in this first cut are
built from the live-site brand system (Fraunces / Inter, the Lisbon palette) and
should be reconciled against the uploaded template before the next batch ships.
