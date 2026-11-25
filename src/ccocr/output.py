"""Output-folder target (spec 2.11): PDF/A + sidecar XML + hash manifest + .tsr.

Files are written under a temporary name and renamed when complete, so a
downstream system polling the folder never sees half-written files.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from lxml import etree

MD_NS = "urn:cc:ocr:metadata:1"
MF_NS = "urn:cc:ocr:manifest:1"

_schemas: dict[str, etree.XMLSchema] = {}


def schema(name: str) -> etree.XMLSchema:
    if name not in _schemas:
        data = resources.files("ccocr").joinpath("schemas", name).read_bytes()
        _schemas[name] = etree.XMLSchema(etree.fromstring(data))
    return _schemas[name]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


@dataclass
class SidecarInput:
    pdf_name: str
    pdf_sha256: str
    page_count: int
    profile: str
    validation_status: str
    validated_by: str
    validated_at: datetime
    doctype: str
    classification: str
    batch_name: str
    batch_sha256: str
    batch_pages: str
    fields: list[dict[str, Any]]
    ocr_engine: str
    languages: str
    scripts: str
    image: str
    pdfa: str
    mean_confidence: float | None


def build_sidecar(s: SidecarInput) -> bytes:
    q = lambda t: f"{{{MD_NS}}}{t}"  # noqa: E731
    root = etree.Element(q("document"), nsmap={"ccocr": MD_NS}, schemaVersion="1.0")
    etree.SubElement(root, q("file"), name=s.pdf_name, sha256=s.pdf_sha256, pages=str(s.page_count))
    etree.SubElement(root, q("profile")).text = s.profile
    v = etree.SubElement(root, q("validation"), status=s.validation_status, at=s.validated_at.isoformat(timespec="seconds"))
    if s.validated_by:
        v.set("confirmedBy", s.validated_by)
    etree.SubElement(root, q("documentType")).text = s.doctype
    if s.classification:
        etree.SubElement(root, q("classification")).text = s.classification
    etree.SubElement(root, q("source"), batch=s.batch_name, sha256=s.batch_sha256, pages=s.batch_pages)
    for f in s.fields:
        el = etree.SubElement(root, q("field"), name=f["name"], type=f.get("type", "text"))
        if f.get("source"):
            el.set("source", f["source"])
        if f.get("confidence") is not None:
            el.set("confidence", f"{f['confidence']:.1f}")
        el.text = str(f["value"])
    proc = etree.SubElement(
        root, q("processing"),
        ocr=s.ocr_engine, languages=s.languages, scripts=s.scripts, pages=str(s.page_count), image=s.image, pdfa=s.pdfa,
    )
    if s.mean_confidence is not None:
        proc.set("meanConfidence", f"{s.mean_confidence:.1f}")
    schema("cc-ocr-metadata-1.xsd").assertValid(root)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_manifest(job_id: str, profile: str, created: datetime, entries: list[tuple[str, str, bytes]]) -> bytes:
    q = lambda t: f"{{{MF_NS}}}{t}"  # noqa: E731
    root = etree.Element(
        q("manifest"), nsmap={"m": MF_NS}, schemaVersion="1.0", jobId=job_id, profile=profile,
        created=created.isoformat(timespec="seconds"),
    )
    for name, role, data in entries:
        etree.SubElement(root, q("entry"), name=name, role=role, sha256=sha256_bytes(data), size=str(len(data)))
    schema("cc-ocr-manifest-1.xsd").assertValid(root)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def check_manifest(manifest_path: Path) -> list[str]:
    """Recompute every hash listed in a manifest; return a list of problems (empty = OK)."""
    problems = []
    root = etree.parse(str(manifest_path)).getroot()
    for e in root.findall(f"{{{MF_NS}}}entry"):
        p = manifest_path.parent / e.get("name")
        if not p.exists():
            problems.append(f"missing {p.name}")
            continue
        if sha256_bytes(p.read_bytes()) != e.get("sha256"):
            problems.append(f"hash mismatch for {p.name}")
    return problems
