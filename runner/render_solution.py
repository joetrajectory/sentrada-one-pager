#!/usr/bin/env python3
"""Render a crossword SOLUTION (every cell filled with its answer letter) plus an
answer key, from a piece's data.json. Proof that the puzzle is a valid, completable
grid. Reproduces the delivered grid exactly (same generator, seed and attempts as
crossword.py), so the solution maps 1:1 onto the printed crossword.

    python runner/render_solution.py <piece-folder> <out.jpg>
"""
import sys, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "crossword"))
import grid_generator as gg
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_NUM = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
GRID_ATTEMPTS = 500  # matches crossword.py cfg["grid_attempts"]


def render(folder, out):
    d = json.load(open(os.path.join(folder, "data.json")))
    res = gg.generate_grid(d["candidates"], min_words=d.get("min_words", 15),
                           max_words=d.get("max_words", 20), seed=d.get("seed"),
                           attempts=GRID_ATTEMPTS)
    rows, cols, grid = res["rows"], res["cols"], res["grid"]
    numbers = {tuple(int(x) for x in k.split(",")): v for k, v in res["numbers"].items()}

    cell = 70
    pad = 60
    grid_w = cols * cell
    grid_h = rows * cell

    # Answer key columns sit below the grid.
    title_f = ImageFont.truetype(FONT_SERIF, 46)
    sub_f = ImageFont.truetype(FONT_NUM, 26)
    key_h_f = ImageFont.truetype(FONT, 30)
    key_f = ImageFont.truetype(FONT_NUM, 24)
    letter_f = ImageFont.truetype(FONT, 40)
    num_f = ImageFont.truetype(FONT_NUM, 18)

    col_w = (max(grid_w, 1500) + pad * 2) // 2 - pad - 40

    def wrap(text, font, width):
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if _dummy.textlength(trial, font=font) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    _dummy = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def keylines(bucket):
        out = []
        for e in res[bucket]:
            raw = f"{e['number']}. {e['clue']}  =  {e['answer']}"
            out.append(wrap(raw, key_f, col_w))
        return out
    across_lines = keylines("across")
    down_lines = keylines("down")
    line_h = 34
    across_n = sum(len(b) for b in across_lines)
    down_n = sum(len(b) for b in down_lines)
    key_rows = max(across_n, down_n)
    key_block_h = 60 + key_rows * line_h

    W = max(grid_w, 1500) + pad * 2
    H = pad + 70 + 40 + grid_h + 60 + key_block_h + pad
    img = Image.new("RGB", (W, H), "#f4ecd8")
    dr = ImageDraw.Draw(img)

    # Title
    title = d.get("title", "CROSSWORD") + "  —  SOLUTION"
    dr.text((pad, pad), title, font=title_f, fill="#2b2118")
    dr.text((pad, pad + 58), "Every answer placed; every crossing letter agrees. This is the completed grid.",
            font=sub_f, fill="#6b5a44")

    # Grid (centred horizontally)
    gx = (W - grid_w) // 2
    gy = pad + 130
    for r in range(rows):
        for c in range(cols):
            ch = grid[r][c]
            if ch is None:
                continue
            x0, y0 = gx + c * cell, gy + r * cell
            dr.rectangle([x0, y0, x0 + cell, y0 + cell], fill="white", outline="#2b2118", width=3)
            n = numbers.get((r, c))
            if n:
                dr.text((x0 + 5, y0 + 3), str(n), font=num_f, fill="#7a6a55")
            bb = dr.textbbox((0, 0), ch, font=letter_f)
            lw, lh = bb[2] - bb[0], bb[3] - bb[1]
            dr.text((x0 + (cell - lw) / 2 - bb[0], y0 + (cell - lh) / 2 - bb[1]),
                    ch, font=letter_f, fill="#2b2118")

    # Answer key
    ky = gy + grid_h + 55
    colx_a = pad
    colx_d = W // 2 + 20
    dr.text((colx_a, ky), "ACROSS", font=key_h_f, fill="#2b2118")
    dr.text((colx_d, ky), "DOWN", font=key_h_f, fill="#2b2118")

    def draw_key(blocks, x):
        y = ky + 50
        for block in blocks:
            for j, ln in enumerate(block):
                dr.text((x + (0 if j == 0 else 24), y), ln, font=key_f, fill="#3a2e20")
                y += line_h

    draw_key(across_lines, colx_a)
    draw_key(down_lines, colx_d)

    img.save(out, "JPEG", quality=90)
    print(f"{os.path.basename(folder)}: {res['word_count']} words, "
          f"{rows}x{cols} -> {out}")


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
