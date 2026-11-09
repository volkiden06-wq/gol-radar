#!/usr/bin/env python3
"""SportScore live-stat signal engine pilot. Telegram delivery enabled."""
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
# No artificial 5/10-match cap: scan every live fixture returned by the feed.
MIN_MARKET_SCORE = {"0.5": 45, "1.5": 58, "2.5": 70, "3.5": 80}
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
    payload = fetch_json(BASE + "/api/v1/fixtures/", {"sport": "football", "status": "live", "limit": 500, "src": "gol-radar"})
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


def period_of(match: dict) -> str:
    status = str(match.get("status") or "").strip().casefold()
    status_text = str(match.get("status_text") or "").strip().casefold()
    period = status + " " + status_text
    if any(x in period for x in ("2nd half", "second half", "2nd period", "second period")):
        return "2Y"
    if any(x in period for x in ("1st half", "first half", "1st period", "first period")):
        return "İY"
    if status in {"ht", "half time"} or status_text in {"ht", "half time", "halftime"}:
        return "HT"
    if status in {"ft", "finished", "ended"} or status_text in {"ft", "finished", "ended", "full time"}:
        return "FT"
    return "?"


def evaluate(match: dict) -> list[dict]:
    period = period_of(match)
    minute = as_int(match.get("live_minute"), -1)
    if minute < 0 or period not in {"İY", "2Y"}:
        return []
    home_goals, away_goals = as_int(match.get("home_score")), as_int(match.get("away_score"))
    total_goals = home_goals + away_goals
    if period == "2Y":
        hh, ah = as_int(match.get("home_ht_score"), -1), as_int(match.get("away_ht_score"), -1)
        if hh < 0 or ah < 0:
            return []
        period_goals = total_goals - hh - ah
        markets = [
            ("2Y 0.5 ÜST", 1, 50, 88, "0.5"),
            ("2Y 1.5 ÜST", 2, 50, 85, "1.5"),
            ("2Y 2.5 ÜST", 3, 50, 80, "2.5"),
            ("2Y 3.5 ÜST", 4, 50, 75, "3.5"),
        ]
    else:
        period_goals = total_goals
        markets = [
            ("İY 0.5 ÜST", 1, 8, 43, "0.5"),
            ("İY 1.5 ÜST", 2, 8, 40, "1.5"),
            ("İY 2.5 ÜST", 3, 8, 35, "2.5"),
        ]
    metrics = stat_totals(match)
    sot, shots = metrics["sot"], metrics["shots"]
    dangerous, attacks, corners = metrics["dangerous"], metrics["attacks"], metrics["corners"]
    pressure = min(35, sot * 9) + min(22, shots * 1.6) + min(22, dangerous * 0.28)
    pressure += min(10, attacks * 0.07) + min(7, corners * 1.4)
    pressure = round(min(100, pressure))
    home, away = str(match.get("home") or "Ev sahibi"), str(match.get("away") or "Deplasman")
    candidates = []
    for market, target, min_minute, max_minute, band in markets:
        if not min_minute <= minute <= max_minute or period_goals >= target:
            continue
        score = pressure + min(12, period_goals * 4)
        threshold = MIN_MARKET_SCORE[band] + (4 if target >= 3 and period_goals == 0 else 0)
        if sot >= (1 if target == 1 else 2) and (dangerous >= 15 or shots >= 4) and score >= threshold:
            candidates.append({
                "market": market, "period": period, "target": target,
                "period_goals": period_goals, "score": min(100, score),
                "minute": minute, "current_score": f"{home_goals}-{away_goals}",
                "metrics": metrics, "home": home, "away": away,
            })
    return candidates

def self_test() -> bool:
    strong = {
        "status": "live", "status_text": "1st Half", "live_minute": 27,
        "home_score": 1, "away_score": 0, "home": "Test A", "away": "Test B",
        "stats": [
            {"label": "Shots on Target", "home": 3, "away": 1},
            {"label": "Shots off Target", "home": 5, "away": 4},
            {"label": "Attacks", "home": 52, "away": 43},
            {"label": "Dangerous Attacks", "home": 32, "away": 28},
            {"label": "Corner Kicks", "home": 3, "away": 2},
        ],
    }
    first = evaluate(strong)
    assert any(x["market"] == "İY 1.5 ÜST" for x in first), first
    assert any(x["market"] == "İY 2.5 ÜST" for x in first), first
    second = dict(strong, status_text="2nd Half", live_minute=67,
                  home_score=2, away_score=1, home_ht_score=1, away_ht_score=0)
    second_candidates = evaluate(second)
    assert any(x["market"] == "2Y 0.5 ÜST" for x in second_candidates), second_candidates
    assert any(x["market"] == "2Y 1.5 ÜST" for x in second_candidates), second_candidates
    assert not evaluate(dict(second, home_ht_score=None))
    assert not evaluate(dict(strong, status_text="Half time"))
    assert not evaluate(dict(strong, status_text="", status="live"))
    print("SELF_TEST | PASS | İY 3 market + 2Y 4 market dönem kontrolleri")
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


def settle_market(market: str, home_ht: int, away_ht: int, home_ft: int | None = None, away_ft: int | None = None) -> str:
    if market.startswith("İY "):
        goals = home_ht + away_ht
    else:
        if home_ft is None or away_ft is None:
            raise ValueError("İkinci yarı sonucu için maç sonu skoru gerekli")
        goals = home_ft + away_ft - home_ht - away_ht
    target = int(float(market.split()[1]) + 0.5)
    return "KAZANDI" if goals >= target else "KAYBETTİ"

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
    half_time, finished = is_half_time(match), is_finished(match)
    if not (half_time or finished):
        return 0
    hh, ah = match.get("home_ht_score"), match.get("away_ht_score")
    if half_time:
        if hh is None: hh = match.get("home_score")
        if ah is None: ah = match.get("away_score")
    if hh is None or ah is None:
        return 0
    hh, ah = as_int(hh, -1), as_int(ah, -1)
    if hh < 0 or ah < 0:
        return 0
    hf = as_int(match.get("home_score"), -1) if finished else None
    af = as_int(match.get("away_score"), -1) if finished else None
    changed = 0
    for record in state.setdefault("signals", {}).values():
        if not isinstance(record, dict) or record.get("slug") != slug or record.get("result"):
            continue
        market = str(record.get("market") or "")
        first = market.startswith("İY ")
        if first and not (half_time or finished): continue
        if not first and not finished: continue
        try:
            result = settle_market(market, hh, ah, hf, af)
        except (ValueError, IndexError):
            continue
        if first:
            result_line = f"🏁 İLK YARI SONUCU: {hh}-{ah}"
        else:
            result_line = f"🏁 MAÇ SONU: {hf}-{af} | İKİNCİ YARI GOLÜ: {hf+af-hh-ah}"
        updated_text = str(record.get("message_text") or "") + f"\n\n{result_line}\n{'✅' if result == 'KAZANDI' else '❌'} {market} — {result}"
        try:
            telegram_call("editMessageText", {
                "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
                "message_id": record["message_id"], "text": updated_text,
                "disable_web_page_preview": True,
            })
            record["result"], record["half_time_score"] = result, [hh, ah]
            if finished: record["full_time_score"] = [hf, af]
            record["result_text"] = updated_text
            changed += 1
            print(f"RESULT_EDITED | {market} | {slug} | {result}")
        except Exception as exc:
            print(f"RESULT_EDIT_ERROR | {market} | {slug} | {exc}")
    return changed

def build_signal_text(signal: dict, detail: dict) -> str:
    competition = detail.get("competition") or ""
    label = "İLK YARI" if signal["period"] == "İY" else "İKİNCİ YARI"
    lines = [f"⚽ GOL RADAR | CANLI {label} SİNYALİ", f"{signal['home']} - {signal['away']}"]
    if competition: lines.append(f"🏆 {competition}")
    lines.extend([
        f"⏱ {signal['minute']}' | Anlık skor: {signal['current_score']}",
        f"🎯 {signal['market']} | Bu yarıdaki gol: {signal['period_goals']}",
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
        assert settle_market("İY 2.5 ÜST", 2, 1) == "KAZANDI"
        assert settle_market("İY 2.5 ÜST", 1, 1) == "KAYBETTİ"
        assert settle_market("2Y 0.5 ÜST", 1, 0, 1, 0) == "KAYBETTİ"
        assert settle_market("2Y 1.5 ÜST", 1, 0, 2, 1) == "KAZANDI"
        assert settle_market("2Y 2.5 ÜST", 1, 0, 2, 1) == "KAYBETTİ"
        assert settle_market("2Y 3.5 ÜST", 1, 0, 3, 2) == "KAZANDI"
        print("SETTLEMENT_TEST | PASS | İY/2Y market sonuçları")
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
            assert "İLK YARI SONUCU: 1-0" in mocked_calls[-1][1]["text"]
            state2 = {"signals": {
                "demo|2Y 1.5 ÜST": {
                    "slug": "demo", "market": "2Y 1.5 ÜST",
                    "message_id": 124, "message_text": "İkinci yarı sinyali", "result": None,
                }
            }}
            updated2 = process_result(
                {"status_text": "Finished", "home_score": 2, "away_score": 1,
                 "home_ht_score": 1, "away_ht_score": 0},
                "demo", state2
            )
            assert updated2 == 1
            assert state2["signals"]["demo|2Y 1.5 ÜST"]["result"] == "KAZANDI"
            assert "İKİNCİ YARI GOLÜ: 2" in mocked_calls[-1][1]["text"]
        finally:
            telegram_call = original_telegram_call
        print("TELEGRAM_EDIT_TEST | PASS | İY devre arasında, 2Y maç sonunda sonuçlanıyor")
        return 0

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_id or not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID GitHub Secret eksik")

    bot = telegram_call("getMe", {})
    chat = telegram_call("getChat", {"chat_id": chat_id})
    print(f"TELEGRAM_READY | bot=@{bot.get('username', 'unknown')} | hedef={chat.get('title') or chat.get('username') or chat.get('first_name') or 'sohbet'}")
    print("GOL RADAR | TÜM CANLI MAÇLAR + İLK/İKİNCİ YARI SİNYALLERİ")
    state = load_state()
    rows = live_fixtures()
    print(f"SPORTSCORE_LIVE | maç={len(rows)}")
    if not rows:
        print("CANLI_MAÇ_YOK")
        return 0
    by_slug = {fixture_slug(row): row for row in rows if fixture_slug(row)}
    pending = {}
    for record in state.get("signals", {}).values():
        if isinstance(record, dict) and not record.get("result"):
            slug = record.get("slug")
            if slug and slug not in by_slug:
                pending[slug] = {"url": record.get("url", ""), "home": record.get("home", "?"), "away": record.get("away", "?")}
    # Scan every fixture returned by the live feed; no 5/10 match rotation.
    selected = list(rows) + list(pending.values())

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
                    "period": signal["period"],
                    "target": signal["target"],
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
