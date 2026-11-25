"""Admin console storage: users, audit log, versioned profiles, folders and settings.

The database (SQLite, ``<data>/.ccocr/admin.sqlite``) is the source of the
configuration (spec 2.7, 9.4). Every change that the watcher must see is
exported as YAML to ``<data>/.ccocr/config/`` (``watch.yaml`` plus one file per
published profile); the watcher reloads ``watch.yaml`` when it changes. YAML is
also the import/export format for backups and test -> production transfer.

Secrets (TSA password, share passwords) are kept in ``secrets.json`` with mode
0600, never in the database, the YAML or the audit log.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..profile import DEFAULTS, ProfileError, load_profile

ROLES = ("admin", "operator", "reviewer")

GLOBAL_DEFAULTS: dict[str, Any] = {
    "watch": {
        "remote": False,
        "poll_interval": 10,
        "stable_polls": 3,
        "marker_suffix": ".done",
        "require_marker": False,
        "retries": 3,
        "retry_backoff": 30,
    },
    "trust": {
        "mode": "openssl-local",  # none | openssl-local | http
        "url": "",
        "ca_file": "",
        "chain_file": "",
        "qualified": False,
        "embed_validation": False,
        "required": True,
        "timeout": 30,
    },
    "eseal": {
        "enabled": False,
        "identity": "cc-ocr",
        "provider": "cc-ca e-seal service",
        "url": "",
        "pades_level": "B-LTA",
        "appearance": "invisible",
        "reason": "Conversion confirmed by operator",
        "location": "",
    },
    "doctypes": [
        {"code": "invoice", "label": "Invoice"},
        {"code": "contract", "label": "Contract"},
        {"code": "document", "label": "Document"},
    ],
    "classifications": [],
    "sheet_presets": [],
    "system": {
        "language": "en",
        "timezone": "Europe/Ljubljana",
        "session_minutes": 30,
        "password_min_length": 12,
        "separation_of_duties": True,
        "four_eyes": False,
    },
}

FOLDER_DEFAULTS: dict[str, Any] = {
    "name": "",
    "in": "/data/in",
    "out": "/data/out",
    "profile": "",
    "paused": False,
    "access": "local",  # local | smb | smb-tunnel | sync-agent (documentation; the share is mounted outside)
    "stable_polls": None,
    "marker_suffix": None,
    "require_marker": None,
    "retries": None,
    "file_types": [],
    "max_mb": 0,
    "duplicate": "error",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def profile_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None)


class _Result:
    """A fully fetched cursor, so no other thread can disturb it."""

    def __init__(self, rows: list[Any], lastrowid: int | None, rowcount: int):
        self._rows, self.lastrowid, self.rowcount = rows, lastrowid, rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self):  # noqa: ANN204
        return iter(self._rows)


class _LockedDB:
    """One SQLite connection shared by the server's worker threads.

    Every statement runs under the store's lock and is fetched completely before
    the lock is released. Without this, parallel requests (a page and its images)
    interleave on the shared connection and a query can come back empty.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn, self._lock = conn, lock

    def execute(self, sql: str, args: Any = ()) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return _Result(cur.fetchall(), cur.lastrowid, cur.rowcount)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Store:
    def __init__(self, data_dir: Path):
        self.data = data_dir
        self.state = data_dir / ".ccocr"
        self.state.mkdir(parents=True, exist_ok=True)
        self.config_dir = self.state / "config"
        self.secrets_file = self.state / "secrets.json"
        self._lock = threading.RLock()
        conn = sqlite3.connect(self.state / "admin.sqlite", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self.db = _LockedDB(conn, self._lock)
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
                roles TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, must_change INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, last_login TEXT, failed INTEGER NOT NULL DEFAULT 0, locked_until TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, user TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
                object TEXT NOT NULL, old TEXT, new TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
                data TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, comment TEXT,
                UNIQUE(name, version));
            CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.db.commit()

    # -- audit ------------------------------------------------------------
    def audit(self, user: str, role: str, action: str, obj: str, old: Any = None, new: Any = None, reason: str = "") -> None:
        def s(v: Any) -> str | None:
            if v is None:
                return None
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

        with self._lock:
            self.db.execute(
                "INSERT INTO audit (at, user, role, action, object, old, new, reason) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso(), user, role, action, obj, s(old), s(new), reason or None),
            )
            self.db.commit()

    def audit_rows(self, user: str = "", action: str = "", text: str = "", limit: int = 500) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM audit WHERE 1=1", []
        if user:
            q += " AND user = ?"
            args.append(user)
        if action:
            q += " AND action = ?"
            args.append(action)
        if text:
            q += " AND (object LIKE ? OR old LIKE ? OR new LIKE ? OR reason LIKE ?)"
            args += [f"%{text}%"] * 4
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(q, args).fetchall()

    def audit_actions(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT action FROM audit ORDER BY action")]

    # -- settings ---------------------------------------------------------
    def get(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        default = copy.deepcopy(GLOBAL_DEFAULTS.get(key))
        if row is None:
            return default
        val = json.loads(row[0])
        if isinstance(default, dict) and isinstance(val, dict):
            return _deep_merge(default, val)
        return val

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
            self.db.commit()

    # -- secrets ----------------------------------------------------------
    def secrets(self) -> dict[str, str]:
        try:
            return json.loads(self.secrets_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_secret(self, key: str, value: str | None) -> None:
        with self._lock:
            s = self.secrets()
            if value:
                s[key] = value
            else:
                s.pop(key, None)
            _write_atomic(self.secrets_file, json.dumps(s), mode=0o600)

    # -- users ------------------------------------------------------------
    def users(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM users ORDER BY login").fetchall()

    def user(self, login: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()

    def user_by_id(self, uid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def user_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def active_admins(self) -> int:
        return sum(1 for u in self.users() if u["active"] and "admin" in u["roles"].split(","))

    def add_user(self, login: str, name: str, pw_hash: str, roles: list[str], must_change: bool = True) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO users (login, name, pw_hash, roles, must_change, created_at) VALUES (?,?,?,?,?,?)",
                (login, name, pw_hash, ",".join(roles), int(must_change), now_iso()),
            )
            self.db.commit()

    def update_user(self, uid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE users SET {cols} WHERE id = ?", (*fields.values(), uid))  # noqa: S608 - fixed column names
            self.db.commit()

    # -- profiles ---------------------------------------------------------
    def profile_names(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT name FROM profiles ORDER BY name")]

    def versions(self, name: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM profiles WHERE name = ? ORDER BY version DESC", (name,)).fetchall()

    def version(self, name: str, version: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM profiles WHERE name = ? AND version = ?", (name, version)).fetchone()

    def published(self, name: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM profiles WHERE name = ? AND status = 'published' ORDER BY version DESC LIMIT 1", (name,)
        ).fetchone()

    def draft(self, name: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM profiles WHERE name = ? AND status = 'draft'", (name,)).fetchone()

    def current(self, name: str) -> sqlite3.Row | None:
        return self.draft(name) or self.published(name)

    @staticmethod
    def validate_profile(data: dict[str, Any]) -> dict[str, Any]:
        """Return the full profile (defaults filled in) or raise ProfileError."""
        name = str(data.get("profile") or "").strip()
        if not name or not all(c.isalnum() or c in "-_." for c in name):
            raise ProfileError("profile name may contain only letters, digits, '-', '_' and '.'")
        clean = {k: v for k, v in data.items() if k in DEFAULTS or k in {"description"}}
        prof = load_profile(clean)  # validates values and compiles every rule
        full = prof.raw
        if "description" in data:
            full["description"] = data["description"]
        return full

    def save_draft(self, data: dict[str, Any], user: str, comment: str = "") -> int:
        """Create or replace the draft of a profile. Returns its version number."""
        full = self.validate_profile(data)
        name = full["profile"]
        with self._lock:
            d = self.draft(name)
            payload = json.dumps(full, ensure_ascii=False)
            if d:
                self.db.execute("UPDATE profiles SET data = ?, created_by = ?, created_at = ? WHERE id = ?",
                                (payload, user, now_iso(), d["id"]))
                ver = d["version"]
            else:
                top = self.db.execute("SELECT MAX(version) FROM profiles WHERE name = ?", (name,)).fetchone()[0] or 0
                ver = top + 1
                self.db.execute(
                    "INSERT INTO profiles (name, version, status, data, created_by, created_at, comment) VALUES (?,?,?,?,?,?,?)",
                    (name, ver, "draft", payload, user, now_iso(), comment),
                )
            self.db.commit()
        return ver

    def discard_draft(self, name: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM profiles WHERE name = ? AND status = 'draft'", (name,))
            self.db.commit()

    def publish(self, name: str, comment: str = "") -> int:
        with self._lock:
            d = self.draft(name)
            if not d:
                raise ProfileError("there is no draft to publish")
            self.validate_profile(json.loads(d["data"]))
            self.db.execute("UPDATE profiles SET status = 'archived' WHERE name = ? AND status = 'published'", (name,))
            self.db.execute("UPDATE profiles SET status = 'published', comment = ? WHERE id = ?", (comment or d["comment"], d["id"]))
            self.db.commit()
        self.export()
        return d["version"]

    def delete_profile(self, name: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM profiles WHERE name = ?", (name,))
            self.db.commit()
        self.export()

    # -- folders ----------------------------------------------------------
    def folders(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.execute("SELECT id, data FROM folders ORDER BY id"):
            f = _deep_merge(FOLDER_DEFAULTS, json.loads(r["data"]))
            f["id"] = r["id"]
            out.append(f)
        return out

    def folder(self, fid: int) -> dict[str, Any] | None:
        return next((f for f in self.folders() if f["id"] == fid), None)

    def save_folder(self, data: dict[str, Any], fid: int | None = None) -> int:
        clean = {k: data.get(k, FOLDER_DEFAULTS[k]) for k in FOLDER_DEFAULTS}
        if not str(clean["in"]).startswith("/") or not str(clean["out"]).startswith("/"):
            raise ValueError("enter the input and output folders, e.g. C:\\Scan\\in or /data/in")
        if clean["in"] == clean["out"]:
            raise ValueError("input and output must be different folders")
        if clean["duplicate"] not in {"error", "skip", "reprocess"}:
            raise ValueError("unknown duplicate handling")
        if clean["profile"] and not self.published(clean["profile"]):
            raise ValueError(f"profile {clean['profile']} has no published version")
        for f in self.folders():
            if f["id"] != fid and f["in"] == clean["in"]:
                raise ValueError(f"{clean['in']} is already watched by folder '{f['name'] or f['id']}'")
        payload = json.dumps(clean, ensure_ascii=False)
        with self._lock:
            if fid is None:
                cur = self.db.execute("INSERT INTO folders (data, updated_at) VALUES (?, ?)", (payload, now_iso()))
                fid = int(cur.lastrowid)
            else:
                self.db.execute("UPDATE folders SET data = ?, updated_at = ? WHERE id = ?", (payload, now_iso(), fid))
            self.db.commit()
        self.export()
        return fid

    def delete_folder(self, fid: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM folders WHERE id = ?", (fid,))
            self.db.commit()
        self.export()

    # -- export for the watcher -------------------------------------------
    def runtime_profile(self, name: str) -> dict[str, Any] | None:
        """Published profile as the pipeline reads it: global trust settings merged in."""
        row = self.published(name)
        if not row:
            return None
        data = json.loads(row["data"])
        trust = self.get("trust")
        data["timestamp"] = _deep_merge(data.get("timestamp") or {}, {
            "mode": trust["mode"], "url": trust["url"], "ca_file": trust["ca_file"], "chain_file": trust["chain_file"],
            "qualified": bool(trust["qualified"]), "embed_validation": bool(trust["embed_validation"]),
            "required": bool(trust["required"]), "timeout": trust["timeout"],
        })
        data["config_version"] = row["version"]
        return data

    def export(self) -> Path:
        """Write watch.yaml and the published profiles for the watcher."""
        with self._lock:
            pdir = self.config_dir / "profiles"
            pdir.mkdir(parents=True, exist_ok=True)
            names = set()
            for name in self.profile_names():
                data = self.runtime_profile(name)
                if data is None:
                    continue
                names.add(f"{name}.yaml")
                _write_atomic(pdir / f"{name}.yaml",
                              "# Generated by the CC OCR admin console. Do not edit: changes are overwritten.\n"
                              + profile_yaml(data))
            for old in pdir.glob("*.yaml"):
                if old.name not in names:
                    old.unlink()
            w = self.get("watch")
            folders = []
            for f in self.folders():
                if not f["profile"] or not self.published(f["profile"]):
                    continue
                entry: dict[str, Any] = {"name": f["name"], "in": f["in"], "out": f["out"],
                                         "profile": str(pdir / f"{f['profile']}.yaml"), "paused": bool(f["paused"]),
                                         "duplicate": f["duplicate"]}
                for k in ("stable_polls", "marker_suffix", "require_marker", "retries"):
                    if f[k] not in (None, ""):
                        entry[k] = f[k]
                if f["file_types"]:
                    entry["file_types"] = list(f["file_types"])
                if f["max_mb"]:
                    entry["max_mb"] = float(f["max_mb"])
                folders.append(entry)
            watch = {
                "root": str(self.data), "remote": bool(w["remote"]), "poll_interval": w["poll_interval"],
                "stable_polls": w["stable_polls"], "marker_suffix": w["marker_suffix"],
                "require_marker": bool(w["require_marker"]), "retries": w["retries"], "retry_backoff": w["retry_backoff"],
                "folders": folders,
            }
            target = self.config_dir / "watch.yaml"
            if folders:
                _write_atomic(target, "# Generated by the CC OCR admin console. Do not edit: changes are overwritten.\n"
                              + yaml.safe_dump(watch, allow_unicode=True, sort_keys=False))
            elif target.exists():
                # No active folders: keep the watcher idle rather than failing on an empty config.
                watch["folders"] = []
                _write_atomic(target, "# Generated by the CC OCR admin console: no folders configured.\n"
                              + yaml.safe_dump(watch, allow_unicode=True, sort_keys=False))
            return target

    # -- first start: import the YAML the container shipped with ----------
    def seed_from(self, watch_yaml: Path) -> list[str]:
        """Import an existing watch config and its profiles once (empty database only)."""
        if self.profile_names() or self.folders() or not watch_yaml.exists():
            return []
        notes = []
        raw = yaml.safe_load(watch_yaml.read_text(encoding="utf-8")) or {}
        base = watch_yaml.parent
        w = self.get("watch")
        for k in w:
            if k in raw:
                w[k] = raw[k]
        if raw.get("remote") and "poll_interval" not in raw:
            w["poll_interval"], w["stable_polls"] = 30, 4
        self.put("watch", w)
        trust_done = False
        for i, f in enumerate(raw.get("folders") or []):
            pname = ""
            if f.get("profile"):
                ppath = base / f["profile"]
                pdata = yaml.safe_load(ppath.read_text(encoding="utf-8")) or {}
                full = load_profile(pdata).raw
                pname = full["profile"]
                if not trust_done and pdata.get("timestamp"):
                    t = self.get("trust")
                    for k in t:
                        if k in full["timestamp"]:
                            t[k] = full["timestamp"][k]
                    self.put("trust", t)
                    trust_done = True
                if not self.published(pname):
                    self.save_draft(full, "import", comment=f"imported from {ppath.name}")
                    self.publish(pname)
                    notes.append(f"profile {pname} imported from {ppath}")
            self.save_folder({**FOLDER_DEFAULTS, "name": f.get("name") or f"Folder {i + 1}",
                              "in": str((base / f["in"]).resolve()), "out": str((base / f["out"]).resolve()),
                              "profile": pname})
            notes.append(f"folder {f['in']} imported")
        self.export()
        return notes

    # -- whole-configuration backup ----------------------------------------
    def export_bundle(self) -> str:
        bundle = {
            "cc_ocr_config": 1,
            "exported_at": now_iso(),
            "settings": {k: self.get(k) for k in GLOBAL_DEFAULTS},
            "profiles": [json.loads(r["data"]) for n in self.profile_names() if (r := self.published(n))],
            "folders": [{k: v for k, v in f.items() if k != "id"} for f in self.folders()],
        }
        return yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False)
