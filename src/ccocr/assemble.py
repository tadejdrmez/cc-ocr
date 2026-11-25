"""Build one searchable PDF/A per document.

Each page = the scan with Tesseract's invisible text page laid on top.

With ``pdf_image: original`` the page of the input file itself is reused
(PDF input) or the input image is embedded without re-encoding (TIFF/JPEG via
img2pdf, so CCITT G4 and JPEG streams pass through). The scan is never
decoded and stored again, so the output stays about the size of the input.
Pages built from a decoded image (optimised image, or input img2pdf cannot pass
through) are stored bilevel as CCITT G4 when they are black and white, else PNG.

OCRmyPDF then converts the document to PDF/A without OCRing again
(``--skip-text``). Size (``output.size``):

* ``original`` - lossless only: images kept as they are, bilevel images
  recompressed with lossless JBIG2 when the encoder is installed.
* ``compact`` - smallest file: additionally re-compresses colour/grey images
  (JPEG quality ``output.jpeg_quality``, default 75) and reduces PNG colours.
  Text and black-and-white pages stay lossless (no lossy JBIG2, spec 2.3).
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


SIZES = ("original", "compact")


@dataclass
class AssemblyPage:
    image: Image.Image
    dpi: float
    text_pdf: bytes
    # Page of the original input as a PDF (the input itself, or the input
    # converted by img2pdf without re-encoding); None = build from ``image``.
    source_pdf: Path | None = None
    source_index: int = 0
    rotation: int = 0  # degrees clockwise to turn the source page upright


def source_as_pdf(batch: Path, workdir: Path, n_pages: int) -> Path | None:
    """The input as a PDF whose pages can be reused unchanged, or None.

    PDFs are used as they are. TIFF/JPEG/PNG are wrapped by img2pdf, which
    embeds CCITT G4, JPEG and most PNG data without decoding it.
    """
    if batch.suffix.lower() == ".pdf":
        try:
            with pikepdf.open(batch) as pdf:
                return batch if len(pdf.pages) == n_pages else None
        except Exception:  # noqa: BLE001
            return None
    try:
        data = img2pdf.convert(str(batch), rotation=img2pdf.Rotation.ifvalid)
    except Exception:  # noqa: BLE001 - e.g. alpha channel, 16-bit: fall back to decoded pages
        return None
    out = workdir / f".{batch.stem}.source.pdf"
    out.write_bytes(data)
    with pikepdf.open(out) as pdf:
        if len(pdf.pages) != n_pages:
            return None
    return out


def _is_bilevel(img: Image.Image) -> bool:
    if img.mode == "1":
        return True
    if img.mode != "L":
        return False
    hist = img.histogram()
    return sum(hist[1:255]) == 0


def _image_page_pdf(img: Image.Image, dpi: float) -> bytes:
    buf = io.BytesIO()
    if _is_bilevel(img):
        img.convert("1").save(buf, format="TIFF", compression="group4", dpi=(dpi, dpi))
    else:
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        img.save(buf, format="PNG", dpi=(dpi, dpi))
    layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
    return img2pdf.convert(buf.getvalue(), layout_fun=layout)


def _box(page: pikepdf.Page) -> tuple[float, float, float, float]:
    box = page.obj.get("/CropBox") or page.mediabox
    x0, y0, x1, y1 = (float(v) for v in box)
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)


def _place_text(dest: pikepdf.Page, pdf: pikepdf.Pdf, text_page: pikepdf.Page, view_rotate: int) -> None:
    """Draw the text page over ``dest`` so it lines up with the page as displayed.

    ``view_rotate`` is the page's /Rotate (clockwise). The text page was made
    from the upright image, so it is mapped onto the unrotated page space.
    """
    x0, y0, w, h = _box(dest)
    tx0, ty0, tw, th = _box(text_page)
    vw, vh = (h, w) if view_rotate in (90, 270) else (w, h)
    sx, sy = vw / tw, vh / th
    m = {
        0: (sx, 0, 0, sy, x0, y0),
        90: (0, sx, -sy, 0, x0 + w, y0),
        180: (-sx, 0, 0, -sy, x0 + w, y0 + h),
        270: (0, -sx, sy, 0, x0, y0 + h),
    }[view_rotate]
    xobj = pdf.copy_foreign(text_page.as_form_xobject())
    name = dest.add_resource(xobj, pikepdf.Name.XObject, prefix="CCOCRText")
    # undo the text page's own origin, then map onto this page
    content = (
        f"q {' '.join(f'{v:.6f}' for v in m)} cm 1 0 0 1 {-tx0:.6f} {-ty0:.6f} cm {name} Do Q\n"
    ).encode()
    dest.contents_add(pdf.make_stream(content), prepend=False)


def build_pdf(pages: list[AssemblyPage], out_path: Path, *, title: str, pdfa: str = "pdfa-2", language: str = "",
              size: str = "original", jpeg_quality: int = 75) -> None:
    if size not in SIZES:
        raise ValueError(f"output.size must be one of {', '.join(SIZES)}")
    combined = pikepdf.new()
    sources: dict[Path, pikepdf.Pdf] = {}
    try:
        for p in pages:
            txt_pdf = pikepdf.open(io.BytesIO(p.text_pdf))
            if p.source_pdf is not None:
                src = sources.get(p.source_pdf) or sources.setdefault(p.source_pdf, pikepdf.open(p.source_pdf))
                combined.pages.append(src.pages[p.source_index])
                dest = combined.pages[-1]
                rot = (int(dest.obj.get("/Rotate", 0)) + p.rotation) % 360
                if rot:
                    dest.obj.Rotate = rot
                elif "/Rotate" in dest.obj:
                    del dest.obj["/Rotate"]
                for k in ("/Annots", "/B", "/StructParents"):  # links/annotations of the scan are not kept
                    if k in dest.obj:
                        del dest.obj[k]
                _place_text(dest, combined, txt_pdf.pages[0], rot)
            else:
                img_pdf = pikepdf.open(io.BytesIO(_image_page_pdf(p.image, p.dpi)))
                combined.pages.append(img_pdf.pages[0])
                dest = combined.pages[-1]
                _place_text(dest, combined, txt_pdf.pages[0], 0)
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw.pdf"
            combined.save(raw)
            _ocrmypdf(raw, out_path, title=title, pdfa=pdfa, size=size, jpeg_quality=jpeg_quality)
    finally:
        for s in sources.values():
            s.close()


def _ocrmypdf(raw: Path, out_path: Path, *, title: str, pdfa: str, size: str, jpeg_quality: int) -> None:
    cmd = [
        "ocrmypdf",
        "--skip-text",
        "--output-type", pdfa,
        "--pdfa-image-compression", "auto",
        "--title", title,
        "--quiet",
    ]
    if size == "compact":
        cmd += ["--optimize", "2", "--jpeg-quality", str(int(jpeg_quality)), "--png-quality", "70"]
    else:
        cmd += ["--optimize", "1"]
    cmd += [str(raw), str(out_path)]
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
