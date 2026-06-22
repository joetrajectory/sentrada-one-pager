#!/usr/bin/env python3
"""
"The Email" — format prototype.

A cold email, printed at A2 and dropped on a senior buyer's desk. The joke is the
brand thesis made physical: nobody responds to your cold outbound, so here is the
email, impossible to ignore. Research flexes in the body; the gap is real and
native — Gmail's "[Message clipped]" — cutting the email off at the hook, so the
only way to read the rest is to reply. Pure typographic UI (the engine's lane).
One-off mock for review.
"""
import ctypes, math, os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
for d in (os.path.normpath(os.path.join(SCRIPT_DIR, "..", "fonts")), os.path.join(SCRIPT_DIR, "fonts")):
    if os.path.isdir(d):
        fc = ctypes.CDLL("libfontconfig.so.1"); fc.FcConfigGetCurrent.restype = ctypes.c_void_p
        fc.FcConfigAppFontAddDir.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        fc.FcConfigAppFontAddDir(fc.FcConfigGetCurrent(), d.encode())
import gi
gi.require_version("Pango", "1.0"); gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo
import cairo
S = Pango.SCALE

W, H = 1488, 2105                       # A2 portrait
APP   = (0xEF/255, 0xEE/255, 0xEA/255)  # warm app background
CARD  = (0xFF/255, 0xFF/255, 0xFE/255)
INK   = (0x20/255, 0x21/255, 0x24/255)
SUB   = (0x66/255, 0x6A/255, 0x71/255)  # meta grey
FAINT = (0x9A/255, 0x9E/255, 0xA4/255)
HAIR  = (0xE4/255, 0xE2/255, 0xDC/255)
LINK  = (0x2F/255, 0x69/255, 0xB0/255)  # email-link blue
CLAY  = (0xC0/255, 0x59/255, 0x33/255)  # Sentrada accent (avatar)
CLIPBG= (0xF3/255, 0xF2/255, 0xEE/255)

SANS = "Inter"

SUBJECT = "We printed this so you’d actually read it"
SENDER_NAME = "Sentrada"
SENDER_ADDR = "<hello@sentrada.io>"
TO_LINE = "to James  ⌄"
TIME = "09:14"

BODY = [
    ("p", "Hi James,"),
    ("p", "This is a cold email. You can tell, because it is sitting on your desk at A2 "
          "rather than in the folder where the other ones quietly go to die."),
    ("p", "Making it impossible to ignore is the easy part. Earning the next sixty seconds "
          "is the part we take seriously, so here is what we actually wanted to say."),
    ("p", "We spent some time on Qflow. A few things stood out:"),
    ("b", "Your first tier-one pour was logged before you had finished arguing about the logo."),
    ("b", "One of your audit trails settled a site dispute in four minutes. The industry "
          "tends to measure that in weeks."),
    ("b", "You have logged more deliveries before 9 a.m. than most platforms manage in a day."),
    ("p", "Which brings us to what we actually noticed. There is a gap between what arrives on "
          "site and what gets recorded, and at your volumes it is large enough to be worth a "
          "short conversation."),
    ("p", "I have put the Qflow numbers into a one-page view. Fifteen minutes on Thursday and I "
          "will walk you through it. If it is not useful, keep the A2 and I will leave you alone."),
    ("sig", "Joe\nSentrada"),
]
CREDIT = "sentrada.io   ·   physical outreach, one company at a time"


def lay(cr, txt, font, size, w=None, color=INK, lead=None, weight=None, justify=False):
    l = PangoCairo.create_layout(cr)
    spec = font + (f" {weight}" if weight else "")
    fd = Pango.FontDescription.from_string(spec); fd.set_absolute_size(size * S)
    l.set_font_description(fd)
    if w:
        l.set_width(int(w * S)); l.set_wrap(Pango.WrapMode.WORD)
    if justify:
        l.set_justify(True)
    if lead:
        al = Pango.AttrList()
        a = Pango.attr_line_height_new_absolute(int(lead * size * S))
        a.start_index = 0; a.end_index = len(txt.encode()); al.insert(a)
        l.set_attributes(al)
    l.set_text(txt, -1)
    return l, color


def draw(cr, lc, x, y):
    l, c = lc
    cr.save(); cr.set_source_rgb(*c); cr.move_to(x, y); PangoCairo.show_layout(cr, l); cr.restore()
    return l.get_pixel_size()


def rr(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x+w-r, y+r, r, -math.pi/2, 0); cr.arc(x+w-r, y+h-r, r, 0, math.pi/2)
    cr.arc(x+r, y+h-r, r, math.pi/2, math.pi); cr.arc(x+r, y+r, r, math.pi, 1.5*math.pi); cr.close_path()


def hair(cr, x0, x1, y):
    cr.save(); cr.set_source_rgb(*HAIR); cr.set_line_width(1.4)
    cr.move_to(x0, y); cr.line_to(x1, y); cr.stroke(); cr.restore()


def icon(cr, kind, x, y, s, col=FAINT):
    cr.save(); cr.set_source_rgb(*col); cr.set_line_width(2.4)
    cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind == "back":
        cr.move_to(x+s, y); cr.line_to(x, y); cr.move_to(x+s*0.4, y-s*0.4)
        cr.line_to(x, y); cr.line_to(x+s*0.4, y+s*0.4); cr.stroke()
    elif kind == "archive":
        cr.rectangle(x, y-s*0.35, s, s*0.7); cr.stroke()
        cr.move_to(x+s*0.35, y); cr.line_to(x+s*0.65, y); cr.stroke()
    elif kind == "bin":
        cr.rectangle(x+s*0.15, y-s*0.25, s*0.7, s*0.6); cr.stroke()
        cr.move_to(x, y-s*0.3); cr.line_to(x+s, y-s*0.3); cr.stroke()
    elif kind == "mail":
        cr.rectangle(x, y-s*0.3, s, s*0.6); cr.move_to(x, y-s*0.3)
        cr.line_to(x+s*0.5, y+s*0.05); cr.line_to(x+s, y-s*0.3); cr.stroke()
    elif kind == "star":
        for i in range(5):
            a = -math.pi/2 + i*2*math.pi/5
            px, py = x+s*0.5+math.cos(a)*s*0.5, y+math.sin(a)*s*0.5
            cr.line_to(px, py) if i else cr.move_to(px, py)
            a2 = a + math.pi/5
            cr.line_to(x+s*0.5+math.cos(a2)*s*0.22, y+math.sin(a2)*s*0.22)
        cr.close_path(); cr.stroke()
    elif kind == "reply":
        cr.move_to(x+s*0.35, y-s*0.35); cr.line_to(x, y); cr.line_to(x+s*0.35, y+s*0.35); cr.stroke()
        cr.move_to(x, y); cr.line_to(x+s*0.7, y); cr.curve_to(x+s, y, x+s, y+s*0.4, x+s, y+s*0.5); cr.stroke()
    cr.restore()


def measure_card(cr, iw, pad):
    """Dry-run the card content to get its height, so the card hugs it."""
    y = 70
    sub = lay(cr, SUBJECT, SANS, 54, w=iw-180, weight="Semibold", lead=1.12)
    y += sub[0].get_pixel_size()[1] + 54
    y += 92 + 30                       # sender row
    y += 48                            # hairline + gap
    for kind, txt in BODY:
        if kind == "sig":
            y += 24
            for line in txt.split("\n"):
                y += lay(cr, line, SANS, 30, lead=1.3)[0].get_pixel_size()[1] + 4
            continue
        w = iw if kind == "p" else iw-44
        d = lay(cr, txt, SANS, 30, w=w, lead=1.46)[0].get_pixel_size()[1]
        y += d + (26 if kind == "p" else 16)
    y += 40                            # gap before buttons
    y += 70                            # reply/forward buttons
    return y + 70                      # bottom padding


def render():
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H); cr = cairo.Context(surf)
    cr.set_source_rgb(*APP); cr.paint()

    # top client toolbar
    ty = 78
    for i, k in enumerate(["back", "archive", "bin", "mail"]):
        icon(cr, k, 96 + i*70, ty, 30)
    draw(cr, lay(cr, "1 of 1,284", SANS, 24, color=FAINT), W-300, ty-16)
    icon(cr, "star", W-150, ty, 30)

    # email card, sized to hug its content (like a real screenshot)
    pad = 78
    cx, cw = 96, W-192
    iw = cw - 2*pad
    ch = measure_card(cr, iw, pad)
    cy = 132
    cr.save(); cr.set_source_rgba(0, 0, 0, 0.10)
    rr(cr, cx+4, cy+9, cw, ch, 26); cr.fill(); cr.restore()
    rr(cr, cx, cy, cw, ch, 26); cr.set_source_rgb(*CARD); cr.fill()

    ix = cx+pad
    y = cy + 70

    # subject + label chip
    sw, sh = draw(cr, lay(cr, SUBJECT, SANS, 54, w=iw-180, color=INK, weight="Semibold", lead=1.12), ix, y)
    rr(cr, ix+iw-150, y+8, 150, 44, 10); cr.set_source_rgb(*(0xEC/255,0xEF/255,0xF4/255)); cr.fill()
    draw(cr, lay(cr, "Inbox", SANS, 23, color=SUB, weight="Medium"), ix+iw-150+34, y+13)
    y += sh + 54

    # sender row: avatar + name/address + meta on the right
    av = 92
    cr.arc(ix+av/2, y+av/2, av/2, 0, 2*math.pi); cr.set_source_rgb(*CLAY); cr.fill()
    draw(cr, lay(cr, "S", SANS, 46, color=(1,1,1), weight="Semibold"), ix+av/2-15, y+av/2-34)
    nx = ix+av+28
    draw(cr, lay(cr, SENDER_NAME, SANS, 31, color=INK, weight="Semibold"), nx, y+4)
    nmw = lay(cr, SENDER_NAME, SANS, 31, weight="Semibold")[0].get_pixel_size()[0]
    draw(cr, lay(cr, "  " + SENDER_ADDR, SANS, 29, color=SUB), nx+nmw, y+6)
    draw(cr, lay(cr, TO_LINE, SANS, 27, color=SUB), nx, y+50)
    # right meta
    tlay = lay(cr, TIME, SANS, 26, color=SUB)
    tw = tlay[0].get_pixel_size()[0]
    draw(cr, tlay, ix+iw-tw-110, y+10)
    icon(cr, "star", ix+iw-78, y+22, 28)
    icon(cr, "reply", ix+iw-32, y+22, 26)
    y += av + 30
    hair(cr, ix, ix+iw, y); y += 48

    # body
    for kind, txt in BODY:
        if kind == "p":
            d = draw(cr, lay(cr, txt, SANS, 30, w=iw, color=INK, lead=1.46), ix, y)
            y += d[1] + 26
        elif kind == "sig":
            y += 24
            for i, line in enumerate(txt.split("\n")):
                col = INK if i == 0 else SUB
                wt = "Semibold" if i == 0 else None
                d = draw(cr, lay(cr, line, SANS, 30, color=col, weight=wt, lead=1.3), ix, y)
                y += d[1] + 4
        else:  # bullet
            cr.save(); cr.set_source_rgb(*INK); cr.arc(ix+12, y+22, 4.5, 0, 2*math.pi); cr.fill(); cr.restore()
            d = draw(cr, lay(cr, txt, SANS, 30, w=iw-44, color=INK, lead=1.46), ix+44, y)
            y += d[1] + 16

    y += 40

    # reply / forward buttons
    for i, (lbl, k) in enumerate([("Reply", "reply"), ("Forward", "mail")]):
        bx = ix + i*210
        rr(cr, bx, y, 188, 70, 35); cr.set_source_rgb(*HAIR); cr.set_line_width(1.6); cr.stroke()
        icon(cr, k, bx+34, y+35, 26, col=SUB)
        draw(cr, lay(cr, lbl, SANS, 28, color=SUB, weight="Medium"), bx+72, y+18)

    # Sentrada print credit, centred on the app background beneath the card
    cl = lay(cr, CREDIT, SANS, 22, color=FAINT, weight="Medium")
    clw = cl[0].get_pixel_size()[0]
    draw(cr, cl, (W-clw)/2, H-78)

    surf.write_to_png(os.path.join(SCRIPT_DIR, "qflow_email.png"))
    print("wrote qflow_email.png")


if __name__ == "__main__":
    render()
