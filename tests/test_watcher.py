import shutil

import pytest

from ccocr.profile import load_profile
from ccocr.watcher import WatchConfig, WatchedFolder, Watcher


def make(tmp_path, **kw):
    cfg = WatchConfig(
        root=tmp_path,
        folders=[WatchedFolder(tmp_path / "in", tmp_path / "out", load_profile({"timestamp": {"mode": "none"}}))],
        stable_polls=3, **kw,
    )
    return Watcher(cfg, log=lambda m: None)


def test_file_ready_only_after_stable_polls(tmp_path):
    w = make(tmp_path)
    f = tmp_path / "in" / "scan.pdf"
    f.write_bytes(b"%PDF-1.4 partial")
    folder = w.config.folders[0]
    assert w.ready_files(folder) == []
    assert w.ready_files(folder) == []
    assert w.ready_files(folder) == [f]


def test_growing_file_resets_counter(tmp_path):
    w = make(tmp_path)
    f = tmp_path / "in" / "scan.pdf"
    folder = w.config.folders[0]
    f.write_bytes(b"a")
    w.ready_files(folder)
    w.ready_files(folder)
    f.write_bytes(b"ab")
    assert w.ready_files(folder) == []


def test_marker_file_makes_ready_immediately_and_temp_files_ignored(tmp_path):
    w = make(tmp_path)
    (tmp_path / "in" / "a.pdf").write_bytes(b"x")
    (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
    (tmp_path / "in" / "b.pdf.part").write_bytes(b"x")
    (tmp_path / "in" / "~c.pdf").write_bytes(b"x")
    assert [p.name for p in w.ready_files(w.config.folders[0])] == ["a.pdf"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_end_to_end_and_duplicate(tmp_path):
    from synth import demo_batch

    w = make(tmp_path)
    src = tmp_path / "batch.tif"
    demo_batch(src)
    shutil.copy(src, tmp_path / "in" / "batch.tif")
    (tmp_path / "in" / "batch.tif.done").write_bytes(b"")
    assert w.poll_once() == 1
    # no text rules in this profile: the two invoices stay together, the QR sheet splits off the contract
    assert sorted(p.name for p in (tmp_path / "out").glob("*.pdf")) == ["contract_batch_002.pdf", "document_batch_001.pdf"]
    assert len(list((tmp_path / "archive").rglob("batch.tif"))) == 1
    shutil.copy(src, tmp_path / "in" / "batch.tif")
    (tmp_path / "in" / "batch.tif.done").write_bytes(b"")
    w.poll_once()
    assert (tmp_path / "error" / "batch.tif.error.txt").read_text().startswith("Duplicate")
