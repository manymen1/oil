# Scoped first-party publication attribution

`fast-event-v5` can infer the publisher as claimant without a literal "says"
phrase, but only under an explicitly reviewed publication/authority scope.
`FIRST_PARTY_PUBLICATION` means that the reviewed source published the scoped
claim. It does **not** mean that the claimed event happened, or that another
independent source corroborated it. Confirmation remains `UNVERIFIED`, independent
confirmation remains `NONE`, and review remains required.

## Current review slice

The dated reviews in `configs/forward.yaml` are narrowly about identity and scope,
not legal clearance, source completeness, authentication credentials or feed SLAs.
They do not change collection permissions, circuit breakers or model-processing rights.

- Aramco: its [official ports and terminals page](https://www.aramco.com/en/what-we-do/operations/ports-and-terminals)
  identifies Aramco terminals, lists Ras Tanura terminal sections, and links to the
  exact configured RSS endpoint. The initial scope covers **Ras Tanura only**, not
  every asset owned by Aramco. The browser could not render the RSS MIME type;
  this review does not claim a new successful collector parse or operational SLA.
- OFAC: its [official description](https://ofac.treasury.gov/faqs/1) identifies its
  sanctions responsibilities; the official site links to [Recent Actions](https://ofac.treasury.gov/recent-actions).
  The initial scope requires a headline explicitly naming OFAC as the sanctions-action
  subject. Generic "Iran-related Designations" headlines are not assigned an
  invented event merely because the publisher is OFAC.
- CENTCOM: code supports a narrow own-military-statements scope, but the live
  registration remains **pending**. A direct request to the
  [mission page](https://www.centcom.mil/ABOUT-US/mission-vision/) returned HTTP 403.
  Cached search evidence was not used to activate inference or bypass access controls.

Reviews expire on October 4, 2026 at their configured timestamp. Review and renew
them explicitly; do not merely extend expiry without rechecking the evidence.
Expired/pending/revoked reviews abstain without stopping raw collection.
`preflight` exposes review status, expiry, registration match and time validity.
No new sources or persistent collector are activated by this feature.

## Gates and scope rules

Every inferred claim requires all of the following:

1. A trusted local review names the same publisher and claimant, with reviewer,
   evidence, validity dates, authority scope and explicit item directory prefixes.
2. The exact source registration hash matches the review and the raw observation.
   Source URL, owner, adapter, allowed hosts or other registration changes invalidate
   the old review until it is renewed. No wildcard channel/hostname matching is used.
3. The story was received after review and processed before expiry. Old captured
   stories cannot acquire implicit first-party attribution from a later review.
4. Referenced raw observations came from real HTTP 200 collection of the exact
   reviewed endpoint, without synthetic/import flags or an off-endpoint redirect.
   The item URL must also be HTTPS on an allowed host and under a reviewed directory;
   credentials, nonstandard ports and ambiguous encoded/traversal paths are rejected.
5. No wire credit/citation, declared original URL, reposter, quotation or reported
   speech suggests another origin. Explicit literal claimant attribution takes
   precedence; conflicting literal origins remain ambiguous, never replaced by the publisher.
6. A single unqualified event matches the reviewed subject scope. Multi-event,
   conditional or uncertain headlines abstain rather than guessing which assertion
   the publisher originated.

For `own_operations`, a single registered facility must be explicitly named and
its owner must match the reviewed operator. Recognized operation subjects are
conservatively tied to that facility, e.g. "Production suspended at Ras Tanura",
"Ras Tanura loading suspended" or "Loading at Ras Tanura suspended". Merely naming
a nearby facility, another operator, or an ambiguous compound subject is insufficient.
An attack on a facility is not automatically an own-operations claim.

For `own_sanctions_actions`, a supported sanctions event must begin with OFAC (or
its full name) performing a supported action such as imposing or lifting sanctions.
For `own_military_statements`, a supported military event must begin with CENTCOM
(or its supported names) conducting or launching the action. This latter rule is
tested with synthetic verified reviews; it is not activated in the live config.
The action must be directly tied to the classified event, not merely appear
earlier in the headline. Compound or temporal clauses abstain; so do unsupported
intervening words (for example, conducting an investigation into an airstrike).

These rules deliberately miss broader, vaguer or differently worded releases.
They do not expand the event taxonomy, infer missing entities, fetch article
bodies, or ask an LLM to determine the claimant.

## Audit and migration semantics

Each event has `claim_origin_basis`: `LITERAL_ATTRIBUTION`,
`FIRST_PARTY_PUBLICATION`, or `UNKNOWN`. `first_party_assessment` records application
or abstention and its reason. Successful inference includes the review hash,
authority scope, literal subject spans and raw observation IDs in
`first_party_evidence`. No receipt timestamp is moved.

The immutable classifier policy snapshot contains full reviews, source
registrations, scope definitions and subject patterns. The event's existing
policy/story input chain makes those decisions inspectable in exported snapshots.
Expiry, revocation and review changes affect future processing only; earlier
events and their historical as-of meaning remain unchanged. Policy changes keep
the experiment epoch and consumed cursor; there is no historical reclassification.

The main supervisor and standalone forward worker use the same review configuration.
`forward-quality` exposes claim-origin basis counts and first-party abstention
reasons. Existing events without those fields remain `LEGACY_UNMODELED` in that
summary. Identity reviews are trusted local configuration, never fields accepted
from an RSS publisher's text or structured news payload.
