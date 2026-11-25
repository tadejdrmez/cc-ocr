"""Separator detection and split proposals (spec chapter 3).

Every detector only *proposes* split points with a reason. The operator
confirms them in the separation screen; the proof of concept accepts them
automatically when ``split.auto_accept`` is set and says so in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from .rules import PageContext, Rule

PAYLOAD_PREFIX = "CCOCR"
PAYLOAD_VERSION = "1"


@dataclass
class SeparatorCode:
    raw: str
    valid: bool
    fields: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def doctype(self) -> str | None:
        return self.fields.get("DT")

    @property
    def classification(self) -> str | None:
        return self.fields.get("CLS")


def parse_payload(raw: str) -> SeparatorCode | None:
    """Parse ``CCOCR:1;SEP;DT=INVOICE;CLS=4.2.1;PROFILE=x``. Returns None for foreign codes."""
    if not raw.startswith(PAYLOAD_PREFIX + ":"):
        return None
    parts = raw.split(";")
    head = parts[0].split(":", 1)
    if len(head) != 2 or head[1] != PAYLOAD_VERSION:
        return SeparatorCode(raw=raw, valid=False, error=f"unsupported version {head[-1]!r}")
    if len(parts) < 2 or parts[1] != "SEP":
        return SeparatorCode(raw=raw, valid=False, error="not a separator code")
    fields: dict[str, str] = {}
    for p in parts[2:]:
        if not p:
            continue
        if "=" not in p:
            return SeparatorCode(raw=raw, valid=False, error=f"malformed field {p!r}")
        k, v = p.split("=", 1)
        fields[k.strip().upper()] = v.strip()
    return SeparatorCode(raw=raw, valid=True, fields=fields)


def read_barcodes(img: Image.Image) -> list[tuple[str, str]]:
    """Return (format, text) for every barcode/QR code found on the page."""
    import zxingcpp

    arr = np.asarray(img.convert("L"))
    out = []
    for r in zxingcpp.read_barcodes(arr):
        out.append((str(r.format).split(".")[-1], r.text))
    return out


@dataclass
class PageFacts:
    """Everything the splitter knows about one page of the batch."""

    index: int
    is_blank: bool
    ink_ratio: float
    separator: SeparatorCode | None = None
    barcodes: list[tuple[str, str]] = field(default_factory=list)
    context: PageContext | None = None


@dataclass
class Proposal:
    before_page: int  # split happens before this 0-based page index
    source: str  # qr-separator | blank-page | text-rule | fixed-count | barcode
    reason: str
    accepted: bool = False
    accepted_by: str = ""


@dataclass
class DocumentPlan:
    pages: list[int]  # 0-based batch page indexes, in order
    metadata: dict[str, str] = field(default_factory=dict)
    review_flags: list[str] = field(default_factory=list)
    started_by: str = "batch start"


@dataclass
class SplitPlan:
    proposals: list[Proposal]
    documents: list[DocumentPlan]
    removed_pages: list[dict[str, Any]]


def plan_split(
    facts: list[PageFacts],
    split_cfg: dict[str, Any],
    rules: list[Rule],
    known_doctypes: set[str] | None = None,
) -> SplitPlan:
    enabled = set(split_cfg.get("proposals") or [])
    auto = bool(split_cfg.get("auto_accept", True))
    drop_blank = bool(split_cfg.get("drop_blank_pages", True))
    fixed = split_cfg.get("fixed_count")

    proposals: list[Proposal] = []
    removed: list[dict[str, Any]] = []
    documents: list[DocumentPlan] = []
    current: DocumentPlan | None = None
    pending_meta: dict[str, str] = {}
    pending_flags: list[str] = []
    pending_reason: str | None = None
    after_blank = False

    def start(reason: str) -> None:
        nonlocal current, pending_meta, pending_flags
        current = DocumentPlan(pages=[], metadata=dict(pending_meta), review_flags=list(pending_flags), started_by=reason)
        documents.append(current)
        pending_meta, pending_flags = {}, []

    for f in facts:
        # 1. QR separator sheet: the sheet itself is removed from the output.
        if "qr-separator" in enabled and f.separator is not None:
            code = f.separator
            reason = f"separator sheet on page {f.index + 1}"
            if not code.valid:
                pending_flags.append(f"invalid separator code on page {f.index + 1}: {code.error}")
            elif known_doctypes and code.doctype and code.doctype.lower() not in known_doctypes:
                pending_flags.append(f"unknown document type {code.doctype!r} on separator page {f.index + 1}")
            if code.valid:
                if code.doctype:
                    pending_meta["doctype"] = code.doctype.lower()
                if code.classification:
                    pending_meta["classification"] = code.classification
                for k, v in code.fields.items():
                    if k not in {"DT", "CLS"}:
                        pending_meta[k.lower()] = v
            proposals.append(Proposal(f.index + 1, "qr-separator", f"{reason} ({code.raw})", auto, "auto" if auto else ""))
            removed.append({"page": f.index + 1, "why": "separator sheet"})
            pending_reason = reason
            current = None
            after_blank = False
            continue

        # 2. Blank page: a separator when blank-page proposals are on, otherwise dropped or kept.
        if f.is_blank:
            if "blank-page" in enabled:
                proposals.append(
                    Proposal(f.index + 1, "blank-page", f"blank page {f.index + 1} ({f.ink_ratio * 100:.2f} % ink)", auto, "auto" if auto else "")
                )
                removed.append({"page": f.index + 1, "why": "blank separator page"})
                if current is not None and current.pages:
                    pending_reason = f"blank page {f.index + 1}"
                    current = None
                after_blank = True
                continue
            if drop_blank:
                removed.append({"page": f.index + 1, "why": f"blank page ({f.ink_ratio * 100:.2f} % ink)"})
                after_blank = True
                continue

        # 3. Text rules and other page-content triggers.
        matched: Rule | None = None
        if "text-rule" in enabled and f.context is not None:
            ctx = f.context
            ctx.prev_blank = after_blank
            matched = next((r for r in rules if r.matches(ctx)), None)
        trigger = None
        if current is not None and current.pages:
            if matched is not None:
                trigger = ("text-rule", f"rule '{matched.name}' matched on page {f.index + 1}")
            elif "barcode" in enabled and f.barcodes:
                fmt, txt = f.barcodes[0]
                trigger = ("barcode", f"{fmt} code '{txt[:40]}' on page {f.index + 1}")
            elif "fixed-count" in enabled and fixed and len(current.pages) >= int(fixed):
                trigger = ("fixed-count", f"{fixed} pages reached")
        if trigger is not None:
            proposals.append(Proposal(f.index, trigger[0], trigger[1], auto, "auto" if auto else ""))
            start(trigger[1])
        elif current is None:
            start(pending_reason or "batch start")
            pending_reason = None
        assert current is not None
        if not current.pages and matched is not None:
            # A rule that matches a document's first page may also name its type.
            for k, v in matched.metadata.items():
                current.metadata.setdefault(k, v)
        current.pages.append(f.index)
        after_blank = False

    documents = [d for d in documents if d.pages]
    return SplitPlan(proposals=proposals, documents=documents, removed_pages=removed)
