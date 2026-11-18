from scanner.deals import (
    Analysis,
    best_trip,
    detect_changes,
    detect_new_layovers,
    find_cool_layovers,
    record_alerted,
)
from scanner.search import Itinerary, Layover, Segment


def itin(kind, price, origin="YUL", dest="CUN", layovers=None, segs=None):
    segs = segs or [Segment(origin, dest, "2026-12-26 08:00", "2026-12-26 13:00", 300)]
    return Itinerary(kind, origin, dest, "2026-12-26", price, ["AC"], segs, layovers or [], "http://x")


def test_best_trip_prefers_cheaper_of_oneways_and_roundtrip():
    out, back = itin("outbound", 800), itin("inbound", 700, "CUN", "YYZ")
    t = best_trip("CUN", "Cancún", [out], [back], [itin("roundtrip", 1600)])
    assert t.price == 1500 and not t.is_roundtrip_fare and t.return_airport == "YYZ"

    t = best_trip("CUN", "Cancún", [out], [back], [itin("roundtrip", 1400)])
    assert t.price == 1400 and t.is_roundtrip_fare


def test_best_trip_needs_both_legs_or_roundtrip():
    assert best_trip("CUN", "Cancún", [itin("outbound", 800)], [], []) is None


def test_changes_first_seen_then_only_meaningful_drops():
    state = {}
    t = best_trip("CUN", "Cancún", [itin("outbound", 1000)], [itin("inbound", 1000, "CUN", "YUL")], [])
    changes = detect_changes([t], state, min_drop_dollars=40, min_drop_percent=5)
    assert [c.old for c in changes] == [None]
    record_alerted(state, Analysis([t], [], changes))

    t.price = 1990  # $10 drop: ignore
    assert detect_changes([t], state, min_drop_dollars=40, min_drop_percent=5) == []

    t.price = 1950  # $50 drop: alert
    (c,) = detect_changes([t], state, min_drop_dollars=40, min_drop_percent=5)
    assert (c.old, c.new) == (2000, 1950)


def test_cool_layover_detection_and_dedup():
    segs = [
        Segment("YUL", "KEF", "2026-12-26 20:00", "2026-12-27 06:00", 300),
        Segment("KEF", "LIS", "2026-12-27 16:00", "2026-12-27 20:00", 240),
    ]
    it = itin("outbound", 900, dest="LIS", segs=segs, layovers=[Layover("KEF", 600)])
    short = itin("outbound", 700, dest="LIS", segs=segs, layovers=[Layover("KEF", 90)])
    found = find_cool_layovers([it, it, short], {"KEF": "Reykjavík"}, 5)
    assert len(found) == 1 and found[0].hours == 10 and found[0].city == "Reykjavík"

    state = {}
    assert len(detect_new_layovers(found, state)) == 1
    record_alerted(state, Analysis([], found))
    assert detect_new_layovers(found, state) == []


def test_ranking_prefers_much_faster_option_when_price_is_close():
    from scanner.deals import Ranking

    slow = itin("outbound", 900, segs=[
        Segment("YUL", "YYZ", "2026-12-26 06:00", "2026-12-26 07:30", 90),
        Segment("YYZ", "CUN", "2026-12-26 19:00", "2026-12-26 23:00", 240),
    ], layovers=[Layover("YYZ", 690)])
    fast = itin("outbound", 950)
    back = itin("inbound", 700, "CUN", "YUL")
    assert best_trip("CUN", "C", [slow, fast], [back], []).outbound is slow
    rank = Ranking(hour_cost=20)
    assert best_trip("CUN", "C", [slow, fast], [back], [], rank).outbound is fast
    # A long layover in a cool city is not penalized.
    rank = Ranking(hour_cost=20, cool={"YYZ": "Toronto"}, min_layover_hours=5)
    assert best_trip("CUN", "C", [slow, fast], [back], [], rank).outbound is slow
