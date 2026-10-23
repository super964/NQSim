"""生成应用图标 icon.ico(纯标准库,无需 PIL):深色底 + 绿红K线 + 均线。"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def png_bytes(size: int, pixels: list) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def draw(size: int) -> list:
    bg = (11, 14, 20, 255)
    up = (38, 166, 154, 255)
    dn = (239, 83, 80, 255)
    ma = (41, 98, 255, 255)
    grid = (28, 33, 43, 255)
    px = [[bg for _ in range(size)] for _ in range(size)]

    def rect(x0, y0, x1, y1, color):
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                px[y][x] = color

    def line(x0, y0, x1, y1, color, w=1):
        steps = max(abs(x1 - x0), abs(y1 - y0), 1) * 3
        for i in range(steps + 1):
            x = int(x0 + (x1 - x0) * i / steps)
            y = int(y0 + (y1 - y0) * i / steps)
            rect(x - w // 2, y - w // 2, x + w // 2 + 1, y + w // 2 + 1, color)

    s = size
    for i in range(1, 4):
        y = int(s * i / 4)
        rect(0, y, s, y + 1, grid)

    # 均线
    line(int(s * 0.12), int(s * 0.62), int(s * 0.42), int(s * 0.40), ma, max(1, s // 32))
    line(int(s * 0.42), int(s * 0.40), int(s * 0.88), int(s * 0.30), ma, max(1, s // 32))

    # 绿K(涨)
    x = int(s * 0.30)
    w = max(2, s // 8)
    rect(x, int(s * 0.22), x + w, int(s * 0.62), up)
    rect(x + w // 3, int(s * 0.12), x + w - w // 3, int(s * 0.72), up)
    # 红K(跌)
    x = int(s * 0.58)
    rect(x, int(s * 0.36), x + w, int(s * 0.76), dn)
    rect(x + w // 3, int(s * 0.28), x + w - w // 3, int(s * 0.86), dn)

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r, g, b, a = px[y][x]
            row += bytes((r, g, b, a))
        rows.append(row)
    return rows


def main():
    sizes = (16, 32, 48, 64, 128, 256)
    images = [(s, png_bytes(s, draw(s))) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    data = b""
    for s, blob in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    out = Path(__file__).resolve().parent / "icon.ico"
    out.write_bytes(header + entries + data)
    print(f"已生成 {out} ({out.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
