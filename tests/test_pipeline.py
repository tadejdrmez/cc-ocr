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
        "output": {"package": "sidecars", "metadata": ["xml", "filename"], "naming": "{doctype}_{seq}"},
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


def test_sidecar_output_is_pdfa(run):
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
    xml = d / "out" / report["documents"][0]["metadata_xml"]
    xml.write_bytes(xml.read_bytes().replace(b"invoice", b"invoicX", 1))
    assert check_manifest(d / "out" / report["documents"][0]["manifest"])


def test_report_written(run):
    d, report = run
    data = json.loads((d / "out" / report["report_file"]).read_text(encoding="utf-8"))
    assert data["batch"]["pages"] == 7 and len(data["documents"]) == 3


# --- single-file output (default): PDF/A-3 + embedded XML + PAdES document timestamp ---


@pytest.fixture(scope="module")
def single(tmp_path_factory):
    d = tmp_path_factory.mktemp("single")
    batch = d / "batch.tif"
    demo_batch(batch)
    init_test_tsa(d / "tsa")
    profile = load_profile({
        "split": {"rules": [{"name": "invoice", "expr": "top matches /RAČUN\\s+št/i", "doctype": "invoice"}]},
        "timestamp": {"mode": "openssl-local", "openssl_dir": str(d / "tsa")},
        "output": {"naming": "{doctype}_{seq}"},
    })
    report = process_batch(batch, profile, d / "out")
    return d, report


def test_single_mode_writes_only_pdfs(single):
    d, report = single
    assert sorted(p.name for p in (d / "out").iterdir() if p.is_file()) == ["contract_003.pdf", "invoice_001.pdf", "invoice_002.pdf"]
    assert (d / "out" / report["report_file"]).exists()


def test_single_mode_pdfa3_with_embedded_xml(single):
    import pikepdf

    from ccocr.output import schema
    from lxml import etree

    d, _ = single
    with pikepdf.open(d / "out" / "invoice_001.pdf") as pdf:
        assert pdf.open_metadata().pdfa_status == "3B"
        assert list(pdf.attachments) == ["invoice_001.xml"]
        spec = pdf.attachments["invoice_001.xml"]
        assert str(spec.obj.get("/AFRelationship")) == "/Data"
        assert len(pdf.Root.AF) == 1
        xml = spec.get_file().read_bytes()
    schema("cc-ocr-metadata-1.xsd").assertValid(etree.fromstring(xml))


def test_single_mode_timestamp_verifies_and_detects_tampering(single):
    from ccocr.pades import verify_pdf

    d, report = single
    cfg = {"mode": "openssl-local", "openssl_dir": str(d / "tsa")}
    pdf = d / "out" / "invoice_002.pdf"
    ok, detail = verify_pdf(pdf, cfg)
    assert ok, detail
    assert report["documents"][1]["timestamp"]["verified"] is True
    data = bytearray(pdf.read_bytes())
    i = data.find(b"/Producer")
    data[i + 1] = ord("p")
    bad = d / "tampered.pdf"
    bad.write_bytes(bytes(data))
    ok, _ = verify_pdf(bad, cfg)
    assert not ok


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_original_scan_streams_are_kept_and_text_aligned(tmp_path):
    """A JPEG scan stays JPEG (no lossless re-encoding), and a page turned by /Rotate keeps
    its text layer on top of the words."""
    import io

    import img2pdf
    import pikepdf
    import pypdfium2 as pdfium
    from PIL import Image, ImageSequence
    from synth import demo_batch

    from ccocr.pipeline import process_batch
    from ccocr.profile import load_profile

    demo_batch(tmp_path / "demo.tif")
    first = next(ImageSequence.Iterator(Image.open(tmp_path / "demo.tif"))).copy().convert("L")
    jpgs = []
    for img in (first, first.rotate(-90, expand=True)):
        b = io.BytesIO()
        img.save(b, "JPEG", quality=80, dpi=(300, 300))
        jpgs.append(b.getvalue())
    src = tmp_path / "scan.pdf"
    src.write_bytes(img2pdf.convert(jpgs))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[1].obj.Rotate = 90
        pdf.save(src)
    prof = load_profile({"timestamp": {"mode": "none"}, "split": {"proposals": [], "auto_accept": True}})
    process_batch(src, prof, tmp_path / "out")
    out = next((tmp_path / "out").glob("*.pdf"))
    assert out.stat().st_size < src.stat().st_size * 1.2
    with pikepdf.open(out) as pdf:
        for page in pdf.pages:
            (img,) = page.images.values()
            filters = img.get("/Filter")
            names = [str(f) for f in filters] if isinstance(filters, pikepdf.Array) else [str(filters)]
            assert "/DCTDecode" in names
    doc = pdfium.PdfDocument(str(out))
    for i in range(2):
        tp = doc[i].get_textpage()
        assert "RAČUN" in tp.get_text_range()
    doc.close()
