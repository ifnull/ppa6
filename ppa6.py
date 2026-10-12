#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Daniel Smith
"""ppa6.py - Print text and images on a PeriPage A6 thermal printer over Bluetooth.

Uses Bluetooth classic RFCOMM via Python's stdlib socket module (no PyBluez).
Requires the system Python on Linux (built with AF_BLUETOOTH) and Pillow.

Protocol derived from the reverse-engineering work in
https://github.com/bitrate16/peripage-python (GPLv3) and cross-checked
against the decompiled official app.

Usage:
    ppa6.py find                        # scan for a PeriPage printer
    ppa6.py -m MAC info                 # query name/firmware/serial/battery
    ppa6.py -m MAC text file.txt        # print a text file (- for stdin)
    ppa6.py -m MAC image photo.png     # print an image, dithered
    ppa6.py -m MAC feed [lines]         # feed paper

    ppa6.py profiles                    # list layout profiles
    ppa6.py -m MAC print -p label "Office Supplies" "Pens, tape, staples"
    ppa6.py -m MAC print -p label -i examples/labels.txt   # one per block
    cat examples/labels.csv | ppa6.py -m MAC print -p label  # or pipe it
    ppa6.py print -p label -i examples/labels.txt --preview out.png

The MAC can also be set via the PERIPAGE_MAC environment variable.
"""

import argparse
import csv
import functools
import io
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass

# pyenv/virtualenv Pythons are often built without Bluetooth support;
# fall back to the system interpreter, which has it.
if not hasattr(socket, 'AF_BLUETOOTH'):
    if os.environ.get('PPA6_REEXEC') or not os.path.exists('/usr/bin/python3'):
        sys.exit('This Python lacks Bluetooth support (AF_BLUETOOTH); '
                 'run with the system /usr/bin/python3.')
    os.environ['PPA6_REEXEC'] = '1'
    os.execv('/usr/bin/python3', ['/usr/bin/python3', os.path.abspath(__file__)]
             + sys.argv[1:])

from PIL import Image, ImageDraw, ImageFont, ImageOps

# --- Printer geometry (PeriPage A6, 203 dpi) ---
ROW_WIDTH = 384          # printable pixels per row
ROW_BYTES = ROW_WIDTH // 8
DOTS_PER_MM = 8          # 203 dpi


class PeriPage:
    """Minimal RFCOMM driver for the PeriPage A6."""

    def __init__(self, mac: str, timeout: float = 3.0):
        self.mac = mac
        self.timeout = timeout
        self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                  socket.BTPROTO_RFCOMM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.mac, 1))
        self.reset()

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send(self, data: bytes):
        self.sock.send(data)

    def ask(self, data: bytes, size: int = 1024) -> bytes:
        self.sock.send(data)
        return self.sock.recv(size)

    # --- Control commands ---

    def reset(self):
        """Wake/init sequence; required after connect and before each print job."""
        self.send(bytes.fromhex('10fffe01') + b'\x00' * 12)

    def feed(self, size: int = 0x40):
        """Advance paper by `size` motor steps (1-255)."""
        self.send(bytes.fromhex('1b4a') + bytes([max(1, min(0xff, size))]))

    def set_concentration(self, level: int):
        """Print darkness: 0 light, 1 medium, 2 dark."""
        self.send(bytes.fromhex('10ff1000') + bytes([max(0, min(2, level))]))

    def info(self) -> dict:
        return {
            'name': self.ask(bytes.fromhex('10ff3011')).decode(errors='replace'),
            'firmware': self.ask(bytes.fromhex('10ff20f1')).decode(errors='replace'),
            'serial': self.ask(bytes.fromhex('10ff20f2')).decode(errors='replace'),
            'battery': self.ask(bytes.fromhex('10ff50f1'))[1],
        }

    # --- Printing ---

    def print_rows(self, rows: bytes, row_delay: float = 0.01):
        """Print raw 1-bit rows (ROW_BYTES bytes each, 1 = black, MSB first)."""
        nrows = len(rows) // ROW_BYTES
        # The height field in the preamble is one byte, so print in chunks.
        for start in range(0, nrows, 0xff):
            height = min(0xff, nrows - start)
            self.reset()
            self.send(bytes.fromhex('1d763000') + bytes([ROW_BYTES, 0x00, height, 0x00]))
            for i in range(start, start + height):
                self.send(rows[i * ROW_BYTES:(i + 1) * ROW_BYTES])
                time.sleep(row_delay)

    def print_image(self, img: Image.Image, dither: bool = True,
                    row_delay: float = 0.01):
        """Scale to printer width, convert to 1-bit, and print."""
        img = img.convert('L')
        if img.width != ROW_WIDTH:
            img = img.resize(
                (ROW_WIDTH, max(1, round(ROW_WIDTH / img.width * img.height))),
                Image.Resampling.LANCZOS)
        img = ImageOps.invert(img)  # printer expects 1 = black
        img = img.convert('1', dither=Image.Dither.FLOYDSTEINBERG if dither
                          else Image.Dither.NONE)
        self.print_rows(img.tobytes(), row_delay=row_delay)


# --- Fonts ---

FONT_DIRS = [
    '/usr/share/fonts/truetype/dejavu',
    '/usr/share/fonts/truetype/liberation',
    '/usr/share/fonts/TTF',
    '/usr/share/fonts/dejavu',
]

# Short names usable as `font = "..."` in profiles. Anything else is treated
# as a file path or handed to fc-match (e.g. "Noto Sans:bold").
FONT_ALIASES = {
    'mono': ['DejaVuSansMono.ttf', 'LiberationMono-Regular.ttf'],
    'mono-bold': ['DejaVuSansMono-Bold.ttf', 'LiberationMono-Bold.ttf'],
    'sans': ['DejaVuSans.ttf', 'LiberationSans-Regular.ttf'],
    'sans-bold': ['DejaVuSans-Bold.ttf', 'LiberationSans-Bold.ttf'],
    'condensed': ['DejaVuSansCondensed.ttf', 'LiberationSansNarrow-Regular.ttf'],
    'condensed-bold': ['DejaVuSansCondensed-Bold.ttf', 'LiberationSansNarrow-Bold.ttf'],
    'serif': ['DejaVuSerif.ttf', 'LiberationSerif-Regular.ttf'],
    'serif-bold': ['DejaVuSerif-Bold.ttf', 'LiberationSerif-Bold.ttf'],
}


@functools.cache
def font_path(name: str) -> str:
    if os.path.isfile(os.path.expanduser(name)):
        return os.path.expanduser(name)
    for base in FONT_ALIASES.get(name, []):
        for d in FONT_DIRS:
            if os.path.exists(os.path.join(d, base)):
                return os.path.join(d, base)
    if name not in FONT_ALIASES and shutil.which('fc-match'):
        path = subprocess.run(['fc-match', '-f', '%{file}', name],
                              capture_output=True, text=True).stdout
        if path:
            return path
    sys.exit(f'Font {name!r} not found; install fonts-dejavu-core or use a '
             f'font path. Aliases: {", ".join(FONT_ALIASES)}')


@functools.cache
def load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(name), size)


def text_width(font: ImageFont.FreeTypeFont, s: str) -> int:
    return font.getbbox(s)[2] if s else 0


# --- Profiles ---
#
# A profile is a TOML file describing the fields a print accepts and how each
# one is styled. See profiles/label.toml for a commented example of every key.

PROFILE_DIRS = [
    os.path.expanduser('~/.config/ppa6/profiles'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'profiles'),
]

PROFILE_DEFAULTS = {
    'description': '',
    'orientation': 'portrait',   # portrait: text across the paper; landscape: along it
    'length': 0,                 # mm along the paper; 0 = fit content
    'min_length': 0,             # mm, when length = 0
    'max_length': 0,             # mm, when length = 0 (0 = no cap)
    'margin': 2,                 # mm; margin_x / margin_y override per axis
    'gap': 1,                    # mm between fields
    'valign': 'center',          # top | center | bottom, when the label has spare height
    'border': 0,                 # border line width in dots (0 = none)
    'border_radius': 0,          # mm
    'batch': True,               # input may hold several records (else one per input)
    'uniform': False,            # cap each field at its smallest size in the batch
    'cut_line': True,            # dashed line between records in a batch
    'spacing': 4,                # mm between records in a batch
    'darkness': -1,              # 0-2, -1 = leave the printer's setting alone
    'dither': False,
    'feed': 0x60,                # motor steps fed after printing
    'fields': [],
}

FIELD_DEFAULTS = {
    'name': '',
    'aliases': [],               # other names accepted in keyed input
    'font': 'sans',
    'size': 0,                   # fixed px size; 0 = auto-fit between min/max
    'min_size': 8,
    'max_size': 0,               # 0 = as large as fits
    'max_ratio': 0.0,            # cap at this fraction of the previous field's size
    'weight': 1,                 # share of the label height when height is bounded
    'wrap': False,               # word-wrap to the label width
    'max_lines': 0,              # 0 = unlimited
    'align': 'center',           # left | center | right
    'line_spacing': 1.0,
    'preserve_spaces': False,    # keep runs of spaces (for preformatted text)
}

# Field keys may also be set at the top level as defaults for every field.
INHERITED_FIELD_KEYS = set(FIELD_DEFAULTS) - {'name', 'aliases'}

CHOICES = {
    'orientation': {'portrait', 'landscape'},
    'valign': {'top', 'center', 'bottom'},
    'align': {'left', 'center', 'right'},
}


def profile_paths() -> dict:
    found = {}
    for d in reversed(PROFILE_DIRS):  # user dir wins
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith('.toml'):
                    found[f[:-5]] = os.path.join(d, f)
    return found


def parse_value(s: str):
    """Interpret an override value as TOML (number, bool, array), else a string."""
    try:
        return tomllib.loads(f'v = {s}')['v']
    except tomllib.TOMLDecodeError:
        return s


def check_keys(where: str, d: dict, allowed: dict):
    for k, v in d.items():
        if k not in allowed:
            sys.exit(f'{where}: unknown key {k!r} '
                     f'(expected one of: {", ".join(sorted(allowed))})')
        if k in CHOICES and v not in CHOICES[k]:
            sys.exit(f'{where}: {k} must be one of {sorted(CHOICES[k])}, got {v!r}')
        # Match the default's type; any number will do for a numeric key.
        want = type(allowed[k])
        if want in (int, float):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        else:
            ok = isinstance(v, want)
        if not ok:
            kind = 'a number' if want in (int, float) else {
                bool: 'true or false', str: 'a string', list: 'a list'}[want]
            sys.exit(f'{where}: {k} must be {kind}, got {v!r}')


def load_profile(name: str, overrides: list[str] = ()) -> dict:
    """Load a profile by name or path, apply KEY=VALUE overrides, fill defaults."""
    if os.sep in name or name.endswith('.toml'):
        path = name
    else:
        path = profile_paths().get(name)
        if not path:
            sys.exit(f'Unknown profile {name!r}; available: '
                     f'{", ".join(profile_paths()) or "(none)"}')
    with open(path, 'rb') as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            sys.exit(f'{path}: {e}')
    raw_fields = raw.pop('fields', [])
    if not raw_fields:
        sys.exit(f'{path}: profile defines no [[fields]]')

    for o in overrides:
        key, sep, value = o.partition('=')
        if not sep:
            sys.exit(f'Override {o!r} must look like KEY=VALUE or FIELD.KEY=VALUE')
        target, _, key = key.strip().rpartition('.')
        if target:
            matches = [f for f in raw_fields
                       if target in [f.get('name'), *f.get('aliases', [])]]
            if not matches:
                sys.exit(f'Override {o!r}: profile has no field {target!r}')
            matches[0][key] = parse_value(value)
        else:
            raw[key] = parse_value(value)

    check_keys(path, raw, PROFILE_DEFAULTS | {'margin_x': 0, 'margin_y': 0}
               | {k: FIELD_DEFAULTS[k] for k in INHERITED_FIELD_KEYS})
    prof = PROFILE_DEFAULTS | {k: v for k, v in raw.items()
                               if k not in INHERITED_FIELD_KEYS}
    prof.setdefault('margin_x', raw.get('margin_x', prof['margin']))
    prof.setdefault('margin_y', raw.get('margin_y', prof['margin']))
    inherited = {k: v for k, v in raw.items() if k in INHERITED_FIELD_KEYS}
    prof['fields'] = []
    for i, f in enumerate(raw_fields):
        check_keys(f'{path} field {i + 1}', f, FIELD_DEFAULTS)
        if not f.get('name'):
            sys.exit(f'{path}: field {i + 1} needs a name')
        prof['fields'].append(FIELD_DEFAULTS | inherited | f)
    prof['path'] = path
    return prof


# --- Input parsing ---

KEYED_LINE = re.compile(r'^\s*([A-Za-z][\w-]*)\s*:\s?(.*)$')


def field_names(profile: dict) -> dict:
    """Map every accepted (lowercased) name or alias to its field name."""
    names = {}
    for f in profile['fields']:
        for n in [f['name'], *f['aliases']]:
            names[n.lower()] = f['name']
    return names


def positional(values: list[str], profile: dict) -> dict:
    """Assign values to fields in order; extras become more lines of the last."""
    names = [f['name'] for f in profile['fields']]
    n = len(names)
    if len(values) > n:
        values = values[:n - 1] + ['\n'.join(values[n - 1:])]
    return dict(zip(names, values))


def keyed(item: dict, profile: dict, where: str) -> dict:
    names = field_names(profile)
    rec = {}
    for k, v in item.items():
        if str(k).lower() not in names:
            sys.exit(f'{where}: unknown field {k!r} for profile '
                     f'(fields: {", ".join(f["name"] for f in profile["fields"])})')
        rec[names[str(k).lower()]] = '' if v is None else str(v)
    return rec


def parse_text_blocks(text: str, profile: dict) -> list[dict]:
    """Blank-line-separated blocks. Lines map to fields in order, or use
    `field: value` lines when a block starts with a known field name."""
    names = field_names(profile)
    records = []
    for block in re.split(r'\n[ \t]*\n', text.strip('\n')):
        lines = [ln.rstrip() for ln in block.splitlines()]
        if not any(lines):
            continue
        m = KEYED_LINE.match(lines[0])
        if m and m[1].lower() in names:
            rec, cur = {}, None
            for ln in lines:
                m = KEYED_LINE.match(ln)
                if m and m[1].lower() in names:
                    cur = names[m[1].lower()]
                    rec[cur] = m[2].strip()
                else:
                    rec[cur] = f'{rec[cur]}\n{ln.strip()}'.strip('\n')
            records.append(rec)
        else:
            records.append(positional([ln.strip() for ln in lines], profile))
    return records


def parse_csv(text: str, profile: dict, delimiter: str) -> list[dict]:
    """CSV/TSV rows; a first row made of field names is used as the header."""
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)
            if any(c.strip() for c in r)]
    if not rows:
        return []
    names = field_names(profile)
    header = [c.strip().lower() for c in rows[0]]
    if all(h in names for h in header if h):
        return [keyed({h: c for h, c in zip(header, r) if h}, profile, 'csv')
                for r in rows[1:]]
    return [positional([c.strip() for c in r], profile) for r in rows]


def parse_json(text: str, profile: dict) -> list[dict]:
    """An object, or a list of objects / arrays / strings."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f'Invalid JSON input: {e}')
    if not isinstance(data, list):
        data = [data]
    records = []
    for i, item in enumerate(data):
        if isinstance(item, dict):
            records.append(keyed(item, profile, f'json item {i + 1}'))
        elif isinstance(item, list):
            records.append(positional([str(v) for v in item], profile))
        else:
            records.append(positional([str(item)], profile))
    return records


def parse_records(text: str, profile: dict, fmt: str = 'auto',
                  filename: str = '') -> list[dict]:
    if not profile['batch']:
        return [positional([text], profile)]
    if fmt == 'auto':
        ext = os.path.splitext(filename)[1].lower()
        if ext in ('.json', '.csv', '.tsv'):
            fmt = ext[1:]
        elif text.lstrip()[:1] in ('[', '{'):
            fmt = 'json'
        else:
            fmt = 'text'
    if fmt == 'json':
        return parse_json(text, profile)
    if fmt in ('csv', 'tsv'):
        return parse_csv(text, profile, ',' if fmt == 'csv' else '\t')
    return parse_text_blocks(text, profile)


# --- Layout ---

def px(mm: float) -> int:
    return round(mm * DOTS_PER_MM)


@dataclass
class Block:
    """One field's text, laid out at a chosen font size."""
    field: dict
    font: ImageFont.FreeTypeFont
    lines: list[str]
    line_h: int
    width: int

    @property
    def height(self) -> int:
        return self.line_h * len(self.lines)


def wrap_lines(text: str, font, width: float, field: dict) -> list[str]:
    out = []
    for para in text.splitlines() or ['']:
        if not field['preserve_spaces']:
            para = ' '.join(para.split())
        if not field['wrap'] or not para:
            out.append(para)
            continue
        words = para.split(' ')
        line = words[0]
        for w in words[1:]:
            if text_width(font, f'{line} {w}') <= width:
                line = f'{line} {w}'
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def fit_field(field: dict, text: str, width: float, band: float,
              cap: float = 0) -> Block:
    """Lay out `text` at the largest size that fits `width` x `band`."""
    def attempt(size):
        font = load_font(field['font'], size)
        lines = wrap_lines(text, font, width, field)
        ascent, descent = font.getmetrics()
        line_h = round((ascent + descent) * field['line_spacing'])
        block = Block(field, font, lines, line_h,
                      max(text_width(font, ln) for ln in lines))
        fits = (block.width <= width and block.height <= band
                and (not field['max_lines'] or len(lines) <= field['max_lines']))
        return fits, block

    lo = field['min_size']
    if field['size']:
        lo = hi = field['size']
    else:
        # Line height is >= the font size for any sane font, so the band
        # bounds the search even when the width is unbounded.
        hi = min(x for x in (field['max_size'] or math.inf, cap or math.inf,
                             band, 1000))
        hi = max(lo, int(hi))
    fits, best = attempt(lo)
    if not fits:
        return overflow(field, best, width)
    while lo < hi:  # largest size that fits
        mid = (lo + hi + 1) // 2
        fits, block = attempt(mid)
        if fits:
            lo, best = mid, block
        else:
            hi = mid - 1
    return best


def overflow(field: dict, block: Block, width: float) -> Block:
    """Text doesn't fit even at min_size: truncate extra lines, warn."""
    name = field['name']
    if field['max_lines'] and len(block.lines) > field['max_lines']:
        lines = block.lines[:field['max_lines']]
        last = lines[-1]
        while last and text_width(block.font, last + '…') > width:
            last = last[:-1].rstrip()
        lines[-1] = last + '…'
        print(f'warning: {name!r} truncated to {field["max_lines"]} line(s)',
              file=sys.stderr)
        block = Block(field, block.font, lines, block.line_h,
                      max(text_width(block.font, ln) for ln in lines))
    if block.width > width:
        print(f'warning: {name!r} is too wide and will be clipped',
              file=sys.stderr)
    return block


def stack(items: list, width: float, height: float, gap: int) -> list[Block]:
    """Fit fields top to bottom. With a bounded height each field gets its
    weight's share of whatever the fields above it left over."""
    blocks = []
    remaining = height - gap * (len(items) - 1)
    prev_size = 0
    for i, (field, text) in enumerate(items):
        band = remaining * field['weight'] / sum(f['weight'] for f, _ in items[i:])
        cap = prev_size * field['max_ratio'] if field['max_ratio'] else 0
        block = fit_field(field, text, width, band, cap)
        blocks.append(block)
        remaining -= block.height
        prev_size = block.font.size
    return blocks


def render_record(profile: dict, record: dict) -> tuple[Image.Image, dict]:
    """Render one record upright (as it will be read), at 1 px per dot.
    Returns the image and the font size chosen for each field."""
    mx, my = px(profile['margin_x']), px(profile['margin_y'])
    gap = px(profile['gap'])
    length = px(profile['length'])
    items = [(f, record[f['name']].replace('\t', '    '))
             for f in profile['fields'] if record.get(f['name'], '').strip()]
    if not items:
        sys.exit(f'Nothing to print in record {record!r}')

    if profile['orientation'] == 'landscape':
        inner_h = ROW_WIDTH - 2 * my
        if length:
            inner_w = length - 2 * mx
        else:
            # Size to the first field (the name), within the length limits.
            cap = px(profile['max_length']) - 2 * mx if profile['max_length'] else math.inf
            natural = stack(items, cap, inner_h, gap)[0].width
            inner_w = min(cap, max(px(profile['min_length']) - 2 * mx, natural))
        blocks = stack(items, inner_w, inner_h, gap)
        width, height = inner_w + 2 * mx, ROW_WIDTH
    else:
        inner_w = ROW_WIDTH - 2 * mx
        blocks = stack(items, inner_w, length - 2 * my if length else math.inf, gap)
        width = ROW_WIDTH
        height = length or sum(b.height for b in blocks) + gap * (len(blocks) - 1) + 2 * my

    content_h = sum(b.height for b in blocks) + gap * (len(blocks) - 1)
    spare = height - 2 * my - content_h
    y = my + {'top': 0, 'center': spare // 2, 'bottom': spare}[profile['valign']]

    img = Image.new('L', (int(width), int(height)), 255)
    draw = ImageDraw.Draw(img)
    for b in blocks:
        for line in b.lines:
            free = inner_w - text_width(b.font, line)
            x = mx + {'left': 0, 'center': free // 2, 'right': free}[b.field['align']]
            draw.text((x, y), line, font=b.font, fill=0)
            y += b.line_h
        y += gap
    if profile['border']:
        draw.rounded_rectangle((0, 0, img.width - 1, img.height - 1),
                               radius=px(profile['border_radius']),
                               outline=0, width=profile['border'])
    return img, {b.field['name']: b.font.size for b in blocks}


def render_job(profile: dict, records: list[dict]) -> Image.Image:
    """Render records into one strip, ROW_WIDTH wide, in print orientation."""
    if profile['uniform'] and len(records) > 1:
        # Shrink every field to the smallest size it got anywhere in the batch,
        # so a set of labels matches. A record can still go smaller if it must.
        # Capping one field can change the label size and squeeze another,
        # so repeat until no size changes (sizes only shrink, so this ends).
        prev = None
        while True:
            smallest = {}
            for rec in records:
                for name, size in render_record(profile, rec)[1].items():
                    smallest[name] = min(size, smallest.get(name, size))
            if smallest == prev:
                break
            prev = smallest
            profile = profile | {'fields': [
                f | {'max_size': smallest[f['name']]} if f['name'] in smallest else f
                for f in profile['fields']]}

    pages = []
    for rec in records:
        img = render_record(profile, rec)[0]
        if profile['orientation'] == 'landscape':
            img = img.transpose(Image.Transpose.ROTATE_270)  # start prints first
        pages.append(img)

    spacing = px(profile['spacing'])
    total = sum(p.height for p in pages) + spacing * (len(pages) - 1)
    strip = Image.new('L', (ROW_WIDTH, total), 255)
    draw = ImageDraw.Draw(strip)
    y = 0
    for i, page in enumerate(pages):
        if i and spacing:
            if profile['cut_line']:
                mid = y - spacing // 2
                for x in range(0, ROW_WIDTH, 16):
                    draw.rectangle((x, mid - 1, x + 7, mid), fill=0)
        strip.paste(page, (0, y))
        y += page.height + spacing
    return strip


def preview_image(profile: dict, strip: Image.Image) -> Image.Image:
    """Strip as printed (1-bit, like print_image), turned upright for
    landscape profiles."""
    strip = strip.convert('1', dither=Image.Dither.FLOYDSTEINBERG
                          if profile['dither'] else Image.Dither.NONE)
    if profile['orientation'] == 'landscape':
        return strip.transpose(Image.Transpose.ROTATE_90)
    return strip


# --- Discovery ---

def find_printer(scan_seconds: int = 12):
    """Scan for bluetooth devices named PeriPage* and print their MAC."""
    if not shutil.which('bluetoothctl'):
        sys.exit('bluetoothctl not found; scan manually and pass -m MAC.')
    print(f'Scanning for {scan_seconds}s (make sure the printer is on)...')
    subprocess.run(['bluetoothctl', '--timeout', str(scan_seconds), 'scan', 'on'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = subprocess.run(['bluetoothctl', 'devices'], capture_output=True,
                         text=True, timeout=10).stdout
    found = re.findall(r'Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) (PeriPage.*)',
                       out, re.IGNORECASE)
    if not found:
        sys.exit('No PeriPage found. Is the printer on (solid power light)?')
    for mac, name in found:
        print(f'{mac}  {name}')
    return found


def list_profiles():
    for name, path in profile_paths().items():
        prof = load_profile(path)
        fields = ', '.join(
            f['name'] + (f' ({"/".join(f["aliases"])})' if f['aliases'] else '')
            for f in prof['fields'])
        print(f'{name:12} {prof["description"]}')
        print(f'{"":12} fields: {fields}')
        print(f'{"":12} {prof["orientation"]}, {path}')


# --- CLI ---

def read_input(args) -> tuple[str, str]:
    """Return (text, filename) from --input, inline values, or piped stdin."""
    if args.input and args.values:
        sys.exit('Pass inline values or --input, not both.')
    if args.input == '-' or (not args.input and not args.values
                             and not sys.stdin.isatty()):
        return sys.stdin.read(), ''
    if args.input:
        with open(args.input, encoding='utf-8') as f:
            return f.read(), args.input
    if not args.values:
        sys.exit('Nothing to print: give inline values, --input FILE, or pipe '
                 'text on stdin.')
    return '', ''


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog='Profiles are TOML files in ' + ' or '.join(PROFILE_DIRS) + '.')
    ap.add_argument('-m', '--mac', default=os.environ.get('PERIPAGE_MAC'),
                    help='printer MAC address (or set PERIPAGE_MAC)')
    ap.add_argument('-d', '--darkness', type=int, choices=[0, 1, 2], default=None,
                    help='print concentration: 0 light, 1 medium, 2 dark')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('find', help='scan for PeriPage printers')
    sub.add_parser('info', help='query printer name/firmware/serial/battery')
    sub.add_parser('profiles', help='list layout profiles and their fields')

    p = sub.add_parser('print', help='print records using a layout profile',
                       description='Fields come from inline values (in profile '
                       'order), --input FILE, or piped stdin. Files may be '
                       'blank-line-separated text blocks (one line per field, '
                       'or "field: value" lines), CSV/TSV, or JSON.')
    p.add_argument('values', nargs='*', help='field values, in profile order')
    p.add_argument('-p', '--profile', default='dense',
                   help='profile name or .toml path (default: dense)')
    p.add_argument('-i', '--input', metavar='FILE',
                   help='read records from FILE (- for stdin)')
    p.add_argument('-f', '--format', default='auto',
                   choices=['auto', 'text', 'csv', 'tsv', 'json'],
                   help='input format (default: from extension / content)')
    p.add_argument('-o', '--set', dest='overrides', action='append', default=[],
                   metavar='KEY=VALUE',
                   help='override a profile setting, e.g. length=60 or '
                   'title.font=serif-bold (repeatable)')
    p.add_argument('--preview', metavar='OUT.png',
                   help='render to an image file instead of printing')

    p = sub.add_parser('text', help='print a text file (- for stdin); '
                       'same as "print -p dense -i FILE"')
    p.add_argument('file')
    p.add_argument('--font-size', type=int, default=0,
                   help='font size in px (default: auto-fit longest line)')
    p.add_argument('--preview', metavar='OUT.png',
                   help='render to an image file instead of printing')

    p = sub.add_parser('image', help='print an image file')
    p.add_argument('file')
    p.add_argument('--no-dither', action='store_true',
                   help='hard threshold instead of Floyd-Steinberg dithering')

    p = sub.add_parser('feed', help='feed paper')
    p.add_argument('size', type=int, nargs='?', default=0x40)

    args = ap.parse_args()

    if args.cmd == 'find':
        find_printer()
        return
    if args.cmd == 'profiles':
        list_profiles()
        return

    if args.cmd == 'text':
        args.cmd, args.profile, args.values = 'print', 'dense', []
        args.input, args.format = args.file, 'auto'
        args.overrides = [f'size={args.font_size}'] if args.font_size else []

    if args.cmd == 'print':
        profile = load_profile(args.profile, args.overrides)
        text, filename = read_input(args)
        records = ([positional(args.values, profile)] if args.values
                   else parse_records(text, profile, args.format, filename)
                   if text.strip() else [])
        if not records:
            sys.exit('No records found in input.')
        strip = render_job(profile, records)
        if args.preview:
            preview_image(profile, strip).save(args.preview)
            print(f'Preview written to {args.preview} ({len(records)} '
                  f'record(s), {strip.height / DOTS_PER_MM:.0f}mm of paper)')
            return
        if args.darkness is None and profile['darkness'] >= 0:
            args.darkness = profile['darkness']

    if not args.mac:
        sys.exit('Printer MAC required: pass -m MAC or set PERIPAGE_MAC '
                 '(run "ppa6.py find" to discover it).')

    with PeriPage(args.mac) as printer:
        if args.darkness is not None:
            printer.set_concentration(args.darkness)
            time.sleep(0.1)

        if args.cmd == 'info':
            for k, v in printer.info().items():
                print(f'{k:9} {v}')
        elif args.cmd == 'print':
            printer.print_image(strip, dither=profile['dither'])
            printer.feed(profile['feed'])
        elif args.cmd == 'image':
            printer.print_image(Image.open(args.file),
                                dither=not args.no_dither)
            printer.feed(0x60)
        elif args.cmd == 'feed':
            printer.feed(args.size)
        time.sleep(0.5)  # let the printer drain its buffer before disconnect


if __name__ == '__main__':
    main()
