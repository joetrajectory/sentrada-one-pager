# Wiring card.py into the runner as step 8b

`card/card.py` follows the same house contract as the other engines, so it drops
into `runner/sentrada_runner.py` exactly like `run_email_engine`. **The runner
session owns `sentrada_runner.py`; this is the spec, not a change — I have not
touched the runner.**

## Where it slots in

Step 8b (companion card) runs after Prompt 7 writes the card copy, and renders
to the per-piece directory the runner already stages from:

    runner/pieces/<slug>/<slug>-card.png

`ship-check` and the deliverables staging pick it up with no further change (it
is a `<slug>-card.png` next to the artefact PNG).

## The contact block comes from the sender, not the recipient

As with `email_engine_data` (where sender name/email come from `config`, never
the model), the card's contact block is the **sender**: Joe Chapman / Sentrada /
email / phone. Only the salutation and body are per-recipient. So add the
sender's `card_phone` (and reuse `sender_email`) to the runner config.

Current sender values for the config:

    "sender_name":  "Joe Chapman"
    "company":      "Sentrada"
    "sender_email": "joe@sentrada.io"
    "card_phone":   "+44 7815 814855"

## Suggested glue (mirrors the email step)

    def card_engine_data(data, args, sender, config):
        """Assemble card.py's --data from the Prompt 7 companion-card copy plus
        the sender identity. Salutation is the recipient's first name; the
        contact block is always the sender."""
        first = args.name.split()[0] if args.name else ""
        paras = []
        for b in (data.get("body") or data.get("paragraphs") or []):
            paras.append(b if isinstance(b, str) else str(b.get("text", "")))
        return {
            "salutation": first,
            "body": paras,
            "contact": {
                "name":    sender.get("sender_name", "Joe Chapman"),
                "company": sender.get("company", "Sentrada"),
                "email":   sender.get("sender_email", ""),
                "phone":   config.get("card_phone", ""),
            },
        }

    def run_card_engine(engine_path, data_path, output_path=None, check=False):
        """Render or validate the companion card. No --template (procedural).
        Same exit-code contract as the others: --check validates and exits
        nonzero on overflow; --output renders at 360 DPI."""
        cmd = [sys.executable, engine_path, "--data", data_path]
        if check:
            cmd.append("--check")
        if output_path:
            cmd += ["--output", output_path, "--print-dpi", "360",
                    "--bleed-mm", "3", "--crop-marks"]
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr

Then in the step-8b handler, mirroring the email path:

    card_engine = os.path.join(REPO_ROOT, "card", "card.py")
    card_data = folder / f"{slug}-card.json"
    card_png = folder / f"{slug}-card.png"
    write_json(card_data, card_engine_data(p7_copy, args, sender, config))

    # 1. fail fast: does the copy fit A6 before committing to a render?
    rc, out = run_card_engine(card_engine, str(card_data), check=True)
    if rc != 0:
        # overflow even at the 2-line compact tier -> route back to copy for a
        # trim (150-word cap). Do NOT ship a squeezed card; the engine refuses.
        die(f"Companion card overflows A6:\n{out}")

    # 2. render the Birch print-ready card
    rc, out = run_card_engine(card_engine, str(card_data), str(card_png))
    if rc != 0:
        die(out)

## Skipping (per CLAUDE.md step 8b)

Step 8b is skipped when the sender supplied custom card copy. In that case feed
the sender's copy through the same engine (it already takes arbitrary
salutation/body/contact), or pass their finished card through untouched. The
engine does not need to know about the skip.

## Dependencies

Pango/Cairo via PyGObject + Pillow (`card/requirements.txt`), same stack as the
other engines. Must run under **python3.12** here (system GI bindings are built
for 3.12). System packages, once per image / SessionStart:

    apt-get install -y gir1.2-pango-1.0 gir1.2-pangocairo-1.0 python3-gi-cairo
    python3.12 -m pip install --break-system-packages pycairo Pillow

Fonts and the wordmark are bundled (Inter in `../fonts`, Fraunces in
`card/fonts`, `card/assets/wordmark-charcoal.png`), so no `fc-cache` or system
font install is needed.
