#!/usr/bin/env python3
"""
ActiveCampaign kampánystatisztika — végleges verzió a debug log alapján.

Amit tudunk az API-ból:
- campaign_report_open_list (type=open): csak megnyitók, email+subscriberid
- campaign_report_link_list: 1 link rekord, info[] tömbben a kattintók email+cég
- campaign_report_bounce_list: bounce-ok, type=soft/hard
- campaign_report_unsubscribe_list: TILTVA — v3-ból kell
- bouncedamt: None a kampány rekordban — v1 bounce listából számoljuk
- Összes küldött: v3 contacts + subscriberid alapján keressük
"""

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

def v1_all(action, extra=None):
    results = []
    page = 0
    while True:
        p = dict(extra or {})
        p["p"] = page
        p["pp"] = 100
        data = v1(action, p)
        batch = [v for k, v in data.items() if k.isdigit() and isinstance(v, dict)]
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results

def v3_all(path, params=None):
    params = dict(params or {})
    params.setdefault("limit", 100)
    params["offset"] = 0
    results = []
    # Derive expected key from path (e.g. "contacts" -> "contacts")
    expected_key = path.split("/")[0]
    while True:
        time.sleep(0.25)
        r = requests.get(f"{AC_API_URL}/api/3/{path}",
                         headers=HDR_V3, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        # Try expected key first, then any list value
        if expected_key in data and isinstance(data[expected_key], list):
            items = data[expected_key]
        else:
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

def get_contact_by_id(subscriber_id):
    """V3 API: kontakt részletek subscriberid alapján."""
    time.sleep(0.25)
    r = requests.get(f"{AC_API_URL}/api/3/contacts/{subscriber_id}",
                     headers=HDR_V3, timeout=15)
    if r.status_code == 200:
        c = r.json().get("contact", {})
        return {
            "name":    f"{c.get('firstName','')} {c.get('lastName','')}".strip(),
            "company": c.get("orgname", "") or "",
        }
    return {"name": "", "company": ""}

def get_all_data(campaign):
    cid = str(campaign["id"])

    # Összesítők — bouncedamt None, ezért azt a bounce listából számoljuk
    summary = {
        "sent":    int(campaign.get("send_amt") or 0),
        "opens":   int(campaign.get("uniqueopens") or 0),
        "clicks":  int(campaign.get("uniquelinkclicks") or 0),
        "bounces": 0,  # alább töltjük ki
        "unsubs":  int(campaign.get("unsubscribes") or 0),
    }

    # 1. Megnyitók (v1) — email + subscriberid
    print("  Megnyitók (v1)...")
    opens_raw = v1_all("campaign_report_open_list",
                       {"campaignid": cid, "type": "open"})
    print(f"  → {len(opens_raw)} megnyitó")
    # {subscriberid: email}
    opened = {r["subscriberid"]: r["email"].lower()
              for r in opens_raw if r.get("subscriberid")}

    # 2. Kattintók (v1) — 1 link rekord, info[] tömbben az emailek és cégek
    print("  Kattintók (v1)...")
    links_raw = v1_all("campaign_report_link_list", {"campaignid": cid})
    print(f"  → {len(links_raw)} link")
    # A kattintókat az info[] tömbből szedjük ki
    clicked = {}  # email_lower -> {email, name, company}
    for link in links_raw:
        info_list = link.get("info", [])
        if isinstance(info_list, list):
            for info in info_list:
                email = info.get("email", "")
                if email:
                    clicked[email.lower()] = {
                        "email":   email,
                        "name":    "",  # alább töltjük v3-ból
                        "company": info.get("customer_acct_name", "") or "",
                        "subid":   info.get("subscriberid", ""),
                    }
        elif isinstance(info_list, dict):
            # Ha dict formátumban jön
            for _, info in info_list.items():
                if isinstance(info, dict):
                    email = info.get("email", "")
                    if email:
                        clicked[email.lower()] = {
                            "email":   email,
                            "name":    "",
                            "company": info.get("customer_acct_name", "") or "",
                            "subid":   info.get("subscriberid", ""),
                        }
    print(f"  → {len(clicked)} egyedi kattintó")

    # 3. Bounce-ok (v1)
    print("  Bounce-ok (v1)...")
    bounces_raw = v1_all("campaign_report_bounce_list", {"campaignid": cid})
    print(f"  → {len(bounces_raw)} bounce")
    bounce_map = {}
    for b in bounces_raw:
        email = b.get("email", "").lower()
        if email:
            btype = str(b.get("type", "")).lower()
            bounce_map[email] = "Hard Bounce" if btype == "hard" else "Soft Bounce"
    summary["bounces"] = len(bounce_map)

    # 4. Leiratkozók — subscriberid alapján ellenőrizzük a megnyitók kontaktListáit
    # A kampány rekordban nincs list/lists mező, ezért per-kontakt ellenőrzés
    print("  Leiratkozók keresése (v3 contactLists alapján)...")
    unsub_emails = set()
    list_ids = []
    # Megnyitók subscriberid-jein ellenőrizzük ki iratkozott le
    for subid in list(opened.keys()):
        try:
            time.sleep(0.25)
            r = requests.get(f"{AC_API_URL}/api/3/contacts/{subid}/contactLists",
                             headers=HDR_V3, timeout=15)
            if r.status_code == 200:
                cls = r.json().get("contactLists", [])
                for cl in cls:
                    if str(cl.get("status")) == "2":  # 2 = unsubscribed
                        # Ez a kontakt leiratkozott — megkeressük az emailt
                        email = opened.get(subid, "")
                        if email:
                            unsub_emails.add(email)
                        if cl.get("list") and cl["list"] not in list_ids:
                            list_ids.append(str(cl["list"]))
        except Exception:
            pass
    print(f"  → {len(unsub_emails)} leiratkozott, lista ID-k: {list_ids}")

    # Ha még mindig nincs list_id, próbáljuk a v1 campaign_report_list végponttal
    if not list_ids:
        try:
            data = v1("campaign_report_list", {"campaignid": cid})
            print(f"  → campaign_report_list válasz kulcsai: {list(data.keys())[:10]}")
            for k, v in data.items():
                if k.isdigit() and isinstance(v, dict):
                    lid = str(v.get("listid", "") or v.get("list", ""))
                    if lid and lid not in list_ids:
                        list_ids.append(lid)
        except Exception as e:
            print(f"  campaign_report_list hiba: {e}")
    print(f"  → Végső lista ID-k: {list_ids}")

    # 5. Összes küldött — v3 contacts listid alapján (status paraméter nélkül = összes)
    print("  Összes küldött kontakt (v3, listid, status nélkül)...")
    all_list_contacts = {}
    for lid in list_ids:
        try:
            batch = v3_all("contacts", {"listid": lid, "limit": 100})
            for c in batch:
                subid = str(c.get("id", ""))
                if subid:
                    all_list_contacts[subid] = c
            print(f"  → Lista {lid}: {len(batch)} kontakt")
        except Exception as e:
            print(f"  → Lista {lid} hiba: {e}")
    print(f"  → Összesen: {len(all_list_contacts)} kontakt")

    print("  Kontakt részletek lekérése (v3)...")
    all_subids = set(opened.keys())
    bounce_subids = {b.get("subscriberid", ""): b for b in bounces_raw if b.get("subscriberid")}
    all_subids.update(bounce_subids.keys())
    # Lista kontaktok hozzáadása (akik nem nyitottak meg és nem pattantak vissza)
    all_subids.update(all_list_contacts.keys())

    # Kontakt adatok lekérése subscriberid alapján
    contact_details = {}  # subid -> {name, company, email}
    total = len(all_subids)
    for i, subid in enumerate(all_subids):
        if i % 10 == 0:
            print(f"    {i}/{total}...")
        det = get_contact_by_id(subid)
        contact_details[subid] = det

    # Kattintók nevei — subid alapján
    for email_l, info in clicked.items():
        subid = info.get("subid", "")
        if subid and subid in contact_details:
            clicked[email_l]["name"] = contact_details[subid]["name"]
            if not clicked[email_l]["company"]:
                clicked[email_l]["company"] = contact_details[subid]["company"]

    # 6. Összerakás
    contacts = []
    unsubs   = []
    seen_emails = set()

    # Megnyitók
    for subid, email_l in opened.items():
        if email_l in seen_emails:
            continue
        seen_emails.add(email_l)
        det = contact_details.get(subid, {})
        is_unsub = email_l in unsub_emails
        record = {
            "name":    det.get("name", ""),
            "email":   email_l,
            "company": det.get("company", ""),
            "status":  bounce_map.get(email_l, "Delivered"),
            "detail":  "",
            "opened":  True,
            "clicked": email_l in clicked,
            "unsub":   is_unsub,
        }
        contacts.append(record)
        if is_unsub:
            unsubs.append({"name": det.get("name",""), "email": email_l, "company": det.get("company","")})

    # Bounce-ok akik nem megnyitók
    for subid, b in bounce_subids.items():
        email_l = b.get("email", "").lower()
        if email_l in seen_emails:
            continue
        seen_emails.add(email_l)
        det = contact_details.get(subid, {})
        btype = str(b.get("type", "")).lower()
        record = {
            "name":    det.get("name", ""),
            "email":   email_l,
            "company": det.get("company", ""),
            "status":  "Hard Bounce" if btype == "hard" else "Soft Bounce",
            "detail":  b.get("reason", "")[:80] if b.get("reason") else "",
            "opened":  False,
            "clicked": False,
            "unsub":   False,
        }
        contacts.append(record)

    # Lista kontaktok akik nem megnyitók, nem bounce-ok, nem kattintók
    for subid, c in all_list_contacts.items():
        email_l = c.get("email", "").lower()
        if email_l in seen_emails:
            continue
        seen_emails.add(email_l)
        det = contact_details.get(subid, {})
        is_unsub = email_l in unsub_emails
        name = det.get("name") or f"{c.get('firstName','')} {c.get('lastName','')}".strip()
        company = det.get("company") or c.get("orgname", "") or ""
        record = {
            "name":    name,
            "email":   email_l,
            "company": company,
            "status":  "Delivered",
            "detail":  "",
            "opened":  False,
            "clicked": email_l in clicked,
            "unsub":   is_unsub,
        }
        contacts.append(record)
        if is_unsub:
            unsub_emails.add(email_l)  # biztos benne legyen

    # Kattintók akik nem szerepelnek még (nem nyitók sem bounce)
    for email_l, info in clicked.items():
        if email_l in seen_emails:
            continue
        seen_emails.add(email_l)
        is_unsub = email_l in unsub_emails
        record = {
            "name":    info.get("name", ""),
            "email":   info.get("email", email_l),
            "company": info.get("company", ""),
            "status":  "Delivered",
            "detail":  "",
            "opened":  False,
            "clicked": True,
            "unsub":   is_unsub,
        }
        contacts.append(record)

    clickers = [
        {"name": c["name"], "email": c["email"], "company": c["company"]}
        for c in contacts if c["clicked"]
    ]
    unsubs = [
        {"name": c["name"], "email": c["email"], "company": c["company"]}
        for c in contacts if c["unsub"]
    ]

    print(f"  Végeredmény: {len(contacts)} kontakt, {len(unsubs)} leiratkozó, {len(clickers)} kattintó")
    print(f"  Összesítők: {summary}")
    return contacts, unsubs, clickers, summary


def build_html(data, password):
    data_json   = json.dumps(data, ensure_ascii=False)
    pwd_escaped = password.replace("\\", "\\\\").replace('"', '\\"')
    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Statisztika — Oktatás 2035</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,300;0,400;0,500;1,400&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f6f5f1;--surface:#fff;--surface2:#f0eeea;--border:#e2dfd9;--border2:#c8c4bc;
  --text:#18170f;--text2:#6b6860;--text3:#a8a49e;
  --red:#b83232;--red-bg:#fdf1f0;--red-bd:#f3c4c1;
  --amber:#a05c10;--amber-bg:#fdf6ec;--amber-bd:#f0d9b0;
  --green:#1a6e42;--green-bg:#f0faf4;--green-bd:#b3dfc8;
  --r:10px;--rs:6px;
}}
body{{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.6;min-height:100vh}}
.page{{max-width:980px;margin:0 auto;padding:36px 24px 80px}}
#login-screen{{display:flex;align-items:center;justify-content:center;min-height:100vh;background:var(--bg)}}
.login-box{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:36px 40px;width:100%;max-width:360px}}
.login-eyebrow{{font-size:10px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);margin-bottom:4px}}
.login-title{{font-size:18px;font-weight:500;margin-bottom:24px;letter-spacing:-.2px}}
.login-label{{font-size:11px;font-weight:500;color:var(--text2);display:block;margin-bottom:6px}}
.login-input{{width:100%;font-family:'DM Mono',monospace;font-size:13px;border:1px solid var(--border2);border-radius:var(--rs);padding:8px 12px;background:var(--bg);color:var(--text);outline:none;letter-spacing:.05em}}
.login-input:focus{{border-color:var(--text);background:var(--surface)}}
.login-btn{{margin-top:14px;width:100%;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;padding:9px 0;border:1px solid var(--text);border-radius:var(--rs);background:var(--text);color:#fff;cursor:pointer;transition:opacity .1s}}
.login-btn:hover{{opacity:.85}}
.login-err{{margin-top:10px;font-size:11px;color:var(--red);min-height:16px;text-align:center}}
.ph{{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:28px;flex-wrap:wrap;gap:12px}}
.ph-eyebrow{{font-size:10px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);margin-bottom:3px}}
.ph-title{{font-size:21px;font-weight:500;letter-spacing:-.2px}}
.upd{{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3);background:var(--surface);border:1px solid var(--border);border-radius:99px;padding:4px 13px}}
.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:24px}}
@media(max-width:780px){{.metrics{{grid-template-columns:repeat(3,1fr)}}}} @media(max-width:480px){{.metrics{{grid-template-columns:repeat(2,1fr)}}}}
.metric{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px 14px;transition:border-color .12s}}
.metric:hover{{border-color:var(--border2)}}
.ml{{font-size:10px;font-weight:500;letter-spacing:.07em;text-transform:uppercase;color:var(--text3);margin-bottom:7px;line-height:1.4;height:2.8em;display:flex;align-items:flex-start}}
.mv{{font-size:34px;font-weight:300;line-height:1;font-family:'DM Mono',monospace;margin-bottom:3px}}
.ms{{font-size:11px;color:var(--text3);min-height:16px}}
.metric.red .mv{{color:var(--red)}}
.pbar{{height:2px;background:var(--surface2);border-radius:99px;margin-top:10px;overflow:hidden}}
.pf{{height:100%;border-radius:99px;background:var(--green);transition:width .5s ease}}
.pf.red{{background:var(--red)}}
.tabs{{display:flex;gap:2px;margin-bottom:16px;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r);padding:3px}}
.tab{{flex:1;text-align:center;padding:8px 12px;font-size:12px;font-weight:500;color:var(--text2);border-radius:8px;cursor:pointer;transition:background .12s,color .12s;border:none;background:none}}
.tab.active{{background:var(--surface);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.tab-count{{font-family:'DM Mono',monospace;font-size:10px;margin-left:5px;opacity:.7}}
.panel{{display:none}}
.panel.active{{display:block}}
.tbl-wrap{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}}
.tbl-search{{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center}}
.tbl-search input[type=text]{{flex:1;font-family:'DM Sans',sans-serif;font-size:12px;border:1px solid var(--border2);border-radius:var(--rs);padding:6px 10px;background:var(--bg);color:var(--text);outline:none}}
.tbl-search input:focus{{border-color:var(--text);background:var(--surface)}}
.tbl-count{{font-size:11px;color:var(--text3);white-space:nowrap;font-family:'DM Mono',monospace}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;font-weight:500;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--text3);padding:8px 14px 7px;border-bottom:1px solid var(--border);background:var(--surface2);position:sticky;top:0}}
td{{padding:8px 14px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#faf9f6}}
.mono{{font-family:'DM Mono',monospace;font-size:11px;color:var(--text2)}}
.dim{{color:var(--text2)}}
.badge{{display:inline-block;font-size:10px;font-weight:500;padding:2px 8px;border-radius:99px;border:1px solid;white-space:nowrap}}
.b-hard{{background:var(--red-bg);border-color:var(--red-bd);color:var(--red)}}
.b-soft{{background:var(--amber-bg);border-color:var(--amber-bd);color:var(--amber)}}
.b-ok{{background:var(--green-bg);border-color:var(--green-bd);color:var(--green)}}
.empty{{padding:28px 14px;text-align:center;color:var(--text3);font-size:13px}}
</style>
</head>
<body>
<div id="login-screen">
  <div class="login-box">
    <div class="login-eyebrow">statisztika</div>
    <div class="login-title">Oktatás 2035 vitairat</div>
    <label class="login-label" for="pw-input">Jelszó</label>
    <input class="login-input" type="password" id="pw-input" placeholder="••••••••" onkeydown="if(event.key==='Enter')doLogin()">
    <button class="login-btn" onclick="doLogin()">Belépés</button>
    <div class="login-err" id="pw-err"></div>
  </div>
</div>
<div class="page" id="main-page" style="display:none">
<div class="ph">
  <div>
    <div class="ph-eyebrow">statisztika</div>
    <h1 class="ph-title">Oktatás 2035 vitairat</h1>
  </div>
  <div class="upd" id="upd">Frissítve: —</div>
</div>
<div class="metrics">
  <div class="metric">
    <div class="ml">Kiküldött e-mailek</div>
    <div class="mv" id="m-sent">—</div>
    <div class="ms"></div>
    <div class="pbar" style="opacity:0"></div>
  </div>
  <div class="metric red">
    <div class="ml">Visszapattant e-mailek</div>
    <div class="mv" id="m-bounce">—</div>
    <div class="ms" id="m-bpct">—</div>
    <div class="pbar"><div class="pf red" id="pb-b" style="width:0%"></div></div>
  </div>
  <div class="metric">
    <div class="ml">Megnyitották az e-mailt</div>
    <div class="mv" id="m-opens">—</div>
    <div class="ms" id="m-opct">unique opens</div>
    <div class="pbar"><div class="pf" id="pb-o" style="width:0%;background:var(--amber)"></div></div>
  </div>
  <div class="metric">
    <div class="ml">Leiratkozások</div>
    <div class="mv" id="m-unsubs">—</div>
    <div class="ms"></div>
    <div class="pbar" style="opacity:0"></div>
  </div>
  <div class="metric">
    <div class="ml">Letöltések száma</div>
    <div class="mv" id="m-visits">—</div>
    <div class="ms"></div>
    <div class="pbar" style="opacity:0"></div>
  </div>
</div>
<div class="tabs">
  <button class="tab active" onclick="switchTab('invited')">E-mailt kapott <span class="tab-count" id="tc-inv">—</span></button>
  <button class="tab" onclick="switchTab('bounced')">Visszapattant <span class="tab-count" id="tc-bnc">—</span></button>
  <button class="tab" onclick="switchTab('opens')">Megnyitották <span class="tab-count" id="tc-opn">—</span></button>
  <button class="tab" onclick="switchTab('unsubs')">Leiratkoztak <span class="tab-count" id="tc-uns">—</span></button>
  <button class="tab" onclick="switchTab('visitors')">Letöltések száma <span class="tab-count" id="tc-vis">—</span></button>
</div>
<div class="panel active" id="p-invited">
  <div class="tbl-wrap">
    <div class="tbl-search">
      <input type="text" id="s-inv" placeholder="Keresés: név, e-mail, cég…" oninput="renderInvited()">
      <span class="tbl-count" id="cnt-inv">—</span>
    </div>
    <div style="max-height:480px;overflow-y:auto">
      <table><thead><tr><th>#</th><th>Név</th><th>E-mail</th><th>Cég</th><th>Státusz</th><th>Megnyitás</th><th>Kattintás</th></tr></thead>
      <tbody id="tb-inv"></tbody></table>
    </div>
  </div>
</div>
<div class="panel" id="p-bounced">
  <div class="tbl-wrap">
    <div class="tbl-search">
      <input type="text" id="s-bnc" placeholder="Keresés: név, e-mail, cég…" oninput="renderBounced()">
      <span class="tbl-count" id="cnt-bnc">—</span>
    </div>
    <div style="max-height:480px;overflow-y:auto">
      <table><thead><tr><th>#</th><th>Név</th><th>E-mail</th><th>Cég</th><th>Típus</th></tr></thead>
      <tbody id="tb-bnc"></tbody></table>
    </div>
  </div>
</div>
<div class="panel" id="p-opens">
  <div class="tbl-wrap">
    <div class="tbl-search">
      <input type="text" id="s-opn" placeholder="Keresés: név, e-mail, cég…" oninput="renderOpens()">
      <span class="tbl-count" id="cnt-opn">—</span>
    </div>
    <div style="max-height:480px;overflow-y:auto">
      <table><thead><tr><th>#</th><th>Név</th><th>E-mail</th><th>Cég</th></tr></thead>
      <tbody id="tb-opn"></tbody></table>
    </div>
  </div>
</div>
<div class="panel" id="p-unsubs">
  <div class="tbl-wrap">
    <div class="tbl-search">
      <input type="text" id="s-uns" placeholder="Keresés: név, e-mail, cég…" oninput="renderUnsubs()">
      <span class="tbl-count" id="cnt-uns">—</span>
    </div>
    <div style="max-height:480px;overflow-y:auto">
      <table><thead><tr><th>#</th><th>Név</th><th>E-mail</th><th>Cég</th></tr></thead>
      <tbody id="tb-uns"></tbody></table>
    </div>
  </div>
</div>
<div class="panel" id="p-visitors">
  <div class="tbl-wrap">
    <div class="tbl-search">
      <input type="text" id="s-vis" placeholder="Keresés: név, e-mail, cég…" oninput="renderVisitors()">
      <span class="tbl-count" id="cnt-vis">—</span>
    </div>
    <div style="max-height:480px;overflow-y:auto">
      <table><thead><tr><th>#</th><th>Név</th><th>E-mail</th><th>Cég</th></tr></thead>
      <tbody id="tb-vis"></tbody></table>
    </div>
  </div>
</div>
</div>
<script>
const PASSWORD="{pwd_escaped}";
const SESSION_KEY="stats_auth";
function doLogin(){{
  const val=document.getElementById('pw-input').value;
  if(val===PASSWORD){{
    sessionStorage.setItem(SESSION_KEY,"1");
    document.getElementById('login-screen').style.display='none';
    document.getElementById('main-page').style.display='block';
  }}else{{
    document.getElementById('pw-err').textContent='Helytelen jelszó.';
    document.getElementById('pw-input').value='';
    document.getElementById('pw-input').focus();
  }}
}}
(function(){{
  if(sessionStorage.getItem(SESSION_KEY)==="1"){{
    document.getElementById('login-screen').style.display='none';
    document.getElementById('main-page').style.display='block';
  }}
}})();
const DATA={data_json};
let state=JSON.parse(JSON.stringify(DATA));
function renderMetrics(){{
  const s=state.summary||{{}};
  document.getElementById('m-sent').textContent=s.sent||'—';
  document.getElementById('m-bounce').textContent=s.bounces||'—';
  document.getElementById('m-opens').textContent=s.opens||'—';
  document.getElementById('m-unsubs').textContent=s.unsubs||'—';
  document.getElementById('m-visits').textContent=s.clicks||'—';
  document.getElementById('upd').textContent='Frissítve: '+(state.updated||'—');
  const bp=s.sent?((s.bounces/s.sent)*100).toFixed(1):0;
  document.getElementById('m-bpct').textContent=bp+'% visszapattanás';
  document.getElementById('pb-b').style.width=Math.min(bp*4,100)+'%';
  const op=s.sent?((s.opens/s.sent)*100).toFixed(1):0;
  document.getElementById('m-opct').textContent=op+'% open rate';
  document.getElementById('pb-o').style.width=Math.min(op,100)+'%';
  document.getElementById('tc-inv').textContent=state.invited.length;
  document.getElementById('tc-bnc').textContent=state.invited.filter(x=>x.status&&x.status.includes('Bounce')).length;
  document.getElementById('tc-opn').textContent=state.invited.filter(x=>x.opened).length;
  document.getElementById('tc-uns').textContent=state.unsubs.length;
  document.getElementById('tc-vis').textContent=(state.reg_clickers||[]).length;
}}
function switchTab(id){{
  const ids=['invited','bounced','opens','unsubs','visitors'];
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',ids[i]===id));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('p-'+id).classList.add('active');
}}
function q(id){{return(document.getElementById(id).value||'').toLowerCase();}}
function esc(s){{return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function renderInvited(){{
  const sq=q('s-inv');
  const rows=state.invited.filter(x=>!sq||(x.name+x.email+x.company).toLowerCase().includes(sq));
  document.getElementById('cnt-inv').textContent=rows.length+' / '+state.invited.length+' rekord';
  const tb=document.getElementById('tb-inv');
  if(!rows.length){{tb.innerHTML='<tr><td colspan="7" class="empty">Nincs találat.</td></tr>';return;}}
  tb.innerHTML=rows.map((r,i)=>{{
    const sc=r.status==='Delivered'?'b-ok':r.status==='Hard Bounce'?'b-hard':'b-soft';
    const op=r.opened?'<span class="badge b-ok">✓ igen</span>':'<span style="color:var(--text3);font-size:11px">—</span>';
    const cl=r.clicked?'<span class="badge b-ok">✓ igen</span>':'<span style="color:var(--text3);font-size:11px">—</span>';
    return`<tr><td class="mono">${{i+1}}</td><td>${{esc(r.name)}}</td><td class="mono">${{esc(r.email)}}</td><td class="dim">${{esc(r.company)}}</td><td><span class="badge ${{sc}}">${{esc(r.status)}}</span></td><td>${{op}}</td><td>${{cl}}</td></tr>`;
  }}).join('');
}}
function renderBounced(){{
  const sq=q('s-bnc');
  const all=state.invited.filter(x=>x.status&&x.status.includes('Bounce'));
  const rows=all.filter(x=>!sq||(x.name+x.email+x.company).toLowerCase().includes(sq));
  document.getElementById('cnt-bnc').textContent=rows.length+' / '+all.length+' rekord';
  const tb=document.getElementById('tb-bnc');
  if(!rows.length){{tb.innerHTML='<tr><td colspan="5" class="empty">Nincs visszapattant.</td></tr>';return;}}
  tb.innerHTML=rows.map((r,i)=>{{
    const sc=r.status==='Hard Bounce'?'b-hard':'b-soft';
    return`<tr><td class="mono">${{i+1}}</td><td>${{esc(r.name)}}</td><td class="mono">${{esc(r.email)}}</td><td class="dim">${{esc(r.company)}}</td><td><span class="badge ${{sc}}">${{esc(r.status)}}</span></td></tr>`;
  }}).join('');
}}
function renderOpens(){{
  const sq=q('s-opn');
  const all=state.invited.filter(x=>x.opened);
  const rows=all.filter(x=>!sq||(x.name+x.email+x.company).toLowerCase().includes(sq));
  document.getElementById('cnt-opn').textContent=rows.length+' / '+all.length+' rekord';
  const tb=document.getElementById('tb-opn');
  if(!rows.length){{tb.innerHTML='<tr><td colspan="4" class="empty">Nincs megnyitó.</td></tr>';return;}}
  tb.innerHTML=rows.map((r,i)=>
    `<tr><td class="mono">${{i+1}}</td><td>${{esc(r.name)}}</td><td class="mono">${{esc(r.email)}}</td><td class="dim">${{esc(r.company)}}</td></tr>`
  ).join('');
}}
function renderUnsubs(){{
  const sq=q('s-uns');
  const rows=state.unsubs.filter(x=>!sq||(x.name+x.email+x.company).toLowerCase().includes(sq));
  document.getElementById('cnt-uns').textContent=rows.length+' / '+state.unsubs.length+' rekord';
  const tb=document.getElementById('tb-uns');
  if(!rows.length){{tb.innerHTML='<tr><td colspan="4" class="empty">Nincs leiratkozott.</td></tr>';return;}}
  tb.innerHTML=rows.map((r,i)=>
    `<tr><td class="mono">${{i+1}}</td><td>${{esc(r.name)}}</td><td class="mono">${{esc(r.email)}}</td><td class="dim">${{esc(r.company)}}</td></tr>`
  ).join('');
}}
function renderVisitors(){{
  const sq=q('s-vis');
  const all=state.reg_clickers||[];
  const rows=all.filter(x=>!sq||(x.name+x.email+x.company).toLowerCase().includes(sq));
  document.getElementById('cnt-vis').textContent=rows.length+' / '+all.length+' rekord';
  const tb=document.getElementById('tb-vis');
  if(!rows.length){{tb.innerHTML='<tr><td colspan="4" class="empty">Nincs adat.</td></tr>';return;}}
  tb.innerHTML=rows.map((r,i)=>
    `<tr><td class="mono">${{i+1}}</td><td>${{esc(r.name)}}</td><td class="mono">${{esc(r.email)}}</td><td class="dim">${{esc(r.company)}}</td></tr>`
  ).join('');
}}
function renderAll(){{renderMetrics();renderInvited();renderBounced();renderOpens();renderUnsubs();renderVisitors();}}
renderAll();
</script>
</body>
</html>"""

def main():
    print("Kampány keresése...")
    campaign = find_campaign()
    print(f"  → ID={campaign['id']}, név='{campaign.get('name')}'")

    contacts, unsubs, clickers, summary = get_all_data(campaign)

    budapest_tz = timezone(timedelta(hours=2))
    now = datetime.now(budapest_tz).strftime("%Y-%m-%d %H:%M")

    data = {
        "invited":      contacts,
        "reg_clickers": clickers,
        "unsubs":       unsubs,
        "summary":      summary,
        "updated":      now,
    }

    html = build_html(data, DASHBOARD_PASSWORD)
    with open("stats.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"stats.html generálva ({len(html):,} karakter)")

if __name__ == "__main__":
    main()
