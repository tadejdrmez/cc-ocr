"""Per-page orientation/script detection and OCR with Tesseract (spec 2.2).

OCRmyPDF applies one language set per run, so per-page language selection is
done here: every page gets OSD, then Tesseract reads it with the language set
of its script and writes a text-only PDF page (the invisible text layer) plus
TSV word data. The assembler later lays each text page over its image and
OCRmyPDF turns the document into PDF/A.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image


class OcrError(RuntimeError):
    pass


# Pages are OCR'd in parallel processes; Tesseract's own OpenMP threads on top
# of that oversubscribe the CPU badly (minutes instead of seconds per page).
_ENV = {**os.environ, "OMP_THREAD_LIMIT": "1"}


@dataclass
class OsdResult:
    rotate: int  # degrees clockwise to turn the page upright (0, 90, 180, 270)
    orientation_conf: float
    script: str
    script_conf: float


@dataclass
class PageOcr:
    text: str
    top: str
    bottom: str
    words: int
    confidence: float
    languages: str
    text_pdf: bytes = field(repr=False)
    attempts: list[dict[str, Any]] = field(default_factory=list)


def tesseract_version() -> str:
    out = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, check=True)
    return (out.stdout or out.stderr).splitlines()[0].strip()


def _save(img: Image.Image, path: Path, dpi: float) -> None:
    img.save(path, format="PNG", dpi=(dpi, dpi))


def detect_osd(img: Image.Image, dpi: float) -> OsdResult | None:
    """Run Tesseract OSD. Returns None when the page has too little text."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "page.png"
        _save(img, p, dpi)
        res = subprocess.run(
            ["tesseract", str(p), "stdout", "--psm", "0", "--dpi", str(int(dpi)), "-l", "osd"],
            capture_output=True,
            text=True,
            env=_ENV,
        )
    out = res.stdout + res.stderr
    m_rot = re.search(r"Rotate:\s*(\d+)", out)
    m_oc = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    m_sc = re.search(r"Script:\s*(\S+)", out)
    m_scc = re.search(r"Script confidence:\s*([\d.]+)", out)
    if not (m_rot and m_sc):
        return None
    return OsdResult(
        rotate=int(m_rot.group(1)) % 360,
        orientation_conf=float(m_oc.group(1)) if m_oc else 0.0,
        script=m_sc.group(1),
        script_conf=float(m_scc.group(1)) if m_scc else 0.0,
    )


def rotate_upright(img: Image.Image, rotate_cw: int) -> Image.Image:
    """Lossless 90-degree-step rotation (clockwise)."""
    if rotate_cw == 90:
        return img.transpose(Image.Transpose.ROTATE_270)
    if rotate_cw == 180:
        return img.transpose(Image.Transpose.ROTATE_180)
    if rotate_cw == 270:
        return img.transpose(Image.Transpose.ROTATE_90)
    return img


def _parse_tsv(tsv: str, height_px: int, zone: float) -> tuple[str, str, str, int, float]:
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    lines: dict[tuple[int, int, int], list[tuple[int, str]]] = {}
    line_top: dict[tuple[int, int, int], int] = {}
    confs: list[float] = []
    for row in reader:
        if row.get("level") != "5":
            continue
        word = (row.get("text") or "").strip()
        if not word:
            continue
        try:
            conf = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
        except (KeyError, ValueError):
            continue
        key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        lines.setdefault(key, []).append((left, word))
        line_top[key] = min(line_top.get(key, top), top)
        if conf >= 0:
            confs.append(conf)
    ordered = sorted(lines, key=lambda k: (line_top[k], k))
    text_lines, top_lines, bottom_lines = [], [], []
    for k in ordered:
        s = " ".join(w for _, w in sorted(lines[k]))
        text_lines.append(s)
        if line_top[k] < height_px * zone:
            top_lines.append(s)
        if line_top[k] > height_px * (1 - zone):
            bottom_lines.append(s)
    mean = sum(confs) / len(confs) if confs else 0.0
    return "\n".join(text_lines), "\n".join(top_lines), "\n".join(bottom_lines), len(confs), round(mean, 1)


def read_page(img: Image.Image, dpi: float, languages: list[str], zone: float = 0.25) -> PageOcr:
    langs = "+".join(languages)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "page.png"
        base = Path(td) / "out"
        _save(img, p, dpi)
        res = subprocess.run(
            [
                "tesseract", str(p), str(base),
                "-l", langs, "--dpi", str(int(dpi)), "--psm", "3",
                "-c", "textonly_pdf=1", "pdf", "tsv",
            ],
            capture_output=True,
            text=True,
            env=_ENV,
        )
        if res.returncode != 0:
            raise OcrError(f"tesseract failed ({langs}): {res.stderr.strip()}")
        tsv = (base.with_suffix(".tsv")).read_text(encoding="utf-8")
        pdf = (base.with_suffix(".pdf")).read_bytes()
    text, top, bottom, words, conf = _parse_tsv(tsv, img.height, zone)
    return PageOcr(text=text, top=top, bottom=bottom, words=words, confidence=conf, languages=langs, text_pdf=pdf)


def language_set(script: str | None, ocr_cfg: dict[str, Any]) -> list[str]:
    if script == "Cyrillic" and ocr_cfg.get("cyrillic"):
        return list(ocr_cfg["cyrillic"])
    if script == "Latin" and ocr_cfg.get("latin"):
        return list(ocr_cfg["latin"])
    return list(ocr_cfg.get("default") or ["eng"])


def ocr_page(img: Image.Image, dpi: float, script: str | None, ocr_cfg: dict[str, Any], zone: float) -> PageOcr:
    """OCR with the script's language set; retry with the default set if confidence is low."""
    primary = language_set(script, ocr_cfg)
    result = read_page(img, dpi, primary, zone)
    attempts = [{"languages": result.languages, "confidence": result.confidence, "words": result.words}]
    threshold = float(ocr_cfg.get("min_confidence", 60))
    if result.words and result.confidence < threshold:
        alternatives = []
        default = list(ocr_cfg.get("default") or [])
        if default and default != primary:
            alternatives.append(default)
        other = language_set("Cyrillic" if script != "Cyrillic" else "Latin", ocr_cfg)
        if other not in (primary, default):
            alternatives.append(other)
        for alt in alternatives:
            r2 = read_page(img, dpi, alt, zone)
            attempts.append({"languages": r2.languages, "confidence": r2.confidence, "words": r2.words})
            if r2.confidence > result.confidence:
                result = r2
    result.attempts = attempts
    return result
