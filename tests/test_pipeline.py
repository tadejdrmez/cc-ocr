"""End-to-end: synthetic 7-page batch -> 3 timestamped, searchable PDF/A documents."""

import json
import shutil

import pypdfium2 as pdfium
import pytest

from ccocr.output import check_manifest
from ccocr.pipeline import process_batch
from ccocr.profile import load_profile
from ccocr.timestamp import init_test_tsa, verify
from synth import demo_batch

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("e2e")
    batch = d / "batch.tif"
    demo_batch(batch)
    init_test_tsa(d / "tsa")
    profile = load_profile({
        "split": {"rules": [{"name": "invoice", "expr": "top matches /RAČUN\\s+št/i", "doctype": "invoice"}]},
        "timestamp": {"mode": "openssl-local", "openssl_dir": str(d / "tsa")},
        "output": {"metadata": ["sidecar-xml", "filename"], "naming": "{doctype}_{seq}"},
    })
    report = process_batch(batch, profile, d / "out")
    return d, report


def test_documents_split_as_expected(run):
    _, report = run
    assert [d["batch_pages"] for d in report["documents"]] == ["1-2", "3", "6-7"]
    assert [d["file"] for d in report["documents"]] == ["invoice_001.pdf", "invoice_002.pdf", "contract_003.pdf"]


def test_text_layer_is_searchable_in_both_scripts(run):
    d, _ = run
    doc = pdfium.PdfDocument(d / "out" / "contract_003.pdf")
    text = " ".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    assert "УГОВОР" in text
    assert "Ugovorne strane" in text


def test_rotated_page_detected(run):
    _, report = run
    assert report["pages"][6]["rotation"] == 90
    assert report["pages"][5]["script"] == "Cyrillic"


def test_output_is_pdfa(run):
    import pikepdf

    d, _ = run
    with pikepdf.open(d / "out" / "invoice_001.pdf") as pdf:
        meta = pdf.open_metadata()
        assert meta.pdfa_status.startswith("2")


def test_manifest_and_timestamp_verify(run):
    d, report = run
    for doc in report["documents"]:
        m = d / "out" / doc["manifest"]
        assert check_manifest(m) == []
        ok, detail = verify(m, d / "out" / doc["timestamp"]["file"], {"mode": "openssl-local", "openssl_dir": str(d / "tsa")})
        assert ok, detail
        assert doc["timestamp"]["qualified"] is False


def test_tampering_is_detected(run):
    d, report = run
    xml = d / "out" / report["documents"][0]["sidecar"]
    xml.write_bytes(xml.read_bytes().replace(b"invoice", b"invoicX", 1))
    assert check_manifest(d / "out" / report["documents"][0]["manifest"])


def test_report_written(run):
    d, report = run
    data = json.loads((d / "out" / report["report_file"]).read_text(encoding="utf-8"))
    assert data["batch"]["pages"] == 7 and len(data["documents"]) == 3
