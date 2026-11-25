"""Image optimiser (spec 2.3).

The original scan is never modified. The optimiser produces:

* ``ocr_image`` - the cleaned image Tesseract reads and split detection uses;
* ``pdf_image`` - the image placed in the output PDF/A: the original scan
  (default, ``pdf_image: original``) or the optimised one.

The OCR text layer must line up with the image in the PDF, so geometric
steps (deskew) are only applied when the PDF also carries the optimised
image. Border removal paints borders white instead of cutting them off, and
DPI normalisation keeps physical size, so neither moves text on the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image

OPTIMISER_VERSION = "0.1.0"


@dataclass
class OptimisedPage:
    ocr_image: Image.Image
    pdf_image: Image.Image
    ocr_dpi: float
    pdf_dpi: float
    ink_ratio: float
    is_blank: bool
    low_dpi: bool
    deskew_deg: float = 0.0
    steps: list[str] = field(default_factory=list)


def _to_gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.uint8)


def _autolevels(g: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(g, (1, 99))
    if hi - lo < 10:
        return g
    out = (g.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def sauvola(g: np.ndarray, window: int, k: float = 0.2, r: float = 128.0) -> np.ndarray:
    """Adaptive binarisation; returns 0 (ink) / 255 (paper)."""
    window = max(3, window | 1)
    f = g.astype(np.float64)
    mean = cv2.boxFilter(f, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    sqmean = cv2.boxFilter(f * f, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    std = np.sqrt(np.maximum(sqmean - mean * mean, 0))
    thresh = mean * (1 + k * (std / r - 1))
    return np.where(f > thresh, 255, 0).astype(np.uint8)


def _otsu(g: np.ndarray) -> np.ndarray:
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return b


def _remove_borders(binary: np.ndarray, gray: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
    """Paint dark scanner borders (connected to the page edge) white."""
    ink = (binary == 0).astype(np.uint8)
    h, w = ink.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    mask = np.zeros_like(ink, dtype=bool)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        touches = x == 0 or y == 0 or x + bw >= w or y + bh >= h
        long_edge = bw > 0.5 * w or bh > 0.5 * h
        dense = area > 0.5 * bw * bh
        if touches and long_edge and dense:
            mask |= labels == i
    if not mask.any():
        return binary, gray, False
    b2, g2 = binary.copy(), gray.copy()
    b2[mask] = 255
    g2[mask] = 255
    return b2, g2, True


def estimate_skew(binary: np.ndarray, max_deg: float) -> float:
    """Projection-profile skew estimate in degrees (positive = counter-clockwise)."""
    ink = (binary == 0).astype(np.uint8) * 255
    scale = min(1.0, 1200.0 / max(ink.shape))
    small = cv2.resize(ink, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else ink
    if small.mean() < 0.5:  # (nearly) empty page
        return 0.0
    h, w = small.shape
    centre = (w / 2, h / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(centre, angle, 1.0)
        rot = cv2.warpAffine(small, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        prof = rot.sum(axis=1, dtype=np.float64)
        return float(np.var(prof))

    best, best_s = 0.0, score(0.0)
    for a in np.arange(-max_deg, max_deg + 0.01, 0.5):
        s = score(float(a))
        if s > best_s:
            best, best_s = float(a), s
    for a in np.arange(best - 0.5, best + 0.51, 0.1):
        s = score(float(a))
        if s > best_s:
            best, best_s = float(a), s
    return round(best, 2)


def _rotate(g: np.ndarray, angle: float) -> np.ndarray:
    h, w = g.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(g, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def ink_ratio(binary: np.ndarray, margin: float = 0.06) -> float:
    """Share of ink pixels in the inner page area, ignoring isolated specks."""
    h, w = binary.shape
    my, mx = int(h * margin), int(w * margin)
    inner = (binary[my:h - my, mx:w - mx] == 0).astype(np.uint8)
    if inner.size == 0:
        return 0.0
    n, _, stats, _ = cv2.connectedComponentsWithStats(inner, connectivity=8)
    min_area = max(4, int(h * w * 2e-6))
    ink = int(sum(stats[i, cv2.CC_STAT_AREA] for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area))
    return ink / inner.size


def optimise(img: Image.Image, dpi: float, settings: dict[str, Any]) -> OptimisedPage:
    steps: list[str] = []
    gray = _to_gray(img)
    target = float(settings.get("dpi") or dpi)
    low_dpi = dpi < float(settings.get("min_dpi", 200))
    if low_dpi:
        steps.append(f"warning: {dpi:.0f} dpi is below the minimum {settings.get('min_dpi')} dpi")
    ocr_dpi = dpi
    if target and abs(target - dpi) / dpi > 0.05:
        f = target / dpi
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
        ocr_dpi = target
        steps.append(f"dpi {dpi:.0f} -> {target:.0f}")

    if settings.get("contrast", True):
        gray = _autolevels(gray)
        steps.append("contrast: auto-levels")
    if settings.get("despeckle", True):
        gray = cv2.medianBlur(gray, 3)
        steps.append("despeckle: median 3")

    mode = settings.get("binarise", "sauvola")
    window = int(round(ocr_dpi / 300.0 * 31))
    binary = sauvola(gray, window) if mode == "sauvola" else _otsu(gray)
    if mode != "none":
        steps.append(f"binarise: {mode}")

    if settings.get("crop", True):
        binary, gray, removed = _remove_borders(binary, gray)
        if removed:
            steps.append("border removal: dark edges painted white")

    deskew_deg = 0.0
    use_optimised_pdf = settings.get("pdf_image", "original") == "optimised"
    if settings.get("deskew", True):
        angle = estimate_skew(binary, float(settings.get("max_deskew_deg", 10)))
        if abs(angle) >= 0.2:
            if use_optimised_pdf:
                gray = _rotate(gray, angle)
                binary = sauvola(gray, window) if mode == "sauvola" else _otsu(gray)
                deskew_deg = angle
                steps.append(f"deskew: {angle:+.2f} deg")
            else:
                steps.append(f"deskew: {angle:+.2f} deg detected, not applied (original image kept in PDF)")

    ratio = ink_ratio(binary)
    is_blank = ratio < float(settings.get("blank_threshold", 0.004))

    ocr_arr = binary if mode != "none" else gray
    ocr_image = Image.fromarray(ocr_arr)
    if use_optimised_pdf:
        pdf_image, pdf_dpi = ocr_image, ocr_dpi
    else:
        pdf_image, pdf_dpi = img, dpi
    return OptimisedPage(
        ocr_image=ocr_image,
        pdf_image=pdf_image,
        ocr_dpi=ocr_dpi,
        pdf_dpi=pdf_dpi,
        ink_ratio=round(ratio, 5),
        is_blank=is_blank,
        low_dpi=low_dpi,
        deskew_deg=deskew_deg,
        steps=steps,
    )
