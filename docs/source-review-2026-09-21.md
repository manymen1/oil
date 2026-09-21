# Initial source screening — 21 September 2026

This is preliminary, Codex-assisted technical and published-policy screening,
not legal clearance, a completed source qualification, or evidence of a trading
edge. No continuous collection, new feed activation, model extraction, account
registration, subscription, or external permission request was performed.
Existing observation enabled flags remain unchanged; do not interpret them as
approval for the proposed automated trading-research use.

## Endpoint diagnostics

[Machine-readable results](source-checks/2026-09-21.json) contain one-off response
metadata and parser counts. Bodies were parsed in memory and not retained;
these samples are not replayable journal evidence. They cannot turn qualification
checks green. No detail articles/PDFs were followed. No blocked resource was
retried with a different identity or access workaround.

| Source | Result in this check | Parsed items | With parsed publication time |
| --- | --- | ---: | ---: |
| Aramco | Runtime HTTP 200, RSS | 50 | 50 |
| ADNOC | Runtime HTTP 200, listing | 10 | 0 |
| Fujairah | Runtime HTTP 200, listing | 6 | 0 |
| gCaptain | Runtime HTTP 200, RSS | 12 | 12 |
| UKMTO | Runtime HTTP 403 | — | — |
| IRNA | Runtime HTTP 200; no adapter | — | — |
| Iran International | Runtime HTTP 200; no adapter | — | — |
| Al Arabiya | Web-tool HTTP 403; no runtime retry | — | — |

Counts describe the diagnostic response, not throughput or complete coverage.
A timestamp being present does not establish its publication/update semantics.
No repeated-poll correction/deletion behaviour was verified. No RSS endpoint
was invented for the three catalog-only candidates.

## Published-policy and identity findings

### aramco

The [official media page](https://www.aramco.com/en/news-media/resources-for-journalists)
publishes the English RSS URL already configured. [Site terms](https://www.aramco.com/en/terms-and-conditions)
identify Aramco as operator and restrict commercial copying without consent.
RSS availability is not treated as permission for trading research, long-term
archiving, redistribution, or model processing. Identity screened; capture and
model use remain pending for this project.

### adnoc

[ADNOC's platform terms](https://adnoc.ae/en/terms-and-conditions), section 3,
reserve copying outside personal non-commercial use without prior written
permission. Identity screened; project-specific capture/model permission remains
pending. The listing diagnostic did not establish article timestamps or correction
handling. A successful listing parse is not a complete article pipeline.

### fujairah

The [official site](https://fujairahport.ae/home/) identifies Fujairah Ports
Authority and reserves rights. Its [notice listing](https://fujairahport.ae/marine-centre/notice-to-mariner-new/)
is technically parseable. No website-content reuse grant was established in this
review. Cargo/storage regulations are not a licence for news or document reuse.
Capture/model use and document timing remain pending.

### ukmto

The [official site](https://www.ukmto.org/) establishes the publisher and product
links. [Terms, clause 20](https://www.ukmto.org/terms-and-conditions), explicitly
publish website information under the Open Government Licence. The
[OGL v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
allows reuse, including commercial use, subject to attribution and exclusions.
Personal data, logos and unlicensed third-party rights are outside its scope;
endorsement must not be implied.

Capture permission is recorded only for covered published UKMTO information.
Runtime access is still blocked. The web rendering of recent incidents displayed
zero reports; that does not prove no incidents or a working collector. Model
processing remains pending until content scope and handling are checked.
The [official homepage](https://www.ukmto.org/) offers approval-based email alerts;
no subscription request was submitted. Confirm a supported delivery route rather
than work around the runtime 403.

### gcaptain

The [publisher privacy page](https://gcaptain.com/about/privacy-policy/) identifies
Unofficial Networks LLC, but does not establish content-reuse rights. The
[homepage](https://gcaptain.com/) carries wire-attributed material; the diagnostic
RSS parser attributed 9 of 12 items. Do not treat these as independent original
claims or assume the outlet grants third-party wire rights. Capture/model use
remains pending. Forum terms were not substituted for a verified news-feed licence.

### irna

[IRNA's own About Us page](https://en.irna.ir/news/85965303/About-Us) identifies
the agency and describes free public news access. That is not an explicit grant
for automated retention, commercial research, or model processing. Identity
screened, without asserting independence or truth. The homepage response has no
qualified adapter; no RSS/API endpoint or corresponding use permission was verified.

### al_arabiya

The [English site](https://english.alarabiya.net/) was blocked in the web check.
Current direct identity/policy inspection remains incomplete. No terminal retry,
replacement endpoint, or inferred permission was added. All checks remain pending.

### iran_international

The [About page](https://www.iranintl.com/en/abouten) and
[privacy policy](https://www.iranintl.com/en/privacyen) identify the publication
and Volant Media. The privacy policy is not a publication-content licence.
The homepage responded, but no qualified RSS/API or HTML adapter exists here.
Capture/model permission and revision/timestamp semantics remain pending.

## What changed in the repository

`configs/source-qualification.json` now records eight partial reviews with
evidence references, current source/profile hashes and a seven-day screening
refresh deadline. The deadline is an operational choice, not a licence expiry.
Catalog-only candidates retain null source hashes and cannot pass the bound
registration check. All sources remain blocked overall, and all model-use checks
remain pending. Profiles, runtime enabled flags, and independence groups were
not changed.

## Remaining actions

1. Confirm a permitted UKMTO delivery route and implement licence attribution
   plus excluded-content handling before considering model processing.
2. Resolve the private-publisher capture/model scope. Use the
   [unsent request template](source-access-request.md); requests require the
   user's chosen sender and approval before transmission.
3. After permission/access is established, retain bounded parser samples in the
   observation journal, validate publication/update semantics and attribution,
   and test corrections/deletions across repeated polls.
4. Only then activate the news-only shakedown. Do not launch the current enabled
   source set merely because a diagnostic returned HTTP 200.
