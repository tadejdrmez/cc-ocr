"""Host folder <-> container path mapping.

Operators know the folders on their machine (``C:\\OCR_Work_SDS\\in``), not the
paths inside the container (``/mnt/sds/in``). Docker does not tell a container
where its bind mounts come from, so the deployment declares them in
``CCOCR_HOST_MOUNTS``::

    CCOCR_HOST_MOUNTS="C:\\cc-ocr\\data=/data;C:\\OCR_Work_SDS=/mnt/sds"

The console then accepts either form, stores the container path, and shows the
host path next to it. A host folder that is not shared with the container is
rejected with a message that says how to share it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_WIN = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


@dataclass(frozen=True)
class Mount:
    host: str
    container: str

    @property
    def windows(self) -> bool:
        return bool(_WIN.match(self.host))


def _norm_host(p: str) -> str:
    p = p.strip().strip('"')
    if _WIN.match(p):
        p = p.replace("/", "\\").rstrip("\\")
        return p[:1].upper() + p[1:] if re.match(r"^[a-z]:", p) else p
    return p.rstrip("/") or "/"


def configured(env: str | None = None) -> list[Mount]:
    raw = os.environ.get("CCOCR_HOST_MOUNTS", "") if env is None else env
    out = []
    for part in re.split(r"[;\n]", raw):
        if "=" not in part:
            continue
        # split at the last '=' followed by '/', so Windows paths with '=' stay intact
        host, _, cont = part.rpartition("=")
        if host.strip() and cont.strip().startswith("/"):
            out.append(Mount(_norm_host(host), cont.strip().rstrip("/") or "/"))
    return out


def container_mounts() -> list[str]:
    """Bind-mounted folders visible inside the container (/data, /mnt/...)."""
    found = []
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mp = line.split()[4]
            if mp == "/data" or mp.startswith(("/data/", "/mnt/", "/srv/", "/scan")):
                found.append(mp)
    except OSError:
        pass
    return sorted(set(found))


class NotShared(ValueError):
    pass


def to_container(path: str, mounts: list[Mount] | None = None) -> str:
    """Translate a host path to the container path; container paths pass through."""
    mounts = configured() if mounts is None else mounts
    p = path.strip().strip('"')
    if not p:
        raise NotShared("enter a folder")
    if p.startswith("/") and not any(not m.windows and _under(p, m.host) for m in mounts):
        return p.rstrip("/") or "/"
    host = _norm_host(p)
    win = bool(_WIN.match(host))
    best: Mount | None = None
    for m in mounts:
        if m.windows != win:
            continue
        if _under(host, m.host, ci=win) and (best is None or len(m.host) > len(best.host)):
            best = m
    if best is None:
        shared = ", ".join(f"{m.host} → {m.container}" for m in mounts) or "none declared"
        raise NotShared(
            f"{path} is not shared with the container. Shared folders: {shared}. "
            "Add the folder to the volumes and to CCOCR_HOST_MOUNTS in docker-compose.yml, "
            "or pick a folder inside a shared one."
        )
    rest = host[len(best.host):].lstrip("\\/").replace("\\", "/")
    return f"{best.container}/{rest}" if rest else best.container


def to_host(container_path: str, mounts: list[Mount] | None = None) -> str | None:
    mounts = configured() if mounts is None else mounts
    best: Mount | None = None
    for m in mounts:
        if _under(container_path, m.container) and (best is None or len(m.container) > len(best.container)):
            best = m
    if best is None:
        return None
    rest = container_path[len(best.container):].lstrip("/")
    if not rest:
        return best.host
    sep = "\\" if best.windows else "/"
    return best.host.rstrip("\\/") + sep + rest.replace("/", sep)


def _under(path: str, base: str, ci: bool = False) -> bool:
    a, b = (path.lower(), base.lower()) if ci else (path, base)
    if b in ("/", ""):
        return True
    return a == b or a.startswith(b.rstrip("\\/") + ("\\" if "\\" in b else "/"))
