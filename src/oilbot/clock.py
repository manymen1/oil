from __future__ import annotations

import socket
import time
import re
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def stamp() -> dict:
    return {
        "utc": utc_now(), "monotonic_ns": time.monotonic_ns(),
        "host_id": socket.gethostname(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "clock_uncertainty_ms": None,
    }


def seconds(after: str, before: str) -> float:
    return (instant(after) - instant(before)).total_seconds()


def epoch_ns(value: str) -> int:
    """Parse ISO timestamps without losing sub-microsecond provider precision."""
    match = re.fullmatch(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,](\d{1,9}))?(?:Z|[+-]\d\d:\d\d)", value)
    if match is None:
        raise ValueError("timezone-aware ISO timestamp with at most nanosecond precision required")
    dt = instant(value)
    delta = dt.replace(microsecond=0) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    digits = match[1] or ""
    return (delta.days * 86400 + delta.seconds) * 10**9 + int(digits.ljust(9, "0") or "0")


def iso_ns(value: int) -> str:
    seconds, nanos = divmod(value, 10**9)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"
