#!/usr/bin/env python3
"""Isolated, no-send connectivity pilot for Forebet + SportScore.

This script intentionally does NOT post to Telegram and does NOT alter the
existing aiALYSIS workflow or its delivery state. It checks whether the two
public sources can be accessed under normal HTTP/robots rules before a signal
adapter is implemented.
"""
import json
import sys
import urllib.robotparser
from urllib.parse import urljoin

import requests

UA = "GolRadarSourcePilot/1.0 (+https://github.com/volkiden06-wq/gol-radar)"
TIMEOUT = 20
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "text/html,application/json"})


def check_sportscore():
    url = "https://sportscore.com/api/widget/matches/"
    try:
        response = SESSION.get(url, params={"sport": "football", "limit": 50}, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if not isinstance(matches, list):
            raise ValueError("API yanıtında matches listesi bulunamadı")
        live = [
            m for m in matches
            if isinstance(m, dict)
            and str(m.get("status") or "").casefold() in {"live", "1h", "2h", "ht", "in_play"}
        ]
        print(
            "SPORTSCORE | OK"
            f" | HTTP={response.status_code}"
            f" | maç={len(matches)}"
            f" | canlı_durumlu={len(live)}"
            f" | örnek_alanlar={sorted(matches[0].keys()) if matches else []}"
        )
        return True
    except Exception as exc:
        print(f"SPORTSCORE | FAIL | {type(exc).__name__}: {exc}")
        return False


def check_forebet():
    robots_url = "https://www.forebet.com/robots.txt"
    live_url = "https://www.forebet.com/en/live-football-tips"
    try:
        robots_response = SESSION.get(robots_url, timeout=TIMEOUT)
        robots_response.raise_for_status()
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(robots_response.text.splitlines())
        allowed = parser.can_fetch(UA, live_url)
        print(f"FOREBET_ROBOTS | HTTP={robots_response.status_code} | live_path_allowed={allowed}")
        if not allowed:
            print("FOREBET | SKIP | robots.txt otomatik erişime izin vermiyor; sayfa taranmayacak.")
            return False
        response = SESSION.get(live_url, timeout=TIMEOUT)
        if response.status_code in (401, 403, 429):
            print(f"FOREBET | BLOCKED | HTTP={response.status_code}; engeli aşma veya proxy denemesi yapılmayacak.")
            return False
        response.raise_for_status()
        html = response.text
        title = ""
        import re
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if match:
            title = re.sub(r"\\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
        print(
            "FOREBET | PAGE_OK"
            f" | HTTP={response.status_code}"
            f" | bytes={len(html.encode('utf-8'))}"
            f" | title={title[:160]}"
            " | signal_parser=NOT_ENABLED"
        )
        return True
    except Exception as exc:
        print(f"FOREBET | FAIL | {type(exc).__name__}: {exc}")
        return False


def main():
    print("GOL-RADAR İKİ KAYNAK PİLOTU | DRY RUN | TELEGRAM GÖNDERİMİ KAPALI")
    sportscore_ok = check_sportscore()
    forebet_ok = check_forebet()
    print(
        "PILOT_SUMMARY"
        f" | sportscore={'OK' if sportscore_ok else 'FAIL'}"
        f" | forebet={'OK' if forebet_ok else 'FAIL'}"
        " | telegram=DISABLED"
        " | legacy_bot=UNCHANGED"
    )
    # A source may legitimately block automated access. Keep the pilot run
    # green so its logs remain available, but do not imply integration success.
    print("SONRAKİ ADIM | İki kaynağın erişim ve veri yapısı doğrulandıktan sonra sinyal ayrıştırıcısı yazılacak.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
