#!/usr/bin/env python3
"""Generates every binary asset the app ships, from nothing but this file.

    python3 tools/assets.py            # write src/animations, src/sounds, src/appmeta icons
    python3 tools/assets.py --check    # fail if what is on disk differs from what this would write
    python3 tools/assets.py --sheet out.png   # also render every animation frame to one PNG

The bar plays these itself. An animation is one draw request naming a file and
a section; a tone is one request naming a file. Nothing here is streamed.

Formats, all taken from the firmware source (busy-app/busybar-firmware):

  .anim   "bicycle1", lib/anim_file/anim_file_format.h; the encoder below is a
          port of scripts/seq2anim.py and scripts/flipper/rle.py, minus the
          QOI-like pixel mode, which the format makes optional per frame.
  .snd    bare 16-bit little-endian mono PCM at 44.1 kHz, no header
          (scripts/audio.py ends in `-f s16le -ar 44100`).
  .png    8-bit RGBA, not interlaced, as the stock app icons are.

Standard library only, Python 3.9+.
"""
import argparse
import math
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

# --- .anim: the container ---------------------------------------------------
SIGNATURE = b"bicycle1"
HEADER = "<8sBBBBHHBBIIII"
COLOR_BGR888 = 0
MASK_BLACK, MASK_WHITE, MASK_RLE_BLACK, MASK_RLE_WHITE, MASK_BITMAP = range(5)
PX_RAW, PX_RLE, PX_QOI = range(3)

RLE_MAX_BLOCKS = 127
RLE_THRESHOLD = 3


def rle_compress(source, blk):
    """lib/toolbox/rle_encode, as scripts/flipper/rle.py writes it."""
    out = bytearray()
    i, n = 0, len(source)
    while i < n:
        head = source[i:i + blk]
        repeat = 0
        for j in range(i, n, blk):
            if source[j:j + blk] != head:
                break
            repeat += 1
        repeat = min(repeat, RLE_MAX_BLOCKS)
        if repeat < RLE_THRESHOLD:
            repeat = verbatim = 0
            for j in range(i, n, blk):
                if source[j:j + blk] == source[j + blk:j + 2 * blk]:
                    repeat += 1
                    if repeat > RLE_THRESHOLD:
                        break
                else:
                    verbatim += 1 + repeat
                    repeat = 0
            verbatim = min(verbatim + repeat, RLE_MAX_BLOCKS)
            out.append(0x80 | verbatim)
            out += source[i:i + verbatim * blk]
            i += verbatim * blk
        else:
            out.append(repeat)
            out += head
            i += repeat * blk
    return bytes(out)


def rle_decompress(source, blk):
    out = bytearray()
    i = 0
    while i < len(source):
        op = source[i]
        count = op & 0x7F
        i += 1
        if op & 0x80:
            out += source[i:i + count * blk]
            i += count * blk
        else:
            out += source[i:i + blk] * count
            i += blk
    return bytes(out)


class Bits:
    """MSB-first bit writer; the mask run lengths are 3 or 8 bits each."""

    def __init__(self):
        self.acc = self.n = self.total = 0
        self.out = bytearray()

    def push(self, value, count):
        self.acc = (self.acc << count) | (value & ((1 << count) - 1))
        self.n += count
        self.total += count
        while self.n >= 8:
            self.n -= 8
            self.out.append((self.acc >> self.n) & 0xFF)
            self.acc &= (1 << self.n) - 1

    def done(self):
        if self.n:
            self.out.append((self.acc << (8 - self.n)) & 0xFF)
        return bytes(self.out), self.total


def encode_mask(mask):
    """Which pixels of this frame differ from the last, as (encoding, bytes, bits)."""
    runs, value, length = [], mask[0], 0
    for bit in mask:
        if bit == value:
            length += 1
        else:
            runs.append(length)
            value, length = bit, 1
    runs.append(length)
    if len(runs) == 1:
        return (MASK_WHITE if mask[0] else MASK_BLACK), b"", 0

    bits = Bits()
    for length in runs:
        if length >= 7:
            while length >= 255:
                bits.push(7, 3)
                bits.push(255, 8)
                length -= 255
                bits.push(0, 3)  # a zero-length run of the other colour
            bits.push(7, 3)
            bits.push(length, 8)
        else:
            bits.push(length, 3)
    data, count = bits.done()
    if count <= len(mask) * 0.75:
        return (MASK_RLE_WHITE if mask[0] else MASK_RLE_BLACK), data, count

    bitmap = bytearray()
    for i in range(0, len(mask), 8):
        byte = 0
        for j, bit in enumerate(mask[i:i + 8]):
            if bit:
                byte |= 128 >> j
        bitmap.append(byte)
    return MASK_BITMAP, bytes(bitmap), len(mask)


def encode_anim(width, height, fps, frames, sections):
    """frames: lists of (r, g, b), row-major. sections: [(name, first, last)],
    inclusive. The firmware adds nothing; "default" (the whole file) goes first,
    as seq2anim.py puts it."""
    assert 0 < width < 256 and 0 < height < 256 and 0 < fps < 256
    sections = [("default", 0, len(frames) - 1)] + list(sections)
    for name, first, last in sections:
        assert 0 <= first <= last < len(frames), name
    keyframes = {first for _, first, _ in sections}

    blobs, max_mask, max_px, previous = [], 0, 0, None
    for index, frame in enumerate(frames):
        assert len(frame) == width * height
        if previous is None or index in keyframes:
            mask = [True] * len(frame)
        else:
            mask = [a != b for a, b in zip(frame, previous)]
        packed = bytearray()
        for (r, g, b), present in zip(frame, mask):
            if present:
                packed += bytes((b, g, r))
        packed = bytes(packed)
        squeezed = rle_compress(packed, 3)
        px_mode, pixels = (PX_RLE, squeezed) if len(squeezed) < len(packed) else (PX_RAW, packed)
        mask_mode, mask_data, mask_bits = encode_mask(mask)
        blobs.append(struct.pack("<BHH", px_mode | (mask_mode << 4), mask_bits, len(pixels))
                     + mask_data + pixels)
        max_mask, max_px = max(max_mask, mask_bits), max(max_px, len(pixels))
        previous = frame

    sections_len = sum(12 + len(name) + 1 for name, _, _ in sections)
    offsets, at = [], struct.calcsize(HEADER) + sections_len
    for blob in blobs:
        offsets.append(at)
        at += len(blob)

    out = bytearray(struct.pack(
        HEADER, SIGNATURE, 0, width, height, COLOR_BGR888, max_mask, max_px, fps, 0,
        sections_len, sum(len(b) for b in blobs), len(sections), len(blobs)))
    for name, first, last in sections:
        out += struct.pack("<III", first, last, offsets[first]) + name.encode() + b"\0"
    for blob in blobs:
        out += blob
    return bytes(out)


def decode_mask(mode, data, bits, count):
    if mode == MASK_BLACK:
        return [False] * count
    if mode == MASK_WHITE:
        return [True] * count
    if mode == MASK_BITMAP:
        return [bool(data[i // 8] & (128 >> (i % 8))) for i in range(count)]
    mask, value, pos = [], mode == MASK_RLE_WHITE, 0

    def take(n):
        nonlocal pos
        v = 0
        for _ in range(n):
            v = (v << 1) | ((data[pos // 8] >> (7 - pos % 8)) & 1)
            pos += 1
        return v

    while pos < bits and len(mask) < count:
        length = take(3)
        if length == 7:
            length = take(8)
        mask += [value] * length
        value = not value
    assert len(mask) == count, (len(mask), count)
    return mask


def qoi_decode(data, old, alpha_in_stream):
    """The QOI-like mode, decode only: here to read files this did not write."""
    lut = [(0, 0, 0, 0)] * 64
    out, last, i = [], (0, 0, 0, 255), 0

    def h(p):
        return (p[0] * 3 + p[1] * 5 + p[2] * 7 + p[3] * 11) % 64

    while i < len(data):
        op = data[i]
        i += 1
        if op == 0xFE:
            px = (data[i], data[i + 1], data[i + 2], last[3])
            i += 3
        elif op == 0xFF:
            px = tuple(data[i:i + 4])
            i += 4
        elif op >> 6 == 0:
            px = lut[op]
        elif op >> 6 == 1:
            px = ((last[0] + ((op >> 4) & 3) - 2) & 255, (last[1] + ((op >> 2) & 3) - 2) & 255,
                  (last[2] + (op & 3) - 2) & 255, last[3])
        elif op >> 6 == 2:
            dg = (op & 63) - 32
            b2 = data[i]
            i += 1
            px = ((last[0] + dg + (b2 >> 4) - 8) & 255, (last[1] + dg) & 255,
                  (last[2] + dg + (b2 & 15) - 8) & 255, last[3])
        else:
            for _ in range((op & 63) + 1):
                out.append(last)
            lut[h(last)] = last
            continue
        lut[h(px)] = px
        out.append(px)
        last = px
    return out


def decode_anim(blob):
    """-> (width, height, fps, [frame], [(name, first, last)]); frames are (r, g, b, a) lists."""
    (sig, _flags, width, height, color, _mm, _mp, fps, _u, sections_len, _frames_len,
     section_count, frame_count) = struct.unpack_from(HEADER, blob)
    assert sig == SIGNATURE, sig
    at = struct.calcsize(HEADER)
    sections = []
    for _ in range(section_count):
        first, last, offset = struct.unpack_from("<III", blob, at)
        end = blob.index(b"\0", at + 12)
        sections.append((blob[at + 12:end].decode(), first, last, offset))
        at = end + 1
    assert at == struct.calcsize(HEADER) + sections_len
    blk = {0: 3, 1: 1, 2: 4}[color]
    count = width * height
    frames, current, offsets = [], [(0, 0, 0, 255)] * count, []
    for _ in range(frame_count):
        offsets.append(at)
        joint, mask_bits, px_len = struct.unpack_from("<BHH", blob, at)
        at += 5
        mask_bytes = (mask_bits + 7) // 8
        mask = decode_mask(joint >> 4, blob[at:at + mask_bytes], mask_bits, count)
        at += mask_bytes
        data = blob[at:at + px_len]
        at += px_len
        mode = joint & 15
        if mode == PX_QOI:
            pixels = qoi_decode(data, None, color == 2)
        else:
            raw = rle_decompress(data, blk) if mode == PX_RLE else data
            assert color != 1, "gray4 is not read here"
            pixels = [(raw[i + 2], raw[i + 1], raw[i], raw[i + 3] if blk == 4 else 255)
                      for i in range(0, len(raw), blk)]
        assert len(pixels) == mask.count(True), (len(pixels), mask.count(True))
        it = iter(pixels)
        current = [next(it) if present else old for present, old in zip(mask, current)]
        frames.append(current)
    assert at == len(blob), (at, len(blob))
    for name, first, _last, offset in sections:
        assert offset == offsets[first], name
    return width, height, fps, frames, [(n, f, l) for n, f, l, _ in sections]


# --- .png -------------------------------------------------------------------
def png(width, height, rgba_rows):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\0" + bytes(v for px in row for v in px) for row in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# --- The mascot -------------------------------------------------------------
# The Claude Code character: a wide body, two arms, four legs, two eyes. It is
# drawn from soft-edged shapes rather than pixels, because the bar's LEDs hold
# a brightness, not just on and off: an edge that falls between two pixels
# lights both partly. That is what rounds the corners, shades the body from
# top to belly, and lets it move by less than a pixel at a time.
#
# The canvas is 24x16, the full height of the display, with room above the
# body for a hop and a pixel either side for a shake. It is placed at (0, 0).
CANVAS_W, CANVAS_H = 24, 16
FPS = 12
LOOP = 48  # frames in every looping section: 4 s
BLACK = (0, 0, 0)

# At rest every straight edge sits on a pixel boundary, so the figure is crisp
# where it is straight and soft only where it curves or moves.
BODY = (4.0, 2.0, 20.0, 12.0)  # left, top, right, bottom
BODY_RADIUS = 3.0
ARMS = ((1.0, 5.0, 5.0, 9.0), (19.0, 5.0, 23.0, 9.0))
ARM_RADIUS = 1.6
LEG_CENTRES = (6.0, 10.0, 14.0, 18.0)
LEG_HALF_WIDTH = 1.0
LEG_TOP, LEG_BOTTOM = 10.5, 15.0
LEG_RADIUS = 0.8
EYES_X = (7.0, 14.0)  # left edge of each eye
EYE_Y, EYE_SIZE = 5.0, 3.0
EYE_RADIUS = 0.9

# Light falls from above: full brightness on the crown, dimmer at the belly,
# and the legs dimmer still, so they read as behind the body. A soft shine sits
# on the upper left of the crown.
SHADE_TOP, SHADE_BELLY = 1.0, 0.55
SHADE_ARM = 0.78
SHADE_LEG_TOP, SHADE_LEG_FOOT = 0.55, 0.36
SHINE = 0.2  # white mixed into the colour at the middle of the shine
SHINE_BOX = (2.2, 0.8, 8.2, 2.6)  # relative to the body's top left corner
# The glint in each eye, as a share of white mixed over black.
GLINT = 0.55
# How far a raised arm travels, in pixels.
ARM_REACH = 3.4
# Brightness is rounded to this many steps, which keeps a still part of the
# picture exactly still from frame to frame.
LEVELS = 16

# Eyes that are not simply open: which of their 3x3 cells are dark.
EYE_SHAPES = {
    "happy": (".#.", "#.#", "..."),    # ^ : pleased
    "more":  ("#..", ".#.", "#.."),    # > : scrunched shut, the left eye
    "less":  ("..#", ".#.", "..#"),    # < : scrunched shut, the right eye
    "cross": ("#.#", ".#.", "#.#"),    # x : something went wrong
}
# The "z" that drifts up from a sleeping head, fading as it goes.
ZED = ("####", "..#.", ".#..", "####")
ZED_X, ZED_Y, ZED_RISE = 20.0, 1.0, 2.0


def hex_rgb(text):
    text = text.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


# One colour and one motion per kind of notification, each with eyes to match.
# The colours are the ones the app has always used; src/kinds.ts names the same
# sections.
KINDS = (
    # name       colour     motion    frames
    ("perm",     "#FF8800", "urge",   LOOP),   # wants permission: marching, one arm up and waving
    ("question", "#AA55FF", "wonder", LOOP),   # asked you something: one eye wide, scratching its head
    ("ask",      "#00AAFF", "wait",   LOOP),   # your turn: looking about, tapping a foot
    ("done",     "#00FF66", "cheer",  LOOP),   # finished: jumping, arms up, eyes pleased
    ("agent",    "#99FF33", "skip",   LOOP),   # a subagent finished: two small hops and a wink
    ("error",    "#FF2244", "alarm",  LOOP),   # the API said no: shaking, eyes crossed, pulsing
    ("hello",    "#D97757", "wave",   LOOP),   # setup's test notification: a wave
    ("idle",     "#D97757", "sleep",  LOOP),   # the app open with nothing to say: asleep
    ("allow",    "#00FF66", "nod",    12),     # START pressed
    ("deny",     "#FF2244", "no",     12),     # BACK pressed
)


def cover(x, y, box, radius):
    """How much of the pixel centred on (x, y) a rounded box covers, 0..1."""
    left, top, right, bottom = box
    if x < left - 1 or x > right + 1 or y < top - 1 or y > bottom + 1:
        return 0.0
    radius = min(radius, (right - left) / 2, (bottom - top) / 2)
    qx = abs(x - (left + right) / 2) - ((right - left) / 2 - radius)
    qy = abs(y - (top + bottom) / 2) - ((bottom - top) / 2 - radius)
    distance = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius
    return min(1.0, max(0.0, 0.5 - distance))


def ease(i, start, length):
    """0 before `start`, 1 after `start + length`, a smooth step in between."""
    t = min(1.0, max(0.0, (i - start) / length))
    return t * t * (3 - 2 * t)


def blink(i):
    """How open the eyes are on frame i of a loop: shut for a moment once per loop,
    and never on arrival."""
    return {26: 0.55, 27: 0.12, 28: 0.12, 29: 0.55}.get(i % LOOP, 1.0)


def swing(i, period):
    """A smooth 0..1..0 with the given period, starting at 0."""
    return (1 - math.cos(2 * math.pi * i / period)) / 2


def pose(motion, i):
    """Everything that moves, for frame i of a motion. Distances are in pixels
    and need not be whole ones; `arms` is how far up each is, 0..1."""
    p = {"dx": 0.0, "dy": 0.0, "squash": 0.0, "lift": (0.0,) * 4, "gaze": 0.0, "gaze_y": 0.0,
         "eyes": ("open", "open"), "open": 1.0, "arms": (0.0, 0.0), "glow": 1.0, "zed": -1.0}

    def march(rate=8, height=1.4):
        # Two legs at a time, a diagonal pair, each step a smooth arc; the body
        # rides up a little on every step.
        phase = i % rate
        half = rate // 2
        arc = math.sin(math.pi * (phase % half) / half)
        pair = (0, 2) if phase < half else (1, 3)
        p["lift"] = tuple(height * arc if n in pair else 0.0 for n in range(4))
        p["dy"] = -0.45 * arc

    if motion == "urge":
        march()
        p["arms"] = (0.0, 0.7 + 0.3 * math.sin(2 * math.pi * i / 6))
        p["eyes"] = ("wide", "wide")
        p["open"] = blink(i)
    elif motion == "wonder":
        # Leaning one way and then the other, looking up, a hand at its head.
        p["dx"] = 0.7 * math.sin(2 * math.pi * i / LOOP)
        p["gaze_y"] = -0.5
        p["eyes"] = ("open", "wide") if i < LOOP // 2 else ("wide", "open")
        p["arms"] = (0.55 + 0.2 * swing(i, 12), 0.0)
        p["squash"] = 0.2 * swing(i, 24)
        p["open"] = blink(i + 4)
    elif motion == "wait":
        # Looking for you, left and right, and tapping a foot in between.
        p["gaze"] = -ease(i, 6, 3) + ease(i, 18, 3) + ease(i, 30, 3) - ease(i, 42, 3)
        tap = i % 12
        p["lift"] = (0.0, 0.0, 0.0, 1.2 if tap in (1, 4) else 0.5 if tap in (0, 2, 3, 5) else 0.0)
        p["squash"] = 0.2 * swing(i, 24)
        p["open"] = blink(i + 4)
    elif motion == "cheer":
        # Crouch, jump with both arms up, land, settle; then stand a while.
        phase = i % 24
        height = (0.0, 0.0, 0.9, 1.6, 2.0, 1.6, 0.9, 0.0, 0.0, 0.0)
        crouch = (0.5, 0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.8, 0.4, 0.15)
        if phase < len(height):
            p["dy"] = -height[phase]
            p["squash"] = crouch[phase]
            p["lift"] = (min(1.3, height[phase]),) * 4
            up = min(1.0, height[phase] / 1.2)
            p["arms"] = (up, up)
        p["eyes"] = ("happy", "happy") if phase < 14 else ("open", "open")
        p["open"] = blink(i + 10)
    elif motion == "skip":
        # Two small hops, a wink, a rest.
        phase = i % 24
        hop = (0.0, 0.6, 1.0, 0.6, 0.0, 0.0, 0.6, 1.0, 0.6, 0.0)
        if phase < len(hop):
            p["dy"] = -hop[phase]
            p["lift"] = (min(1.0, hop[phase]),) * 4
        p["eyes"] = ("open", "happy") if 10 <= phase < 18 else ("open", "open")
        p["open"] = blink(i + 2)
    elif motion == "alarm":
        phase = i % 24
        # A whole pixel each way: anything less would smear the legs instead of moving them.
        p["dx"] = float((0, 1, 0, -1)[phase % 4]) if phase < 8 else 0.0
        p["eyes"] = ("cross", "cross")
        p["glow"] = 0.7 + 0.3 * (1 - swing(i, 12))
    elif motion == "wave":
        # Standing, one arm going up and waving, the way you say hello.
        p["arms"] = (0.0, ease(i, 2, 4) * (0.75 + 0.25 * math.sin(2 * math.pi * i / 6)) * (1 - ease(i, 40, 4)))
        p["squash"] = 0.2 * swing(i, 24)
        p["open"] = blink(i)
    elif motion == "sleep":
        # Eyes shut, one slow breath per loop, dimmed, and a z drifting up.
        p["squash"] = 0.9 * swing(i, LOOP)
        p["open"] = 0.0
        p["glow"] = 0.62 + 0.1 * (1 - swing(i, LOOP))
        p["zed"] = (i % 24) / 24
    elif motion == "nod":
        dip = (0.0, 0.7, 1.2, 0.7, 0.0, -0.5, -0.2, 0.0)
        p["squash"] = max(0.0, dip[i]) if i < len(dip) else 0.0
        p["dy"] = min(0.0, dip[i]) if i < len(dip) else 0.0
        p["eyes"] = ("happy", "happy")
    elif motion == "no":
        p["dx"] = float((0, 1, 0, -1)[i % 4]) if i < 8 else 0.0
        p["eyes"] = ("more", "less")
    else:
        raise ValueError(motion)
    return p


_light_cache = {}


def cells(shape, left, top):
    """The dark cells of an eye shape, as one-pixel boxes."""
    return [(left + cx, top + cy, left + cx + 1, top + cy + 1)
            for cy, row in enumerate(shape) for cx, ch in enumerate(row) if ch == "#"]


def mascot_light(motion, i):
    """Frame i of a motion as brightness alone: [(body, white)] per pixel, each
    0..1. `body` scales the kind's colour; `white` is mixed in on top of it."""
    p = pose(motion, i)
    key = tuple(round(v, 3) if isinstance(v, float) else v for v in
                (p["dx"], p["dy"], p["squash"], *p["lift"], p["gaze"], p["gaze_y"], *p["eyes"],
                 p["open"], *p["arms"], p["glow"], p["zed"]))
    if key in _light_cache:
        return _light_cache[key]

    dx, dy, squash = p["dx"], p["dy"], p["squash"]
    # A squash lowers the crown and leaves the feet where they are.
    body = (BODY[0] + dx, BODY[1] + dy + squash, BODY[2] + dx, BODY[3] + dy)
    arms = [(l + dx, t + dy + squash * 0.6 - ARM_REACH * up, r + dx, b + dy + squash * 0.3 - ARM_REACH * up)
            for (l, t, r, b), up in zip(ARMS, p["arms"])]
    # The feet move in whole pixels only. A lean of less than one leaves them planted, which is
    # what a lean is; a fraction of a pixel would only blur four thin legs into a comb.
    step = float(int(dx))
    legs = [(c - LEG_HALF_WIDTH + step, LEG_TOP + dy, c + LEG_HALF_WIDTH + step, LEG_BOTTOM + dy - lift)
            for c, lift in zip(LEG_CENTRES, p["lift"])]
    shine = (body[0] + SHINE_BOX[0], body[1] + SHINE_BOX[1], body[0] + SHINE_BOX[2], body[1] + SHINE_BOX[3])

    # Each eye is some dark boxes and, when it is open, a glint.
    eye_top = EYE_Y + dy + squash * 0.8 + p["gaze_y"]
    holes, glints = [], []
    for ex, style in zip(EYES_X, p["eyes"]):
        left = ex + dx + p["gaze"]
        if style in EYE_SHAPES:
            holes += [(box, 0.25) for box in cells(EYE_SHAPES[style], left, eye_top)]
            continue
        tall = EYE_SIZE + (1.0 if style == "wide" else 0.0)
        # A closing eye shuts downwards, onto its lower lid.
        height = max(0.9, tall * p["open"])
        bottom = eye_top + EYE_SIZE
        holes.append(((left, bottom - height, left + EYE_SIZE, bottom), EYE_RADIUS))
        if p["open"] > 0.9:
            glints.append((left + 0.1, bottom - height + 0.1, left + 1.1, bottom - height + 1.1))

    zed = []
    if p["zed"] >= 0:
        zed = cells(ZED, ZED_X, ZED_Y - ZED_RISE * p["zed"])
        zed_light = 0.75 * math.sin(math.pi * p["zed"])

    out = []
    for py in range(CANVAS_H):
        for px in range(CANVAS_W):
            x, y = px + 0.5, py + 0.5
            # Legs first, then the arms and the body over them: the brightest wins.
            light = 0.0
            for leg in legs:
                c = cover(x, y, leg, LEG_RADIUS)
                if c:
                    t = min(1.0, max(0.0, (y - LEG_TOP - dy) / (LEG_BOTTOM - LEG_TOP)))
                    light = max(light, c * (SHADE_LEG_TOP + (SHADE_LEG_FOOT - SHADE_LEG_TOP) * t))
            for arm in arms:
                light = max(light, cover(x, y, arm, ARM_RADIUS) * SHADE_ARM)
            white = 0.0
            c = cover(x, y, body, BODY_RADIUS)
            if c:
                t = min(1.0, max(0.0, (y - body[1]) / (body[3] - body[1])))
                light = max(light, c * (SHADE_TOP + (SHADE_BELLY - SHADE_TOP) * t))
                white = SHINE * c * cover(x, y, shine, 0.9)

            for box, radius in holes:
                dark = cover(x, y, box, radius)
                light *= 1 - dark
                white *= 1 - dark
            for box in glints:
                white = max(white, GLINT * cover(x, y, box, 0.3))
            light *= p["glow"]
            white *= p["glow"]
            for box in zed:
                light = max(light, zed_light * cover(x, y, box, 0.1))
            out.append((round(light * LEVELS) / LEVELS, round(white * LEVELS) / LEVELS))
    _light_cache[key] = out
    return out


def mascot_frame(color, motion, i):
    return [tuple(min(255, round(channel * light + 255 * white)) for channel in color)
            for light, white in mascot_light(motion, i)]


def mascot_anim():
    frames, sections = [], []
    for name, color, motion, count in KINDS:
        first = len(frames)
        frames += [mascot_frame(hex_rgb(color), motion, i) for i in range(count)]
        sections.append((name, first, len(frames) - 1))
    return encode_anim(CANVAS_W, CANVAS_H, FPS, frames, sections), frames, sections


# --- The fuse: how long a prompt can still be answered -----------------------
# A one-pixel bar under the text that burns down from the right, with a bright
# spark at the burning end. Section `tN` starts with N seconds left, so a prompt
# that reaches the bar late, or comes back to it, starts where its time stands
# and is still one draw request. There is one fuse for each "Answer time" the
# app's Setup screen offers: (seconds, frames a second, seconds a section).
#
# The short fuses run at four frames a second, as smooth as 47 pixels get. The
# long ones would need thousands of frames at that rate to show motion nobody
# can see -- over ten minutes a pixel lasts thirteen seconds -- so they tick
# once a second and carry a section every few seconds instead of every one.
# The app rounds the time left up to the nearest section the file has.
FUSE_W, FUSE_H = 47, 1
FUSES = {"fuse15": (15, 4, 1), "fuse30": (30, 4, 1), "fuse60": (60, 4, 1),
         "fuse300": (300, 1, 5), "fuse600": (600, 1, 10)}
FUSE_CALM, FUSE_URGENT = hex_rgb("#FF8800"), hex_rgb("#FF2244")
FUSE_URGENT_SECONDS = 5
FUSE_BODY = 0.6  # the unburnt part, dimmer than the spark at its end


def fuse_anim(seconds, fps, step):
    total = seconds * fps
    frames = []
    for i in range(total + 1):
        left = (total - i) / fps
        length = FUSE_W * (total - i) / total  # in pixels, and rarely a whole number
        color = FUSE_URGENT if left <= FUSE_URGENT_SECONDS else FUSE_CALM
        row = []
        for x in range(FUSE_W):
            lit = min(1.0, max(0.0, length - x))  # the last pixel is lit by the part of it that is left
            spark = lit if x >= length - 1.5 else 0.0
            light = round(max(lit * FUSE_BODY, spark) * LEVELS) / LEVELS
            row.append(tuple(round(c * light) for c in color))
        frames.append(row)
    sections = [(f"t{n}", (seconds - n) * fps, total) for n in range(step, seconds + 1, step)]
    return encode_anim(FUSE_W, FUSE_H, fps, frames, sections), frames, sections


# --- The wipe: a CRT switching off -------------------------------------------
# The picture collapses to a bright band, then to a stub in the middle, then
# nothing. Each entry is (height, half-width from centre).
SCREEN_W, SCREEN_H = 72, 16
WIPE_STEPS = ((3, 36), (1, 36), (1, 5), (0, 0))


def wipe_anim():
    frames = []
    for height, half in WIPE_STEPS:
        top = (SCREEN_H - height) // 2
        frames.append([
            (255, 255, 255) if top <= y < top + height and abs(x - SCREEN_W // 2 + 0.5) < half else BLACK
            for y in range(SCREEN_H) for x in range(SCREEN_W)])
    return encode_anim(SCREEN_W, SCREEN_H, FPS, frames, []), frames, []


# --- Sound ------------------------------------------------------------------
# Short melodies rather than one pitch: a single beep on a small speaker is
# hard to tell from another single beep. Rising asks, falling reports, a
# descending run reads as trouble. Nothing below ~580 Hz.
SAMPLE_RATE = 44100
TONES = {
    "perm": (740, 988),
    "question": (880, 1109, 1319),
    "ask": (698, 880),
    "done": (988, 740),
    "agent": (1319, 1047, 1319),
    "error": (880, 740, 587),
}
NOTE_SECONDS = 0.22
ATTACK_SECONDS = 0.005  # a waveform that starts mid-cycle clicks
RELEASE_SECONDS = 0.012
DECAY_SECONDS = 0.08  # each note's exponential fade: struck, not held
# Loudest sample as a fraction of full scale. With the bell envelope this is
# about -15 dBFS RMS, an eighth of the power of a held sine at the same peak.
# The bar's own volume setting scales it from there.
SOUND_PEAK = 0.6


def tone(kind):
    per_note = int(NOTE_SECONDS * SAMPLE_RATE)
    attack = int(ATTACK_SECONDS * SAMPLE_RATE)
    release = int(RELEASE_SECONDS * SAMPLE_RATE)
    out = bytearray()
    for freq in TONES[kind]:
        for i in range(per_note):
            t = i / SAMPLE_RATE
            envelope = min(1.0, i / attack, (per_note - 1 - i) / release) * math.exp(-t / DECAY_SECONDS)
            out += struct.pack("<h", round(32767 * SOUND_PEAK * envelope * math.sin(2 * math.pi * freq * t)))
    return bytes(out)


# --- Icons ------------------------------------------------------------------
# The mascot again, at the two sizes the bar's menus want. '#' is full
# brightness, '+' and '-' the two shades below it.
CLAY = hex_rgb("#D97757")
ICON_SHADES = {"#": 1.0, "+": 0.7, "-": 0.45}
ICON_FRONT = (
    "........",
    ".+####+.",
    ".#.##.#.",
    ".######.",
    "+######+",
    ".++++++.",
    ".-.--.-.",
    "........",
)
ICON_BACK = (
    "...........",
    "..+#####+..",
    "..#######..",
    "..#.###.#..",
    "..#.###.#..",
    ".+#######+.",
    ".+#######+.",
    "..+++++++..",
    "..+++++++..",
    "..-.-.-.-..",
    "...........",
)


def icon(rows, color):
    return png(len(rows[0]), len(rows), [
        [(*(round(c * ICON_SHADES[ch]) for c in color), 255) if ch in ICON_SHADES else (0, 0, 0, 0)
         for ch in row] for row in rows])


# --- Putting it on disk -----------------------------------------------------
def build():
    """{path relative to src/: bytes}, and the frames behind each animation."""
    files, sheets = {}, {}
    makers = [("clawd", mascot_anim), ("wipe", wipe_anim)]
    makers += [(name, lambda spec=spec: fuse_anim(*spec)) for name, spec in FUSES.items()]
    for name, make in makers:
        blob, frames, sections = make()
        width, height, _fps, decoded, _ = decode_anim(blob)
        assert [[px[:3] for px in f] for f in decoded] == [list(f) for f in frames], name
        files[f"animations/{name}.anim"] = blob
        sheets[name] = (width, height, frames, sections)
    for kind in TONES:
        files[f"sounds/{kind}.snd"] = tone(kind)
    files["appmeta/icon_front_8x8.png"] = icon(ICON_FRONT, CLAY)
    files["appmeta/icon_back_11x11.png"] = icon(ICON_BACK, (0xFF, 0xFF, 0xFF))
    return files, sheets


def sheet(sheets, scale=4, per_row=24):
    """Every frame of every animation, scaled up, for looking at."""
    blocks = []
    for width, height, frames, _ in sheets.values():
        per = max(1, min(per_row, (CANVAS_W * per_row) // width))
        for start in range(0, len(frames), per):
            row = frames[start:start + per]
            for y in range(height):
                line = []
                for frame in row:
                    line += [(*frame[y * width + x], 255) for x in range(width)] + [(40, 40, 40, 255)]
                blocks.append(line)
            blocks.append([])
    full = max(len(line) for line in blocks)
    rows = []
    for line in blocks:
        line = line + [(20, 20, 20, 255)] * (full - len(line))
        scaled = [px for px in line for _ in range(scale)]
        rows += [scaled] * scale
    return png(full * scale, len(rows), rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="compare with what is on disk; write nothing")
    parser.add_argument("--sheet", metavar="PNG", help="render every animation frame into one image")
    args = parser.parse_args()

    files, sheets = build()
    stale = []
    for rel, blob in sorted(files.items()):
        path = os.path.join(SRC, rel)
        try:
            with open(path, "rb") as fh:
                same = fh.read() == blob
        except OSError:
            same = False
        if args.check:
            if not same:
                stale.append(rel)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(blob)
        print(f"{'  same' if same else ' wrote'}  src/{rel}  {len(blob)} B")
    if args.sheet:
        with open(args.sheet, "wb") as fh:
            fh.write(sheet(sheets))
    if stale:
        print("out of date, run `python3 tools/assets.py`: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
