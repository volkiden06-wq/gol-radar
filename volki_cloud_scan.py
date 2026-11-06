import os, json, requests
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "volki_state.json"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
THRESHOLD = int(os.getenv("ALERT_SCORE_THRESHOLD", "65"))
MAX_STATS = int(os.getenv("MAX_STATS_REQUESTS_PER_RUN", "5"))

if not API_KEY or not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Eksik GitHub Actions Secrets: API_FOOTBALL_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")

HEADERS = {"x-apisports-key": API_KEY}
def api(path, params=None):
    r = requests.get("https://v3.football.api-sports.io/" + path, headers=HEADERS, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data.get("response", [])

def telegram(message):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram mesajı gönderilemedi: " + str(r.text[:300]))

def score(stats, minute):
    totals = {}
    for team in stats:
        for item in team.get("statistics", []):
            name = item.get("type", "").lower().replace(" ", "")
            val = item.get("value")
            if val is None: continue
            if isinstance(val, str): val = val.replace("%","")
            try: val = float(val)
            except (ValueError, TypeError): continue
            totals[name] = totals.get(name, 0) + val
    shots_on = totals.get("shotsongoal", 0)
    shots = totals.get("totalshots", 0)
    corners = totals.get("cornerkicks", 0)
    dangerous = totals.get("shotsinsidebox", 0)
    points = min(35, shots_on * 7) + min(20, shots * 2) + min(18, corners * 2) + min(15, dangerous * 1.5)
    if minute >= 70: points += 5
    if minute <= 20: points -= 8
    return max(0, min(100, round(points))), totals

def main():
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {"sent": {}, "signals": {}, "date": "", "cursor": 0}
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["stats_today"] = 0
    fixtures = api("fixtures", {"live": "all"})
    live = []
    for f in fixtures:
        status = f.get("fixture", {}).get("status", {}).get("short")
        minute = f.get("fixture", {}).get("status", {}).get("elapsed") or 0
        if status in ("1H","2H") and 15 <= minute <= 85:
            live.append(f)
    print(f"API canlı maç sayısı: {len(fixtures)} | Analiz aralığındaki maç: {len(live)}")
    if not live:
        print("Şu anda analiz aralığında maç bulunamadı.")
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    start = int(state.get("cursor", 0)) % len(live)
    ordered = live[start:] + live[:start]
    used = 0
    last_index = start
    for offset, f in enumerate(ordered):
        original_index = (start + offset) % len(live)
        last_index = (original_index + 1) % len(live)
        if used >= MAX_STATS: break
        fid = str(f["fixture"]["id"])
        if fid in state.get("sent", {}):
            continue
        minute = f["fixture"]["status"].get("elapsed") or 0
        stats = api("fixtures/statistics", {"fixture": fid})
        used += 1
        points, totals = score(stats, minute)
        home, away = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
        gh, ga = f["goals"].get("home") or 0, f["goals"].get("away") or 0
        print(f"{home} - {away} | {minute}' | {gh}-{ga} | baskı {points}/100")
        if points >= THRESHOLD:
            msg = (f"⚽ VOLKI FOOTBALL AI\n🔥 GOL BASKISI SİNYALİ\n"
                   f"{home} - {away}\n⏱ {minute}' | Skor: {gh}-{ga}\n"
                   f"📊 Baskı puanı: {points}/100\n"
                   f"Şut: {int(totals.get('totalshots',0))} | İsabetli: {int(totals.get('shotsongoal',0))} | Korner: {int(totals.get('cornerkicks',0))}\n"
                   "Not: İstatistiksel uyarıdır, gol garantisi veya bahis tavsiyesi değildir.")
            telegram(msg)
            state.setdefault("sent", {})[fid] = {"minute": minute, "score": points, "home": home, "away": away, "goals": [gh,ga]}
            state.setdefault("signals", {})[fid] = state["sent"][fid]
    state["cursor"] = last_index
    state["stats_today"] = state.get("stats_today", 0) + used
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"İstatistik isteği: {used} | Bugünkü toplam: {state['stats_today']} | Sonraki tarama başlangıç indeksi: {state['cursor']}")

if __name__ == "__main__":
    main()
