"""Shared results and exceptions for encoding and output validation."""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class EncodeOutcome:
    ok: bool = False
    rejected: bool = False
    requeued: bool = False             # put back into the queue, nothing ran
    reason: str = ""
    output_size: int = 0
    input_size: int = 0
    vmaf: float | None = None          # on the VMAF scale
    quality_metric: str = ""
    quality_value: float | None = None
    quality_details: dict = field(default_factory=dict)
    elapsed: float = 0.0
    log_tail: str = ""
    fell_back_to_cpu: bool = False
    hw_failure_reason: str = ""        # why the GPU path was abandoned


class JobCancelled(Exception):
    pass


class SourceChangedError(RuntimeError):
    """The original was modified while it was being encoded."""
