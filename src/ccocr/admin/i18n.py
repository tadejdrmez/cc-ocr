"""Translations for the admin console.

Templates call ``_("English text")``; the English text is the key. A language
is a dict from English to the translated text; missing entries fall back to
English, so a partial translation is safe to ship. Slovenian and Serbian
catalogues are filled once the UI wording is final.
"""

from __future__ import annotations

# Decided 25 Nov 2026: the console ships in English only for now. Add a language
# here once its catalogue below is complete.
LANGUAGES = {
    "en": "English",
}

CATALOG: dict[str, dict[str, str]] = {
    "sl": {
        "Dashboard": "Nadzorna plošča",
        "Profiles": "Profili",
        "Folders": "Mape",
        "Sign in": "Prijava",
        "Sign out": "Odjava",
        "Save": "Shrani",
    },
    "sr-Latn": {},
    "sr-Cyrl": {},
}


def translator(lang: str):  # noqa: ANN201
    table = CATALOG.get(lang, {})

    def _(text: str) -> str:
        return table.get(text, text)

    return _
