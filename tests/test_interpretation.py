import pytest
from oilbot.forward import classify, VERSION


@pytest.mark.parametrize("title,kind", [
    ("Iran attacks tanker near Hormuz", "TANKER_ATTACK"),
    ("Tanker attacked near Hormuz", "TANKER_ATTACK"),
    ("US and Iran agree to ceasefire", "CEASEFIRE_REACHED"),
    ("Ceasefire agreed between Iran and US", "CEASEFIRE_REACHED"),
    ("Hormuz reopens to shipping", "SHIPPING_RESTORED"),
    ("Shipping resumed through Hormuz", "SHIPPING_RESTORED"),
])
def test_active_passive_wording(title, kind):
    events = classify({"title":title,"source_id":"synthetic"}, [])
    assert any(e["event_type"] == kind for e in events)
    assert all(e["claim_origin"] is None and e["confirmation"] == "UNVERIFIED" for e in events)
    assert VERSION == "fast-event-v6"


@pytest.mark.parametrize("title,assertion", [
    ("Iran denies attacking tanker near Hormuz", "denied"),
    ("Tanker not attacked near Hormuz", "denied"),
    ("Iran may attack tanker near Hormuz", "hypothetical"),
    ("Last year tanker attacked near Hormuz", "historical"),
    ("In 2020 tanker attacked near Hormuz", "historical"),
    ("Unconfirmed tanker attack: Iran attacks tanker", "unclear"),
    ("Iran denies it could attack tanker near Hormuz", "unclear"),
])
def test_nonasserted_claims_are_not_asserted_events(title, assertion):
    events = classify({"title":title,"source_id":"synthetic"}, [])
    assert events and all(e["assertion"] == assertion for e in events)
    assert all(not e["asserted_oil_event_candidate"] and e["state"] == "REVIEW_REQUIRED" for e in events)
    assert all(e["confirmation"] == "UNVERIFIED" for e in events)


@pytest.mark.parametrize("title", ["Toyota production suspended", "US imposes sanctions on North Korean hackers"])
def test_unrelated_production_and_sanctions_remain_non_oil_review_candidates(title):
    events = classify({"title":title,"source_id":"synthetic"}, [])
    assert events and all(e["oil_relevance"] == "irrelevant" and not e["asserted_oil_event_candidate"] for e in events)


def test_unknown_location_is_not_automatically_irrelevant():
    event = classify({"title":"Production suspended","source_id":"synthetic"}, [])[0]
    assert event["oil_relevance"] == "uncertain"
    assert event["oil_mechanism"] == "unknown"


def test_competing_oil_and_non_oil_context_abstains():
    event = classify({"title":"Toyota production suspended during oil crisis","source_id":"synthetic"}, [])[0]
    assert event["oil_relevance"] == "uncertain"


def test_conditional_hormuz_restoration_is_not_actual_reopening():
    events = classify({"title":"Iran says plan would reopen the Strait of Hormuz if Washington agrees", "source_id":"synthetic"}, [])
    event = next(e for e in events if e["event_type"] == "SHIPPING_RESTORED")
    assert event["assertion"] == "hypothetical" and not event["asserted_oil_event_candidate"]
    assert event["claim_origin"] == "iran" and event["confirmation"] == "UNVERIFIED"
