"""Turn raw search results into trip options and decide what is worth emailing."""

from __future__ import annotations

from dataclasses import dataclass, field

from .search import Itinerary


@dataclass
class Trip:
    """A complete way to get there and back, priced for everyone."""

    destination: str
    destination_name: str
    price: int
    outbound: Itinerary
    inbound: Itinerary | None  # None for round-trip fares (return picked at booking)
    is_roundtrip_fare: bool = False

    @property
    def return_airport(self) -> str:
        return self.inbound.destination if self.inbound else self.outbound.origin


@dataclass
class CoolLayover:
    itinerary: Itinerary
    airport: str
    city: str
    hours: float


@dataclass
class Change:
    destination: str
    name: str
    old: int | None
    new: int
    all_time_low: bool


@dataclass
class Analysis:
    trips: list[Trip]  # cheapest trip per destination, sorted by price
    layovers: list[CoolLayover]
    changes: list[Change] = field(default_factory=list)
    new_layovers: list[CoolLayover] = field(default_factory=list)


def _cheapest(items: list[Itinerary]) -> Itinerary | None:
    return min(items, key=lambda i: (i.price, i.total_minutes), default=None)


def best_trip(
    code: str,
    name: str,
    outbound: list[Itinerary],
    inbound: list[Itinerary],
    roundtrip: list[Itinerary],
) -> Trip | None:
    """Cheapest of: separate one-ways (any return airport) vs. a round-trip fare."""
    candidates: list[Trip] = []
    out, back = _cheapest(outbound), _cheapest(inbound)
    if out and back:
        candidates.append(Trip(code, name, out.price + back.price, out, back))
    rt = _cheapest(roundtrip)
    if rt:
        candidates.append(Trip(code, name, rt.price, rt, None, is_roundtrip_fare=True))
    return min(candidates, key=lambda t: t.price, default=None)


def find_cool_layovers(itins: list[Itinerary], cool: dict[str, str], min_hours: float) -> list[CoolLayover]:
    found = []
    for it in itins:
        for lay in it.layovers:
            hours = lay.minutes / 60
            if lay.airport in cool and hours >= min_hours:
                found.append(CoolLayover(it, lay.airport, cool[lay.airport], round(hours, 1)))
    # One entry per itinerary/airport, cheapest first.
    seen, unique = set(), []
    for c in sorted(found, key=lambda c: c.itinerary.price):
        k = (c.itinerary.key, c.airport)
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique


def detect_changes(trips: list[Trip], state: dict, *, min_drop_dollars: int, min_drop_percent: float) -> list[Change]:
    """New destinations and meaningful price drops since the last alert."""
    dest_state = state.setdefault("destinations", {})
    changes = []
    for t in trips:
        s = dest_state.get(t.destination, {})
        last = s.get("last_alert_price")
        low = s.get("all_time_low")
        is_low = low is None or t.price < low
        if last is None:
            changes.append(Change(t.destination, t.destination_name, None, t.price, is_low))
            continue
        drop = last - t.price
        if drop >= min_drop_dollars or (last and drop / last * 100 >= min_drop_percent):
            changes.append(Change(t.destination, t.destination_name, last, t.price, is_low))
    return sorted(changes, key=lambda c: c.new)


def detect_new_layovers(layovers: list[CoolLayover], state: dict) -> list[CoolLayover]:
    seen = state.setdefault("layovers_seen", {})
    new = []
    for c in layovers:
        k = f"{c.itinerary.key}#{c.airport}"
        prev = seen.get(k)
        if prev is None or c.itinerary.price < prev:
            new.append(c)
    return new


def record_alerted(state: dict, analysis: Analysis) -> None:
    """Remember what we told you so the next email only has news."""
    dest_state = state.setdefault("destinations", {})
    for c in analysis.changes:
        dest_state.setdefault(c.destination, {})["last_alert_price"] = c.new
    seen = state.setdefault("layovers_seen", {})
    for c in analysis.layovers:
        seen[f"{c.itinerary.key}#{c.airport}"] = c.itinerary.price


def record_observed(state: dict, trips: list[Trip], now_iso: str) -> None:
    dest_state = state.setdefault("destinations", {})
    for t in trips:
        s = dest_state.setdefault(t.destination, {})
        s["last_seen_price"] = t.price
        s["all_time_low"] = min(t.price, s.get("all_time_low", t.price))
        hist = s.setdefault("history", [])
        hist.append([now_iso, t.price])
        del hist[:-200]  # keep the file small
