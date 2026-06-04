#!/usr/bin/env python3
"""
Sentrada newspaper layout engine.

Places structured newspaper copy onto a blank newspaper template image and
produces a print-ready PNG with real, perfect text every time.

    python newspaper.py --template template.png --data mmc.json --output mmc_final.png

The template is just newsprint texture, column rules and structural elements with
no text. This script flows typed copy onto it. All zone positions are defined as
percentages of the template dimensions (see CONFIG below), so the same config
works at any resolution. Detect the template size, recalculate every zone in
pixels, lay the text out with Pango/Cairo (proper justification + hyphenation),
then composite the text into the paper with a multiply blend so the newsprint
grain shows through the ink.

Dependencies (see requirements.txt and README.md):
    pycairo, PyGObject (Pango + PangoCairo), Pillow, pyphen
    system libs: libpango-1.0, libpangocairo-1.0, libcairo2, fontconfig
    fonts: Playfair Display, Lora, Inter (bundled in ../fonts, registered at runtime)
"""

import argparse
import ctypes
import io
import json
import os
import sys

# ---------------------------------------------------------------------------
# Font registration. We register the bundled ./fonts directory with the running
# fontconfig instance BEFORE Pango is used, so the script is self contained and
# does not depend on the fonts being installed system wide.
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.environ.get(
    "SENTRADA_FONT_DIR", os.path.normpath(os.path.join(SCRIPT_DIR, "..", "fonts"))
)


def register_app_fonts(font_dir):
    """Add font_dir to the current fontconfig config so Pango can resolve the
    bundled families without a system install."""
    if not os.path.isdir(font_dir):
        print(f"[warn] font directory not found: {font_dir} (relying on system fonts)")
        return
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
    except OSError:
        try:
            fc = ctypes.CDLL("libfontconfig.so")
        except OSError:
            print("[warn] libfontconfig not loadable; relying on system fonts")
            return
    fc.FcConfigGetCurrent.restype = ctypes.c_void_p
    fc.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fc.FcConfigAppFontAddDir.restype = ctypes.c_int
    ok = fc.FcConfigAppFontAddDir(fc.FcConfigGetCurrent(), font_dir.encode("utf-8"))
    if not ok:
        print(f"[warn] could not register app fonts from {font_dir}")


register_app_fonts(FONT_DIR)

import gi  # noqa: E402

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

import cairo  # noqa: E402
import pyphen  # noqa: E402
from PIL import Image, ImageChops, ImageFilter  # noqa: E402

SCALE = Pango.SCALE

# ---------------------------------------------------------------------------
# CONFIG. Everything that controls the look of the page lives here. Positions are
# fractions of the template width/height so the layout is resolution independent.
# Calibrate the `zones` once the final template lands: each zone is
# (x, y, w, h) as a fraction of the image (origin top left).
# ---------------------------------------------------------------------------

CONFIG = {
    # Colours -----------------------------------------------------------------
    "ink": (0x1A / 255, 0x1A / 255, 0x1A / 255),  # near black, #1A1A1A
    "rule_grey": (0x99 / 255, 0x99 / 255, 0x99 / 255),  # hairline rules, #999999
    # Fonts. Pango font description strings (family + style/weight). --------
    "font_masthead": "Playfair Display Bold",
    "font_headline": "Playfair Display Bold",
    "font_body": "Lora",
    "font_sans": "Inter",
    "font_pullquote": "Playfair Display Italic",
    "font_stat": "Playfair Display Bold",
    "font_sidebar_head": "Playfair Display Bold",
    # Type sizes, as a fraction of image HEIGHT (auto scales with resolution) --
    "size_masthead": 0.045,  # starting size, shrinks to fit width
    "size_masthead_min": 0.028,
    "size_edition": 0.0105,
    "size_headline": 0.0300,  # starting size, shrinks to fit box
    "size_headline_min": 0.0175,
    "size_byline": 0.0120,
    "size_body": 0.0125,  # starting size, shrinks so 3 columns balance and fit
    "size_body_min": 0.0064,
    "size_pullquote": 0.0205,
    "size_pullquote_attrib": 0.0110,
    "size_stat_number": 0.0560,
    "size_stat_descriptor": 0.0140,
    "size_stat_source": 0.0092,
    "size_sidebar_head": 0.0165,
    "size_sidebar_byline": 0.0105,
    "size_sidebar_body": 0.0118,
    # Leading (baseline to baseline) as a multiple of the font size -----------
    "lead_headline": 1.10,
    "lead_body": 1.20,
    "lead_pullquote": 1.30,
    "lead_sidebar": 1.20,
    "lead_masthead": 1.05,
    # Masthead tracking (letter spacing) as a fraction of the masthead size ---
    "masthead_tracking": 0.10,
    # Article column geometry -------------------------------------------------
    "gutter_frac": 0.020,  # gutter between columns, fraction of image width
    "pad_x_frac": 0.015,  # text padding from zone L/R edges, fraction of width
    "pad_y_frac": 0.010,  # text padding from zone T/B edges, fraction of height
    "para_gap_frac": 0.5,  # inter paragraph gap, as a fraction of body leading
    # Rules / dividers --------------------------------------------------------
    "hairline_frac": 0.00055,  # rule thickness as a fraction of image height (>=1px)
    "draw_dividers": True,  # masthead rule + vertical rule between body and sidebar
    # Logo --------------------------------------------------------------------
    "logo_path": os.path.normpath(os.path.join(SCRIPT_DIR, "..", "assets", "sentrada-logo.png")),
    "logo_width_frac": 0.030,  # logo width as a fraction of image width
    "logo_margin_frac": 0.030,  # margin from the bottom right corner, fraction of width
    # Ink / paper realism -----------------------------------------------------
    "text_blur_px": 0.4,  # base Gaussian blur to simulate ink spread on newsprint
    "blur_reference_width": 948,  # blur is scaled by image_width / this, so the
    # ink spread looks the same at any upscale factor
    "hyphenation_lang": "en_GB",
    # Validation --------------------------------------------------------------
    "lead_words_min": 580,
    "lead_words_max": 660,
    # Zones: (x, y, w, h) as fractions of the image. Origin top left. ---------
    # These are starting calibration values; adjust once the template is final.
    "zones": {
        "masthead": (0.045, 0.013, 0.910, 0.070),
        "headline": (0.045, 0.095, 0.585, 0.110),
        "byline": (0.045, 0.208, 0.585, 0.024),
        "article": (0.045, 0.250, 0.585, 0.495),
        "pullquote": (0.045, 0.752, 0.585, 0.078),
        "statbox": (0.655, 0.095, 0.300, 0.150),
        "sidebar": (0.655, 0.255, 0.300, 0.490),
    },
}

# ---------------------------------------------------------------------------
# Small text utilities.
# ---------------------------------------------------------------------------

_HYPHENATOR = None


def get_hyphenator(lang):
    global _HYPHENATOR
    if _HYPHENATOR is None:
        _HYPHENATOR = pyphen.Pyphen(lang=lang)
    return _HYPHENATOR


def sanitise(text):
    """Apply the rendering copy rules. No em dashes anywhere in the output."""
    if text is None:
        return ""
    # Replace em dash (and the rarer figure/quotation dashes) with a comma break,
    # and any double hyphen used as an em dash substitute.
    for ch in ("—", "―", "‒", "⸺", "⸻"):
        text = text.replace(" " + ch + " ", ", ").replace(ch, ", ")
    text = text.replace(" -- ", ", ").replace("--", ", ")
    return text


def soft_hyphenate(text, lang):
    """Insert U+00AD soft hyphens at valid break points so Pango can hyphenate.
    Pango renders a hyphen at any soft hyphen it breaks on (insert-hyphens)."""
    hyph = get_hyphenator(lang)
    out_tokens = []
    for token in text.split(" "):
        # Pyphen leaves tokens with digits/symbols/short words untouched.
        out_tokens.append(hyph.inserted(token, hyphen="­") if token else token)
    return " ".join(out_tokens)


def word_count(text):
    return len(text.split())


# ---------------------------------------------------------------------------
# Pango helpers. Every layout draws onto the shared transparent text surface.
# ---------------------------------------------------------------------------


def _whole_range(attr, text):
    attr.start_index = 0
    attr.end_index = len(text.encode("utf-8"))
    return attr


def make_layout(cr):
    layout = PangoCairo.create_layout(cr)
    return layout


def set_font(layout, font_str, size_px):
    fd = Pango.FontDescription.from_string(font_str)
    fd.set_absolute_size(size_px * SCALE)
    layout.set_font_description(fd)
    return fd


def build_attrs(text, *, leading_px=None, letter_spacing_px=None,
                language=None, insert_hyphens=None):
    attrs = Pango.AttrList()
    if leading_px is not None:
        attrs.insert(_whole_range(
            Pango.attr_line_height_new_absolute(int(round(leading_px * SCALE))), text))
    if letter_spacing_px is not None:
        attrs.insert(_whole_range(
            Pango.attr_letter_spacing_new(int(round(letter_spacing_px * SCALE))), text))
    if language is not None:
        attrs.insert(_whole_range(
            Pango.attr_language_new(Pango.Language.from_string(language)), text))
    if insert_hyphens is not None:
        attrs.insert(_whole_range(
            Pango.attr_insert_hyphens_new(insert_hyphens), text))
    return attrs


def draw_layout(cr, layout, x, y, ink):
    cr.save()
    cr.set_source_rgb(*ink)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def measure(layout):
    w, h = layout.get_pixel_size()
    return w, h


# ---------------------------------------------------------------------------
# Rules (hairlines). Drawn onto the same text surface so they get the same paper
# treatment as the ink.
# ---------------------------------------------------------------------------


def hrule(cr, x1, x2, y, weight, colour):
    cr.save()
    cr.set_source_rgb(*colour)
    cr.set_line_width(max(1.0, weight))
    cr.move_to(x1, y)
    cr.line_to(x2, y)
    cr.stroke()
    cr.restore()


def vrule(cr, x, y1, y2, weight, colour):
    cr.save()
    cr.set_source_rgb(*colour)
    cr.set_line_width(max(1.0, weight))
    cr.move_to(x, y1)
    cr.line_to(x, y2)
    cr.stroke()
    cr.restore()


# ---------------------------------------------------------------------------
# Element renderers.
# ---------------------------------------------------------------------------


def render_masthead(cr, ctx, data):
    W, H, cfg = ctx["W"], ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["masthead"]

    # Masthead name: large serif capitals, centred, generous tracking. Shrink to
    # fit the content width if necessary.
    name = sanitise(data["masthead_name"]).upper()
    start = cfg["size_masthead"] * H
    floor = cfg["size_masthead_min"] * H
    size = start
    layout = make_layout(cr)
    while size >= floor:
        set_font(layout, cfg["font_masthead"], size)
        tracking = cfg["masthead_tracking"] * size
        layout.set_attributes(build_attrs(name, letter_spacing_px=tracking))
        layout.set_width(-1)
        layout.set_text(name, -1)
        tw, th = measure(layout)
        if tw <= zw:
            break
        size -= max(1.0, H * 0.001)
    name_x = zx + (zw - tw) / 2.0
    name_y = zy
    draw_layout(cr, layout, name_x, name_y, ink)
    name_bottom = name_y + th

    # Edition line (left) and date (right) on a single strip beneath the name.
    sub_size = cfg["size_edition"] * H
    line_y = name_bottom + H * 0.006

    edition = sanitise(data["edition_line"])
    el = make_layout(cr)
    set_font(el, cfg["font_sans"], sub_size)
    el.set_width(-1)
    el.set_text(edition, -1)
    draw_layout(cr, el, zx, line_y, ink)

    date = sanitise(data["date"])
    dl = make_layout(cr)
    set_font(dl, cfg["font_sans"], sub_size)
    dl.set_width(-1)
    dl.set_text(date, -1)
    dw, dh = measure(dl)
    draw_layout(cr, dl, zx + zw - dw, line_y, ink)

    # Rule under the masthead.
    if cfg["draw_dividers"]:
        rule_y = zy + zh
        hrule(cr, zx, zx + zw, rule_y, ctx["hairline"] * 1.6, ink)


def render_headline(cr, ctx, data):
    W, H, cfg = ctx["W"], ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["headline"]

    text = sanitise(data["headline"])
    start = cfg["size_headline"] * H
    floor = cfg["size_headline_min"] * H
    lead = cfg["lead_headline"]

    layout = make_layout(cr)
    layout.set_width(int(zw * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD)
    layout.set_alignment(Pango.Alignment.LEFT)

    size = start
    chosen = None
    while size >= floor:
        set_font(layout, cfg["font_headline"], size)
        layout.set_attributes(build_attrs(text, leading_px=lead * size))
        layout.set_text(text, -1)
        tw, th = measure(layout)
        if th <= zh:
            chosen = size
            break
        size -= max(1.0, H * 0.0008)

    if chosen is None:
        # Still does not fit at the floor: ellipsize to the zone height and warn.
        size = floor
        set_font(layout, cfg["font_headline"], size)
        layout.set_attributes(build_attrs(text, leading_px=lead * size))
        layout.set_text(text, -1)
        layout.set_height(int(zh * SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        print("[warn] headline does not fit at minimum size; truncating.")

    draw_layout(cr, layout, zx, zy, ink)


def render_byline(cr, ctx, data, key, zone_key):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"][zone_key]
    text = sanitise(data[key])
    layout = make_layout(cr)
    set_font(layout, cfg["font_sans"], cfg["size_byline"] * H)
    layout.set_width(int(zw * SCALE))
    layout.set_text(text, -1)
    draw_layout(cr, layout, zx, zy, ink)


# ----- Lead article: three balanced, justified, hyphenated columns -----------


def measure_paragraph(cr, text, font_str, size_px, leading_px, col_w, lang):
    """Height in pixels of a single justified paragraph at the given column width."""
    layout = make_layout(cr)
    set_font(layout, font_str, size_px)
    layout.set_width(int(col_w * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_justify(True)
    layout.set_attributes(build_attrs(
        text, leading_px=leading_px, language=lang, insert_hyphens=True))
    layout.set_text(text, -1)
    return layout, measure(layout)[1]


def _column_height(heights, gap, group):
    if not group:
        return 0.0
    return sum(heights[i] for i in group) + gap * (len(group) - 1)


def balance_columns(heights, gap):
    """Split paragraphs (in order) across three columns so the columns end at
    approximately the same vertical position. Splits happen only at paragraph
    boundaries, never mid paragraph. We pick the two break points that minimise
    the tallest column (tie broken by the smallest spread), which keeps the
    three columns level without ever cutting a paragraph in half."""
    n = len(heights)
    if n == 0:
        return [[], [], []]
    if n <= 3:
        # One paragraph per column, in order; trailing columns may be empty.
        return ([[i] for i in range(n)] + [[], [], []])[:3]

    best = None  # (max_height, spread, groups)
    # b1 = last paragraph of column 1, b2 = last paragraph of column 2.
    # Ranges guarantee all three columns receive at least one paragraph.
    for b1 in range(0, n - 2):
        for b2 in range(b1 + 1, n - 1):
            groups = [list(range(0, b1 + 1)),
                      list(range(b1 + 1, b2 + 1)),
                      list(range(b2 + 1, n))]
            ch = [_column_height(heights, gap, g) for g in groups]
            key = (max(ch), max(ch) - min(ch))
            if best is None or key < best[0]:
                best = (key, groups)
    return best[1]


def render_article(cr, ctx, data):
    W, H, cfg = ctx["W"], ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    lang = cfg["hyphenation_lang"]
    zx, zy, zw, zh = ctx["px"]["article"]

    pad_x = cfg["pad_x_frac"] * W
    pad_y = cfg["pad_y_frac"] * H
    gutter = cfg["gutter_frac"] * W
    inner_x = zx + pad_x
    inner_y = zy + pad_y
    inner_w = zw - 2 * pad_x
    avail_h = zh - 2 * pad_y
    col_w = (inner_w - 2 * gutter) / 3.0

    raw = sanitise(data["lead_article"])
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    hyph = [soft_hyphenate(p, lang) for p in paragraphs]

    start = int(round(cfg["size_body"] * H))
    floor = int(round(cfg["size_body_min"] * H))

    chosen = None
    size = start
    while size >= floor:
        leading = cfg["lead_body"] * size
        gap = cfg["para_gap_frac"] * leading
        measured = [measure_paragraph(cr, hp, cfg["font_body"], size, leading, col_w, lang)
                    for hp in hyph]
        heights = [m[1] for m in measured]
        groups = balance_columns(heights, gap)
        col_heights = [
            sum(heights[i] for i in g) + gap * max(0, len(g) - 1) for g in groups
        ]
        if max(col_heights) <= avail_h:
            chosen = (size, leading, gap, groups)
            break
        size -= 1

    if chosen is None:
        size = floor
        leading = cfg["lead_body"] * size
        gap = cfg["para_gap_frac"] * leading
        measured = [measure_paragraph(cr, hp, cfg["font_body"], size, leading, col_w, lang)
                    for hp in hyph]
        heights = [m[1] for m in measured]
        groups = balance_columns(heights, gap)
        chosen = (size, leading, gap, groups)
        print("[warn] lead article does not fit the article zone at the minimum "
              "body size; it may overflow. Trim copy or enlarge the zone.")

    size, leading, gap, groups = chosen
    # Re render each column group, paragraph by paragraph, stacking with the gap.
    for ci, group in enumerate(groups):
        cx = inner_x + ci * (col_w + gutter)
        cy = inner_y
        for pidx in group:
            layout, ph = measure_paragraph(
                cr, hyph[pidx], cfg["font_body"], size, leading, col_w, lang)
            draw_layout(cr, layout, cx, cy, ink)
            cy += ph + gap

    ctx["body_size"] = size  # exported for sidebar consistency if wanted


# ----- Pull quote ------------------------------------------------------------


def render_pullquote(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    grey = cfg["rule_grey"]
    zx, zy, zw, zh = ctx["px"]["pullquote"]

    quote = "“" + sanitise(data["pull_quote_text"]) + "”"
    attrib = sanitise(data["pull_quote_attribution"])

    q_size = cfg["size_pullquote"] * H
    q_lead = cfg["lead_pullquote"] * q_size
    a_size = cfg["size_pullquote_attrib"] * H

    ql = make_layout(cr)
    set_font(ql, cfg["font_pullquote"], q_size)
    ql.set_width(int(zw * SCALE))
    ql.set_alignment(Pango.Alignment.CENTER)
    ql.set_wrap(Pango.WrapMode.WORD)
    ql.set_attributes(build_attrs(quote, leading_px=q_lead))
    ql.set_text(quote, -1)
    qw, qh = measure(ql)

    al = make_layout(cr)
    set_font(al, cfg["font_sans"], a_size)
    al.set_width(int(zw * SCALE))
    al.set_alignment(Pango.Alignment.CENTER)
    al.set_text(attrib, -1)
    aw, ah = measure(al)

    gap = H * 0.008
    block_h = qh + gap + ah
    top = zy + max(0.0, (zh - block_h) / 2.0)

    weight = ctx["hairline"]
    # Rules above and below the pull quote text (not in the template image).
    hrule(cr, zx, zx + zw, top - H * 0.012, weight, grey)
    draw_layout(cr, ql, zx, top, ink)
    draw_layout(cr, al, zx, top + qh + gap, ink)
    hrule(cr, zx, zx + zw, top + block_h + H * 0.010, weight, grey)


# ----- Stat box --------------------------------------------------------------


def render_statbox(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    grey = cfg["rule_grey"]
    zx, zy, zw, zh = ctx["px"]["statbox"]
    weight = ctx["hairline"]

    # Top rule of the stat box.
    hrule(cr, zx, zx + zw, zy, weight, grey)

    y = zy + H * 0.012

    number = sanitise(data["stat_number"])
    nl = make_layout(cr)
    set_font(nl, cfg["font_stat"], cfg["size_stat_number"] * H)
    nl.set_width(int(zw * SCALE))
    nl.set_alignment(Pango.Alignment.CENTER)
    nl.set_text(number, -1)
    nw, nh = measure(nl)
    draw_layout(cr, nl, zx, y, ink)
    y += nh + H * 0.004

    desc = sanitise(data["stat_descriptor"])
    dl = make_layout(cr)
    set_font(dl, cfg["font_body"], cfg["size_stat_descriptor"] * H)
    dl.set_width(int(zw * SCALE))
    dl.set_alignment(Pango.Alignment.CENTER)
    dl.set_attributes(build_attrs(
        desc, leading_px=cfg["lead_sidebar"] * cfg["size_stat_descriptor"] * H))
    dl.set_text(desc, -1)
    dw, dh = measure(dl)
    draw_layout(cr, dl, zx, y, ink)
    y += dh + H * 0.006

    source = sanitise(data["stat_source"])
    sl = make_layout(cr)
    set_font(sl, cfg["font_sans"], cfg["size_stat_source"] * H)
    sl.set_width(int(zw * SCALE))
    sl.set_alignment(Pango.Alignment.CENTER)
    sl.set_text(source, -1)
    sw, sh = measure(sl)
    draw_layout(cr, sl, zx, y, ink)
    y += sh + H * 0.012

    # Bottom rule of the stat box.
    hrule(cr, zx, zx + zw, min(y, zy + zh), weight, grey)


# ----- Sidebar stories -------------------------------------------------------


def render_sidebar(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    grey = cfg["rule_grey"]
    lang = cfg["hyphenation_lang"]
    zx, zy, zw, zh = ctx["px"]["sidebar"]
    weight = ctx["hairline"]

    stories = [
        (data["sidebar_1_headline"], data["sidebar_1_byline"], data["sidebar_1_body"]),
        (data["sidebar_2_headline"], data["sidebar_2_byline"], data["sidebar_2_body"]),
    ]

    y = zy
    for idx, (head, byl, body) in enumerate(stories):
        if idx > 0:
            y += H * 0.012
            hrule(cr, zx, zx + zw, y, weight, grey)
            y += H * 0.014

        # Headline.
        head = sanitise(head)
        hl = make_layout(cr)
        set_font(hl, cfg["font_sidebar_head"], cfg["size_sidebar_head"] * H)
        hl.set_width(int(zw * SCALE))
        hl.set_wrap(Pango.WrapMode.WORD)
        hl.set_attributes(build_attrs(
            head, leading_px=1.1 * cfg["size_sidebar_head"] * H))
        hl.set_text(head, -1)
        _, hh = measure(hl)
        draw_layout(cr, hl, zx, y, ink)
        y += hh + H * 0.004

        # Byline.
        byl = sanitise(byl)
        bl = make_layout(cr)
        set_font(bl, cfg["font_sans"], cfg["size_sidebar_byline"] * H)
        bl.set_width(int(zw * SCALE))
        bl.set_text(byl, -1)
        _, bh = measure(bl)
        draw_layout(cr, bl, zx, y, ink)
        y += bh + H * 0.006

        # Body: justified, hyphenated.
        body = soft_hyphenate(sanitise(body), lang)
        size = cfg["size_sidebar_body"] * H
        lead = cfg["lead_sidebar"] * size
        layout = make_layout(cr)
        set_font(layout, cfg["font_body"], size)
        layout.set_width(int(zw * SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_justify(True)
        layout.set_alignment(Pango.Alignment.LEFT)
        layout.set_attributes(build_attrs(
            body, leading_px=lead, language=lang, insert_hyphens=True))
        layout.set_text(body, -1)
        _, body_h = measure(layout)
        draw_layout(cr, layout, zx, y, ink)
        y += body_h


# ----- Dividers --------------------------------------------------------------


def render_dividers(cr, ctx):
    if not ctx["cfg"]["draw_dividers"]:
        return
    H, cfg = ctx["H"], ctx["cfg"]
    grey = cfg["rule_grey"]
    art = ctx["px"]["article"]
    side = ctx["px"]["statbox"]
    # Vertical rule in the central gutter, between the article block and sidebar.
    x = (art[0] + art[2] + side[0]) / 2.0
    y1 = ctx["px"]["headline"][1]
    y2 = art[1] + art[3]
    vrule(cr, x, y1, y2, ctx["hairline"], grey)


# ---------------------------------------------------------------------------
# Compositing: text surface -> ink over white -> multiply onto template.
# ---------------------------------------------------------------------------


def cairo_to_pil(surface, W, H):
    surface.flush()
    buf = io.BytesIO()
    surface.write_to_png(buf)  # handles un-premultiplication for us
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def composite(template_rgb, text_rgba, cfg, W):
    # Simulate ink spreading into the newsprint with a sub pixel blur, scaled so
    # the effect is consistent at any upscale factor.
    blur = cfg["text_blur_px"] * (W / float(cfg["blur_reference_width"]))
    if blur > 0:
        text_rgba = text_rgba.filter(ImageFilter.GaussianBlur(blur))
    # Flatten the ink onto white to get a multiply map: white where there is no
    # ink (no change), dark where there is.
    white = Image.new("RGBA", template_rgb.size, (255, 255, 255, 255))
    ink_map = Image.alpha_composite(white, text_rgba).convert("RGB")
    # Multiply lets the paper grain show through the ink.
    return ImageChops.multiply(template_rgb, ink_map)


def place_logo(image_rgb, cfg, W, H):
    path = cfg["logo_path"]
    if not path or not os.path.exists(path):
        print(f"[note] logo not found at {path}; skipping logo placement.")
        return image_rgb
    logo = Image.open(path).convert("RGBA")
    target_w = max(1, int(round(cfg["logo_width_frac"] * W)))
    ratio = target_w / float(logo.width)
    target_h = max(1, int(round(logo.height * ratio)))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)
    margin = int(round(cfg["logo_margin_frac"] * W))
    x = W - target_w - margin
    y = H - target_h - margin
    out = image_rgb.convert("RGBA")
    out.alpha_composite(logo, (x, y))
    return out.convert("RGB")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = [
    "masthead_name", "edition_line", "date", "headline", "byline", "lead_article",
    "pull_quote_text", "pull_quote_attribution", "stat_number", "stat_descriptor",
    "stat_source", "sidebar_1_headline", "sidebar_1_byline", "sidebar_1_body",
    "sidebar_2_headline", "sidebar_2_byline", "sidebar_2_body",
]


def validate(data, cfg):
    missing = [f for f in REQUIRED_FIELDS if f not in data or data[f] in (None, "")]
    if missing:
        print(f"[error] missing required fields: {', '.join(missing)}")
        sys.exit(1)
    n = word_count(data["lead_article"])
    if n < cfg["lead_words_min"] or n > cfg["lead_words_max"]:
        print(f"[warn] lead_article is {n} words, outside the tested "
              f"{cfg['lead_words_min']}-{cfg['lead_words_max']} range "
              f"(target 620). Proceeding anyway.")
    else:
        print(f"[ok] lead_article word count: {n}")


def resolve_zones(cfg, W, H):
    return {name: (fx * W, fy * H, fw * W, fh * H)
            for name, (fx, fy, fw, fh) in cfg["zones"].items()}


def build(template_path, data_path, output_path, cfg=CONFIG):
    with open(data_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    validate(data, cfg)

    template = Image.open(template_path).convert("RGB")
    W, H = template.size
    print(f"[info] template {os.path.basename(template_path)}: {W}x{H}px")

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surface)

    ctx = {
        "W": W, "H": H, "cfg": cfg,
        "px": resolve_zones(cfg, W, H),
        "hairline": max(1.0, cfg["hairline_frac"] * H),
    }

    render_masthead(cr, ctx, data)
    render_headline(cr, ctx, data)
    render_byline(cr, ctx, data, "byline", "byline")
    render_article(cr, ctx, data)
    render_pullquote(cr, ctx, data)
    render_statbox(cr, ctx, data)
    render_sidebar(cr, ctx, data)
    render_dividers(cr, ctx)

    text_rgba = cairo_to_pil(surface, W, H)
    result = composite(template, text_rgba, cfg, W)
    result = place_logo(result, cfg, W, H)

    result.save(output_path)
    print(f"[done] wrote {output_path} ({W}x{H}px)")


def main():
    ap = argparse.ArgumentParser(
        description="Place structured newspaper copy onto a blank template.")
    ap.add_argument("--template", required=True, help="blank newspaper template PNG")
    ap.add_argument("--data", required=True, help="JSON file of structured copy")
    ap.add_argument("--output", required=True, help="output PNG path")
    args = ap.parse_args()
    build(args.template, args.data, args.output)


if __name__ == "__main__":
    main()
