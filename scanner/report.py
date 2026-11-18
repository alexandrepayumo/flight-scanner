"""Render the email (HTML + plain-text fallback)."""

from __future__ import annotations

from datetime import datetime
from html import escape

from .deals import Analysis, CoolLayover, Trip
from .search import Itinerary

AIRPORT_NAMES = {"YUL": "Montréal", "YYZ": "Toronto", "YOW": "Ottawa"}


def money(n: int | None) -> str:
    return "—" if n is None else f"${n:,}"


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def _time(s: str) -> str:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%b %d %H:%M")


def describe(it: Itinerary) -> str:
    first, last = it.segments[0], it.segments[-1]
    stops = "nonstop" if it.stops == 0 else f"{it.stops} stop" + ("s" if it.stops > 1 else "")
    via = ""
    if it.layovers:
        via = " via " + ", ".join(f"{l.airport} {_hm(l.minutes)}" for l in it.layovers)
    return (
        f"{first.origin}→{last.destination} {_time(first.depart)} → {_time(last.arrive)} · "
        f"{', '.join(it.airlines)} · {stops}{via} · {_hm(it.total_minutes)} total"
    )


def _link(url: str, text: str) -> str:
    return f'<a href="{escape(url)}">{escape(text)}</a>'


def _trip_rows(trips: list[Trip], adults: int) -> str:
    rows = []
    for t in trips:
        if t.is_roundtrip_fare:
            back = f"Round-trip fare back to {t.outbound.origin} on return date (pick flight at booking)"
            links = _link(t.outbound.url, "Google Flights")
        else:
            back = escape(describe(t.inbound))
            links = _link(t.outbound.url, "outbound") + " · " + _link(t.inbound.url, "return")
        rows.append(
            "<tr>"
            f"<td><b>{escape(t.destination_name)}</b> ({t.destination})</td>"
            f"<td><b>{money(t.price)}</b><br><small>{money(round(t.price / adults))} pp</small></td>"
            f"<td><small>↗ {escape(describe(t.outbound))}<br>↘ {back}</small></td>"
            f"<td><small>{links}</small></td>"
            "</tr>"
        )
    return "".join(rows)


def _layover_rows(items: list[CoolLayover]) -> str:
    return "".join(
        "<tr>"
        f"<td><b>{c.hours:g}h in {escape(c.city)}</b> ({c.airport})</td>"
        f"<td>{money(c.itinerary.price)}<br><small>{c.itinerary.kind}</small></td>"
        f"<td><small>{escape(describe(c.itinerary))}</small></td>"
        f"<td><small>{_link(c.itinerary.url, 'Google Flights')}</small></td>"
        "</tr>"
        for c in items
    )


TABLE = '<table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse;font-family:sans-serif;font-size:14px">{}</table>'


def subject(analysis: Analysis, is_digest: bool) -> str:
    if analysis.changes:
        c = analysis.changes[0]
        more = len(analysis.changes) - 1
        drop = f" (↓{money(c.old - c.new)})" if c.old else ""
        tail = f" + {more} more" if more > 0 else ""
        return f"✈️ {c.name} {money(c.new)} for 2{drop}{tail}"
    if analysis.new_layovers:
        c = analysis.new_layovers[0]
        return f"✈️ New layover idea: {c.hours:g}h in {c.city} ({money(c.itinerary.price)})"
    if is_digest and analysis.trips:
        t = analysis.trips[0]
        return f"✈️ Daily flight summary: cheapest is {t.destination_name} at {money(t.price)} for 2"
    return "✈️ Flight scanner update"


def render(analysis: Analysis, cfg: dict, *, is_digest: bool, stats: str) -> tuple[str, str]:
    trip = cfg["trip"]
    adults = trip["adults"]
    returns = ", ".join(AIRPORT_NAMES.get(a, a) for a in trip["return_airports"])
    parts = [
        '<meta charset="utf-8"><div style="font-family:sans-serif">',
        f"<p>Montréal → anywhere on <b>{trip['depart_date']}</b>, back to {returns} on "
        f"<b>{trip['return_date']}</b>. Prices are <b>total for {adults} people on the same flights</b> "
        f"in {trip['currency']}, from Google Flights.</p>",
    ]

    if analysis.changes:
        items = []
        for c in analysis.changes:
            if c.old is None:
                what = f"now tracked at <b>{money(c.new)}</b>"
            else:
                what = f"<b>{money(c.new)}</b> (was {money(c.old)}, ↓{money(c.old - c.new)})"
            badge = " 🔥 lowest seen" if c.all_time_low and c.old is not None else ""
            items.append(f"<li>{escape(c.name)} ({c.destination}): {what}{badge}</li>")
        parts.append("<h3>What's new</h3><ul>" + "".join(items) + "</ul>")

    top = analysis.trips[: cfg["alerts"]["top_n"]]
    if top:
        parts.append("<h3>Cheapest trips right now</h3>")
        parts.append(TABLE.format(
            "<tr><th>Destination</th><th>Total</th><th>Flights</th><th>Book</th></tr>" + _trip_rows(top, adults)
        ))

    lay = analysis.new_layovers if not is_digest else analysis.layovers
    if lay:
        title = "New long layovers in fun cities" if not is_digest else "Long layovers in fun cities"
        parts.append(f"<h3>{title}</h3><p><small>Itineraries with a stop of "
                     f"{cfg['layovers']['min_hours']}h+ in a city worth leaving the airport for. "
                     "Check visa/transit rules before you count on leaving the airport.</small></p>")
        parts.append(TABLE.format(
            "<tr><th>Layover</th><th>Leg price</th><th>Flights</th><th>Book</th></tr>" + _layover_rows(lay[:10])
        ))

    parts.append(
        "<p><small>Notes: two one-ways are separate tickets, so a missed connection on one does not "
        "protect the other. Prices change fast; confirm on the airline site. "
        f"{escape(stats)}</small></p></div>"
    )
    html = "".join(parts)

    text_lines = [f"Flights {trip['depart_date']} → {trip['return_date']}, totals for {adults} ({trip['currency']})", ""]
    for c in analysis.changes:
        text_lines.append(f"NEW: {c.name} {money(c.new)}" + (f" (was {money(c.old)})" if c.old else ""))
    text_lines.append("")
    for t in top:
        text_lines.append(f"{t.destination_name}: {money(t.price)}")
        text_lines.append(f"  out:  {describe(t.outbound)}")
        text_lines.append(f"  back: {describe(t.inbound) if t.inbound else 'round-trip fare'}")
        text_lines.append(f"  {t.outbound.url}")
    for c in lay[:10]:
        text_lines.append(f"LAYOVER {c.hours:g}h in {c.city}: {money(c.itinerary.price)} {describe(c.itinerary)}")
    return html, "\n".join(text_lines)
