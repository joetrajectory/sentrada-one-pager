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


def ellip_layout(cr, txt, size, w, color=INK, weight=None):
    """A single-line layout that ellipsises at the end if it exceeds width w, so a
    long sender name or email address never collides with the right-hand meta."""
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(SANS + (f" {weight}" if weight else ""))
    fd.set_absolute_size(size * S)
    lay.set_font_description(fd)
    lay.set_width(int(max(0, w) * S))
    lay.set_ellipsize(Pango.EllipsizeMode.END)
    lay.set_text(txt, -1)
    return lay, color


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


# The real Material Symbols (outlined, 24x24 viewBox) vector paths Gmail itself
# uses for these controls, so the chrome is pixel-accurate rather than an
# approximation. Filled with the even-odd rule (the outlined glyphs are filled
# regions with opposite-wound holes). The "report spam" dot is appended as a
# bezier circle (it ships as an <svg> <circle>, not in the path data).
ICON_PATHS = {
    "back": "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z",
    "archive": "M20.54 5.23l-1.39-1.68C18.88 3.21 18.47 3 18 3H6c-.47 0-.88.21-1.16.55L3.46 5.23C3.17 5.57 3 6.02 3 6.5V19c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6.5c0-.48-.17-.93-.46-1.27zM6.24 5h11.52l.81.97H5.44l.8-.97zM5 19V8h14v11H5zm8.45-9h-2.9v3H8l4 4 4-4h-2.55z",
    "spam": "M15.73 3H8.27L3 8.27v7.46L8.27 21h7.46L21 15.73V8.27L15.73 3zM19 14.9L14.9 19H9.1L5 14.9V9.1L9.1 5h5.8L19 9.1v5.8z M11 7h2v7h-2z M13 16C13 16.5523 12.5523 17 12 17C11.4477 17 11 16.5523 11 16C11 15.4477 11.4477 15 12 15C12.5523 15 13 15.4477 13 16Z",
    "trash": "M16 9v10H8V9h8m-1.5-6h-5l-1 1H5v2h14V4h-3.5l-1-1zM18 7H6v12c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7z",
    "mail": "M22 6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6zm-2 0l-8 5-8-5h16zm0 12H4V8l8 5 8-5v10z",
    "snooze": "M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z",
    "kebab": "M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z",
    "chevL": "M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12l4.58-4.59z",
    "chevR": "M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6-6-6z",
    "chevD": "M7 10l5 5 5-5H7z",
    "star": "M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z",
    "reply": "M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z",
    "replyall": "M7 8V5l-7 7 7 7v-3l-4-4 4-4zm6 1V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z",
    "forward": "M14 8.83L17.17 12 14 15.17V14H6v-4h8V8.83M12 4v4H4v8h8v4l8-8-8-8z",
}

_PATH_TOKEN = re.compile(r"([MmLlHhVvCcSsZz])|(-?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)")


def _svg_path(cr, d):
    """Trace an SVG path string (the subset Material icons use: M L H V C S Z,
    absolute and relative) into the current cairo path, in 24x24 user space."""
    toks = [m.group(1) if m.group(1) else float(m.group(2))
            for m in _PATH_TOKEN.finditer(d)]
    i, n = 0, len(toks)
    cx = cy = sx = sy = pcx = pcy = 0.0
    cmd, prev_cubic = None, False
    while i < n:
        if isinstance(toks[i], str):
            cmd = toks[i]
            i += 1
        c = cmd
        if c in ("M", "m"):
            x, y = toks[i], toks[i + 1]; i += 2
            if c == "m": x += cx; y += cy
            cr.move_to(x, y); cx, cy = x, y; sx, sy = x, y
            cmd = "l" if c == "m" else "L"; prev_cubic = False
        elif c in ("L", "l"):
            x, y = toks[i], toks[i + 1]; i += 2
            if c == "l": x += cx; y += cy
            cr.line_to(x, y); cx, cy = x, y; prev_cubic = False
        elif c in ("H", "h"):
            x = toks[i]; i += 1
            if c == "h": x += cx
            cr.line_to(x, cy); cx = x; prev_cubic = False
        elif c in ("V", "v"):
            y = toks[i]; i += 1
            if c == "v": y += cy
            cr.line_to(cx, y); cy = y; prev_cubic = False
        elif c in ("C", "c"):
            x1, y1, x2, y2, x, y = toks[i:i + 6]; i += 6
            if c == "c":
                x1 += cx; y1 += cy; x2 += cx; y2 += cy; x += cx; y += cy
            cr.curve_to(x1, y1, x2, y2, x, y)
            pcx, pcy = x2, y2; cx, cy = x, y; prev_cubic = True
        elif c in ("S", "s"):
            x2, y2, x, y = toks[i:i + 4]; i += 4
            if c == "s":
                x2 += cx; y2 += cy; x += cx; y += cy
            x1, y1 = (2 * cx - pcx, 2 * cy - pcy) if prev_cubic else (cx, cy)
            cr.curve_to(x1, y1, x2, y2, x, y)
            pcx, pcy = x2, y2; cx, cy = x, y; prev_cubic = True
        elif c in ("Z", "z"):
            cr.close_path(); cx, cy = sx, sy; prev_cubic = False
        else:
            break


def icon(cr, kind, cx, cy, s, col=ICONG):
    """Draw a Material icon, filled, centred on (cx, cy) with box size s."""
    d = ICON_PATHS.get(kind)
    if not d:
        return
    cr.save()
    cr.set_source_rgb(*col)
    cr.translate(cx - s / 2.0, cy - s / 2.0)
    cr.scale(s / 24.0, s / 24.0)
    cr.new_path()
    _svg_path(cr, d)
    cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
    cr.fill()
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
            icon(cr, k, x, ty, isz)
            x += gap
        # right: "1 of N" then the prev/next nav chevrons, right-aligned.
        unread = data.get("account", {}).get("unread_count")
        if unread:
            pos = layout(cr, f"1 of {unread:,}", G["small_size"] * H, color=SUB)
            pw, ph = measure(pos)
            redge = ix + iw
            icon(cr, "chevR", redge - isz * 0.5, ty, isz, col=FAINT)
            icon(cr, "chevL", redge - isz * 1.7, ty, isz, col=FAINT)
            show(cr, pos, redge - isz * 2.4 - pw, ty - ph / 2)

    # --- subject + label chip (fixed) -------------------------------------
    # The subject is fixed-size chrome, but a long one is shrunk just enough to
    # stay within two lines (down to a floor) rather than pushing the sender row
    # down the page.
    y = G["subject_y"] * H
    subw = iw - 0.18 * W
    ssize = G["subject_size"] * H
    sfloor = ssize * 0.70
    sub = layout(cr, subject, ssize, w=subw, weight="Medium", color=INK, lead=1.14)
    while sub[0].get_line_count() > 2 and ssize > sfloor:
        ssize *= 0.94
        sub = layout(cr, subject, ssize, w=subw, weight="Medium", color=INK, lead=1.14)
    sh = measure(sub)[1]
    if draw:
        show(cr, sub, ix, y)
        label = data.get("label", "Inbox")
        chip = layout(cr, label, G["small_size"] * H, color=SUB, weight="Medium")
        cw, chh = measure(chip)
        pad = 0.014 * W
        rrect(cr, ix + iw - cw - 2 * pad, y + sh * 0.12, cw + 2 * pad, chh + pad, 0.006 * H)
        cr.set_source_rgb(*CHIP)
        cr.fill()
        show(cr, chip, ix + iw - cw - pad, y + sh * 0.12 + pad / 2)
    y += sh + 0.034 * H

    # --- sender row (fixed) -----------------------------------------------
    av = G["avatar"] * H
    if draw:
        # avatar: a supplied logo/photo clipped to the disc, else a coloured disc
        # with the sender's initial
        img_path = sender.get("avatar_image")
        avatar_drawn = False
        if img_path and os.path.exists(img_path):
            try:
                side = max(1, int(round(av)))
                pim = Image.open(img_path).convert("RGB").resize((side, side))
                pim.putalpha(255)                       # opaque: ARGB32 premult == straight
                buf = bytearray(pim.tobytes("raw", "BGRA"))
                isurf = cairo.ImageSurface.create_for_data(
                    buf, cairo.FORMAT_ARGB32, side, side, side * 4)
                cr.save()
                cr.arc(ix + av / 2, y + av / 2, av / 2, 0, 2 * math.pi)
                cr.clip()
                cr.set_source_surface(isurf, ix, y)
                cr.paint()
                cr.restore()
                avatar_drawn = True
            except Exception:
                avatar_drawn = False
        if not avatar_drawn:
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

        # right-hand meta first, so name/address know where they must stop
        ts = data.get("timestamp", "")
        if ts:
            tsl = layout(cr, ts, G["small_size"] * H, color=SUB)
            tslw = measure(tsl)[0]
            show(cr, tsl, ix + iw - tslw - 0.10 * W, y + av * 0.10)
            mright = ix + iw - tslw - 0.10 * W
        else:
            mright = ix + iw - 0.090 * W
        mright -= 0.015 * W                              # gap before the right meta
        icon(cr, "star", ix + iw - 0.066 * W, y + av * 0.30, 0.022 * H, col=FAINT)
        icon(cr, "reply", ix + iw - 0.030 * W, y + av * 0.30, 0.022 * H, col=ICONG)

        # name (bold) then <address> (grey), each ellipsised so a long name or
        # email never runs into the meta on the right
        nx = ix + av + 0.022 * W
        msize = G["meta_size"] * H
        avail = max(0.0, mright - nx)
        name = ellip_layout(cr, sender["name"], msize, avail * 0.62, color=INK, weight="Bold")
        nw = measure(name)[0]
        show(cr, name, nx, y + av * 0.06)
        addr_avail = mright - (nx + nw)
        if addr_avail > 0.05 * W:
            addr = ellip_layout(cr, f"  <{sender['email']}>", msize, addr_avail, color=SUB)
            show(cr, addr, nx + nw, y + av * 0.08)

        to_label = data.get("recipient", {}).get("to_label")
        if not to_label:
            # The artefact is the recipient's own inbox view, so the forensic
            # default is "to me" (what Gmail actually shows them), not their name.
            to_label = "to me"
        tol = layout(cr, to_label + "  ", G["small_size"] * H, color=SUB)
        tw = measure(tol)[0]
        show(cr, tol, nx, y + av * 0.52)
        icon(cr, "chevD", nx + tw + 0.006 * W, y + av * 0.60, 0.020 * H, col=SUB)
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
                lay = layout(cr, line, bs, color=INK, lead=1.3)
                if draw:
                    show(cr, lay, ix, y)
                y += measure(lay)[1] + bs * 0.12

    # --- the ever-present P.S.: the format's one wink (auto-scaled) --------
    ps = data.get("postscript")
    if ps:
        ps = typographic(str(ps).strip())
        if not ps.lower().startswith("p.s"):
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
        for lbl, k in [("Reply", "reply"), ("Reply all", "replyall"), ("Forward", "forward")]:
            ll = layout(cr, lbl, G["small_size"] * H, color=SUB, weight="Medium")
            lwid = measure(ll)[0]
            bw = lwid + 0.064 * W
            rrect(cr, bx, y, bw, bh, bh / 2)
            cr.set_source_rgb(*HAIR)
            cr.set_line_width(lw)
            cr.stroke()
            icon(cr, k, bx + 0.030 * W, y + bh / 2, 0.018 * H, col=SUB)
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
    global _SRGB
    if _SRGB is None:
        _SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
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

    img.save(args.output, icc_profile=srgb_bytes())
    print(f"wrote {args.output} ({W}x{H}, sRGB)")


if __name__ == "__main__":
    main()
