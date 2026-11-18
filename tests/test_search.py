import json

import pytest

from scanner.search import BlockedOrChanged, _to_itinerary, parse_results


def _segment(frm, to, dep_date, dep_time, arr_date, arr_time, minutes):
    s = [None] * 22
    s[3], s[4], s[5], s[6] = frm, frm + " name", to + " name", to
    s[8], s[10], s[11], s[17] = dep_time, arr_time, minutes, "A320"
    s[20], s[21] = dep_date, arr_date
    return s


def _page(payload) -> str:
    return (
        '<html><script class="ds:1">AF_initDataCallback({key: \'ds:1\', hash: \'1\', data:'
        + json.dumps(payload)
        + ", sideChannel: {}});</script></html>"
    )


def test_parses_both_blocks_and_skips_unpriced_entries():
    s1 = _segment("YUL", "KEF", [2026, 12, 26], [20], [2026, 12, 27], [6], 300)
    s2 = _segment("KEF", "LIS", [2026, 12, 27], [16, 30], [2026, 12, 27], [20], 240)
    good = [["FI", ["Icelandair"], [s1, s2]], [[None, 1500]]]
    unpriced = [["AC", ["Air Canada"], [s1]], [[], "token"]]
    payload = [None, None, [[good]], [[unpriced, good]]]

    flights = parse_results(_page(payload))
    assert [f.price for f in flights] == [1500, 1500]

    it = _to_itinerary(flights[0], kind="outbound", origin="YUL", destination="LIS", date="2026-12-26", url="u")
    assert it.stops == 1
    assert it.layovers[0].airport == "KEF" and it.layovers[0].minutes == 630
    assert it.total_minutes == 300 + 630 + 240


def test_page_without_data_is_reported_as_blocked():
    with pytest.raises(BlockedOrChanged):
        parse_results("<html>consent page</html>")
