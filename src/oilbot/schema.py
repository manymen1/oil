from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Protocol, Iterable

from .clock import instant, epoch_ns


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class NewsItem:
    native_id: str
    url: str
    title: str
    text: str
    published_at: str | None = None
    status: str = "update"
    origin: str | None = None
    links: tuple[str, ...] = ()
    author: str | None = None
    original_url: str | None = None
    reposter: str | None = None
    media_sha256: str | None = None


class NewsSourceAdapter(Protocol):
    def parse(self, payload: bytes, url: str, content_type: str) -> list[NewsItem]: ...


@dataclass(frozen=True)
class InstrumentDefinition:
    instrument_id: str
    product: str
    month: str
    exchange: str
    currency: str
    multiplier: str
    tick_size: str
    last_trade_at: str
    sessions: list[dict]
    available_at: str
    vendor_id: str
    data_mode: str = "fixture"
    broker_id: str | None = None

    def validate(self) -> None:
        if self.product not in {"CL", "MCL"} or self.exchange != "NYMEX" or self.currency != "USD":
            raise ValueError("unsupported instrument")
        if Decimal(self.multiplier) != {"CL": 1000, "MCL": 100}[self.product]:
            raise ValueError("incorrect multiplier")
        if Decimal(self.tick_size) != Decimal("0.01"):
            raise ValueError("incorrect tick size")
        import re
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", self.month):
            raise ValueError("explicit YYYY-MM contract month required")
        instant(self.available_at)
        instant(self.last_trade_at)
        if not self.instrument_id or not self.vendor_id or not self.sessions:
            raise ValueError("contract identifiers and sessions required")
        for session in self.sessions:
            if instant(session["open"]) >= instant(session["close"]):
                raise ValueError("invalid session")

    def ticks(self, price: str) -> int:
        value = Decimal(price) / Decimal(self.tick_size)
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError("price is not a finite tick multiple")
        return int(value)


@dataclass(frozen=True, kw_only=True)
class MarketTiming:
    provider: str = "fixture"
    contract_month: str | None = None
    exchange_event_ns: int | None = None
    provider_receive_ns: int | None = None
    local_receive_ns: int | None = None
    availability_basis: str = "fixture"
    sequence_scope: str = "instrument"
    source_record_id: str | None = None
    flags: int = 0

    def validate_timing(self, definition):
        if type(self.flags) is not int or not 0 <= self.flags <= 255:
            raise ValueError("invalid market flags")
        if self.contract_month is not None and self.contract_month != definition.month:
            raise ValueError("contract month mismatch")
        for value in (self.exchange_event_ns, self.provider_receive_ns, self.local_receive_ns):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid nanosecond timestamp")
        basis = {"fixture": None, "local_receive": self.local_receive_ns,
                 "provider_receive_proxy": self.provider_receive_ns}
        if self.availability_basis not in basis:
            raise ValueError("unknown availability basis")
        if self.availability_basis != "fixture" and basis[self.availability_basis] != epoch_ns(self.available_at):
            raise ValueError("availability timestamp does not match declared basis")


@dataclass(frozen=True)
class QuoteEvent(MarketTiming):
    instrument_id: str
    available_at: str
    bid: str | None
    ask: str | None
    bid_size: int
    ask_size: int
    bid_at: str
    ask_at: str
    sequence: int
    data_mode: str
    subscription: str
    source_at: str | None = None
    event_type: str = "quote"
    bid_count: int | None = None
    ask_count: int | None = None

    def validate(self, definition: InstrumentDefinition) -> None:
        definition.validate()
        self.validate_timing(definition)
        if self.event_type != "quote":
            raise ValueError("QuoteEvent cannot represent a trade or status")
        if self.instrument_id != definition.instrument_id:
            raise ValueError("instrument mismatch")
        for at in (self.available_at, self.bid_at, self.ask_at):
            instant(at)
        if epoch_ns(definition.available_at) > epoch_ns(self.available_at):
            raise ValueError("future instrument definition")
        if max(epoch_ns(self.bid_at), epoch_ns(self.ask_at)) > epoch_ns(self.available_at):
            raise ValueError("future quote component")
        if self.bid is not None:
            definition.ticks(self.bid)
        if self.ask is not None:
            definition.ticks(self.ask)
        if self.bid is not None and self.ask is not None and Decimal(self.bid) > Decimal(self.ask):
            raise ValueError("crossed book")
        if any(type(v) is not int or v < 0 for v in (self.bid_size, self.ask_size, self.sequence)):
            raise ValueError("invalid size/sequence")
        if any(v is not None and (type(v) is not int or v < 0) for v in (self.bid_count, self.ask_count)):
            raise ValueError("invalid order count")
        if self.data_mode not in {"fixture", "realtime", "historical", "delayed", "snapshot", "aggregated"}:
            raise ValueError("unknown market data mode")


# Existing fixture/research callers continue to mean quote; new trades are explicit.
MarketEvent = QuoteEvent


@dataclass(frozen=True)
class TradeEvent(MarketTiming):
    instrument_id: str
    available_at: str
    trade_price: str
    trade_size: int
    aggressor: str
    sequence: int
    data_mode: str
    subscription: str
    event_type: str = "trade"

    def validate(self, definition):
        definition.validate()
        self.validate_timing(definition)
        if self.instrument_id != definition.instrument_id or self.event_type != "trade":
            raise ValueError("trade instrument/type mismatch")
        if epoch_ns(definition.available_at) > epoch_ns(self.available_at):
            raise ValueError("future instrument definition")
        if not isinstance(self.trade_price, str):
            raise ValueError("trade price required")
        definition.ticks(self.trade_price)
        if type(self.trade_size) is not int or self.trade_size <= 0:
            raise ValueError("invalid trade size")
        if type(self.sequence) is not int or self.sequence < 0 or self.aggressor not in {"buy", "sell", "unknown"}:
            raise ValueError("invalid trade sequence/aggressor")
        if self.data_mode not in {"fixture", "historical", "realtime", "delayed"}:
            raise ValueError("unknown trade data mode")


@dataclass(frozen=True)
class MarketStatusEvent(MarketTiming):
    instrument_id: str
    available_at: str
    status: str
    reason: str
    sequence: int
    data_mode: str
    subscription: str
    event_type: str = "status"

    def validate(self, definition):
        definition.validate()
        self.validate_timing(definition)
        if self.event_type != "status" or self.instrument_id != definition.instrument_id or self.status not in {"GAP", "HALTED", "RESET", "RESUMED"}:
            raise ValueError("invalid market status")
        if type(self.sequence) is not int or self.sequence < 0 or self.data_mode not in {"fixture", "historical", "realtime", "delayed"}:
            raise ValueError("invalid status sequence/data mode")
        if epoch_ns(definition.available_at) > epoch_ns(self.available_at):
            raise ValueError("future instrument definition")


def market_event(payload: dict):
    types = {"quote": QuoteEvent, "trade": TradeEvent, "status": MarketStatusEvent}
    kind = payload.get("event_type", "quote")
    if kind not in types:
        raise ValueError("unknown market event type")
    return types[kind](**payload)


class MarketDataAdapter(Protocol):
    def definitions(self) -> Iterable[InstrumentDefinition]: ...
    def events(self) -> Iterable[QuoteEvent | TradeEvent | MarketStatusEvent]: ...


FACT_FIELDS = {
    "actor", "asset", "action", "location", "event_time", "operational_status",
    "reported_quantity", "duration", "attribution", "restoration_window",
    "restoration_extent", "flow_evidence",
}
ASSERTIONS = {"asserted", "denied", "conditional", "historical", "unknown"}
OPERATIONS = {"unknown", "operating", "impaired", "suspended", "partly_restored", "restored"}


@dataclass(frozen=True)
class Fact:
    field: str
    value: str
    start: int
    end: int
    quote: str
    assertion: str
    unit: str | None = None
    quantity_kind: str | None = None

    def validate(self, text: str) -> None:
        if self.field not in FACT_FIELDS or self.assertion not in ASSERTIONS:
            raise ValueError("invalid fact category")
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end <= len(text):
            raise ValueError("invalid evidence offsets")
        if text[self.start:self.end] != self.quote or not self.quote.strip():
            raise ValueError("unsupported evidence span")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("missing fact value")
        if self.field == "operational_status" and self.value not in OPERATIONS:
            raise ValueError("invalid operational status")
        if self.field == "operational_status" and self.assertion == "asserted":
            import re
            if re.search(r"\b(?:not\s+(?:suspended|restored|impaired|operating)|if|unless|might|could|would|denies|denied)\b", self.quote, re.I):
                raise ValueError("operational assertion conflicts with explicit conditional/denial language")
        if self.field == "reported_quantity":
            if self.quantity_kind not in {"gross_capacity", "production_loss", "delivery_disruption", "replacement_supply", "inventory_withdrawal", "demand_reduction", "unknown"}:
                raise ValueError("quantity accounting boundary required")
            if not self.unit or not Decimal(self.value).is_finite() or Decimal(self.value) < 0:
                raise ValueError("invalid reported quantity")
            import re
            numbers = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", self.quote)
            if Decimal(self.value) not in [Decimal(number.replace(",", "")) for number in numbers]:
                raise ValueError("quantity not literally supported; do not infer or multiply capacity")
            if self.unit.casefold() not in self.quote.casefold():
                raise ValueError("original reported unit must appear in quantity evidence")


def to_dict(record) -> dict:
    return asdict(record)
