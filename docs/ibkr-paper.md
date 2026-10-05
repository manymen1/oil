# IBKR / MCL paper integration

The user selected Interactive Brokers and delegated the instrument choice. The
pilot target is **Micro WTI futures (MCL), NYMEX, USD**, not standard CL, a CFD,
an ETF or a continuous-futures order. MCL represents 100 barrels and has a $0.01
price increment ($1 per tick per contract). Its smaller contract size is the
reason for choosing it for engineering/paper testing, not a profitability claim.
See [CME's contract FAQ](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq).

## Implemented boundary

`oilbot ibkr-preflight` checks local configuration without opening a socket.
`oilbot ibkr-capture --connect` explicitly opens a **read-only** TWS API session
through IB Gateway or TWS, waits for both the account list and next-valid-ID
handshake, requests contract details, positions and all visible open orders,
then records a bounded tick-by-tick BidAsk sample. No orders are submitted,
modified, cancelled or automatically adopted. Existing positions/orders are
reported as blockers, including positions in other products in the target account.
The snapshot is not continuous order reconciliation.

MCL discovery validates symbol, security type, venue, currency, multiplier and
tick size. It selects the nearest unique last-trading date strictly beyond seven
calendar days from the current UTC date. Both contract month and exact conId
must come from the broker response. The contract is fixed for the capture;
there is no mid-session rollover or guessed expiry time. This expiry rule is an
engineering policy, not a liquidity/volume-based roll strategy.

The output directory must be new. It contains frozen `config.json`, append-only
`ibkr.sqlite3`, and `report.json`. Callbacks retain local UTC and monotonic receipt
timestamps, original provider seconds and BidAsk attributes. Raw callback fields
are recorded before quote admission; invalid, crossed, zero-size, flagged,
off-tick, old/future or time-regressing quotes do not count as usable. Numeric
unset/NaN values fail admission. No exchange sequence is invented: ordering is
explicitly local-callback-only. Callback journal account identities are replaced
by exact-match booleans; error messages/reject JSON are omitted to avoid leaking
account identifiers. Keep all captured broker data private; it is not public
redistribution material.

Unknown broker errors, account mismatches, queue overflow, missing snapshot-end
callbacks, connection loss and clock regressions/steps fail the run. There is no
silent delayed-data fallback or reconnect across an unrecorded gap. Shutdown
disconnects and drains already queued callbacks before reporting. An interrupted
process with only a session-start record is incomplete, never successful.

`capture_checks_passed` means only that this bounded diagnostic passed its checks.
Every report keeps `market_data_qualified: false`, `trade_authorized: false` and
`broker_execution: disabled`. These raw callbacks are intentionally not inserted
into the existing qualified quote archive or promoted into simulated fills.

## Local setup

Install the optional official SDK with:

```bash
.venv/bin/python -m pip install -e '.[ibkr]'
```

The optional extra pins the official Mac/Linux 10.50.02 source archive by SHA-256
and installs its Python subdirectory; it does not use the old PyPI `ibapi` release.
Archive checksum:
`673129e5cba58c4d77bc40647265f84ea42f605eccf88fa4c1221d62d12454f3`.
The SDK declares `protobuf==5.29.5`. Base observation installs do not require either.
The official SDK's license/notice apply; vendor code is not copied into this repo.

Use a logged-in **paper** IB Gateway (configured port 4002) or TWS (7497), with
socket clients enabled and **Read-Only API left enabled**. Do not follow the
order-placement setup's instruction to disable read-only for this capture.
Use a dedicated nonzero client ID, default 71. Account login/2FA and any required
market-data subscriptions remain the account owner's responsibility. This code
does not log in, obtain credentials, buy subscriptions or change account settings.

`configs/ibkr-paper.json` defaults to loopback and `paper_session_confirmed: false`.
Set the real paper account ID in the environment variable
`OILBOT_IBKR_PAPER_ACCOUNT` locally (do not paste passwords into chat or commit
account identifiers). Confirm the running application is logged into paper mode
before changing `paper_session_confirmed` to true in a private config copy.
The exact configured account must appear in `managedAccounts`. The DU prefix and
paper port are extra guards, **not cryptographic proof of account type**; this
milestone remains read-only even after those checks pass.

For Gateway running on Windows rather than WSL, explicitly configure its private
IP and the appropriate trusted-client/network rules. No host/credential discovery
or firewall changes are performed. The socket is not TLS: use only a trusted local
network, never expose it to the public Internet. Custom/live ports are rejected.

```bash
PYTHONPATH=src .venv/bin/python -m oilbot ibkr-preflight
PYTHONPATH=src .venv/bin/python -m oilbot ibkr-capture \
  --config configs/ibkr-paper.json --connect --out data/ibkr/NEW-CAPTURE
```

Exit code 2 means blocked/failed, including empty market data outside trading
hours. There is no recurring service installed by these commands. Source/news
services and the observation configuration are unchanged.

## Remaining before automated broker paper orders

1. Verify a real read-only paper connection and the account's tick-data entitlement.
2. Qualify session calendars, precise expiry, receive timing/clock uncertainty,
   data gaps, halt signals and reconnect behavior; build the canonical archive
   adapter and continuous monitoring. This sample alone does not establish these.
3. Add broker-specific durable order intents, submission uncertainty recovery,
   execution/commission reconciliation, broker-native protection and kill-switch
   tests. The local paper engine cannot stand in for broker fills.
4. Validate a frozen strategy with causal data and explicit paper risk limits;
   existing research candidates still never become orders automatically.
5. Run the forward broker-paper campaign and fault tests. Real-money activation
   requires separate approval and account/risk qualification.

## Verification on 29 September 2026

- Full repository suite: **539 passed in 77.30 seconds**, including 63 IBKR tests.
- The official SDK 10.50.2 and protobuf 5.29.5 were installed in the project venv;
  `pip check` passed. Optional-extra installation was also checked with pip dry-run.
- The SDK's real protobuf decoder was exercised offline for account, handshake,
  contract, BidAsk and error callbacks; no broker socket was opened.
- Fault tests cover account/port/product guards, expiry ambiguity, stale/invalid
  quotes, incomplete snapshots, disconnects, queued shutdown errors, overflow,
  clock steps, interruption, existing exposure and disabled order operations.
- Local preflight still reports `PAPER_LOGIN_NOT_CONFIRMED` and
  `EXPLICIT_PAPER_ACCOUNT_REQUIRED`. No paper/live account connection, market-data
  entitlement test, broker order, data purchase or service activation occurred.

These are engineering checks, not a validated trading strategy or evidence of
profitability. The original collector/admission deployments were not modified.

Primary references checked for this implementation:

- [IBKR official SDK downloads](https://interactivebrokers.github.io/)
- [TWS API installation](https://www.interactivebrokers.com/docs/tws-api/doc/quick-start/installation)
- [Contract details](https://www.interactivebrokers.com/docs/tws-api/doc/contracts-financial-instruments/contract-details/receive-contract-details)
- [Tick-by-tick requests](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/tick-by-tick-data/request-tick-by-tick-data)
- [BidAsk callbacks](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/tick-by-tick-data/receive-tick-by-tick-data)
- [Versioned error callback](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/receiving-error-messages)
- [All submitted orders snapshot](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/requesting-currently-active-orders/all-submitted-orders)
