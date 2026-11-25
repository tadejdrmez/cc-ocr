import shutil

import pytest

from ccocr.profile import load_profile
from ccocr.watcher import WatchConfig, WatchedFolder, Watcher


def make(tmp_path, auto_accept=True, proposals=None, **kw):
    split = {"auto_accept": auto_accept}
    if proposals is not None:
        split["proposals"] = proposals
    prof = load_profile({"timestamp": {"mode": "none"}, "split": split})
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


def _fake_hold(monkeypatch):
    from ccocr import review

    def hold(job_dir, profile, log=None, **kw):
        review.write_json(job_dir / review.PLAN, {"status": "ready", "pages": [{}], "documents": [{"pages": [0]}], **kw,
                                                  "out_dir": str(kw["out_dir"]), "reports_dir": str(kw["reports_dir"]),
                                                  "batch_file": review.batch_file(job_dir).name})
        return {"pages": [{}], "documents": [{"pages": [0]}]}

    monkeypatch.setattr(review, "hold", hold)


def test_auto_accept_off_holds_batch_for_operator(tmp_path, monkeypatch):
    _fake_hold(monkeypatch)
    w = make(tmp_path, auto_accept=False)
    (tmp_path / "in" / "a.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
    assert w.poll_once() == 1
    held = list((tmp_path / "review").iterdir())
    assert len(held) == 1 and held[0].is_dir() and (held[0] / "a.pdf").is_file() and (held[0] / "plan.json").is_file()
    assert not any((tmp_path / "out").iterdir())
    assert w.db.execute("SELECT status FROM batches").fetchone()[0] == "waiting-separation"


def test_hold_that_cannot_be_analysed_goes_to_error(tmp_path):
    w = make(tmp_path, auto_accept=False)
    (tmp_path / "in" / "a.pdf").write_bytes(b"not a pdf")
    (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
    w.poll_once()
    assert not list((tmp_path / "review").iterdir())
    assert "could not be prepared" in (tmp_path / "error" / "a.pdf.error.txt").read_text()


def test_operator_reject_moves_batch_to_error(tmp_path, monkeypatch):
    from ccocr import review

    _fake_hold(monkeypatch)
    w = make(tmp_path, auto_accept=False)
    (tmp_path / "in" / "a.pdf").write_bytes(b"%PDF-1.4 x")
    (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
    w.poll_once()
    job = next((tmp_path / "review").iterdir())
    review.write_json(job / review.DECISION, {"action": "reject", "by": "ana", "reason": "wrong box"})
    w.poll_once()
    assert not job.exists()
    txt = (tmp_path / "error" / "a.pdf.error.txt").read_text()
    assert "rejected" in txt and "ana" in txt and "wrong box" in txt
    assert w.db.execute("SELECT status FROM batches").fetchone()[0] == "rejected"


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


def test_dropped_folder_files_found_hidden_folders_skipped(tmp_path):
    w = make(tmp_path)
    inbox = tmp_path / "in"
    (inbox / "Batch A" / "sub").mkdir(parents=True)
    (inbox / ".hidden").mkdir()
    (inbox / "Batch A" / "a.pdf").write_bytes(b"x")
    (inbox / "Batch A" / "sub" / "b.pdf").write_bytes(b"x")
    (inbox / "Batch A" / "sub" / "b.pdf.done").write_bytes(b"")
    (inbox / ".hidden" / "c.pdf").write_bytes(b"x")
    folder = w.config.folders[0]
    assert [p.name for p in w.ready_files(folder)] == ["b.pdf"]
    w.ready_files(folder)
    assert sorted(p.name for p in w.ready_files(folder)) == ["a.pdf", "b.pdf"]


def test_empty_subfolders_pruned_only_when_quiet(tmp_path):
    import os
    import time

    w = make(tmp_path)
    inbox = tmp_path / "in"
    old = inbox / "done" / "deep"
    old.mkdir(parents=True)
    fresh = inbox / "copying"
    fresh.mkdir()
    past = time.time() - 3600
    for d in (old, old.parent):
        os.utime(d, (past, past))
    w._prune_empty_dirs(w.config.folders[0])
    assert not (inbox / "done").exists()
    assert fresh.exists() and inbox.exists()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_dropped_folder_mirrored_in_output(tmp_path):
    from synth import demo_batch

    w = make(tmp_path)
    sub = tmp_path / "in" / "SDS 2026" / "November"
    sub.mkdir(parents=True)
    demo_batch(sub / "batch.tif")
    (sub / "batch.tif.done").write_bytes(b"")
    assert w.poll_once() == 1
    out_sub = tmp_path / "out" / "SDS 2026" / "November"
    assert len(list(out_sub.glob("*.pdf"))) == 2
    assert list((tmp_path / "out" / "_reports").glob("*.job-report.json"))
    assert not (out_sub / "_reports").exists()
    assert (next((tmp_path / "archive").iterdir()) / "SDS 2026" / "November" / "batch.tif").exists()


def test_waiting_batch_dropped_again_is_not_a_duplicate(tmp_path, monkeypatch):
    _fake_hold(monkeypatch)
    w = make(tmp_path, auto_accept=False)
    for _ in range(2):
        (tmp_path / "in" / "a.pdf").write_bytes(b"%PDF-1.4 x")
        (tmp_path / "in" / "a.pdf.done").write_bytes(b"")
        assert w.poll_once() == 1
    assert len(list((tmp_path / "review").iterdir())) == 2
    assert not list((tmp_path / "error").iterdir())


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_no_split_profile_never_waits_for_operator(tmp_path):
    from synth import demo_batch

    w = make(tmp_path, auto_accept=False, proposals=[])
    demo_batch(tmp_path / "in" / "batch.tif")
    (tmp_path / "in" / "batch.tif.done").write_bytes(b"")
    assert w.poll_once() == 1
    assert not list((tmp_path / "review").iterdir())
    assert len(list((tmp_path / "out").glob("*.pdf"))) == 1


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_separation_screen_round_trip(tmp_path):
    import json

    from synth import demo_batch

    from ccocr import review

    w = make(tmp_path, auto_accept=False)
    demo_batch(tmp_path / "in" / "batch.tif")
    (tmp_path / "in" / "batch.tif.done").write_bytes(b"")
    assert w.poll_once() == 1
    job = next((tmp_path / "review").iterdir())
    plan = review.read_json(job / review.PLAN)
    assert plan["status"] == "ready" and len(plan["pages"]) == 7
    assert plan["pages"][4]["separator"]["doctype"] == "CONTRACT"
    assert (job / "thumbs" / "p0001.jpg").is_file() and (job / "thumbs" / "p0007-l.jpg").is_file()
    assert not list((tmp_path / "out").glob("*.pdf"))

    # The operator keeps both invoices together, drops the blank page and the sheet,
    # and turns the last page upside down (to exercise re-reading a turned page).
    review.write_json(job / review.DECISION, {
        "action": "confirm", "by": "ana", "at": "2026-11-25T10:00:00",
        "documents": [{"pages": [0, 1, 2], "doctype": "INVOICE", "classification": "1.1"},
                      {"pages": [5, 6], "doctype": "CONTRACT", "classification": "4.2.1"}],
        "rotate": {"6": 180},
    })
    w.poll_once()
    assert not job.exists()
    pdfs = sorted((tmp_path / "out").glob("*.pdf"))
    assert len(pdfs) == 2
    report = json.loads(next((tmp_path / "out" / "_reports").glob("*.json")).read_text())
    assert report["split"]["auto_accepted"] is False and report["split"]["operator"]["by"] == "ana"
    assert [d["batch_pages"] for d in report["documents"]] == ["1-3", "6-7"]
    assert all(d["validation"] == "confirmed by ana" for d in report["documents"])
    assert w.db.execute("SELECT status FROM batches").fetchone()[0] == "done"
    assert list((tmp_path / "archive").rglob("batch.tif"))
    assert any("by the operator" in n for p in report["pages"] if p["page"] == 7 for n in p["notes"])
