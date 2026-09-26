"""Small auditable controls and diagnostics; no remote clock or network calls."""
import json

from .clock import seconds, utc_now
from .forward_quality import storage_sizes


def reset_source_circuit(store, source_id, reason):
    if not reason.strip():
        raise ValueError("a nonempty review reason is required")
    key = "source:" + source_id
    with store.transaction() as db:
        row = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
        cursor = json.loads(row[0]) if row else {}
        circuit = cursor.get("circuit")
        if not circuit:
            raise ValueError("source has no open circuit")
        rid = store.append("source_circuit_transition", {"source_id": source_id, "state": "RESET_BY_OPERATOR",
            "reason": reason.strip(), "input_revision_ids": [circuit["record_id"]]}, db=db)
        # Re-fetch complete content after parser/access review; a 304 would not
        # demonstrate the broken parser now works.
        for field in ("circuit", "etag", "last_modified", "next_poll"):
            cursor.pop(field, None)
        cursor.update(failures=0, parse_failures=0)
        store.set_cursor(db, key, cursor)
    return rid


def clock_sample(component, previous, current):
    state, drift = "BASELINE", None
    if previous:
        if (current["boot_id"], current["host_id"]) != (previous["boot_id"], previous["host_id"]):
            state = "HOST_OR_BOOT_CHANGED"
        elif current["monotonic_ns"] < previous["monotonic_ns"]:
            state = "MONOTONIC_REGRESSION"
        else:
            drift = seconds(current["utc"], previous["utc"]) - (current["monotonic_ns"] - previous["monotonic_ns"]) / 1e9
            state = "WALL_MONOTONIC_DIVERGENCE" if abs(drift) > 5 else "NO_STEP_DETECTED"
    return {"component": component, "state": state, "previous": previous, "current": current,
            "wall_minus_monotonic_seconds": drift, "absolute_accuracy": "NOT_MEASURED",
            "clock_uncertainty_ms": None, "input_revision_ids": []}


def sample_storage(runtime, config, *, interval_seconds=300):
    at = utc_now()
    prior = runtime.cursor("storage:last_sample")
    if prior and 0 <= seconds(at, prior) < interval_seconds:
        return
    sample = storage_sizes(config)
    with runtime.transaction() as db:
        runtime.append("storage_sample", sample, available_at=at, db=db)
        runtime.set_cursor(db, "storage:last_sample", at)
