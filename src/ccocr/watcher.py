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
    name: str = ""
    paused: bool = False
    # Per-folder overrides; None = use the global value of the watch config.
    stable_polls: int | None = None
    marker_suffix: str | None = None
    require_marker: bool | None = None
    retries: int | None = None
    file_types: tuple[str, ...] = ()  # e.g. (".pdf", ".tif"); empty = all supported types
    max_mb: float = 0  # 0 = no limit
    duplicate: str = "error"  # error (move to error/) | skip (delete silently is never done: moved to archive/) | reprocess


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
    source: Path | None = None  # file the config was read from (reloaded when it changes)


def load_watch_config(path: Path) -> WatchConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    root = (base / raw.get("root", ".")).resolve()
    remote = bool(raw.get("remote", False))
    folders = []
    for f in raw.get("folders", []):
        dup = f.get("duplicate", "error")
        if dup not in {"error", "skip", "reprocess"}:
            raise ValueError(f"folder {f.get('name') or f['in']}: duplicate must be error, skip or reprocess")
        folders.append(
            WatchedFolder(
                inbox=(base / f["in"]).resolve(),
                out=(base / f["out"]).resolve(),
                profile=load_profile(base / f["profile"]) if f.get("profile") else load_profile(None),
                name=str(f.get("name") or ""),
                paused=bool(f.get("paused", False)),
                stable_polls=f.get("stable_polls"),
                marker_suffix=f.get("marker_suffix"),
                require_marker=f.get("require_marker"),
                retries=f.get("retries"),
                file_types=tuple("." + t.lower().lstrip(".") for t in (f.get("file_types") or [])),
                max_mb=float(f.get("max_mb") or 0),
                duplicate=dup,
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
        source=path,
    )


def _candidate(p: Path, marker_suffix: str, file_types: tuple[str, ...] = ()) -> bool:
    if not p.is_file() or p.name.startswith((".", "~")):
        return False
    if p.suffix.lower() in IGNORED_SUFFIXES or p.name.endswith(marker_suffix):
        return False
    if file_types and p.suffix.lower() not in file_types:
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
        for d in ("processing", "archive", "error", "review", ".ccocr"):
            (r / d).mkdir(parents=True, exist_ok=True)
        for f in self.config.folders:
            f.inbox.mkdir(parents=True, exist_ok=True)
            f.out.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(r / ".ccocr" / "state.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS batches (sha256 TEXT PRIMARY KEY, name TEXT, job_id TEXT, status TEXT, at TEXT)"
        )
        self.db.commit()

    # -- per-folder settings ------------------------------------------------
    def _opt(self, folder: WatchedFolder, name: str):  # noqa: ANN202
        v = getattr(folder, name)
        return getattr(self.config, name) if v is None else v

    # -- detection ---------------------------------------------------------
    def ready_files(self, folder: WatchedFolder) -> list[Path]:
        ready = []
        present = set()
        marker_suffix = self._opt(folder, "marker_suffix")
        stable_polls = self._opt(folder, "stable_polls")
        for p in sorted(folder.inbox.iterdir()):
            if not _candidate(p, marker_suffix, folder.file_types):
                continue
            present.add(p)
            marker = p.with_name(p.name + marker_suffix)
            if marker.exists():
                ready.append(p)
                continue
            if self._opt(folder, "require_marker"):
                continue
            st = p.stat()
            s = self._seen.get(p)
            if s and s.size == st.st_size and s.mtime == st.st_mtime and st.st_size > 0:
                s.stable += 1
            else:
                self._seen[p] = s = _Seen(st.st_size, st.st_mtime, 0)
            if s.stable >= stable_polls - 1 and _can_open_exclusively(p):
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

    def _record(self, digest: str, name: str, job_id: str, status: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?,?,?)", (digest, name, job_id, status, datetime.now().isoformat()))
        self.db.commit()

    def handle(self, folder: WatchedFolder, path: Path) -> None:
        root = self.config.root
        marker = path.with_name(path.name + self._opt(folder, "marker_suffix"))
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + path.stem[:20].replace(" ", "_")
        if folder.max_mb and path.stat().st_size > folder.max_mb * 1024 * 1024:
            dest = self._move(path, root / "error", path.name)
            dest.with_name(dest.name + ".error.txt").write_text(
                f"File is larger than the folder limit of {folder.max_mb:g} MB.\n", encoding="utf-8"
            )
            if marker.exists():
                marker.unlink()
            self._seen.pop(path, None)
            self.log(f"too large, moved to error/: {path.name}")
            return
        work = self._move(path, root / "processing", f"{job_id}__{path.name}")
        if marker.exists():
            marker.unlink()
        self._seen.pop(path, None)
        digest = sha256_file(work)
        row = self.db.execute("SELECT job_id, status FROM batches WHERE sha256 = ?", (digest,)).fetchone()
        if row and row[1] in ("done", "waiting-separation") and folder.duplicate != "reprocess":
            if folder.duplicate == "skip":
                self._move(work, root / "archive" / "duplicates", path.name)
                self.log(f"duplicate skipped and archived: {path.name} (job {row[0]})")
                return
            dest = self._move(work, root / "error", path.name)
            dest.with_name(dest.name + ".error.txt").write_text(
                f"Duplicate: identical file (SHA-256 {digest}) was already processed as job {row[0]}.\n", encoding="utf-8"
            )
            self.log(f"duplicate skipped: {path.name} (job {row[0]})")
            return
        if not folder.profile["split"].get("auto_accept", False):
            # Split proposals must be confirmed by an operator (spec 3.2): the batch waits in
            # review/ for the separation screen. Nothing is written to the output folder.
            self._move(work, root / "review", f"{job_id}__{path.name}")
            self._record(digest, path.name, job_id, "waiting-separation")
            self.log(f"job {job_id} waits for operator separation (auto-accept is off in profile {folder.profile.name})")
            return
        try:
            report = process_batch(work, folder.profile, folder.out, job_id=job_id, batch_name=path.name, log=self.log)
        except Exception as e:  # noqa: BLE001
            n = self._failures.get(digest, 0) + 1
            self._failures[digest] = n
            retries = self._opt(folder, "retries")
            if n < retries:
                self.log(f"job {job_id} failed (attempt {n}/{retries}), will retry: {e}")
                self._move(work, folder.inbox, path.name)
                time.sleep(min(self.config.retry_backoff * n, 300))
                return
            dest = self._move(work, root / "error", path.name)
            dest.with_name(dest.name + ".error.txt").write_text(
                f"Job {job_id} failed after {n} attempt(s).\n\n{traceback.format_exc()}", encoding="utf-8"
            )
            self._record(digest, path.name, job_id, "error")
            self.log(f"job {job_id} moved to error/: {e}")
            return
        archive = root / "archive" / datetime.now().strftime("%Y-%m-%d")
        self._move(work, archive, path.name)
        self._record(digest, path.name, job_id, "done")
        self.log(f"job {job_id} done: {len(report['documents'])} document(s) -> {folder.out}")

    def poll_once(self) -> int:
        handled = 0
        for folder in self.config.folders:
            if folder.paused:
                continue
            try:
                files = self.ready_files(folder)
            except OSError as e:
                self.log(f"folder unavailable: {folder.inbox} ({e}); will retry")
                continue
            for p in files:
                self.handle(folder, p)
                handled += 1
        return handled

    def _reload_if_changed(self, mtime: float | None) -> float | None:
        """Pick up a config written by the admin console without restarting the container."""
        src = self.config.source
        if src is None or not src.exists():
            return mtime
        now = src.stat().st_mtime
        if mtime is not None and now != mtime:
            try:
                new = load_watch_config(src)
            except Exception as e:  # noqa: BLE001 - keep running with the old config
                self.log(f"new configuration not loaded, keeping the previous one: {e}")
                return now
            for f in new.folders:
                f.inbox.mkdir(parents=True, exist_ok=True)
                f.out.mkdir(parents=True, exist_ok=True)
            self.config = new
            self._seen.clear()
            self.log(f"configuration reloaded: {len(new.folders)} folder(s)")
        return now

    def run(self, once: bool = False) -> None:
        active = sum(not f.paused for f in self.config.folders)
        self.log(f"watching {active} folder(s), every {self.config.poll_interval:.0f} s")
        mtime = self.config.source.stat().st_mtime if self.config.source and self.config.source.exists() else None
        while True:
            self.poll_once()
            if once:
                return
            time.sleep(self.config.poll_interval)
            mtime = self._reload_if_changed(mtime)
