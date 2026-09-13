"""
tools/generate_icon.py — генерирует assets/icon.png и assets/icon.ico.

Логотип: тёмно-синяя скруглённая плашка (в тон акценту приложения #5B8DEF)
с узлом-графом — три соединённых узла + центральная точка анализа,
метафора «сеть + ИИ». Рисуется в высоком разрешении и уменьшается, чтобы
силуэт читался даже на 16x16 в панели задач.

Запуск (нужен Pillow, не входит в runtime-requirements.txt):
    pip install pillow
    python tools/generate_icon.py
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
ACCENT = (91, 141, 239, 255)         # #5B8DEF
ACCENT_LIGHT = (143, 179, 255, 255)  # #8FB3FF
BG_TOP = (36, 64, 111, 255)          # #24406F
BG_BOTTOM = (8, 11, 20, 255)         # #080B14
WHITE = (240, 243, 250, 255)


def rounded_square_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def make_base():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    for y in range(SIZE):
        t = y / SIZE
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        ImageDraw.Draw(grad).line([(0, y), (SIZE, y)], fill=(r, g, b, 255))

    radius = int(SIZE * 0.22)
    mask = rounded_square_mask(SIZE, radius)
    img.paste(grad, (0, 0), mask)

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [-SIZE * 0.35, -SIZE * 0.35, SIZE * 0.75, SIZE * 0.75], fill=(*ACCENT[:3], 140),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(SIZE * 0.12))
    glow_masked = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_masked.paste(glow, (0, 0), mask)
    img = Image.alpha_composite(img, glow_masked)

    border = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bw = max(2, int(SIZE * 0.008))
    ImageDraw.Draw(border).rounded_rectangle(
        [bw, bw, SIZE - 1 - bw, SIZE - 1 - bw], radius=radius - bw,
        outline=(255, 255, 255, 40), width=bw,
    )
    img = Image.alpha_composite(img, border)
    return img, mask


def draw_network_glyph(img):
    d = ImageDraw.Draw(img, "RGBA")
    cx, cy = SIZE / 2, SIZE / 2 - SIZE * 0.02
    r = SIZE * 0.30

    angles = [-90, 150, 30]
    pts = [
        (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
        for a in angles
    ]

    line_w = int(SIZE * 0.028)
    for i in range(3):
        d.line([pts[i], pts[(i + 1) % 3]], fill=(*ACCENT_LIGHT[:3], 230), width=line_w)
    for p in pts:
        d.line([(cx, cy), p], fill=(*WHITE[:3], 160), width=int(line_w * 0.6))

    node_r = SIZE * 0.075
    for p in pts:
        d.ellipse([p[0] - node_r, p[1] - node_r, p[0] + node_r, p[1] + node_r], fill=WHITE)
        d.ellipse(
            [p[0] - node_r, p[1] - node_r, p[0] + node_r, p[1] + node_r],
            outline=(*ACCENT[:3], 255), width=int(SIZE * 0.012),
        )

    center_r = SIZE * 0.095
    d.ellipse([cx - center_r, cy - center_r, cx + center_r, cy + center_r], fill=ACCENT_LIGHT)
    d.ellipse(
        [cx - center_r, cy - center_r, cx + center_r, cy + center_r],
        outline=WHITE, width=int(SIZE * 0.014),
    )
    return img


def main():
    img, mask = make_base()
    img = draw_network_glyph(img)
    img.putalpha(Image.composite(img.split()[3], Image.new("L", img.size, 0), mask))

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_png = os.path.join(root, "assets", "icon.png")
    out_ico = os.path.join(root, "assets", "icon.ico")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)

    img.save(out_png)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    img.save(out_ico, sizes=[(s, s) for s in sizes])
    print("Saved:", out_png, out_ico)


if __name__ == "__main__":
    main()
