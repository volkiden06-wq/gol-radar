#!/usr/bin/env python3
"""SportScore live-stat signal engine pilot. Dry-run only; never sends Telegram."""
import json
import os
import re
import sys
import time
from typing import Any

import requests

BASE = "https://sportscore.com"
UA = "GolRadarSignalPilot/1.0 (+https://github.com/volkiden06-wq/gol-radar)"
TIMEOUT = 18
MAX_DETAILS_PER_RUN = 5  # 1 fixture list + 5 details per 5-min run; approx. 1,728 requests/day.
MIN_SCORE_05 = 62
MIN_SCORE_15 = 76
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    m = re.search(r"-?\d+", str(value).replace(",", ""))
    return int(m.group(0)) if m else default


def fetch_json(url: str, params: dict | None = None) -> Any:
    response = SESSION.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def live_fixtures() -> list[dict]:
    payload = fetch_json(BASE + "/api/v1/fixtures/", {"sport": "football", "status": "live", "limit": 100, "src": "gol-radar"})
    if isinstance(payload, dict):
        rows = payload.get("matches") or payload.get("fixtures") or payload.get("data") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [x for x in rows if isinstance(x, dict)]


def fixture_slug(row: dict) -> str:
    parts = [x for x in str(row.get("url") or "").strip("/").split("/") if x]
    # SportScore detail API expects the human-readable match slug, not the trailing ID.
    return parts[-2] if len(parts) >= 2 else ""


def match_detail(row: dict) -> dict:
    slug = fixture_slug(row)
    if not slug:
        raise ValueError("Maç URL'sinden slug alınamadı")
    payload = fetch_json(BASE + "/api/v1/match/", {"sport": "football", "slug": slug, "src": "gol-radar"})
    if not isinstance(payload, dict) or not isinstance(payload.get("match"), dict):
        raise ValueError("SportScore maç detay yanıtı beklenen yapıda değil")
    return payload["match"]


def stat_totals(match: dict) -> dict:
    aliases = {
        "Ball Possession": "possession",
        "Shots on Target": "sot",
        "Shots off Target": "off",
        "Total Shots": "shots",
        "Attacks": "attacks",
        "Dangerous Attacks": "dangerous",
        "Corner Kicks": "corners",
        "Corners": "corners",
    }
    totals = {k: 0 for k in ("possession", "sot", "off", "shots", "attacks", "dangerous", "corners")}
    stats = match.get("stats") or []
    if isinstance(stats, dict):
        stats = stats.get("statistics") or stats.get("data") or []
    if not isinstance(stats, list):
        return totals
    for item in stats:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        key = aliases.get(label)
        if not key:
            continue
        home = as_int(item.get("home"))
        away = as_int(item.get("away"))
        totals[key] = home + away
    if totals["shots"] == 0:
        totals["shots"] = totals["sot"] + totals["off"]
    return totals


def evaluate(match: dict) -> list[dict]:
    status = str(match.get("status") or "").strip().casefold()
    status_text = str(match.get("status_text") or "").strip().casefold()
    period_text = status + " " + status_text
    minute = as_int(match.get("live_minute"), -1)
    if minute < 0 or status in {"finished", "ft", "half time", "ht", "ended"}:
        return []
    if any(token in period_text for token in ("2nd half", "second half", "2nd period", "second period", "extra time", "penalties")):
        return []
    if status_text in {"ht", "half time", "halftime", "finished", "ft"}:
        return []
    # Do not infer first-half status from minute alone. Require an explicit
    # first-half marker from the source to prevent second-half false positives.
    first_half_markers = ("1st half", "first half", "1h", "1st period", "first period")
    if not any(marker in period_text for marker in first_half_markers):
        return []
    home_goals = as_int(match.get("home_score"))
    away_goals = as_int(match.get("away_score"))
    goals = home_goals + away_goals
    metrics = stat_totals(match)
    sot = metrics["sot"]
    shots = metrics["shots"]
    dangerous = metrics["dangerous"]
    attacks = metrics["attacks"]
    corners = metrics["corners"]

    # A transparent rules-based score, not a claim of a guaranteed prediction.
    pressure = min(35, sot * 9) + min(22, shots * 1.6) + min(22, dangerous * 0.28)
    pressure += min(10, attacks * 0.07) + min(7, corners * 1.4)
    pressure = round(min(100, pressure))

    home = str(match.get("home") or "Ev sahibi")
    away = str(match.get("away") or "Deplasman")
    candidates = []

    # First-half Over 0.5: only before half-time and while score is 0-0.
    if 12 <= minute <= 39 and goals == 0:
        score = pressure + (5 if 20 <= minute <= 34 else 0)
        if sot >= 2 and (dangerous >= 25 or shots >= 7) and score >= MIN_SCORE_05:
            candidates.append({
                "market": "İY 0.5 ÜST",
                "score": min(100, score),
                "minute": minute,
                "current_score": f"{home_goals}-{away_goals}",
                "metrics": metrics,
                "home": home,
                "away": away,
            })

    # First-half Over 1.5: higher threshold; do not signal once market is already won.
    if 10 <= minute <= 34 and goals <= 1:
        score = pressure + (8 if goals == 1 else 0)
        if sot >= 4 and shots >= 11 and dangerous >= 50 and score >= MIN_SCORE_15:
            candidates.append({
                "market": "İY 1.5 ÜST",
                "score": min(100, score),
                "minute": minute,
                "current_score": f"{home_goals}-{away_goals}",
                "metrics": metrics,
                "home": home,
                "away": away,
            })
    return candidates


def self_test() -> bool:
    strong = {
        "status": "live", "status_text": "1st Half", "live_minute": 27, "home_score": 0, "away_score": 0,
        "home": "Test A", "away": "Test B",
        "stats": [
            {"label": "Shots on Target", "home": 3, "away": 1},
            {"label": "Shots off Target", "home": 5, "away": 4},
            {"label": "Attacks", "home": 52, "away": 43},
            {"label": "Dangerous Attacks", "home": 32, "away": 28},
            {"label": "Corner Kicks", "home": 3, "away": 2},
        ],
    }
    first_half = evaluate(strong)
    assert any(x["market"] == "İY 0.5 ÜST" for x in first_half), first_half
    assert any(x["market"] == "İY 1.5 ÜST" for x in first_half), first_half
    already_scored = dict(strong, home_score=1)
    assert not any(x["market"] == "İY 0.5 ÜST" for x in evaluate(already_scored))
    half_time = dict(strong, status="HT", live_minute=45)
    assert not evaluate(half_time)
    second_half = dict(strong, status="live", status_text="2nd Half", live_minute=27)
    assert not evaluate(second_half)
    unknown_period = dict(strong, status="live", status_text="", live_minute=27)
    assert not evaluate(unknown_period)
    quiet = dict(strong, stats=[])
    assert not evaluate(quiet)
    print("SELF_TEST | PASS | İY 0.5/1.5 eşikleri, skor filtresi, devre filtresi ve eksik veri kontrolü")
    return True


def main() -> int:
    if "--self-test" in sys.argv:
        self_test()
        return 0
    print("GOL RADAR SPORTScore ANALİZ | DRY RUN | TELEGRAM GÖNDERİMİ KAPALI")
    rows = live_fixtures()
    print(f"SPORTSCORE_LIVE | maç={len(rows)}")
    if not rows:
        print("CANLI_MAÇ_YOK")
        return 0
    # Rotate through live matches over successive 5-minute runs to respect the free API allowance.
    slot = int(time.time() // 300)
    start = (slot * MAX_DETAILS_PER_RUN) % len(rows)
    selected = [rows[(start + i) % len(rows)] for i in range(min(MAX_DETAILS_PER_RUN, len(rows)))]
    checked = 0
    candidate_count = 0
    for fixture in selected:
        try:
            detail = match_detail(fixture)
            checked += 1
            signals = evaluate(detail)
            home = detail.get("home") or fixture.get("home") or "?"
            away = detail.get("away") or fixture.get("away") or "?"
            minute = detail.get("live_minute", "?")
            score = f"{detail.get('home_score', 0)}-{detail.get('away_score', 0)}"
            print(f"MATCH | {home} - {away} | {minute}' | period={detail.get('status_text', detail.get('status', '?'))} | {score} | stats={stat_totals(detail)}")
            if checked == 1:
                print(f"SPORTSCORE_SCHEMA | match_keys={sorted(detail.keys())} | score_fields={{k: detail.get(k) for k in detail if 'score' in k.casefold() or 'goal' in k.casefold()}}")
                print(f"SPORTSCORE_FIXTURE_KEYS | {sorted(fixture.keys())}")
            for signal in signals:
                candidate_count += 1
                print(
                    f"CANDIDATE | {signal['market']} | {signal['home']} - {signal['away']}"
                    f" | {signal['minute']}' | skor={signal['current_score']}"
                    f" | puan={signal['score']}/100 | metrik={signal['metrics']}"
                )
        except Exception as exc:
            print(f"MATCH_ERROR | {fixture.get('url', '?')} | {type(exc).__name__}: {exc}")
    print(f"RUN_SUMMARY | canlı={len(rows)} | detay_kontrol={checked} | aday={candidate_count} | telegram=DISABLED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
