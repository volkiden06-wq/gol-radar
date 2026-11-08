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
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZvaG1sd29ld2p1cGNlem53bWRyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDE4MjYsImV4cCI6MjA4OTUxNzgyNn0.Ye-85hcsrLeORp-ThZCxssrc92WOm4ZI-B3mvTmwju4"
STATE_PATH = Path("aialysis_state.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CONFIDENCE_MIN = 70
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
    # Only first-half / full-time goal markets; exclude corners, cards, etc.
    category = str(row.get("kategori") or "").strip().casefold()
    if "gol" not in category or "gol geldi geliyor" in category:
        return False
    g = confidence(row)
    o = odds(row)
    return g >= CONFIDENCE_MIN and (o is None or o >= ODDS_MIN)


def latest_per_market(rows):
    # Mirrors aiALYSIS' visible client filtering: newest first, one prediction
    # per match + category + line; then its 70% confidence / 1.35 odds gate.
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
    for attempt in range(3):
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=30)
        if r.status_code == 429:
            try:
                wait = int((r.json().get("parameters") or {}).get("retry_after", 5))
            except Exception:
                wait = 5
            if attempt < 2:
                time.sleep(min(max(wait + 1, 2), 60))
                continue
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram gönderim hatası: {str(data)[:250]}")
        return
    raise RuntimeError("Telegram hız sınırı aşıldı; mesaj sonraki taramada yeniden denenecek.")


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
    return f"{source}:{row.get('mac_id', row.get('fixture_id'))}:{market}"


def make_message(source, rows, match):
    first = rows[0]
    home = (match or {}).get("ev_takim_adi") or first.get("ev_sahibi") or first.get("ev_takim_adi") or "Ev sahibi"
    away = (match or {}).get("dep_takim_adi") or first.get("deplasman") or first.get("dep_takim_adi") or "Deplasman"
    league = (match or {}).get("lig_adi") or first.get("lig") or ""
    minute = (match or {}).get("elapsed")
    kickoff = safe_time((match or {}).get("mac_tarihi") or first.get("mac_tarihi"))
    score = ""
    if (match or {}).get("ev_gol") is not None and (match or {}).get("dep_gol") is not None:
        score = f" | Skor {match['ev_gol']}-{match['dep_gol']}"
    time_line = (f"⏱ {minute or '?'}'"+score) if source == "CANLI" else f"🗓 {kickoff or 'Saat bilgisi yok'}"
    markets = []
    for row in rows:
        g = confidence(row)
        o = odds(row)
        odds_line = f" | Oran {o:.2f}" if o is not None else ""
        markets.append(f"• {market_text(row)} | Güven %{g}{odds_line}")
    return (
        f"⚽ {home} - {away}\n"
        + (f"🏆 {league}\n" if league else "")
        + f"{time_line}\n"
        + "\n".join(markets)
        + "\nℹ️ Tahminler analiz üzerine verilmektedir."
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
        "select": "fixture_id,mac_tarihi,ev_takim_adi,dep_takim_adi,lig_adi,analiz,created_at",
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


def normalize_result(value):
    s = str(value or '').strip().casefold()
    if s in {'kazandi', 'kazandı', 'won', 'win', 'basarili', 'başarılı', 'dogru', 'doğru'}:
        return 'KAZANDI'
    if s in {'kaybetti', 'lost', 'lose', 'basarisiz', 'başarısız', 'yanlis', 'yanlış'}:
        return 'KAYBETTİ'
    return None


def settle_sent_predictions(state):
    pending = [(key, item) for key, item in state.get('sent', {}).items()
               if isinstance(item, dict) and not item.get('settled') and item.get('match_id')]
    ids = sorted({str(item['match_id']) for _, item in pending if str(item['match_id']).isdigit()})
    settled_count = 0
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        if not batch:
            continue
        rows = get_json('tahmin_sonuclari', {
            'mac_id': 'in.(' + ','.join(batch) + ')',
            'user_id': 'is.null',
            'select': 'mac_id,kategori,cizgi,ust_alt,yontem,sonuc',
            'limit': 1000,
        })
        by_market = {}
        for row in rows:
            result = normalize_result(row.get('sonuc'))
            if result:
                k = (str(row.get('mac_id')), str(row.get('kategori') or '').casefold(),
                     str(row.get('cizgi') or ''), side_text(row.get('ust_alt')).casefold(),
                     str(row.get('yontem') or ''))
                by_market[k] = result
        for _, item in pending:
            mid = str(item.get('match_id'))
            if mid not in batch:
                continue
            mk = (mid, str(item.get('kategori') or '').casefold(), str(item.get('cizgi') or ''),
                  side_text(item.get('ust_alt')).casefold(), str(item.get('yontem') or ''))
            result = by_market.get(mk)
            if not result:
                candidates = [v for k, v in by_market.items() if k[0] == mid and k[1] == mk[1] and k[2] == mk[2]]
                result = candidates[0] if candidates and len(set(candidates)) == 1 else None
            if result:
                item['settled'] = True
                item['result'] = result
                item['settled_at'] = datetime.now(timezone.utc).isoformat()
                telegram_send(f"{'✅' if result == 'KAZANDI' else '❌'} {result}\\n⚽ {item.get('home', 'Maç')}\\n🎯 {item.get('kategori', 'Gol')} {item.get('cizgi') or ''} {side_text(item.get('ust_alt'))}")
                settled_count += 1
                save_state(state)
                time.sleep(4.0)
    return settled_count

def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID GitHub Secrets eksik.")
    state = load_state()
    settled_count = settle_sent_predictions(state)
    live = collect_live()
    prematch, prematch_matches = collect_prematch()
    ids = [x.get("mac_id") for x in live] + [x.get("fixture_id") for x in prematch_matches]
    matches = fixture_map(ids)

    grouped = {}
    for source, rows in (("CANLI", live), ("MAÇ ÖNÜ", prematch)):
        for row in rows:
            key = prediction_key(source, row)
            if key in state["sent"]:
                continue
            match_id = row.get("mac_id") or row.get("fixture_id")
            group_key = (source, str(match_id))
            if group_key not in grouped:
                match = matches.get(int(match_id)) if match_id and str(match_id).isdigit() else None
                if source == "MAÇ ÖNÜ" and match is None:
                    match = row
                grouped[group_key] = {"match": match, "rows": []}
            grouped[group_key]["rows"].append((key, row))

    queue = sorted(
        grouped.items(),
        key=lambda item: (0 if item[0][0] == "CANLI" else 1,
                          str(item[1]["rows"][0][1].get("created_at") or ""))
    )
    sent_now = 0
    prediction_count = 0
    for (source, match_id), group in queue[:MAX_SEND_PER_RUN]:
        rows = [x[1] for x in group["rows"]]
        telegram_send(make_message(source, rows, group["match"]))
        stamp = datetime.now(timezone.utc).isoformat()
        for key, row in group["rows"]:
            state["sent"][key] = {
                "created_at": row.get("created_at") or stamp,
                "source": source,
                "match_id": str(match_id),
                "home": f"{(group['match'] or {}).get('ev_takim_adi') or row.get('ev_takim_adi') or row.get('ev_sahibi') or 'Ev sahibi'} - {(group['match'] or {}).get('dep_takim_adi') or row.get('dep_takim_adi') or row.get('deplasman') or 'Deplasman'}",
                "kategori": row.get("kategori"),
                "cizgi": row.get("cizgi"),
                "ust_alt": row.get("ust_alt"),
                "yontem": row.get("yontem"),
                "settled": False,
            }
        sent_now += 1
        prediction_count += len(rows)
        save_state(state)
        # Stay well below Telegram's per-chat/channel message rate.
        time.sleep(4.0)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_counts"] = {
        "live": len(live), "prematch": len(prematch),
        "sent_messages": sent_now, "sent_predictions": prediction_count,
        "queued_matches": len(queue), "settled_predictions": settled_count,
    }
    save_state(state)
    print(f"aiALYSIS | canlı tahmin: {len(live)} | maç önü tahmin: {len(prematch)} | gönderilen maç mesajı: {sent_now} | gönderilen tahmin: {prediction_count} | kuyruk maç: {len(queue)}")
    if len(queue) > MAX_SEND_PER_RUN:
        print(f"Kalan {len(queue)-MAX_SEND_PER_RUN} maç sonraki 5 dakikalık çalışmalarda gönderilecek.")


if __name__ == "__main__":
    main()
