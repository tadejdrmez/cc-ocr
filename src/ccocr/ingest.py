"""Load a scanned batch (PDF, multi-page TIFF or single images) as page images."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageSequence

SUPPORTED = {".pdf", ".tif", ".tiff", ".png", ".jpg", ".jpeg"}


@dataclass
class SourcePage:
    index: int  # 0-based position in the batch
    image: Image.Image  # original scan, unchanged except decoding
    dpi: float
    notes: list[str] = field(default_factory=list)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def _pdf_pages(path: Path) -> list[SourcePage]:
    import pikepdf
    import pypdfium2 as pdfium

    pages: list[SourcePage] = []
    pdf = pdfium.PdfDocument(str(path))
    with pikepdf.open(path) as pk:
        for i, page in enumerate(pdf):
            w_pt, h_pt = page.get_size()
            dpi = 300.0
            # A scanned page normally holds one full-page image: render at its
            # native resolution so no detail is invented or thrown away.
            try:
                imgs = list(pk.pages[i].images.values())
                if len(imgs) == 1:
                    iw = int(imgs[0].Width)
                    dpi = max(72.0, min(600.0, iw / (w_pt / 72.0)))
            except Exception:  # pragma: no cover - defensive
                pass
            bitmap = page.render(scale=dpi / 72.0, grayscale=False)
            img = bitmap.to_pil()
            if img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            if _is_gray(img):
                img = img.convert("L")
            pages.append(SourcePage(index=i, image=img, dpi=round(dpi, 1)))
    pdf.close()
    return pages


def _is_gray(img: Image.Image) -> bool:
    if img.mode == "L":
        return True
    a = np.asarray(img.convert("RGB").resize((max(1, img.width // 8), max(1, img.height // 8))), dtype=np.int16)
    return int(np.abs(a[..., 0] - a[..., 1]).max()) < 8 and int(np.abs(a[..., 1] - a[..., 2]).max()) < 8


def _image_pages(path: Path) -> list[SourcePage]:
    pages: list[SourcePage] = []
    with Image.open(path) as im:
        for i, frame in enumerate(ImageSequence.Iterator(im)):
            dpi_info = frame.info.get("dpi") or im.info.get("dpi") or (300, 300)
            dpi = float(dpi_info[0]) if dpi_info and dpi_info[0] else 300.0
            img = frame.copy()
            if img.mode == "1":
                img = img.convert("L")
            elif img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            pages.append(SourcePage(index=i, image=img, dpi=dpi))
    return pages


def load_batch(path: Path) -> list[SourcePage]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported input type {suffix!r}")
    if suffix == ".pdf":
        return _pdf_pages(path)
    return _image_pages(path)
