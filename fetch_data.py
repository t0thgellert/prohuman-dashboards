#!/usr/bin/env python3
"""DEBUG verzió — kiírja a nyers API válaszokat hogy lássuk mi jön vissza."""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

AC_API_URL         = os.environ["AC_API_URL"].rstrip("/")
AC_API_KEY         = os.environ["AC_API_KEY"]
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
CAMPAIGN_NAME      = "Oktatás és versenyképesség letöltési link küldés"

HDR_V3 = {"Api-Token": AC_API_KEY}

def v1(action, extra=None):
    time.sleep(0.25)
    p = {"api_key": AC_API_KEY, "api_action": action, "api_output": "json"}
    if extra:
        p.update(extra)
    r = requests.get(f"{AC_API_URL}/admin/api.php", params=p, timeout=30)
    r.raise_for_status()
    return r.json()

def v3_all(path, params=None):
    params = dict(params or {})
    params.setdefault("limit", 100)
    params["offset"] = 0
    results = []
    while True:
        time.sleep(0.25)
        r = requests.get(f"{AC_API_URL}/api/3/{path}",
                         headers=HDR_V3, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = next((v for v in data.values() if isinstance(v, list)), [])
        results.extend(items)
        total = int(data.get("meta", {}).get("total", len(items)))
        params["offset"] += len(items)
        if params["offset"] >= total or not items:
            break
    return results

def find_campaign():
    for c in v3_all("campaigns", {"limit": 200}):
        if c.get("name", "").strip() == CAMPAIGN_NAME:
            return c
    raise ValueError(f"Kampány nem található: '{CAMPAIGN_NAME}'")

def debug_v1(action, extra, label):
    """Meghív egy v1 végpontot és kiírja az első 2 rekordot és a kulcsokat."""
    print(f"\n=== DEBUG: {label} ===")
    print(f"  action={action}, params={extra}")
    data = v1(action, {**extra, "p": 0, "pp": 5})
    print(f"  result_code: {data.get('result_code')}")
    print(f"  result_message: {data.get('result_message')}")
    # Numerikus kulcsok = rekordok
    records = {k: v for k, v in data.items() if k.isdigit()}
    print(f"  Rekordok száma (első lap): {len(records)}")
    if records:
        first = list(records.values())[0]
        print(f"  Első rekord kulcsai: {list(first.keys())}")
        print(f"  Első rekord: {json.dumps(first, ensure_ascii=False)[:300]}")
    else:
        # Ha nincs numerikus kulcs, kiírjuk az összes kulcsot
        print(f"  Összes kulcs a válaszban: {list(data.keys())}")
        print(f"  Teljes válasz: {json.dumps(data, ensure_ascii=False)[:500]}")
    return data

def main():
    print("Kampány keresése...")
    campaign = find_campaign()
    cid = str(campaign["id"])
    print(f"  → ID={cid}, név='{campaign.get('name')}'")
    print(f"  → send_amt={campaign.get('send_amt')}, uniqueopens={campaign.get('uniqueopens')}, uniquelinkclicks={campaign.get('uniquelinkclicks')}, bouncedamt={campaign.get('bouncedamt')}, unsubscribes={campaign.get('unsubscribes')}")

    # Debug minden v1 végpont
    debug_v1("campaign_report_open_list",  {"campaignid": cid, "type": "open"},  "MEGNYITÓK (type=open)")
    debug_v1("campaign_report_open_list",  {"campaignid": cid, "type": "all"},   "ÖSSZES (type=all)")
    debug_v1("campaign_report_link_list",  {"campaignid": cid},                  "KATTINTÓK")
    debug_v1("campaign_report_bounce_list",{"campaignid": cid},                  "BOUNCE-OK")
    debug_v1("campaign_report_unsubscribe_list", {"campaignid": cid},            "LEIRATKOZÓK")

    # Minimális HTML generálás hogy ne bukjon el
    data = {"invited": [], "reg_clickers": [], "unsubs": [],
            "summary": {"sent": 0, "opens": 0, "clicks": 0, "bounces": 0, "unsubs": 0},
            "updated": "debug"}
    print("\nDebug kész — stats.html nem frissül ebben a verzióban.")

if __name__ == "__main__":
    main()
