"""Build one searchable PDF/A per document.

Each page = the configured image (original scan by default, embedded
losslessly) with Tesseract's invisible text page laid on top. OCRmyPDF then
converts the document to PDF/A without OCRing again (``--skip-text``) and with
lossless image compression only (no lossy JBIG2, spec 2.3).
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import img2pdf
import pikepdf
from PIL import Image


@dataclass
class AssemblyPage:
    image: Image.Image
    dpi: float
    text_pdf: bytes


def _image_page_pdf(img: Image.Image, dpi: float) -> bytes:
    buf = io.BytesIO()
    mode = img.mode
    if mode not in ("L", "RGB", "1"):
        img = img.convert("RGB")
    img.save(buf, format="PNG", dpi=(dpi, dpi))
    layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
    return img2pdf.convert(buf.getvalue(), layout_fun=layout)


def build_pdf(pages: list[AssemblyPage], out_path: Path, *, title: str, pdfa: str = "pdfa-2", language: str = "") -> None:
    combined = pikepdf.new()
    for p in pages:
        img_pdf = pikepdf.open(io.BytesIO(_image_page_pdf(p.image, p.dpi)))
        txt_pdf = pikepdf.open(io.BytesIO(p.text_pdf))
        combined.pages.append(img_pdf.pages[0])
        dest = combined.pages[-1]
        dest.add_overlay(txt_pdf.pages[0], pikepdf.Rectangle(*dest.mediabox))
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw.pdf"
        combined.save(raw)
        cmd = [
            "ocrmypdf",
            "--skip-text",
            "--output-type", pdfa,
            "--pdfa-image-compression", "lossless",
            "--optimize", "0",
            "--title", title,
            "--quiet",
            str(raw),
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        # Exit code 0 = ok, 10 = PDF/A conversion produced a file but could not
        # be confirmed as valid; report both.
        if res.returncode not in (0, 10):
            raise RuntimeError(f"ocrmypdf failed ({res.returncode}): {res.stderr.strip()[-800:]}")
        if res.returncode == 10:
            raise PdfaWarning(res.stderr.strip()[-400:])


class PdfaWarning(RuntimeWarning):
    """Output was written but OCRmyPDF could not confirm PDF/A validity."""


def ocrmypdf_version() -> str:
    res = subprocess.run(["ocrmypdf", "--version"], capture_output=True, text=True)
    return res.stdout.strip()
