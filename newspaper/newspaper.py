#!/usr/bin/env python3
"""
Sentrada newspaper layout engine.

Places structured newspaper copy onto a blank newspaper template image and
produces a print-ready PNG with real, perfect text every time.

    python newspaper.py --template template.png --data mmc.json --output mmc_final.png

The template carries only newsprint texture, the khaki masthead band, the tan
stat box, and the baked structural rules (column dividers, sidebar boundary,
pull-quote top rule, sidebar divider). This script flows typed copy onto it.

Every zone is defined as a fraction of the template dimensions (see CONFIG), so
the same config works at any resolution. Text is auto-sized to FILL its zone:
the body, headline, stat number and sidebar bodies all grow/shrink to fill,
never floating in the middle. Text is laid out with Pango/Cairo (justified,
en_GB hyphenated) then composited into the paper with a multiply blend so the
newsprint grain shows through the ink.
"""

import argparse
import ctypes
import io
import json
import os
import sys

# ---------------------------------------------------------------------------
# Register the bundled ./fonts directory with fontconfig BEFORE Pango is used,
# so the script is self contained and does not need the fonts installed system
# wide.
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.environ.get(
    "SENTRADA_FONT_DIR", os.path.normpath(os.path.join(SCRIPT_DIR, "..", "fonts"))
)


def register_app_fonts(font_dir):
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
    if not fc.FcConfigAppFontAddDir(fc.FcConfigGetCurrent(), font_dir.encode("utf-8")):
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
# CONFIG. Everything that controls the look of the page. Zone rectangles are
# fractions of the image (x, y, w, h), origin top left, derived from the
# finalised template's measured zone fractions.
# ---------------------------------------------------------------------------

CONFIG = {
    # Colours -----------------------------------------------------------------
    "ink": (0x1A / 255, 0x1A / 255, 0x1A / 255),  # near black, #1A1A1A
    # Fonts -------------------------------------------------------------------
    "font_masthead": "Playfair Display Bold",
    "font_headline": "Playfair Display Bold",
    "font_body": "Lora",
    "font_sans": "Inter",
    "font_pullquote": "Playfair Display Italic",
    "font_stat": "Playfair Display Bold",
    "font_sidebar_head": "Playfair Display Bold",
    # Leading (baseline to baseline) as a multiple of the font size. These are
    # the BASE values; vertical justification may loosen them slightly to fill.
    "lead_headline": 1.10,
    "lead_body": 1.20,
    "lead_pullquote": 1.30,
    "lead_sidebar": 1.20,
    # Masthead tracking (letter spacing) as a fraction of the masthead size.
    "masthead_tracking": 0.10,
    # Fill behaviour ----------------------------------------------------------
    "fill_target": 0.97,        # aim to fill this fraction of a zone's depth
    "fill_min": 0.85,           # warn if a zone fills less than this
    "feather_cap": 1.45,        # max leading stretch (x base) for vertical fill
    "body_size_min": 0.0060,    # body auto-size search range, fraction of height
    "body_size_max": 0.0220,
    "headline_size_min": 0.0140,
    "headline_size_max": 0.0480,
    "stat_size_min": 0.0250,
    "stat_size_max": 0.1600,
    "sidebar_body_min": 0.0075,
    "sidebar_body_max": 0.0190,
    "sidebar_head_min": 0.0120,
    "sidebar_head_max": 0.0230,
    # Small type that is not auto-filled --------------------------------------
    "size_edition": 0.0100,
    "size_byline": 0.0118,
    "size_pullquote_attrib": 0.0085,
    "size_stat_descriptor": 0.0150,
    "size_stat_source": 0.0095,
    "size_sidebar_byline": 0.0108,
    # Column geometry. The template already has the column dividers baked in, so
    # we align text to them and DO NOT draw our own.
    "gutter_frac": 0.0127,          # gutter width, fraction of image width
    "col_divider_1_x": 0.2521,      # baked vertical rule (do not draw)
    "col_divider_2_x": 0.4726,      # baked vertical rule (do not draw)
    "article_pad_top_frac": 0.004,  # small top/bottom inset within the columns zone
    "article_pad_bottom_frac": 0.004,
    # Baked rules the template already provides; the engine must not redraw them.
    "pullquote_top_rule_y": 0.8194,  # baked rule above the pull quote
    # The engine draws ONLY the lower pull-quote rule, matched to the baked rules.
    "lower_rule_default": (0x55 / 255, 0x55 / 255, 0x55 / 255),
    "hairline_frac": 0.00065,        # fallback rule thickness, fraction of height
    # Logo --------------------------------------------------------------------
    "logo_path": os.path.normpath(os.path.join(SCRIPT_DIR, "..", "assets", "sentrada-logo.png")),
    # Ink / paper realism -----------------------------------------------------
    "text_blur_px": 0.4,
    "blur_reference_width": 948,
    "hyphenation_lang": "en_GB",
    # Validation --------------------------------------------------------------
    "lead_words_min": 580,
    "lead_words_max": 660,
    # Zones: (x, y, w, h) as fractions of the image, from the template spec.
    "zones": {
        "masthead":  (0.0000, 0.0000, 1.0000, 0.0567),
        "headline":  (0.0338, 0.0574, 0.6592, 0.1193),
        "byline":    (0.0338, 0.1775, 0.6592, 0.0164),
        "article":   (0.0338, 0.1976, 0.6519, 0.6160),
        "pullquote": (0.0338, 0.8262, 0.6519, 0.0455),
        "kicker":    (0.0338, 0.8784, 0.6519, 0.1059),
        "statbox":   (0.7173, 0.0634, 0.2532, 0.2274),
        "sidebar_1": (0.7068, 0.3095, 0.2689, 0.1431),
        "sidebar_2": (0.7068, 0.4847, 0.2689, 0.3356),
        "logo":      (0.9388, 0.9769, 0.0475, 0.0231),
    },
}

# ---------------------------------------------------------------------------
# Text utilities.
# ---------------------------------------------------------------------------

_HYPHENATOR = None


def get_hyphenator(lang):
    global _HYPHENATOR
    if _HYPHENATOR is None:
        _HYPHENATOR = pyphen.Pyphen(lang=lang)
    return _HYPHENATOR


def sanitise(text):
    """Rendering copy rules. No em dashes anywhere in the output."""
    if text is None:
        return ""
    for ch in ("—", "―", "‒", "⸺", "⸻"):
        text = text.replace(" " + ch + " ", ", ").replace(ch, ", ")
    return text.replace(" -- ", ", ").replace("--", ", ")


def soft_hyphenate(text, lang):
    hyph = get_hyphenator(lang)
    return " ".join(hyph.inserted(t, hyphen="­") if t else t for t in text.split(" "))


def word_count(text):
    return len(text.split())


# ---------------------------------------------------------------------------
# Pango helpers.
# ---------------------------------------------------------------------------


def _whole_range(attr, text):
    attr.start_index = 0
    attr.end_index = len(text.encode("utf-8"))
    return attr


def make_layout(cr):
    return PangoCairo.create_layout(cr)


def set_font(layout, font_str, size_px):
    fd = Pango.FontDescription.from_string(font_str)
    fd.set_absolute_size(size_px * SCALE)
    layout.set_font_description(fd)


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
        attrs.insert(_whole_range(Pango.attr_insert_hyphens_new(insert_hyphens), text))
    return attrs


def draw_layout(cr, layout, x, y, ink):
    cr.save()
    cr.set_source_rgb(*ink)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def measure(layout):
    return layout.get_pixel_size()


def hrule(cr, x1, x2, y, weight, colour):
    cr.save()
    cr.set_source_rgb(*colour)
    cr.set_line_width(max(1.0, weight))
    cr.move_to(x1, y)
    cr.line_to(x2, y)
    cr.stroke()
    cr.restore()


def largest_fitting(feasible, lo, hi, iters=34):
    """Largest value in [lo, hi] for which feasible(value) is True, assuming
    feasible is monotone (True at small values, False above some threshold).
    Used to size text to fill a zone without overflowing."""
    if not feasible(lo):
        return lo, False  # cannot fit even at the minimum
    if feasible(hi):
        return hi, True    # fits even at the maximum (zone may underfill)
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return lo, True


# ---------------------------------------------------------------------------
# Element renderers.
# ---------------------------------------------------------------------------


def render_masthead(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["masthead"]
    margin = zx if zx > 0 else 0.0338 * ctx["W"]
    content_w = ctx["W"] - 2 * margin

    name = sanitise(data["masthead_name"]).upper()
    sub_size = cfg["size_edition"] * H

    # Size the masthead name to fill the band: as large as fits the band height
    # (about 55%) and the content width, with generous tracking.
    layout = make_layout(cr)

    def name_metrics(size):
        set_font(layout, cfg["font_masthead"], size)
        layout.set_attributes(build_attrs(name, letter_spacing_px=cfg["masthead_tracking"] * size))
        layout.set_width(-1)
        layout.set_text(name, -1)
        return measure(layout)

    def feasible(size):
        w, h = name_metrics(size)
        return w <= content_w and h <= zh * 0.62

    size, _ = largest_fitting(feasible, 0.020 * H, 0.060 * H)
    nw, nh = name_metrics(size)

    block_h = nh + zh * 0.10 + sub_size * 1.2
    top = zy + max(0.0, (zh - block_h) / 2.0)
    draw_layout(cr, layout, margin + (content_w - nw) / 2.0, top, ink)

    line_y = top + nh + zh * 0.10
    el = make_layout(cr)
    set_font(el, cfg["font_sans"], sub_size)
    el.set_width(-1)
    el.set_text(sanitise(data["edition_line"]), -1)
    draw_layout(cr, el, margin, line_y, ink)

    dl = make_layout(cr)
    set_font(dl, cfg["font_sans"], sub_size)
    dl.set_width(-1)
    dl.set_text(sanitise(data["date"]), -1)
    dw, _ = measure(dl)
    draw_layout(cr, dl, margin + content_w - dw, line_y, ink)


def render_headline(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["headline"]
    text = sanitise(data["headline"])
    lead = cfg["lead_headline"]

    layout = make_layout(cr)
    layout.set_width(int(zw * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.LEFT)

    def height_at(size):
        set_font(layout, cfg["font_headline"], size)
        layout.set_attributes(build_attrs(text, leading_px=lead * size))
        layout.set_text(text, -1)
        return measure(layout)[1]

    # Largest size that fills the headline zone height without overflowing.
    size, fits = largest_fitting(
        lambda s: height_at(s) <= zh, cfg["headline_size_min"] * H, cfg["headline_size_max"] * H)
    height_at(size)
    if not fits:
        layout.set_height(int(zh * SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        print("[warn] headline does not fit at minimum size; truncating.")

    # Vertically centre the wrapped headline within its zone.
    th = measure(layout)[1]
    draw_layout(cr, layout, zx, zy + max(0.0, (zh - th) / 2.0), ink)


def render_byline(cr, ctx, data, key, zone_key):
    H, cfg = ctx["H"], ctx["cfg"]
    zx, zy, zw, zh = ctx["px"][zone_key]
    layout = make_layout(cr)
    set_font(layout, cfg["font_sans"], cfg["size_byline"] * H)
    layout.set_width(int(zw * SCALE))
    layout.set_text(sanitise(data[key]), -1)
    th = measure(layout)[1]
    draw_layout(cr, layout, zx, zy + max(0.0, (zh - th) / 2.0), cfg["ink"])


# ----- Lead article: three balanced, justified, hyphenated columns -----------


def make_paragraph_layout(cr, text, font_str, size_px, leading_px, col_w, lang):
    layout = make_layout(cr)
    set_font(layout, font_str, size_px)
    layout.set_width(int(col_w * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_justify(True)
    layout.set_attributes(build_attrs(
        text, leading_px=leading_px, language=lang, insert_hyphens=True))
    layout.set_text(text, -1)
    return layout


def _column_height(heights, gap, group):
    return 0.0 if not group else sum(heights[i] for i in group) + gap * (len(group) - 1)


def balance_columns(heights, gap):
    """Partition paragraphs (in order) across three columns, splitting only at
    paragraph boundaries (never mid paragraph), choosing the two break points
    that minimise the tallest column so the columns end level."""
    n = len(heights)
    if n == 0:
        return [[], [], []]
    if n <= 3:
        return ([[i] for i in range(n)] + [[], [], []])[:3]
    best = None
    for b1 in range(0, n - 2):
        for b2 in range(b1 + 1, n - 1):
            groups = [list(range(0, b1 + 1)), list(range(b1 + 1, b2 + 1)), list(range(b2 + 1, n))]
            ch = [_column_height(heights, gap, g) for g in groups]
            key = (max(ch), max(ch) - min(ch))
            if best is None or key < best[0]:
                best = (key, groups)
    return best[1]


def feather_leading(measure_fn, base_leading, avail_h, cap_factor):
    """Largest leading in [base, base*cap] whose laid-out height still fits
    avail_h. Vertically justifies a column/block to bottom-align by gently
    loosening line spacing. Returns base if it already overflows."""
    if measure_fn(base_leading) >= avail_h:
        return base_leading
    hi = base_leading * cap_factor
    if measure_fn(hi) <= avail_h:
        return hi
    lo = base_leading
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if measure_fn(mid) <= avail_h:
            lo = mid
        else:
            hi = mid
    return lo


def render_article(cr, ctx, data):
    W, H, cfg = ctx["W"], ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    lang = cfg["hyphenation_lang"]
    zx, zy, zw, zh = ctx["px"]["article"]

    gutter = cfg["gutter_frac"] * W
    d1 = cfg["col_divider_1_x"] * W
    d2 = cfg["col_divider_2_x"] * W
    # Column left edges and a single representative width (columns are equal to
    # within a pixel), aligned to the baked dividers.
    col_x = [zx, d1 + gutter / 2.0, d2 + gutter / 2.0]
    col_right = [d1 - gutter / 2.0, d2 - gutter / 2.0, zx + zw]
    col_w = sum(col_right[i] - col_x[i] for i in range(3)) / 3.0

    top = zy + cfg["article_pad_top_frac"] * H
    avail_h = zh - (cfg["article_pad_top_frac"] + cfg["article_pad_bottom_frac"]) * H

    paragraphs = [p.strip() for p in sanitise(data["lead_article"]).split("\n\n") if p.strip()]
    hyph = [soft_hyphenate(p, lang) for p in paragraphs]

    def metrics_at(size):
        leading = cfg["lead_body"] * size
        gap = 0.5 * leading
        heights = [measure(make_paragraph_layout(
            cr, hp, cfg["font_body"], size, leading, col_w, lang))[1] for hp in hyph]
        groups = balance_columns(heights, gap)
        col_h = [_column_height(heights, gap, g) for g in groups]
        return leading, gap, heights, groups, col_h

    # Size the body so the tallest balanced column fills the columns zone.
    def feasible(size):
        return max(metrics_at(size)[4]) <= avail_h

    size, fits = largest_fitting(
        feasible, cfg["body_size_min"] * H, cfg["body_size_max"] * H)
    leading, gap, heights, groups, col_h = metrics_at(size)
    fill = max(col_h) / avail_h if avail_h else 0
    if not fits:
        print("[warn] lead article overflows the columns zone at the minimum body "
              "size; trim copy or enlarge the zone.")
    elif fill < cfg["fill_min"]:
        print(f"[warn] columns fill only {fill:.0%} of the zone even at the maximum "
              f"body size; the article may be too short for this layout.")

    # Render each column, vertically justified to bottom-align with the others.
    for ci, group in enumerate(groups):
        if not group:
            continue

        def col_height(ld, g=group):
            return sum(measure(make_paragraph_layout(
                cr, hyph[i], cfg["font_body"], size, ld, col_w, lang))[1] for i in g) \
                + (0.5 * ld) * (len(g) - 1)

        ld = feather_leading(col_height, leading, avail_h, cfg["feather_cap"])
        col_gap = 0.5 * ld
        cy = top
        for i in group:
            layout = make_paragraph_layout(cr, hyph[i], cfg["font_body"], size, ld, col_w, lang)
            draw_layout(cr, layout, col_x[ci], cy, ink)
            cy += measure(layout)[1] + col_gap


# ----- Pull quote ------------------------------------------------------------


def render_pullquote(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["pullquote"]

    quote = "“" + sanitise(data["pull_quote_text"]) + "”"
    attrib = sanitise(data["pull_quote_attribution"])

    # Attribution first (fixed size) so we can reserve its space, then a gap and
    # clearance above the lower rule, before sizing the quote to fill the rest.
    a_size = cfg["size_pullquote_attrib"] * H
    al = make_layout(cr)
    set_font(al, cfg["font_sans"], a_size)
    al.set_width(int(zw * SCALE))
    al.set_alignment(Pango.Alignment.CENTER)
    al.set_text(attrib, -1)
    ah = measure(al)[1]

    gap = H * 0.004
    clearance = H * 0.006
    quote_budget = zh - clearance - gap - ah

    layout = make_layout(cr)
    layout.set_width(int(zw * SCALE))
    layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_wrap(Pango.WrapMode.WORD)

    def q_height(size):
        set_font(layout, cfg["font_pullquote"], size)
        layout.set_attributes(build_attrs(quote, leading_px=cfg["lead_pullquote"] * size))
        layout.set_text(quote, -1)
        return measure(layout)[1]

    q_size, _ = largest_fitting(lambda s: q_height(s) <= quote_budget, 0.0070 * H, 0.030 * H)
    q_height(q_size)
    qh = measure(layout)[1]

    block_h = qh + gap + ah
    qy = zy + max(0.0, (zh - clearance - block_h) / 2.0)
    draw_layout(cr, layout, zx, qy, ink)
    draw_layout(cr, al, zx, qy + qh + gap, ink)

    # The template already has the rule ABOVE the pull quote; draw only the rule
    # BELOW it, matched to the baked rules' colour and weight.
    hrule(cr, zx, zx + zw, zy + zh, ctx["rule_weight"], ctx["rule_colour"])


# ----- Stat box (on the tan fill) --------------------------------------------


def render_statbox(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["statbox"]

    number = sanitise(data["stat_number"])
    desc = sanitise(data["stat_descriptor"])
    source = sanitise(data["stat_source"])

    # Stat number: as large as possible within the tan box (width and ~half the
    # box height, leaving room for the descriptor and source beneath).
    nl = make_layout(cr)
    nl.set_width(-1)

    def num_ok(size):
        set_font(nl, cfg["font_stat"], size)
        nl.set_text(number, -1)
        w, h = measure(nl)
        return w <= zw * 0.92 and h <= zh * 0.52

    nsize, _ = largest_fitting(num_ok, cfg["stat_size_min"] * H, cfg["stat_size_max"] * H)
    set_font(nl, cfg["font_stat"], nsize)
    nl.set_text(number, -1)
    nw, nh = measure(nl)

    dl = make_layout(cr)
    set_font(dl, cfg["font_body"], cfg["size_stat_descriptor"] * H)
    dl.set_width(int(zw * 0.90 * SCALE))
    dl.set_alignment(Pango.Alignment.CENTER)
    dl.set_attributes(build_attrs(desc, leading_px=1.2 * cfg["size_stat_descriptor"] * H))
    dl.set_text(desc, -1)
    dw, dh = measure(dl)

    sl = make_layout(cr)
    set_font(sl, cfg["font_sans"], cfg["size_stat_source"] * H)
    sl.set_width(int(zw * 0.90 * SCALE))
    sl.set_alignment(Pango.Alignment.CENTER)
    sl.set_text(source, -1)
    sw, sh = measure(sl)

    gap1 = H * 0.006
    gap2 = H * 0.008
    block_h = nh + gap1 + dh + gap2 + sh
    y = zy + max(0.0, (zh - block_h) / 2.0)
    draw_layout(cr, nl, zx + (zw - nw) / 2.0, y, ink)
    y += nh + gap1
    draw_layout(cr, dl, zx + (zw - dw) / 2.0, y, ink)
    y += dh + gap2
    draw_layout(cr, sl, zx + (zw - sw) / 2.0, y, ink)


# ----- Sidebar stories (two explicit zones, each filled) ---------------------


def render_sidebar_story(cr, ctx, zone, head, byline, body):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    lang = cfg["hyphenation_lang"]
    zx, zy, zw, zh = zone

    head = sanitise(head)
    byline = sanitise(byline)
    body = soft_hyphenate(sanitise(body), lang)

    # Headline: largest size (within range) that wraps to at most ~32% of the
    # zone height and fits the width.
    hl = make_layout(cr)
    hl.set_width(int(zw * SCALE))
    hl.set_wrap(Pango.WrapMode.WORD)

    def head_h(size):
        set_font(hl, cfg["font_sidebar_head"], size)
        hl.set_attributes(build_attrs(head, leading_px=1.08 * size))
        hl.set_text(head, -1)
        return measure(hl)[1]

    hsize, _ = largest_fitting(
        lambda s: head_h(s) <= zh * 0.34, cfg["sidebar_head_min"] * H, cfg["sidebar_head_max"] * H)
    head_h(hsize)
    hh = measure(hl)[1]

    bl = make_layout(cr)
    set_font(bl, cfg["font_sans"], cfg["size_sidebar_byline"] * H)
    bl.set_width(int(zw * SCALE))
    bl.set_text(byline, -1)
    bh = measure(bl)[1]

    gap_hb = H * 0.004
    gap_bb = H * 0.007
    body_top = zy + hh + gap_hb + bh + gap_bb
    body_avail = (zy + zh) - body_top

    # Body: largest size that fits the remaining depth, then feathered to fill it.
    def body_layout(size, leading):
        layout = make_layout(cr)
        set_font(layout, cfg["font_body"], size)
        layout.set_width(int(zw * SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_justify(True)
        layout.set_alignment(Pango.Alignment.LEFT)
        layout.set_attributes(build_attrs(
            body, leading_px=leading, language=lang, insert_hyphens=True))
        layout.set_text(body, -1)
        return layout

    bsize, _ = largest_fitting(
        lambda s: measure(body_layout(s, cfg["lead_sidebar"] * s))[1] <= body_avail,
        cfg["sidebar_body_min"] * H, cfg["sidebar_body_max"] * H)
    base_lead = cfg["lead_sidebar"] * bsize
    lead = feather_leading(
        lambda ld: measure(body_layout(bsize, ld))[1], base_lead, body_avail, cfg["feather_cap"])

    draw_layout(cr, hl, zx, zy, ink)
    draw_layout(cr, bl, zx, zy + hh + gap_hb, ink)
    draw_layout(cr, body_layout(bsize, lead), zx, body_top, ink)


def render_sidebar(cr, ctx, data):
    render_sidebar_story(cr, ctx, ctx["px"]["sidebar_1"],
                         data["sidebar_1_headline"], data["sidebar_1_byline"],
                         data["sidebar_1_body"])
    render_sidebar_story(cr, ctx, ctx["px"]["sidebar_2"],
                         data["sidebar_2_headline"], data["sidebar_2_byline"],
                         data["sidebar_2_body"])


# ----- Kicker (optional full-width block beneath the pull quote) --------------


def render_kicker(cr, ctx, data):
    """The template has a full-width 'kicker' zone below the pull quote. It has
    no dedicated field in the standard schema; if the data supplies kicker_text
    (and optional kicker_headline / kicker_byline) we fill it, otherwise we
    leave it blank."""
    if "kicker_text" not in data or not data.get("kicker_text"):
        return
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    lang = cfg["hyphenation_lang"]
    zx, zy, zw, zh = ctx["px"]["kicker"]
    text = soft_hyphenate(sanitise(data["kicker_text"]), lang)

    gutter = cfg["gutter_frac"] * ctx["W"]
    d1 = cfg["col_divider_1_x"] * ctx["W"]
    d2 = cfg["col_divider_2_x"] * ctx["W"]
    col_x = [zx, d1 + gutter / 2.0, d2 + gutter / 2.0]
    col_right = [d1 - gutter / 2.0, d2 - gutter / 2.0, zx + zw]
    col_w = sum(col_right[i] - col_x[i] for i in range(3)) / 3.0

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    def metrics_at(size):
        leading = cfg["lead_body"] * size
        gap = 0.5 * leading
        heights = [measure(make_paragraph_layout(
            cr, p, cfg["font_body"], size, leading, col_w, lang))[1] for p in paragraphs]
        groups = balance_columns(heights, gap)
        return leading, gap, groups, [_column_height(heights, gap, g) for g in groups]

    size, _ = largest_fitting(
        lambda s: max(metrics_at(s)[3]) <= zh, cfg["body_size_min"] * H, cfg["body_size_max"] * H)
    leading, gap, groups, _ = metrics_at(size)
    for ci, group in enumerate(groups):
        cy = zy
        for i in group:
            layout = make_paragraph_layout(cr, paragraphs[i], cfg["font_body"], size, leading, col_w, lang)
            draw_layout(cr, layout, col_x[ci], cy, ink)
            cy += measure(layout)[1] + gap


# ---------------------------------------------------------------------------
# Rule colour sampling: match the engine-drawn lower pull-quote rule to the
# rules already baked into the template.
# ---------------------------------------------------------------------------


def sample_rule(template_rgb, cfg, W, H):
    """Look along the baked pull-quote top rule and pick its darkest pixel as the
    rule colour, so our lower rule matches. Falls back to a default grey."""
    y = int(round(cfg["pullquote_top_rule_y"] * H))
    x0 = int(round(cfg["zones"]["pullquote"][0] * W))
    x1 = int(round((cfg["zones"]["pullquote"][0] + cfg["zones"]["pullquote"][2]) * W))
    px = template_rgb.load()
    best = None
    for yy in (y - 1, y, y + 1):
        if not (0 <= yy < H):
            continue
        for xx in range(max(0, x0), min(W, x1), max(1, (x1 - x0) // 200)):
            r, g, b = px[xx, yy][:3]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            if best is None or lum < best[0]:
                best = (lum, (r, g, b))
    weight = max(1.0, cfg["hairline_frac"] * H)
    if best is None or best[0] > 200:
        return cfg["lower_rule_default"], weight
    r, g, b = best[1]
    return (r / 255, g / 255, b / 255), weight


# ---------------------------------------------------------------------------
# Compositing.
# ---------------------------------------------------------------------------


def cairo_to_pil(surface):
    surface.flush()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def composite(template_rgb, text_rgba, cfg, W):
    blur = cfg["text_blur_px"] * (W / float(cfg["blur_reference_width"]))
    if blur > 0:
        text_rgba = text_rgba.filter(ImageFilter.GaussianBlur(blur))
    white = Image.new("RGBA", template_rgb.size, (255, 255, 255, 255))
    ink_map = Image.alpha_composite(white, text_rgba).convert("RGB")
    return ImageChops.multiply(template_rgb, ink_map)


def place_logo(image_rgb, ctx):
    cfg, W, H = ctx["cfg"], ctx["W"], ctx["H"]
    path = cfg["logo_path"]
    if not path or not os.path.exists(path):
        print(f"[note] logo not found at {path}; skipping logo placement.")
        return image_rgb
    zx, zy, zw, zh = ctx["px"]["logo"]
    logo = Image.open(path).convert("RGBA")
    # Fit within the logo zone, preserving aspect ratio, bottom-right aligned.
    ratio = min(zw / logo.width, zh / logo.height)
    tw, th = max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio))
    logo = logo.resize((tw, th), Image.LANCZOS)
    x = int(round(zx + zw - tw))
    y = int(round(zy + zh - th))
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
              f"{cfg['lead_words_min']}-{cfg['lead_words_max']} range (target 620). "
              f"Proceeding anyway.")
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
    rule_colour, rule_weight = sample_rule(template, cfg, W, H)
    ctx = {"W": W, "H": H, "cfg": cfg, "px": resolve_zones(cfg, W, H),
           "rule_colour": rule_colour, "rule_weight": rule_weight}

    render_masthead(cr, ctx, data)
    render_headline(cr, ctx, data)
    render_byline(cr, ctx, data, "byline", "byline")
    render_article(cr, ctx, data)
    render_pullquote(cr, ctx, data)
    render_statbox(cr, ctx, data)
    render_sidebar(cr, ctx, data)
    render_kicker(cr, ctx, data)

    text_rgba = cairo_to_pil(surface)
    result = composite(template, text_rgba, cfg, W)
    result = place_logo(result, ctx)
    result.save(output_path)
    print(f"[done] wrote {output_path} ({W}x{H}px)")


def main():
    ap = argparse.ArgumentParser(
        description="Place structured newspaper copy onto a blank template.")
    ap.add_argument("--template", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    build(args.template, args.data, args.output)


if __name__ == "__main__":
    main()
