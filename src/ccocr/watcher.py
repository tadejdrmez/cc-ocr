"""Hot-folder watcher (spec 2.4-2.6, 2.10.4).

Network shares do not deliver reliable file events, so folders are polled. A
file is taken only when its size and modification time have not changed for
``stable_polls`` polls and it can be opened for writing (scanner software
usually keeps it locked while writing), or when its marker file exists.

Folders under ``root``::

    processing/   batches being processed (moved here atomically)
    archive/      original batches, unchanged, kept for traceability
    error/        batches that failed, with an .error.txt next to them
    .ccocr/       state database (SHA-256 of every processed batch)
"""

from __future__ import annotations

import shutil
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from .ingest import SUPPORTED, sha256_file
from .pipeline import process_batch
from .profile import Profile, load_profile

IGNORED_SUFFIXES = {".tmp", ".part", ".crdownload", ".partial"}


@dataclass
class WatchedFolder:
    inbox: Path
    out: Path
    profile: Profile


@dataclass
class WatchConfig:
    root: Path
    folders: list[WatchedFolder]
    poll_interval: float = 10.0
    stable_polls: int = 3
    marker_suffix: str = ".done"
    require_marker: bool = False
    retries: int = 3
    retry_backoff: float = 30.0


def load_watch_config(path: Path) -> WatchConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    root = (base / raw.get("root", ".")).resolve()
    remote = bool(raw.get("remote", False))
    folders = []
    for f in raw.get("folders", []):
        folders.append(
            WatchedFolder(
                inbox=(base / f["in"]).resolve(),
                out=(base / f["out"]).resolve(),
                profile=load_profile(base / f["profile"]) if f.get("profile") else load_profile(None),
            )
        )
    if not folders:
        raise ValueError("watch config has no folders")
    return WatchConfig(
        root=root,
        folders=folders,
        # Remote folders over a tunnel: longer interval, more stable polls (spec 2.10.4).
        poll_interval=float(raw.get("poll_interval", 30 if remote else 10)),
        stable_polls=int(raw.get("stable_polls", 4 if remote else 3)),
        marker_suffix=raw.get("marker_suffix", ".done"),
        require_marker=bool(raw.get("require_marker", False)),
        retries=int(raw.get("retries", 3)),
        retry_backoff=float(raw.get("retry_backoff", 30)),
    )


def _candidate(p: Path, marker_suffix: str) -> bool:
    if not p.is_file() or p.name.startswith((".", "~")):
        return False
    if p.suffix.lower() in IGNORED_SUFFIXES or p.name.endswith(marker_suffix):
        return False
    return p.suffix.lower() in SUPPORTED


def _can_open_exclusively(p: Path) -> bool:
    try:
        with open(p, "r+b"):
            return True
    except OSError:
        return False


@dataclass
class _Seen:
    size: int
    mtime: float
    stable: int = 0


@dataclass
class Watcher:
    config: WatchConfig
    log: Callable[[str], None] = print
    _seen: dict[Path, _Seen] = field(default_factory=dict)
    _failures: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        r = self.config.root
        for d in ("processing", "archive", "error", ".ccocr"):
            (r / d).mkdir(parents=True, exist_ok=True)
        for f in self.config.folders:
            f.inbox.mkdir(parents=True, exist_ok=True)
            f.out.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(r / ".ccocr" / "state.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS batches (sha256 TEXT PRIMARY KEY, name TEXT, job_id TEXT, status TEXT, at TEXT)"
        )
        self.db.commit()

    # -- detection ---------------------------------------------------------
    def ready_files(self, folder: WatchedFolder) -> list[Path]:
        ready = []
        present = set()
        for p in sorted(folder.inbox.iterdir()):
            if not _candidate(p, self.config.marker_suffix):
                continue
            present.add(p)
            marker = p.with_name(p.name + self.config.marker_suffix)
            if marker.exists():
                ready.append(p)
                continue
            if self.config.require_marker:
                continue
            st = p.stat()
            s = self._seen.get(p)
            if s and s.size == st.st_size and s.mtime == st.st_mtime and st.st_size > 0:
                s.stable += 1
            else:
                self._seen[p] = s = _Seen(st.st_size, st.st_mtime, 0)
            if s.stable >= self.config.stable_polls - 1 and _can_open_exclusively(p):
                ready.append(p)
        for p in list(self._seen):
            if p.parent == folder.inbox and p not in present:
                del self._seen[p]
        return ready

    # -- processing --------------------------------------------------------
    def _move(self, src: Path, dest_dir: Path, name: str | None = None) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (name or src.name)
        n = 2
        while dest.exists():
            dest = dest_dir / f"{dest.stem}_{n}{dest.suffix}"
            n += 1
        try:
            src.rename(dest)
        except OSError:  # different file system (e.g. share -> local disk)
            shutil.copy2(src, dest)
            if sha256_file(src) != sha256_file(dest):
                raise OSError(f"copy of {src} could not be verified")
            src.unlink()
        return dest

    def handle(self, folder: WatchedFolder, path: Path) -> None:
        root = self.config.root
        marker = path.with_name(path.name + self.config.marker_suffix)
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + path.stem[:20].replace(" ", "_")
        work = self._move(path, root / "processing", f"{job_id}__{path.name}")
        if marker.exists():
            marker.unlink()
        self._seen.pop(path, None)
        digest = sha256_file(work)
        row = self.db.execute("SELECT job_id, status FROM batches WHERE sha256 = ?", (digest,)).fetchone()
        if row and row[1] == "done":
            dest = self._move(work, root / "error", path.name)
            dest.with_name(dest.name + ".error.txt").write_text(
                f"Duplicate: identical file (SHA-256 {digest}) was already processed as job {row[0]}.\n", encoding="utf-8"
            )
            self.log(f"duplicate skipped: {path.name} (job {row[0]})")
            return
        try:
            report = process_batch(work, folder.profile, folder.out, job_id=job_id, batch_name=path.name, log=self.log)
        except Exception as e:  # noqa: BLE001
            n = self._failures.get(digest, 0) + 1
            self._failures[digest] = n
            if n < self.config.retries:
                self.log(f"job {job_id} failed (attempt {n}/{self.config.retries}), will retry: {e}")
                self._move(work, folder.inbox, path.name)
                time.sleep(min(self.config.retry_backoff * n, 300))
                return
            dest = self._move(work, root / "error", path.name)
            dest.with_name(dest.name + ".error.txt").write_text(
                f"Job {job_id} failed after {n} attempt(s).\n\n{traceback.format_exc()}", encoding="utf-8"
            )
            self.db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?)", (digest, path.name, job_id, "error", datetime.now().isoformat()))
            self.db.commit()
            self.log(f"job {job_id} moved to error/: {e}")
            return
        archive = root / "archive" / datetime.now().strftime("%Y-%m-%d")
        self._move(work, archive, path.name)
        self.db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?)", (digest, path.name, job_id, "done", datetime.now().isoformat()))
        self.db.commit()
        self.log(f"job {job_id} done: {len(report['documents'])} document(s) -> {folder.out}")

    def poll_once(self) -> int:
        handled = 0
        for folder in self.config.folders:
            try:
                files = self.ready_files(folder)
            except OSError as e:
                self.log(f"folder unavailable: {folder.inbox} ({e}); will retry")
                continue
            for p in files:
                self.handle(folder, p)
                handled += 1
        return handled

    def run(self, once: bool = False) -> None:
        self.log(f"watching {len(self.config.folders)} folder(s), every {self.config.poll_interval:.0f} s")
        while True:
            self.poll_once()
            if once:
                return
            time.sleep(self.config.poll_interval)
