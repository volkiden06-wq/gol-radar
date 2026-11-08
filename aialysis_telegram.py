#!/usr/bin/env python3
"""Forward public aiALYSIS predictions to the existing Telegram target.

The Supabase anon key below is the public browser key embedded in aiALYSIS'
own client bundle; it is not a service-role credential. Access remains subject
to the site's public database policies.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://fohmlwoewjupceznwmdr.supabase.co"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZvaG1sd29ld2p1Y2V6bndtZHIiLCJyb2xlIjoiYW5vbiIsImlhdCI6MTc3Mzk0MTgyNiwiZXhwIjoyMDg5NTE3ODI2fQ.Ye-85hcsrLeORp-ThZCxssrc92WOm4ZI-B3mvTmwju4"
STATE_PATH = Path("aialysis_state.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CONFIDENCE_MIN = 60
ODDS_MIN = 1.35
UPCOMING_HOURS = int(os.getenv("AIALYSIS_UPCOMING_HOURS", "24"))
MAX_SEND_PER_RUN = int(os.getenv("AIALYSIS_MAX_SEND_PER_RUN", "80"))
HEADERS = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_json(path, params=None):
    url = BASE + "/rest/v1/" + path
    response = SESSION.get(url, params=params, timeout=25)
    if not response.ok:
        raise RuntimeError(f"aiALYSIS API {response.status_code}: {response.text[:250]}")
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"aiALYSIS API beklenmeyen yanıt: {str(data)[:200]}")
    return data


def pages(path, params, page_size=1000, max_pages=20):
    out = []
    for offset in range(0, page_size * max_pages, page_size):
        q = dict(params)
        q["limit"] = page_size
        q["offset"] = offset
        part = get_json(path, q)
        out.extend(part)
        if len(part) < page_size:
            break
    return out


def confidence(row):
    value = row.get("guc_gosterim")
    if value in (None, ""):
        value = row.get("guc", row.get("olasilik"))
    try:
        return min(int(float(value or 0)), 95)
    except (ValueError, TypeError):
        return 0


def odds(row):
    try:
        return float(row["oran"]) if row.get("oran") not in (None, "") else None
    except (ValueError, TypeError):
        return None


def passes_site_filter(row):
    g = confidence(row)
    o = odds(row)
    return g >= CONFIDENCE_MIN and (o is None or o >= ODDS_MIN)


def latest_per_market(rows):
    # Mirrors aiALYSIS' visible client filtering: newest first, one prediction
    # per match + category + line; then its 60% confidence / 1.35 odds gate.
    ordered = sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True)
    seen = set()
    result = []
    for row in ordered:
        category = str(row.get("kategori") or "").strip()
        if not category:
            continue
        key = (str(row.get("mac_id") or row.get("fixture_id") or ""),
               category.casefold(), str(row.get("cizgi") or ""))
        if key in seen:
            continue
        seen.add(key)
        if passes_site_filter(row):
            result.append(row)
    return result


def side_text(value):
    s = str(value or "").upper().replace("Ü", "U")
    if "UST" in s or "OVER" in s:
        return "ÜST"
    if "ALT" in s or "UNDER" in s:
        return "ALT"
    return str(value or "")


def market_text(row):
    category = str(row.get("kategori") or "Tahmin")
    line = row.get("cizgi")
    side = side_text(row.get("ust_alt"))
    if line not in (None, "", 0, "0"):
        return f"{category} {line} {side}".strip()
    return f"{category} {side}".strip()


def safe_time(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d.%m %H:%M")
    except Exception:
        return str(value)


def telegram_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram gönderim hatası: {str(data)[:250]}")


def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"sent": {}, "last_run": None}


def save_state(state):
    # Bound state growth while retaining enough history to prevent repeats.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    sent = state.get("sent", {})
    state["sent"] = {
        k: v for k, v in sent.items()
        if isinstance(v, dict) and str(v.get("created_at") or "") >= cutoff
    }
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fixture_map(ids):
    result = {}
    ids = sorted(set(int(x) for x in ids if str(x).isdigit()))
    for i in range(0, len(ids), 150):
        batch = ids[i:i + 150]
        if not batch:
            continue
        rows = get_json("maclar", {
            "select": "fixture_id,mac_tarihi,durum,elapsed,ev_takim_adi,dep_takim_adi,lig_adi,ulke,ev_gol,dep_gol",
            "fixture_id": "in.(" + ",".join(map(str, batch)) + ")",
            "limit": 1000,
        })
        for row in rows:
            result[int(row["fixture_id"])] = row
    return result


def prediction_key(source, row):
    market = "|".join([
        str(row.get("kategori") or "").casefold(),
        str(row.get("cizgi") or ""),
        side_text(row.get("ust_alt")).casefold(),
        str(row.get("yontem") or ""),
    ])
    return f"{source}:{row.get('mac_id', row.get('fixture_id'))}:{market}:{row.get('created_at') or ''}"


def make_message(source, row, match):
    home = (match or {}).get("ev_takim_adi") or row.get("ev_sahibi") or row.get("ev_takim_adi") or "Ev sahibi"
    away = (match or {}).get("dep_takim_adi") or row.get("deplasman") or row.get("dep_takim_adi") or "Deplasman"
    league = (match or {}).get("lig_adi") or row.get("lig") or ""
    minute = (match or {}).get("elapsed")
    status = str((match or {}).get("durum") or "").upper()
    kickoff = safe_time((match or {}).get("mac_tarihi") or row.get("mac_tarihi"))
    score = ""
    if (match or {}).get("ev_gol") is not None and (match or {}).get("dep_gol") is not None:
        score = f" | Skor {match['ev_gol']}-{match['dep_gol']}"
    if source == "CANLI":
        time_line = f"⏱ {minute or '?'}'"+score
    else:
        time_line = f"🗓 {kickoff or 'Saat bilgisi yok'}"
    g = confidence(row)
    o = odds(row)
    odds_line = f"\n💹 Oran: {o:.2f}" if o is not None else ""
    link_id = row.get("mac_id") or row.get("fixture_id")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{home}-vs-{away}".lower()).strip("-")
    link = f"https://aialysis.live/mac/{slug}-{link_id}" if link_id else "https://aialysis.live/aialysis"
    return (
        f"🤖 aiALYSIS | {source} TAHMİNİ\n"
        f"⚽ {home} - {away}\n"
        + (f"🏆 {league}\n" if league else "")
        + f"{time_line}\n"
        f"🎯 {market_text(row)}\n"
        f"📊 Güven: %{g}{odds_line}\n"
        f"🔎 {link}\n"
        "ℹ️ İstatistiksel tahmindir; gol veya bahis sonucu garantisi değildir."
    )


def collect_live():
    active = get_json("maclar", {
        "select": "fixture_id",
        "durum": "in.(1H,HT,2H,ET,P,LIVE,BT,SUSP,INT)",
        "limit": 1000,
    })
    ids = [int(x["fixture_id"]) for x in active if x.get("fixture_id")]
    rows = []
    for i in range(0, len(ids), 150):
        batch = ids[i:i + 150]
        if not batch:
            continue
        rows.extend(get_json("tahmin_sonuclari", {
            "mac_id": "in.(" + ",".join(map(str, batch)) + ")",
            "user_id": "is.null",
            "kaynak": "eq.canli",
            "sonuc": "eq.BEKLIYOR",
            "select": "mac_id,kategori,cizgi,ust_alt,guc,guc_gosterim,oran,created_at,yontem,sonuc,kaynak,ev_sahibi,deplasman,lig",
            "order": "created_at.desc",
            "limit": 1000,
        }))
    return latest_per_market(rows)


def collect_prematch():
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=UPCOMING_HOURS)
    rows = pages("mac_oncu_analizler", {
        "mac_tarihi": f"gte.{now.isoformat()}",
        "order": "mac_tarihi.asc",
        "select": "fixture_id,mac_tarihi,ev_takim_adi,dep_takim_adi,lig_adi,ulke,analiz,created_at",
    }, max_pages=10)
    result = []
    for match in rows:
        kickoff = match.get("mac_tarihi")
        try:
            dt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt > end:
            continue
        analysis = match.get("analiz") or {}
        if analysis.get("veri_yok"):
            continue
        recommendations = analysis.get("oneriler") or []
        if not isinstance(recommendations, list):
            continue
        # The aiALYSIS pre-match screen displays every recommendation that
        # has a category and a line (or a package selection), subject to its
        # own confidence and odds gate. Keep all qualifying recommendations.
        prepared = []
        for rec in recommendations:
            if not isinstance(rec, dict) or not rec.get("kategori"):
                continue
            if rec.get("cizgi") in (None, "", 0, "0") and not str(rec.get("kategori", "")).lower().startswith(("paket", "kombine")):
                continue
            prepared.append({
                **rec,
                "mac_id": match.get("fixture_id"),
                "fixture_id": match.get("fixture_id"),
                "created_at": match.get("created_at") or kickoff,
                "mac_tarihi": kickoff,
                "ev_takim_adi": match.get("ev_takim_adi"),
                "dep_takim_adi": match.get("dep_takim_adi"),
                "lig": match.get("lig_adi"),
            })
        # Same market priority as the site: prefer piyasa_v1 where present.
        priority = {str(x.get("kategori", "")).casefold() for x in prepared if x.get("yontem") == "piyasa_v1"}
        prepared = [x for x in prepared if x.get("yontem") == "piyasa_v1" or str(x.get("kategori", "")).casefold() not in priority]
        result.extend(x for x in prepared if passes_site_filter(x))
    return result, rows


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID GitHub Secrets eksik.")
    state = load_state()
    live = collect_live()
    prematch, prematch_matches = collect_prematch()
    ids = [x.get("mac_id") for x in live] + [x.get("fixture_id") for x in prematch_matches]
    matches = fixture_map(ids)
    queue = []
    for source, rows in (("CANLI", live), ("MAÇ ÖNÜ", prematch)):
        for row in rows:
            key = prediction_key(source, row)
            if key not in state["sent"]:
                match_id = row.get("mac_id") or row.get("fixture_id")
                match = matches.get(int(match_id)) if match_id and str(match_id).isdigit() else None
                if source == "MAÇ ÖNÜ" and match is None:
                    match = row
                queue.append((key, source, row, match))
    queue.sort(key=lambda x: (0 if x[1] == "CANLI" else 1, str(x[2].get("created_at") or "")))
    sent_now = 0
    for key, source, row, match in queue[:MAX_SEND_PER_RUN]:
        telegram_send(make_message(source, row, match))
        state["sent"][key] = {
            "created_at": row.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "source": source,
        }
        sent_now += 1
        save_state(state)
        time.sleep(0.12)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_counts"] = {"live": len(live), "prematch": len(prematch), "sent": sent_now, "queued": len(queue)}
    save_state(state)
    print(f"aiALYSIS | canlı tahmin: {len(live)} | maç önü tahmin: {len(prematch)} | gönderilen: {sent_now} | kuyruk: {len(queue)}")
    if len(queue) > MAX_SEND_PER_RUN:
        print(f"Kalan {len(queue)-MAX_SEND_PER_RUN} tahmin sonraki 5 dakikalık çalışmada gönderilecek.")


if __name__ == "__main__":
    main()
