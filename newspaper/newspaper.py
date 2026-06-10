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
    "lead_pullquote": 1.34,
    "lead_sidebar": 1.42,
    # Masthead tracking (letter spacing) as a fraction of the masthead size.
    # Tight and dense; the nameplate is auto-sized to span the full page width
    # and fill the khaki band, so tracking stays small to let the glyphs grow.
    "masthead_tracking": 0.010,
    # Fill behaviour ----------------------------------------------------------
    "fill_target": 0.97,        # aim to fill this fraction of a zone's depth
    "fill_min": 0.85,           # warn if a zone fills less than this
    "feather_cap": 1.60,        # max leading stretch (x base) for vertical fill
    "body_size_min": 0.0060,    # body auto-size search range, fraction of height
    "body_size_max": 0.0220,
    "headline_size_min": 0.0140,
    "headline_size_max": 0.0480,
    "stat_size_min": 0.0250,
    "stat_size_max": 0.1600,
    "sidebar_body_min": 0.0095,
    "sidebar_body_max": 0.0215,
    "sidebar_head_min": 0.0160,
    "sidebar_head_max": 0.0320,
    # Sidebar headline may occupy up to this fraction of its (sidebar 1) zone
    # height. Sidebar bodies are never stretched to fill; see render_sidebar.
    "sidebar_head_frac": 0.40,
    # Small type that is not auto-filled --------------------------------------
    "size_edition": 0.0100,
    "size_byline": 0.0118,
    "size_pullquote_attrib": 0.0085,
    "size_stat_descriptor": 0.0225,   # roughly 2x: the descriptor reads boldly
    "size_stat_source": 0.0105,
    "size_sidebar_byline": 0.0108,
    # Stat number: fill the box. Constrained on INK extents (actual glyph bounds,
    # not the line-height box) so the figures genuinely dominate.
    "stat_ink_width_frac": 0.96,      # number ink may fill this much of box width
    "stat_ink_height_frac": 0.60,     # ...and up to this much of box height
    # Column geometry. The template already has the column dividers baked in, so
    # we align text to them and DO NOT draw our own.
    "gutter_frac": 0.0127,          # gutter width, fraction of image width
    "col_divider_1_x": 0.2521,      # baked vertical rule (do not draw)
    "col_divider_2_x": 0.4726,      # baked vertical rule (do not draw)
    "col_pad_frac": 0.0065,         # breathing room each side of a column so body
    # text never touches the baked divider rules (fraction of image width)
    "article_pad_top_frac": 0.004,  # small top/bottom inset within the columns zone
    "article_pad_bottom_frac": 0.004,
    # Hyphenation. Favour fewer breaks: only hyphenate longer words and keep a
    # decent run of letters either side of a break.
    "hyph_left": 3,
    "hyph_right": 3,
    "hyph_min_word_len": 8,
    # Baked rule under the headline block (the standfirst rule). The headline
    # grows to fill the band down to here; the byline and columns then chain off
    # the headline's actual bottom rather than sitting at fixed positions.
    "standfirst_rule_y": 0.1767,
    # Edition line letterspacing (fraction of its size) when set in spaced caps.
    "edition_tracking": 0.10,
    # Khaki band vertical extent (the nameplate is centred and sized to it).
    "masthead_band": (0.0119, 0.0567),
    # Baked rules the template already provides; the engine must not redraw them.
    "pullquote_top_rule_y": 0.8233,  # baked rule above the pull-quote feature
    # The faint baked sub-rule partway down the bottom block (article width only).
    # The pull-quote feature now spans the whole bottom block, so this stray rule
    # is erased (cloned over with paper) to keep the feature clean.
    "sub_rule_y": 0.8717,
    "sub_rule_x_end": 0.693,
    # Baked rule between the two sidebar stories. It sits at a fixed y but the
    # sidebar content length varies per send, so the engine inpaints it out of
    # the template and redraws it at an adaptive position: one line height below
    # sidebar 1's content end (see render_sidebar).
    "sidebar_rule_y": 0.4840,
    "sidebar_rule_x0": 0.7000,
    "sidebar_rule_x1": 0.9790,
    "lower_rule_default": (0x55 / 255, 0x55 / 255, 0x55 / 255),
    "hairline_frac": 0.00065,        # fallback rule thickness, fraction of height
    # Logo --------------------------------------------------------------------
    "logo_path": os.path.normpath(os.path.join(SCRIPT_DIR, "..", "assets", "sentrada-logo.png")),
    # Ink / paper realism. A faint sub-pixel blur simulates ink spreading into
    # newsprint. It scales with resolution but is capped low so text stays sharp
    # at print resolution (set ink_blur_cap to 0, or pass --ink-blur 0, for dead
    # sharp "pasted" text).
    "text_blur_px": 0.4,
    "blur_reference_width": 948,
    "ink_blur_cap": 0.6,
    "hyphenation_lang": "en_GB",
    # Physical page size, for downsampling a supersampled render to an exact
    # print resolution. A2 portrait.
    "a2_mm": (420, 594),
    # Validation --------------------------------------------------------------
    "lead_words_min": 580,
    "lead_words_max": 660,
    # Zones: (x, y, w, h) as fractions of the image, from the template spec.
    # Baked horizontal rules sit at y = 0.0567 (under masthead), 0.1767 (under
    # the headline block), 0.8233 (under the columns), 0.8717 (faint sub-rule,
    # erased). The headline block lives BETWEEN the masthead and standfirst rules
    # with breathing room; the pull-quote feature fills the whole bottom block.
    "zones": {
        "masthead":  (0.0000, 0.0000, 1.0000, 0.0567),
        "headline":  (0.0338, 0.0660, 0.6592, 0.0930),
        "byline":    (0.0338, 0.1600, 0.6592, 0.0150),
        "article":   (0.0338, 0.1840, 0.6519, 0.6320),
        # Pull-quote feature: fills the entire bottom block, from the baked rule
        # at 0.8233 down to a small bottom margin, large and generously spaced so
        # the lower third reads as a deliberate closing feature, not dead space.
        "pullquote": (0.0338, 0.8290, 0.6519, 0.1480),
        "kicker":    (0.0338, 0.9050, 0.6519, 0.0793),
        "statbox":   (0.7173, 0.0634, 0.2532, 0.2274),
        # Sidebar 1 now runs down to just above the baked divider rule (0.4840),
        # closing the dead gap above it. Sidebar 2 starts just below that rule
        # and runs down to the pull-quote depth (~0.93), filling the lower-right
        # that was empty. The rail's vertical divider runs to 0.95, and no baked
        # horizontal rule crosses the rail below 0.484, so this is clean.
        "sidebar_1": (0.7068, 0.3095, 0.2689, 0.1705),
        "sidebar_2": (0.7068, 0.4885, 0.2689, 0.4415),
        "logo":      (0.9388, 0.9769, 0.0475, 0.0231),
    },
}

# ---------------------------------------------------------------------------
# Text utilities.
# ---------------------------------------------------------------------------

_HYPHENATOR = None


def get_hyphenator(cfg):
    global _HYPHENATOR
    if _HYPHENATOR is None:
        _HYPHENATOR = pyphen.Pyphen(
            lang=cfg["hyphenation_lang"], left=cfg["hyph_left"], right=cfg["hyph_right"])
    return _HYPHENATOR


def sanitise(text):
    """Rendering copy rules. No em dashes anywhere in the output."""
    if text is None:
        return ""
    for ch in ("—", "―", "‒", "⸺", "⸻"):
        text = text.replace(" " + ch + " ", ", ").replace(ch, ", ")
    return text.replace(" -- ", ", ").replace("--", ", ")


def keep_phrases_together(text):
    """Bind runs of capitalised words (proper nouns like "MMC Ventures") with a
    non-breaking space so centred, narrow text blocks do not split them across
    lines. Only the space BETWEEN two capitalised tokens is hardened."""
    if not text:
        return text
    tokens = text.split(" ")
    out = [tokens[0]] if tokens else []
    for i in range(1, len(tokens)):
        prev, cur = tokens[i - 1], tokens[i]
        cap = lambda w: bool(w) and (w[0].isupper() or w[0].isdigit())
        out.append((" " if cap(prev) and cap(cur) else " ") + cur)
    return "".join(out)


def prevent_orphan(text, words=2):
    """Bind the final `words` tokens with non-breaking spaces so a wrap can never
    strand a single short word on its own last line. Used on the headline (so
    "Who Runs Sales?" stays together) and on the last paragraph of the lead so a
    column never ends on a lone word."""
    if not text:
        return text
    nbsp = chr(0x00A0)
    toks = text.split(" ")
    if len(toks) <= words:
        return nbsp.join(toks)
    head = " ".join(toks[:-words])
    tail = nbsp.join(toks[-words:])
    return head + " " + tail


def soft_hyphenate(text, cfg):
    """Insert U+00AD soft hyphens for Pango to break on, but sparingly: only in
    words at least hyph_min_word_len long, and never the final word of the text
    (so the last line stays clean). Pango still only breaks when a line would
    otherwise be too loose."""
    hyph = get_hyphenator(cfg)
    min_len = cfg["hyph_min_word_len"]
    tokens = text.split(" ")
    last = len(tokens) - 1
    out = []
    for i, t in enumerate(tokens):
        if t and i != last and len(t) >= min_len:
            out.append(hyph.inserted(t, hyphen="­"))
        else:
            out.append(t)
    return " ".join(out)


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
    band_top, band_bot = cfg["masthead_band"]
    band_top, band_bot = band_top * H, band_bot * H
    band_h = band_bot - band_top

    # The nameplate is the loudest text on the page: size it to span the FULL
    # printable width (margin to margin) and fill the khaki band almost to its
    # height. Width is normally the binding limit; tracking is small so the
    # glyphs grow as large as possible.
    layout = make_layout(cr)

    def name_metrics(size):
        set_font(layout, cfg["font_masthead"], size)
        layout.set_attributes(build_attrs(name, letter_spacing_px=cfg["masthead_tracking"] * size))
        layout.set_width(-1)
        layout.set_text(name, -1)
        # Measure the INK (actual cap glyphs), not the logical line box: all-caps
        # has no descenders, so the logical box is far taller than the letters.
        # Capping ink height to the band lets the glyphs grow until WIDTH binds,
        # so the nameplate spans the full page rather than being throttled small.
        r = layout.get_pixel_extents()[0]
        return r.width, r.height

    def feasible(size):
        w, h = name_metrics(size)
        return w <= content_w and h <= band_h * 0.86

    size, _ = largest_fitting(feasible, 0.030 * H, 0.130 * H)
    nw, nh = name_metrics(size)
    # Centre the nameplate in the khaki band, both axes. Use ink extents so the
    # caps sit optically centred rather than floating on the line box.
    ink_r = layout.get_pixel_extents()[0]
    name_x = margin + (content_w - ink_r.width) / 2.0 - ink_r.x
    name_y = band_top + (band_h - ink_r.height) / 2.0 - ink_r.y
    draw_layout(cr, layout, name_x, name_y, ink)

    # Edition line and date sit just below the band, the edition line in spaced
    # capitals to read like a broadsheet folio.
    line_y = band_bot + 0.004 * H
    edition = sanitise(data["edition_line"]).upper()
    el = make_layout(cr)
    set_font(el, cfg["font_sans"], sub_size)
    el.set_attributes(build_attrs(edition, letter_spacing_px=cfg["edition_tracking"] * sub_size))
    el.set_width(-1)
    el.set_text(edition, -1)
    draw_layout(cr, el, margin, line_y, ink)

    dl = make_layout(cr)
    set_font(dl, cfg["font_sans"], sub_size)
    dl.set_width(-1)
    dl.set_text(sanitise(data["date"]), -1)
    dw, _ = measure(dl)
    draw_layout(cr, dl, margin + content_w - dw, line_y, ink)


def render_headline_and_byline(cr, ctx, data):
    """Headline grown to FILL the band from its zone top down to the standfirst
    rule, in two or three balanced lines. The byline (bold) chains directly below
    the headline's last line, and the lead columns chain directly below the
    byline: no fixed gaps, so the top of the page is loud with no dead space.
    Stores the article top (where the columns begin) in ctx."""
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    hx, hy, hw, hh = ctx["px"]["headline"]

    region_top = hy
    region_bottom = cfg["standfirst_rule_y"] * H - 0.004 * H

    text = prevent_orphan(sanitise(data["headline"]), words=2)
    lead = cfg["lead_headline"]
    layout = make_layout(cr)
    layout.set_width(int(hw * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.LEFT)

    byl = make_layout(cr)
    set_font(byl, cfg["font_sans"] + " Bold", cfg["size_byline"] * H)
    byl.set_width(int(hw * SCALE))
    byl.set_text(sanitise(data["byline"]), -1)
    byl_h = measure(byl)[1]

    gap_hb = H * 0.010   # headline to byline
    gap_ba = H * 0.014   # byline to first column
    avail_head = region_bottom - region_top - byl_h - gap_hb

    def height_at(size):
        set_font(layout, cfg["font_headline"], size)
        layout.set_attributes(build_attrs(text, leading_px=lead * size))
        layout.set_text(text, -1)
        return measure(layout)[1]

    # Grow the headline to fill the available band depth (up to three lines).
    size, fits = largest_fitting(
        lambda s: height_at(s) <= avail_head, cfg["headline_size_min"] * H, cfg["headline_size_max"] * H)
    height_at(size)
    if not fits:
        layout.set_height(int(avail_head * SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        print("[warn] headline does not fit at minimum size; truncating.")

    # Balanced breaking: no full line under 50% of block width (last under 40%).
    def line_widths():
        ws = []
        it = layout.get_iter()
        while True:
            ws.append(it.get_line_extents()[1].width / SCALE)
            if not it.next_line():
                break
        return ws

    def balanced():
        ws = line_widths()
        if len(ws) <= 1:
            return True
        return all(w >= 0.50 * hw for w in ws[:-1]) and ws[-1] >= 0.40 * hw

    floor = cfg["headline_size_min"] * H
    while size * 0.98 >= floor and not balanced():
        size *= 0.98
        height_at(size)
    if not balanced():
        print("[warn] headline lines remain ragged at the minimum size.")
    head_h = measure(layout)[1]

    # Chain: headline at region top, byline directly beneath, columns directly
    # beneath the byline.
    draw_layout(cr, layout, hx, region_top, ink)
    byline_y = region_top + head_h + gap_hb
    draw_layout(cr, byl, hx, byline_y, ink)
    ctx["article_top_y"] = byline_y + byl_h + gap_ba


# ----- Lead article: three balanced, justified, hyphenated columns -----------


def make_paragraph_layout(cr, text, font_str, size_px, leading_px, col_w, lang):
    layout = make_layout(cr)
    set_font(layout, font_str, size_px)
    layout.set_width(int(col_w * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_justify(True)
    layout.set_justify_last_line(False)  # last line of a paragraph stays left
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
    groups = best[1]

    # Hard requirement: columns must end at the same depth. If the shortest
    # column is more than 3% shorter than the tallest, try shifting a single
    # paragraph across the worst boundary and keep the move only if it reduces
    # the spread. (The min-max partition above is already optimal at paragraph
    # granularity, so this rarely fires; it is a guard for awkward copy.)
    def spread(gs):
        ch = [_column_height(heights, gap, g) for g in gs]
        return max(ch) - min(ch), ch
    sp, ch = spread(groups)
    if ch and max(ch) > 0 and sp / max(ch) > 0.03:
        for _ in range(len(heights)):
            tallest = max(range(3), key=lambda i: ch[i])
            cand = None
            for j in (tallest - 1, tallest + 1):  # adjacent columns only
                if 0 <= j < 3 and groups[tallest]:
                    moved = [list(g) for g in groups]
                    if j < tallest:           # give tallest's first para to left
                        moved[j].append(moved[tallest].pop(0))
                    else:                     # give tallest's last para to right
                        moved[j].insert(0, moved[tallest].pop())
                    if all(moved):            # never empty a column
                        nsp, _ = spread(moved)
                        if cand is None or nsp < cand[0]:
                            cand = (nsp, moved)
            if cand is None or cand[0] >= sp:
                break
            groups = cand[1]
            sp, ch = spread(groups)
    return groups


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

    pad = cfg["col_pad_frac"] * W
    d1 = cfg["col_divider_1_x"] * W
    d2 = cfg["col_divider_2_x"] * W
    # Column left edges and right edges, inset by `pad` from the baked dividers
    # (and from the zone edges) so body text never touches a structural rule.
    col_x = [zx + pad, d1 + pad, d2 + pad]
    col_right = [d1 - pad, d2 - pad, zx + zw - pad]
    col_w = sum(col_right[i] - col_x[i] for i in range(3)) / 3.0

    # Columns chain off the byline (ctx["article_top_y"], set by the headline)
    # rather than a fixed top, so growing the headline never opens a gap. The
    # bottom stays fixed, just above the baked rule at 0.8233.
    zone_bottom = zy + zh
    top = ctx.get("article_top_y", zy + cfg["article_pad_top_frac"] * H)
    avail_h = zone_bottom - top - cfg["article_pad_bottom_frac"] * H

    paragraphs = [p.strip() for p in sanitise(data["lead_article"]).split("\n\n") if p.strip()]
    # Bind the last two words of every paragraph so no paragraph (and therefore
    # no column) ends on a lone word.
    hyph = [prevent_orphan(soft_hyphenate(p, cfg), words=2) for p in paragraphs]
    # One continuous flow, as a real broadsheet sets it: paragraphs separated by
    # newlines with a first-line indent (no vertical gaps), text breaking across
    # columns mid-paragraph wherever the line boundaries fall.
    text = "\n".join(hyph)

    layout = make_layout(cr)
    layout.set_width(int(col_w * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_justify(True)
    layout.set_justify_last_line(False)  # last line of a paragraph stays left

    def reflow(size):
        set_font(layout, cfg["font_body"], size)
        layout.set_indent(int(round(1.4 * size * SCALE)))
        layout.set_attributes(build_attrs(
            text, leading_px=cfg["lead_body"] * size, language=lang, insert_hyphens=True))
        layout.set_text(text, -1)

    def line_bounds():
        """(top, bottom) of each line's logical extent, in pixels, in flow order."""
        bounds = []
        it = layout.get_iter()
        while True:
            l = it.get_line_extents()[1]
            bounds.append((l.y / SCALE, (l.y + l.height) / SCALE))
            if not it.next_line():
                break
        return bounds

    def column_split():
        """Cut the flow into three runs of whole lines. All lines share one
        leading, so an even line-count split is optimal: columns differ by at
        most one line, and when the count is not divisible by three the short
        column falls last, as in a traditional broadsheet."""
        b = line_bounds()
        n = len(b)
        base, rem = divmod(n, 3)
        counts = [base + (1 if i < rem else 0) for i in range(3)]
        c1, c2 = counts[0], counts[0] + counts[1]
        runs = [(0, c1), (c1, c2), (c2, n)]
        heights = [b[e - 1][1] - b[s][0] for s, e in runs]
        return b, runs, heights

    def feasible(size):
        reflow(size)
        return max(column_split()[2]) <= avail_h

    size, fits = largest_fitting(feasible, cfg["body_size_min"] * H, cfg["body_size_max"] * H)
    reflow(size)
    bounds, runs, heights = column_split()
    leading = cfg["lead_body"] * size
    if not fits:
        print("[warn] lead article overflows the columns zone at the minimum body "
              "size; trim copy or enlarge the zone.")
    spread = max(heights) - min(heights)
    print(f"[info] column depths {[round(h, 1) for h in heights]}px; "
          f"spread {spread:.1f}px = {spread / leading:.2f} line heights")
    if spread > leading:
        print("[warn] column spread exceeds one line height.")

    # Each column draws the SAME flow layout, clipped to its run of lines and
    # shifted so the run's first line sits at the column top.
    for ci, (s, e) in enumerate(runs):
        y0, y1 = bounds[s][0], bounds[e - 1][1]
        cr.save()
        cr.rectangle(col_x[ci] - 0.9 * pad, top, col_w + 1.8 * pad, y1 - y0)
        cr.clip()
        draw_layout(cr, layout, col_x[ci], top - y0, ink)
        cr.restore()


# ----- Pull quote ------------------------------------------------------------


def render_pullquote(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["pullquote"]

    quote = "“" + sanitise(data["pull_quote_text"]) + "”"
    attrib = sanitise(keep_phrases_together(data["pull_quote_attribution"]))

    # Attribution first (fixed size) so we can reserve its space, then a gap,
    # before sizing the quote to fill the rest of this tall closing block.
    a_size = cfg["size_pullquote_attrib"] * H
    al = make_layout(cr)
    set_font(al, cfg["font_sans"], a_size)
    al.set_width(int(zw * SCALE))
    al.set_alignment(Pango.Alignment.CENTER)
    al.set_text(attrib, -1)
    ah = measure(al)[1]

    gap = H * 0.012
    clearance = H * 0.010
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

    # Size the quote to fill the block. It is the closing feature of the page, so
    # it is allowed to grow large; the block is tall enough to carry it.
    q_size, _ = largest_fitting(lambda s: q_height(s) <= quote_budget, 0.0090 * H, 0.046 * H)
    q_height(q_size)
    qh = measure(layout)[1]

    block_h = qh + gap + ah
    qy = zy + max(0.0, (zh - block_h) / 2.0)
    draw_layout(cr, layout, zx, qy, ink)
    draw_layout(cr, al, zx, qy + qh + gap, ink)
    # No engine-drawn rule: the baked rule at the top of the block is its only
    # border, and the faint baked sub-rule lower down is erased post-composite.


# ----- Stat box (on the tan fill) --------------------------------------------


def render_statbox(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["statbox"]

    number = sanitise(data["stat_number"])
    desc = keep_phrases_together(sanitise(data["stat_descriptor"]))
    source = sanitise(data["stat_source"])

    # Stat number: the single most dominant element on the page. Size it on its
    # INK extents (the real glyph bounds, not the line-height box) so it fills
    # the box. For a wide multi-digit number the box WIDTH is the binding limit;
    # for a narrow number the height limit applies.
    nl = make_layout(cr)
    nl.set_width(-1)

    def num_ink(size):
        set_font(nl, cfg["font_stat"], size)
        nl.set_text(number, -1)
        return nl.get_pixel_extents()[0]  # ink rect: x, y, width, height

    def num_ok(size):
        ink_r = num_ink(size)
        return (ink_r.width <= zw * cfg["stat_ink_width_frac"]
                and ink_r.height <= zh * cfg["stat_ink_height_frac"])

    nsize, _ = largest_fitting(num_ok, cfg["stat_size_min"] * H, cfg["stat_size_max"] * H)
    ink_r = num_ink(nsize)

    # Descriptor, sized down if needed so the number + descriptor + source still
    # fit the box height.
    gap1 = H * 0.010
    gap2 = H * 0.008
    src_h_est = cfg["size_stat_source"] * H * 1.4
    desc_budget = zh - ink_r.height - gap1 - gap2 - src_h_est - H * 0.02

    dl = make_layout(cr)
    dl.set_width(int(zw * 0.92 * SCALE))
    dl.set_alignment(Pango.Alignment.CENTER)

    def desc_h(size):
        set_font(dl, cfg["font_body"], size)
        # Leading matched proportionally to the body (1.20); the descriptor was
        # noticeably looser than the rest of the page at 1.18 of a large size.
        dl.set_attributes(build_attrs(desc, leading_px=1.06 * size))
        dl.set_text(desc, -1)
        return measure(dl)[1]

    dsize, _ = largest_fitting(
        lambda s: desc_h(s) <= desc_budget, 0.010 * H, cfg["size_stat_descriptor"] * H)
    desc_h(dsize)
    dw, dh = measure(dl)

    sl = make_layout(cr)
    set_font(sl, cfg["font_sans"], cfg["size_stat_source"] * H)
    sl.set_width(int(zw * 0.92 * SCALE))
    sl.set_alignment(Pango.Alignment.CENTER)
    sl.set_text(source, -1)
    sw, sh = measure(sl)

    block_h = ink_r.height + gap1 + dh + gap2 + sh
    y = zy + max(0.0, (zh - block_h) / 2.0)
    # The ink rect starts ink_r.y below the layout origin; offset so the glyphs
    # (not the line box) align to y.
    draw_layout(cr, nl, zx + (zw - ink_r.width) / 2.0 - ink_r.x, y - ink_r.y, ink)
    y += ink_r.height + gap1
    draw_layout(cr, dl, zx + (zw - dw) / 2.0, y, ink)
    y += dh + gap2
    draw_layout(cr, sl, zx + (zw - sw) / 2.0, y, ink)


# ----- Sidebar stories (two explicit zones, each filled) ---------------------


def _sidebar_headline_layout(cr, ctx, zw, head, size):
    cfg = ctx["cfg"]
    hl = make_layout(cr)
    hl.set_width(int(zw * SCALE))
    hl.set_wrap(Pango.WrapMode.WORD)
    set_font(hl, cfg["font_sidebar_head"], size)
    hl.set_attributes(build_attrs(head, leading_px=1.08 * size))
    hl.set_text(head, -1)
    return hl


def _sidebar_body_layout(cr, ctx, zw, body, size):
    cfg = ctx["cfg"]
    layout = make_layout(cr)
    set_font(layout, cfg["font_body"], size)
    layout.set_width(int(zw * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_justify(True)
    layout.set_justify_last_line(False)  # last line stays left aligned
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_attributes(build_attrs(
        body, leading_px=cfg["lead_sidebar"] * size,
        language=cfg["hyphenation_lang"], insert_hyphens=True))
    layout.set_text(body, -1)
    return layout


def render_sidebar(cr, ctx, data):
    """Both sidebar stories share ONE headline size and ONE body size, computed
    from sidebar 1, and both render at a single fixed leading. Text is NEVER
    stretched or feathered to fill. Each story ends where its content ends; the
    only thing that adapts is sidebar 2's zone foot, which is reported as the
    content end plus a small pad (~2 line heights). Short copy therefore leaves
    honest page-margin whitespace below, not an obviously underfilled zone; long
    copy (80-100 words) simply runs deeper. Returns sidebar 2's content-end y so
    the caller knows the true bottom of the filled area."""
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]

    stories = []
    for n in (1, 2):
        zone = ctx["px"][f"sidebar_{n}"]
        stories.append((
            zone,
            sanitise(data[f"sidebar_{n}_headline"]),
            sanitise(data[f"sidebar_{n}_byline"]),
            soft_hyphenate(sanitise(data[f"sidebar_{n}_body"]), cfg),
        ))

    byline_size = cfg["size_sidebar_byline"] * H
    gap_hb = H * 0.005
    gap_bb = H * 0.008

    # Shared headline size: large, real section-headline scale. Largest that fits
    # BOTH headlines within sidebar_head_frac of each zone's height and its width.
    def head_fits_all(size):
        for zone, head, _, _ in stories:
            zw, zh = zone[2], zone[3]
            if measure(_sidebar_headline_layout(cr, ctx, zw, head, size))[1] > zh * cfg["sidebar_head_frac"]:
                return False
        return True

    head_size, _ = largest_fitting(
        head_fits_all, cfg["sidebar_head_min"] * H, cfg["sidebar_head_max"] * H)

    # Shared body size: largest size at which sidebar 1's body fills its zone at
    # the fixed leading. Applied unchanged to both stories. No feathering, no
    # resizing to fill: word spacing stays even and copy is never distorted.
    z1 = stories[0][0]
    h1 = measure(_sidebar_headline_layout(cr, ctx, z1[2], stories[0][1], head_size))[1]
    body1_avail = (z1[1] + z1[3]) - (z1[1] + h1 + gap_hb + byline_size + gap_bb)
    body_size, _ = largest_fitting(
        lambda s: measure(_sidebar_body_layout(cr, ctx, z1[2], stories[0][3], s))[1] <= body1_avail,
        cfg["sidebar_body_min"] * H, cfg["sidebar_body_max"] * H)

    # One line height, the unit the rule and sidebar 2 chain off.
    line_h = cfg["lead_sidebar"] * body_size

    def draw_story(zone_x, zone_w, top_y, head, byline, body):
        """Draw a sidebar story from top_y and return the y of its last inked
        pixel (the actual rendered content end at the final font sizes, NOT an
        estimate from the zone), so the rule and the next story chain off real
        geometry and can never collide."""
        hl = _sidebar_headline_layout(cr, ctx, zone_w, head, head_size)
        hh = measure(hl)[1]
        bl = make_layout(cr)
        set_font(bl, cfg["font_sans"], byline_size)
        bl.set_width(int(zone_w * SCALE))
        bl.set_text(byline, -1)
        bh = measure(bl)[1]
        body_lay = _sidebar_body_layout(cr, ctx, zone_w, body, body_size)
        body_top = top_y + hh + gap_hb + bh + gap_bb
        draw_layout(cr, hl, zone_x, top_y, ink)
        draw_layout(cr, bl, zone_x, top_y + hh + gap_hb, ink)
        draw_layout(cr, body_lay, zone_x, body_top, ink)
        # Last inked pixel of the body = body_top + (ink rect top + ink height).
        body_ink = body_lay.get_pixel_extents()[0]
        return body_top + body_ink.y + body_ink.height

    (zf1, head1, by1, body1), (zf2, head2, by2, body2) = stories
    end1 = draw_story(zf1[0], zf1[2], zf1[1], head1, by1, body1)

    # Engine-owned divider rule: the baked one was inpainted out of the template
    # (its fixed position collides with variable-length content), and is redrawn
    # post-composite ONE FULL LINE below sidebar 1's actual last inked pixel,
    # matched to the baked rules' RGB and weight. Sidebar 2 starts 1.5 line
    # heights below the rule. Both gaps are measured from real ink, so larger
    # headlines or an extra hyphenated line can never close them.
    rule_y = end1 + line_h
    ctx["sidebar_rule"] = (zf2[0], zf2[0] + zf2[2], rule_y)

    end2 = draw_story(zf2[0], zf2[2], rule_y + 1.5 * line_h, head2, by2, body2)
    # Sidebar 2's adaptive zone foot: its last inked pixel plus ~2 line heights.
    # Anything placed below the rail (none at present) must clear this.
    ctx["sidebar2_zone_end"] = end2 + 2.0 * line_h


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
    text = soft_hyphenate(sanitise(data["kicker_text"]), cfg)

    pad = cfg["col_pad_frac"] * ctx["W"]
    d1 = cfg["col_divider_1_x"] * ctx["W"]
    d2 = cfg["col_divider_2_x"] * ctx["W"]
    col_x = [zx + pad, d1 + pad, d2 + pad]
    col_right = [d1 - pad, d2 - pad, zx + zw - pad]
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


def _lum(p):
    return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]


def sample_rule(template_rgb, cfg, W, H):
    """Measure the baked pull-quote top rule's exact colour and thickness so the
    lower rule we draw is identical. At many x positions we find the dark run
    crossing the rule's y, then average its colour and median its thickness."""
    y = int(round(cfg["pullquote_top_rule_y"] * H))
    x0 = int(round(cfg["zones"]["pullquote"][0] * W))
    x1 = int(round((cfg["zones"]["pullquote"][0] + cfg["zones"]["pullquote"][2]) * W))
    px = template_rgb.load()
    span = max(6, int(round(0.004 * H)))  # vertical search window around the rule

    # Background luminance from just above the rule.
    bg_samples = [_lum(px[xx, max(0, y - span)])
                  for xx in range(max(0, x0), min(W, x1), max(1, (x1 - x0) // 100))]
    if not bg_samples:
        return cfg["lower_rule_default"], max(1.0, cfg["hairline_frac"] * H)
    bg = sorted(bg_samples)[len(bg_samples) // 2]

    thicknesses, colours = [], []
    for xx in range(max(0, x0), min(W, x1), max(1, (x1 - x0) // 240)):
        col = [(yy, px[xx, yy]) for yy in range(max(0, y - span), min(H, y + span + 1))]
        darkest = min(col, key=lambda t: _lum(t[1]))
        if _lum(darkest[1]) > bg - 25:
            continue  # no rule here (e.g. a gap)
        thr = (bg + _lum(darkest[1])) / 2.0
        run = [p for (yy, p) in col if _lum(p) <= thr]
        if run:
            thicknesses.append(len(run))
            colours.extend(run)
    if not colours:
        return cfg["lower_rule_default"], max(1.0, cfg["hairline_frac"] * H)
    r = sum(c[0] for c in colours) / len(colours) / 255.0
    g = sum(c[1] for c in colours) / len(colours) / 255.0
    b = sum(c[2] for c in colours) / len(colours) / 255.0
    weight = max(1.0, sorted(thicknesses)[len(thicknesses) // 2])
    return (r, g, b), weight


# ---------------------------------------------------------------------------
# Compositing.
# ---------------------------------------------------------------------------


def cairo_to_pil(surface):
    surface.flush()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def composite(template_rgb, text_rgba, cfg, W, ink_blur=None):
    if ink_blur is None:
        blur = min(cfg["ink_blur_cap"], cfg["text_blur_px"] * (W / float(cfg["blur_reference_width"])))
    else:
        blur = ink_blur
    if blur > 0:
        text_rgba = text_rgba.filter(ImageFilter.GaussianBlur(blur))
    white = Image.new("RGBA", template_rgb.size, (255, 255, 255, 255))
    ink_map = Image.alpha_composite(white, text_rgba).convert("RGB")
    return ImageChops.multiply(template_rgb, ink_map)


def draw_rule_on_image(image_rgb, ctx, x0, x1, y):
    """Draw a structural rule directly onto the composited image at the exact
    RGB and thickness sampled from the baked rules, so it is identical to them
    (the multiply/blur ink pipeline is bypassed for structural rules)."""
    from PIL import ImageDraw
    colour = tuple(int(round(c * 255)) for c in ctx["rule_colour"])
    width = max(1, int(round(ctx["rule_weight"])))
    d = ImageDraw.Draw(image_rgb)
    d.line([(int(round(x0)), int(round(y))), (int(round(x1)), int(round(y)))],
           fill=colour, width=width)
    return image_rgb


def erase_h_rule(template_rgb, W, H, y_frac, x0_frac, x1_frac):
    """Inpaint a baked horizontal rule out of the template by cloning a strip of
    clean newsprint texture from just above it over its location. Done on the
    TEMPLATE before text is composited."""
    y = int(round(y_frac * H))
    x0 = int(round(x0_frac * W))
    x1 = int(round(x1_frac * W))
    band = max(3, int(round(0.006 * H)))      # rows to overwrite, centred on rule
    top = y - band // 2
    src_top = top - band - max(2, int(round(0.003 * H)))  # clean paper above
    if src_top < 0:
        return template_rgb
    strip = template_rgb.crop((x0, src_top, x1, src_top + band))
    template_rgb.paste(strip, (x0, top))
    return template_rgb


def trim_edge_frame(image_rgb, cfg):
    """Remove the 1px dark halo a LANCZOS resize leaves at the image border by
    cloning the outer ring inward. The print is cut to the image edge, so there
    must be no outline. Top-edge cloning stays within the khaki masthead band, so
    its colour is preserved."""
    W, H = image_rgb.size
    r = max(2, int(round(0.0009 * W)))
    px = image_rgb.load()
    # Top and bottom rings.
    top_src = image_rgb.crop((0, r, W, r + 1)).resize((W, r))
    image_rgb.paste(top_src, (0, 0))
    bot_src = image_rgb.crop((0, H - r - 1, W, H - r)).resize((W, r))
    image_rgb.paste(bot_src, (0, H - r))
    # Left and right rings.
    left_src = image_rgb.crop((r, 0, r + 1, H)).resize((r, H))
    image_rgb.paste(left_src, (0, 0))
    right_src = image_rgb.crop((W - r - 1, 0, W - r, H)).resize((r, H))
    image_rgb.paste(right_src, (W - r, 0))
    return image_rgb


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


def finalize_output(result, cfg, print_dpi, print_size):
    """Optionally downsample the finished page to an exact print resolution.
    Rendering large (e.g. an 8x template) and scaling the FINAL composite down to
    300 DPI supersamples the glyph edges and paper texture, which is cleaner than
    rendering at the target size directly. Embeds the DPI in the PNG."""
    if print_size:
        tw, th = print_size
    elif print_dpi:
        mm = cfg["a2_mm"]
        tw = int(round(mm[0] / 25.4 * print_dpi))
        th = int(round(mm[1] / 25.4 * print_dpi))
    else:
        return result, None

    src_ar = result.width / result.height
    dst_ar = tw / th
    if abs(src_ar - dst_ar) > 0.01:
        print(f"[warn] template aspect ratio {src_ar:.4f} differs from the print "
              f"target {dst_ar:.4f}; the downsample will distort slightly.")
    if (tw, th) != result.size:
        scale = tw / result.width
        print(f"[info] downsampling {result.width}x{result.height} -> {tw}x{th} "
              f"({scale:.2f}x) for print" + (f" at {print_dpi} DPI" if print_dpi else ""))
        result = result.resize((tw, th), Image.LANCZOS)
    return result, print_dpi


def build(template_path, data_path, output_path, cfg=CONFIG,
          print_dpi=None, print_size=None, render_dpi=None, render_size=None,
          ink_blur=None):
    with open(data_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    validate(data, cfg)

    template = Image.open(template_path).convert("RGB")
    print(f"[info] template {os.path.basename(template_path)}: "
          f"{template.width}x{template.height}px")

    # Optionally upscale a small template to the print size BEFORE rendering, so
    # the text is drawn crisp at full resolution even from a small input. Only
    # the paper texture is interpolated (it is just grain); the glyphs are not.
    if render_size or render_dpi:
        if render_size:
            tw, th = render_size
        else:
            mm = cfg["a2_mm"]
            tw = int(round(mm[0] / 25.4 * render_dpi))
            th = int(round(mm[1] / 25.4 * render_dpi))
        if (tw, th) != template.size:
            print(f"[info] upscaling template {template.width}x{template.height} -> "
                  f"{tw}x{th} before rendering" + (f" ({render_dpi} DPI)" if render_dpi else ""))
            template = template.resize((tw, th), Image.LANCZOS)
    W, H = template.size

    # Inpaint baked rules the engine replaces or retires: the faint sub-rule in
    # the bottom block (the pull-quote feature owns that space) and the fixed
    # inter-sidebar rule (redrawn adaptively after sidebar 1's content).
    erase_h_rule(template, W, H, cfg["sub_rule_y"], 0.0, cfg["sub_rule_x_end"])
    erase_h_rule(template, W, H, cfg["sidebar_rule_y"],
                 cfg["sidebar_rule_x0"], cfg["sidebar_rule_x1"])

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surface)
    rule_colour, rule_weight = sample_rule(template, cfg, W, H)
    ctx = {"W": W, "H": H, "cfg": cfg, "px": resolve_zones(cfg, W, H),
           "rule_colour": rule_colour, "rule_weight": rule_weight}

    render_masthead(cr, ctx, data)
    render_headline_and_byline(cr, ctx, data)
    render_article(cr, ctx, data)
    render_pullquote(cr, ctx, data)
    render_statbox(cr, ctx, data)
    render_sidebar(cr, ctx, data)
    render_kicker(cr, ctx, data)

    text_rgba = cairo_to_pil(surface)
    result = composite(template, text_rgba, cfg, W, ink_blur=ink_blur)
    if "sidebar_rule" in ctx:
        rx0, rx1, ry = ctx["sidebar_rule"]
        result = draw_rule_on_image(result, ctx, rx0, rx1, ry)
    result = place_logo(result, ctx)

    result, dpi = finalize_output(result, cfg, print_dpi, print_size)
    result = trim_edge_frame(result, cfg)  # last: kill any resize edge halo
    save_kwargs = {"dpi": (dpi, dpi)} if dpi else {}
    result.save(output_path, **save_kwargs)
    print(f"[done] wrote {output_path} ({result.width}x{result.height}px)")


def main():
    ap = argparse.ArgumentParser(
        description="Place structured newspaper copy onto a blank template.")
    ap.add_argument("--template", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--print-dpi", type=int, default=None,
                    help="downsample the finished page to A2 at this DPI "
                         "(e.g. 300 -> 4961x7016) and tag the PNG")
    ap.add_argument("--print-size", default=None,
                    help="explicit WIDTHxHEIGHT to downsample to, overrides --print-dpi")
    ap.add_argument("--render-dpi", type=int, default=None,
                    help="upscale the template to A2 at this DPI BEFORE rendering, so "
                         "a small template yields crisp text at full print size "
                         "(e.g. 300 -> render at 4961x7016)")
    ap.add_argument("--render-size", default=None,
                    help="explicit WIDTHxHEIGHT to upscale the template to before rendering")
    ap.add_argument("--ink-blur", type=float, default=None,
                    help="ink-spread blur in px on the text (0 = razor sharp). "
                         "Default is a faint, capped blur for newsprint realism.")
    args = ap.parse_args()

    def parse_size(s):
        if not s:
            return None
        w, h = s.lower().split("x")
        return (int(w), int(h))

    build(args.template, args.data, args.output,
          print_dpi=args.print_dpi, print_size=parse_size(args.print_size),
          render_dpi=args.render_dpi, render_size=parse_size(args.render_size),
          ink_blur=args.ink_blur)


if __name__ == "__main__":
    main()
