"""Synthetic releases only: no external article corpus or model calls."""
import pytest

from oilbot.sources import ListingAdapter, NewsCollector, ParseFailure
from test_oil import config, journals, response, source


def release(body="Loading at the oil terminal remains suspended pending inspection."):
    return ('<h1>Operations update</h1><nav>Other stories</nav><article>'
            '<div class="field--name-body body">' + body + '</div></article>').encode()


@pytest.mark.parametrize("kind,url", [
    ("ofac", "https://ofac.treasury.gov/recent-actions/20260928"),
    ("centcom", "https://www.centcom.mil/MEDIA/PUBLIC-RELEASES/Article/123/test/"),
])
def test_release_body_identity_and_unknown_date(kind, url):
    item, = ListingAdapter(kind).parse(release(), url, "text/html")
    assert item.native_id == url and item.published_at is None
    assert "remains suspended pending inspection" in item.text
    assert "Other stories" not in item.text
    assert item.origin is None and not item.links
    with pytest.raises(ParseFailure, match="INCOMPLETE_DETAIL"):
        ListingAdapter(kind).parse(b"<h1>Update</h1><main>Navigation only</main>", url, "text/html")


def test_ofac_does_not_use_navigation_body():
    url = "https://ofac.treasury.gov/recent-actions/20260928"
    html = b'<div class="field--name-body">Navigation and unrelated announcements</div>' + release()
    item, = ListingAdapter("ofac").parse(html, url, "text/html")
    assert "Navigation" not in item.text


def test_ofac_current_article_selector():
    html = release().replace(b'class="field--name-body body"', b'class="field--name-field-body"><div class="field__item"').replace(b'</article>', b'</div></article>')
    item, = ListingAdapter("ofac").parse(html, "https://ofac.treasury.gov/recent-actions/20260928", "text/html")
    assert "pending inspection" in item.text


def test_opt_in_detail_revisions_no_listing_downgrade_and_304_queue(config, journals):
    news, _ = journals
    src = {**source(config), "id": "ofac", "adapter": "ofac", "capture_details": True,
           "url": "https://ofac.treasury.gov/recent-actions", "allowed_hosts": ["ofac.treasury.gov"]}
    detail = src["url"] + "/20260928"
    listing = ('<a href="' + detail + '">Operations update</a>').encode()
    state = {"status": 200, "body": release(), "denied": False}
    calls = []
    def fetch(_, url, cursor):
        calls.append(url)
        return response(listing if url == src["url"] else state["body"],
                        status=state["status"] if url == src["url"] else 403 if state["denied"] else 200,
                        content_type="text/html", url=url)
    worker = NewsCollector(news, [src], fetch)
    worker.fetch_source(src)
    first, = news.records("story_revision")
    worker.fetch_source(src)
    assert len(news.records("story_revision")) == 1
    state.update(status=304, body=release("Loading at the oil terminal resumed after the completed inspection."))
    worker.fetch_source(src)
    revised = news.records("story_revision")[-1]
    assert revised["payload"]["supersedes_id"] == first["id"]
    assert revised["payload"]["local_received_at"] == news.get(revised["payload"]["input_revision_ids"][0])["available_at"]
    state["denied"] = True
    assert worker.fetch_source(src)["errors"] == 1
    count = calls.count(detail)
    assert worker.fetch_source(src)["errors"] == 1
    assert calls.count(detail) == count


def test_official_details_default_off(config, journals):
    news, _ = journals
    src = {**source(config), "adapter": "ofac", "url": "https://ofac.treasury.gov/recent-actions"}
    calls = []
    def fetch(_, url, cursor):
        calls.append(url)
        return response(b'<a href="/recent-actions/20260928">Update</a>', url=url, content_type="text/html")
    NewsCollector(news, [src], fetch).fetch_source(src)
    assert calls == [src["url"]]
    assert len(news.records("story_revision")) == 1


def test_recovery_keeps_listing_discovery_only(config, journals):
    news, _ = journals
    src = {**source(config), "id": "ofac", "adapter": "ofac", "capture_details": True,
           "url": "https://ofac.treasury.gov/recent-actions", "allowed_hosts": ["ofac.treasury.gov"]}
    detail = src["url"] + "/20260928"
    news.capture(src, response(b'<a href="/recent-actions/20260928">Update</a>',
                               url=src["url"], content_type="text/html"))
    oid = news.capture(src, response(release(), url=detail, content_type="text/html"))
    worker = NewsCollector(news, [src])
    worker.recover_unparsed()
    row, = news.records("story_revision")
    assert "remains suspended" in row["payload"]["text"]
    assert oid in row["payload"]["input_revision_ids"]
    worker.recover_unparsed()
    assert len(news.records("story_revision")) == 1
