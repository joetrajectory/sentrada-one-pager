#!/usr/bin/env python3
"""
Generate a STAND-IN blank newspaper template for testing the layout engine.

This is scaffolding only. Replace it with the real Magnific-upscaled template
once it is finalised; newspaper.py works on any blank template image. The point
of this file is to let you run the pipeline end to end today.

    python make_test_template.py --output template.png            # 948x1341
    python make_test_template.py --output template_4x.png --scale 4

It produces a cream newsprint field with a faint grain so you can verify the
multiply compositing lets the texture show through the ink.
"""

import argparse

from PIL import Image, ImageChops, ImageFilter

CREAM = (244, 240, 230)  # Lisbon Stone-ish cream
BASE_W, BASE_H = 948, 1341  # A2 portrait proportion, pre-upscale


def make_template(width, height):
    base = Image.new("RGB", (width, height), CREAM)
    # Faint paper grain via low amplitude noise multiplied onto the cream.
    noise = Image.effect_noise((width, height), 14).convert("L")
    noise = noise.point(lambda v: 200 + (v - 128) // 4)  # compress to a tight band
    grain = Image.merge("RGB", (noise, noise, noise))
    paper = ImageChops.multiply(base, grain.point(lambda v: 180 + v // 4))
    paper = Image.blend(base, paper, 0.35)
    paper = paper.filter(ImageFilter.GaussianBlur(0.4))
    return paper


def main():
    ap = argparse.ArgumentParser(description="Generate a stand-in blank template.")
    ap.add_argument("--output", required=True)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiply the 948x1341 base size (e.g. 4 for the upscale)")
    args = ap.parse_args()
    w = int(round(BASE_W * args.scale))
    h = int(round(BASE_H * args.scale))
    make_template(w, h).save(args.output)
    print(f"wrote {args.output} ({w}x{h})")


if __name__ == "__main__":
    main()
