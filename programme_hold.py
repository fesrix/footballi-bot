"""Temporary publishing hold for two programmes."""

import html
import os
import re
import unicodedata
from urllib.parse import unquote


def programme_hold_enabled():
    return os.getenv("FOOTBALLI_PROGRAMME_HOLD", "").strip().casefold() not in {
        "false", "0", "off", "no"
    }


_SEPARATOR = r"[\W_]*"
_PROGRAMMES = {
    "football_bartar": (
        ("فوتبال", "برتر"), ("فوتبال", "360", "برتر"),
        ("football", "bartar"), ("footballi", "bartar"), ("futbal", "bartar"),
        ("football", "360", "bartar"), ("football", "360", "برتر"),
        ("football", "bartare"), ("futball", "bartar"),
    ),
    "shabhaye_footballi": (("شب", "های", "فوتبالی"), ("شب", "ها", "فوتبالی"))
    + tuple(("shab", plural, noun) for plural in ("haye", "hay", "ha")
              for noun in ("footballi", "footbali", "futbali")),
}
_PATTERNS = {
    name: re.compile(r"(?<![^\W_])(?:" + "|".join(
        _SEPARATOR.join(map(re.escape, parts)) for parts in aliases
    ) + r")(?![^\W_])")
    for name, aliases in _PROGRAMMES.items()
}
_PERSIAN_CHARS = str.maketrans("يك۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "یک01234567890123456789")


def _text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _text_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _text_values(child)


def blocked_programme(*metadata):
    """Return a programme code when any supplied metadata field matches."""
    if not programme_hold_enabled():
        return ""
    for value in _text_values(metadata):
        decoded = unicodedata.normalize("NFKC", html.unescape(unquote(value))).translate(_PERSIAN_CHARS)
        normalized = "".join(ch for ch in decoded.casefold() if unicodedata.category(ch) != "Mn")
        for name, pattern in _PATTERNS.items():
            if pattern.search(normalized):
                return name
    return ""


def blocked_candidate_programme(candidate, *metadata):
    # Playlist and priority labels are inferred by the bot. Source metadata
    # can carry its own category inside source_metadata and is still checked.
    source_fields = {key: value for key, value in candidate.items()
                     if key not in {"category", "priority_reason", "programme_hold"}}
    return blocked_programme(source_fields, *metadata)
