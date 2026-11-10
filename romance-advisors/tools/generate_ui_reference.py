"""Build the public synthetic overlay from an empty RGBA canvas (stdlib only).

No screenshot, account data, external image, font or metadata is used as input.
Geometry follows the workbench's top/activity/comments/bottom occlusion regions.
Run from any directory: python3 tools/generate_ui_reference.py
"""
from pathlib import Path
import struct
import zlib

WIDTH, HEIGHT = 851, 1848


def build():
    pixels = bytearray(WIDTH * HEIGHT * 4)

    def box(x, y, w, h, color, radius=0):
        for row in range(max(0, y), min(HEIGHT, y + h)):
            for col in range(max(0, x), min(WIDTH, x + w)):
                # Rounded corners are computed from geometry, never image input.
                dx = max(x + radius - col, 0, col - (x + w - radius - 1))
                dy = max(y + radius - row, 0, row - (y + h - radius - 1))
                if dx * dx + dy * dy <= radius * radius:
                    i = (row * WIDTH + col) * 4
                    pixels[i:i + 4] = bytes(color)

    dark = (35, 31, 35, 150)
    light = (255, 249, 241, 210)
    muted = (255, 249, 241, 115)
    accent = (244, 172, 154, 220)
    # Top account placeholder: generic round silhouette and two text skeletons.
    box(24, 70, 355, 88, dark, 44)
    box(39, 84, 59, 59, muted, 29)
    box(115, 92, 165, 13, light, 6)
    box(115, 121, 112, 9, muted, 4)
    box(294, 91, 64, 43, accent, 20)
    for x in (615, 669, 723):
        box(x, 86, 43, 43, dark, 21)
    # A rank strip and upper-right activity card stay inside their UI regions.
    box(24, 180, 455, 50, dark, 24)
    for x, w in ((45, 72), (138, 116), (278, 169)):
        box(x, 199, w, 10, muted, 5)
    box(663, 266, 160, 129, dark, 18)
    box(703, 286, 80, 36, accent, 12)
    box(686, 343, 114, 9, light, 4)
    box(704, 367, 78, 8, muted, 4)
    # Comment skeletons contain no nicknames, avatars, user levels or messages.
    # Their region begins at 70.8% and ends before the 8.4% bottom toolbar.
    for row, width in enumerate((415, 365, 440, 330, 390)):
        y = 1322 + row * 65
        box(24, y, width, 48, dark, 15)
        box(42, y + 17, 58, 11, accent, 5)
        box(116, y + 17, width - 140, 11, muted, 5)
    box(24, 1720, 423, 68, dark, 34)
    box(52, 1748, 248, 12, muted, 6)
    for x in (480, 570, 660, 750):
        box(x, 1724, 60, 60, dark, 30)
        box(x + 19, 1743, 22, 22, light, 7)

    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))

    # Write only mandatory PNG chunks: there is no inherited or hidden metadata.
    rows = b''.join(b'\x00' + pixels[y * WIDTH * 4:(y + 1) * WIDTH * 4]
                    for y in range(HEIGHT))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', WIDTH, HEIGHT, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


if __name__ == '__main__':
    output = Path(__file__).resolve().parents[1] / 'static/assets/douyin-ui-reference-v1.png'
    output.write_bytes(build())
