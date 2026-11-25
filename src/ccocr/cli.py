"""Command line: ``ccocr process|watch|separator-sheet|test-rule|verify|init-test-tsa``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def _log(msg: str) -> None:
    print(msg, flush=True)


def cmd_process(a: argparse.Namespace) -> int:
    from .pipeline import process_batch
    from .profile import load_profile

    profile = load_profile(a.profile)
    if a.tsa_dir:
        profile.raw["timestamp"]["openssl_dir"] = a.tsa_dir
    rc = 0
    for f in a.files:
        report = process_batch(Path(f), profile, Path(a.out), workers=a.workers, log=_log)
        review = [d for d in report["documents"] if d["review_flags"]]
        _log(f"{Path(f).name}: {len(report['documents'])} document(s), {len(review)} need review; report {report['report_file']}")
        if report["warnings"]:
            rc = 1
    return rc


def cmd_watch(a: argparse.Namespace) -> int:
    from .watcher import Watcher, load_watch_config

    Watcher(load_watch_config(Path(a.config)), log=_log).run(once=a.once)
    return 0


def cmd_sheet(a: argparse.Namespace) -> int:
    from .sheets import payload, write_sheets

    sheets = []
    for dt in a.doctype:
        code = payload(dt, a.classification or "", a.profile or "")
        sheets.append((code, dt.upper()))
    write_sheets(Path(a.out), sheets, copies=a.copies)
    _log(f"wrote {len(sheets) * a.copies} separator sheet(s) to {a.out}")
    return 0


def cmd_test_rule(a: argparse.Namespace) -> int:
    """Show which pages of a batch a rule matches, to tune it before putting it in a profile."""
    from .ingest import load_batch
    from .pipeline import _analyse_page
    from .profile import load_profile
    from .rules import RuleError, compile_rule

    try:
        rule = compile_rule("test", a.expr)
    except RuleError as e:
        _log(f"rule error: {e}")
        return 2
    profile = load_profile(a.profile)
    profile.raw["split"]["proposals"] = ["text-rule"]
    pages = load_batch(Path(a.file))
    prev_blank = False
    for sp in pages:
        r = _analyse_page(sp, profile, len(pages))
        if r.facts.context is None:
            _log(f"page {sp.index + 1:>3}: {'blank' if r.opt.is_blank else 'no text'}")
            prev_blank = r.opt.is_blank
            continue
        r.facts.context.prev_blank = prev_blank
        hit = rule.matches(r.facts.context)
        first = (r.facts.context.top.splitlines() or [""])[0][:70]
        _log(f"page {sp.index + 1:>3}: {'MATCH' if hit else '  -  '}  [{r.ocr.languages}, {r.ocr.confidence:.0f}%]  {first}")
        prev_blank = False
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    """Check every output in a folder: PDFs with a document timestamp, and manifest + .tsr pairs."""
    from .output import check_manifest
    from .pades import verify_pdf
    from .timestamp import verify

    cfg = {"mode": "openssl-local" if a.tsa_dir else "http", "openssl_dir": a.tsa_dir or "", "ca_file": a.ca or "",
           "chain_file": a.chain or ""}
    bad = checked = 0
    folder = Path(a.dir)
    manifests = sorted(folder.glob("*.manifest.xml"))
    paired = {m.name.replace(".manifest.xml", ".pdf") for m in manifests}
    for pdf in sorted(folder.glob("*.pdf")):
        if pdf.name in paired:
            continue
        ok, detail = verify_pdf(pdf, cfg)
        checked += 1
        bad += not ok
        _log(f"{'OK  ' if ok else 'FAIL'} {pdf.name}: {detail}")
    for m in manifests:
        problems = check_manifest(m)
        tsr = m.with_name(m.name.replace(".manifest.xml", ".tsr"))
        ts_ok, detail = (verify(m, tsr, cfg) if tsr.exists() else (False, "no .tsr"))
        ok = not problems and ts_ok
        checked += 1
        bad += not ok
        _log(f"{'OK  ' if ok else 'FAIL'} {m.name}: hashes {'ok' if not problems else problems}; timestamp {detail}")
    if not checked:
        _log("nothing to verify")
    return 1 if bad else 0


def cmd_extract_xml(a: argparse.Namespace) -> int:
    """Write the metadata XML embedded in a CC OCR PDF next to it (or to stdout)."""
    import pikepdf

    with pikepdf.open(a.pdf) as pdf:
        names = [n for n in pdf.attachments if n.lower().endswith(".xml")]
        if not names:
            _log("no embedded XML")
            return 1
        data = pdf.attachments[names[0]].get_file().read_bytes()
    if a.stdout:
        sys.stdout.buffer.write(data)
    else:
        out = Path(a.pdf).with_suffix(".xml")
        out.write_bytes(data)
        _log(f"wrote {out}")
    return 0


def cmd_fetch_trust(a: argparse.Namespace) -> int:
    from .timestamp import TimestampError, fetch_trust

    try:
        root, chain = fetch_trust(a.root_url, a.root_sha256, Path(a.dir), a.chain_url)
    except (TimestampError, OSError) as e:
        _log(f"FAILED: {e}")
        return 1
    _log(f"root fingerprint matches; wrote {root}" + (f" and {chain}" if chain else ""))
    return 0


def cmd_init_tsa(a: argparse.Namespace) -> int:
    from .timestamp import init_test_tsa

    init_test_tsa(Path(a.dir))
    _log(f"test TSA created in {a.dir} (NOT for production)")
    return 0


def cmd_serve_tsa(a: argparse.Namespace) -> int:
    from .timestamp import serve_test_tsa

    serve_test_tsa(Path(a.dir), a.host, a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ccocr", description="CC OCR scanned-document importer")
    p.add_argument("--version", action="version", version=f"cc-ocr {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("process", help="process batch files once")
    s.add_argument("files", nargs="+")
    s.add_argument("--profile", help="profile YAML")
    s.add_argument("--out", required=True, help="output folder")
    s.add_argument("--workers", type=int)
    s.add_argument("--tsa-dir", help="test TSA directory (openssl-local mode)")
    s.set_defaults(fn=cmd_process)

    s = sub.add_parser("watch", help="watch hot folders")
    s.add_argument("--config", required=True, help="watch config YAML")
    s.add_argument("--once", action="store_true", help="poll once and exit")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("separator-sheet", help="print QR separator sheets")
    s.add_argument("--doctype", action="append", required=True)
    s.add_argument("--classification")
    s.add_argument("--profile")
    s.add_argument("--copies", type=int, default=1)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_sheet)

    s = sub.add_parser("test-rule", help="show which pages a split rule matches")
    s.add_argument("file")
    s.add_argument("--expr", required=True)
    s.add_argument("--profile")
    s.set_defaults(fn=cmd_test_rule)

    s = sub.add_parser("verify", help="check hashes and timestamps in an output folder")
    s.add_argument("dir")
    s.add_argument("--tsa-dir")
    s.add_argument("--ca", help="trusted root certificate")
    s.add_argument("--chain", help="intermediate CA certificate(s)")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("fetch-trust", help="download a TSA root (checked against its SHA-256 pin) and intermediates")
    s.add_argument("--root-url", required=True)
    s.add_argument("--root-sha256", required=True, help="expected fingerprint, e.g. DD:79:4F:...")
    s.add_argument("--chain-url", action="append", default=[])
    s.add_argument("--dir", required=True)
    s.set_defaults(fn=cmd_fetch_trust)

    s = sub.add_parser("extract-xml", help="extract the metadata XML embedded in an output PDF")
    s.add_argument("pdf")
    s.add_argument("--stdout", action="store_true")
    s.set_defaults(fn=cmd_extract_xml)

    s = sub.add_parser("init-test-tsa", help="create a test timestamp authority (not for production)")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_init_tsa)

    s = sub.add_parser("serve-test-tsa", help="run a test RFC 3161 TSA over HTTP (not for production)")
    s.add_argument("dir")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=3180)
    s.set_defaults(fn=cmd_serve_tsa)

    a = p.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
