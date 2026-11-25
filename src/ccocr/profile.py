"""Processing profiles.

In the finished product profiles are edited in the admin UI and stored in the
database; YAML is only an import/export format (spec 2.7). The proof of
concept reads the YAML directly.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .rules import Rule, compile_rule

DEFAULTS: dict[str, Any] = {
    "profile": "default",
    "document_type": "document",
    "classification": "",
    "optimiser": {
        "despeckle": True,
        "contrast": True,
        "binarise": "sauvola",  # sauvola | otsu | none
        "deskew": True,
        "max_deskew_deg": 10.0,
        "crop": True,
        "dpi": 300,
        "min_dpi": 200,
        "blank_threshold": 0.004,  # ink share of the inner page area
        "pdf_image": "original",  # original | optimised
    },
    "ocr": {
        "osd": True,
        "default": ["slv", "eng"],
        "latin": ["slv", "hrv", "srp_latn", "eng"],
        "cyrillic": ["srp", "srp_latn"],
        "min_confidence": 60,
        "output": "auto",  # auto = pdfa-3 for single-file output, pdfa-2 for sidecars
    },
    "split": {
        "proposals": ["qr-separator", "blank-page", "text-rule"],
        "zone": 0.25,
        "fixed_count": None,
        "rules": [],
        # Proof of concept only: there is no operator screen yet, so proposals
        # are accepted automatically and the report marks them as such.
        "auto_accept": True,
        "drop_blank_pages": True,
    },
    "output": {
        # single  = one PDF/A-3 per document: metadata XML embedded, PAdES document timestamp
        # sidecars = PDF/A-2 + .xml + .manifest.xml + .tsr next to each other
        "package": "single",
        "metadata": ["xml"],  # xml and/or filename
        "naming": "{doctype}_{batch}_{seq}",
        "date_format": "%Y-%m-%d",
        "max_length": 120,
        "on_collision": "suffix",
    },
    "timestamp": {
        "mode": "openssl-local",  # openssl-local | http | none
        "url": "",
        "ca_file": "",
        "tsa_cert": "",
        "openssl_dir": "",
        "qualified": False,
        "hash": "sha256",
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Profile:
    raw: dict[str, Any]
    rules: list[Rule]

    @property
    def name(self) -> str:
        return self.raw["profile"]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


class ProfileError(ValueError):
    pass


def load_profile(source: str | Path | dict[str, Any] | None) -> Profile:
    if source is None:
        data: dict[str, Any] = {}
    elif isinstance(source, dict):
        data = source
    else:
        data = yaml.safe_load(Path(source).read_text(encoding="utf-8")) or {}
    raw = _merge(DEFAULTS, data)

    opt = raw["optimiser"]
    if opt["pdf_image"] not in {"original", "optimised"}:
        raise ProfileError("optimiser.pdf_image must be 'original' or 'optimised'")
    if opt["binarise"] not in {"sauvola", "otsu", "none"}:
        raise ProfileError("optimiser.binarise must be sauvola, otsu or none")
    for m in raw["output"]["metadata"]:
        if m not in {"xml", "sidecar-xml", "filename"}:
            raise ProfileError(f"unknown output.metadata mode {m!r}")
    if raw["output"]["package"] not in {"single", "sidecars"}:
        raise ProfileError("output.package must be 'single' or 'sidecars'")
    if raw["timestamp"]["mode"] not in {"openssl-local", "http", "none"}:
        raise ProfileError("timestamp.mode must be openssl-local, http or none")
    known = {"qr-separator", "blank-page", "text-rule", "fixed-count", "barcode"}
    for p in raw["split"]["proposals"]:
        if p not in known:
            raise ProfileError(f"unknown split proposal {p!r}")

    rules = []
    for i, r in enumerate(raw["split"]["rules"] or []):
        if not isinstance(r, dict) or "expr" not in r:
            raise ProfileError(f"split.rules[{i}] needs an 'expr'")
        meta = {k: str(v) for k, v in r.items() if k in {"doctype", "classification"} and v}
        rules.append(compile_rule(r.get("name") or f"rule-{i + 1}", r["expr"], meta))
    return Profile(raw=raw, rules=rules)
