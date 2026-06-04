#!/usr/bin/env python3
"""
Generate a STAND-IN blank newspaper template that matches the finalised Sentrada
template's structure, for testing the layout engine.

This is scaffolding. The real finalised template (the upscaled newsprint file)
should replace it; newspaper.py works on any blank template of the same
proportions because every zone is a fraction of the image. This generator
reproduces the baked elements the engine relies on so a test render is faithful:

  - warm khaki masthead band (text sits on top of it)
  - tan stat box fill with a border (stat number/descriptor sit on top)
  - two vertical column dividers (engine does NOT draw its own)
  - vertical sidebar boundary
  - baked horizontal rules: under the masthead, under the headline block, under
    the columns, the rule ABOVE the pull quote, the kicker rules, and the
    divider between the two sidebar stories

    python make_test_template.py --output template.png            # 948x1341
    python make_test_template.py --output template_4x.png --scale 4
"""

import argparse

from PIL import Image, ImageChops, ImageDraw, ImageFilter

BASE_W, BASE_H = 948, 1341  # A2 proportion, 1:1.414

CREAM = (238, 233, 221)
KHAKI = (199, 186, 152)
TAN = (203, 188, 158)
RULE = (85, 85, 85)  # charcoal hairline, matches CONFIG lower_rule_default

# Zone fractions (mirrors CONFIG in newspaper.py).
MASTHEAD_Y_END = 0.0567
HEADLINE_RULE_Y = 0.1939     # under the byline / above the columns text
COLUMNS_Y0, COLUMNS_Y1 = 0.1976, 0.8136
COL_DIV_1, COL_DIV_2 = 0.2521, 0.4726
ARTICLE_X0, ARTICLE_X1 = 0.0338, 0.6857
PULLQUOTE_TOP_RULE_Y = 0.8194
PULLQUOTE_Y1 = 0.8717
KICKER_Y0, KICKER_Y1 = 0.8784, 0.9843
SIDEBAR_DIV_X = 0.693
STAT_X0, STAT_X1 = 0.7173, 0.9705
STAT_Y0, STAT_Y1 = 0.0634, 0.2908
SIDEBAR_DIVIDER_Y = 0.4686   # between sidebar_1 (ends 0.4526) and sidebar_2 (0.4847)
SIDEBAR_X0, SIDEBAR_X1 = 0.7068, 0.9757


def paper(width, height):
    base = Image.new("RGB", (width, height), CREAM)
    noise = Image.effect_noise((width, height), 14).convert("L")
    grain = Image.merge("RGB", (noise, noise, noise))
    textured = ImageChops.multiply(base, grain.point(lambda v: 188 + v // 5))
    out = Image.blend(base, textured, 0.30)
    return out.filter(ImageFilter.GaussianBlur(0.4))


def make_template(width, height):
    img = paper(width, height)
    d = ImageDraw.Draw(img, "RGBA")
    hair = max(1, round(height * 0.00065))

    def X(f):
        return round(f * width)

    def Y(f):
        return round(f * height)

    # Warm khaki masthead band.
    d.rectangle([0, 0, width, Y(MASTHEAD_Y_END)], fill=KHAKI)
    d.line([(0, Y(MASTHEAD_Y_END)), (width, Y(MASTHEAD_Y_END))], fill=RULE, width=hair)

    # Tan stat box with border.
    d.rectangle([X(STAT_X0), Y(STAT_Y0), X(STAT_X1), Y(STAT_Y1)], fill=TAN,
                outline=RULE, width=hair)

    # Vertical sidebar boundary.
    d.line([(X(SIDEBAR_DIV_X), Y(MASTHEAD_Y_END)), (X(SIDEBAR_DIV_X), Y(PULLQUOTE_Y1))],
           fill=RULE, width=hair)

    # Horizontal rule under the headline / byline block.
    d.line([(X(ARTICLE_X0), Y(HEADLINE_RULE_Y)), (X(ARTICLE_X1), Y(HEADLINE_RULE_Y))],
           fill=RULE, width=hair)

    # Column dividers within the columns zone.
    for cx in (COL_DIV_1, COL_DIV_2):
        d.line([(X(cx), Y(COLUMNS_Y0)), (X(cx), Y(COLUMNS_Y1))], fill=RULE, width=hair)

    # Rule under the columns / above the pull quote.
    d.line([(X(ARTICLE_X0), Y(PULLQUOTE_TOP_RULE_Y)), (X(ARTICLE_X1), Y(PULLQUOTE_TOP_RULE_Y))],
           fill=RULE, width=hair)

    # Kicker top and bottom rules.
    d.line([(X(ARTICLE_X0), Y(KICKER_Y0)), (X(ARTICLE_X1), Y(KICKER_Y0))], fill=RULE, width=hair)
    d.line([(X(ARTICLE_X0), Y(KICKER_Y1)), (X(ARTICLE_X1), Y(KICKER_Y1))], fill=RULE, width=hair)

    # Divider between the two sidebar stories.
    d.line([(X(SIDEBAR_X0), Y(SIDEBAR_DIVIDER_Y)), (X(SIDEBAR_X1), Y(SIDEBAR_DIVIDER_Y))],
           fill=RULE, width=hair)

    return img


def main():
    ap = argparse.ArgumentParser(description="Generate a stand-in blank template.")
    ap.add_argument("--output", required=True)
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()
    w, h = round(BASE_W * args.scale), round(BASE_H * args.scale)
    make_template(w, h).save(args.output)
    print(f"wrote {args.output} ({w}x{h})")


if __name__ == "__main__":
    main()
