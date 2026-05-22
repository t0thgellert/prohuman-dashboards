#!/usr/bin/env python3
"""
Lekéri az ActiveCampaign API-ból a kampánystatisztikákat
és újragenerálja a stats.html-t.
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

HEADERS = {
    "Api-Token": AC_API_KEY,
    "Content-Type": "application/json",
}

def req(path, params=None):
    time.sleep(0.25)
    r = requests.get(f"{AC_API_URL}/api/3/{path}", headers=HEADERS,
                     params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def ac_get(path, params=None):
    params = dict(params or {})
    params.setdefault("limit", 100)
    params["offset"] = 0
    results = []
    while True:
        data = req(path, params)
        items = next((v for v in data.values() if isinstance(v, list)), [])
        results.extend(items)
        total = int(data.get("meta", {}).get("total", len(items)))
        params["offset"] += len(items)
        if params["offset"] >= total or not items:
            break
    return results

def find_campaign():
    for c in ac_get("campaigns", {"limit": 200}):
        if c.get("name", "").strip() == CAMPAIGN_NAME:
            return c
    raise ValueError(f"Kampány nem található: '{CAMPAIGN_NAME}'")

def get_all_data(campaign):
    cid = campaign["id"]

    summary = {
        "sent":    int(campaign.get("send_amt") or 0),
        "opens":   int(campaign.get("uniqueopens") or 0),
        "clicks":  int(campaign.get("uniquelinkclicks") or 0),
        "bounces": int(campaign.get("bouncedamt") or 0),
        "unsubs":  int(campaign.get("unsubscribes") or 0),
    }
    print(f"  Összesítők: {summary}")

    # Kampány részletek — lista ID-k
    print("  Kampány lista ID-k lekérése...")
    camp_detail = req(f"campaigns/{cid}")
    camp_obj    = camp_detail.get("campaign", {})

    # Az AC API-ban a kampányhoz rendelt listák különböző mezőkben lehetnek
    list_ids = []
    if camp_obj.get("lists"):
        list_ids = [str(x) for x in camp_obj["lists"]]
    if not list_ids and camp_obj.get("list"):
        list_ids = [str(camp_obj["list"])]
    # Próbáljuk a campaignLists végpontot is
    if not list_ids:
        try:
            cl = ac_get("campaignLists", {"campaign": cid})
            list_ids = [str(x.get("list")) for x in cl if x.get("list")]
        except Exception as e:
            print(f"  campaignLists hiba: {e}")

    print(f"  → Lista ID-k: {list_ids}")

    # Kontaktok lekérése listánként
    all_contacts_raw = []
    for lid in list_ids:
        print(f"  Kontaktok lekérése listából {lid}...")
        try:
            batch = ac_get("contacts", {"listid": lid, "limit": 100})
            print(f"  → {len(batch)} kontakt")
            all_contacts_raw.extend(batch)
        except Exception as e:
            print(f"  Lista {lid} hiba: {e}")

    # Deduplikálás email alapján
    seen = set()
    contacts_raw = []
    for c in all_contacts_raw:
        e = c.get("email", "")
        if e and e not in seen:
            seen.add(e)
            contacts_raw.append(c)
    print(f"  → {len(contacts_raw)} egyedi kontakt")

    # Ha lista alapú lekérés sem működött, próbáljuk tag alapján
    if not contacts_raw:
        print("  Lista alapú lekérés üres, összes kontakt lekérése...")
        contacts_raw = ac_get("contacts", {"limit": 100, "status": 1})
        print(f"  → {len(contacts_raw)} aktív kontakt")

    # Kontaktok feldolgozása
    # Megnyitás/bounce státuszt nem tudjuk kontakt szinten megbízhatóan lekérni,
    # ezért a "opened" mezőt a contactList státuszból próbáljuk
    contacts = []
    unsubs   = []

    for cd in contacts_raw:
        contact_id  = str(cd.get("id", ""))
        bounced     = bool(cd.get("bouncedAt"))
        hard_bounce = str(cd.get("bouncedHard", "0")) == "1"

        # Leiratkozás státusz: contactLists-ből
        is_unsub = False
        for cl in (cd.get("contactLists") or []):
            if str(cl.get("status")) == "2":
                is_unsub = True

        name    = f"{cd.get('firstName', '')} {cd.get('lastName', '')}".strip()
        email   = cd.get("email", "")
        company = cd.get("orgname", "") or ""

        # Megnyitás: ha a contact rekordban van ilyen info
        # Az AC v3 API-ban nincs közvetlen "opened this campaign" mező kontakton
        # A legjobb amit tehetünk: bouncedAt alapján állítjuk a státuszt
        opened = False  # alapból ismeretlen

        record = {
            "name":    name,
            "email":   email,
            "company": company,
            "status":  ("Hard Bounce" if hard_bounce else "Soft Bounce") if bounced else "Delivered",
            "detail":  "",
            "opened":  opened,
            "unsub":   is_unsub,
        }
        contacts.append(record)
        if is_unsub:
            unsubs.append({"name": name, "email": email, "company": company})

    clickers = []  # külön tab, egyelőre üres
    return contacts, unsubs, clickers, summary


def build_html(data: dict, password: str) -> str:
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
      <table><thead><tr><th>#</th><th>Név</th><th>E-mail</th><th>Cég</th><th>Státusz</th></tr></thead>
      <tbody id="tb-inv"></tbody></table>
    </div>
  </div>
</div>
<div class="panel" id="p-bounced">
  <div class="tbl-wrap">
    <div class="tbl-search">
      <input type="text" id="s-bnc" placeholder="Keresés…" oninput="renderBounced()">
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
  if(!rows.length){{tb.innerHTML='<tr><td colspan="5" class="empty">Nincs találat.</td></tr>';return;}}
  tb.innerHTML=rows.map((r,i)=>{{
    const sc=r.status==='Delivered'?'b-ok':r.status==='Hard Bounce'?'b-hard':'b-soft';
    return`<tr><td class="mono">${{i+1}}</td><td>${{esc(r.name)}}</td><td class="mono">${{esc(r.email)}}</td><td class="dim">${{esc(r.company)}}</td><td><span class="badge ${{sc}}">${{esc(r.status)}}</span></td></tr>`;
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
    print(f"  → {len(contacts)} kontakt, {len(unsubs)} leiratkozó")

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
