"""Movie overview: title and year from folder names, everything that is not a series."""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.core import movies  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, LibraryPath, MediaFile  # noqa: E402

GiB = 1024**3


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    yield
    engine.dispose()


@pytest.fixture()
def client():
    # No lifespan: the queue worker must not start.
    return TestClient(app)


def _library(path: str, name: str) -> int:
    with db.session_scope() as s:
        lib = LibraryPath(path=path, name=name, enabled=True)
        s.add(lib)
        s.flush()
        return lib.id


def _add(lib_id, path, *, codec="h264", state="candidate", size=10 * GiB, **kw) -> int:
    with db.session_scope() as s:
        row = MediaFile(
            path=path, library_id=lib_id, video_codec=codec, state=state, size=size,
            width=1920, height=1080,
            estimated_saving_bytes=kw.pop("estimated_saving_bytes", 4 * GiB if state == "candidate" else 0),
            plan={"encoder": "libsvtav1"} if state == "candidate" else None,
            **kw,
        )
        s.add(row)
        s.flush()
        return row.id


@pytest.mark.parametrize("name,expected", [
    ("Dune (2021)", ("Dune", 2021)),
    ("1917 (2019)", ("1917", 2019)),
    ("Blade.Runner.2049.2017.1080p.BluRay", ("Blade Runner 2049", 2017)),
    ("Loose Film 2010", ("Loose Film", 2010)),
    ("Heat", ("Heat", None)),
    # A resolution is not a year.
    ("Movie.1920x1080", ("Movie 1920x1080", None)),
])
def test_title_and_year(name, expected):
    assert movies.title_year(name) == expected


def test_movies_are_everything_that_is_not_a_series(client):
    films = _library("/media/movies", "Filme")
    tv = _library("/media/tv", "Serien")
    _add(films, "/media/movies/Dune (2021)/Dune (2021).mkv", codec="av1", state="done",
         size=8 * GiB, original_size=20 * GiB, converted_at=dt.datetime(2026, 9, 1))
    heat = _add(films, "/media/movies/Heat (1995)/Heat (1995).mkv", codec="hevc", state="skipped",
                decision_reason="HEVC / H.265 ist von der Konvertierung ausgeschlossen.")
    main = _add(films, "/media/movies/Arrival (2016)/Arrival (2016).mkv", size=12 * GiB)
    extra = _add(films, "/media/movies/Arrival (2016)/Featurette.mkv", state="probed", size=GiB)
    _add(films, "/media/movies/Loose.Film.2010.mkv")
    _add(tv, "/media/tv/Dark/Season 01/Dark - S01E01.mkv")

    body = client.get("/api/movies").json()
    assert [(m["title"], m["year"]) for m in body["items"]] == [
        ("Arrival", 2016), ("Dune", 2021), ("Heat", 1995), ("Loose Film", 2010),
    ]
    arrival, dune, heat_movie, _ = body["items"]

    # Versions and extras stay one movie, the largest file first.
    assert arrival["episodes"] == 2
    assert [f["id"] for f in arrival["files"]] == [main, extra]
    assert arrival["files"][0]["bucket"] == "pending"
    assert arrival["potential_saving"] == 4 * GiB

    assert dune["in_av1"] == 1 and dune["saved_bytes"] == 12 * GiB

    assert heat_movie["files"][0]["id"] == heat
    assert heat_movie["files"][0]["bucket"] == "excluded"
    assert "ausgeschlossen" in heat_movie["files"][0]["decision_reason"]

    assert body["totals"]["episodes"] == 5          # the series episode is not counted


def test_the_series_view_does_not_list_movies(client):
    films = _library("/media/movies", "Filme")
    _add(films, "/media/movies/Dune (2021)/Dune (2021).mkv")
    assert client.get("/api/series").json()["items"] == []
