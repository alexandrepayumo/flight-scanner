"""Google Flights searches via the fast-flights library."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

log = logging.getLogger(__name__)


@dataclass
class Segment:
    origin: str
    destination: str
    depart: str  # local time, "YYYY-MM-DD HH:MM"
    arrive: str  # local time
    minutes: int


@dataclass
class Layover:
    airport: str
    minutes: int


@dataclass
class Itinerary:
    """One priced option returned by Google Flights."""

    kind: str  # "outbound", "inbound" or "roundtrip"
    origin: str
    destination: str
    date: str
    price: int  # total for all passengers
    airlines: list[str]
    segments: list[Segment]
    layovers: list[Layover] = field(default_factory=list)
    url: str = ""

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def total_minutes(self) -> int:
        return sum(s.minutes for s in self.segments) + sum(l.minutes for l in self.layovers)

    @property
    def key(self) -> str:
        """Stable identity for an itinerary (same flights, same day)."""
        legs = "|".join(f"{s.origin}-{s.destination}@{s.depart}" for s in self.segments)
        return f"{self.kind}:{legs}"

    def to_dict(self) -> dict:
        return asdict(self)


def _fmt(dt) -> str:
    (y, m, d), (hh, mm) = dt.date, dt.time
    return f"{y:04d}-{m:02d}-{d:02d} {hh:02d}:{mm:02d}"


def _parse_local(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def _to_itinerary(raw, *, kind: str, origin: str, destination: str, date: str, url: str) -> Itinerary:
    segments = [
        Segment(
            origin=f.from_airport.code,
            destination=f.to_airport.code,
            depart=_fmt(f.departure),
            arrive=_fmt(f.arrival),
            minutes=int(f.duration or 0),
        )
        for f in raw.flights
    ]
    layovers = []
    for prev, nxt in zip(segments, segments[1:]):
        # Both times are local to the connecting airport, so the difference is exact.
        gap = (_parse_local(nxt.depart) - _parse_local(prev.arrive)).total_seconds() / 60
        layovers.append(Layover(airport=prev.destination, minutes=int(gap)))
    return Itinerary(
        kind=kind,
        origin=origin,
        destination=destination,
        date=date,
        price=int(raw.price),
        airlines=list(raw.airlines),
        segments=segments,
        layovers=layovers,
        url=url,
    )


class FlightSearcher:
    def __init__(self, *, adults: int, currency: str, seat: str, delay: float, retries: int):
        self.adults = adults
        self.currency = currency
        self.seat = seat
        self.delay = delay
        self.retries = retries
        self.requests = 0
        self.failures = 0

    def _run(self, query, **meta) -> list[Itinerary]:
        url = query.url()
        for attempt in range(self.retries + 1):
            if self.requests:
                time.sleep(self.delay + random.uniform(0, self.delay))
            self.requests += 1
            try:
                results = get_flights(query)
                return [_to_itinerary(r, url=url, **meta) for r in results if r.price]
            except FlightsNotFound:
                return []
            except Exception as exc:  # network errors, bot pages, parser changes
                log.warning("search %s attempt %d failed: %s", meta, attempt + 1, exc)
        self.failures += 1
        return []

    def one_way(self, origin: str, destination: str, date: str, kind: str) -> list[Itinerary]:
        query = create_query(
            flights=[FlightQuery(date=date, from_airport=origin, to_airport=destination)],
            trip="one-way",
            seat=self.seat,
            passengers=Passengers(adults=self.adults),
            language="en-US",
            currency=self.currency,
        )
        return self._run(query, kind=kind, origin=origin, destination=destination, date=date)

    def round_trip(self, origin: str, destination: str, depart: str, ret: str) -> list[Itinerary]:
        """Prices are the full round trip; the listed segments are the outbound only."""
        query = create_query(
            flights=[
                FlightQuery(date=depart, from_airport=origin, to_airport=destination),
                FlightQuery(date=ret, from_airport=destination, to_airport=origin),
            ],
            trip="round-trip",
            seat=self.seat,
            passengers=Passengers(adults=self.adults),
            language="en-US",
            currency=self.currency,
        )
        return self._run(query, kind="roundtrip", origin=origin, destination=destination, date=depart)
