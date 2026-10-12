#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Daniel Smith
"""Regenerate the README images in docs/images/.

Each image is rendered by ppa6.py's --preview from the files in examples/,
then drawn as a strip of thermal paper. Run from anywhere:

    docs/make_images.py
"""

import os
import random
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'docs', 'images')

# (output name, ppa6.py print arguments)
EXAMPLES = [
    ('label', ['-p', 'label', '-i', 'examples/labels.txt']),
    ('label-single', ['-p', 'label', 'Office Supplies',
                      'Pens, tape, staples, and sticky notes']),
    ('label-options', ['-p', 'label', '-i', 'examples/labels.txt',
                       '-o', 'length=60', '-o', 'title.font=serif-bold']),
    ('label-border', ['-p', 'label', '-o', 'border=3', 'First Aid',
                      'Bandages, gauze, tape, and antiseptic wipes']),
    ('label-not-uniform', ['-p', 'label', '-i', 'examples/labels.txt',
                           '-o', 'uniform=false']),
    ('label-long', ['-p', 'label', 'Holiday Decorations and Gift Wrap',
                    'Ornaments, lights, ribbon, and gift tags']),
    ('tag', ['-p', 'tag', '-i', 'examples/labels.csv']),
    ('dense', ['-p', 'dense', '-i', 'examples/recipe.txt']),
]

# Profiles printed sideways; their previews are upright, so the strip is turned.
LANDSCAPE_PROFILES = {'label'}

PAPER = (250, 248, 242)
INK = (28, 28, 30)
PAPER_MARGIN = 40     # dots of blank paper beside the 384-dot print area (58mm roll)
LEAD = 24             # blank paper before and after the print
TOOTH = 7             # size of the torn-edge zigzag
SHADOW = 10


def torn_edge(length: int, rng: random.Random) -> list[int]:
    """Depth of the tear at each tooth along an edge."""
    return [rng.randint(0, TOOTH) for _ in range(length // TOOTH + 2)]


def paper_strip(preview: Image.Image, landscape: bool, seed: int) -> Image.Image:
    """Draw a 1-bit preview as a torn strip of paper with a soft shadow."""
    # Work with the paper running top to bottom, then turn it for landscape.
    if landscape:
        preview = preview.transpose(Image.Transpose.ROTATE_270)
    w = preview.width + 2 * PAPER_MARGIN
    h = preview.height + 2 * (LEAD + TOOTH)

    rng = random.Random(seed)
    top, bottom = torn_edge(w, rng), torn_edge(w, rng)
    outline = ([(i * TOOTH, top[i]) for i in range(len(top))]
               + [(i * TOOTH, h - 1 - bottom[i]) for i in reversed(range(len(bottom)))])
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)

    paper = Image.new('RGB', (w, h), PAPER)
    ink = preview.convert('L').point(lambda v: 255 if v < 128 else 0)
    paper.paste(INK, (PAPER_MARGIN, LEAD + TOOTH), ink)

    if landscape:
        paper = paper.transpose(Image.Transpose.ROTATE_90)
        mask = mask.transpose(Image.Transpose.ROTATE_90)

    pad = SHADOW * 3
    canvas = Image.new('RGBA', (paper.width + 2 * pad, paper.height + 2 * pad),
                       (0, 0, 0, 0))
    shadow = Image.new('L', canvas.size, 0)
    shadow.paste(mask.point(lambda v: v * 45 // 255), (pad + 2, pad + 4))
    shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW / 2))
    canvas.paste((0, 0, 0, 255), (0, 0), shadow)
    canvas.paste(paper, (pad, pad), mask)
    return canvas


def main():
    os.makedirs(OUT, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for seed, (name, args) in enumerate(EXAMPLES):
            raw = os.path.join(tmp, f'{name}.png')
            subprocess.run([sys.executable, os.path.join(ROOT, 'ppa6.py'),
                            'print', *args, '--preview', raw],
                           cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            preview = Image.open(raw)
            landscape = args[args.index('-p') + 1] in LANDSCAPE_PROFILES
            paper_strip(preview, landscape, seed).save(
                os.path.join(OUT, f'{name}.png'), optimize=True)
            print(f'docs/images/{name}.png')


if __name__ == '__main__':
    main()
