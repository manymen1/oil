# Source development / access qualification — 28 September 2026

This is a development record, not permission or production qualification. No live
registrations, circuit state, polling schedule or rights were changed. Model
processing remains pending. The 30-story pilot authorization does not cover new
source text or the future holdout. Tests use synthetic article text.

## Local WSL probes

One-off direct requests with redirects disabled; no credentials, proxy rotation,
challenge solving or access bypass. Response bodies were inspected programmatically
for structure; only metadata was returned to the assistant, not article text.

| Source and URL | Observation | Decision |
| --- | --- | --- |
| [UKMTO recent incidents](https://www.ukmto.org/recent-incidents) | 01:54:04 UTC: HTTP 403, 4545 bytes | Still blocked. Obtain supported access; do not retry around the restriction. |
| [IRNA English](https://en.irna.ir/) | 01:54:05 UTC: HTTP 200, 6257 bytes, no RSS link or recognizable `/news/` links in local response | Unqualified. Browser/search access is not evidence of collector-ready content. Cause not established; need a supported endpoint and capture-rights review. |
| [OFAC listing](https://ofac.treasury.gov/recent-actions) | 01:54:06 UTC: HTTP 200, 45196 bytes; discovered dated detail link | Listing and sampled detail reachable. Parser must target the article body, not navigation fields. |
| [CENTCOM listing](https://www.centcom.mil/MEDIA/PUBLIC-RELEASES/) | 01:54:08 UTC: HTTP 200, 46886 bytes; discovered article link | Listing accessible; sampled detail returned HTTP 403. Full-body adapter is synthetic-tested, not live-qualified. |

Listing response SHA-256 values, in table order:

```
UKMTO b4f739c832f2c9f7995ecb2f7c91531ab7e656b63433d4230bb39852a400887c
IRNA  0a6caaf3129dbca82a534077c3ba332ae9a5342c6b246a77fcda341421601141
OFAC  f39bca3ca8d14c1dba00010749bee7cd10e648d88a695d4d08b1b5ed83d56a6a
CENTCOM a98fcff8ec79f4c924edf3e9484f7437cac4a4d21cc078d9d4672ae710de6bf9
```

Sampled detail URLs: OFAC `/recent-actions/20260924`; CENTCOM
`/MEDIA/PUBLIC-RELEASES/Article/4611911/centcom-completes-exercise-eager-lion-in-colorado/`.
Do not interpret OFAC's date-only release field as an exact publication time or an
effective date. The current selector is `article .field--name-field-body .field__item`.
CENTCOM `.body`, `.ArticleBody`, `.article-body` support is provisional until an
authorized accessible release can validate it. Missing body structure fails closed.

## Implemented opt-in detail capture

Development parser version: `source-v3`. A CENTCOM/OFAC registration can later set
`capture_details: true`. Default remains false; no config enables it in this change.
When enabled, listings remain raw discovery evidence, and detail pages become
story revisions at their own receipt time. Repeated listing headlines cannot
downgrade full text. At most two registered-host detail links are fetched per
listing poll; 304 listings retain the queued URLs. The existing round-robin queue
is not a comprehensive historical crawler; newly rotated-out links may be missed.

Raw detail bytes are durable before parsing; parse failures remain recoverable.
401/403 detail responses latch a per-URL, source-policy-bound denial, so subsequent
polls do not retry the denied URL under that policy. Health stays degraded while
denied queued work remains. No denied URL was reset in the live collector.
This is not a general detail-endpoint retry/backoff redesign.

No recursively linked PDFs, SDN files, external releases or license documents are
followed. Capturing the page does not guarantee affected-entity or effective-date
coverage when those facts occur only in attachments. Text capture does not add
body classification to the live title-only policy. Later activation must explicitly
decide and version body interpretation rather than silently widening the hot path.

Before enabling details: validate allowed access and use scope, test selectors on
authorized pages, requalify timestamp/revision semantics, review source-policy and
first-party-identity hashes (a config change invalidates bound reviews), establish
a new baseline, then run a separate canary after the pinned soak. Extra HTTP traffic
can increase latency; measure it rather than claiming this is the fast lane.

## Source backlog / acceptance gates

Priority: existing-source details, UKMTO and IRNA qualification; then ADNOC and
qualified port notices; OPEC releases and EIA inventories as context. Reuters is
optional. Every addition needs stable item identity, original claimant distinct
from publisher, bounded raw-first parsing, revision/withdrawal semantics, receipt
timing, rights and a measured canary. A reachable page is not a licence or an SLA.

Measure unique reviewed relevant claims, operational episode stages, publication
versus receipt lag, duplicate/syndication volume, health and missing details with
source-specific denominators. Reprints count as receipts, not independent evidence.
No additional source was enabled, no claimed source gap was closed by this work,
and no access blocker was bypassed.
