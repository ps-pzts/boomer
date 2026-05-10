from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass
class Alert:
    severity: AlertSeverity
    title: str
    body: str
    source_task_id: str | None = None
    channels_tried: list[str] = field(default_factory=list)
    channels_ok: list[str] = field(default_factory=list)
