#!/usr/bin/env python3
"""The pictures in the README, from a real bar.

    python3 tools/shots.py --capture --usage 41 6   # put each screen on the bar and read it back
    python3 tools/shots.py                          # redraw docs/*.png and docs/hero.gif from docs/raw/

`--capture` shows each screen on the bar the way a hook would, silently, and
reads the front display back through `GET /api/screen`: what is saved in
docs/raw/ is what the bar's 72x16 pixels held. The README's pictures are those
captures drawn as a grid of LEDs.

The animation is the one thing that is put together: the mascot and the fuse
are the frames of the app's own .anim files (the bar plays exactly these), laid
over a capture for the text. Reading the display back fast enough to film it
would be the kind of traffic this project exists to avoid.

Standard library only.
"""
import argparse
import base64
import json
import os
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import assets  # noqa: E402
import busyclaude as bc  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
RAW = os.path.join(DOCS, "raw")
WIDTH, HEIGHT = 72, 16

# What each screen is made of: an item for the inbox, and when to look.
PROJECT = "api-server"
SCENES = (
    # name        item                                                              seconds after launch
    ("prompt",   {"kind": "perm", "text": "$ git push", "ask": True, "window": 15}, 2.5),
    ("question", {"kind": "question", "text": "a question", "ask": True, "options": 4, "window": 30}, 2.5),
    ("finished", {"kind": "done", "text": "finished"}, 2.0),
    ("usage",    None, 5.4),   # the same launch as `finished`, once its text has given way
    ("error",    {"kind": "error", "text": "rate limit"}, 2.0),
    ("idle",     "opened by hand", 2.5),
)
HISTORY = [{"kind": "done", "title": PROJECT, "text": "finished"},
           {"kind": "perm", "title": PROJECT, "text": "$ git push"}]


# --- reading and writing pictures ---------------------------------------------
def read_png(path):
    """A PNG written by assets.png: 8-bit RGBA, no filtering."""
    with open(path, "rb") as fh:
        blob = fh.read()
    width, height = struct.unpack(">II", blob[16:24])
    data, at = b"", 8
    while at < len(blob):
        length, kind = struct.unpack(">I4s", blob[at:at + 8])
        if kind == b"IDAT":
            data += blob[at + 8:at + 8 + length]
        at += 12 + length
    raw = zlib.decompress(data)
    stride = width * 4 + 1
    return [[tuple(raw[y * stride + 1 + x * 4:y * stride + 4 + x * 4]) for x in range(width)]
            for y in range(height)]


def write_png(path, rows):
    with open(path, "wb") as fh:
        fh.write(assets.png(len(rows[0]), len(rows), [[(*px, 255) for px in row] for row in rows]))


CELL, DOT = 8, 6
BACKDROP, UNLIT = (9, 9, 11), (24, 24, 27)


def leds(rows):
    """A 72x16 picture as the matrix of LEDs it is shown on: one dot per pixel."""
    pad = (CELL - DOT) // 2
    out = []
    for row in rows:
        for cy in range(CELL):
            line = []
            for px in row:
                colour = px if px != (0, 0, 0) else UNLIT
                for cx in range(CELL):
                    inside = pad <= cx < pad + DOT and pad <= cy < pad + DOT
                    corner = (cx in (pad, pad + DOT - 1)) and (cy in (pad, pad + DOT - 1))
                    line.append(colour if inside and not corner else BACKDROP)
            out.append(line)
    return out


def lzw(indices, bits):
    """GIF's flavour of LZW: variable-width codes, least significant bit first."""
    clear, stop = 1 << bits, (1 << bits) + 1
    out, acc, n = bytearray(), 0, 0
    width = bits + 1
    table, following = {}, stop + 1

    def emit(code):
        nonlocal acc, n
        acc |= code << n
        n += width
        while n >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            n -= 8

    emit(clear)
    prefix = indices[0]
    for index in indices[1:]:
        key = (prefix, index)
        if key in table:
            prefix = table[key]
            continue
        emit(prefix)
        if following < 4096:
            table[key] = following
            following += 1
            if following > (1 << width) and width < 12:
                width += 1
        else:
            emit(clear)
            table, following, width = {}, stop + 1, bits + 1
        prefix = index
    emit(prefix)
    emit(stop)
    if n:
        out.append(acc & 0xFF)
    return bytes(out)


def write_gif(path, frames, centiseconds):
    """An endlessly looping GIF; every frame brings its own palette."""
    width, height = len(frames[0][0]), len(frames[0])
    out = bytearray(b"GIF89a" + struct.pack("<HHBBB", width, height, 0, 0, 0))
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"
    for rows in frames:
        palette = sorted({px for row in rows for px in row})
        assert len(palette) <= 256, len(palette)
        bits = max(2, (len(palette) - 1).bit_length())
        lookup = {colour: i for i, colour in enumerate(palette)}
        table = b"".join(bytes(c) for c in palette) + b"\0" * (3 * ((1 << bits) - len(palette)))
        out += b"\x21\xF9\x04\x04" + struct.pack("<H", centiseconds) + b"\x00\x00"
        out += b"\x2C" + struct.pack("<HHHHB", 0, 0, width, height, 0x80 | (bits - 1)) + table
        data = lzw([lookup[px] for row in rows for px in row], bits)
        out += bytes([bits]) + b"".join(
            bytes([len(data[i:i + 255])]) + data[i:i + 255] for i in range(0, len(data), 255)) + b"\0"
    out += b"\x3B"
    with open(path, "wb") as fh:
        fh.write(out)


# --- the bar ------------------------------------------------------------------
def screen(bar):
    data = base64.b64decode(bar.call("GET", "/api/screen", {"display": 0}))
    assert len(data) == WIDTH * HEIGHT * 3, len(data)
    # The bar sends blue, green, red.
    return [[(data[(y * WIDTH + x) * 3 + 2], data[(y * WIDTH + x) * 3 + 1], data[(y * WIDTH + x) * 3])
             for x in range(WIDTH)] for y in range(HEIGHT)]


def capture(usage):
    bar = bc.Bar()
    if not bar.reachable():
        raise SystemExit(bc.explain_unreachable(bar, "nothing answers there"))
    os.makedirs(RAW, exist_ok=True)
    origin = bar.origin()
    numbers = {"five": usage[0], "week": usage[1], "at": time.time()} if usage else None
    launched = 0.0
    for name, item, when in SCENES:
        if item is not None:
            box = bar.read_box()
            doc = {"v": bc.INBOX_VERSION, "n": bc.nonce(), "fresh": "", "items": [], "history": HISTORY}
            if numbers:
                doc["usage"] = numbers
            if isinstance(item, dict):
                doc["items"] = [dict({"id": bc.nonce(), "title": PROJECT, "ask": False, "options": 0,
                                      "window": 0, "sid": "shots", "ttl": item.get("window", 12),
                                      "exp": time.time() + 30}, **item)]
            else:
                box["seen"] = json.dumps({"n": doc["n"]})  # nothing new: the app's own screen
            box["inbox"] = json.dumps(doc, separators=(",", ":"))
            bar.write_box(box)  # nothing in it is `fresh`, so nothing chimes
            bar.launch()
            launched = time.time()
            bc.write_json(bc.STATE_FILE, {"busy_until": time.time() + 30, "up": True, "origin": origin})
        time.sleep(max(0.0, launched + when - time.time()))
        rows = screen(bar)
        write_png(os.path.join(RAW, f"{name}.png"), rows)
        print(f"captured {name}: {sum(px != (0, 0, 0) for row in rows for px in row)} pixels lit")
    print(bar.restore())


# --- the README's pictures ----------------------------------------------------
def animate(name, kind, count, fuse=None):
    """`count` frames of a scene: the capture's text, the mascot's own frames, the fuse's."""
    still = read_png(os.path.join(RAW, f"{name}.png"))
    colour, motion = next((assets.hex_rgb(c), m) for k, c, m, _ in assets.KINDS if k == kind)
    spec = assets.FUSES[f"fuse{fuse}"] if fuse else None
    burning = assets.fuse_anim(*spec)[1] if spec else None
    frames = []
    for i in range(count):
        rows = [list(row) for row in still]
        mascot = assets.mascot_frame(colour, motion, i % assets.LOOP)
        for y in range(assets.CANVAS_H):
            rows[y][:assets.CANVAS_W] = mascot[y * assets.CANVAS_W:(y + 1) * assets.CANVAS_W]
        if burning:
            # The fuse runs at its own rate (four frames a second, for the short ones) under a mascot at twelve.
            fps = spec[1]
            rows[HEIGHT - 1][25:25 + assets.FUSE_W] = burning[2 * fps + i * fps // assets.FPS]
        frames.append(leds(rows))
    return frames


def render():
    for name, _, _ in SCENES:
        write_png(os.path.join(DOCS, f"{name}.png"), leds(read_png(os.path.join(RAW, f"{name}.png"))))
    frames = (animate("prompt", "perm", 42, fuse=15) + animate("finished", "done", 30)
              + animate("usage", "done", 24) + animate("question", "question", 36, fuse=30))
    write_gif(os.path.join(DOCS, "hero.gif"), frames, round(100 / assets.FPS))
    # Every kind of notification side by side: the mascot alone, mid-motion.
    picks = (("perm", 5), ("question", 6), ("ask", 10), ("done", 4), ("agent", 13), ("error", 1), ("hello", 8), ("idle", 14))
    strip = [[] for _ in range(assets.CANVAS_H)]
    for kind, i in picks:
        colour, motion = next((assets.hex_rgb(c), m) for k, c, m, _ in assets.KINDS if k == kind)
        frame = assets.mascot_frame(colour, motion, i)
        for y in range(assets.CANVAS_H):
            strip[y] += frame[y * assets.CANVAS_W:(y + 1) * assets.CANVAS_W] + [(0, 0, 0)]
    write_png(os.path.join(DOCS, "mascot.png"), leds(strip))
    for name in sorted(os.listdir(DOCS)):
        path = os.path.join(DOCS, name)
        if os.path.isfile(path):
            print(f"docs/{name}  {os.path.getsize(path) // 1024 or 1} KB")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", action="store_true", help="show each screen on the bar and read it back first")
    parser.add_argument("--usage", nargs=2, type=float, metavar=("FIVE", "WEEK"),
                        help="the usage numbers to put on the usage and idle screens")
    args = parser.parse_args()
    if args.capture:
        capture(args.usage)
    render()
    return 0


if __name__ == "__main__":
    sys.exit(main())
