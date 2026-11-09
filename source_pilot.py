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
    base = "https://sportscore.com/api/v1/fixtures/"
    checks = [
        ("live", {"sport": "football", "status": "live", "limit": 100}),
        ("today", {"sport": "football", "limit": 100}),
    ]
    all_ok = True
    for label, params in checks:
        try:
            response = SESSION.get(base, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                matches = payload.get("matches") or payload.get("fixtures") or payload.get("data") or []
            elif isinstance(payload, list):
                matches = payload
            else:
                matches = []
            if not isinstance(matches, list):
                raise ValueError("API yanıtında maç listesi bulunamadı")
            statuses = {}
            for match in matches:
                if not isinstance(match, dict):
                    continue
                status = str(match.get("status") or match.get("status_text") or "unknown").strip()
                statuses[status] = statuses.get(status, 0) + 1
            print(
                f"SPORTSCORE_{label.upper()} | OK"
                f" | HTTP={response.status_code}"
                f" | maç={len(matches)}"
                f" | durumlar={statuses}"
                f" | örnek_alanlar={sorted(matches[0].keys()) if matches and isinstance(matches[0], dict) else []}"
            )
        except Exception as exc:
            all_ok = False
            print(f"SPORTSCORE_{label.upper()} | FAIL | {type(exc).__name__}: {exc}")
    return all_ok

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
