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


def telegram_call(method: str, payload: dict) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN GitHub Secret bulunamadı")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload,
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description', 'bilinmeyen hata')}")
    return data.get("result") or {}


def settle_market(market: str, home_ht: int, away_ht: int) -> str:
    total = home_ht + away_ht
    if market == "İY 0.5 ÜST":
        return "KAZANDI" if total >= 1 else "KAYBETTİ"
    if market == "İY 1.5 ÜST":
        return "KAZANDI" if total >= 2 else "KAYBETTİ"
    raise ValueError(f"Bilinmeyen market: {market}")


def load_state() -> dict:
    path = "sportscore_state.json"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"signals": {}}
    except (OSError, ValueError):
        return {"signals": {}}


def save_state(state: dict) -> None:
    with open("sportscore_state.json", "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)


def is_half_time(match: dict) -> bool:
    status = str(match.get("status") or "").strip().casefold()
    status_text = str(match.get("status_text") or "").strip().casefold()
    return status in {"ht", "half time"} or status_text in {"ht", "half time", "halftime"}


def is_finished(match: dict) -> bool:
    status = str(match.get("status") or "").strip().casefold()
    status_text = str(match.get("status_text") or "").strip().casefold()
    return status in {"ft", "finished", "ended"} or status_text in {"ft", "finished", "ended", "full time"}


def process_result(match: dict, slug: str, state: dict) -> int:
    changed = 0
    home_ht = match.get("home_ht_score")
    away_ht = match.get("away_ht_score")
    if is_half_time(match):
        if home_ht is None:
            home_ht = match.get("home_score")
        if away_ht is None:
            away_ht = match.get("away_score")
    if home_ht is None or away_ht is None:
        return 0
    home_ht, away_ht = as_int(home_ht, -1), as_int(away_ht, -1)
    if home_ht < 0 or away_ht < 0:
        return 0
    if not (is_half_time(match) or is_finished(match)):
        return 0

    signals = state.setdefault("signals", {})
    for key, record in list(signals.items()):
        if not isinstance(record, dict) or record.get("slug") != slug or record.get("result"):
            continue
        result = settle_market(record.get("market", ""), home_ht, away_ht)
        text = (
            str(record.get("message_text") or "")
            + f"\n\n🏁 İLK YARI SONUCU: {home_ht}-{away_ht}"
            + f"\n{'✅' if result == 'KAZANDI' else '❌'} {record.get('market')} — {result}"
        )
        try:
            telegram_call("editMessageText", {
                "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
                "message_id": record["message_id"],
                "text": text,
                "disable_web_page_preview": True,
            })
            record["result"] = result
            record["half_time_score"] = [home_ht, away_ht]
            record["result_text"] = text
            changed += 1
            print(f"RESULT_EDITED | {record.get('market')} | {slug} | {home_ht}-{away_ht} | {result}")
        except Exception as exc:
            print(f"RESULT_EDIT_ERROR | {record.get('market')} | {slug} | {exc}")
    return changed


def build_signal_text(signal: dict, detail: dict) -> str:
    competition = detail.get("competition") or ""
    lines = [
        "⚽ GOL RADAR | CANLI İLK YARI SİNYALİ",
        f"{signal['home']} - {signal['away']}",
    ]
    if competition:
        lines.append(f"🏆 {competition}")
    lines.extend([
        f"⏱ {signal['minute']}' | Anlık skor: {signal['current_score']}",
        f"🎯 {signal['market']}",
        f"📊 Analiz puanı: {signal['score']}/100",
        f"📈 İsabetli şut: {signal['metrics']['sot']} | Toplam şut: {signal['metrics']['shots']}",
        f"⚠️ Tehlikeli atak: {signal['metrics']['dangerous']} | Korner: {signal['metrics']['corners']}",
        "ℹ️ İstatistiksel analizdir; gol garantisi veya bahis tavsiyesi değildir.",
    ])
    return "\n".join(lines)


def main() -> int:
    if "--self-test" in sys.argv:
        self_test()
        assert settle_market("İY 0.5 ÜST", 1, 0) == "KAZANDI"
        assert settle_market("İY 0.5 ÜST", 0, 0) == "KAYBETTİ"
        assert settle_market("İY 1.5 ÜST", 1, 1) == "KAZANDI"
        assert settle_market("İY 1.5 ÜST", 1, 0) == "KAYBETTİ"
        print("SETTLEMENT_TEST | PASS | İY 0.5/1.5 ilk yarı sonuçları")
        global telegram_call
        original_telegram_call = telegram_call
        mocked_calls = []
        def fake_telegram_call(method, payload):
            mocked_calls.append((method, payload))
            return {"message_id": 999}
        telegram_call = fake_telegram_call
        try:
            state = {"signals": {
                "demo|İY 0.5 ÜST": {
                    "slug": "demo", "market": "İY 0.5 ÜST",
                    "message_id": 123, "message_text": "Örnek sinyal", "result": None,
                }
            }}
            updated = process_result(
                {"status_text": "Half time", "home_score": 1, "away_score": 0},
                "demo", state
            )
            assert updated == 1
            assert state["signals"]["demo|İY 0.5 ÜST"]["result"] == "KAZANDI"
            assert mocked_calls[0][0] == "editMessageText"
            assert "İLK YARI SONUCU: 1-0" in mocked_calls[0][1]["text"]
        finally:
            telegram_call = original_telegram_call
        print("TELEGRAM_EDIT_TEST | PASS | ilk yarı sonucu eski Telegram mesajına ekleniyor")
        return 0

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_id or not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID GitHub Secret eksik")

    bot = telegram_call("getMe", {})
    chat = telegram_call("getChat", {"chat_id": chat_id})
    print(f"TELEGRAM_READY | bot=@{bot.get('username', 'unknown')} | hedef={chat.get('title') or chat.get('username') or chat.get('first_name') or 'sohbet'}")
    print("GOL RADAR SPORTScore ANALİZ | TELEGRAM SİNYALİ + İLK YARI SONUÇ DÜZENLEME")
    state = load_state()
    rows = live_fixtures()
    print(f"SPORTSCORE_LIVE | maç={len(rows)}")
    if not rows:
        print("CANLI_MAÇ_YOK")
        return 0

    by_slug = {fixture_slug(row): row for row in rows if fixture_slug(row)}
    pending_fixtures = {}
    for record in state.get("signals", {}).values():
        if isinstance(record, dict) and not record.get("result"):
            slug = record.get("slug")
            if slug and slug not in pending_fixtures:
                pending_fixtures[slug] = by_slug.get(slug) or {
                    "url": record.get("url", ""),
                    "home": record.get("home", "?"),
                    "away": record.get("away", "?"),
                }
    pending_slugs = list(pending_fixtures)
    selected = [pending_fixtures[slug] for slug in pending_slugs[:MAX_DETAILS_PER_RUN]]
    selected_slugs = {fixture_slug(row) for row in selected if fixture_slug(row)}
    remaining_slots = max(0, MAX_DETAILS_PER_RUN - len(selected))
    new_rows = [row for row in rows if fixture_slug(row) and fixture_slug(row) not in selected_slugs]
    # This engine only evaluates first-half markets. Spend detail requests on
    # explicit first-half fixtures first; retain other fixtures as a fallback
    # only when the feed has no first-half candidates.
    first_half_rows = [
        row for row in new_rows
        if any(marker in str(row.get("status_text") or "").casefold()
               for marker in ("1st half", "first half"))
    ]
    scan_rows = first_half_rows or new_rows
    if scan_rows and remaining_slots:
        slot = int(time.time() // 300)
        start = (slot * remaining_slots) % len(scan_rows)
        selected.extend(scan_rows[(start + i) % len(scan_rows)] for i in range(min(remaining_slots, len(scan_rows))))

    checked = 0
    sent = 0
    edited = 0
    candidates = 0
    for fixture in selected:
        slug = fixture_slug(fixture)
        try:
            detail = match_detail(fixture)
            checked += 1
            edited += process_result(detail, slug, state)
            home = detail.get("home") or fixture.get("home") or "?"
            away = detail.get("away") or fixture.get("away") or "?"
            minute = detail.get("live_minute", "?")
            score = f"{detail.get('home_score', 0)}-{detail.get('away_score', 0)}"
            print(f"MATCH | {home} - {away} | {minute}' | period={detail.get('status_text', detail.get('status', '?'))} | {score} | stats={stat_totals(detail)}")

            for signal in evaluate(detail):
                candidates += 1
                key = f"{slug}|{signal['market']}"
                if key in state.setdefault("signals", {}):
                    print(f"DUPLICATE_SKIPPED | {key}")
                    continue
                message_text = build_signal_text(signal, detail)
                result = telegram_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": message_text,
                    "disable_web_page_preview": True,
                })
                message_id = result.get("message_id")
                if not message_id:
                    raise RuntimeError("Telegram yanıtında message_id bulunamadı")
                state["signals"][key] = {
                    "slug": slug,
                    "url": str(fixture.get("url") or ""),
                    "market": signal["market"],
                    "home": signal["home"],
                    "away": signal["away"],
                    "minute": signal["minute"],
                    "signal_score": signal["score"],
                    "signal_match_score": signal["current_score"],
                    "message_id": message_id,
                    "message_text": message_text,
                    "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "result": None,
                }
                save_state(state)
                sent += 1
                print(f"TELEGRAM_SENT | {signal['market']} | {home} - {away} | message_id={message_id}")
        except Exception as exc:
            print(f"MATCH_ERROR | {fixture.get('url', '?')} | {type(exc).__name__}: {exc}")

    save_state(state)
    print(f"RUN_SUMMARY | canlı={len(rows)} | detay_kontrol={checked} | aday={candidates} | gönderilen={sent} | sonuç_düzenlenen={edited}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
