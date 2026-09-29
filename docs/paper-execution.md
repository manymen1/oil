# Local paper bot: execution and risk

This milestone implements the execution path missing from the standalone oil
project. It is a deterministic, local, single-contract paper account. It cannot
connect to a broker or send real orders. An older local paper engine was adapted
from the predecessor checkout, with stricter intent, audit and market guards.

## Run the complete synthetic execution loop

```bash
PYTHONPATH=src .venv/bin/python -m oilbot paper-scenario --file tests/fixtures/paper-engineering.json --out data/my-paper-scenario
```

The scenario is explicitly synthetic and includes quotes, long and short paper
intents, an exit target, a stop, and cancellation through a kill switch. Its MCL
contract is a **test fixture**, not the user's selected live instrument. Prices,
fees, loss limits and risk settings are engineering assumptions, not financial
recommendations or calibrated trading limits.

The directory contains a frozen `scenario.json`, append-only `paper.sqlite3`,
and `report.json`. To recover the *same* interrupted scenario, use the same
command with `--resume`. Changed inputs, contract definitions or risk limits
cannot silently reuse its account. Completed inputs are idempotent and the
reconciled result is identical after replay/resume.

A verified snapshot with explicit paper intents can also be replayed:

```bash
PYTHONPATH=src .venv/bin/python -m oilbot paper-replay --manifest SNAPSHOT/manifest.json --instrument EXACT_CONTRACT_ID --limits configs/paper-limits.json --out data/new-paper-replay
```

The contract must exist unambiguously in the snapshot market archive. Output must
be new and outside the snapshot. Recorded archive gaps reject replay; explicit
market status events are processed. Quote sequences must be instrument-scoped
and consecutive; discontinuities latch an entry halt. Provider/channel sequences
that include other message types need a separately qualified normalization
adapter, not silently invented continuity.

## The explicit paper-intent boundary

The engine accepts `PAPER_INTENT` only with `authorization: paper_only`, an exact
instrument ID, episode ID, integer quantity, direction (+1/-1) and decimal-string
limit price. `RESEARCH_CANDIDATE`, `ABSTAIN`, unknown contracts and unspecified
limits do not authorize an order. The news collector and economic bridge are
**not** automatically promoted into an intent generator.

```json
{
  "action": "PAPER_INTENT",
  "authorization": "paper_only",
  "instrument_id": "MCL-2026-10",
  "episode_id": "synthetic-example-only",
  "quantity": 1,
  "direction": 1,
  "limit_price": "72.03"
}
```

That example is a schema illustration, not a proposed trade. In a scenario,
wrap it in an input with a unique ID, availability time, and `kind: signal`.
For snapshot replay, store it as a dated `paper_intent` record with causal input
references. Future strategy-policy work must produce these intents only under
an explicit paper experiment policy and validated evidence; this milestone does
not implement a predictive strategy or claim any edge.

## Implemented behavior

- Durable submitted/acknowledged/working/partial/filled/cancelled order events,
  explicit cancellation, late-cancel rejection, deterministic order/event IDs.
- Delayed limit entries on subsequent executable quotes; no same-input fills or
  price chasing beyond the stricter supplied/arrival cap.
- Displayed-size partial entry with cancelled remainder and partial exits across
  subsequent quotes. Acknowledgements are simulated locally, not broker ACKs.
- Entry position/quantity and gross-notional limits; one position or pending
  order per account; one filled trade per episode; cooldown and UTC daily trade
  and marked-loss limits.
- Stop, target, maximum-hold and expiry exits; fees and adverse slippage on both
  sides; realized cash P&L plus explicitly timestamped marked equity.
- Freshness, spread, market flags, quote depth, session, expiry and clock-order
  checks. Missing prices do not manufacture fills. Halts leave exposure visible.
- A latched kill switch cancels entries and requests flattening. Flattening needs
  later executable quotes. Market RESUMED requires a new quote and does not
  automatically clear the account's entry halt.
- Atomic input/fill/account commits and input deduplication across crashes.
  The account cursor must match its last immutable account record; fill-ledger
  cash and signed quantities must reconcile with account cash and position.

The daily loss limit is a trigger, **not a guaranteed maximum loss**: gaps,
slippage or missing executable prices can exceed it. Stops do not guarantee
liquidity. Gross-notional checks constrain entries and do not model broker margin.

## Verified run and limitations

The saved engineering run is at `data/reviews/20260929-paper-engine/scenario/`.
It finished flat with reconciled ledgers. Its synthetic P&L is solely an arithmetic
fixture result and is not an estimate of live returns. No capture services were
changed or restarted to run it, and no persistent order-submission service was
installed.

Validation: the full regression suite passed **476 tests in 43.88 seconds**.
The final paper-focused checks passed **43 tests**, including the order-state
summary, intent guards, partial fills, halt/resume, crash recovery, cash/position
reconciliation, duplicate inputs, cancellation races and nanosecond latency.
`git diff --check` passed. Both existing capture/admission services remained active.

This implements a paper execution core, **not the full live bot**. Remaining work:

1. Qualified source processing and a validated automatic economic/episode policy.
2. A selected, entitled prospective market feed with trustworthy timing and gaps.
3. A frozen strategy and prospective paper campaign using actual synchronized
   news and market inputs, including realistic cost and missing-fill analysis.
4. Multi-contract portfolio/capital/margin controls and external OMS reconciliation.
5. A user-selected broker/instrument adapter, sandbox qualification, user-approved
   exposure/loss limits and separate live activation.

The current single-contract account prevents overlapping positions within that
account; it does not aggregate risk across independent account journals. There
is no broker order/fill ingestion, real account balance, margin-change simulation,
or guarantee of exactly-once network execution. The kill switch stays latched;
automatic resume of trading is intentionally absent.
