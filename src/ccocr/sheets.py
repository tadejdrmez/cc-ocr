"""Printable QR separator sheets (spec 3.1)."""

from __future__ import annotations

import io
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

from .separators import PAYLOAD_PREFIX, PAYLOAD_VERSION

A4_300 = (2480, 3508)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def payload(doctype: str, classification: str = "", profile: str = "", **extra: str) -> str:
    parts = [f"{PAYLOAD_PREFIX}:{PAYLOAD_VERSION}", "SEP", f"DT={doctype.upper()}"]
    if classification:
        parts.append(f"CLS={classification}")
    if profile:
        parts.append(f"PROFILE={profile}")
    for k, v in extra.items():
        parts.append(f"{k.upper()}={v}")
    for p in parts:
        if ";" in p[len(p.split("=")[0]):]:
            raise ValueError("values must not contain ';'")
    return ";".join(parts)


def _font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:  # pragma: no cover
        return ImageFont.load_default()


def sheet_image(code: str, label: str) -> Image.Image:
    page = Image.new("L", A4_300, 255)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=24, border=4)
    qr.add_data(code)
    qr.make(fit=True)
    qimg = qr.make_image(fill_color="black", back_color="white").convert("L")
    page.paste(qimg, ((A4_300[0] - qimg.width) // 2, 700))
    d = ImageDraw.Draw(page)
    title = _font(FONT_BOLD, 110)
    small = _font(FONT, 48)
    d.text((A4_300[0] // 2, 360), "CC OCR SEPARATOR", font=title, fill=0, anchor="mm")
    d.text((A4_300[0] // 2, 520), label, font=_font(FONT_BOLD, 80), fill=0, anchor="mm")
    d.text((A4_300[0] // 2, 700 + qimg.height + 120), code, font=small, fill=0, anchor="mm")
    d.text((A4_300[0] // 2, 3300), "Place this sheet in front of each document. It is removed from the output.",
           font=small, fill=0, anchor="mm")
    return page


def write_sheets(path: Path, sheets: list[tuple[str, str]], copies: int = 1) -> None:
    imgs = [sheet_image(code, label) for code, label in sheets for _ in range(copies)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:], resolution=300)
    path.write_bytes(buf.getvalue())
