#!/usr/bin/env python3
"""
Sentrada board game layout engine.

Places structured, company-specific copy onto a blank board game template image
and produces a print-ready A2 board onto which every segment of the path carries
real, perfect text.

    # 1) Detect the path segments (runs once per template, cached to JSON)
    python boardgame.py detect --template boardgame_template.png \
        --output segment_map.json

    # 2) Render a company's copy onto the board
    python boardgame.py --template boardgame_template.png \
        --data test_qflow.json --segments segment_map.json \
        --output qflow_boardgame.png

Sibling of the newspaper engine (../newspaper/newspaper.py) and the crossword
engine (../crossword/crossword.py): the same Pango/Cairo text layout, the same
resolution-independent geometry (every position is a fraction of the template
dimensions, so one map works at any DPI), the same fontconfig font registration,
sRGB ICC tagging and print-production flags, and the same fail-fast safety guards
(font resolution, quarantine on overflow).

Two things differ, both forced by the format:

  * The path segments are irregular coloured shapes baked into the template, not
    rectangular zones. A computer-vision pass (`detect`) finds each segment,
    orders them along the path from the bottom-left start to the top-right
    finish, measures a usable text rectangle and a local path-direction angle for
    each, and writes a segment map. Detection runs ONCE per template; rendering
    reads the cached map.

  * The board is dark charcoal card stock, so the text is LIGHT (cream on the
    coloured segments, gold on the dark title ground). A literal multiply blend
    -- correct for dark ink on the newspaper's cream paper -- would erase light
    ink on a dark ground. Instead the ink is modulated by the template's LOCAL
    texture (a high-pass of the card grain and the moulded 3D shadows) and
    composited normally, so the light ink still reads as printed into the stock
    rather than pasted on top.
"""

import argparse
import ctypes
import io
import json
import math
import os
import sys

# ---------------------------------------------------------------------------
# Register the bundled font directories with fontconfig BEFORE Pango is used, so
# the engine is self contained (mirrors the newspaper/crossword engines). The
# board's own fonts live in ./fonts (Fraunces for the hero title, Inter for the
# copy); the shared ../fonts directory is also registered as a fallback.
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIRS = [
    os.environ.get("SENTRADA_BOARDGAME_FONT_DIR", os.path.join(SCRIPT_DIR, "fonts")),
    os.environ.get("SENTRADA_FONT_DIR", os.path.normpath(os.path.join(SCRIPT_DIR, "..", "fonts"))),
]


def register_app_fonts(font_dirs):
    dirs = [d for d in font_dirs if os.path.isdir(d)]
    if not dirs:
        print(f"[warn] no font directories found: {font_dirs} (relying on system fonts)")
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
    for d in dirs:
        if not fc.FcConfigAppFontAddDir(fc.FcConfigGetCurrent(), d.encode("utf-8")):
            print(f"[warn] could not register app fonts from {d}")


register_app_fonts(FONT_DIRS)

import gi  # noqa: E402

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

import cairo  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageCms, ImageFilter  # noqa: E402

SCALE = Pango.SCALE

# ---------------------------------------------------------------------------
# CONFIG. Everything that controls the look of the board. Positions are fractions
# of the image (origin top left), so the same config and segment map work at any
# resolution (the production template is 8x Magnific upscaled before print).
# ---------------------------------------------------------------------------

CONFIG = {
    # Colours -----------------------------------------------------------------
    # Light ink on a dark ground. Gold for the title/numbers/credit on the dark
    # board; warm cream for the copy on the coloured segments.
    "gold": (0xCE / 255, 0xA8 / 255, 0x5C / 255),        # warm gold, #CEA85C
    "gold_dim": (0xB7 / 255, 0x97 / 255, 0x62 / 255),    # muted gold for segment numbers
    "cream": (0xF2 / 255, 0xE9 / 255, 0xE1 / 255),       # segment copy, #F2E9E1
    # Fonts -------------------------------------------------------------------
    "font_title": "Fraunces Bold",          # THE / COMPANY / GAME (premium display serif)
    "font_subtitle": "Fraunces Italic",     # elegant italic strapline
    "font_copy": "Inter",                   # segment copy (clean sans)
    "font_label": "Inter Bold",             # START / FINISH
    "font_number": "Inter",                 # segment numbers
    "font_credit": "Inter",                 # sentrada credit
    # Segment copy ------------------------------------------------------------
    # Auto-size range as a fraction of image HEIGHT. Curved/short segments simply
    # resolve to the smaller end of the range; copy is never truncated.
    "copy_size_min": 0.0080,
    "copy_size_max": 0.0210,
    "copy_lead": 1.12,                      # line leading (multiple of size)
    "copy_rect_inset": 0.84,                # use this fraction of the detected usable rect
    "copy_pad_frac": 0.10,                  # extra padding inside the rect (each side)
    # Segment numbers ---------------------------------------------------------
    "number_size_frac": 0.012,              # fraction of image height
    "number_inset_frac": 0.10,              # inset from the usable rect's top-left corner
    # Title zone --------------------------------------------------------------
    "title_the_frac": 0.34,                 # "THE" cap height as a fraction of the COMPANY line
    "title_gap_frac": 0.10,                 # gap between title lines (fraction of company size)
    "title_company_max": 0.090,             # COMPANY/GAME line max size, fraction of height
    "title_zone_fill": 0.88,                # title block fills this fraction of the title zone
    "title_tracking_the": 0.22,             # letterspacing on "THE" (spaced caps)
    "subtitle_size_frac": 0.40,             # subtitle size as a fraction of the COMPANY size
    "subtitle_gap_frac": 0.46,              # gap title->subtitle (fraction of company size)
    # START / FINISH labels ---------------------------------------------------
    "label_size_frac": 0.0150,              # START / FINISH size, fraction of height
    "label_tracking": 0.10,
    "label_offset_frac": 0.66,              # perpendicular offset to the cap edge (x card width)
    "label_tip_inset_frac": 0.55,           # START/FINISH anchor, distance in from the tip (x card)
    # Credit ------------------------------------------------------------------
    "credit_text": "sentrada",
    "credit_size_frac": 0.0150,
    "credit_tracking": 0.18,                # discreet letterspacing
    "credit_margin_frac": 0.030,            # inset from the bottom-right corner
    # Ink / stock realism -----------------------------------------------------
    # A faint sub-pixel blur simulates ink spreading into the card (mirrors the
    # newspaper engine), scaled with resolution and capped low.
    "text_blur_px": 0.4,
    "blur_reference_width": 1491,
    "ink_blur_cap": 0.7,
    # Texture modulation: the ink is multiplied by a HIGH-PASS of the card (the
    # local grain and moulded shadows, normalised to ~1.0) so light ink reads as
    # printed into the stock without being crushed on the dark ground. The base
    # is a Gaussian of the luminance at this radius (fraction of width); the
    # modulation is clamped to [lo, hi].
    "texture_base_frac": 0.012,
    "texture_lo": 0.72,
    "texture_hi": 1.10,
    "texture_strength": 0.85,               # 0 = no texture, 1 = full high-pass
    # Detection ---------------------------------------------------------------
    # The coloured cards are thresholded off the near-black ground (a pixel is a
    # card if it is far enough from the sampled background OR saturated and bright
    # enough), solidified into one snake, thinned to a centreline, then cut at the
    # divider grooves. Lengths are fractions of the image; kernels are fractions
    # of the min dimension; Hough/cluster lengths are multiples of the snake width.
    "detect_bg_distance": 60,               # min RGB distance from background
    "detect_min_sat": 45,                   # ...or min HSV saturation
    "detect_min_val": 55,                   # with at least this value
    "detect_val_floor": 33,                 # reject anything darker than this (deep shadow)
    "detect_close_frac": 0.010,             # solidify: close grain holes (frac of min dim)
    "detect_open_frac": 0.0085,             # ...then smooth the boundary
    # Divider grooves are found as dark valleys in the centreline luminance.
    "detect_lum_smooth_frac": 0.075,        # smooth the centreline luminance (x snake width)
    "detect_valley_step_frac": 0.016,       # arc resample step (x snake width)
    "detect_valley_shoulder_frac": 0.42,    # shoulder distance for the valley test (x card)
    "detect_valley_core_frac": 0.16,        # core window for the local minimum (x card)
    "detect_valley_depth": 13,              # min luminance drop groove vs shoulders
    "detect_cluster_frac": 0.50,            # merge grooves within this (x snake width)
    "detect_cap_clear_frac": 0.40,          # ignore grooves within this of the caps (x width)
    "detect_gapfill_ratio": 1.55,           # safety net: recover a groove in a gap larger than this x median
    "detect_valley_snap_frac": 0.28,        # snap a recovered cut to the deepest valley within this (x median)
    "detect_inset_along": 0.86,             # usable rect: fraction of card length used
    "detect_inset_across": 0.86,            # ...and of card width
    "detect_title_erode_frac": 0.022,       # clearance from cards for the title zone (frac width)
    # Physical page size, for downsampling a supersampled render to print res.
    # Orientation is taken from the template (this board is A2 landscape).
    "a2_mm": (594, 420),                    # A2 landscape (long edge first)
    # Print production (off by default; see the newspaper engine for rationale).
    "crop_mark_mm": 4.0,
    "crop_mark_weight_mm": 0.12,
    "crop_mark_gap_mm": 1.5,
}

# Reference colours for naming detected segments (debug aid only).
PALETTE = {
    "terracotta": (198, 96, 54),
    "burgundy":   (140, 50, 52),
    "sage":       (108, 122, 96),
    "gold":       (196, 150, 70),
}


# ---------------------------------------------------------------------------
# Pango helpers (same idiom as the newspaper / crossword engines).
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


def build_attrs(text, *, leading_px=None, letter_spacing_px=None):
    attrs = Pango.AttrList()
    if leading_px is not None:
        attrs.insert(_whole_range(
            Pango.attr_line_height_new_absolute(int(round(leading_px * SCALE))), text))
    if letter_spacing_px is not None:
        attrs.insert(_whole_range(
            Pango.attr_letter_spacing_new(int(round(letter_spacing_px * SCALE))), text))
    return attrs


def draw_layout(cr, layout, x, y, ink):
    cr.save()
    cr.set_source_rgb(*ink)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def measure(layout):
    return layout.get_pixel_size()


def largest_fitting(feasible, lo, hi, iters=34):
    """Largest value in [lo, hi] for which feasible(value) is True, assuming
    feasible is monotone. Used to size text to fill a zone without overflowing."""
    if not feasible(lo):
        return lo, False
    if feasible(hi):
        return hi, True
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return lo, True


def smarten(text):
    """Typographic apostrophes/quotes and an ellipsis, with the house rule that
    em dashes become a comma break (mirrors the sibling engines). British English
    copy comes in clean; this just normalises punctuation glyphs."""
    if text is None:
        return ""
    for ch in ("—", "―", "‒", "⸺", "⸻"):
        text = text.replace(" " + ch + " ", ", ").replace(ch, ", ")
    text = text.replace(" -- ", ", ").replace("--", ", ").replace("...", "…")
    out, opener = [], " \t\n([{‘“"
    for i, ch in enumerate(text):
        prev = text[i - 1] if i > 0 else ""
        if ch == '"':
            out.append("“" if (prev == "" or prev in opener) else "”")
        elif ch == "'":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            out.append("‘" if ((prev == "" or prev in opener) and not nxt.isdigit()) else "’")
        else:
            out.append(ch)
    return "".join(out)


# ===========================================================================
# SEGMENT DETECTION (the `detect` subcommand).
#
# The path segments are irregular coloured cards baked into the template, divided
# by thin embossed grooves (not background-coloured gaps), so a plain threshold
# fills the whole snake into one blob. Detection therefore:
#   1. Thresholds the coloured cards off the charcoal ground into a SOLID snake.
#   2. Thins the snake to a 1px centreline and orders it from the bottom-left
#      start cap to the top-right finish cap.
#   3. Finds the divider grooves as straight lines spanning the snake width
#      (Hough), projects them onto the centreline, and clusters them into cuts.
#   4. Recovers any missed divider by spacing (gap fill), protecting the longer
#      end caps so they stay single segments.
#   5. Builds each segment's geometry (centroid, band polygon, usable text
#      rectangle, path-direction angle, colour) from the centreline + local width.
# Everything is stored as fractions of the image, so the map is resolution
# independent and survives the 8x upscale before print.
# ===========================================================================


def _load_cv():
    import cv2  # imported lazily so `render` works without OpenCV installed
    return cv2


def _kernel(n):
    cv2 = _load_cv()
    n = n if n % 2 == 1 else n + 1
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))


def _fill_holes(binary):
    """Fill interior holes in a binary mask (card grain leaves speckle holes) by
    flood-filling the background from a corner and OR-ing in the rest."""
    cv2 = _load_cv()
    h, w = binary.shape
    ff = binary.copy()
    m = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, m, (0, 0), 255)
    return binary | cv2.bitwise_not(ff)


def _background_colour(img_rgb):
    H, W = img_rgb.shape[:2]
    k = max(4, int(0.02 * min(H, W)))
    corners = np.concatenate([
        img_rgb[:k, :k].reshape(-1, 3), img_rgb[:k, -k:].reshape(-1, 3),
        img_rgb[-k:, :k].reshape(-1, 3), img_rgb[-k:, -k:].reshape(-1, 3)])
    return np.median(corners, axis=0)


def _card_mask(img_rgb, cfg):
    """Boolean mask (uint8 0/255) of the coloured card pixels against the
    near-black ground: a pixel is a card pixel if it is far enough from the
    sampled background colour, or saturated and bright enough."""
    cv2 = _load_cv()
    bg = _background_colour(img_rgb)
    dist = np.linalg.norm(img_rgb.astype(np.float32) - bg, axis=2)
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    sat, val = hsv[:, :, 1], hsv[:, :, 2]
    mask = ((dist > cfg["detect_bg_distance"]) |
            ((sat > cfg["detect_min_sat"]) & (val > cfg["detect_min_val"])))
    mask = (mask & (val > cfg["detect_val_floor"])).astype(np.uint8) * 255
    return mask


def _solid_path(img_rgb, cfg):
    """The card mask solidified into one filled snake (close grain holes, fill
    interiors, open to smooth the boundary)."""
    cv2 = _load_cv()
    H, W = img_rgb.shape[:2]
    md = min(H, W)
    mask = _card_mask(img_rgb, cfg)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _kernel(int(cfg["detect_close_frac"] * md)))
    mask = _fill_holes(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _kernel(int(cfg["detect_open_frac"] * md)))
    return mask


def _norm_pm90(angle):
    """Fold an angle (degrees) into (-90, 90] so text never sets upside down."""
    while angle <= -90:
        angle += 180
    while angle > 90:
        angle -= 180
    return angle


def _colour_name(rgb):
    best, bestd = None, 1e9
    for name, ref in PALETTE.items():
        d = sum((rgb[i] - ref[i]) ** 2 for i in range(3))
        if d < bestd:
            best, bestd = name, d
    return best


def _centreline(path, W, H):
    """Thin the solid snake to a 1px centreline and order it from the bottom-left
    cap to the top-right cap by a breadth-first walk between the two extreme tips.
    Returns the ordered (N, 2) array of (x, y) points."""
    cv2 = _load_cv()
    from collections import deque
    skel = cv2.ximgproc.thinning(path)
    ys, xs = np.where(skel > 0)
    pts = set(zip(xs.tolist(), ys.tolist()))
    if not pts:
        raise RuntimeError("centreline thinning produced no pixels; check the card mask")

    def nbrs(p):
        x, y = p
        return [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                if (dx or dy) and (x + dx, y + dy) in pts]

    endpoints = [p for p in pts if len(nbrs(p)) == 1]
    cand = endpoints if endpoints else list(pts)
    start = min(cand, key=lambda p: p[0] ** 2 + (p[1] - H) ** 2)        # bottom-left
    finish = min(cand, key=lambda p: (p[0] - W) ** 2 + p[1] ** 2)       # top-right

    prev = {start: None}
    q = deque([start])
    while q:
        u = q.popleft()
        if u == finish:
            break
        for v in nbrs(u):
            if v not in prev:
                prev[v] = u
                q.append(v)
    if finish not in prev:
        raise RuntimeError("centreline endpoints are not connected")
    chain = []
    u = finish
    while u is not None:
        chain.append(u)
        u = prev[u]
    return np.array(chain[::-1], dtype=np.float64)


def _arc_length(P):
    d = np.sqrt(((P[1:] - P[:-1]) ** 2).sum(1))
    return np.concatenate([[0.0], np.cumsum(d)])


def _divider_cuts(img_rgb, path, P, s, dt, cfg):
    """Find the divider grooves directly, as dark VALLEYS in the luminance sampled
    ALONG the centreline. Each groove crosses the centreline as a local minimum
    that is meaningfully darker than the tile faces on either side; sampling on the
    centreline (always inside the snake) avoids the background and the card grain.
    This is far more reliable than fitting straight lines and then filling missed
    ones by even spacing, which drifts off the real, irregular tile boundaries.
    Returns the sorted interior groove arc-positions and the snake width."""
    cv2 = _load_cv()
    H, W = img_rgb.shape[:2]
    width = 2.0 * float(dt.max())
    total = s[-1]
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    xi = np.clip(P[:, 0].astype(int), 0, W - 1)
    yi = np.clip(P[:, 1].astype(int), 0, H - 1)
    lum = gray[yi, xi]
    k = max(3, int(cfg["detect_lum_smooth_frac"] * width)) | 1
    lum = np.convolve(lum, np.ones(k) / k, mode="same")

    # Resample to a uniform arc step, then run a valley detector: a point is a
    # groove if it is the local minimum of a small core window AND darker than the
    # brighter of its two shoulders (about half a card away) by a depth threshold.
    step = max(1.0, cfg["detect_valley_step_frac"] * width)
    su = np.arange(0.0, total, step)
    lu = np.interp(su, s, lum)
    card = width  # cards are roughly square, so the snake width sets the card scale
    sh = max(2, int(cfg["detect_valley_shoulder_frac"] * card / step))
    win = max(1, int(cfg["detect_valley_core_frac"] * card / step))
    depth = cfg["detect_valley_depth"]
    raw = []
    for i in range(sh, len(lu) - sh):
        if lu[i] > lu[i - win:i + win + 1].min() + 0.5:
            continue
        shoulder = min(lu[i - sh:i - win].max(), lu[i + win:i + sh].max())
        if shoulder - lu[i] >= depth:
            raw.append((su[i], lu[i]))

    # Merge valleys closer than half a card, keeping the deeper one.
    raw.sort()
    merge = cfg["detect_cluster_frac"] * width
    grooves = []
    for arc, val in raw:
        if grooves and arc - grooves[-1][0] < merge:
            if val < grooves[-1][1]:
                grooves[-1] = (arc, val)
            continue
        grooves.append((arc, val))
    edge = cfg["detect_cap_clear_frac"] * width
    grooves = [g[0] for g in grooves if edge < g[0] < total - edge]
    return grooves, width, su, lu


def _fill_cuts(grooves, total, cfg, su, lu):
    """Recover a divider the valley pass missed (a low-contrast groove between two
    similarly coloured tiles shows up only as a shallow dip): for any interior gap
    well over the median card length, insert the expected number of cuts, but SNAP
    each to the deepest luminance valley in a window around its even-spaced guess
    so the recovered cut lands on the real (faint) groove, not an even-spaced
    estimate that would drift off the tile. The first and last intervals (the
    longer end caps) are protected from splitting."""
    allc = np.array([0.0] + list(grooves) + [total])
    gaps = np.diff(allc)
    interior = gaps[1:-1] if len(gaps) > 2 else gaps
    m = float(np.median(interior)) if len(interior) else total / 30.0
    n = len(allc)
    snap = cfg["detect_valley_snap_frac"] * m

    def deepest_valley(centre):
        lo, hi = centre - snap, centre + snap
        win = (su >= lo) & (su <= hi)
        if not np.any(win):
            return centre
        idx = np.where(win)[0]
        return float(su[idx[int(np.argmin(lu[idx]))]])

    out = []
    for gi, (a, b) in enumerate(zip(allc[:-1], allc[1:])):
        out.append(a)
        g = b - a
        is_cap = (gi == 0 or gi == n - 2)
        if (not is_cap) and g > cfg["detect_gapfill_ratio"] * m:
            k = int(round(g / m)) - 1
            for j in range(1, k + 1):
                out.append(deepest_valley(a + g * j / (k + 1)))
    out.append(total)
    return sorted(set(round(x, 2) for x in out)), m


def _segment_geometry(P, s, dt, a, b, inset_along, inset_across):
    """Build one segment's geometry from the centreline interval [a, b] (arc
    positions): centroid, band polygon, usable rectangle and path-direction angle.
    All in pixel coordinates."""
    def idx_at(arc):
        return int(np.argmin(np.abs(s - arc)))
    ia, ib, im = idx_at(a), idx_at(b), idx_at((a + b) / 2.0)
    cx, cy = P[im]
    j0, j1 = max(0, im - 5), min(len(P) - 1, im + 5)
    tang = P[j1] - P[j0]
    angle = _norm_pm90(math.degrees(math.atan2(tang[1], tang[0])))
    along = b - a
    seg_idx = np.clip(np.arange(ia, ib + 1), 0, len(P) - 1)
    half = float(np.median(dt[P[seg_idx, 1].astype(int), P[seg_idx, 0].astype(int)]))
    across = 2.0 * half
    # Band polygon: offset the centreline interval by +/- the local half width.
    step = max(1, (ib - ia) // 6)
    left, right = [], []
    for k in seg_idx[::step]:
        t = P[min(len(P) - 1, k + 2)] - P[max(0, k - 2)]
        nl = math.hypot(t[0], t[1]) or 1.0
        nx, ny = -t[1] / nl, t[0] / nl
        left.append((P[k][0] + nx * half, P[k][1] + ny * half))
        right.append((P[k][0] - nx * half, P[k][1] - ny * half))
    polygon = left + right[::-1]
    usable = {"cx": cx, "cy": cy, "w": along * inset_along, "h": across * inset_across,
              "angle": angle}
    return (cx, cy), polygon, usable, angle


def detect(template_path, output_path, cfg=CONFIG, overlay_path=None):
    cv2 = _load_cv()
    pil = Image.open(template_path).convert("RGB")
    img = np.array(pil)
    H, W = img.shape[:2]
    print(f"[info] template {os.path.basename(template_path)}: {W}x{H}px")

    path = _solid_path(img, cfg)
    cov = float((path > 0).mean())
    print(f"[info] path mask covers {cov:.3f} of the image")
    dt = cv2.distanceTransform((path > 0).astype(np.uint8), cv2.DIST_L2, 5)
    P = _centreline(path, W, H)
    s = _arc_length(P)
    print(f"[info] centreline: {len(P)} points, arc length {s[-1]:.0f}px, "
          f"snake width ~{2 * dt.max():.0f}px")

    grooves, width, su, lu = _divider_cuts(img, path, P, s, dt, cfg)
    cuts, card_len = _fill_cuts(grooves, s[-1], cfg, su, lu)
    nseg = len(cuts) - 1
    print(f"[info] grooves: {len(grooves)} detected (luminance valleys), median card "
          f"{card_len:.0f}px -> {nseg} segments after gap recovery")

    segments = []
    for i in range(nseg):
        (cx, cy), poly, usable, angle = _segment_geometry(
            P, s, dt, cuts[i], cuts[i + 1],
            cfg["detect_inset_along"], cfg["detect_inset_across"])
        # Sample the card colour from a small patch at the centroid.
        x0, y0 = int(np.clip(cx - 6, 0, W - 1)), int(np.clip(cy - 6, 0, H - 1))
        patch = img[y0:y0 + 12, x0:x0 + 12].reshape(-1, 3)
        colour = [int(round(v)) for v in patch.mean(axis=0)]
        segments.append({
            "index": i + 1,
            "centroid": [cx / W, cy / H],
            "polygon": [[px / W, py / H] for px, py in poly],
            "usable_rect": {"cx": cx / W, "cy": cy / H, "w": usable["w"] / W,
                            "h": usable["h"] / H, "angle": round(angle, 2)},
            "rotation_deg": round(angle, 2),
            "colour": colour,
            "colour_name": _colour_name(colour),
        })

    # START / FINISH anchors: the moulded arrow and laurel-trophy emblems sit at
    # the rounded centreline TIPS, which can fall inside a longer end-cap segment
    # (so the segment centroid is not on the emblem). Anchor the labels to the
    # tips directly, a little way in from each end, with the local tangent and
    # half-width so the renderer can offset the word to the cap edge.
    total = s[-1]

    def _anchor(arc):
        i = int(np.argmin(np.abs(s - arc)))
        j0, j1 = max(0, i - 5), min(len(P) - 1, i + 5)
        t = P[j1] - P[j0]
        ang = _norm_pm90(math.degrees(math.atan2(t[1], t[0])))
        hw = float(dt[int(P[i][1]), int(P[i][0])])
        return {"cx": P[i][0] / W, "cy": P[i][1] / H, "angle": round(ang, 2),
                "hw": hw / H}

    inset = cfg["label_tip_inset_frac"] * card_len
    title_zone = _detect_title_zone(path, cfg)
    seg_map = {
        "image_width": W,
        "image_height": H,
        "segment_count": len(segments),
        "title_zone": {"x": round(title_zone[0], 4), "y": round(title_zone[1], 4),
                       "w": round(title_zone[2], 4), "h": round(title_zone[3], 4)},
        "start_anchor": _anchor(min(inset, total / 2)),
        "finish_anchor": _anchor(max(total - inset, total / 2)),
        "segments": segments,
    }
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(seg_map, fh, indent=2)
    print(f"[done] wrote segment map: {output_path} ({len(segments)} segments)")

    overlay_path = overlay_path or os.path.splitext(output_path)[0] + "_overlay.png"
    _draw_detection_overlay(img, seg_map, overlay_path)
    print(f"[done] wrote detection overlay: {overlay_path}")
    return seg_map


def _largest_empty_rect(empty, x0, y0, x1, y1):
    """Largest all-True axis-aligned rectangle inside empty[y0:y1, x0:x1], by the
    classic histogram/stack method. Returns (rx, ry, rw, rh) in `empty` coords."""
    sub = empty[y0:y1, x0:x1]
    rows, cols = sub.shape
    if rows == 0 or cols == 0:
        return None
    height = np.zeros(cols, dtype=np.int32)
    best = (0, 0, 0, 0, 0)
    for r in range(rows):
        height = np.where(sub[r], height + 1, 0)
        stack = []
        c = 0
        while c <= cols:
            cur = height[c] if c < cols else 0
            start = c
            while stack and stack[-1][1] >= cur:
                s_col, s_h = stack.pop()
                area = s_h * (c - s_col)
                if area > best[0]:
                    best = (area, s_col, r - s_h + 1, c - s_col, s_h)
                start = s_col
            stack.append((start, cur))
            c += 1
    if best[0] == 0:
        return None
    _, bx, by, bw, bh = best
    return x0 + bx, y0 + by, bw, bh


def _detect_title_zone(path, cfg):
    """The title sits in the largest open dark rectangle between the path curves.
    Erode the empty (non-card) region for clearance from the cards, restrict the
    search to the path's interior bounding box, and take the largest empty
    rectangle."""
    cv2 = _load_cv()
    H, W = path.shape
    ys, xs = np.where(path > 0)
    if len(xs) == 0:
        return (0.30, 0.40, 0.40, 0.20)
    px0, px1, py0, py1 = xs.min(), xs.max(), ys.min(), ys.max()
    empty = path == 0
    clear = max(3, int(cfg["detect_title_erode_frac"] * W))
    empty = cv2.erode(empty.astype(np.uint8), _kernel(clear)) > 0
    scale = max(1, int(min(H, W) / 360))
    small = empty[::scale, ::scale]
    sx0, sy0 = px0 // scale, py0 // scale
    sx1 = min(small.shape[1], px1 // scale + 1)
    sy1 = min(small.shape[0], py1 // scale + 1)
    r = _largest_empty_rect(small, sx0, sy0, sx1, sy1)
    if r is None:
        return (0.30, 0.40, 0.40, 0.20)
    rx, ry, rw, rh = (v * scale for v in r)
    return (rx / W, ry / H, rw / W, rh / H)


def _draw_detection_overlay(img_rgb, seg_map, out_path):
    """Numbered overlay for visual verification: each segment's band polygon,
    usable rectangle, centroid and index in path order, plus the title zone."""
    cv2 = _load_cv()
    W, H = seg_map["image_width"], seg_map["image_height"]
    ov = img_rgb.copy()
    prev = None
    for s in seg_map["segments"]:
        poly = np.array([[int(px * W), int(py * H)] for px, py in s["polygon"]], np.int32)
        cv2.polylines(ov, [poly], True, (0, 255, 255), 2)
        cx, cy = int(s["centroid"][0] * W), int(s["centroid"][1] * H)
        u = s["usable_rect"]
        box = cv2.boxPoints(((u["cx"] * W, u["cy"] * H), (u["w"] * W, u["h"] * H), u["angle"]))
        cv2.polylines(ov, [box.astype(np.int32)], True, (80, 220, 80), 1)
        if prev is not None:
            cv2.line(ov, prev, (cx, cy), (255, 0, 255), 1)
        prev = (cx, cy)
        cv2.circle(ov, (cx, cy), 4, (255, 0, 255), -1)
        cv2.putText(ov, str(s["index"]), (cx - 10, cy + 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    tz = seg_map["title_zone"]
    cv2.rectangle(ov, (int(tz["x"] * W), int(tz["y"] * H)),
                  (int((tz["x"] + tz["w"]) * W), int((tz["y"] + tz["h"]) * H)),
                  (0, 165, 255), 2)
    Image.fromarray(ov).save(out_path)

# ===========================================================================
# TEXT PLACEMENT (the render path).
# ===========================================================================


def _draw_rotated_centred(cr, cx, cy, angle_deg, layout, ink, dy=0.0):
    """Draw a Pango layout centred at (cx, cy), rotated by angle_deg. dy shifts the
    block along its own vertical axis (after rotation)."""
    w, h = measure(layout)
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(math.radians(angle_deg))
    cr.translate(-w / 2.0, -h / 2.0 + dy)
    cr.set_source_rgb(*ink)
    cr.move_to(0, 0)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def render_segment_copy(cr, ctx, seg, text, is_instruction):
    """Place a segment's copy inside its usable rectangle, rotated to the path
    direction, auto-sized to fit. Copy is immutable: if it will not fit even at the
    minimum size the segment is left blank (number only) and a failure is recorded
    for P4 to shorten the line. Returns True if the copy was placed."""
    cfg, H = ctx["cfg"], ctx["H"]
    W = ctx["W"]
    u = seg["usable_rect"]
    cx, cy = u["cx"] * W, u["cy"] * H
    angle = u["angle"]
    pad = cfg["copy_pad_frac"]
    rect_w = u["w"] * W * (1 - pad)
    rect_h = u["h"] * H * (1 - pad)

    text = smarten(text)
    layout = make_layout(cr)
    layout.set_width(int(max(8, rect_w) * SCALE))
    layout.set_wrap(Pango.WrapMode.WORD)
    layout.set_alignment(Pango.Alignment.CENTER)

    def fits(size):
        set_font(layout, cfg["font_copy"], size)
        layout.set_attributes(build_attrs(text, leading_px=cfg["copy_lead"] * size))
        layout.set_text(text, -1)
        w, h = measure(layout)
        return w <= rect_w and h <= rect_h

    # Word wrapping makes "fits" NON-monotone: a smaller font can wrap to fewer
    # lines whose widest line then overruns the rect, so the feasible sizes form
    # an interval, not a half-line. A plain largest-fitting (which assumes the
    # floor fits) would wrongly reject a card that fits comfortably one size up.
    # Scan from the max size down and take the largest that fits; only fail if no
    # size in the range fits at all.
    lo, hi = cfg["copy_size_min"] * H, cfg["copy_size_max"] * H
    step = max(0.2, (hi - lo) / 80.0)
    size, ok = lo, False
    s = hi
    while s >= lo:
        if fits(s):
            size, ok = s, True
            break
        s -= step
    set_font(layout, cfg["font_copy"], size)
    layout.set_attributes(build_attrs(text, leading_px=cfg["copy_lead"] * size))
    layout.set_text(text, -1)
    if not ok:
        ctx["overflow"].append((seg["index"], text))
        return False
    _draw_rotated_centred(cr, cx, cy, angle, layout, cfg["cream"])
    return True


def render_segment_number(cr, ctx, seg):
    """Small segment number in the top-left corner of the usable rectangle, in the
    rotated frame of the card so it sits squarely on the stock."""
    cfg, H, W = ctx["cfg"], ctx["H"], ctx["W"]
    u = seg["usable_rect"]
    cx, cy = u["cx"] * W, u["cy"] * H
    rw, rh = u["w"] * W, u["h"] * H
    size = cfg["number_size_frac"] * H
    nl = make_layout(cr)
    set_font(nl, cfg["font_number"], size)
    nl.set_text(str(seg["index"]), -1)
    inset = cfg["number_inset_frac"]
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(math.radians(u["angle"]))
    cr.translate(-rw / 2.0 + inset * rw, -rh / 2.0 + inset * rh)
    cr.set_source_rgb(*cfg["gold_dim"])
    cr.move_to(0, 0)
    PangoCairo.show_layout(cr, nl)
    cr.restore()


def render_title(cr, ctx, data):
    """THE / [COMPANY] / GAME stacked and centred in the title zone, with the
    strapline beneath, all in gold on the dark ground. Sized so the block fills the
    title zone without touching the path."""
    cfg, W, H = ctx["cfg"], ctx["W"], ctx["H"]
    tz = ctx["title_zone_px"]
    zx, zy, zw, zh = tz
    company = (data.get("company_name") or "").strip().upper()
    title = data.get("title")
    if title and not company:
        # Derive the company line from "THE X GAME" if only a title was supplied.
        t = title.upper().strip()
        if t.startswith("THE ") and t.endswith(" GAME"):
            company = t[4:-5].strip()
        else:
            company = t
    subtitle = smarten(data.get("subtitle") or "Navigate the chaos. Try not to lose your mind.")

    gold = cfg["gold"]
    fill_w = zw * cfg["title_zone_fill"]
    fill_h = zh * cfg["title_zone_fill"]

    def company_layout(size):
        cl = make_layout(cr)
        set_font(cl, cfg["font_title"], size)
        cl.set_text(company, -1)
        return cl

    def the_layout(size):
        the_size = size * cfg["title_the_frac"]
        tl = make_layout(cr)
        set_font(tl, cfg["font_title"], the_size)
        tl.set_attributes(build_attrs("THE", letter_spacing_px=cfg["title_tracking_the"] * the_size))
        tl.set_text("THE", -1)
        return tl, the_size

    def game_layout(size):
        gl = make_layout(cr)
        set_font(gl, cfg["font_title"], size)
        gl.set_text("GAME", -1)
        return gl

    def sub_layout(size):
        sl = make_layout(cr)
        set_font(sl, cfg["font_subtitle"], size * cfg["subtitle_size_frac"])
        sl.set_width(int(fill_w * SCALE))
        sl.set_wrap(Pango.WrapMode.WORD)
        sl.set_alignment(Pango.Alignment.CENTER)
        sl.set_text(subtitle, -1)
        return sl

    gap = lambda size: cfg["title_gap_frac"] * size
    sub_gap = lambda size: cfg["subtitle_gap_frac"] * size

    def block_metrics(size):
        cl = company_layout(size)
        cw, ch = measure(cl)
        tl, _ = the_layout(size)
        tw, th = measure(tl)
        gl = game_layout(size)
        gw, gh = measure(gl)
        sl = sub_layout(size)
        sw, sh = measure(sl)
        total_h = th + gap(size) + ch + gap(size) + gh + sub_gap(size) + sh
        max_w = max(cw, tw, gw, sw)
        return total_h, max_w, (cl, tl, gl, sl, th, ch, gh, sh)

    def fits(size):
        h, w, _ = block_metrics(size)
        return h <= fill_h and w <= fill_w

    size, _ = largest_fitting(fits, 0.018 * H, cfg["title_company_max"] * H)
    total_h, _, (cl, tl, gl, sl, th, ch, gh, sh) = block_metrics(size)

    # Centre the block vertically in the title zone; centre each line horizontally.
    cxc = zx + zw / 2.0
    y = zy + (zh - total_h) / 2.0
    for lay, lh in ((tl, th), (cl, ch), (gl, gh)):
        lw = measure(lay)[0]
        draw_layout(cr, lay, cxc - lw / 2.0, y, gold)
        y += lh + gap(size)
    y += sub_gap(size) - gap(size)
    # The subtitle is a CENTER-aligned layout with a set_width box, so its text
    # centres within that box: position the box (not its natural width) on cxc.
    draw_layout(cr, sl, cxc - fill_w / 2.0, y, gold)


def render_start_finish(cr, ctx):
    """START on the first segment and FINISH on the last, small bold caps in gold,
    rotated to their cap and offset perpendicular toward the outer edge so they sit
    clear of the arrow / laurel-trophy emblems already moulded into the caps (the
    template provides the symbols; the engine adds the words)."""
    cfg, W, H = ctx["cfg"], ctx["W"], ctx["H"]
    size = cfg["label_size_frac"] * H
    cxC, cyC = W / 2.0, H / 2.0
    anchors = (("START", ctx["seg_map"].get("start_anchor")),
               ("FINISH", ctx["seg_map"].get("finish_anchor")))

    for which, a in anchors:
        if not a:
            continue
        cx, cy = a["cx"] * W, a["cy"] * H
        th = math.radians(a["angle"])
        # Perpendicular to the cap, pointing AWAY from the board centre (into the
        # dark margin / cap edge), so the word never lands on the moulded emblem.
        perp = (-math.sin(th), math.cos(th))
        if perp[0] * (cx - cxC) + perp[1] * (cy - cyC) < 0:
            perp = (-perp[0], -perp[1])
        off = cfg["label_offset_frac"] * (2 * a["hw"] * H)
        lx, ly = cx + perp[0] * off, cy + perp[1] * off
        lab = make_layout(cr)
        set_font(lab, cfg["font_label"], size)
        lab.set_attributes(build_attrs(which, letter_spacing_px=cfg["label_tracking"] * size))
        lab.set_text(which, -1)
        lw, lh = measure(lab)
        cr.save()
        cr.translate(lx, ly)
        cr.rotate(th)
        cr.set_source_rgb(*cfg["gold"])
        cr.move_to(-lw / 2.0, -lh / 2.0)
        PangoCairo.show_layout(cr, lab)
        cr.restore()


def render_credit(cr, ctx):
    """Lowercase 'sentrada' credit, discreet, letterspaced, gold, in the
    bottom-right corner on the dark ground."""
    cfg, W, H = ctx["cfg"], ctx["W"], ctx["H"]
    text = cfg["credit_text"]
    size = cfg["credit_size_frac"] * H
    cl = make_layout(cr)
    set_font(cl, cfg["font_credit"], size)
    cl.set_attributes(build_attrs(text, letter_spacing_px=cfg["credit_tracking"] * size))
    cl.set_text(text, -1)
    cw, ch = measure(cl)
    m = cfg["credit_margin_frac"]
    x = W - m * W - cw
    y = H - m * H - ch
    draw_layout(cr, cl, x, y, cfg["gold"])


# ---------------------------------------------------------------------------
# Compositing: light ink printed into dark stock.
# ---------------------------------------------------------------------------


def cairo_to_pil(surface):
    surface.flush()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def composite_ink(template_rgb, text_rgba, cfg, W, ink_blur=None):
    """Composite the light ink onto the dark card so it reads as printed into the
    stock. A literal multiply (as on the newspaper's cream paper) would crush light
    ink on a dark ground, so instead the ink is modulated by a HIGH-PASS of the
    card luminance -- the local grain and moulded 3D shadows, normalised to ~1.0 --
    and then alpha-composited. The texture shows through the ink; the ink keeps its
    brightness."""
    if ink_blur is None:
        blur = min(cfg["ink_blur_cap"], cfg["text_blur_px"] * (W / float(cfg["blur_reference_width"])))
    else:
        blur = ink_blur
    if blur > 0:
        text_rgba = text_rgba.filter(ImageFilter.GaussianBlur(blur))

    card = np.asarray(template_rgb.convert("RGB"), dtype=np.float32)
    ink = np.asarray(text_rgba, dtype=np.float32)
    rgb, alpha = ink[:, :, :3], ink[:, :, 3:4] / 255.0

    # High-pass texture: luminance over a local Gaussian base, clamped.
    lum = (0.299 * card[:, :, 0] + 0.587 * card[:, :, 1] + 0.114 * card[:, :, 2])
    radius = max(1.0, cfg["texture_base_frac"] * W)
    base = np.asarray(Image.fromarray(lum.astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    ratio = lum / np.clip(base, 1.0, None)
    ratio = np.clip(ratio, cfg["texture_lo"], cfg["texture_hi"])
    strength = cfg["texture_strength"]
    tex = (1.0 - strength) + strength * ratio
    ink_mod = np.clip(rgb * tex[:, :, None], 0, 255)

    out = card * (1.0 - alpha) + ink_mod * alpha
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


# ---------------------------------------------------------------------------
# Output finishing (downsample to print DPI, sRGB tag, bleed + crop marks).
# Same behaviour as the newspaper / crossword engines.
# ---------------------------------------------------------------------------


def finalize_output(result, cfg, print_dpi, print_size):
    if print_size:
        tw, th = print_size
    elif print_dpi:
        mm = cfg["a2_mm"]
        tw = int(round(mm[0] / 25.4 * print_dpi))
        th = int(round(mm[1] / 25.4 * print_dpi))
    else:
        return result, None
    src_ar, dst_ar = result.width / result.height, tw / th
    if abs(src_ar - dst_ar) > 0.01:
        print(f"[warn] template aspect ratio {src_ar:.4f} differs from the print "
              f"target {dst_ar:.4f}; the downsample will distort slightly.")
    if (tw, th) != result.size:
        print(f"[info] downsampling {result.width}x{result.height} -> {tw}x{th} for print"
              + (f" at {print_dpi} DPI" if print_dpi else ""))
        result = result.resize((tw, th), Image.LANCZOS)
    return result, print_dpi


_SRGB_ICC = None


def srgb_icc_bytes():
    global _SRGB_ICC
    if _SRGB_ICC is None:
        _SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    return _SRGB_ICC


def effective_dpi(image, cfg):
    return image.width / (cfg["a2_mm"][0] / 25.4)


def add_bleed_and_marks(image, cfg, bleed_mm, crop_marks):
    if bleed_mm <= 0 and not crop_marks:
        return image
    from PIL import ImageDraw
    dpi = effective_dpi(image, cfg)
    W, H = image.size
    bleed = max(0, int(round(bleed_mm / 25.4 * dpi)))
    if crop_marks:
        L = max(2, int(round(cfg["crop_mark_mm"] / 25.4 * dpi)))
        w = max(1, int(round(cfg["crop_mark_weight_mm"] / 25.4 * dpi)))
        gap = max(bleed, int(round(cfg["crop_mark_gap_mm"] / 25.4 * dpi)))
        pad = max(2, int(round(1.0 / 25.4 * dpi)))
        margin = gap + L + pad
    else:
        margin = bleed
    # The board's ground is dark charcoal; sample it so the bleed/slug match.
    ground = tuple(int(v) for v in np.median(
        np.asarray(image)[:6, :6].reshape(-1, 3), axis=0))
    canvas = Image.new("RGB", (W + 2 * margin, H + 2 * margin), ground)
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
            d.line([(tx + hx * gap, ty), (tx + hx * (gap + L), ty)], fill=(255, 255, 255), width=w)
            d.line([(tx, ty + vy * gap), (tx, ty + vy * (gap + L))], fill=(255, 255, 255), width=w)
    return canvas


def assert_fonts_resolved(cr, cfg):
    """Hard-fail if any configured font family resolves to a substitute, so a fresh
    container missing the bundled fonts never silently renders in a fallback face
    (mirrors the sibling engines' guard)."""
    layout = PangoCairo.create_layout(cr)
    pctx = layout.get_context()
    keys = ("font_title", "font_subtitle", "font_copy", "font_label",
            "font_number", "font_credit")
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
        print(f"[error] check the bundled fonts in {FONT_DIRS}.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Checks.
# ---------------------------------------------------------------------------


def run_checks(ctx, data, cfg):
    out = []
    add = lambda lvl, field, msg: out.append((lvl, field, msg))
    n_map = ctx["seg_map"]["segment_count"]
    seg_entries = {s["index"]: s for s in data.get("segments", [])}
    n_data = len(seg_entries)
    if n_data != n_map:
        add("warn", "segments",
            f"data has {n_data} segments but the map detected {n_map}; "
            f"indices beyond {min(n_data, n_map)} are not both present")
    # Anti-pattern flag: a positive metric inverted into a penalty (P4 concern).
    for s in data.get("segments", []):
        t = (s.get("text") or "").lower()
        if s.get("type") == "content" and ("move back" in t or "go back" in t) and any(
                w in t for w in ("closes", "raises", "wins", "lands", "secures", "record", "beats")):
            add("warn", f"segment {s['index']}",
                "looks like a positive event paired with a backward move (possible "
                "metric inversion); confirm with P4")
    # Copy that would not fit even at the minimum size (recorded during render).
    for idx, text in ctx.get("overflow", []):
        add("fail", f"segment {idx}",
            f"copy does not fit the segment at the minimum size; shorten the line "
            f"(\"{text[:48]}{'…' if len(text) > 48 else ''}\")")
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


# ---------------------------------------------------------------------------
# Render driver.
# ---------------------------------------------------------------------------


def build(template_path, data_path, segments_path, output_path, cfg=CONFIG,
          print_dpi=None, print_size=None, render_dpi=None, render_size=None,
          ink_blur=None, bleed_mm=0.0, crop_marks=False):
    with open(data_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    with open(segments_path, "r", encoding="utf-8") as fh:
        seg_map = json.load(fh)

    template = Image.open(template_path).convert("RGB")
    print(f"[info] template {os.path.basename(template_path)}: {template.width}x{template.height}px")
    # Orient the print size to the template (this board is A2 landscape).
    if template.width >= template.height and cfg["a2_mm"][0] < cfg["a2_mm"][1]:
        cfg = dict(cfg, a2_mm=(cfg["a2_mm"][1], cfg["a2_mm"][0]))

    if render_size or render_dpi:
        if render_size:
            tw, th = render_size
        else:
            mm = cfg["a2_mm"]
            tw = int(round(mm[0] / 25.4 * render_dpi))
            th = int(round(mm[1] / 25.4 * render_dpi))
        if (tw, th) != template.size:
            print(f"[info] upscaling template {template.width}x{template.height} -> {tw}x{th} "
                  f"before rendering" + (f" ({render_dpi} DPI)" if render_dpi else ""))
            template = template.resize((tw, th), Image.LANCZOS)
    W, H = template.size

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surface)
    assert_fonts_resolved(cr, cfg)

    tz = seg_map["title_zone"]
    ctx = {
        "W": W, "H": H, "cfg": cfg, "seg_map": seg_map,
        "segments_by_index": {s["index"]: s for s in seg_map["segments"]},
        "title_zone_px": (tz["x"] * W, tz["y"] * H, tz["w"] * W, tz["h"] * H),
        "overflow": [],
    }

    data_by_index = {s["index"]: s for s in data.get("segments", [])}
    for seg in seg_map["segments"]:
        entry = data_by_index.get(seg["index"], {"type": "empty", "text": ""})
        render_segment_number(cr, ctx, seg)
        text = (entry.get("text") or "").strip()
        if entry.get("type") in ("content", "instruction") and text:
            render_segment_copy(cr, ctx, seg, text, entry.get("type") == "instruction")

    render_title(cr, ctx, data)
    render_start_finish(cr, ctx)
    render_credit(cr, ctx)

    text_rgba = cairo_to_pil(surface)
    result = composite_ink(template, text_rgba, cfg, W, ink_blur=ink_blur)

    result, dpi = finalize_output(result, cfg, print_dpi, print_size)
    if bleed_mm > 0 or crop_marks:
        result = add_bleed_and_marks(result, cfg, bleed_mm, crop_marks)

    diagnostics = run_checks(ctx, data, cfg)
    fails = [d for d in diagnostics if d[0] == "fail"]
    if output_path is not None:
        save_kwargs = {"icc_profile": srgb_icc_bytes()}
        if dpi:
            save_kwargs["dpi"] = (dpi, dpi)
        if fails:
            root, ext = os.path.splitext(output_path)
            quarantine = root + ".FAILED" + ext
            result.save(quarantine, **save_kwargs)
            print(f"[error] {len(fails)} layout failure(s); deliverable WITHHELD:")
            for _lvl, field, msg in fails:
                print(f"[error]   {field}: {msg}")
            print(f"[error] wrote quarantined render to {quarantine} (NOT print-ready).")
        else:
            result.save(output_path, **save_kwargs)
            print(f"[done] wrote {output_path} ({result.width}x{result.height}px)")
    return diagnostics


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _parse_size(s):
    if not s:
        return None
    w, h = s.lower().split("x")
    return (int(w), int(h))


def main():
    ap = argparse.ArgumentParser(
        description="Render a company board game onto a blank template, or detect "
                    "the template's path segments.")
    sub = ap.add_subparsers(dest="command")

    dp = sub.add_parser("detect", help="run CV segment detection and write a segment map")
    dp.add_argument("--template", required=True)
    dp.add_argument("--output", required=True, help="segment map JSON path")
    dp.add_argument("--overlay", default=None, help="overlay PNG path (default: <output>_overlay.png)")

    # Render is the default command (no subcommand), matching the spec's CLI.
    for p in (ap,):
        p.add_argument("--template", help="blank board template PNG")
        p.add_argument("--data", help="segment copy JSON (from P4)")
        p.add_argument("--segments", help="segment map JSON (from detect)")
        p.add_argument("--output", help="output PNG path (optional with --check)")
        p.add_argument("--check", action="store_true", help="validate and print a PASS/FAIL report")
        p.add_argument("--print-dpi", type=int, default=None)
        p.add_argument("--print-size", default=None)
        p.add_argument("--render-dpi", type=int, default=None)
        p.add_argument("--render-size", default=None)
        p.add_argument("--ink-blur", type=float, default=None,
                       help="ink-spread blur in px on the text (0 = razor sharp)")
        p.add_argument("--bleed-mm", type=float, default=0.0)
        p.add_argument("--crop-marks", action="store_true")

    args = ap.parse_args()

    if args.command == "detect":
        detect(args.template, args.output)
        return

    if not args.template or not args.data or not args.segments:
        ap.error("--template, --data and --segments are required for rendering")
    if not args.check and not args.output:
        ap.error("--output is required unless --check is given")

    render_dpi = args.render_dpi
    render_size = _parse_size(args.render_size)
    if args.check and not render_dpi and not render_size:
        render_dpi = 300

    diagnostics = build(
        args.template, args.data, args.segments,
        args.output if not args.check else (args.output or None),
        print_dpi=args.print_dpi, print_size=_parse_size(args.print_size),
        render_dpi=render_dpi, render_size=render_size, ink_blur=args.ink_blur,
        bleed_mm=args.bleed_mm, crop_marks=args.crop_marks)

    if args.check:
        ok = print_report(diagnostics, os.path.basename(args.data))
        sys.exit(0 if ok else 1)
    if any(d[0] == "fail" for d in diagnostics):
        sys.exit(1)


if __name__ == "__main__":
    main()
