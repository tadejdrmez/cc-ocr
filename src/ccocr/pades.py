"""Single-file output: PDF/A-3 with embedded metadata and a PAdES document timestamp.

Operators get one file per document. The sidecar metadata XML travels inside
the PDF as a PDF/A-3 associated file (``/AFRelationship /Data``), and an
RFC 3161 document timestamp (``/SubFilter /ETSI.RFC3161``, PAdES) is appended
as an incremental update. It covers every byte before it, including the
embedded XML, so the separate hash manifest and ``.tsr`` are not needed.
Any PDF reader (Adobe Acrobat/Reader, Foxit) shows and verifies it.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pikepdf
from asn1crypto import tsp
from pikepdf import AttachedFileSpec, Name

from .timestamp import TimestampError


def embed_files(pdf_path: Path, files: list[tuple[str, bytes, str, str]]) -> None:
    """Embed (name, data, mime, description) as PDF/A-3 associated files (in place)."""
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        now = datetime.now(timezone.utc)
        af = pdf.Root.get("/AF", pikepdf.Array())
        for name, data, mime, desc in files:
            spec = AttachedFileSpec(
                pdf, data, description=desc, filename=name, mime_type=mime,
                creation_date=now.strftime("D:%Y%m%d%H%M%SZ"), mod_date=now.strftime("D:%Y%m%d%H%M%SZ"),
                relationship=Name.Data,
            )
            pdf.attachments[name] = spec
            af.append(spec.obj)
        pdf.Root.AF = af
        pdf.save(pdf_path)


def _timestamper(cfg: dict[str, Any]):
    from pyhanko.sign.timestamps.api import TimeStamper

    mode = cfg.get("mode")

    class _Openssl(TimeStamper):
        async def async_request_tsa_response(self, req: tsp.TimeStampReq) -> tsp.TimeStampResp:
            tsa_dir = Path(cfg.get("openssl_dir") or os.environ.get("CCOCR_TEST_TSA_DIR", ""))
            conf = tsa_dir / "tsa.cnf"
            if not conf.exists():
                raise TimestampError(f"test TSA not initialised in {tsa_dir!s}")
            with tempfile.TemporaryDirectory() as td:
                q, r = Path(td) / "q.tsq", Path(td) / "r.tsr"
                q.write_bytes(req.dump())
                res = subprocess.run(
                    ["openssl", "ts", "-reply", "-config", str(conf), "-queryfile", str(q), "-out", str(r)],
                    capture_output=True, text=True, cwd=tsa_dir,
                )
                if res.returncode != 0:
                    raise TimestampError(f"openssl ts -reply failed: {res.stderr.strip()}")
                return tsp.TimeStampResp.load(r.read_bytes())

    class _Http(TimeStamper):
        async def async_request_tsa_response(self, req: tsp.TimeStampReq) -> tsp.TimeStampResp:
            url = cfg.get("url")
            if not url:
                raise TimestampError("timestamp.url is not set")
            r = urllib.request.Request(url, data=req.dump(), headers={"Content-Type": "application/timestamp-query"})
            user, pw = os.environ.get("CCOCR_TSA_USER"), os.environ.get("CCOCR_TSA_PASSWORD")
            if user and pw:
                import base64

                r.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: urllib.request.urlopen(r, timeout=float(cfg.get("timeout", 30))).read())
            return tsp.TimeStampResp.load(data)

    if mode == "openssl-local":
        return _Openssl()
    if mode == "http":
        return _Http()
    raise TimestampError(f"timestamp mode {mode!r} cannot timestamp a PDF")


def _load_certs(path: str) -> list[Any]:
    from asn1crypto import pem, x509

    if not path:
        return []
    data = Path(path).read_bytes()
    if pem.detect(data):
        return [x509.Certificate.load(der) for _, _, der in pem.unarmor(data, multiple=True)]
    return [x509.Certificate.load(data)]


def trust_material(cfg: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """(trust roots, intermediate certificates) from the timestamp config."""
    ca = cfg.get("ca_file") or ""
    tsa_dir = cfg.get("openssl_dir") or os.environ.get("CCOCR_TEST_TSA_DIR", "")
    if not ca and cfg.get("mode") == "openssl-local" and tsa_dir and (Path(tsa_dir) / "ca.pem").exists():
        ca = str(Path(tsa_dir) / "ca.pem")
    return _load_certs(ca), _load_certs(cfg.get("chain_file") or "")


def add_document_timestamp(pdf_path: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """Append a PAdES document timestamp (ETSI.RFC3161) to the PDF in place.

    With ``embed_validation: true`` the TSA's certificate chain and its
    OCSP/CRL answers are fetched now and stored in the PDF's DSS, so the
    timestamp still validates later without the CA being reachable.
    """
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko_certvalidator import ValidationContext

    ts = _timestamper(cfg)
    vc = None
    if cfg.get("embed_validation"):
        roots, others = trust_material(cfg)
        if not roots:
            raise TimestampError("embed_validation needs timestamp.ca_file (the trusted root)")
        vc = ValidationContext(trust_roots=roots, other_certs=others, allow_fetching=True)
    with open(pdf_path, "rb") as f:
        writer = IncrementalPdfFileWriter(f, strict=False)
        out = io.BytesIO()
        signers.PdfTimeStamper(ts, field_name="CC-OCR-Timestamp").timestamp_pdf(
            writer, "sha256", validation_context=vc, output=out
        )
    tmp = pdf_path.with_name(f".{pdf_path.name}.ts")
    tmp.write_bytes(out.getvalue())
    os.replace(tmp, pdf_path)
    info = read_timestamp(pdf_path)
    info["qualified"] = bool(cfg.get("qualified"))
    info["validation_data_embedded"] = vc is not None
    return info


def read_timestamp(pdf_path: Path) -> dict[str, Any]:
    """Time, serial and TSA name of the document timestamp in the PDF."""
    from pyhanko.pdf_utils.reader import PdfFileReader

    with open(pdf_path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        sigs = [s for s in r.embedded_signatures if s.sig_object.get("/SubFilter") == "/ETSI.RFC3161"]
        if not sigs:
            return {}
        content = sigs[-1].signed_data["encap_content_info"]["content"].parsed
        tsa = ""
        try:
            tsa = content["tsa"].chosen.human_friendly
        except Exception:  # noqa: BLE001
            pass
        return {"gen_time": content["gen_time"].native.isoformat(), "serial": int(content["serial_number"].native), "tsa": tsa}


def verify_pdf(pdf_path: Path, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Check the document timestamp offline: covered bytes intact, token valid, chain to the configured root."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_timestamp
    from pyhanko_certvalidator import ValidationContext

    roots, others = trust_material(cfg)
    vc = ValidationContext(trust_roots=roots, other_certs=others, allow_fetching=False)
    with open(pdf_path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        sigs = [s for s in r.embedded_signatures if s.sig_object.get("/SubFilter") == "/ETSI.RFC3161"]
        if not sigs:
            return False, "no document timestamp"
        status = validate_pdf_timestamp(sigs[-1], validation_context=vc)
        covers_all = status.coverage.name in ("ENTIRE_FILE", "ENTIRE_REVISION")
        ok = status.intact and status.valid and covers_all and (status.trusted or not roots)
        detail = f"intact={status.intact} valid={status.valid} trusted={status.trusted} coverage={status.coverage.name}"
        if not roots:
            detail += " (no CA configured: chain not checked)"
        return ok, detail
