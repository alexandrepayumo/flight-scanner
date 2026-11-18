"""Hourly entry point: search, compare with last run, email if there is news.

Usage:
    python -m scanner.main             # search + email (needs SMTP_* and EMAIL_TO env vars)
    python -m scanner.main --dry-run   # search, write out/email.html, send nothing, keep state untouched
    python -m scanner.main --limit 3   # only the first N destinations (quick test)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import emailer
from .deals import (
    Analysis,
    Ranking,
    best_trip,
    detect_changes,
    detect_new_layovers,
    find_cool_layovers,
    record_alerted,
    record_observed,
)
from .report import render, subject
from .search import FlightSearcher

log = logging.getLogger("scanner")
ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Toronto")


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = state.get("layovers_seen", {})
    if len(seen) > 2000:
        state["layovers_seen"] = dict(list(seen.items())[-2000:])
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def scan(cfg: dict, limit: int | None) -> tuple[Analysis, FlightSearcher, list]:
    trip = cfg["trip"]
    s = FlightSearcher(
        adults=trip["adults"],
        currency=trip["currency"],
        seat=trip["seat"],
        delay=cfg["search"]["delay_seconds"],
        retries=cfg["search"]["retries"],
    )
    origin, out_date, ret_date = trip["origin"], trip["depart_date"], trip["return_date"]
    dests = list(cfg["destinations"].items())[:limit]

    lay = cfg["layovers"]
    rank = Ranking(cfg["search"].get("hour_cost", 0), lay["cool_cities"], lay["min_hours"])
    trips, all_itins = [], []
    for i, (code, name) in enumerate(dests, 1):
        log.info("[%d/%d] %s (%s)", i, len(dests), name, code)
        outbound = s.one_way(origin, code, out_date, "outbound")
        inbound = []
        for home in trip["return_airports"]:
            inbound += s.one_way(code, home, ret_date, "inbound")
        roundtrip = s.round_trip(origin, code, out_date, ret_date)
        all_itins += outbound + inbound + roundtrip
        t = best_trip(code, name, outbound, inbound, roundtrip, rank)
        if t:
            trips.append(t)
            log.info("    best %s total", t.price)

    trips.sort(key=lambda t: t.price)
    layovers = find_cool_layovers(all_itins, lay["cool_cities"], lay["min_hours"])
    return Analysis(trips=trips, layovers=layovers), s, all_itins


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force-email", action="store_true", help="send even if nothing is new")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("primp").setLevel(logging.WARNING)  # it logs every HTTP response

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    state_path = Path(os.environ.get("STATE_PATH", ROOT / "state" / "state.json"))
    state = load_state(state_path)
    now = datetime.now(TZ)

    if now.date() >= date.fromisoformat(cfg["trip"]["depart_date"]):
        log.info("Departure date reached; nothing left to scan.")
        return 0

    missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO") if not os.environ.get(k)]
    if missing and not args.dry_run:
        log.error("Missing email settings: %s. Add them as repository secrets (see README).", ", ".join(missing))
        return 2

    analysis, searcher, itins = scan(cfg, args.limit)
    stats = f"{searcher.requests} searches, {searcher.failures} failed, run at {now:%Y-%m-%d %H:%M} ET."
    log.info(stats)

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps([i.to_dict() for i in itins], indent=1), encoding="utf-8")

    # Health check: if Google starts blocking us, say so once instead of going silent.
    failed_run = searcher.requests > 0 and searcher.failures / max(1, searcher.requests) > 0.5
    state["failed_runs"] = state.get("failed_runs", 0) + 1 if failed_run else 0
    if state["failed_runs"] == 3 and not args.dry_run:
        emailer.send(
            "âš ï¸ Flight scanner: searches are failing",
            f"<p>The last 3 hourly runs mostly failed ({stats}). Google may be blocking the runner. "
            "Check the GitHub Actions logs.</p>",
            f"The last 3 hourly runs mostly failed. {stats}",
        )
    if not analysis.trips:
        log.error("No trips found. %s", stats)
        if not args.dry_run:
            save_state(state_path, state)
        return 1

    alerts = cfg["alerts"]
    analysis.changes = detect_changes(
        analysis.trips, state, min_drop_dollars=alerts["min_drop_dollars"], min_drop_percent=alerts["min_drop_percent"]
    )
    analysis.new_layovers = detect_new_layovers(analysis.layovers, state)

    today = now.date().isoformat()
    digest_hour = alerts.get("daily_digest_hour")
    is_digest = digest_hour is not None and now.hour >= digest_hour and state.get("last_digest_date") != today
    should_send = bool(analysis.changes or analysis.new_layovers or is_digest or args.force_email)

    html, text = render(analysis, cfg, is_digest=is_digest, stats=stats)
    subj = subject(analysis, is_digest)
    (out_dir / "email.html").write_text(html, encoding="utf-8")
    log.info("Subject: %s | changes=%d new_layovers=%d digest=%s send=%s",
             subj, len(analysis.changes), len(analysis.new_layovers), is_digest, should_send)

    if args.dry_run:
        log.info("Dry run: wrote out/email.html, not sending, state unchanged.")
        return 0

    if should_send:
        emailer.send(subj, html, text)
        log.info("Email sent to %d recipient(s).", len(emailer.recipients()))
        record_alerted(state, analysis)
        if is_digest:
            # The digest shows everything, so reset baselines to current prices.
            for t in analysis.trips:
                state.setdefault("destinations", {}).setdefault(t.destination, {})["last_alert_price"] = t.price
            state["last_digest_date"] = today

    record_observed(state, analysis.trips, now.isoformat(timespec="minutes"))
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
