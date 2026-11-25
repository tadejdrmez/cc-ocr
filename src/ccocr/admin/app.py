"""CC OCR admin web console (spec 2.7, 9.2-9.5).

Server-rendered pages (FastAPI + Jinja2), no external assets, so the console
works on an offline server. Sign-in with local accounts; roles Administrator,
Operator and Reviewer. Every change is written to the append-only audit log.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..naming import NamingError, build_name
from ..profile import ProfileError
from ..rules import PageContext, RuleError, compile_rule
from . import forms, mounts
from .i18n import LANGUAGES, translator
from .security import check_password, hash_password, new_token, password_problem, temp_password
from .store import FOLDER_DEFAULTS, ROLES, Store, now_iso, profile_yaml

HERE = Path(__file__).parent
ROLE_LABELS = {"admin": "Administrator", "operator": "Operator", "reviewer": "Reviewer"}
LOCK_AFTER = 5
LOCK_MINUTES = 5


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def create_app(data_dir: Path, seed_config: Path | None = None) -> FastAPI:
    store = Store(data_dir)
    notes = store.seed_from(seed_config) if seed_config else []
    if not (store.config_dir / "watch.yaml").exists():
        store.export()
    key_file = store.state / "session.key"
    if not key_file.exists():
        key_file.write_text(new_token(32))
        os.chmod(key_file, 0o600)
    setup_file = store.state / "setup-token"
    if store.user_count() == 0 and not setup_file.exists():
        setup_file.write_text(new_token(12))
        os.chmod(setup_file, 0o600)

    app = FastAPI(title="CC OCR admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.seed_notes = notes
    app.add_middleware(SessionMiddleware, secret_key=key_file.read_text().strip(), session_cookie="ccocr_session",
                       same_site="strict", https_only=os.environ.get("CCOCR_HTTPS_ONLY") == "1", max_age=12 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(ROLE_LABELS=ROLE_LABELS, version=__version__, SECTIONS=forms.SECTIONS,
                                 display_value=forms.display_value, get_path=forms.get_path, LANGUAGES=LANGUAGES)

    if store.user_count() == 0:
        print(f"CC OCR admin: no users yet. Open /setup and use the setup code: {setup_file.read_text().strip()}", flush=True)

    # -- helpers -------------------------------------------------------------
    def current_user(request: Request) -> sqlite3.Row | None:
        uid = request.session.get("uid")
        if not uid:
            return None
        u = store.user_by_id(uid)
        if not u or not u["active"]:
            request.session.clear()
            return None
        minutes = int(store.get("system")["session_minutes"])
        last = request.session.get("seen", 0)
        if time.time() - last > minutes * 60:
            request.session.clear()
            return None
        request.session["seen"] = int(time.time())
        return u

    def roles_of(u: sqlite3.Row) -> list[str]:
        return [r for r in u["roles"].split(",") if r]

    def need(request: Request, *roles: str) -> sqlite3.Row:
        u = current_user(request)
        if u is None:
            raise Redirect("/login?next=" + quote(str(request.url.path)))
        if u["must_change"] and request.url.path not in ("/account/password", "/logout"):
            raise Redirect("/account/password")
        if roles and not set(roles) & set(roles_of(u)):
            raise Redirect("/?denied=1")
        return u

    def actor(u: sqlite3.Row) -> tuple[str, str]:
        return u["login"], "+".join(ROLE_LABELS[r] for r in roles_of(u))

    def flash(request: Request, msg: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append([kind, msg])

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = request.session["csrf"] = new_token()
        return tok

    async def form_data(request: Request):  # noqa: ANN202
        form = await request.form()
        if form.get("csrf") != request.session.get("csrf"):
            raise Redirect(str(request.url.path) + "?csrf=1")
        return form

    def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200) -> HTMLResponse:
        u = current_user(request) if "uid" in request.session else None
        lang = store.get("system")["language"]
        c = {
            "request": request, "user": u, "roles": roles_of(u) if u else [], "csrf": csrf_token(request),
            "flashes": request.session.pop("flash", []), "_": translator(lang), "lang": lang,
            "trust_mode": store.get("trust")["mode"], "trust_qualified": store.get("trust")["qualified"],
            "path": request.url.path, "nav_counts": nav_counts(),
        }
        if request.query_params.get("denied"):
            c["flashes"].append(["bad", "Your role does not allow that page."])
        if request.query_params.get("csrf"):
            c["flashes"].append(["bad", "The form expired. Please try again."])
        c.update(ctx or {})
        return templates.TemplateResponse(request, name, c, status_code=status)

    def nav_counts() -> dict[str, int]:
        root = store.data
        def count(d: str, pattern: str = "*") -> int:
            p = root / d
            return sum(1 for x in p.glob(pattern) if x.is_file() and not x.name.endswith(".error.txt")) if p.exists() else 0
        return {"review": len([j for j in review_jobs() if j["state"] in ("ready", "legacy")]),
                "error": count("error"), "processing": count("processing")}

    # -- operator separation (review/<job>/, written by the watcher; see ccocr/review.py) --

    def review_dir() -> Path:
        return store.data / "review"

    def job_state(d: Path) -> tuple[str, dict[str, Any]]:
        try:
            plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "analysing", {}
        if (d / "decision.json").exists() or (d / "decision.taken.json").exists():
            return "released", plan
        return plan.get("status", "analysing"), plan

    def review_jobs() -> list[dict[str, Any]]:
        rd = review_dir()
        if not rd.is_dir():
            return []
        out = []
        for d in sorted(rd.iterdir()):
            at = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if d.is_file():  # held by version 0.2.0 before the separation screen existed
                if not d.name.endswith(".tmp"):
                    out.append({"job": d.name.split("__", 1)[0], "name": d.name.split("__", 1)[-1], "state": "legacy", "at": at})
                continue
            state, plan = job_state(d)
            out.append({"job": d.name, "name": plan.get("batch_name", "?"), "state": state, "at": at,
                        "profile": (plan.get("profile") or {}).get("name", ""), "folder": plan.get("folder", ""),
                        "pages": len(plan.get("pages") or []), "documents": len(plan.get("documents") or []),
                        "flags": sum(1 for pg in plan.get("pages") or [] if pg.get("flags")), "error": plan.get("error", "")})
        return out

    def can_separate(u: sqlite3.Row) -> bool:
        r = roles_of(u)
        return "operator" in r or ("admin" in r and not store.get("system")["separation_of_duties"])

    def need_separator(request: Request) -> sqlite3.Row:
        u = need(request)
        if not can_separate(u):
            raise Redirect("/review?role=1")
        return u

    def job_dir_or_none(job: str) -> Path | None:
        # Job ids come from file names (račun_št, scan(1), ...): accept any name that is a
        # direct child folder of review/, and nothing that could leave it.
        if not job or job in (".", "..") or len(job) > 200 or any(c in job for c in "/\\\0"):
            return None
        rd = review_dir()
        d = rd / job
        try:
            if d.is_symlink() or not d.is_dir() or d.resolve().parent != rd.resolve():
                return None
        except OSError:
            return None
        return d

    def doctype_options() -> list[tuple[str, str]]:
        return [(d["code"], f"{d['label']} ({d['code']})") for d in store.get("doctypes")]

    def field_options(f: forms.Field) -> list[tuple[str, str]]:
        if f.key in ("document_type", "document_types"):
            return doctype_options()
        return f.options

    templates.env.globals["field_options"] = field_options

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):  # noqa: ANN202
        return RedirectResponse(exc.url, status_code=303)

    # -- health ----------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    # -- setup and sign-in -----------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):  # noqa: ANN202
        if store.user_count():
            return RedirectResponse("/login", 303)
        return render(request, "setup.html", {"min_len": store.get("system")["password_min_length"]})

    @app.post("/setup")
    async def setup_post(request: Request):  # noqa: ANN202
        if store.user_count():
            return RedirectResponse("/login", 303)
        form = await form_data(request)
        min_len = int(store.get("system")["password_min_length"])
        err = None
        code = setup_file.read_text().strip() if setup_file.exists() else ""
        login = str(form.get("login", "")).strip().lower()
        if not code or form.get("code", "").strip() != code:
            err = "The setup code is wrong. It is printed in the container log (docker compose logs ccocr-admin)."
        elif not login.replace(".", "").replace("-", "").isalnum():
            err = "The login may contain letters, digits, '.' and '-'."
        elif form.get("password") != form.get("password2"):
            err = "The two passwords are different."
        else:
            err = password_problem(str(form.get("password", "")), min_len)
        if err:
            return render(request, "setup.html", {"error": err, "min_len": min_len, "form": form}, 400)
        store.add_user(login, str(form.get("name") or login).strip(), hash_password(str(form["password"])), ["admin"],
                       must_change=False)
        setup_file.unlink(missing_ok=True)
        store.audit(login, "Administrator", "Create first administrator", f"user {login}")
        flash(request, "Administrator created. Sign in with your new password.")
        return RedirectResponse("/login", 303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):  # noqa: ANN202
        if store.user_count() == 0:
            return RedirectResponse("/setup", 303)
        return render(request, "login.html", {"next": request.query_params.get("next", "/")})

    @app.post("/login")
    async def login_post(request: Request):  # noqa: ANN202
        form = await form_data(request)
        login = str(form.get("login", "")).strip().lower()
        nxt = str(form.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        u = store.user(login)
        now = datetime.now(timezone.utc)
        if u and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now:
            return render(request, "login.html", {"error": f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.", "next": nxt}, 400)
        if not u or not u["active"] or not check_password(str(form.get("password", "")), u["pw_hash"]):
            if u:
                failed = u["failed"] + 1
                locked = (now + timedelta(minutes=LOCK_MINUTES)).isoformat() if failed >= LOCK_AFTER else None
                store.update_user(u["id"], failed=0 if locked else failed, locked_until=locked)
                store.audit(login, "-", "Failed sign-in", f"user {login}", reason="account locked" if locked else "")
            return render(request, "login.html", {"error": "Wrong login or password.", "next": nxt}, 400)
        store.update_user(u["id"], failed=0, locked_until=None, last_login=now_iso())
        request.session.clear()
        request.session.update(uid=u["id"], seen=int(time.time()), csrf=new_token())
        store.audit(*actor(u), "Sign in", f"user {login}", new="local")
        return RedirectResponse(nxt, 303)

    @app.post("/logout")
    async def logout(request: Request):  # noqa: ANN202
        u = current_user(request)
        if u:
            store.audit(*actor(u), "Sign out", f"user {u['login']}")
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/account/password", response_class=HTMLResponse)
    def password_page(request: Request):  # noqa: ANN202
        u = need(request)
        return render(request, "password.html", {"must": bool(u["must_change"]), "min_len": store.get("system")["password_min_length"]})

    @app.post("/account/password")
    async def password_post(request: Request):  # noqa: ANN202
        u = need(request)
        form = await form_data(request)
        min_len = int(store.get("system")["password_min_length"])
        err = None
        if not check_password(str(form.get("current", "")), u["pw_hash"]):
            err = "The current password is wrong."
        elif form.get("password") != form.get("password2"):
            err = "The two new passwords are different."
        elif form.get("password") == form.get("current"):
            err = "Choose a password different from the current one."
        else:
            err = password_problem(str(form.get("password", "")), min_len)
        if err:
            return render(request, "password.html", {"error": err, "must": bool(u["must_change"]), "min_len": min_len}, 400)
        store.update_user(u["id"], pw_hash=hash_password(str(form["password"])), must_change=0)
        store.audit(*actor(u), "Change own password", f"user {u['login']}")
        flash(request, "Password changed.")
        return RedirectResponse("/", 303)

    # -- dashboard and jobs ------------------------------------------------------
    def folder_status(f: dict[str, Any]) -> dict[str, Any]:
        st: dict[str, Any] = {"ok": True, "waiting": 0, "msg": ""}
        try:
            inbox = Path(f["in"])
            if not inbox.is_dir():
                return {"ok": False, "waiting": 0, "msg": "input folder not found"}
            waiting = 0
            for _dp, dirs, files in os.walk(inbox):  # dropped folders count too
                dirs[:] = [d for d in dirs if not d.startswith((".", "~"))]
                waiting += sum(1 for n in files if not n.startswith((".", "~")))
            st["waiting"] = waiting
            if not Path(f["out"]).is_dir():
                st.update(ok=False, msg="output folder not found")
        except OSError as e:
            st.update(ok=False, msg=str(e))
        return st

    def recent_batches(limit: int = 15) -> list[dict[str, Any]]:
        db = store.state / "state.sqlite"
        if not db.exists():
            return []
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT name, job_id, status, at FROM batches ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        return [{"name": r[0], "job": r[1], "status": r[2], "at": r[3][:16].replace("T", " ")} for r in rows]

    def reports(limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for f in store.folders():
            rdir = Path(f["out"]) / "_reports"
            if not rdir.is_dir():
                continue
            for p in rdir.glob("*.job-report.json"):
                out.append({"folder": f["id"], "folder_name": f["name"], "file": p.name, "mtime": p.stat().st_mtime})
        out.sort(key=lambda r: r["mtime"], reverse=True)
        return out[:limit]

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):  # noqa: ANN202
        need(request)
        folders = [{**f, "status": folder_status(f)} for f in store.folders()]
        today = datetime.now().strftime("%Y-%m-%d")
        batches = recent_batches(50)
        done_today = sum(1 for b in batches if b["status"] == "done" and b["at"].startswith(today))
        return render(request, "dashboard.html", {
            "folders": folders, "batches": batches[:12], "done_today": done_today,
            "seed_notes": app.state.seed_notes, "disk": shutil.disk_usage(store.data),
            "watch_cfg": (store.config_dir / "watch.yaml").exists(),
        })

    @app.get("/review", response_class=HTMLResponse)
    def review_list(request: Request):  # noqa: ANN202
        u = need(request)
        ctx = {"jobs": review_jobs(), "can_separate": can_separate(u)}
        if request.query_params.get("role"):
            ctx["role_note"] = True
        return render(request, "review_list.html", ctx)

    @app.get("/review/{job}", response_class=HTMLResponse)
    def review_job(request: Request, job: str):  # noqa: ANN202
        u = need_separator(request)
        d = job_dir_or_none(job)
        if d is None:
            flash(request, "That batch is no longer waiting for separation.", "warn")
            return RedirectResponse("/review", 303)
        state, plan = job_state(d)
        if state != "ready":
            flash(request, {"released": "That batch has already been released or rejected.",
                            "failed": "That batch could not be analysed."}.get(state, "That batch is still being analysed."), "warn")
            return RedirectResponse("/review", 303)
        known = {c for c, _ in doctype_options()}
        extra = [(c, c) for c in plan.get("document_types") or [] if c not in known]
        return render(request, "review.html", {
            "job": job, "plan": plan, "doctypes": doctype_options() + extra,
            "classifications": store.get("classifications"), "login": u["login"],
        })

    @app.get("/review/{job}/page/{n}.jpg")
    def review_thumb(request: Request, job: str, n: int, size: str = "s"):  # noqa: ANN202
        need_separator(request)
        d = job_dir_or_none(job)
        p = d / "thumbs" / f"p{n:04d}{'-l' if size == 'l' else ''}.jpg" if d else None
        if p is None or not p.is_file():
            return Response(status_code=404)
        return Response(p.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    def _write_decision(d: Path, decision: dict[str, Any]) -> bool:
        target = d / "decision.json"
        if target.exists() or (d / "decision.taken.json").exists():
            return False
        tmp = d / "decision.json.tmp"
        tmp.write_text(json.dumps(decision, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        return True

    @app.post("/review/{job}/confirm")
    async def review_confirm(request: Request, job: str):  # noqa: ANN202
        u = need_separator(request)
        form = await form_data(request)
        d = job_dir_or_none(job)
        if d is None or job_state(d)[0] != "ready":
            flash(request, "That batch is no longer waiting for separation.", "warn")
            return RedirectResponse("/review", 303)
        plan = job_state(d)[1]
        n = len(plan.get("pages") or [])
        try:
            layout = json.loads(str(form.get("layout", "")))
            docs, seen = [], set()
            for doc in layout.get("documents") or []:
                pages = [int(x) for x in doc.get("pages") or []]
                if not pages:
                    continue
                if any(x < 0 or x >= n or x in seen for x in pages) or pages != sorted(pages):
                    raise ValueError("each page may be in one document only, in batch order")
                seen.update(pages)
                docs.append({"pages": pages, "doctype": str(doc.get("doctype") or "")[:60],
                             "classification": str(doc.get("classification") or "")[:60]})
            if not docs:
                raise ValueError("keep at least one page")
            rotate = {str(int(k)): int(v) % 360 for k, v in (layout.get("rotate") or {}).items()
                      if 0 <= int(k) < n and int(v) % 90 == 0 and int(v) % 360}
        except (ValueError, TypeError, AttributeError) as e:
            flash(request, f"The layout could not be read: {e}", "bad")
            return RedirectResponse(f"/review/{quote(job)}", 303)
        removed = sorted(set(range(n)) - seen)
        login, role = actor(u)
        decision = {"action": "confirm", "by": login, "role": role, "at": now_iso(), "documents": docs,
                    "rotate": rotate, "removed": removed, "note": str(form.get("note", ""))[:500],
                    "proposed_documents": len(plan.get("documents") or [])}
        if not _write_decision(d, decision):
            flash(request, "Someone else has already released this batch.", "warn")
            return RedirectResponse("/review", 303)
        store.audit(login, role, "Confirm separation", f"batch {plan.get('batch_name')} (job {job})",
                    old={"documents": [x["pages"] for x in plan.get("documents") or []]},
                    new={"documents": [x["pages"] for x in docs], "removed": removed, "rotate": rotate},
                    reason=decision["note"])
        flash(request, f"{plan.get('batch_name')}: {len(docs)} document(s) released. The watcher writes them on its next poll.")
        return RedirectResponse("/review", 303)

    @app.post("/review/{job}/reject")
    async def review_reject(request: Request, job: str):  # noqa: ANN202
        u = need_separator(request)
        form = await form_data(request)
        d = job_dir_or_none(job)
        reason = str(form.get("reason", "")).strip()[:500]
        if d is None or job_state(d)[0] not in ("ready", "failed"):
            flash(request, "That batch is no longer waiting for separation.", "warn")
            return RedirectResponse("/review", 303)
        if not reason:
            flash(request, "Write why the batch is rejected.", "bad")
            return RedirectResponse(f"/review/{quote(job)}" if job_state(d)[0] == "ready" else "/review", 303)
        plan = job_state(d)[1]
        login, role = actor(u)
        if not _write_decision(d, {"action": "reject", "by": login, "role": role, "at": now_iso(), "reason": reason}):
            flash(request, "Someone else has already released this batch.", "warn")
            return RedirectResponse("/review", 303)
        store.audit(login, role, "Reject batch", f"batch {plan.get('batch_name')} (job {job})", reason=reason)
        flash(request, f"{plan.get('batch_name')} rejected. It moves to the error folder on the next poll.")
        return RedirectResponse("/review", 303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):  # noqa: ANN202
        need(request)
        errors = []
        edir = store.data / "error"
        if edir.is_dir():
            for p in sorted(edir.glob("*.error.txt"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
                errors.append({"name": p.name[: -len(".error.txt")], "text": p.read_text(encoding="utf-8", errors="replace")[:4000],
                               "at": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
        review = [j for j in review_jobs() if j["state"] in ("ready", "legacy")]
        return render(request, "jobs.html", {"reports": [
            {**r, "at": datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")} for r in reports()],
            "errors": errors, "review": review, "batches": recent_batches(100)})

    @app.get("/jobs/report", response_class=HTMLResponse)
    def job_report(request: Request, folder: int, file: str):  # noqa: ANN202
        need(request)
        f = store.folder(folder)
        if not f or "/" in file or not file.endswith(".job-report.json"):
            return RedirectResponse("/jobs", 303)
        p = Path(f["out"]) / "_reports" / file
        if not p.is_file():
            return RedirectResponse("/jobs", 303)
        rep = json.loads(p.read_text(encoding="utf-8"))
        return render(request, "report.html", {"rep": rep, "raw": json.dumps(rep, ensure_ascii=False, indent=2), "file": file})

    # -- profiles ------------------------------------------------------------------
    @app.get("/profiles", response_class=HTMLResponse)
    def profiles(request: Request):  # noqa: ANN202
        need(request, "admin")
        rows = []
        for n in store.profile_names():
            pub, dr = store.published(n), store.draft(n)
            used = [f["name"] for f in store.folders() if f["profile"] == n]
            rows.append({"name": n, "published": pub["version"] if pub else None, "draft": dr["version"] if dr else None,
                         "used": used, "data": json.loads((dr or pub)["data"])})
        return render(request, "profiles.html", {"rows": rows})

    @app.post("/profiles/new")
    async def profile_new(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        name = str(form.get("name", "")).strip()
        copy_from = str(form.get("copy_from", ""))
        if name in store.profile_names():
            flash(request, f"A profile named {name} already exists.", "bad")
            return RedirectResponse("/profiles", 303)
        base: dict[str, Any] = {}
        if copy_from and (src := store.current(copy_from)):
            base = json.loads(src["data"])
        base["profile"] = name
        base.setdefault("split", {})["auto_accept"] = False if not copy_from else base.get("split", {}).get("auto_accept", False)
        try:
            ver = store.save_draft(base, u["login"], comment="new profile" + (f" from {copy_from}" if copy_from else ""))
        except ProfileError as e:
            flash(request, str(e), "bad")
            return RedirectResponse("/profiles", 303)
        store.audit(*actor(u), "Create profile", f"profile {name} v{ver} (draft)", new=copy_from and f"copy of {copy_from}")
        return RedirectResponse(f"/profiles/{quote(name)}", 303)

    @app.post("/profiles/import")
    async def profile_import(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        up = form.get("file")
        try:
            data = yaml.safe_load((await up.read()).decode("utf-8")) if up and hasattr(up, "read") else None
            if not isinstance(data, dict):
                raise ProfileError("the file is not a profile YAML")
            data.pop("timestamp", None)  # trust services are set globally
            data.pop("config_version", None)
            ver = store.save_draft(data, u["login"], comment=f"imported from {getattr(up, 'filename', 'file')}")
        except (ProfileError, RuleError, yaml.YAMLError, UnicodeDecodeError) as e:
            flash(request, f"Import failed: {e}", "bad")
            return RedirectResponse("/profiles", 303)
        store.audit(*actor(u), "Import profile", f"profile {data['profile']} v{ver} (draft)", new=getattr(up, "filename", ""))
        flash(request, f"Imported as draft v{ver}. Check it and publish.")
        return RedirectResponse(f"/profiles/{quote(data['profile'])}", 303)

    def profile_ctx(name: str) -> dict[str, Any] | None:
        row = store.current(name)
        if not row:
            return None
        return {"name": name, "row": row, "data": json.loads(row["data"]), "pub": store.published(name),
                "draft": store.draft(name), "versions": store.versions(name),
                "langs": forms.installed_langs(),
                "used": [f["name"] for f in store.folders() if f["profile"] == name]}

    @app.get("/profiles/{name}", response_class=HTMLResponse)
    def profile_edit(request: Request, name: str, tab: str = "general"):  # noqa: ANN202
        need(request, "admin")
        ctx = profile_ctx(name)
        if not ctx:
            return RedirectResponse("/profiles", 303)
        if tab not in {s[0] for s in forms.SECTIONS} | {"rules", "history"}:
            tab = "general"
        ctx["tab"] = tab
        if tab == "history":
            vs = ctx["versions"]
            a = request.query_params.get("a")
            b = request.query_params.get("b")
            if vs and len(vs) > 1:
                a = int(a or vs[1]["version"])
                b = int(b or vs[0]["version"])
                ra, rb = store.version(name, a), store.version(name, b)
                if ra and rb:
                    ctx["diff"] = list(difflib.unified_diff(
                        profile_yaml(json.loads(ra["data"])).splitlines(), profile_yaml(json.loads(rb["data"])).splitlines(),
                        f"v{a}", f"v{b}", lineterm=""))
                    ctx["a"], ctx["b"] = a, b
        return render(request, "profile.html", ctx)

    def edit_base(name: str) -> dict[str, Any]:
        row = store.current(name)
        if row is None:
            raise Redirect("/profiles")
        return json.loads(row["data"])

    @app.post("/profiles/{name}/section/{sid}")
    async def profile_section(request: Request, name: str, sid: str):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        if sid not in {s[0] for s in forms.SECTIONS}:
            return RedirectResponse(f"/profiles/{quote(name)}", 303)
        old = edit_base(name)
        try:
            new = forms.apply_form(old, sid, form, form.getlist)
            changed = {f.key: (forms.get_path(old, f.key), forms.get_path(new, f.key))
                       for f in next(fs for s, _, fs in forms.SECTIONS if s == sid)
                       if forms.get_path(old, f.key) != forms.get_path(new, f.key)}
            reason = str(form.get("reason", "")).strip()
            if "split.auto_accept" in changed and changed["split.auto_accept"][1] and not reason:
                raise forms.FormError("Turning on automatic acceptance needs a reason.")
            if not changed:
                flash(request, "No changes.")
                return RedirectResponse(f"/profiles/{quote(name)}?tab={sid}", 303)
            ver = store.save_draft(new, u["login"])
        except (forms.FormError, ProfileError, RuleError) as e:
            flash(request, str(e), "bad")
            return RedirectResponse(f"/profiles/{quote(name)}?tab={sid}", 303)
        store.audit(*actor(u), "Edit profile draft", f"profile {name} v{ver}",
                    old={k: v[0] for k, v in changed.items()}, new={k: v[1] for k, v in changed.items()},
                    reason=str(form.get("reason", "")).strip())
        flash(request, f"Saved in draft v{ver}. Publish it to use it.")
        return RedirectResponse(f"/profiles/{quote(name)}?tab={sid}", 303)

    @app.post("/profiles/{name}/rules")
    async def profile_rules(request: Request, name: str):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        data = edit_base(name)
        rules = list(data.get("split", {}).get("rules") or [])
        op = form.get("op")
        idx = int(form.get("idx", -1))
        old_rules = json.loads(json.dumps(rules))
        try:
            if op in ("save", "add"):
                r = {"name": str(form.get("name", "")).strip(), "expr": str(form.get("expr", "")).strip()}
                for k in ("doctype", "classification"):
                    if str(form.get(k, "")).strip():
                        r[k] = str(form.get(k)).strip()
                if not r["name"]:
                    raise forms.FormError("Give the rule a name.")
                compile_rule(r["name"], r["expr"])
                if op == "add":
                    rules.append(r)
                else:
                    rules[idx] = r
            elif op == "delete":
                rules.pop(idx)
            elif op in ("up", "down"):
                j = idx - 1 if op == "up" else idx + 1
                if 0 <= j < len(rules):
                    rules[idx], rules[j] = rules[j], rules[idx]
            data.setdefault("split", {})["rules"] = rules
            ver = store.save_draft(data, u["login"])
        except (RuleError, ProfileError, forms.FormError, IndexError) as e:
            flash(request, f"Rule not saved: {e}", "bad")
            return RedirectResponse(f"/profiles/{quote(name)}?tab=rules", 303)
        store.audit(*actor(u), f"Rule {op}", f"profile {name} v{ver}", old=old_rules, new=rules)
        flash(request, f"Rules saved in draft v{ver}.")
        return RedirectResponse(f"/profiles/{quote(name)}?tab=rules", 303)

    @app.post("/rules/test")
    async def rule_test(request: Request):  # noqa: ANN202
        need(request, "admin")
        form = await form_data(request)
        expr = str(form.get("expr", ""))
        text = str(form.get("text", ""))
        zone = float(form.get("zone") or 0.25)
        lines = text.splitlines()
        k = max(1, round(len(lines) * zone)) if lines else 0
        ctx = PageContext(text=text, top="\n".join(lines[:k]), bottom="\n".join(lines[-k:]) if k else "",
                          page=int(form.get("page") or 2), words=len(text.split()), confidence=float(form.get("confidence") or 90),
                          prev_blank="prev_blank" in form, first_page=int(form.get("page") or 2) == 1)
        try:
            rule = compile_rule("test", expr)
        except RuleError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "match": rule.matches(ctx), "top": ctx.top, "bottom": ctx.bottom, "words": ctx.words})

    @app.post("/profiles/{name}/publish")
    async def profile_publish(request: Request, name: str):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        try:
            ver = store.publish(name, str(form.get("comment", "")).strip())
        except (ProfileError, RuleError) as e:
            flash(request, f"Not published: {e}", "bad")
            return RedirectResponse(f"/profiles/{quote(name)}", 303)
        store.audit(*actor(u), "Publish profile", f"profile {name} v{ver}", reason=str(form.get("comment", "")).strip())
        flash(request, f"Published v{ver}. Watched folders using {name} pick it up within a minute.")
        return RedirectResponse(f"/profiles/{quote(name)}", 303)

    @app.post("/profiles/{name}/discard")
    async def profile_discard(request: Request, name: str):  # noqa: ANN202
        u = need(request, "admin")
        await form_data(request)
        d = store.draft(name)
        if d:
            store.discard_draft(name)
            store.audit(*actor(u), "Discard profile draft", f"profile {name} v{d['version']}")
            flash(request, f"Draft v{d['version']} discarded.")
        if not store.profile_names() or name not in store.profile_names():
            return RedirectResponse("/profiles", 303)
        return RedirectResponse(f"/profiles/{quote(name)}", 303)

    @app.post("/profiles/{name}/rollback/{ver}")
    async def profile_rollback(request: Request, name: str, ver: int):  # noqa: ANN202
        u = need(request, "admin")
        await form_data(request)
        row = store.version(name, ver)
        if not row:
            return RedirectResponse(f"/profiles/{quote(name)}?tab=history", 303)
        store.discard_draft(name)
        new = store.save_draft(json.loads(row["data"]), u["login"], comment=f"rollback to v{ver}")
        store.audit(*actor(u), "Roll back profile", f"profile {name}", new=f"draft v{new} = copy of v{ver}")
        flash(request, f"Draft v{new} now holds the settings of v{ver}. Publish it to apply.")
        return RedirectResponse(f"/profiles/{quote(name)}?tab=history", 303)

    @app.post("/profiles/{name}/delete")
    async def profile_delete(request: Request, name: str):  # noqa: ANN202
        u = need(request, "admin")
        await form_data(request)
        used = [f["name"] for f in store.folders() if f["profile"] == name]
        if used:
            flash(request, f"{name} is used by folder(s): {', '.join(used)}. Change those folders first.", "bad")
            return RedirectResponse(f"/profiles/{quote(name)}", 303)
        store.delete_profile(name)
        store.audit(*actor(u), "Delete profile", f"profile {name}")
        flash(request, f"Profile {name} deleted.")
        return RedirectResponse("/profiles", 303)

    @app.get("/profiles/{name}/export")
    def profile_export(request: Request, name: str, v: int | None = None):  # noqa: ANN202
        u = need(request, "admin")
        row = store.version(name, v) if v else store.current(name)
        if not row:
            return RedirectResponse("/profiles", 303)
        store.audit(*actor(u), "Export profile", f"profile {name} v{row['version']}")
        body = f"# CC OCR profile {name} v{row['version']} ({row['status']}), exported {now_iso()}\n" + profile_yaml(json.loads(row["data"]))
        return Response(body, media_type="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}-v{row["version"]}.yaml"'})

    # -- rules overview, naming ---------------------------------------------------
    @app.get("/rules", response_class=HTMLResponse)
    def rules_overview(request: Request):  # noqa: ANN202
        need(request, "admin")
        rows = []
        for n in store.profile_names():
            r = store.current(n)
            d = json.loads(r["data"])
            rows.append({"name": n, "draft": r["status"] == "draft", "rules": d["split"].get("rules") or [],
                         "on": "text-rule" in (d["split"].get("proposals") or [])})
        return render(request, "rules.html", {"rows": rows})

    @app.get("/naming", response_class=HTMLResponse)
    def naming_page(request: Request):  # noqa: ANN202
        need(request, "admin")
        rows = []
        for n in store.profile_names():
            d = json.loads(store.current(n)["data"])
            rows.append({"name": n, "out": d["output"], "folders": [f for f in store.folders() if f["profile"] == n]})
        return render(request, "naming.html", {"rows": rows})

    @app.post("/naming/preview")
    async def naming_preview(request: Request):  # noqa: ANN202
        need(request, "admin")
        form = await form_data(request)
        meta = {"doctype": form.get("doctype") or "invoice", "classification": form.get("classification") or "4.2.1",
                "batch": form.get("batch") or "scan-2026-11-25", "seq": "001", "profile": form.get("profile") or "incoming-mixed",
                "date": datetime.now().strftime(str(form.get("date_format") or "%Y-%m-%d")),
                "issuer": form.get("issuer") or "Mlekarna Celje-Šentjur d.o.o.", "number": form.get("number") or "2026-001"}
        try:
            name = build_name(str(form.get("pattern", "")), meta, max_length=int(form.get("max_length") or 120))
        except (NamingError, ValueError, KeyError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "name": name, "length": len(name) - len(".pdf") if name.endswith(".pdf") else len(name)})

    # -- folders -------------------------------------------------------------------
    @app.get("/folders", response_class=HTMLResponse)
    def folders_page(request: Request):  # noqa: ANN202
        need(request, "admin")
        return render(request, "folders.html", {"folders": [{**f, "status": folder_status(f), "host_in": mounts.to_host(f["in"]),
                                                             "host_out": mounts.to_host(f["out"])} for f in store.folders()],
                                                "watch": store.get("watch")})

    def folder_form_ctx(f: dict[str, Any], error: str | None = None) -> dict[str, Any]:
        return {"f": f, "profiles": [n for n in store.profile_names() if store.published(n)], "error": error,
                "mounts": mounts.configured(), "container_mounts": mounts.container_mounts(),
                "host_in": mounts.to_host(f.get("in") or ""), "host_out": mounts.to_host(f.get("out") or ""),
                "watch": store.get("watch"), "types": [".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"],
                "share_user_set": bool(store.secrets().get(f"share_{f.get('id')}_password"))}

    @app.get("/folders/new", response_class=HTMLResponse)
    def folder_new(request: Request):  # noqa: ANN202
        need(request, "admin")
        return render(request, "folder.html", folder_form_ctx({**FOLDER_DEFAULTS, "id": None}))

    @app.get("/folders/{fid}", response_class=HTMLResponse)
    def folder_edit(request: Request, fid: int):  # noqa: ANN202
        need(request, "admin")
        f = store.folder(fid)
        if not f:
            return RedirectResponse("/folders", 303)
        return render(request, "folder.html", folder_form_ctx(f))

    def opt_int(v: Any) -> int | None:
        v = str(v or "").strip()
        return int(v) if v else None

    @app.post("/folders/save")
    async def folder_save(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        fid = opt_int(form.get("id"))
        old = store.folder(fid) if fid else None
        data = {
            "name": str(form.get("name", "")).strip(), "in": str(form.get("in", "")).strip().rstrip("/"),
            "out": str(form.get("out", "")).strip().rstrip("/"), "profile": str(form.get("profile", "")),
            "paused": "paused" in form, "access": str(form.get("access", "local")),
            "require_marker": True if form.get("require_marker") == "yes" else False if form.get("require_marker") == "no" else None,
            "marker_suffix": str(form.get("marker_suffix", "")).strip() or None, "duplicate": str(form.get("duplicate", "error")),
            "file_types": [t for t in form.getlist("file_types")], "max_mb": float(form.get("max_mb") or 0),
        }
        try:
            data["in"] = mounts.to_container(data["in"]) if data["in"] else data["in"]
            data["out"] = mounts.to_container(data["out"]) if data["out"] else data["out"]
            data["stable_polls"] = opt_int(form.get("stable_polls"))
            data["retries"] = opt_int(form.get("retries"))
            if not data["name"]:
                raise ValueError("Give the folder a name.")
            new_id = store.save_folder(data, fid)
        except ValueError as e:
            return render(request, "folder.html", folder_form_ctx({**FOLDER_DEFAULTS, **data, "id": fid}, str(e)), 400)
        changes = {k: (old.get(k) if old else None, data[k]) for k in data if not old or old.get(k) != data[k]}
        store.audit(*actor(u), "Add folder" if not fid else "Edit folder", f"folder {data['name']}",
                    old={k: v[0] for k, v in changes.items()} if old else None, new={k: v[1] for k, v in changes.items()})
        flash(request, "Folder saved. The watcher applies the change within one poll interval.")
        return RedirectResponse(f"/folders/{new_id}", 303)

    @app.post("/folders/{fid}/delete")
    async def folder_delete(request: Request, fid: int):  # noqa: ANN202
        u = need(request, "admin")
        await form_data(request)
        f = store.folder(fid)
        if f:
            store.delete_folder(fid)
            store.audit(*actor(u), "Remove folder", f"folder {f['name']}", old={"in": f["in"], "out": f["out"]})
            flash(request, f"Folder {f['name']} is no longer watched. No files were deleted.")
        return RedirectResponse("/folders", 303)

    @app.post("/folders/{fid}/test")
    async def folder_test(request: Request, fid: int):  # noqa: ANN202
        need(request, "admin")
        await form_data(request)
        f = store.folder(fid)
        if not f:
            return JSONResponse({"ok": False, "steps": []})
        steps = []
        for label, path in (("Input", f["in"]), ("Output", f["out"])):
            p = Path(path)
            probe = p / f".ccocr-probe-{new_token(4)}"
            try:
                if not p.is_dir():
                    raise OSError("folder does not exist")
                list(p.iterdir())
                probe.write_bytes(b"probe")
                probe.unlink()
                steps.append({"ok": True, "text": f"{label}: {path} can be listed, written and cleaned up"})
            except OSError as e:
                steps.append({"ok": False, "text": f"{label}: {path}: {e.strerror or e}"})
        return JSONResponse({"ok": all(s["ok"] for s in steps), "steps": steps})

    @app.post("/folders/watch")
    async def watch_settings(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        old = store.get("watch")
        try:
            new = {"remote": "remote" in form, "poll_interval": int(form.get("poll_interval") or 10),
                   "stable_polls": int(form.get("stable_polls") or 3), "marker_suffix": str(form.get("marker_suffix") or ".done"),
                   "require_marker": "require_marker" in form, "retries": int(form.get("retries") or 3),
                   "retry_backoff": int(form.get("retry_backoff") or 30)}
            if not (2 <= new["poll_interval"] <= 3600 and 1 <= new["stable_polls"] <= 20 and 1 <= new["retries"] <= 20):
                raise ValueError("values out of range")
        except ValueError as e:
            flash(request, f"Not saved: {e}", "bad")
            return RedirectResponse("/folders", 303)
        store.put("watch", new)
        store.export()
        store.audit(*actor(u), "Edit watch settings", "global watch settings", old=old, new=new)
        flash(request, "Watch settings saved.")
        return RedirectResponse("/folders", 303)

    # -- separator sheets ---------------------------------------------------------
    @app.get("/sheets", response_class=HTMLResponse)
    def sheets_page(request: Request):  # noqa: ANN202
        need(request, "admin", "operator")
        return render(request, "sheets.html", {"doctypes": doctype_options(), "classes": store.get("classifications"),
                                               "profiles": [n for n in store.profile_names() if store.published(n)],
                                               "presets": store.get("sheet_presets")})

    @app.post("/sheets/pdf")
    async def sheets_pdf(request: Request):  # noqa: ANN202
        u = need(request, "admin", "operator")
        form = await form_data(request)
        from ..sheets import payload, write_sheets

        dt = str(form.get("doctype", "document"))
        try:
            code = payload(dt, str(form.get("classification", "")).strip(), str(form.get("profile", "")).strip())
            copies = max(1, min(200, int(form.get("copies") or 1)))
        except ValueError as e:
            flash(request, str(e), "bad")
            return RedirectResponse("/sheets", 303)
        tmp = store.state / f"sheets-{new_token(6)}.pdf"
        try:
            write_sheets(tmp, [(code, str(form.get("label") or dt.upper()))], copies=copies)
            body = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)
        if form.get("save_preset"):
            presets = [p for p in store.get("sheet_presets") if p["code"] != code]
            presets.append({"code": code, "doctype": dt, "classification": form.get("classification", ""),
                            "profile": form.get("profile", ""), "label": form.get("label", "")})
            store.put("sheet_presets", presets)
        store.audit(*actor(u), "Print separator sheets", code, new=f"{copies} copies")
        return Response(body, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="separator-{dt}.pdf"'})

    @app.post("/sheets/preset/delete")
    async def sheets_preset_delete(request: Request):  # noqa: ANN202
        need(request, "admin", "operator")
        form = await form_data(request)
        store.put("sheet_presets", [p for p in store.get("sheet_presets") if p["code"] != form.get("code")])
        return RedirectResponse("/sheets", 303)

    # -- document types -----------------------------------------------------------
    @app.get("/types", response_class=HTMLResponse)
    def types_page(request: Request):  # noqa: ANN202
        need(request, "admin")
        return render(request, "types.html", {"doctypes": store.get("doctypes"), "classes": store.get("classifications")})

    @app.post("/types")
    async def types_post(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        def parse(text: str) -> list[dict[str, str]]:
            out = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                code, _, label = line.partition("=")
                code = code.strip()
                if not code or ";" in code or "=" in code:
                    raise ValueError(f"'{line}': use one line per entry, e.g. invoice = Invoice")
                out.append({"code": code, "label": label.strip() or code})
            return out
        try:
            dts = parse(str(form.get("doctypes", "")))
            cls = parse(str(form.get("classes", "")))
            if not dts:
                raise ValueError("Keep at least one document type.")
        except ValueError as e:
            flash(request, str(e), "bad")
            return RedirectResponse("/types", 303)
        old = {"doctypes": store.get("doctypes"), "classifications": store.get("classifications")}
        store.put("doctypes", dts)
        store.put("classifications", cls)
        store.audit(*actor(u), "Edit document types", "document types and classifications", old=old,
                    new={"doctypes": dts, "classifications": cls})
        flash(request, "Saved.")
        return RedirectResponse("/types", 303)

    # -- trust services -------------------------------------------------------------
    @app.get("/trust", response_class=HTMLResponse)
    def trust_page(request: Request):  # noqa: ANN202
        need(request, "admin")
        s = store.secrets()
        return render(request, "trust.html", {
            "t": store.get("trust"), "e": store.get("eseal"), "tsa_user": s.get("tsa_user", ""),
            "tsa_pw_set": bool(s.get("tsa_password")),
            "env_override": bool(os.environ.get("CCOCR_TSA_USER") and os.environ.get("CCOCR_TSA_PASSWORD")),
            "last": store.get("trust_last_test")})

    @app.post("/trust")
    async def trust_post(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        old = store.get("trust")
        mode = str(form.get("mode", "openssl-local"))
        if mode not in ("none", "openssl-local", "http"):
            mode = "openssl-local"
        url = str(form.get("url", "")).strip()
        if mode == "http" and not url.startswith(("http://", "https://")):
            flash(request, "Enter the TimeStamp service URL (http:// or https://).", "bad")
            return RedirectResponse("/trust", 303)
        reason = str(form.get("reason", "")).strip()
        if mode == "none" and old["mode"] != "none" and not reason:
            flash(request, "Switching timestamps off needs a reason.", "bad")
            return RedirectResponse("/trust", 303)
        new = {"mode": mode, "url": url, "ca_file": str(form.get("ca_file", "")).strip(),
               "chain_file": str(form.get("chain_file", "")).strip(), "qualified": "qualified" in form,
               "embed_validation": "embed_validation" in form, "required": "required" in form,
               "timeout": max(5, min(120, int(form.get("timeout") or 30)))}
        store.put("trust", new)
        store.export()
        store.audit(*actor(u), "Edit timestamp settings", "trust services", old=old, new=new, reason=reason)
        flash(request, "Timestamp settings saved and applied to all profiles.")
        return RedirectResponse("/trust", 303)

    @app.post("/trust/credentials")
    async def trust_credentials(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        user = str(form.get("tsa_user", "")).strip()
        pw = str(form.get("tsa_password", ""))
        store.set_secret("tsa_user", user or None)
        if pw or form.get("clear"):
            store.set_secret("tsa_password", None if form.get("clear") else pw)
        store.audit(*actor(u), "Set TSA credentials", "trust services", new={"user": user, "password": "changed" if pw else "unchanged"})
        flash(request, "Credentials saved in the secrets store.")
        return RedirectResponse("/trust", 303)

    @app.post("/trust/test")
    async def trust_test(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        await form_data(request)
        from ..timestamp import TimestampError, timestamp

        t = store.get("trust")
        cfg = {**t, "hash": "sha256", "openssl_dir": os.environ.get("CCOCR_TEST_TSA_DIR", "")}
        started = time.time()
        try:
            res = timestamp(f"CC OCR connection test {now_iso()}".encode(), cfg)
            result = {"ok": True, "text": "Timestamps are off." if res is None else
                      f"Token received in {time.time() - started:.1f} s from {res.tsa}, time {res.gen_time}, serial {res.serial}."}
        except (TimestampError, OSError, ValueError) as e:
            result = {"ok": False, "text": f"{type(e).__name__}: {e}"}
        except Exception as e:  # noqa: BLE001 - show any failure to the admin
            result = {"ok": False, "text": f"{type(e).__name__}: {e}"}
        result["at"] = now_iso()
        result["mode"] = t["mode"]
        store.put("trust_last_test", result)
        store.audit(*actor(u), "Test timestamp service", t["url"] or t["mode"], new="ok" if result["ok"] else result["text"][:300])
        return RedirectResponse("/trust", 303)

    @app.post("/eseal")
    async def eseal_post(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        old = store.get("eseal")
        new = {**old, "provider": str(form.get("provider", "")).strip(), "url": str(form.get("url", "")).strip(),
               "pades_level": str(form.get("pades_level", "B-LTA")), "appearance": str(form.get("appearance", "invisible")),
               "reason": str(form.get("reason", "")).strip(), "location": str(form.get("location", "")).strip(),
               "identity": "cc-ocr", "enabled": False}
        store.put("eseal", new)
        store.audit(*actor(u), "Edit e-seal settings", "trust services", old=old, new=new)
        flash(request, "E-seal settings saved. Sealing is not active yet.")
        return RedirectResponse("/trust", 303)

    # -- users --------------------------------------------------------------------
    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request):  # noqa: ANN202
        need(request, "admin")
        return render(request, "users.html", {"users": store.users(), "system": store.get("system"),
                                              "temp": request.session.pop("temp_pw", None)})

    @app.post("/users/new")
    async def users_new(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        login = str(form.get("login", "")).strip().lower()
        roles = [r for r in form.getlist("roles") if r in ROLES]
        if not login.replace(".", "").replace("-", "").isalnum() or store.user(login):
            flash(request, "Choose a free login (letters, digits, '.' and '-').", "bad")
            return RedirectResponse("/users", 303)
        if not roles:
            flash(request, "Give the user at least one role.", "bad")
            return RedirectResponse("/users", 303)
        sod = store.get("system")["separation_of_duties"]
        if sod and "admin" in roles and len(roles) > 1:
            flash(request, "Separation of duties is on: an administrator cannot also be operator or reviewer.", "bad")
            return RedirectResponse("/users", 303)
        pw = temp_password()
        store.add_user(login, str(form.get("name") or login).strip(), hash_password(pw), roles, must_change=True)
        store.audit(*actor(u), "Create user", f"user {login}", new={"roles": roles})
        request.session["temp_pw"] = {"login": login, "password": pw}
        return RedirectResponse("/users", 303)

    @app.post("/users/{uid}")
    async def users_edit(request: Request, uid: int):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        target = store.user_by_id(uid)
        if not target:
            return RedirectResponse("/users", 303)
        op = form.get("op")
        if op == "reset":
            pw = temp_password()
            store.update_user(uid, pw_hash=hash_password(pw), must_change=1, failed=0, locked_until=None)
            store.audit(*actor(u), "Reset password", f"user {target['login']}")
            request.session["temp_pw"] = {"login": target["login"], "password": pw}
            return RedirectResponse("/users", 303)
        roles = [r for r in form.getlist("roles") if r in ROLES]
        active = "active" in form
        name = str(form.get("name") or target["name"]).strip()
        was_admin = "admin" in target["roles"].split(",") and target["active"]
        if not roles:
            flash(request, "Keep at least one role.", "bad")
            return RedirectResponse("/users", 303)
        if store.get("system")["separation_of_duties"] and "admin" in roles and len(roles) > 1:
            flash(request, "Separation of duties is on: an administrator cannot also be operator or reviewer.", "bad")
            return RedirectResponse("/users", 303)
        if was_admin and ("admin" not in roles or not active) and store.active_admins() <= 1:
            flash(request, "This is the last active administrator.", "bad")
            return RedirectResponse("/users", 303)
        if uid == u["id"] and not active:
            flash(request, "You cannot deactivate your own account.", "bad")
            return RedirectResponse("/users", 303)
        old = {"name": target["name"], "roles": target["roles"], "active": bool(target["active"])}
        store.update_user(uid, name=name, roles=",".join(roles), active=int(active))
        store.audit(*actor(u), "Edit user", f"user {target['login']}", old=old,
                    new={"name": name, "roles": ",".join(roles), "active": active})
        flash(request, f"User {target['login']} saved.")
        return RedirectResponse("/users", 303)

    # -- audit --------------------------------------------------------------------
    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, user: str = "", action: str = "", q: str = ""):  # noqa: ANN202
        need(request, "admin")
        return render(request, "audit.html", {"rows": store.audit_rows(user, action, q), "users": [x["login"] for x in store.users()],
                                              "actions": store.audit_actions(), "f": {"user": user, "action": action, "q": q}})

    @app.get("/audit.csv")
    def audit_csv(request: Request, user: str = "", action: str = "", q: str = ""):  # noqa: ANN202
        u = need(request, "admin")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "user", "role", "action", "object", "old", "new", "reason"])
        for r in store.audit_rows(user, action, q, limit=1_000_000):
            w.writerow([r["at"], r["user"], r["role"], r["action"], r["object"], r["old"] or "", r["new"] or "", r["reason"] or ""])
        store.audit(*actor(u), "Export audit log", "audit log", new={"user": user, "action": action, "q": q})
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="cc-ocr-audit-{datetime.now():%Y%m%d}.csv"'})

    # -- system ---------------------------------------------------------------------
    @app.get("/system", response_class=HTMLResponse)
    def system_page(request: Request):  # noqa: ANN202
        need(request, "admin")
        return render(request, "system.html", {"s": store.get("system"), "data_dir": str(store.data),
                                               "config_dir": str(store.config_dir), "langs": forms.installed_langs()})

    @app.post("/system")
    async def system_post(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        form = await form_data(request)
        old = store.get("system")
        new = {**old, "language": form.get("language") if form.get("language") in LANGUAGES else "en",
               "timezone": str(form.get("timezone") or "Europe/Ljubljana").strip(),
               "session_minutes": max(5, min(480, int(form.get("session_minutes") or 30))),
               "password_min_length": max(8, min(64, int(form.get("password_min_length") or 12))),
               "separation_of_duties": "separation_of_duties" in form, "four_eyes": "four_eyes" in form}
        store.put("system", new)
        store.audit(*actor(u), "Edit system settings", "system", old=old, new=new)
        flash(request, "System settings saved.")
        return RedirectResponse("/system", 303)

    @app.get("/system/export")
    def system_export(request: Request):  # noqa: ANN202
        u = need(request, "admin")
        store.audit(*actor(u), "Export configuration", "all settings, published profiles, folders")
        return Response(store.export_bundle(), media_type="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="cc-ocr-config-{datetime.now():%Y%m%d-%H%M}.yaml"'})

    return app


def serve(data_dir: Path, host: str = "0.0.0.0", port: int = 8080, seed_config: Path | None = None) -> None:
    import uvicorn

    app = create_app(data_dir, seed_config)
    uvicorn.run(app, host=host, port=port, proxy_headers=True, log_level="info")
