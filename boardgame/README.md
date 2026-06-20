# Board game layout engine

Places structured, company-specific copy onto a blank board game template and
produces a print-ready A2 board with perfect, real text on every segment of the
path.

The fourth production format after the newspaper and crossword engines, and a
direct sibling of both: same Pango/Cairo text layout, same resolution-independent
geometry (every position is a fraction of the template, so one map works at any
DPI), same fontconfig font registration, sRGB ICC tagging, print-production flags
and fail-fast safety guards.

The template carries only the artwork: a winding S-curve of ~30 coloured card
segments on a dark charcoal ground, with a moulded arrow on the start cap and a
laurel-trophy on the finish cap. This engine detects the segments, then flows
typed copy onto them, the title into the central void, and the START / FINISH
labels and Sentrada credit into place.

## Usage

Detection runs **once per template** and is cached to a JSON map; rendering reads
the map.

```bash
# 1) Detect the path segments (writes segment_map.json + a numbered overlay)
python boardgame.py detect --template boardgame_template.png --output segment_map.json

# 2) Render a company's copy onto the board
python boardgame.py --template boardgame_template.png \
    --data test_qflow.json --segments segment_map.json --output qflow_boardgame.png
```

Always eyeball `segment_map_overlay.png` after detection: it draws each segment's
band, usable rectangle and index in path order, plus the title zone, so a
mis-detection is caught before any copy is placed.

## Setup

The render path uses the same stack as the newspaper engine (Pango/Cairo for
text, Pillow + NumPy for compositing). Detection additionally needs OpenCV
(contrib, for `cv2.ximgproc.thinning`).

```bash
# system libs (Pango, Cairo, fontconfig, gobject-introspection): see
# ../newspaper/setup.sh, then:
pip install -r requirements.txt
```

Fraunces (the hero display serif) and Inter are bundled in `./fonts` and
registered with fontconfig at runtime, with the shared `../fonts` as a fallback,
so no system font install is needed. Override with `SENTRADA_BOARDGAME_FONT_DIR`.

## How detection works

The cards are divided by thin embossed grooves, not background-coloured gaps, so a
plain colour threshold fills the whole snake into one blob. Detection therefore:

1. **Threshold** the coloured cards off the charcoal ground (distance from the
   sampled background, or saturation + brightness) and solidify them into one
   snake.
2. **Thin** the snake to a 1px centreline and order it from the bottom-left start
   cap to the top-right finish cap by a breadth-first walk between the two tips.
3. **Find the dividers** as straight lines spanning the snake width (Canny +
   Hough, rejecting the card grain and the snake's own edge), project them onto
   the centreline and cluster them into cuts.
4. **Recover** any divider the Hough missed by the median card length (gap fill),
   protecting the longer end caps so they stay single segments.
5. **Build geometry** for each segment from the centreline and the local width: a
   centroid, a band polygon, a usable text rectangle and a path-direction angle.

Everything is stored as fractions of the image, so the map survives the 8x
upscale before print. Re-run `detect` only when the template artwork changes.

The detection parameters all live in `CONFIG` (the `detect_*` keys) and are
documented inline; thresholds are in image/width-relative units so they hold
across resolutions.

## JSON schema (the Copy Agent / P4 contract)

`test_qflow.json` is a complete worked example.

```json
{
  "company_name": "Qflow",
  "title": "THE QFLOW GAME",
  "subtitle": "Navigate the chaos. Try not to lose your mind.",
  "segments": [
    { "index": 1, "type": "empty",       "text": "" },
    { "index": 2, "type": "content",     "text": "Seed round closes. Move ahead 2." },
    { "index": 4, "type": "instruction", "text": "Roll again." }
  ]
}
```

| Field | Notes |
|-------|-------|
| `company_name` | Sets the centre line of the title (`THE` / `COMPANY` / `GAME`). Rendered uppercase. |
| `title` | Optional override; `THE X GAME` is parsed back to the company line if `company_name` is absent. |
| `subtitle` | Strapline under the title. Defaults to "Navigate the chaos. Try not to lose your mind." |
| `segments[]` | One entry per path segment, keyed by `index` (1..N, matching the segment map). |
| `segments[].type` | `content` (2-3 lines of company copy), `instruction` (a one-liner like "Roll again.") or `empty` (number only). |
| `segments[].text` | The copy. Empty string for `empty` segments. |

Target mix for a ~30-segment board: roughly 15-18 `content`, 5-6 `instruction`,
the rest `empty`. Segment 1 (start cap) and segment N (finish cap) are best left
`empty` so the START / FINISH labels own them.

**Copy is immutable.** The engine never truncates or squeezes text to fit. Each
segment's copy is auto-sized to its card; on a tight or curved card the type
simply drops to a smaller size in range. If a line will not fit even at the
minimum size, that segment is left blank (number only) and the run is recorded as
a `--check` failure for P4 to shorten the line, never edited by the engine.

House copy rules (British English, no em dashes, no exclamation marks) apply.
Punctuation is normalised to typographic forms at render time.

## What the engine draws

- **Segment copy** in warm cream (#F2E9E1), rotated to the local path direction so
  it reads along the direction of travel, auto-sized to the card.
- **Segment numbers** 1..N, small light-gold, in the top-left of each card.
- **Title** `THE` / `[COMPANY]` / `GAME` in bold Fraunces with the italic
  strapline beneath, gold, centred in the largest open dark space between the path
  curves (the title zone is found during detection).
- **START / FINISH** small gold caps beside the moulded arrow and laurel-trophy
  emblems on the end caps.
- **Sentrada credit** lowercase, letterspaced, gold, in the bottom-right corner.

## Compositing: light ink on dark stock

A literal multiply blend (correct for the newspaper's dark ink on cream paper)
would crush light ink on this dark ground. Instead the ink is modulated by a
**high-pass of the card luminance** (the local grain and the moulded 3D shadows,
normalised to ~1.0) and composited normally, so the light ink keeps its
brightness while the stock texture still shows through it. The result reads as
printed into the card, not pasted on top. A faint, resolution-scaled ink-spread
blur (capped low) finishes the effect; override per run with `--ink-blur` (0 for
dead sharp).

## Print resolution

Text is drawn as real glyphs at the output resolution, so sharpness comes from the
render size, not the template. Render a small template straight to print size:

```bash
python boardgame.py --template boardgame_template.png --data test_qflow.json \
    --segments segment_map.json --output print.png --render-dpi 360 --print-dpi 360
```

`--render-dpi` upscales the template to A2 (landscape) before rendering so the
text is crisp at full size; `--print-dpi` downsamples the finished board to an
exact A2 at that DPI and tags the PNG. Every saved file carries an sRGB ICC
profile. Bleed and crop marks are off by default (`--bleed-mm`, `--crop-marks`),
matching the newspaper engine; Birch Print handles trim.

## Pre-render check

```bash
python boardgame.py --template boardgame_template.png --data company.json \
    --segments segment_map.json --check
```

Runs at print resolution and prints a PASS/FAIL report: a segment/data count
mismatch, any copy that overflows its card at the minimum size (a hard failure;
shorten the line), and a flag for a positive event paired with a backward move (a
possible metric inversion for P4 to confirm). Exits non-zero on any failure, so it
can gate a build. On failure during a normal render the board is written to a
`*.FAILED.png` path and the deliverable is withheld.

## Safety guards

- **Output-on-fail guard.** Any `fail` withholds the deliverable and writes a
  `*.FAILED.png` for inspection; the process exits non-zero.
- **Font resolution.** Every configured font must resolve to its real face, or the
  run aborts rather than rendering in a substitute.
- **Immutable copy.** Text is never truncated to fit; overflow is a flagged
  failure for P4, not a silent squeeze.
