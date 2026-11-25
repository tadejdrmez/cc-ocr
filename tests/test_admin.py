"""Admin console: sign-in, profile drafts and publishing, export for the watcher, audit."""

import re
import shutil
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ccocr.admin.app import create_app  # noqa: E402
from ccocr.watcher import load_watch_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PW = "Correct-horse-42"


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture()
def client(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    shutil.copy(ROOT / "profiles" / "example.yaml", seed / "example.yaml")
    (seed / "watch.yaml").write_text(f"root: {tmp_path}\nfolders:\n  - in: {tmp_path}/in\n    out: {tmp_path}/out\n    profile: example.yaml\n")
    c = TestClient(create_app(tmp_path, seed / "watch.yaml"))
    code = (tmp_path / ".ccocr" / "setup-token").read_text().strip()
    r = c.post("/setup", data={"csrf": _csrf(c.get("/setup").text), "code": code, "name": "Tadej", "login": "tadej",
                               "password": PW, "password2": PW}, follow_redirects=True)
    r = c.post("/login", data={"csrf": _csrf(r.text), "login": "tadej", "password": PW}, follow_redirects=True)
    assert r.url.path == "/"
    c.data_dir = tmp_path
    c.token = _csrf(r.text)
    return c


def test_setup_requires_code(tmp_path):
    c = TestClient(create_app(tmp_path))
    r = c.post("/setup", data={"csrf": _csrf(c.get("/setup").text), "code": "wrong", "name": "x", "login": "x",
                               "password": PW, "password2": PW})
    assert r.status_code == 400 and "setup code is wrong" in r.text


def test_requires_sign_in_and_csrf(tmp_path):
    c = TestClient(create_app(tmp_path))
    assert c.get("/profiles", follow_redirects=False).status_code == 303


def test_wrong_password_locks_after_five(client):
    client.post("/logout", data={"csrf": client.token})
    for _ in range(5):
        t = _csrf(client.get("/login").text)
        client.post("/login", data={"csrf": t, "login": "tadej", "password": "nope"})
    t = _csrf(client.get("/login").text)
    r = client.post("/login", data={"csrf": t, "login": "tadej", "password": PW})
    assert "Too many failed attempts" in r.text


@pytest.mark.parametrize("path", ["/", "/jobs", "/profiles", "/profiles/incoming-mixed", "/profiles/incoming-mixed?tab=rules",
                                  "/profiles/incoming-mixed?tab=split", "/profiles/incoming-mixed?tab=output",
                                  "/profiles/incoming-mixed?tab=history", "/folders", "/folders/1", "/folders/new", "/rules",
                                  "/naming", "/sheets", "/types", "/trust", "/users", "/audit", "/system"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "<h1>" in r.text


def test_seed_import_and_export(client):
    w = load_watch_config(client.data_dir / ".ccocr" / "config" / "watch.yaml")
    assert len(w.folders) == 1 and w.folders[0].profile.name == "incoming-mixed"


def test_draft_publish_cycle_and_audit(client):
    t = client.token
    r = client.post("/profiles/incoming-mixed/section/ocr", data={
        "csrf": t, "ocr.osd": "on", "ocr.latin": "slv eng", "ocr.cyrillic": "srp srp_latn", "ocr.default": "slv eng",
        "ocr.min_confidence": "70", "ocr.output": "auto"}, follow_redirects=True)
    assert "Saved in draft v2" in r.text
    exported = client.data_dir / ".ccocr" / "config" / "profiles" / "incoming-mixed.yaml"
    assert yaml.safe_load(exported.read_text())["ocr"]["min_confidence"] == 60  # draft is not live yet
    client.post("/profiles/incoming-mixed/publish", data={"csrf": t, "comment": "x"})
    live = yaml.safe_load(exported.read_text())
    assert live["ocr"]["min_confidence"] == 70 and live["ocr"]["latin"] == ["slv", "eng"] and live["config_version"] == 2
    audit = client.get("/audit.csv").text
    assert "Edit profile draft" in audit and "Publish profile" in audit


def test_unknown_language_and_bad_rule_rejected(client):
    t = client.token
    r = client.post("/profiles/incoming-mixed/section/ocr", data={
        "csrf": t, "ocr.latin": "slv klingon", "ocr.cyrillic": "srp", "ocr.default": "slv", "ocr.min_confidence": "60",
        "ocr.output": "auto"}, follow_redirects=True)
    assert "not installed: klingon" in r.text
    r = client.post("/profiles/incoming-mixed/rules", data={"csrf": t, "op": "add", "name": "x", "expr": "top contains"},
                    follow_redirects=True)
    assert "Rule not saved" in r.text


def test_auto_accept_needs_reason(client):
    t = client.token
    r = client.post("/profiles/new", data={"csrf": t, "name": "fresh"}, follow_redirects=True)
    assert "Accept split proposals automatically" in client.get("/profiles/fresh?tab=split").text
    form = {"csrf": t, "split.proposals": ["qr-separator"], "split.zone": "25", "split.fixed_count": "",
            "split.drop_blank_pages": "on", "split.auto_accept": "on"}
    r = client.post("/profiles/fresh/section/split", data=form, follow_redirects=True)
    assert "needs a reason" in r.text
    r = client.post("/profiles/fresh/section/split", data={**form, "reason": "uniform forms"}, follow_redirects=True)
    assert "Saved in draft" in r.text
    assert "uniform forms" in client.get("/audit.csv").text


def test_new_profile_defaults_to_operator_confirmation(client):
    client.post("/profiles/new", data={"csrf": client.token, "name": "fresh2"})
    client.post("/profiles/fresh2/publish", data={"csrf": client.token})
    data = yaml.safe_load((client.data_dir / ".ccocr" / "config" / "profiles" / "fresh2.yaml").read_text())
    assert data["split"]["auto_accept"] is False


def test_users_and_last_admin(client):
    t = client.token
    r = client.post("/users/new", data={"csrf": t, "login": "op1", "name": "Operator", "roles": ["operator"]}, follow_redirects=True)
    assert "Temporary password" in r.text
    r = client.post("/users/1", data={"csrf": t, "op": "save", "name": "Tadej", "roles": ["operator"], "active": "on"},
                    follow_redirects=True)
    assert "last active administrator" in r.text


def test_rule_test_and_naming_preview(client):
    r = client.post("/rules/test", data={"csrf": client.token, "expr": 'top matches /RAČUN/i', "text": "Račun št. 1\nx", "page": "2"})
    assert r.json() == {"ok": True, "match": True, "top": "Račun št. 1", "bottom": "x", "words": 4}
    r = client.post("/naming/preview", data={"csrf": client.token, "pattern": "{doctype}_{issuer}", "max_length": "120"})
    assert r.json()["name"].startswith("invoice_Mlekarna-Celje-Šentjur")


def test_tsa_credentials_go_to_secrets_file(client, monkeypatch):
    client.post("/trust/credentials", data={"csrf": client.token, "tsa_user": "ccocr", "tsa_password": "s3cret!"})
    secrets = client.data_dir / ".ccocr" / "secrets.json"
    assert (secrets.stat().st_mode & 0o777) == 0o600
    assert "s3cret" not in client.get("/audit.csv").text and "s3cret" not in client.get("/trust").text
    from ccocr import timestamp

    monkeypatch.delenv("CCOCR_TSA_USER", raising=False)
    monkeypatch.setenv("CCOCR_SECRETS_FILE", str(secrets))
    assert timestamp.tsa_credentials() == ("ccocr", "s3cret!")


def test_folder_accepts_host_path(client, monkeypatch):
    d = client.data_dir
    monkeypatch.setenv("CCOCR_HOST_MOUNTS", f"C:\\cc-ocr\\data={d};C:\\OCR_Work_SDS={d}/sds")
    form = {"csrf": client.token, "name": "SDS", "in": r"C:\OCR_Work_SDS\data\in", "out": r"C:\OCR_Work_SDS\data\out",
            "profile": "incoming-mixed", "duplicate": "error"}
    r = client.post("/folders/save", data=form, follow_redirects=True)
    assert "Folder saved" in r.text and r"C:\OCR_Work_SDS\data\in" in r.text
    w = yaml.safe_load((d / ".ccocr" / "config" / "watch.yaml").read_text())
    assert {"in": f"{d}/sds/data/in", "out": f"{d}/sds/data/out"}.items() <= w["folders"][1].items()
    r = client.post("/folders/save", data={**form, "in": r"D:\Nope\in"})
    assert r.status_code == 400 and "not shared with the container" in r.text


def _held_job(data_dir, job="20261125-100000-scan"):
    import json

    d = data_dir / "review" / job
    (d / "thumbs").mkdir(parents=True)
    (d / "scan.pdf").write_bytes(b"%PDF-1.4 x")
    for i in range(1, 5):
        (d / "thumbs" / f"p{i:04d}.jpg").write_bytes(b"\xff\xd8jpg")
    page = {"blank": False, "ink": 0.05, "separator": None, "barcodes": [], "rotation": 0, "words": 80,
            "confidence": 91.0, "languages": "slv+eng", "top": "RAČUN", "text": "RAČUN 1", "notes": [], "flags": []}
    plan = {"job_id": job, "batch_name": "scan.pdf", "batch_file": "scan.pdf", "sha256": "x", "status": "ready",
            "held_at": "2026-11-25T10:00:00", "folder": "Main", "rel": "", "out_dir": "/tmp/o", "reports_dir": "/tmp/o/_reports",
            "profile": {"name": "example", "raw": {}},
            "pages": [{**page, "index": i} for i in range(4)],
            "proposals": [{"before_page": 2, "source": "text-rule", "reason": "rule 'invoice'", "accepted": False, "accepted_by": ""}],
            "documents": [{"pages": [0, 1], "doctype": "invoice", "classification": "", "review_flags": [], "started_by": "start"},
                          {"pages": [2, 3], "doctype": "invoice", "classification": "", "review_flags": [], "started_by": "rule"}],
            "removed_pages": [], "document_types": []}
    (d / "plan.json").write_text(json.dumps(plan))
    return d


def _operator(client):
    from ccocr.admin.security import hash_password

    store = client.app.state.store
    store.add_user("ana", "Ana", hash_password(PW), ["operator"], must_change=False)
    client.post("/logout", data={"csrf": client.token})
    r = client.post("/login", data={"csrf": _csrf(client.get("/login").text), "login": "ana", "password": PW}, follow_redirects=True)
    client.token = _csrf(r.text)
    return client


def test_separation_needs_operator_when_duties_are_separated(client):
    d = _held_job(client.data_dir)
    r = client.get("/review")
    assert "scan.pdf" in r.text and "Separation" in r.text
    r = client.get(f"/review/{d.name}", follow_redirects=True)
    assert r.url.path == "/review" and "needs the Operator role" in r.text


def test_operator_confirms_changed_split(client):
    import json

    d = _held_job(client.data_dir)
    c = _operator(client)
    r = c.get(f"/review/{d.name}")
    assert r.status_code == 200 and 'id="plan-data"' in r.text
    assert c.get(f"/review/{d.name}/page/1.jpg").content.startswith(b"\xff\xd8")
    assert c.get(f"/review/{d.name}/page/9.jpg").status_code == 404
    assert c.get("/review/..%2F..%2Fetc/page/1.jpg").status_code in (303, 404)
    layout = {"documents": [{"pages": [0, 1, 2], "doctype": "contract", "classification": "4.2.1"}], "rotate": {"1": 90}}
    r = c.post(f"/review/{d.name}/confirm", data={"csrf": c.token, "layout": json.dumps(layout), "note": "one contract"},
               follow_redirects=True)
    assert "1 document(s) released" in r.text
    dec = json.loads((d / "decision.json").read_text())
    assert dec["by"] == "ana" and dec["removed"] == [3] and dec["rotate"] == {"1": 90}
    assert dec["documents"][0]["doctype"] == "contract"
    # second release is refused, and the audit log has the change
    r = c.post(f"/review/{d.name}/confirm", data={"csrf": c.token, "layout": json.dumps(layout)}, follow_redirects=True)
    assert "no longer waiting" in r.text
    rows = c.app.state.store.audit_rows(action="Confirm separation")
    assert rows and rows[0]["user"] == "ana" and rows[0]["reason"] == "one contract"


def test_operator_layout_is_checked_and_reject_needs_reason(client):
    import json

    d = _held_job(client.data_dir)
    c = _operator(client)
    bad = {"documents": [{"pages": [0, 1]}, {"pages": [1, 2]}]}
    r = c.post(f"/review/{d.name}/confirm", data={"csrf": c.token, "layout": json.dumps(bad)}, follow_redirects=True)
    assert "could not be read" in r.text and not (d / "decision.json").exists()
    r = c.post(f"/review/{d.name}/reject", data={"csrf": c.token, "reason": ""}, follow_redirects=True)
    assert "Write why" in r.text and not (d / "decision.json").exists()
    c.post(f"/review/{d.name}/reject", data={"csrf": c.token, "reason": "pages missing"})
    dec = json.loads((d / "decision.json").read_text())
    assert dec == {**dec, "action": "reject", "by": "ana", "reason": "pages missing"}


def test_store_survives_parallel_requests(client):
    from concurrent.futures import ThreadPoolExecutor

    store = client.app.state.store

    def hit(_):
        return store.user_count(), store.user_by_id(1)["login"], store.get("system")["session_minutes"]

    with ThreadPoolExecutor(16) as pool:
        assert set(pool.map(hit, range(2000))) == {(1, "tadej", 30)}


def test_separation_opens_jobs_named_after_any_file(client):
    from urllib.parse import quote

    c = _operator(client)
    for job in ("20261125-100000-račun_št", "20261125-100001-scan(1) #2"):
        d = _held_job(client.data_dir, job)
        assert f'href="/review/{quote(job)}"' in c.get("/review").text
        r = c.get(f"/review/{quote(job)}")
        assert r.status_code == 200 and 'id="plan-data"' in r.text, job
        assert c.get(f"/review/{quote(job)}/page/1.jpg").status_code == 200
        r = c.post(f"/review/{quote(job)}/reject", data={"csrf": c.token, "reason": "test"}, follow_redirects=True)
        assert (d / "decision.json").exists()
    (client.data_dir / "elsewhere").mkdir()
    for bad in ("..", "..%2Felsewhere", "%2E%2E"):
        r = c.get(f"/review/{bad}", follow_redirects=True)
        assert r.url.path in ("/review", "/") or r.status_code == 404
