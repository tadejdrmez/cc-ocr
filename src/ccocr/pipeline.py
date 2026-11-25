"""Batch pipeline: import -> optimise -> split proposals -> OCR -> PDF/A -> output.

Order (spec chapter 8, steps 1-5, 9, 12): every page is OCR'd once, before
splitting, so that text rules can see its text; the same per-page text layer
is reused when documents are assembled. With auto-accept off, the watcher
stops after the analysis (see ``review.py``), the operator confirms the split
in the console, and the batch is finished with the operator's decision.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .assemble import AssemblyPage, PdfaWarning, build_pdf, ocrmypdf_version
from .ingest import SourcePage, load_batch, sha256_file
from .naming import NamingError, build_name, unique_path
from .ocr import OsdResult, PageOcr, detect_osd, ocr_page, rotate_upright, tesseract_version
from .optimiser import OPTIMISER_VERSION, OptimisedPage, optimise
from .pades import add_document_timestamp, embed_files, verify_pdf
from .output import SidecarInput, build_manifest, build_sidecar, check_manifest, sha256_bytes, write_atomic
from .profile import Profile
from .rules import PageContext
from .separators import DocumentPlan, PageFacts, Proposal, SplitPlan, parse_payload, plan_split, read_barcodes
from .timestamp import TimestampError, timestamp, verify

Log = Callable[[str], None]


@dataclass
class PageResult:
    source: SourcePage
    opt: OptimisedPage
    facts: PageFacts
    osd: OsdResult | None = None
    rotation: int = 0
    ocr: PageOcr | None = None
    pdf_image: Any = None
    notes: list[str] = field(default_factory=list)


def _page_ranges(pages: list[int]) -> str:
    """0-based indexes -> '3-5,7' (1-based)."""
    out, start, prev = [], None, None
    for p in pages:
        n = p + 1
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(out)


def _analyse_page(sp: SourcePage, profile: Profile, n_pages: int, force_rotation: int | None = None) -> PageResult:
    """Optimise, read codes and OCR one page. ``force_rotation`` (degrees clockwise) replaces
    the OSD orientation, for pages the operator turned on the separation screen."""
    opt_cfg, ocr_cfg, split_cfg = profile["optimiser"], profile["ocr"], profile["split"]
    opt = optimise(sp.image, sp.dpi, opt_cfg)
    facts = PageFacts(index=sp.index, is_blank=opt.is_blank, ink_ratio=opt.ink_ratio)
    pr = PageResult(source=sp, opt=opt, facts=facts, pdf_image=opt.pdf_image)

    wants_codes = {"qr-separator", "barcode"} & set(split_cfg["proposals"])
    if wants_codes and not opt.is_blank:
        for fmt, text in read_barcodes(sp.image):
            code = parse_payload(text)
            if code is not None:
                facts.separator = code
            else:
                facts.barcodes.append((fmt, text))
    if facts.separator is not None or opt.is_blank:
        return pr  # separator sheets and blank pages are not OCR'd

    ocr_img = opt.ocr_image
    if force_rotation is not None:
        pr.rotation = force_rotation % 360
        if pr.rotation:
            ocr_img = rotate_upright(ocr_img, pr.rotation)
            pr.pdf_image = rotate_upright(opt.pdf_image, pr.rotation)
        pr.notes.append(f"rotated {pr.rotation} deg clockwise by the operator")
        if ocr_cfg.get("osd", True):
            pr.osd = detect_osd(ocr_img, opt.ocr_dpi)  # script only; the page is upright now
    elif ocr_cfg.get("osd", True):
        pr.osd = detect_osd(ocr_img, opt.ocr_dpi)
        if pr.osd and pr.osd.rotate and pr.osd.orientation_conf >= 1.5:
            pr.rotation = pr.osd.rotate
            ocr_img = rotate_upright(ocr_img, pr.rotation)
            pr.pdf_image = rotate_upright(opt.pdf_image, pr.rotation)
            pr.notes.append(f"rotated {pr.rotation} deg clockwise (OSD confidence {pr.osd.orientation_conf:.1f})")
        elif pr.osd is None:
            pr.notes.append("OSD unreliable (little text): default language set used")
    script = pr.osd.script if (pr.osd and pr.osd.script_conf >= 1.0) else None
    zone = float(split_cfg.get("zone", 0.25))
    pr.ocr = ocr_page(ocr_img, opt.ocr_dpi, script, ocr_cfg, zone)
    facts.context = PageContext(
        text=pr.ocr.text, top=pr.ocr.top, bottom=pr.ocr.bottom, page=sp.index + 1,
        words=pr.ocr.words, confidence=pr.ocr.confidence, first_page=sp.index == 0,
    )
    return pr


def analyse_batch(
    batch: Path, profile: Profile, *, workers: int | None = None, job_id: str = "", log: Log = lambda m: None,
    pages: list[SourcePage] | None = None,
) -> list[PageResult]:
    """Steps 1-3: load, optimise, read codes and OCR every page (no output yet)."""
    if pages is None:
        log(f"[{job_id}] {batch.name}: loading")
        pages = load_batch(batch)
    log(f"[{job_id}] {len(pages)} page(s); optimising and reading")
    workers = workers or max(1, min(len(pages), os.cpu_count() or 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda sp: _analyse_page(sp, profile, len(pages)), pages))


def split_plan(results: list[PageResult], profile: Profile, *, auto: bool) -> SplitPlan:
    known = {str(d).lower() for d in profile.raw.get("document_types", []) or []} or None
    return plan_split([r.facts for r in results], {**profile["split"], "auto_accept": auto}, profile.rules, known)


def apply_decision(plan: SplitPlan, decision: dict[str, Any], n_pages: int) -> SplitPlan:
    """Replace the proposed documents with the operator's (spec 3.2).

    ``decision["documents"]`` is a list of ``{"pages": [0-based...], "doctype", "classification"}``;
    pages in no document are removed. Proposals are marked accepted when the operator kept a
    split at that place and rejected otherwise.
    """
    by = str(decision.get("by") or "operator")
    docs: list[DocumentPlan] = []
    seen: set[int] = set()
    old_by_page = {p: d for d in plan.documents for p in d.pages}
    for d in decision.get("documents") or []:
        pages = [int(p) for p in d.get("pages") or []]
        if not pages:
            continue
        if any(p < 0 or p >= n_pages or p in seen for p in pages):
            raise ValueError("operator decision lists a page twice or a page outside the batch")
        seen.update(pages)
        old = old_by_page.get(pages[0])
        meta = dict(old.metadata) if old else {}
        for k in ("doctype", "classification"):
            if d.get(k):
                meta[k] = str(d[k])
        flags = sorted({f for p in pages if (o := old_by_page.get(p)) for f in o.review_flags})
        docs.append(DocumentPlan(pages=pages, metadata=meta, review_flags=flags, started_by=f"operator {by}"))
    if not docs:
        raise ValueError("operator decision has no documents")
    starts = {d.pages[0] for d in docs[1:]}
    proposals = [Proposal(p.before_page, p.source, p.reason, p.before_page in starts, by) for p in plan.proposals]
    removed = [{"page": p + 1, "why": f"removed by {by}"} for p in range(n_pages) if p not in seen]
    return SplitPlan(proposals=proposals, documents=docs, removed_pages=removed)


def process_batch(
    batch: Path,
    profile: Profile,
    out_dir: Path,
    *,
    job_id: str | None = None,
    batch_name: str | None = None,
    workers: int | None = None,
    confirmed_by: str | None = None,
    reports_dir: Path | None = None,
    results: list[PageResult] | None = None,
    decision: dict[str, Any] | None = None,
    log: Log = lambda m: None,
) -> dict[str, Any]:
    """Process one batch. ``results`` (from :func:`analyse_batch`) skips the analysis;
    ``decision`` (from the operator separation screen) replaces the automatic split."""
    started = datetime.now(timezone.utc).astimezone()
    job_id = job_id or started.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    out_dir.mkdir(parents=True, exist_ok=True)
    batch_name = batch_name or batch.name
    batch_stem = Path(batch_name).stem
    batch_sha = sha256_file(batch)
    if results is None:
        results = analyse_batch(batch, profile, workers=workers, job_id=job_id, log=log)

    plan = split_plan(results, profile, auto=confirmed_by is None)
    if decision is not None:
        plan = apply_decision(plan, decision, len(results))
    log(f"[{job_id}] {len(plan.documents)} document(s) from {len(plan.proposals)} split proposal(s)")

    versions = {
        "cc-ocr": __version__,
        "optimiser": OPTIMISER_VERSION,
        "tesseract": tesseract_version(),
        "ocrmypdf": ocrmypdf_version(),
    }
    out_cfg, ts_cfg, ocr_cfg = profile["output"], profile["timestamp"], profile["ocr"]
    min_conf = float(ocr_cfg.get("min_confidence", 60))
    package = out_cfg.get("package", "single")
    single = package == "single"
    want_xml = bool({"xml", "sidecar-xml"} & set(out_cfg.get("metadata", [])))
    pdfa = ocr_cfg.get("output") or "auto"
    if pdfa == "auto" or (single and want_xml and pdfa != "pdfa-3"):
        # Embedding the metadata XML needs PDF/A-3 (associated files).
        pdfa = "pdfa-3" if single else "pdfa-2"
    # Without an operator decision (confirmed_by) the proposals are applied as they are,
    # and every report and XML says so ("auto-accepted").
    auto = confirmed_by is None
    documents_report: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Documents are written to a staging folder and moved into out/ only when the whole
    # batch succeeded. A failure (e.g. TSA unreachable) leaves nothing half-done in out/;
    # the watcher retries the batch later and finally moves it to error/.
    required_ts = bool(ts_cfg.get("required", True)) and ts_cfg.get("mode") != "none"
    stage = out_dir / f".staging-{job_id}"
    stage.mkdir(parents=True, exist_ok=True)
    try:
        for seq, doc in enumerate(plan.documents, start=1):
            doc_pages = [results[i] for i in doc.pages]
            meta: dict[str, str] = {
                "doctype": doc.metadata.get("doctype") or profile["document_type"],
                "classification": doc.metadata.get("classification") or profile["classification"] or "",
                "profile": profile.name,
                "batch": batch_stem,
                "seq": f"{seq:03d}",
                "date": started.strftime(out_cfg.get("date_format", "%Y-%m-%d")),
                "pages": str(len(doc_pages)),
                **{k: v for k, v in doc.metadata.items() if k not in {"doctype", "classification"}},
            }
            flags = list(doc.review_flags)
            low = [r.source.index + 1 for r in doc_pages if r.ocr and r.ocr.words and r.ocr.confidence < min_conf]
            if low:
                flags.append(f"low OCR confidence on batch page(s) {', '.join(map(str, low))}")
            lowdpi = [r.source.index + 1 for r in doc_pages if r.opt.low_dpi]
            if lowdpi:
                flags.append(f"resolution below minimum on batch page(s) {', '.join(map(str, lowdpi))}")

            naming = out_cfg.get("naming", "{doctype}_{batch}_{seq}")
            try:
                base = build_name(naming, meta, max_length=int(out_cfg.get("max_length", 120)))
            except NamingError as e:
                flags.append(f"naming: {e}; fallback name used")
                base = build_name("{doctype}_{batch}_{seq}", meta)
            exts = (".pdf",) if single else (".pdf", ".xml", ".manifest.xml", ".tsr")
            base = unique_path(out_dir, base, exts, out_cfg.get("on_collision", "suffix"))

            pdf_path = stage / f"{base}.pdf"
            tmp_pdf = stage / f".{base}.pdf.part"
            asm = [
                AssemblyPage(image=r.pdf_image, dpi=r.opt.pdf_dpi, text_pdf=r.ocr.text_pdf)  # type: ignore[union-attr]
                for r in doc_pages
                if r.ocr is not None
            ]
            if not asm:
                warnings.append(f"document {seq} has no OCR'd pages; skipped")
                continue
            langs = sorted({r.ocr.languages for r in doc_pages if r.ocr})
            try:
                build_pdf(asm, tmp_pdf, title=base, pdfa=pdfa)
                pdfa_status = "converted"
            except PdfaWarning as w:
                pdfa_status = "written; PDF/A validity not confirmed"
                flags.append(f"PDF/A conversion warning: {w}")
            confs = [r.ocr.confidence for r in doc_pages if r.ocr and r.ocr.words]
            mean_conf = round(sum(confs) / len(confs), 1) if confs else None
            scripts = sorted({(r.osd.script if r.osd else "unknown") for r in doc_pages})

            sidecar: bytes | None = None
            if want_xml:
                fields = [
                    {"name": k, "type": "text", "value": v, "source": "separator" if k in doc.metadata else "default"}
                    for k, v in meta.items()
                    if k not in {"profile", "seq", "pages", "batch"} and v
                ]
                sidecar = build_sidecar(
                    SidecarInput(
                        pdf_name=pdf_path.name,
                        # In single-file mode the XML lives inside the PDF, so it cannot carry
                        # the PDF's own hash; the document timestamp protects both instead.
                        pdf_sha256=None if single else sha256_bytes(tmp_pdf.read_bytes()),
                        page_count=len(asm), profile=profile.name,
                        validation_status="auto-accepted" if auto else "confirmed", validated_by="" if auto else confirmed_by,
                        validated_at=datetime.now(timezone.utc).astimezone(), doctype=meta["doctype"],
                        classification=meta["classification"], batch_name=batch_name, batch_sha256=batch_sha,
                        batch_pages=_page_ranges(doc.pages), fields=fields,
                        ocr_engine=f"OCRmyPDF {versions['ocrmypdf']} / {versions['tesseract']}",
                        languages=";".join(langs), scripts=",".join(scripts),
                        image=profile["optimiser"]["pdf_image"], pdfa=pdfa, mean_confidence=mean_conf,
                    )
                )

            ts_report: dict[str, Any] = {"mode": ts_cfg.get("mode")}
            xml_name = None
            manifest_name = None
            integrity: Any = "ok"
            if single:
                if sidecar is not None:
                    xml_name = f"{base}.xml"
                    embed_files(tmp_pdf, [(xml_name, sidecar, "application/xml", "CC OCR document metadata (urn:cc:ocr:metadata:1)")])
                if ts_cfg.get("mode") != "none":
                    try:
                        info = add_document_timestamp(tmp_pdf, ts_cfg)
                        ok, detail = verify_pdf(tmp_pdf, ts_cfg)
                        ts_report.update(kind="PAdES document timestamp (ETSI.RFC3161)", **info, verified=ok, verify_detail=detail)
                        if not info.get("qualified"):
                            ts_report["note"] = "NON-QUALIFIED timestamp (test/pilot)"
                    except Exception as e:  # noqa: BLE001
                        if required_ts:
                            raise TimestampError(f"document {seq}: timestamp failed: {e}") from e
                        flags.append(f"timestamp failed: {e}")
                        ts_report["error"] = str(e)
                    if required_ts and not ts_report.get("verified"):
                        raise TimestampError(f"document {seq}: timestamp did not verify: {ts_report.get('verify_detail')}")
                os.replace(tmp_pdf, pdf_path)
                pdf_sha = sha256_bytes(pdf_path.read_bytes())
            else:
                os.replace(tmp_pdf, pdf_path)
                pdf_bytes = pdf_path.read_bytes()
                pdf_sha = sha256_bytes(pdf_bytes)
                entries: list[tuple[str, str, bytes]] = [(pdf_path.name, "document", pdf_bytes)]
                if sidecar is not None:
                    xml_name = f"{base}.xml"
                    write_atomic(stage / xml_name, sidecar)
                    entries.append((xml_name, "metadata", sidecar))
                manifest = build_manifest(job_id, profile.name, datetime.now(timezone.utc).astimezone(), entries)
                manifest_path = stage / f"{base}.manifest.xml"
                manifest_name = manifest_path.name
                write_atomic(manifest_path, manifest)
                try:
                    ts = timestamp(manifest, ts_cfg)
                    if ts is not None:
                        tsr_path = stage / f"{base}.tsr"
                        write_atomic(tsr_path, ts.token)
                        ok, detail = verify(manifest_path, tsr_path, ts_cfg)
                        ts_report.update(
                            kind="RFC 3161 token over the hash manifest", file=tsr_path.name, gen_time=ts.gen_time,
                            serial=ts.serial, tsa=ts.tsa, qualified=ts.qualified, verified=ok, verify_detail=detail,
                        )
                        if not ts.qualified:
                            ts_report["note"] = "NON-QUALIFIED timestamp (test/pilot)"
                except (TimestampError, OSError) as e:
                    if required_ts:
                        raise TimestampError(f"document {seq}: timestamp failed: {e}") from e
                    flags.append(f"timestamp failed: {e}")
                    ts_report["error"] = str(e)
                if required_ts and ts_cfg.get("mode") != "none" and not ts_report.get("verified"):
                    raise TimestampError(f"document {seq}: timestamp did not verify: {ts_report.get('verify_detail')}")
                problems = check_manifest(manifest_path)
                integrity = "ok" if not problems else problems

            documents_report.append(
                {
                    "seq": seq,
                    "file": pdf_path.name,
                    "package": package,
                    "metadata_xml": (f"embedded: {xml_name}" if single else xml_name) if xml_name else None,
                    "manifest": manifest_name,
                    "sha256": pdf_sha,
                    "batch_pages": _page_ranges(doc.pages),
                    "page_count": len(asm),
                    "started_by": doc.started_by,
                    "metadata": meta,
                    "languages": langs,
                    "scripts": scripts,
                    "mean_confidence": mean_conf,
                    "pdfa": {"type": pdfa, "status": pdfa_status},
                    "timestamp": ts_report,
                    "integrity": integrity,
                    "review_flags": flags,
                    "validation": "auto-accepted" if auto else f"confirmed by {confirmed_by}",
                }
            )
            log(f"[{job_id}]   {pdf_path.name}  pages {_page_ranges(doc.pages)}  {'REVIEW: ' + '; '.join(flags) if flags else 'ok'}")

    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    for f in sorted(stage.iterdir()):
        if not f.name.startswith("."):
            os.replace(f, out_dir / f.name)
    shutil.rmtree(stage, ignore_errors=True)

    report = {
        "job_id": job_id,
        "batch": {"file": batch_name, "sha256": batch_sha, "pages": len(results)},
        "profile": profile.name,
        "profile_version": profile.raw.get("config_version"),  # set when the admin console published it
        "versions": versions,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "pages": [
            {
                "page": r.source.index + 1,
                "dpi": r.source.dpi,
                "ink_ratio": r.opt.ink_ratio,
                "blank": r.opt.is_blank,
                "separator": r.facts.separator.raw if r.facts.separator else None,
                "barcodes": [t for _, t in r.facts.barcodes],
                "rotation": r.rotation,
                "script": r.osd.script if r.osd else None,
                "languages": r.ocr.languages if r.ocr else None,
                "confidence": r.ocr.confidence if r.ocr else None,
                "words": r.ocr.words if r.ocr else 0,
                "ocr_attempts": r.ocr.attempts if r.ocr else [],
                "optimiser": r.opt.steps,
                "notes": r.notes,
            }
            for r in results
        ],
        "split": {
            "proposals": [asdict(p) for p in plan.proposals],
            "removed_pages": plan.removed_pages,
            "auto_accepted": auto,
            "operator": ({k: v for k, v in decision.items() if k != "documents"} if decision else None),
        },
        "documents": documents_report,
        "warnings": warnings,
    }
    reports = reports_dir or out_dir / "_reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{batch_stem}.{job_id}.job-report.json"
    write_atomic(report_path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    report["report_file"] = str(report_path)
    return report
