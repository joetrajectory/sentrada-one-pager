#!/usr/bin/env python3
"""
Sentrada "The Email" layout engine.

Renders a cold email as a forensic, full-bleed Gmail reading pane and outputs a
print-ready A2 PNG. The format is the brand thesis made physical: nobody responds
to your cold outbound, so here is the email, enlarged until it is impossible to
ignore and dropped on the buyer's desk. The whole artefact is recognisable inbox
chrome plus the sender's actual words, so it is true by construction and entirely
typographic. No template image and no AI image generation: the chrome is drawn
procedurally, the body is the sender's copy, the engine just typesets it
perfectly at print size.

    python email.py --data test_qflow.json --output qflow_email.png

A direct sibling of the newspaper and crossword engines: same Pango/Cairo text
layout, same fontconfig registration, same resolution-independent geometry (every
position is a fraction of the canvas, so one layout works at any DPI), same sRGB
ICC tagging, the same --check gate and *.FAILED.png quarantine, and the same
immutable-copy rule (overflow is a flagged failure, never a silent squeeze).
"""

import argparse
import ctypes
import json
import math
import os
import re
import sys

# ---------------------------------------------------------------------------
# Register the bundled fonts with fontconfig BEFORE Pango loads, so the engine
# is self contained. Shared ../fonts first, then a local ./fonts override.
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def register_app_fonts():
    dirs = [
        os.environ.get("SENTRADA_EMAIL_FONT_DIR"),
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
from PIL import Image, ImageCms  # noqa: E402

S = Pango.SCALE

# ---------------------------------------------------------------------------
# Everything below is in fractions of the canvas (W, H) so the layout is
# resolution independent: render at any size, print at any DPI, identical
# composition. A2 portrait is the only supported trim (420 x 594 mm).
# ---------------------------------------------------------------------------
A2_MM = (420.0, 594.0)
DEFAULT_SIZE = (1488, 2105)  # ~90 dpi preview; use --print-dpi for print masters

SANS = "Roboto"  # Gmail's actual UI/body face, bundled in ./fonts for fidelity

# Forensic Gmail palette
WHITE = (1.0, 1.0, 1.0)
INK = (0x20 / 255, 0x21 / 255, 0x24 / 255)   # primary text  #202124
SUB = (0x5F / 255, 0x63 / 255, 0x68 / 255)   # secondary     #5F6368
ICONG = (0x44 / 255, 0x47 / 255, 0x46 / 255)  # toolbar icons #444746
FAINT = (0x9A / 255, 0xA0 / 255, 0xA6 / 255)  # tertiary
HAIR = (0xE6 / 255, 0xE6 / 255, 0xE6 / 255)   # dividers      #E6E6E6
LINK = (0x1A / 255, 0x73 / 255, 0xE8 / 255)   # Gmail blue     #1A73E8
CHIP = (0xE8 / 255, 0xEA / 255, 0xED / 255)   # label chip bg
CLAY = (0xC0 / 255, 0x59 / 255, 0x33 / 255)   # Sentrada accent (default avatar)

# Geometry, as fractions of W or H. Tuned to a desktop Gmail reading pane that
# fills the whole sheet (no surrounding app grey: full bleed).
G = dict(
    margin_x=0.072,      # left/right content margin (frac W)
    toolbar_y=0.030,     # toolbar baseline (frac H)
    subject_y=0.074,     # subject top (frac H)
    subject_size=0.0300, # frac H
    body_size=0.0172,    # larger body: fills the A2 and reads across a desk
    meta_size=0.0150,
    small_size=0.0128,
    avatar=0.050,        # avatar diameter (frac H)
    line_lead=1.52,      # body leading multiple
    para_gap=0.022,      # gap after a paragraph (frac H)
    hairline=0.0007,     # stroke width (frac H), clamped to >= 1px
)


# ---------------------------------------------------------------------------
# Schema / data
# ---------------------------------------------------------------------------
def load_data(path):
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    sender = d.get("sender", {})
    if "name" not in sender or "email" not in sender:
        raise SystemExit("[fatal] data.sender must have 'name' and 'email'")
    if "subject" not in d:
        raise SystemExit("[fatal] data.subject is required")
    if not d.get("body"):
        raise SystemExit("[fatal] data.body must be a non-empty list of blocks")
    return d


def typographic(s):
    """Normalise punctuation to typographic forms (house rule), leaving the
    sender's words otherwise untouched."""
    if not s:
        return s
    # No "--" -> em dash conversion: the house rule forbids em dashes, so the
    # engine must never manufacture one. Only ellipsis and curly quotes.
    s = s.replace("...", "…")
    s = s.replace("'", "’")
    # straight double quotes -> curly, naive but adequate for body copy
    out, open_q = [], True
    for ch in s:
        if ch == '"':
            out.append("“" if open_q else "”")
            open_q = not open_q
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Cairo / Pango helpers
# ---------------------------------------------------------------------------
def layout(cr, txt, size, w=None, weight=None, lead=None, color=INK):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(SANS + (f" {weight}" if weight else ""))
    fd.set_absolute_size(size * S)
    lay.set_font_description(fd)
    if w:
        lay.set_width(int(w * S))
        lay.set_wrap(Pango.WrapMode.WORD)
    if lead:
        al = Pango.AttrList()
        a = Pango.attr_line_height_new_absolute(int(lead * size * S))
        a.start_index = 0
        a.end_index = 2 ** 31 - 1
        al.insert(a)
        lay.set_attributes(al)
    lay.set_text(txt, -1)
    return lay, color


def show(cr, lc, x, y):
    lay, color = lc
    cr.save()
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, lay)
    cr.restore()
    return lay.get_pixel_size()


def measure(lc):
    return lc[0].get_pixel_size()


def rrect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def hairline(cr, x0, x1, y, lw):
    cr.save()
    cr.set_source_rgb(*HAIR)
    cr.set_line_width(lw)
    cr.move_to(x0, y)
    cr.line_to(x1, y)
    cr.stroke()
    cr.restore()


def icon(cr, kind, cx, cy, s, lw, col=ICONG):
    """Forensic-ish Gmail toolbar glyphs, drawn centred on (cx, cy), box size s."""
    cr.save()
    cr.set_source_rgb(*col)
    cr.set_line_width(lw)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    h = s / 2
    if kind == "back":
        cr.move_to(cx + h, cy)
        cr.line_to(cx - h, cy)
        cr.move_to(cx - h * 0.35, cy - h * 0.45)
        cr.line_to(cx - h, cy)
        cr.line_to(cx - h * 0.35, cy + h * 0.45)
        cr.stroke()
    elif kind == "archive":
        cr.rectangle(cx - h, cy - h * 0.55, s, h * 0.5)
        cr.rectangle(cx - h * 0.85, cy - h * 0.05, s * 0.85, h * 0.85)
        cr.move_to(cx - h * 0.28, cy + h * 0.2)
        cr.line_to(cx + h * 0.28, cy + h * 0.2)
        cr.stroke()
    elif kind == "spam":  # ! in a circle
        cr.arc(cx, cy, h, 0, 2 * math.pi)
        cr.move_to(cx, cy - h * 0.45)
        cr.line_to(cx, cy + h * 0.15)
        cr.stroke()
        cr.arc(cx, cy + h * 0.5, lw * 0.7, 0, 2 * math.pi)
        cr.fill()
    elif kind == "trash":
        cr.move_to(cx - h * 0.75, cy - h * 0.5)
        cr.line_to(cx + h * 0.75, cy - h * 0.5)
        cr.stroke()
        cr.move_to(cx - h * 0.3, cy - h * 0.5)
        cr.line_to(cx - h * 0.3, cy - h * 0.72)
        cr.line_to(cx + h * 0.3, cy - h * 0.72)
        cr.line_to(cx + h * 0.3, cy - h * 0.5)
        cr.stroke()
        cr.move_to(cx - h * 0.55, cy - h * 0.5)
        cr.line_to(cx - h * 0.42, cy + h * 0.72)
        cr.line_to(cx + h * 0.42, cy + h * 0.72)
        cr.line_to(cx + h * 0.55, cy - h * 0.5)
        cr.stroke()
    elif kind == "mail":  # mark as unread
        cr.rectangle(cx - h, cy - h * 0.6, s, h * 1.2)
        cr.move_to(cx - h, cy - h * 0.6)
        cr.line_to(cx, cy + h * 0.1)
        cr.line_to(cx + h, cy - h * 0.6)
        cr.stroke()
    elif kind == "snooze":  # clock
        cr.arc(cx, cy, h, 0, 2 * math.pi)
        cr.move_to(cx, cy - h * 0.5)
        cr.line_to(cx, cy)
        cr.line_to(cx + h * 0.4, cy + h * 0.25)
        cr.stroke()
    elif kind == "kebab":
        for dy in (-h * 0.6, 0, h * 0.6):
            cr.arc(cx, cy + dy, lw * 0.85, 0, 2 * math.pi)
            cr.fill()
    elif kind == "star":
        for i in range(5):
            a = -math.pi / 2 + i * 2 * math.pi / 5
            px, py = cx + math.cos(a) * h, cy + math.sin(a) * h
            cr.line_to(px, py) if i else cr.move_to(px, py)
            a2 = a + math.pi / 5
            cr.line_to(cx + math.cos(a2) * h * 0.44, cy + math.sin(a2) * h * 0.44)
        cr.close_path()
        cr.stroke()
    elif kind == "reply":
        cr.move_to(cx - h * 0.2, cy - h * 0.6)
        cr.line_to(cx - h * 0.8, cy)
        cr.line_to(cx - h * 0.2, cy + h * 0.6)
        cr.stroke()
        cr.move_to(cx - h * 0.8, cy)
        cr.line_to(cx + h * 0.3, cy)
        cr.curve_to(cx + h, cy, cx + h, cy + h * 0.5, cx + h, cy + h * 0.9)
        cr.stroke()
    elif kind == "forward":
        cr.move_to(cx + h * 0.2, cy - h * 0.6)
        cr.line_to(cx + h * 0.8, cy)
        cr.line_to(cx + h * 0.2, cy + h * 0.6)
        cr.stroke()
        cr.move_to(cx + h * 0.8, cy)
        cr.line_to(cx - h * 0.3, cy)
        cr.curve_to(cx - h, cy, cx - h, cy + h * 0.5, cx - h, cy + h * 0.9)
        cr.stroke()
    elif kind == "chevL":
        cr.move_to(cx + h * 0.3, cy - h * 0.5)
        cr.line_to(cx - h * 0.3, cy)
        cr.line_to(cx + h * 0.3, cy + h * 0.5)
        cr.stroke()
    elif kind == "chevR":
        cr.move_to(cx - h * 0.3, cy - h * 0.5)
        cr.line_to(cx + h * 0.3, cy)
        cr.line_to(cx - h * 0.3, cy + h * 0.5)
        cr.stroke()
    elif kind == "chevD":
        cr.move_to(cx - h * 0.5, cy - h * 0.25)
        cr.line_to(cx, cy + h * 0.25)
        cr.line_to(cx + h * 0.5, cy - h * 0.25)
        cr.stroke()
    cr.restore()


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def build_blocks(data):
    """Normalise the body into (kind, payload) tuples with typographic copy."""
    out = []
    for blk in data["body"]:
        t = blk.get("type", "p")
        if t == "signature":
            lines = blk.get("lines")
            if not lines:
                lines = [x for x in (blk.get("name"), blk.get("detail")) if x]
            out.append(("sig", [typographic(x) for x in lines]))
        elif t in ("bullet", "li"):
            out.append(("bullet", typographic(blk.get("text", ""))))
        else:
            out.append(("p", typographic(blk.get("text", ""))))
    return out


# The body auto-sizes to fill the sheet: a short email grows to command the A2, a
# long one shrinks to fit. Only the body/signature/P.S. and their spacing scale;
# the Gmail chrome (toolbar, subject, sender row, action pills) stays fixed, as it
# is in a real client. The search aims the content bottom at FILL_TARGET; an email
# too long to fit even at BODY_SCALE_MIN is a hard overflow failure (copy is never
# squeezed, only flagged for shortening).
BODY_SCALE_MIN, BODY_SCALE_MAX = 0.60, 1.45
FILL_TARGET = 0.94
HARD_LIMIT = 0.97


def flow(cr, data, W, H, bscale, draw):
    """Lay the artefact out with the body at scale `bscale`. With draw=False no
    marks are made (measurement only) and the y of the content bottom is returned,
    so the caller can search for the scale that best fills the sheet."""
    mx = G["margin_x"] * W
    ix, iw = mx, W - 2 * mx
    lw = max(1.0, G["hairline"] * H)
    sender = data["sender"]
    subject = typographic(data["subject"])
    blocks = build_blocks(data)

    if draw:
        cr.set_source_rgb(*WHITE)
        cr.paint()

        # --- toolbar (fixed) ----------------------------------------------
        ty = G["toolbar_y"] * H
        isz = 0.020 * H
        gap = 0.040 * W
        x = mx + isz / 2
        for k in ["back", "archive", "spam", "trash", "mail", "snooze", "kebab"]:
            icon(cr, k, x, ty, isz, lw * 1.7)
            x += gap
        # right: "1 of N" then the prev/next nav chevrons, right-aligned.
        unread = data.get("account", {}).get("unread_count")
        if unread:
            # Tolerate "1,284" as a string (a natural authoring); a bare
            # format spec on a str raises ValueError mid-render.
            try:
                unread = f"{int(str(unread).replace(',', '')):,}"
            except ValueError:
                unread = str(unread)
            pos = layout(cr, f"1 of {unread}", G["small_size"] * H, color=SUB)
            pw, ph = measure(pos)
            redge = ix + iw
            icon(cr, "chevR", redge - isz * 0.5, ty, isz, lw * 1.7, col=FAINT)
            icon(cr, "chevL", redge - isz * 1.7, ty, isz, lw * 1.7, col=FAINT)
            show(cr, pos, redge - isz * 2.4 - pw, ty - ph / 2)

    # --- subject + label chip (fixed) -------------------------------------
    y = G["subject_y"] * H
    # Measure the chip FIRST: it is drawn over the subject's line, so the
    # subject must wrap short of whatever the label actually needs (a long
    # label used to stamp its opaque chip mid-word over the subject).
    label = data.get("label", "Inbox")
    chip = layout(cr, label, G["small_size"] * H, color=SUB, weight="Medium")
    cw, chh = measure(chip)
    pad = 0.014 * W
    reserve = max(0.18 * W, cw + 2 * pad + 0.02 * W)
    sub = layout(cr, subject, G["subject_size"] * H, w=iw - reserve,
                 weight="Medium", color=INK, lead=1.14)
    sh = measure(sub)[1]
    if draw:
        show(cr, sub, ix, y)
        rrect(cr, ix + iw - cw - 2 * pad, y + sh * 0.12, cw + 2 * pad, chh + pad, 0.006 * H)
        cr.set_source_rgb(*CHIP)
        cr.fill()
        show(cr, chip, ix + iw - cw - pad, y + sh * 0.12 + pad / 2)
    y += sh + 0.034 * H

    # --- sender row (fixed) -----------------------------------------------
    av = G["avatar"] * H
    if draw:
        acol = sender.get("avatar_color")
        if acol:
            acol = tuple(int(acol.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        else:
            acol = CLAY
        cr.arc(ix + av / 2, y + av / 2, av / 2, 0, 2 * math.pi)
        cr.set_source_rgb(*acol)
        cr.fill()
        initial = sender.get("avatar_initial") or sender["name"].strip()[:1].upper()
        ini = layout(cr, initial, av * 0.52, color=WHITE, weight="Medium")
        iw2, ih2 = measure(ini)
        show(cr, ini, ix + av / 2 - iw2 / 2, y + av / 2 - ih2 / 2)

        nx = ix + av + 0.022 * W
        name = layout(cr, sender["name"], G["meta_size"] * H, weight="Bold", color=INK)
        nw = measure(name)[0]
        show(cr, name, nx, y + av * 0.06)
        addr = layout(cr, f"  <{sender['email']}>", G["meta_size"] * H, color=SUB)
        show(cr, addr, nx + nw, y + av * 0.08)
        to_label = data.get("recipient", {}).get("to_label")
        if not to_label:
            # The artefact is the recipient's own inbox view, so the forensic
            # default is "to me" (what Gmail actually shows them), not their name.
            to_label = "to me"
        tol = layout(cr, to_label + "  ", G["small_size"] * H, color=SUB)
        tw = measure(tol)[0]
        show(cr, tol, nx, y + av * 0.52)
        icon(cr, "chevD", nx + tw + 0.004 * W, y + av * 0.62, 0.012 * H, lw * 1.5, col=SUB)

        ts = data.get("timestamp", "")
        if ts:
            tsl = layout(cr, ts, G["small_size"] * H, color=SUB)
            tslw = measure(tsl)[0]
            show(cr, tsl, ix + iw - tslw - 0.10 * W, y + av * 0.10)
        icon(cr, "star", ix + iw - 0.066 * W, y + av * 0.30, 0.020 * H, lw * 1.7, col=FAINT)
        icon(cr, "reply", ix + iw - 0.030 * W, y + av * 0.30, 0.020 * H, lw * 1.7, col=ICONG)
    y += av + 0.022 * H
    if draw:
        hairline(cr, ix, ix + iw, y, lw)
    y += 0.030 * H

    # --- body (auto-scaled) -----------------------------------------------
    bs = G["body_size"] * H * bscale
    pg = G["para_gap"] * H * bscale
    for kind, payload in blocks:
        if kind == "p":
            lay = layout(cr, payload, bs, w=iw, color=INK, lead=G["line_lead"])
            if draw:
                show(cr, lay, ix, y)
            y += measure(lay)[1] + pg
        elif kind == "bullet":
            lay = layout(cr, payload, bs, w=iw - bs * 1.4, color=INK, lead=G["line_lead"])
            if draw:
                cr.save()
                cr.set_source_rgb(*INK)
                cr.arc(ix + bs * 0.32, y + bs * 0.78, bs * 0.13, 0, 2 * math.pi)
                cr.fill()
                cr.restore()
                show(cr, lay, ix + bs * 1.4, y)
            y += measure(lay)[1] + 0.011 * H * bscale
        elif kind == "sig":
            # A typed sign-off is plain body text, not a designed block: same
            # ink and weight as the body, no bold on "Best regards,".
            y += 0.013 * H * bscale
            for line in payload:
                # w=iw: an unwrapped long signature line runs to the sheet edge
                # and is razor-clipped at the trim.
                lay = layout(cr, line, bs, w=iw, color=INK, lead=1.3)
                if draw:
                    show(cr, lay, ix, y)
                y += measure(lay)[1] + bs * 0.12

    # --- the ever-present P.S.: the format's one wink (auto-scaled) --------
    ps = data.get("postscript")
    if ps:
        ps = typographic(str(ps).strip())
        if not re.match(r"p\.?s\b", ps, re.IGNORECASE):
            ps = "P.S. " + ps
        y += 0.016 * H * bscale
        lay = layout(cr, ps, bs, w=iw, color=INK, lead=G["line_lead"])
        if draw:
            show(cr, lay, ix, y)
        y += measure(lay)[1] + 0.017 * H * bscale

    # --- reply / forward action pills (fixed) -----------------------------
    y += 0.026 * H
    bh = 0.030 * H
    if draw:
        bx = ix
        for lbl, k in [("Reply", "reply"), ("Reply all", "reply"), ("Forward", "forward")]:
            ll = layout(cr, lbl, G["small_size"] * H, color=SUB, weight="Medium")
            lwid = measure(ll)[0]
            bw = lwid + 0.064 * W
            rrect(cr, bx, y, bw, bh, bh / 2)
            cr.set_source_rgb(*HAIR)
            cr.set_line_width(lw)
            cr.stroke()
            icon(cr, k, bx + 0.026 * W, y + bh / 2, 0.016 * H, lw * 1.6, col=SUB)
            show(cr, ll, bx + 0.046 * W, y + bh / 2 - measure(ll)[1] / 2)
            bx += bw + 0.018 * W
    return y + bh


def render(data, W, H, check=False):
    """Draw the artefact at (W, H), auto-sizing the body to fill the sheet.
    Returns (cairo.Surface, failures[])."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)

    target = H * FILL_TARGET
    lo, hi = BODY_SCALE_MIN, BODY_SCALE_MAX
    if flow(cr, data, W, H, lo, draw=False) > target:
        scale = lo                       # even smallest body overruns the target
    elif flow(cr, data, W, H, hi, draw=False) <= target:
        scale = hi                       # even largest body underfills: cap it
    else:
        for _ in range(28):              # largest scale whose bottom <= target
            mid = (lo + hi) / 2
            if flow(cr, data, W, H, mid, draw=False) <= target:
                lo = mid
            else:
                hi = mid
        scale = lo

    bottom = flow(cr, data, W, H, scale, draw=True)
    failures = []
    if bottom > H * HARD_LIMIT:
        failures.append(("body", f"content overflows the sheet even at the minimum "
                         f"text size (reaches {int(bottom)}px of {int(H * HARD_LIMIT)}px). "
                         f"Shorten the email; the engine never squeezes copy."))
    return surf, failures


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
_SRGB = None


def srgb_bytes():
    # Header datetime + profile ID zeroed: LittleCMS stamps generation time,
    # which would make byte-identical re-renders impossible.
    global _SRGB
    if _SRGB is None:
        raw = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        raw[24:36] = bytes(12)
        raw[84:100] = bytes(16)
        _SRGB = bytes(raw)
    return _SRGB


def surface_to_pil(surf):
    W, H = surf.get_width(), surf.get_height()
    buf = surf.get_data()
    img = Image.frombuffer("RGBA", (W, H), bytes(buf), "raw", "BGRA", 0, 1)
    return img.convert("RGB")


def size_for(args):
    if args.print_size:
        w, h = args.print_size.lower().split("x")
        return int(w), int(h)
    if args.print_dpi:
        return (round(A2_MM[0] / 25.4 * args.print_dpi),
                round(A2_MM[1] / 25.4 * args.print_dpi))
    if args.render_size:
        w, h = args.render_size.lower().split("x")
        return int(w), int(h)
    return DEFAULT_SIZE


def main():
    ap = argparse.ArgumentParser(description="Sentrada 'The Email' layout engine")
    ap.add_argument("--data", required=True, help="email JSON (sender, subject, body, ...)")
    ap.add_argument("--output", default=None, help="output PNG path")
    ap.add_argument("--check", action="store_true",
                    help="validate at the chosen size and exit non-zero on any failure")
    ap.add_argument("--print-dpi", type=int, default=None,
                    help="render straight to an exact A2 at this DPI (e.g. 300)")
    ap.add_argument("--print-size", default=None, help="explicit WIDTHxHEIGHT override")
    ap.add_argument("--render-size", default=None,
                    help="WIDTHxHEIGHT for preview renders (default 1488x2105)")
    args = ap.parse_args()

    if not args.output and not args.check:
        ap.error("--output is required unless --check is given")

    # A bare --check must validate at the production print geometry, not the
    # small preview default: fit decisions at ~90 DPI can flip at 360.
    if args.check and not (args.print_dpi or args.print_size or args.render_size):
        args.print_dpi = 360

    data = load_data(args.data)
    W, H = size_for(args)
    surf, failures = render(data, W, H, check=args.check)

    if args.check:
        if failures:
            for field, msg in failures:
                print(f"  [FAIL] {field}: {msg}")
            print(f"CHECK: FAIL ({len(failures)} issue(s)) at {W}x{H}")
            sys.exit(1)
        print(f"CHECK: PASS at {W}x{H}")
        return

    img = surface_to_pil(surf)
    if failures:
        quarantine = os.path.splitext(args.output)[0] + ".FAILED.png"
        img.save(quarantine, icc_profile=srgb_bytes())
        for field, msg in failures:
            print(f"  [FAIL] {field}: {msg}")
        print(f"[fatal] deliverable withheld; wrote {quarantine} for inspection")
        sys.exit(1)

    save_kwargs = {"icc_profile": srgb_bytes()}
    if args.print_dpi:
        save_kwargs["dpi"] = (args.print_dpi, args.print_dpi)
    img.save(args.output, **save_kwargs)
    print(f"wrote {args.output} ({W}x{H}, sRGB"
          + (f", {args.print_dpi} DPI)" if args.print_dpi else ")"))


if __name__ == "__main__":
    main()
