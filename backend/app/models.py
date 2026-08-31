"""SQLAlchemy ORM models."""
from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class FileState(str, enum.Enum):
    """Where a file sits in the pipeline."""

    NEW = "new"                  # discovered, no metadata probe yet
    PROBED = "probed"            # ffprobe metadata read
    ANALYZING = "analyzing"      # deep analysis running
    CANDIDATE = "candidate"      # worth converting, has a plan
    SKIPPED = "skipped"          # analysis says: no meaningful gain
    QUEUED = "queued"
    ENCODING = "encoding"
    DONE = "done"                # converted successfully
    FAILED = "failed"
    MISSING = "missing"          # gone from disk on last scan
    IGNORED = "ignored"          # user excluded it by hand


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"        # finished, but result was worse -> discarded


class Setting(Base):
    """Every knob lives here - the UI is the only place settings are set."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LibraryPath(Base):
    __tablename__ = "library_paths"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional per-path override of the global quality profile
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class MediaFile(Base):
    """One video file on disk plus everything ffprobe told us about it."""

    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_media_state", "state"),
        Index("ix_media_library", "library_id"),
        Index("ix_media_saving", "estimated_saving_bytes"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(2048), unique=True)
    library_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_paths.id", ondelete="SET NULL")
    )

    # --- filesystem ---
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime: Mapped[float] = mapped_column(Float, default=0.0)
    container: Mapped[str] = mapped_column(String(32), default="")

    # --- video stream ---
    video_codec: Mapped[str] = mapped_column(String(32), default="")
    profile: Mapped[str] = mapped_column(String(64), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    video_bitrate: Mapped[int] = mapped_column(BigInteger, default=0)
    bit_depth: Mapped[int] = mapped_column(Integer, default=8)
    pix_fmt: Mapped[str] = mapped_column(String(32), default="")
    is_hdr: Mapped[bool] = mapped_column(Boolean, default=False)
    hdr_format: Mapped[str] = mapped_column(String(32), default="")
    color_primaries: Mapped[str] = mapped_column(String(32), default="")
    color_transfer: Mapped[str] = mapped_column(String(32), default="")
    color_space: Mapped[str] = mapped_column(String(32), default="")
    interlaced: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- other streams (list of dicts, kept verbatim for the planner) ---
    audio_streams: Mapped[Any] = mapped_column(JSON, default=list)
    subtitle_streams: Mapped[Any] = mapped_column(JSON, default=list)

    # --- pipeline ---
    state: Mapped[str] = mapped_column(String(24), default=FileState.NEW.value)
    error: Mapped[str] = mapped_column(Text, default="")
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- latest analysis, denormalised so the UI can sort without a join ---
    estimated_size: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_saving_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_saving_pct: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    advisor_note: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[Any] = mapped_column(JSON, nullable=True)
    analysis_depth: Mapped[str] = mapped_column(String(16), default="")  # quick|sample|vmaf
    analyzed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # --- result after a successful conversion ---
    original_size: Mapped[int] = mapped_column(BigInteger, default=0)
    converted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    measured_vmaf: Mapped[float | None] = mapped_column(Float, nullable=True)

    first_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="file", cascade="all, delete-orphan")

    @property
    def saved_bytes(self) -> int:
        if self.original_size and self.size:
            return max(0, self.original_size - self.size)
        return 0


class Job(Base):
    """A single encode attempt."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_job_state", "state"), Index("ix_job_created", "created_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    file: Mapped[MediaFile] = relationship(back_populates="jobs")

    state: Mapped[str] = mapped_column(String(16), default=JobState.QUEUED.value)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower runs first
    plan: Mapped[Any] = mapped_column(JSON, nullable=True)

    progress: Mapped[float] = mapped_column(Float, default=0.0)   # 0..1
    speed: Mapped[float] = mapped_column(Float, default=0.0)      # x realtime
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    eta_seconds: Mapped[int] = mapped_column(Integer, default=0)
    current_size: Mapped[int] = mapped_column(BigInteger, default=0)

    input_size: Mapped[int] = mapped_column(BigInteger, default=0)
    output_size: Mapped[int] = mapped_column(BigInteger, default=0)
    predicted_size: Mapped[int] = mapped_column(BigInteger, default=0)
    vmaf: Mapped[float | None] = mapped_column(Float, nullable=True)

    error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def saved_bytes(self) -> int:
        if self.state == JobState.DONE.value and self.input_size and self.output_size:
            return self.input_size - self.output_size
        return 0


class LearningSample(Base):
    """One (features -> actual outcome) pair produced by a finished encode.

    The predictor fits on these, so size estimates get better the longer
    Optimizarr runs on this specific library with this specific hardware.
    """

    __tablename__ = "learning_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))

    features: Mapped[Any] = mapped_column(JSON)          # dict of float
    predicted_bitrate: Mapped[float] = mapped_column(Float, default=0.0)
    actual_bitrate: Mapped[float] = mapped_column(Float, default=0.0)
    actual_vmaf: Mapped[float | None] = mapped_column(Float, nullable=True)
    encoder: Mapped[str] = mapped_column(String(32), default="")
    crf: Mapped[float] = mapped_column(Float, default=0.0)
    source_codec: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class HistoryEntry(Base):
    """Human-readable activity feed shown on the dashboard."""

    __tablename__ = "history"
    __table_args__ = (Index("ix_history_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info|success|warning|error
    category: Mapped[str] = mapped_column(String(32), default="system")
    message: Mapped[str] = mapped_column(Text, default="")
    file_id: Mapped[int | None] = mapped_column(ForeignKey("media_files.id", ondelete="SET NULL"))
    detail: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ScanRun(Base):
    """Bookkeeping for a library scan so the UI can show live progress."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="running")
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    files_seen: Mapped[int] = mapped_column(Integer, default=0)
    files_new: Mapped[int] = mapped_column(Integer, default=0)
    files_probed: Mapped[int] = mapped_column(Integer, default=0)
    files_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    current_path: Mapped[str] = mapped_column(String(2048), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
