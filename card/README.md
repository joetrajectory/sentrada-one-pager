# Companion-card engine

The fourth Sentrada layout engine. Renders one A6 companion card (the note that
goes in the box with each artefact) to a print-ready PNG at 360 DPI. Procedural
and deterministic, like `email/email.py`: no AI image generation, text is
guaranteed legible because Pango lays it out.

## Usage

    # fit check only (no render, non-zero exit on overflow)
    python3.12 card/card.py --text card/samples/chris-evans.txt --check

    # trim proof (1488 x 2098 px)
    python3.12 card/card.py --text card/samples/lesley-ronaldson.txt \
        --output lesley-card.png

    # Birch print-ready (3mm bleed + crop marks, 1573 x 2183 px)
    python3.12 card/card.py --text card/samples/chris-evans.txt \
        --output chris-card.png --bleed-mm 3 --crop-marks

`--landscape` flips to landscape A6. See `RUNNER_INTEGRATION.md` for step 8b
wiring and dependencies.

## Input format

A plain-text file holding ONE card:

    Chris,

    First body paragraph...

    Second body paragraph...

    Joe Chapman
    Sentrada

Salutation = first block, sign-off = last block (name + organisation), body =
everything between. Blocks are blank-line separated. Nothing is hardcoded.

## Auto-fit

Body type auto-fits between 8.5 and 11.5 pt to absorb length variation. If a
card will not fit A6 even at 8.5 pt, the engine flags the overflow (in mm) and
exits non-zero rather than clipping. Across the 2026-06-25 batch the fitted size
ranged 8.5 pt (Chris) to 10 pt (Lesley).

## Brand

Palette and type are from the live site: Charcoal `#1B1B1B`, Lisbon Clay
`#C4724E` / `#A55E3D`, Warm Stone `#F2E9E1`; Fraunces (display) and Inter (body),
bundled in `fonts/`. The Sentrada Canva template is the source of truth and
should be reconciled against this first cut before the next batch.

## samples/

Two real cards from batch 2026-06-25, rendered short (Lesley) and long (Chris),
in trim and bleed-plus-crop-marks variants, for eyeballing typography and bleed.
