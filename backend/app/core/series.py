"""Group library files into series, seasons and episodes.

There is no Sonarr connection - the folder layout is the source of truth.  The
layout Sonarr produces (and most people keep by hand) is

    <library>/<Series>/Season 01/<Series> - S01E01 - Title.mkv

so the series is the first folder below the library root, season and episode
come from ``S01E01`` (or ``1x01``) in the file name, and a season folder fills
in the season when the name has none.  A folder only counts as a series when at
least one file in it looks like an episode; every other folder - and every loose
file that does not look like an episode - is a movie (see ``movies``).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..models import FileState
from . import codecs

_SXXEYY = re.compile(r"(?<![a-z0-9])s(\d{1,3})[ ._-]?e(\d{1,4})", re.IGNORECASE)
# "1x02" - but not the "20x108" inside "1920x1080".
_NXNN = re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)", re.IGNORECASE)
_SEASON_DIR = re.compile(r"^(?:season|staffel|saison|series|s)[ ._-]*(\d{1,3})$", re.IGNORECASE)
_SPECIALS_DIRS = {"specials", "special"}

# Order matters: what a file *is* (converted, on its way, already AV1) beats
# what the analysis last said about it.
BUCKETS = ("converted", "av1", "active", "pending", "excluded", "failed", "other")

# What a forced run over a series or season picks up.  AV1 files stay out: they
# are where the rest is headed, and re-encoding them in bulk only costs quality.
# A single one can still be forced from its own row.
FORCEABLE = ("pending", "excluded", "failed", "other")


@dataclass(frozen=True)
class Placement:
    """Where one file sits in a series."""

    series: str
    folder: str             # series folder below the library root, "" for loose files
    season: int | None
    episode: int | None


def _numbers_from_name(stem: str) -> tuple[int | None, int | None, int]:
    """(season, episode, offset of the marker) from a file name."""
    for pattern in (_SXXEYY, _NXNN):
        match = pattern.search(stem)
        if match:
            return int(match.group(1)), int(match.group(2)), match.start()
    return None, None, -1


def _season_from_folder(name: str) -> int | None:
    name = name.strip()
    if name.casefold() in _SPECIALS_DIRS:
        return 0
    match = _SEASON_DIR.match(name)
    return int(match.group(1)) if match else None


def _series_from_name(stem: str, marker_at: int) -> str:
    """"Show.Name.S01E01" -> "Show Name", for files lying loose in the library root."""
    head = stem[:marker_at] if marker_at > 0 else stem
    head = re.sub(r"[._]+", " ", head).strip(" -")
    return head or stem


def place(path: str, library_root: str) -> Placement | None:
    """Group, season and episode of one file; None when it is not below the root.

    A file lying loose in the root is a group of its own, named after the file.
    """
    norm = path.replace("\\", "/")
    root = library_root.replace("\\", "/").rstrip("/")
    if not norm.startswith(root + "/"):
        return None
    parts = [p for p in norm[len(root) + 1:].split("/") if p]
    if not parts:
        return None

    stem = parts[-1].rsplit(".", 1)[0]
    season, episode, marker_at = _numbers_from_name(stem)
    if season is None:
        for folder in reversed(parts[1:-1]):
            season = _season_from_folder(folder)
            if season is not None:
                break

    if len(parts) == 1:
        return Placement(_series_from_name(stem, marker_at), "", season, episode)
    return Placement(parts[0], parts[0], season, episode)


def bucket(state: str, video_codec: str, ignored: bool) -> str:
    """Which segment of the progress bar one file belongs to."""
    if state == FileState.DONE.value:
        return "converted"
    if state in (FileState.QUEUED.value, FileState.ENCODING.value):
        return "active"
    if codecs.normalise(video_codec) == "av1":
        return "av1"
    if state == FileState.FAILED.value:
        return "failed"
    if ignored or state in (FileState.SKIPPED.value, FileState.IGNORED.value):
        return "excluded"
    if state == FileState.CANDIDATE.value:
        return "pending"
    return "other"          # new, probed, analyzing, missing


def season_label(season: int | None) -> str:
    if season is None:
        return "Ohne Staffel"
    if season == 0:
        return "Specials"
    return f"Staffel {season}"


def season_order(season: int | None) -> tuple[int, int]:
    """Numbered seasons first, then specials, then files without a season."""
    if season is None:
        return (2, 0)
    if season == 0:
        return (1, 0)
    return (0, season)


def make_key(library_id: int, name: str) -> str:
    return f"{library_id}:{name}"


def split_key(key: str) -> tuple[int, str] | None:
    library_id, sep, name = key.partition(":")
    if not sep or not library_id.isdigit() or not name:
        return None
    return int(library_id), name


@dataclass
class Tally:
    """File counts and sizes for a series or one of its seasons."""

    episodes: int = 0
    total_size: int = 0
    saved_bytes: int = 0
    potential_saving: int = 0
    last_converted: dt.datetime | None = None
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0))

    def add(self, kind: str, row: Any) -> None:
        self.episodes += 1
        self.total_size += row.size or 0
        self.counts[kind] += 1
        if kind == "converted":
            # Only a replaced original freed anything.  In sidecar mode
            # original_size stays 0 and both files are still on disk.
            if row.original_size and row.size:
                self.saved_bytes += max(0, row.original_size - row.size)
            self._seen_conversion(row.converted_at)
        elif kind == "pending":
            self.potential_saving += max(0, row.estimated_saving_bytes or 0)

    def merge(self, other: Tally) -> None:
        self.episodes += other.episodes
        self.total_size += other.total_size
        self.saved_bytes += other.saved_bytes
        self.potential_saving += other.potential_saving
        for kind, count in other.counts.items():
            self.counts[kind] += count
        self._seen_conversion(other.last_converted)

    def _seen_conversion(self, when: dt.datetime | None) -> None:
        if when and (self.last_converted is None or when > self.last_converted):
            self.last_converted = when

    @property
    def in_av1(self) -> int:
        return self.counts["converted"] + self.counts["av1"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "episodes": self.episodes,
            "in_av1": self.in_av1,
            "total_size": self.total_size,
            "saved_bytes": self.saved_bytes,
            "potential_saving": self.potential_saving,
            "counts": dict(self.counts),
        }


@dataclass
class Episode:
    file_id: int
    season: int | None
    episode: int | None
    bucket: str
    row: Any = field(default=None, repr=False, compare=False)   # the columns it was grouped from


@dataclass
class SeriesGroup:
    library_id: int
    library: str
    name: str
    path: str
    tally: Tally = field(default_factory=Tally)
    seasons: dict[int | None, Tally] = field(default_factory=dict)
    episodes: list[Episode] = field(default_factory=list)

    @property
    def key(self) -> str:
        return make_key(self.library_id, self.name)

    @property
    def looks_like_series(self) -> bool:
        return any(e.season is not None or e.episode is not None for e in self.episodes)


def group(rows: Iterable[Any], libraries: dict[int, tuple[str, str]]) -> list[SeriesGroup]:
    """Sort files into folder groups - series and movies alike.

    ``rows`` need ``id``, ``path``, ``library_id``, ``state``, ``video_codec``,
    ``ignored``, ``size``, ``original_size``, ``estimated_saving_bytes`` and
    ``converted_at``.  ``libraries`` maps a library id to (root path, name).
    """
    found: dict[tuple[int, str], SeriesGroup] = {}
    for row in rows:
        library = libraries.get(row.library_id)
        if library is None:
            continue
        root, library_name = library
        spot = place(row.path, root)
        if spot is None:
            continue

        entry = found.get((row.library_id, spot.series))
        if entry is None:
            base = root.replace("\\", "/").rstrip("/")
            entry = found[(row.library_id, spot.series)] = SeriesGroup(
                library_id=row.library_id, library=library_name, name=spot.series,
                path=f"{base}/{spot.folder}" if spot.folder else (base or "/"),
            )
        kind = bucket(row.state, row.video_codec, bool(row.ignored))
        entry.episodes.append(Episode(row.id, spot.season, spot.episode, kind, row))
        entry.tally.add(kind, row)
        entry.seasons.setdefault(spot.season, Tally()).add(kind, row)

    # Callers pick: ``looks_like_series`` for the series view, the rest are movies.
    return sorted(found.values(), key=lambda g: (g.name.casefold(), g.library_id))
