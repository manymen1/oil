"""Frozen prospective EIA experiment with actual worker clocks and no orders.

The worker stores immutable decisions, including late/missing-input abstentions.
An exporter adds matured hypothetical labels without reconstructing decisions.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from zoneinfo import ZoneInfo

from .clock import epoch_ns, instant, iso_ns, utc_now
from .databento import file_hash
from .features import MarketView
from .inventory_strategy import evaluate_inventory, inventory_features, latest_macro, load_inventory_rules
from .macro import read_macro
from .macro_dataset import COMMON_EXCLUSIONS, comparisons
from .macro_scheduler import validate_schedule
from .market import atomic_json
from .market_curve import read_curve
from .outcomes import HORIZONS, OutcomePolicy, forward_outcomes
from .research_stats import amount, paired_uncertainty
from .schema import canonical, digest
from .store import Journal, component_lock

BASELINES = ["no_trade", "inventory_only", "price_only_60s", "inventory_plus_price", "inventory_continuation_v2"]


def code_identity():
    # Hash the complete package: parsing, timing, pricing and evaluation changes
    # can alter an experiment even when the strategy class remains unchanged.
    return {path.name: file_hash(path) for path in sorted(Path(__file__).parent.glob("*.py"))}


def validate_spec(spec):
    required = {"schema", "version", "evaluation_start", "holdout_start", "evaluation_end", "rules",
        "outcome_policy", "horizon_seconds", "decision_lateness_seconds", "grouping", "missing_data",
        "baselines", "statistical_method", "extra_round_trip_costs_usd", "latency_stress_ms", "fixed_monthly_cost_usd"}
    if set(spec) != required or spec["schema"] != "eia-shadow-spec-v1":
        raise ValueError("complete EIA shadow specification required")
    if not isinstance(spec["version"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,80}", spec["version"]):
        raise ValueError("explicit experiment version required")
    if not epoch_ns(spec["evaluation_start"]) < epoch_ns(spec["holdout_start"]) < epoch_ns(spec["evaluation_end"]):
        raise ValueError("fixed chronological start, holdout and end required")
    rules, costs = load_inventory_rules(spec["rules"]), OutcomePolicy(**spec["outcome_policy"])
    costs.validate()
    if rules.version != "inventory-continuation-v2-draft" or Decimal(rules.fee_per_contract_side) != Decimal(costs.fee_per_contract_side) or rules.slippage_ticks_per_side != costs.slippage_ticks_per_side:
        raise ValueError("matching v2 decision and outcome costs required")
    if type(spec["horizon_seconds"]) is not int or spec["horizon_seconds"] not in HORIZONS:
        raise ValueError("supported preselected horizon required")
    if type(spec["decision_lateness_seconds"]) is not int or not 1 <= spec["decision_lateness_seconds"] <= 30:
        raise ValueError("decision lateness must be 1..30 seconds")
    if spec["grouping"] != "eia_reporting_period" or spec["missing_data"] != "retain_null_exclude_common_comparisons" or spec["baselines"] != BASELINES:
        raise ValueError("fixed release grouping, common population and baselines required")
    method = spec["statistical_method"]
    if set(method) != {"block_groups", "draws", "seed", "minimum_train_groups", "minimum_holdout_groups", "minimum_holdout_trades"}:
        raise ValueError("predeclared block bootstrap and sample screens required")
    paired_uncertainty([], BASELINES, BASELINES[-1], **{k: method[k] for k in ("block_groups", "draws", "seed")})
    for key in ("minimum_train_groups", "minimum_holdout_groups", "minimum_holdout_trades"):
        if type(method[key]) is not int or method[key] < 1:
            raise ValueError("positive predeclared sample screens required")
    stresses = spec["extra_round_trip_costs_usd"]
    if not isinstance(stresses, list) or len(stresses) < 2 or any(not isinstance(v, str) for v in stresses):
        raise ValueError("predeclared decimal-string cost scenarios required")
    if len(set(stresses)) != len(stresses) or amount(stresses[0]) != 0:
        raise ValueError("unique cost scenarios starting at zero required")
    for value in stresses:
        amount(value)
    delays = spec["latency_stress_ms"]
    if not isinstance(delays, list) or not delays or delays[0] != 0 or len(set(delays)) != len(delays) or any(type(v) is not int or not 0 <= v <= 60000 for v in delays):
        raise ValueError("unique predeclared latency scenarios required")
    if spec["fixed_monthly_cost_usd"] is not None:
        amount(spec["fixed_monthly_cost_usd"])
    return rules, costs


def freeze_shadow(spec, schedule, destination, *, clock=utc_now):
    validate_spec(spec)
    validate_schedule(schedule)
    now = clock()
    if epoch_ns(now) >= epoch_ns(spec["evaluation_start"]):
        raise ValueError("freeze must precede prospective evaluation start")
    target = Path(destination).resolve()
    if target.exists():
        raise ValueError("protocol output already exists")
    bundle = target.parent / (target.stem + "-code")
    if bundle.exists():
        raise ValueError("frozen code output already exists")
    protocol = {"schema": "eia-shadow-protocol-v1", "frozen_at": now, "spec": spec,
        "spec_hash": digest(spec), "calendar": schedule, "calendar_hash": digest(schedule),
        "code_files": code_identity(), "code_directory": bundle.name, "trade_authorized": False, "promotion": False,
        "market_data_qualified": False, "broker_execution": "disabled"}
    protocol["id"] = digest(protocol)
    target.parent.mkdir(parents=True, exist_ok=True)
    (bundle / "oilbot").mkdir(parents=True)
    for name, expected in protocol["code_files"].items():
        shutil.copyfile(Path(__file__).parent / name, bundle / "oilbot" / name)
        if file_hash(bundle / "oilbot" / name) != expected:
            raise ValueError("source changed while freezing experiment code")
    with target.open("x") as stream:
        stream.write(canonical(protocol) + "\n")
    return protocol


def verify_shadow_protocol(protocol):
    if (protocol.get("schema") != "eia-shadow-protocol-v1"
            or protocol.get("id") != digest({k: v for k, v in protocol.items() if k != "id"})
            or protocol.get("spec_hash") != digest(protocol["spec"])
            or protocol.get("calendar_hash") != digest(protocol["calendar"])
            or protocol.get("trade_authorized") is not False or protocol.get("promotion") is not False
            or protocol.get("market_data_qualified") is not False or protocol.get("broker_execution") != "disabled"):
        raise ValueError("changed or unauthorized shadow protocol")
    validate_spec(protocol["spec"])
    validate_schedule(protocol["calendar"])
    if epoch_ns(protocol["frozen_at"]) >= epoch_ns(protocol["spec"]["evaluation_start"]):
        raise ValueError("protocol was not frozen before evaluation")
    if protocol["code_files"] != code_identity():
        raise ValueError("experiment code changed; run the frozen code or create a new prospective protocol")
    return protocol


def load_shadow_protocol(path):
    path = Path(path).resolve()
    protocol = verify_shadow_protocol(json.loads(path.read_text()))
    bundle = (path.parent / protocol["code_directory"]).resolve()
    if bundle.parent != path.parent or not all(file_hash(bundle / "oilbot" / name) == expected for name, expected in protocol["code_files"].items()):
        raise ValueError("frozen experiment source checksum mismatch")
    return protocol


def shadow_rows(path, protocol, *, through):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
            "SELECT * FROM records WHERE available_at<=? ORDER BY seq", (instant(through).isoformat(timespec="microseconds"),))]
    bindings = [r for r in rows if r["kind"] == "shadow_protocol"]
    if len(bindings) != 1 or bindings[0]["payload"] != {"protocol_id": protocol["id"]}:
        raise ValueError("shadow journal protocol mismatch")
    for row in rows:
        if row["id"] != digest([row["kind"], row["payload"]]):
            raise ValueError("changed shadow record identity")
    return rows


def append_shadow(journal, kind, payload, *, at, db=None):
    return journal.append(kind, payload, record_id=digest([kind, payload]), available_at=at, db=db)


def comparison_exclusions(decision):
    return sorted({r for r in decision["reason_codes"] if r in COMMON_EXCLUSIONS
        or r in {"MISSING_CONTINUATION_CONTEXT", "MISSED_FROZEN_DECISION_WINDOW", "MARKET_INPUT_UNAVAILABLE", "UNEXPECTED_RELEASE_PERIOD"}
        or r.startswith(("INCOMPLETE_CONTINUATION_MARKET:", "CONTINUATION_CONTRACT_CHANGED:", "CONTINUATION_LIQUIDITY:"))})


def release_events(protocol):
    policy, spec = protocol["calendar"], protocol["spec"]
    zone = ZoneInfo(policy["timezone"])
    s, result = policy["sources"]["eia"], []
    day, end = date.fromisoformat(policy["calendar_start"]), date.fromisoformat(policy["calendar_end"])
    while day < end:
        if day.weekday() == s["weekday"]:
            local = datetime.fromisoformat(s["overrides"].get(day.isoformat(), day.isoformat() + "T" + s["release_time"]))
            at = local.replace(tzinfo=zone).astimezone(instant(spec["evaluation_start"]).tzinfo).isoformat()
            if epoch_ns(spec["evaluation_start"]) <= epoch_ns(at) < epoch_ns(spec["evaluation_end"]):
                result.append({"release_group": "eia:" + (day - timedelta(days=s["period_lag_days"])).isoformat(),
                    "expected_release_at": at, "grace_seconds": s["publication_grace_seconds"]})
        day += timedelta(days=1)
    return result


def shadow_tick(protocol_path, macro_path, market_path, root, *, clock=utc_now, _market=None, _cache=None):
    protocol = load_shadow_protocol(protocol_path)
    spec = protocol["spec"]
    rules, costs = validate_spec(spec)
    macro_path, root = Path(macro_path).resolve(), Path(root).resolve()
    inputs = [macro_path.parent, Path(protocol_path).resolve().parent]
    if market_path is not None:
        inputs.append(Path(market_path).resolve().parent)
    if any(root == source or source in root.parents for source in inputs):
        raise ValueError("shadow state must be outside input roots")
    sampled_at = clock()
    sampled_ns = epoch_ns(sampled_at)
    if sampled_ns >= epoch_ns(spec["evaluation_end"]):
        return {"state": "EXPERIMENT_ENDED", "protocol_id": protocol["id"], "trade_authorized": False}
    if sampled_ns < epoch_ns(spec["evaluation_start"]):
        return {"state": "WAITING_FOR_START", "protocol_id": protocol["id"], "trade_authorized": False}
    local_day = instant(sampled_at).astimezone(ZoneInfo(protocol["calendar"]["timezone"])).date()
    if not date.fromisoformat(protocol["calendar"]["calendar_start"]) <= local_day < date.fromisoformat(protocol["calendar"]["calendar_end"]):
        return {"state": "CALENDAR_RENEWAL_REQUIRED", "protocol_id": protocol["id"], "trade_authorized": False}
    observations = None
    if _cache is not None:
        with closing(sqlite3.connect(macro_path.as_uri() + "?mode=ro", uri=True)) as db:
            signature = db.execute("SELECT seq,id FROM records WHERE kind='macro_revision' AND available_at<=? ORDER BY seq DESC LIMIT 1",
                (instant(sampled_at).isoformat(timespec="microseconds"),)).fetchone()
        key = str(macro_path), signature
        if _cache.get("key") == key:
            observations = _cache["observations"]
    if observations is None:
        observations = read_macro(macro_path, through=sampled_at)
        if _cache is not None:
            _cache.update(key=key, observations=observations)
    known_eia = [r for r in observations if r["payload"]["source"] == "eia"
        and epoch_ns(spec["evaluation_start"]) <= epoch_ns(r["payload"]["received_at"]) < epoch_ns(spec["evaluation_end"])]
    root.mkdir(parents=True, exist_ok=True)
    with component_lock(root, "shadow"):
        journal = Journal(root / "shadow.sqlite3")
        binding = {"protocol_id": protocol["id"]}
        existing = journal.records("shadow_protocol")
        if existing and (len(existing) != 1 or existing[0]["payload"] != binding):
            raise ValueError("different shadow protocol; use new state")
        if not existing:
            append_shadow(journal, "shadow_protocol", binding, at=sampled_at)
        # Persist first-seen observations before computing. Existing completed
        # release groups survive restart and cannot be decided a second time.
        groups = {}
        for row in known_eia:
            group = "eia:" + row["payload"]["observation"]["period"]
            groups.setdefault(group, row)
        pending, written = 0, []
        expected_groups = {r["release_group"] for r in release_events(protocol)}
        for group, eia in sorted(groups.items(), key=lambda item: item[1]["seq"]):
            if journal.cursor("shadow_done:" + group):
                continue
            due_ns = epoch_ns(eia["payload"]["received_at"]) + rules.confirmation_seconds * 10**9
            if sampled_ns < due_ns:
                pending += 1
                continue
            # All input reads are bounded by the actual processing start. A
            # computation's completion becomes the actionable decision clock.
            feature_at = clock()
            if epoch_ns(feature_at) < sampled_ns:
                raise ValueError("shadow worker clock regressed")
            available = [r for r in observations if epoch_ns(r["available_at"]) <= epoch_ns(feature_at)]
            cot = latest_macro(available, "cftc")
            features, market = None, None
            failures = []
            try:
                market = _market if _market is not None else read_curve(market_path, through=feature_at,
                    start=iso_ns(epoch_ns(eia["payload"]["received_at"]) - 310 * 10**9)) if market_path is not None else None
                if market is not None:
                    market = {"records": [r for r in market["records"] if epoch_ns(r["payload"]["available_at"]) <= epoch_ns(feature_at)],
                        "gaps": [g for g in market["gaps"] if not g.get("available_at") or epoch_ns(g["available_at"]) <= epoch_ns(feature_at)]}
                    features = inventory_features(MarketView(market["records"], market["gaps"]), eia["payload"]["received_at"], feature_at, rules)
            except (ValueError, OSError, sqlite3.Error):
                failures.append("MARKET_INPUT_UNAVAILABLE")
                features, market = None, None
            decision = evaluate_inventory(eia, cot, features, at=feature_at, rules=rules)
            current = latest_macro(available, "eia")
            if current is not None and current["id"] != eia["id"]:
                failures.append("SUPERSEDED_BEFORE_DECISION")
                decision["input_revision_ids"].append(current["id"])
            completed_at = clock()
            if epoch_ns(completed_at) < epoch_ns(feature_at):
                raise ValueError("shadow worker clock regressed")
            if epoch_ns(completed_at) > due_ns + spec["decision_lateness_seconds"] * 10**9:
                failures.append("MISSED_FROZEN_DECISION_WINDOW")
            if group not in expected_groups:
                failures.append("UNEXPECTED_RELEASE_PERIOD")
            if failures:
                decision["action"] = "ABSTAIN"
                decision["reason_codes"].extend(failures)
                # Inventory-only/price-only counterfactual directions remain
                # visible, but common comparisons exclude the failed event.
                decision["baselines"]["inventory_plus_price"] = 0
                decision["baselines"]["inventory_continuation_v2"] = 0
            row = {"schema": "eia-shadow-decision-v1", "protocol_id": protocol["id"], "release_group": group,
                "revision_id": eia["id"], "received_at": eia["payload"]["received_at"], "available_at": eia["available_at"],
                "due_at": iso_ns(due_ns), "feature_at": feature_at, "decision_at": completed_at,
                "decision_clock": "actual_worker_completion", "processing_ms": (epoch_ns(completed_at) - epoch_ns(feature_at)) / 1e6,
                "decision_lateness_ms": (epoch_ns(completed_at) - due_ns) / 1e6,
                "initial_snapshot": eia["payload"]["initial_snapshot"], "revision": eia["payload"]["revision"],
                "supersedes_id": eia["payload"]["supersedes_id"], "decision": decision, "features": features,
                "comparison_exclusions": comparison_exclusions(decision),
                "macro_input_hash": digest(available), "market_input_hash": digest(market) if market else None,
                "market_modes": sorted({r["payload"]["data_mode"] for r in market["records"] if r["kind"] == "market"}) if market else [],
                "dataset_role": "engineering_fixture" if _market is not None else "prospective_unqualified_research",
                "trade_authorized": False, "authorized_contracts": 0, "market_data_qualified": False}
            with journal.transaction() as db:
                rid = append_shadow(journal, "shadow_decision", row, at=completed_at, db=db)
                journal.set_cursor(db, "shadow_done:" + group, rid)
            written.append(rid)
        heartbeat_at = clock()
        prior_heartbeat = journal.cursor("shadow_heartbeat")
        if written or pending or not prior_heartbeat or epoch_ns(heartbeat_at) - epoch_ns(prior_heartbeat) >= 30 * 10**9:
            with journal.transaction() as db:
                append_shadow(journal, "shadow_tick", {"protocol_id": protocol["id"], "checked_at": heartbeat_at,
                    "decisions_written": written, "pending_groups": pending, "known_groups": len(groups),
                    "trade_authorized": False}, at=heartbeat_at, db=db)
                journal.set_cursor(db, "shadow_heartbeat", heartbeat_at)
    return {"state": "OBSERVING", "protocol_id": protocol["id"], "decisions_written": written,
        "pending_groups": pending, "known_groups": len(groups), "trade_authorized": False}


def shadow_worker(protocol_path, macro_path, market_path, root, *, stop=None, once=False):
    stop = stop or threading.Event()
    result = None
    cache = {}
    while not stop.is_set():
        result = shadow_tick(protocol_path, macro_path, market_path, root, _cache=cache)
        if once or result["state"] in {"EXPERIMENT_ENDED", "CALENDAR_RENEWAL_REQUIRED"}:
            return result
        stop.wait(1)
    return result or {"state": "STOPPED", "trade_authorized": False}


def shadow_health(protocol_path, journal_path, *, at=None):
    protocol = load_shadow_protocol(protocol_path)
    at = at or utc_now()
    spec, reasons = protocol["spec"], []
    result = {"schema": "shadow-health-v1", "checked_at": at, "protocol_id": protocol["id"],
        "trade_authorized": False, "market_data_qualified": False, "broker_execution": "disabled"}
    if epoch_ns(at) < epoch_ns(spec["evaluation_start"]):
        return {**result, "state": "WAITING_FOR_START", "reason_codes": []}
    if epoch_ns(at) >= epoch_ns(spec["evaluation_end"]):
        return {**result, "state": "EXPERIMENT_ENDED", "reason_codes": []}
    try:
        records = shadow_rows(journal_path, protocol, through=at)
    except sqlite3.Error:
        return {**result, "state": "DEGRADED", "reason_codes": ["SHADOW_JOURNAL_UNAVAILABLE"]}
    ticks = [r for r in records if r["kind"] == "shadow_tick"]
    age = (epoch_ns(at) - epoch_ns(ticks[-1]["available_at"])) / 1e9 if ticks else None
    if age is None or not 0 <= age <= 90:
        reasons.append("SHADOW_HEARTBEAT_STALE")
    decisions = [r["payload"] for r in records if r["kind"] == "shadow_decision"]
    completed = {r["release_group"] for r in decisions}
    missing = [r["release_group"] for r in release_events(protocol) if epoch_ns(r["expected_release_at"]) + r["grace_seconds"] * 10**9 <= epoch_ns(at) and r["release_group"] not in completed]
    if missing:
        reasons.append("EXPECTED_RELEASE_DECISIONS_MISSING")
    return {**result, "state": "DEGRADED" if reasons else "OBSERVING", "reason_codes": reasons,
        "heartbeat_age_seconds": age, "decision_groups": len(completed), "missing_decision_groups": missing,
        "abstentions": sum(r["decision"]["action"] == "ABSTAIN" for r in decisions),
        "late_decisions": sum("MISSED_FROZEN_DECISION_WINDOW" in r["comparison_exclusions"] for r in decisions)}


def build_shadow_dataset(protocol_path, shadow_path, macro_path, market_path, destination, *, through, _market=None):
    protocol = load_shadow_protocol(protocol_path)
    spec = protocol["spec"]
    _, policy = validate_spec(spec)
    cutoff = epoch_ns(through)
    if cutoff > epoch_ns(utc_now()):
        raise ValueError("cannot export future shadow research")
    target = Path(destination).resolve()
    sources = [Path(p).resolve().parent for p in (protocol_path, shadow_path, macro_path) if p is not None]
    if market_path is not None:
        sources.append(Path(market_path).resolve().parent)
    if target.exists() or any(target == source or source in target.parents for source in sources):
        raise ValueError("new dataset output outside input roots required")
    records = shadow_rows(shadow_path, protocol, through=through)
    observations = read_macro(macro_path, through=through)
    by_id = {r["id"]: r for r in observations}
    market = _market if _market is not None else read_curve(market_path, through=through) if market_path is not None else None
    if market is not None:
        market = {"records": [r for r in market["records"] if epoch_ns(r["payload"]["available_at"]) <= cutoff],
            "gaps": [g for g in market["gaps"] if not g.get("available_at") or epoch_ns(g["available_at"]) <= cutoff]}
    view = MarketView(market["records"], market["gaps"]) if market is not None else None
    rows, used_groups = [], set()
    modes = set()
    for record in records:
        if record["kind"] != "shadow_decision":
            continue
        p = record["payload"]
        revision = by_id.get(p["revision_id"])
        if not revision or revision["payload"]["received_at"] != p["received_at"] or epoch_ns(revision["available_at"]) != epoch_ns(p["available_at"]):
            raise ValueError("shadow macro revision binding mismatch")
        if p["protocol_id"] != protocol["id"] or p["release_group"] in used_groups:
            raise ValueError("changed protocol or duplicate shadow release group")
        if not epoch_ns(p["available_at"]) <= epoch_ns(p["feature_at"]) <= epoch_ns(p["decision_at"]) == epoch_ns(record["available_at"]):
            raise ValueError("noncausal shadow decision clocks")
        if p["decision"]["rules_hash"] != digest(spec["rules"]) or p["comparison_exclusions"] != comparison_exclusions(p["decision"]):
            raise ValueError("shadow rules or exclusions mismatch")
        used_groups.add(p["release_group"])
        modes.update(p["market_modes"])
        row = {k: p[k] for k in ("release_group", "revision_id", "received_at", "available_at", "supersedes_id",
            "initial_snapshot", "revision", "decision_at", "decision_clock", "decision", "features", "comparison_exclusions")}
        row.update(schema="macro-research-row-v1", state="EVALUATED", feature_at=p["feature_at"],
            protocol_id=protocol["id"], shadow_record_id=record["id"], outcomes=None, comparisons=None,
            latency_scenarios={}, trade_authorized=False, authorized_contracts=0)
        if view is not None and p["features"] is not None:
            for extra_delay in spec["latency_stress_ms"]:
                scenario = OutcomePolicy(**{**asdict(policy), "delay_ms": policy.delay_ms + extra_delay})
                outcomes = forward_outcomes(view, p["features"]["roles"], p["decision_at"], scenario)
                for horizon, outcome in outcomes["horizons"].items():
                    mature = epoch_ns(p["decision_at"]) + int(horizon) * 10**9 + scenario.delay_ms * 10**6 <= cutoff
                    outcome["label_state"] = "MATURED" if mature else "PENDING_HORIZON"
                    if not mature:
                        outcome["CL_return"] = None
                        outcome["instruments"] = {role: {"mid_return": None, **{side: {"status": "PENDING_HORIZON", "net_pnl": None} for side in ("long", "short")}} for role in ("CL1", "MCL1")}
                labels = comparisons(p["decision"], outcomes, p["comparison_exclusions"], policy.contracts)
                row["latency_scenarios"][str(extra_delay)] = {"outcomes": outcomes, "comparisons": labels}
                if extra_delay == 0:
                    row.update(outcomes=outcomes, comparisons=labels)
        row["id"] = digest(row)
        rows.append(row)
    expected = [r for r in release_events(protocol) if epoch_ns(r["expected_release_at"]) + r["grace_seconds"] * 10**9 <= cutoff]
    expected_groups = {r["release_group"] for r in expected}
    captured = {"eia:" + r["payload"]["observation"]["period"] for r in observations
        if r["payload"]["source"] == "eia" and epoch_ns(spec["evaluation_start"]) <= epoch_ns(r["payload"]["received_at"]) < epoch_ns(spec["evaluation_end"])}
    local_day = instant(through).astimezone(ZoneInfo(protocol["calendar"]["timezone"])).date()
    denominator = {"expected_groups_due": len(expected_groups), "captured_groups": len(captured),
        "recorded_decision_groups": len(rows), "abstentions": sum(r["decision"]["action"] == "ABSTAIN" for r in rows),
        "missing_capture_groups": sorted(expected_groups - captured), "missing_decision_groups": sorted((expected_groups & captured) - used_groups),
        "unexpected_capture_groups": sorted(captured - {r["release_group"] for r in release_events(protocol)}),
        "unpriced_decisions": sum(r["comparisons"] is None for r in rows),
        "calendar_complete_through_cutoff": date.fromisoformat(protocol["calendar"]["calendar_start"]) <= local_day < date.fromisoformat(protocol["calendar"]["calendar_end"])}
    # Count capture and parse stages separately from normalized release groups.
    # An HTTP receipt that failed parsing must not be called an absent request.
    with closing(sqlite3.connect(Path(macro_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        start, end = instant(spec["evaluation_start"]).isoformat(timespec="microseconds"), instant(through).isoformat(timespec="microseconds")
        stages = list(db.execute("SELECT kind,json_extract(payload,'$.status'),COUNT(*) FROM records WHERE kind IN ('macro_capture','macro_parse') AND json_extract(payload,'$.source')='eia' AND available_at>=? AND available_at<=? GROUP BY 1,2", (start, end)))
    denominator["eia_capture_status_counts"] = {str(status): count for kind, status, count in stages if kind == "macro_capture"}
    denominator["eia_parse_status_counts"] = {str(status): count for kind, status, count in stages if kind == "macro_parse"}
    primary = str(spec["horizon_seconds"])
    denominator["primary_comparison_state_counts"] = dict(Counter((r.get("comparisons") or {}).get(primary, {}).get("state", "NO_PRICED_LABELS") for r in rows))
    denominator["research_policy_trade_directions"] = sum(r["decision"]["baselines"]["inventory_continuation_v2"] != 0 for r in rows)
    target.mkdir(parents=True)
    for name, values in (("rows.jsonl", rows), ("observations.jsonl", observations)):
        with (target / name).open("x") as stream:
            for value in values:
                stream.write(canonical(value) + "\n")
    metadata = {"schema": "macro-research-dataset-v1", "pipeline": "frozen_prospective_shadow",
        "through": through, "rows": len(rows), "release_groups": len(used_groups), "pending_decisions": 0,
        "comparison_eligible_rows": sum(not r["comparison_exclusions"] for r in rows),
        "rules": spec["rules"], "rules_hash": digest(spec["rules"]), "outcome_policy": spec["outcome_policy"],
        "protocol_id": protocol["id"], "protocol": protocol, "denominator": denominator,
        "macro_input_hash": digest(observations), "shadow_input_hash": digest(records),
        "market_input_hash": digest(market) if market is not None else None,
        "market_modes": sorted(modes), "execution_role": "MCL1",
        "dataset_role": "engineering_fixture" if "fixture" in modes or _market is not None else "unpriced_research" if not modes else "prospective_unqualified_research",
        "files": {name: file_hash(target / name) for name in ("rows.jsonl", "observations.jsonl")},
        "market_data_qualified": False, "trade_authorized": False, "promotion": False, "expected_profit": None,
        "limitations": ["Actual recorded worker decisions; outcome fills remain hypothetical.",
            "Raw provider capture does not qualify entitlements, contracts, clocks or completeness.",
            "Missed windows are retained and excluded; decisions are never reconstructed at earlier times.",
            "Calendar coverage is bounded and must be renewed in a new reviewed prospective protocol.",
            "Fixed-horizon labels do not validate the paper engine's stop/target exit policy."]}
    atomic_json(target / "dataset.json", metadata)
    return metadata


def evaluate_shadow_dataset(protocol_path, directory):
    from .macro_evaluation import evaluate_macro_dataset
    protocol = load_shadow_protocol(protocol_path)
    metadata = json.loads((Path(directory) / "dataset.json").read_text())
    spec, method = protocol["spec"], protocol["spec"]["statistical_method"]
    if metadata.get("pipeline") != "frozen_prospective_shadow" or metadata.get("protocol_id") != protocol["id"]:
        raise ValueError("dataset must belong to the frozen shadow protocol")
    if epoch_ns(metadata["through"]) < epoch_ns(spec["evaluation_end"]):
        raise ValueError("holdout remains locked until the predeclared evaluation end")
    grid = {}
    for extra_delay in spec["latency_stress_ms"]:
        results = {}
        for stress in spec["extra_round_trip_costs_usd"]:
            results[stress] = evaluate_macro_dataset(directory, holdout_start=spec["holdout_start"],
            horizon_seconds=spec["horizon_seconds"], strategy="inventory_continuation_v2",
            minimum_train_groups=method["minimum_train_groups"], minimum_holdout_groups=method["minimum_holdout_groups"],
            minimum_holdout_trades=method["minimum_holdout_trades"], extra_round_trip_cost_usd=stress,
            bootstrap_block_groups=method["block_groups"], bootstrap_draws=method["draws"], bootstrap_seed=method["seed"],
            fixed_monthly_cost_usd=spec["fixed_monthly_cost_usd"], latency_extra_ms=extra_delay)
        grid[str(extra_delay)] = results
    return {"schema": "eia-shadow-evaluation-v1", "protocol_id": protocol["id"], "cost_scenarios": grid["0"],
        "latency_scenarios": grid,
        "denominator": metadata["denominator"], "market_data_qualified": False,
        "trade_authorized": False, "promotion": False, "economic_validation": "unavailable",
        "deployment_blockers": ["QUALIFIED_MARKET_PROVENANCE_REQUIRED", "PROSPECTIVE_PAPER_AND_RISK_REVIEW_REQUIRED"]}
