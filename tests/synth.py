"""Generate synthetic scanned batches for tests and demos (no real customer data)."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ccocr.sheets import payload, sheet_image

A4 = (2480, 3508)  # 300 dpi
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

LOREM_SL = (
    "Na podlagi naročila vam zaračunavamo opravljene storitve. Plačilo izvedite na transakcijski račun "
    "do roka plačila. Za zamudo pri plačilu obračunamo zakonske zamudne obresti. Hvala za zaupanje."
)
LOREM_SR_CYR = (
    "Уговорне стране су се сагласиле да ће испорука робе бити извршена у року од тридесет дана. "
    "Купац се обавезује да плати уговорену цену на рачун продавца. Сви спорови решаваће се споразумно."
)
LOREM_SR_LAT = (
    "Ugovorne strane su saglasne da se ovaj ugovor primenjuje od dana potpisivanja. Sve izmene moraju biti "
    "u pisanom obliku. Ugovor je sačinjen u dva istovetna primerka, po jedan za svaku stranu."
)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=font) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def text_page(title: str, body: str, *, paragraphs: int = 4, footer: str = "") -> Image.Image:
    img = Image.new("L", A4, 255)
    d = ImageDraw.Draw(img)
    fb, f = ImageFont.truetype(FONT_B, 72), ImageFont.truetype(FONT, 46)
    d.text((200, 260), title, font=fb, fill=0)
    y = 480
    for _ in range(paragraphs):
        for line in _wrap(d, body, f, A4[0] - 400):
            d.text((200, y), line, font=f, fill=0)
            y += 66
        y += 50
    if footer:
        d.text((200, A4[1] - 300), footer, font=f, fill=0)
    return img


def scanned(img: Image.Image, *, skew: float = 0.0, noise: float = 0.002, seed: int = 0) -> Image.Image:
    """Make a clean page look scanned: slight skew, speckles, grey paper."""
    rnd = np.random.default_rng(seed)
    if skew:
        img = img.rotate(skew, resample=Image.Resampling.BICUBIC, fillcolor=255, expand=False)
    a = np.asarray(img, dtype=np.int16)
    a = np.clip(a * 0.92 + 12, 0, 255)  # grey paper, weaker black
    mask = rnd.random(a.shape) < noise
    a[mask] = 0
    return Image.fromarray(a.astype(np.uint8))


def blank_page(seed: int = 1) -> Image.Image:
    return scanned(Image.new("L", A4, 255), noise=0.0003, seed=seed)


def demo_batch(path: Path) -> list[int]:
    """Write a 7-page demo batch; return the expected document sizes."""
    random.seed(7)
    pages = [
        scanned(text_page("RAČUN št. 2026-001", LOREM_SL, footer="Izdajatelj: Mlekarna Šentjur d.o.o."), skew=1.2, seed=1),
        scanned(text_page("Nadaljevanje računa 2026-001", LOREM_SL, paragraphs=2), seed=2),
        scanned(text_page("RAČUN št. 2026-002", LOREM_SL, footer="Izdajatelj: Čistilnica Žalec d.o.o."), seed=3),
        blank_page(seed=4),
        sheet_image(payload("CONTRACT", "4.2.1", "demo"), "CONTRACT"),
        scanned(text_page("УГОВОР О КУПОПРОДАЈИ", LOREM_SR_CYR), seed=6),
        scanned(text_page("Ugovor, strana 2", LOREM_SR_LAT), seed=7).rotate(90, expand=True),
    ]
    pages[0].save(path, save_all=True, append_images=pages[1:], dpi=(300, 300), compression="tiff_lzw")
    return [2, 1, 2]


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-batch.tif")
    print(demo_batch(out), "->", out)
