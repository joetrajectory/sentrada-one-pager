# Companion-card layout engine

The fourth Sentrada layout engine. Renders the A6 note that ships in the box
beside the artefact (newspaper / crossword / "the email") to a print-ready PNG at
360 DPI. The artefact carries no sender branding by design; this card is where
Sentrada is revealed, so it reads like a personal note from the founder on fine
stationery, not marketing collateral.

Procedural and deterministic, like `email/email.py`: no template image, no AI
image generation. Warm stone paper, one clay slice accent, the recipient's name,
the body copy and the contact block, all typeset by Pango so the text is
guaranteed legible.

## Usage

    # fit check only (no render, non-zero exit on overflow)
    python3.12 card/card.py --data card/samples/marcus.json --check

    # screen preview (1748 x 1240, A6 landscape at 300 DPI)
    python3.12 card/card.py --data card/samples/lesley.json --output lesley.png

    # Birch print master (A6 at 360 DPI, 3 mm bleed + crop marks)
    python3.12 card/card.py --data card/samples/lesley.json \
        --output lesley-card.png --print-dpi 360 --bleed-mm 3 --crop-marks

Runs under **python3.12** (the gobject-introspection bindings in this
environment are built for 3.12). Same CLI contract as the other engines:
`--data`, `--output`, `--check`, `--print-dpi`, `--print-size`, `--render-size`,
plus crossword's `--bleed-mm` / `--crop-marks`.

## Input

A card JSON (`--data`):

    {
      "salutation": "Lesley",
      "body": [
        "First paragraph ...",
        "Second paragraph ...",
        "Third paragraph ..."
      ],
      "contact": {
        "name":  "Joe Chapman",
        "company": "Sentrada",
        "email": "joe@sentrada.io",
        "phone": "+44 7912 345678"
      }
    }

`salutation` may be a bare first name (the comma is added) or include its own
punctuation; `recipient.first_name` is also accepted. `body` is a list of
paragraph strings (3-4 typical, up to 5 short at the 150-word ceiling). Nothing
is hardcoded per recipient. A plain-text card is also accepted via `--text`
(salutation / body / sign-off blocks; the sign-off lines map to name, company,
email, phone in order).

## The auto-fit ladder

The card holds variable length without looking empty or cramped. The engine
measures the laid-out card and applies the first tier that fits the 148 x 105 mm
trim with the bottom safe margin intact:

| Tier | Contact block | Body line-height | Paragraph gap |
| ---- | ------------- | ---------------- | ------------- |
| 1 (default) | 4-line | 1.55 | 11 px |
| 2 | 4-line | 1.46 | 8 px |
| 3 | 2-line compact (`Company · Email · Phone`) | 1.45 | 6 px |
| overflow | copy exceeds the card → flagged failure, trim a sentence | — | — |

Body type stays **9 pt** at every tier (it must stay readable on uncoated
stock); the salutation gap and slice gap tighten slightly as it descends. The
4-line contact comfortably holds ~120 words; the 2-line compact buys up to ~150,
which is the hard content cap. Copy that overflows the tightest tier is **never
squeezed** — the deliverable is withheld and a `*.FAILED.png` is written for
inspection, exactly like the other engines.

## Brand / spec

Built to the design hand-off (`Companion Card.dc.html` + its README). A6
landscape, 148 x 105 mm, 3 mm bleed. Warm Stone `#F2E9E1` paper, Charcoal
`#1B1B1B` ink, a single Lisbon Clay `#C4724E` slice (the only accent). Fraunces
light italic salutation, Inter body and contact. The `one of one / sentrada`
lockup sits bottom-right at 50% opacity. Fonts are bundled (Inter in `../fonts`,
Fraunces in `./fonts`); the wordmark is `assets/wordmark-charcoal.png`.

## samples/

The three design test cards: `lesley` (short, tier 1), `chris` (full, tier 2)
and `marcus` (150-word stress, tier 3 compact), plus rendered `*-card.png`
(print, bleed + crop marks) and `*-preview.png` (trim) for eyeballing.

See `INTEGRATION.md` for wiring into the runner as step 8b.
