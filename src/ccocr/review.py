"""Operator separation (spec 3.2, chapter 8 steps 4 and 7).

With ``split.auto_accept`` off, the watcher analyses a batch once and parks it
in ``review/<job_id>/``::

    review/<job_id>/
        <batch file>          the scan, unchanged
        plan.json             what the console shows: pages, proposals, documents
        state.json            per-page analysis (OCR text layers, rotation), reused on release
        thumbs/p0001.jpg      small page image, upright
        thumbs/p0001-l.jpg    larger preview
        decision.json         written by the console when the operator confirms or rejects

The console only writes ``decision.json`` (atomically). The watcher picks it up
on its next poll, re-creates the page images from the batch (cheap), re-reads
only the pages the operator turned, and writes the documents exactly as the
operator laid them out. OCR is not repeated for the other pages.

``state.json`` is plain JSON (no pickle), so a file in the data folder can
never run code.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from .ingest import load_batch, sha256_file
from .ocr import OsdResult, PageOcr, rotate_upright
from .optimiser import optimise
from .pipeline import Log, PageResult, _analyse_page, analyse_batch, process_batch, split_plan
from .profile import Profile, load_profile
from .rules import PageContext
from .separators import PageFacts, parse_payload

PLAN = "plan.json"
STATE = "state.json"
DECISION = "decision.json"
TAKEN = "decision.taken.json"
THUMB_W, LARGE_W = 180, 1000
TEXT_LIMIT = 6000


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def batch_file(job_dir: Path) -> Path | None:
    skip = {PLAN, STATE, DECISION, TAKEN}
    for p in sorted(job_dir.iterdir()):
        if p.is_file() and p.name not in skip and not p.name.endswith((".tmp", ".error.txt")):
            return p
    return None


# -- state (de)serialisation ---------------------------------------------------
def _page_state(r: PageResult) -> dict[str, Any]:
    f = r.facts
    return {
        "index": r.source.index, "dpi": r.source.dpi, "source_notes": r.source.notes,
        "rotation": r.rotation, "notes": r.notes,
        "osd": asdict(r.osd) if r.osd else None,
        "ocr": ({**{k: v for k, v in asdict(r.ocr).items() if k != "text_pdf"},
                 "text_pdf": base64.b64encode(r.ocr.text_pdf).decode()} if r.ocr else None),
        "facts": {"is_blank": f.is_blank, "ink_ratio": f.ink_ratio,
                  "separator": f.separator.raw if f.separator else None,
                  "barcodes": [list(b) for b in f.barcodes],
                  "context": asdict(f.context) if f.context else None},
    }


def _restore(st: dict[str, Any], sp: Any, profile: Profile) -> PageResult:
    opt = optimise(sp.image, sp.dpi, profile["optimiser"])
    fx = st["facts"]
    facts = PageFacts(index=st["index"], is_blank=fx["is_blank"], ink_ratio=fx["ink_ratio"],
                      separator=parse_payload(fx["separator"]) if fx["separator"] else None,
                      barcodes=[tuple(b) for b in fx["barcodes"]],
                      context=PageContext(**fx["context"]) if fx["context"] else None)
    ocr = None
    if st["ocr"]:
        o = dict(st["ocr"])
        o["text_pdf"] = base64.b64decode(o["text_pdf"])
        ocr = PageOcr(**o)
    pr = PageResult(source=sp, opt=opt, facts=facts, osd=OsdResult(**st["osd"]) if st["osd"] else None,
                    rotation=st["rotation"], ocr=ocr, notes=list(st["notes"]))
    pr.pdf_image = rotate_upright(opt.pdf_image, pr.rotation) if pr.rotation else opt.pdf_image
    return pr


# -- hold ----------------------------------------------------------------------
def _thumbs(results: list[PageResult], d: Path) -> None:
    d.mkdir(exist_ok=True)
    for r in results:
        img = r.pdf_image if r.pdf_image is not None else r.opt.pdf_image
        img = img.convert("L") if img.mode not in ("L", "RGB") else img
        for w, suffix in ((THUMB_W, ""), (LARGE_W, "-l")):
            t = img.copy()
            t.thumbnail((w, w * 2), Image.Resampling.LANCZOS)
            t.save(d / f"p{r.source.index + 1:04d}{suffix}.jpg", "JPEG", quality=72 if suffix else 60)


def _page_view(r: PageResult, profile: Profile) -> dict[str, Any]:
    f, o = r.facts, r.ocr
    min_conf = float(profile["ocr"].get("min_confidence", 60))
    flags = []
    if o and o.words and o.confidence < min_conf:
        flags.append(f"low OCR confidence ({o.confidence:.0f})")
    if r.opt.low_dpi:
        flags.append(f"low resolution ({r.source.dpi:.0f} dpi)")
    if f.separator is not None and not f.separator.valid:
        flags.append(f"unreadable separator sheet: {f.separator.error}")
    sep = None
    if f.separator is not None:
        sep = {"valid": f.separator.valid, "doctype": f.separator.doctype, "fields": f.separator.fields}
    return {
        "index": r.source.index, "blank": f.is_blank, "ink": round(f.ink_ratio, 5), "separator": sep,
        "barcodes": [b[1] for b in f.barcodes], "rotation": r.rotation,
        "words": o.words if o else 0, "confidence": round(o.confidence, 1) if o else None,
        "languages": o.languages if o else "", "top": (o.top[:200] if o else ""),
        "text": (o.text[:TEXT_LIMIT] if o else ""), "notes": r.notes, "flags": flags,
    }


def hold(job_dir: Path, profile: Profile, *, job_id: str, batch_name: str, sha256: str,
         out_dir: Path, reports_dir: Path, folder: str, rel: str = "", log: Log = lambda m: None) -> dict[str, Any]:
    """Analyse a batch in ``job_dir`` and write what the separation screen needs."""
    batch = batch_file(job_dir)
    if batch is None:
        raise FileNotFoundError(f"no batch file in {job_dir}")
    base = {
        "job_id": job_id, "batch_name": batch_name, "batch_file": batch.name, "sha256": sha256,
        "held_at": datetime.now().isoformat(timespec="seconds"), "folder": folder, "rel": rel,
        "out_dir": str(out_dir), "reports_dir": str(reports_dir),
        "profile": {"name": profile.name, "raw": profile.raw},
    }
    write_json(job_dir / PLAN, {**base, "status": "analysing"})
    try:
        results = analyse_batch(batch, profile, job_id=job_id, log=log)
        plan = split_plan(results, profile, auto=False)
        _thumbs(results, job_dir / "thumbs")
        write_json(job_dir / STATE, [_page_state(r) for r in results])
    except Exception as e:  # noqa: BLE001
        write_json(job_dir / PLAN, {**base, "status": "failed", "error": f"{type(e).__name__}: {e}"})
        raise
    view = {
        **base, "status": "ready",
        "pages": [_page_view(r, profile) for r in results],
        "proposals": [asdict(p) for p in plan.proposals],
        "documents": [{"pages": d.pages, "doctype": d.metadata.get("doctype", ""),
                       "classification": d.metadata.get("classification", ""),
                       "review_flags": d.review_flags, "started_by": d.started_by} for d in plan.documents],
        "removed_pages": plan.removed_pages,
        "document_types": list(profile.raw.get("document_types") or []),
    }
    write_json(job_dir / PLAN, view)
    return view


# -- release -------------------------------------------------------------------
def validate_decision(plan: dict[str, Any], decision: dict[str, Any]) -> None:
    n = len(plan.get("pages") or [])
    seen: set[int] = set()
    for d in decision.get("documents") or []:
        for p in d.get("pages") or []:
            if not isinstance(p, int) or p < 0 or p >= n or p in seen:
                raise ValueError("each page may be in one document only")
            seen.add(p)
    if not seen:
        raise ValueError("keep at least one page")
    for k, v in (decision.get("rotate") or {}).items():
        if int(k) < 0 or int(k) >= n or int(v) % 90:
            raise ValueError("rotation must be a multiple of 90 degrees")


def release(job_dir: Path, decision: dict[str, Any], *, workers: int | None = None,
            log: Log = lambda m: None) -> dict[str, Any]:
    """Write the documents of a held batch as the operator laid them out."""
    plan = read_json(job_dir / PLAN)
    validate_decision(plan, decision)
    profile = load_profile(plan["profile"]["raw"])
    batch = job_dir / plan["batch_file"]
    states = read_json(job_dir / STATE)
    pages = load_batch(batch)
    if len(pages) != len(states):
        raise ValueError(f"batch has {len(pages)} pages, the analysis had {len(states)}")
    turn = {int(k): int(v) % 360 for k, v in (decision.get("rotate") or {}).items() if int(v) % 360}
    results = []
    for st, sp in zip(states, pages):
        if sp.index in turn:
            log(f"[{plan['job_id']}] page {sp.index + 1}: turned {turn[sp.index]} deg by the operator, reading again")
            results.append(_analyse_page(sp, profile, len(pages), force_rotation=st["rotation"] + turn[sp.index]))
        else:
            results.append(_restore(st, sp, profile))
    return process_batch(
        batch, profile, Path(plan["out_dir"]), job_id=plan["job_id"], batch_name=plan["batch_name"],
        workers=workers, confirmed_by=str(decision.get("by") or "operator"), reports_dir=Path(plan["reports_dir"]),
        results=results, decision=decision, log=log,
    )


def pending(review_dir: Path) -> list[Path]:
    """Job folders with an operator decision waiting to be applied."""
    if not review_dir.is_dir():
        return []
    return sorted(d for d in review_dir.iterdir() if d.is_dir() and (d / DECISION).is_file())


def remove(job_dir: Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)


def digest(path: Path) -> str:
    return sha256_file(path)
