"""Output file names from a token pattern (spec 2.11.2, decision D4).

* Unicode names, NFC-normalised; Slovenian/BCS diacritics are kept.
* Only characters unsafe on Windows/SMB are replaced.
* Length limited in characters; long values are shortened token by token,
  keeping ``number`` and ``date`` intact.
* Existing files are never overwritten; collisions compare case-insensitively.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
TOKEN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
PROTECTED = ("number", "date")


class NamingError(ValueError):
    pass


def sanitise_value(value: str) -> str:
    v = unicodedata.normalize("NFC", str(value))
    v = UNSAFE.sub("-", v)
    v = re.sub(r"\s+", "-", v.strip())
    v = re.sub(r"-{2,}", "-", v)
    return v.strip("-.")


def _render(pattern: str, values: dict[str, str]) -> str:
    return TOKEN.sub(lambda m: values[m.group(1)], pattern)


def build_name(pattern: str, metadata: dict[str, str], *, max_length: int = 120, extension: str = ".pdf") -> str:
    """Render ``pattern`` (without extension) into a safe base name."""
    pattern = re.sub(r"\.pdf$", "", pattern, flags=re.IGNORECASE)
    missing = [t for t in TOKEN.findall(pattern) if not str(metadata.get(t, "")).strip()]
    if missing:
        raise NamingError(f"missing value for {', '.join('{' + m + '}' for m in missing)}")
    values = {t: sanitise_value(metadata[t]) for t in TOKEN.findall(pattern)}
    limit = max_length - len(extension)
    name = sanitise_value(_render(pattern, values))
    # Shorten the longest non-protected token until the name fits.
    while len(name) > limit:
        candidates = [k for k in values if k not in PROTECTED and len(values[k]) > 3]
        if not candidates:
            name = name[:limit].rstrip("-.")
            break
        k = max(candidates, key=lambda c: len(values[c]))
        values[k] = values[k][: max(3, len(values[k]) - (len(name) - limit))].rstrip("-.")
        name = sanitise_value(_render(pattern, values))
    if not name:
        raise NamingError("pattern produced an empty name")
    if name.split(".")[0].upper() in RESERVED:
        name = f"_{name}"
    return name


def unique_path(directory: Path, base: str, extensions: tuple[str, ...], on_collision: str = "suffix") -> str:
    """Return a base name for which none of ``base + ext`` exists (case-insensitive)."""
    existing = {p.name.casefold() for p in directory.iterdir()} if directory.exists() else set()

    def free(b: str) -> bool:
        return all((b + e).casefold() not in existing for e in extensions)

    if free(base):
        return base
    if on_collision == "error":
        raise NamingError(f"{base} already exists")
    n = 2
    while not free(f"{base}_{n}"):
        n += 1
    return f"{base}_{n}"
