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
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageFilter  # noqa: E402

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
    # Body-size range as a fraction of page height. sidebar_body_min is the
    # PREFERRED floor (the reference-zone size never drops below it, which keeps
    # the established look). sidebar_body_abs_min is a lower readability floor the
    # rail may shrink to ONLY when several long stories would otherwise overflow
    # the rail; copy is never altered, only the shared type size.
    "sidebar_body_min": 0.0095,
    "sidebar_body_abs_min": 0.0060,
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
    # Baked rule under the headline block (the standfirst rule, article width).
    # It is inpainted out and redrawn adaptively one line below the byline, so
    # the headline can grow past its fixed position and the columns chain off it.
    "standfirst_rule_y": 0.1767,
    "standfirst_rule_x0": 0.000,
    "standfirst_rule_x1": 0.700,
    # Top-of-page vertical chain (fractions of H). The headline fills from below
    # the edition line down to headline_zone_bottom; the columns then chain off
    # the byline, so the headline is big and nothing is cramped.
    "masthead_edition_gap": 0.008,   # masthead glyphs -> edition line
    "edition_headline_gap": 0.010,   # edition line -> headline
    "headline_zone_bottom": 0.2080,  # headline grows to fill down to here
    "byline_rule_gap": 0.009,        # byline -> standfirst rule
    "rule_article_gap": 0.013,       # standfirst rule -> first column line
    # Edition line letterspacing (fraction of its size) when set in spaced caps.
    "edition_tracking": 0.10,
    # Right boundary of the masthead/edition row (the sidebar divider x). Nothing
    # in that row may cross into the rail.
    "rail_boundary_x": 0.693,
    # Rail vertical justification. The stat box, sidebar 1 and sidebar 2 render at
    # natural heights; the surplus down to the pull-quote bottom is split evenly
    # into the two gaps. If a gap would exceed gap_cap_frac of page height, the
    # sidebar body leading is loosened (up to sidebar_lead_stretch_max) and the
    # rail re-distributed so it never looks scattered.
    "rail_bottom_y": 0.9700,        # bottom of the main content (pull-quote block)
    "rail_gap_cap_frac": 0.080,
    "sidebar_lead_stretch_max": 1.15,
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
    # Print production. Bleed extends the artwork past the trim so a cut that
    # drifts never exposes a white sliver; crop marks show the trim corners in
    # the bleed margin. Both are OFF by default (exact-A2 output); enable per run
    # with --bleed-mm / --crop-marks once the print supplier's spec is known.
    "crop_mark_mm": 4.0,          # crop mark length
    "crop_mark_weight_mm": 0.12,  # crop mark stroke weight
    "crop_mark_gap_mm": 1.5,      # gap between the trim corner and the mark
    # Validation --------------------------------------------------------------
    "lead_words_min": 580,
    "lead_words_max": 660,
    # Folio row (edition line + date) spans the full page width with a hairline
    # rule beneath it. The stat box and the whole rail are shifted DOWN by
    # rail_shift so the rail starts below the folio (clearing the date), aligning
    # the rail top with the headline top. The baked tan box is moved to match.
    "rail_shift": 0.0200,
    "folio_rule_gap": 0.0042,   # folio baseline -> folio rule
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


def smarten_quotes(text):
    """Convert straight quotes, apostrophes and triple-dots to typographic forms
    so headlines and body match the curly quotes the engine sets on the pull
    quote. A straight quote OPENS when it begins the string or follows whitespace
    or an opening bracket; otherwise it CLOSES (and a single quote before a digit
    or after a letter is an apostrophe, e.g. "don't", "the '90s")."""
    text = text.replace("...", "…")  # ellipsis
    opener_prev = " \t\n([{‘“"  # start-like contexts
    out = []
    for i, ch in enumerate(text):
        if ch == '"':
            prev = text[i - 1] if i > 0 else ""
            out.append("“" if (prev == "" or prev in opener_prev) else "”")
        elif ch == "'":
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            opening = (prev == "" or prev in opener_prev) and not nxt.isdigit()
            out.append("‘" if opening else "’")
        else:
            out.append(ch)
    return "".join(out)


def sanitise(text):
    """Rendering copy rules: no em dashes, and straight quotes/apostrophes/triple
    dots normalised to typographic forms so every field matches the curly quotes
    the engine sets on the pull quote."""
    if text is None:
        return ""
    for ch in ("—", "―", "‒", "⸺", "⸻"):
        text = text.replace(" " + ch + " ", ", ").replace(ch, ", ")
    text = text.replace(" -- ", ", ").replace("--", ", ")
    return smarten_quotes(text)


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


# Short function words that must never sit alone on a line in a centred, narrow
# block (the stat descriptor wrapped "Cold calls made / by / Cognism SDRs").
_CONNECTORS = frozenset(
    "a an and as at but by for from in into nor of on or over per so the to "
    "under via with".split())


def bind_connectors(text):
    """Attach short lowercase function words to the FOLLOWING word with a
    non-breaking space ("by Cognism", "in 2025"), so a wrap can never strand a
    connector alone on its own line. Runs after keep_phrases_together, whose
    NBSPs stay intact because splitting here is on plain spaces only."""
    if not text:
        return text
    nbsp = chr(0x00A0)
    tokens = text.split(" ")
    out = []
    for i, t in enumerate(tokens):
        if out and t and tokens[i - 1].lower() in _CONNECTORS:
            out[-1] += nbsp + t
        else:
            out.append(t)
    return " ".join(out)


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
        # Gate on the LETTER count, not the raw token length: trailing punctuation
        # (e.g. "Reuters," is 8 chars but 7 letters) must not push a short proper
        # noun over the minimum and get it hyphenated.
        letters = sum(c.isalpha() for c in t)
        if t and i != last and letters >= min_len:
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

    # Folio row: edition line + date as ONE continuous row spanning the FULL page
    # width, sharing an exact text baseline. Edition left-aligned at the left
    # margin, date right-aligned at the RIGHT page margin. A hairline rule runs
    # the full width beneath it (drawn post-composite, matched to the baked rules).
    line_y = band_bot + cfg["masthead_edition_gap"] * H
    right_edge = ctx["W"] - margin
    content_w = right_edge - margin

    # The folio is ONE row of equal weight: edition line and date are both set in
    # spaced capitals at a single shared size (same font, same letterspacing), so
    # the date reads with the same weight as the edition caps rather than looking
    # lighter. Both share one baseline; edition left, date right.
    edition = sanitise(data["edition_line"]).upper()
    date = sanitise(data["date"]).upper()

    def folio_layout(text, s):
        lay = make_layout(cr)
        set_font(lay, cfg["font_sans"], s)
        lay.set_attributes(build_attrs(text, letter_spacing_px=cfg["edition_tracking"] * s))
        lay.set_width(-1)
        lay.set_text(text, -1)
        return lay

    def folio_total(s):  # both lines plus a minimum separating gap
        return measure(folio_layout(edition, s))[0] + 0.04 * ctx["W"] + measure(folio_layout(date, s))[0]

    # The edition line is left-aligned and must clear the baked vertical rail rule
    # (the date sits to its right, past the rule, where there is no rule to hit).
    # Constrain the shared folio size so the edition text ends before the rule;
    # without this, a long dateline overruns and "BERLIN" lands on the rule.
    rail_rule_x = cfg["rail_boundary_x"] * ctx["W"]
    edition_max_w = rail_rule_x - margin - cfg["col_pad_frac"] * ctx["W"]

    def folio_fits(s):
        return (measure(folio_layout(edition, s))[0] <= edition_max_w
                and folio_total(s) <= content_w)

    folio_size = sub_size
    if not folio_fits(sub_size):
        folio_size, _ = largest_fitting(folio_fits, 0.0060 * H, sub_size)

    el = folio_layout(edition, folio_size)
    dl = folio_layout(date, folio_size)
    dw = measure(dl)[0]

    # Align both on one baseline: draw each so its first-line baseline lands on
    # baseline_y. (Top-aligning would split the baselines if the sizes differed.)
    el_asc = el.get_baseline() / SCALE
    dl_asc = dl.get_baseline() / SCALE
    baseline_y = line_y + max(el_asc, dl_asc)
    draw_layout(cr, el, margin, baseline_y - el_asc, ink)
    draw_layout(cr, dl, right_edge - dw, baseline_y - dl_asc, ink)
    # Record the edition's right edge so run_checks can guard against any future
    # overrun across the rail rule.
    ctx["edition_right_x"] = margin + measure(el)[0]
    ctx["rail_rule_x"] = rail_rule_x

    # The headline chains off the bottom of this row.
    ctx["edition_bottom"] = max(baseline_y - el_asc + measure(el)[1],
                                baseline_y - dl_asc + measure(dl)[1])
    # Full-width hairline rule beneath the folio, separating it from all content.
    folio_rule_y = ctx["edition_bottom"] + cfg["folio_rule_gap"] * H
    ctx["folio_rule"] = (margin, right_edge, folio_rule_y)


def render_headline_and_byline(cr, ctx, data):
    """Headline grown to FILL the band from its zone top down to the standfirst
    rule, in two or three balanced lines. The byline (bold) chains directly below
    the headline's last line, and the lead columns chain directly below the
    byline: no fixed gaps, so the top of the page is loud with no dead space.
    Stores the article top (where the columns begin) in ctx."""
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    hx, hy, hw, hh = ctx["px"]["headline"]

    # Chain off the edition line; fill down to the headline zone bottom. The
    # standfirst rule (inpainted from the template) is redrawn below the byline
    # and the columns chain below that, so the headline gets full room.
    region_top = ctx.get("edition_bottom", hy) + cfg["edition_headline_gap"] * H
    region_bottom = cfg["headline_zone_bottom"] * H

    # Bind the trailing phrase so the last line is full enough to satisfy the
    # balanced-line rule at the three-line fill size (otherwise a too-short last
    # line forces the balancer to shrink the headline back to two lines).
    text = prevent_orphan(sanitise(data["headline"]), words=3)
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
    ctx["headline_fits"] = fits
    ctx["headline_balanced"] = balanced()

    # Chain: headline at region top, byline directly beneath, then the redrawn
    # standfirst rule, then the columns, each measured off the previous element's
    # real bottom so nothing crowds.
    draw_layout(cr, layout, hx, region_top, ink)
    byline_y = region_top + head_h + gap_hb
    draw_layout(cr, byl, hx, byline_y, ink)
    byline_bottom = byline_y + byl_h
    ctx["byline_bottom"] = byline_bottom
    rule_y = byline_bottom + cfg["byline_rule_gap"] * H
    ctx["standfirst_rule"] = (
        cfg["standfirst_rule_x0"] * ctx["W"], cfg["standfirst_rule_x1"] * ctx["W"], rule_y)
    ctx["article_top_y"] = rule_y + cfg["rule_article_gap"] * H


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
    ctx["columns_fit"] = fits
    ctx["columns_spread_lines"] = spread / leading if leading else 0
    ctx["column_foot"] = top + max(heights)
    ctx["columns_bottom"] = zone_bottom

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
    attrib = bind_connectors(sanitise(keep_phrases_together(data["pull_quote_attribution"])))

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
    # Record the pull-quote block's bottom so the rail can justify down to it.
    ctx["pullquote_bottom"] = qy + qh + gap + ah


# ----- Stat box (on the tan fill) --------------------------------------------


def render_statbox(cr, ctx, data):
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]
    zx, zy, zw, zh = ctx["px"]["statbox"]

    number = sanitise(data["stat_number"])
    desc = bind_connectors(keep_phrases_together(sanitise(data["stat_descriptor"])))
    source = sanitise(data.get("stat_source", ""))  # optional

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
    # fit the box height. The source line is OPTIONAL: when absent it reserves no
    # space and is not drawn, so the number + descriptor re-centre on their own.
    has_source = bool(source)
    gap1 = H * 0.010
    gap2 = H * 0.008 if has_source else 0.0
    src_h_est = cfg["size_stat_source"] * H * 1.4 if has_source else 0.0
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

    sl, sw, sh = None, 0.0, 0.0
    if has_source:
        sl = make_layout(cr)
        set_font(sl, cfg["font_sans"], cfg["size_stat_source"] * H)
        sl.set_width(int(zw * 0.92 * SCALE))
        sl.set_alignment(Pango.Alignment.CENTER)
        sl.set_text(source, -1)
        sw, sh = measure(sl)

    block_h = ink_r.height + gap1 + dh + gap2 + sh
    y = zy + max(0.0, (zh - block_h) / 2.0)
    ctx["statbox_top"] = zy
    ctx["statbox_overflow"] = block_h > zh
    # The ink rect starts ink_r.y below the layout origin; offset so the glyphs
    # (not the line box) align to y.
    draw_layout(cr, nl, zx + (zw - ink_r.width) / 2.0 - ink_r.x, y - ink_r.y, ink)
    y += ink_r.height + gap1
    draw_layout(cr, dl, zx + (zw - dw) / 2.0, y, ink)
    if has_source:
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


def _sidebar_body_layout(cr, ctx, zw, body, size, lead_mult=1.0):
    cfg = ctx["cfg"]
    layout = make_layout(cr)
    set_font(layout, cfg["font_body"], size)
    layout.set_width(int(zw * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_justify(True)
    layout.set_justify_last_line(False)  # last line stays left aligned
    layout.set_alignment(Pango.Alignment.LEFT)
    layout.set_attributes(build_attrs(
        body, leading_px=cfg["lead_sidebar"] * size * lead_mult,
        language=cfg["hyphenation_lang"], insert_hyphens=True))
    layout.set_text(body, -1)
    return layout


def collect_sidebar_stories(data):
    """Gather 1 to 4 sidebar stories from sidebar_N_headline/byline/body fields,
    in order, stopping at the first missing headline. Backward compatible: a file
    with only sidebar_1/sidebar_2 yields two stories."""
    out = []
    for n in range(1, 5):
        head = data.get(f"sidebar_{n}_headline")
        if not head:
            break
        out.append((head, data.get(f"sidebar_{n}_byline", ""), data.get(f"sidebar_{n}_body", "")))
    return out


def render_sidebar(cr, ctx, data):
    """Render an array of 1 to 4 sidebar stories in the right rail, each sharing
    ONE headline size and ONE body size (computed from the sidebar-1 reference
    zone) at a single fixed leading. Text is never stretched or feathered to fill.

    The rail is justified vertically: the stat box is fixed (baked tan fill); the
    stories render at natural heights and the surplus down to the pull-quote
    block's bottom is split evenly into the gaps (stat box -> story 1, then one
    gap before each subsequent story). The last story's foot lands on the pull-
    quote bottom, so surplus reads as editorial spacing, not as a hole below the
    last story. Inter-story rules are centred in each between-story gap. If the
    gaps would exceed the cap, the body leading is loosened (<=1.15x) and the rail
    re-distributed; more stories or longer copy simply shrink the gaps."""
    H, cfg = ctx["H"], ctx["cfg"]
    ink = cfg["ink"]

    raw = collect_sidebar_stories(data)
    if not raw:
        return
    stories = [(sanitise(h), sanitise(b), soft_hyphenate(sanitise(body), cfg))
               for h, b, body in raw]

    # Shared rail geometry comes from the sidebar-1 reference zone.
    ref = ctx["px"]["sidebar_1"]
    rail_x, ref_top, rail_w, ref_h = ref
    byline_size = cfg["size_sidebar_byline"] * H
    gap_hb = H * 0.005
    gap_bb = H * 0.008

    # Shared headline size: largest that fits EVERY headline within the reference
    # zone's headline budget and the rail width.
    def head_fits_all(size):
        for head, _, _ in stories:
            if measure(_sidebar_headline_layout(cr, ctx, rail_w, head, size))[1] > ref_h * cfg["sidebar_head_frac"]:
                return False
        return True

    head_size, _ = largest_fitting(
        head_fits_all, cfg["sidebar_head_min"] * H, cfg["sidebar_head_max"] * H)

    # Shared body size: largest at which the reference story's body fills the
    # reference zone at the fixed leading. Independent of story count, so the body
    # size stays constant whether the rail carries two stories or four.
    ref_head_h = measure(_sidebar_headline_layout(cr, ctx, rail_w, stories[0][0], head_size))[1]
    body_ref_avail = ref_h - (ref_head_h + gap_hb + byline_size + gap_bb)
    body_size, _ = largest_fitting(
        lambda s: measure(_sidebar_body_layout(cr, ctx, rail_w, stories[0][2], s))[1] <= body_ref_avail,
        cfg["sidebar_body_min"] * H, cfg["sidebar_body_max"] * H)

    def story_pieces(head, byline, body, lead_mult):
        """(headline_layout, hh, byline_layout, bh, body_layout, content_h) where
        content_h is top-of-story to last inked pixel at the final font sizes."""
        hl = _sidebar_headline_layout(cr, ctx, rail_w, head, head_size)
        hh = measure(hl)[1]
        bl = make_layout(cr)
        set_font(bl, cfg["font_sans"], byline_size)
        bl.set_width(int(rail_w * SCALE))
        bl.set_text(byline, -1)
        bh = measure(bl)[1]
        body_lay = _sidebar_body_layout(cr, ctx, rail_w, body, body_size, lead_mult)
        body_ink = body_lay.get_pixel_extents()[0]
        content_h = hh + gap_hb + bh + gap_bb + body_ink.y + body_ink.height
        return hl, hh, bl, bh, body_lay, content_h

    def draw_story(top_y, hl, hh, bl, bh, body_lay):
        draw_layout(cr, hl, rail_x, top_y, ink)
        draw_layout(cr, bl, rail_x, top_y + hh + gap_hb, ink)
        draw_layout(cr, body_lay, rail_x, top_y + hh + gap_hb + bh + gap_bb, ink)

    # --- Vertical justification of the rail -------------------------------------
    # N stories sit below the fixed stat box; the surplus down to the pull-quote
    # bottom splits evenly into N gaps (one before each story). The last story's
    # foot lands on the pull-quote bottom.
    n = len(stories)
    anchor_top = ref_top                      # just below the baked stat-box rule
    rail_bottom = ctx.get("pullquote_bottom", cfg["rail_bottom_y"] * H)
    rail_avail = rail_bottom - anchor_top

    # The shared body size from the reference zone assumes ONE short story. When
    # the rail carries several stories, or long ones, their natural combined
    # height can exceed the rail. The rail is bounded by the page, so it cannot
    # grow; instead shrink the shared body size (uniformly, never below the
    # readable floor) until the natural content plus one line of gap per story
    # fits. Copy is never altered, only the type size. The body size is never
    # enlarged past the reference, so short copy is unaffected.
    head_hs = [measure(_sidebar_headline_layout(cr, ctx, rail_w, h, head_size))[1]
               for h, _, _ in stories]

    def _byline_h(b):
        bl = make_layout(cr)
        set_font(bl, cfg["font_sans"], byline_size)
        bl.set_width(int(rail_w * SCALE))
        bl.set_text(b, -1)
        return measure(bl)[1]

    byl_hs = [_byline_h(b) for _, b, _ in stories]

    def natural_total(s):
        t = 0.0
        for i, (_, _, body) in enumerate(stories):
            bi = _sidebar_body_layout(cr, ctx, rail_w, body, s).get_pixel_extents()[0]
            t += head_hs[i] + gap_hb + byl_hs[i] + gap_bb + bi.y + bi.height
        return t

    def rail_fits(s):
        return natural_total(s) + n * (cfg["lead_sidebar"] * s) <= rail_avail

    if not rail_fits(body_size):
        fit_size, ok = largest_fitting(rail_fits, cfg["sidebar_body_abs_min"] * H, body_size)
        body_size = fit_size
        if not ok:
            print("[warn] rail copy does not fit even at the minimum body size; "
                  "the sidebar stories are too long for the rail.")
        else:
            print(f"[info] rail: body size reduced to {body_size/H:.4f}H so "
                  f"{n} stories fit the rail")

    gap_cap = cfg["rail_gap_cap_frac"] * H
    min_gap = cfg["lead_sidebar"] * body_size  # never tighter than one line

    lead_mult = 1.0
    while True:
        pieces = [story_pieces(h, b, body, lead_mult) for h, b, body in stories]
        total = sum(p[5] for p in pieces)
        gap = (rail_bottom - anchor_top - total) / n
        if gap <= gap_cap or lead_mult >= cfg["sidebar_lead_stretch_max"]:
            break
        lead_mult = min(cfg["sidebar_lead_stretch_max"], lead_mult * 1.04)
    gap = max(min_gap, gap)

    rules = []
    tops, feet = [], []
    y = anchor_top
    for i, p in enumerate(pieces):
        y += gap                              # gap before each story
        if i > 0:                             # rule centred in each between-story gap
            rules.append((rail_x, rail_x + rail_w, y - gap / 2.0))
        tops.append(y)
        draw_story(y, *p[:5])
        y += p[5]                             # advance past this story's content
        feet.append(y)
    ctx["sidebar_rules"] = rules
    ctx["sidebar_tops"] = tops
    ctx["sidebar_feet"] = feet
    ctx["sidebar_last_foot"] = y
    ctx["rail_gap"] = gap
    ctx["rail_gap_capped"] = gap > gap_cap
    ctx["rail_lead_mult"] = lead_mult
    ctx["rail_line_h"] = cfg["lead_sidebar"] * body_size
    print(f"[info] rail: stories={n} gap={gap/H:.4f}H lead_mult={lead_mult:.2f} "
          f"last foot={y/H:.4f} rail_bottom={rail_bottom/H:.4f}")


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


def shift_statbox(template_rgb, cfg, W, H):
    """Move the baked tan stat box DOWN by rail_shift so it begins below the folio
    row. The box (with its exact fill, border and texture) is copied and re-stamped
    rail_shift lower; the strip it vacated at the old top is inpainted with clean
    newsprint cloned from the blank area immediately to its left. The downward
    stamp also covers the baked rule that sat just under the old box."""
    shift = int(round(cfg["rail_shift"] * H))
    if shift <= 0:
        return template_rgb
    x0 = int(round(0.708 * W))
    x1 = int(round(0.982 * W))
    y0 = int(round(0.064 * H))
    y1 = int(round(0.297 * H))
    box = template_rgb.crop((x0, y0, x1, y1))
    template_rgb.paste(box, (x0, y0 + shift))         # box, shifted down
    w = x1 - x0
    src = template_rgb.crop((x0 - w, y0, x0, y0 + shift))  # blank newsprint to the left
    template_rgb.paste(src, (x0, y0))                 # cover the vacated top strip
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
    "sidebar_1_headline", "sidebar_1_byline", "sidebar_1_body",
]

# Expected word counts per field (inclusive). lead_article uses the cfg range.
# Outside-range counts are warnings (the engine still renders), not failures.
FIELD_WORD_RANGES = {
    "masthead_name": (1, 5),
    "edition_line": (4, 16),
    "date": (1, 4),
    "headline": (6, 16),
    "byline": (2, 6),
    "pull_quote_text": (10, 30),
    "pull_quote_attribution": (2, 7),
    "stat_number": (1, 1),
    "stat_descriptor": (3, 10),
    "stat_source": (2, 9),
    "kicker_text": (20, 120),
}
for _n in range(1, 5):
    FIELD_WORD_RANGES[f"sidebar_{_n}_headline"] = (4, 11)
    FIELD_WORD_RANGES[f"sidebar_{_n}_byline"] = (2, 4)
    # Production target is 60-80 words (see README); the check tolerates 40-110
    # before warning, because the rail adapts: short copy distributes into gaps,
    # long copy shrinks the shared body size to fit.
    FIELD_WORD_RANGES[f"sidebar_{_n}_body"] = (40, 110)


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


def run_checks(ctx, data, cfg):
    """Collect a pass/fail report from the rendered layout: word counts outside
    range, zones that overflow, rail gaps at the cap, and text colliding with a
    structural rule. Returns a list of (level, field, message), level in
    {'fail','warn','info'}. Geometry comes from the values the renderers stash in
    ctx, so this must run AFTER a full render."""
    H = ctx["H"]
    out = []
    add = lambda lvl, field, msg: out.append((lvl, field, msg))

    # --- Template surgery (rule erasure + stat-box shift) --------------------
    for lvl, field, msg in ctx.get("surgery_issues", []):
        add(lvl, field, msg)

    # --- Word counts ---------------------------------------------------------
    for field, (lo, hi) in FIELD_WORD_RANGES.items():
        if field not in data or data[field] in (None, ""):
            continue
        if field == "lead_article":
            lo, hi = cfg["lead_words_min"], cfg["lead_words_max"]
        wc = len(str(data[field]).split())
        if wc < lo:
            add("warn", field, f"{wc} words, below expected {lo}-{hi}")
        elif wc > hi:
            add("warn", field, f"{wc} words, above expected {lo}-{hi}")
    n = word_count(data.get("lead_article", ""))
    if n < cfg["lead_words_min"] or n > cfg["lead_words_max"]:
        add("warn", "lead_article", f"{n} words, outside {cfg['lead_words_min']}-{cfg['lead_words_max']}")

    # --- Zone fit ------------------------------------------------------------
    if ctx.get("headline_fits") is False:
        add("fail", "headline", "does not fit its band even at minimum size (truncated)")
    if ctx.get("headline_balanced") is False:
        add("warn", "headline", "lines remain ragged (uneven length) at the minimum size")
    if ctx.get("columns_fit") is False:
        add("fail", "lead_article", "overflows the three columns at the minimum body size; trim copy")
    if ctx.get("columns_spread_lines", 0) > 1.05:
        add("warn", "lead_article", f"columns end {ctx['columns_spread_lines']:.1f} line heights apart")
    cf, cb = ctx.get("column_foot"), ctx.get("columns_bottom")
    if cf and cb and cf > cb + 1:
        add("fail", "lead_article", "columns overflow past the column-zone bottom")
    if ctx.get("statbox_overflow"):
        add("warn", "stat_descriptor", "stat block is taller than the box; descriptor/source crowd the edges")

    # --- Rail gaps -----------------------------------------------------------
    if ctx.get("rail_gap_capped"):
        add("warn", "sidebar_*_body",
            f"rail gaps hit the {cfg['rail_gap_cap_frac']:.0%} cap even at max leading "
            f"(gap {ctx['rail_gap']/H:.1%} of page); add a story or lengthen bodies")

    # --- Structural-rule collisions ------------------------------------------
    sf = ctx.get("standfirst_rule")
    if sf:
        if ctx.get("byline_bottom") is not None and ctx["byline_bottom"] > sf[2] + 1:
            add("fail", "byline", "byline runs into the standfirst rule")
        if ctx.get("article_top_y") is not None and ctx["article_top_y"] < sf[2] - 1:
            add("fail", "lead_article", "columns start above the standfirst rule (rule cuts the text)")
    fr, st = ctx.get("folio_rule"), ctx.get("statbox_top")
    if fr and st is not None and st <= fr[2]:
        add("fail", "stat box", "stat box top sits at or above the folio rule")
    er, rr = ctx.get("edition_right_x"), ctx.get("rail_rule_x")
    if er is not None and rr is not None and er > rr - 1:
        add("fail", "edition_line", "edition/dateline overruns the vertical rail rule")
    tops, feet, rules = ctx.get("sidebar_tops", []), ctx.get("sidebar_feet", []), ctx.get("sidebar_rules", [])
    for i, (_, _, ry) in enumerate(rules):       # rule i divides story i and i+1
        if i < len(feet) and feet[i] > ry + 1:
            add("fail", f"sidebar_{i+1}_body", "story foot crosses the divider rule below it")
        if i + 1 < len(tops) and tops[i + 1] < ry - 1:
            add("fail", f"sidebar_{i+2}_headline", "story headline starts above its divider rule")
    pb, lf, lh = ctx.get("pullquote_bottom"), ctx.get("sidebar_last_foot"), ctx.get("rail_line_h", 0)
    if pb and lf is not None:
        if lf > pb + 2 * lh:
            add("fail", "sidebar (last)", "last story overflows past the pull-quote bottom")
        elif lf < pb - 2 * lh:
            add("info", "sidebar (last)", "last story foot is above the pull-quote bottom (rail not full; expected with short copy)")
    return out


def print_report(diags, label):
    fails = [d for d in diags if d[0] == "fail"]
    warns = [d for d in diags if d[0] == "warn"]
    tag = {"fail": "FAIL", "warn": "WARN", "info": "INFO"}
    print(f"\n=== Pre-render check: {label} ===")
    if not diags:
        print("  (no issues)")
    for lvl, field, msg in diags:
        print(f"  [{tag[lvl]}] {field}: {msg}")
    ok = not fails
    print("  " + "-" * 40)
    print(f"  {'PASS' if ok else 'FAIL'} — {len(fails)} failure(s), {len(warns)} warning(s)")
    return ok


def resolve_zones(cfg, W, H):
    z = {name: (fx * W, fy * H, fw * W, fh * H)
         for name, (fx, fy, fw, fh) in cfg["zones"].items()}
    # Shift the stat box and the rail down so they begin below the folio row.
    # The baked tan box is moved by the same amount in build (shift_statbox).
    s = cfg["rail_shift"] * H
    for name in ("statbox", "sidebar_1", "sidebar_2"):
        x, y, w, h = z[name]
        z[name] = (x, y + s, w, h)
    return z


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


_SRGB_ICC = None


def srgb_icc_bytes():
    """The sRGB ICC profile, cached, so every saved file is tagged with an
    unambiguous colour space. An untagged RGB file forces the print RIP to guess;
    a tagged file converts predictably to the press's CMYK profile."""
    global _SRGB_ICC
    if _SRGB_ICC is None:
        _SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _SRGB_ICC


def effective_dpi(image, cfg):
    """The page's real DPI, from its width against the A2 long-edge-in-inches."""
    return image.width / (cfg["a2_mm"][0] / 25.4)


def add_bleed_and_marks(image, cfg, bleed_mm, crop_marks):
    """Extend the trimmed A2 page with `bleed_mm` of bleed on every side and,
    optionally, draw crop marks just OUTSIDE the bleed. Bleed is filled by
    replicating the edge pixels (clamp) so a cut drifting into the bleed shows
    artwork, never white. Crop marks live in a white slug beyond the bleed (never
    over live artwork) and point at the trim corners. The total margin is sized to
    hold the bleed AND the marks, so nothing is clipped. Returns the enlarged
    image; the trim box is inset by `margin` on every side."""
    if bleed_mm <= 0 and not crop_marks:
        return image
    dpi = effective_dpi(image, cfg)
    W, H = image.size
    bleed = max(0, int(round(bleed_mm / 25.4 * dpi)))
    if crop_marks:
        L = max(2, int(round(cfg["crop_mark_mm"] / 25.4 * dpi)))
        w = max(1, int(round(cfg["crop_mark_weight_mm"] / 25.4 * dpi)))
        gap = max(bleed, int(round(cfg["crop_mark_gap_mm"] / 25.4 * dpi)))  # marks start outside the bleed
        pad = max(2, int(round(1.0 / 25.4 * dpi)))                          # breathing room past the marks
        margin = gap + L + pad
    else:
        margin = bleed

    canvas = Image.new("RGB", (W + 2 * margin, H + 2 * margin), (255, 255, 255))
    canvas.paste(image, (margin, margin))
    if bleed > 0:
        # Replicate the edge rows/columns outward into the bleed zone only.
        top = image.crop((0, 0, W, 1)).resize((W, bleed))
        bot = image.crop((0, H - 1, W, H)).resize((W, bleed))
        left = image.crop((0, 0, 1, H)).resize((bleed, H))
        right = image.crop((W - 1, 0, W, H)).resize((bleed, H))
        canvas.paste(top, (margin, margin - bleed)); canvas.paste(bot, (margin, margin + H))
        canvas.paste(left, (margin - bleed, margin)); canvas.paste(right, (margin + W, margin))
        for cx, cy, sx, sy in ((margin - bleed, margin - bleed, 0, 0),
                               (margin + W, margin - bleed, W - 1, 0),
                               (margin - bleed, margin + H, 0, H - 1),
                               (margin + W, margin + H, W - 1, H - 1)):
            canvas.paste(image.crop((sx, sy, sx + 1, sy + 1)).resize((bleed, bleed)), (cx, cy))
    if crop_marks:
        d = ImageDraw.Draw(canvas)
        col = (0, 0, 0)
        x0, y0, x1, y1 = margin, margin, margin + W, margin + H  # trim box on the canvas
        for tx, ty, hx, vy in ((x0, y0, -1, -1), (x1, y0, 1, -1),
                               (x0, y1, -1, 1), (x1, y1, 1, 1)):
            d.line([(tx + hx * gap, ty), (tx + hx * (gap + L), ty)], fill=col, width=w)
            d.line([(tx, ty + vy * gap), (tx, ty + vy * (gap + L))], fill=col, width=w)
    return canvas


def _region_mean_rgb(px, x0, x1, y0, y1, step=5):
    rs = gs = bs = c = 0
    for x in range(int(x0), int(x1), max(1, step)):
        for y in range(int(y0), int(y1), max(1, step)):
            p = px[x, y]
            rs += p[0]; gs += p[1]; bs += p[2]; c += 1
    if not c:
        return (0.0, 0.0, 0.0)
    return (rs / c, gs / c, bs / c)


def verify_surgery(template, cfg, W, H):
    """Post-conditions for the template pixel surgery (rule erasure + stat-box
    shift). Those operations clone fixed pixel strips at fractions calibrated to
    the PRODUCTION template; a different template would silently corrupt (a ghost
    rule, a half-cloned box). Runs on the template BEFORE text is composited, so
    any darkness found is structural, not copy. Returns 'fail' diagnostics so
    build() quarantines the render rather than shipping a smeared page."""
    issues = []
    px = template.load()
    band = max(3, int(round(0.006 * H)))

    # 1) Erased rules must leave no residual dark horizontal line.
    erased = [
        ("sub_rule", cfg["sub_rule_y"], 0.0, cfg["sub_rule_x_end"]),
        ("sidebar_rule", cfg["sidebar_rule_y"], cfg["sidebar_rule_x0"], cfg["sidebar_rule_x1"]),
        ("standfirst_rule", cfg["standfirst_rule_y"], cfg["standfirst_rule_x0"], cfg["standfirst_rule_x1"]),
    ]
    for name, yf, x0f, x1f in erased:
        y = int(round(yf * H)); x0 = int(round(x0f * W)); x1 = int(round(x1f * W))
        if x1 - x0 < 10 or y - 3 * band < 0:
            continue
        ref = _region_mean_rgb(px, x0, x1, y - 3 * band, y - 2 * band)
        paper_lum = _lum(ref)
        step = max(1, (x1 - x0) // 200)
        darkest = min(_lum(_region_mean_rgb(px, x0, x1, yy, yy + 1, step))
                      for yy in range(max(0, y - band // 2), min(H, y + band // 2 + 1)))
        if darkest < paper_lum - 18:
            issues.append(("fail", f"template/{name}",
                f"a dark rule persists after erase (row {darkest:.0f} vs paper {paper_lum:.0f}); "
                "the template's baked rules do not sit where the engine expects"))

    # 2) Stat box must be relocated: box colour at the shifted top, paper where it
    #    vacated. (Skip when rail_shift is 0, since nothing moves.)
    shift = int(round(cfg["rail_shift"] * H))
    if shift > band:
        sx0 = int(round(0.708 * W)); sx1 = int(round(0.982 * W)); oy = int(round(0.064 * H))
        qx0, qx1 = sx0 + (sx1 - sx0) // 4, sx1 - (sx1 - sx0) // 4
        box_new = _region_mean_rgb(px, qx0, qx1, oy + shift + band, oy + shift + 4 * band)
        vacated = _region_mean_rgb(px, qx0, qx1, oy, oy + max(1, shift - band))
        d = sum((box_new[i] - vacated[i]) ** 2 for i in range(3)) ** 0.5
        if d < 20:
            issues.append(("fail", "template/statbox",
                f"stat box did not relocate (box/paper colour distance {d:.0f} < 20); "
                "the baked tan box is not where the engine expects on this template"))
    return issues


def assert_fonts_resolved(cr, cfg):
    """Hard-fail if any configured font family resolves to a substitute rather
    than the real face. A fresh container missing the bundled ./fonts would
    otherwise render in a fallback and still pass every layout check, so this is
    the guard against a silently ugly batch."""
    layout = PangoCairo.create_layout(cr)
    pctx = layout.get_context()
    keys = ("font_masthead", "font_headline", "font_body", "font_sans",
            "font_pullquote", "font_stat", "font_sidebar_head")
    checked, missing = set(), []
    for spec in (cfg[k] for k in keys):
        fd = Pango.FontDescription.from_string(spec)
        fam = (fd.get_family() or "").strip()
        if not fam or fam in checked:
            continue
        checked.add(fam)
        font = pctx.load_font(fd)
        resolved = (font.describe().get_family() if font else "") or ""
        if fam.lower() not in resolved.lower():
            missing.append((spec, f"resolved to '{resolved}'" if resolved else "no font loaded"))
    if missing:
        print("[error] required fonts did not resolve to their real faces:")
        for spec, why in missing:
            print(f"[error]   '{spec}' -> {why}")
        print(f"[error] check the bundled fonts in {FONT_DIR} (or set "
              f"SENTRADA_FONT_DIR). Refusing to render in substitute fonts.")
        sys.exit(1)


def build(template_path, data_path, output_path, cfg=CONFIG,
          print_dpi=None, print_size=None, render_dpi=None, render_size=None,
          ink_blur=None, bleed_mm=0.0, crop_marks=False):
    """Render the page. Returns the diagnostics list from run_checks. If
    output_path is None the finished page is not saved (validation-only)."""
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
    erase_h_rule(template, W, H, cfg["standfirst_rule_y"],
                 cfg["standfirst_rule_x0"], cfg["standfirst_rule_x1"])
    # Move the baked stat box down so the rail clears the full-width folio row.
    shift_statbox(template, cfg, W, H)
    # Verify the surgery landed before drawing anything on top of it.
    surgery_issues = verify_surgery(template, cfg, W, H)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surface)
    assert_fonts_resolved(cr, cfg)
    rule_colour, rule_weight = sample_rule(template, cfg, W, H)
    ctx = {"W": W, "H": H, "cfg": cfg, "px": resolve_zones(cfg, W, H),
           "rule_colour": rule_colour, "rule_weight": rule_weight,
           "surgery_issues": surgery_issues}

    render_masthead(cr, ctx, data)
    render_headline_and_byline(cr, ctx, data)
    render_article(cr, ctx, data)
    render_pullquote(cr, ctx, data)
    render_statbox(cr, ctx, data)
    render_sidebar(cr, ctx, data)
    render_kicker(cr, ctx, data)

    text_rgba = cairo_to_pil(surface)
    result = composite(template, text_rgba, cfg, W, ink_blur=ink_blur)
    for key in ("folio_rule", "standfirst_rule"):
        if key in ctx:
            rx0, rx1, ry = ctx[key]
            result = draw_rule_on_image(result, ctx, rx0, rx1, ry)
    for rx0, rx1, ry in ctx.get("sidebar_rules", []):
        result = draw_rule_on_image(result, ctx, rx0, rx1, ry)
    result = place_logo(result, ctx)

    result, dpi = finalize_output(result, cfg, print_dpi, print_size)
    result = trim_edge_frame(result, cfg)  # kill any resize edge halo on the trim
    if bleed_mm > 0 or crop_marks:
        result = add_bleed_and_marks(result, cfg, bleed_mm, crop_marks)

    diagnostics = run_checks(ctx, data, cfg)
    fails = [d for d in diagnostics if d[0] == "fail"]
    if output_path is not None:
        save_kwargs = {"icc_profile": srgb_icc_bytes()}
        if dpi:
            save_kwargs["dpi"] = (dpi, dpi)
        if fails:
            # A FAIL means the page is not print-ready. Never write it to the
            # deliverable path where it could be mistaken for a good render;
            # quarantine it under a .FAILED name for inspection instead.
            root, ext = os.path.splitext(output_path)
            quarantine = root + ".FAILED" + ext
            result.save(quarantine, **save_kwargs)
            print(f"[error] {len(fails)} layout failure(s); deliverable WITHHELD:")
            for _lvl, field, msg in fails:
                print(f"[error]   {field}: {msg}")
            print(f"[error] wrote quarantined render to {quarantine} "
                  f"(NOT print-ready). Fix the copy/template and re-run.")
        else:
            result.save(output_path, **save_kwargs)
            print(f"[done] wrote {output_path} ({result.width}x{result.height}px)")
    return diagnostics


def main():
    ap = argparse.ArgumentParser(
        description="Place structured newspaper copy onto a blank template.")
    ap.add_argument("--template", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--output", default=None,
                    help="output PNG path. Optional with --check (validation only).")
    ap.add_argument("--check", action="store_true",
                    help="run pre-render validation and print a PASS/FAIL report "
                         "(word counts, zone overflow, rail gaps at cap, rule "
                         "collisions). Exits non-zero on any failure. Runs at the "
                         "print resolution (defaults to 300 DPI) so the check "
                         "matches what will be printed.")
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
    ap.add_argument("--bleed-mm", type=float, default=0.0,
                    help="extend the page with this much bleed (mm) on every side, "
                         "filled by replicating the edge so a drifting cut never "
                         "shows white. Off (0) by default. Typical print spec: 3.")
    ap.add_argument("--crop-marks", action="store_true",
                    help="draw crop marks at the trim corners (in the bleed margin). "
                         "Adds a 3mm margin if no bleed is set.")
    args = ap.parse_args()

    def parse_size(s):
        if not s:
            return None
        w, h = s.lower().split("x")
        return (int(w), int(h))

    if not args.check and not args.output:
        ap.error("--output is required unless --check is given")

    render_dpi = args.render_dpi
    render_size = parse_size(args.render_size)
    # The check must run at print resolution so wrapping/hyphenation matches the
    # printed page; default it to 300 DPI when no render size was requested.
    if args.check and not render_dpi and not render_size:
        render_dpi = 300

    diagnostics = build(
        args.template, args.data, args.output,
        print_dpi=args.print_dpi, print_size=parse_size(args.print_size),
        render_dpi=render_dpi, render_size=render_size, ink_blur=args.ink_blur,
        bleed_mm=args.bleed_mm, crop_marks=args.crop_marks)

    if args.check:
        ok = print_report(diagnostics, os.path.basename(args.data))
        sys.exit(0 if ok else 1)

    # Normal render: exit non-zero on any layout failure so a batch runner (or
    # CI) treats the withheld deliverable as a hard error, not a success.
    if any(d[0] == "fail" for d in diagnostics):
        sys.exit(1)


if __name__ == "__main__":
    main()
