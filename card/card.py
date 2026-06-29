#!/usr/bin/env python3
"""Sentrada companion-card engine.

Renders one A6 companion card to a print-ready PNG at 360 DPI, the same
resolution as the rest of the batch. Deterministic and procedural: no AI image
generation, text is guaranteed legible because it is laid out by Pango/Cairo.

This is the fourth layout engine in the Sentrada pipeline and follows the same
house pattern as email/email.py: a single standalone script, a --check mode that
validates fit without rendering, and crossword-style --bleed-mm / --crop-marks
options for the Birch print path.

  python3.12 card/card.py --text <card.txt> --output <slug>-card.png \
       [--check] [--bleed-mm 3] [--crop-marks] [--landscape]

The input is a plain-text file holding ONE card: a first-name salutation line,
then body paragraphs (blank-line separated), then a two-line sign-off. Nothing
about any recipient or card is hardcoded; everything is parsed from --text.

Requires Pango/Cairo via PyGObject. In this environment that means python3.12
(the system gobject-introspection bindings are built for 3.12, not 3.11).
"""

import argparse
import os
import sys
import tempfile

# --- Fonts -----------------------------------------------------------------
# Register the bundled brand fonts (Fraunces display, Inter body) with
# fontconfig BEFORE Pango initialises it, so the engine is self-contained and
# does not depend on the host having the fonts installed.
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def _register_fonts():
    conf = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{font_dir}</dir>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <cachedir>{cache}</cachedir>
</fontconfig>
""".format(font_dir=FONT_DIR, cache=os.path.join(tempfile.gettempdir(), "sentrada-fc-cache"))
    path = os.path.join(tempfile.gettempdir(), "sentrada-card-fonts.conf")
    with open(path, "w") as fh:
        fh.write(conf)
    os.environ["FONTCONFIG_FILE"] = path


_register_fonts()

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

# --- Print spec ------------------------------------------------------------
DPI = 360
PX_PER_MM = DPI / 25.4

# A6 trim, portrait. Orientation can be flipped with --landscape; the uploaded
# Canva template is the source of truth and overrides this default.
TRIM_W_MM = 105.0
TRIM_H_MM = 148.0

# Clear safe-margin inset for text (keeps copy away from the trim edge).
MARGIN_L_MM = 11.0
MARGIN_R_MM = 11.0
MARGIN_T_MM = 14.0
MARGIN_B_MM = 13.0

# --- Brand -----------------------------------------------------------------
# Palette taken verbatim from the live site (styles.css).
CHARCOAL = (0x1B / 255, 0x1B / 255, 0x1B / 255)
CLAY = (0xC4 / 255, 0x72 / 255, 0x4E / 255)
CLAY_DEEP = (0xA5 / 255, 0x5E / 255, 0x3D / 255)
STONE = (0xF2 / 255, 0xE9 / 255, 0xE1 / 255)

FONT_DISPLAY = "Fraunces"   # wordmark + signature
FONT_BODY = "Inter"         # salutation, body, sign-off name

# --- Typography ------------------------------------------------------------
# Body type auto-fits within this point range to absorb card length variation.
# At 360 DPI 8.5pt is still crisply legible in print; we flag overflow rather
# than going smaller, because legibility is non-negotiable.
BODY_PT_MAX = 11.5
BODY_PT_MIN = 8.5
BODY_PT_STEP = 0.25

LINE_SPACING = 1.42        # body leading multiplier
PARA_GAP_EM = 0.85         # gap between body paragraphs, in body em
SALUTATION_GAP_EM = 1.1    # gap after the salutation
SIGNOFF_GAP_EM = 1.4       # gap before the sign-off

WORDMARK_PT = 15.0
SIGNATURE_PT = 13.5        # the "Sentrada" signature in the sign-off
FOOTER_PT = 8.0


def pt_to_px(pt):
    return pt / 72.0 * DPI


def mm_to_px(mm):
    return mm * PX_PER_MM


# --- Card parsing ----------------------------------------------------------
class Card:
    """One parsed companion card: salutation, body paragraphs, sign-off."""

    def __init__(self, salutation, paragraphs, signoff_name, signoff_org):
        self.salutation = salutation
        self.paragraphs = paragraphs
        self.signoff_name = signoff_name
        self.signoff_org = signoff_org


def parse_card(text):
    """Parse a plain-text card into blocks.

    Blocks are separated by blank lines. The first block is the salutation,
    the last block is the two-line sign-off (name then organisation), and
    everything between is the body. Lines wrapped inside a single block are
    re-joined with spaces.
    """
    raw_blocks = []
    current = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                raw_blocks.append(current)
                current = []
        else:
            current.append(line.strip())
    if current:
        raw_blocks.append(current)

    if len(raw_blocks) < 3:
        raise ValueError(
            "Card needs a salutation, at least one body paragraph and a "
            "sign-off (got %d block(s))." % len(raw_blocks)
        )

    salutation = " ".join(raw_blocks[0])
    signoff = raw_blocks[-1]
    signoff_name = signoff[0]
    signoff_org = " ".join(signoff[1:]) if len(signoff) > 1 else ""
    paragraphs = [" ".join(b) for b in raw_blocks[1:-1]]
    return Card(salutation, paragraphs, signoff_name, signoff_org)


# --- Layout ----------------------------------------------------------------
def _layout(ctx, text, family, weight, size_px, width_px, italic=False,
            spacing_mult=1.0, align_left=True):
    layout = PangoCairo.create_layout(ctx)
    desc = Pango.FontDescription()
    desc.set_family(family)
    desc.set_weight(weight)
    if italic:
        desc.set_style(Pango.Style.ITALIC)
    desc.set_absolute_size(size_px * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_width(int(width_px * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD)
    if align_left:
        layout.set_alignment(Pango.Alignment.LEFT)
    if spacing_mult != 1.0:
        # Pango line spacing is leading added on top of the natural line height.
        extra = (spacing_mult - 1.0) * size_px
        layout.set_spacing(int(extra * Pango.SCALE))
    layout.set_text(text, -1)
    return layout


def _layout_height(layout):
    return layout.get_pixel_size()[1]


def measure_flow(ctx, card, body_px, text_w):
    """Measure the flowed content (salutation + body + sign-off) at body_px.

    Returns (total_height_px, blocks) where blocks is a list of
    (kind, layout, y_gap_before) ready to draw, or to total up for --check.
    """
    blocks = []
    total = 0.0

    salutation_px = body_px * 1.05
    sal = _layout(ctx, card.salutation, FONT_BODY, Pango.Weight.SEMIBOLD,
                  salutation_px, text_w)
    blocks.append(("salutation", sal, 0.0))
    total += _layout_height(sal)

    for i, para in enumerate(card.paragraphs):
        gap = (SALUTATION_GAP_EM if i == 0 else PARA_GAP_EM) * body_px
        lay = _layout(ctx, para, FONT_BODY, Pango.Weight.NORMAL, body_px,
                      text_w, spacing_mult=LINE_SPACING)
        blocks.append(("body", lay, gap))
        total += gap + _layout_height(lay)

    name = _layout(ctx, card.signoff_name, FONT_BODY, Pango.Weight.MEDIUM,
                   body_px, text_w)
    blocks.append(("signoff_name", name, SIGNOFF_GAP_EM * body_px))
    total += SIGNOFF_GAP_EM * body_px + _layout_height(name)

    if card.signoff_org:
        org = _layout(ctx, card.signoff_org, FONT_DISPLAY, Pango.Weight.NORMAL,
                      SIGNATURE_PT_to_px(body_px), text_w, italic=True)
        blocks.append(("signoff_org", org, 0.2 * body_px))
        total += 0.2 * body_px + _layout_height(org)

    return total, blocks


def SIGNATURE_PT_to_px(body_px):
    # Signature scales gently with the body so it never dwarfs short cards.
    return max(pt_to_px(SIGNATURE_PT), body_px * 1.15)


def fit_body_size(ctx, card, text_w, avail_h):
    """Largest body size in the range that fits avail_h, or None if it overflows.

    Returns (body_px, total_h). On overflow returns (BODY_PT_MIN px, total_h)
    so the caller can report by how much the smallest size overran.
    """
    pt = BODY_PT_MAX
    last = None
    while pt >= BODY_PT_MIN - 1e-9:
        body_px = pt_to_px(pt)
        total, _ = measure_flow(ctx, card, body_px, text_w)
        last = (body_px, total)
        if total <= avail_h:
            return body_px, total, False
        pt -= BODY_PT_STEP
    return last[0], last[1], True  # overflow at smallest size


# --- Geometry --------------------------------------------------------------
class Geometry:
    def __init__(self, landscape, bleed_mm):
        tw, th = (TRIM_H_MM, TRIM_W_MM) if landscape else (TRIM_W_MM, TRIM_H_MM)
        self.trim_w = mm_to_px(tw)
        self.trim_h = mm_to_px(th)
        self.bleed = mm_to_px(bleed_mm)
        self.page_w = int(round(self.trim_w + 2 * self.bleed))
        self.page_h = int(round(self.trim_h + 2 * self.bleed))
        # Trim origin within the page (top-left of the trim box).
        self.ox = self.bleed
        self.oy = self.bleed
        self.margin_l = mm_to_px(MARGIN_L_MM)
        self.margin_r = mm_to_px(MARGIN_R_MM)
        self.margin_t = mm_to_px(MARGIN_T_MM)
        self.margin_b = mm_to_px(MARGIN_B_MM)

    @property
    def text_w(self):
        return self.trim_w - self.margin_l - self.margin_r

    @property
    def footer_h(self):
        return mm_to_px(6.0)

    @property
    def header_h(self):
        # Wordmark + rule + gap below it.
        return pt_to_px(WORDMARK_PT) + mm_to_px(5.0)

    @property
    def flow_top(self):
        return self.oy + self.margin_t + self.header_h

    @property
    def flow_bottom(self):
        return self.oy + self.trim_h - self.margin_b - self.footer_h

    @property
    def avail_h(self):
        return self.flow_bottom - self.flow_top


# --- Rendering -------------------------------------------------------------
def set_source(ctx, rgb):
    ctx.set_source_rgb(*rgb)


def draw_crop_marks(ctx, geo):
    """Standard corner crop marks sitting in the bleed, mirroring crossword.py."""
    if geo.bleed <= 0:
        return
    mark = mm_to_px(3.0)      # mark length
    gap = mm_to_px(1.2)       # gap from trim so marks sit in the bleed only
    ctx.set_line_width(mm_to_px(0.18))
    set_source(ctx, CHARCOAL)
    corners = [
        (geo.ox, geo.oy),                                  # top-left
        (geo.ox + geo.trim_w, geo.oy),                     # top-right
        (geo.ox, geo.oy + geo.trim_h),                     # bottom-left
        (geo.ox + geo.trim_w, geo.oy + geo.trim_h),        # bottom-right
    ]
    for cx, cy in corners:
        sx = -1 if cx <= geo.ox + 1 else 1
        sy = -1 if cy <= geo.oy + 1 else 1
        # horizontal mark
        ctx.move_to(cx + sx * gap, cy)
        ctx.line_to(cx + sx * (gap + mark), cy)
        # vertical mark
        ctx.move_to(cx, cy + sy * gap)
        ctx.line_to(cx, cy + sy * (gap + mark))
        ctx.stroke()


def draw_card(geo, card, body_px, blocks, out_path, crop_marks):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, geo.page_w, geo.page_h)
    ctx = cairo.Context(surface)

    # Background extends through the bleed.
    set_source(ctx, STONE)
    ctx.rectangle(0, 0, geo.page_w, geo.page_h)
    ctx.fill()

    # Wordmark, top-left of the safe area.
    wm = _layout(ctx, "Sentrada", FONT_DISPLAY, Pango.Weight.SEMIBOLD,
                 pt_to_px(WORDMARK_PT), geo.text_w)
    wx = geo.ox + geo.margin_l
    wy = geo.oy + geo.margin_t
    ctx.move_to(wx, wy)
    set_source(ctx, CHARCOAL)
    PangoCairo.show_layout(ctx, wm)

    # Thin clay rule under the wordmark.
    rule_y = wy + _layout_height(wm) + mm_to_px(2.2)
    ctx.set_line_width(mm_to_px(0.4))
    set_source(ctx, CLAY)
    ctx.move_to(wx, rule_y)
    ctx.line_to(wx + mm_to_px(14.0), rule_y)
    ctx.stroke()

    # Flowed content.
    x = geo.ox + geo.margin_l
    y = geo.flow_top
    for kind, layout, gap in blocks:
        y += gap
        ctx.move_to(x, y)
        if kind == "signoff_org":
            set_source(ctx, CLAY_DEEP)
        else:
            set_source(ctx, CHARCOAL)
        PangoCairo.show_layout(ctx, layout)
        y += _layout_height(layout)

    # Footer: sentrada.io, clay, bottom-left of the safe area.
    footer = _layout(ctx, "sentrada.io", FONT_BODY, Pango.Weight.MEDIUM,
                     pt_to_px(FOOTER_PT), geo.text_w)
    fy = geo.oy + geo.trim_h - geo.margin_b - _layout_height(footer)
    ctx.move_to(x, fy)
    set_source(ctx, CLAY)
    PangoCairo.show_layout(ctx, footer)

    if crop_marks:
        draw_crop_marks(ctx, geo)

    surface.flush()
    surface.write_to_png(out_path)


# --- CLI -------------------------------------------------------------------
def run(args):
    with open(args.text, "r") as fh:
        card = parse_card(fh.read())

    geo = Geometry(args.landscape, args.bleed_mm)

    # A throwaway surface for measuring before we know the final size.
    measure_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    measure_ctx = cairo.Context(measure_surface)

    body_px, total_h, overflow = fit_body_size(
        measure_ctx, card, geo.text_w, geo.avail_h)
    body_pt = body_px / DPI * 72.0

    if args.check:
        orient = "landscape" if args.landscape else "portrait"
        print("Card fit check (%s, A6, %d DPI)" % (orient, DPI))
        print("  paragraphs:   %d" % len(card.paragraphs))
        print("  text area:    %.0f x %.0f px  (%.1f mm wide)"
              % (geo.text_w, geo.avail_h, geo.text_w / PX_PER_MM))
        print("  fitted body:  %.2f pt" % body_pt)
        print("  content:      %.0f px  (available %.0f px)"
              % (total_h, geo.avail_h))
        if overflow:
            print("  OVERFLOW:     %.0f px (%.1f mm) over at the %.1f pt minimum"
                  % (total_h - geo.avail_h,
                     (total_h - geo.avail_h) / PX_PER_MM, BODY_PT_MIN))
            print("FAIL")
            return 1
        print("  headroom:     %.0f px (%.1f mm)"
              % (geo.avail_h - total_h, (geo.avail_h - total_h) / PX_PER_MM))
        print("OK")
        return 0

    if overflow:
        sys.stderr.write(
            "ERROR: card overflows the A6 text area by %.1f mm even at the "
            "%.1f pt minimum. Shorten the copy or run --check for detail.\n"
            % ((total_h - geo.avail_h) / PX_PER_MM, BODY_PT_MIN))
        return 1

    # Re-measure at the chosen size to get drawable blocks, then render.
    render_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    render_ctx = cairo.Context(render_surface)
    _, blocks = measure_flow(render_ctx, card, body_px, geo.text_w)
    draw_card(geo, card, body_px, blocks, args.output, args.crop_marks)
    print("Rendered %s  (%dx%d px, body %.2f pt, bleed %g mm%s)"
          % (args.output, geo.page_w, geo.page_h, body_pt, args.bleed_mm,
             ", crop marks" if args.crop_marks else ""))
    return 0


def main():
    p = argparse.ArgumentParser(
        description="Render one Sentrada companion card to a print-ready A6 PNG.")
    p.add_argument("--text", required=True,
                   help="Plain-text file holding ONE card (salutation -> sign-off).")
    p.add_argument("--output", help="Output PNG path (slug-card.png).")
    p.add_argument("--check", action="store_true",
                   help="Validate fit only; print overflow and exit non-zero. No render.")
    p.add_argument("--bleed-mm", type=float, default=0.0,
                   help="Bleed on every side in mm (Birch uses 3). Default 0 (trim).")
    p.add_argument("--crop-marks", action="store_true",
                   help="Draw corner crop marks in the bleed (needs --bleed-mm > 0).")
    p.add_argument("--landscape", action="store_true",
                   help="Landscape A6 (default portrait). Template is source of truth.")
    args = p.parse_args()

    if not args.check and not args.output:
        p.error("--output is required unless --check is set.")

    try:
        return run(args)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
