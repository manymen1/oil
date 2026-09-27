from copy import deepcopy
from datetime import timedelta

import pytest

from oilbot.clock import instant, stamp, utc_now
from oilbot.first_party import FirstPartyResolver, validate_reviews
from oilbot.forward import ForwardRecorder, classify
from oilbot.replay import export_manifest, load_manifest
from oilbot.schema import NewsItem, digest
from test_forward import setup, warm


@pytest.fixture
def context(setup):
    cfg = setup[0]
    reviews = deepcopy(cfg.raw["first_party_reviews"])
    now = utc_now()
    for review in reviews:
        review.update(reviewed_at=(instant(now) - timedelta(minutes=1)).isoformat(),
                      expires_at=(instant(now) + timedelta(hours=1)).isoformat(), status="verified",
                      reviewer="synthetic-test-review", evidence=["Synthetic test only; no production identity review"])
    return cfg, reviews, now


def inputs(context, source_id="aramco", title="Production suspended at Ras Tanura"):
    cfg, reviews, now = context
    source = next(s for s in cfg.sources if s["id"] == source_id)
    review = next(r for r in reviews if r["source_id"] == source_id)
    from urllib.parse import urlparse
    url = "https://" + urlparse(source["url"]).hostname + review["item_path_prefixes"][0] + "example"
    story = {"source_id": source_id, "url": url, "title": title, "local_received_at": now}
    obs = {"id": "raw-test", "kind": "observation", "payload": {"source_id": source_id,
        "source_policy": digest(source), "status": 200, "delivery": "http", "synthetic": False,
        "requested_url": source["url"], "url": source["url"]}}
    return source, story, [obs]


def evaluate(context, story, observations, sources=None):
    cfg, reviews, now = context
    resolver = FirstPartyResolver(sources or cfg.sources, reviews, cfg.raw["assets"])
    events = classify(story, cfg.raw["assets"])
    assert events
    for event in events:
        resolver.apply(event, story, observations, at=now, event_count=len(events))
    return events[0]


@pytest.mark.parametrize("sid,title", [("aramco", "Production suspended at Ras Tanura"),
    ("ofac", "OFAC imposes new sanctions"), ("centcom", "CENTCOM conducted an airstrike")])
def test_verified_scoped_publication_is_attribution_not_confirmation(context, sid, title):
    source, story, observations = inputs(context, sid, title)
    event = evaluate(context, story, observations)
    assert event["claim_origin"] == sid
    assert event["claim_origin_basis"] == "FIRST_PARTY_PUBLICATION"
    assert event["state"] == "OFFICIAL_CLAIM" and event["confirmation"] == "UNVERIFIED"
    assert event["independent_confirmation"] == "NONE" and event["review_required"]
    for span in event["first_party_evidence"]["subject_evidence"]:
        assert story["title"][span["start"]:span["end"]] == span["quote"]


@pytest.mark.parametrize("title,reason", [
    ("Production suspended at Ruwais", "ASSET_OUTSIDE_AUTHORITY_SCOPE"),
    ("Production suspended", "ASSET_OUTSIDE_AUTHORITY_SCOPE"),
    ("Ras Tanura and Ruwais production suspended", "ASSET_OUTSIDE_AUTHORITY_SCOPE"),
    ("ADNOC production suspended near Ras Tanura", "AMBIGUOUS_OPERATION_SUBJECT"),
    ("Shell production suspended near Ras Tanura", "AMBIGUOUS_OPERATION_SUBJECT"),
    ("Production suspended near Ras Tanura", "AMBIGUOUS_OPERATION_SUBJECT"),
    ("Ras Tanura unaffected; production suspended elsewhere", "AMBIGUOUS_OPERATION_SUBJECT"),
    ("Production may be suspended at Ras Tanura", "QUALIFIED_OR_UNCERTAIN_HEADLINE"),
    ("Minister says production suspended at Ras Tanura", "REPORTED_SPEECH_OR_QUOTE"),
    ("According to Reuters, production suspended at Ras Tanura", "REPORTED_SPEECH_OR_QUOTE"),
    ("Tanker hit near Ras Tanura", "EVENT_OUTSIDE_AUTHORITY_SCOPE"),
    ("Production suspended and exports halted at Ras Tanura", "MULTIPLE_EVENT_HEADLINE"),
])
def test_outside_scope_or_ambiguous_subject_abstains(context, title, reason):
    _, story, observations = inputs(context, title=title)
    event = evaluate(context, story, observations)
    assert event["claim_origin"] is None and event["claim_origin_basis"] == "UNKNOWN"
    assert event["first_party_assessment"]["reason"] == reason


@pytest.mark.parametrize("sid,title", [("ofac", "Iran imposes new sanctions"),
    ("ofac", "New sanctions announced"), ("centcom", "Iran conducted an airstrike")])
def test_official_site_is_not_enough_without_scoped_action_subject(context, sid, title):
    _, story, observations = inputs(context, sid, title)
    event = evaluate(context, story, observations)
    assert event["claim_origin"] is None
    assert event["first_party_assessment"]["reason"] == "MISSING_FIRST_PARTY_ACTION_SUBJECT"


@pytest.mark.parametrize("sid,title", [
    ("ofac", "OFAC imposes penalties while Iran expands sanctions"),
    ("ofac", "OFAC imposes fines; EU sanctions announced"),
    ("centcom", "CENTCOM conducted exercises after Iran launched an airstrike"),
    ("centcom", "CENTCOM conducted investigation into an airstrike"),
])
def test_official_action_must_be_tied_to_the_classified_event(context, sid, title):
    _, story, observations = inputs(context, sid, title)
    event = evaluate(context, story, observations)
    assert event["claim_origin"] is None
    assert event["first_party_assessment"]["reason"] == "AMBIGUOUS_OFFICIAL_ACTION_SUBJECT"


@pytest.mark.parametrize("field,value", [("origin", "reuters"), ("source_attributions", ["reuters"]),
    ("original_url", "https://example.test/original"), ("reposter", "someone"),
    ("wire_evidence", [{"basis": "SOURCE_CITATION"}])])
def test_third_party_provenance_vetoes_inference(context, field, value):
    _, story, observations = inputs(context)
    story[field] = value
    event = evaluate(context, story, observations)
    assert event["claim_origin"] is None and event["first_party_assessment"]["reason"] == "THIRD_PARTY_PROVENANCE"


@pytest.mark.parametrize("url", ["https://evil.test/en/news-media/news/example", "https://www.aramco.com.evil.test/en/news-media/news/example",
    "http://www.aramco.com/en/news-media/news/example", "https://user@www.aramco.com/en/news-media/news/example",
    "https://www.aramco.com:8443/en/news-media/news/example", "https://www.aramco.com/unreviewed/example",
    "https://www.aramco.com/en/news-media/news/../elsewhere", "https://www.aramco.com/en/news-media/news/%2e%2e/elsewhere"])
def test_url_identity_cannot_be_inferred_from_similar_host_or_unsafe_path(context, url):
    _, story, observations = inputs(context)
    story["url"] = url
    event = evaluate(context, story, observations)
    assert event["first_party_assessment"]["reason"] == "ITEM_OUTSIDE_REVIEWED_CHANNEL"


@pytest.mark.parametrize("field,value", [("source_policy", "0" * 64), ("source_id", "other"), ("delivery", "local_import"),
    ("synthetic", True), ("status", 403), ("requested_url", "https://example.test"), ("url", "https://example.test")])
def test_transport_binding_is_required(context, field, value):
    _, story, observations = inputs(context)
    observations[0]["payload"][field] = value
    assert evaluate(context, story, observations)["first_party_assessment"]["reason"] == "TRANSPORT_REGISTRATION_MISMATCH"


def test_missing_reviews_expiry_revocation_and_registry_changes_fail_closed(context):
    cfg, reviews, now = context
    _, story, observations = inputs(context)
    review = next(r for r in reviews if r["source_id"] == "aramco")
    for status in ("pending", "revoked"):
        review["status"] = status
        assert evaluate(context, story, observations)["first_party_assessment"]["reason"] == "REVIEW_NOT_VERIFIED"
    review["status"] = "verified"
    for receipt in ((instant(now) - timedelta(days=1)).isoformat(), (instant(now) + timedelta(days=1)).isoformat()):
        assert evaluate(context, {**story, "local_received_at": receipt}, observations)["claim_origin"] is None
    review["expires_at"] = now
    assert evaluate(context, story, observations)["first_party_assessment"]["reason"] == "REVIEW_NOT_VALID_AT_RECEIPT_AND_PROCESSING"
    review["expires_at"] = (instant(now) + timedelta(hours=1)).isoformat()
    changed_sources = [{**s, "poll_seconds": s["poll_seconds"] + 60} for s in cfg.sources]
    assert evaluate(context, story, observations, changed_sources)["first_party_assessment"]["reason"] == "SOURCE_REGISTRATION_MISMATCH"
    reviews.remove(review)
    assert evaluate(context, story, observations)["first_party_assessment"]["reason"] == "NO_REVIEW"


def test_explicit_attribution_is_not_overridden(context):
    _, story, observations = inputs(context, title="Iran says production suspended at Ras Tanura")
    event = evaluate(context, story, observations)
    assert event["claim_origin"] == "iran" and event["claim_origin_basis"] == "LITERAL_ATTRIBUTION"
    assert event["first_party_assessment"]["reason"] == "EXPLICIT_ATTRIBUTION_TAKES_PRECEDENCE"


@pytest.mark.parametrize("title", ["Ras Tanura loading suspended", "Loading at Ras Tanura suspended"])
def test_owned_facility_subject_positions(context, title):
    _, story, observations = inputs(context, title=title)
    assert evaluate(context, story, observations)["claim_origin_basis"] == "FIRST_PARTY_PUBLICATION"


def test_registered_owner_change_abstains(context):
    cfg, reviews, now = context
    _, story, observations = inputs(context)
    assets = deepcopy(cfg.raw["assets"])
    for asset in assets:
        if asset["id"] == "ras_tanura":
            asset["owner"] = "Unknown operator"
    event = classify(story, assets)[0]
    FirstPartyResolver(cfg.sources, reviews, assets).apply(event, story, observations, at=now)
    assert event["first_party_assessment"]["reason"] == "ASSET_OWNERSHIP_MISMATCH"
    assert event["claim_origin"] is None


def test_production_review_state_and_hash_bindings(setup):
    from oilbot.cli import preflight
    cfg = setup[0]
    sources = {s["id"]: s for s in cfg.sources}
    reviews = {r["source_id"]: r for r in cfg.raw["first_party_reviews"]}
    assert reviews["centcom"]["status"] == "pending"
    assert reviews["aramco"]["asset_ids"] == ["ras_tanura"]
    assert all(digest(sources[sid]) == r["source_policy_hash"] for sid, r in reviews.items())
    assert all(row["registration_matches"] for row in preflight(cfg)["first_party_reviews"])


def test_capture_to_event_preserves_policy_and_snapshot_provenance(setup, context, tmp_path):
    cfg, news, output, worker = setup
    _, reviews, _ = context
    source, story, _ = inputs(context)
    worker = ForwardRecorder(news, output, cfg.raw["assets"], sources=cfg.sources, first_party_reviews=reviews)
    warm(news, source)
    clock = stamp()
    oid = news.capture(source, {"body": b"synthetic test feed bytes", "url": source["url"], "requested_url": source["url"],
        "status": 200, "content_type": "application/rss+xml", "headers": {}, "received": clock, "started": clock,
        "first_byte": clock, "delivery": "http", "synthetic": False})  # Isolated test journal, never production.
    news.accept_items(source, oid, [NewsItem("scoped", story["url"], story["title"], story["title"])], {})
    worker.run_once()
    event = output.records("fast_event")[0]
    assert event["payload"]["claim_origin_basis"] == "FIRST_PARTY_PUBLICATION"
    policy = output.get(worker.policy_record_id)["payload"]["policy"]
    assert policy["first_party_reviews"] == reviews
    before = output.records("fast_event")
    revoked = deepcopy(reviews)
    for review in revoked:
        review["status"] = "revoked"
    new_worker = ForwardRecorder(news, output, cfg.raw["assets"], sources=cfg.sources, first_party_reviews=revoked)
    assert new_worker.epoch == worker.epoch and new_worker.run_once()["processed"] == 0
    assert output.records("fast_event") == before
    _, reader, _ = load_manifest(export_manifest(cfg, tmp_path / "scoped-snapshot"))
    assert any(r["id"] == event["id"] for r in reader.records)


@pytest.mark.parametrize("field,value", [("scope", "all_news"), ("claim_origin", "someone_else"),
    ("evidence", []), ("reviewer", ""), ("item_path_prefixes", ["/"]), ("asset_owner", "ADNOC")])
def test_invalid_authority_review_is_rejected(context, field, value):
    cfg, reviews, _ = context
    review = next(r for r in reviews if r["source_id"] == "aramco")
    review[field] = value
    with pytest.raises(ValueError):
        validate_reviews(reviews, cfg.sources, cfg.raw["assets"])
