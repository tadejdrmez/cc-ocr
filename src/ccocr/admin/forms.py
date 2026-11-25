"""Field definitions for the profile editor, and form <-> profile conversion.

Each field maps a dotted profile key (``optimiser.dpi``) to an input type. The
templates render the fields generically, so adding a setting is one line here.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

KNOWN_LANGS = ["slv", "eng", "hrv", "srp", "srp_latn", "bos", "sqi", "deu", "ita", "hun", "mkd"]


def installed_langs() -> list[str]:
    if shutil.which("tesseract"):
        try:
            out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10).stdout
            langs = [x.strip() for x in out.splitlines()[1:] if x.strip() and x.strip() != "osd"]
            if langs:
                return sorted(langs)
        except (OSError, subprocess.SubprocessError):
            pass
    return KNOWN_LANGS


@dataclass
class Field:
    key: str
    label: str
    kind: str  # text, int, float, bool, select, multi, langs, percent, textarea
    help: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    audited_reason: bool = False  # changing it needs a written justification


SECTIONS: list[tuple[str, str, list[Field]]] = [
    ("general", "General", [
        Field("description", "Description", "text"),
        Field("document_type", "Default document type", "select", "Used when no separator sheet or rule gives a type."),
        Field("classification", "Default classification", "text", "Classification code, e.g. 4.2.1. Leave empty if not used."),
        Field("document_types", "Document types this profile accepts", "multi",
              "Separator sheets with another type are flagged for review."),
    ]),
    ("optimiser", "Optimiser", [
        Field("optimiser.despeckle", "Despeckle", "bool", "Removes isolated dots and scanner noise."),
        Field("optimiser.contrast", "Auto contrast", "bool", "Normalises faint or dark scans."),
        Field("optimiser.binarise", "Binarisation", "select", "Black/white conversion used for OCR only.",
              [("sauvola", "Sauvola (adaptive, recommended)"), ("otsu", "Otsu (global)"), ("none", "None")]),
        Field("optimiser.deskew", "Deskew", "bool"),
        Field("optimiser.max_deskew_deg", "Maximum deskew angle (°)", "float", "", min=0, max=45),
        Field("optimiser.crop", "Remove black borders", "bool"),
        Field("optimiser.dpi", "Target resolution (dpi)", "int", "", min=150, max=600),
        Field("optimiser.min_dpi", "Minimum resolution (dpi)", "int", "Pages below this are flagged for review.", min=72, max=600),
        Field("optimiser.blank_threshold", "Blank page ink threshold (%)", "percent",
              "A page with less ink than this on its inner area counts as blank.", min=0, max=5),
        Field("optimiser.pdf_image", "Image in the output PDF/A", "select",
              "The original scan is the recommended default (D2). The optimised image gives smaller files.",
              [("original", "Original scan"), ("optimised", "Optimised image")]),
    ]),
    ("ocr", "OCR & languages", [
        Field("ocr.osd", "Detect orientation and script on every page", "bool",
              "Each page is rotated upright and its script (Latin or Cyrillic) picks the language set."),
        Field("ocr.latin", "Language set for Latin pages", "langs"),
        Field("ocr.cyrillic", "Language set for Cyrillic pages", "langs", "Keep srp and srp_latn together for Serbian."),
        Field("ocr.default", "Default language set", "langs", "Used when a page has too little text to detect the script."),
        Field("ocr.min_confidence", "Minimum page confidence", "int",
              "Below this the page is re-run with the other set and flagged for review.", min=0, max=100),
        Field("ocr.output", "PDF/A level", "select", "",
              [("auto", "Automatic (PDF/A-3 for single file, PDF/A-2 with sidecars)"), ("pdfa-2", "PDF/A-2b"), ("pdfa-3", "PDF/A-3b")]),
    ]),
    ("split", "Split", [
        Field("split.proposals", "Split proposals", "multi", "Automatic methods only propose split points. Select none for no splitting: every input file becomes one searchable PDF and nothing waits for an operator.",
              [("qr-separator", "QR separator sheets"), ("blank-page", "Blank pages"), ("text-rule", "Text rules"),
               ("fixed-count", "Fixed page count")]),
        Field("split.zone", "Top / bottom zone for rules (%)", "percent", "Share of the page height that 'top' and 'bottom' look at.",
              min=5, max=50),
        Field("split.fixed_count", "Fixed page count", "int", "Only used when 'Fixed page count' is on. Empty = off.", min=1, max=1000),
        Field("split.drop_blank_pages", "Remove blank pages from documents", "bool"),
        Field("split.auto_accept", "Accept split proposals automatically", "bool",
              "Off by default: batches wait for an operator to confirm the split. Turning it on needs a reason and is audited. Not needed when no split proposals are selected.",
              audited_reason=True),
    ]),
    ("output", "Output & naming", [
        Field("output.package", "Output package", "select", "",
              [("single", "One PDF/A-3 per document (XML embedded, PDF timestamp)"),
               ("sidecars", "PDF/A-2 with .xml, .manifest.xml and .tsr next to it")]),
        Field("output.size", "File size", "select",
              "Original keeps the scan images exactly as scanned. Compact makes the smallest file: colour and grey images "
              "are re-compressed; black-and-white pages and the text layer stay lossless.",
              [("original", "Original images (lossless)"), ("compact", "Compact (smallest file)")]),
        Field("output.jpeg_quality", "Compact: JPEG quality", "int", "30 to 95. Lower = smaller files.", min=30, max=95),
        Field("output.metadata", "Metadata", "multi", "",
              [("xml", "Metadata XML"), ("filename", "Metadata in the file name")]),
        Field("output.naming", "File name pattern", "text",
              "Tokens: {doctype} {classification} {batch} {seq} {date} {profile}, plus fields from separator sheets."),
        Field("output.date_format", "Date format", "select", "",
              [("%Y-%m-%d", "2026-11-25"), ("%Y%m%d", "20261125"), ("%d.%m.%Y", "25.11.2026")]),
        Field("output.max_length", "Maximum file name length", "int", "Characters, not bytes.", min=20, max=240),
        Field("output.on_collision", "If the name already exists", "select", "Existing files are never overwritten.",
              [("suffix", "Add _2, _3 …"), ("error", "Stop with an error")]),
    ]),
]


def get_path(data: dict[str, Any], key: str) -> Any:
    cur: Any = data
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def set_path(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def display_value(f: Field, value: Any) -> Any:
    if f.kind == "percent" and value is not None:
        return round(float(value) * 100, 4)
    if f.kind == "langs":
        return " ".join(value or [])
    return value


class FormError(ValueError):
    pass


def apply_form(profile: dict[str, Any], section: str, form: dict[str, Any], getlist) -> dict[str, Any]:  # noqa: ANN001
    """Return a copy of ``profile`` with one section's fields taken from the submitted form."""
    out = copy.deepcopy(profile)
    fields = next(fs for sid, _, fs in SECTIONS if sid == section)
    for f in fields:
        name = f.key
        if f.kind == "bool":
            value: Any = name in form
        elif f.kind == "multi":
            value = list(getlist(name))
        else:
            raw = str(form.get(name, "")).strip()
            if f.kind in ("int", "float", "percent"):
                if raw == "":
                    if f.key == "split.fixed_count":
                        set_path(out, name, None)
                        continue
                    raise FormError(f"{f.label}: enter a number")
                try:
                    num = float(raw.replace(",", "."))
                except ValueError as e:
                    raise FormError(f"{f.label}: '{raw}' is not a number") from e
                if (f.min is not None and num < f.min) or (f.max is not None and num > f.max):
                    raise FormError(f"{f.label}: must be between {f.min:g} and {f.max:g}")
                value = int(num) if f.kind == "int" else (num / 100 if f.kind == "percent" else num)
            elif f.kind == "langs":
                value = [x for x in raw.replace(",", " ").replace("+", " ").split() if x]
                if not value:
                    raise FormError(f"{f.label}: choose at least one language")
                avail = set(installed_langs())
                bad = [x for x in value if x not in avail]
                if bad:
                    raise FormError(f"{f.label}: language pack(s) not installed: {', '.join(bad)}")
            else:
                value = raw
        set_path(out, name, value)
    return out
