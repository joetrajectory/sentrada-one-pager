#!/usr/bin/env python3
"""
Flat, print-honest board mock for Sentrada.

A deliberate contrast to boardgame.py: instead of faking debossed text on a
photograph of moulded leather, this draws the whole board as flat vector artwork
designed FOR the text -- clean tiles, horizontal copy, high contrast -- which is
both more legible and more honest to a flat A2 print. One-off mock for review.
"""
import ctypes, math, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
for d in (os.path.join(SCRIPT_DIR, "fonts"), os.path.normpath(os.path.join(SCRIPT_DIR, "..", "fonts"))):
    if os.path.isdir(d):
        fc = ctypes.CDLL("libfontconfig.so.1"); fc.FcConfigGetCurrent.restype = ctypes.c_void_p
        fc.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        fc.FcConfigAppFontAddDir(fc.FcConfigGetCurrent(), d.encode())
import gi
gi.require_version("Pango", "1.0"); gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo
import cairo
S = Pango.SCALE

# --- palette (Sentrada brand, lifted brighter for flat print on charcoal) ------
BG       = (0x15/255, 0x17/255, 0x1A/255)
CREAM    = (0xF7/255, 0xF1/255, 0xE7/255)
GOLD     = (0xD9/255, 0xB2/255, 0x66/255)
STONE    = (0x2A/255, 0x2C/255, 0x30/255)   # instruction / event tiles
TERRA    = (0xC8/255, 0x62/255, 0x3C/255)
BURG     = (0xA8/255, 0x42/255, 0x40/255)
SAGE     = (0x77/255, 0x86/255, 0x62/255)
GOLDTILE = (0xCB/255, 0x9C/255, 0x4E/255)
PALETTE  = [TERRA, SAGE, GOLDTILE, BURG]

W, H = 1800, 1273   # A2 landscape proportion

# --- board content: SHORT copy, one move per tile -----------------------------
# kind: start | finish | instruction | content ; move: optional chip text
TILES = [
    {"kind": "start"},
    {"kind": "content", "text": "Seed round closes", "move": "+2"},
    {"kind": "content", "text": "First live pilot on site", "move": "+1"},
    {"kind": "instruction", "text": "Roll again"},
    {"kind": "content", "text": "Materials logged at the gate", "move": "+3"},
    {"kind": "content", "text": "Carbon reporting goes live", "move": "+2"},
    {"kind": "content", "text": "A national framework signs you", "move": "+2"},
    {"kind": "instruction", "text": "Lose a turn"},
    {"kind": "instruction", "text": "Miss a go"},
    {"kind": "content", "text": "Series A oversubscribed", "move": "+3"},
    {"kind": "content", "text": "A delivery slips through", "move": "back 2"},
    {"kind": "content", "text": "Site inspections go paperless", "move": "+2"},
    {"kind": "instruction", "text": "Roll again"},
    {"kind": "content", "text": "Audit trail saves the day", "move": "+2"},
    {"kind": "content", "text": "Rolled out estate-wide", "move": "+3"},
    {"kind": "instruction", "text": "Lose a turn"},
    {"kind": "content", "text": "Environmental data wins", "move": "+2"},
    {"kind": "content", "text": "Series B oversubscribed", "move": "+3"},
    {"kind": "content", "text": "The IPO bell rings", "move": "+2"},
    {"kind": "finish"},
]


def rr(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def text(cr, s, font, size, col, cx, cy, wmax, align="center", spacing=0.0):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(font); fd.set_absolute_size(size * S)
    lay.set_font_description(fd)
    if wmax:
        lay.set_width(int(wmax * S)); lay.set_wrap(Pango.WrapMode.WORD)
    lay.set_alignment({"center": Pango.Alignment.CENTER, "left": Pango.Alignment.LEFT}[align])
    if spacing:
        al = Pango.AttrList(); at = Pango.attr_letter_spacing_new(int(spacing * size * S))
        at.start_index = 0; at.end_index = len(s.encode()); al.insert(at); lay.set_attributes(al)
    lay.set_text(s, -1)
    tw, th = lay.get_pixel_size()
    cr.save(); cr.set_source_rgb(*col)
    cr.move_to(cx - (tw / 2 if align == "center" else 0), cy - th / 2)
    PangoCairo.show_layout(cr, lay); cr.restore()
    return tw, th


def mix(c, d, t):
    return tuple(c[i] * (1 - t) + d[i] * t for i in range(3))


def layout_centres():
    """Serpentine: 3 rows of 8 (bottom L->R, middle R->L, top L->R), with the two
    central tiles of the middle row left out so the title owns the centre, exactly
    like the open middle of the brand's S-curve."""
    cols, rows = 8, 3
    mx, top, bot = 120, 250, H - 150
    ys = [bot, (top + bot) / 2, top]
    xs = [mx + i * (W - 2 * mx) / (cols - 1) for i in range(cols)]
    cen, skip = [], set()
    for r in range(rows):
        seq = xs if r % 2 == 0 else xs[::-1]
        for c, x in enumerate(seq):
            if r == 1 and c in (2, 3, 4, 5):   # middle-row centre -> title void
                skip.add(len(cen))
            cen.append((x, ys[r]))
    title = (xs[2], ys[1], xs[5], ys[1])       # span of the void
    return cen, (xs[1] - xs[0]), skip, title


def draw():
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*BG); cr.paint()

    cen, pitch, skip, title = layout_centres()
    slots = [c for i, c in enumerate(cen) if i not in skip]
    n = len(TILES)
    tile = pitch * 0.80
    r = tile * 0.16

    # connector track behind the tiles (subtle, rounded), following the full path
    cr.save(); cr.set_source_rgba(*GOLD, 0.28)
    cr.set_line_width(tile * 0.09); cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.move_to(*cen[0])
    for (x, y) in cen[1:]:
        cr.line_to(x, y)
    cr.stroke(); cr.restore()

    pi = 0
    for i in range(n):
        t = TILES[i]
        cx, cy = slots[i]
        x, y = cx - tile / 2, cy - tile / 2
        kind = t["kind"]
        if kind in ("content",):
            fill = PALETTE[pi % len(PALETTE)]; pi += 1
        elif kind == "instruction":
            fill = STONE
        elif kind == "start":
            fill = STONE
        else:
            fill = GOLDTILE

        # tile body with a soft top highlight + thin dark keyline
        rr(cr, x, y, tile, tile, r); cr.set_source_rgb(*fill); cr.fill()
        rr(cr, x, y, tile, tile, r); cr.set_source_rgba(1, 1, 1, 0.06)
        cr.rectangle(x, y, tile, tile * 0.5); cr.clip(); cr.paint(); cr.reset_clip()
        rr(cr, x, y, tile, tile, r); cr.set_source_rgba(0, 0, 0, 0.30); cr.set_line_width(2.0); cr.stroke()

        light = sum(fill[:3]) / 3 > 0.55
        ink = STONE if light else CREAM

        if kind == "start":
            text(cr, "START", "Inter Bold", tile * 0.13, GOLD, cx, cy - tile * 0.12, tile, spacing=0.12)
            cr.save(); cr.translate(cx, cy + tile * 0.12); cr.set_source_rgb(*GOLD)
            cr.set_line_width(tile * 0.045); cr.set_line_cap(cairo.LINE_CAP_ROUND)
            a = tile * 0.20
            cr.move_to(-a, 0); cr.line_to(a, 0); cr.stroke()
            cr.move_to(a * 0.4, -a * 0.5); cr.line_to(a, 0); cr.line_to(a * 0.4, a * 0.5); cr.stroke(); cr.restore()
        elif kind == "finish":
            _trophy(cr, cx, cy - tile * 0.10, tile * 0.30, STONE)
            text(cr, "FINISH", "Inter Bold", tile * 0.13, STONE, cx, cy + tile * 0.22, tile, spacing=0.12)
        elif kind == "instruction":
            text(cr, t["text"], "Fraunces Italic", tile * 0.135, GOLD, cx, cy, tile * 0.82)
        else:
            text(cr, t["text"], "Inter Semibold", tile * 0.125, ink, cx, cy - tile * 0.10, tile * 0.84)
            _chip(cr, cx, cy + tile * 0.26, tile, t.get("move", ""), fill, ink, light)

        # number badge, top-left
        nb = tile * 0.14
        cr.arc(x + nb * 1.05, y + nb * 1.05, nb * 0.62, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.28 if not light else 0.18); cr.fill()
        text(cr, str(i + 1), "Inter Bold", tile * 0.11, ink, x + nb * 1.05, y + nb * 1.02, None)

    # --- centre title block, on a charcoal panel that masks the void ----------
    tcx, tcy = (title[0] + title[2]) / 2, title[1]
    pw, ph = (title[2] - title[0]) + tile * 1.4, tile * 2.6
    rr(cr, tcx - pw / 2, tcy - ph / 2, pw, ph, tile * 0.18)
    cr.set_source_rgb(*BG); cr.fill()
    text(cr, "THE", "Fraunces Bold", 34, GOLD, tcx, tcy - 92, None, spacing=0.22)
    text(cr, "QFLOW", "Fraunces Bold", 96, GOLD, tcx, tcy - 26, None)
    text(cr, "GAME", "Fraunces Bold", 96, GOLD, tcx, tcy + 56, None)
    text(cr, "Navigate the chaos. Try not to lose your mind.", "Fraunces Italic", 27, GOLD, tcx, tcy + 120, W * 0.5)
    text(cr, "sentrada", "Inter", 22, GOLD, W - 150, H - 48, None, spacing=0.18)

    surf.write_to_png(os.path.join(SCRIPT_DIR, "qflow_flat_mock.png"))
    print("wrote qflow_flat_mock.png")


def _chip(cr, cx, cy, tile, move, fill, ink, light):
    if not move:
        return
    back = move.lower().startswith("back")
    num = move.split()[-1].lstrip("+")
    label = num
    cw, ch = tile * 0.34, tile * 0.20
    chipcol = mix(fill, (0, 0, 0), 0.34) if not light else mix(fill, (0, 0, 0), 0.22)
    rr(cr, cx - cw / 2, cy - ch / 2, cw, ch, ch * 0.5); cr.set_source_rgb(*chipcol); cr.fill()
    ctext = CREAM
    # chevron
    cr.save(); cr.translate(cx - cw * 0.22, cy); cr.set_source_rgb(*ctext)
    cr.set_line_width(tile * 0.022); cr.set_line_cap(cairo.LINE_CAP_ROUND)
    a = ch * 0.22; d = 1 if not back else -1
    cr.move_to(-a, d * a * 0.6); cr.line_to(0, -d * a * 0.6); cr.line_to(a, d * a * 0.6); cr.stroke(); cr.restore()
    text(cr, label, "Inter Bold", tile * 0.11, ctext, cx + cw * 0.12, cy, None)


def _trophy(cr, x, y, s, col):
    cr.save(); cr.set_source_rgb(*col); cr.set_line_width(s * 0.11); cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(x - s * 0.32, y - s * 0.36); cr.line_to(x + s * 0.32, y - s * 0.36)
    cr.line_to(x + s * 0.22, y + s * 0.06)
    cr.curve_to(x + s * 0.2, y + s * 0.2, x - s * 0.2, y + s * 0.2, x - s * 0.22, y + s * 0.06)
    cr.close_path(); cr.stroke()
    cr.arc(x - s * 0.34, y - s * 0.2, s * 0.15, math.radians(90), math.radians(270)); cr.stroke()
    cr.arc(x + s * 0.34, y - s * 0.2, s * 0.15, math.radians(-90), math.radians(90)); cr.stroke()
    cr.move_to(x, y + s * 0.18); cr.line_to(x, y + s * 0.34); cr.stroke()
    cr.move_to(x - s * 0.2, y + s * 0.4); cr.line_to(x + s * 0.2, y + s * 0.4); cr.stroke(); cr.restore()


if __name__ == "__main__":
    draw()
