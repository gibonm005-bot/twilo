import requests
import time
import json
import random
import sqlite3
import os
import re
import html
import threading
import hmac
import hashlib
import datetime
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
try:   # Firebase is optional: without the package the bot simply stays on SQLite
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = credentials = firestore = None

# ==========================================
# Configuration
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8965168894:AAHnUsq582V2Sd4ucZ993Dc_4AimX4unpuk")
ADMIN_ID = 7493096480
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# ==========================================
# OTP FORWARDER BOTS: one extra bot per service. Each posts ONLY its own service's OTPs into the OTP group(s).
# Put the tokens in Render -> Environment (or paste them between the quotes). Empty = the main bot sends that service.
#   facebook -> Facebook only | instagram -> Instagram only | whatsapp -> WhatsApp only | other -> Telegram + every other service
# ==========================================
FWD_BOTS = {
    "facebook":  os.environ.get("FWD_FACEBOOK_TOKEN", "9274066613:AAFUvFLyrya_94BVtyj0omatvv1dXi3-tQw").strip(),   # @otp2_fb_bot
    "instagram": os.environ.get("FWD_INSTAGRAM_TOKEN", "9285050639:AAEfLKEfELmt3883E_DXNN89dyThOUPBfrc").strip(),   # @otp2_ig_bot
    "whatsapp":  os.environ.get("FWD_WHATSAPP_TOKEN", "9007954306:AAGbIIAUuzWblWpbJDh04LcAOuoxuE1Ry2o").strip(),   # @otp2_wa_bot
    "other":     os.environ.get("FWD_OTHER_TOKEN", "8964119626:AAEdpEgGE5GpcdOhl32dcG_VRpXQXT447LU").strip(),   # @otp2_tg_bot
}
FWD_PREMIUM = os.environ.get("FWD_PREMIUM", "0") == "1"   # 1 only if the forwarder bots' owner has Telegram Premium
FWD_LABEL = {"facebook": "Facebook", "instagram": "Instagram", "whatsapp": "WhatsApp", "other": "Telegram + Others"}
_FWD = threading.local()          # per-thread sender override while a forwarder bot is posting
_fwd_names = {}                   # route -> @username (filled at startup via getMe)

def _api_base():
    return getattr(_FWD, "url", None) or BASE_URL

def fwd_route(service):
    sv = str(service or "").lower()
    if "facebook" in sv: return "facebook"
    if "instagram" in sv: return "instagram"
    if "whatsapp" in sv: return "whatsapp"
    return "other"

def fwd_init():
    """Startup check: ask Telegram who each forwarder token belongs to."""
    for route, tok in FWD_BOTS.items():
        if not tok: continue
        try:
            r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
            _fwd_names[route] = ("@" + r["result"]["username"]) if r.get("ok") else "invalid token"
        except Exception:
            _fwd_names[route] = "no connection"
        print(f"Forwarder [{route}] -> {_fwd_names[route]}")

_fwd_ids = {}
def fwd_group_check(group_id):
    """Is every forwarder bot (and the main bot) inside the OTP group? Returns a status text."""
    lines = []
    for route in ("facebook", "instagram", "whatsapp", "other"):
        tok = FWD_BOTS.get(route)
        label = FWD_LABEL[route]
        if not tok:
            lines.append(f"🟠 <b>{label}</b> ➜ no forwarder token (main bot sends)"); continue
        try:
            if route not in _fwd_ids:
                me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
                _fwd_ids[route] = (me["result"]["id"], me["result"]["username"]) if me.get("ok") else None
            ident = _fwd_ids[route]
            if not ident: lines.append(f"🔴 <b>{label}</b> ➜ invalid token"); continue
            r = requests.get(f"https://api.telegram.org/bot{tok}/getChatMember", params={"chat_id": group_id, "user_id": ident[0]}, timeout=10).json()
            st = (r.get("result") or {}).get("status") if r.get("ok") else None
            if st in ("administrator", "creator"): lines.append(f"🟢 <b>{label}</b> ➜ @{ident[1]} is admin")
            elif st == "member": lines.append(f"🟠 <b>{label}</b> ➜ @{ident[1]} is only a member (make it admin)")
            else: lines.append(f"🔴 <b>{label}</b> ➜ @{ident[1]} is NOT in the group")
        except Exception:
            lines.append(f"🔴 <b>{label}</b> ➜ check failed")
    return "\n".join(lines)

def fwd_export_link(group_id):
    """Invite link for the OTP button: main bot first, then any forwarder bot that is admin in the group."""
    for base in [BASE_URL] + [f"https://api.telegram.org/bot{t}/" for t in FWD_BOTS.values() if t]:
        try:
            r = requests.post(base + "exportChatInviteLink", json={"chat_id": group_id}, timeout=10).json()
            if r.get("ok"): return r["result"]
        except Exception:
            pass
    return None

def otp_bots_check_text():
    if not otp_forward_groups: return "⚠️ <b>No OTP group set yet.</b>"
    return "🛡 <b>OTP BOTS CHECK</b>\n━━━━━━━━━━━━━━━━━\n" + fwd_group_check(otp_forward_groups[0]) + "\n\n<i>Each service is posted by its own bot, so the number bot does not need to be admin there.</i>"

def fwd_status_text():
    lines = []
    for route in ("facebook", "instagram", "whatsapp", "other"):
        tok = FWD_BOTS.get(route)
        who = _fwd_names.get(route) or ("checking..." if tok else "main bot")
        dot = "🟢" if who.startswith("@") else ("🟠" if (not tok or who == "checking...") else "🔴")
        lines.append(f"{dot} <b>{FWD_LABEL[route]}</b> ➜ <code>{html.escape(who)}</code>")
    return "\n".join(lines)
BOT_USERNAME = "ns_sms_bot"

# Mini App (Live OTP) settings
# WEBAPP_URL = public HTTPS address of this bot server (it serves index.html at "/"), e.g. https://your-app.onrender.com/
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()
API_PORT = int(os.environ.get("PORT", os.environ.get("API_PORT", "8080")))
# Custom (animated) emoji icons on buttons only work if the bot owner has Telegram Premium.
# Leave OFF if icons do not show. Admin can toggle it live: OWNER PANEL > DXA CONTROL > PREMIUM ICONS
PREMIUM_EMOJI_DEFAULT = os.environ.get("PREMIUM_EMOJI", "1") == "1"

# 2oo9 API Configuration
API_2OO9_BASE = "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"
API_2OO9_KEY_DEFAULT = "MK50IRMEXS8"
# Fallback Stex SMS base URLs (comma separated in env API_2OO9_FALLBACKS). Tried in order if the main URL fails.
API_2OO9_FALLBACKS = [u.strip().rstrip("/") for u in os.environ.get("API_2OO9_FALLBACKS", "").split(",") if u.strip()]
CONSOLE_POLL_SECONDS = max(2, int(os.environ.get("CONSOLE_POLL_SECONDS", "4")))
# Ranges stay in the Get Number menu this long after their last traffic (video style: "Saudi Arabia 6h"). Default 24h.
RANGE_KEEP_SECONDS = max(300, int(os.environ.get("RANGE_KEEP_SECONDS", "86400")))
CORE_SERVICES = ["WhatsApp", "Facebook", "Instagram", "Telegram"]   # always shown first in Get Number
cached_other_kb = None

def age_label(ts):
    """Seconds since last traffic -> 32s / 9m / 6h / 2d (empty when unknown)."""
    try: ts = int(ts or 0)
    except Exception: ts = 0
    if ts <= 0: return ""
    d = max(0, int(time.time()) - ts)
    if d < 60: return f"{d}s"
    if d < 3600: return f"{d // 60}m"
    if d < 86400: return f"{d // 3600}h"
    return f"{d // 86400}d"

# ---- service version helpers (Facebook V2 / Facebook V3 ...) + Stex SMS master switch ----
_VER_RE = re.compile(r"\s+V(\d+)$", re.I)
def svc_has_ver(name): return bool(_VER_RE.search(str(name or "").strip()))
def svc_base(name): return _VER_RE.sub("", str(name or "").strip())
def svc_ver_num(name):
    m = _VER_RE.search(str(name or "").strip()); return int(m.group(1)) if m else 2
def v2_label(name):
    n = str(name or "").strip(); return n if svc_has_ver(n) else n + " V2"
def v1_on():
    """V1 menu/numbers are available when Stex SMS or Mino SMS is ON."""
    return stex_on() or mino_on()

def stex_on():
    """Master switch: OFF = Stex SMS is not used at all (menu, pollers, live numbers). Panels keep working."""
    try: return bool(bot_settings.get("stex_on", True))
    except Exception: return True

def stex_request(method, path, timeout=10, **kw):
    """Call Stex SMS API on the main URL; on error/5xx try each fallback URL in order."""
    last_err = None
    for base in [API_2OO9_BASE] + API_2OO9_FALLBACKS:
        try:
            r = requests.request(method, f"{base}{path}", timeout=timeout, **kw)
            if r.status_code < 500: return r
            last_err = Exception(f"HTTP {r.status_code} from {base}")
        except Exception as e:
            last_err = e
    raise last_err or Exception("stex_request failed")

# Global states
current_db_mode = "sqlite"
db_firebase = None 
waiting_for_firebase = False
user_states = {}

# User & Cooldown States
user_cache = {} 
user_active_sessions = {} # format: {"number": user_id}
user_cooldowns = {} 

# Voltx Auto System States
voltx_auto_mode = True 
voltx_keys = [API_2OO9_KEY_DEFAULT]
force_join_status = False
force_join_channels = []

# OTP Forwarding States
otp_forward_groups = []
range_groups = []   # groups where posted ranges (e.g. 2519474XXX) are auto-stocked
otp_button_link = "https://t.me/your_default_bot"
_range_link_cache = {}

def range_group_button():
    """Telegram link of the first range group (cached). None when no range group is set."""
    try:
        for g in list(range_groups):
            g = str(g)
            if g in _range_link_cache: return _range_link_cache[g]
            link = ""
            ch = requests.post(BASE_URL + "getChat", json={"chat_id": g}, timeout=10).json()
            if ch.get("ok"):
                uname = ch["result"].get("username")
                link = f"https://t.me/{uname}" if uname else (ch["result"].get("invite_link") or "")
            if not link:
                ex = requests.post(BASE_URL + "exportChatInviteLink", json={"chat_id": g}, timeout=10).json()
                if ex.get("ok"): link = ex["result"]
            if link: _range_link_cache[g] = link; return link
    except Exception:
        pass
    return None
recent_success_otps = set()

voltx_dynamic_data = {} 
recent_traffic = []
daily_stats = {"date": "", "numbers": 0, "otps": 0} # 🌟 Daily Stats Tracker
daily_user_otps = {"date": "", "users": {}}

# 🌟 Caching Variables for Rocket Speed!
cached_services_kb = None
cached_countries_kb = {}
cached_leaderboard_text = ""


# ==========================================
# Owner added services (manual) + live traffic merged
# ==========================================
def merged_services(include_live=True):
    """Live traffic services + owner-added (manual) services. Manual ranges never expire."""
    out = {}
    if include_live:
        for sid, d in voltx_dynamic_data.items():
            _pa = PREMIUM_APPS.get(next((a for a in PREMIUM_APPS if a.lower() == str(sid).lower()), None) or "")
            out[sid] = {"emoji_id": (_pa["id"] if _pa else d.get("emoji_id")), "normal_emoji": (_pa["emoji"] if _pa else d.get("normal_emoji")),
                        "ranges": {r: dict(rd) for r, rd in d.get("ranges", {}).items() if stex_on() or rd.get("src") == "mino"}, "manual": False}
            if not out[sid]["ranges"]: out.pop(sid, None)
    now = int(time.time())
    for name, rngs in (bot_settings.get("manual_services") or {}).items():
        key = next((k for k in out if k.lower() == name.lower()), None)
        if key is None:
            canon = next((k for k in PREMIUM_APPS if k.lower() == name.lower()), None)
            info = PREMIUM_APPS.get(canon or "Other", PREMIUM_APPS["Other"])
            key = canon or name
            out[key] = {"emoji_id": info["id"], "normal_emoji": info["emoji"], "ranges": {}, "manual": True}
        out[key]["manual"] = True
        for r in rngs:
            cur = out[key]["ranges"].get(r) or {"last_seen": 0, "hits": 0}
            cur["manual"] = True
            out[key]["ranges"][r] = cur
    return out

def parse_manual_ranges(text):
    """Pull ranges like 2519474XXX / +251 9474 / 251947 out of a text. Returns clean digit strings (X removed)."""
    found = []
    for line in str(text or "").splitlines():
        for tok in re.split(r"[,;|]+|\s{2,}", line):
            t = re.sub(r"[\s\-\(\)]", "", tok)
            for m in re.findall(r"\+?(\d{4,13})[xX]*", t):
                if m not in found: found.append(m)
    return found[:50]

def manual_services_text():
    ms = bot_settings.get("manual_services") or {}
    lines = ["🧩 <b>ADD SERVICE</b>", "━━━━━━━━━━━━━━━━━"]
    if not ms:
        lines.append("<i>No owner added service yet.</i>")
    for name, rngs in ms.items():
        by_c = {}
        for r in rngs:
            code, cname = get_country_info(r)
            by_c.setdefault((code, cname), []).append(r)
        lines.append(f"📦 <b>{html.escape(name)}</b>")
        for (code, cname), rr in by_c.items():
            lines.append(f"   {html.escape(cname)} +{code} ➜ <code>{len(rr)}</code> range" + ("s" if len(rr) > 1 else ""))
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("<i>Add a service + range: its country appears in the bot menu automatically. "
                 "Same country with 4/5 ranges = every number request rotates between those ranges.</i>")
    return "\n".join(lines)

def manual_services_keyboard():
    kb = {"inline_keyboard": [[{"text": "ADD SERVICE", "callback_data": "msvc_add", "style": "success"}]]}
    for i, name in enumerate((bot_settings.get("manual_services") or {}).keys()):
        kb["inline_keyboard"].append([{"text": f"DELETE {name}", "callback_data": f"msvc_del_{i}", "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "RANGE GROUP", "callback_data": "manage_range_group", "style": "primary"},
                                  {"text": "NUMBER STOCK", "callback_data": "manage_stock", "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5267490665117275176", "style": "danger"}])
    return kb

def save_manual_service(name, ranges):
    ms = bot_settings.setdefault("manual_services", {})
    key = next((k for k in ms if k.lower() == name.lower()), name)
    cur = ms.setdefault(key, [])
    added = 0
    for r in ranges:
        if r not in cur: cur.append(r); added += 1
    save_local_data()
    return key, added

def build_services_kb(entries):
    """Video style menu: WhatsApp / Facebook / Instagram / Telegram always on top, everything else under Other Service."""
    global cached_services_kb, cached_other_kb
    def find(name, ver):
        return next((e for e in entries if e[0] == ver and str(e[1]).lower() == name.lower()), None)
    kb = {"inline_keyboard": []}
    used = set()
    for name in CORE_SERVICES:
        if is_hidden_service(name): continue
        info = PREMIUM_APPS.get(name, PREMIUM_APPS["Other"])
        btns = []
        if v1_on():
            btns.append({"text": name, "callback_data": f"srv_{name}"[:60], "style": "primary", "icon_custom_emoji_id": info["id"]})
        fam = sorted([e for e in entries if e[0] == "v2" and svc_base(e[1]).lower() == name.lower()], key=lambda e: svc_ver_num(e[1]))
        for e in fam:
            btns.append({"text": v2_label(e[1]), "callback_data": f"sv2_{e[1]}"[:60], "style": "success", "icon_custom_emoji_id": info["id"]})
        for i in range(0, len(btns), 2): kb["inline_keyboard"].append(btns[i:i + 2])
        used.add(name.lower())
    others = [e for e in entries if (svc_base(e[1]).lower() if e[0] == "v2" else str(e[1]).lower()) not in used]
    if others:
        kb["inline_keyboard"].append([{"text": "Other Service", "callback_data": "srv_other", "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "Close", "callback_data": "close_panel", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}])
    cached_services_kb = kb

    okb = {"inline_keyboard": []}; row = []
    for ver, sid, data, total in sorted(others, key=lambda e: e[3], reverse=True)[:40]:
        nm = v2_label(sid) if ver == "v2" else str(sid).strip(); nm = (nm[:15] + "…") if len(nm) > 16 else nm
        cb = f"srv_{html.escape(str(sid))[:15]}" if ver == "v1" else f"sv2_{html.escape(str(sid))[:15]}"
        row.append({"text": nm, "callback_data": cb, "style": "primary" if ver == "v1" else "success", "icon_custom_emoji_id": data["emoji_id"]})
        if len(row) == 2: okb["inline_keyboard"].append(row); row = []
    if row: okb["inline_keyboard"].append(row)
    okb["inline_keyboard"].append([{"text": "Back", "callback_data": "back_to_services", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}])
    cached_other_kb = okb

def auto_cache_menu_thread():
    global cached_services_kb, cached_countries_kb, voltx_auto_mode, voltx_dynamic_data
    while True:
        try:
            _merged = merged_services(voltx_auto_mode) if v1_on() else {}   # V1 = Stex SMS (live traffic + owner added); {} when Stex is OFF
            _sp = smspro_view()                          # V2 = SMS Pro + extra panels: {service: {country_code: {name, count}}}
            entries = []                                 # (version, service, data, total)
            for _sid, _sd in _merged.items():
                entries.append(("v1", _sid, _sd, sum(r["hits"] for r in _sd["ranges"].values())))
            for _sn, _cm in _sp.items():
                _canon = next((a for a in PREMIUM_APPS if a.lower() == _sn.lower()), "Other")
                _info = PREMIUM_APPS[_canon]
                entries.append(("v2", _sn, {"emoji_id": _info["id"], "normal_emoji": _info["emoji"], "ranges": {}, "manual": True},
                                sum(c["count"] for c in _cm.values())))
            entries = [e for e in entries if not is_hidden_service(e[1])]   # admin "HIDE SERVICES"
            build_services_kb(entries)                                       # menu is always there, even with no traffic
            if not entries:
                cached_countries_kb = {}
            if entries:
                # owner added / stocked services first, then by hits (V1) or by number count (V2)
                top_services = sorted(entries, key=lambda x: (x[2].get("manual", False), x[3]), reverse=True)[:60]

                new_srv_kb = {"inline_keyboard": []}
                row = []
                for ver, sid, data, total in top_services:
                    safe_sid = html.escape(str(sid))
                    cb = f"srv_{safe_sid[:15]}" if ver == "v1" else f"sv2_{safe_sid[:15]}"
                    _nm = str(sid).strip()
                    _nm = (_nm[:12] + "…") if len(_nm) > 13 else _nm
                    _lbl = _nm.upper() + (" V2" if ver == "v2" else "")
                    # V1 (Stex) = blue, V2 (SMS Pro / panels) = green; 2 services per row so the list stays tidy
                    row.append({"text": _lbl, "callback_data": cb, "style": "primary" if ver == "v1" else "success", "icon_custom_emoji_id": data["emoji_id"]})
                    if len(row) == 2: new_srv_kb["inline_keyboard"].append(row); row = []
                if row: new_srv_kb["inline_keyboard"].append(row)
                new_srv_kb["inline_keyboard"].append([{"text": "Close", "callback_data": "close_panel", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}])

                # (menu is built by build_services_kb)

                new_cnt_kb = {}
                for ver, sid, data, total in top_services:
                    safe_sid_short = html.escape(str(sid))[:15]
                    std_service = detect_service("", sid)
                    rate = get_otp_reward(std_service)
                    country_map = {}
                    if ver == "v1":
                        for r, r_data in data["ranges"].items():
                            clean_r = str(r).replace("X", "")
                            if not re.match(r'^\d+$', clean_r): continue
                            c_code, c_name = get_country_info(clean_r)
                            if c_code not in country_map: country_map[c_code] = {"name": c_name, "hits": 0, "manual": False, "seen": 0}
                            country_map[c_code]["hits"] += r_data["hits"]
                            country_map[c_code]["seen"] = max(country_map[c_code]["seen"], int(r_data.get("last_seen", 0) or 0))
                            if r_data.get("manual"): country_map[c_code]["manual"] = True
                        top_countries = sorted(country_map.items(), key=lambda x: (x[1]["seen"], x[1]["hits"]), reverse=True)[:40]   # freshest first
                    else:
                        _spc = next((v for k, v in _sp.items() if k.lower() == str(sid).lower()), {})
                        # V2: every country that has numbers in stock, biggest stock first
                        top_countries = sorted(((cc, {"name": cd["name"], "hits": cd["count"], "manual": True}) for cc, cd in _spc.items()),
                                               key=lambda x: x[1]["hits"], reverse=True)[:30]

                    cnt_kb = {"inline_keyboard": []}
                    styles_c = ["primary", "success"]; style_idx_c = 0; c_row = []
                    for c_code, c_data in top_countries:
                        c_name_full = c_data["name"]
                        parts = c_name_full.split(" ", 1)
                        emoji_char = parts[0]
                        clean_name = parts[1] if len(parts) > 1 else c_name_full
                        short_name = clean_name if len(clean_name) <= 14 else clean_name[:13] + "…"
                        if ver == "v2":
                            # SMS Pro / panel countries: show how many numbers are available right now
                            label = f"Other +{c_code}" if "Other" in clean_name else short_name
                            btn_text = f"{label} ({c_data['hits']})"
                            cb = f"sp_{safe_sid_short}_{c_code}"
                        else:
                            _age = age_label(c_data.get("seen", 0))
                            btn_text = (f"Other +{c_code}" if "Other" in clean_name else short_name) + (f" {_age}" if _age else "")
                            cb = f"cc_{safe_sid_short}_{c_code}"
                        btn = {"text": btn_text, "callback_data": cb, "style": styles_c[style_idx_c % 2]}
                        if emoji_char in GLOBAL_BODY_EMOJIS: btn["icon_custom_emoji_id"] = GLOBAL_BODY_EMOJIS[emoji_char]
                        c_row.append(btn)
                        style_idx_c += 1
                        if len(c_row) == 2: cnt_kb["inline_keyboard"].append(c_row); c_row = []
                    if c_row: cnt_kb["inline_keyboard"].append(c_row)

                    cnt_kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_services", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}])
                    new_cnt_kb[(("v2:" if ver == "v2" else "") + safe_sid_short)] = cnt_kb

                cached_countries_kb = new_cnt_kb
                
            # --- 🌟 Leaderboard Caching ---
            global cached_leaderboard_text, daily_user_otps
            today = time.strftime("%Y-%m-%d")
            if daily_user_otps.get("date") != today:
                daily_user_otps["date"] = today
                daily_user_otps["users"] = {}
                
            top_users = sorted(daily_user_otps["users"].items(), key=lambda x: x[1], reverse=True)[:5]
            lb_msg = "<blockquote><tg-emoji emoji-id=\"5240021484516185513\">🏆</tg-emoji> <b>𝗧𝗢𝗣 𝟱 𝗟𝗘𝗔𝗗𝗘𝗥𝗕𝗢𝗔𝗥𝗗 (TODAY)</b></blockquote>\n━━━━━━━━━━━━━━━━━\n"
            emoji_map = [("1️⃣", "5238176680098440792"), ("2️⃣", "5237806896299157399"), ("3️⃣", "5239965456667812809"), ("4️⃣", "5237911487342751418"), ("5️⃣", "5240278297790686165")]
            
            if not top_users: 
                lb_msg += "<i>No OTPs received today.</i>\n"
            else:
                for idx, (uid_str, otp_count) in enumerate(top_users):
                    u_data = get_user(int(uid_str))
                    fname = u_data["first_name"] if u_data else "User"
                    emoji_char, emoji_id = emoji_map[idx]
                    lb_msg += f"<blockquote><tg-emoji emoji-id=\"{emoji_id}\">{emoji_char}</tg-emoji> <a href='tg://user?id={uid_str}'>{html.escape(str(fname))}</a> - {otp_count} OTP</blockquote>\n"
            lb_msg += "━━━━━━━━━━━━━━━━━"
            cached_leaderboard_text = lb_msg
            
        except Exception as e:
            pass
        time.sleep(5) # 🌟 5 সেকেন্ড পরপর ব্যাকগ্রাউন্ডে ক্যাশ রেডি হবে

# Admin Settings (DXA Control)
bot_settings = {
    "withdraw_on": True,
    "min_withdraw": 10.0,
    "support_link": "https://t.me/snotpsupport",
    "w_group": "",
    "auto_br_on": False,
    "auto_br_interval": 60,
    "cooldown": 5, # Default 5 seconds
    "num_req": 3, # Default 3 numbers per request (video style)
    "w_methods": ["bKash", "Nagad"],
    "otp_default_rate": 0.5,
    "otp_service_rates": {},
    "main_channel_link": "",
    "premium_emoji": PREMIUM_EMOJI_DEFAULT,
    "stock_auto": True,      # auto refill stocked ranges back to their target
    "stock_ttl_min": 0,      # 0 = stocked numbers never expire, else drop after N minutes
    "stock_default": 1000,   # default numbers per range (500 / 1000 / 2000)
    "emoji_overrides": {},   # {"svc:Facebook": {"id": "...", "emoji": "📘"}, "cc:🇧🇩": {"id": "..."}} set from OWNER PANEL > DXA CONTROL > MANUAL EMOJIS
    "manual_services": {},   # {"Telegram": ["251947", "251948"]} owner added services (never expire)
    "auto_range_on": True,   # range group: auto add top traffic ranges (stock + post to group)
    "auto_range_top": 10,    # how many top-traffic ranges to auto stock each round (WhatsApp first)
    # ---- Admin feature switches (OWNER PANEL > FEATURES) ----
    "feat_get": True, "feat_search": True, "feat_traffic": True, "feat_twofa": True,
    "feat_gmail": True, "feat_refer": True, "feat_profile": True, "feat_lb": True,
    "maintenance": False,    # True = only the admin can use the bot
    "hidden_services": [],   # service names hidden from Get Number menu + Live Traffic
    # ---- Gmail option (OWNER PANEL > GMAIL) ----
    "gmail_stock": [],       # ["email:password", ...] admin loaded accounts
    "gmail_limit": 3,        # accounts per user per day (0 = unlimited)
    "gmail_given": {},       # {"user_id": {"date": "YYYY-MM-DD", "n": 1}}
    "gmail_total_given": 0,
    "gmail_site": "",        # https:// link opened INSIDE the bot as a Mini App (web_app button)
    "gmail_miniapp": "",     # t.me/<bot>/<app> Telegram Mini App link
    "ref_earn": {},          # {"user_id": total referral earning}
}

# Feature switches: (key, label, reply-keyboard command it controls)
FEATURES = [("get", "Get Number", "LIVE NUMBER"), ("search", "Search Number", "SEARCH NUMBER"),
            ("traffic", "Traffic", "LIVE TRAFFIC"), ("twofa", "2FA Online", "2FA ONLINE"),
            ("gmail", "Gmail", "GMAIL"), ("refer", "Refer", "INVITE FRIEND"),
            ("profile", "My Profile", "WALLET"), ("lb", "Leaderboard", "LEADERBOARD")]
_FEATURE_OF_CMD = {cmd: key for key, _label, cmd in FEATURES}

def feat_on(key):
    return bool(bot_settings.get(f"feat_{key}", True))

def is_hidden_service(name):
    n = str(name or "").strip().lower()
    return bool(n) and n in {str(h).strip().lower() for h in bot_settings.get("hidden_services", [])}

BOT_DATA_FILE = "bot_data.json"
USERS_LIST_FILE = "users_list.json"

# ==========================================
# Helper: Get Active Voltx Key & Rates
# ==========================================
def get_api_key():
    return random.choice(voltx_keys) if voltx_keys else API_2OO9_KEY_DEFAULT

def get_otp_reward(service_name):
    rates = bot_settings.get("otp_service_rates", {})
    val = rates.get(service_name, bot_settings.get("otp_default_rate", 0.5))
    return float(val)

def update_daily_stat(key, amount=1):
    global daily_stats
    today = time.strftime("%Y-%m-%d")
    if daily_stats.get("date") != today:
        daily_stats = {"date": today, "numbers": 0, "otps": 0}
    daily_stats[key] = daily_stats.get(key, 0) + amount

# ==========================================
# Local Storage Managers
# ==========================================
def load_local_data():
    global recent_traffic, voltx_dynamic_data, voltx_keys, force_join_status, force_join_channels, voltx_auto_mode, otp_forward_groups, otp_button_link, recent_success_otps, bot_settings, user_active_sessions, daily_stats, daily_user_otps
    if os.path.exists(BOT_DATA_FILE):
        try:
            with open(BOT_DATA_FILE, "r") as f:
                data = json.load(f)
                recent_traffic = data.get("recent_traffic", [])
                voltx_dynamic_data = data.get("voltx_dynamic_data", {})
                loaded_keys = data.get("voltx_keys", [])
                
                if not loaded_keys:
                    voltx_keys = [API_2OO9_KEY_DEFAULT]
                else:
                    voltx_keys = loaded_keys
                    
                force_join_status = data.get("force_join_status", False)
                force_join_channels = data.get("force_join_channels", [])
                voltx_auto_mode = data.get("voltx_auto_mode", True)
                otp_forward_groups = data.get("otp_forward_groups", [])
                range_groups[:] = data.get("range_groups", [])
                otp_button_link = data.get("otp_button_link", "https://t.me/your_default_bot")
                recent_success_otps = set(data.get("recent_success_otps", []))
                
                loaded_settings = data.get("bot_settings", {})
                for k, v in loaded_settings.items(): bot_settings[k] = v
                if not (bot_settings.get("support_link") or "").strip(): bot_settings["support_link"] = "https://t.me/snotpsupport"   # empty saved value -> default
                
                daily_stats = data.get("daily_stats", {"date": "", "numbers": 0, "otps": 0})
                daily_user_otps = data.get("daily_user_otps", {"date": "", "users": {}})
                user_active_sessions = data.get("user_active_sessions", {})
                for _sid, _d in voltx_dynamic_data.items():
                    _canon = next((a for a in PREMIUM_APPS if a.lower() == str(_sid).lower()), None)
                    if _canon and isinstance(_d, dict):
                        _d["emoji_id"] = PREMIUM_APPS[_canon]["id"]; _d["normal_emoji"] = PREMIUM_APPS[_canon]["emoji"]
                apply_emoji_overrides()
        except Exception as e:
            print(f"Error loading local data: {e}")

def save_local_data():
    global recent_traffic, voltx_dynamic_data, voltx_keys, force_join_status, force_join_channels, voltx_auto_mode, otp_forward_groups, otp_button_link, recent_success_otps, bot_settings, user_active_sessions, daily_stats
    try:
        with open(BOT_DATA_FILE, "w") as f:
            json.dump({
                "recent_traffic": recent_traffic, 
                "voltx_dynamic_data": voltx_dynamic_data,
                "voltx_keys": voltx_keys,
                "force_join_status": force_join_status,
                "force_join_channels": force_join_channels,
                "voltx_auto_mode": voltx_auto_mode,
                "otp_forward_groups": otp_forward_groups,
                "range_groups": range_groups,
                "otp_button_link": otp_button_link,
                "recent_success_otps": list(recent_success_otps),
                "bot_settings": bot_settings,
                "user_active_sessions": user_active_sessions,
                "daily_stats": daily_stats,
                "daily_user_otps": daily_user_otps
            }, f)
    except Exception as e:
        print(f"Error saving local data: {e}")
        
    # 🌟 Firebase এ লাইভ আপডেট পাঠানো
    if current_db_mode == "firebase" and db_firebase:
        try:
            db_firebase.collection('settings').document('bot_config').set({
                "bot_settings": bot_settings,
                "force_join_status": force_join_status,
                "force_join_channels": force_join_channels,
                "otp_forward_groups": otp_forward_groups,
                "range_groups": range_groups,
                "otp_button_link": otp_button_link,
                "voltx_keys": voltx_keys,
                "voltx_auto_mode": voltx_auto_mode
            }, merge=True)
        except: pass

# গ্লোবাল ভেরিয়েবলে ইউজার লিস্ট ক্যাশ করে রাখা হলো
cached_user_list = set()

def add_to_broadcast_list(user_id):
    global cached_user_list
    if not cached_user_list and os.path.exists(USERS_LIST_FILE):
        try:
            with open(USERS_LIST_FILE, "r") as f:
                cached_user_list = set(json.load(f))
        except: pass
        
    if user_id not in cached_user_list:
        cached_user_list.add(user_id)
        try:
            with open(USERS_LIST_FILE, "w") as f:
                json.dump(list(cached_user_list), f)
        except: pass

def get_all_user_ids():
    global user_cache
    users = set()
    
    # 🌟 ১. প্রথমে RAM Cache থেকে দ্রুত ইউজার নিবে
    for uid in user_cache.keys():
        users.add(int(uid))
        
    # 🌟 ২. এরপর লোকাল JSON ফাইল থেকে নিবে
    if os.path.exists(USERS_LIST_FILE):
        try:
            with open(USERS_LIST_FILE, "r") as f:
                for u in json.load(f): users.add(int(u))
        except: pass
        
    #RAM সবসময় আপডেটেড থাকে!
    return list(users)

# ==========================================
# Telegram APIs & Force Join Helpers
# ==========================================
def get_bot_info():
    try:
        res = requests.get(BASE_URL + "getMe").json()
        if res.get("ok"): return res["result"]["username"]
    except Exception: pass
    return ""

def set_bot_commands():
    commands = [{"command": "start", "description": "Start the bot or refresh menu"}]
    try: requests.post(BASE_URL + "setMyCommands", json={"commands": commands})
    except Exception: pass

# ==========================================
# Icon fallback: custom emoji -> normal emoji (when owner has no Telegram Premium)
# ==========================================
def premium_on():
    if getattr(_FWD, "plain", False): return False
    return bool(bot_settings.get("premium_emoji", False))

_TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.S)

def render_text(text):
    return apply_emojis(text) if premium_on() else _TG_EMOJI_RE.sub(r"\1", text)

TEXT_ICONS = [
    ("RANGE GROUP", "🗂"), ("NUMBER STOCK", "📦"), ("AUTO REFILL", "♻️"), ("EXPIRY", "⏱"), ("CLEAR ALL", "🧹"), ("STOP FILLING", "⏹"), ("DEFAULT:", "🎯"), ("YES, CLEAR", "🧹"),
    ("OPEN LIVE OTP APP", "⚡"), ("LIVE OTP APP", "⚡"), ("LIVE NUMBER", "📱"), ("LIVE TRAFFIC", "📈"),
    ("INVITE FRIEND", "👥"), ("WALLET", "💰"), ("LEADERBOARD", "🏆"), ("OWNER PANEL", "👑"),
    ("STEX SMS", "📡"), ("BROADCAST", "📣"), ("FORCE JOIN", "🔗"), ("OTP GROUP", "💬"), ("USER CONTROL", "👥"),
    ("DXA CONTROL", "⚙️"), ("UPLOAD FIREBASE", "🔥"), ("SQLITE", "🗄"), ("FIREBASE", "🗄"),
    ("CLOSE", "✖️"), ("BACK", "◀️"), ("CANCEL", "✖️"), ("REFRESH", "🔄"), ("COPY LINK", "🔗"),
    ("CHANGE NUMBER", "🔄"), ("VERIFY", "✅"), ("JOIN", "🔗"), ("APPROVED", "✅"), ("APPROVE", "✅"),
    ("REJECTED", "❌"), ("REJECT", "❌"), ("DELETE", "🗑"), ("ADD", "➕"), ("SET ", "➕"),
    ("STATUS: ON", "🟢"), ("STATUS: OFF", "🔴"), ("VOLTX AUTO: ON", "🟢"), ("VOLTX AUTO: OFF", "🔴"),
    ("MIN WITHDRAW", "💸"), ("OTP CONTROL", "🔐"), ("REFER REWARD", "🎁"), ("COOLDOWN", "⏳"),
    ("NUM/REQ", "🔢"), ("W. METHODS", "💳"), ("W. GROUP", "👥"), ("PREMIUM ICONS", "✨"),
    ("DEFAULT RATE", "💰"), ("USER PROFILE", "👤"), ("BALANCE", "💵"), ("LINK:", "🔗"), ("MAIN CHANNEL", "📢"),
    ("FEATURES", "🎛"), ("HIDE SERVICES", "🙈"), ("GMAIL", "📧"), ("MAINTENANCE", "🛠"), ("SHARE LINK", "📤"), ("GET ANOTHER", "📧"), ("ADD STOCK", "➕"), ("CLEAR STOCK", "🧹"), ("LIMIT", "🔢"),
    ("GET NUMBER", "📞"), ("SEARCH NUMBER", "🔍"), ("TRAFFIC", "🚦"), ("2FA ONLINE", "🛡"), ("REFER", "🎁"), ("MY PROFILE", "👤"),
    ("SUPPORT", "💬"), ("ADMIN PANEL", "🛡"), ("REMOVE CC", "🚫"), ("ADD CC", "➕"), ("CHANGE COUNTRY", "🌐"),
    ("CONTACT SUPPORT", "💬"), ("PREV", "◀️"), ("NEXT", "▶️"), ("PAGE ", "📄"), ("CHANNEL", "📣"), ("NUMBER", "📞"),
    ("OTHER", "🌐"), ("+", "📋"),
]
_ID_ICON = {}

def _icon_for(text, eid):
    up = str(text).upper().lstrip()
    for key, icon in TEXT_ICONS:
        if up.startswith(key): return icon
    for name, info in PREMIUM_APPS.items():
        if up.startswith(name.upper()): return info["emoji"]
    if eid:
        if not _ID_ICON:
            for ch, i in GLOBAL_BODY_EMOJIS.items(): _ID_ICON[i] = ch
            for info in PREMIUM_APPS.values(): _ID_ICON[info["id"]] = info["emoji"]
        return _ID_ICON.get(str(eid))
    return None

def _strip_styles(markup):
    out = {}
    for key, val in markup.items():
        if key in ("inline_keyboard", "keyboard") and isinstance(val, list):
            out[key] = [[({k: v for k, v in b.items() if k != "style"} if isinstance(b, dict) else b) for b in row] for row in val]
        else:
            out[key] = val
    return out

def adapt_markup(markup):
    """Premium OFF: drop custom-emoji icons and put a normal emoji in front of the button text instead."""
    if not markup or not isinstance(markup, dict): return markup
    markup = beautify_markup(markup)          # every button gets a color + icon
    if bot_settings.get("transparent_buttons", True): markup = _strip_styles(markup)   # transparent = Telegram default glass buttons, no green/blue/red fill
    if premium_on(): return markup
    out = {}
    for key, val in markup.items():
        if key in ("inline_keyboard", "keyboard") and isinstance(val, list):
            rows = []
            for row in val:
                new_row = []
                for b in row:
                    if isinstance(b, dict):
                        nb = dict(b)
                        eid = nb.pop("icon_custom_emoji_id", None)
                        t = nb.get("text", "")
                        if t and ord(t[0]) < 128:
                            icon = _icon_for(t, eid)
                            if icon: nb["text"] = f"{icon} {t}"
                        new_row.append(nb)
                    else:
                        new_row.append(b)
                rows.append(new_row)
            out[key] = rows
        else:
            out[key] = val
    return out

EFFECT_FIRE = "5104841245755180586"
EFFECT_PARTY = "5046509860389126442"

# ==========================================
# MESSAGE LAYER  (no GIFs: every screen gets an animated (custom) emoji instead)
# `gif="screen"` arguments in the code are now just the screen name -> its animated emoji below.
# Add a new feature: add one line to SCREEN_ICON and pass gif="yourscreen" when sending.
# ==========================================
SCREEN_ICON = {   # screen -> (IC key, normal emoji fallback)
    "start": ("wave", "👋"), "services": ("globe", "🌐"), "countries": ("globe", "🌐"), "number": ("phone", "📱"),
    "traffic": ("fire", "🔥"), "refer": ("gift", "🎁"), "wallet": ("money", "💰"), "leaderboard": ("trophy", "🏆"),
    "search": ("search", "🔍"), "twofa": ("shield", "🛡"), "gmail": ("gmail", "📧"), "support": ("chat", "💬"),
    "owner": ("gear", "⚙️"),
}
_EMOJI_CHARS_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")

def _with_screen_icon(text, screen, parse_mode):
    """Put the screen's animated emoji in front of the text unless the text already starts with an emoji."""
    try:
        if not screen or parse_mode != "HTML" or screen not in SCREEN_ICON: return text
        head = (text or "")[:160]
        if "<tg-emoji" in head or _EMOJI_CHARS_RE.match(re.sub(r"^(<[^>]+>|\s)+", "", head)): return text
        key, fb = SCREEN_ICON[screen]
        return f"{_temoji(key, fb)} {text}"
    except Exception:
        return text

def _tg(method, payload, timeout=15):
    try: return requests.post(BASE_URL + method, json=payload, timeout=timeout).json()
    except Exception: return None

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", effect_id=None, gif=None):
    text = _with_screen_icon(text, gif, parse_mode)
    payload = {"chat_id": chat_id, "text": render_text(text), "parse_mode": parse_mode}
    if reply_markup: payload["reply_markup"] = adapt_markup(reply_markup)
    try:
        if effect_id:
            # Message effects only work in private chats; fall back silently if rejected
            res = requests.post(_api_base() + "sendMessage", json=dict(payload, message_effect_id=effect_id)).json()
            if res.get("ok"): return res
        return requests.post(_api_base() + "sendMessage", json=payload).json()
    except Exception: return None

def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML", gif=None):
    text = _with_screen_icon(text, gif, parse_mode)
    payload = {"chat_id": chat_id, "message_id": message_id, "text": render_text(text), "parse_mode": parse_mode}
    if reply_markup: payload["reply_markup"] = adapt_markup(reply_markup)
    res = _tg("editMessageText", payload)
    # an OLD GIF message (sent before this update) cannot be edited as text -> replace it with a normal message
    if res and not res.get("ok") and "no text in the message" in str(res.get("description", "")):
        try: delete_message(chat_id, message_id)
        except Exception: pass
        send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)

def answer_callback_query(callback_query_id, text="", show_alert=False):
    payload = {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert}
    requests.post(BASE_URL + "answerCallbackQuery", json=payload)

def delete_message(chat_id, message_id):
    payload = {"chat_id": chat_id, "message_id": message_id}
    requests.post(BASE_URL + "deleteMessage", json=payload)

# ==========================================
# VIDEO STYLE UI HELPERS (premium emoji, rich messages, number card)
# ==========================================
BOT_TITLE = os.environ.get("BOT_TITLE", "The Profit Player Number Bot")
RICH_MARK = "<!--rich-->"
CLASSIC_SEP = "<!--classic-->"
IC = {  # premium (animated) emoji ids already used in this bot
    "star": "5352552689983067014", "wave": "5353027129250453493", "rocket": "5352597830089347330",
    "globe": "5334590977837403844", "folder": "5257969839313526622", "spark": "5337102391244263212",
    "phone": "5337132498965010628", "search": "5463352748751753567", "traffic": "5429571662737611814",
    "shield": "5190447043545438788", "gift": "5420396762189831222", "user": "5352861489541714456",
    "trophy": "5240021484516185513", "chat": "5337302974806922068", "gear": "5420155432272438703",
    "gmail": "5429558795015593080", "key": "5197288647275071607", "ok": "5352694861990501856", "no": "5420130255174145507",
    "money": "5429612421977253466", "fire": "5337267511261960341", "share": "5420517437885943844", "stats": "5353032893096567467",
    "back": "5438541186539232243", "change": "5429386648431405093", "ban": "5334807341109908955",
    "bell": "5352980533150259581", "arrow": "5420618897898381296", "green": "5192812028632274956",
    "cal": "5352585194295564660", "getnum": "5429167978761461870", "grp": "5314366268898300483",
    "id": "5226929552319594190", "lock": "5337255927735163754", "hour": "5337172996211648018",
}

def _temoji(key_or_id, fallback):
    eid = IC.get(key_or_id, key_or_id)
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

def flag_iso(flag):
    cps = [ord(c) for c in str(flag)]
    if len(cps) >= 2 and all(0x1F1E6 <= c <= 0x1F1FF for c in cps[:2]):
        return chr(cps[0] - 0x1F1E6 + 65) + chr(cps[1] - 0x1F1E6 + 65)
    return "WW"

_LANG_RANGES = [(0x0980, 0x09FF, "BENGALI"), (0x0400, 0x04FF, "RUSSIAN"), (0x0600, 0x06FF, "ARABIC"),
                (0x0900, 0x097F, "HINDI"), (0x4E00, 0x9FFF, "CHINESE"), (0x0E00, 0x0E7F, "THAI"),
                (0x3040, 0x30FF, "JAPANESE"), (0xAC00, 0xD7AF, "KOREAN")]

def detect_lang(text):
    for ch in str(text or ""):
        o = ord(ch)
        for a, b, name in _LANG_RANGES:
            if a <= o <= b: return name
    return "ENGLISH"

def send_rich_message(chat_id, rich_html, reply_markup=None):
    """Bot API 10.1 rich message (real tables / headings). Returns None when Telegram rejects it so the caller can fall back."""
    payload = {"chat_id": chat_id, "rich_message": {"html": render_text(rich_html)}}
    if reply_markup: payload["reply_markup"] = adapt_markup(reply_markup)
    try:
        res = requests.post(_api_base() + "sendRichMessage", json=payload, timeout=15).json()
        if res.get("ok"): return res
    except Exception:
        pass
    return None

def edit_reply_markup(chat_id, message_id, markup):
    try:
        requests.post(BASE_URL + "editMessageReplyMarkup",
                      json={"chat_id": chat_id, "message_id": message_id, "reply_markup": adapt_markup(markup)}, timeout=10)
    except Exception:
        pass

def parse_2fa_input(text):
    """Accepts a plain Base32 key (spaces/dashes/lowercase ok) or an otpauth:// link / QR text.
    Returns (secret, digits, period, algo) or None when it is not a valid key."""
    raw = str(text or "").strip()
    digits, period, algo = 6, 30, "SHA1"
    if raw.lower().startswith("otpauth://"):
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
            raw = (q.get("secret") or [""])[0]
            digits = int((q.get("digits") or [6])[0]); period = int((q.get("period") or [30])[0])
            algo = ((q.get("algorithm") or ["SHA1"])[0]).upper()
        except Exception:
            return None
    s = re.sub(r"[\s\-_=]", "", raw).upper()
    if not re.fullmatch(r"[A-Z2-7]{8,128}", s): return None
    if digits not in (6, 7, 8) or not (10 <= period <= 120) or algo not in ("SHA1", "SHA256", "SHA512"): return None
    return s, digits, period, algo

def totp_now(secret, digits=6, period=30, algo="SHA1", now=None):
    import base64, struct
    s = re.sub(r"[\s\-_=]", "", str(secret)).upper()
    s += "=" * (-len(s) % 8)
    key = base64.b32decode(s)
    now = time.time() if now is None else now
    digest = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}.get(algo, hashlib.sha1)
    h = hmac.new(key, struct.pack(">Q", int(now // period)), digest).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}", period - int(now % period)

# ---------- 2FA Authenticator (video style) ----------
twofa_last = {}   # user_id -> (secret, digits, period, algo) so the Refresh button works

def twofa_prompt_text():
    return (f'{_temoji("shield", "🛡")} <b>2FA Authenticator</b>\n━━━━━━━━━━━━━━━━━\n'
            f'Please send your <b>2FA Secret Key</b> below.\n\n'
            f'{_temoji("key", "🔑")} <i>Example:</i> <code>JBSWY3DPEHPK3PXP</code>\n'
            f'<i>You can also paste an otpauth:// link.</i>')

def twofa_prompt_kb():
    return {"inline_keyboard": [[{"text": "Cancel", "callback_data": "twofa_cancel", "icon_custom_emoji_id": IC["no"], "style": "danger"}]]}

def twofa_result(user_id):
    """(text, keyboard) for the current code of the user's last secret, or None."""
    cfg = twofa_last.get(user_id)
    if not cfg: return None
    secret, digits, period, algo = cfg
    code, left = totp_now(secret, digits, period, algo)
    bar_n = max(0, min(10, round(10 * left / period)))
    text = (f'{_temoji("shield", "🛡")} <b>2FA Authenticator</b>\n━━━━━━━━━━━━━━━━━\n'
            f'{_temoji("lock", "🔐")} <b>Code:</b> <code>{code}</code>\n'
            f'{_temoji("hour", "⏳")} <b>Expires in:</b> {left}s\n'
            f'<code>{"█" * bar_n}{"░" * (10 - bar_n)}</code>\n━━━━━━━━━━━━━━━━━\n'
            f'<i>Tap the code to copy it. Refresh for a new one.</i>')
    kb = {"inline_keyboard": [
        [{"text": code, "copy_text": {"text": code}, "icon_custom_emoji_id": IC["shield"], "style": "success"}],
        [{"text": "Refresh", "callback_data": "twofa_ref", "icon_custom_emoji_id": "5229111790842952353", "style": "primary"},
         {"text": "Close", "callback_data": "close_panel", "icon_custom_emoji_id": IC["no"], "style": "danger"}]]}
    return text, kb

# ---------- Gmail option ----------
def gmail_parse(line):
    """'email:pass' / 'email|pass' / 'email pass' / 'email;pass' -> (email, password) or None."""
    m = re.match(r"^\s*([\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+)\s*[:|;,\s\t]+\s*(\S.*?)\s*$", str(line or ""))
    return (m.group(1), m.group(2)) if m else None

def gmail_add_stock(text):
    added = bad = dup = 0
    have = {gmail_parse(x)[0].lower() for x in bot_settings.get("gmail_stock", []) if gmail_parse(x)}
    for ln in str(text or "").splitlines():
        if not ln.strip(): continue
        p = gmail_parse(ln)
        if not p: bad += 1; continue
        if p[0].lower() in have: dup += 1; continue
        bot_settings.setdefault("gmail_stock", []).append(f"{p[0]}:{p[1]}")
        have.add(p[0].lower()); added += 1
    if added: save_local_data()
    return added, dup, bad

def gmail_user_left(user_id):
    """None = unlimited, else how many accounts the user can still take today."""
    lim = int(bot_settings.get("gmail_limit", 0) or 0)
    if lim <= 0: return None
    rec = (bot_settings.get("gmail_given") or {}).get(str(user_id)) or {}
    used = rec.get("n", 0) if rec.get("date") == time.strftime("%Y-%m-%d") else 0
    return max(0, lim - used)

def gmail_take(user_id):
    """Pop one account for the user. Returns ("ok", email, pw) / ("empty",) / ("limit",)."""
    if gmail_user_left(user_id) == 0: return ("limit",)
    stock = bot_settings.get("gmail_stock", [])
    while stock:
        p = gmail_parse(stock.pop(0))
        if not p: continue
        today = time.strftime("%Y-%m-%d")
        g = bot_settings.setdefault("gmail_given", {})
        rec = g.get(str(user_id))
        if not rec or rec.get("date") != today: rec = {"date": today, "n": 0}
        rec["n"] += 1; g[str(user_id)] = rec
        bot_settings["gmail_total_given"] = bot_settings.get("gmail_total_given", 0) + 1
        save_local_data()
        return ("ok", p[0], p[1])
    save_local_data()
    return ("empty",)

def gmail_link_rows():
    rows = []
    site = (bot_settings.get("gmail_site") or "").strip()
    app_ = (bot_settings.get("gmail_miniapp") or "").strip()
    if app_: rows.append([{"text": "Open Gmail App", "url": app_, "icon_custom_emoji_id": IC["gmail"], "style": "success"}])
    if site: rows.append([{"text": "Open Gmail Site" if app_ else "Open Gmail App", "web_app": {"url": site}, "icon_custom_emoji_id": IC["gmail"], "style": "primary"}])
    return rows

def gmail_user_content(user_id):
    """(text, keyboard) shown when a user taps the Gmail button."""
    res = gmail_take(user_id)
    gi = _temoji("gmail", "📧")
    if res[0] == "limit":
        return (f'{gi} <b>Gmail</b>\n━━━━━━━━━━━━━━━━━\n{_temoji("hour", "⏳")} <b>Daily limit reached.</b>\n'
                f'<i>Come back tomorrow for more accounts.</i>', ({"inline_keyboard": gmail_link_rows()} if gmail_link_rows() else None))
    if res[0] == "empty":
        return (f'{gi} <b>Gmail</b>\n━━━━━━━━━━━━━━━━━\n{_temoji("ban", "🚫")} <b>Out of stock right now.</b>\n'
                f'<i>Please try again later.</i>', ({"inline_keyboard": gmail_link_rows()} if gmail_link_rows() else None))
    _, email, pw = res
    left = gmail_user_left(user_id)
    text = (f'{gi} <b>Your Gmail Account</b>\n━━━━━━━━━━━━━━━━━\n'
            f'{_temoji("user", "👤")} <b>Email:</b>\n<code>{html.escape(email)}</code>\n— — — — — — — — — —\n'
            f'{_temoji("lock", "🔐")} <b>Password:</b>\n<code>{html.escape(pw)}</code>\n━━━━━━━━━━━━━━━━━\n'
            f'<i>Tap a button to copy. Change the password after login.</i>'
            + (f'\n{_temoji("stats", "📊")} <i>Left today: {left}</i>' if left is not None else ""))
    kb = {"inline_keyboard": [
        [{"text": "Copy Email", "copy_text": {"text": email}, "icon_custom_emoji_id": IC["gmail"], "style": "primary"},
         {"text": "Copy Password", "copy_text": {"text": pw[:256]}, "icon_custom_emoji_id": IC["lock"], "style": "success"}],
        [{"text": "Get Another", "callback_data": "gmail_more", "icon_custom_emoji_id": IC["change"], "style": "primary"},
         {"text": "Close", "callback_data": "close_panel", "icon_custom_emoji_id": IC["no"], "style": "danger"}]]}
    kb["inline_keyboard"] = gmail_link_rows() + kb["inline_keyboard"]
    return text, kb

# animated premium emoji shown next to each number (1st number = 1st id, 2nd = 2nd id ... then it repeats)
NUM_ANIM_IDS = ["5866382725937632001", "5866009583473924494", "5866456517770745292", "5866046133645611309", "5866469651780736116"]

def _num_button(number_text, flag_id=None, idx=0):
    b = {"text": number_text, "copy_text": {"text": number_text}, "style": "success"}
    b["icon_custom_emoji_id"] = NUM_ANIM_IDS[idx % len(NUM_ANIM_IDS)]
    return b

CODE_BTN_DIGITS = 4   # "Code (3929)" = last N digits of the number (change to 3 if you want 3 digits)

def _code_button(number):
    d = re.sub(r"\D", "", str(number))
    return {"text": f"Code ({d[-CODE_BTN_DIGITS:]})", "callback_data": f"nc_{d}", "icon_custom_emoji_id": IC["change"], "style": "primary"}

def find_number_otp(number_digits, user_id=None, max_age=1500):
    """Newest OTP (last 25 min) the panels received for this number; None if nothing arrived yet."""
    tail = re.sub(r"\D", "", str(number_digits))[-8:]
    if not tail: return None
    now = time.time()
    with feed_lock:
        for e in reversed(otp_feed):
            if now - e.get("ts", 0) > max_age: break
            if not str(e.get("_full", "")).endswith(tail) or not e.get("code"): continue
            if e.get("_owner") not in (None, user_id): continue
            return dict(e)
    return None

def build_number_card(service_name, c_code, numbers, change_cb, back_cb):
    """Number card exactly like the video: 'Facebook : 🇨🇲 CAMEROON / Wait Here Your OTP Coming Soon 🔔' + tap-to-copy numbers."""
    canon = next((a for a in PREMIUM_APPS if a.lower() == str(service_name).lower()), None) or detect_service("", service_name)
    info = PREMIUM_APPS.get(canon, PREMIUM_APPS["Other"])
    disp = canon if canon != "Other" else str(service_name).strip().title()
    first = str(numbers[0]).lstrip("+") if numbers else str(c_code)
    _, c_name = get_country_info(first)
    flag, _, cname = c_name.partition(" ")
    cname = cname or flag
    flag_id = GLOBAL_BODY_EMOJIS.get(flag)
    flag_html = _temoji(flag_id, flag) if flag_id else flag
    _iso = flag_iso(flag); _iso_txt = f" ({_iso})" if _iso != "WW" else ""
    msg = (f'{_temoji(info["id"], info["emoji"])} {flag_html} <b>{html.escape(cname.title())}{_iso_txt}</b> <b>Allocated</b> {_temoji("ok", "✅")}\n'
           f'{_temoji("hour", "⏳")} <b>Waiting for OTP........</b>')
    kb = {"inline_keyboard": [[{"text": disp, "callback_data": "noop", "icon_custom_emoji_id": info["id"], "style": "primary"}]]}
    for _i, n in enumerate(numbers):
        kb["inline_keyboard"].append([_num_button("+" + str(n).lstrip("+"), flag_id, _i), _code_button(n)])
    kb["inline_keyboard"].append([
        {"text": "Remove CC", "callback_data": "rcc_rm", "icon_custom_emoji_id": IC["ban"], "style": "primary"},
        {"text": "Change Number", "callback_data": change_cb, "icon_custom_emoji_id": IC["change"], "style": "danger"},
    ])
    kb["inline_keyboard"].append([
        {"text": "Change Country", "callback_data": back_cb, "icon_custom_emoji_id": IC["globe"], "style": "danger"},
        {"text": "OTP Group", "url": otp_button_link, "icon_custom_emoji_id": IC["shield"], "style": "primary"},
    ])
    _rl = range_group_button()
    if _rl:
        kb["inline_keyboard"].append([{"text": "Range Group", "url": _rl, "icon_custom_emoji_id": IC["grp"], "style": "primary"}])
    if WEBAPP_URL:
        kb["inline_keyboard"].append([{"text": "OPEN LIVE OTP APP", "icon_custom_emoji_id": IC["traffic"], "web_app": {"url": WEBAPP_URL}, "style": "primary"}])
    return msg, kb

_BTN_STYLE = {"Change Number": ("danger", IC["change"]), "Change Country": ("danger", IC["globe"]),
              "OTP Group": ("primary", IC["shield"]), "Range Group": ("primary", IC["grp"]),
              "Remove CC": ("primary", IC["ban"]), "Add CC": ("primary", IC["ban"])}

def toggle_remove_cc(rows, removing, cc=None):
    """Rebuild the number-card keyboard with / without the country code (state is read from the message itself)."""
    if cc is None:
        for row in rows:
            for b in row:
                if "copy_text" in b:
                    cc = get_country_info(re.sub(r"\D", "", b["copy_text"]["text"]))[0]; break
            if cc: break
    flag_id = None
    if cc:
        flag_id = GLOBAL_BODY_EMOJIS.get(get_country_info(cc)[1].split(" ", 1)[0])
    out = []; _ni = 0
    for row in rows:
        nr = []
        for b in row:
            b = dict(b)
            t = b.get("text", "")
            if "copy_text" in b:
                digits = re.sub(r"\D", "", b["copy_text"]["text"])
                new = (digits[len(cc):] if (cc and digits.startswith(cc)) else digits) if removing else ("+" + (cc or "") + digits)
                b = _num_button(new, flag_id, _ni); _ni += 1
            elif t in ("Remove CC", "Add CC"):
                b["text"] = "Add CC" if removing else "Remove CC"
                b["callback_data"] = f"rcc_add_{cc}" if removing else "rcc_rm"
                b["style"], b["icon_custom_emoji_id"] = _BTN_STYLE["Remove CC"]
            elif t in _BTN_STYLE:
                b["style"], b["icon_custom_emoji_id"] = _BTN_STYLE[t]
            elif b.get("callback_data") == "noop":
                b.setdefault("style", "primary")
                app = PREMIUM_APPS.get(next((a for a in PREMIUM_APPS if a.lower() == t.lower()), "Other"))
                if app: b["icon_custom_emoji_id"] = app["id"]
            nr.append(b)
        out.append(nr)
    return {"inline_keyboard": out}

def build_otp_rich(service_name, raw_number, message_text):
    """OTP group post exactly like the video: heading + bordered table (PREFIX / NUMBER / OTP) + code pill."""
    c_code, c_name = get_country_info(raw_number)
    flag = c_name.split(" ", 1)[0]
    iso = flag_iso(flag)
    app = PREMIUM_APPS.get(service_name, PREMIUM_APPS["Other"])
    num = re.sub(r"\D", "", str(raw_number))
    prefix = num[:6]
    masked = ("+" + "•" * 6 + num[-2:]) if len(num) >= 8 else ("+" + num)
    otp = extract_otp_code(message_text)
    snippet = html.escape(re.sub(r"\s+", " ", str(message_text)).strip())[:80]
    flag_id = GLOBAL_BODY_EMOJIS.get(flag)
    flag_html = _temoji(flag_id, flag) if flag_id else flag
    head = (f'{flag_html} {iso} · {_temoji(app["id"], app["emoji"])} {html.escape(str(service_name).upper())} · {detect_lang(message_text)}')
    return (
        f'<h3>{head}</h3>'
        f'<table bordered striped>'
        f'<tr><td>{_temoji("arrow", "➤")} PREFIX</td><td>{prefix}</td></tr>'
        f'<tr><td>{_temoji("phone", "📱")} NUMBER</td><td>{html.escape(masked)}</td></tr>'
        f'<tr><td>{_temoji("chat", "💬")} OTP</td><td>{html.escape(otp)}</td></tr>'
        f'</table>'
        f'<p><code>{snippet}</code></p>'
    )

class Loader:
    """Smooth animated loading message that runs while real work happens.
    title "Processing..." gives the compact video style: '⚙ Processing... Finding Number...'"""
    BAR = ["▰▱▱▱▱▱", "▰▰▱▱▱▱", "▰▰▰▱▱▱", "▰▰▰▰▱▱", "▰▰▰▰▰▱", "▰▰▰▰▰▰", "▱▰▰▰▰▰", "▱▱▰▰▰▰", "▱▱▱▰▰▰", "▱▱▱▱▰▰", "▱▱▱▱▱▰", "▱▱▱▱▱▱"]
    SPIN = ["◜", "◠", "◝", "◞", "◡", "◟"]

    def __init__(self, chat_id, message_id, title, steps, delay=0.45):
        self.chat_id, self.message_id, self.title, self.steps, self.delay = chat_id, message_id, title, steps, delay
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def _run(self):
        i = 0
        while not self._stop.is_set() and i < 14:
            step = self.steps[min(i // 2, len(self.steps) - 1)]
            if self.title == "Processing...":
                dots = "." * (i % 3 + 1)
                txt = f'{_temoji("gear", "⚙️")} <b>Processing{dots}</b> <i>{step}{dots}</i>'
            else:
                txt = (f"<blockquote>✨ <b>{self.title}</b></blockquote>\n"
                       f"{self.SPIN[i % len(self.SPIN)]}  <code>{self.BAR[i % len(self.BAR)]}</code>\n"
                       f"<i>{step}</i>")
            edit_message(self.chat_id, self.message_id, txt)
            i += 1
            self._stop.wait(self.delay)

    def stop(self):
        self._stop.set()
        try: self._t.join(timeout=3)
        except Exception: pass

    def __init__(self, chat_id, message_id, title, steps, delay=0.45):
        self.chat_id, self.message_id, self.title, self.steps, self.delay = chat_id, message_id, title, steps, delay
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def _run(self):
        i = 0
        while not self._stop.is_set() and i < 14:
            step = self.steps[min(i // 2, len(self.steps) - 1)]
            txt = (f"<blockquote>✨ <b>{self.title}</b></blockquote>\n"
                   f"{self.SPIN[i % len(self.SPIN)]}  <code>{self.BAR[i % len(self.BAR)]}</code>\n"
                   f"<i>{step}</i>")
            edit_message(self.chat_id, self.message_id, txt)
            i += 1
            self._stop.wait(self.delay)

    def stop(self):
        self._stop.set()
        try: self._t.join(timeout=3)
        except Exception: pass

def copy_message(chat_id, from_chat_id, message_id, reply_markup=None):
    payload = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    if reply_markup: payload["reply_markup"] = adapt_markup(reply_markup)
    try: return requests.post(BASE_URL + "copyMessage", json=payload).json()
    except Exception: return None

def get_chat_details(chat_identifier):
    chat_identifier = str(chat_identifier).strip()
    if chat_identifier.startswith("https://t.me/"):
        chat_identifier = "@" + chat_identifier.split("/")[-1]
    elif not chat_identifier.startswith("@") and not chat_identifier.startswith("-100"):
        chat_identifier = "@" + chat_identifier
        
    url = BASE_URL + "getChat"
    res = requests.get(url, params={"chat_id": chat_identifier}).json()
    if res.get("ok"):
        chat = res["result"]
        chat_data = {
            "id": chat["id"],
            "username": "@" + chat["username"] if "username" in chat else "",
            "title": chat.get("title", "Private Channel"),
            "invite_link": chat.get("invite_link", "")
        }
        if not chat_data["invite_link"]:
            inv_res = requests.post(BASE_URL + "exportChatInviteLink", json={"chat_id": chat["id"]}).json()
            if inv_res.get("ok"):
                chat_data["invite_link"] = inv_res["result"]
        return chat_data
    return None

def is_user_joined(user_id):
    if not force_join_status or not force_join_channels: return True
    if user_id == ADMIN_ID: return True
    
    for ch in force_join_channels:
        try:
            res = requests.get(BASE_URL + "getChatMember", params={"chat_id": ch["id"], "user_id": user_id}).json()
            if res.get("ok"):
                status = res["result"]["status"]
                if status in ["left", "kicked"]: return False
            else:
                return False
        except: return False
    return True

# ==========================================
# Global Emojis Map
# ==========================================
GLOBAL_BODY_EMOJIS = {
    "➖": "5870818207383686839", "🚫": "5334807341109908955", "😒": "5334763399299506604",
    "🖥": "5334880948259427772", "🌐": "5334590977837403844", "🌟": "5337102391244263212",
    "🕓": "5336983442125001376", "⌛": "5337172996211648018", "💬": "5337302974806922068",
    "🔐": "5337255927735163754", "🍏": "5337132498965010628", "❔": "5336850036145823599",
    "⚠️": "5336944168944047463", "🔥": "5337267511261960341", "💸": "5429612421977253466",
    "🥚": "5348390922507817684", "👨‍⚖": "5334763399299506604", "🐁": "5348494358205207761",
    "🧻": "5348486915026884464", "⚗": "5346311574221000149", "🛴": "5348075478634766440",
    "📊": "5353032893096567467", "🔢": "5226929552319594190", "👤": "5352861489541714456",
    "📁": "5352721946054268944", "🚀": "5352597830089347330", "💎": "5352838545826420397",
    "📍": "5352922460897452503", "👋": "5353027129250453493", "✅": "5352694861990501856",
    "1️⃣": "5352651766288652742", "2️⃣": "5355186458418257716", "3️⃣": "5352867219028091093",
    "4️⃣": "5352566657216714037", "5️⃣": "5353086880835474989", "6️⃣": "5354859211975071385",
    "7️⃣": "5352859127309707652", "8️⃣": "5352957533600389988", "9️⃣": "5353060913463204207",
    "🔤": "5352727417842606016", "📣": "5352980533150259581", "📤": "5353001161878182134",
    "✨": "5352552689983067014", "🔹": "5352638632278660622", "🎙": "5355102594886833928",
    "💴": "5352985330628730418", "📅": "5352585194295564660", "📴": "5352974971167611327",
    "✏️": "5395444784611480792", "📱": "5337132498965010628", "🔗": "5420517437885943844",
    "❌": "5420130255174145507", "⚙️": "5420155432272438703", "🫂": "5420145051336485498",
    "➕": "5420323438508155202", "🗑": "5422557736330106570", "🎁": "5420396762189831222",
    "➤": "5420618897898381296", "🏢": "5420156334215565595", "💳": "5190899075968441286",
    "📝": "5192739271886282680", "🛡": "5190447043545438788", "🤝": "5192805934073685937",
    "💰": "5190576863226933563", "👀": "5190645917711114179", "🕹": "5193100774988617665",
    "🟢": "5192812028632274956", "🧪": "5190781475468915802", "🎨": "5190751148704833975",
    "📂": "5257969839313526622", "🌍": "5780471598922337683", "📌": "5318986077455795572",
    "📢": "5789428375261023681", "🆔": "5226929552319594190", "📈": "5352877703043258544",
    "🔔": "5352980533150259581", "🏦": "5429612421977253466", "🧾": "5192739271886282680",
    "👨‍⚖️": "5334763399299506604", "🔍": "5463352748751753567", "🔑": "5197288647275071607",
    "🏆": "5240021484516185513", "🚦": "5429571662737611814", "👥": "5420145051336485498", 
    "🎯": "5352922460897452503", "📶": "5429353834881261942", "⏳": "5337172996211648018", 
    "🔸": "5429576112323732785", "🎉": "5420396762189831222", "💵": "5429612421977253466", 
    "👑": "5352838545826420397", "↳": "5420618897898381296",
    "🇺🇸": "5913463998522592692", "🇺🇦": "5911406692007941050", "🇵🇱": "5913550391789752571",
    "🇰🇿": "5913724621433082323", "🇨🇳": "5913779335021466780", "🇦🇿": "5911197578640233518",
    "🇪🇺": "5911106310585193018", "🇦🇲": "5913272455866093666", "🇷🇺": "5913274246867456342",
    "🇺🇿": "5911051846104912282", "🇩🇪": "5911096835887337583", "🇯🇵": "5913293711659241040",
    "🇹🇷": "5910995113881901195", "🇧🇾": "5911011185649521599", "🇬🇧": "5913443365499703513",
    "🇮🇳": "5913754823643107921", "🇧🇷": "5911148568768418614", "🇿🇲": "5913564754160389778",
    "🇾🇪": "5913346492512341993", "🏴󠁧󠁢󠁷󠁬󠁳󠁿": "5911297801702084799", "🇻🇳": "5913428887164949581",
    "🇻🇦": "5911211932420938860", "🇻🇺": "5913511535220625585", "🇺🇾": "5913623088406204470",
    "🇦🇪": "5913726554168365343", "🇺🇬": "5913488939397681980", "🇹🇲": "5913315521503170180",
    "🇹🇳": "5911332947419468671", "🇹🇹": "5911228635548750294", "🇹🇬": "5913423260757790970",
    "🇹🇭": "5913617968805187987", "🇹🇿": "5911418949844603556", "🇹🇯": "5911287639809463107",
    "🇨🇭": "5913271227505448072", "🇸🇪": "5911156510162949403", "🇸🇿": "5913374525763883286",
    "🇸🇷": "5913275539652611719", "🇸🇩": "5911387497799094470", "🇪🇸": "5911193287967904547",
    "🇱🇰": "5911293163137406640", "🇸🇸": "5911406262511211744", "🇿🇦": "5911203119148044594",
    "🇸🇴": "5911397852965244436", "🇸🇧": "5911482712929080608", "🇸🇮": "5913431983836368644",
    "🇸🇰": "5913751666842145020", "🇸🇬": "5911531460808051849", "🇸🇱": "5911210450657218661",
    "🇸🇨": "5911185183364616913", "🇷🇸": "5913592598433369871", "🇸🇳": "5910995302860461643",
    "🏴󠁧󠁢󠁳󠁣󠁴󠁿": "5911460091336331851", "🇸🇹": "5913574331937462345", "🇸🇲": "5913587968458625465",
    "🇼🇸": "5913325971158602854", "🇰🇳": "5913691898077253637", "🇻🇨": "5911318941531116255",
    "🇱🇨": "5911243659344351824", "🇵🇸": "5913684768431541668", "🇷🇼": "5911455229433352234",
    "🇷🇴": "5913460373570195273", "🇶🇦": "5911260864983339619", "🇵🇷": "5911504350974317480",
    "🇵🇹": "5911023653939581472", "🇵🇭": "5911268638874145162", "🇵🇪": "5911207993935925780",
    "🇵🇾": "5911014265141072316", "🇵🇬": "5911107251183030903", "🇵🇦": "5913428968769327174",
    "🇵🇼": "5911283903187915549", "🇵🇰": "5913705895375672082", "🇴🇲": "5913570801474343473",
    "🇳🇴": "5913617397574537046", "🇳🇬": "5911143844304393105", "🇳🇪": "5911270086278124251",
    "🇳🇿": "5913640044937089340", "🇳🇱": "5913367645226275100", "🇳🇵": "5913496520014958723",
    "🇳🇦": "5911108535378252443", "🇲🇿": "5911333419865871464", "🇲🇦": "5911482111633658301",
    "🇲🇪": "5913239436157522151", "🇲🇳": "5911041383564580038", "🇲🇨": "5911245347266500057",
    "🇲🇩": "5913456847402045950", "🇲🇻": "5913501399097806832", "🇲🇱": "5911305266355245916",
    "🇲🇹": "5911023714069123567", "🇧🇲": "5913680005312811090", "🇲🇶": "5911378005921370347",
    "🇲🇭": "5913235935759175692", "🇲🇺": "5913291113204027321", "🇲🇽": "5913687302462246518",
    "🇫🇲": "5911271104185373336", "🇲🇾": "5913654360063087453", "🇰🇪": "5911154710571651231",
    "🇲🇬": "5913766918271012920", "🇲🇰": "5913394029210374721", "🇱🇺": "5913390842344640293",
    "🇱🇹": "5911172315642597775", "🇱🇮": "5911166650580734660", "🇱🇾": "5911236989260140996",
    "🇱🇷": "5913324167272337727", "🇰🇮": "5911294443037660118", "🇽🇰": "5911433681582429010",
    "🇰🇼": "5913290705182134003", "🇰🇬": "5911202161370337549", "🇱🇦": "5913718526874489279",
    "🇱🇻": "5913738489882480243", "🇱🇧": "5911504273664905447", "🇱🇸": "5911059881988723711",
    "🇮🇩": "5913479361620611038", "🇮🇷": "5911308891307643032", "🇮🇶": "5911382442622587735",
    "🇮🇪": "5913440715504881532", "🇮🇱": "5911471936856134692", "🇮🇹": "5913688444923547525",
    "🇯🇲": "5913232280742006526", "🇯🇴": "5913234136167878475", "🇮🇸": "5911047899029967246",
    "🇭🇺": "5913767635530551104", "🇭🇳": "5911406889576436289", "🇭🇹": "5913459789454643194",
    "🇬🇾": "5913579412883771480", "🇬🇼": "5911398694778836149", "🇬🇳": "5913471858312744319",
    "🇬🇹": "5913324858762072330", "🇬🇩": "5913228063084121946", "🇬🇷": "5911210399117611448",
    "🇬🇭": "5913391155877252952", "🇬🇪": "5913434771270144023", "🇬🇲": "5913657267755945883",
    "🇬🇦": "5911037896051137264", "🇫🇷": "5913605586414473124", "🇫🇮": "5911041344909873378",
    "🇫🇯": "5911393832875856716", "🇪🇹": "5911078333168227043", "🇩🇴": "5911152099231536123",
    "🇹🇱": "5911141915864076479", "🇪🇨": "5911273865849347408", "🇪🇬": "5913694831539916769",
    "🇸🇻": "5913238624408703010", "🏴󠁧󠁢󠁥󠁮󠁧󠁿": "5913475719488344315", "🇪🇪": "5910986042910969906",
    "🇩🇲": "5911377121158107430", "🇩🇯": "5911407709915190157", "🇩🇰": "5911206009661034712",
    "🇨🇾": "5911023550860366409", "🇭🇷": "5913692684056269311", "🇨🇷": "5911261745451635030",
    "🇨🇬": "5911338788574990168", "🇨🇩": "5913770362834783827", "🇰🇲": "5911338582416560604",
    "🇰🇭": "5913699998385573485", "🇨🇲": "5911172109484167745", "🇨🇦": "5913623736946265914",
    "🇨🇻": "5913571501554012193", "🇨🇫": "5913443245240619222", "🇹🇩": "5913299849167507310",
    "🇨🇿": "5911198691036764307", "🇨🇱": "5911470957603592832", "🇨🇴": "5913773060074246009",
    "🇧🇮": "5913766441529642752", "🇧🇼": "5911513782722499475", "🇧🇦": "5913700002680541032",
    "🇧🇴": "5913638795101606133", "🇧🇹": "5913236734623093021", "🇧🇯": "5913735869952430547",
    "🇦🇷": "5913573356979884082", "🇦🇺": "5913632326880858455", "🇦🇹": "5911338831524664592",
    "🇧🇸": "5911451643135660214", "🇧🇭": "5913581663446634403", "🇧🇩": "5911365056594973179",
    "🇧🇧": "5911016996740272263", "🇧🇪": "5913529642802745141", "🇧🇿": "5913355005137522807",
    "🇦🇬": "5913389025573475085", "🇦🇴": "5913753316109586411", "🇦🇩": "5911314702398396902",
    "🇩🇿": "5913782968563800236", "🇦🇱": "5911357458797826163", "🇦🇫": "5913492040364068694",
    "🇿🇼": "5911092502265336396", "🇨🇺": "5431551436502611633", "🇰🇵": "5434142701941437163",
    "🇻🇪": "5434009132753499322", "🇸🇾": "5433910876786670092", "🇲🇲": "5433666360003540231",
    "🇳🇮": "5334807849418003620", "🇰🇷": "5913371673905598425", "🇬🇶": "5911306279967529251",
    "🇬🇱": "5292014752283774878", "🇫🇴": "5296469342039327674", "🇨🇮": "5222233374948602940",
    "🇧🇳": "5911336409163109113", "🇧🇬": "5294329219965272288", "🇧🇫": "5913407764515786948",
    "🇪🇷": "5433723401464198287", "🇲🇼": "5433968339154122439", "🇲🇷": "5433859405898594234",
    "🇳🇷": "5434131139889478358", "🇸🇦": "4985897134424328239", "🇹🇴": "5433640100573491806",
    "🇹🇻": "5433684690923961019", "🇹🇼": "5366187256937726720", "🇭🇰": "5292166459118606932",
    "🇲🇴": "6323557758096377611"
}

def apply_emojis(text):
    hidden = []
    def hide(match):
        hidden.append(match.group(0))
        return f"__TG_EMOJI_{len(hidden)-1}__"
    text = re.sub(r'<tg-emoji[^>]*>.*?</tg-emoji>', hide, text)
    for char, eid in GLOBAL_BODY_EMOJIS.items():
        text = text.replace(char, f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>')
    for i, h in enumerate(hidden):
        text = text.replace(f"__TG_EMOJI_{i}__", h)
    return text

# ==========================================
# Premium Emoji Library (owner supplied pool)
# Future-e notun ID dite hole shudhu nicher _PE_PACKS e add korun.
# ==========================================
def _pe_ids(s):
    """Whitespace separated IDs -> de-duplicated list (order kept)."""
    return list(dict.fromkeys(s.split()))

_PE_PACKS = {
    "pack1": _pe_ids("""
6273636451271840147 6273627839862411998 6275933872228143219 6275805225072726715 6273833298212950992
6273836553798160975 6275821709157208262 6273840152980755328 6276250600296420994 6273660902520656812
6276206679960851369 6274026000510620496 6275831652006498759 6273943519958667519 6273899174421337911
6273903662662160701 6273648825072620903 6274007313107915274 6273918549018808282 6273835720574505901
6274000217821941951 6275870663194449243 6276184294591304440 6273997297244180325 6276225182679962762
6276215630672696826 6276153138898539096 6274075349684850643 6273532203825631831 6273561254984421826
6273524679042929818 6273989188345926558 6275976465418818017 6274048295685853461 5278229754099540071
5285350148451344065 5456166744479533085 5310076528577491230 5422502846648039176 5281024850096301559
5280815637944344694 5280735970595971856 5267388960291694083 5420539939219579641 5280591887328093175
5420329588901289204 5431618850309292766 5453866707888152017 5280869505424172134 5280623335078634360
"""),
    "pack2": _pe_ids("""
5278611117130653414 5298747646096187189 5422413713191744617 5280894678227492455 5280662183057825163
5303434898525136153 5266965772869053171 5305260190906471236 5296652101487696644 5298858555036672046
5296240686570414655 5296275986906620070 5269711060130079104 5303514492859065897 5303183810442044150
5397894054136520680 5296401966887345356 5296738331546098668 5456292668625687926 5303283556762526394
5303366226293040224 5303416490295304868 5463310606532644686 5427154326294376920 5231008469875706287
5434112216263576692 5281031958267176200 5278653723206231402 5280751174780199841 5456547209862482913
5280705635241959358 5280757711720423435 5456458746421088612 5230964098568571494 5312265175192056107
5231479898371019163 5278334035905486862 5456119615803395808 5454218272436165856 5280926521115025358
5281007837730841762 5278219081105811309 5307884768111631358 5307884136751439109 5303051181851943323
5307986296843541691
"""),
    "pack3": _pe_ids("""
5283112960116399950 5282870814155219459 5282852032263233269 5280883773305528071 5280566207718631192
5269758970490265542 5267346358511086054 5296258364655805333 5298640971993456864 5298588152485651370
5308010997200461648 5305680792758788581 5307734633234833786 5310180286397423397 5307900569296313923
5310023511501184484 5307606144993205843 5310263926590545944 5269674389699305645 5454370584861384827
5453898589430400486 5456421831177175816 5454292261837776725 5456502344634108251 5454065032298009119
5454261883534087465 5454313393076872799 5267128496294997633 6237975191784266396 6237610939902858402
6235655011796261649 6235336389647407554 6237568505625973857 6235748783817234507 6235485016990685581
5472057595193743789 5472266485223152215 5474143948572223102 5472105634402952328 5474181387802141134
5474294289607448720 5472102739594991482 5472057973150867581 5472191366245149325 5472092191155314021
5472372704059351699 5472101141867158867 5474264147526965147 5472379868064802149
"""),
    "pack4": _pe_ids("""
6073556477824472025 6071278787947925866 6071123877067494706 6071096140168696563 5084979757905347540
5084974483685507801 5082478549340783285 5084923566848213749 5085022089103016925 5084613633418199991
5082413149873767213 5116151848855667552 5104960787579929462 5107195471948940313 5100702284556272564
5098094273039959279 5098265504796115765 5100657930429006538 5084813564145828384 5116395218882528029
5116204921766544244 5116093437300442328 5116368680279606270 5116296632203216188 5116245732545790791
5116296400274981694 5893281616486209299 5774138454896022007 5893473283696759404 5866207164854439951
5866382725937632001 5866009583473924494 5866456517770745292 5866400713260666880 5866469651780736116
5866046133645611309 5866098858664137050 5866325903520305486 5865994057167146921 5866493836741579471
5865988791537242108 5866317158966890859 5866189817481531778 5866336335995867469 5866206774012416141
5866324451821358773 5866474230215873258 5866354903139487976 5866208942970901081 5866395366026384460
"""),
    "pack6": _pe_ids("""
5294236848103643477 5291937511591925566 5294077418917616055 5294202819077756005 5294048127240655242
5291994273879709721 5294215205763434181 5294516785482062829 5292186323342350940 5294005972136647964
5292208210495689627 5291978717508164018 5294007002928798927 5294444247779399477 5291975174160145850
5294323533428579078 5294031587321600012 5294108398516720753 5291824687096027834 5294526187165471742
5294134426018536120 5291774466043435275 5294171848068584842 5293984969746566866 5294121983498277263
5294201479047957700 5294026179957772585 5291892229751723900 5292098293692650297 5294308947719640437
5294153164960848949 5294051631933967760 5294225191562400452 5291997306126626950 5292290347450259214
5292203503211535593 5294210571493724819 5291780728105753403 5294231037012888049 5294068833277990704
5294010206974397371 5294351381996521508 5294035229453865597 5292098684534675100 5292063805105263554
5293991322003200135 5291999676948569127 5291963947115631526 5294062721539526918 5294242852467923382
"""),
    "pack7": _pe_ids("""
5294531860817268837 5294127214768468283 5294485513825178032 5294522197140857947 5292083733753517221
5293992082212409502 5294337307388695687 5294410107084365278 5292170045416297012 5291922054004625949
5291951143818123103 5292245976143124155 5291992809295861098 5292055799286224027 5294399820637688352
5292014752283774878 5294049961191690629 5291817660529533837 5294321325815389139 5294349389131697267
5292013274815028523 5294347396266873249 5291948395039054764 5294409819321550432 5294336633078831209
5291892096607739008 5292062692708736193 5292045130587462814 5291901034434682297 5292166459118606932
5294229581018975260 5294354358408859664 5291933173674957761 5294220170745630736 5294325010897327367
5294471971793293647 5294318478252070646 5294069056616289553 5291826830284709120 5294505107465982830
5291799063321139445 5291950280529697493 5291988613112814801 5294227175837290463 5292111852904416801
5294538934628405146 5294193812531333564 5294408281723262763 5292066437920218075 5292091954320922577
"""),
    "pack8": _pe_ids("""
5291981530711746037 5292236016113966127 5294193108156699621 5292040693886247604 5291793810576137439
5291858711826946840 5292048742654957785 5294343084119708700 5294423709245787718 5294023611567332075
5291991568050312348 5294241881805312589 5291858351049696702 5292004203844097218 5292086972158858331
5294532213004588353 5294180730060954484 5294429743674840973 5294127824653797277 5294535073452809778
5291838156113470124 5294158486425325375 5294378161117614233 5294316532631883496 5292108962391414885
5294086708931874940 5294254478944393569 5292021761670404922 5294463274484521342 5294458756178924088
5291917797692042265 5294189019347833274 5294240825243358100 5291809418487290691 5294456308047563965
5294471336138134209 5291761718580502030 5291813666209946812 5291825606219029010 5294289826525238172
5291959935616178405 5291917995260533077 5294525611639852679 5291798075478661634 5292099427564018941
5292190970496963836 5294436555492973610 5292121516580820347 5292166360334357676 5294107724206856227
"""),
    "pack9": _pe_ids("""
5294335323113807278 5294191265615729158 5292147350809106831 5292183188016222701 5294163983983463099
5294434665707368018 5292087023698466689 5294458584380230360 5291891186074672309 5294494314213167952
5294451304410663668 5294538440707166931 5294279359689938006 5294283890880433237 5294058817414255960
5294325281480266304 5294513087515216901 5292102670264328257 5294177148058228060 5294396668131692138
5294312482477724867 5291737091238026321 5291791748991835084 5294013428199869487 5294095745543069603
5294120269806328883 5292146096678658977 5293994384314882755 5294097669688415562 5294283689016973348
5294362935458548705 5294484680601521871 5293993400767367408 5294098958178603764 5294320866253884749
5294244076533600593 5294192317882716626 5294314831824835370 5293993521026453119 5294263837678131580
5294448585696368047 5294217645304864345 5291928449210932974 5294476442854247878 5294235963340379688
5294228039125718124 5294139949346476093 5294058972033076492 5294100109229838880 5294422158762592930
"""),
}

# Colored-circle emojis: (fallback_emoji, id)
_PE_COLOR_CIRCLES = [
    ("🔵", "5983593169424487129"), ("🟤", "5983467112134349032"), ("🟢", "5983322513470395165"),
    ("🟠", "5983306454587674702"), ("🔵", "5994422667468345454"), ("🟠", "5994527743843245305"),
    ("🔵", "5994461236274663880"), ("🟢", "5994302623132421837"), ("🔵", "5994385765109338648"),
    ("🔵", "5994339800369335877"), ("🔵", "5994312475787399544"), ("🔴", "5996723223750774974"),
    ("🔵", "6003394871329558277"), ("🔵", "6012491255580201216"), ("🟣", "6025938214068426027"),
    ("🔵", "6028146767856406850"), ("🟠", "6037240175694712667"), ("🔵", "5780694945811666229"),
    ("🔵", "5796266362628215828"), ("🟠", "5771550071380253630"), ("🔵", "5776155096660316678"),
    ("⚫️", "5776385062094246730"), ("🟢", "5828112720494335599"), ("🟣", "5837045681239363644"),
    ("⚫️", "5848268233215316408"), ("🟣", "5883996631631664390"), ("🟢", "5893009495948267487"),
    ("🟡", "5908878390789674543"), ("⚫️", "5942809650335323342"), ("🟣", "5942595842568359981"),
    ("🔵", "5978715598404719436"), ("🔵", "6003621843171284367"),
]

# All "✨" pack IDs in one flat de-duplicated list
PREMIUM_EMOJI_POOL = list(dict.fromkeys(i for ids in _PE_PACKS.values() for i in ids))
PREMIUM_EMOJI_COLORS = {}   # fallback emoji -> [ids]
for _fb, _id in _PE_COLOR_CIRCLES:
    PREMIUM_EMOJI_COLORS.setdefault(_fb, []).append(_id)

def pe_tag(eid, fallback="✨"):
    """<tg-emoji> HTML tag for any premium emoji ID."""
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

def pe_random(fallback="✨", pack=None):
    """Random premium emoji tag (optionally from one pack, e.g. pack='pack6')."""
    ids = _PE_PACKS.get(pack) if pack else PREMIUM_EMOJI_POOL
    return pe_tag(random.choice(ids or PREMIUM_EMOJI_POOL), fallback)

def pe_color(fallback="🔵", index=None):
    """Premium colored circle tag. fallback = 🔵 🟢 🟠 🟣 🔴 🟡 ⚫️ 🟤"""
    ids = PREMIUM_EMOJI_COLORS.get(fallback) or [i for v in PREMIUM_EMOJI_COLORS.values() for i in v]
    eid = ids[index % len(ids)] if index is not None else random.choice(ids)
    return pe_tag(eid, fallback)

def pe_id(n, pack=None):
    """n-th premium emoji ID (stable, wraps around). Use as icon_custom_emoji_id on buttons."""
    ids = _PE_PACKS.get(pack) if pack else PREMIUM_EMOJI_POOL
    ids = ids or PREMIUM_EMOJI_POOL
    return ids[n % len(ids)]

# ==========================================
# Premium Apps & Service Keywords Setup
# ==========================================
PREMIUM_APPS = {
    "Facebook": {"emoji": "📘", "id": "5280815637944344694"},
    "WhatsApp": {"emoji": "📗", "id": "5280735970595971856"},
    "Telegram": {"emoji": "✈️", "id": "5280662183057825163"},
    "Instagram": {"emoji": "📸", "id": "5281024850096301559"},
    "Google": {"emoji": "🔍", "id": "5429558795015593080"},
    "Microsoft": {"emoji": "🪟", "id": "5979047775470358891"},
    "TikTok": {"emoji": "🎵", "id": "5280662183057825163"},
    "Bkash": {"emoji": "💸", "id": "5431552076452766597"},
    "Binance": {"emoji": "🪙", "id": "5429438106434575354"},
    "Snapchat": {"emoji": "👻", "id": "5429398137468919845"},
    "Uber": {"emoji": "🚗", "id": "5298715455316303708"},
    "Discord": {"emoji": "🎮", "id": "5429594374524676604"},
    "Amazon": {"emoji": "📦", "id": "5431358656895568355"},
    "Viber": {"emoji": "💜", "id": "5429470576387335368"},
    "Twitter": {"emoji": "🐦", "id": "5431899135580087595"},
    "Netflix": {"emoji": "🎬", "id": "5431641055290235797"},
    "Spotify": {"emoji": "🎧", "id": "5429235839244739644"},
    "Foodpanda": {"emoji": "🐼", "id": "5336879280578138635"},
    "Pathao": {"emoji": "🛵", "id": "5336879280578138635"},
    "ChatGPT": {"emoji": "🤖", "id": "5429503209548847635"},
    "Imo": {"emoji": "💭", "id": "5431378937731129128"},
    "Funny": {"emoji": "😂", "id": "6273660902520656812"},
    "Mod": {"emoji": "🛡️", "id": "6276250600296420994"},
    "Other": {"emoji": "🌐", "id": "5429353834881261942"}
}

SERVICE_SMS_KEYWORDS = {
    "Facebook": ["facebook", "fb code", "fb", "meta"],
    "WhatsApp": ["whats", "whatsapp", "whatsapp code"],
    "Telegram": ["telegram", "tg code"],
    "Instagram": ["instagram", "ig code"],
    "Google": ["google", "Googl"],
    "Microsoft": ["microsoft", "micro", "Microsof", "xbox"],
    "TikTok": ["tiktok", "tik tok"],
    "Bkash": ["bkash"],
    "Binance": ["binance", "bnb"],
    "Snapchat": ["snapchat", "snap"],
    "Uber": ["uber"],
    "Discord": ["discord"],
    "Amazon": ["amazon", "aws"],
    "Viber": ["viber"],
    "Twitter": ["twitter", "x.com"],
    "Netflix": ["netflix"],
    "Spotify": ["spotify"],
    "Foodpanda": ["foodpanda", "panda"],
    "Pathao": ["pathao"],
    "ChatGPT": ["openai", "chatgpt"],
    "Imo": ["imo code", "imo"],
    "Funny": ["funny"],
    "Mod": ["mod"]
}

COUNTRY_CODES = {
    "1": "🇺🇸 USA/Canada", "380": "🇺🇦 Ukraine", "48": "🇵🇱 Poland", "7": "🇰🇿 Kazakhstan",
    "86": "🇨🇳 China", "994": "🇦🇿 Azerbaijan", "374": "🇦🇲 Armenia", "79": "🇷🇺 Russia",
    "998": "🇺🇿 Uzbekistan", "49": "🇩🇪 Germany", "81": "🇯🇵 Japan", "90": "🇹🇷 Turkey",
    "375": "🇧🇾 Belarus", "44": "🇬🇧 United Kingdom", "91": "🇮🇳 India", "55": "🇧🇷 Brazil",
    "260": "🇿🇲 Zambia", "967": "🇾🇪 Yemen", "84": "🇻🇳 Vietnam", "379": "🇻🇦 Vatican City",
    "678": "🇻🇺 Vanuatu", "598": "🇺🇾 Uruguay", "971": "🇦🇪 UAE", "256": "🇺🇬 Uganda",
    "993": "🇹🇲 Turkmenistan", "216": "🇹🇳 Tunisia", "228": "🇹🇬 Togo", "66": "🇹🇭 Thailand",
    "255": "🇹🇿 Tanzania", "992": "🇹🇯 Tajikistan", "41": "🇨🇭 Switzerland", "46": "🇸🇪 Sweden",
    "268": "🇸🇿 Eswatini", "597": "🇸🇷 Suriname", "249": "🇸🇩 Sudan", "34": "🇪🇸 Spain",
    "94": "🇱🇰 Sri Lanka", "211": "🇸🇸 South Sudan", "27": "🇿🇦 South Africa", "252": "🇸🇴 Somalia",
    "677": "🇸🇧 Solomon Islands", "386": "🇸🇮 Slovenia", "421": "🇸🇰 Slovakia", "65": "🇸🇬 Singapore",
    "232": "🇸🇱 Sierra Leone", "248": "🇸🇨 Seychelles", "381": "🇷🇸 Serbia", "221": "🇸🇳 Senegal",
    "239": "🇸🇹 Sao Tome", "378": "🇸🇲 San Marino", "685": "🇼🇸 Samoa", "970": "🇵🇸 Palestine",
    "250": "🇷🇼 Rwanda", "40": "🇷🇴 Romania", "974": "🇶🇦 Qatar", "351": "🇵🇹 Portugal",
    "63": "🇵🇭 Philippines", "51": "🇵🇪 Peru", "595": "🇵🇾 Paraguay", "675": "🇵🇬 Papua New Guinea",
    "507": "🇵🇦 Panama", "680": "🇵🇼 Palau", "92": "🇵🇰 Pakistan", "968": "🇴🇲 Oman",
    "47": "🇳🇴 Norway", "234": "🇳🇬 Nigeria", "227": "🇳🇪 Niger", "64": "🇳🇿 New Zealand",
    "31": "🇳🇱 Netherlands", "977": "🇳🇵 Nepal", "264": "🇳🇦 Namibia", "258": "🇲🇿 Mozambique",
    "212": "🇲🇦 Morocco", "382": "🇲🇪 Montenegro", "976": "🇲🇳 Mongolia", "377": "🇲🇨 Monaco",
    "373": "🇲🇩 Moldova", "960": "🇲🇻 Maldives", "223": "🇲🇱 Mali", "356": "🇲🇹 Malta",
    "596": "🇲🇶 Martinique", "692": "🇲🇭 Marshall Islands", "230": "🇲🇺 Mauritius", "52": "🇲🇽 Mexico",
    "691": "🇫🇲 Micronesia", "60": "🇲🇾 Malaysia", "254": "🇰🇪 Kenya", "261": "🇲🇬 Madagascar",
    "389": "🇲🇰 North Macedonia", "352": "🇱🇺 Luxembourg", "370": "🇱🇹 Lithuania", "423": "🇱🇮 Liechtenstein",
    "218": "🇱🇾 Libya", "231": "🇱🇷 Liberia", "686": "🇰🇮 Kiribati", "383": "🇽🇰 Kosovo",
    "965": "🇰🇼 Kuwait", "996": "🇰🇬 Kyrgyzstan", "856": "🇱🇦 Laos", "371": "🇱🇻 Latvia",
    "961": "🇱🇧 Lebanon", "266": "🇱🇸 Lesotho", "62": "🇮🇩 Indonesia", "98": "🇮🇷 Iran",
    "964": "🇮🇶 Iraq", "353": "🇮🇪 Ireland", "972": "🇮🇱 Israel", "39": "🇮🇹 Italy",
    "962": "🇯🇴 Jordan", "354": "🇮🇸 Iceland", "36": "🇭🇺 Hungary", "504": "🇭🇳 Honduras",
    "509": "🇭🇹 Haiti", "592": "🇬🇾 Guyana", "245": "🇬🇼 Guinea-Bissau", "224": "🇬🇳 Guinea",
    "502": "🇬🇹 Guatemala", "30": "🇬🇷 Greece", "233": "🇬🇭 Ghana", "995": "🇬🇪 Georgia",
    "220": "🇬🇲 Gambia", "241": "🇬🇦 Gabon", "33": "🇫🇷 France", "358": "🇫🇮 Finland",
    "679": "🇫🇯 Fiji", "251": "🇪🇹 Ethiopia", "670": "🇹🇱 Timor-Leste", "593": "🇪🇨 Ecuador",
    "20": "🇪🇬 Egypt", "503": "🇸🇻 El Salvador", "372": "🇪🇪 Estonia", "253": "🇩🇯 Djibouti",
    "45": "🇩🇰 Denmark", "357": "🇨🇾 Cyprus", "385": "🇭🇷 Croatia", "506": "🇨🇷 Costa Rica",
    "242": "🇨🇬 Congo", "243": "🇨🇩 DR Congo", "269": "🇰🇲 Comoros", "855": "🇰🇭 Cambodia",
    "237": "🇨🇲 Cameroon", "238": "🇨🇻 Cape Verde", "236": "🇨🇫 CAR", "235": "🇹🇩 Chad",
    "420": "🇨🇿 Czechia", "56": "🇨🇱 Chile", "57": "🇨🇴 Colombia", "257": "🇧🇮 Burundi",
    "267": "🇧🇼 Botswana", "387": "🇧🇦 Bosnia", "591": "🇧🇴 Bolivia", "975": "🇧🇹 Bhutan",
    "229": "🇧🇯 Benin", "54": "🇦🇷 Argentina", "61": "🇦🇺 Australia", "43": "🇦🇹 Austria",
    "973": "🇧🇭 Bahrain", "880": "🇧🇩 Bangladesh", "32": "🇧🇪 Belgium", "501": "🇧🇿 Belize",
    "244": "🇦🇴 Angola", "376": "🇦🇩 Andorra", "213": "🇩🇿 Algeria", "355": "🇦🇱 Albania",
    "93": "🇦🇫 Afghanistan", "263": "🇿🇼 Zimbabwe", "53": "🇨🇺 Cuba", "850": "🇰🇵 North Korea",
    "58": "🇻🇪 Venezuela", "963": "🇸🇾 Syria", "95": "🇲🇲 Myanmar", "505": "🇳🇮 Nicaragua",
    "82": "🇰🇷 South Korea", "240": "🇬🇶 Equatorial Guinea", "299": "🇬🇱 Greenland", "298": "🇫🇴 Faroe Islands",
    "225": "🇨🇮 Ivory Coast", "673": "🇧🇳 Brunei", "359": "🇧🇬 Bulgaria", "226": "🇧🇫 Burkina Faso",
    "291": "🇪🇷 Eritrea", "265": "🇲🇼 Malawi", "222": "🇲🇷 Mauritania", "674": "🇳🇷 Nauru",
    "966": "🇸🇦 Saudi Arabia", "676": "🇹🇴 Tonga", "688": "🇹🇻 Tuvalu", "886": "🇹🇼 Taiwan",
    "852": "🇭🇰 Hong Kong", "853": "🇲🇴 Macau", "297": "🇦🇼 Aruba", "599": "🇨🇼 Curacao",
    "500": "🇫🇰 Falkland Islands", "594": "🇬🇫 French Guiana", "590": "🇬🇵 Guadeloupe", "262": "🇷🇪 Reunion",
    "687": "🇳🇨 New Caledonia", "683": "🇳🇺 Niue", "672": "🇳🇫 Norfolk Island", "681": "🇼🇫 Wallis and Futuna",
    "682": "🇨🇰 Cook Islands", "689": "🇵🇫 French Polynesia", "350": "🇬🇮 Gibraltar", "508": "🇵🇲 Saint Pierre"
}

def get_country_info(range_str):
    clean_range = str(range_str or "").replace("X", "")
    for code, name in sorted(COUNTRY_CODES.items(), key=lambda x: len(x[0]), reverse=True):
        if clean_range.startswith(code): return code, name
    return clean_range[:3], f"🏳️ Other (+{clean_range[:3]})"

# ==========================================
# OTP Forwarding Pipeline 
# ==========================================

def detect_service(message_text, raw_sid=""):
    msg_lower = str(message_text).lower()
    sid_lower = str(raw_sid).lower()
    
    for service, keywords in SERVICE_SMS_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf'\b{re.escape(kw)}\b', msg_lower) or kw in msg_lower:
                return service
                
    for service in PREMIUM_APPS.keys():
        if service.lower() in sid_lower:
            return service
            
    return "Other"

def get_service_info_html(service_name):
    app_info = PREMIUM_APPS.get(service_name, PREMIUM_APPS["Other"])
    emoji_id = app_info["id"]
    normal_emoji = app_info["emoji"]
    short_name = service_name.upper()
    if short_name == "FACEBOOK": short_name = "FB"
    if short_name == "WHATSAPP": short_name = "WS"
    if short_name == "INSTAGRAM": short_name = "IG"
    
    return f'<tg-emoji emoji-id="{emoji_id}">{normal_emoji}</tg-emoji> <b>{html.escape(short_name)}</b>', emoji_id

def extract_otp_code(message_text):
    # 🌟 স্পেস বা হাইফেনসহ ৩-৮ ডিজিটের কোড ধরার লজিক
    match = re.search(r'\b(\d{3}[\s-]?\d{3,4}|\d{4,8})\b', str(message_text))
    if match: return match.group(1)
    return "COPY"

def deliver_to_inbox(user_id, service_name, raw_number, msg_text, current_balance, reward):
    c_code, c_name = get_country_info(raw_number)
    service_html, srv_eid = get_service_info_html(service_name)
    clean_raw_number = str(raw_number).lstrip('+') 
    
    otp = extract_otp_code(msg_text)
    text = (
        f"<blockquote><tg-emoji emoji-id=\"5352694861990501856\">✅</tg-emoji> <b>OTP RECEIVED</b></blockquote>\n"
        f"{service_html} ➜ <code>+{clean_raw_number}</code>\n"
        f"— — — — — — — — — —\n"
        f"<tg-emoji emoji-id=\"5337255927735163754\">🔐</tg-emoji> <b>CODE</b> ➜ <code>{html.escape(otp)}</code>\n"
        f"<blockquote><tg-emoji emoji-id=\"5420323438508155202\">➕</tg-emoji> <b>ADDED</b>  ➜ {reward:.2f} TK\n"
        f"<tg-emoji emoji-id=\"5190899075968441286\">💳</tg-emoji> <b>BALANCE</b> ➜ {current_balance:.2f} TK</blockquote>"
    )
    rows = [[{"text": f"{otp}", "icon_custom_emoji_id": srv_eid, "copy_text": {"text": otp}, "style": "success"}]]
    if WEBAPP_URL:
        rows.append([{"text": "LIVE OTP APP", "icon_custom_emoji_id": "5429571662737611814", "web_app": {"url": WEBAPP_URL}, "style": "primary"}])
    send_message(user_id, text, reply_markup={"inline_keyboard": rows}, effect_id=EFFECT_PARTY)

def generate_otp_display_classic(service_name, raw_number, message_text, source=""):
    c_code, c_name = get_country_info(raw_number)
    
    parts = c_name.split(" ", 1)
    country_emoji = parts[0]
    
    if len(raw_number) > 6:
        masked_number = f"{raw_number[:3]}DXA{raw_number[-3:]}"
    else:
        masked_number = raw_number
        
    service_html, srv_eid = get_service_info_html(service_name)
    
    text = (
        f"━━━━━━━━━━━━━━━━━\n"
        f"{service_html} ➜ {country_emoji} <code>{html.escape(masked_number)}</code>\n"
        f"━━━━━━━━━━━━━━━━━"
    )
    
    otp = extract_otp_code(message_text)
    
    # 🌟 রেঞ্জ বের করার লজিক (ডিফল্ট ৫ ডিজিট)
    clean_range = str(raw_number)[:5] if len(str(raw_number)) >= 5 else str(raw_number)
    _pfx = "spalloc" if source == "smspro" else "alloc"
    _svc_key = re.sub(r"[^a-z0-9]", "", service_name.lower()) if source == "smspro" else service_name.lower()
    deep_link = f"https://t.me/{BOT_USERNAME}?start={_pfx}_{clean_range}_{_svc_key}"
    
    markup = {"inline_keyboard": []}
    
    # 🌟 প্রথম লাইন (CHANNEL এবং OTP)
    row1 = []
    main_channel_link = bot_settings.get("main_channel_link", "")
    if main_channel_link:
        row1.append({"text": "𝗖𝗛𝗔𝗡𝗡𝗘𝗟", "url": main_channel_link, "icon_custom_emoji_id": "5429353834881261942", "style": "primary"})
        
    row1.append({"text": f"{otp}", "icon_custom_emoji_id": srv_eid, "copy_text": {"text": otp}, "style": "success"})
    
    markup["inline_keyboard"].append(row1)
    
    # 🌟 দ্বিতীয় লাইন (GET NUMBER)
    markup["inline_keyboard"].append([
        {"text": "GET NUMBER", "url": deep_link, "icon_custom_emoji_id": "5429167978761461870", "style": "danger"}
    ])
    
    return text, markup

def generate_otp_display(service_name, raw_number, message_text, source=""):
    """Video style: rich message (table) + CHANNEL / NUMBER buttons. The old text style is kept as automatic fallback."""
    classic_text, _ = generate_otp_display_classic(service_name, raw_number, message_text, source)
    rich = build_otp_rich(service_name, raw_number, message_text)
    clean_range = str(raw_number)[:5] if len(str(raw_number)) >= 5 else str(raw_number)
    _pfx = "spalloc" if source == "smspro" else "alloc"
    _svc_key = re.sub(r"[^a-z0-9]", "", service_name.lower()) if source == "smspro" else service_name.lower()
    deep_link = f"https://t.me/{BOT_USERNAME}?start={_pfx}_{clean_range}_{_svc_key}"
    # OTP GROUP layout: row 1 = [ OTP (tap to copy) | CHANNEL (main channel) ], row 2 = [ NUMBER ]
    # (the user's inbox message keeps only the OTP button - see deliver_to_inbox)
    otp = extract_otp_code(message_text)
    _svc_html, srv_eid = get_service_info_html(service_name)
    row1 = []
    if otp and otp != "COPY":
        row1.append({"text": f"{otp}", "copy_text": {"text": otp}, "icon_custom_emoji_id": srv_eid, "style": "success"})
    main_channel_link = bot_settings.get("main_channel_link", "")
    if main_channel_link:
        row1.append({"text": "CHANNEL", "url": main_channel_link, "icon_custom_emoji_id": IC["bell"], "style": "primary"})
    rows = []
    if row1: rows.append(row1)
    rows.append([{"text": "GET NUMBER", "url": deep_link, "icon_custom_emoji_id": IC["getnum"], "style": "danger"}])
    return RICH_MARK + rich + CLASSIC_SEP + classic_text, {"inline_keyboard": rows}

def send_otp_to_groups(formatted_text, markup, service=None):
    global otp_forward_groups
    if not otp_forward_groups: return False
    rich, classic = None, formatted_text
    if formatted_text.startswith(RICH_MARK):
        rich, _, classic = formatted_text[len(RICH_MARK):].partition(CLASSIC_SEP)
    tok = FWD_BOTS.get(fwd_route(service)) if service is not None else ""
    def _post(group_id):
        resp = send_rich_message(group_id, rich, reply_markup=markup) if rich else None
        if not resp: resp = send_message(group_id, classic, reply_markup=markup)
        return resp
    sent_any = False
    for group_id in otp_forward_groups:
        resp = None
        if tok:                                   # this service has its own forwarder bot
            _FWD.url = f"https://api.telegram.org/bot{tok}/"; _FWD.plain = not FWD_PREMIUM
            try: resp = _post(group_id)
            finally: _FWD.url = None; _FWD.plain = False
            if not (resp and resp.get("ok")):
                print(f"Forwarder [{fwd_route(service)}] could not post in {group_id} (is it admin there?) -> main bot sends it")
        if not (resp and resp.get("ok")): resp = _post(group_id)
        if resp and resp.get("ok"): sent_any = True
        time.sleep(1)
    return sent_any
        
    sent_any = False
    for group_id in otp_forward_groups:
        resp = send_message(group_id, formatted_text, reply_markup=markup)
        if resp and resp.get("ok"): sent_any = True
        time.sleep(1) 
        
    return sent_any

# ==========================================
# 2oo9 API & Background Listeners
# ==========================================
def fetch_live_access():
    headers = {"mauthapi": get_api_key()}
    try:
        res = stex_request("GET", "/liveaccess", headers=headers, timeout=10)
        data = res.json()
        if data.get("meta", {}).get("code") == 200:
            return data.get("data", {}).get("services", [])
    except: pass
    return []

def _stex_fetch_live_number(rid):
    headers = {"mauthapi": get_api_key(), "Content-Type": "application/json"}
    payload = {"rid": str(rid)}
    try:
        res = stex_request("POST", "/getnum", headers=headers, json=payload, timeout=10)
        data = res.json()
        if data.get("meta", {}).get("code") == 200:
            return data.get("data", {})
    except: pass
    return None

# ==========================================
# MINO SMS - second API provider (same kind of system as Stex)
#   * ranges from /liveaccess are added to the same service menu
#   * numbers: Stex first, Mino is used when Stex has no number for that range
#   * OTPs: /success_otp is polled; the number's owner gets it in the inbox + OTP group (same flow as Stex)
#   * key: MINO_API_KEY (env/default) or set from OWNER PANEL > MINO SETTINGS (empty = Mino is OFF)
# ==========================================
MINO_BASE = os.environ.get("MINO_BASE", "https://minosms.com").strip().rstrip("/")
MINO_API_KEY = os.environ.get("MINO_API_KEY", "mino_live_67fc6c0cfb4a28e3410ca45aa8c0c2dc").strip()
def mino_key():
    """API key set from the admin panel (saved in bot_settings) wins over the env/default one."""
    try: return str(bot_settings.get("mino_key") or MINO_API_KEY or "").strip()
    except Exception: return str(MINO_API_KEY or "").strip()

MINO_POLL_SECONDS = max(3, int(os.environ.get("MINO_POLL_SECONDS", "10")))
MINO_TRAFFIC_SECONDS = max(15, int(os.environ.get("MINO_TRAFFIC_SECONDS", "45")))

def mino_on():
    return bool(mino_key()) and bool(bot_settings.get("mino_on", True))

def mino_request(method, path, timeout=12, **kw):
    return requests.request(method, f"{MINO_BASE}{path}", timeout=timeout, **kw)

def mino_get_number(rid):
    """POST /getnumber -> same dict the Stex code reads (full_number / country / operator). None on failure."""
    if not mino_on(): return None
    try:
        rid_s = str(rid).strip()
        if not rid_s.upper().endswith("X"): rid_s += "XXX"
        d = mino_request("POST", "/getnumber", json={"api_key": mino_key(), "rid": rid_s, "national_format": 1, "remove_plus": 1}).json()
        if str(d.get("status", "")).lower() == "success" and d.get("number"):
            return {"full_number": str(d["number"]).strip(), "country": d.get("country") or "Unknown", "operator": d.get("operator") or "Unknown"}
    except Exception:
        pass
    return None

_mino_last_test = ["not tested yet"]

def mino_test():
    if not mino_key(): return "no API key set"
    try:
        d = mino_request("GET", "/liveaccess", params={"api_key": mino_key()}, timeout=15).json()
        if (d.get("meta") or {}).get("code") == 200:
            sv = (d.get("data") or {}).get("services", [])
            return f"✅ connected · {len(sv)} services · {sum(len(x.get('ranges', [])) for x in sv)} ranges"
        return "❌ " + str(d.get("message") or d.get("meta") or d)[:80]
    except Exception as e:
        return "❌ " + str(e)[:80]

def mino_status_text():
    k = mino_key()
    masked = (k[:9] + "..." + k[-4:]) if len(k) > 14 else (k or "not set")
    return ("🔑 <b>MINO SMS SETTINGS</b>\n━━━━━━━━━━━━━━━━━\n"
            f"<b>Status:</b> {'🟢 ON' if mino_on() else '🔴 OFF'}\n"
            f"<b>API key:</b> <code>{html.escape(masked)}</code>\n"
            f"<b>Last test:</b> {html.escape(_mino_last_test[0])}\n\n"
            "<i>Mino works even when STEX SMS is OFF.</i>")

def mino_settings_kb():
    on = mino_on()
    return {"inline_keyboard": [
        [{"text": "MINO SMS: " + ("ON" if on else "OFF"), "callback_data": "toggle_mino", "style": "success" if on else "danger",
          "icon_custom_emoji_id": "5352694861990501856" if on else "5420130255174145507"}],
        [{"text": "TEST CONNECTION", "callback_data": "mino_test", "style": "primary"}],
        [{"text": "SET API KEY", "callback_data": "mino_setkey", "style": "success", "icon_custom_emoji_id": "5429501315468270290"}],
        [{"text": "Back", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5438541186539232243", "style": "primary"}]]}

def handle_mino_key_message(message):
    uid, chat_id, mid = message["from"]["id"], message["chat"]["id"], message["message_id"]
    st = user_states.get(uid) or {}
    key = (message.get("text") or "").strip()
    delete_message(chat_id, mid)          # the key must not stay in the chat
    if len(key) < 10 or re.search(r"\s", key):
        send_message(chat_id, "❌ <b>That does not look like an API key.</b> Send only the key."); return
    bot_settings["mino_key"] = key
    bot_settings["mino_on"] = True
    save_local_data()
    user_states.pop(uid, None)
    _mino_last_test[0] = mino_test()
    if st.get("msg_id"): edit_message(chat_id, st["msg_id"], mino_status_text(), reply_markup=mino_settings_kb())

def fetch_live_number(rid):
    """Stex first (unchanged behaviour); if Stex gives no number, try Mino."""
    res = _stex_fetch_live_number(rid) if stex_on() else None
    if res and str(res.get("full_number", "")).strip(): return res
    return mino_get_number(rid) or res

def _mino_age(otp_data):
    """Seconds since the OTP was created (0 when unknown)."""
    try:
        dt = datetime.datetime.fromisoformat(str(otp_data.get("created_at", "")).replace("Z", "+00:00"))
        return max(0, time.time() - dt.timestamp())
    except Exception:
        return 0

def mino_traffic_worker():
    """Mino /liveaccess -> live ranges are added to the same service menu as the Stex traffic."""
    global voltx_dynamic_data
    while True:
        try:
            if mino_on():
                d = mino_request("GET", "/liveaccess", params={"api_key": mino_key()}, timeout=15).json()
                if (d.get("meta") or {}).get("code") == 200:
                    now = int(time.time())
                    for sv in (d.get("data") or {}).get("services", []):
                        raw_sid = str(sv.get("sid") or "").strip()
                        if not raw_sid: continue
                        sid = detect_service("", raw_sid)
                        seen = min(now, int(sv.get("last_at") or 0) // 1000) or now
                        if now - seen > RANGE_KEEP_SECONDS: continue
                        info = PREMIUM_APPS.get(sid, PREMIUM_APPS["Other"])
                        ent = voltx_dynamic_data.setdefault(sid, {"emoji_id": info["id"], "normal_emoji": info["emoji"], "ranges": {}})
                        for rg in sv.get("ranges", []):
                            rg = str(rg).replace("X", "").replace("x", "")
                            if not re.fullmatch(r"\d{5,13}", rg): continue
                            e = ent["ranges"].setdefault(rg, {"last_seen": seen, "hits": 0}); e["src"] = "mino"
                            e["last_seen"] = max(int(e.get("last_seen", 0) or 0), seen)
                            if int(e.get("hits", 0) or 0) < 1: e["hits"] = 1
        except Exception:
            pass
        time.sleep(MINO_TRAFFIC_SECONDS)

# ---- EVERY real-time OTP of the Stex console -> OTP groups ----
import queue as _queue
console_group_q = _queue.Queue(maxsize=500)
console_group_seen = set()
_console_group_ready = [False]        # first console read = baseline only (old hits are not dumped into the group)
group_msg_sent = {}                    # md5(message) -> time, shared with /success-otp so the group never gets the same OTP twice

def _msg_key(msg):
    return hashlib.md5(re.sub(r"\s+", " ", str(msg)).strip().encode("utf-8", "ignore")).hexdigest()

def group_claim(msg):
    """True if this message was not posted to the groups yet (and marks it as posted)."""
    now = time.time(); k = _msg_key(msg)
    if len(group_msg_sent) > 3000:
        for kk in [kk for kk, t in group_msg_sent.items() if now - t > 1800]: group_msg_sent.pop(kk, None)
    if now - group_msg_sent.get(k, 0) < 900: return False
    group_msg_sent[k] = now
    return True

def console_group_push(h):
    """Called for every console hit (oldest first). New OTP hits are queued for the OTP groups."""
    try:
        msg = str(h.get("message") or h.get("sms") or ""); rng = str(h.get("range") or "").strip()
        if not msg or not rng: return
        sig = f"{h.get('time', '')}_{rng}_{_msg_key(msg)[:10]}"
        if sig in console_group_seen: return
        console_group_seen.add(sig)
        if len(console_group_seen) > 3000: console_group_seen.clear(); console_group_seen.add(sig)
        if not _console_group_ready[0]: return                      # baseline pass
        if not bot_settings.get("console_to_group", True) or not otp_forward_groups: return
        if extract_otp_code(msg) == "COPY": return                  # not an OTP (no code inside)
        console_group_q.put_nowait(h)
    except Exception:
        pass

def console_group_sender():
    while True:
        h = console_group_q.get()
        try:
            if not otp_forward_groups: continue
            msg = str(h.get("message") or h.get("sms") or ""); rng = str(h.get("range") or "").replace("X", "").strip()
            if not group_claim(msg): continue
            number = re.sub(r"\D", "", str(h.get("number") or h.get("phone") or "")) or rng
            service = detect_service(msg, h.get("sid") or "Unknown")
            text, markup = generate_otp_display(service, number, msg)
            send_otp_to_groups(text, markup, service)
        except Exception as e:
            print("console_group_sender error:", e)

def voltx_console_listener():
    global recent_traffic, voltx_dynamic_data
    cycle_count = 0
    while True:
        if not stex_on(): time.sleep(3); continue
        try:
            headers = {"mauthapi": get_api_key()}
            res = stex_request("GET", "/console", headers=headers, timeout=10)
            data = res.json()
            if data.get("meta", {}).get("code") == 200:
                hits = data.get("data", {}).get("hits", [])
                new_traffic, seen_signatures = [], set()
                current_time = int(time.time())
                
                for h in reversed(hits):  # oldest first -> newest gets the highest feed id
                    console_feed_add(h)
                    console_group_push(h)
                _console_group_ready[0] = True
                for h in hits:
                    sig = f"{h.get('time', 0)}_{h.get('range', '')}"
                    if sig not in seen_signatures:
                        h['local_receive_time'] = current_time 
                        new_traffic.append(h)
                        seen_signatures.add(sig)
                        
                        raw_sid = h.get("sid") or "Unknown"
                        raw_msg = h.get("message") or ""
                        raw_range = str(h.get("range", "")).replace("X", "")
                        
                        if raw_range:
                            detected_sid = detect_service(raw_msg, raw_sid)
                            app_info = PREMIUM_APPS.get(detected_sid, PREMIUM_APPS["Other"])
                            emoji_id, normal_emoji = app_info["id"], app_info["emoji"]
                            
                            if detected_sid not in voltx_dynamic_data:
                                voltx_dynamic_data[detected_sid] = {"emoji_id": emoji_id, "normal_emoji": normal_emoji, "ranges": {}}
                            if raw_range not in voltx_dynamic_data[detected_sid]["ranges"]:
                                voltx_dynamic_data[detected_sid]["ranges"][raw_range] = {"last_seen": current_time, "hits": 0}
                            voltx_dynamic_data[detected_sid]["ranges"][raw_range]["last_seen"] = current_time
                            voltx_dynamic_data[detected_sid]["ranges"][raw_range]["hits"] += 1

                fifteen_mins_ago = current_time - 900
                for h in recent_traffic:
                    if h.get('local_receive_time', 0) >= fifteen_mins_ago:
                        sig = f"{h.get('time', 0)}_{h.get('range', '')}"
                        if sig not in seen_signatures:
                            new_traffic.append(h)
                            seen_signatures.add(sig)
                        
                new_traffic.sort(key=lambda x: x.get('local_receive_time', 0), reverse=True)
                recent_traffic = new_traffic

                services_to_delete = []
                for sid, s_data in voltx_dynamic_data.items():
                    ranges_to_delete = []
                    for r, r_data in s_data["ranges"].items():
                        if current_time - r_data["last_seen"] > RANGE_KEEP_SECONDS: ranges_to_delete.append(r)
                    for r in ranges_to_delete: del s_data["ranges"][r]
                    if not s_data["ranges"]: services_to_delete.append(sid)
                for sid in services_to_delete: del voltx_dynamic_data[sid]

                cycle_count += 1
                if cycle_count % 3 == 0: save_local_data()

        except Exception: pass
        time.sleep(CONSOLE_POLL_SECONDS)

def voltx_sms_listener():
    global recent_success_otps, otp_forward_groups, user_active_sessions
    group_sent_otps = set() 
    while True:
        if not stex_on(): time.sleep(3); continue
        try:
            headers = {"mauthapi": get_api_key(), "Content-Type": "application/json"}
            res = stex_request("GET", "/success-otp", headers=headers, timeout=15)
            
            if res.status_code == 200:
                data = res.json()
                otps = []
                
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list): 
                        otps = data["data"]
                    elif "data" in data and isinstance(data["data"], dict):
                        if "otps" in data["data"]: 
                            otps = data["data"]["otps"]
                        elif "hits" in data["data"]: 
                            otps = data["data"]["hits"]
                    elif "otps" in data: 
                        otps = data["otps"]
                    elif "hits" in data: 
                        otps = data["hits"]
                elif isinstance(data, list):
                    otps = data
                    
                for otp_data in reversed(otps):
                    msg_text = str(otp_data.get("message", otp_data.get("sms", otp_data.get("code", ""))))
                    raw_number = str(otp_data.get("number", otp_data.get("phone", "")))
                    raw_service = str(otp_data.get("sid", otp_data.get("service", "Unknown")))
                    
                    if not msg_text or not raw_number: continue
                    
                    sig = f"{raw_number}_{msg_text}"
                    detected_service = detect_service(msg_text, raw_service)
                    feed_add(sig, raw_number, detected_service, msg_text)

                    # 1. Forward to Groups (RAM only tracking - resets on restart)
                    if sig not in group_sent_otps:
                        group_sent_otps.add(sig)
                        if otp_forward_groups and group_claim(msg_text):
                            formatted_message, markup = generate_otp_display(detected_service, raw_number, msg_text)
                            send_otp_to_groups(formatted_message, markup, detected_service)
                    
                    # 2. Check Inbox Ownership (Strictly persistent tracking)
                    if sig not in recent_success_otps:
                        recent_success_otps.add(sig)
                        
                        owner_id = None
                        last_8_digits = raw_number[-8:] if len(raw_number) > 8 else raw_number
                        for active_num, uid in list(user_active_sessions.items()):
                            if active_num.endswith(last_8_digits):
                                owner_id = uid
                                del user_active_sessions[active_num]
                                break
                                
                        if owner_id:
                            feed_set_owner(sig, owner_id)
                            reward = get_otp_reward(detected_service)
                            update_user_stats(owner_id, reward, 0)
                            
                            # 🌟 ইনবক্সে মেসেজ পাঠানোর ফাংশন কল করা হলো
                            user_balance = get_user(owner_id).get("balance", 0.0)
                            deliver_to_inbox(owner_id, detected_service, raw_number, msg_text, user_balance, reward)
                            
                            today_date = time.strftime("%Y-%m-%d")
                            if daily_user_otps.get("date") != today_date:
                                daily_user_otps["date"] = today_date
                                daily_user_otps["users"] = {}
                            uid_str = str(owner_id)
                            daily_user_otps["users"][uid_str] = daily_user_otps["users"].get(uid_str, 0) + 1
                        
                        update_daily_stat("otps", 1)
                        save_local_data()
                        time.sleep(1) 
                        
            if len(recent_success_otps) > 2000:
                recent_success_otps = set(list(recent_success_otps)[-1000:])
                save_local_data()
            if len(group_sent_otps) > 2000:
                group_sent_otps = set(list(group_sent_otps)[-1000:])
        except Exception:
            pass
        time.sleep(10)

# ---- Mino: same flow as above, source = Mino /success_otp ----
def mino_sms_listener():
    global recent_success_otps, otp_forward_groups, user_active_sessions
    group_sent_otps = set() 
    while True:
        if not mino_on(): time.sleep(5); continue
        try:
            res = mino_request("GET", "/success_otp", params={"api_key": mino_key()}, timeout=15)
            
            if res.status_code == 200:
                data = res.json()
                otps = []
                
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list): 
                        otps = data["data"]
                    elif "data" in data and isinstance(data["data"], dict):
                        if "otps" in data["data"]: 
                            otps = data["data"]["otps"]
                        elif "hits" in data["data"]: 
                            otps = data["data"]["hits"]
                    elif "otps" in data: 
                        otps = data["otps"]
                    elif "hits" in data: 
                        otps = data["hits"]
                elif isinstance(data, list):
                    otps = data
                    
                for otp_data in reversed(otps):
                    msg_text = str(otp_data.get("message", otp_data.get("sms", otp_data.get("code", ""))))
                    raw_number = str(otp_data.get("number", otp_data.get("phone", "")))
                    raw_service = str(otp_data.get("sid", otp_data.get("service", "Unknown")))
                    
                    if not msg_text or not raw_number: continue
                    
                    sig = f"{raw_number}_{msg_text}"
                    detected_service = detect_service(msg_text, raw_service)
                    feed_add(sig, raw_number, detected_service, msg_text)

                    # 1. Forward to Groups (RAM only tracking - resets on restart)
                    if sig not in group_sent_otps:
                        group_sent_otps.add(sig)
                        if _mino_age(otp_data) <= 900 and otp_forward_groups and group_claim(msg_text):   # old OTPs are not dumped into the group after a restart
                            formatted_message, markup = generate_otp_display(detected_service, raw_number, msg_text)
                            send_otp_to_groups(formatted_message, markup, detected_service)
                    
                    # 2. Check Inbox Ownership (Strictly persistent tracking)
                    if sig not in recent_success_otps:
                        recent_success_otps.add(sig)
                        
                        owner_id = None
                        last_8_digits = raw_number[-8:] if len(raw_number) > 8 else raw_number
                        for active_num, uid in list(user_active_sessions.items()):
                            if active_num.endswith(last_8_digits):
                                owner_id = uid
                                del user_active_sessions[active_num]
                                break
                                
                        if owner_id:
                            feed_set_owner(sig, owner_id)
                            reward = get_otp_reward(detected_service)
                            update_user_stats(owner_id, reward, 0)
                            
                            # 🌟 ইনবক্সে মেসেজ পাঠানোর ফাংশন কল করা হলো
                            user_balance = get_user(owner_id).get("balance", 0.0)
                            deliver_to_inbox(owner_id, detected_service, raw_number, msg_text, user_balance, reward)
                            
                            today_date = time.strftime("%Y-%m-%d")
                            if daily_user_otps.get("date") != today_date:
                                daily_user_otps["date"] = today_date
                                daily_user_otps["users"] = {}
                            uid_str = str(owner_id)
                            daily_user_otps["users"][uid_str] = daily_user_otps["users"].get(uid_str, 0) + 1
                        
                        update_daily_stat("otps", 1)
                        save_local_data()
                        time.sleep(1) 
                        
            if len(recent_success_otps) > 2000:
                recent_success_otps = set(list(recent_success_otps)[-1000:])
                save_local_data()
            if len(group_sent_otps) > 2000:
                group_sent_otps = set(list(group_sent_otps)[-1000:])
        except Exception:
            pass
        time.sleep(MINO_POLL_SECONDS)

# ==========================================
# Live OTP Feed Store + Mini App API
# ==========================================
otp_feed = deque(maxlen=400)   # newest at the right
feed_index = {}                # sig -> entry
feed_lock = threading.Lock()
_last_feed_id = [0]

def mask_number(raw):
    d = re.sub(r"\D", "", str(raw))
    if len(d) <= 6: return d
    return d[:2] + "*" * (len(d) - 5) + d[-3:]

def feed_add(sig, raw_number, service, msg_text):
    with feed_lock:
        if sig in feed_index: return
        now_ms = int(time.time() * 1000)
        fid = max(now_ms, _last_feed_id[0] + 1)
        _last_feed_id[0] = fid
        c_code, c_name = get_country_info(raw_number)
        parts = c_name.split(" ", 1)
        code = extract_otp_code(msg_text)
        entry = {
            "id": fid, "ts": int(time.time()), "service": service,
            "number": mask_number(raw_number),
            "country": parts[1] if len(parts) > 1 else c_name, "flag": parts[0] if len(parts) > 1 else "",
            "code": "" if code == "COPY" else code,
            "message": str(msg_text)[:300],
            "_full": re.sub(r"\D", "", str(raw_number)), "_owner": None,
        }
        otp_feed.append(entry)
        feed_index[sig] = entry
        if len(feed_index) > 900:
            alive = {id(e) for e in otp_feed}
            for k in [k for k, e in feed_index.items() if id(e) not in alive]: feed_index.pop(k, None)

def feed_set_owner(sig, owner_id):
    with feed_lock:
        e = feed_index.get(sig)
        if e: e["_owner"] = int(owner_id)

# ---- Stex console feed (every OTP seen in the Stex SMS console, with its range) ----
console_feed = deque(maxlen=400)   # newest at the right
console_seen = set()
_last_console_id = [0]

def console_feed_add(h):
    try:
        raw_msg = str(h.get("message") or h.get("sms") or "")
        raw_range = str(h.get("range") or "").strip()
        if not raw_range or not raw_msg: return
        sig = f"{h.get('time', '')}_{raw_range}_{hashlib.md5(raw_msg.encode('utf-8', 'ignore')).hexdigest()[:10]}"
        with feed_lock:
            if sig in console_seen: return
            console_seen.add(sig)
            if len(console_seen) > 1500:
                keep = {e["_sig"] for e in console_feed}
                console_seen.intersection_update(keep | {sig})
            now_ms = int(time.time() * 1000)
            fid = max(now_ms, _last_console_id[0] + 1)
            _last_console_id[0] = fid
            c_code, c_name = get_country_info(raw_range)
            parts = c_name.split(" ", 1)
            code = extract_otp_code(raw_msg)
            console_feed.append({
                "id": fid, "ts": int(time.time()),
                "service": detect_service(raw_msg, h.get("sid") or ""),
                "range": raw_range,
                "country": parts[1] if len(parts) > 1 else c_name, "flag": parts[0] if len(parts) > 1 else "",
                "code": "" if code == "COPY" else code,
                "message": raw_msg[:300], "_sig": sig,
            })
    except Exception:
        pass

def _public(e, full=False):
    d = {k: v for k, v in e.items() if not k.startswith("_")}
    if full: d["number"] = "+" + e["_full"]
    return d

def validate_init_data(init_data, max_age=86400):
    """Verify Telegram WebApp initData signature. Returns user id or None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash): return None
        if time.time() - int(pairs.get("auth_date", 0)) > max_age: return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None

_rate = {}
def _rate_ok(ip, limit=180, window=60):
    now = time.time()
    hits = [t for t in _rate.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _rate[ip] = hits
        return False
    hits.append(now); _rate[ip] = hits
    if len(_rate) > 5000: _rate.clear()
    return True

INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

class ApiHandler(BaseHTTPRequestHandler):
    server_version = "OTPApi/1.0"

    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(raw)

    def do_OPTIONS(self): self._send(204, b"")
    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        try:
            ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            if not _rate_ok(ip): return self._send(429, {"ok": False, "error": "rate_limited"})
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path.rstrip("/") or "/"

            if path in ("/", "/index.html"):
                with open(INDEX_FILE, "rb") as f: return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/health": return self._send(200, {"ok": True})

            if path == "/api/feed":
                after = int(q.get("after", ["0"])[0] or 0)
                limit = min(int(q.get("limit", ["30"])[0] or 30), 60)
                midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
                with feed_lock:
                    items = [_public(e) for e in reversed(otp_feed) if e["id"] > after][:limit]
                    today = sum(1 for e in otp_feed if e["ts"] >= midnight)
                return self._send(200, {"ok": True, "today": max(today, daily_stats.get("otps", 0) if daily_stats.get("date") == time.strftime("%Y-%m-%d") else 0), "items": items})

            if path == "/api/console":
                after = int(q.get("after", ["0"])[0] or 0)
                limit = min(int(q.get("limit", ["30"])[0] or 30), 60)
                midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
                with feed_lock:
                    items = [_public(e) for e in reversed(console_feed) if e["id"] > after][:limit]
                    today = sum(1 for e in console_feed if e["ts"] >= midnight)
                return self._send(200, {"ok": True, "source": "console", "today": today, "items": items})

            if path == "/api/search":
                num = re.sub(r"\D", "", q.get("number", [""])[0])
                if len(num) < 6: return self._send(200, {"ok": True, "items": []})
                with feed_lock:
                    items = [_public(e) for e in reversed(otp_feed) if e["_full"] == num or (len(num) >= 8 and (e["_full"].endswith(num) or num.endswith(e["_full"])))][:20]
                return self._send(200, {"ok": True, "items": items})

            if path == "/api/my":
                uid = validate_init_data(q.get("initData", [""])[0])
                if not uid: return self._send(401, {"ok": False, "error": "unauthorized"})
                waiting = ["+" + str(n).lstrip("+") for n, owner in list(user_active_sessions.items()) if str(owner) == str(uid)]
                with feed_lock:
                    items = [_public(e, full=True) for e in reversed(otp_feed) if e["_owner"] == uid][:20]
                return self._send(200, {"ok": True, "waiting": waiting, "items": items})

            return self._send(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            try: self._send(500, {"ok": False, "error": "server_error"})
            except Exception: pass

def start_api_server():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", API_PORT), ApiHandler)
        srv.daemon_threads = True
        print(f"✅ Mini App API running on port {API_PORT}  (serving index.html at /)")
        srv.serve_forever()
    except Exception as e:
        print(f"❌ Mini App API failed to start: {e}")

# ==========================================
# Hybrid Database Initialization
# ==========================================
def init_sqlite():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT DEFAULT 'User', balance REAL DEFAULT 0.0, total_invites INTEGER DEFAULT 0)")
    try: cursor.execute("ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT 'User'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE users ADD COLUMN total_otps INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    conn.commit()
    conn.close()

def migrate_sqlite_to_firebase():
    global db_firebase, user_cache
    if not db_firebase: return
    try:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, balance, total_invites FROM users")
        rows = cursor.fetchall()
        if not rows: return conn.close()
        
        count = 0
        batch = db_firebase.batch()
        for row in rows:
            uid, fname, bal, inv = row
            doc_ref = db_firebase.collection('users').document(str(uid))
            batch.set(doc_ref, {"user_id": uid, "first_name": fname, "balance": firestore.Increment(bal), "total_invites": firestore.Increment(inv)}, merge=True)
            count += 1
            if count % 400 == 0:
                batch.commit()
                batch = db_firebase.batch()
        if count % 400 != 0: batch.commit()
        cursor.execute("DELETE FROM users")
        conn.commit()
        conn.close()
        user_cache.clear()
    except Exception as e: print(f"Migration Error: {e}")

def init_firebase_from_file():
    global current_db_mode, db_firebase, bot_settings, force_join_status, force_join_channels, otp_forward_groups, otp_button_link, voltx_keys, voltx_auto_mode
    if os.path.exists("temp_firebase.json"):
        try:
            if not firebase_admin._apps: 
                firebase_admin.initialize_app(credentials.Certificate("temp_firebase.json"))
            db_firebase = firestore.client()
            current_db_mode = "firebase"
            print("✅ Firebase Auto-Connected on Startup!")
            
            doc = db_firebase.collection('settings').document('bot_config').get()
            if doc.exists:
                data = doc.to_dict()
                if "bot_settings" in data: bot_settings.update(data["bot_settings"])
                if "force_join_status" in data: force_join_status = data["force_join_status"]
                if "force_join_channels" in data: force_join_channels = data["force_join_channels"]
                if "otp_forward_groups" in data: otp_forward_groups = data["otp_forward_groups"]
                if "otp_button_link" in data: otp_button_link = data["otp_button_link"]
                if "voltx_keys" in data: voltx_keys = data["voltx_keys"]
                if "voltx_auto_mode" in data: voltx_auto_mode = data["voltx_auto_mode"]
                print("✅ Settings synced from Firebase!")

            # 🌟 One-Time Sync on Startup (বট চালু হওয়ার সময় অটো সিঙ্ক)
            try:
                local_users = set()
                if os.path.exists(USERS_LIST_FILE):
                    with open(USERS_LIST_FILE, "r") as f:
                        local_users = set(json.load(f))
                
                docs = db_firebase.collection('users').select([]).stream()
                fb_users = set(int(doc.id) for doc in docs)
                
                combined_users = local_users.union(fb_users)
                if len(combined_users) > len(local_users):
                    with open(USERS_LIST_FILE, "w") as f:
                        json.dump(list(combined_users), f)
                    print(f"✅ Auto-Synced {len(fb_users)} users on Startup!")
            except Exception as e:
                print(f"Startup Sync Error: {e}")

        except Exception as e:
            print(f"❌ Firebase Auto-Connect Error: {e}")

def get_user(user_id):
    global current_db_mode, db_firebase, user_cache
    if user_id in user_cache: return user_cache[user_id]
    if current_db_mode == "firebase" and db_firebase:
        doc = db_firebase.collection('users').document(str(user_id)).get()
        if doc.exists:
            user_data = doc.to_dict()
            user_cache[user_id] = user_data 
            return user_data
        return None
    else: 
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT first_name, balance, total_invites FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row: 
            user_data = {"user_id": user_id, "first_name": row[0], "balance": row[1], "total_invites": row[2]}
            user_cache[user_id] = user_data 
            return user_data
        return None

def add_user(user_id, first_name="User"):
    global current_db_mode, db_firebase, user_cache
    add_to_broadcast_list(user_id) 
    if user_id in user_cache and user_cache[user_id]["first_name"] == first_name: return 
    
    if current_db_mode == "firebase" and db_firebase:
        doc_ref = db_firebase.collection('users').document(str(user_id))
        if not doc_ref.get().exists:
            new_data = {"user_id": user_id, "first_name": first_name, "balance": 0.0, "total_invites": 0}
            doc_ref.set(new_data)
            user_cache[user_id] = new_data
        else: 
            doc_ref.update({"first_name": first_name})
            if user_id in user_cache: user_cache[user_id]["first_name"] = first_name
    else: 
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, first_name, balance, total_invites) VALUES (?, ?, 0.0, 0)", (user_id, first_name))
        cursor.execute("UPDATE users SET first_name = ? WHERE user_id = ?", (first_name, user_id))
        conn.commit()
        conn.close()
        if user_id not in user_cache: user_cache[user_id] = {"user_id": user_id, "first_name": first_name, "balance": 0.0, "total_invites": 0}
        else: user_cache[user_id]["first_name"] = first_name

def update_user_stats(user_id, balance_add, invite_add):
    global current_db_mode, db_firebase, user_cache
    bal_add = float(balance_add)
    inv_add = int(invite_add)
    
    if user_id not in user_cache:
        get_user(user_id) # Ensure user is loaded
        
    if user_id in user_cache:
        user_cache[user_id]["balance"] = round(user_cache[user_id]["balance"] + bal_add, 2) # 🌟 Fix: Round balance
        user_cache[user_id]["total_invites"] += inv_add
        
    if current_db_mode == "firebase" and db_firebase:
        db_firebase.collection('users').document(str(user_id)).update({"balance": firestore.Increment(bal_add), "total_invites": firestore.Increment(inv_add)})
    else: 
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = balance + ?, total_invites = total_invites + ? WHERE user_id = ?", (bal_add, inv_add, user_id))
        conn.commit()
        conn.close()

def get_total_users(): return len(get_all_user_ids()) 

def get_top_users(limit=5):
    global current_db_mode, db_firebase
    if current_db_mode == "firebase" and db_firebase:
        docs = db_firebase.collection('users').order_by('balance', direction=firestore.Query.DESCENDING).limit(limit).stream()
        return [(doc.to_dict().get("user_id"), doc.to_dict().get("first_name", "User"), doc.to_dict().get("balance")) for doc in docs]
    else:
        conn = sqlite3.connect("bot_database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, first_name, balance FROM users ORDER BY balance DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()
        return rows

# ==========================================
# Sub-Menu Builders
# ==========================================
def get_otp_group_keyboard():
    kb = {"inline_keyboard": []}
    
    link_display = otp_button_link if otp_button_link else "Not Set"
    if len(link_display) > 25: link_display = link_display[:25] + "..."
    kb["inline_keyboard"].append([{"text": f"Link: {link_display}", "callback_data": "edit_otp_link", "icon_custom_emoji_id": "5420517437885943844", "style": "primary"}])
    
    mc_link = bot_settings.get("main_channel_link", "")
    mc_display = mc_link if mc_link else "Not Set"
    if len(mc_display) > 25: mc_display = mc_display[:25] + "..."
    kb["inline_keyboard"].append([{"text": f"Main Channel: {mc_display}", "callback_data": "edit_main_channel", "icon_custom_emoji_id": "5429353834881261942", "style": "primary"}])
    
    if otp_forward_groups:
        grp = otp_forward_groups[0]
        kb["inline_keyboard"].append([{"text": f"Delete Group: {grp}", "callback_data": "del_otp_group_0", "icon_custom_emoji_id": "5422557736330106570", "style": "danger"}])
        
    kb["inline_keyboard"].append([{"text": "CHECK BOTS", "callback_data": "otp_bots_check", "icon_custom_emoji_id": "5190447043545438788", "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "Set Forward Group", "callback_data": "add_otp_group", "icon_custom_emoji_id": "5429501315468270290", "style": "success"},
                                  {"text": "RANGE GROUP", "callback_data": "manage_range_group", "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5267490665117275176", "style": "danger"}])
    return kb

def get_force_join_keyboard():
    kb = {"inline_keyboard": []}
    status_text = "STATUS: ON" if force_join_status else "STATUS: OFF"
    status_icon = "5352694861990501856" if force_join_status else "5420130255174145507"
    
    kb["inline_keyboard"].append([{"text": status_text, "callback_data": "toggle_fj", "icon_custom_emoji_id": status_icon, "style": "primary" if force_join_status else "danger"}])
    
    for idx, ch in enumerate(force_join_channels):
        name = ch.get("username", ch.get("title", f"Channel {idx}"))
        kb["inline_keyboard"].append([{"text": f"Delete: {name}", "callback_data": f"del_fj_{idx}", "icon_custom_emoji_id": "5438178416421544431", "style": "danger"}])
        
    kb["inline_keyboard"].append([{"text": "Add Channel", "callback_data": "add_fj", "icon_custom_emoji_id": "5429501315468270290", "style": "success"}])
    kb["inline_keyboard"].append([{"text": "Back", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5438541186539232243", "style": "primary"}])
    return kb

def get_force_join_alert_keyboard():
    kb = {"inline_keyboard": []}
    for ch in force_join_channels:
        name = ch.get("title", ch.get("username", "Our Channel"))
        url = ch.get("invite_link", "")
        if not url and ch.get("username"): url = f"https://t.me/{ch['username'].replace('@', '')}"
        # 🌟 Join বাটনে প্রিমিয়াম লিংক ইমোজি
        kb["inline_keyboard"].append([{"text": f"JOIN {name}", "url": url, "icon_custom_emoji_id": "5420517437885943844", "style": "danger"}])
    
    # 🌟 Verify বাটনে প্রিমিয়াম টিকমার্ক ইমোজি
    kb["inline_keyboard"].append([{"text": "VERIFY", "callback_data": "check_fj_joined", "icon_custom_emoji_id": "5352694861990501856", "style": "success"}])
    return kb

def get_voltx_keys_keyboard():
    kb = {"inline_keyboard": []}
    status_text = "VOLTX AUTO: ON" if voltx_auto_mode else "VOLTX AUTO: OFF"
    status_icon = "5352694861990501856" if voltx_auto_mode else "5420130255174145507"
    
    kb["inline_keyboard"].append([{"text": "STEX SMS: " + ("ON" if stex_on() else "OFF"), "callback_data": "toggle_stex",
                                   "icon_custom_emoji_id": "5352694861990501856" if stex_on() else "5420130255174145507", "style": "success" if stex_on() else "danger"}])
    kb["inline_keyboard"].append([{"text": status_text, "callback_data": "toggle_vk", "icon_custom_emoji_id": status_icon, "style": "primary" if voltx_auto_mode else "danger"}])
    
    for idx, key in enumerate(voltx_keys):
        masked_key = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else key
        kb["inline_keyboard"].append([{"text": f"Delete: {masked_key}", "callback_data": f"del_vk_{idx}", "icon_custom_emoji_id": "5438178416421544431", "style": "danger"}])
        
    kb["inline_keyboard"].append([{"text": "Add Stex SMS Key", "callback_data": "add_vk", "icon_custom_emoji_id": "5429501315468270290", "style": "success"}])
    kb["inline_keyboard"].append([{"text": "Back", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5438541186539232243", "style": "primary"}])
    return kb

def get_main_keyboard(user_id):
    """Bottom menu: clean 2-column grid, green/blue checkerboard, a full-width row only for the odd one out."""
    keyboard_layout = []
    if WEBAPP_URL:
        keyboard_layout.append([{"text": "LIVE OTP APP", "icon_custom_emoji_id": IC["traffic"], "style": "success", "web_app": {"url": WEBAPP_URL}}])
    # (feature key, label, animated icon) - labels stay identical, the handlers match on them
    items = [
        ("get", "Get Number", IC["phone"]), ("search", "Search Number", IC["search"]),
        ("traffic", "Traffic", IC["traffic"]), ("twofa", "2FA Online", IC["shield"]),
        ("gmail", "Gmail", IC["gmail"]), ("refer", "Refer", IC["gift"]),
        ("profile", "My Profile", IC["user"]), ("lb", "Leaderboard", IC["trophy"]),
        (None, "Support", IC["chat"]),
    ]
    # buttons of features the admin switched OFF are removed (OWNER PANEL > FEATURES); the grid re-balances itself
    on = [(label, icon) for key, label, icon in items if key is None or feat_on(key)]
    for r in range(0, len(on), 2):
        pair = on[r:r + 2]
        row = []
        for c, (label, icon) in enumerate(pair):
            if label == "Support": style = "danger"
            else: style = "success" if ((r // 2) + c) % 2 == 0 else "primary"
            row.append({"text": label, "icon_custom_emoji_id": icon, "style": style})
        keyboard_layout.append(row)
    if user_id == ADMIN_ID:
        keyboard_layout.append([{"text": "Admin Panel", "icon_custom_emoji_id": IC["gear"], "style": "danger"}])
    return {"keyboard": keyboard_layout, "resize_keyboard": True, "is_persistent": False,
            "input_field_placeholder": "Choose an option below"}

def get_admin_inline_keyboard():
    global current_db_mode
    db_status = "SQLITE ACTIVE" if current_db_mode == "sqlite" else "FIREBASE ACTIVE"
    
    return {
        "inline_keyboard": [
            [
                {
                    "text": "STEX SMS SETTINGS", 
                    "callback_data": "manage_voltx", 
                    "icon_custom_emoji_id": "5240122519326860770", 
                    "style": "danger"
                }
            ],
            [{"text": "STEX SMS: " + ("ON" if stex_on() else "OFF"), "callback_data": "toggle_stex_a", "style": "success" if stex_on() else "danger",
              "icon_custom_emoji_id": "5352694861990501856" if stex_on() else "5420130255174145507"}],
            [{"text": "MINO SMS: " + ("ON" if mino_on() else "OFF"), "callback_data": "toggle_mino_a", "style": "success" if mino_on() else "danger",
              "icon_custom_emoji_id": "5352694861990501856" if mino_on() else "5420130255174145507"},
             {"text": "MINO SETTINGS", "callback_data": "mino_settings", "style": "success"}],
            [{"text": "BUTTONS: " + ("TRANSPARENT" if bot_settings.get("transparent_buttons", True) else "COLORED"), "callback_data": "toggle_btnstyle", "style": "primary"}],
            [{"text": "SMS PRO SETTINGS", "callback_data": "manage_smspro", "style": "success"}],
            [{"text": "CONNECT PANEL  (URL + API)", "callback_data": "xpa_add", "style": "success", "icon_custom_emoji_id": IC["key"]}],
            [xp_quick_btn("hadi"), xp_quick_btn("zyron")],
            [{"text": "ALL PANELS (%d/%d)" % panels_health(), "callback_data": "xpa_list", "style": "primary", "icon_custom_emoji_id": IC["globe"]},
             {"text": "LOGIN PANELS", "callback_data": "cpa_list", "style": "primary", "icon_custom_emoji_id": IC["shield"]}],
            [
                {
                    "text": "BROADCAST", 
                    "callback_data": "broadcast_menu", 
                    "icon_custom_emoji_id": "5296633779157243809", 
                    "style": "primary"
                },
                {
                    "text": "FORCE JOIN", 
                    "callback_data": "manage_fj", 
                    "icon_custom_emoji_id": "5429353834881261942", 
                    "style": "success"
                }
            ],
            [
                {
                    "text": "OTP GROUP", 
                    "callback_data": "manage_otp_group", 
                    "icon_custom_emoji_id": "5429208347159077664", 
                    "style": "danger"
                },
                {"text": "RANGE GROUP", "callback_data": "manage_range_group", "icon_custom_emoji_id": "5429208347159077664", "style": "success"}
            ],
            [
                {
                    "text": "USER CONTROL", 
                    "callback_data": "user_control", 
                    "icon_custom_emoji_id": "5429274992166609833", 
                    "style": "primary"
                },
                {"text": "NUMBER STOCK", "callback_data": "manage_stock", "icon_custom_emoji_id": "5429591316507963112", "style": "primary"}
            ],
            [
                {
                    "text": "DXA CONTROL", 
                    "callback_data": "dxa_control", 
                    "icon_custom_emoji_id": "5240451569656308978", 
                    "style": "danger"
                },
                {"text": "ADD SERVICE", "callback_data": "manage_msvc", "style": "success"}
            ],
            [
                {"text": "FEATURES ON/OFF", "callback_data": "ft_panel", "icon_custom_emoji_id": IC["gear"], "style": "primary"},
                {"text": "HIDE SERVICES", "callback_data": "hs_panel", "icon_custom_emoji_id": IC["ban"], "style": "danger"}
            ],
            [
                {"text": f"GMAIL ({len(bot_settings.get('gmail_stock', []))})", "callback_data": "gm_panel", "icon_custom_emoji_id": IC["gmail"], "style": "success"},
                {"text": "MAINTENANCE: " + ("ON" if bot_settings.get("maintenance") else "OFF"), "callback_data": "ft_maint",
                 "icon_custom_emoji_id": IC["no"] if bot_settings.get("maintenance") else IC["green"], "style": "danger" if bot_settings.get("maintenance") else "success"}
            ],
            [
                {
                    "text": "UPLOAD FIREBASE",
                    "callback_data": "upload_firebase", 
                    "icon_custom_emoji_id": "5429362935916962442", 
                    "style": "primary"
                }
            ],
            [
                {
                    "text": db_status, 
                    "callback_data": "db_status", 
                    "icon_custom_emoji_id": "5429591316507963112", 
                    "style": "success"
                }
            ],
            [
                {
                    "text": "CLOSE", 
                    "callback_data": "close_panel", 
                    "icon_custom_emoji_id": "5438541186539232243", 
                    "style": "danger"
                }
            ]
        ]
    }

# ==========================================
# Admin: FEATURES ON/OFF, HIDE SERVICES, GMAIL panels
# ==========================================
def _onoff_btn(on, label, cb):
    return {"text": f"{'ON' if on else 'OFF'} · {label}", "callback_data": cb,
            "icon_custom_emoji_id": IC["green"] if on else IC["no"], "style": "success" if on else "danger"}

def features_panel_text():
    lines = [f'{_temoji("gear", "⚙️")} <b>FEATURES ON/OFF</b>', "━━━━━━━━━━━━━━━━━"]
    for key, label, _cmd in FEATURES:
        lines.append(f'{"🟢" if feat_on(key) else "🔴"} {label}')
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("<i>OFF = the button disappears from the user menu and the feature is blocked.</i>")
    return "\n".join(lines)

def features_panel_kb():
    kb = {"inline_keyboard": []}
    row = []
    for key, label, _cmd in FEATURES:
        row.append(_onoff_btn(feat_on(key), label, f"ft_t_{key}"))
        if len(row) == 2: kb["inline_keyboard"].append(row); row = []
    if row: kb["inline_keyboard"].append(row)
    kb["inline_keyboard"].append([_onoff_btn(not bot_settings.get("maintenance"), "Bot Online (OFF = maintenance)", "ft_maint_p")])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": IC["back"], "style": "primary"}])
    return kb

def all_service_names():
    names = {}
    try:
        for sid in merged_services(True).keys(): names[sid.lower()] = sid
    except Exception: pass
    try:
        for sn in smspro_view().keys(): names.setdefault(str(sn).lower(), str(sn))
    except Exception: pass
    for h in bot_settings.get("hidden_services", []): names.setdefault(str(h).lower(), str(h))
    return sorted(names.values(), key=lambda s: s.lower())

def hide_panel_text():
    hid = bot_settings.get("hidden_services", [])
    return (f'{_temoji("ban", "🚫")} <b>HIDE SERVICES</b>\n━━━━━━━━━━━━━━━━━\n'
            f'<i>Tap a service to hide / show it. Hidden services disappear from Get Number and Live Traffic.</i>\n\n'
            f'🙈 Hidden now: <b>{len(hid)}</b>')

def hide_panel_kb():
    kb = {"inline_keyboard": []}
    row = []
    for name in all_service_names()[:40]:
        hid = is_hidden_service(name)
        info = PREMIUM_APPS.get(next((a for a in PREMIUM_APPS if a.lower() == name.lower()), "Other"), PREMIUM_APPS["Other"])
        row.append({"text": f"{'HIDDEN' if hid else 'SHOWN'} · {name[:14]}", "callback_data": ("hs_t_" + name)[:60],
                    "icon_custom_emoji_id": info["id"], "style": "danger" if hid else "success"})
        if len(row) == 2: kb["inline_keyboard"].append(row); row = []
    if row: kb["inline_keyboard"].append(row)
    kb["inline_keyboard"].append([{"text": "Unhide All", "callback_data": "hs_clear", "icon_custom_emoji_id": IC["ok"], "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": IC["back"], "style": "primary"}])
    return kb

def gmail_panel_text():
    on = feat_on("gmail"); lim = int(bot_settings.get("gmail_limit", 0) or 0)
    return (f'{_temoji("gmail", "📧")} <b>GMAIL PANEL</b>\n━━━━━━━━━━━━━━━━━\n'
            f'{"🟢" if on else "🔴"} Status: <b>{"ON" if on else "OFF"}</b>\n'
            f'📦 Stock: <b>{len(bot_settings.get("gmail_stock", []))}</b>\n'
            f'✅ Given total: <b>{bot_settings.get("gmail_total_given", 0)}</b>\n'
            f'🔢 Daily limit / user: <b>{lim if lim else "Unlimited"}</b>\n━━━━━━━━━━━━━━━━━\n'
            f'🌐 Site (Mini App): <code>{html.escape(bot_settings.get("gmail_site") or "not set")}</code>\n'
            f'🤖 Telegram App: <code>{html.escape(bot_settings.get("gmail_miniapp") or "not set")}</code>\n━━━━━━━━━━━━━━━━━\n'
            f'<i>Add accounts as <code>email:password</code>, one per line.</i>')

def gmail_panel_kb(confirm_clear=False):
    kb = {"inline_keyboard": [
        [_onoff_btn(feat_on("gmail"), "Gmail Option", "ft_t_gmail_gm")],
        [{"text": "ADD STOCK", "callback_data": "gm_add", "icon_custom_emoji_id": IC["gmail"], "style": "success"},
         {"text": f"LIMIT: {bot_settings.get('gmail_limit', 0) or '∞'}", "callback_data": "gm_limit", "icon_custom_emoji_id": IC["stats"], "style": "primary"}]]}
    kb["inline_keyboard"].append([
        {"text": "SET SITE LINK", "callback_data": "gm_site", "icon_custom_emoji_id": IC["share"], "style": "primary"},
        {"text": "SET MINI APP", "callback_data": "gm_app", "icon_custom_emoji_id": IC["gmail"], "style": "primary"}])
    if confirm_clear:
        kb["inline_keyboard"].append([{"text": "YES, CLEAR ALL", "callback_data": "gm_clear_yes", "icon_custom_emoji_id": IC["ok"], "style": "danger"},
                                      {"text": "CANCEL", "callback_data": "gm_panel", "icon_custom_emoji_id": IC["no"], "style": "primary"}])
    else:
        kb["inline_keyboard"].append([{"text": "CLEAR STOCK", "callback_data": "gm_clear", "icon_custom_emoji_id": IC["ban"], "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": IC["back"], "style": "primary"}])
    return kb

# ==========================================
# Manual Emojis: admin picks a premium emoji for every service / country
# (OWNER PANEL > DXA CONTROL > MANUAL EMOJIS). Saved in bot_settings["emoji_overrides"].
# ==========================================
_EMO_DEF_SVC = {k: v["id"] for k, v in PREMIUM_APPS.items()}
_EMO_DEF_FLAG = dict(GLOBAL_BODY_EMOJIS)
_EMO_PER_PAGE, _EMO_PACK_PER_PAGE = 20, 30

def apply_emoji_overrides():
    """Put the saved manual emojis into PREMIUM_APPS (services) and GLOBAL_BODY_EMOJIS (country flags)."""
    ov = bot_settings.get("emoji_overrides") or {}
    for name, eid in _EMO_DEF_SVC.items():
        if name in PREMIUM_APPS: PREMIUM_APPS[name]["id"] = eid
    for k in [k for k in PREMIUM_APPS if k not in _EMO_DEF_SVC]: PREMIUM_APPS.pop(k, None)
    for k in list(GLOBAL_BODY_EMOJIS):
        if k in _EMO_DEF_FLAG: GLOBAL_BODY_EMOJIS[k] = _EMO_DEF_FLAG[k]
        else: GLOBAL_BODY_EMOJIS.pop(k, None)
    for key, v in ov.items():
        kind, _, name = str(key).partition(":")
        v = v or {}
        eid = str(v.get("id", ""))
        if not eid.isdigit(): continue
        if kind == "svc":
            canon = next((a for a in PREMIUM_APPS if a.lower() == name.lower()), None)
            if canon: PREMIUM_APPS[canon]["id"] = eid
            else: PREMIUM_APPS[name] = {"emoji": v.get("emoji") or "🌐", "id": eid}
        elif kind == "cc":
            GLOBAL_BODY_EMOJIS[name] = eid
    _ID_ICON.clear()

def _emo_services():
    names, seen = [], set()
    def add(n):
        n = str(n).strip()
        if n and n.lower() not in seen: seen.add(n.lower()); names.append(n)
    for n in list(PREMIUM_APPS): add(n)
    for n in sorted(list(voltx_dynamic_data.keys()), key=str.lower): add(n)
    for n in sorted(list((bot_settings.get("manual_services") or {}).keys()), key=str.lower): add(n)
    return names

def _emo_countries():
    out, seen = [], set()
    for nm in COUNTRY_CODES.values():
        flag, _, cname = nm.partition(" ")
        if flag not in seen: seen.add(flag); out.append((flag, cname or flag))
    return sorted(out, key=lambda x: x[1].lower())

def _emo_current(kind, key):
    if kind == "s":
        canon = next((a for a in PREMIUM_APPS if a.lower() == str(key).lower()), None) or "Other"
        return PREMIUM_APPS[canon]["id"], PREMIUM_APPS[canon]["emoji"]
    return GLOBAL_BODY_EMOJIS.get(key), key

def _emo_okey(kind, key):
    return ("svc:" if kind == "s" else "cc:") + str(key)

def _emo_item(kind, idx):
    items = _emo_services() if kind == "s" else _emo_countries()
    if not (0 <= idx < len(items)): return None, None
    if kind == "s": return items[idx], items[idx]
    return items[idx][0], f"{items[idx][0]} {items[idx][1]}"

def _emo_html(eid, fb):
    return _temoji(eid, fb) if eid else fb

def emo_menu_text():
    n = len(bot_settings.get("emoji_overrides") or {})
    return ("✨ <b>MANUAL EMOJIS</b>\n━━━━━━━━━━━━━━━━━\n"
            "Pick any service or country, then set its premium emoji.\n"
            f"<b>Manually set:</b> {n}\n"
            "<i>PREMIUM ICONS must be ON and the bot owner needs Telegram Premium.</i>")

def emo_menu_kb():
    return {"inline_keyboard": [
        [{"text": "SERVICES", "callback_data": "emo_l_s_0", "icon_custom_emoji_id": IC["getnum"], "style": "primary"},
         {"text": "COUNTRIES", "callback_data": "emo_l_c_0", "icon_custom_emoji_id": IC["globe"], "style": "primary"}],
        [{"text": "BACK", "callback_data": "dxa_control", "icon_custom_emoji_id": IC["back"], "style": "danger"}]]}

def emo_list_view(kind, page):
    items = _emo_services() if kind == "s" else _emo_countries()
    pages = max(1, (len(items) + _EMO_PER_PAGE - 1) // _EMO_PER_PAGE)
    page = max(0, min(int(page), pages - 1))
    ov = bot_settings.get("emoji_overrides") or {}
    rows, row = [], []
    for i in range(page * _EMO_PER_PAGE, min(len(items), (page + 1) * _EMO_PER_PAGE)):
        if kind == "s": key, label = items[i], items[i][:20]
        else: key, label = items[i][0], f"{items[i][0]} {items[i][1]}"[:22]
        eid, _fb = _emo_current(kind, key)
        b = {"text": label, "callback_data": f"emo_p_{kind}_{i}", "style": "success" if _emo_okey(kind, key) in ov else "primary"}
        if eid: b["icon_custom_emoji_id"] = eid
        row.append(b)
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    nav = []
    if page > 0: nav.append({"text": "PREV", "callback_data": f"emo_l_{kind}_{page-1}", "icon_custom_emoji_id": IC["back"], "style": "primary"})
    nav.append({"text": f"{page+1}/{pages}", "callback_data": "emo_nop", "style": "primary"})
    if page < pages - 1: nav.append({"text": "NEXT", "callback_data": f"emo_l_{kind}_{page+1}", "icon_custom_emoji_id": IC["arrow"], "style": "primary"})
    rows.append(nav)
    rows.append([{"text": "BACK", "callback_data": "dxa_emoji", "icon_custom_emoji_id": IC["back"], "style": "danger"}])
    title = "SERVICES" if kind == "s" else "COUNTRIES"
    txt = (f"✨ <b>MANUAL EMOJIS — {title}</b>\n━━━━━━━━━━━━━━━━━\n"
           f"Tap one to set its premium emoji.\n<i>Green = already set manually. Total: {len(items)}</i>")
    return txt, {"inline_keyboard": rows}

def emo_pick_view(st):
    kind, key = st["kind"], st["key"]
    eid, fb = _emo_current(kind, key)
    pend = st.get("pending")
    t = (f"✨ <b>MANUAL EMOJI</b>\n━━━━━━━━━━━━━━━━━\n"
         f"<b>{'Service' if kind == 's' else 'Country'}:</b> {html.escape(st.get('label', str(key)))}\n"
         f"<b>Current:</b> {_emo_html(eid, fb)} <code>{eid or '-'}</code>\n")
    if pend:
        t += f"<b>New:</b> {_emo_html(pend['id'], pend.get('emoji') or fb)} <code>{pend['id']}</code>\n\nPress <b>SAVE</b> to apply it everywhere."
        kb = {"inline_keyboard": [[
            {"text": "SAVE", "callback_data": "emo_save", "icon_custom_emoji_id": IC["ok"], "style": "success"},
            {"text": "CANCEL", "callback_data": "emo_cancel", "icon_custom_emoji_id": IC["no"], "style": "danger"}]]}
        if st.get("pack"): kb["inline_keyboard"].append([{"text": "BACK TO PACK", "callback_data": f"emo_g_{st.get('gpage', 0)}", "icon_custom_emoji_id": IC["back"], "style": "primary"}])
        return t, kb
    t += ("\nSend now:\n• a <b>premium emoji</b> (from your emoji keyboard)\n• or a custom emoji <b>ID</b>\n"
          "• or a pack link like <code>t.me/addemoji/PackName</code> to browse the whole pack")
    kb = {"inline_keyboard": []}
    if st.get("pack"): kb["inline_keyboard"].append([{"text": "OPEN PACK", "callback_data": f"emo_g_{st.get('gpage', 0)}", "icon_custom_emoji_id": IC["spark"], "style": "primary"}])
    if _emo_okey(kind, key) in (bot_settings.get("emoji_overrides") or {}):
        kb["inline_keyboard"].append([{"text": "RESET TO DEFAULT", "callback_data": "emo_reset", "icon_custom_emoji_id": IC["change"], "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": f"emo_l_{kind}_{st.get('page', 0)}", "icon_custom_emoji_id": IC["back"], "style": "danger"}])
    return t, kb

def emo_pack_view(st, page):
    pack = st.get("pack") or []
    pages = max(1, (len(pack) + _EMO_PACK_PER_PAGE - 1) // _EMO_PACK_PER_PAGE)
    page = max(0, min(int(page), pages - 1)); st["gpage"] = page
    rows, row = [], []
    for i in range(page * _EMO_PACK_PER_PAGE, min(len(pack), (page + 1) * _EMO_PACK_PER_PAGE)):
        row.append({"text": str(i + 1), "callback_data": f"emo_x_{i}", "icon_custom_emoji_id": pack[i][0], "style": "primary"})
        if len(row) == 5: rows.append(row); row = []
    if row: rows.append(row)
    nav = []
    if page > 0: nav.append({"text": "PREV", "callback_data": f"emo_g_{page-1}", "icon_custom_emoji_id": IC["back"], "style": "primary"})
    nav.append({"text": f"{page+1}/{pages}", "callback_data": "emo_nop", "style": "primary"})
    if page < pages - 1: nav.append({"text": "NEXT", "callback_data": f"emo_g_{page+1}", "icon_custom_emoji_id": IC["arrow"], "style": "primary"})
    rows.append(nav)
    rows.append([{"text": "BACK", "callback_data": "emo_cancel", "icon_custom_emoji_id": IC["back"], "style": "danger"}])
    txt = (f"✨ <b>EMOJI PACK</b> <code>{html.escape(str(st.get('pack_name', '')))}</code>\n━━━━━━━━━━━━━━━━━\n"
           f"Tap an emoji to select it for <b>{html.escape(st.get('label', ''))}</b>. ({len(pack)} emojis)")
    return txt, {"inline_keyboard": rows}

def _utf16_slice(text, off, ln):
    return text.encode("utf-16-le")[off * 2:(off + ln) * 2].decode("utf-16-le", "ignore")

def handle_emoji_pick_message(message):
    """Admin sent something while 'waiting_emoji_pick': a premium emoji, an ID or an addemoji pack link."""
    uid, chat_id, mid = message["from"]["id"], message["chat"]["id"], message["message_id"]
    st = user_states.get(uid) or {}
    target = st.get("msg_id")
    text = (message.get("text") or "").strip()
    cand = None
    for ent in (message.get("entities") or []):
        if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
            ch = _utf16_slice(message.get("text") or "", ent.get("offset", 0), ent.get("length", 1)) or "✨"
            cand = {"id": str(ent["custom_emoji_id"]), "emoji": ch}; break
    if not cand and (message.get("sticker") or {}).get("custom_emoji_id"):
        stk = message["sticker"]; cand = {"id": str(stk["custom_emoji_id"]), "emoji": stk.get("emoji") or "✨"}
    if not cand and re.fullmatch(r"\d{15,25}", text):
        cand = {"id": text, "emoji": _emo_current(st.get("kind"), st.get("key"))[1] or "✨"}
    if cand:
        st["pending"] = cand
        t, kb = emo_pick_view(st)
        if target: edit_message(chat_id, target, t, reply_markup=kb)
        delete_message(chat_id, mid); return
    m = re.search(r"t\.me/addemoji/([A-Za-z0-9_]+)", text)
    if m:
        try:
            r = requests.get(BASE_URL + "getStickerSet", params={"name": m.group(1)}, timeout=20).json()
        except Exception:
            r = {}
        pack = [(str(x["custom_emoji_id"]), x.get("emoji") or "✨") for x in (r.get("result") or {}).get("stickers", []) if x.get("custom_emoji_id")]
        if pack:
            st["pack"] = pack; st["pack_name"] = m.group(1); st["gpage"] = 0; st.pop("pending", None)
            t, kb = emo_pack_view(st, 0)
            if target: edit_message(chat_id, target, t, reply_markup=kb)
        else:
            send_message(chat_id, "❌ <b>Pack not found or it has no custom emojis.</b>")
        delete_message(chat_id, mid); return
    send_message(chat_id, "❌ <b>Send a premium emoji, an emoji ID, or a t.me/addemoji/... link.</b>")

def handle_emoji_callback(query_id, user_id, chat_id, message_id, data):
    """All callbacks starting with emo_ (admin only)."""
    global bot_settings
    if user_id != ADMIN_ID:
        answer_callback_query(query_id, "Admin only", show_alert=True); return
    if data == "emo_nop":
        answer_callback_query(query_id); return
    if data.startswith("emo_l_"):
        _, _, k, pg = data.split("_", 3)
        user_states.pop(user_id, None)
        t, kb = emo_list_view(k, int(pg))
        edit_message(chat_id, message_id, t, reply_markup=kb); answer_callback_query(query_id); return
    if data.startswith("emo_p_"):
        _, _, k, idx = data.split("_", 3)
        key, label = _emo_item(k, int(idx))
        if key is None: answer_callback_query(query_id, "List changed, open it again", show_alert=True); return
        st = {"state": "waiting_emoji_pick", "msg_id": message_id, "kind": k, "key": key, "label": label, "page": int(idx) // _EMO_PER_PAGE}
        user_states[user_id] = st
        t, kb = emo_pick_view(st)
        edit_message(chat_id, message_id, t, reply_markup=kb); answer_callback_query(query_id); return
    st = user_states.get(user_id)
    if not isinstance(st, dict) or st.get("state") != "waiting_emoji_pick":
        answer_callback_query(query_id, "Session expired, open MANUAL EMOJIS again", show_alert=True); return
    st["msg_id"] = message_id
    if data.startswith("emo_g_"):
        t, kb = emo_pack_view(st, int(data[6:]))
        edit_message(chat_id, message_id, t, reply_markup=kb); answer_callback_query(query_id); return
    if data.startswith("emo_x_"):
        i = int(data[6:]); pack = st.get("pack") or []
        if 0 <= i < len(pack): st["pending"] = {"id": pack[i][0], "emoji": pack[i][1]}
        t, kb = emo_pick_view(st)
        edit_message(chat_id, message_id, t, reply_markup=kb); answer_callback_query(query_id); return
    if data == "emo_cancel":
        st.pop("pending", None)
        t, kb = emo_pick_view(st)
        edit_message(chat_id, message_id, t, reply_markup=kb); answer_callback_query(query_id); return
    if data in ("emo_save", "emo_reset"):
        okey = _emo_okey(st["kind"], st["key"])
        ov = bot_settings.setdefault("emoji_overrides", {})
        if data == "emo_save":
            pend = st.get("pending")
            if not pend: answer_callback_query(query_id, "Nothing selected", show_alert=True); return
            ov[okey] = {"id": str(pend["id"]), "emoji": pend.get("emoji") or "✨"}
        else:
            ov.pop(okey, None)
        apply_emoji_overrides(); save_local_data()
        k, page = st["kind"], st.get("page", 0)
        user_states.pop(user_id, None)
        t, kb = emo_list_view(k, page)
        edit_message(chat_id, message_id, t, reply_markup=kb)
        answer_callback_query(query_id, "✅ Saved" if data == "emo_save" else "↩️ Back to default"); return

def dxa_control_keyboard():
    grp = bot_settings.get("w_group", "")
    grp_status = grp if grp else "NOT SET"
    return {"inline_keyboard": [
        [{"text": f"MIN WITHDRAW: {bot_settings['min_withdraw']}", "icon_custom_emoji_id": "5352877703043258544", "callback_data": "dxa_min_w", "style": "success"},
         {"text": "OTP CONTROL", "icon_custom_emoji_id": "5190576863226933563", "callback_data": "dxa_otp_control", "style": "primary"}],
        [{"text": f"REFER REWARD: {bot_settings.get('refer_reward', 0.2)}", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "dxa_ref_r", "style": "success"},
         {"text": f"COOLDOWN: {bot_settings['cooldown']}s", "icon_custom_emoji_id": "5337172996211648018", "callback_data": "dxa_cool", "style": "primary"}],
        [{"text": f"NUM/REQ: {bot_settings['num_req']}", "icon_custom_emoji_id": "5337132498965010628", "callback_data": "dxa_num_req", "style": "success"},
         {"text": "W. METHODS", "icon_custom_emoji_id": "5190899075968441286", "callback_data": "manage_w_methods", "style": "primary"}],
        [{"text": f"W. GROUP: {grp_status}", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "dxa_w_group", "style": "success"}],
        [{"text": "MANUAL EMOJIS", "icon_custom_emoji_id": IC["spark"], "callback_data": "dxa_emoji", "style": "success"}],
        [{"text": f"PREMIUM ICONS: {'ON' if bot_settings.get('premium_emoji') else 'OFF'}", "callback_data": "dxa_prem_toggle", "style": "primary"}],
        [{"text": "BACK", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
    ]}

def dxa_otp_control_keyboard():
    kb = {"inline_keyboard": [
        [{"text": f"Default Rate: {bot_settings['otp_default_rate']}", "icon_custom_emoji_id": "5352877703043258544", "callback_data": "dxa_def_rate", "style": "primary"}],
        [{"text": "Set Service Rate", "icon_custom_emoji_id": "5395444784611480792", "callback_data": "dxa_srv_rate", "style": "success"}]
    ]}
    
    # অ্যাড করা স্পেসিফিক সার্ভিস রেটগুলো ডায়নামিক ইনলাইন বাটন হিসেবে দেখানোর জন্য
    for srv_name, rate in bot_settings.get("otp_service_rates", {}).items():
        app_info = PREMIUM_APPS.get(srv_name, PREMIUM_APPS["Other"])
        kb["inline_keyboard"].append([{"text": f"Delete: {srv_name} ({rate})", "icon_custom_emoji_id": app_info["id"], "callback_data": f"del_srv_rate_{srv_name}", "style": "danger"}])
        
    kb["inline_keyboard"].append([{"text": "BACK", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "dxa_control", "style": "danger"}])
    return kb
    
def dxa_manage_w_methods_keyboard():
    kb = {"inline_keyboard": []}
    for idx, method in enumerate(bot_settings.get("w_methods", [])):
        kb["inline_keyboard"].append([{"text": f"Delete: {method}", "callback_data": f"del_w_method_{idx}", "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "Add Method", "callback_data": "add_w_method", "style": "success"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "dxa_control", "style": "primary"}])
    return kb

def get_user_control_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "USER PROFILE", "icon_custom_emoji_id": "5352861489541714456", "callback_data": "uc_profile", "style": "primary"}],
            [{"text": "BACK", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]
    }

def get_user_profile_keyboard(target_uid):
    return {
        "inline_keyboard": [
            [{"text": "BALANCE", "icon_custom_emoji_id": "5429612421977253466", "callback_data": f"uc_balance_{target_uid}", "style": "success"}],
            [{"text": "BACK", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "user_control", "style": "danger"}]
        ]
    }

def get_upload_firebase_keyboard():
    return {"inline_keyboard": [[{"text": "CANCEL", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5420130255174145507", "style": "danger"}]]}

def get_back_only_keyboard():
    return {"inline_keyboard": [[{"text": "BACK", "callback_data": "user_cancel", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}]]}

def get_leaderboard_keyboard():
    return {"inline_keyboard": [[{"text": "REFRESH", "callback_data": "refresh_leaderboard", "icon_custom_emoji_id": "5229111790842952353", "style": "success"}]]}

def get_owner_panel_text():
    today = time.strftime("%Y-%m-%d")
    if daily_stats.get("date") != today:
        daily_stats["date"] = today
        daily_stats["numbers"] = 0
        daily_stats["otps"] = 0
    services = len(voltx_dynamic_data)
    ranges = sum(len(d.get("ranges", {})) for d in voltx_dynamic_data.values())
    db = "FIREBASE" if current_db_mode == "firebase" else "SQLITE"
    _pon, _ptot = panels_health()
    return (
        "<blockquote><b>👑 OWNER ZONE  ·  PREMIUM CONTROL</b></blockquote>\n"
        "<b>📊 LIVE OVERVIEW</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Users</b>  ➜  <code>{get_total_users()}</code>\n"
        f"📱 <b>Numbers today</b>  ➜  <code>{daily_stats['numbers']}</code>\n"
        f"💬 <b>OTP today</b>  ➜  <code>{daily_stats['otps']}</code>\n"
        f"📡 <b>Live services</b>  ➜  <code>{services}</code>  ·  <code>{ranges}</code> ranges\n"
        f"📲 <b>SMS Pro stock</b>  ➜  <code>{smspro_stock_total()}</code>\n"
        f"🧩 <b>Panels online</b>  ➜  {'🟢' if _ptot and _pon == _ptot else ('🟠' if _pon else '🔴')} <code>{_pon}/{_ptot}</code>\n"
        f"📤 <b>OTP forwarder bots</b>\n<blockquote>{fwd_status_text()}</blockquote>\n"
        f"🗄 <b>Database</b>  ➜  <code>{db}</code>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"<i>🕒 {time.strftime('%d %b %Y  %I:%M %p')}</i>"
    )

def get_leaderboard_text():
    global cached_leaderboard_text
    if cached_leaderboard_text:
        return cached_leaderboard_text
    return "⏳ <b>Loading leaderboard, please wait...</b>"

TRAFFIC_WINDOW = 600                       # "10 Minute LIVE Traffic"
TRAFFIC_TZ_HOURS = float(os.environ.get("TRAFFIC_TZ_HOURS", "6"))   # shown in "Updated hh:mm AM" (6 = Bangladesh)

def _traffic_groups():
    """{service: {"total": n, "countries": {code: {"flag","name","hits"}}, "ranges": {range: hits}}} for the last 10 minutes."""
    cutoff = int(time.time()) - TRAFFIC_WINDOW
    groups = {}
    for h in list(recent_traffic):
        if h.get("local_receive_time", 0) < cutoff: continue
        rng = str(h.get("range", "")).replace("X", "")
        if not re.match(r"^\d+$", rng): continue
        svc = detect_service(h.get("message") or "", h.get("sid") or "")
        if is_hidden_service(svc): continue
        g = groups.setdefault(svc, {"total": 0, "countries": {}, "ranges": {}})
        g["total"] += 1
        g["ranges"][rng] = g["ranges"].get(rng, 0) + 1
        code, cname = get_country_info(rng)
        flag, _, name = cname.partition(" ")
        c = g["countries"].setdefault(code, {"flag": flag, "name": name or flag, "hits": 0})
        c["hits"] += 1
    return groups

def _traffic_level(hits, top):
    r = hits / max(1, top)
    if r >= 0.6: return "HIGH", "🟢"
    if r >= 0.3: return "MEDIUM", "🟡"
    return "LOW", "🔴"

def get_live_traffic_content():
    markup = {"inline_keyboard": [
        [{"text": "Explore Ranges", "callback_data": "explore_ranges", "icon_custom_emoji_id": IC["search"], "style": "success"}],
        [{"text": "Refresh", "callback_data": "refresh_traffic", "icon_custom_emoji_id": "5229111790842952353", "style": "primary"}],
        [{"text": "Close", "callback_data": "close_panel", "icon_custom_emoji_id": IC["back"], "style": "danger"}],
    ]}
    head = f'{_temoji("5337267511261960341", "🔥")} 10 Minute LIVE Traffic\n\n'
    groups = _traffic_groups()
    if not groups:
        return head + "<i>No traffic in the last 10 minutes.</i>", markup
    msg = head
    for svc, g in sorted(groups.items(), key=lambda x: x[1]["total"], reverse=True)[:8]:
        info = PREMIUM_APPS.get(svc, PREMIUM_APPS["Other"])
        msg += f'{_temoji(info["id"], info["emoji"])} {html.escape(svc.upper())} {g["total"]}\n'
        top = max(c["hits"] for c in g["countries"].values())
        for c in sorted(g["countries"].values(), key=lambda c: c["hits"], reverse=True)[:7]:
            lvl, dot = _traffic_level(c["hits"], top)
            fid = GLOBAL_BODY_EMOJIS.get(c["flag"])
            flag_html = _temoji(fid, c["flag"]) if fid else c["flag"]
            dot_html = _temoji(IC["green"], dot) if dot == "🟢" else dot
            msg += f'{flag_html} {html.escape(c["name"].upper())} : {lvl} {dot_html}\n'
        msg += "\n"
    stamp = time.strftime("%I:%M %p", time.gmtime(time.time() + TRAFFIC_TZ_HOURS * 3600))
    msg += f'{_temoji("hour", "🕐")} Updated {stamp}'
    return msg, markup

def get_explore_ranges_content():
    """Top live ranges of the last 10 minutes - tap one to get a number from exactly that range."""
    rows = []
    for svc, g in _traffic_groups().items():
        for rng, hits in g["ranges"].items():
            rows.append((hits, svc, rng))
    rows.sort(reverse=True)
    kb = {"inline_keyboard": []}
    for hits, svc, rng in rows[:12]:
        info = PREMIUM_APPS.get(svc, PREMIUM_APPS["Other"])
        flag = get_country_info(rng)[1].split(" ", 1)[0]
        b = {"text": f"{svc.upper()} · {rng}XXX ({hits})", "callback_data": f"rg_{rng}_{svc}"[:64], "style": "primary", "icon_custom_emoji_id": info["id"]}
        kb["inline_keyboard"].append([b])
    kb["inline_keyboard"].append([{"text": "Back", "callback_data": "refresh_traffic", "icon_custom_emoji_id": IC["back"], "style": "danger"}])
    if not rows:
        return f'{_temoji("search", "🔍")} <b>Explore Ranges</b>\n<i>No live ranges right now.</i>', kb
    return f'{_temoji("search", "🔍")} <b>Explore Ranges</b>\n<i>Tap a range to get a number from it.</i>', kb
    
    if not hits_to_show:
        return "╔═════════════════╗\n║  <tg-emoji emoji-id=\"5352877703043258544\">📈</tg-emoji> <b>15M LIVE TRAFFIC</b>\n╚═════════════════╝\n<i>No traffic data available.</i>\n━━━━━━━━━━━━━━━━━━━", markup

    service_counts = {}
    for h in hits_to_show:
        raw_sid = h.get("sid") or "Unknown"
        raw_msg = h.get("message") or ""
        detected_sid = detect_service(raw_msg, raw_sid)
        app_info = PREMIUM_APPS.get(detected_sid, PREMIUM_APPS["Other"])
        
        if detected_sid not in service_counts: service_counts[detected_sid] = {"count": 0, "emoji_id": app_info["id"], "normal_emoji": app_info["emoji"]}
        service_counts[detected_sid]["count"] += 1

    sorted_services = sorted(service_counts.items(), key=lambda item: item[1]["count"], reverse=True)[:6]

    msg = "╔═════════════════╗\n║  <tg-emoji emoji-id=\"5352877703043258544\">📈</tg-emoji> <b>15M LIVE TRAFFIC</b>\n╚═════════════════╝\n"
    fire_emoji_id = "5337267511261960341"
    
    for i, (sid, data) in enumerate(sorted_services):
        msg += f"<tg-emoji emoji-id=\"{data['emoji_id']}\">{data['normal_emoji']}</tg-emoji> {html.escape(str(sid))} ➜ <tg-emoji emoji-id=\"{fire_emoji_id}\">🔥</tg-emoji> {data['count']} OTP\n"
        if i < len(sorted_services) - 1: msg += "— — — — — — — — — —\n"
    msg += "━━━━━━━━━━━━━━━━━━━"
    return msg, markup

def get_services_content():
    global cached_services_kb
    if cached_services_kb:
        return (f'<blockquote>{_temoji("globe", "🌐")} <b>SELECT A SERVICE</b></blockquote>\n'
                f'<i>{_temoji("arrow", "➤")} Tap a service to see the available countries</i>'), cached_services_kb
    return "⏳ <b>Warming up live services...</b>\n<i>Please try again in a few seconds.</i>", None

CC_ROWS_PER_PAGE = 15   # 15 rows x 2 countries = 30 countries per page (like the video)

def get_ranges_content(sid_short, c_code):
    """Step 3 (video): 'Select WhatsApp' -> one button per active range of that country, e.g. 'WhatsApp5 27s'."""
    rows = []; full_sid = sid_short
    for sid, data in merged_services(True).items():
        if sid.lower().startswith(sid_short.lower()):
            full_sid = sid
            for r, rd in data["ranges"].items():
                clean = str(r).replace("X", "")
                if re.fullmatch(r"\d+", clean) and get_country_info(clean)[0] == c_code:
                    rows.append((int(rd.get("last_seen", 0) or 0), int(rd.get("hits", 0) or 0), clean))
            break
    if not rows: return None, None
    rows.sort(reverse=True)
    info = PREMIUM_APPS.get(detect_service("", full_sid), PREMIUM_APPS["Other"])
    kb = {"inline_keyboard": []}
    for seen, hits, clean in rows[:20]:
        age = age_label(seen)
        kb["inline_keyboard"].append([{"text": f"{full_sid}{hits if hits else ''}" + (f" {age}" if age else ""),
                                       "callback_data": f"rg_{clean}_{full_sid}"[:64], "style": "primary", "icon_custom_emoji_id": info["id"]}])
    kb["inline_keyboard"].append([{"text": "Back", "callback_data": f"srv_{sid_short}", "icon_custom_emoji_id": IC["back"], "style": "danger"}])
    return (f'<blockquote>{_temoji("globe", "🌐")} <b>SELECT {html.escape(full_sid).upper()}</b></blockquote>\n'
            f'<i>{_temoji("arrow", "➤")} Tap a range to get your number</i>'), kb

def get_countries_content(sid_short, v2=False, page=0):
    global cached_countries_kb
    _ck = ("v2:" if v2 else "") + sid_short
    if _ck in cached_countries_kb:
        rows = cached_countries_kb[_ck]["inline_keyboard"][:-1]
        pages = max(1, (len(rows) + CC_ROWS_PER_PAGE - 1) // CC_ROWS_PER_PAGE)
        page = max(0, min(int(page or 0), pages - 1))
        view = [list(r) for r in rows[page * CC_ROWS_PER_PAGE:(page + 1) * CC_ROWS_PER_PAGE]]
        if pages > 1:
            tag = "2" if v2 else "1"
            nav = []
            if page > 0:
                nav.append({"text": "PREV", "callback_data": f"cpg_{tag}_{page - 1}_{sid_short}", "icon_custom_emoji_id": IC["back"], "style": "primary"})
            nav.append({"text": f"Page {page + 1} / {pages}", "callback_data": "noop", "icon_custom_emoji_id": IC["green"], "style": "success"})
            if page < pages - 1:
                nav.append({"text": "NEXT", "callback_data": f"cpg_{tag}_{page + 1}_{sid_short}", "icon_custom_emoji_id": IC["arrow"], "style": "primary"})
            view.append(nav)
        view.append([{"text": "Back To Services", "callback_data": "back_to_services", "icon_custom_emoji_id": IC["back"], "style": "danger"}])
        std_service = detect_service("", sid_short)
        label = html.escape(sid_short).upper() if std_service == "Other" else std_service.upper()
        return (f'<blockquote>{_temoji("globe", "🌐")} <b>{label}</b>' + (" <i>· V2</i>" if (v2 and not svc_has_ver(sid_short)) else "") + '</blockquote>\n'
                f'<i>{_temoji("arrow", "➤")} Select a country</i>'), {"inline_keyboard": view}, None
    return None, None, "❌ <b>No active countries for this service. (Out of Stock)</b>"

# ==========================================
# Number Stock (owner pulls 500 / 1K / 2K numbers per range; users get stocked numbers first)
# ==========================================
STOCK_FILE = "number_stock.json"
STOCK_FETCH_DELAY = max(0.0, float(os.environ.get("STOCK_FETCH_DELAY", "0.3")))
number_stock = {}        # {range: [{"n": full_number, "c": country, "o": operator, "t": epoch}]}
stock_targets = {}       # {range: wanted amount}
stock_jobs = {}          # {range: {"stop": bool}} (fills running right now)
stock_lock = threading.RLock()
stock_sem = threading.Semaphore(2)   # at most 2 ranges are pulled from Stex at the same time
stock_dirty = [False]
stock_retry_at = {}      # {range: epoch} auto refill waits until then after a failed/empty fill (protects the Stex API)
stock_view = {}          # {chat_id: message_id} owner message currently showing the stock page (live progress)

def _k(n):
    n = int(n)
    return f"{n/1000:g}K" if n >= 1000 else str(n)

def stock_load():
    global number_stock, stock_targets
    try:
        if os.path.exists(STOCK_FILE):
            with open(STOCK_FILE, "r") as f:
                d = json.load(f)
            with stock_lock:
                number_stock = d.get("stock", {})
                stock_targets = {k: int(v) for k, v in d.get("targets", {}).items()}
    except Exception as e:
        print(f"Stock load error: {e}")

def stock_save():
    try:
        with stock_lock:
            blob = json.dumps({"stock": number_stock, "targets": stock_targets})
            stock_dirty[0] = False
        tmp = STOCK_FILE + ".tmp"
        with open(tmp, "w") as f: f.write(blob)
        os.replace(tmp, STOCK_FILE)
    except Exception as e:
        print(f"Stock save error: {e}")

def stock_total():
    with stock_lock:
        return sum(len(v) for v in number_stock.values())

def stock_pop(candidates):
    """Give one stocked number whose range matches one of the candidate ranges (exact match first)."""
    cand = [str(c) for c in (candidates or []) if c]
    if not cand: return None
    ttl = int(bot_settings.get("stock_ttl_min", 0) or 0) * 60
    now = time.time()
    with stock_lock:
        keys = [k for k, v in number_stock.items() if v]
        pick = [k for k in keys if k in cand] or [k for k in keys if any(k.startswith(c) or c.startswith(k) for c in cand)]
        random.shuffle(pick)
        for k in pick:
            lst = number_stock[k]
            while lst:
                e = lst.pop(0)
                stock_dirty[0] = True
                if ttl and now - e.get("t", 0) > ttl: continue
                return e
    return None

def stock_panel_text():
    with stock_lock:
        total = sum(len(v) for v in number_stock.values())
        ranges = sum(1 for v in number_stock.values() if v)
        running = [(r, len(number_stock.get(r, [])), stock_targets.get(r, 0)) for r in stock_jobs]
    ttl = int(bot_settings.get("stock_ttl_min", 0) or 0)
    lines = [
        "📦 <b>NUMBER STOCK</b>",
        "━━━━━━━━━━━━━━━━━",
        f"🔢 <b>Total</b> ➜ <code>{total}</code> numbers in <code>{ranges}</code> ranges",
        f"🎯 <b>Default per range</b> ➜ <code>{_k(bot_settings.get('stock_default', 1000))}</code>",
        f"♻️ <b>Auto refill</b> ➜ {'ON' if bot_settings.get('stock_auto', True) else 'OFF'}   ⏱ <b>Expiry</b> ➜ {'OFF' if not ttl else str(ttl) + ' min'}",
    ]
    if running:
        lines.append("⏳ <b>Filling now</b>")
        for r, cur, tgt in running: lines.append(f"   <code>{r}</code>  {cur}/{tgt}")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("<i>Users get stocked numbers first. If a range is empty, the bot asks Stex live as before.</i>")
    return "\n".join(lines)

def stock_keyboard():
    kb = {"inline_keyboard": [[{"text": "ADD RANGE", "callback_data": "stk_add", "style": "success"}]]}
    with stock_lock:
        rows = sorted(number_stock.items(), key=lambda x: -len(x[1]))[:12]
        for r, lst in rows:
            kb["inline_keyboard"].append([{"text": f"DELETE {r}  •  {len(lst)}/{_k(stock_targets.get(r, 0))}", "callback_data": f"stk_del_{r}", "style": "danger"}])
    ttl = int(bot_settings.get("stock_ttl_min", 0) or 0)
    kb["inline_keyboard"].append([
        {"text": f"AUTO REFILL: {'ON' if bot_settings.get('stock_auto', True) else 'OFF'}", "callback_data": "stk_auto", "style": "primary"},
        {"text": f"EXPIRY: {'OFF' if not ttl else str(ttl) + 'm'}", "callback_data": "stk_ttl", "style": "primary"},
    ])
    kb["inline_keyboard"].append([
        {"text": f"DEFAULT: {_k(bot_settings.get('stock_default', 1000))}", "callback_data": "stk_def", "style": "primary"},
        {"text": "REFRESH", "callback_data": "manage_stock", "style": "success"},
    ])
    kb["inline_keyboard"].append([
        {"text": "STOP FILLING", "callback_data": "stk_stop", "style": "danger"},
        {"text": "CLEAR ALL", "callback_data": "stk_clear", "style": "danger"},
    ])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5267490665117275176", "style": "danger"}])
    return kb

def show_stock_panel(chat_id, message_id):
    stock_view[chat_id] = message_id
    edit_message(chat_id, message_id, stock_panel_text(), reply_markup=stock_keyboard())

def stock_refresh_panel(chat_id, msg_id):
    # only touch the owner's message while it is still showing the stock page
    if chat_id and msg_id and stock_view.get(chat_id) == msg_id:
        edit_message(chat_id, msg_id, stock_panel_text(), reply_markup=stock_keyboard())

def stock_fill(rng, target, chat_id=None, msg_id=None, notify=True):
    """Pull numbers for one range from Stex until the stock for it reaches `target`."""
    rng, target = str(rng), int(target)
    with stock_lock:
        if rng in stock_jobs: return
        stock_jobs[rng] = {"stop": False}
        stock_targets[rng] = target
        known = {e["n"] for e in number_stock.get(rng, [])}
        start_count = len(known)
    fails = dups = 0; last_ui = 0; why = ""
    try:
        with stock_sem:
            while True:
                job = stock_jobs.get(rng)
                if not job or job["stop"]: why = "stopped"; break
                with stock_lock:
                    if len(number_stock.get(rng, [])) >= target: break
                data = fetch_live_number(rng)
                full = str((data or {}).get("full_number", "")).strip()
                if not full:
                    fails += 1
                    if fails >= 8: why = "Stex did not return numbers for this range"; break
                    time.sleep(2); continue
                fails = 0
                with stock_lock:
                    if full in known:
                        dups += 1
                        if dups >= 40: why = "range has no more new numbers"; break
                        continue
                    dups = 0; known.add(full)
                    number_stock.setdefault(rng, []).append({"n": full, "c": str(data.get("country", "Unknown")), "o": str(data.get("operator", "Unknown")), "t": int(time.time())})
                    stock_dirty[0] = True
                if chat_id and msg_id and time.time() - last_ui > 5:
                    last_ui = time.time(); stock_refresh_panel(chat_id, msg_id)
                if STOCK_FETCH_DELAY: time.sleep(STOCK_FETCH_DELAY)
    except Exception as e:
        why = f"error: {e}"
    finally:
        with stock_lock:
            stock_jobs.pop(rng, None)
            cur = len(number_stock.get(rng, []))
        if why and why != "stopped" and cur < target: stock_retry_at[rng] = time.time() + 600
        else: stock_retry_at.pop(rng, None)
        stock_save()
        stock_refresh_panel(chat_id, msg_id)
        if notify:
            added = cur - start_count
            if why and why != "stopped" and cur < target:
                send_message(ADMIN_ID, f"⚠️ Range <code>{rng}</code>: stocked {cur}/{target} (+{added}). Stopped: {html.escape(why)}")
            elif not why:
                send_message(ADMIN_ID, f"✅ Range <code>{rng}</code> stocked: <b>{cur}</b> numbers (+{added})")

def stock_start(rng, target, chat_id=None, msg_id=None, notify=True):
    threading.Thread(target=stock_fill, args=(rng, target, chat_id, msg_id, notify), daemon=True).start()

def stock_worker():
    """Background: save stock to disk, drop expired numbers, auto refill ranges that fell below 80% of target."""
    last_refill = 0
    while True:
        try:
            now = time.time()
            ttl = int(bot_settings.get("stock_ttl_min", 0) or 0) * 60
            if ttl:
                with stock_lock:
                    for lst in number_stock.values():
                        keep = [e for e in lst if now - e.get("t", 0) <= ttl]
                        if len(keep) != len(lst): lst[:] = keep; stock_dirty[0] = True
            if stock_dirty[0]: stock_save()
            if bot_settings.get("stock_auto", True) and v1_on() and now - last_refill > 30:
                last_refill = now
                for r, tgt in list(stock_targets.items()):
                    with stock_lock:
                        cur = len(number_stock.get(r, [])); busy = r in stock_jobs
                    if tgt and not busy and cur <= tgt * 0.8 and now >= stock_retry_at.get(r, 0):
                        stock_start(r, tgt, notify=False)
        except Exception:
            pass
        time.sleep(5)


auto_range_posted = {}   # {range: epoch} so a range is not re-posted to the group again and again

def auto_range_worker():
    """Every minute: take the top live-traffic ranges (by hits), stock them and post new ones in the range group(s)."""
    while True:
        try:
            if bot_settings.get("auto_range_on", True) and voltx_dynamic_data and v1_on():
                top_n = int(bot_settings.get("auto_range_top", 5) or 5)
                target = int(bot_settings.get("stock_default", 1000))
                scored = {}
                _fresh = time.time() - 6 * 3600
                for sid, d in list(voltx_dynamic_data.items()):
                    for r, rd in list(d.get("ranges", {}).items()):
                        if re.fullmatch(r"\d{5,13}", str(r)) and int(rd.get("last_seen", 0) or 0) >= _fresh:
                            bonus = 10**6 if str(sid).lower() == "whatsapp" else 0     # WhatsApp ranges are stocked first
                            scored[r] = scored.get(r, 0) + int(rd.get("hits", 0)) + bonus
                top = [r for r, h in sorted(scored.items(), key=lambda x: x[1], reverse=True)[:top_n] if h > 0]
                now = time.time(); new_post = []
                for r in top:
                    with stock_lock:
                        busy = r in stock_jobs; cur = len(number_stock.get(r, []))
                    if not busy and cur < target and now >= stock_retry_at.get(r, 0):
                        stock_start(r, target, notify=False)
                    if range_groups and now - auto_range_posted.get(r, 0) > 6 * 3600:
                        auto_range_posted[r] = now; new_post.append(r)
                if new_post:
                    txt = "📈 <b>Traffic ranges</b>\n" + "\n".join(f"<code>{r}XXX</code>" for r in new_post)
                    for g in list(range_groups):
                        try: send_message(g, txt)
                        except Exception: pass
        except Exception:
            pass
        time.sleep(60)

GROUP_RANGE_RE = re.compile(r"\+?(\d{5,12})[xX]{2,}")

def extract_ranges(text, strict=False):
    """Find ranges like 2519474XXX in text (one per line is fine). strict=True (groups) needs the XX ending so OTP codes are never read as ranges."""
    found = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line: continue
        hits = GROUP_RANGE_RE.findall(line)
        if not hits and len(line) <= 40:
            m = re.fullmatch(r"(?:range|rid)?[:=]?\+?(\d{5,12})[xX]{2,}", re.sub(r"[\s\-\(\)]", "", line), re.I)
            if m: hits = [m.group(1)]
        if not hits and not strict:
            r = parse_range_input(line)
            if r: hits = [r]
        for h in hits:
            if h not in found: found.append(h)
    return found[:30]

def handle_range_group_message(message):
    """A message in a range group: every range in it (e.g. 2519474XXX) gets stocked automatically."""
    try:
        gid = str(message["chat"]["id"])
        if gid not in [str(g) for g in range_groups]: return
        if (message.get("from") or {}).get("is_bot"): return
        ranges = extract_ranges(message.get("text") or message.get("caption") or "", strict=True)[:5]
        target = int(bot_settings.get("stock_default", 1000))
        started = []
        for r in ranges:
            with stock_lock:
                busy = r in stock_jobs; cur = len(number_stock.get(r, []))
            if busy or cur >= target: continue
            started.append(r); stock_start(r, target)
        if started:
            send_message(ADMIN_ID, "🗂 <b>Range group</b> ➜ stocking " + ", ".join(f"<code>{r}</code>" for r in started) + f" ({_k(target)} each)")
    except Exception:
        pass

def get_range_group_keyboard():
    kb = {"inline_keyboard": []}
    for i, g in enumerate(range_groups):
        kb["inline_keyboard"].append([{"text": f"Delete Group: {g}", "callback_data": f"rgrp_del_{i}", "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "Add Range Group", "callback_data": "rgrp_add", "style": "success"}])
    kb["inline_keyboard"].append([
        {"text": f"AUTO RANGE (TRAFFIC): {'ON' if bot_settings.get('auto_range_on', True) else 'OFF'}", "callback_data": "rgrp_auto", "style": "primary"},
        {"text": f"TOP: {bot_settings.get('auto_range_top', 5)}", "callback_data": "rgrp_top", "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "ADD SERVICE", "callback_data": "manage_msvc", "style": "success"}, {"text": "NUMBER STOCK", "callback_data": "manage_stock", "style": "primary"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5267490665117275176", "style": "danger"}])
    return kb

RANGE_GROUP_TEXT = ("🗂 <b>RANGE GROUP</b>\n━━━━━━━━━━━━━━━━━\n"
                    "Any range posted in these groups (like <code>2519474XXX</code>) is stocked automatically.\n"
                    "With <b>AUTO RANGE</b> ON the bot also adds the highest-traffic live ranges itself (stock + post in the group).\n"
                    "<i>Add the bot to the group as admin so it can read messages.</i>")

def stock_back_keyboard():
    return {"inline_keyboard": [[{"text": "BACK", "callback_data": "manage_stock", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}]]}

def get_number_allocation_content(sid_short, c_code, user_id=None, exact_range=False):
    global voltx_auto_mode, voltx_dynamic_data, user_active_sessions, bot_settings, otp_button_link
    if not v1_on():
        return ("❌ <b>This service is currently unavailable.</b>",
                {"inline_keyboard": [[{"text": "BACK", "callback_data": "back_to_services", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}]]})
    target_ranges = []
    full_sid = sid_short
    back_cb = "back_to_services" if exact_range else f"srv_{sid_short}"

    if exact_range:
        best_ranges = [c_code]
    elif voltx_auto_mode or any(k.lower().startswith(sid_short.lower()) for k in (bot_settings.get("manual_services") or {})):
        manual_rs = []
        for sid, data in merged_services(voltx_auto_mode).items():
            if sid.lower().startswith(sid_short.lower()):
                full_sid = sid
                for r, r_data in data["ranges"].items():
                    if c_code.startswith(r) or r.startswith(c_code): 
                        target_ranges.append((r, r_data["hits"]))
                        if r_data.get("manual"): manual_rs.append(r)
                break
        target_ranges.sort(key=lambda x: x[1], reverse=True)
        best_ranges = [x[0] for x in target_ranges[:3]]
        # every owner added range of this country is used (4/5 ranges => numbers rotate between all of them)
        for r in manual_rs:
            if r not in best_ranges: best_ranges.append(r)
    else:
        services_data = fetch_live_access()
        for s in services_data:
            sid_val = s.get('sid') or ""
            if sid_val.lower().startswith(sid_short.lower()):
                full_sid = sid_val
                for r in s.get('ranges', []):
                    clean_r = str(r).replace("X", "")
                    if c_code.startswith(clean_r) or clean_r.startswith(c_code):
                        if clean_r not in target_ranges:
                            target_ranges.append(clean_r)
                break
        best_ranges = target_ranges[:3]
            
    # 🌟 এপিআই লিস্টে না থাকলেও লিংকের রেঞ্জ দিয়ে সরাসরি রিকোয়েস্ট করবে
    if not best_ranges: 
        best_ranges = [c_code]
        
    req_count = bot_settings.get("num_req", 1)
    fetched_numbers = []
    
    for _ in range(req_count):
        selected_rid = random.choice(best_ranges)
        stocked = stock_pop(best_ranges)
        if stocked:
            full_number = html.escape(str(stocked["n"]))
            fetched_numbers.append((full_number, html.escape(str(stocked.get("c", "Unknown"))), html.escape(str(stocked.get("o", "Unknown")))))
            if user_id and full_number: user_active_sessions[str(full_number)] = user_id
            continue
        num_data = fetch_live_number(selected_rid)
        if num_data:
            full_number = html.escape(str(num_data.get("full_number", "")))
            country = html.escape(str(num_data.get("country", "Unknown")))
            operator = html.escape(str(num_data.get("operator", "Unknown")))
            fetched_numbers.append((full_number, country, operator))
            
            if user_id and full_number:
                user_active_sessions[str(full_number)] = user_id
    
    if fetched_numbers:
        update_daily_stat("numbers", len(fetched_numbers))
        save_local_data()
        
        _nums = [str(n[0]).lstrip("+") for n in fetched_numbers]
        _change = (f"rg_{c_code}_{sid_short}" if exact_range else f"cc_{sid_short}_{c_code}")
        return build_number_card(full_sid, c_code, _nums, _change, back_cb)
    else:
        return f"❌ <b>Out of stock in these ranges. Please try again.</b>", {"inline_keyboard": [[{"text": "BACK", "callback_data": back_cb, "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}]]}

# ==========================================
# SMS PRO PANEL  (second number panel - works next to Stex SMS)
#   * numbers come from a .txt file uploaded by the owner (service is asked after upload)
#   * different ranges can be added to the same service (upload again, pick the same service)
#   * OTPs are read from the SMS Pro API (sms-logs) and auto forwarded (user inbox + OTP group + live feed)
# ==========================================
SMSPRO_FILE = "smspro_data.json"
SMSPRO_DEFAULT_URL = os.environ.get("SMSPRO_API_URL", "https://smspro.tel/api/v1/sms-logs").strip()
SMSPRO_POLL_SECONDS = max(3, int(os.environ.get("SMSPRO_POLL_SECONDS", "3")))
SMSPRO_SESSION_TTL = 20 * 60          # a taken number waits this long for its OTP
SMSPRO_AUTH_MODES = ["auto", "bearer", "header", "query"]
smspro_lock = threading.RLock()
smspro = {
    "keys": [],            # SMS Pro API keys (several accounts allowed)
    "url": SMSPRO_DEFAULT_URL,
    "auth": "auto",        # how the key is sent: auto (all ways) / bearer / header (X-API-KEY) / query (?api_key=)
    "auto": True,          # OTP listener ON/OFF
    "stock": {},           # {service: {range: [numbers]}}
    "sessions": {},        # {number: {"uid": 123, "svc": "Facebook", "ts": 1700000000}}
    "seen": [],            # processed log ids (so one OTP is never forwarded twice)
    "baseline": False,     # first poll only marks old logs as seen (no flood of old OTPs)
    "last_status": "not started",
    "panels": [],          # extra panels added by the owner (name + base URL + API key), see MULTI PANEL section
    "alerts": True,        # admin alerts: panel down / low stock
    "low_stock": 20,       # alert when a service has this many numbers (or less) left
    "main_panel": {},      # pseudo panel of the main SMS Pro key (learned ranges, numbers path)
    "auto_range": True,    # main SMS Pro: learn active ranges from OTP logs + auto add numbers per range
    "did_url": "",         # main SMS Pro: numbers (DID) request URL, placeholders {range_name} {range} {limit} {key}
    "range_target": 500,
    "cpanels": [],         # Auto Captcha panels (login + math captcha + JSON link), see AUTO CAPTCHA PANELS section
}
smspro_seen_set = set()

def smspro_load():
    global smspro_seen_set
    if os.path.exists(SMSPRO_FILE):
        try:
            with open(SMSPRO_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k in smspro:
                if k in d: smspro[k] = d[k]
        except Exception as e:
            print(f"SMS Pro load error: {e}")
    env_key = os.environ.get("SMSPRO_API_KEY", "").strip()
    if env_key and env_key not in smspro["keys"]: smspro["keys"].append(env_key)
    smspro_seen_set = set(smspro.get("seen", []))

def smspro_save():
    with smspro_lock:
        try:
            smspro["seen"] = list(smspro_seen_set)[-4000:]
            try: _xp_pre_save()
            except Exception: pass
            tmp = SMSPRO_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(smspro, f)
            os.replace(tmp, SMSPRO_FILE)
        except Exception as e:
            print(f"SMS Pro save error: {e}")

def _nk(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

def smspro_clean_service(name):
    name = re.sub(r"[^A-Za-z0-9 .\-]", "", str(name or "")).strip()
    name = re.sub(r"\s+", " ", name)[:15].strip()
    if not name: return ""
    for k in PREMIUM_APPS:
        if k != "Other" and k.lower() == name.lower(): return k
    return name

def panel_svc(p, name):
    """Service name for a panel. Panel version V2 -> 'Facebook', V3 -> 'Facebook V3', V4 -> 'Facebook V4' ..."""
    base = smspro_clean_service(name)
    if not base: return ""
    try: v = int((p or {}).get("ver", 2) or 2)
    except Exception: v = 2
    if v >= 3 and not svc_has_ver(base):
        suf = f" V{v}"; base = base[:15 - len(suf)].strip() + suf
    return base

def _ctx_panel(state_data):
    kind, _, pid = str((state_data or {}).get("ctx") or "").partition(":")
    if kind == "xp": return "xp", _xp_get(pid)
    if kind == "cp": return "cp", cp_find(pid)
    return None, None

def _panel_back_kb(kind, p):
    if kind and p: return {"inline_keyboard": [[{"text": "BACK", "callback_data": f"{kind}pa_open_{p['id']}", "style": "danger"}]]}
    return smspro_back_kb()

def _panel_page(kind, p):
    if kind == "xp" and p: return xp_panel_text(p), xp_panel_kb(p)
    if kind == "cp" and p: return cp_panel_text(p), cp_panel_kb(p)
    return smspro_panel_text(), smspro_panel_keyboard()

def smspro_parse_numbers(text):
    """Every number in a txt file / pasted text. One per line (commas / tabs also ok). 8-15 digits."""
    out, seen = [], set()
    for line in str(text or "").splitlines():
        for tok in re.split(r"[,;\t|]+", line):
            parts = [tok]
            if len(re.sub(r"\D", "", tok)) > 15: parts = tok.split()
            for p in parts:
                d = re.sub(r"\D", "", p)
                if 8 <= len(d) <= 15 and d not in seen:
                    seen.add(d); out.append(d)
    return out

def smspro_range_of(number):
    """Range of a number = country code + next 4 digits (like 2519474XXX style ranges)."""
    code, _ = get_country_info(number)
    n = len(code) if number.startswith(code) else 3
    return number[:max(4, min(len(number) - 3, n + 4))]

def smspro_add_numbers(service, numbers):
    service = smspro_clean_service(service)
    with smspro_lock:
        existing = set(smspro["sessions"].keys())
        for rngs in smspro["stock"].values():
            for lst in rngs.values(): existing.update(lst)
        key = next((k for k in smspro["stock"] if k.lower() == service.lower()), service)
        added, dup, per_range = 0, 0, {}
        for n in numbers:
            if n in existing: dup += 1; continue
            r = smspro_range_of(n)
            smspro["stock"].setdefault(key, {}).setdefault(r, []).append(n)
            existing.add(n); added += 1
            per_range[r] = per_range.get(r, 0) + 1
    smspro_save()
    return key, added, dup, per_range

def smspro_stock_total():
    with smspro_lock:
        return sum(len(l) for rngs in smspro["stock"].values() for l in rngs.values())

def smspro_view():
    """{service: {country_code: {"name": ..., "count": n}}} only where numbers are available."""
    out = {}
    with smspro_lock:
        for svc, rngs in smspro["stock"].items():
            cm = {}
            for r, lst in rngs.items():
                if not lst: continue
                code, cname = get_country_info(r)
                e = cm.setdefault(code, {"name": cname, "count": 0})
                e["count"] += len(lst)
            if cm: out[svc] = cm
    return out

def smspro_take(sid_short, c_code, count, by_prefix=None):
    """Take `count` numbers of a service + country. Several ranges of the same country rotate randomly."""
    with smspro_lock:
        key = _nk(sid_short)
        svc = next((k for k in smspro["stock"] if _nk(k) == key), None) or \
              next((k for k in smspro["stock"] if _nk(k).startswith(key)), None)
        if not svc: return None, []
        rngs = smspro["stock"][svc]
        pools = []
        if by_prefix:
            pools = [l for r, l in rngs.items() if l and (r.startswith(by_prefix) or by_prefix.startswith(r))]
        if not pools:
            pools = [l for r, l in rngs.items() if l and (r.startswith(c_code) or c_code.startswith(r))]
        got = []
        for _ in range(count):
            pools = [l for l in pools if l]
            if not pools: break
            got.append(random.choice(pools).pop(0))
        if got:
            now = time.time()
            for n in got: smspro["sessions"][n] = {"uid": None, "svc": svc, "ts": now}
        for r in [r for r, l in rngs.items() if not l]: rngs.pop(r, None)
        if not rngs: smspro["stock"].pop(svc, None)
    return svc, got

def get_smspro_allocation_content(sid_short, c_code, user_id=None, by_prefix=None):
    req_count = max(1, int(bot_settings.get("num_req", 1) or 1))
    svc, nums = smspro_take(sid_short, c_code, req_count, by_prefix)
    safe_sid = html.escape(str(svc or sid_short))[:15]
    back_cb = f"sv2_{safe_sid}"
    if not nums:
        return ("❌ <b>Out of stock. Please try again later.</b>",
                {"inline_keyboard": [[{"text": "BACK", "callback_data": "back_to_services", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}]]})
    with smspro_lock:
        for n in nums:
            if n in smspro["sessions"]: smspro["sessions"][n]["uid"] = user_id
        smspro["hot_until"] = time.time() + 240
    smspro_save()
    update_daily_stat("numbers", len(nums))
    save_local_data()

    if by_prefix: c_code = get_country_info(by_prefix)[0]
    return build_number_card(svc, c_code, [str(n) for n in nums], f"sp_{safe_sid}_{c_code}", back_cb)

# ---------- SMS Pro API (sms-logs) ----------
_SP_TEXT_KEYS = ("message", "body", "text", "sms", "content", "msg", "sms_text", "message_text", "smsbody")
_SP_NUM_KEYS = ("to", "number", "phone", "recipient", "msisdn", "destination", "receiver", "did", "phone_number",
                "to_number", "dest", "dst", "destination_number", "mobile", "num", "did_number")
_SP_FROM_KEYS = ("from", "sender", "sid", "service", "source", "cli", "from_number", "originator", "sender_id", "senderid", "app")
_SP_ID_KEYS = ("id", "uuid", "log_id", "message_id", "sms_id", "_id", "msg_id")
_SP_TIME_KEYS = ("created_at", "time", "timestamp", "date", "received_at", "datetime", "createdat", "sent_at", "dt")
_SP_LIST_KEYS = ("data", "logs", "sms_logs", "smslogs", "sms", "items", "results", "messages", "records", "rows", "hits", "otps")

def smspro_request(key):
    mode = smspro.get("auth", "auto")
    headers, params = {"Accept": "application/json"}, {}
    if mode in ("auto", "bearer"): headers["Authorization"] = f"Bearer {key}"
    if mode in ("auto", "header"): headers.update({"X-API-KEY": key, "Api-Key": key})
    if mode in ("auto", "query"): params.update({"api_key": key, "key": key, "token": key})
    return requests.get(smspro.get("url") or SMSPRO_DEFAULT_URL, headers=headers, params=params, timeout=15)

def smspro_extract_logs(data, depth=0):
    if depth > 4: return []
    if isinstance(data, list): return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        lo = {str(k).lower(): v for k, v in data.items()}
        for k in _SP_LIST_KEYS:
            v = lo.get(k)
            if isinstance(v, list): return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                r = smspro_extract_logs(v, depth + 1)
                if r: return r
    return []

def smspro_norm(e):
    """One log entry -> {"sig", "text", "numbers": [digits...], "sender"} or None."""
    lo = {str(k).lower(): v for k, v in e.items()}
    def first(keys):
        for k in keys:
            v = lo.get(k)
            if v not in (None, "", [], {}): return v
        return ""
    text = str(first(_SP_TEXT_KEYS)).strip()
    if not text: return None
    nums = []
    for k in _SP_NUM_KEYS:
        d = re.sub(r"\D", "", str(lo.get(k, "")))
        if 7 <= len(d) <= 15 and d not in nums: nums.append(d)
    sender = str(first(_SP_FROM_KEYS))
    d_from = re.sub(r"\D", "", sender)
    if 7 <= len(d_from) <= 15 and d_from not in nums: nums.append(d_from)   # last resort (inbound logs)
    if not nums: return None
    lid = str(first(_SP_ID_KEYS))
    if lid: sig = "sp:" + lid
    else: sig = "sp:" + hashlib.md5(f"{first(_SP_TIME_KEYS)}|{nums[0]}|{text}".encode("utf-8", "ignore")).hexdigest()
    return {"sig": sig, "text": text, "numbers": nums, "sender": sender, "range_name": str(first(("range_name", "range", "rangename", "range_id"))).strip()}

def smspro_find_session(numbers, mask=None):
    """Which waiting user owns this OTP? Full number -> last 8 digits; masked number (237624XXX405) -> visible start + end digits."""
    now = time.time()
    pre = suf = ""
    if mask:
        m = re.match(r"^\+?(\d*)[^\d]+(\d*)$", re.sub(r"[\s\-()]", "", str(mask)))
        if m and len(m.group(1)) + len(m.group(2)) >= 6: pre, suf = m.group(1), m.group(2)
    with smspro_lock:
        live = [(n, s) for n, s in smspro["sessions"].items() if s.get("uid") and now - s.get("ts", 0) <= SMSPRO_SESSION_TTL]
        if pre or suf:
            hits = [(n, s) for n, s in live if n.startswith(pre) and n.endswith(suf) and len(n) >= len(pre) + len(suf)]
            if len(hits) >= 1: return max(hits, key=lambda x: x[1].get("ts", 0))   # newest taker wins if several fit the mask
        for num in numbers:
            last8 = num[-8:]
            for s_num, s in live:
                if s_num.endswith(last8): return s_num, s
    return None, None


_CP_MASK_CH = "Xx*•#?"


def _cp_number_cell(text):
    """Cell text -> (digits, mask). Normal number: ('237624433405', None). Masked: ('237624405', '237624XXX405'). Not a number: (None, None)."""
    c = re.sub(r"[\s()\-]", "", _cp_text(text))
    if not c or len(c) > 22 or re.search(r"\d{4}-\d{2}-\d{2}|:", _cp_text(text)): return None, None
    core = c.lstrip("+")
    if core.isdigit(): return (core, None) if 7 <= len(core) <= 15 else (None, None)
    if re.fullmatch(r"\d*[" + re.escape(_CP_MASK_CH) + r"]{2,}\d*", core):
        digits = re.sub(r"\D", "", core)
        if len(digits) >= 6: return digits, re.sub(r"[" + re.escape(_CP_MASK_CH) + r"]+", "X", core)
    return None, None

def _sp_forward_entry(ent):
    """One new OTP log entry -> live feed, OTP groups, owner inbox. Used by SMS Pro and every extra panel."""
    s_num, sess = smspro_find_session(ent["numbers"], ent.get("mask"))
    number = s_num or ent["numbers"][0]
    service = detect_service(ent["text"], ent["sender"])
    feed_add(ent["sig"], number, service, ent["text"])
    if otp_forward_groups:
        formatted, markup = generate_otp_display(service, number, ent["text"], source="smspro")
        send_otp_to_groups(formatted, markup, service)
        if sess: time.sleep(0.5)        # group first, user chat half a second later
    if sess:
        owner_id = sess["uid"]
        with smspro_lock: smspro["sessions"].pop(s_num, None)
        feed_set_owner(ent["sig"], owner_id)
        reward = get_otp_reward(service)
        update_user_stats(owner_id, reward, 0)
        bal = (get_user(owner_id) or {}).get("balance", 0.0)
        deliver_to_inbox(owner_id, service, number, ent["text"], bal, reward)
        today = time.strftime("%Y-%m-%d")
        if daily_user_otps.get("date") != today:
            daily_user_otps["date"] = today; daily_user_otps["users"] = {}
        daily_user_otps["users"][str(owner_id)] = daily_user_otps["users"].get(str(owner_id), 0) + 1
    update_daily_stat("otps", 1)
    return 1

def smspro_poll_once():
    total = 0
    for key in list(smspro["keys"]):
        try:
            r = smspro_request(key)
        except Exception as e:
            smspro["last_status"] = f"connection error: {str(e)[:80]}"; continue
        if r.status_code != 200:
            smspro["last_status"] = f"HTTP {r.status_code}"; continue
        try: data = r.json()
        except Exception:
            smspro["last_status"] = "reply is not JSON"; continue
        logs = smspro_extract_logs(data)
        entries = [x for x in (smspro_norm(l) for l in logs) if x]
        smspro["last_status"] = f"OK · {len(logs)} logs"
        try: xp_learn_ranges(smspro_main_panel(), [x for x in entries if x["sig"] not in smspro_seen_set])
        except Exception as ex: print("main learn error:", ex)
        if not smspro.get("baseline"):
            for x in entries: smspro_seen_set.add(x["sig"])
            continue
        for ent in reversed(entries):
            if ent["sig"] in smspro_seen_set: continue
            smspro_seen_set.add(ent["sig"])
            if not re.search(r"\d{4,8}", ent["text"]): continue        # not an OTP style message
            total += _sp_forward_entry(ent)
    if smspro["keys"] and not smspro.get("baseline") and "OK" in smspro.get("last_status", ""):
        smspro["baseline"] = True
    if total: save_local_data()
    return total

def smspro_sms_listener():
    while True:
        try:
            if smspro.get("auto") and smspro.get("keys"):
                before = len(smspro_seen_set)
                smspro_poll_once()
                now = time.time()
                with smspro_lock:
                    for n in [n for n, s in smspro["sessions"].items() if now - s.get("ts", 0) > SMSPRO_SESSION_TTL * 3]:
                        smspro["sessions"].pop(n, None)
                if len(smspro_seen_set) != before: smspro_save()
        except Exception as e:
            smspro["last_status"] = f"error: {str(e)[:80]}"
        time.sleep(SMSPRO_POLL_SECONDS)

def smspro_test_text():
    if not smspro["keys"]:
        return "❌ <b>No SMS Pro API key set.</b>\nTap <b>ADD API KEY</b> first."
    try:
        r = smspro_request(smspro["keys"][0])
    except Exception as e:
        return f"❌ <b>Connection failed</b>\n<code>{html.escape(str(e)[:300])}</code>"
    lines = [f"🧪 <b>SMS PRO API TEST</b>", f"HTTP <code>{r.status_code}</code> · <code>{html.escape(smspro.get('url', ''))}</code>"]
    try:
        data = r.json()
        logs = smspro_extract_logs(data)
        ents = [x for x in (smspro_norm(l) for l in logs) if x]
        lines.append(f"Logs found: <code>{len(logs)}</code> · readable (number + message): <code>{len(ents)}</code>")
        if ents:
            e0 = ents[0]
            lines.append(f"Sample ➜ <code>+{e0['numbers'][0]}</code> from <code>{html.escape(e0['sender'][:20])}</code>\n<code>{html.escape(e0['text'][:120])}</code>")
        elif logs:
            lines.append("⚠️ Logs came but number/message fields were not recognised. Raw first log:\n<code>" + html.escape(json.dumps(logs[0], ensure_ascii=False)[:500]) + "</code>")
        else:
            lines.append("Raw reply:\n<code>" + html.escape(r.text[:500]) + "</code>")
    except Exception:
        lines.append("Raw reply:\n<code>" + html.escape(r.text[:500]) + "</code>")
    return "\n".join(lines)

# ---------- SMS Pro admin panel ----------
def smspro_panel_text():
    with smspro_lock:
        stock_lines = []
        for svc, rngs in smspro["stock"].items():
            tot = sum(len(l) for l in rngs.values())
            by_c = {}
            for r, l in rngs.items():
                if l:
                    cn = get_country_info(r)[1]
                    by_c[cn] = by_c.get(cn, 0) + len(l)
            stock_lines.append(f"📦 <b>{html.escape(svc)}</b> ➜ <code>{tot}</code> numbers · {len(rngs)} range" + ("s" if len(rngs) != 1 else ""))
            for cn, c in list(by_c.items())[:6]:
                stock_lines.append(f"   {html.escape(cn)} ➜ <code>{c}</code>")
        waiting = sum(1 for s in smspro["sessions"].values() if s.get("uid"))
    return (
        "📲 <b>SMS PRO SETTINGS</b>\n━━━━━━━━━━━━━━━━━\n"
        f"🔌 <b>OTP listener</b> ➜ <code>{'ON' if smspro.get('auto') else 'OFF'}</code> · <code>{html.escape(str(smspro.get('last_status', '')))}</code>\n"
        f"🔑 <b>API keys</b> ➜ <code>{len(smspro['keys'])}</code>   🔐 <b>Auth</b> ➜ <code>{smspro.get('auth', 'auto')}</code>\n"
        f"🌐 <code>{html.escape(str(smspro.get('url', '')))}</code>\n"
        f"🧩 <b>Extra panels</b> ➜ <code>{len(smspro.get('panels', []))}</code>\n"
        f"📈 <b>Auto range</b> ➜ <code>{'ON' if smspro.get('auto_range', True) else 'OFF'}</code> · <code>{len((smspro.get('main_panel') or {}).get('ranges') or {})}</code> active · <code>{int(smspro.get('range_target', 500))}</code>/range\n"
        f"📱 <b>Stock</b> ➜ <code>{smspro_stock_total()}</code>   ⏳ <b>Waiting OTP</b> ➜ <code>{waiting}</code>\n"
        "━━━━━━━━━━━━━━━━━\n" + ("\n".join(stock_lines) if stock_lines else "<i>No numbers added yet. Tap ADD NUMBERS and send a .txt file.</i>")
    )

def smspro_panel_keyboard():
    kb = {"inline_keyboard": [
        [{"text": "ADD SERVICE", "callback_data": "spa_newsvc", "style": "success"},
         {"text": "ADD NUMBERS (TXT FILE)", "callback_data": "spa_add", "style": "success"}],
        [{"text": "PANEL MANAGEMENT  (API + AUTO CAPTCHA)", "callback_data": "cpa_menu", "style": "primary"}],
        [{"text": "STOCK / DELETE", "callback_data": "spa_stock", "style": "primary"},
         {"text": "TEST API", "callback_data": "spa_test", "style": "primary"}],
        [{"text": f"AUTO OTP: {'ON' if smspro.get('auto') else 'OFF'}", "callback_data": "spa_auto", "style": "success" if smspro.get("auto") else "danger"},
         {"text": f"AUTH: {smspro.get('auth', 'auto').upper()}", "callback_data": "spa_auth", "style": "primary"}],
        [{"text": "ADD API KEY", "callback_data": "spa_key", "style": "success"},
         {"text": "API URL", "callback_data": "spa_url", "style": "primary"}],
        [{"text": f"AUTO RANGE: {'ON' if smspro.get('auto_range', True) else 'OFF'}", "callback_data": "spa_ar", "style": "success" if smspro.get("auto_range", True) else "danger"},
         {"text": "DID URL (NUMBERS)", "callback_data": "spa_did", "style": "primary"}],
        [{"text": "PANEL NUMBERS: " + ("OTP ONLY" if bot_settings.get("panel_otp_only", True) else "AUTO FETCH"), "callback_data": "spa_oo",
          "style": "success" if bot_settings.get("panel_otp_only", True) else "danger"}],
    ]}
    for i, k in enumerate(smspro["keys"]):
        masked = f"{k[:4]}...{k[-4:]}" if len(k) > 8 else k
        kb["inline_keyboard"].append([{"text": f"Delete key: {masked}", "callback_data": f"spa_delkey_{i}", "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "back_to_admin", "icon_custom_emoji_id": "5267490665117275176", "style": "danger"}])
    return kb

def smspro_back_kb():
    return {"inline_keyboard": [[{"text": "BACK", "callback_data": "spa_menu", "style": "danger"}]]}

def smspro_stock_keyboard():
    kb = {"inline_keyboard": []}
    with smspro_lock:
        for i, (svc, rngs) in enumerate(smspro["stock"].items()):
            tot = sum(len(l) for l in rngs.values())
            kb["inline_keyboard"].append([{"text": f"DELETE {svc} ({tot})", "callback_data": f"spa_delsvc_{i}", "style": "danger"}])
    kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "spa_menu", "style": "primary"}])
    return kb

def smspro_stock_text():
    lines = ["🗂 <b>SMS PRO STOCK</b>", "━━━━━━━━━━━━━━━━━"]
    with smspro_lock:
        if not smspro["stock"]: lines.append("<i>Empty.</i>")
        for svc, rngs in smspro["stock"].items():
            lines.append(f"📦 <b>{html.escape(svc)}</b>")
            for r, l in sorted(rngs.items()):
                lines.append(f"   <code>{r}XXX</code> ➜ {len(l)}  ({html.escape(get_country_info(r)[1])})")
    return "\n".join(lines)[:3900]

def smspro_edit(chat_id, msg_id, text, kb):
    if msg_id: edit_message(chat_id, msg_id, text, reply_markup=kb)
    else: send_message(chat_id, text, reply_markup=kb)

def smspro_finish_add(chat_id, msg_id, service, numbers, ctx=None):
    key, added, dup, per_range = smspro_add_numbers(service, numbers)
    lines = [f"✅ <b>{html.escape(key)}</b>: +{added} numbers added" + (f" · {dup} duplicate skipped" if dup else "")]
    for r, c in list(sorted(per_range.items()))[:12]:
        lines.append(f"   <code>{r}XXX</code> ({html.escape(get_country_info(r)[1])}) ➜ {c}")
    if len(per_range) > 12: lines.append(f"   … +{len(per_range) - 12} more ranges")
    _k, _p = _ctx_panel({"ctx": ctx}); _t, _kb = _panel_page(_k, _p)
    smspro_edit(chat_id, msg_id, _t + "\n\n" + "\n".join(lines), _kb)

def handle_smspro_state(state, state_data, message, chat_id, message_id, text):
    """Owner typed / uploaded something while an SMS Pro prompt was open."""
    if state.startswith("waiting_cp_"):
        return handle_cp_state(state, state_data, message, chat_id, message_id, text)
    if state.startswith("waiting_xp_"):
        return handle_xp_state(state, state_data, message, chat_id, message_id, text)
    if state == "waiting_sp_newsvc":
        uid = message["from"]["id"]; target = state_data.get("msg_id")
        delete_message(chat_id, message_id)
        _k, _p = _ctx_panel(state_data)
        name = panel_svc(_p, text) if _p else smspro_clean_service(text)
        if not name:
            smspro_edit(chat_id, target, "❌ <b>Invalid service name.</b> Send a name like <code>Facebook</code>:", _panel_back_kb(_k, _p)); return
        user_states[uid] = {"state": "waiting_sp_file", "msg_id": target, "service": name, "ctx": state_data.get("ctx", "")}
        smspro_edit(chat_id, target, f"📄 <b>Service: {html.escape(name)}</b>\nNow send the numbers .txt file (or paste numbers).\n"
                    "<i>Its countries will appear inside the Get Number menu of this service automatically.</i>", _panel_back_kb(_k, _p))
        return
    target = state_data.get("msg_id")
    uid = message["from"]["id"]
    if state == "waiting_sp_file":
        raw = text
        if "document" in message:
            try:
                fid = message["document"]["file_id"]
                info = requests.get(BASE_URL + f"getFile?file_id={fid}", timeout=20).json()
                url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}"
                raw = requests.get(url, timeout=60).content.decode("utf-8-sig", "ignore")
            except Exception as e:
                smspro_edit(chat_id, target, f"❌ <b>Could not read the file.</b>\n<code>{html.escape(str(e)[:150])}</code>", smspro_back_kb()); return
        nums = smspro_parse_numbers(raw)
        delete_message(chat_id, message_id)
        if not nums:
            smspro_edit(chat_id, target, "❌ <b>No numbers found.</b>\nSend a .txt file with one number per line.", smspro_back_kb()); return
        if state_data.get("service"):      # service already chosen (ADD SERVICE / panel upload): add directly
            user_states.pop(uid, None)
            smspro_finish_add(chat_id, target, state_data["service"], nums, state_data.get("ctx")); return
        ranges = {}
        for n in nums: ranges[smspro_range_of(n)] = ranges.get(smspro_range_of(n), 0) + 1
        user_states[uid] = {"state": "waiting_sp_service", "msg_id": target, "numbers": nums, "ctx": state_data.get("ctx", "")}
        kb = {"inline_keyboard": []}
        names = list(smspro["stock"].keys()); row = []
        for i, nm in enumerate(names):
            row.append({"text": nm, "callback_data": f"spa_pick_{i}", "style": "primary"})
            if len(row) == 2: kb["inline_keyboard"].append(row); row = []
        if row: kb["inline_keyboard"].append(row)
        kb["inline_keyboard"].append([{"text": "BACK", "callback_data": "spa_menu", "style": "danger"}])
        rl = "\n".join(f"   <code>{r}XXX</code> ({html.escape(get_country_info(r)[1])}) ➜ {c}" for r, c in list(sorted(ranges.items()))[:10])
        smspro_edit(chat_id, target,
                    f"📄 <b>{len(nums)} numbers found</b> · {len(ranges)} range" + ("s" if len(ranges) != 1 else "") + f"\n{rl}\n━━━━━━━━━━━━━━━━━\n"
                    "🧩 <b>Which service should these numbers be added to?</b>\n"
                    "Tap an existing service, or send a new service name (e.g. <code>Facebook</code>).", kb)
    elif state == "waiting_sp_service":
        delete_message(chat_id, message_id)
        _k, _p = _ctx_panel(state_data)
        name = panel_svc(_p, text) if _p else smspro_clean_service(text)
        if not name:
            smspro_edit(chat_id, target, "❌ <b>Invalid service name.</b> Send a name like <code>Facebook</code>:", _panel_back_kb(_k, _p)); return
        nums = state_data.get("numbers", [])
        user_states.pop(uid, None)
        smspro_finish_add(chat_id, target, name, nums, state_data.get("ctx"))
    elif state == "waiting_sp_key":
        delete_message(chat_id, message_id)
        k = text.strip()
        if k and k not in smspro["keys"]:
            smspro["keys"].append(k); smspro["baseline"] = False; smspro_save()
        user_states.pop(uid, None)
        smspro_edit(chat_id, target, smspro_panel_text(), smspro_panel_keyboard())
    elif state == "waiting_sp_did":
        delete_message(chat_id, message_id)
        u = text.strip()
        if u == "-" or u.startswith("http"):
            smspro["did_url"] = "" if u == "-" else u
            mp = smspro_main_panel()
            if u == "-": mp["nums_path"] = None
            smspro_save()
        user_states.pop(uid, None)
        smspro_edit(chat_id, target, smspro_panel_text() if (u == "-" or u.startswith("http")) else "❌ <b>URL must start with http/https.</b>",
                    smspro_panel_keyboard() if (u == "-" or u.startswith("http")) else smspro_back_kb())
    elif state == "waiting_sp_url":
        delete_message(chat_id, message_id)
        u = text.strip()
        if u.startswith("http"):
            smspro["url"] = u; smspro["baseline"] = False; smspro_save()
        user_states.pop(uid, None)
        smspro_edit(chat_id, target, smspro_panel_text() if u.startswith("http") else "❌ <b>URL must start with http/https.</b>",
                    smspro_panel_keyboard() if u.startswith("http") else smspro_back_kb())

def handle_smspro_callback(data, query_id, user_id, chat_id, message_id):
    if user_id != ADMIN_ID:
        answer_callback_query(query_id, "Owner only", show_alert=True); return
    if data.startswith("cpa_"):
        return handle_cp_callback(data, query_id, user_id, chat_id, message_id)
    if data.startswith("xpa_"):
        return handle_xp_callback(data, query_id, user_id, chat_id, message_id)
    if data in ("manage_smspro", "spa_menu"):
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, smspro_panel_text(), reply_markup=smspro_panel_keyboard()); answer_callback_query(query_id)
    elif data == "spa_newsvc":
        user_states[user_id] = {"state": "waiting_sp_newsvc", "msg_id": message_id}
        edit_message(chat_id, message_id, "🧩 <b>ADD SERVICE</b>\nSend the service name (e.g. <code>Facebook</code>):", reply_markup=smspro_back_kb()); answer_callback_query(query_id)
    elif data == "spa_add":
        user_states[user_id] = {"state": "waiting_sp_file", "msg_id": message_id}
        edit_message(chat_id, message_id, "📄 <b>Send the numbers .txt file</b>\n\nOne number per line, e.g.\n<code>8801712345678\n+8801812345678</code>\n\n<i>Different ranges are fine in one file. You can also paste the numbers as text.</i>", reply_markup=smspro_back_kb())
        answer_callback_query(query_id)
    elif data.startswith("spa_pick_"):
        st = user_states.get(user_id) or {}
        names = list(smspro["stock"].keys())
        idx = int(data[9:])
        if st.get("state") != "waiting_sp_service" or idx >= len(names):
            answer_callback_query(query_id, "Upload the file again", show_alert=True); return
        user_states.pop(user_id, None)
        smspro_finish_add(chat_id, message_id, names[idx], st.get("numbers", []), st.get("ctx")); answer_callback_query(query_id, "Added")
    elif data == "spa_stock":
        edit_message(chat_id, message_id, smspro_stock_text(), reply_markup=smspro_stock_keyboard()); answer_callback_query(query_id)
    elif data.startswith("spa_delsvc_"):
        names = list(smspro["stock"].keys()); idx = int(data[11:])
        if idx < len(names):
            with smspro_lock: smspro["stock"].pop(names[idx], None)
            smspro_save()
        edit_message(chat_id, message_id, smspro_stock_text(), reply_markup=smspro_stock_keyboard()); answer_callback_query(query_id, "Service deleted")
    elif data == "spa_auto":
        smspro["auto"] = not smspro.get("auto", True); smspro_save()
        edit_message(chat_id, message_id, smspro_panel_text(), reply_markup=smspro_panel_keyboard())
        answer_callback_query(query_id, "SMS Pro OTP: " + ("ON" if smspro["auto"] else "OFF"))
    elif data == "spa_auth":
        cur = smspro.get("auth", "auto")
        smspro["auth"] = SMSPRO_AUTH_MODES[(SMSPRO_AUTH_MODES.index(cur) + 1) % len(SMSPRO_AUTH_MODES)] if cur in SMSPRO_AUTH_MODES else "auto"
        smspro_save()
        edit_message(chat_id, message_id, smspro_panel_text(), reply_markup=smspro_panel_keyboard()); answer_callback_query(query_id, "Auth: " + smspro["auth"])
    elif data == "spa_test":
        answer_callback_query(query_id, "Testing...")
        edit_message(chat_id, message_id, smspro_test_text(), reply_markup=smspro_back_kb())
    elif data == "spa_key":
        user_states[user_id] = {"state": "waiting_sp_key", "msg_id": message_id}
        edit_message(chat_id, message_id, "🔑 <b>Send the SMS Pro API key</b>", reply_markup=smspro_back_kb()); answer_callback_query(query_id)
    elif data == "spa_url":
        user_states[user_id] = {"state": "waiting_sp_url", "msg_id": message_id}
        edit_message(chat_id, message_id, f"🌐 <b>Send the SMS Pro logs API URL</b>\nNow: <code>{html.escape(str(smspro.get('url', '')))}</code>", reply_markup=smspro_back_kb()); answer_callback_query(query_id)
    elif data == "spa_oo":
        bot_settings["panel_otp_only"] = not bot_settings.get("panel_otp_only", True); save_local_data()
        edit_message(chat_id, message_id, smspro_panel_text(), reply_markup=smspro_panel_keyboard())
        answer_callback_query(query_id, "Panels: " + ("OTP only (numbers from your upload)" if bot_settings["panel_otp_only"] else "auto fetch numbers"))
    elif data == "spa_ar":
        smspro["auto_range"] = not smspro.get("auto_range", True); smspro_save()
        edit_message(chat_id, message_id, smspro_panel_text(), reply_markup=smspro_panel_keyboard()); answer_callback_query(query_id, "Auto range: " + ("ON" if smspro["auto_range"] else "OFF"))
    elif data == "spa_did":
        user_states[user_id] = {"state": "waiting_sp_did", "msg_id": message_id}
        edit_message(chat_id, message_id, "📱 <b>Send the DID / numbers request URL</b>\nExample:\n<code>https://smspro.tel/api/v1/dids?range_name={range_name}&limit={limit}</code>\nNow: <code>" + html.escape(str(smspro.get("did_url") or "auto-detect")) + "</code>\n<i>Send <code>-</code> for auto-detect.</i>", reply_markup=smspro_back_kb()); answer_callback_query(query_id)
    elif data.startswith("spa_delkey_"):
        idx = int(data[11:])
        if idx < len(smspro["keys"]): smspro["keys"].pop(idx); smspro_save()
        edit_message(chat_id, message_id, smspro_panel_text(), reply_markup=smspro_panel_keyboard()); answer_callback_query(query_id, "Key deleted")
    else:
        answer_callback_query(query_id)


# ==========================================
# MULTI PANEL SYSTEM  (owner adds ANY number panel: name + base URL + API key)
#   * the bot auto-detects the OTP-logs endpoint and the numbers endpoint of the panel
#   * numbers are imported automatically (and re-synced on a timer) into the same stock as SMS Pro,
#     so their countries show inside the matching Stex SMS service (V2 button when Stex has it too)
#   * OTPs are read from the panel logs and forwarded exactly like SMS Pro OTPs
#   * numbers can also be uploaded as a .txt file for any panel / service
# ==========================================
XP_LOG_PATHS = ["", "/sms-logs", "/api/v1/sms-logs", "/api/sms-logs", "/api/v1/logs", "/api/logs", "/logs",
                "/api/v1/sms", "/api/sms", "/sms", "/api/v1/messages", "/api/messages", "/messages",
                "/api/v1/otps", "/api/otps", "/otps", "/api/getsms", "/getsms"]
XP_NUM_PATHS = ["/api/v1/dids", "/api/v1/did", "/api/v1/ranges", "/api/v1/numbers/available", "/numbers", "/api/v1/numbers", "/api/numbers", "/my-numbers", "/api/my-numbers", "/api/v1/my-numbers",
                "/api/v1/dids", "/api/dids", "/dids", "/api/v1/phones", "/api/phones", "/phones",
                "/getnumbers", "/api/getnumbers", "/api/v1/stock", "/api/stock", "/stock"]
_XP_SVC_KEYS = ("service", "service_name", "app", "application", "platform", "sid", "name_service")
_XP_NUM_KEYS = _SP_NUM_KEYS + ("number_value", "phonenumber")
_xp_known = {}        # {panel_id: set(numbers already imported)}  (so used numbers are never re-added)
_xp_busy = set()
_xp_prev_totals = {}
_xp_low_alerted = set()

def _xp_get(pid):
    return next((p for p in smspro.get("panels", []) if p.get("id") == pid), None)

def smspro_main_panel():
    """The main SMS Pro key behaves like a panel, so it shares the auto-range code."""
    mp = smspro.get("main_panel")
    if not isinstance(mp, dict) or not mp: mp = smspro["main_panel"] = {"id": "main", "name": "SMS Pro", "ranges": {}, "known": [], "logs_path": "", "nums_path": None}
    u = urllib.parse.urlparse(smspro.get("url") or SMSPRO_DEFAULT_URL)
    mp["base"] = f"{u.scheme}://{u.netloc}"; mp["key"] = (smspro.get("keys") or [""])[0]; mp["auth"] = smspro.get("auth", "auto")
    mp["did_url"] = smspro.get("did_url", ""); mp["range_target"] = smspro.get("range_target", 500); mp["auto_range"] = smspro.get("auto_range", True)
    if mp["did_url"]: mp["nums_path"] = mp["did_url"]
    mp.setdefault("ranges", {})
    return mp

def _xp_pre_save():
    mp = smspro.get("main_panel")
    if isinstance(mp, dict) and mp.get("id") in _xp_known: mp["known"] = list(_xp_known[mp["id"]])[-30000:]
    for p in smspro.get("panels", []):
        if p["id"] in _xp_known: p["known"] = list(_xp_known[p["id"]])[-30000:]

def _xp_known_set(p):
    if p["id"] not in _xp_known: _xp_known[p["id"]] = set(p.get("known", []))
    return _xp_known[p["id"]]

def _xp_mask(k):
    k = str(k or "")
    return f"{k[:4]}...{k[-3:]}" if len(k) > 9 else ("set" if k else "none")

def xp_request(p, path="", params=None, timeout=15):
    key, mode = p.get("key", ""), p.get("auth", "auto")
    headers, q = {"Accept": "application/json"}, dict(p.get("qs") or {})
    q.update(params or {})
    if key:
        if mode in ("auto", "bearer"): headers["Authorization"] = f"Bearer {key}"
        if mode in ("auto", "header"): headers.update({"X-API-KEY": key, "Api-Key": key})
        if mode in ("auto", "query"): q.update({"api_key": key, "key": key, "token": key})
    if path and path.startswith("http"): url = path.replace("{key}", key).replace("{api_key}", key)
    else: url = p["base"].rstrip("/") + (path or "")
    return requests.get(url, headers=headers, params=q, timeout=timeout)

def _xp_items(data, depth=0):
    if depth > 4: return []
    if isinstance(data, list): return data
    if isinstance(data, dict):
        lo = {str(k).lower(): v for k, v in data.items()}
        for k in _SP_LIST_KEYS + ("numbers", "dids", "phones", "stock", "list"):
            v = lo.get(k)
            if isinstance(v, list): return v
            if isinstance(v, dict):
                r = _xp_items(v, depth + 1)
                if r: return r
    return []

def xp_extract_numbers(data):
    """Reply of a numbers endpoint -> [(digits, service_hint)]."""
    out = []
    for it in _xp_items(data):
        if isinstance(it, (str, int)):
            d = re.sub(r"\D", "", str(it))
            if 8 <= len(d) <= 15: out.append((d, ""))
        elif isinstance(it, dict):
            lo = {str(k).lower(): v for k, v in it.items()}
            num = ""
            for k in _XP_NUM_KEYS:
                d = re.sub(r"\D", "", str(lo.get(k, "")))
                if 8 <= len(d) <= 15: num = d; break
            if not num: continue
            svc = ""
            for k in _XP_SVC_KEYS:
                v = lo.get(k)
                if isinstance(v, str) and v.strip(): svc = v.strip(); break
            out.append((num, svc))
    return out

def xp_discover(p, deadline=60):
    """Try common endpoints; remember the OTP-logs path and the numbers path. None = not found."""
    t0 = time.time(); logs_path = nums_path = None
    _pu = urllib.parse.urlparse(p["base"]); _root = f"{_pu.scheme}://{_pu.netloc}"
    _cands = [(path, p["base"].rstrip("/") + path) for path in XP_LOG_PATHS + XP_NUM_PATHS]
    _nm = re.sub(r"[^a-z0-9]", "", str(p.get("name", "")).lower())
    _cr = [f"/crapi/{s}/viewstats" for s in dict.fromkeys([_nm, _nm[:3], _nm[:4]]) if s]
    _cands = _cands[:1] + [(x, _root + x) for x in _cr] + _cands[1:]
    if _pu.path.strip("/"): _cands += [(path, _root + path) for path in XP_LOG_PATHS + XP_NUM_PATHS if path]
    for path, full in _cands:
        if time.time() - t0 > deadline or (logs_path is not None and nums_path is not None): break
        if path in XP_NUM_PATHS and nums_path is not None: continue
        try: r = xp_request(p, full, timeout=8)
        except Exception: continue
        if r.status_code != 200: continue
        try: data = r.json()
        except Exception: continue
        items = _xp_items(data)
        if not items: continue
        if any(smspro_norm(x) for x in items if isinstance(x, dict)):     # entries carry number + message = OTP logs
            if logs_path is None: logs_path = full if path or p["base"] != full else ""
            continue
        if nums_path is None and xp_extract_numbers(data):
            nums_path = full
    if p.get("logs_path") is None or logs_path is not None: p["logs_path"] = logs_path
    if p.get("nums_path") is None or nums_path is not None: p["nums_path"] = nums_path
    if logs_path and "/crapi/" in logs_path:                    # CR API: max 200 records per call
        p.setdefault("qs", {}).setdefault("records", "200"); p["auto_nums"] = False; p["auto_range"] = False
    p["last_disc"] = time.time()
    smspro_save()
    return logs_path, nums_path

def xp_sync_numbers(p):
    """Import NEW numbers of a panel into the stock. Returns (added, note)."""
    path = p.get("nums_path")
    if path is None: return 0, "numbers endpoint not found (set it with NUMBERS PATH, or upload a file)"
    if p.get("auto_range", True) and p.get("ranges") is not None and (p.get("did_url") or "").strip(): return 0, f"auto range mode · {len(p.get('ranges', {}))} active ranges, {int(p.get('range_target', 500))} numbers each"
    known = _xp_known_set(p)
    pairs, seen_local = [], set()
    for page in range(1, 11):
        try: r = xp_request(p, path, params=({"page": page} if page > 1 else None), timeout=20)
        except Exception as e: return 0, f"connection error: {str(e)[:80]}"
        if r.status_code != 200: return 0, f"HTTP {r.status_code}"
        try: got = xp_extract_numbers(r.json())
        except Exception: return 0, "reply is not JSON"
        new = [x for x in got if x[0] not in seen_local]
        if not new: break
        for x in new: seen_local.add(x[0])
        pairs += new
        if len(got) < 20: break
    fresh = [x for x in pairs if x[0] not in known]
    by_svc = {}
    for num, svc in fresh:
        by_svc.setdefault(panel_svc(p, svc) or panel_svc(p, p.get("service")) or "Other", []).append(num)
    added = 0
    for svc, nums in by_svc.items():
        _, a, _, _ = smspro_add_numbers(svc, nums); added += a
    for num, _ in fresh: known.add(num)
    p["last_sync"] = time.time(); p["numbers_added"] = p.get("numbers_added", 0) + added
    p["last_sync_note"] = f"{len(pairs)} numbers on panel · +{added} new"
    smspro_save()
    return added, p["last_sync_note"]

def xp_fail(p, msg):
    p["last_status"] = msg; p["fails"] = p.get("fails", 0) + 1
    if p["fails"] >= 5 and not p.get("alerted") and smspro.get("alerts", True):
        p["alerted"] = True
        try: send_message(ADMIN_ID, f"⚠️ <b>Panel down: {html.escape(p['name'])}</b>\n<code>{html.escape(msg[:150])}</code>\n<i>5 failed checks in a row. OTP/number sync paused until it answers again.</i>" + (f"\n<pre>{html.escape(str(p.get('login_debug', ''))[-1200:])}</pre>" if p.get("login_debug") else ""))
        except Exception: pass

def xp_poll_panel(p):
    if p.get("logs_path") is None:
        if time.time() - p.get("last_disc", 0) > 300: xp_discover(p)
        if p.get("logs_path") is None: p["last_status"] = "OTP logs endpoint not found"; return
    try: r = xp_request(p, p["logs_path"])
    except Exception as e: xp_fail(p, f"connection error: {str(e)[:80]}"); return
    if r.status_code != 200: xp_fail(p, f"HTTP {r.status_code}"); return
    try: data = r.json()
    except Exception: xp_fail(p, "reply is not JSON"); return
    items = [x for x in _xp_items(data) if isinstance(x, dict)]
    entries = []
    for l in items:
        e = smspro_norm(l)
        if e: e["sig"] = f"{p['id']}:{e['sig']}"; entries.append(e)
    p["last_status"] = f"OK · {len(items)} logs"; p["fails"] = 0; p["alerted"] = False
    try: xp_learn_ranges(p, [e for e in entries if e["sig"] not in smspro_seen_set])
    except Exception as ex: print("xp_learn error:", ex)
    if not p.get("baseline"):
        for e in entries: smspro_seen_set.add(e["sig"])
        p["baseline"] = True; smspro_save(); return
    total = 0
    for ent in reversed(entries):
        if ent["sig"] in smspro_seen_set: continue
        smspro_seen_set.add(ent["sig"])
        if not re.search(r"\d{4,8}", ent["text"]): continue
        total += _sp_forward_entry(ent)
        p["otps"] = p.get("otps", 0) + 1
    if total: save_local_data()
    smspro_save()

# ---------- AUTO ACTIVE RANGES (learn ranges from OTP logs -> post in range group -> stock N numbers each) ----------
def xp_learn_ranges(p, ents):
    """Every OTP log shows which range is alive. Remember it (hits, service, prefix)."""
    rs = p.setdefault("ranges", {}); now = time.time()
    for e in ents:
        num = e["numbers"][0]
        name = e.get("range_name") or ""
        prefix = smspro_range_of(num)
        key = name or prefix
        v = rs.setdefault(key, {"name": name or prefix, "prefix": prefix, "hits": 0, "last": 0, "posted": 0, "retry": 0, "nums": [], "service": ""})
        v["hits"] += 1; v["last"] = now; v["prefix"] = prefix
        svc = smspro_clean_service(detect_service(e["text"], e["sender"]))
        if svc and svc != "Other": v["service"] = svc
    if len(rs) > 300:
        for k in sorted(rs, key=lambda k: rs[k]["last"])[:len(rs) - 300]: rs.pop(k, None)

def xp_range_numbers(p, v, limit):
    """Ask the panel (DID Template URL) for up to `limit` numbers of one range."""
    t = (p.get("did_url") or "").strip() or (p.get("nums_path") or "")
    if not t: return []
    q = urllib.parse.quote
    templ = any(x in t for x in ("{range", "{limit", "{count"))
    got, seen = [], set()
    for page in range(1, 7):
        url = t
        for k, val in (("{range_name}", q(v["name"])), ("{range}", q(v["prefix"])), ("{limit}", str(limit)), ("{count}", str(limit)), ("{page}", str(page))):
            url = url.replace(k, val)
        params = {} if templ else {"range_name": v["name"], "range": v["prefix"], "limit": limit, "count": limit, "per_page": limit}
        if page > 1 and "{page}" not in t: params["page"] = page
        r = xp_request(p, url, params=params, timeout=25)
        if r.status_code != 200: break
        new = [x for x in xp_extract_numbers(r.json()) if x[0] not in seen]
        if not new: break
        for x in new: seen.add(x[0]); got.append(x[0])
        if len(got) >= limit or "{page}" not in t and templ: break
    return got[:limit]

def xp_auto_ranges(p):
    rs = p.get("ranges") or {}; now = time.time()
    target = max(1, int(p.get("range_target", 500) or 500))
    active = sorted([v for v in rs.values() if now - v.get("last", 0) <= 24 * 3600], key=lambda v: v.get("hits", 0), reverse=True)[:60]
    posted = []; known = _xp_known_set(p); added_all = 0
    for v in active:
        if range_groups and now - v.get("posted", 0) > 6 * 3600:
            v["posted"] = now; posted.append(v)
        if now < v.get("retry", 0): continue
        svc = panel_svc(p, v.get("service") or p.get("service") or "Other") or "Other"
        with smspro_lock:
            skey = next((k for k in smspro["stock"] if k.lower() == svc.lower()), svc)
            have = set(n for lst in smspro["stock"].get(skey, {}).values() for n in lst)
        v["nums"] = [n for n in v.get("nums", []) if n in have]
        need = target - len(v["nums"])
        if need <= target * 0.2: continue
        try: nums = [n for n in xp_range_numbers(p, v, need) if n not in known][:need]
        except Exception as e: v["retry"] = now + 600; p["last_sync_note"] = f"range fill error: {str(e)[:60]}"; continue
        if not nums: v["retry"] = now + 600; continue
        _, a, _, _ = smspro_add_numbers(svc, nums)
        for n in nums: known.add(n)
        v["nums"] = (v["nums"] + nums)[-target * 2:]; v["retry"] = now + 60; added_all += a
        p["numbers_added"] = p.get("numbers_added", 0) + a
    if added_all: p["last_sync_note"] = f"auto ranges {len(active)} · +{added_all} numbers"
    p["last_sync"] = now
    if posted:
        txt = "📈 <b>Active ranges</b> · " + html.escape(p["name"]) + "\n" + "\n".join(f"<code>{v['prefix']}XXX</code> {html.escape(v['name'][:40])}" for v in posted[:20])
        for g in list(range_groups):
            try: send_message(g, txt)
            except Exception: pass
    smspro_save()

def _xp_run(fn, p):
    try: fn(p)
    except Exception as e: p["last_status"] = f"error: {str(e)[:80]}"
    finally: _xp_busy.discard((fn.__name__, p["id"]))

def _xp_check_stock():
    thr = int(smspro.get("low_stock", 20) or 0)
    with smspro_lock:
        totals = {svc: sum(len(l) for l in rngs.values()) for svc, rngs in smspro["stock"].items()}
    msgs = []
    for svc, n in totals.items():
        if n <= thr and svc not in _xp_low_alerted: _xp_low_alerted.add(svc); msgs.append(f"📉 <b>{html.escape(svc)}</b> low stock ➜ <code>{n}</code> left")
        elif n > thr: _xp_low_alerted.discard(svc)
    for svc in list(_xp_prev_totals):
        if svc not in totals and svc not in _xp_low_alerted: _xp_low_alerted.add(svc); msgs.append(f"🚫 <b>{html.escape(svc)}</b> is out of stock")
    _xp_prev_totals.clear(); _xp_prev_totals.update(totals)
    if msgs and smspro.get("alerts", True):
        try: send_message(ADMIN_ID, "⚠️ <b>STOCK ALERT</b>\n" + "\n".join(msgs) + "\n<i>Add numbers from SMS PRO SETTINGS.</i>")
        except Exception: pass

def panel_numbers_allowed():
    """OTP ONLY mode (default): panels are used only to read OTPs. Numbers come from the .txt upload in the bot."""
    return not bot_settings.get("panel_otp_only", True)

def xp_worker():
    last_stock = 0
    while True:
        try:
            now = time.time()
            for p in list(smspro.get("panels", [])):
                if p.get("auto_otp", True) and ("xp_poll_panel", p["id"]) not in _xp_busy:
                    _xp_busy.add(("xp_poll_panel", p["id"])); threading.Thread(target=_xp_run, args=(xp_poll_panel, p), daemon=True).start()
                if panel_numbers_allowed() and p.get("auto_nums", True) and p.get("nums_path") is not None and now - p.get("last_sync", 0) >= max(1, int(smspro.get("sync_min", 10))) * 60 \
                        and ("xp_sync_numbers", p["id"]) not in _xp_busy:
                    _xp_busy.add(("xp_sync_numbers", p["id"])); threading.Thread(target=_xp_run, args=(xp_sync_numbers, p), daemon=True).start()
            for p in list(smspro.get("panels", [])):
                if panel_numbers_allowed() and p.get("auto_range", True) and p.get("ranges") and now - p.get("last_ar", 0) >= 60 and ("xp_auto_ranges", p["id"]) not in _xp_busy:
                    p["last_ar"] = now; _xp_busy.add(("xp_auto_ranges", p["id"])); threading.Thread(target=_xp_run, args=(xp_auto_ranges, p), daemon=True).start()
            if panel_numbers_allowed() and smspro.get("auto_range", True) and smspro.get("keys"):
                mp = smspro_main_panel()
                if mp.get("ranges") and now - mp.get("last_ar", 0) >= 60 and ("xp_auto_ranges", "main") not in _xp_busy:
                    if mp.get("nums_path") is None and now - mp.get("last_disc", 0) > 300: xp_discover(mp)
                    if mp.get("nums_path") is not None:
                        mp["last_ar"] = now; _xp_busy.add(("xp_auto_ranges", "main")); threading.Thread(target=_xp_run, args=(xp_auto_ranges, mp), daemon=True).start()
            if now - last_stock > 120: last_stock = now; _xp_check_stock()
        except Exception as e:
            print("xp_worker error:", e)
        time.sleep(SMSPRO_POLL_SECONDS)

# ==========================================
# AUTO CAPTCHA PANELS  (video style: login with username + password + math captcha, then read the SMS/OTP JSON)
#   OWNER PANEL > SMS PRO SETTINGS > PANEL MANAGEMENT > ADD AUTO CAPTCHA PANEL > Add New Provider
#   The bot asks (one by one): provider name, login URL, username, password, message (JSON) link,
#   number column name + serial, message column name + serial. Any panel of this type works - no code change needed.
# ==========================================
from html.parser import HTMLParser as _CPHTMLParser

_cp_sessions = {}
_cp_busy = set()
_CP_UA = "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
_CP_LIST_KEYS = ("aadata", "data", "rows", "records", "result", "results", "items", "logs", "messages", "sms")


class _CPFormParser(_CPHTMLParser):
    """Collects every <form> with its <input> fields (no external libs)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []; self._cur = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "method": a.get("method", "post").lower(), "inputs": []}
            self.forms.append(self._cur)
        elif tag in ("input", "button", "select", "textarea") and self._cur is not None:
            self._cur["inputs"].append({"tag": tag, "name": a.get("name", ""), "type": (a.get("type") or ("text" if tag == "input" else "submit")).lower(),
                                        "value": a.get("value", ""), "id": a.get("id", ""), "ph": a.get("placeholder", "")})

    def handle_endtag(self, tag):
        if tag == "form": self._cur = None


def _cp_text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s if s is not None else "")))).strip()


try:
    requests.packages.urllib3.disable_warnings()      # panels on a bare IP often use self-signed https
except Exception:
    pass

_CP_MATH = r"(\d{1,3})\s*(\+|[-–]|\*|×|÷|/|x|plus|minus|times)\s*(\d{1,3})"


def _cp_calc(m):
    a, op, b = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if op in ("+", "plus"): return str(a + b)
    if op in ("-", "–", "minus"): return str(a - b)
    if op in ("*", "x", "×", "times"): return str(a * b)
    if op in ("÷", "/"): return str(a // b) if b else None
    return None


def _cp_question(page_html):
    """('10 + 5', '15') from 'What is 10 + 5 = ?'. Looks at the visible text first, then at the raw html (placeholders, labels, scripts)."""
    raw = html.unescape(str(page_html or ""))
    for txt in (_cp_text(raw), re.sub(r"\s+", " ", raw)):
        m = (re.search(r"(?:what\s+is|solve|calculate|captcha|answer|sum\s+of)\W{0,10}" + _CP_MATH, txt, re.I)
             or re.search(_CP_MATH + r"\s*(?:=|\?)", txt, re.I))
        if m: return m.group(0).strip(), _cp_calc(m)
    return None, None


def _cp_questions(page_html):
    """Every math question found on the page, best match first: [('2 + 8', '10'), ...]"""
    raw = html.unescape(str(page_html or ""))
    out = []
    for txt in (_cp_text(raw), re.sub(r"\s+", " ", raw)):
        for rx in (r"(?:what\s+is|solve|calculate|captcha|answer|sum\s+of)\W{0,10}" + _CP_MATH, _CP_MATH + r"\s*(?:=|\?)"):
            for m in re.finditer(rx, txt, re.I):
                item = (m.group(0).strip(), _cp_calc(m))
                if item[1] is not None and item not in out: out.append(item)
    return out


def _cp_solve_math(page_html):
    return _cp_question(page_html)[1]


_CP_ERR_RE = re.compile(r"(invalid|incorrect|wrong|failed|error|not match|expired|blocked|disabled|suspended)[^<>{}]{0,80}", re.I)


def _cp_page_msg(body):
    raw = str(body)
    for m in re.finditer(r"<(div|span|p|label|small|li|h\d|b|strong)[^>]*(?:class|id)\s*=\s*[\"'][^\"']*(?:alert|error|danger|invalid|warning|msg|message)[^\"']*[\"'][^>]*>(.*?)</\1>", raw, re.S | re.I):
        t = _cp_text(m.group(2))
        if t: return t[:100]
    visible = _cp_text(re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", raw, flags=re.S | re.I))
    m = _CP_ERR_RE.search(visible)
    return (m.group(0).strip()[:100] if m else "login form came back, the page shows no error text")


_cp_last_html = {}     # panel id -> {"login": html, "post": html, "req": text}  (for the owner command /cpdebug)


def _cp_login_once(p, dbg, attempt=0):
    """One full login try with a fresh session. Returns (ok, note, session)."""
    s = requests.Session(); s.verify = False
    s.headers.update({"User-Agent": _CP_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                      "Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1"})
    try:
        r = s.get(p["login_url"], timeout=20)
    except Exception as e:
        return False, f"login page error: {str(e)[:80]}", None
    if "charset" not in r.headers.get("Content-Type", "").lower(): r.encoding = "utf-8"
    dbg.append(f"[try {attempt + 1}] GET {r.status_code} {r.url}")
    if r.status_code >= 400: return False, f"login page HTTP {r.status_code}", None
    fp = _CPFormParser()
    try: fp.feed(r.text)
    except Exception: pass
    form = next((f for f in fp.forms if any(i["type"] == "password" for i in f["inputs"])), None)
    if not form: return False, "login form not found on that page (is it the real login URL?)", None
    _cp_last_html[p["id"]] = {"login": r.text, "post": "", "req": ""}
    dbg.append("    inputs: " + ", ".join(f"{i['name'] or i['id'] or '?'}:{i['type']}" + (f"={(i['value'] or '(empty)')[:10]}" if i["type"] == "hidden" else "") for i in form["inputs"]) +
               f" | action='{form['action']}' method={form['method']} | cookies: {','.join(c.name for c in s.cookies) or '-'}")
    data = {i["name"]: i["value"] for i in form["inputs"] if i["name"] and i["type"] == "hidden"}
    pw = next((i for i in form["inputs"] if i["type"] == "password" and i["name"]), None)
    pw_name = pw["name"] if pw else "password"
    texts = [i for i in form["inputs"] if i["type"] in ("text", "email", "tel", "number", "") and i["name"]]
    key = lambda i: f"{i['name']} {i['id']} {i['ph']}".lower()
    user = next((i for i in texts if re.search(r"user|login|email|mail|phone|mobile|account", key(i))), None)
    cap = next((i for i in texts if re.search(r"capt|answer|ans\b|result|sum|math|verify|code", key(i)) and i is not user), None)
    if not user: user = next((i for i in texts if i is not cap), None)
    if not cap: cap = next((i for i in texts if i is not user), None)
    user_name = user["name"] if user else "username"
    _qs = _cp_questions(r.text)
    question, ans = _qs[attempt % len(_qs)] if _qs else (None, None)
    if cap and ans is None: return False, "captcha is not a simple math question (image captcha is not supported)", None
    cap_name = cap["name"] if cap else ("capt" if ans is not None else "")
    data[user_name] = p["username"]; data[pw_name] = p["password"]
    if cap_name: data[cap_name] = ans
    sub = next((i for i in form["inputs"] if i["type"] == "submit" and i["name"]), None)
    if sub: data[sub["name"]] = sub["value"] or "Login"
    explicit = form["action"].strip() and form["action"].strip() != "#" and not form["action"].lower().startswith("javascript")
    primary = urllib.parse.urljoin(r.url, form["action"]) if explicit else r.url
    cands = [primary] + [u for u in (urllib.parse.urljoin(r.url, "signin"), urllib.parse.urljoin(r.url, "login")) if u != primary]
    action = cands[min(attempt, len(cands) - 1)]
    dbg.append(f"    form: user={user_name} pass={pw_name} captcha={cap_name or '-'} | question='{question}' answer='{ans}' | POST {action}")
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(r.url))
    _cp_last_html[p["id"]]["req"] = ("POST " + action + "\n" + "\n".join(f"{k} = {('<' + str(len(v)) + ' chars>') if k == pw_name else v}" for k, v in data.items()))
    try:
        if form["method"] == "get": r2 = s.get(action, params=data, timeout=25)
        else: r2 = s.post(action, data=data, headers={"Referer": r.url, "Origin": origin}, timeout=25, allow_redirects=True)
    except Exception as e:
        return False, f"login post error: {str(e)[:80]}", None
    if "charset" not in r2.headers.get("Content-Type", "").lower(): r2.encoding = "utf-8"
    body = r2.text
    _cp_last_html[p["id"]]["post"] = body
    dbg.append(f"    redirects: {' > '.join(str(h.status_code) + ' ' + h.headers.get('Location', '') for h in r2.history) or 'none'}")
    pw_here = bool(re.search(r"type\s*=\s*[\"']?password", body, re.I))
    on_login = bool(re.search(r"/(login|signin)/?$", r2.url.split("?")[0], re.I))
    q2, _ = _cp_question(body)
    msg = _cp_page_msg(body)
    dbg.append(f"    POST -> {r2.status_code} {r2.url} | login page again: {pw_here and (on_login or q2 is not None)} | page says: {msg[:80]}")
    if r2.status_code >= 400 or (pw_here and (on_login or q2 is not None)):
        return False, f"login failed ({msg})", None
    s._cp_landing = (r2.url, body)
    return True, "logged in", s


_cp_cool = {}      # panel id -> [failed logins in a row, retry not before (time), last note]


def cp_cooling(p):
    """(seconds left, note) while the bot waits after failed logins; (0, '') when it may try again."""
    c = _cp_cool.get(p["id"])
    if c and c[1] > time.time(): return int(c[1] - time.time()), c[2]
    return 0, ""


def cp_login(p):
    """Login with up to 3 tries (a wrong captcha answer or a stale form just needs another try). Returns (ok, note).
    After a failure the background worker waits 30s, 60s, 2m ... up to 10 min before the next try (no hammering = no ban)."""
    dbg, last = [], "login failed"
    for attempt in range(3):
        ok, note, s = _cp_login_once(p, dbg, attempt)
        if ok:
            _cp_sessions[p["id"]] = s; p["login_debug"] = "\n".join(dbg)[-1800:]
            _cp_cool.pop(p["id"], None)
            return True, "logged in"
        last = note; low = note.lower()
        if any(w in low for w in ("page error", "http 4", "http 5", "form not found", "not a simple math", "post error")): break
        if attempt >= 1 and re.search(r"password|username|user name|credential|no such|blocked|disabled|suspend", low) and "captcha" not in low: break
        time.sleep(1)
    p["login_debug"] = "\n".join(dbg)[-1800:]
    n = (_cp_cool.get(p["id"]) or [0])[0] + 1
    _cp_cool[p["id"]] = [n, time.time() + min(30 * 2 ** (n - 1), 600), last]
    return False, last


def cp_debug_dump(chat_id, arg=""):
    """/cpdebug hadi : one fresh login try, then sends the login page, the server's answer and the request (password hidden) as files."""
    panels = smspro.get("cpanels", [])
    p = cp_find(arg.strip().lower()) if arg.strip() else (panels[0] if panels else None)
    p = p or next((x for x in panels if arg.strip().lower() in x["name"].lower()), None)
    if not p: send_message(chat_id, "❌ Panel not found. Use <code>/cpdebug hadi</code>"); return
    send_message(chat_id, f"⏳ <b>Testing {html.escape(p['name'])}...</b> (one login try, files follow)")
    _cp_sessions.pop(p["id"], None)
    ok, note = cp_login(p)
    d = _cp_last_html.get(p["id"]) or {}
    send_message(chat_id, ("✅ <b>Login OK</b>" if ok else f"❌ <b>Login failed</b>: <code>{html.escape(note[:150])}</code>") +
                 f"\n<pre>{html.escape(str(p.get('login_debug', ''))[-1500:])}</pre>")
    for fname, content in (("1_login_page.html", d.get("login", "")), ("2_server_answer.html", d.get("post", "")), ("3_request.txt", d.get("req", ""))):
        if content:
            try: requests.post(BASE_URL + "sendDocument", data={"chat_id": chat_id}, files={"document": (fname, str(content).encode("utf-8", "ignore"))}, timeout=40)
            except Exception: pass


def _cp_cdr_template(url):
    """DataTables query used by IMS / INTS style panels (data_smscdr.php): yesterday .. tomorrow, newest first."""
    kv = [("fdate1", "{y_from}"), ("fdate2", "{t_to}"), ("frange", ""), ("fclient", ""), ("fnum", ""), ("fcli", ""), ("fgdate", ""), ("fgmonth", ""),
          ("fgrange", ""), ("fgclient", ""), ("fgnumber", ""), ("fgcli", ""), ("fg", "0"), ("sEcho", "1"), ("iColumns", "9"), ("sColumns", ",,,,,,,,"),
          ("iDisplayStart", "0"), ("iDisplayLength", "100")]
    for i in range(9):
        kv += [(f"mDataProp_{i}", str(i)), (f"sSearch_{i}", ""), (f"bRegex_{i}", "false"), (f"bSearchable_{i}", "true"), (f"bSortable_{i}", "true")]
    kv += [("sSearch", ""), ("bRegex", "false"), ("iSortCol_0", "0"), ("sSortDir_0", "desc"), ("iSortingCols", "1"), ("_", "{ts}")]
    return url.split("?")[0] + "?" + "&".join(f"{k}={v if v.startswith('{') else urllib.parse.quote(v, safe='')}" for k, v in kv)


def _cp_try_json(s, url, referer):
    try:
        r = s.get(url, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01", "Referer": referer}, timeout=20)
        data = r.json()
    except Exception:
        return None
    if isinstance(data, list): return data
    if isinstance(data, dict) and any(str(k).lower() in ("aadata", "data", "rows", "records") for k in data): return data
    return None


def cp_discover(p, s):
    """Logged in but no data link yet: find the SMS report JSON link by itself. Returns (found, note)."""
    base = re.sub(r"/(login|signin)/?$", "", p["login_url"].split("?")[0].rstrip("/"), flags=re.I)
    land_url, land_html = getattr(s, "_cp_landing", (base + "/", ""))
    cands = []
    def add(u):
        if u not in cands: cands.append(u)
    links = re.findall(r"href\s*=\s*[\"']([^\"'#]+)[\"']", land_html, re.I)
    pages = [urllib.parse.urljoin(land_url, html.unescape(l)) for l in links
             if re.search(r"sms|cdr|report", l, re.I) and not re.search(r"\.(css|js|png|jpe?g|gif|ico|svg)(\?|$)", l, re.I)][:6]
    for pg in pages:
        try: ph = s.get(pg, timeout=15).text
        except Exception: continue
        for m in re.findall(r"[\"']([^\"']*data_[A-Za-z0-9_]*\.php[^\"']*)[\"']", ph): add(urllib.parse.urljoin(pg, html.unescape(m)))
        for m in re.findall(r"(?:sAjaxSource|ajax)[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", ph): add(urllib.parse.urljoin(pg, html.unescape(m)))
    for path in ("/agent/res/data_smscdr.php", "/client/res/data_smscdr.php", "/res/data_smscdr.php"): add(base + path)
    cands.sort(key=lambda u: 0 if re.search(r"sms|cdr", u, re.I) else 1)
    tried = 0
    for c in cands[:12]:
        tried += 1
        tpl = c if "?" in c else _cp_cdr_template(c)
        auto = "?" not in c
        probe = dict(p, data_url=tpl, auto_cols=auto)
        if _cp_try_json(s, _cp_data_url(probe), base + "/") is not None:
            p["data_url"] = tpl; p["auto_cols"] = auto; smspro_save()
            return True, "found " + c
    return False, (f"login OK, but the SMS data link was not found automatically (tried {tried}). "
                   "Open the panel's SMS report in Chrome, copy the JSON link (DevTools ➜ Network), then tap Data Link.")


def _cp_data_url(p):
    u = p["data_url"].strip(); today = time.strftime("%Y-%m-%d")
    yest = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400)); tom = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    u = (u.replace("{today}", today).replace("{from}", urllib.parse.quote(today + " 00:00:00")).replace("{to}", urllib.parse.quote(today + " 23:59:59"))
          .replace("{y_from}", urllib.parse.quote(yest + " 00:00:00")).replace("{t_to}", urllib.parse.quote(tom + " 23:59:59")).replace("{ts}", str(int(time.time() * 1000))))
    if not p.get("auto_cols"):          # a link pasted by the owner: keep its dates = today
        u = re.sub(r"((?:fdate1|date1|from|start)=)\d{4}-\d{2}-\d{2}", r"\g<1>" + today, u)
        u = re.sub(r"((?:fdate2|date2|to|end)=)\d{4}-\d{2}-\d{2}", r"\g<1>" + today, u)
    return u


def _cp_rows(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        lo = {str(k).lower(): v for k, v in data.items()}
        for k in _CP_LIST_KEYS:
            v = lo.get(k)
            if isinstance(v, list): return v
            if isinstance(v, dict):
                r = _cp_rows(v)
                if r: return r
    return []


def cp_fetch(p):
    """Logged-in GET of the message link. Re-logs in once if the session expired. Returns (rows, note)."""
    for attempt in (0, 1):
        s = _cp_sessions.get(p["id"])
        if s is None:
            _left, _note = cp_cooling(p)
            if _left: return None, _note                    # cooling down after failed logins
            ok, note = cp_login(p)
            if not ok: return None, note
            s = _cp_sessions[p["id"]]
        if not p.get("data_url"):
            found, why = cp_discover(p, s)
            if not found: return None, why
        try:
            r = s.get(_cp_data_url(p), headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*", "Referer": p["login_url"]}, timeout=25)
            data = r.json()
        except Exception as e:
            _cp_sessions.pop(p["id"], None)          # expired / HTML page -> login again once
            if attempt == 1: return None, f"data link did not return JSON ({str(e)[:50]})"
            continue
        return _cp_rows(data), "ok"
    return None, "unknown error"


def _cp_cell(row, idx):
    try: return _cp_text(row[idx - 1])
    except Exception: return ""


def cp_auto_row(p, row):
    """Find the number + SMS text inside a row without any column setup."""
    if isinstance(row, dict):
        e = smspro_norm(row)
        return dict(e, sig=f"cp:{p['id']}:{e['sig']}") if e else None
    if not isinstance(row, (list, tuple)): return None
    cells = [_cp_text(c) for c in row]
    ni = mask = None
    for i, c in enumerate(cells):
        d, mk = _cp_number_cell(c)
        if d and (mk or len(d) >= 8): ni = i; mask = mk; break
    if ni is None: return None
    pool = [c for i, c in enumerate(cells) if i != ni and len(c) >= 4 and not re.fullmatch(r"[\d.\s,:/\-]+", c)]
    coded = [c for c in pool if re.search(r"\d{3,8}", c)]
    msg = max(coded or pool, key=len) if (coded or pool) else ""
    if not msg: return None
    rng = cells[ni - 1] if ni > 0 and 0 < len(cells[ni - 1]) < 60 and not re.fullmatch(r"[\d\s:\-]+", cells[ni - 1]) else ""
    cli = cells[ni + 1] if ni + 1 < len(cells) and 0 < len(cells[ni + 1]) < 30 and not re.fullmatch(r"[\d.\s]+", cells[ni + 1]) else ""
    sig = "cp:%s:%s" % (p["id"], hashlib.md5(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8", "ignore")).hexdigest())
    return {"sig": sig, "text": msg, "numbers": [_cp_number_cell(cells[ni])[0]], "sender": cli, "range_name": rng, "mask": mask}


def cp_row_entry(p, row):
    """One table row (list by serial, or dict by column name) -> OTP entry or None."""
    num = msg = rng = cli = ""; mask = None
    if isinstance(row, dict):
        lo = {str(k).lower(): v for k, v in row.items()}
        for n in p.get("num_names", []):
            d = re.sub(r"\D", "", _cp_text(lo.get(n.lower(), "")))
            if 7 <= len(d) <= 15: num = d; break
        for n in p.get("msg_names", []):
            t = _cp_text(lo.get(n.lower(), ""))
            if len(t) >= 3: msg = t; break
        rng = _cp_text(lo.get("range", "") or lo.get("range_name", "")); cli = _cp_text(lo.get("cli", "") or lo.get("sender", ""))
        if not num or not msg:
            e = smspro_norm(row)
            if e: return dict(e, sig=f"cp:{p['id']}:{e['sig']}")
    elif isinstance(row, (list, tuple)):
        ni = None
        mask = None
        for i in p.get("num_idx", []):
            d, mk = _cp_number_cell(_cp_cell(row, i))
            if d: num = d; ni = i; mask = mk; break
        for i in p.get("msg_idx", []):
            t = _cp_cell(row, i)
            if len(t) >= 3 and not re.fullmatch(r"[\d.\s,]+", t): msg = t; break
        if ni:
            c = _cp_cell(row, ni - 1)
            if c and not re.fullmatch(r"[\d\s:\-]+", c) and len(c) < 60: rng = c
            c = _cp_cell(row, ni + 1)
            if c and len(c) < 30: cli = c
    if not num or not msg:
        return cp_auto_row(p, row)             # columns not set / not matching -> detect them by looking at the row
    sig = "cp:%s:%s" % (p["id"], hashlib.md5(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8", "ignore")).hexdigest())
    return {"sig": sig, "text": msg, "numbers": [num], "sender": cli, "range_name": rng, "mask": mask}


def cp_poll(p):
    rows, note = cp_fetch(p)
    if rows is None: xp_fail(p, note); return
    ents = [e for e in (cp_row_entry(p, r) for r in rows) if e]
    p["last_status"] = f"OK · {len(rows)} rows · {len(ents)} readable"; p["fails"] = 0; p["alerted"] = False
    if not p.get("baseline"):
        for e in ents: smspro_seen_set.add(e["sig"])
        p["baseline"] = True; smspro_save(); return
    total = 0
    for ent in reversed(ents):
        if ent["sig"] in smspro_seen_set: continue
        smspro_seen_set.add(ent["sig"])
        if not re.search(r"\d{4,8}|\d{3}[- ]\d{3}", ent["text"]): continue      # also WhatsApp style 554-321
        total += _sp_forward_entry(ent); p["otps"] = p.get("otps", 0) + 1
    if total: save_local_data()
    smspro_save()


def _cp_run(p):
    try: cp_poll(p)
    except Exception as e: p["last_status"] = f"error: {str(e)[:80]}"
    finally: _cp_busy.discard(p["id"])


def cp_worker():
    while True:
        try:
            for p in list(smspro.get("cpanels", [])):
                if p.get("on", True) and p["id"] not in _cp_busy:
                    _cp_busy.add(p["id"]); threading.Thread(target=_cp_run, args=(p,), daemon=True).start()
        except Exception as e:
            print("cp_worker error:", e)
        time.sleep(max(3, SMSPRO_POLL_SECONDS))


def cp_find(pid):
    return next((p for p in smspro.get("cpanels", []) if p["id"] == pid), None)


# ---------- UI ----------
def cp_menu_text():
    return (f"{_temoji('gear', '⚙️')} <b>Panel Management</b>\n━━━━━━━━━━━━━━━━━\n"
            "Select which type of panel system you want to manage:\n\n"
            f"{_temoji('key', '🔑')} <b>API PANEL</b> ➜ panel gives an API link + key\n"
            f"{_temoji('shield', '🛡')} <b>AUTO CAPTCHA PANEL</b> ➜ panel has a login page (username + password + math captcha)")


def cp_menu_kb():
    return {"inline_keyboard": [
        [{"text": f"ADD API PANEL ({len(smspro.get('panels', []))})", "callback_data": "xpa_list", "style": "primary", "icon_custom_emoji_id": IC["key"]}],
        [{"text": f"ADD AUTO CAPTCHA PANEL ({len(smspro.get('cpanels', []))})", "callback_data": "cpa_list", "style": "success", "icon_custom_emoji_id": IC["shield"]}],
        [{"text": "PANEL SETUP TUTORIAL", "callback_data": "cpa_tut", "style": "primary", "icon_custom_emoji_id": IC["spark"]}],
        [{"text": "Back to System", "callback_data": "spa_menu", "style": "danger", "icon_custom_emoji_id": IC["back"]}]]}


def cp_list_text():
    ps = smspro.get("cpanels", [])
    act = sum(1 for p in ps if p.get("on", True))
    lines = [f"{_temoji('shield', '🛡')} <b>Auto Captcha Panel Management</b>", "━━━━━━━━━━━━━━━━━",
             f"{_temoji('traffic', '📡')} Active Monitors: <code>{act}</code>", "", f"{_temoji('green', '🟢')} <b>Available Providers:</b>"]
    if not ps: lines.append("<i>No provider yet. Tap Add New Provider.</i>")
    for p in ps:
        ok = str(p.get("last_status", "")).startswith("OK")
        lines.append(f"{'🟢' if ok and p.get('on', True) else ('⚪' if not p.get('on', True) else '🟠')} <b>{html.escape(p['name'])}</b> · {html.escape(str(p.get('last_status', 'new'))[:40])}")
    return "\n".join(lines)


def cp_list_kb():
    rows = []
    ps = smspro.get("cpanels", [])
    for i in range(0, len(ps), 2):
        rows.append([{"text": p["name"], "callback_data": f"cpa_open_{p['id']}", "style": "primary" if (i + j) % 2 == 0 else "success", "icon_custom_emoji_id": IC["globe"]} for j, p in enumerate(ps[i:i + 2])])
    rows.append([{"text": "Add New Provider", "callback_data": "cpa_add", "style": "success", "icon_custom_emoji_id": IC["ok"]}])
    if ps: rows.append([{"text": "Delete Provider", "callback_data": "cpa_delmenu", "style": "danger", "icon_custom_emoji_id": IC["ban"]}])
    rows.append([{"text": "Back", "callback_data": "cpa_menu", "style": "primary", "icon_custom_emoji_id": IC["back"]}])
    return {"inline_keyboard": rows}


def cp_cancel_kb():
    return {"inline_keyboard": [[{"text": "Cancel", "callback_data": "cpa_list", "style": "danger", "icon_custom_emoji_id": IC["no"]}]]}


CP_PRESETS = {
    "hadi": {"name": "SMS Hadi", "login_url": "http://smshadi.net/login"},
    "ints": {"name": "INTS", "login_url": "http://151.80.19.204/ints/login"},
}


def _cp_mask(pw):
    pw = str(pw or "")
    return pw if len(pw) <= 2 else pw[0] + "•" * (len(pw) - 2) + pw[-1]


def cp_panel_text(p, test=""):
    ok = str(p.get("last_status", "")).startswith("OK")
    data = "🤖 auto-detect" + (" ✅" if p.get("data_url") else " (on first login)") if (p.get("auto_cols") or not p.get("data_url")) else f"<code>{html.escape(p['data_url'][:60])}</code>"
    return (f"{_temoji('gear', '⚙️')} <b>Configure {html.escape(p['name'])} Panel</b>\n━━━━━━━━━━━━━━━━━\n"
            f"Type: <code>Auto Captcha Panel</code>\nStatus: {'🟢 Monitoring' if p.get('on', True) else '⚪ Turned OFF'}\n"
            f"Login Status: {'✅ Active &amp; Fetching' if ok else '🟠 ' + html.escape(str(p.get('last_status', 'new'))[:50])}\n"
            f"Login URL: <code>{html.escape(p['login_url'])}</code>\nUser: <code>{html.escape(p['username'])}</code>\n"
            f"Pass: <code>{html.escape(_cp_mask(p.get('password', '')))}</code> <i>({len(p.get('password', ''))} chars)</i>\n"
            f"Data link: {data}\n"
            f"🧩 Number version ➜ <code>V{int(p.get('ver', 2) or 2)}</code> <i>(new services get this version)</i>\n"
            f"✅ OTPs forwarded: <code>{p.get('otps', 0)}</code>" + (f"\n\n{test}" if test else ""))


def cp_panel_kb(p):
    i = p["id"]; on = p.get("on", True)
    return {"inline_keyboard": [
        [{"text": "Turn OFF" if on else "Turn ON", "callback_data": f"cpa_tog_{i}", "style": "danger" if on else "success", "icon_custom_emoji_id": IC["no"] if on else IC["green"]}],
        [{"text": "Test Connection", "callback_data": f"cpa_test_{i}", "style": "success", "icon_custom_emoji_id": IC["ok"]}],
        [{"text": "ADD SERVICE", "callback_data": f"cpa_svc_{i}", "style": "success"}, {"text": "UPLOAD NUMBERS (TXT)", "callback_data": f"cpa_file_{i}", "style": "success"}],
        [{"text": f"VERSION: V{int(p.get('ver', 2) or 2)}", "callback_data": f"cpa_ver_{i}", "style": "primary"}],
        [{"text": "Change Login", "callback_data": f"cpa_cred_{i}", "style": "primary", "icon_custom_emoji_id": IC["key"]},
         {"text": "Data Link", "callback_data": f"cpa_link_{i}", "style": "primary", "icon_custom_emoji_id": IC["share"]}],
        [{"text": "Back to Providers", "callback_data": "cpa_list", "style": "danger", "icon_custom_emoji_id": IC["back"]}]]}


def cp_create(d, chat_id, target):
    """Save a captcha panel, log in once and show the result (used by the manual flow and the two ready-made panels)."""
    pid = d.get("id") or str(int(time.time() * 1000))[-9:]
    smspro["cpanels"] = [x for x in smspro.get("cpanels", []) if x["id"] != pid]; _cp_sessions.pop(pid, None)
    p = {"id": pid, "name": d["name"], "login_url": d["login_url"], "username": d["username"], "password": d["password"],
         "data_url": d.get("data_url", ""), "num_names": d.get("num_names", []), "num_idx": d.get("num_idx", []),
         "msg_names": d.get("msg_names", []), "msg_idx": d.get("msg_idx", []), "auto_cols": not d.get("data_url"), "preset": d.get("preset", ""),
         "on": True, "baseline": False, "last_status": "connecting...", "fails": 0, "otps": 0}
    smspro.setdefault("cpanels", []).append(p); smspro_save()
    smspro_edit(chat_id, target, f"⏳ <b>Logging in to {html.escape(p['name'])}...</b>", None)
    res = cp_test_text(p)
    smspro_edit(chat_id, target, f"{_temoji('ok', '✅')} <b>{html.escape(p['name'])} saved.</b>\nThe bot logs in by itself (math captcha solved automatically) and forwards new OTPs.\n\n" + cp_panel_text(p, res), cp_panel_kb(p))
    _cp_busy.add(p["id"]); threading.Thread(target=_cp_run, args=(p,), daemon=True).start()


def cp_tutorial_text():
    return (f"{_temoji('spark', '🌟')} <b>PANEL SETUP TUTORIAL</b>\n━━━━━━━━━━━━━━━━━\n"
            "<b>Auto Captcha Panel</b> (panel with a login page + math captcha):\n"
            "1) Add New Provider ➜ send a name\n2) Send the <b>login URL</b> (e.g. <code>http://panel.com/login</code>)\n"
            "3) Send the panel <b>username</b> and <b>password</b>\n"
            "4) Send <code>auto</code> (the bot finds the data link by itself), or open the panel in Chrome ➜ SMS reports page ➜ copy the <b>JSON data link</b> (DevTools ➜ Network ➜ the request that returns the table). "
            "Dates like <code>fdate1=2026-10-28</code> auto-update to today. Placeholders <code>{today}</code> <code>{from}</code> <code>{to}</code> also work.\n"
            "5) Column names (if data is JSON objects) and column <b>serials</b> (if data is table rows, counted from 1). "
            "Example table: Date=1, Range=2, <b>Number=3</b>, CLI=4, <b>SMS=5</b> ➜ Number serial <code>3</code>, Message serial <code>5</code>. "
            "Several choices can be sent comma separated, e.g. <code>3, 5</code>.\n\n"
            "<b>API Panel</b>: ADD API PANEL ➜ name, base URL, API key (auto detected).\n\n"
            "Numbers for any panel: SMS PRO SETTINGS ➜ ADD NUMBERS (TXT FILE). OTPs of every panel are forwarded automatically to the user who took the number, the OTP group and the live feed.")


def cp_test_text(p):
    _cp_sessions.pop(p["id"], None)
    ok, note = cp_login(p)
    dbg = f"\n<pre>{html.escape(str(p.get('login_debug', ''))[-1500:])}</pre>" if p.get("login_debug") else ""
    if not ok: return f"❌ <b>Login</b>: <code>{html.escape(note)}</code>{dbg}"
    rows, note = cp_fetch(p)
    if rows is None: return f"✅ <b>Login OK</b>\n❌ <b>Data</b>: <code>{html.escape(note)}</code>"
    ents = [e for e in (cp_row_entry(p, r) for r in rows) if e]
    t = f"✅ <b>Login OK</b>\n📨 Rows: <code>{len(rows)}</code> · readable OTP rows: <code>{len(ents)}</code>"
    if ents: t += f"\n   <code>+{ents[0]['numbers'][0]}</code> {html.escape(ents[0]['text'][:70])}"
    elif rows: t += f"\n⚠️ Rows found but number/message could not be read. First row:\n<code>{html.escape(json.dumps(rows[0], ensure_ascii=False)[:220])}</code>"
    else: t += "\n<i>No SMS rows yet (login and link are fine).</i>"
    return t


def _cp_prompt(n, title, ask, hint="", total=9):
    return f"{_temoji('key', '🔑')} <b>{title}</b>  <code>{n}/{total}</code>\n{ask}" + (f"\n<i>{hint}</i>" if hint else "")


def _cp_list_split(text, numbers=False):
    out = []
    for x in re.split(r"[,;\s]+", text.strip()):
        if not x: continue
        if numbers:
            if x.isdigit() and int(x) > 0: out.append(int(x))
        else: out.append(x.lower()[:40])
    return out


def handle_cp_state(state, state_data, message, chat_id, message_id, text):
    uid = message["from"]["id"]; target = state_data.get("msg_id"); text = (text or "").strip()
    delete_message(chat_id, message_id)                      # also removes the typed password from the chat
    d = dict(state_data)
    def nxt(new_state, prompt, **kw):
        d.update(kw); d["state"] = new_state; user_states[uid] = d
        smspro_edit(chat_id, target, prompt, cp_cancel_kb())
    if not text: smspro_edit(chat_id, target, "❌ Empty. Send it again:", cp_cancel_kb()); return
    if state == "waiting_cp_pre_user":                      # ready-made panel: username -> password -> done
        nxt("waiting_cp_pre_pass", _cp_prompt(2, "Password", f"Send the <b>{html.escape(CP_PRESETS[d['preset']]['name'])}</b> <b>password</b>:", "your message is deleted right away", total=2), username=text); return
    if state == "waiting_cp_pre_pass":
        user_states.pop(uid, None); pr = CP_PRESETS[d["preset"]]
        return cp_create({"id": d["preset"], "name": pr["name"], "login_url": pr["login_url"], "username": d["username"], "password": text, "preset": d["preset"]}, chat_id, target)
    if state == "waiting_cp_cuser":                         # change login of an existing panel
        nxt("waiting_cp_cpass", _cp_prompt(2, "Password", "Send the new <b>password</b>:", "your message is deleted right away", total=2), username=text); return
    if state == "waiting_cp_cpass":
        user_states.pop(uid, None); p = cp_find(d.get("pid"))
        if not p: smspro_edit(chat_id, target, "❌ Panel not found.", cp_cancel_kb()); return
        p["username"], p["password"] = d["username"], text; p["baseline"] = p.get("baseline", False); _cp_sessions.pop(p["id"], None); smspro_save()
        smspro_edit(chat_id, target, f"⏳ <b>Logging in to {html.escape(p['name'])}...</b>", None)
        smspro_edit(chat_id, target, cp_panel_text(p, cp_test_text(p)), cp_panel_kb(p)); return
    if state == "waiting_cp_newlink":                       # set / reset the data link
        p = cp_find(d.get("pid")); user_states.pop(uid, None)
        if not p: smspro_edit(chat_id, target, "❌ Panel not found.", cp_cancel_kb()); return
        if text.lower() in ("auto", "-"): p["data_url"] = ""; p["auto_cols"] = True
        elif text.startswith("http"): p["data_url"] = text; p["auto_cols"] = False
        else: smspro_edit(chat_id, target, "❌ Send a link starting with http, or <code>auto</code>:", cp_cancel_kb()); user_states[uid] = d; return
        p["baseline"] = False; _cp_sessions.pop(p["id"], None); smspro_save()
        smspro_edit(chat_id, target, cp_panel_text(p, cp_test_text(p)), cp_panel_kb(p)); return
    if state == "waiting_cp_name":
        name = re.sub(r"[<>&]", "", text)[:24].strip()
        nxt("waiting_cp_login", _cp_prompt(2, "Login URL", f"Send the panel <b>login URL</b> of <b>{html.escape(name)}</b>:", "e.g. http://smshadi.net/login"), name=name)
    elif state == "waiting_cp_login":
        if not text.startswith("http"): smspro_edit(chat_id, target, "❌ <b>URL must start with http/https.</b> Send it again:", cp_cancel_kb()); return
        nxt("waiting_cp_user", _cp_prompt(3, "Username", "Send the panel <b>username</b>:"), login_url=text)
    elif state == "waiting_cp_user":
        nxt("waiting_cp_pass", _cp_prompt(4, "Password", "Send the panel <b>password</b>:", "your message is deleted right away"), username=text)
    elif state == "waiting_cp_pass":
        nxt("waiting_cp_link", _cp_prompt(5, "Message Link", "Send the <b>link</b> where the SMS/OTP data (JSON) comes from, or send <code>auto</code>:", "auto = the bot finds the link and columns by itself"), password=text)
    elif state == "waiting_cp_link":
        if text.lower() in ("auto", "-", "skip"):
            user_states.pop(uid, None); return cp_create(dict(d, data_url=""), chat_id, target)
        if not text.startswith("http"): smspro_edit(chat_id, target, "❌ <b>Link must start with http/https.</b> Send it again:", cp_cancel_kb()); return
        nxt("waiting_cp_numname", _cp_prompt(6, "Number Column Name", "Which <b>name</b> has the Number column in the data?", "e.g. number, phone  (send - if the data is plain table rows)"), data_url=text)
    elif state == "waiting_cp_numname":
        nxt("waiting_cp_numidx", _cp_prompt(7, "Number Column Serial", "What is the <b>serial</b> of the Number column?", "e.g. 3, 5  (counted from 1)"), num_names=[] if text == "-" else _cp_list_split(text))
    elif state == "waiting_cp_numidx":
        idx = _cp_list_split(text, True)
        if not idx: smspro_edit(chat_id, target, "❌ Send numbers like <code>3, 5</code>:", cp_cancel_kb()); return
        nxt("waiting_cp_msgname", _cp_prompt(8, "Message Column Name", "Which <b>name</b> has the Message/OTP column in the data?", "e.g. message, sms  (send - if the data is plain table rows)"), num_idx=idx)
    elif state == "waiting_cp_msgname":
        nxt("waiting_cp_msgidx", _cp_prompt(9, "Message Column Serial", "What is the <b>serial</b> of the Message column?", "e.g. 5, 7  (counted from 1)"), msg_names=[] if text == "-" else _cp_list_split(text))
    elif state == "waiting_cp_msgidx":
        idx = _cp_list_split(text, True)
        if not idx: smspro_edit(chat_id, target, "❌ Send numbers like <code>5, 7</code>:", cp_cancel_kb()); return
        user_states.pop(uid, None)
        cp_create(dict(d, msg_idx=idx), chat_id, target)


def handle_cp_callback(data, query_id, user_id, chat_id, message_id):
    user_states.pop(user_id, None)
    if data == "cpa_menu":
        edit_message(chat_id, message_id, cp_menu_text(), reply_markup=cp_menu_kb()); answer_callback_query(query_id)
    elif data == "cpa_list":
        edit_message(chat_id, message_id, cp_list_text(), reply_markup=cp_list_kb()); answer_callback_query(query_id)
    elif data == "cpa_tut":
        edit_message(chat_id, message_id, cp_tutorial_text(), reply_markup={"inline_keyboard": [[{"text": "Back", "callback_data": "cpa_menu", "style": "danger", "icon_custom_emoji_id": IC["back"]}]]}); answer_callback_query(query_id)
    elif data == "cpa_add":
        user_states[user_id] = {"state": "waiting_cp_name", "msg_id": message_id}
        edit_message(chat_id, message_id, _cp_prompt(1, "Provider Name", "Send the <b>name</b> of the new provider:"), reply_markup=cp_cancel_kb()); answer_callback_query(query_id)
    elif data.startswith("cpa_open_"):
        p = cp_find(data[9:])
        if p: edit_message(chat_id, message_id, cp_panel_text(p), reply_markup=cp_panel_kb(p))
        answer_callback_query(query_id)
    elif data.startswith(("cpa_svc_", "cpa_file_", "cpa_ver_")):
        act, _, pid = data[4:].partition("_"); p = cp_find(pid)
        if not p: answer_callback_query(query_id, "Panel not found", show_alert=True); return
        if act == "ver":
            p["ver"] = 2 + (int(p.get("ver", 2) or 2) - 2 + 1) % 5; smspro_save()
            edit_message(chat_id, message_id, cp_panel_text(p), reply_markup=cp_panel_kb(p)); answer_callback_query(query_id, f"Version: V{p['ver']}")
        elif act == "svc":
            user_states[user_id] = {"state": "waiting_sp_newsvc", "msg_id": message_id, "ctx": f"cp:{pid}"}
            edit_message(chat_id, message_id, f"🧩 <b>ADD SERVICE · {html.escape(p['name'])}</b>\nSend the service name (e.g. <code>Facebook</code>).\n<i>Saved as <b>{html.escape(panel_svc(p, 'Facebook'))}</b> style names.</i>", reply_markup=_panel_back_kb("cp", p)); answer_callback_query(query_id)
        else:
            user_states[user_id] = {"state": "waiting_sp_file", "msg_id": message_id, "ctx": f"cp:{pid}"}
            edit_message(chat_id, message_id, f"📄 <b>Send the numbers .txt file</b> for <b>{html.escape(p['name'])}</b>\n<i>You will be asked which service to add them to.</i>", reply_markup=_panel_back_kb("cp", p)); answer_callback_query(query_id)
    elif data.startswith("cpa_tog_"):
        p = cp_find(data[8:])
        if p: p["on"] = not p.get("on", True); smspro_save(); edit_message(chat_id, message_id, cp_panel_text(p), reply_markup=cp_panel_kb(p))
        answer_callback_query(query_id)
    elif data.startswith("cpa_test_"):
        p = cp_find(data[9:])
        if not p: answer_callback_query(query_id); return
        answer_callback_query(query_id, "Testing...")
        edit_message(chat_id, message_id, cp_panel_text(p, "⏳ <i>Testing login + data link...</i>"), reply_markup=None)
        res = cp_test_text(p)
        edit_message(chat_id, message_id, cp_panel_text(p, res), reply_markup=cp_panel_kb(p))
    elif data.startswith("cpa_pre_"):                       # ready-made panel button on the admin panel
        key = data[8:]
        if key not in CP_PRESETS: answer_callback_query(query_id); return
        p = cp_find(key)
        if p: edit_message(chat_id, message_id, cp_panel_text(p), reply_markup=cp_panel_kb(p))
        else:
            user_states[user_id] = {"state": "waiting_cp_pre_user", "msg_id": message_id, "preset": key}
            edit_message(chat_id, message_id, _cp_prompt(1, "Username", f"Send the <b>{html.escape(CP_PRESETS[key]['name'])}</b> panel <b>username</b>:", f"login page: {CP_PRESETS[key]['login_url']}", total=2), reply_markup=cp_cancel_kb())
        answer_callback_query(query_id)
    elif data.startswith("cpa_cred_"):
        p = cp_find(data[9:])
        if p:
            user_states[user_id] = {"state": "waiting_cp_cuser", "msg_id": message_id, "pid": p["id"]}
            edit_message(chat_id, message_id, _cp_prompt(1, "Username", f"Send the new <b>username</b> for <b>{html.escape(p['name'])}</b>:", total=2), reply_markup=cp_cancel_kb())
        answer_callback_query(query_id)
    elif data.startswith("cpa_link_"):
        p = cp_find(data[9:])
        if p:
            user_states[user_id] = {"state": "waiting_cp_newlink", "msg_id": message_id, "pid": p["id"]}
            edit_message(chat_id, message_id, f"{_temoji('share', '🔗')} <b>Data Link</b>\nSend the JSON link of the SMS report, or <code>auto</code> to let the bot find it:", reply_markup=cp_cancel_kb())
        answer_callback_query(query_id)
    elif data == "cpa_delmenu":
        kb = {"inline_keyboard": [[{"text": f"DELETE {p['name']}", "callback_data": f"cpa_del_{p['id']}", "style": "danger", "icon_custom_emoji_id": IC["ban"]}] for p in smspro.get("cpanels", [])]}
        kb["inline_keyboard"].append([{"text": "Back", "callback_data": "cpa_list", "style": "primary", "icon_custom_emoji_id": IC["back"]}])
        edit_message(chat_id, message_id, "🗑 <b>Which provider do you want to delete?</b>", reply_markup=kb); answer_callback_query(query_id)
    elif data.startswith("cpa_del_"):
        p = cp_find(data[8:])
        kb = {"inline_keyboard": [[{"text": "YES, DELETE", "callback_data": f"cpa_delyes_{data[8:]}", "style": "danger", "icon_custom_emoji_id": IC["ban"]},
                                   {"text": "CANCEL", "callback_data": "cpa_list", "style": "primary", "icon_custom_emoji_id": IC["no"]}]]}
        edit_message(chat_id, message_id, f"⚠️ Delete <b>{html.escape(p['name']) if p else '?'}</b>?", reply_markup=kb); answer_callback_query(query_id)
    elif data.startswith("cpa_delyes_"):
        pid = data[11:]; smspro["cpanels"] = [p for p in smspro.get("cpanels", []) if p["id"] != pid]; _cp_sessions.pop(pid, None); smspro_save()
        edit_message(chat_id, message_id, cp_list_text(), reply_markup=cp_list_kb()); answer_callback_query(query_id, "Deleted")
    else:
        answer_callback_query(query_id)


# ---------- colorful buttons everywhere ----------
# Any button that has no color / animated icon yet gets one here, so no page of the bot is ever white or plain.
_BTN_RULES = [   # (keywords in the button text, style or None, IC key)
    (("BACK", "PREV"), "danger", "back"), (("CLOSE", "CANCEL", "NO,", "SKIP"), "danger", "no"),
    (("DELETE", "REMOVE", "CLEAR", "REJECT", "STOP", "BAN", "OFF"), "danger", "ban"),
    (("ADD", "NEW", "YES", "CONFIRM", "APPROVE", "SAVE", "START", "VERIFY", "JOIN", "ON"), "success", "ok"),
    (("REFRESH", "CHANGE", "RELOAD", "RETRY", "AGAIN", "NEXT"), "primary", "change"),
    (("STOCK", "RANGE", "FOLDER", "FILE", "UPLOAD", "NUMBER"), "primary", "folder"), (("GROUP", "FORWARD"), "primary", "grp"),
    (("CHANNEL", "BROADCAST", "NOTIF", "ALERT"), "primary", "bell"), (("KEY", "API", "TOKEN", "PASSWORD"), "primary", "key"),
    (("LINK", "SHARE", "URL", "COPY"), "primary", "share"), (("TRAFFIC", "LIVE"), "primary", "traffic"), (("STATS", "REPORT", "STATUS"), "primary", "stats"),
    (("WALLET", "BALANCE", "WITHDRAW", "RATE", "MONEY", "PAY"), "success", "money"), (("GMAIL", "MAIL"), "primary", "gmail"),
    (("USER", "PROFILE", "ADMIN"), "primary", "user"), (("SETTING", "CONTROL", "PANEL", "FEATURE", "CONFIG", "MODE"), "primary", "gear"),
    (("TEST", "CHECK"), "success", "rocket"), (("SUPPORT", "CHAT", "HELP"), "danger", "chat"), (("GIFT", "REFER", "REWARD"), "primary", "gift"),
    (("SEARCH", "FIND"), "primary", "search"), (("LOCK", "SECURITY", "FORCE"), "primary", "lock"), (("TIME", "COOLDOWN", "EXPIRY"), "primary", "hour"),
]


def _btn_rule(text):
    up = str(text).upper().strip()
    words = set(re.findall(r"[A-Z]+", up))
    for kws, style, ic in _BTN_RULES:
        for k in kws:
            if (k in words) or (len(k) > 3 and up.startswith(k)) or up.startswith(k + " ") or up == k: return style, ic
    return None, "spark"


# ==========================================
# PREMIUM EMOJI LIBRARY  (ids given by the owner - use them for any new button / message later)
#   pool_emoji("set1", 3)  -> id        dot_emoji("green") -> id        icon_tag(id) -> <tg-emoji> html
#   Set "emoji_pool_fallback": True in bot_settings to give buttons that have no icon a pool icon instead of the same spark.
# ==========================================
def _ids(blob): return list(dict.fromkeys(blob.split()))
PREMIUM_EMOJI_POOL = {
    "set1": _ids("""6273636451271840147 6273627839862411998 6275933872228143219 6275805225072726715 6273833298212950992 6273836553798160975 6275821709157208262 6273840152980755328 6276250600296420994 6273660902520656812 6276206679960851369 6274026000510620496 6275831652006498759 6273943519958667519 6273899174421337911 6273903662662160701 6273648825072620903 6274007313107915274 6273918549018808282 6273835720574505901 6274000217821941951 6275870663194449243 6276184294591304440 6273997297244180325 6276225182679962762 6276215630672696826 6276153138898539096 6274075349684850643 6273532203825631831 6273561254984421826 6273524679042929818 6273989188345926558 6275976465418818017 6274048295685853461 5278229754099540071 5285350148451344065 5456166744479533085 5310076528577491230 5422502846648039176 5281024850096301559 5280815637944344694 5280735970595971856 5267388960291694083 5420539939219579641 5280591887328093175 5420329588901289204 5431618850309292766 5453866707888152017 5280869505424172134 5280623335078634360"""),
    "set2": _ids("""5278611117130653414 5298747646096187189 5422413713191744617 5280894678227492455 5280662183057825163 5303434898525136153 5266965772869053171 5305260190906471236 5296652101487696644 5298858555036672046 5296240686570414655 5296275986906620070 5269711060130079104 5303514492859065897 5303183810442044150 5397894054136520680 5296401966887345356 5296738331546098668 5456292668625687926 5303283556762526394 5303366226293040224 5303416490295304868 5463310606532644686 5427154326294376920 5231008469875706287 5434112216263576692 5281031958267176200 5278653723206231402 5280751174780199841 5456547209862482913 5280705635241959358 5280757711720423435 5456458746421088612 5230964098568571494 5312265175192056107 5231479898371019163 5278334035905486862 5456119615803395808 5454218272436165856 5280926521115025358 5281007837730841762 5278219081105811309 5307884768111631358 5307884136751439109 5303051181851943323 5307986296843541691"""),
    "set3": _ids("""5283112960116399950 5282870814155219459 5282852032263233269 5280883773305528071 5280566207718631192 5269758970490265542 5267346358511086054 5296258364655805333 5298640971993456864 5298588152485651370 5308010997200461648 5305680792758788581 5307734633234833786 5310180286397423397 5307900569296313923 5310023511501184484 5307606144993205843 5310263926590545944 5269674389699305645 5454370584861384827 5453898589430400486 5456421831177175816 5454292261837776725 5456502344634108251 5454065032298009119 5454261883534087465 5454313393076872799 5267128496294997633 6237975191784266396 6237610939902858402 6235655011796261649 6235336389647407554 6237568505625973857 6235748783817234507 6235485016990685581 5472057595193743789 5472266485223152215 5474143948572223102 5472105634402952328 5474181387802141134 5474294289607448720 5472102739594991482 5472057973150867581 5472191366245149325 5472092191155314021 5472372704059351699 5472101141867158867 5474264147526965147 5472379868064802149"""),
    "set4": _ids("""6073556477824472025 6071278787947925866 6071123877067494706 6071096140168696563 5084979757905347540 5084974483685507801 5082478549340783285 5084923566848213749 5085022089103016925 5084613633418199991 5082413149873767213 5116151848855667552 5104960787579929462 5107195471948940313 5100702284556272564 5098094273039959279 5098265504796115765 5100657930429006538 5084813564145828384 5116395218882528029 5116204921766544244 5116093437300442328 5116368680279606270 5116296632203216188 5116245732545790791 5116296400274981694 5893281616486209299 5774138454896022007 5893473283696759404 5866207164854439951 5866382725937632001 5866009583473924494 5866456517770745292 5866400713260666880 5866469651780736116 5866046133645611309 5866098858664137050 5866325903520305486 5865994057167146921 5866493836741579471 5865988791537242108 5866317158966890859 5866189817481531778 5866336335995867469 5866206774012416141 5866324451821358773 5866474230215873258 5866354903139487976 5866208942970901081 5866395366026384460"""),
    "set6": _ids("""5294236848103643477 5291937511591925566 5294077418917616055 5294202819077756005 5294048127240655242 5291994273879709721 5294215205763434181 5294516785482062829 5292186323342350940 5294005972136647964 5292208210495689627 5291978717508164018 5294007002928798927 5294444247779399477 5291975174160145850 5294323533428579078 5294031587321600012 5294108398516720753 5291824687096027834 5294526187165471742 5294134426018536120 5291774466043435275 5294171848068584842 5293984969746566866 5294121983498277263 5294201479047957700 5294026179957772585 5291892229751723900 5292098293692650297 5294308947719640437 5294153164960848949 5294051631933967760 5294225191562400452 5291997306126626950 5292290347450259214 5292203503211535593 5294210571493724819 5291780728105753403 5294231037012888049 5294068833277990704 5294010206974397371 5294351381996521508 5294035229453865597 5292098684534675100 5292063805105263554 5293991322003200135 5291999676948569127 5291963947115631526 5294062721539526918 5294242852467923382"""),
    "set7": _ids("""5294531860817268837 5294127214768468283 5294485513825178032 5294522197140857947 5292083733753517221 5293992082212409502 5294337307388695687 5294410107084365278 5292170045416297012 5291922054004625949 5291951143818123103 5292245976143124155 5291992809295861098 5292055799286224027 5294399820637688352 5292014752283774878 5294049961191690629 5291817660529533837 5294321325815389139 5294349389131697267 5292013274815028523 5294347396266873249 5291948395039054764 5294409819321550432 5294336633078831209 5291892096607739008 5292062692708736193 5292045130587462814 5291901034434682297 5292166459118606932 5294229581018975260 5294354358408859664 5291933173674957761 5294220170745630736 5294325010897327367 5294471971793293647 5294318478252070646 5294069056616289553 5291826830284709120 5294505107465982830 5291799063321139445 5291950280529697493 5291988613112814801 5294227175837290463 5292111852904416801 5294538934628405146 5294193812531333564 5294408281723262763 5292066437920218075 5292091954320922577"""),
    "set8": _ids("""5291981530711746037 5292236016113966127 5294193108156699621 5292040693886247604 5291793810576137439 5291858711826946840 5292048742654957785 5294343084119708700 5294423709245787718 5294023611567332075 5291991568050312348 5294241881805312589 5291858351049696702 5292004203844097218 5292086972158858331 5294532213004588353 5294180730060954484 5294429743674840973 5294127824653797277 5294535073452809778 5291838156113470124 5294158486425325375 5294378161117614233 5294316532631883496 5292108962391414885 5294086708931874940 5294254478944393569 5292021761670404922 5294463274484521342 5294458756178924088 5291917797692042265 5294189019347833274 5294240825243358100 5291809418487290691 5294456308047563965 5294471336138134209 5291761718580502030 5291813666209946812 5291825606219029010 5294289826525238172 5291959935616178405 5291917995260533077 5294525611639852679 5291798075478661634 5292099427564018941 5292190970496963836 5294436555492973610 5292121516580820347 5292166360334357676 5294107724206856227"""),
    "set9": _ids("""5294335323113807278 5294191265615729158 5292147350809106831 5292183188016222701 5294163983983463099 5294434665707368018 5292087023698466689 5294458584380230360 5291891186074672309 5294494314213167952 5294451304410663668 5294538440707166931 5294279359689938006 5294283890880433237 5294058817414255960 5294325281480266304 5294513087515216901 5292102670264328257 5294177148058228060 5294396668131692138 5294312482477724867 5291737091238026321 5291791748991835084 5294013428199869487 5294095745543069603 5294120269806328883 5292146096678658977 5293994384314882755 5294097669688415562 5294283689016973348 5294362935458548705 5294484680601521871 5293993400767367408 5294098958178603764 5294320866253884749 5294244076533600593 5294192317882716626 5294314831824835370 5293993521026453119 5294263837678131580 5294448585696368047 5294217645304864345 5291928449210932974 5294476442854247878 5294235963340379688 5294228039125718124 5294139949346476093 5294058972033076492 5294100109229838880 5294422158762592930"""),
}
DOT_EMOJI = {   # colored circle emojis
    "blue":   _ids("5983593169424487129 5994422667468345454 5994461236274663880 5994385765109338648 5994339800369335877 5994312475787399544 6003394871329558277 6012491255580201216 6028146767856406850 5780694945811666229 5796266362628215828 5776155096660316678 5978715598404719436 6003621843171284367"),
    "brown":  _ids("5983467112134349032"),
    "green":  _ids("5983322513470395165 5994302623132421837 5828112720494335599 5893009495948267487"),
    "orange": _ids("5983306454587674702 5994527743843245305 6037240175694712667 5771550071380253630"),
    "red":    _ids("5996723223750774974"),
    "purple": _ids("6025938214068426027 5837045681239363644 5883996631631664390 5942595842568359981"),
    "black":  _ids("5776385062094246730 5848268233215316408 5942809650335323342"),
    "yellow": _ids("5908878390789674543"),
}
_POOL_ALL = [i for k in sorted(PREMIUM_EMOJI_POOL) for i in PREMIUM_EMOJI_POOL[k]]

def pool_emoji(group="set1", i=0):
    lst = PREMIUM_EMOJI_POOL.get(group) or _POOL_ALL
    return lst[int(i) % len(lst)]

def dot_emoji(color="green", i=0):
    lst = DOT_EMOJI.get(color) or DOT_EMOJI["blue"]
    return lst[int(i) % len(lst)]

def icon_tag(emoji_id, fallback="✨"):
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def _icon_fallback(text, ric):
    """Icon for a button without its own rule: the usual spark, or (emoji_pool_fallback ON) a stable pool icon picked from the text."""
    if ric in IC: return IC[ric]
    if bot_settings.get("emoji_pool_fallback", False):
        h = sum(ord(c) * (n + 1) for n, c in enumerate(str(text)))
        return _POOL_ALL[h % len(_POOL_ALL)]
    return IC["spark"]

def beautify_markup(markup):
    """Fill in a missing color (style) and animated icon on every button of every page. Existing ones are kept."""
    try:
        out = {}
        for key, val in markup.items():
            if key in ("inline_keyboard", "keyboard") and isinstance(val, list):
                rows = []
                for ri, row in enumerate(val):
                    new_row = []
                    for ci, b in enumerate(row):
                        if isinstance(b, dict):
                            nb = dict(b); t = str(nb.get("text", ""))
                            rs, ric = _btn_rule(t)
                            if not nb.get("style"): nb["style"] = rs or ("primary" if (ri + ci) % 2 == 0 else "success")
                            if t and ord(t[0]) < 128 and not nb.get("icon_custom_emoji_id") and "request_users" not in nb: nb["icon_custom_emoji_id"] = _icon_fallback(t, ric)
                            new_row.append(nb)
                        else:
                            new_row.append(b)
                    rows.append(new_row)
                out[key] = rows
            else:
                out[key] = val
        return out
    except Exception:
        return markup


# ---------- panel admin UI ----------
_XP_KEY_PARAMS = ("token", "api_key", "apikey", "api-key", "key", "access_token")
_XP_CR_RE = re.compile(r"/crapi/([^/]+)/", re.I)
XP_QUICK = {   # one-tap panels on the admin panel: only the API key (and URL if unknown) is needed
    "hadi": {"name": "Hadi", "url": "http://147.135.212.197/crapi/had/viewstats"},
    "zyron": {"name": "Zyron", "url": ""},
}

def xp_find_name(name):
    n = str(name or "").strip().lower()
    return next((p for p in smspro.get("panels", []) if str(p.get("name", "")).strip().lower() == n), None)

def xp_parse_connect(text):
    """'URL KEY [Name]' | 'KEY URL' | 'URL?token=KEY' | 'URL' | 'URL -'  ->  dict(base, key, name, qs, nokey) or None."""
    parts = str(text or "").replace(",", " ").split()
    idx = next((i for i, x in enumerate(parts) if x.lower().startswith("http")), None)
    if idx is None: return None
    url, rest = parts[idx], parts[:idx] + parts[idx + 1:]
    u = urllib.parse.urlparse(url)
    qs = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True)); key = ""; nokey = False
    for k in list(qs):
        if k.lower() in _XP_KEY_PARAMS and qs[k]: key = key or qs[k]; qs.pop(k)
    if rest:
        if rest[0] == "-": nokey = True; rest = rest[1:]
        elif not key: key = rest[0]; rest = rest[1:]
    name = re.sub(r"[<>&]", "", " ".join(rest)).strip()[:24]
    base = urllib.parse.urlunparse((u.scheme, u.netloc, u.path.rstrip("/"), "", "", ""))
    return {"base": base, "key": key, "name": name, "qs": qs, "nokey": nokey}

def xp_auto_name(base):
    u = urllib.parse.urlparse(base); host = (u.hostname or "").lower()
    m = _XP_CR_RE.search(u.path); nm = m.group(1) if m else ""
    if not nm and host and not re.match(r"^[\d.]+$", host) and ":" not in host:
        labels = [l for l in host.split(".") if l and l != "www"]
        nm = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")
    nm = re.sub(r"[^A-Za-z0-9 ]", "", nm).title()[:20] or "Panel"
    name, i = nm, 2
    while xp_find_name(name): name = f"{nm} {i}"; i += 1
    return name

def xp_dot(p):
    s = str(p.get("last_status", ""))
    return "🟢" if s.startswith("OK") else ("🔴" if int(p.get("fails", 0) or 0) >= 3 else "🟠")

def panels_health():
    """(online, total) over API panels + captcha panels."""
    try:
        ps = list(smspro.get("panels", [])) + list(smspro.get("cpanels", []))
        return sum(1 for p in ps if str(p.get("last_status", "")).startswith("OK") and p.get("on", True)), len(ps)
    except Exception:
        return 0, 0

def xp_list_text():
    on, tot = panels_health()
    lines = ["<blockquote><b>🧩 PANEL HUB</b></blockquote>", "━━━━━━━━━━━━━━━━━",
             f"📡 <b>Online</b> ➜ <code>{on}/{tot}</code>   🔔 <b>Alerts</b> ➜ <code>{'ON' if smspro.get('alerts', True) else 'OFF'}</code>", ""]
    ps = smspro.get("panels", [])
    if not ps: lines.append("<i>No panel yet.</i>\n⚡ Tap <b>CONNECT PANEL</b> and send the <b>base URL</b> + <b>API key</b>. That is all.")
    for p in ps:
        lines.append(f"{xp_dot(p)} <b>{html.escape(p['name'])}</b>  ·  OTP <code>{p.get('otps', 0)}</code>  ·  <i>{html.escape(str(p.get('last_status', 'new'))[:34])}</i>")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>🔄 numbers sync every {smspro.get('sync_min', 10)} min</i>")
    return "\n".join(lines)

def xp_list_kb():
    kb = {"inline_keyboard": [[{"text": "CONNECT PANEL  (URL + API)", "callback_data": "xpa_add", "style": "success", "icon_custom_emoji_id": IC["key"]}]]}
    quick = []
    for k, q in XP_QUICK.items():
        ex = xp_find_name(q["name"])
        quick.append({"text": q["name"].upper() + (" " + xp_dot(ex) if ex else ""), "callback_data": (f"xpa_open_{ex['id']}" if ex else f"xpa_q_{k}"), "style": "primary" if ex else "success", "icon_custom_emoji_id": IC["globe"]})
    kb["inline_keyboard"].append(quick)
    rest = [p for p in smspro.get("panels", []) if str(p.get("name", "")).lower() not in {q["name"].lower() for q in XP_QUICK.values()}]
    for i in range(0, len(rest), 2):
        kb["inline_keyboard"].append([{"text": f"{xp_dot(p)} {p['name']}", "callback_data": f"xpa_open_{p['id']}", "style": "primary"} for p in rest[i:i + 2]])
    kb["inline_keyboard"].append([{"text": f"ALERTS: {'ON' if smspro.get('alerts', True) else 'OFF'}", "callback_data": "xpa_alerts", "style": "success" if smspro.get("alerts", True) else "danger"},
                                  {"text": "BACK", "callback_data": "spa_menu", "style": "danger", "icon_custom_emoji_id": IC["back"]}])
    return kb

def xp_panel_text(p):
    lp, npth = p.get("logs_path"), p.get("nums_path")
    fmt = lambda x: "not found" if x is None else (x or "(base URL)")
    return (f"<blockquote><b>🧩 {html.escape(p['name'])}</b>  {xp_dot(p)}</blockquote>\n━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>Base</b> ➜ <code>{html.escape(p['base'])}</code>\n🔑 <b>API</b> ➜ <code>{html.escape(_xp_mask(p.get('key')))}</code>   🔐 <code>{p.get('auth', 'auto')}</code>\n"
            f"📨 <b>OTP logs</b> ➜ <code>{html.escape(fmt(lp))}</code>\n📱 <b>Numbers</b> ➜ <code>{html.escape(fmt(npth))}</code>\n"
            f"🧩 <b>Service</b> ➜ <code>{html.escape(p.get('service') or 'from panel / Other')}</code>   🏷 <code>V{int(p.get('ver', 2) or 2)}</code>\n"
            f"📊 <b>Status</b> ➜ <code>{html.escape(str(p.get('last_status', 'new'))[:60])}</code>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>OTPs</b> <code>{p.get('otps', 0)}</code>  ·  ➕ <b>Numbers</b> <code>{p.get('numbers_added', 0)}</code>  ·  📈 <b>Ranges</b> <code>{len(p.get('ranges') or {})}</code>\n"
            f"🔄 <i>{html.escape(str(p.get('last_sync_note', 'no sync yet')))}</i>")

def xp_panel_kb(p):
    i = p["id"]
    on = lambda v: "ON" if v else "OFF"
    return {"inline_keyboard": [
        [{"text": "TEST", "callback_data": f"xpa_test_{i}", "style": "primary", "icon_custom_emoji_id": IC["ok"]}, {"text": "FETCH NUMBERS", "callback_data": f"xpa_sync_{i}", "style": "success", "icon_custom_emoji_id": IC["phone"]}],
        [{"text": "CHANGE API KEY", "callback_data": f"xpa_key_{i}", "style": "primary", "icon_custom_emoji_id": IC["key"]}, {"text": "CHANGE URL", "callback_data": f"xpa_url_{i}", "style": "primary", "icon_custom_emoji_id": IC["globe"]}],
        [{"text": f"AUTO OTP: {on(p.get('auto_otp', True))}", "callback_data": f"xpa_ao_{i}", "style": "success" if p.get("auto_otp", True) else "danger"},
         {"text": f"AUTO NUMBERS: {on(p.get('auto_nums', True))}", "callback_data": f"xpa_an_{i}", "style": "success" if p.get("auto_nums", True) else "danger"}],
        [{"text": f"AUTO RANGE ({int(p.get('range_target', 500))}/range): {on(p.get('auto_range', True))}", "callback_data": f"xpa_ar_{i}", "style": "success" if p.get("auto_range", True) else "danger"}],
        [{"text": "UPLOAD NUMBERS FILE", "callback_data": f"xpa_file_{i}", "style": "success"}, {"text": "ADD SERVICE", "callback_data": f"xpa_nsvc_{i}", "style": "success"}],
        [{"text": "DEFAULT SERVICE", "callback_data": f"xpa_svc_{i}", "style": "primary"}, {"text": f"VERSION: V{int(p.get('ver', 2) or 2)}", "callback_data": f"xpa_ver_{i}", "style": "primary"}],
        [{"text": "RENAME", "callback_data": f"xpa_ren_{i}", "style": "primary"}, {"text": "RE-DETECT", "callback_data": f"xpa_disc_{i}", "style": "primary"}, {"text": f"AUTH: {p.get('auth', 'auto').upper()}", "callback_data": f"xpa_auth_{i}", "style": "primary"}],
        [{"text": "NUMBERS PATH", "callback_data": f"xpa_np_{i}", "style": "primary"}, {"text": "LOGS PATH", "callback_data": f"xpa_lp_{i}", "style": "primary"}],
        [{"text": "DELETE PANEL", "callback_data": f"xpa_del_{i}", "style": "danger", "icon_custom_emoji_id": IC["ban"]}, {"text": "BACK", "callback_data": "xpa_list", "style": "danger", "icon_custom_emoji_id": IC["back"]}],
    ]}

def xp_back_kb(pid=None):
    return {"inline_keyboard": [[{"text": "BACK", "callback_data": (f"xpa_open_{pid}" if pid else "xpa_list"), "style": "danger"}]]}

def xp_test_text(p):
    lines = [f"🧪 <b>{html.escape(p['name'])} TEST</b>"]
    for label, path in (("OTP logs", p.get("logs_path")), ("Numbers", p.get("nums_path"))):
        if path is None: lines.append(f"❌ <b>{label}</b>: endpoint not found"); continue
        try:
            r = xp_request(p, path, timeout=12)
            line = f"<b>{label}</b> ➜ HTTP <code>{r.status_code}</code>"
            data = r.json()
            if label == "OTP logs":
                ents = [x for x in (smspro_norm(l) for l in _xp_items(data) if isinstance(l, dict)) if x]
                line += f" · readable logs <code>{len(ents)}</code>"
                if ents: line += f"\n   <code>+{ents[0]['numbers'][0]}</code> {html.escape(ents[0]['text'][:70])}"
            else:
                nums = xp_extract_numbers(data); line += f" · numbers <code>{len(nums)}</code>"
                if nums: line += f"\n   <code>+{nums[0][0]}</code> {html.escape(nums[0][1] or '(no service in reply)')}"
            lines.append(line)
        except Exception as e:
            lines.append(f"❌ <b>{label}</b>: <code>{html.escape(str(e)[:120])}</code>")
    return "\n".join(lines)

XP_CONNECT_HELP = ("⚡ <b>CONNECT PANEL</b>\n━━━━━━━━━━━━━━━━━\n"
    "Send the <b>base URL</b> and the <b>API key</b> in one message:\n<code>https://panel.com  YOUR_API_KEY</code>\n\n"
    "✅ A full link with the key inside also works (<code>...?token=KEY</code>)\n"
    "✅ Or send the URL first and the key in the next message\n"
    "✅ Add a name at the end if you want: <code>URL KEY Zyron</code>\n\n"
    "<i>OTP logs + numbers endpoints are detected automatically.</i>")

def xp_quick_btn(k):
    """Admin-panel button for a one-tap panel (Hadi / Zyron): green dot when connected."""
    q = XP_QUICK[k]; ex = xp_find_name(q["name"])
    return {"text": q["name"].upper() + (" " + xp_dot(ex) if ex else ""), "callback_data": (f"xpa_open_{ex['id']}" if ex else f"xpa_q_{k}"),
            "style": "success" if ex else "primary", "icon_custom_emoji_id": IC["globe"]}

def xp_connect(chat_id, target, name, base, key, qs=None):
    """Create (or update) an API panel from only a base URL + API key, detect its endpoints, show the result."""
    base = str(base).rstrip("/"); qs = dict(qs or {})
    p = xp_find_name(name)
    if p is None:
        p = {"id": str(int(time.time() * 1000))[-9:], "name": name, "auth": "auto", "service": "", "auto_otp": True, "auto_nums": True, "auto_range": True,
             "range_target": 500, "ranges": {}, "otps": 0, "numbers_added": 0, "known": [], "final_url": "", "did_url": ""}
        smspro["panels"].append(p)
    p.update({"base": base, "key": key, "qs": qs, "logs_path": None, "nums_path": None, "baseline": False, "last_status": "connecting...", "fails": 0, "alerted": False})
    cr = bool(_XP_CR_RE.search(urllib.parse.urlparse(base).path)) or base.lower().endswith("viewstats")     # CR API (Hadi style): the URL itself is the OTP feed
    if cr:
        p.update({"auth": "query", "logs_path": base, "auto_nums": False, "auto_range": False}); p["qs"].setdefault("records", "200")
    smspro_save()
    smspro_edit(chat_id, target, f"⏳ <b>Connecting to {html.escape(p['name'])}...</b>\n<i>Checking the API key and finding the OTP + numbers endpoints (up to a minute).</i>", None)
    if not cr: xp_discover(p)
    lp, npth = p.get("logs_path"), p.get("nums_path"); note = ""; verified = False
    try:
        if lp is not None:
            r = xp_request(p, lp, timeout=12); j = r.json()
            st = str(j.get("status", j.get("result", "success"))).lower() if isinstance(j, dict) else "success"
            if r.status_code == 200 and st not in ("error", "fail", "failed", "false", "0"):
                verified = True
                note += f"\n\n✅ <b>Connected</b> · readable OTP logs: <code>{len([1 for l in _xp_items(j) if isinstance(l, dict) and smspro_norm(l)])}</code>"
            else:
                why = str((j.get("message") or j.get("msg") or j.get("error") or "") if isinstance(j, dict) else "")[:80]
                note += f"\n\n⚠️ Panel answered HTTP <code>{r.status_code}</code> {html.escape(why)}\n<i>Check the API key / URL (CHANGE API KEY, CHANGE URL).</i>"
    except Exception as e:
        note += f"\n\n⚠️ Logs error: <code>{html.escape(str(e)[:80])}</code>"
    if npth is not None:
        try: _, n2 = xp_sync_numbers(p); note += f"\n\n📱 Numbers: {html.escape(n2)}"
        except Exception as e: note += f"\n\n⚠️ Numbers error: <code>{html.escape(str(e)[:80])}</code>"
    if lp is None and npth is None:
        note = "\n\n⚠️ <b>No endpoint detected.</b> Check the URL + key, or set LOGS PATH / NUMBERS PATH by hand."
    p["last_status"] = "OK · connected" if verified else ("API key / URL rejected" if lp is not None else ("added" if npth is not None else "no endpoint found"))
    smspro_save()
    smspro_edit(chat_id, target, xp_panel_text(p) + note, xp_panel_kb(p))
    return p

def handle_xp_state(state, state_data, message, chat_id, message_id, text):
    uid = message["from"]["id"]; target = state_data.get("msg_id"); text = (text or "").strip()
    delete_message(chat_id, message_id)
    if state == "waiting_xp_name":
        name = re.sub(r"[<>&]", "", text)[:24].strip()
        if not name: smspro_edit(chat_id, target, "❌ Send a panel name:", xp_back_kb()); return
        user_states[uid] = {"state": "waiting_xp_url", "msg_id": target, "name": name}
        smspro_edit(chat_id, target, f"🌐 <b>{html.escape(name)}</b>\nSend the panel <b>base URL</b>\n<i>e.g. https://panel.com  or the full API link</i>", xp_back_kb())
    elif state == "waiting_xp_url":
        if not text.startswith("http"): smspro_edit(chat_id, target, "❌ <b>URL must start with http/https.</b> Send it again:", xp_back_kb()); return
        user_states[uid] = {"state": "waiting_xp_key", "msg_id": target, "name": state_data["name"], "base": text.rstrip("/"), "final": "", "did": ""}
        smspro_edit(chat_id, target, "🔑 Send the <b>API key</b> (send <code>-</code> if the panel needs no key):\n<i>OTP logs + numbers links are detected automatically.</i>", xp_back_kb())
    elif state == "waiting_xp_final":
        if text != "-" and not text.startswith("http"): smspro_edit(chat_id, target, "❌ <b>URL must start with http/https.</b> Send it again:", xp_back_kb()); return
        user_states[uid] = {"state": "waiting_xp_did", "msg_id": target, "name": state_data["name"], "base": state_data["base"], "final": "" if text == "-" else text}
        smspro_edit(chat_id, target, "📱 Send the <b>DID Template URL</b> (numbers / DID API link)\n<i>send <code>-</code> to auto-detect</i>", xp_back_kb())
    elif state == "waiting_xp_did":
        if text != "-" and not text.startswith("http"): smspro_edit(chat_id, target, "❌ <b>URL must start with http/https.</b> Send it again:", xp_back_kb()); return
        user_states[uid] = {"state": "waiting_xp_key", "msg_id": target, "name": state_data["name"], "base": state_data["base"], "final": state_data["final"], "did": "" if text == "-" else text}
        smspro_edit(chat_id, target, "🔑 Send the <b>API key</b> (send <code>-</code> if the panel needs no key):", xp_back_kb())
    elif state == "waiting_xp_key":
        user_states.pop(uid, None)
        xp_connect(chat_id, target, state_data["name"], state_data["base"], "" if text == "-" else text, state_data.get("qs"))
    elif state == "waiting_xp_quick":
        pr = xp_parse_connect(text); dflt = state_data.get("default_url", ""); pname = state_data.get("name", "")
        if not pr and dflt and text and len(text.split()) == 1 and text != "-":      # preset (Hadi): only the API key was sent
            pr = xp_parse_connect(dflt); pr["key"] = text
        if not pr:
            smspro_edit(chat_id, target, "❌ <b>I need a link starting with http/https.</b>\n\n" + XP_CONNECT_HELP, xp_back_kb()); return
        name = pname or pr["name"] or xp_auto_name(pr["base"])
        if pr["key"] or pr["nokey"]:
            user_states.pop(uid, None); xp_connect(chat_id, target, name, pr["base"], pr["key"], pr["qs"])
        else:
            user_states[uid] = {"state": "waiting_xp_key", "msg_id": target, "name": name, "base": pr["base"], "qs": pr["qs"]}
            smspro_edit(chat_id, target, f"🔑 <b>{html.escape(name)}</b>\nNow send the <b>API key</b> (send <code>-</code> if the panel needs no key):", xp_back_kb())
    elif state in ("waiting_xp_newkey", "waiting_xp_newurl", "waiting_xp_newname"):
        p = _xp_get(state_data.get("pid")); user_states.pop(uid, None)
        if not p: smspro_edit(chat_id, target, xp_list_text(), xp_list_kb()); return
        if state == "waiting_xp_newname":
            nm = re.sub(r"[<>&]", "", text)[:24].strip()
            if nm: p["name"] = nm; smspro_save()
            smspro_edit(chat_id, target, xp_panel_text(p), xp_panel_kb(p))
        elif state == "waiting_xp_newkey":
            xp_connect(chat_id, target, p["name"], p["base"], "" if text == "-" else text, p.get("qs"))
        else:
            pr = xp_parse_connect(text)
            if not pr:
                user_states[uid] = state_data
                smspro_edit(chat_id, target, "❌ <b>URL must start with http/https.</b> Send it again:", xp_back_kb(p["id"])); return
            xp_connect(chat_id, target, p["name"], pr["base"], pr["key"] or p.get("key", ""), pr["qs"])
    elif state in ("waiting_xp_svc", "waiting_xp_np", "waiting_xp_lp"):
        p = _xp_get(state_data.get("pid"))
        user_states.pop(uid, None)
        if not p: smspro_edit(chat_id, target, xp_list_text(), xp_list_kb()); return
        if state == "waiting_xp_svc": p["service"] = "" if text == "-" else smspro_clean_service(text)
        else:
            val = None if text.lower() == "auto" else ("" if text in ("-", "/") else (text if text.startswith("/") else "/" + text))
            p["nums_path" if state == "waiting_xp_np" else "logs_path"] = val
            if state == "waiting_xp_lp": p["baseline"] = False
        smspro_save()
        smspro_edit(chat_id, target, xp_panel_text(p), xp_panel_kb(p))

def handle_xp_callback(data, query_id, user_id, chat_id, message_id):
    rest = data[4:]
    if rest == "list":
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, xp_list_text(), reply_markup=xp_list_kb()); answer_callback_query(query_id); return
    if rest == "add":
        user_states[user_id] = {"state": "waiting_xp_quick", "msg_id": message_id}
        edit_message(chat_id, message_id, XP_CONNECT_HELP, reply_markup=xp_back_kb()); answer_callback_query(query_id); return
    if rest.startswith("q_"):
        q = XP_QUICK.get(rest[2:])
        if not q: answer_callback_query(query_id); return
        ex = xp_find_name(q["name"])
        if ex: edit_message(chat_id, message_id, xp_panel_text(ex), reply_markup=xp_panel_kb(ex)); answer_callback_query(query_id); return
        user_states[user_id] = {"state": "waiting_xp_quick", "msg_id": message_id, "name": q["name"], "default_url": q["url"]}
        ask = (f"⚡ <b>{html.escape(q['name'])}</b>\nSend only the <b>API key</b>.\n<i>Default URL:</i> <code>{html.escape(q['url'])}</code>\n\n<i>Another URL? Send</i> <code>URL KEY</code>" if q["url"]
               else f"⚡ <b>{html.escape(q['name'])}</b>\nSend the <b>base URL</b> and the <b>API key</b>:\n<code>https://panel.com  YOUR_API_KEY</code>")
        edit_message(chat_id, message_id, ask, reply_markup=xp_back_kb()); answer_callback_query(query_id); return
    if rest == "alerts":
        smspro["alerts"] = not smspro.get("alerts", True); smspro_save()
        edit_message(chat_id, message_id, xp_list_text(), reply_markup=xp_list_kb()); answer_callback_query(query_id); return
    action, _, pid = rest.partition("_")
    p = _xp_get(pid)
    if not p:
        answer_callback_query(query_id, "Panel not found", show_alert=True); return
    show = lambda extra="": edit_message(chat_id, message_id, xp_panel_text(p) + extra, reply_markup=xp_panel_kb(p))
    if action == "open": user_states.pop(user_id, None); show(); answer_callback_query(query_id)
    elif action == "test":
        answer_callback_query(query_id, "Testing...")
        edit_message(chat_id, message_id, xp_test_text(p), reply_markup=xp_back_kb(pid))
    elif action == "sync":
        answer_callback_query(query_id, "Fetching numbers...")
        if p.get("nums_path") is None: xp_discover(p)
        added, note = xp_sync_numbers(p); show(f"\n\n➕ {html.escape(note)}")
    elif action == "an": p["auto_nums"] = not p.get("auto_nums", True); smspro_save(); show(); answer_callback_query(query_id)
    elif action == "ar": p["auto_range"] = not p.get("auto_range", True); smspro_save(); show(); answer_callback_query(query_id)
    elif action == "ao": p["auto_otp"] = not p.get("auto_otp", True); smspro_save(); show(); answer_callback_query(query_id)
    elif action == "auth":
        cur = p.get("auth", "auto"); p["auth"] = SMSPRO_AUTH_MODES[(SMSPRO_AUTH_MODES.index(cur) + 1) % len(SMSPRO_AUTH_MODES)] if cur in SMSPRO_AUTH_MODES else "auto"
        smspro_save(); show(); answer_callback_query(query_id, "Auth: " + p["auth"])
    elif action == "disc":
        answer_callback_query(query_id, "Detecting...")
        p["logs_path"] = p["nums_path"] = None; xp_discover(p); p["baseline"] = False; show()
    elif action == "file":
        user_states[user_id] = {"state": "waiting_sp_file", "msg_id": message_id, "service": panel_svc(p, p.get("service")) or None, "ctx": f"xp:{pid}"}
        txt = "📄 <b>Send the numbers .txt file</b> for this panel\n" + ("" if p.get("service") else "<i>You will be asked which service to add them to.</i>")
        edit_message(chat_id, message_id, txt, reply_markup=xp_back_kb(pid)); answer_callback_query(query_id)
    elif action == "nsvc":
        user_states[user_id] = {"state": "waiting_sp_newsvc", "msg_id": message_id, "ctx": f"xp:{pid}"}
        edit_message(chat_id, message_id, f"🧩 <b>ADD SERVICE · {html.escape(p['name'])}</b>\nSend the service name (e.g. <code>Facebook</code>).", reply_markup=xp_back_kb(pid)); answer_callback_query(query_id)
    elif action == "ver":
        p["ver"] = 2 + (int(p.get("ver", 2) or 2) - 2 + 1) % 5; smspro_save(); show(); answer_callback_query(query_id, f"Version: V{p['ver']}")
    elif action in ("key", "url", "ren"):
        st = {"key": "waiting_xp_newkey", "url": "waiting_xp_newurl", "ren": "waiting_xp_newname"}[action]
        user_states[user_id] = {"state": st, "msg_id": message_id, "pid": pid}
        hint = {"key": f"🔑 Send the new <b>API key</b> for <b>{html.escape(p['name'])}</b> (<code>-</code> = no key):",
                "url": f"🌐 Send the new <b>base URL</b> for <b>{html.escape(p['name'])}</b>\n<i>The key can be inside the link (?token=KEY), or leave it and the old key stays.</i>",
                "ren": "🏷 Send the new <b>name</b>:"}[action]
        edit_message(chat_id, message_id, hint, reply_markup=xp_back_kb(pid)); answer_callback_query(query_id)
    elif action in ("svc", "np", "lp"):
        st = {"svc": "waiting_xp_svc", "np": "waiting_xp_np", "lp": "waiting_xp_lp"}[action]
        user_states[user_id] = {"state": st, "msg_id": message_id, "pid": pid}
        hint = {"svc": "Send the default <b>service name</b> for numbers that come without a service (<code>-</code> to clear).",
                "np": "Send the <b>numbers API path</b>, e.g. <code>/api/numbers</code>\n(<code>auto</code> = detect again, <code>-</code> = base URL itself)",
                "lp": "Send the <b>OTP logs API path</b>, e.g. <code>/api/sms-logs</code>\n(<code>auto</code> = detect again, <code>-</code> = base URL itself)"}[action]
        edit_message(chat_id, message_id, hint, reply_markup=xp_back_kb(pid)); answer_callback_query(query_id)
    elif action == "del":
        kb = {"inline_keyboard": [[{"text": "YES, DELETE", "callback_data": f"xpa_delyes_{pid}", "style": "danger"}, {"text": "CANCEL", "callback_data": f"xpa_open_{pid}", "style": "primary"}]]}
        edit_message(chat_id, message_id, f"🗑 <b>Delete panel {html.escape(p['name'])}?</b>\n<i>Numbers already in stock stay (delete them from STOCK / DELETE).</i>", reply_markup=kb); answer_callback_query(query_id)
    elif action == "delyes":
        smspro["panels"] = [x for x in smspro["panels"] if x["id"] != pid]; _xp_known.pop(pid, None); smspro_save()
        edit_message(chat_id, message_id, xp_list_text(), reply_markup=xp_list_kb()); answer_callback_query(query_id, "Panel deleted")
    else: answer_callback_query(query_id)

# ==========================================
# Background Broadcaster Tool 
# ==========================================
RANGE_RE = re.compile(r"^(?:range|rid)?[:=]?\+?(\d{5,15})[xX]*$", re.I)

def parse_range_input(text):
    """Detects a pasted StexSMS range like 2519474XXX, +251 9474 XXX, Range: 25194 ..."""
    t = re.sub(r"[\s\-\(\)]", "", str(text or ""))
    m = RANGE_RE.match(t)
    return m.group(1) if m else None

def guess_service_for_range(rng):
    best, best_hits = "Other", -1
    for sid, data in voltx_dynamic_data.items():
        for r, rd in data.get("ranges", {}).items():
            if r.startswith(rng) or rng.startswith(r):
                if rd.get("hits", 0) > best_hits: best, best_hits = sid, rd.get("hits", 0)
    return best

def allocate_pasted_range(chat_id, user_id, rng, service=None, edit_id=None):
    now = time.time()
    last_req = user_cooldowns.get(user_id, 0)
    if now - last_req < bot_settings.get("cooldown", 0):
        send_message(chat_id, f"⌛ <b>Please wait {int(bot_settings['cooldown'] - (now - last_req))} seconds before requesting again.</b>")
        return
    user_cooldowns[user_id] = now

    if edit_id:
        mid = edit_id
    else:
        wait = send_message(chat_id, f"⏳ <b>Range detected:</b> <code>{rng}</code>")
        mid = wait["result"]["message_id"] if wait and "result" in wait else None
    loader = Loader(chat_id, mid, "RANGE DETECTED", [f"Range {rng}", "Requesting number", "Reserving your number"]).start() if mid else None

    service = service or guess_service_for_range(rng)
    msg, markup = get_number_allocation_content(service, rng, user_id=user_id, exact_range=True)
    if loader: loader.stop()
    if mid: edit_message(chat_id, mid, msg, reply_markup=markup)
    else: send_message(chat_id, msg, reply_markup=markup)

def broadcast_worker(from_chat_id, message_id, target_msg_id):
    users = get_all_user_ids() 
    success_count = 0
    for uid in users:
        try:
            res = copy_message(uid, from_chat_id, message_id)
            if res and res.get("ok"): success_count += 1
            time.sleep(0.05) 
        except: pass
        
    send_message(from_chat_id, f"✅ <b>Broadcast Completed!</b>\nSuccessfully sent to {success_count} users out of {len(users)}.")
    
    if target_msg_id:
        edit_message(from_chat_id, target_msg_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())

# ==========================================
# Message Handlers
# ==========================================
_MENU_LABELS = ["LIVE OTP APP", "LIVE NUMBER", "LIVE TRAFFIC", "INVITE FRIEND", "WALLET", "LEADERBOARD", "OWNER PANEL",
                "Get Number", "Search Number", "Traffic", "2FA Online", "Gmail", "Refer", "My Profile", "Leaderboard", "Support", "Admin Panel"]
_MENU_ALIAS = {"GET NUMBER": "LIVE NUMBER", "TRAFFIC": "LIVE TRAFFIC", "REFER": "INVITE FRIEND", "MY PROFILE": "WALLET",
               "LEADERBOARD": "LEADERBOARD", "ADMIN PANEL": "OWNER PANEL", "SEARCH NUMBER": "SEARCH NUMBER",
               "2FA ONLINE": "2FA ONLINE", "GMAIL": "GMAIL", "SUPPORT": "SUPPORT"}
_MENU_CMDS = {"LIVE NUMBER", "LIVE TRAFFIC", "INVITE FRIEND", "WALLET", "LEADERBOARD", "OWNER PANEL", "SEARCH NUMBER", "2FA ONLINE", "GMAIL", "SUPPORT"}

def normalize_menu_text(text):
    """Reply-keyboard taps (with or without the fallback emoji in front) -> internal command name."""
    t = (text or "").strip()
    for label in _MENU_LABELS:
        if t.endswith(label):
            prefix = t[:-len(label)].strip()
            if not prefix or all(ord(c) > 127 or c in "\ufe0f\u200d" for c in prefix.replace(" ", "")):
                return _MENU_ALIAS.get(label.upper(), label)
    return text

def handle_message(message):
    global waiting_for_firebase, current_db_mode, db_firebase, user_states, voltx_keys, force_join_channels, otp_forward_groups, otp_button_link, bot_settings
    
    # 🌟 বট শুধুমাত্র ইনবক্সের মেসেজে রিপ্লাই দিবে (গ্রুপ ইগনোর করবে)
    if message.get("chat", {}).get("type") != "private":
        handle_range_group_message(message)
        return
        
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "")
    message_id = message["message_id"]
    first_name = message["from"].get("first_name", "User")
    text = normalize_menu_text(text)
    if user_id == ADMIN_ID and text.startswith("/cpdebug"):
        threading.Thread(target=cp_debug_dump, args=(chat_id, text[8:]), daemon=True).start(); return

    # ---- Maintenance mode + feature switches (admin: OWNER PANEL > FEATURES) ----
    if user_id != ADMIN_ID and bot_settings.get("maintenance"):
        send_message(chat_id, f'{_temoji("gear", "🛠")} <b>Bot is under maintenance.</b>\n<i>Please come back in a little while.</i>')
        return
    _fk = _FEATURE_OF_CMD.get(text)
    if _fk and not feat_on(_fk):
        user_states.pop(user_id, None)
        send_message(chat_id, f'{_temoji("ban", "🚫")} <b>This option is currently turned off by admin.</b>',
                     reply_markup=get_main_keyboard(user_id))
        return

    # ---- 2FA Authenticator: user sent the secret key ----
    _st2fa = user_states.get(user_id)
    if _st2fa and isinstance(_st2fa, dict) and _st2fa.get("state") == "waiting_2fa_secret":
        if text in _MENU_CMDS or str(text).startswith("/"):
            del user_states[user_id]
        elif text:
            delete_message(chat_id, message_id)   # keep the secret key out of the chat
            _cfg = parse_2fa_input(text)
            if not _cfg:
                send_message(chat_id, f'{_temoji("no", "❌")} <b>Invalid secret key.</b>\n'
                                      f'<i>Send the Base32 key (letters A-Z and digits 2-7) or an otpauth:// link.</i>\n'
                                      f'{_temoji("key", "🔑")} <i>Example:</i> <code>JBSWY3DPEHPK3PXP</code>',
                             reply_markup=twofa_prompt_kb())
                return
            try:
                twofa_last[user_id] = _cfg
                _res = twofa_result(user_id)
                del user_states[user_id]
                send_message(chat_id, _res[0], reply_markup=_res[1])
            except Exception:
                send_message(chat_id, f'{_temoji("no", "❌")} <b>Invalid secret key.</b>\n<i>Please check it and send again.</i>',
                             reply_markup=twofa_prompt_kb())
            return

    if text == "🔙 BACK" and user_id == ADMIN_ID:
        send_message(chat_id, "❌ <b>Action Cancelled!</b>", reply_markup=get_main_keyboard(user_id)) 
        if user_id in user_states: del user_states[user_id]
        send_message(chat_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
        return

    # Native Chat Selection Listener
    if "chat_shared" in message:
        shared_chat_id = message["chat_shared"]["chat_id"]
        if user_id == ADMIN_ID and user_id in user_states:
            state_data = user_states[user_id]
            state = state_data.get("state")
            target_msg_id = state_data.get("msg_id")
            
            send_message(chat_id, "✅ <b>Successfully Selected!</b>", reply_markup=get_main_keyboard(user_id)) 
            
            if state == "waiting_otp_group":
                otp_forward_groups.clear() 
                otp_forward_groups.append(str(shared_chat_id))
                
                _lnk = fwd_export_link(shared_chat_id)
                if _lnk: otp_button_link = _lnk
                
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:", reply_markup=get_otp_group_keyboard())
                send_message(chat_id, otp_bots_check_text() + ("" if _lnk else "\n\n⚠️ <i>Invite link not created automatically. Set it with the Link button.</i>"))
                del user_states[user_id]
                return
                
            elif state == "waiting_range_group":
                if str(shared_chat_id) not in [str(g) for g in range_groups]: range_groups.append(str(shared_chat_id))
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, RANGE_GROUP_TEXT, reply_markup=get_range_group_keyboard())
                del user_states[user_id]
                return

            elif state == "waiting_dxa_w_group":
                bot_settings["w_group"] = str(shared_chat_id)
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
                del user_states[user_id]
                return
                
            elif state == "waiting_fj_channel":
                chat_details = get_chat_details(str(shared_chat_id))
                if chat_details:
                    force_join_channels.append(chat_details)
                    save_local_data()
                    if target_msg_id: edit_message(chat_id, target_msg_id, "🔗 <b>FORCE JOIN SYSTEM</b>\nManage channels below:", reply_markup=get_force_join_keyboard())
                else:
                    if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Failed to verify channel! Make sure bot is admin.</b>", reply_markup=get_back_only_keyboard())
                del user_states[user_id]
                return

    if not is_user_joined(user_id):
        fj_msg = (
            "<b>════《 <tg-emoji emoji-id=\"5337267511261960341\">🔥</tg-emoji> ACCESS REQUIRED 》════</b>\n\n"
            "<b><tg-emoji emoji-id=\"5420517437885943844\">🔗</tg-emoji> JOIN ALL CHANNELS BELOW TO USE THIS BOT.</b>\n\n"
            "<b><tg-emoji emoji-id=\"5352694861990501856\">✅</tg-emoji> JOIN ALL CHANNELS, THEN CLICK VERIFY.</b>"
        )
        send_message(chat_id, fj_msg, reply_markup=get_force_join_alert_keyboard())
        return

    if user_id == ADMIN_ID and isinstance(user_states.get(user_id), dict) and user_states[user_id].get("state") == "waiting_mino_key":
        handle_mino_key_message(message); return
    if user_id == ADMIN_ID and isinstance(user_states.get(user_id), dict) and user_states[user_id].get("state") == "waiting_emoji_pick":
        handle_emoji_pick_message(message); return

    # Handle Admin & User Input States
    if user_id in user_states:
        state_data = user_states[user_id]
        if isinstance(state_data, dict):
            state = state_data.get("state")
            target_msg_id = state_data.get("msg_id")
        else:
            state = state_data
            target_msg_id = None
            
        if state == "waiting_vk_key":
            voltx_keys.append(text.strip())
            save_local_data()
            if target_msg_id: edit_message(chat_id, target_msg_id, "🔑 <b>STEX SMS KEY SYSTEM</b>\nManage API Keys below:", reply_markup=get_voltx_keys_keyboard())
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif (state in ("waiting_sp_file", "waiting_sp_service", "waiting_sp_key", "waiting_sp_url", "waiting_sp_did", "waiting_sp_newsvc") or str(state).startswith("waiting_xp_") or str(state).startswith("waiting_cp_")) and user_id == ADMIN_ID:
            handle_smspro_state(state, state_data, message, chat_id, message_id, text)
            return

        elif state == "waiting_gm_stock" and user_id == ADMIN_ID:
            delete_message(chat_id, message_id)
            added, dup, bad = gmail_add_stock(text)
            note = f'\n\n✅ Added <b>{added}</b>' + (f' · ♻️ duplicate {dup}' if dup else '') + (f' · ⚠️ bad format {bad}' if bad else '')
            if added or dup or bad: del user_states[user_id]
            if target_msg_id:
                if added or dup or bad: edit_message(chat_id, target_msg_id, gmail_panel_text() + note, reply_markup=gmail_panel_kb())
                else: edit_message(chat_id, target_msg_id, "❌ <b>No valid line found.</b>\nSend like:\n<code>name@gmail.com:password</code>", reply_markup=get_back_only_keyboard())
            return

        elif state in ("waiting_gm_site", "waiting_gm_app") and user_id == ADMIN_ID:
            delete_message(chat_id, message_id)
            val = text.strip()
            key = "gmail_site" if state == "waiting_gm_site" else "gmail_miniapp"
            if val in ("0", "-", "remove", "off"):
                bot_settings[key] = ""
            elif state == "waiting_gm_site" and val.lower().startswith("https://"):
                bot_settings[key] = val
            elif state == "waiting_gm_app" and re.match(r"^(https?://)?t\.me/\S+$", val, re.I):
                bot_settings[key] = val if val.lower().startswith("http") else "https://" + val
            else:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Invalid link.</b>\n" + ("Site link must start with <code>https://</code>" if key == "gmail_site" else "Mini app link looks like <code>https://t.me/YourBot/app</code>") + "\nSend <code>0</code> to remove.", reply_markup=get_back_only_keyboard())
                return
            save_local_data(); del user_states[user_id]
            if target_msg_id: edit_message(chat_id, target_msg_id, gmail_panel_text() + "\n\n✅ <b>Saved</b>", reply_markup=gmail_panel_kb())
            return

        elif state == "waiting_gm_limit" and user_id == ADMIN_ID:
            delete_message(chat_id, message_id)
            try:
                bot_settings["gmail_limit"] = max(0, int(text.strip()))
                save_local_data(); del user_states[user_id]
                if target_msg_id: edit_message(chat_id, target_msg_id, gmail_panel_text(), reply_markup=gmail_panel_kb())
            except ValueError:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Send a number (0 = unlimited).</b>", reply_markup=get_back_only_keyboard())
            return

        elif state in ("waiting_msvc_name", "waiting_msvc_range") and user_id == ADMIN_ID:
            delete_message(chat_id, message_id)
            ranges = parse_manual_ranges(text)
            if state == "waiting_msvc_range":
                name = state_data.get("name", "")
            else:
                name = re.sub(r"\+?\d[\d\s\-\(\)]*[xX]*", " ", text)
                name = re.sub(r"[,;:|\-\n]+", " ", name).strip()
            if not name:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Service name not found.</b> Send the service name (e.g. <code>Telegram</code>):", reply_markup=get_back_only_keyboard())
                return
            if not ranges:
                user_states[user_id] = {"state": "waiting_msvc_range", "msg_id": target_msg_id, "name": name}
                if target_msg_id: edit_message(chat_id, target_msg_id, f"📦 <b>{html.escape(name)}</b>\n\nNow send the range(s), one per line or comma separated:\n<code>2519474XXX</code>", reply_markup=get_back_only_keyboard())
                return
            key, added = save_manual_service(name, ranges)
            del user_states[user_id]
            if target_msg_id: edit_message(chat_id, target_msg_id, manual_services_text() + f"\n\n✅ <b>{html.escape(key)}</b>: +{added} range saved", reply_markup=manual_services_keyboard())
            return

        elif state == "waiting_stock_range":
            delete_message(chat_id, message_id)
            ranges = extract_ranges(text)
            if not ranges:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>No valid range found.</b>\nSend ranges like <code>2519474XXX</code>, one per line.", reply_markup=stock_back_keyboard())
                return
            user_states[user_id] = {"state": "stock_pick", "msg_id": target_msg_id, "ranges": ranges}
            pick_kb = {"inline_keyboard": [
                [{"text": "1,000 numbers", "callback_data": "stk_go_1000", "style": "success"}, {"text": "2,000 numbers", "callback_data": "stk_go_2000", "style": "success"}],
                [{"text": "BACK", "callback_data": "manage_stock", "icon_custom_emoji_id": "5438541186539232243", "style": "danger"}]]}
            if target_msg_id: edit_message(chat_id, target_msg_id, "📦 <b>How many numbers per range?</b>\n" + "\n".join(f"<code>{r}</code>" for r in ranges), reply_markup=pick_kb)
            return

        elif state == "waiting_otp_link":
            if text.startswith("http"):
                otp_button_link = text.strip()
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:", reply_markup=get_otp_group_keyboard())
            else:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Invalid URL format. Must start with http/https.</b>", reply_markup=get_back_only_keyboard())
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return

        elif state == "waiting_main_channel_link":
            if text.startswith("http"):
                bot_settings["main_channel_link"] = text.strip()
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:", reply_markup=get_otp_group_keyboard())
            else:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Invalid URL format. Must start with http/https.</b>", reply_markup=get_back_only_keyboard())
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return

        elif state == "waiting_broadcast":
            if target_msg_id: edit_message(chat_id, target_msg_id, "⏳ <b>Broadcasting message to all users...</b>")
            threading.Thread(target=broadcast_worker, args=(chat_id, message_id, target_msg_id), daemon=True).start()
            del user_states[user_id]
            return
            
        elif state == "waiting_firebase":
            try:
                # 🌟 ফাইল আপলোড সাপোর্ট অ্যাড করা হলো
                if "document" in message:
                    file_id = message["document"]["file_id"]
                    file_info = requests.get(BASE_URL + f"getFile?file_id={file_id}").json()
                    file_path = file_info["result"]["file_path"]
                    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                    text = requests.get(file_url).text
                
                # 🌟 স্মার্ট কোট (Smart Quotes) বা ভুল টেক্সট ফিক্স
                text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

                firebase_cred = json.loads(text)
                with open("temp_firebase.json", "w") as f: json.dump(firebase_cred, f)
                try:
                    if not firebase_admin._apps: firebase_admin.initialize_app(credentials.Certificate("temp_firebase.json"))
                    db_firebase = firestore.client()
                    current_db_mode = "firebase"
                    waiting_for_firebase = False
                    
                    # 🌟 One-Time Sync: Firebase থেকে সব ইউজার ID লোকালে সেভ করা (লিমিট বাঁচাতে)
                    try:
                        local_users = set()
                        if os.path.exists(USERS_LIST_FILE):
                            with open(USERS_LIST_FILE, "r") as f:
                                local_users = set(json.load(f))
                        
                        # শুধুমাত্র ID গুলো আনা হচ্ছে, পুরো ডাটা নয় (এতে ব্যান্ডউইথ ও লিমিট বাঁচবে)
                        docs = db_firebase.collection('users').select([]).stream()
                        fb_users = set(int(doc.id) for doc in docs)
                        
                        combined_users = local_users.union(fb_users)
                        if len(combined_users) > len(local_users):
                            with open(USERS_LIST_FILE, "w") as f:
                                json.dump(list(combined_users), f)
                            print(f"✅ Synced {len(fb_users)} users from Firebase to Local List!")
                    except Exception as e:
                        print(f"User Sync Error: {e}")

                    # 🌟 Firebase এ সেটিংস চেক এবং সেভ/সিঙ্ক করার লজিক
                    try:
                        config_ref = db_firebase.collection('settings').document('bot_config')
                        doc = config_ref.get()
                        if not doc.exists:
                            config_data = {
                                "bot_settings": bot_settings,
                                "force_join_status": force_join_status,
                                "force_join_channels": force_join_channels,
                                "otp_forward_groups": otp_forward_groups,
                                "otp_button_link": otp_button_link,
                                "voltx_keys": voltx_keys,
                                "voltx_auto_mode": voltx_auto_mode
                            }
                            config_ref.set(config_data)
                        else:
                            data = doc.to_dict()
                            if "bot_settings" in data: bot_settings.update(data["bot_settings"])
                            if "force_join_status" in data: force_join_status = data["force_join_status"]
                            if "force_join_channels" in data: force_join_channels = data["force_join_channels"]
                            if "otp_forward_groups" in data: otp_forward_groups = data["otp_forward_groups"]
                            if "otp_button_link" in data: otp_button_link = data["otp_button_link"]
                            if "voltx_keys" in data: voltx_keys = data["voltx_keys"]
                            if "voltx_auto_mode" in data: voltx_auto_mode = data["voltx_auto_mode"]
                            save_local_data()
                    except Exception as e:
                        pass
                    
                    if target_msg_id: edit_message(chat_id, target_msg_id, "⏳ <b>Syncing SQLite data to Firebase... Please wait.</b>")
                    migrate_sqlite_to_firebase()
                    
                    if target_msg_id: edit_message(chat_id, target_msg_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
                    send_message(chat_id, "✅ <b>Firebase successfully connected & Synced!</b>")
                except Exception as e:
                    if target_msg_id: edit_message(chat_id, target_msg_id, f"❌ <b>Failed to connect to Firebase:</b>\n<code>{html.escape(str(e))}</code>", reply_markup=get_back_only_keyboard())
            except Exception:
                if target_msg_id: edit_message(chat_id, target_msg_id, "⚠️ <b>Invalid JSON format. Please upload the .json file directly.</b>", reply_markup=get_back_only_keyboard())
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return

        elif isinstance(state, str) and state.startswith("waiting_withdraw_amount_"):
            method = state[24:]
            user_data = get_user(user_id)
            bal = user_data.get("balance", 0)
            min_w = bot_settings.get("min_withdraw", 10.0)
            
            try:
                amount = float(text.strip())
                if amount < min_w:
                    if target_msg_id: edit_message(chat_id, target_msg_id, f"❌ <b>Minimum withdraw is {min_w} BDT.</b>\nTry again:", reply_markup=get_back_only_keyboard())
                elif amount > bal:
                    if target_msg_id: edit_message(chat_id, target_msg_id, f"❌ <b>Insufficient balance! You only have {bal:.2f} BDT.</b>\nTry again:", reply_markup=get_back_only_keyboard())
                else:
                    user_states[user_id] = {"state": f"waiting_withdraw_number_{method}_{amount}", "msg_id": target_msg_id}
                    if target_msg_id: edit_message(chat_id, target_msg_id, f"✅ <b>Amount Set: {amount:.2f} BDT</b>\n\n💳 <b>Method:</b> {method}\n💬 <b>Now send your {method} account number:</b>", reply_markup=get_back_only_keyboard())
            except ValueError:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Invalid amount. Send numbers only.</b>\nTry again:", reply_markup=get_back_only_keyboard())
            delete_message(chat_id, message_id)
            return

        elif isinstance(state, str) and state.startswith("waiting_withdraw_number_"):
            parts = state.split("_")
            method = parts[3]
            amount = float(parts[4])
            
            user_data = get_user(user_id)
            bal = user_data.get("balance", 0)
            
            if bal < amount:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Insufficient balance!</b>", reply_markup=get_main_keyboard(user_id))
            else:
                account_number = text.strip()
                update_user_stats(user_id, -amount, 0)
                
                w_group = bot_settings.get("w_group", "")
                if w_group:
                    w_msg = (
                        f"<tg-emoji emoji-id=\"5420396762189831222\">🆕</tg-emoji> <b>NEW WITHDRAW REQUEST</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"<tg-emoji emoji-id=\"5352861489541714456\">👤</tg-emoji> <b>User:</b> <a href='tg://user?id={user_id}'>{html.escape(first_name)}</a>\n"
                        f"— — — — — — — — — —\n"
                        f"<tg-emoji emoji-id=\"5226929552319594190\">🆔</tg-emoji> <b>ID:</b> <code>{user_id}</code>\n"
                        f"— — — — — — — — — —\n"
                        f"<tg-emoji emoji-id=\"5429612421977253466\">💵</tg-emoji> <b>Amount:</b> <b>{amount:.2f} BDT</b>\n"
                        f"— — — — — — — — — —\n"
                        f"<tg-emoji emoji-id=\"5190899075968441286\">💳</tg-emoji> <b>Method:</b> {method}\n"
                        f"— — — — — — — — — —\n"
                        f"<tg-emoji emoji-id=\"5337132498965010628\">📞</tg-emoji> <b>Number:</b> <code>{account_number}</code>\n"
                        f"━━━━━━━━━━━━━━━━━"
                    )
                    
                    w_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "APPROVE", "icon_custom_emoji_id": "5352694861990501856", "callback_data": f"admin_w_approve_{user_id}_{amount}_{account_number}", "style": "success"},
                                {"text": "REJECT", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"admin_w_reject_{user_id}_{amount}_{account_number}", "style": "danger"}
                            ]
                        ]
                    }
                    send_message(w_group, w_msg, reply_markup=w_markup)
                
                success_msg = (
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"<tg-emoji emoji-id=\"5429612421977253466\">💵</tg-emoji> <b>Amount:</b> {amount:.2f} BDT\n"
                    f"— — — — — — — — — —\n"
                    f"<tg-emoji emoji-id=\"5190899075968441286\">💳</tg-emoji> <b>Method:</b> {method}\n"
                    f"— — — — — — — — — —\n"
                    f"<tg-emoji emoji-id=\"5337132498965010628\">📞</tg-emoji> <b>Number:</b> <code>{account_number}</code>\n"
                    f"━━━━━━━━━━━━━━━━━"
                )
                if target_msg_id:
                    delete_message(chat_id, target_msg_id)
                send_message(chat_id, success_msg, reply_markup=get_main_keyboard(user_id))
                    
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return

        elif state == "waiting_dxa_ref_r":
            try:
                bot_settings["refer_reward"] = float(text.strip())
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
            except ValueError: pass
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return

        elif state == "waiting_dxa_min_w":
            try:
                bot_settings["min_withdraw"] = float(text.strip())
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
            except ValueError: pass
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif state == "waiting_dxa_cool":
            try:
                bot_settings["cooldown"] = int(text.strip())
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
            except ValueError: pass
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif state == "waiting_dxa_num_req":
            try:
                bot_settings["num_req"] = int(text.strip())
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
            except ValueError: pass
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif state == "waiting_w_method":
            bot_settings["w_methods"].append(text.strip())
            save_local_data()
            if target_msg_id: edit_message(chat_id, target_msg_id, "💳 <b>WITHDRAW METHODS</b>\nManage methods below:", reply_markup=dxa_manage_w_methods_keyboard())
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif state == "waiting_dxa_def_rate":
            try:
                bot_settings["otp_default_rate"] = float(text.strip())
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "💰 <b>OTP REWARD CONTROL</b>\nManage rates below:", reply_markup=dxa_otp_control_keyboard())
            except ValueError: pass
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif state == "waiting_dxa_srv_rate":
            try:
                parts = text.split("-")
                srv_name = parts[0].strip()
                srv_rate = float(parts[1].strip())
                bot_settings["otp_service_rates"][srv_name] = srv_rate
                save_local_data()
                if target_msg_id: edit_message(chat_id, target_msg_id, "💰 <b>OTP REWARD CONTROL</b>\nManage rates below:", reply_markup=dxa_otp_control_keyboard())
            except Exception:
                send_message(chat_id, "❌ Invalid format. Use: ServiceName - Rate (e.g. Telegram - 5.0)")
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif state == "waiting_uc_id":
            try:
                target_uid = int(text.strip())
                target_data = get_user(target_uid)
                
                if target_data:
                    safe_name = html.escape(str(target_data.get('first_name', 'User')))
                    total_otps = target_data.get('total_otps', 0)
                    msg = (
                        f"<tg-emoji emoji-id=\"5352861489541714456\">👤</tg-emoji> <b>USER PROFILE</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"<b>Name:</b> {safe_name}\n"
                        f"<b>ID:</b> <code>{target_uid}</code>\n"
                        f"<b>Balance:</b> {target_data.get('balance', 0):.2f} BDT\n"
                        f"<b>Total OTPs:</b> {total_otps}\n"
                        f"<b>Total Invites:</b> {target_data.get('total_invites', 0)}\n"
                        f"━━━━━━━━━━━━━━━━━"
                    )
                    if target_msg_id: edit_message(chat_id, target_msg_id, msg, reply_markup=get_user_profile_keyboard(target_uid))
                else:
                    if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>User not found in database!</b>", reply_markup=get_user_control_keyboard())
            except ValueError:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Invalid User ID format!</b>", reply_markup=get_user_control_keyboard())
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return
            
        elif isinstance(state, str) and state.startswith("waiting_uc_balance_"):
            target_uid = int(state[19:])
            try:
                amount_to_add = float(text.strip())
                update_user_stats(target_uid, amount_to_add, 0)
                
                target_data = get_user(target_uid)
                msg = (
                    f"✅ <b>Balance Updated Successfully!</b>\n\n"
                    f"<tg-emoji emoji-id=\"5352861489541714456\">👤</tg-emoji> <b>Name:</b> {html.escape(target_data['first_name'])}\n"
                    f"<tg-emoji emoji-id=\"5429612421977253466\">💵</tg-emoji> <b>New Balance:</b> {target_data['balance']:.2f} BDT"
                )
                if target_msg_id: edit_message(chat_id, target_msg_id, msg, reply_markup=get_user_profile_keyboard(target_uid))
            except ValueError:
                if target_msg_id: edit_message(chat_id, target_msg_id, "❌ <b>Invalid amount format! Use numbers (e.g. 50 or -10).</b>", reply_markup=get_user_profile_keyboard(target_uid))
            del user_states[user_id]
            delete_message(chat_id, message_id)
            return

    if text in _MENU_CMDS:
        if user_id in user_states: del user_states[user_id]

    # 🌟 চেক করা হচ্ছে ইউজার আগে থেকেই ডাটাবেসে আছে কি না
    is_new_user = (get_user(user_id) is None)

    add_user(user_id, first_name)
    user_data = get_user(user_id)

    pasted_range = None if (text.startswith("/") or not feat_on("search")) else parse_range_input(text)
    if pasted_range:
        allocate_pasted_range(chat_id, user_id, pasted_range)
        return

    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            param = parts[1]
            if param.startswith("alloc_") or param.startswith("spalloc_"):
                try:
                    _, req_range, req_sid = param.split("_", 2)
                    
                    last_req = user_cooldowns.get(user_id, 0)
                    now = time.time()
                    if now - last_req < bot_settings.get("cooldown", 0):
                        send_message(chat_id, f"⌛ <b>Please wait {int(bot_settings['cooldown'] - (now - last_req))} seconds before requesting again.</b>")
                        return
                    user_cooldowns[user_id] = now
                    
                    # 🌟 মেসেজ পাঠিয়ে মেসেজ আইডি ক্যাচ করা
                    wait_msg = send_message(chat_id, f"⏳ <b>Fetching number for {req_sid.title().replace('_', ' ')}...</b>")
                    loader = None
                    if wait_msg and "result" in wait_msg:
                        loader = Loader(chat_id, wait_msg["result"]["message_id"], "Processing...", ["Finding Number"]).start()

                    if param.startswith("spalloc_"):
                        msg, markup = get_smspro_allocation_content(req_sid, get_country_info(req_range)[0], user_id=user_id, by_prefix=req_range)
                    else:
                        msg, markup = get_number_allocation_content(req_sid, req_range, user_id=user_id)
                    if loader: loader.stop()

                    # 🌟 নতুন মেসেজ না দিয়ে আগেরটাতেই এডিট করা
                    if wait_msg and "result" in wait_msg:
                        edit_message(chat_id, wait_msg["result"]["message_id"], msg, reply_markup=markup)
                    else:
                        send_message(chat_id, msg, reply_markup=markup)
                    return
                except Exception:
                    pass
            else:
                try:
                    inviter_id = int(param)
                    # 🌟 শুধুমাত্র নতুন ইউজার হলেই রেফারেল কাউন্ট হবে!
                    if is_new_user and inviter_id != user_id and get_user(inviter_id):
                        ref_amount = bot_settings.get("refer_reward", 0.2)
                        update_user_stats(inviter_id, ref_amount, 1)
                        _re = bot_settings.setdefault("ref_earn", {}); _re[str(inviter_id)] = round(float(_re.get(str(inviter_id), 0)) + float(ref_amount), 2); save_local_data()
                        send_message(inviter_id, f"🎉 <b>New Referral!</b>\nSomeone joined using your link. You received {ref_amount} BDT.")
                except ValueError: pass
        _nm = html.escape(str(first_name))
        try: _live = (_temoji("green", "🟢") + " <b>Live</b> · OTP engine online") if (panels_health()[0] or stex_on()) else "🟠 <b>Syncing</b> · OTP engine starting"
        except Exception: _live = ""
        _start_rich = (
            f'<h3><i>{html.escape(BOT_TITLE)}</i> {_temoji("star", "⭐")}</h3><hr/>'
            f'<p>{_temoji("wave", "👋")} Hello, <b>{_nm}</b>!</p>'
            f'<p>Get OTP codes instantly using virtual phone numbers – fast, reliable, global.</p>'
            f'<p>{_temoji("spark", "🌟")} Instant Delivery · {_temoji("globe", "🌐")} Global Numbers</p>'
            f'<p>{_temoji("folder", "📂")} Auto OTP Detection</p><p>{_live}</p><hr/>'
            f'<p>{_temoji("rocket", "🚀")} Use the menu buttons below:</p>'
        )
        if not send_rich_message(chat_id, _start_rich, reply_markup=get_main_keyboard(user_id)):
            send_message(chat_id,
                f"<b><i>{html.escape(BOT_TITLE)}</i></b> {_temoji('star', '⭐')}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"{_temoji('wave', '👋')} Hello, <b>{_nm}</b>!\n\n"
                f"Get OTP codes instantly using virtual phone numbers – fast, reliable, global.\n\n"
                f"{_temoji('spark', '🌟')} Instant Delivery · {_temoji('globe', '🌐')} Global Numbers\n"
                f"{_temoji('folder', '📂')} Auto OTP Detection\n{_live}\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"{_temoji('rocket', '🚀')} Use the menu buttons below:",
                reply_markup=get_main_keyboard(user_id))
        
    elif text == "LIVE NUMBER":
        msg, markup = get_services_content()
        if markup:
            send_message(chat_id, msg, reply_markup=markup, gif="services")
        else:
            send_message(chat_id, msg)
        
    elif text == "LIVE TRAFFIC":
        msg, markup = get_live_traffic_content()
        send_message(chat_id, msg, reply_markup=markup, gif="traffic")
            
    elif text == "INVITE FRIEND":
        invite_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        ref_reward = bot_settings.get("refer_reward", 0.2)
        _earn = (bot_settings.get("ref_earn") or {}).get(str(user_id))
        if _earn is None: _earn = user_data["total_invites"] * ref_reward
        msg = (
            f'{_temoji("user", "👥")} <b>Refer &amp; Earn</b>\n━━━━━━━━━━━━━━━━━\n'
            f'{_temoji("gift", "🎉")} আপনার বন্ধুদের এই লিংক দিয়ে ইনভাইট করুন। ওরা bot এ জয়েন করলেই আপনি '
            f'<b>{ref_reward} TK</b> বোনাস সরাসরি আপনার ব্যালেন্সে পাবেন!\n\n'
            f'{_temoji("grp", "👥")} <b>Total Referred:</b> {user_data["total_invites"]}\n'
            f'{_temoji("money", "💰")} <b>Total Referral Earning:</b> {float(_earn):.2f} TK\n━━━━━━━━━━━━━━━━━\n'
            f'{_temoji("share", "🔗")} <b>Your Referral Link:</b>\n{html.escape(invite_link)}'
        )
        _share = "https://t.me/share/url?url=" + urllib.parse.quote(invite_link, safe="") + "&text=" + urllib.parse.quote("Get OTP numbers instantly 🚀", safe="")
        send_message(chat_id, msg, gif="refer", reply_markup={"inline_keyboard": [
            [{"text": "Share Link", "url": _share, "icon_custom_emoji_id": IC["share"], "style": "primary"}],
            [{"text": "Close", "callback_data": "close_panel", "icon_custom_emoji_id": IC["no"], "style": "danger"}]]})

    elif text == "WALLET":
        _bal = float(user_data.get("balance", 0) or 0); _minw = float(bot_settings.get("min_withdraw", 10.0))
        _bar_n = 0 if _minw <= 0 else max(0, min(10, int(_bal / _minw * 10)))
        _bar = "▰" * _bar_n + "▱" * (10 - _bar_n)
        _state = (f'{_temoji("ok", "✅")} <b>Ready to withdraw</b>' if _bal >= _minw
                  else f'{_temoji("hour", "⏳")} <i>{max(_minw - _bal, 0):.2f} BDT more to unlock withdraw</i>')
        wallet_txt = (
            f'<blockquote>{_temoji("money", "💰")} <b>YOUR WALLET</b></blockquote>\n'
            f'━━━━━━━━━━━━━━━━━\n'
            f'{_temoji("user", "👤")} <b>Name</b>  ➜  {html.escape(str(first_name))}\n'
            f'{_temoji("id", "🆔")} <b>ID</b>  ➜  <code>{user_id}</code>\n'
            f'{_temoji("grp", "👥")} <b>Invites</b>  ➜  <code>{int(user_data.get("total_invites", 0) or 0)}</code>\n'
            f'━━━━━━━━━━━━━━━━━\n'
            f'<blockquote><b>BALANCE</b>  ➜  <b>{_bal:.2f} BDT</b>\n<code>{_bar}</code>  <i>min {_minw:g}</i></blockquote>\n'
            f'{_state}\n\n'
            f'{_temoji("5190899075968441286", "💳")} <b>Withdraw methods</b> <i>(tap one)</i>'
        )
        kb = {"inline_keyboard": []}
        for method in bot_settings.get("w_methods", []):
            kb["inline_keyboard"].append([{"text": method, "icon_custom_emoji_id": "5431783519355444455", "callback_data": f"user_withdraw_{method}", "style": "primary"}])
        send_message(chat_id, wallet_txt, reply_markup=kb, gif="wallet")
        
    elif text == "LEADERBOARD":
        send_message(chat_id, get_leaderboard_text(), reply_markup=get_leaderboard_keyboard(), gif="leaderboard")

    elif text == "SEARCH NUMBER":
        send_message(chat_id, f'{_temoji("search", "🔍")} <b>Search Number</b>\n'
                              f'<i>Send a range / prefix and I will find a number for it.</i>\n'
                              f'Example: <code>23762</code> or <code>2519474XXX</code>', gif="search")

    elif text == "2FA ONLINE":
        user_states[user_id] = {"state": "waiting_2fa_secret"}
        send_message(chat_id, twofa_prompt_text(), reply_markup=twofa_prompt_kb(), gif="twofa")

    elif text == "GMAIL":
        _gt, _gk = gmail_user_content(user_id)
        send_message(chat_id, _gt, reply_markup=_gk, gif="gmail")

    elif text == "SUPPORT":
        _sl = (bot_settings.get("support_link") or "").strip()
        if _sl:
            _url = _sl if _sl.startswith("http") else f"https://t.me/{_sl.lstrip('@')}"
            send_message(chat_id, f'{_temoji("chat", "💬")} <b>Need help?</b>\n<i>Tap the button below to contact support.</i>',
                         gif="support", reply_markup={"inline_keyboard": [[{"text": "Contact Support", "url": _url, "icon_custom_emoji_id": IC["chat"], "style": "primary"}]]})
        else:
            send_message(chat_id, f'{_temoji("chat", "💬")} <b>Support</b>\n<i>Support link is not set yet.</i>')
    elif text == "OWNER PANEL":
        if user_id == ADMIN_ID:
            send_message(chat_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard(), gif="owner")
        else:
            send_message(chat_id, "⚠️ <b>Access Denied!</b>\nThis menu is restricted to the admin.")

def handle_callback(callback_query):
    global user_states, voltx_auto_mode, current_db_mode, force_join_status, force_join_channels, voltx_keys, otp_forward_groups, bot_settings
    query_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    data = callback_query["data"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]

    if data == "check_fj_joined":
        if is_user_joined(user_id):
            delete_message(chat_id, message_id)
            send_message(chat_id, "✅ <b>Thank you for joining! You can now use the bot.</b>", reply_markup=get_main_keyboard(user_id))
        else:
            answer_callback_query(query_id, "❌ You haven't joined all channels!", show_alert=True)
        return

    if not is_user_joined(user_id):
        answer_callback_query(query_id, "Join channels first!", show_alert=True)
        return

    if user_id != ADMIN_ID and bot_settings.get("maintenance"):
        answer_callback_query(query_id, "Bot is under maintenance. Please come back later.", show_alert=True)
        return
    if data.startswith(("srv_", "sv2_", "cc_", "sp_", "rg_")) and not feat_on("get"):
        answer_callback_query(query_id, "This option is currently turned off by admin.", show_alert=True)
        return
    if data.startswith("gmail_") and not feat_on("gmail"):
        answer_callback_query(query_id, "Gmail is currently turned off by admin.", show_alert=True)
        return
    if data.startswith("twofa_") and not feat_on("twofa"):
        answer_callback_query(query_id, "2FA is currently turned off by admin.", show_alert=True)
        return
    if data.startswith(("ft_", "hs_", "gm_")) and user_id != ADMIN_ID:
        answer_callback_query(query_id, "Admin only.", show_alert=True)
        return

    if data == "manage_smspro" or data.startswith("spa_") or data.startswith("xpa_") or data.startswith("cpa_"):
        handle_smspro_callback(data, query_id, user_id, chat_id, message_id)
        return

    if data.startswith("sp_"):
        try:
            _, sp_sid, sp_code = data.split("_", 2)
        except ValueError:
            answer_callback_query(query_id, "Invalid", show_alert=True); return
        last_req = user_cooldowns.get(user_id, 0)
        now = time.time()
        if now - last_req < bot_settings.get("cooldown", 0):
            answer_callback_query(query_id, f"Please wait {int(bot_settings['cooldown'] - (now - last_req))} seconds.", show_alert=True)
            return
        user_cooldowns[user_id] = now
        answer_callback_query(query_id, "Allocating number...")
        loader = Loader(chat_id, message_id, "Processing...", ["Finding Number"]).start()
        msg, markup = get_smspro_allocation_content(sp_sid, sp_code, user_id=user_id)
        loader.stop()
        edit_message(chat_id, message_id, msg, reply_markup=markup, gif="number")
        return

    if data == "explore_ranges":
        msg, markup = get_explore_ranges_content()
        edit_message(chat_id, message_id, msg, reply_markup=markup)
        answer_callback_query(query_id)
        return

    if data == "refresh_traffic":
        answer_callback_query(query_id, "Refreshing...")
        edit_message(chat_id, message_id, "<blockquote>📈 <b>LIVE TRAFFIC</b></blockquote>\n◜ <code>▰▰▰▱▱▱</code>\n<i>Syncing live data</i>")
        time.sleep(0.35)
        msg, markup = get_live_traffic_content()
        edit_message(chat_id, message_id, msg, reply_markup=markup, gif="traffic")
        return

    if data == "back_to_services":
        msg, markup = get_services_content()
        edit_message(chat_id, message_id, msg, reply_markup=markup, gif="services" if markup else None)
        answer_callback_query(query_id)
        return

    if data.startswith("sv2_"):
        msg, markup, err = get_countries_content(data[4:], v2=True)
        if err: answer_callback_query(query_id, err, show_alert=True)
        else:
            edit_message(chat_id, message_id, msg, reply_markup=markup, gif="countries")
            answer_callback_query(query_id)
        return

    if data == "noop":
        answer_callback_query(query_id)
        return

    if data.startswith("cpg_"):
        try:
            _, _tag, _pg, _sid = data.split("_", 3)
        except ValueError:
            answer_callback_query(query_id, "Invalid", show_alert=True); return
        msg, markup, err = get_countries_content(_sid, v2=(_tag == "2"), page=int(_pg))
        if err: answer_callback_query(query_id, err, show_alert=True)
        else:
            edit_message(chat_id, message_id, msg, reply_markup=markup, gif="countries")
            answer_callback_query(query_id)
        return

    if data.startswith("nc_"):
        _num = re.sub(r"\D", "", data[3:])
        answer_callback_query(query_id, "Checking...")
        _e = find_number_otp(_num, chat_id)
        if _e:
            _shtml, _seid = get_service_info_html(_e.get("service", ""))
            send_message(chat_id, f'<blockquote>{_temoji("ok", "✅")} <b>CODE FOUND</b></blockquote>\n'
                                  f'{_shtml} ➜ <code>+{_num}</code>\n— — — — — — — — — —\n'
                                  f'{_temoji("lock", "🔐")} <b>CODE</b> ➜ <code>{html.escape(_e["code"])}</code>',
                         reply_markup={"inline_keyboard": [[{"text": _e["code"], "copy_text": {"text": _e["code"]}, "icon_custom_emoji_id": _seid, "style": "success"}]]})
        else:
            send_message(chat_id, f'{_temoji("no", "❌")} <b>Code not found</b>\n<code>+{_num}</code>\n<i>No SMS arrived yet. Request the OTP on the app, then tap Code again.</i>')
        return

    if data == "rcc_rm" or data.startswith("rcc_add_"):
        try:
            _rows = callback_query["message"]["reply_markup"]["inline_keyboard"]
        except Exception:
            answer_callback_query(query_id, "Please get a new number", show_alert=True); return
        _removing = (data == "rcc_rm")
        _cc = None if _removing else data[len("rcc_add_"):]
        edit_reply_markup(chat_id, message_id, toggle_remove_cc(_rows, _removing, _cc))
        answer_callback_query(query_id, "Country code removed" if _removing else "Country code added")
        return

    if data == "srv_other":
        if cached_other_kb:
            edit_message(chat_id, message_id, f'<blockquote>{_temoji("globe", "🌐")} <b>OTHER SERVICE</b></blockquote>\n<i>{_temoji("arrow", "➤")} Select a service</i>', reply_markup=cached_other_kb, gif="services")
            answer_callback_query(query_id)
        else:
            answer_callback_query(query_id, "No other service right now.", show_alert=True)
        return

    if data.startswith("srv_"):
        sid_short = data[4:]
        msg, markup, err = get_countries_content(sid_short)
        if err: answer_callback_query(query_id, err, show_alert=True)
        else:
            edit_message(chat_id, message_id, msg, reply_markup=markup, gif="countries")
            answer_callback_query(query_id)
        return

    if data.startswith("rg_"):
        try:
            _, rng, svc_name = data.split("_", 2)
        except ValueError:
            answer_callback_query(query_id, "Invalid range", show_alert=True); return
        answer_callback_query(query_id, "Allocating number...")
        allocate_pasted_range(chat_id, user_id, rng, service=svc_name, edit_id=message_id)
        return

    if data.startswith("cc_"):
        parts = data.split("_")
        sid_short = parts[1]
        c_code = parts[2]
        msg, markup = get_ranges_content(sid_short, c_code)
        if not msg:
            answer_callback_query(query_id, "No active range for this country right now.", show_alert=True)
            return
        edit_message(chat_id, message_id, msg, reply_markup=markup, gif="countries")
        answer_callback_query(query_id)
        return

    if data == "refresh_leaderboard":
        answer_callback_query(query_id, "Refreshing...")
        edit_message(chat_id, message_id, "<blockquote>🏆 <b>LEADERBOARD</b></blockquote>\n◜ <code>▰▰▰▱▱▱</code>\n<i>Ranking players</i>")
        time.sleep(0.35)
        edit_message(chat_id, message_id, get_leaderboard_text(), reply_markup=get_leaderboard_keyboard(), gif="leaderboard")
        return

    if data.startswith("user_withdraw_"):
        method = data[14:]
        user_data = get_user(user_id)
        bal = user_data.get("balance", 0)
        min_w = bot_settings.get("min_withdraw", 10.0)
        
        if bal < min_w:
            answer_callback_query(query_id, f"❌ Minimum withdraw is {min_w} BDT. You have {bal:.2f} BDT.", show_alert=True)
            return
            
        user_states[user_id] = {"state": f"waiting_withdraw_amount_{method}", "msg_id": message_id}
        edit_message(chat_id, message_id, f"💳 <b>Withdraw via {method}</b>\n\n💵 Your Balance: {bal:.2f} BDT\n💬 <b>Enter the amount you want to withdraw:</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
        return

    if data == "user_cancel":
        if user_id in user_states: del user_states[user_id]
        delete_message(chat_id, message_id)
        send_message(chat_id, "❌ Action Cancelled.", reply_markup=get_main_keyboard(user_id))
        answer_callback_query(query_id)
        return

    # Admin Operations Only
    if user_id != ADMIN_ID:
        answer_callback_query(query_id, "Access Denied!", show_alert=True)
        return
    if not (data.startswith("stk_") or data == "manage_stock"): stock_view.pop(chat_id, None)
        
    if data.startswith("admin_w_approve_") or data.startswith("admin_w_reject_"):
        if user_id != ADMIN_ID:
            answer_callback_query(query_id, "⚠️ Access Denied! You cannot perform this action.", show_alert=True)
            return

        parts = data.split("_")
        action = parts[2]
        req_user_id = int(parts[3])
        amount = float(parts[4])
        acc_number = parts[5]
        
        masked_number = f"{acc_number[:3]}DXA{acc_number[-3:]}" if len(acc_number) >= 6 else acc_number
        
        msg_text = callback_query.get("message", {}).get("text", "")
        fname_match = re.search(r"User:\s*(.+)", msg_text)
        req_first_name = fname_match.group(1) if fname_match else "User"
        method_match = re.search(r"Method:\s*(.+)", msg_text)
        req_method = method_match.group(1) if method_match else "Unknown"
        
        if action == "approve":
            btn_text = "APPROVED BY ADMIN"
            btn_style = "success"
            btn_icon = "5352694861990501856"
            send_message(req_user_id, f"<tg-emoji emoji-id=\"5420396762189831222\">🎉</tg-emoji> <b>Withdrawal Approved!</b>\nYour request of {amount:.2f} BDT has been sent to <code>{masked_number}</code>.")
        else:
            btn_text = "REJECTED BY ADMIN"
            btn_style = "danger"
            btn_icon = "5420130255174145507"
            update_user_stats(req_user_id, amount, 0)
            send_message(req_user_id, f"<tg-emoji emoji-id=\"5336944168944047463\">⚠️</tg-emoji> <b>Withdrawal Rejected!</b>\nYour request of {amount:.2f} BDT to <code>{masked_number}</code> was rejected and refunded.")
            
        updated_msg = (
            f"<tg-emoji emoji-id=\"5420396762189831222\">🆕</tg-emoji> <b>WITHDRAW REQUEST PROCESSED</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"<tg-emoji emoji-id=\"5352861489541714456\">👤</tg-emoji> <b>User:</b> <a href='tg://user?id={req_user_id}'>{html.escape(req_first_name)}</a>\n"
            f"— — — — — — — — — —\n"
            f"<tg-emoji emoji-id=\"5226929552319594190\">🆔</tg-emoji> <b>ID:</b> <code>{req_user_id}</code>\n"
            f"— — — — — — — — — —\n"
            f"<tg-emoji emoji-id=\"5429612421977253466\">💵</tg-emoji> <b>Amount:</b> <b>{amount:.2f} BDT</b>\n"
            f"— — — — — — — — — —\n"
            f"<tg-emoji emoji-id=\"5190899075968441286\">💳</tg-emoji> <b>Method:</b> {req_method}\n"
            f"— — — — — — — — — —\n"
            f"<tg-emoji emoji-id=\"5431783519355444455\">📞</tg-emoji> <b>Number:</b> <code>{masked_number}</code>\n"
            f"━━━━━━━━━━━━━━━━━"
        )
        
        updated_markup = {
            "inline_keyboard": [
                [{"text": btn_text, "icon_custom_emoji_id": btn_icon, "callback_data": "ignore_action", "style": btn_style}]
            ]
        }
        
        edit_message(chat_id, message_id, updated_msg, reply_markup=updated_markup)
        answer_callback_query(query_id, f"Request {action.title()}d!")
        return

    # ===== Voltx Key Manager =====
    if data == "manage_voltx":
        edit_message(chat_id, message_id, "🔑 <b>STEX SMS KEY SYSTEM</b>\nManage API Keys below:", reply_markup=get_voltx_keys_keyboard())
        answer_callback_query(query_id)
    elif data == "toggle_vk":
        voltx_auto_mode = not voltx_auto_mode
        save_local_data()
        edit_message(chat_id, message_id, "🔑 <b>STEX SMS KEY SYSTEM</b>\nManage API Keys below:", reply_markup=get_voltx_keys_keyboard())
        answer_callback_query(query_id, f"Voltx Mode: {'ON' if voltx_auto_mode else 'OFF'}")
    elif data in ("toggle_mino", "toggle_mino_a", "mino_settings", "mino_test", "mino_setkey") and user_id == ADMIN_ID:
        if data in ("toggle_mino", "toggle_mino_a"):
            if not mino_key(): answer_callback_query(query_id, "Set the Mino API key first", show_alert=True); return
            bot_settings["mino_on"] = not mino_on()
            save_local_data()
        if data == "mino_test":
            answer_callback_query(query_id, "Testing...")
            _mino_last_test[0] = mino_test() + " · " + time.strftime("%H:%M:%S")
        elif data == "mino_setkey":
            user_states[user_id] = {"state": "waiting_mino_key", "msg_id": message_id}
            edit_message(chat_id, message_id, "🔑 <b>SET MINO API KEY</b>\nSend the new Mino API key now. (Your message is deleted right away.)", reply_markup={"inline_keyboard": [[{"text": "Back", "callback_data": "mino_settings", "icon_custom_emoji_id": "5438541186539232243", "style": "primary"}]]})
            answer_callback_query(query_id); return
        else:
            answer_callback_query(query_id, "Mino SMS: " + ("ON" if mino_on() else "OFF"))
        if data == "toggle_mino_a":
            edit_message(chat_id, message_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
        else:
            user_states.pop(user_id, None)
            edit_message(chat_id, message_id, mino_status_text(), reply_markup=mino_settings_kb())
    elif data in ("toggle_stex", "toggle_stex_a") and user_id == ADMIN_ID:
        bot_settings["stex_on"] = not stex_on()
        save_local_data()
        if data == "toggle_stex":
            edit_message(chat_id, message_id, "🔑 <b>STEX SMS KEY SYSTEM</b>\nManage API Keys below:", reply_markup=get_voltx_keys_keyboard())
        else:
            edit_message(chat_id, message_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
        answer_callback_query(query_id, "Stex SMS: " + ("ON" if stex_on() else "OFF - panels only"))
    elif data == "toggle_btnstyle" and user_id == ADMIN_ID:
        bot_settings["transparent_buttons"] = not bot_settings.get("transparent_buttons", True)
        save_local_data()
        edit_message(chat_id, message_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
        answer_callback_query(query_id, "Buttons: " + ("transparent" if bot_settings["transparent_buttons"] else "colored"))
    elif data == "add_vk":
        user_states[user_id] = {"state": "waiting_vk_key", "msg_id": message_id}
        edit_message(chat_id, message_id, "🔑 <b>Please reply to this message with the new Stex SMS API Key.</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data.startswith("del_vk_"):
        idx = int(data[7:])
        if idx < len(voltx_keys): 
            if len(voltx_keys) > 1:
                voltx_keys.pop(idx)
                save_local_data()
                edit_message(chat_id, message_id, "🔑 <b>STEX SMS KEY SYSTEM</b>\nManage API Keys below:", reply_markup=get_voltx_keys_keyboard())
                answer_callback_query(query_id, "Key Deleted!")
            else:
                answer_callback_query(query_id, "Cannot delete the only key!", show_alert=True)

    # ===== Force Join Manager =====
    elif data == "manage_fj":
        edit_message(chat_id, message_id, "🔗 <b>FORCE JOIN SYSTEM</b>\nManage channels below:", reply_markup=get_force_join_keyboard())
        answer_callback_query(query_id)
    elif data == "toggle_fj":
        force_join_status = not force_join_status
        save_local_data()
        edit_message(chat_id, message_id, "🔗 <b>FORCE JOIN SYSTEM</b>\nManage channels below:", reply_markup=get_force_join_keyboard())
        answer_callback_query(query_id, f"Force Join: {'ON' if force_join_status else 'OFF'}")
    elif data == "add_fj":
        user_states[user_id] = {"state": "waiting_fj_channel", "msg_id": message_id}
        select_kb = {"keyboard": [[{"text": "Select Force Join Channel", "request_chat": {"request_id": 3, "chat_is_channel": True}}], [{"text": "🔙 BACK"}]], "resize_keyboard": True, "one_time_keyboard": True}
        send_message(chat_id, "👇 <b>Select Channel from below:</b>\n<i>(Make sure bot is an admin there)</i>", reply_markup=select_kb)
        answer_callback_query(query_id)
    elif data.startswith("del_fj_"):
        idx = int(data[7:])
        if idx < len(force_join_channels): force_join_channels.pop(idx)
        save_local_data()
        edit_message(chat_id, message_id, "🔗 <b>FORCE JOIN SYSTEM</b>\nManage channels below:", reply_markup=get_force_join_keyboard())
        answer_callback_query(query_id, "Channel Deleted!")

    # ===== Owner added services (ADD SERVICE) =====
    elif data == "manage_msvc":
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, manual_services_text(), reply_markup=manual_services_keyboard())
        answer_callback_query(query_id)
    elif data == "msvc_add":
        user_states[user_id] = {"state": "waiting_msvc_name", "msg_id": message_id}
        edit_message(chat_id, message_id, "🧩 <b>Send the service name</b> (e.g. <code>Telegram</code>)\n\nYou can send name + ranges together:\n<code>Telegram - 2519474XXX, 2519475XXX</code>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data.startswith("msvc_del_"):
        idx = int(data[9:])
        names = list((bot_settings.get("manual_services") or {}).keys())
        if idx < len(names):
            bot_settings["manual_services"].pop(names[idx], None)
            save_local_data()
        edit_message(chat_id, message_id, manual_services_text(), reply_markup=manual_services_keyboard())
        answer_callback_query(query_id, "Service deleted")
    elif data == "rgrp_auto":
        bot_settings["auto_range_on"] = not bot_settings.get("auto_range_on", True)
        save_local_data()
        edit_message(chat_id, message_id, RANGE_GROUP_TEXT, reply_markup=get_range_group_keyboard())
        answer_callback_query(query_id, "Auto range: " + ("ON" if bot_settings["auto_range_on"] else "OFF"))
    elif data == "rgrp_top":
        order = [3, 5, 10, 15]
        cur = int(bot_settings.get("auto_range_top", 5))
        bot_settings["auto_range_top"] = order[(order.index(cur) + 1) % len(order)] if cur in order else 5
        save_local_data()
        edit_message(chat_id, message_id, RANGE_GROUP_TEXT, reply_markup=get_range_group_keyboard())
        answer_callback_query(query_id, f"Top {bot_settings['auto_range_top']} traffic ranges")

    # ===== Range Group + Number Stock =====
    elif data == "manage_range_group":
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, RANGE_GROUP_TEXT, reply_markup=get_range_group_keyboard())
        answer_callback_query(query_id)
    elif data == "rgrp_add":
        user_states[user_id] = {"state": "waiting_range_group", "msg_id": message_id}
        select_kb = {"keyboard": [[{"text": "Select Range Group", "request_chat": {"request_id": 3, "chat_is_channel": False}}], [{"text": "🔙 BACK"}]], "resize_keyboard": True, "one_time_keyboard": True}
        send_message(chat_id, "👇 <b>Select Range Group from below:</b>", reply_markup=select_kb)
        answer_callback_query(query_id)
    elif data.startswith("rgrp_del_"):
        idx = int(data[9:])
        if idx < len(range_groups): range_groups.pop(idx)
        save_local_data()
        edit_message(chat_id, message_id, RANGE_GROUP_TEXT, reply_markup=get_range_group_keyboard())
        answer_callback_query(query_id, "Range group removed")
    elif data == "manage_stock":
        user_states.pop(user_id, None)
        show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id)
    elif data == "stk_add":
        user_states[user_id] = {"state": "waiting_stock_range", "msg_id": message_id}
        stock_view.pop(chat_id, None)
        edit_message(chat_id, message_id, "📦 <b>ADD RANGE</b>\nSend one or more ranges, one per line.\nExample: <code>2519474XXX</code>", reply_markup=stock_back_keyboard())
        answer_callback_query(query_id)
    elif data.startswith("stk_go_"):
        st = user_states.get(user_id)
        ranges = st.get("ranges") if isinstance(st, dict) and st.get("state") == "stock_pick" else None
        if not ranges:
            answer_callback_query(query_id, "Session expired. Tap ADD RANGE again.", show_alert=True)
        else:
            amount = int(data[7:])
            user_states.pop(user_id, None)
            show_stock_panel(chat_id, message_id)
            for r in ranges: stock_start(r, amount, chat_id, message_id)
            answer_callback_query(query_id, f"Stocking {_k(amount)} per range...")
    elif data.startswith("stk_del_"):
        r = data[8:]
        with stock_lock:
            if r in stock_jobs: stock_jobs[r]["stop"] = True
            number_stock.pop(r, None); stock_targets.pop(r, None); stock_dirty[0] = True
        stock_save()
        show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id, f"Range {r} removed")
    elif data == "stk_auto":
        bot_settings["stock_auto"] = not bot_settings.get("stock_auto", True)
        save_local_data(); show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id, "Auto refill: " + ("ON" if bot_settings["stock_auto"] else "OFF"))
    elif data == "stk_ttl":
        order = [0, 15, 30, 60]; cur = int(bot_settings.get("stock_ttl_min", 0) or 0)
        bot_settings["stock_ttl_min"] = order[(order.index(cur) + 1) % len(order)] if cur in order else 0
        save_local_data(); show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id, "Expiry: " + ("OFF" if not bot_settings["stock_ttl_min"] else f"{bot_settings['stock_ttl_min']} min"))
    elif data == "stk_def":
        order = [500, 1000, 2000]; cur = int(bot_settings.get("stock_default", 1000))
        bot_settings["stock_default"] = order[(order.index(cur) + 1) % len(order)] if cur in order else 1000
        save_local_data(); show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id, f"Default per range: {_k(bot_settings['stock_default'])}")
    elif data == "stk_stop":
        with stock_lock:
            for j in stock_jobs.values(): j["stop"] = True
        bot_settings["stock_auto"] = False
        save_local_data(); show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id, "Filling stopped. Auto refill is OFF.", show_alert=True)
    elif data == "stk_clear":
        confirm_kb = {"inline_keyboard": [[{"text": "YES, CLEAR", "callback_data": "stk_clear_yes", "style": "danger"}, {"text": "CANCEL", "callback_data": "manage_stock", "style": "success"}]]}
        stock_view.pop(chat_id, None)
        edit_message(chat_id, message_id, f"🧹 <b>Clear all stock?</b>\n{stock_total()} numbers will be deleted.", reply_markup=confirm_kb)
        answer_callback_query(query_id)
    elif data == "stk_clear_yes":
        with stock_lock:
            for j in stock_jobs.values(): j["stop"] = True
            number_stock.clear(); stock_targets.clear(); stock_dirty[0] = True
        stock_save()
        show_stock_panel(chat_id, message_id)
        answer_callback_query(query_id, "Stock cleared")

    # ===== OTP Group Manager =====
    elif data == "manage_otp_group":
        edit_message(chat_id, message_id, "🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:", reply_markup=get_otp_group_keyboard())
        answer_callback_query(query_id)
    elif data == "otp_bots_check":
        if user_id != ADMIN_ID: answer_callback_query(query_id, "Admin only", show_alert=True); return
        answer_callback_query(query_id, "Checking bots...")
        send_message(chat_id, otp_bots_check_text())
    elif data == "add_otp_group":
        user_states[user_id] = {"state": "waiting_otp_group", "msg_id": message_id}
        select_kb = {"keyboard": [[{"text": "Select OTP Group", "request_chat": {"request_id": 1, "chat_is_channel": False}}], [{"text": "🔙 BACK"}]], "resize_keyboard": True, "one_time_keyboard": True}
        send_message(chat_id, "👇 <b>Select OTP Group from below:</b>", reply_markup=select_kb)
        answer_callback_query(query_id)
    elif data.startswith("del_otp_group_"):
        idx = int(data[14:])
        if idx < len(otp_forward_groups): otp_forward_groups.pop(idx)
        save_local_data()
        edit_message(chat_id, message_id, "🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:", reply_markup=get_otp_group_keyboard())
        answer_callback_query(query_id, "Group Deleted!")
    elif data == "edit_otp_link":
        user_states[user_id] = {"state": "waiting_otp_link", "msg_id": message_id}
        edit_message(chat_id, message_id, "🔗 <b>Please send the new URL for the inline button attached to forwarded OTPs.</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "edit_main_channel":
        user_states[user_id] = {"state": "waiting_main_channel_link", "msg_id": message_id}
        edit_message(chat_id, message_id, "🔗 <b>Please send the URL for the MAIN CHANNEL inline button.</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)

    # ===== 2FA / Gmail (user side) =====
    elif data == "twofa_cancel":
        user_states.pop(user_id, None)
        answer_callback_query(query_id, "Cancelled")
        delete_message(chat_id, message_id)
    elif data == "twofa_ref":
        _res = twofa_result(user_id)
        if not _res:
            answer_callback_query(query_id, "Session expired. Tap 2FA Online and send your key again.", show_alert=True)
        else:
            edit_message(chat_id, message_id, _res[0], reply_markup=_res[1])
            answer_callback_query(query_id, "Refreshed")
    elif data == "gmail_more":
        _gt, _gk = gmail_user_content(user_id)
        send_message(chat_id, _gt, reply_markup=_gk)
        answer_callback_query(query_id)

    # ===== Admin: Features ON/OFF, Maintenance, Hide Services, Gmail =====
    elif data == "ft_panel":
        edit_message(chat_id, message_id, features_panel_text(), reply_markup=features_panel_kb())
        answer_callback_query(query_id)
    elif data.startswith("ft_t_"):
        key = data[5:]
        back_gm = key.endswith("_gm")
        if back_gm: key = key[:-3]
        if any(k == key for k, _l, _c in FEATURES):
            bot_settings[f"feat_{key}"] = not feat_on(key)
            save_local_data()
        if back_gm: edit_message(chat_id, message_id, gmail_panel_text(), reply_markup=gmail_panel_kb())
        else: edit_message(chat_id, message_id, features_panel_text(), reply_markup=features_panel_kb())
        answer_callback_query(query_id, ("ON" if feat_on(key) else "OFF") + f" · {key}")
    elif data in ("ft_maint", "ft_maint_p"):
        bot_settings["maintenance"] = not bot_settings.get("maintenance", False)
        save_local_data()
        if data == "ft_maint_p": edit_message(chat_id, message_id, features_panel_text(), reply_markup=features_panel_kb())
        else: edit_message(chat_id, message_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
        answer_callback_query(query_id, "Maintenance: " + ("ON (only admin can use the bot)" if bot_settings["maintenance"] else "OFF (bot is live)"), show_alert=bot_settings["maintenance"])
    elif data == "hs_panel":
        edit_message(chat_id, message_id, hide_panel_text(), reply_markup=hide_panel_kb())
        answer_callback_query(query_id)
    elif data.startswith("hs_t_"):
        name = data[5:]
        full = next((n for n in all_service_names() if n.lower().startswith(name.lower())), name)
        hid = bot_settings.setdefault("hidden_services", [])
        if is_hidden_service(full): hid[:] = [h for h in hid if str(h).lower() != full.lower()]
        else: hid.append(full)
        save_local_data()
        edit_message(chat_id, message_id, hide_panel_text(), reply_markup=hide_panel_kb())
        answer_callback_query(query_id, f"{full}: " + ("HIDDEN" if is_hidden_service(full) else "SHOWN"))
    elif data == "hs_clear":
        bot_settings["hidden_services"] = []
        save_local_data()
        edit_message(chat_id, message_id, hide_panel_text(), reply_markup=hide_panel_kb())
        answer_callback_query(query_id, "All services are visible again")
    elif data == "gm_panel":
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, gmail_panel_text(), reply_markup=gmail_panel_kb())
        answer_callback_query(query_id)
    elif data == "gm_add":
        user_states[user_id] = {"state": "waiting_gm_stock", "msg_id": message_id}
        edit_message(chat_id, message_id, f'{_temoji("gmail", "📧")} <b>ADD GMAIL STOCK</b>\n\nSend accounts, one per line:\n<code>name@gmail.com:password</code>\n<i>You can send several messages.</i>', reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data in ("gm_site", "gm_app"):
        user_states[user_id] = {"state": "waiting_gm_site" if data == "gm_site" else "waiting_gm_app", "msg_id": message_id}
        edit_message(chat_id, message_id, (f'{_temoji("share", "🌐")} <b>Send the Gmail site link</b> (must be <code>https://</code>). It opens inside the bot as a Mini App.' if data == "gm_site" else f'{_temoji("gmail", "🤖")} <b>Send the Telegram Mini App link</b>, e.g. <code>https://t.me/YourBot/gmail</code>') + "\n<i>Send 0 to remove.</i>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "gm_limit":
        user_states[user_id] = {"state": "waiting_gm_limit", "msg_id": message_id}
        edit_message(chat_id, message_id, "🔢 <b>Send daily Gmail limit per user</b> (0 = unlimited):", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "gm_clear":
        edit_message(chat_id, message_id, gmail_panel_text() + "\n\n⚠️ <b>Delete all stock?</b>", reply_markup=gmail_panel_kb(confirm_clear=True))
        answer_callback_query(query_id)
    elif data == "gm_clear_yes":
        bot_settings["gmail_stock"] = []
        save_local_data()
        edit_message(chat_id, message_id, gmail_panel_text(), reply_markup=gmail_panel_kb())
        answer_callback_query(query_id, "Stock cleared")

    # ===== DXA Control Panel =====
    elif data == "dxa_control":
        edit_message(chat_id, message_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
        answer_callback_query(query_id)
    elif data == "dxa_emoji":
        if user_id != ADMIN_ID: answer_callback_query(query_id, "Admin only", show_alert=True); return
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, emo_menu_text(), reply_markup=emo_menu_kb())
        answer_callback_query(query_id)
    elif data.startswith("emo_"):
        handle_emoji_callback(query_id, user_id, chat_id, message_id, data)
    elif data == "dxa_prem_toggle":
        bot_settings["premium_emoji"] = not bot_settings.get("premium_emoji", False)
        save_local_data()
        edit_message(chat_id, message_id, "⚙️ <b>DXA CONTROL PANEL</b>\nManage core bot configurations:", reply_markup=dxa_control_keyboard())
        answer_callback_query(query_id, "Premium icons: " + ("ON (needs Telegram Premium owner)" if bot_settings["premium_emoji"] else "OFF (normal emoji)"), show_alert=bot_settings["premium_emoji"])
    elif data == "dxa_min_w":
        user_states[user_id] = {"state": "waiting_dxa_min_w", "msg_id": message_id}
        edit_message(chat_id, message_id, "💸 <b>Enter new Minimum Withdraw Amount (e.g. 50):</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "dxa_ref_r":
        user_states[user_id] = {"state": "waiting_dxa_ref_r", "msg_id": message_id}
        edit_message(chat_id, message_id, "💸 <b>Enter new Referral Reward Amount (e.g. 5.0):</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "dxa_otp_control":
        edit_message(chat_id, message_id, "💰 <b>OTP REWARD CONTROL</b>\nManage rates below:", reply_markup=dxa_otp_control_keyboard())
        answer_callback_query(query_id)
    elif data == "dxa_def_rate":
        user_states[user_id] = {"state": "waiting_dxa_def_rate", "msg_id": message_id}
        edit_message(chat_id, message_id, "💰 <b>Enter new Default OTP Rate (e.g. 0.5):</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
        
    elif data.startswith("del_srv_rate_"):
        srv_name = data[13:]
        if srv_name in bot_settings["otp_service_rates"]:
            del bot_settings["otp_service_rates"][srv_name]
            save_local_data()
        edit_message(chat_id, message_id, "💰 <b>OTP REWARD CONTROL</b>\nManage rates below:", reply_markup=dxa_otp_control_keyboard())
        answer_callback_query(query_id, f"Rate for {srv_name} deleted!")
    elif data == "dxa_srv_rate":
        user_states[user_id] = {"state": "waiting_dxa_srv_rate", "msg_id": message_id}
        edit_message(chat_id, message_id, "💰 <b>Enter Specific Service Rate.</b>\nFormat: <code>ServiceName - Rate</code>\nExample: <code>Telegram - 2.5</code>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "dxa_cool":
        user_states[user_id] = {"state": "waiting_dxa_cool", "msg_id": message_id}
        edit_message(chat_id, message_id, "⏳ <b>Enter new Cooldown time in seconds (e.g. 10):</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "dxa_num_req":
        user_states[user_id] = {"state": "waiting_dxa_num_req", "msg_id": message_id}
        edit_message(chat_id, message_id, "📱 <b>Enter how many numbers a user gets per request (e.g. 3):</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data == "manage_w_methods":
        edit_message(chat_id, message_id, "💳 <b>WITHDRAW METHODS</b>\nManage methods below:", reply_markup=dxa_manage_w_methods_keyboard())
        answer_callback_query(query_id)
    elif data == "add_w_method":
        user_states[user_id] = {"state": "waiting_w_method", "msg_id": message_id}
        edit_message(chat_id, message_id, "💳 <b>Enter new Withdraw Method Name (e.g. bKash):</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
    elif data.startswith("del_w_method_"):
        idx = int(data[13:])
        if idx < len(bot_settings["w_methods"]): bot_settings["w_methods"].pop(idx)
        save_local_data()
        edit_message(chat_id, message_id, "💳 <b>WITHDRAW METHODS</b>\nManage methods below:", reply_markup=dxa_manage_w_methods_keyboard())
        answer_callback_query(query_id, "Method Deleted!")
    elif data == "dxa_w_group":
        user_states[user_id] = {"state": "waiting_dxa_w_group", "msg_id": message_id}
        select_kb = {"keyboard": [[{"text": "Select Withdraw Group", "request_chat": {"request_id": 2, "chat_is_channel": False}}], [{"text": "🔙 BACK"}]], "resize_keyboard": True, "one_time_keyboard": True}
        send_message(chat_id, "👇 <b>Select Withdraw Group from below:</b>", reply_markup=select_kb)
        answer_callback_query(query_id)

    # ===== Broadcast System =====
    elif data == "broadcast_menu":
        user_states[user_id] = {"state": "waiting_broadcast", "msg_id": message_id}
        edit_message(chat_id, message_id, "📣 <b>BROADCAST SYSTEM</b>\n\nSend any text, photo, video, or audio message you want to broadcast to all users.", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)

    elif data == "upload_firebase":
        user_states[user_id] = {"state": "waiting_firebase", "msg_id": message_id}
        edit_message(chat_id, message_id, "🔥 <b>FIREBASE SETUP</b>\n\nPlease reply with the <b>entire JSON content</b> of your Firebase Service Account file.", reply_markup=get_upload_firebase_keyboard())
        answer_callback_query(query_id, "Send JSON Now")

    # ===== User Control =====
    elif data == "user_control":
        msg = f"👥 <b>USER CONTROL</b>\n━━━━━━━━━━━━━━━━━\n📊 <b>Total Users:</b> {get_total_users()}\n\n👇 Manage users below:"
        edit_message(chat_id, message_id, msg, reply_markup=get_user_control_keyboard())
        answer_callback_query(query_id)
        
    elif data == "uc_profile":
        user_states[user_id] = {"state": "waiting_uc_id", "msg_id": message_id}
        edit_message(chat_id, message_id, "🆔 <b>Send the Telegram User ID to view profile:</b>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
        
    elif data.startswith("uc_balance_"):
        target_uid = int(data[11:])
        user_states[user_id] = {"state": f"waiting_uc_balance_{target_uid}", "msg_id": message_id}
        edit_message(chat_id, message_id, f"💵 <b>Send the amount to Add or Subtract.</b>\n<i>(Examples: To add use 50, to deduct use -10)</i>", reply_markup=get_back_only_keyboard())
        answer_callback_query(query_id)
        
    elif data == "back_to_admin":
        if user_id in user_states: del user_states[user_id]
        edit_message(chat_id, message_id, get_owner_panel_text(), reply_markup=get_admin_inline_keyboard())
        answer_callback_query(query_id)
        
    elif data == "db_status": 
        answer_callback_query(query_id, f"Current DB: {current_db_mode.upper()}", show_alert=True)
        
    elif data == "close_panel":
        answer_callback_query(query_id, "Closed")
        delete_message(chat_id, message_id)
        
    else:
        answer_callback_query(query_id, f"{data} clicked!", show_alert=True)

# ==========================================
# Hadi panel (CR API: /crapi/had/viewstats) - auto-added on start if missing
# Token can be overridden with env HADI_TOKEN
# ==========================================
HADI_URL = os.environ.get("HADI_URL", "http://147.135.212.197/crapi/had/viewstats")
HADI_TOKEN = os.environ.get("HADI_TOKEN", "QlNTRUFBUzRSdGl8ZZNkf0KSYkpcUlRGVWaHVXpTkUZhY21jioOQaw")

def seed_hadi_panel():
    """Add the Hadi CR-API panel once (OTP logs only; this API has no numbers endpoint)."""
    try:
        panels = smspro.setdefault("panels", [])
        logs = f"{HADI_URL}?records=200"          # max 200 records per call (API limit)
        ex = next((x for x in panels if str(x.get("name", "")).strip().lower() == "hadi"), None)
        if ex:
            if os.environ.get("HADI_TOKEN"): ex["key"] = HADI_TOKEN        # env wins only when it is set on purpose
            ex.setdefault("auth", "query"); ex.setdefault("qs", {})
        else:
            panels.append({"id": "hadi", "name": "Hadi", "base": HADI_URL, "key": HADI_TOKEN, "final_url": logs, "did_url": "",
                           "auth": "query", "logs_path": logs, "nums_path": None, "service": "", "auto_otp": True,
                           "auto_nums": False, "auto_range": False, "range_target": 500, "ranges": {}, "baseline": False,
                           "last_status": "added", "fails": 0, "otps": 0, "numbers_added": 0, "known": []})
        smspro_save()
    except Exception as e:
        print("seed_hadi_panel error:", e)

# ==========================================
# Main
# ==========================================
def main():
    global BOT_USERNAME
    
    load_local_data()
    smspro_load()
    seed_hadi_panel()
    stock_load()
    init_sqlite()
    init_firebase_from_file()
    
    try: requests.post(BASE_URL + "deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
    except Exception as e: print("deleteWebhook error:", e)
    BOT_USERNAME = get_bot_info()
    set_bot_commands()
    
    threading.Thread(target=voltx_console_listener, daemon=True).start()
    threading.Thread(target=console_group_sender, daemon=True).start()
    threading.Thread(target=voltx_sms_listener, daemon=True).start()
    threading.Thread(target=mino_sms_listener, daemon=True).start()
    threading.Thread(target=mino_traffic_worker, daemon=True).start()
    threading.Thread(target=smspro_sms_listener, daemon=True).start()
    threading.Thread(target=fwd_init, daemon=True).start()
    threading.Thread(target=xp_worker, daemon=True).start()
    threading.Thread(target=cp_worker, daemon=True).start()
    threading.Thread(target=auto_cache_menu_thread, daemon=True).start()
    threading.Thread(target=start_api_server, daemon=True).start()
    threading.Thread(target=stock_worker, daemon=True).start()
    threading.Thread(target=auto_range_worker, daemon=True).start()
    
    print(f"Bot @{BOT_USERNAME} is running with Stex SMS + SMS Pro, DXA Control & OTP Forwarding...")
    
    offset = None
    while True:
        try:
            url = BASE_URL + "getUpdates"
            res = requests.get(url, params={"timeout": 30, "offset": offset}, timeout=45).json()
            if not res.get("ok"):
                print("getUpdates failed:", res)
                time.sleep(3)
            if res.get("ok"):
                for update in res["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update: 
                        threading.Thread(target=handle_message, args=(update["message"],)).start()
                    elif "callback_query" in update: 
                        threading.Thread(target=handle_callback, args=(update["callback_query"],)).start()
        except Exception as e:
            print("Polling error:", e)
            time.sleep(3)

if __name__ == "__main__":
    main()
