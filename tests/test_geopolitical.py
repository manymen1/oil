from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from oilbot.config import load_config
from oilbot.economic_review import assess, mapping_at, template
from oilbot.features import MarketView
from oilbot.forward import ForwardRecorder
from oilbot.geopolitical import GeopoliticalRules, build_geopolitical_dataset, news_decision, overlap_groups
from oilbot.inventory_strategy import inventory_features
from oilbot.linking import LinkerWorker
from oilbot.replay import export_manifest
from oilbot.store import Journal
from oilbot.story_review import Archive
from test_forward import capture, warm
from test_research_v2 import DEFINITIONS, at, quote, record


@pytest.fixture
def news_case(tmp_path, monkeypatch):
    clock = [at(-600)]
    for module in ("oilbot.clock", "oilbot.store", "oilbot.forward", "oilbot.forward_review",
                   "oilbot.linking", "oilbot.economic_review", "oilbot.geopolitical"):
        monkeypatch.setattr(module + ".utc_now", lambda: clock[0])
    cfg = replace(load_config("configs/forward.yaml"), root=tmp_path / "capture")
    news, forward = Journal(cfg.db("news")), Journal(cfg.db("forward"))
    worker = ForwardRecorder(news, forward, cfg.raw["assets"])
    clock[0] = at(-400)
    for source in cfg.sources[:2]: warm(news, source)
    clock[0] = at()
    story = capture(news, cfg.sources[0], "IRGC says tanker attacked in Hormuz", native="news", published_at=at())
    clock[0] = at(1)
    worker.run_once()
    clock[0] = at(400)
    archive = Archive(export_manifest(cfg, tmp_path / "snapshot"))
    event = archive.events[story["id"]][0]
    rows = [record(d) for d in DEFINITIONS]
    for second in range(-310, 370):
        for d in DEFINITIONS:
            ticks = max(0, min(second, 60) // (10 if d == DEFINITIONS[1] else 5))
            bid = Decimal("72.00") + Decimal(".01") * ticks
            rows.append(record(quote(d, second, bid=str(bid), ask=str(bid + Decimal(".02")))))
    market = {"records":rows, "gaps":[]}
    rules = GeopoliticalRules()
    features = inventory_features(MarketView(rows), at(), at(60), rules.market())
    return cfg, news, forward, worker, clock, archive, event, story, features, market


def decide(case, **kwargs):
    *_, archive, event, story, features, market = case
    return news_decision(event, story, mapping_at(archive, at(60)), features, at=at(60), rules=GeopoliticalRules(), **kwargs)


def test_risk_claim_never_becomes_physical_confirmation(news_case):
    result = decide(news_case)
    assert result["action"] == "RESEARCH_CANDIDATE" and result["lane"] == "risk_premium"
    assert result["hypothesis_direction"] == 1
    assert result["confirmation"] == "UNVERIFIED_CLAIM"
    assert result["baselines"]["news_plus_continuation"] == 1
    assert not result["physical_disruption_confirmed"] and not result["physical_review_eligible"]
    assert not result["trade_authorized"] and result["authorized_contracts"] == 0
    assert result["receipt_to_candidate_seconds"] == 1


@pytest.mark.parametrize("key,value,reason", [("assertion","hypothetical","NONASSERTED_OR_QUALIFIED_CLAIM"),
    ("assertion","denied","NONASSERTED_OR_QUALIFIED_CLAIM"),
    ("claim_origin",None,"UNKNOWN_CLAIM_ORIGIN"), ("oil_relevance","uncertain","AMBIGUOUS_OIL_RELEVANCE"),
    ("novelty",False,"NO_NOVEL_CLAIM"), ("classifier_version","legacy","UNSUPPORTED_CAPTURED_CLASSIFIER"),
    ("event_type","SANCTIONS_TIGHTENED","NO_REVIEWED_DIRECTION_POLICY_FOR_EVENT_TYPE")])
def test_ambiguous_and_unsupported_claims_abstain(news_case, key, value, reason):
    news_case[6]["payload"][key] = value
    result = decide(news_case)
    assert result["action"] == "ABSTAIN" and reason in result["comparison_exclusions"]


@pytest.mark.parametrize("publication,reason", [(None,"MISSING_PUBLICATION_TIME"),
    (at(-181),"OLD_OR_FUTURE_PUBLICATION"), (at(1),"OLD_OR_FUTURE_PUBLICATION")])
def test_age_and_undated_feed_fail_closed(news_case, publication, reason):
    news_case[7]["payload"]["published_at"] = publication
    assert reason in decide(news_case)["reason_codes"]


def test_explicit_operational_headline_requires_dated_review(news_case):
    news_case[6]["payload"]["event_type"] = "SHIPPING_RESTRICTION"
    result = decide(news_case)
    assert result["lane"] == "physical_transition"
    assert result["hypothesis_direction"] == 0
    assert "PHYSICAL_ASSESSMENT_REQUIRED" in result["reason_codes"]


def test_literal_evidence_and_time_binding(news_case):
    news_case[6]["payload"]["evidence"]["quote"] = "invented evidence"
    with pytest.raises(ValueError, match="evidence"):
        decide(news_case)


def test_faded_price_does_not_remove_baseline_population(news_case):
    features = news_case[8]
    # Rebuild a real plateau rather than forge independently bound midpoint fields.
    market = deepcopy(news_case[-1])
    for r in market["records"]:
        if r["kind"] == "market" and r["payload"]["available_at"] > at():
            r["payload"]["bid"],r["payload"]["ask"] = "72.12","72.14"
    rebuilt = inventory_features(MarketView(market["records"]),at(),at(60),GeopoliticalRules().market())
    features.clear(); features.update(rebuilt)
    result = decide(news_case)
    assert result["action"] == "ABSTAIN" and result["comparison_exclusions"] == []
    assert result["baselines"]["news_direction_only"] == 1 and result["baselines"]["news_plus_continuation"] == 0


def test_dataset_real_mcl_costs_future_cutoff_and_hashes(news_case,tmp_path):
    *_, archive,event,story,features,market = news_case
    target = tmp_path / "dataset"
    meta = build_geopolitical_dataset(archive,target,through=at(62),market=market)
    row = json.loads((target / "rows.jsonl").read_text())
    assert row["decision"]["action"] == "RESEARCH_CANDIDATE"
    assert row["comparisons"]["1"]["baselines"]["news_plus_continuation"]["net_pnl"] == "-6.00"
    assert row["comparisons"]["5"]["state"] == "PENDING_HORIZON"
    assert row["outcomes"]["horizons"]["5"]["instruments"]["MCL1"]["long"]["net_pnl"] is None
    assert not meta["promotion"] and meta["lanes"] == {"risk_premium":1}
    later = deepcopy(market)
    later["records"].append(record(quote(DEFINITIONS[2],500,bid="90.00",ask="90.02")))
    second = tmp_path / "second"
    again = build_geopolitical_dataset(archive,second,through=at(62),market=later)
    assert (target / "rows.jsonl").read_bytes() == (second / "rows.jsonl").read_bytes()
    assert meta["market_hash"] == again["market_hash"]


def test_later_correction_blocks_decision_without_rewriting_capture(news_case,tmp_path):
    cfg,news,forward,worker,clock,archive,event,*_ = news_case
    clock[0] = at(30)
    capture(news,cfg.sources[0],"Earlier tanker report is withdrawn",native="news",status="withdrawal",published_at=at(30))
    # Intentionally do not run the classifier: story supersession alone must veto.
    archive = Archive(export_manifest(cfg,tmp_path / "corrected-snapshot"))
    clock[0] = at(400)
    target = tmp_path / "corrected"
    build_geopolitical_dataset(archive,target,through=at(62),market=news_case[-1])
    row = json.loads((target / "rows.jsonl").read_text())
    assert "SUPERSEDED_CAPTURED_STORY" in row["decision"]["comparison_exclusions"]
    assert forward.get(event["id"]) == event


def test_reprints_share_evaluation_group_not_confirmation(news_case,tmp_path):
    cfg,news,forward,worker,clock,archive,event,*_ = news_case
    clock[0] = at(5)
    capture(news,cfg.sources[1],"IRGC says tanker attacked in Hormuz",native="reprint",published_at=at())
    worker.run_once()
    LinkerWorker(news,forward).run_once()
    clock[0] = at(400)
    archive = Archive(export_manifest(cfg,tmp_path / "reprint-snapshot"))
    target = tmp_path / "reprints"
    meta = build_geopolitical_dataset(archive,target,through=at(370),market=news_case[-1])
    rows = [json.loads(line) for line in (target / "rows.jsonl").read_text().splitlines()]
    assert meta["subjects"] == 2 and meta["evaluation_groups"] == 1
    assert len({r["evaluation_group"] for r in rows}) == 1
    assert all(not r["decision"]["physical_disruption_confirmed"] for r in rows)


@pytest.mark.parametrize("reviewer_type", ["assistant", "human"])
def test_physical_review_uses_actual_review_time_and_reviewer_gate(news_case,tmp_path,reviewer_type):
    cfg,news,forward,worker,clock,archive,*_ = news_case
    clock[0] = at(10)
    title = "Aramco says crude production suspended after operating normally"
    story = capture(news,cfg.sources[0],title,native="physical",published_at=at(10))
    clock[0] = at(11); worker.run_once()
    archive = Archive(export_manifest(cfg,tmp_path / "physical-snapshot"))
    event = archive.events[story["id"]][0]
    value = template(archive,event["id"])["assessment"]
    value.update(reviewer="synthetic-reviewer",reviewer_type=reviewer_type,authorization="synthetic test",
        reason="Synthetic captured operating claim",episode_reviewed=True,novel=True,
        stage="disruption",state_before="OPERATING",state_after="SUSPENDED",assertion="asserted",
        mechanism="supply",confirmation_level="OPERATOR_REPORT",claim_origin="Aramco",
        evidence=[dict(text_field="title",start=0,end=len(title),quote=title,
            supports=["stage","state_before","state_after","assertion","mechanism","confirmation_level","claim_origin"])])
    clock[0] = at(100)
    path = tmp_path / "reviews.db"
    review = assess(archive,path,value)
    clock[0] = at(400)
    target = tmp_path / "physical"
    build_geopolitical_dataset(archive,target,through=at(370),assessments=path,market=news_case[-1])
    rows = [json.loads(line) for line in (target / "rows.jsonl").read_text().splitlines()]
    reviewed = next(r for r in rows if r["assessment_id"] == review["id"])
    assert reviewed["decision_at"] == at(100)
    assert ("PHYSICAL_REVIEW_INELIGIBLE" in reviewed["decision"]["reason_codes"]) == (reviewer_type == "assistant")
    assert reviewed["transition"]["payload"]["reviewer_type"] == reviewer_type
    assert reviewed["decision"]["physical_review_eligible"] == (reviewer_type == "human")
    assert not reviewed["decision"]["physical_disruption_confirmed"]
    assert not reviewed["decision"]["trade_authorized"]


def test_conflicting_directions_from_one_headline_are_excluded(news_case,tmp_path):
    cfg,news,forward,worker,clock,archive,*_ = news_case
    clock[0] = at(5)
    story = capture(news,cfg.sources[0],"IRGC says tanker attacked in Hormuz; ceasefire reached",native="mixed",published_at=at(5))
    worker.run_once()
    clock[0] = at(400)
    archive = Archive(export_manifest(cfg,tmp_path / "mixed-snapshot"))
    target = tmp_path / "mixed"
    build_geopolitical_dataset(archive,target,through=at(370),market=news_case[-1])
    rows = [json.loads(line) for line in (target / "rows.jsonl").read_text().splitlines()]
    mixed = [r for r in rows if r["story_revision_id"] == story["id"]]
    assert len(mixed) >= 2
    assert all("CONFLICTING_HEADLINE_DIRECTIONS" in r["decision"]["comparison_exclusions"] for r in mixed)


def test_missing_market_and_pending_decision(news_case,tmp_path):
    target = tmp_path / "unpriced"
    meta = build_geopolitical_dataset(news_case[5],target,through=at(70))
    row = json.loads((target / "rows.jsonl").read_text())
    assert meta["dataset_role"] == "unpriced_research"
    assert "MISSING_MARKET_DATA" in row["decision"]["reason_codes"] and row["outcomes"] is None
    build_geopolitical_dataset(news_case[5],tmp_path / "pending",through=at(59))
    pending = json.loads((tmp_path / "pending/rows.jsonl").read_text())
    assert pending["state"] == "PENDING_DECISION" and pending["decision"] is None
    with pytest.raises(ValueError,match="snapshot"):
        build_geopolitical_dataset(news_case[5],news_case[5].path.parent / "bad",through=at(70))
    with pytest.raises(ValueError,match="already exists"):
        build_geopolitical_dataset(news_case[5],target,through=at(70))
