import shutil

import pytest

from ccocr.profile import load_profile
from ccocr.watcher import WatchConfig, WatchedFolder, Watcher


def make(tmp_path, auto_accept=True, **kw):
    prof = load_profile({"timestamp": {"mode": "none"}, "split": {"auto_accept": auto_accept}})
    folder_kw = {k: kw.pop(k) for k in list(kw) if k in ("paused", "file_types", "max_mb", "duplicate", "require_marker")}
    cfg = WatchConfig(
        root=tmp_path,
        folders=[WatchedFolder(tmp_path / "in", tmp_path / "out", prof, **folder_kw)],
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


def test_auto_accept_off_holds_batch_for_operator(tmp_path):
    w = make(tmp_path, auto_accept=False)
    (tmp_path / "in" / "a.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
    assert w.poll_once() == 1
    held = list((tmp_path / "review").iterdir())
    assert len(held) == 1 and held[0].name.endswith("__a.pdf")
    assert not any((tmp_path / "out").iterdir())
    assert w.db.execute("SELECT status FROM batches").fetchone()[0] == "waiting-separation"


def test_folder_overrides_types_size_and_pause(tmp_path):
    w = make(tmp_path, file_types=(".tif",), max_mb=0.000001)
    folder = w.config.folders[0]
    (tmp_path / "in" / "a.pdf").write_bytes(b"x")
    (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
    (tmp_path / "in" / "b.tif").write_bytes(b"x" * 100)
    (tmp_path / "in" / "b.tif.done").write_bytes(b"")
    assert [p.name for p in w.ready_files(folder)] == ["b.tif"]
    w.poll_once()
    assert (tmp_path / "error" / "b.tif.error.txt").read_text().startswith("File is larger")
    folder.paused = True
    (tmp_path / "in" / "c.tif").write_bytes(b"x")
    (tmp_path / "in" / "c.tif.done").write_bytes(b"")
    assert w.poll_once() == 0


def test_config_reload(tmp_path):
    from ccocr.watcher import load_watch_config

    cfg = tmp_path / "watch.yaml"
    cfg.write_text(f"root: {tmp_path}\nfolders:\n  - in: {tmp_path}/in\n    out: {tmp_path}/out\n")
    w = Watcher(load_watch_config(cfg), log=lambda m: None)
    m = w._reload_if_changed(None)
    import os
    cfg.write_text(cfg.read_text() + f"  - name: second\n    in: {tmp_path}/in2\n    out: {tmp_path}/out2\n    paused: true\n")
    os.utime(cfg, (m + 5, m + 5))
    w._reload_if_changed(m)
    assert [f.name for f in w.config.folders] == ["", "second"] and w.config.folders[1].paused
    assert (tmp_path / "in2").is_dir()
