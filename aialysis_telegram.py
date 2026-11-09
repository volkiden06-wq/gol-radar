#!/usr/bin/env python3
"""Forward public aiALYSIS predictions to the existing Telegram target.

The Supabase anon key below is the public browser key embedded in aiALYSIS'
own client bundle; it is not a service-role credential. Access remains subject
to the site's public database policies.
"""
import json
import html
import os
import re
import sys
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://fohmlwoewjupceznwmdr.supabase.co"
ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZvaG1sd29ld2p1cGNlem53bWRyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM5NDE4MjYsImV4cCI6MjA4OTUxNzgyNn0.Ye-85hcsrLeORp-ThZCxssrc92WOm4ZI-B3mvTmwju4"
STATE_PATH = Path("aialysis_state.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# aiALYSIS applies its own publication thresholds before saving an analysis.
# Telegram forwards every published system analysis without a second threshold.
UPCOMING_HOURS = int(os.getenv("AIALYSIS_UPCOMING_HOURS", "24"))
MAX_SEND_PER_RUN = int(os.getenv("AIALYSIS_MAX_SEND_PER_RUN", "20"))
MAX_PREMATCH_SEND_PER_RUN = int(os.getenv("AIALYSIS_MAX_PREMATCH_SEND_PER_RUN", "2"))
HEADERS = {"apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
RETRY_POLICY = Retry(
    total=4, connect=4, read=3, status=3,
    backoff_factor=0.6,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True,
)
SESSION.mount("https://", HTTPAdapter(max_retries=RETRY_POLICY))


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
        normalized = str(value or 0).strip().replace("%", "").replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        return min(max(int(float(match.group(0))), 0), 95) if match else 0
    except (ValueError, TypeError):
        return 0


def odds(row):
    try:
        return float(row["oran"]) if row.get("oran") not in (None, "") else None
    except (ValueError, TypeError):
        return None


def passes_site_filter(row):
    # The website publishes only its selected AI analyses after its own
    # confidence/category rules. Do not add Telegram-only filters.
    return bool(str(row.get("kategori") or "").strip())


def latest_per_market(rows):
    # Mirror the website's published AI set: newest analysis per match/category.
    # The website itself applies publication thresholds; Telegram adds no second gate.
    ordered = sorted(rows, key=lambda x: x.get("created_at") or "", reverse=True)
    seen = set()
    result = []
    for row in ordered:
        category = str(row.get("kategori") or "").strip()
        if not category:
            continue
        key = (
            str(row.get("mac_id") or row.get("fixture_id") or ""),
            match_text(category),
            line_text(row.get("cizgi")),
            match_text(side_text(row.get("ust_alt"))),
            match_text(row.get("yontem")),
        )
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


def match_text(value):
    # Normalize Turkish dotted/dotless characters and Unicode accents before
    # comparing market labels returned by different aiALYSIS data paths.
    raw = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(ch for ch in raw if not unicodedata.combining(ch)).strip()


def line_text(value):
    # Treat equivalent numeric lines (for example 1.5 and 1.50) as identical.
    raw = str(value or "").strip().replace(",", ".")
    try:
        return format(Decimal(raw).normalize(), "f")
    except (InvalidOperation, ValueError):
        return raw


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
            "parse_mode": "HTML",
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
        return (data.get("result") or {}).get("message_id")
    raise RuntimeError("Telegram hız sınırı aşıldı; mesaj sonraki taramada yeniden denenecek.")

def telegram_edit(message_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    r = requests.post(url, json={
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=30)
    try:
        data = r.json()
    except ValueError:
        data = {"ok": False, "description": r.text[:250]}
    if not data.get("ok"):
        # Telegram can return HTTP 400 for an unchanged message.
        if "message is not modified" in str(data).casefold():
            return True
        raise RuntimeError(f"Telegram mesaj düzenleme hatası: {str(data)[:250]}")
    r.raise_for_status()
    return True



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
    published_at = str(row.get("created_at") or "")
    return f"{source}:{row.get('mac_id', row.get('fixture_id'))}:{market}:{published_at}"


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
        odds_line = f"  |  Oran <b>{o:.2f}</b>" if o is not None else ""
        markets.append(f"🎯 <b>{html.escape(market_text(row))}</b>  |  Güven <b>%{g}</b>{odds_line}")
    heading = "🔴 CANLI MAÇ SİNYALİ" if source == "CANLI" else "🟡 MAÇ ÖNÜ ANALİZİ"
    return (
        f"<b>{heading}</b>\n"
        f"⚽ <b>{html.escape(str(home))} - {html.escape(str(away))}</b>\n"
        + (f"🏆 {html.escape(str(league))}\n" if league else "")
        + f"{time_line}\n"
        + "━━━━━━━━━━━━━━\n"
        + "\n".join(markets)
        + "\n━━━━━━━━━━━━━━\n"
        + "<i>(Verilen tahminler analiz sonucudur. Yatırım tavsiyesi değildir.)</i>"
    )


def collect_live():
    active = get_json("maclar", {
        "select": "fixture_id,durum,elapsed",
        "durum": "in.(1H,HT,2H,LIVE,BT)",
        "limit": 1000,
    })
    # Include every regulation-time live fixture returned by the site.
    # A missing elapsed value must not suppress an otherwise active match;
    # stoppage time is valid, while extra time and penalties are excluded.
    eligible = []
    for match in active:
        status = str(match.get("durum") or "").strip().upper()
        raw_elapsed = match.get("elapsed")
        if status in {"ET", "P", "SUSP", "INT"}:
            continue
        if raw_elapsed in (None, ""):
            eligible.append(match)
            continue
        try:
            minute = int(float(str(raw_elapsed).replace("'", "").strip()))
        except (ValueError, TypeError):
            eligible.append(match)
            continue
        if 0 <= minute <= 120:
            eligible.append(match)
    ids = [int(x["fixture_id"]) for x in eligible if x.get("fixture_id")]
    rows = []
    for i in range(0, len(ids), 150):
        batch = ids[i:i + 150]
        if not batch:
            continue
        rows.extend(get_json("tahmin_sonuclari", {
            "mac_id": "in.(" + ",".join(map(str, batch)) + ")",
            "user_id": "is.null",
            # Do not hard-code a single source label: the site's live page may
            # publish the same system analysis under another source value.
            "select": "mac_id,kategori,cizgi,ust_alt,guc,guc_gosterim,oran,created_at,yontem,sonuc,kaynak,ev_sahibi,deplasman,lig",
            "order": "created_at.desc",
            "limit": 1000,
        }))
    source_counts = {}
    result_counts = {}
    for row in rows:
        source_label = str(row.get("kaynak") or "(boş)").strip().casefold()
        result_label = str(row.get("sonuc") or "(boş)").strip().casefold()
        source_counts[source_label] = source_counts.get(source_label, 0) + 1
        result_counts[result_label] = result_counts.get(result_label, 0) + 1

    # First choose the newest row for each distinct market across ALL statuses.
    # Filtering pending rows first can resurrect an older, already superseded signal.
    latest = latest_per_market(rows)
    pending_labels = {"(boş)", "bekliyor", "pending", "waiting"}
    filtered = [
        row for row in latest
        if str(row.get("sonuc") or "(boş)").strip().casefold() in pending_labels
    ]
    market_counts = {}
    for row in latest:
        label = market_text(row)
        market_counts[label] = market_counts.get(label, 0) + 1
    pending_market_counts = {}
    for row in filtered:
        label = market_text(row)
        pending_market_counts[label] = pending_market_counts.get(label, 0) + 1
    print(
        f"CANLI kaynak kontrolü | aktif maç: {len(active)} | uygun dakika/durum: {len(eligible)} "
        f"| ham sistem tahmini: {len(rows)} | son sürüm analiz: {len(latest)} "
        f"| bekleyen son sürüm: {len(filtered)} | kaynaklar: {source_counts} | sonuçlar: {result_counts}"
    )
    print(f"CANLI piyasa dağılımı | tüm son analizler: {market_counts} | gönderilebilir: {pending_market_counts}")
    return filtered

def collect_prematch():
    now = datetime.now(timezone.utc)
    rows = pages("mac_oncu_analizler", {
        "mac_tarihi": f"gte.{now.isoformat()}",
        "order": "mac_tarihi.asc",
        "select": "fixture_id,mac_tarihi,ev_takim_adi,dep_takim_adi,lig_adi,analiz,created_at",
    }, max_pages=10)
    result = []
    raw_recommendations = 0
    matches_with_recommendations = 0
    for match in rows:
        kickoff = match.get("mac_tarihi")
        try:
            dt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        except Exception:
            continue
        analysis = match.get("analiz") or {}
        if analysis.get("veri_yok"):
            continue
        recommendations = analysis.get("oneriler") or []
        if isinstance(recommendations, list) and recommendations:
            matches_with_recommendations += 1
            raw_recommendations += len(recommendations)
        if not isinstance(recommendations, list):
            continue
        # Forward every recommendation actually published by aiALYSIS.
        prepared = []
        for rec in recommendations:
            if not isinstance(rec, dict) or not rec.get("kategori"):
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
    print(f"MAÇ ÖNÜ kaynak kontrolü | yaklaşan maç: {len(rows)} | öneri bulunan maç: {matches_with_recommendations} | ham öneri: {raw_recommendations} | site filtresinden geçen: {len(result)}")
    return result, rows


def normalize_result(value):
    # aiALYSIS may label finalized markets in Turkish or English.
    raw = str(value or '').strip().casefold()
    s = raw.replace('ı', 'i').replace('İ', 'i').replace('ş', 's').replace('ğ', 'g').replace('ü', 'u').replace('ö', 'o').replace('ç', 'c')
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    win = {
        'kazandi', 'won', 'win', 'basarili', 'dogru', 'tuttu', 'geldi',
        'isabet', 'olumlu', 'success', 'successful'
    }
    lose = {
        'kaybetti', 'lost', 'lose', 'basarisiz', 'yanlis', 'tutmadi',
        'gelmedi', 'isabet degil', 'olumsuz', 'failed'
    }
    if s in win:
        return 'KAZANDI'
    if s in lose:
        return 'KAYBETTİ'
    return None


def settle_sent_predictions(state):
    # Backfill metadata for messages sent before result tracking was added.
    for key, item in state.get('sent', {}).items():
        if not isinstance(item, dict) or item.get('match_id'):
            continue
        try:
            source, match_id, market = key.split(':', 2)
            parts = market.split('|')
            item.update({
                'source': source,
                'match_id': match_id,
                'kategori': parts[0] if len(parts) > 0 else '',
                'cizgi': parts[1] if len(parts) > 1 else '',
                'ust_alt': parts[2] if len(parts) > 2 else '',
                'yontem': parts[3] if len(parts) > 3 else None,
                'settled': False,
            })
        except Exception:
            continue
    pending = [(key, item) for key, item in state.get('sent', {}).items()
               if isinstance(item, dict) and not item.get('settled') and item.get('match_id')]
    ids = sorted({str(item['match_id']) for _, item in pending if str(item['match_id']).isdigit()})
    settled_count = 0
    # A winning market can be confirmed while the fixture is still live.
    # A losing market is only final after the fixture itself has ended.
    fixture_status = {}
    for offset in range(0, len(ids), 100):
        batch_ids = ids[offset:offset + 100]
        if not batch_ids:
            continue
        fixtures = get_json('maclar', {
            'select': 'fixture_id,durum',
            'fixture_id': 'in.(' + ','.join(batch_ids) + ')',
            'limit': 1000,
        })
        for fixture in fixtures:
            fixture_status[str(fixture.get('fixture_id'))] = str(fixture.get('durum') or '').strip().upper()
    final_statuses = {'FT', 'AET', 'PEN', 'FT_PEN'}
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
                k = (str(row.get('mac_id')), match_text(row.get('kategori')),
                     line_text(row.get('cizgi')), match_text(side_text(row.get('ust_alt'))),
                     match_text(row.get('yontem')))
                by_market[k] = result
        for _, item in pending:
            mid = str(item.get('match_id'))
            if mid not in batch:
                continue
            mk = (mid, match_text(item.get('kategori')), line_text(item.get('cizgi')),
                  match_text(side_text(item.get('ust_alt'))), match_text(item.get('yontem')))
            result = by_market.get(mk)
            if not result:
                # If the method label differs or is absent, allow a fallback
                # only when match, market, line, and side all match uniquely.
                # Never infer a result from the opposite side or another line.
                candidates = [
                    v for k, v in by_market.items()
                    if k[0] == mid and k[1] == mk[1] and k[2] == mk[2]
                    and k[3] == mk[3]
                ]
                result = candidates[0] if candidates and len(set(candidates)) == 1 else None
            # Publish both wins and losses only after the fixture is officially finished.
            # This keeps the channel's result labels consistent with the user's request.
            if fixture_status.get(mid) not in final_statuses:
                result = None
            if result:
                item['settled'] = True
                item['result'] = result
                item['settled_at'] = datetime.now(timezone.utc).isoformat()
                settled_count += 1
                save_state(state)
    flush_result_message_edits(state)
    still_pending = sum(1 for item in state.get('sent', {}).values()
                       if isinstance(item, dict) and not item.get('settled') and item.get('match_id'))
    settled_without_message = sum(1 for item in state.get('sent', {}).values()
                                  if isinstance(item, dict) and item.get('settled')
                                  and not item.get('telegram_message_id'))
    print(f"Sonuç kontrolü | yeni sonuçlanan: {settled_count} | sonuç bekleyen: {still_pending} | mesaj kimliği olmayan eski sonuç: {settled_without_message}")
    return settled_count



def flush_result_message_edits(state):
    # Results must stay attached to the original live-signal post.
    # Never publish a separate result message.
    grouped = {}
    for key, item in state.get("sent", {}).items():
        if not isinstance(item, dict) or not item.get("settled") or not item.get("result"):
            continue
        if item.get("result_posted"):
            continue
        message_id = item.get("telegram_message_id")
        if not message_id:
            item["result_edit_error"] = "Orijinal Telegram mesaj kimliği bulunamadı; sonuç mesaja eklenemiyor."
            continue
        grouped.setdefault(str(message_id), []).append(item)

    for message_id, items in grouped.items():
        base_text = next((x.get("telegram_message_text") for x in items if x.get("telegram_message_text")), "")
        if not base_text:
            for x in items:
                x["result_edit_error"] = "Orijinal mesaj metni bulunamadı; sonuç mesaja eklenemiyor."
            save_state(state)
            continue

        related = [
            x for x in state.get("sent", {}).values()
            if isinstance(x, dict) and str(x.get("telegram_message_id")) == message_id
            and x.get("settled") and x.get("result")
        ]
        lines = []
        seen = set()
        for x in related:
            line = f"{'✅' if x.get('result') == 'KAZANDI' else '❌'} {x.get('result')} — {x.get('kategori', 'Analiz')} {x.get('cizgi') or ''} {side_text(x.get('ust_alt'))}".strip()
            if line not in seen:
                seen.add(line)
                lines.append(line)

        if not lines:
            continue
        result_block = "🏁 MAÇ SONUÇLARI\n" + "\n".join(lines)
        try:
            telegram_edit(int(message_id), base_text + "\n\n" + result_block)
            for x in related:
                x["result_posted"] = True
                x.pop("result_edit_error", None)
            save_state(state)
        except Exception as exc:
            # Keep it pending and retry on the next scheduled scan.
            # If the original post was deleted, Telegram cannot append to it;
            # do not violate the user's requirement by sending a new post.
            for x in related:
                x["result_posted"] = False
                x["result_edit_error"] = str(exc)[:250]
            print(f"Sonuç orijinal Telegram mesajına eklenemedi; ayrı mesaj gönderilmeyecek, tekrar denenecek (mesaj {message_id}): {exc}")
            save_state(state)

def send_live_performance_report(state):
    # Every three hours: report the predictions sent in the last interval,
    # plus today's cumulative live-only scorecard.
    now = datetime.now().astimezone()
    last_value = state.get("last_live_report_at")
    if not last_value:
        state["last_live_report_at"] = now.isoformat()
        save_state(state)
        print("Canlı performans raporu zamanlayıcısı başlatıldı; ilk rapor 3 saat sonra gönderilecek.")
        return False
    try:
        last_report = datetime.fromisoformat(str(last_value))
        if last_report.tzinfo is None:
            last_report = last_report.astimezone()
    except (ValueError, TypeError):
        last_report = now

    if now - last_report < timedelta(hours=3):
        return False

    today = now.date()
    live_items = []
    interval_items = []
    for item in state.get("sent", {}).values():
        if not isinstance(item, dict) or item.get("source") != "CANLI":
            continue
        try:
            created_at = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
            local_created = created_at.astimezone()
        except (ValueError, TypeError):
            continue
        if local_created.date() == today:
            live_items.append(item)
        if last_report <= local_created <= now:
            interval_items.append(item)

    def counts(items):
        won = sum(1 for x in items if x.get("settled") and x.get("result") == "KAZANDI")
        lost = sum(1 for x in items if x.get("settled") and x.get("result") == "KAYBETTİ")
        total = len(items)
        pending = max(0, total - won - lost)
        decided = won + lost
        rate = (won / decided * 100) if decided else 0.0
        return total, won, lost, pending, rate

    period = counts(interval_items)
    daily = counts(live_items)
    report = (
        "📊 <b>CANLI PERFORMANS RAPORU</b>\n"
        f"🕒 <b>Son 3 saat</b>  ·  {last_report.astimezone().strftime('%H:%M')}–{now.strftime('%H:%M')}\n"
        "━━━━━━━━━━━━━━\n"
        f"🎯 Gönderilen tahmin: <b>{period[0]}</b>\n"
        f"✅ Kazanan: <b>{period[1]}</b>\n"
        f"❌ Kaybeden: <b>{period[2]}</b>\n"
        f"⏳ Bekleyen: <b>{period[3]}</b>\n"
        f"📈 Başarı oranı: <b>%{period[4]:.1f}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📅 <b>Bugün toplam</b>: {daily[0]} tahmin  |  ✅ {daily[1]}  |  ❌ {daily[2]}  |  ⏳ {daily[3]}\n"
        f"🏆 Günlük başarı: <b>%{daily[4]:.1f}</b>\n"
        "<i>Başarı oranı yalnızca sonucu kesinleşen canlı tahminlerden hesaplanır. Bekleyenler paydaya dahil değildir.</i>"
    )
    telegram_send(report)
    state["last_live_report_at"] = now.isoformat()
    save_state(state)
    print(f"Canlı performans raporu gönderildi | son 3 saat: {period[0]} | kazandı: {period[1]} | kaybetti: {period[2]} | bekleyen: {period[3]} | başarı: %{period[4]:.1f}")
    return True


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID GitHub Secrets eksik.")
    state = load_state()
    settled_count = settle_sent_predictions(state)
    send_live_performance_report(state)

    # LIVE ONLY: never collect, queue, or send pre-match fixtures.
    live = collect_live()
    ids = [x.get("mac_id") for x in live]
    matches = fixture_map(ids)

    grouped = {}
    already_sent = 0
    for row in live:
        key = prediction_key("CANLI", row)
        if key in state["sent"]:
            already_sent += 1
            continue
        match_id = row.get("mac_id") or row.get("fixture_id")
        group_key = ("CANLI", str(match_id))
        if group_key not in grouped:
            match = matches.get(int(match_id)) if match_id and str(match_id).isdigit() else None
            grouped[group_key] = {"match": match, "rows": []}
        grouped[group_key]["rows"].append((key, row))

    print(f"CANLI tekrar kontrolü | daha önce gönderilmiş analiz: {already_sent} | yeni canlı maç grubu: {len(grouped)}")
    live_queue = sorted(
        grouped.items(),
        key=lambda item: str(item[1]["rows"][0][1].get("created_at") or "")
    )
    queue = live_queue[:MAX_SEND_PER_RUN]
    sent_now = 0
    prediction_count = 0
    for (source, match_id), group in queue:
        rows = [x[1] for x in group["rows"]]
        message_text = make_message(source, rows, group["match"])
        telegram_message_id = telegram_send(message_text)
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
                "telegram_message_id": telegram_message_id,
                "telegram_message_text": message_text,
            }
        sent_now += 1
        prediction_count += len(rows)
        save_state(state)
        time.sleep(4.0)

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_counts"] = {
        "live": len(live), "prematch": 0,
        "sent_messages": sent_now, "sent_predictions": prediction_count,
        "queued_matches": len(queue), "settled_predictions": settled_count,
    }
    save_state(state)
    print(f"aiALYSIS CANLI-ONLY | canlı analiz: {len(live)} | gönderilen maç mesajı: {sent_now} | gönderilen analiz: {prediction_count} | kuyruk maç: {len(queue)}")
    if len(live_queue) > MAX_SEND_PER_RUN:
        print(f"Kalan {len(live_queue)-MAX_SEND_PER_RUN} canlı maç sonraki taramalarda gönderilecek.")


if __name__ == "__main__":
    main()
