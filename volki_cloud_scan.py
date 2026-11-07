import os, json, re, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "volki_state.json"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
THRESHOLD = int(os.getenv("ALERT_SCORE_THRESHOLD", "60"))
MAX_STATS = int(os.getenv("MAX_STATS_REQUESTS_PER_RUN", "8"))
MAX_DAILY_CALLS = int(os.getenv("MAX_API_CALLS_PER_DAY", "90"))
API_CALLS_THIS_RUN = 0
PREFERRED_LEAGUES = {2,3,39,40,41,42,43,45,46,48,61,62,71,72,78,79,88,94,98,103,106,119,128,135,136,140,141,144,179,180,197,203,204,207,218,235,253,262,265,271,281,307,318,333,345,357,365,383,399,475,848}
EMPTY_RETRY_HOURS = 3

if not API_KEY or not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Eksik GitHub Actions Secrets: API_FOOTBALL_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

HEADERS = {"x-apisports-key": API_KEY}

def api(path, params=None):
    global API_CALLS_THIS_RUN
    r = requests.get("https://v3.football.api-sports.io/" + path, headers=HEADERS, params=params, timeout=25)
    API_CALLS_THIS_RUN += 1
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    remaining = r.headers.get("x-ratelimit-requests-remaining")
    if remaining is not None:
        print(f"API kalan günlük istek: {remaining}")
    return data.get("response", [])

def telegram_api(method, payload=None):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload or {}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} hatası: {str(data)[:300]}")
    return data["result"]

def telegram_check():
    bot = telegram_api("getMe")
    chat = telegram_api("getChat", {"chat_id": CHAT_ID})
    target = chat.get("title") or chat.get("username") or chat.get("first_name") or "adı görünmeyen sohbet"
    print(f"Telegram bağlantısı doğrulandı: @{bot.get('username', 'bot')} | HEDEF SOHBET: {target} | tür: {chat.get('type', 'bilinmiyor')} | chat_id sonu: {str(chat.get('id', ''))[-4:]}")

def telegram(message):
    telegram_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    })

def normalize_stats(team):
    values = {}
    for item in team.get("statistics", []):
        name = re.sub(r"[^a-z0-9]", "", str(item.get("type", "")).lower())
        value = item.get("value")
        if value is None:
            continue
        if isinstance(value, str):
            value = value.replace("%", "").strip()
        try:
            values[name] = float(value)
        except (ValueError, TypeError):
            continue
    return values

def team_pressure(values, minute):
    # Tek takımın hücum baskısını puanlar; iki takımın istatistikleri birbirine eklenmez.
    shots_on = values.get("shotsongoal", 0)
    shots = values.get("totalshots", 0)
    corners = values.get("cornerkicks", 0)
    inside_box = values.get("shotsinsidebox", 0)
    saves = values.get("goalkeepersaves", 0)

    points = (
        min(40, shots_on * 10) +
        min(15, shots * 1.5) +
        min(16, inside_box * 2) +
        min(12, corners * 2) +
        min(8, saves * 2)
    )
    if minute >= 70:
        points += 4
    return max(0, min(100, round(points)))

def score(stats, minute):
    if not stats:
        return 0, {}, "İstatistik verisi yok"
    candidates = []
    for team in stats:
        values = normalize_stats(team)
        name = team.get("team", {}).get("name", "Takım")
        candidates.append((team_pressure(values, minute), name, values))
    if not candidates:
        return 0, {}, "İstatistik verisi yok"
    points, name, values = max(candidates, key=lambda item: item[0])
    return points, values, name

def main():
    global API_CALLS_THIS_RUN
    print(f"AYARLAR | Güçlü sinyal eşiği: {THRESHOLD}/100 | Tarama başına en fazla maç: {MAX_STATS} | Günlük API bütçesi: {MAX_DAILY_CALLS}")
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {
        "sent": {}, "signals": {}, "date": "", "cursor": 0
    }
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["stats_today"] = 0
        state["api_calls_today"] = 0
        state["no_stats_until"] = {}

    try:
        telegram_check()
        if not state.get("telegram_test_sent"):
            telegram("✅ VOLKI FOOTBALL AI\nTelegram bağlantı testi başarılı. Bu sohbet, botun bildirim göndereceği hedeftir.")
            state["telegram_test_sent"] = True
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print("TELEGRAM TEST MESAJI BAŞARIYLA GÖNDERİLDİ.")
    except Exception as exc:
        print(f"TELEGRAM BAĞLANTI/TEST GÖNDERİMİ BAŞARISIZ: {exc}")
        raise

    calls_today = int(state.get("api_calls_today", 0))
    if calls_today >= MAX_DAILY_CALLS:
        print(f"Günlük API bütçesi doldu ({calls_today}/{MAX_DAILY_CALLS}); bu tarama API çağrısı yapmadan durduruldu.")
        return

    fixtures = api("fixtures", {"live": "all"})
    state["api_calls_today"] = calls_today + API_CALLS_THIS_RUN

    # Önceki sinyallerin maç sonu sonuçlarını kontrol et. Her çalışmada en fazla
    # 4 bitmiş maç sorgulanır; sorgular günlük API bütçesine dahildir.
    pending = [
        (fid, rec) for fid, rec in state.get("signals", {}).items()
        if isinstance(rec, dict) and not rec.get("result")
    ]
    checked_results = 0
    for fid, rec in pending:
        if checked_results >= 4:
            break
        if int(state.get("api_calls_today", 0)) >= MAX_DAILY_CALLS:
            print("Günlük API bütçesi sonuç kontrolü sırasında doldu.")
            break
        try:
            details = api("fixtures", {"id": fid})
            state["api_calls_today"] = int(state.get("api_calls_today", 0)) + 1
            checked_results += 1
            if not details:
                continue
            match = details[0]
            status = (match.get("fixture", {}).get("status", {}).get("short") or "").upper()
            if status not in ("FT", "AET", "PEN"):
                continue

            final_goals = match.get("goals") or {}
            final_home = final_goals.get("home")
            final_away = final_goals.get("away")
            if final_home is None or final_away is None:
                continue
            signal_goals = rec.get("goals") or [0, 0]
            signal_home = int(signal_goals[0] or 0)
            signal_away = int(signal_goals[1] or 0)
            pressure_team = rec.get("pressure_team")
            home_name = rec.get("home") or match.get("teams", {}).get("home", {}).get("name", "Ev sahibi")
            away_name = rec.get("away") or match.get("teams", {}).get("away", {}).get("name", "Deplasman")

            if pressure_team and pressure_team == home_name:
                success = int(final_home) > signal_home
                basis = "pressure_team"
            elif pressure_team and pressure_team == away_name:
                success = int(final_away) > signal_away
                basis = "pressure_team"
            else:
                # Eski kayıtlarda baskı yapan takım saklanmamış olabilir.
                success = (int(final_home) + int(final_away)) > (signal_home + signal_away)
                basis = "any_goal"

            label = "BAŞARILI" if success else "BAŞARISIZ"
            if basis == "pressure_team":
                explanation = (
                    f"{pressure_team}, sinyalden sonra gol attı."
                    if success else f"{pressure_team}, sinyalden sonra gol atamadı."
                )
            else:
                explanation = (
                    "Sinyalden sonra maçta en az bir gol oldu (eski kayıtta baskı takımı yok)."
                    if success else "Sinyalden sonra maçta gol olmadı (eski kayıtta baskı takımı yok)."
                )

            result_message = (
                "📊 VOLKI FOOTBALL AI | SİNYAL SONUCU\n"
                f"{home_name} - {away_name}\n"
                f"⏱ Sinyal anı: {rec.get('minute', '?')}' | Skor: {signal_home}-{signal_away}\n"
                f"🏁 Maç sonu: {final_home}-{final_away}\n"
                f"{'✅' if success else '❌'} {label}\n"
                f"🎯 {explanation}\n"
                "Not: Bu değerlendirme, sinyalden sonra gol olup olmadığını ölçer; maç kazananını veya bahis sonucunu ifade etmez."
            )
            telegram(result_message)
            rec["result"] = label
            rec["final_goals"] = [int(final_home), int(final_away)]
            rec["result_basis"] = basis
            rec["result_checked_at"] = datetime.now(timezone.utc).isoformat()
            state.setdefault("signals", {})[fid] = rec
            if fid in state.get("sent", {}):
                state["sent"][fid] = rec
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"SİNYAL SONUCU GÖNDERİLDİ: {home_name} - {away_name} | {label}")
        except Exception as exc:
            print(f"SONUÇ KONTROL HATASI | fixture={fid}: {exc}")

    live = []
    for fixture in fixtures:
        status = fixture.get("fixture", {}).get("status", {}).get("short")
        minute = fixture.get("fixture", {}).get("status", {}).get("elapsed") or 0
        if status in ("1H", "2H") and 15 <= minute <= 85:
            live.append(fixture)

    print(f"API canlı maç sayısı: {len(fixtures)} | Analiz aralığındaki maç: {len(live)}")
    if not live:
        print("Şu anda analiz aralığında maç bulunamadı.")
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    remaining_budget = MAX_DAILY_CALLS - int(state.get("api_calls_today", 0))
    scan_limit = min(MAX_STATS, remaining_budget)
    if scan_limit <= 0:
        print("Günlük API bütçesinde istatistik analizi için istek kalmadı.")
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    preferred = [f for f in live if f.get("league", {}).get("id") in PREFERRED_LEAGUES]
    others = [f for f in live if f.get("league", {}).get("id") not in PREFERRED_LEAGUES]
    live = preferred + others
    print(f"Öncelikli liglerdeki canlı maç: {len(preferred)} | Diğer: {len(others)}")
    start = int(state.get("cursor", 0)) % len(live)
    used = 0
    skipped_missing = 0
    with_stats = 0
    last_index = start
    for offset in range(len(live)):
        if used >= scan_limit:
            break
        original_index = (start + offset) % len(live)
        fixture = live[original_index]
        fid = str(fixture["fixture"]["id"])

        if fid in state.get("sent", {}):
            last_index = (original_index + 1) % len(live)
            continue
        retry_at = state.get("no_stats_until", {}).get(fid)
        if retry_at:
            try:
                if datetime.fromisoformat(retry_at) > datetime.now(timezone.utc):
                    skipped_missing += 1
                    last_index = (original_index + 1) % len(live)
                    continue
            except (ValueError, TypeError):
                pass

        minute = fixture["fixture"]["status"].get("elapsed") or 0
        stats = api("fixtures/statistics", {"fixture": fid})
        used += 1
        state["api_calls_today"] = int(state.get("api_calls_today", 0)) + 1
        points, metrics, pressure_team = score(stats, minute)
        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        goals = fixture.get("goals") or {}
        gh, ga = goals.get("home") or 0, goals.get("away") or 0

        print(f"{home} - {away} | {minute}' | {gh}-{ga} | baskı yapan: {pressure_team} | {points}/100")
        if not stats:
            state.setdefault("no_stats_until", {})[fid] = (
                datetime.now(timezone.utc) + timedelta(hours=EMPTY_RETRY_HOURS)
            ).isoformat()
            print(f"İstatistik alınamadı: fixture={fid}; {EMPTY_RETRY_HOURS} saat yeniden sorgulanmayacak")
        else:
            with_stats += 1
        if stats and points >= THRESHOLD:
            msg = (
                "⚽ VOLKI FOOTBALL AI\n"
                "🔥 GÜÇLÜ GOL BASKISI SİNYALİ\n"
                f"{home} - {away}\n"
                f"⏱ {minute}' | Skor: {gh}-{ga}\n"
                f"🎯 Baskı yapan takım: {pressure_team}\n"
                f"📊 Baskı puanı: {points}/100\n"
                f"Şut: {int(metrics.get('totalshots', 0))} | "
                f"İsabetli şut: {int(metrics.get('shotsongoal', 0))} | "
                f"Korner: {int(metrics.get('cornerkicks', 0))}\n"
                "Not: İstatistiksel uyarıdır; gol garantisi veya bahis tavsiyesi değildir."
            )
            try:
                telegram(msg)
                record = {
                    "minute": minute, "score": points, "pressure_team": pressure_team,
                    "home": home, "away": away, "goals": [gh, ga]
                }
                state.setdefault("sent", {})[fid] = record
                state.setdefault("signals", {})[fid] = record
                print(f"TELEGRAM SİNYALİ BAŞARIYLA GÖNDERİLDİ: {home} - {away}")
            except Exception as exc:
                print(f"TELEGRAM GÖNDERİM HATASI ({home} - {away}): {exc}")
                print("Maç gönderilmiş olarak kaydedilmedi; sonraki taramada yeniden denenebilir.")

        last_index = (original_index + 1) % len(live)

    state["cursor"] = last_index
    now = datetime.now(timezone.utc)
    state["no_stats_until"] = {
        fid: until for fid, until in state.get("no_stats_until", {}).items()
        if isinstance(until, str) and until > now.isoformat()
    }
    state["stats_today"] = int(state.get("stats_today", 0)) + used
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"İstatistik sorgusu: {used} maç | Veri bulunan: {with_stats} | "
        f"Geçici atlanan verisiz maç: {skipped_missing} | Günlük API çağrısı: "
        f"{state['api_calls_today']}/{MAX_DAILY_CALLS} | "
        f"Sonraki tarama indeksi: {state['cursor']}"
    )

if __name__ == "__main__":
    main()
