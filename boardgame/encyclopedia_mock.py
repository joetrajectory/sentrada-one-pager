#!/usr/bin/env python3
"""
Encyclopaedia entry — format prototype (the 'future/ambition' pillar).

A neutral, third-party RECORD of the recipient's company written from the future,
so the aspiration reads as settled fact rather than a vendor's pitch. Rendered as
a premium A2 reference plate: cream stock, classic typography, a fact-box with a
future-dated hook, a projection figure, mock citations. Pure type + light vector
(no illustration) -- squarely the engine's lane. One-off mock for review.
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

PAPER  = (0xF2/255, 0xEA/255, 0xD8/255)
PAPER2 = (0xEA/255, 0xDF/255, 0xC8/255)   # fact-box tint
INK    = (0x2B/255, 0x23/255, 0x18/255)
SOFT   = (0x6E/255, 0x60/255, 0x49/255)   # secondary / captions
CLAY   = (0xB0/255, 0x53/255, 0x33/255)   # one restrained accent (Lisbon Clay)

W, H = 1600, 2263                          # A2 portrait proportion
M = 140

HEAD_FONT = "Playfair Display Bold"
SEC_FONT  = "Playfair Display Bold"
BODY_FONT = "Lora"
IT_FONT   = "Lora Italic"
SANS      = "Inter"

# ---- content (Qflow worked example: real-ish past blended into projected future)
RUNNING = "THE ENCYCLOPÆDIA OF THE BUILT ENVIRONMENT"
EDITION = "2035 EDITION · VOL. IV"
HEADWORD = "Qflow"
PRON = "(ˈkjuː·floʊ)  n."
DEFN = ("the British technology company whose material and environmental data "
        "platform became, between 2024 and 2031, the default record through which "
        "global construction told the truth about itself.")

LEFT = [
    ("Origins",
     "Qflow was founded in 2018 in London, the work of engineers who had tired of "
     "an industry that built in the twenty-first century yet kept its records in the "
     "nineteenth. On the ordinary site the materials arriving at the gate, the waste "
     "leaving it, and the carbon embodied in both were logged on paper, in "
     "photographs, or not at all. The founding insight was deceptively plain: capture "
     "that data at the moment of movement, verify it, and the chaos of construction "
     "becomes, for the first time, a record that can be trusted."),
    (None,
     "Early pilots on tier-one projects established the method. A delivery photographed "
     "and logged at the gate could be reconciled against the order; a load of waste "
     "tracked to its destination; a concrete pour evidenced in real time rather than "
     "reconstructed, months later, in a dispute. What had been the province of "
     "clipboards and good intentions became, in the company's hands, an audit trail."),
    ("Ascent",
     "Between 2024 and 2027 Qflow moved from promising supplier to industry fixture. "
     "Adoption spread first across national frameworks and then, by force of habit, "
     "everywhere their contractors worked. Each new site arrived with the same small "
     "revelation: that what had felt like an unknowable tide of lorries, skips and "
     "pours could simply be counted, and once counted, improved."),
    ("Method",
     "The instrument was modest — a record made at the point of movement and checked "
     "against the order, the ticket and the law. From it came consequences out of all "
     "proportion to the effort: disputes settled before they began, waste diverted "
     "from landfill, carbon counted as routinely as cost. Competitors arrived in time, "
     "but the verb had already been coined, and it was not theirs."),
]
RIGHT = [
    (None,
     "Rework on adopting sites fell sharply; environmental reporting that had been an "
     "annual scramble became continuous and audit-grade. Regulators, and then "
     "insurers, came to reference the Qflow record as a matter of course."),
    ("Legacy",
     "By 2031 the platform had become the layer through which construction accounted "
     "for itself. Public projects required it; embodied-carbon reporting, once exotic, "
     "was routine. The company's 2030 listing confirmed commercially what the sites "
     "already knew. Among crews a new verb had quietly entered use: to qflow a "
     "delivery was to log it, and have it believed. The firm that set out merely to "
     "tidy the paperwork of building had, almost in passing, given the industry a "
     "memory."),
    ("Reception",
     "Historians of the period treat Qflow less as a software company than as the "
     "moment construction accepted measurement. Its records, dry as they read, are "
     "now among the most cited primary sources on how the built environment learned, "
     "at last, to account for what it consumed."),
]

FACTS = [
    ("Founded", "2018, London"),
    ("Sector", "Construction technology"),
    ("Field", "Quality & environmental data"),
    ("Status", "Private (listing 2030, proj.)"),
]
MILES = [
    ("2018", "Founded", False),
    ("2020", "First tier-one deployment", False),
    ("2024", "Series B", False),
    ("2027", "Adopted on national frameworks", False),
    ("2030", "Public listing", True),
    ("2031", "Cited in building regulations", True),
]
SEEALSO = "Building Information Modelling · Embodied Carbon · The Paperless Site · Construction's Data Decade"
CITES = [
    "1. “The Site That Could Not Lie”, Journal of the Built Environment, 2032.",
    "2. Royal Inst. of Building, Annual Review, 2031, pp. 44–47.",
    "3. Harris & Cohen, On Evidence in Construction (rev. ed., 2033).",
]


def layout(cr, txt, font, size, *, w=None, justify=False, align="left",
           lead=None, spacing=0.0, color=INK):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(font); fd.set_absolute_size(size * S)
    lay.set_font_description(fd)
    if w:
        lay.set_width(int(w * S)); lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_alignment({"left": Pango.Alignment.LEFT, "center": Pango.Alignment.CENTER,
                       "right": Pango.Alignment.RIGHT}[align])
    if justify:
        lay.set_justify(True); lay.set_justify_last_line(False)
    al = Pango.AttrList()
    if lead:
        al.insert(_w(Pango.attr_line_height_new_absolute(int(lead * size * S)), txt))
    if spacing:
        al.insert(_w(Pango.attr_letter_spacing_new(int(spacing * size * S)), txt))
    if justify:
        al.insert(_w(Pango.attr_language_new(Pango.Language.from_string("en_GB")), txt))
        al.insert(_w(Pango.attr_insert_hyphens_new(True), txt))
    lay.set_attributes(al)
    lay.set_text(txt, -1)
    return lay, color


def _w(attr, txt):
    attr.start_index = 0; attr.end_index = len(txt.encode()); return attr


def draw(cr, lay_color, x, y):
    lay, color = lay_color
    cr.save(); cr.set_source_rgb(*color); cr.move_to(x, y)
    PangoCairo.show_layout(cr, lay); cr.restore()
    return lay.get_pixel_size()


def rule(cr, x0, x1, y, wt, color=INK):
    cr.save(); cr.set_source_rgb(*color); cr.set_line_width(wt)
    cr.move_to(x0, y); cr.line_to(x1, y); cr.stroke(); cr.restore()


def sc(s):  # spaced small caps text
    return s.upper()


def render():
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    cr.set_source_rgb(*PAPER); cr.paint()
    # faint page vignette for warmth
    g = cairo.RadialGradient(W/2, H*0.42, H*0.2, W/2, H*0.5, H*0.75)
    g.add_color_stop_rgba(0, 0, 0, 0, 0); g.add_color_stop_rgba(1, 0x40/255, 0x33/255, 0x1f/255, 0.10)
    cr.set_source(g); cr.paint()

    # outer keyline border (double rule, classic plate)
    cr.set_source_rgb(*INK)
    cr.set_line_width(3); cr.rectangle(M*0.55, M*0.55, W - M*1.1, H - M*1.1); cr.stroke()
    cr.set_line_width(1); cr.rectangle(M*0.55+9, M*0.55+9, W - M*1.1-18, H - M*1.1-18); cr.stroke()

    # running head
    y = M*0.95
    lay = layout(cr, sc(RUNNING), SANS, 21, spacing=0.30, color=INK, align="center", w=W-2*M)
    draw(cr, lay, M, y)
    y += 34
    rule(cr, M, W-M, y, 1.2)
    lay = layout(cr, sc(EDITION), SANS, 14.5, spacing=0.30, color=SOFT, align="center", w=W-2*M)
    draw(cr, lay, M, y+9)
    rule(cr, M, W-M, y+38, 1.2)

    # headword block
    hy = y + 92
    hw = layout(cr, HEADWORD, HEAD_FONT, 132, color=INK)
    wlay, _ = hw; ww, wh = wlay.get_pixel_size()
    draw(cr, hw, M, hy)
    # pronunciation baseline-aligned to the right of the headword
    pr = layout(cr, PRON, IT_FONT, 34, color=SOFT)
    draw(cr, pr, M + ww + 26, hy + wh*0.46)
    # definition line (the thesis), italic
    dy = hy + wh + 8
    dlay = layout(cr, DEFN, IT_FONT, 33, w=W-2*M, lead=1.28, color=INK)
    dw, dh = draw(cr, dlay, M, dy)
    by0 = dy + dh + 26
    rule(cr, M, W-M, by0, 3)
    rule(cr, M, W-M, by0+7, 1)
    by0 += 34

    # ---- columns ----
    gutter = 54
    colw = (W - 2*M - gutter) / 2
    lx, rx = M, M + colw + gutter
    bottom = H - M*1.15 - 150

    # RIGHT COLUMN: fact-box, then figure, then prose
    fb_h = draw_factbox(cr, rx, by0, colw)
    fig_y = by0 + fb_h + 40
    fig_h = draw_figure(cr, rx, fig_y, colw)
    ry = fig_y + fig_h + 44
    flow_column(cr, RIGHT, rx, ry, colw, bottom, drop=False)

    # LEFT COLUMN: prose with a drop cap
    flow_column(cr, LEFT, lx, by0, colw, bottom, drop=True)

    # footer: See also + citations + credit
    fy = bottom + 30
    rule(cr, M, W-M, fy, 1)
    fy += 16
    draw(cr, layout(cr, "SEE ALSO", SANS+" Bold", 14, spacing=0.22, color=CLAY), M, fy)
    draw(cr, layout(cr, SEEALSO, IT_FONT, 19, w=W-2*M, color=INK), M, fy+22)
    fy += 64
    for c in CITES:
        d = draw(cr, layout(cr, c, BODY_FONT, 14.5, w=(W-2*M), color=SOFT), M, fy); fy += d[1] + 4
    # discreet maker's mark
    cm = layout(cr, "sentrada", SANS, 17, spacing=0.18, color=SOFT)
    cw, _ = cm[0].get_pixel_size()
    draw(cr, cm, W - M - cw, H - M*0.95)

    surf.write_to_png(os.path.join(SCRIPT_DIR, "qflow_encyclopaedia.png"))
    print("wrote qflow_encyclopaedia.png")


def flow_column(cr, blocks, x, y, w, bottom, drop):
    first = True
    for head, body in blocks:
        if head:
            hl = layout(cr, sc(head), SEC_FONT, 19, spacing=0.18, color=CLAY)
            draw(cr, hl, x, y); y += 30
        if first and drop:
            y = draw_with_dropcap(cr, body, x, y, w)
            first = False
        else:
            d = draw(cr, layout(cr, body, BODY_FONT, 20.5, w=w, justify=True, lead=1.34, color=INK), x, y)
            y += d[1] + 16
    return y


def draw_with_dropcap(cr, text, x, y, w):
    cap, rest = text[0], text[1:].lstrip()
    cl = layout(cr, cap, HEAD_FONT, 96, color=CLAY)
    cap_w, cap_h = cl[0].get_pixel_size()
    cap_w += 14
    draw(cr, cl, x, y - 12)
    # first lines beside the cap (reduced width), then the remainder full width
    body_size, lead = 20.5, 1.34
    head_lines = 3
    probe = layout(cr, rest, BODY_FONT, body_size, w=w - cap_w, justify=True, lead=lead, color=INK)[0]
    it = probe.get_iter(); idx = 0
    for _ in range(head_lines):
        if not it.next_line():
            break
    idx = it.get_index() if it else len(rest.encode())
    b = rest.encode(); head_txt = b[:idx].decode("utf-8", "ignore"); tail_txt = b[idx:].decode("utf-8", "ignore")
    dh = draw(cr, layout(cr, head_txt, BODY_FONT, body_size, w=w - cap_w, justify=True, lead=lead, color=INK), x + cap_w, y)
    y2 = y + max(cap_h - 12, dh[1]) + 4
    if tail_txt.strip():
        d2 = draw(cr, layout(cr, tail_txt, BODY_FONT, body_size, w=w, justify=True, lead=lead, color=INK), x, y2)
        y2 += d2[1]
    return y2 + 16


def draw_factbox(cr, x, y, w):
    pad = 22
    # estimate height
    rows = len(FACTS) + len(MILES)
    h = 56 + 30 + len(FACTS)*30 + 34 + len(MILES)*30 + pad
    cr.save(); cr.set_source_rgb(*PAPER2); cr.rectangle(x, y, w, h); cr.fill(); cr.restore()
    cr.set_source_rgb(*INK); cr.set_line_width(1.4); cr.rectangle(x, y, w, h); cr.stroke()
    cr.set_line_width(5); cr.move_to(x, y); cr.line_to(x+w, y); cr.stroke()  # heavy top rule
    iy = y + pad
    draw(cr, layout(cr, sc(HEADWORD), SEC_FONT, 26, spacing=0.10, color=INK, align="center", w=w-2*pad), x+pad, iy)
    iy += 44
    draw(cr, layout(cr, sc("At a glance"), SANS, 12.5, spacing=0.24, color=SOFT, align="center", w=w-2*pad), x+pad, iy)
    iy += 30
    for k, v in FACTS:
        draw(cr, layout(cr, k, SANS+" Bold", 14.5, color=INK), x+pad, iy)
        kl = layout(cr, v, BODY_FONT, 16.5, w=w-2*pad, align="right", color=INK)
        draw(cr, kl, x+pad, iy-1)
        iy += 30
    iy += 6
    draw(cr, layout(cr, sc("Milestones"), SANS, 12.5, spacing=0.24, color=SOFT, w=w-2*pad), x+pad, iy)
    iy += 26
    for yr, ev, proj in MILES:
        draw(cr, layout(cr, yr, SANS+" Bold", 14.5, color=(CLAY if proj else INK)), x+pad, iy)
        label = ev + ("  (proj.)" if proj else "")
        draw(cr, layout(cr, label, BODY_FONT, 15.5, w=w-2*pad-70, color=(CLAY if proj else INK)), x+pad+70, iy)
        iy += 30
    return h


def draw_figure(cr, x, y, w):
    h = 250
    cap_h = 40
    # series: real (solid) then projected (dashed), rising
    pts = [0.06, 0.08, 0.12, 0.20, 0.34, 0.52, 0.70, 0.86, 0.96]
    split = 5
    fx, fy, fw, fh = x+8, y+8, w-16, h-16
    cr.save()
    cr.set_source_rgb(*SOFT); cr.set_line_width(1)
    cr.move_to(fx, fy); cr.line_to(fx, fy+fh); cr.line_to(fx+fw, fy+fh); cr.stroke()  # axes
    def P(i): return (fx + fw*i/(len(pts)-1), fy+fh - fh*pts[i]*0.92)
    cr.set_line_width(3); cr.set_source_rgb(*INK); cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(*P(0))
    for i in range(1, split+1): cr.line_to(*P(i))
    cr.stroke()
    cr.set_source_rgb(*CLAY); cr.set_dash([7, 6])
    cr.move_to(*P(split))
    for i in range(split+1, len(pts)): cr.line_to(*P(i))
    cr.stroke(); cr.set_dash([])
    px, py = P(len(pts)-1); cr.arc(px, py, 5, 0, 2*math.pi); cr.fill()
    cr.restore()
    draw(cr, layout(cr, "Fig. 1 — Sites reporting through Qflow, 2018–2031 (broken line projected).",
                    IT_FONT, 14.5, w=w, color=SOFT), x, y+h+6)
    return h + cap_h


if __name__ == "__main__":
    render()
