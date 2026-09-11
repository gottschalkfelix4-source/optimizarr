"""Movies: every folder group that is not a series (see ``series.group``).

Radarr names a movie folder "Title (Year)"; release names put the year between
dots.  Both are understood, anything else keeps its name and has no year.
"""
from __future__ import annotations

import re

_PAREN_YEAR = re.compile(r"^(.*?)[\s._-]*[(\[]((?:19|20)\d{2})[)\]]")
# A year stands on its own - not the "1920" in "1920x1080".
_BARE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?![\dx])")


def _clean(text: str) -> str:
    return re.sub(r"[._]+", " ", text).strip(" -")


def title_year(name: str) -> tuple[str, int | None]:
    """"Dune (2021)" -> ("Dune", 2021); "Blade.Runner.2049.2017.1080p" ->
    ("Blade Runner 2049", 2017) - the last year wins, so a year in the title
    stays part of it."""
    match = _PAREN_YEAR.match(name)
    if match and _clean(match.group(1)):
        return _clean(match.group(1)), int(match.group(2))
    years = [m for m in _BARE_YEAR.finditer(name) if m.start() > 0]
    if years:
        last = years[-1]
        title = _clean(name[:last.start()])
        if title:
            return title, int(last.group(1))
    return _clean(name) or name, None
