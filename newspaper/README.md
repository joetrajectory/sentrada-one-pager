# Newspaper layout engine

Places structured newspaper copy onto a blank newspaper template image and
produces a print-ready A2 newspaper PNG with perfect, real text every time.

This replaces the old approach of generating the entire newspaper (text and all)
as a single AI image, which was unreliable for text accuracy and layout
consistency. Here the template carries only the newsprint texture, column rules
and structural elements; this engine flows typed copy onto it with proper
broadsheet typesetting (justified, hyphenated three-column flow) and composites
the text into the paper so it reads as printed, not pasted on.

## Usage

```bash
python newspaper.py --template template.png --data mmc.json --output mmc_final.png
```

The output PNG is written at the same resolution as the input template.

## Setup

The engine uses Pango/Cairo for text layout (Pillow alone cannot justify and
hyphenate columns well enough), plus Pillow for compositing and pyphen for
hyphenation.

```bash
./setup.sh                      # system libs (Pango, Cairo) + Python deps
# or, if the system libs are already present:
pip install -r requirements.txt
```

The three required Google Fonts (Playfair Display, Lora, Inter) are bundled in
`../fonts` and registered with fontconfig at runtime, so no system font install
is needed. Override the location with the `SENTRADA_FONT_DIR` env var.

## Trying it without the final template

The real blank template is not finalised yet. A stand-in generator is included so
you can run the pipeline today:

```bash
python make_test_template.py --output template.png            # 948x1341
python make_test_template.py --output template_4x.png --scale 4   # 3792x5364
python newspaper.py --template template_4x.png --data mmc.json --output out.png
```

`mmc.json` is real production copy and exercises every field. At the 4x
production resolution the full page renders in roughly eight seconds.

## Print resolution

The engine renders fonts as real glyphs at the template's pixel size, so text
sharpness comes from the render resolution, not from how much the template is
upscaled. The template upscale only adds newsprint texture detail.

For an A2 print run at the standard 300 DPI (4961x7016 px), either:

```bash
# Render directly on a 300 DPI (5x, 4961x7016) template:
python newspaper.py --template template_5x.png --data mmc.json --output print.png --print-dpi 300

# ...or render on a larger master (e.g. 8x) and downsample the FINISHED page to
# an exact 300 DPI, which supersamples the glyph edges and texture:
python newspaper.py --template template_8x.png --data mmc.json --output print.png --print-dpi 300
```

`--print-dpi 300` downsamples the composited page to A2 at that DPI and tags the
PNG. Use `--print-size 4961x7016` to give an explicit pixel target instead.
Rendering at 8x uses ~1.3 GB RAM and about 30 seconds; 229 DPI (4x) is fine for
foam board, 300 DPI (5x+) matches commercial print specs.

Because the text is drawn fresh as glyphs, you do not even need a big template.
`--render-dpi 300` upscales a SMALL template (e.g. the 948x1341 original, under
1 MB) to A2 at 300 DPI before rendering, so the text comes out crisp at full
print size while only the paper grain is interpolated:

```bash
python newspaper.py --template template.png --data mmc.json --output print.png --render-dpi 300
```

This is the easiest route when a large upscaled template is awkward to move
around: keep the template small, let the engine render at print size.

## Calibrating to the real template

Every zone position is a fraction of the image dimensions, so the same config
works at any resolution (pre- or post-upscale). When the final template lands,
open `newspaper.py` and adjust the `zones` block in `CONFIG` near the top:

```python
"zones": {
    # (x, y, w, h) as fractions of the image, origin top left
    "masthead":  (0.045, 0.013, 0.910, 0.070),
    "headline":  (0.045, 0.095, 0.585, 0.110),
    ...
}
```

Everything else (fonts, type sizes as a fraction of height, leading multiples,
gutters, padding, ink blur) is also in `CONFIG` and documented inline.

## JSON schema (the Copy Agent contract)

`mmc.json` is a complete worked example. Every field is a flat top-level string.
This table is the contract between the Copy Agent and the engine: target the word
counts and the engine lays the page out with no manual adjustment. Counts are
guidance, not hard limits (the engine still renders outside them); `--check`
warns when a field is out of range and fails on anything that will not fit.

| Field | Required | Words | Notes |
|-------|----------|-------|-------|
| `masthead_name` | yes | 1–5 | Publication name. Rendered UPPERCASE, sized to span the full page width and fill the khaki band. |
| `edition_line` | yes | 4–16 | Folio strapline. Rendered in spaced capitals, left of the date; auto-shrinks if the folio row would exceed the page width. |
| `date` | yes | 1–4 | Folio date. Rendered in the SAME spaced caps/size as the edition line, right-aligned at the right margin, sharing its baseline. |
| `headline` | yes | 6–16 | Grows to fill the band down to the standfirst rule, two or three balanced lines (no line under 50% width, last under 40%). Over ~16 words it shrinks; far over and it truncates (a check failure). |
| `byline` | yes | 2–6 | Bold, chained directly beneath the headline. |
| `lead_article` | yes | 580–660 (target 620) | Three-column continuous flow. Separate paragraphs with `\n\n`. See below. |
| `pull_quote_text` | yes | 10–30 | Large centred italic feature quote filling the bottom-left block. |
| `pull_quote_attribution` | yes | 2–7 | Sits under the quote. Adjacent proper nouns are kept on one line. |
| `stat_number` | yes | 1 | The dominant figure (e.g. `447`, `$1bn`, `449,933`, `42%`). Sized by ink width to fill the stat box, so multi-digit/comma figures fit. |
| `stat_descriptor` | yes | 3–10 | Caption under the number. Adjacent capitalised words (e.g. "MMC Ventures") stay together. |
| `stat_source` | optional | 2–9 | Small source line under the descriptor. If absent, it is not drawn and the number + descriptor re-centre in the box. |
| `sidebar_1_headline` | yes | 4–11 | First rail story headline (section-headline scale). |
| `sidebar_1_byline` | yes | 2–4 | First rail story byline. |
| `sidebar_1_body` | yes | 60–80 (target) | First rail story body. See rail sizing below. |
| `sidebar_2_*` | optional | as above | Second rail story (headline/byline/body). |
| `sidebar_3_*` | optional | as above | Third rail story. |
| `sidebar_4_*` | optional | as above | Fourth rail story. |
| `kicker_text` | optional | 20–120 | Extra column block beneath the pull quote; only rendered if present. |

### Lead article

Target 620 words; the engine warns under 580 / over 660 and fails if the copy
cannot fit three columns at the minimum body size. Use `\n\n` for paragraph
breaks. The article is set as one continuous broadsheet flow (paragraphs
newline-separated with a first-line indent) and split across the three columns at
line boundaries, breaking mid-paragraph and mid-sentence so all three columns end
level (within one line height). No em dashes (they become a comma break).

### Sidebar stories (1 to 4) and the self-balancing rail

**Production standard: three stories of 60–80 words each.** That fills the rail
at a comfortable body size with tidy gaps. Supply between one and four stories as
`sidebar_N_headline` / `sidebar_N_byline` / `sidebar_N_body`, numbered from 1 with
no gaps. The engine collects them in order and stops at the first missing
`sidebar_N_headline`:

- **Absent `sidebar_2`+** — fewer stories are rendered; the rail distributes
  however many it is given. Only `sidebar_1` is mandatory.
- **All stories share one headline size and one body size** so the rail is
  typographically consistent; the words themselves are never stretched, padded
  or feathered to fill.
- **The rail self-balances vertically.** The stat box is fixed at the top; the
  stories sit below at their natural heights and the surplus down to the
  pull-quote bottom is split evenly into the gaps (one before each story), with a
  divider rule centred in each between-story gap. The last story's foot lands on
  the pull-quote bottom, so spare space reads as editorial spacing, never as a
  hole below the last story.
- **Short copy → gaps grow.** If the gaps would exceed ~8% of page height, the
  body leading is loosened up to 1.15x and the rail re-distributed. Two ~50-word
  stories leave ~12% gaps; three 60–80 word stories bring it to ~2–4%.
- **Long copy → type shrinks to fit.** When several long stories would be taller
  than the rail (which is bounded by the page and cannot grow), the shared body
  size is reduced uniformly to a readability floor so everything fits, down to
  the pull-quote bottom. Only the type size adapts; the copy is never altered.
  The body size is never enlarged past the established reference, so short copy
  is unaffected. If even the floor will not fit, `--check` fails: the fix is
  shorter or fewer stories, never editing the words.

The engine handles ~40–110 words per body (`--check` warns outside that) by these
two mechanisms, but 60–80 × 3 is the sweet spot the Copy Agent should target.

### Optional fields when absent

`stat_source`, `sidebar_2`–`sidebar_4` and `kicker_text` are optional. When
`stat_source` is absent the source line is not drawn and the stat number and
descriptor re-centre in the box. When a `sidebar_N` story is absent the rail
renders fewer stories and redistributes. When `kicker_text` is absent the kicker
block is skipped. No field needs a placeholder value; omit it entirely.

## Pre-render check

Run a validation pass that renders the page (at print resolution) and prints a
PASS/FAIL report without needing to eyeball the output. Use it in the Copy Agent
pipeline so a send self-reports before it goes to print.

```bash
python newspaper.py --template template.png --data company.json --check
```

`--output` is optional with `--check` (nothing is saved unless you also pass it).
The check runs at 300 DPI by default so wrapping and hyphenation match the printed
page. It reports:

- **Word counts** outside the ranges above (warning).
- **Zone overflow** — headline that truncates, lead article that will not fit the
  columns, columns overflowing the zone, a stat block taller than the box.
- **Rail gaps at the cap** — gaps still over ~8% after the leading stretch
  (warning; add a story or lengthen bodies).
- **Structural-rule collisions** — byline touching the standfirst rule, columns
  starting above it, the stat box reaching the folio rule, a sidebar story
  crossing its divider rule, or the last story overflowing the pull-quote bottom.

It exits `0` on pass and `1` if there is any failure, so it can gate a build:

```bash
python newspaper.py --template t.png --data company.json --check \
  && python newspaper.py --template t.png --data company.json --output final.png --render-dpi 300 --print-dpi 300
```

## How the layout works

- **Resolution independent.** The template size is detected and every zone is
  recomputed in pixels from the `CONFIG` fractions. Type sizes are fractions of
  image height. The ink-spread blur is scaled by the upscale factor so the paper
  effect is identical at any size.
- **Masthead** large serif capitals sized by cap height to span the full page
  width and fill the khaki band.
- **Folio row** edition line and date as one continuous row of spaced capitals,
  full width (edition left, date at the right margin) on a shared baseline, with
  a full-width hairline rule beneath separating the folio from all content.
- **Headline** large bold serif that grows to fill the band down to the
  standfirst rule in two or three balanced lines; the bold byline and then the
  columns chain off it adaptively (no fixed gaps). If it cannot fit even at the
  minimum size it is truncated with a warning (a `--check` failure).
- **Lead article** one continuous justified, hyphenated (en_GB) flow split across
  three columns at line boundaries — mid-paragraph and mid-sentence, like a real
  broadsheet — so all three columns end level within one line height.
- **Pull quote** large centred italic serif filling the bottom-left block.
- **Stat box** the dominant figure plus caption, shifted down so the rail clears
  the folio row. The baked tan box is relocated by the engine to match.
- **Rail (1–4 sidebar stories)** self-balancing: natural heights with the surplus
  distributed evenly into the gaps, divider rules centred in each gap, last
  story's foot on the pull-quote bottom. See the schema section above.
- **Structural rules** the inter-sidebar, standfirst and folio rules are
  engine-drawn at content-adaptive positions (the fixed baked ones are inpainted
  out) and matched to the baked rule colour and weight, so they never collide
  with variable-length copy.
- **Logo** `../assets/sentrada-logo.png`, ~3% of image width, bottom right.
  Skipped gracefully with a note if the file is missing.
- **Compositing** all text is drawn on a transparent layer, given a sub-pixel
  Gaussian blur to simulate ink spread, then multiplied onto the template so the
  newsprint grain shows through the ink.

## Copy rules enforced at render time

No em dashes (they are replaced with a comma break). All body and sidebar text is
justified with hyphenation. Text is near-black (#1A1A1A) on the cream template.

Hyphenation is en_GB, left/right 3, minimum 8 **letters**, and never the last
word of a block. The minimum counts letters only, not trailing punctuation, so a
7-letter proper noun followed by a comma ("Reuters,") is not hyphenated — earlier
it was, because the comma pushed the token to 8 characters.

## Ink sharpness

Text is rendered as real glyphs at full output resolution, so it is sharp. A
faint sub-pixel blur is then applied to the ink to simulate it spreading into the
newsprint, capped low (`ink_blur_cap`, default 0.6 px) so text stays crisp at
print resolution. Override per run with `--ink-blur` (e.g. `--ink-blur 0` for
dead-sharp, `--ink-blur 0.3` for a whisper).
