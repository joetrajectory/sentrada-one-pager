#!/usr/bin/env python3
"""
Sentrada companion-card layout engine.

Renders the A6 note that ships in the box beside the artefact (newspaper /
crossword / "the email") and outputs a print-ready PNG at 360 DPI. The artefact
itself carries no sender branding by design; this card is where Sentrada is
revealed, so it must read like a personal note from the founder on fine
stationery, not marketing collateral. The whole card is typographic: warm stone
paper, a single clay slice accent, the recipient's name, the body copy and the
contact block. No template image and no AI image generation.

    python card.py --data test_lesley.json --output lesley_card.png

A direct sibling of the newspaper, crossword and email engines: same Pango/Cairo
text layout, same ctypes fontconfig registration, same --check gate and
*.FAILED.png quarantine, the same immutable-copy rule (overflow is a flagged
failure, never a silent squeeze) and the same crossword-style --bleed-mm /
--crop-marks finishing for Birch.

The one thing specific to this format is the auto-fit ladder: the card must hold
~3 short paragraphs up to a 150-word note without looking empty or cramped, so
the engine measures the laid-out card and drops to the first tier that fits the
148 x 105 mm trim with the bottom safe margin intact. Body type never shrinks
below 9 pt (it has to stay readable on uncoated stock); instead the line height,
paragraph gap and finally the contact block (4 lines -> 2 lines) tighten. Copy
that overflows even the tightest tier is flagged for trimming, never squeezed.
"""

import argparse
import ctypes
import json
import math
import os
import sys

# ---------------------------------------------------------------------------
# Register the bundled fonts with fontconfig BEFORE Pango loads, so the engine
# is self contained. Shared ../fonts first (Inter), then ./fonts (Fraunces).
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def register_app_fonts():
    dirs = [
        os.environ.get("SENTRADA_CARD_FONT_DIR"),
        os.path.normpath(os.path.join(SCRIPT_DIR, "..", "fonts")),
        os.path.join(SCRIPT_DIR, "fonts"),
    ]
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
    except OSError:
        print("[warn] libfontconfig not loadable; relying on system fonts")
        return
    fc.FcConfigGetCurrent.restype = ctypes.c_void_p
    fc.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fc.FcConfigAppFontAddDir.restype = ctypes.c_int
    for d in dirs:
        if d and os.path.isdir(d):
            fc.FcConfigAppFontAddDir(fc.FcConfigGetCurrent(), d.encode("utf-8"))


register_app_fonts()

import gi  # noqa: E402

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402
import cairo  # noqa: E402
from PIL import Image, ImageCms, ImageDraw  # noqa: E402

S = Pango.SCALE

# ---------------------------------------------------------------------------
# Print spec (the README hand-off is the source of truth; values in mm/pt so the
# layout is resolution independent and prints to exact size at any DPI).
# ---------------------------------------------------------------------------
A6_MM = (148.0, 105.0)            # A6 landscape: width, height
DEFAULT_SIZE = (1748, 1240)       # A6 landscape at 300 DPI, for screen previews

DISPLAY = "Fraunces"              # salutation (display serif)
BODY = "Inter"                    # everything else

# Palette (design tokens).
CHARCOAL = (0x1B / 255, 0x1B / 255, 0x1B / 255)          # ink
CLAY = (0xC4 / 255, 0x72 / 255, 0x4E / 255)              # the single accent
STONE = (0xF2 / 255, 0xE9 / 255, 0xE1 / 255)             # paper
WHITE = (1.0, 1.0, 1.0)
BODY_INK = (0x1B / 255, 0x1B / 255, 0x1B / 255, 0.90)    # body copy
SECONDARY = (0x1B / 255, 0x1B / 255, 0x1B / 255, 0.68)   # contact detail lines

# Wordmark (charcoal on transparent, scaled to a fixed width).
WORDMARK = os.path.join(SCRIPT_DIR, "assets", "wordmark-charcoal.png")
WORDMARK_XHEIGHT = 0.744          # x-height as a fraction of the cropped wordmark
LOCKUP_OPACITY = 0.50
# The wordmark is a bold logotype, so at equal size/opacity it reads heavier
# than the "one of one /" label beside it. Shave its size a touch and paint it
# a little dimmer than the label so the two read as one quiet unit.
WORDMARK_SIZE_TRIM = 0.95
WORDMARK_DIM = 0.80               # wordmark alpha as a fraction of the label's

# Slice motif: clay parallelogram, skewX(-32deg).
SLICE_W_MM = 10.58                # 40 px
SLICE_H_MM = 0.79                 # 3 px
SLICE_SKEW = math.tan(math.radians(-32.0))

# Type sizes (pt). 1 px at 96 dpi = 0.75 pt.
PT_SALUTATION = 12.75             # 17 px
PT_BODY = 9.0                     # 12 px, the floor: never smaller
PT_NAME = 9.0                     # 12 px, contact name
PT_DETAIL = 8.25                  # 11 px, contact detail
PT_LOCKUP = 6.75                  # 9 px, "one of one /"

# Letter spacing, in em of the run's own size.
EM_SALUTATION = -0.006
EM_BODY = 0.002
EM_NAME = 0.002
EM_DETAIL = 0.004
EM_LOCKUP = 0.040

BODY_MEASURE_CAP_MM = 128.0       # do not let body lines run the full width
SALUTATION_LH = 1.12
CONTACT_LH = 1.42

# Once a tier is chosen, the leftover vertical slack (the contact is pinned to
# the bottom margin) is distributed across the salutation and paragraph gaps so
# the body fills down to the contact instead of leaving a dead band above it.
# Capped per gap so a genuinely short note keeps natural breathing room rather
# than stretching its paragraphs absurdly far apart.
MAX_EXTRA_GAP_MM = 4.0

# The auto-fit ladder. The renderer applies the first tier whose laid-out card
# fits the trim with the bottom safe margin intact. Padding is (top, right,
# bottom, left) in mm; gaps in mm; body line-height is a multiple of 9 pt.
# footer_pad is the minimum gap between the body's last line and the footer row
# (the prototype's footer padding-top: 20 / 14 / 12 px), so the body never
# crowds the contact block. It is part of the fit test, not just decoration.
TIERS = [
    dict(tier=1, contact="4line", body_lh=1.55, para_gap=2.91, sal_gap=3.97,
         slice_gap=2.38, footer_pad=5.29, pad=(8.47, 10.58, 6.88, 10.58)),
    dict(tier=2, contact="4line", body_lh=1.46, para_gap=2.12, sal_gap=3.18,
         slice_gap=2.12, footer_pad=3.70, pad=(8.47, 10.58, 6.88, 10.58)),
    dict(tier=3, contact="2line", body_lh=1.45, para_gap=1.59, sal_gap=2.65,
         slice_gap=1.85, footer_pad=3.17, pad=(7.94, 9.53, 6.88, 9.53)),
]


# ---------------------------------------------------------------------------
# Unit helpers. The canvas width fixes the effective DPI, so mm/pt map to px
# exactly at whatever resolution we are rendering.
# ---------------------------------------------------------------------------
def dpi_of(W):
    return W / (A6_MM[0] / 25.4)


def mk_units(W):
    dpi = dpi_of(W)
    return (lambda v: v / 25.4 * dpi,    # mm -> px
            lambda v: v / 72.0 * dpi)    # pt -> px


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def _normalise(d):
    """Accept a few shapes and return (salutation, [paragraphs], contact)."""
    sal = d.get("salutation")
    if not sal:
        first = (d.get("recipient") or {}).get("first_name")
        sal = first or ""
    sal = sal.strip()
    if sal and sal[-1] not in ",.!?:;":
        sal += ","

    body = d.get("body") or []
    paras = []
    for b in body:
        if isinstance(b, str):
            t = b
        else:
            t = b.get("text", "")
        t = t.strip()
        if t:
            paras.append(t)

    contact = d.get("contact") or d.get("signoff") or {}
    if "name" not in contact:
        raise SystemExit("[fatal] data.contact.name is required")
    if not sal:
        raise SystemExit("[fatal] data.salutation (or recipient.first_name) is required")
    if not paras:
        raise SystemExit("[fatal] data.body must be a non-empty list of paragraphs")
    return sal, paras, contact


def load_data(path):
    with open(path, "r", encoding="utf-8") as fh:
        return _normalise(json.load(fh))


def load_text(path):
    """Convenience: a plain-text card (salutation / body / sign-off blocks).
    Sign-off lines map to contact name, company, email, phone in order."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    blocks, cur = [], []
    for line in raw.splitlines():
        if line.strip() == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line.strip())
    if cur:
        blocks.append(cur)
    if len(blocks) < 3:
        raise SystemExit("[fatal] text card needs salutation, body and a sign-off block")
    sign = blocks[-1]
    keys = ["name", "company", "email", "phone"]
    contact = {k: sign[i] for i, k in enumerate(keys) if i < len(sign)}
    return _normalise({
        "salutation": " ".join(blocks[0]),
        "body": [" ".join(b) for b in blocks[1:-1]],
        "contact": contact,
    })


def typographic(s):
    """Render the copy verbatim. The design reference sets straight apostrophes
    and quotes (e.g. Gong's, isn't), so the card does NOT smart-quote; the copy
    is authored to the house rules upstream (no em dashes) and printed as given.
    Kept as a single chokepoint in case a normalisation is ever wanted."""
    return s or ""


# ---------------------------------------------------------------------------
# Cairo / Pango helpers
# ---------------------------------------------------------------------------
def set_src(cr, color):
    if len(color) == 4:
        cr.set_source_rgba(*color)
    else:
        cr.set_source_rgb(*color)


def layout(cr, txt, family, size_px, weight=Pango.Weight.NORMAL, italic=False,
           width=None, lh=None, em=0.0):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription()
    fd.set_family(family)
    fd.set_weight(weight)
    if italic:
        fd.set_style(Pango.Style.ITALIC)
    fd.set_absolute_size(size_px * S)
    lay.set_font_description(fd)
    if width:
        lay.set_width(int(width * S))
        lay.set_wrap(Pango.WrapMode.WORD)
    attrs = Pango.AttrList()
    if lh:
        a = Pango.attr_line_height_new_absolute(int(lh * size_px * S))
        a.start_index, a.end_index = 0, 2 ** 31 - 1
        attrs.insert(a)
    if em:
        a = Pango.attr_letter_spacing_new(int(em * size_px * S))
        a.start_index, a.end_index = 0, 2 ** 31 - 1
        attrs.insert(a)
    lay.set_attributes(attrs)
    lay.set_text(txt, -1)
    return lay


def show(cr, lay, x, y, color):
    cr.save()
    set_src(cr, color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, lay)
    cr.restore()
    return lay.get_pixel_size()


def height(lay):
    return lay.get_pixel_size()[1]


def assert_fonts_resolved(cr):
    """Hard-fail if Fraunces or Inter resolve to a substitute, so a fresh
    container missing the bundled fonts never renders in a fallback face
    (mirrors the newspaper/crossword guard)."""
    pctx = PangoCairo.create_layout(cr).get_context()
    missing = []
    for fam in (DISPLAY, BODY):
        fd = Pango.FontDescription()
        fd.set_family(fam)
        font = pctx.load_font(fd)
        resolved = (font.describe().get_family() if font else "") or ""
        if fam.lower() not in resolved.lower():
            missing.append((fam, f"resolved to '{resolved}'" if resolved else "no font loaded"))
    if missing:
        print("[error] required fonts did not resolve to their real faces:")
        for fam, why in missing:
            print(f"[error]   '{fam}' -> {why}")
        print(f"[error] check the bundled fonts in {SCRIPT_DIR}/fonts and ../fonts "
              "(or set SENTRADA_CARD_FONT_DIR).")
        sys.exit(1)


_WORDMARK_SURF = None


def wordmark_surface():
    global _WORDMARK_SURF
    if _WORDMARK_SURF is None and os.path.isfile(WORDMARK):
        _WORDMARK_SURF = cairo.ImageSurface.create_from_png(WORDMARK)
    return _WORDMARK_SURF


# ---------------------------------------------------------------------------
# Layout model: measure (and optionally draw) the card at a given tier.
# ---------------------------------------------------------------------------
def contact_lines(cr, contact, tier, pt):
    """Return the contact block as a list of (layout, color), already styled
    for the tier's 4-line or 2-line treatment."""
    name = layout(cr, contact["name"], BODY, pt(PT_NAME),
                  weight=Pango.Weight.MEDIUM, lh=CONTACT_LH, em=EM_NAME)
    lines = [(name, CHARCOAL)]
    details = [contact.get(k) for k in ("company", "email", "phone")]
    details = [d for d in details if d]
    if tier["contact"] == "2line":
        if details:
            joined = " · ".join(details)
            lines.append((layout(cr, joined, BODY, pt(PT_DETAIL),
                                  lh=CONTACT_LH, em=EM_DETAIL), SECONDARY))
    else:
        for d in details:
            lines.append((layout(cr, d, BODY, pt(PT_DETAIL),
                                  lh=CONTACT_LH, em=EM_DETAIL), SECONDARY))
    return lines


def flow(cr, model, W, H, tier, draw):
    """Lay the card out at `tier`. With draw=False nothing is painted; returns
    (natural_body_bottom_px, footer_top_px) so the caller can test the fit at
    the tier's natural (minimum) spacing. When draw=True the leftover slack is
    distributed across the salutation and paragraph gaps so the body fills down
    to the contact; this never overflows because it only consumes slack."""
    mm, pt = mk_units(W)
    sal_text, paras, contact = model
    top, right, bottom, left = (mm(v) for v in tier["pad"])
    content_w = W - left - right
    body_w = min(content_w, mm(BODY_MEASURE_CAP_MM))

    # --- measure every block once -----------------------------------------
    sw, sh = mm(SLICE_W_MM), mm(SLICE_H_MM)
    slice_gap = mm(tier["slice_gap"])
    sal = layout(cr, typographic(sal_text), DISPLAY, pt(PT_SALUTATION),
                 weight=Pango.Weight.LIGHT, italic=True, lh=SALUTATION_LH,
                 em=EM_SALUTATION)
    sal_h = height(sal)
    para_lays = [layout(cr, typographic(p), BODY, pt(PT_BODY), width=body_w,
                        lh=tier["body_lh"], em=EM_BODY) for p in paras]
    para_hs = [height(l) for l in para_lays]
    lines = contact_lines(cr, contact, tier, pt)
    block_h = sum(height(l) for l, _ in lines)
    footer_bottom = H - bottom
    footer_top = footer_bottom - block_h

    base_sal_gap = mm(tier["sal_gap"])
    base_para_gap = mm(tier["para_gap"])
    natural_bottom = (top + sh + slice_gap + sal_h + base_sal_gap
                      + sum(para_hs) + base_para_gap * max(0, len(paras) - 1))

    if not draw:
        return natural_bottom, footer_top

    # distribute slack across the (1 salutation + n-1 paragraph) gaps
    n_gaps = 1 + max(0, len(paras) - 1)
    slack = (footer_top - mm(tier["footer_pad"])) - natural_bottom
    extra = min(slack / n_gaps, mm(MAX_EXTRA_GAP_MM)) if slack > 0 and n_gaps else 0.0
    sal_gap = base_sal_gap + extra
    para_gap = base_para_gap + extra

    cr.save()
    set_src(cr, STONE)
    cr.paint()
    cr.restore()

    # slice motif (the one accent), skewX(-32deg)
    dx = SLICE_SKEW * sh
    cr.save()
    set_src(cr, CLAY)
    cr.move_to(left, top)
    cr.line_to(left + sw, top)
    cr.line_to(left + sw + dx, top + sh)
    cr.line_to(left + dx, top + sh)
    cr.close_path()
    cr.fill()
    cr.restore()

    y = top + sh + slice_gap
    show(cr, sal, left, y, CHARCOAL)
    y += sal_h + sal_gap
    for i, lay in enumerate(para_lays):
        show(cr, lay, left, y, BODY_INK)
        y += para_hs[i]
        if i < len(paras) - 1:
            y += para_gap

    cy = footer_top
    for lay, color in lines:
        show(cr, lay, left, cy, color)
        cy += height(lay)
    _draw_lockup(cr, W, right, footer_bottom, pt, mm)

    return natural_bottom, footer_top


def _draw_lockup(cr, W, right, footer_bottom, pt, mm):
    """Right-aligned 'one of one /' + wordmark as one lockup at 50% opacity.
    The two read as the same size on a shared baseline: the wordmark is scaled
    so its cap height equals the text's, and both baselines are aligned."""
    label = layout(cr, "one of one /", BODY, pt(PT_LOCKUP), em=EM_LOCKUP)
    lw, lh = label.get_pixel_size()
    baseline = label.get_baseline() / S          # text baseline from layout top
    # the text's x-height (lowercase letter height), measured off an
    # ascender/descender-free string, is the size the wordmark must match.
    xh, _ = layout(cr, "one one", BODY, pt(PT_LOCKUP)).get_pixel_extents()
    x_height = xh.height
    surf = wordmark_surface()
    gap = mm(6 / 3.7795)                          # 6 px at 96 dpi
    if surf:
        wm_h = x_height / WORDMARK_XHEIGHT * WORDMARK_SIZE_TRIM
        scale = wm_h / surf.get_height()
        wm_w = surf.get_width() * scale
    else:
        scale = 1.0
        wm_w = wm_h = 0
    total = lw + gap + wm_w
    x0 = W - right - total
    bl = footer_bottom - (lh - baseline)          # shared baseline (descent below)
    # label at the lockup opacity
    cr.save()
    cr.push_group()
    show(cr, label, x0, bl - baseline, CHARCOAL)  # text baseline on bl
    cr.pop_group_to_source()
    cr.paint_with_alpha(LOCKUP_OPACITY)
    cr.restore()
    # wordmark a little dimmer, so the bold logotype reads as the label's weight
    if surf:
        cr.save()
        cr.translate(x0 + lw + gap, bl - wm_h)    # wordmark sits on bl
        cr.scale(scale, scale)
        cr.set_source_surface(surf, 0, 0)
        cr.paint_with_alpha(LOCKUP_OPACITY * WORDMARK_DIM)
        cr.restore()


# ---------------------------------------------------------------------------
# Render: walk the ladder, draw the first tier that fits.
# ---------------------------------------------------------------------------
def choose_tier(cr, model, W, H):
    """Return (tier_dict, fits_bool, overflow_px) for the first fitting tier,
    or the tightest tier with its overflow if none fit."""
    mm, _ = mk_units(W)
    last = None
    for tier in TIERS:
        body_bottom, footer_top = flow(cr, model, W, H, tier, draw=False)
        # the body must clear the footer by the tier's footer_pad gap
        overflow = body_bottom - (footer_top - mm(tier["footer_pad"]))
        last = (tier, overflow)
        if overflow <= 0:
            return tier, True, 0.0
    return last[0], False, last[1]


def render(model, W, H):
    """Draw the card at (W, H). Returns (cairo.Surface, tier, failures[])."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    assert_fonts_resolved(cr)
    tier, fits, overflow = choose_tier(cr, model, W, H)
    failures = []
    if not fits:
        mm, _ = mk_units(W)
        failures.append(("body", "copy overflows the tightest tier by "
                         f"{overflow / mm(1.0):.1f} mm ({int(overflow)}px); the card never "
                         "squeezes type below 9 pt. Trim a sentence (150-word cap)."))
    flow(cr, model, W, H, tier, draw=True)
    return surf, tier, failures


# ---------------------------------------------------------------------------
# Output finishing: PIL, sRGB tag, bleed + crop marks (mirrors crossword.py).
# ---------------------------------------------------------------------------
_SRGB_ICC = None


def srgb_icc_bytes():
    global _SRGB_ICC
    if _SRGB_ICC is None:
        _SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _SRGB_ICC


def surface_to_pil(surf):
    W, H = surf.get_width(), surf.get_height()
    img = Image.frombuffer("RGBA", (W, H), bytes(surf.get_data()), "raw", "BGRA", 0, 1)
    return img.convert("RGB")


CROP_MARK_MM = 4.0
CROP_MARK_WEIGHT_MM = 0.12
CROP_MARK_GAP_MM = 1.5


def add_bleed_and_marks(image, bleed_mm, crop_marks):
    if bleed_mm <= 0 and not crop_marks:
        return image
    dpi = dpi_of(image.width)
    W, H = image.size
    bleed = max(0, int(round(bleed_mm / 25.4 * dpi)))
    if crop_marks:
        L = max(2, int(round(CROP_MARK_MM / 25.4 * dpi)))
        w = max(1, int(round(CROP_MARK_WEIGHT_MM / 25.4 * dpi)))
        gap = max(bleed, int(round(CROP_MARK_GAP_MM / 25.4 * dpi)))
        pad = max(2, int(round(1.0 / 25.4 * dpi)))
        margin = gap + L + pad
    else:
        margin = bleed
    canvas = Image.new("RGB", (W + 2 * margin, H + 2 * margin), (255, 255, 255))
    canvas.paste(image, (margin, margin))
    if bleed > 0:
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
        x0, y0, x1, y1 = margin, margin, margin + W, margin + H
        for tx, ty, hx, vy in ((x0, y0, -1, -1), (x1, y0, 1, -1), (x0, y1, -1, 1), (x1, y1, 1, 1)):
            d.line([(tx + hx * gap, ty), (tx + hx * (gap + L), ty)], fill=(0, 0, 0), width=w)
            d.line([(tx, ty + vy * gap), (tx, ty + vy * (gap + L))], fill=(0, 0, 0), width=w)
    return canvas


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def size_for(args):
    if args.print_size:
        w, h = args.print_size.lower().split("x")
        return int(w), int(h)
    if args.print_dpi:
        return (round(A6_MM[0] / 25.4 * args.print_dpi),
                round(A6_MM[1] / 25.4 * args.print_dpi))
    if args.render_size:
        w, h = args.render_size.lower().split("x")
        return int(w), int(h)
    return DEFAULT_SIZE


def main():
    ap = argparse.ArgumentParser(description="Sentrada companion-card layout engine")
    ap.add_argument("--data", help="card JSON (salutation, body, contact)")
    ap.add_argument("--text", help="plain-text card (salutation / body / sign-off blocks)")
    ap.add_argument("--output", default=None, help="output PNG path")
    ap.add_argument("--check", action="store_true",
                    help="validate fit at the chosen size and exit non-zero on overflow")
    ap.add_argument("--print-dpi", type=int, default=None,
                    help="render straight to an exact A6 at this DPI (e.g. 360)")
    ap.add_argument("--print-size", default=None, help="explicit WIDTHxHEIGHT override")
    ap.add_argument("--render-size", default=None,
                    help="WIDTHxHEIGHT for preview renders (default 1748x1240)")
    ap.add_argument("--bleed-mm", type=float, default=0.0, help="bleed (mm) on every side")
    ap.add_argument("--crop-marks", action="store_true", help="draw crop marks in the bleed")
    args = ap.parse_args()

    if not args.data and not args.text:
        ap.error("one of --data or --text is required")
    if not args.output and not args.check:
        ap.error("--output is required unless --check is given")

    model = load_data(args.data) if args.data else load_text(args.text)
    W, H = size_for(args)
    surf, tier, failures = render(model, W, H)

    if args.check:
        label = os.path.basename(args.data or args.text)
        if failures:
            for field, msg in failures:
                print(f"  [FAIL] {field}: {msg}")
            print(f"CHECK: FAIL ({label}, tier {tier['tier']}) at {W}x{H}")
            sys.exit(1)
        print(f"CHECK: PASS ({label}, tier {tier['tier']} "
              f"[{tier['contact']}, lh {tier['body_lh']}]) at {W}x{H}")
        return

    img = surface_to_pil(surf)
    if failures:
        quarantine = os.path.splitext(args.output)[0] + ".FAILED.png"
        img.save(quarantine, icc_profile=srgb_icc_bytes())
        for field, msg in failures:
            print(f"  [FAIL] {field}: {msg}")
        print(f"[fatal] deliverable withheld; wrote {quarantine} for inspection")
        sys.exit(1)

    img = add_bleed_and_marks(img, args.bleed_mm, args.crop_marks)
    save_kwargs = {"icc_profile": srgb_icc_bytes()}
    if args.print_dpi:
        save_kwargs["dpi"] = (args.print_dpi, args.print_dpi)
    img.save(args.output, **save_kwargs)
    print(f"wrote {args.output} ({img.width}x{img.height}, tier {tier['tier']} "
          f"[{tier['contact']}], sRGB"
          + (f", {args.print_dpi} DPI" if args.print_dpi else "")
          + (f", {args.bleed_mm}mm bleed" if args.bleed_mm else "")
          + (", crop marks" if args.crop_marks else "") + ")")


if __name__ == "__main__":
    main()
