"""Offline GLBX.MDP3 MBP-1/trades import; never requests or purchases data."""
from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .clock import epoch_ns, iso_ns
from .schema import InstrumentDefinition, QuoteEvent, TradeEvent, MarketStatusEvent, digest

UNDEF_PRICE = 2**63 - 1
BAD_FLAGS = 8 | 4


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class DatabentoHistoricalAdapter:
    """Native DBN or numeric CSV (pretty_ts=False, pretty_px=False).

    An explicit, dated contract registry binds vendor IDs to delivery months.
    Historical provider receipt is a proxy, never fabricated local receipt.
    MBP-1 already includes trades: do not import a duplicate trades file.
    """
    def __init__(self, path: Path, registry: Path, *, schema="mbp-1"):
        if schema not in {"mbp-1", "trades"}:
            raise ValueError("only mbp-1 and trades are supported")
        self.path, self.registry, self.schema = Path(path), Path(registry), schema
        raw = json.loads(self.registry.read_text())
        if raw.get("dataset") != "GLBX.MDP3" or not raw.get("definition_source"):
            raise ValueError("GLBX.MDP3 definition provenance required")
        self._definitions = [InstrumentDefinition(**row) for row in raw["definitions"]]
        for definition in self._definitions:
            definition.validate()
            if definition.data_mode != "historical":
                raise ValueError("historical definitions required")
        self.by_vendor = {int(d.vendor_id): d for d in self._definitions}
        if len(self.by_vendor) != len(self._definitions):
            raise ValueError("duplicate vendor contract mapping")
        self.source_hash = file_hash(path)
        self.registry_hash = file_hash(registry)

    def definitions(self):
        return iter(self._definitions)

    def _rows(self):
        if self.path.suffix == ".csv":
            with self.path.open(newline="") as stream:
                yield from csv.DictReader(stream)
            return
        try:
            import databento as db
        except ImportError as exc:
            raise RuntimeError("DBN import requires pip install -e '.[databento]'") from exc
        store = db.DBNStore.from_file(self.path)
        if str(store.metadata.dataset) != "GLBX.MDP3" or str(store.metadata.schema) != self.schema:
            raise ValueError("DBN dataset/schema mismatch")
        for row in store:
            # DBN numeric values preserve exact nanoseconds and 1e-9 price units.
            value = {key: getattr(row, key) for key in
                     ("instrument_id", "ts_event", "ts_recv", "price", "size", "sequence", "flags", "action", "side")}
            if self.schema == "mbp-1":
                for key in ("bid_px", "ask_px", "bid_sz", "ask_sz", "bid_ct", "ask_ct"):
                    value[key + "_00"] = getattr(row.levels[0], key)
            yield value

    def events(self):
        prior_ns = None
        for index, row in enumerate(self._rows()):
            vendor = int(row["instrument_id"])
            if vendor not in self.by_vendor:
                raise ValueError(f"unmapped Databento instrument_id {vendor}")
            definition = self.by_vendor[vendor]
            recv, event_ns = int(row["ts_recv"]), int(row["ts_event"])
            if prior_ns is not None and recv < prior_ns:
                raise ValueError("Databento input not ordered by provider receipt")
            prior_ns = recv
            flags, sequence = int(row["flags"]), int(row["sequence"])
            at = iso_ns(recv)
            common = dict(instrument_id=definition.instrument_id, available_at=at,
                          contract_month=definition.month, provider="databento", data_mode="historical",
                          subscription="GLBX.MDP3:" + self.schema, sequence=sequence,
                          sequence_scope="publisher_channel", exchange_event_ns=event_ns,
                          provider_receive_ns=recv, local_receive_ns=None,
                          availability_basis="provider_receive_proxy", flags=flags,
                          source_record_id=digest([self.source_hash, index]))
            if flags & BAD_FLAGS:
                yield MarketStatusEvent(**common, status="GAP", reason="PROVIDER_QUALITY_FLAGS")
            elif flags & 32:
                yield MarketStatusEvent(**common, status="RESUMED", reason="PROVIDER_SNAPSHOT")
            if row["action"] == "R":
                yield MarketStatusEvent(**common, status="RESET", reason="BOOK_RESET")
            if row["action"] == "T":
                yield TradeEvent(**common, trade_price=_price(row["price"]), trade_size=int(row["size"]),
                                 aggressor={"B": "buy", "A": "sell", "N": "unknown"}[str(row["side"])])
            if self.schema == "mbp-1":
                yield QuoteEvent(**common, bid=_price(row["bid_px_00"]), ask=_price(row["ask_px_00"]),
                                 bid_size=int(row["bid_sz_00"]), ask_size=int(row["ask_sz_00"]),
                                 bid_count=int(row["bid_ct_00"]), ask_count=int(row["ask_ct_00"]),
                                 bid_at=at, ask_at=at)

    def provenance(self):
        return {"dataset": "GLBX.MDP3", "schema": self.schema, "source_sha256": self.source_hash,
                "registry_sha256": self.registry_hash, "availability_basis": "provider_receive_proxy",
                "local_receive_known": False, "sequence_gaps": "Not inferred from symbol-filtered channel sequences"}


def _price(value):
    raw = int(value)
    return None if raw == UNDEF_PRICE else str(Decimal(raw) / Decimal(10**9))
