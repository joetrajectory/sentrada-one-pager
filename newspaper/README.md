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

## The JSON input

`mmc.json` shows the full schema. Required fields:

`masthead_name`, `edition_line`, `date`, `headline`, `byline`, `lead_article`,
`pull_quote_text`, `pull_quote_attribution`, `stat_number`, `stat_descriptor`,
`stat_source`, `sidebar_1_headline`, `sidebar_1_byline`, `sidebar_1_body`,
`sidebar_2_headline`, `sidebar_2_byline`, `sidebar_2_body`.

`lead_article` is the body copy, target 620 words. Before placing anything the
engine counts the words and warns (but proceeds) if it is under 580 or over 660.
Use `\n\n` for paragraph breaks; they are preserved and the column balancer
prefers to break columns at paragraph boundaries rather than mid-sentence.

## How the layout works

- **Resolution independent.** The template size is detected and every zone is
  recomputed in pixels from the `CONFIG` fractions. Type sizes are fractions of
  image height. The ink-spread blur is scaled by the upscale factor so the paper
  effect is identical at any size.
- **Masthead** large tracked serif capitals, centred, with edition line (left)
  and date (right) beneath and a rule under it.
- **Headline** large bold serif over the left two-thirds. It shrinks to fit the
  zone, with a minimum-size floor; if it still will not fit it is truncated with
  a warning.
- **Lead article** flows into three equal, justified, hyphenated (en_GB) columns
  with gutters and edge padding. Paragraph breaks become a small vertical gap
  (no first-line indent), broadsheet style. The body size auto-fits so the
  columns fill the zone, and paragraphs are partitioned across the three columns
  to keep them level without ever cutting a paragraph in half.
- **Pull quote** larger centred italic serif spanning the article width, with
  hairline rules drawn above and below (not part of the template image).
- **Stat box, sidebar stories** stacked in the right third, justified and
  hyphenated, with hairline separators.
- **Logo** `../assets/sentrada-logo.png`, ~3% of image width, bottom right.
  Skipped gracefully with a note if the file is missing.
- **Compositing** all text is drawn on a transparent layer, given a sub-pixel
  Gaussian blur to simulate ink spread, then multiplied onto the template so the
  newsprint grain shows through the ink.

## Copy rules enforced at render time

No em dashes (they are replaced with a comma break). All body and sidebar text is
justified with hyphenation. Text is near-black (#1A1A1A) on the cream template.
