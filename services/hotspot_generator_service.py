# -*- coding: utf-8 -*-
"""
MAX RADIUS - MikroTik Hotspot Dynamic Template & RouterOS Script Generator Service.
Generates customized Hotspot portal files and PCQ/Queue/Walled-Garden configuration scripts.
"""

import io
import zipfile
import logging
from database.db import query_all, query_one, execute_write

logger = logging.getLogger('hotspot_generator')


def get_hotspot_generator_settings(default_host=None):
    """
    Fetches the configured hotspot and portal generator settings from wisp_system_settings,
    with smart fallbacks based on runtime environment.
    """
    try:
        rows = query_all("SELECT `key`, `value` FROM wisp_system_settings")
        s = {r['key']: r['value'] for r in rows} if rows else {}
    except Exception as e:
        logger.warning("Error fetching hotspot generator settings: %s", e)
        s = {}

    server_host = s.get('hotspot_server_host') or s.get('system_server_ip') or default_host or '199.247.3.47'
    # Clean host if protocol included
    server_host = server_host.replace('http://', '').replace('https://', '').strip().rstrip('/')

    result = dict(s)
    result.update({
        'total_download': s.get('hotspot_total_download', '100M').strip(),
        'total_upload': s.get('hotspot_total_upload', '50M').strip(),
        'server_host': server_host,
        'hotspot_server_host': server_host,
        'folder_name': s.get('hotspot_folder_name', 'max-radius').strip() or 'max-radius',
        'hotspot_folder_name': s.get('hotspot_folder_name', 'max-radius').strip() or 'max-radius',
        'dns_name': s.get('hotspot_dns_name', 'm.net').strip() or 'm.net',
        'gateway_ip': s.get('hotspot_gateway_ip', '10.100.10.1').strip() or '10.100.10.1',
        'network_name': s.get('network_name', 'شبكة ماكس نت اللاسلكية').strip()
    })
    return result


def save_hotspot_generator_settings(data):
    """
    Saves hotspot generator & subscriber portal settings to wisp_system_settings.
    """
    # Key mapping for hotspot specific settings
    key_mapping = {
        'total_download': 'hotspot_total_download',
        'total_upload': 'hotspot_total_upload',
        'server_host': 'hotspot_server_host',
        'folder_name': 'hotspot_folder_name',
        'dns_name': 'hotspot_dns_name',
        'gateway_ip': 'hotspot_gateway_ip'
    }

    for input_key, val in data.items():
        if val is None:
            continue
        db_key = key_mapping.get(input_key, input_key)
        v = str(val).strip()
        if input_key in ['total_download', 'total_upload']:
            v = v.upper()
        elif input_key == 'server_host':
            v = v.replace('http://', '').replace('https://', '').rstrip('/')

        existing = query_one("SELECT `key` FROM wisp_system_settings WHERE `key` = ?", (db_key,))
        if existing:
            execute_write("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = ?", (v, db_key))
        else:
            execute_write("INSERT INTO wisp_system_settings (`key`, `value`) VALUES (?, ?)", (db_key, v))

    return True


def render_hotspot_template(filename, params):
    """
    Renders raw HTML hotspot templates with dynamic parameter substitution.
    """
    server_host = params.get('server_host', '199.247.3.47')
    base_url = f"http://{server_host}"

    if filename == 'login.html':
        return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>بوابة المشتركين | جاري التحويل...</title>
  <meta http-equiv="refresh" content="0; url={base_url}/user/login?loginlink=$(link-login-only)&dst=$(link-orig-esc)&mac=$(mac)&ip=$(ip)&error=$(error-esc)&username=$(username)&chap_id=$(chap-id)&chap_challenge=$(chap-challenge)">
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;700;900&display=swap');
    body {{ font-family: 'Tajawal', sans-serif; background-color: #070b14; color: #f1f5f9; }}
    .portal-card {{ background: #0b1220; border: 1px solid #1c2840; }}
  </style>
  <script>
    window.location.href = "{base_url}/user/login?loginlink=" + encodeURIComponent("$(link-login-only)") +
      "&dst=" + encodeURIComponent("$(link-orig-esc)") +
      "&mac=" + encodeURIComponent("$(mac)") +
      "&ip=" + encodeURIComponent("$(ip)") +
      "&error=" + encodeURIComponent("$(error)") +
      "&username=" + encodeURIComponent("$(username)") +
      "&chap_id=" + encodeURIComponent("$(chap-id)") +
      "&chap_challenge=" + encodeURIComponent("$(chap-challenge)");
  </script>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
  <div class="portal-card p-8 rounded-3xl max-w-sm w-full text-center space-y-4 shadow-2xl">
    <div class="w-16 h-16 rounded-2xl bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center justify-center text-2xl mx-auto animate-pulse">
      <i class="fa-solid fa-wifi"></i>
    </div>
    <h2 class="text-lg font-black text-white">جاري الاتصال بالشبكة...</h2>
    <p class="text-xs text-slate-400">يتم تحويلك الآن إلى صفحة تسجيل الدخول الذكية</p>
    
    <div class="pt-2">
      <a href="{base_url}/user/login?loginlink=$(link-login-only)&dst=$(link-orig-esc)&mac=$(mac)&ip=$(ip)&error=$(error-esc)&username=$(username)"
         class="inline-flex items-center justify-center gap-2 w-full py-3 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-black rounded-xl text-xs transition">
        <span>اضغط هنا إذا لم يتم التحويل تلقائياً</span>
        <i class="fa-solid fa-arrow-left"></i>
      </a>
    </div>
  </div>
</body>
</html>"""

    elif filename == 'status.html':
        return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>حالة الاتصال | لوحة المشترك</title>
  <meta http-equiv="refresh" content="2; url={base_url}/user/dashboard?username=$(username)&mac=$(mac)&ip=$(ip)&uptime=$(uptime)&bytes_in=$(bytes-in-nice)&bytes_out=$(bytes-out-nice)&logout_url=$(link-logout)">
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;700;900&display=swap');
    body {{ font-family: 'Tajawal', sans-serif; background-color: #070b14; color: #f1f5f9; }}
    .portal-card {{ background: #0b1220; border: 1px solid #1c2840; }}
  </style>
  <script>
    window.location.href = "{base_url}/user/dashboard?username=" + encodeURIComponent("$(username)") +
      "&mac=" + encodeURIComponent("$(mac)") +
      "&ip=" + encodeURIComponent("$(ip)") +
      "&uptime=" + encodeURIComponent("$(uptime)") +
      "&bytes_in=" + encodeURIComponent("$(bytes-in-nice)") +
      "&bytes_out=" + encodeURIComponent("$(bytes-out-nice)") +
      "&logout_url=" + encodeURIComponent("$(link-logout)");
  </script>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
  <div class="portal-card p-6 sm:p-8 rounded-3xl max-w-sm w-full text-center space-y-4 shadow-2xl">
    <div class="w-16 h-16 rounded-2xl bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center justify-center text-3xl mx-auto shadow-lg shadow-emerald-500/20">
      <i class="fa-solid fa-circle-check"></i>
    </div>
    <h2 class="text-lg font-black text-white">متصل بالإنترنت بنجاح</h2>
    <p class="text-xs text-slate-400">حساب المشترك: <strong class="text-emerald-400 font-mono">$(username)</strong></p>

    <!-- Session Stats summary -->
    <div class="p-3.5 rounded-2xl bg-[#070b14] border border-[#1e293b] text-xs text-slate-300 text-right space-y-1.5 font-mono">
      <div class="flex justify-between items-center">
        <span class="text-slate-400 font-sans">عنوان IP:</span>
        <span class="text-white font-bold">$(ip)</span>
      </div>
      <div class="flex justify-between items-center">
        <span class="text-slate-400 font-sans">مدة الاتصال:</span>
        <span class="text-sky-400 font-bold">$(uptime)</span>
      </div>
      <div class="flex justify-between items-center">
        <span class="text-slate-400 font-sans">الاستهلاك:</span>
        <span class="text-emerald-400 font-bold">$(bytes-total-nice)</span>
      </div>
    </div>

    <!-- Actions -->
    <div class="space-y-2 pt-2">
      <a href="{base_url}/user/dashboard?username=$(username)"
         class="inline-flex items-center justify-center gap-2 w-full py-3 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-black rounded-xl text-xs transition shadow-md shadow-emerald-500/20">
        <i class="fa-solid fa-gauge"></i>
        <span>فتح لوحة تحكم المشترك</span>
      </a>

      <form action="$(link-logout)" name="logout">
        <button type="submit" class="inline-flex items-center justify-center gap-1.5 w-full py-2 bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 font-bold rounded-xl border border-rose-500/30 text-xs transition">
          <i class="fa-solid fa-power-off"></i>
          <span>قطع الاتصال وتسجيل الخروج</span>
        </button>
      </form>
    </div>
  </div>
</body>
</html>"""

    elif filename == 'alogin.html':
        return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>تم تسجيل الدخول | MAX RADIUS</title>
  <meta http-equiv="refresh" content="1; url={base_url}/user/dashboard?username=$(username)&logged_in=1&logout_url=$(link-logout)&status_url=$(link-status)">
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;700;900&display=swap');
    body {{ font-family: 'Tajawal', sans-serif; background-color: #070b14; color: #f1f5f9; }}
    .portal-card {{ background: #0b1220; border: 1px solid #1c2840; }}
  </style>
  <script>
    setTimeout(function() {{
      window.location.href = "{base_url}/user/dashboard?username=" + encodeURIComponent("$(username)") + "&logged_in=1&logout_url=" + encodeURIComponent("$(link-logout)") + "&status_url=" + encodeURIComponent("$(link-status)");
    }}, 1000);
  </script>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
  <div class="portal-card p-8 rounded-3xl max-w-sm w-full text-center space-y-4 shadow-2xl">
    <div class="w-16 h-16 rounded-2xl bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center justify-center text-3xl mx-auto shadow-lg shadow-emerald-500/20 animate-bounce">
      <i class="fa-solid fa-circle-check"></i>
    </div>
    <h2 class="text-xl font-black text-white">تم الاتصال بنجاح!</h2>
    <p class="text-xs text-slate-400">مرحباً بك <strong class="text-emerald-400 font-mono">$(username)</strong>، جاري نقلك إلى لوحة التحكم...</p>
    
    <div class="pt-2">
      <a href="{base_url}/user/dashboard?username=$(username)&logged_in=1&logout_url=$(link-logout)&status_url=$(link-status)" 
         class="inline-block w-full py-3 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-black rounded-xl text-xs transition shadow-md shadow-emerald-500/20">
        فتح لوحة التحكم فوراً
      </a>
    </div>
  </div>
</body>
</html>"""

    elif filename == 'logout.html':
        return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>تم تسجيل الخروج | MAX RADIUS</title>
  <meta http-equiv="refresh" content="2; url={base_url}/user/login?logged_out=1&loginlink=$(link-login-only)&dst=$(link-orig-esc)&mac=$(mac)&ip=$(ip)&username=$(username)">
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;700;900&display=swap');
    body {{ font-family: 'Tajawal', sans-serif; background-color: #070b14; color: #f1f5f9; }}
    .portal-card {{ background: #0b1220; border: 1px solid #1c2840; }}
  </style>
  <script>
    setTimeout(function() {{
      window.location.href = "{base_url}/user/login?logged_out=1&loginlink=" + encodeURIComponent("$(link-login-only)") +
        "&dst=" + encodeURIComponent("$(link-orig-esc)") +
        "&mac=" + encodeURIComponent("$(mac)") +
        "&ip=" + encodeURIComponent("$(ip)") +
        "&username=" + encodeURIComponent("$(username)");
    }}, 2000);
  </script>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
  <div class="portal-card p-8 rounded-3xl max-w-sm w-full text-center space-y-4 shadow-2xl">
    <div class="w-16 h-16 rounded-2xl bg-amber-500/20 text-amber-400 border border-amber-500/30 flex items-center justify-center text-3xl mx-auto shadow-lg shadow-amber-500/20">
      <i class="fa-solid fa-lock"></i>
    </div>
    <h2 class="text-xl font-black text-white">تم تسجيل الخروج</h2>
    <p class="text-xs text-slate-400 leading-relaxed">
      تم إنهاء جلسة الاتصال بالإنترنت بنجاح للحفاظ على رصيدك.
    </p>
    
    <div class="p-3.5 rounded-2xl bg-[#070b14] border border-[#1e293b] text-xs text-slate-300 text-right space-y-1.5 font-mono">
      <div class="flex justify-between items-center"><span class="text-slate-400 font-sans">الحساب:</span> <span class="text-white font-bold">$(username)</span></div>
      <div class="flex justify-between items-center"><span class="text-slate-400 font-sans">مدة الاتصال:</span> <span class="text-sky-400 font-bold">$(uptime)</span></div>
      <div class="flex justify-between items-center"><span class="text-slate-400 font-sans">البيانات المستهلكة:</span> <span class="text-emerald-400 font-bold">$(bytes-total-nice)</span></div>
    </div>

    <div class="pt-2">
      <a href="{base_url}/user/login?logged_out=1&loginlink=$(link-login-only)&dst=$(link-orig-esc)&mac=$(mac)&ip=$(ip)&username=$(username)" class="inline-flex items-center justify-center gap-2 w-full py-3 bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-black rounded-xl text-xs transition shadow-md shadow-emerald-500/20">
        <i class="fa-solid fa-right-to-bracket"></i>
        <span>تسجيل الدخول مجدداً</span>
      </a>
    </div>
  </div>
</body>
</html>"""

    elif filename == 'redirect.html':
        return """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>إعادة توجيه...</title>
    <meta http-equiv="refresh" content="0; url=$(link-redirect)">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="expires" content="-1">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: #0b1329;
            color: #94a3b8;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
        }
        .loader-card {
            text-align: center;
            background: #111a36;
            padding: 30px;
            border-radius: 16px;
            border: 1px solid #1e293b;
        }
        .spinner {
            width: 40px;
            height: 40px;
            border: 3px solid rgba(59, 130, 246, 0.2);
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 15px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        a { color: #38bdf8; text-decoration: none; font-size: 14px; }
    </style>
    <script>
        var targetUrl = "$(link-redirect)";
        if (targetUrl && targetUrl.length > 0 && targetUrl !== "$(link-redirect)") {
            window.location.replace(targetUrl);
        }
    </script>
</head>
<body>
    <div class="loader-card">
        <div class="spinner"></div>
        <p>جاري تحويلك تلقائياً...</p>
        <p><a href="$(link-redirect)">اضغط هنا إذا لم يتم التحويل تلقائياً</a></p>
    </div>
    <script>
        setTimeout(function() {
            window.location.href = "$(link-redirect)";
        }, 300);
    </script>
</body>
</html>"""

    elif filename == 'rlogin.html':
        return """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تسجيل الدخول...</title>
    <meta http-equiv="refresh" content="0; url=$(link-login-only)?dst=$(link-orig-esc)">
    <meta http-equiv="pragma" content="no-cache">
    <meta http-equiv="expires" content="-1">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: #0b1329;
            color: #94a3b8;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
        }
        .loader-card {
            text-align: center;
            background: #111a36;
            padding: 30px;
            border-radius: 16px;
            border: 1px solid #1e293b;
        }
        .spinner {
            width: 40px;
            height: 40px;
            border: 3px solid rgba(59, 130, 246, 0.2);
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 15px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        a { color: #38bdf8; text-decoration: none; font-size: 14px; }
    </style>
    <script>
        var targetUrl = "$(link-login-only)?dst=$(link-orig-esc)";
        if (targetUrl && targetUrl.indexOf("$") === -1) {
            window.location.replace(targetUrl);
        }
    </script>
</head>
<body>
    <div class="loader-card">
        <div class="spinner"></div>
        <p>جاري تحويلك لصفحة تسجيل الدخول...</p>
        <p><a href="$(link-login-only)?dst=$(link-orig-esc)">اضغط هنا إذا لم يتم التحويل تلقائياً</a></p>
    </div>
    <script>
        setTimeout(function() {
            window.location.href = "$(link-login-only)?dst=$(link-orig-esc)";
        }, 300);
    </script>
</body>
</html>"""

    elif filename == 'radadvert.html':
        return """<!DOCTYPE html>
<html>
<head>
<title>...</title>
<meta http-equiv="refresh" content="2; url=$(link-orig)">
</head>
<body>
$(advert-content)
</body>
</html>"""

    elif filename == 'md5.js':
        # Standard lightweight routeros md5 helper
        return """/*
 * A JavaScript implementation of the RSA Data Security, Inc. MD5 Message
 * Digest Algorithm, as defined in RFC 1321.
 * Version 2.1 Copyright (C) Paul Johnston 1999 - 2002.
 */
var hexcase = 0;
var b64pad  = "";
var chrsz   = 8;
function hex_md5(s){ return binl2hex(core_md5(str2binl(s), s.length * chrsz));}
function b64_md5(s){ return binl2b64(core_md5(str2binl(s), s.length * chrsz));}
function str_md5(s){ return binl2str(core_md5(str2binl(s), s.length * chrsz));}
function hex_hmac_md5(key, data) { return binl2hex(core_hmac_md5(key, data)); }
function b64_hmac_md5(key, data) { return binl2b64(core_hmac_md5(key, data)); }
function str_hmac_md5(key, data) { return binl2str(core_hmac_md5(key, data)); }
function core_md5(x, len)
{
  x[len >> 5] |= 0x80 << ((len) % 32);
  x[(((len + 64) >>> 9) << 4) + 14] = len;
  var a =  1732584193;
  var b = -271733879;
  var c = -1732584194;
  var d =  271733878;
  for(var i = 0; i < x.length; i += 16)
  {
    var olda = a;
    var oldb = b;
    var oldc = c;
    var oldd = d;
    a = md5_ff(a, b, c, d, x[i+ 0], 7 , -680876936);
    d = md5_ff(d, a, b, c, x[i+ 1], 12, -389564586);
    c = md5_ff(c, d, a, b, x[i+ 2], 17,  606105819);
    b = md5_ff(b, c, d, a, x[i+ 3], 22, -1044525330);
    a = md5_ff(a, b, c, d, x[i+ 4], 7 , -176418897);
    d = md5_ff(d, a, b, c, x[i+ 5], 12,  1200080426);
    c = md5_ff(c, d, a, b, x[i+ 6], 17, -1473231341);
    b = md5_ff(b, c, d, a, x[i+ 7], 22, -45705983);
    a = md5_ff(a, b, c, d, x[i+ 8], 7 ,  1770035416);
    d = md5_ff(d, a, b, c, x[i+ 9], 12, -1958414417);
    c = md5_ff(c, d, a, b, x[i+10], 17, -42063);
    b = md5_ff(b, c, d, a, x[i+11], 22, -1990404162);
    a = md5_ff(a, b, c, d, x[i+12], 7 ,  1804603682);
    d = md5_ff(d, a, b, c, x[i+13], 12, -40341101);
    c = md5_ff(c, d, a, b, x[i+14], 17, -1502002290);
    b = md5_ff(b, c, d, a, x[i+15], 22,  1236535329);
    a = md5_gg(a, b, c, d, x[i+ 1], 5 , -165796510);
    d = md5_gg(d, a, b, c, x[i+ 6], 9 , -1069501632);
    c = md5_gg(c, d, a, b, x[i+11], 14,  643717713);
    b = md5_gg(b, c, d, a, x[i+ 0], 20, -373897302);
    a = md5_gg(a, b, c, d, x[i+ 5], 5 , -701558691);
    d = md5_gg(d, a, b, c, x[i+10], 9 ,  38016083);
    c = md5_gg(c, d, a, b, x[i+15], 14, -660478335);
    b = md5_gg(b, c, d, a, x[i+ 4], 20, -405537848);
    a = md5_gg(a, b, c, d, x[i+ 9], 5 ,  568446438);
    d = md5_gg(d, a, b, c, x[i+14], 9 , -1019803690);
    c = md5_gg(c, d, a, b, x[i+ 3], 14, -187363961);
    b = md5_gg(b, c, d, a, x[i+ 8], 20,  1163531501);
    a = md5_gg(a, b, c, d, x[i+13], 5 , -1444681467);
    d = md5_gg(d, a, b, c, x[i+ 2], 9 , -51403784);
    c = md5_gg(c, d, a, b, x[i+ 7], 14,  1735328473);
    b = md5_gg(b, c, d, a, x[i+12], 20, -1926607734);
    a = md5_hh(a, b, c, d, x[i+ 5], 4 , -378558);
    d = md5_hh(d, a, b, c, x[i+ 8], 11, -2022574463);
    c = md5_hh(c, d, a, b, x[i+11], 16,  1839030562);
    b = md5_hh(b, c, d, a, x[i+14], 23, -35309556);
    a = md5_hh(a, b, c, d, x[i+ 1], 4 , -1530992060);
    d = md5_hh(d, a, b, c, x[i+ 4], 11,  1272893353);
    c = md5_hh(c, d, a, b, x[i+ 7], 16, -155497632);
    b = md5_hh(b, c, d, a, x[i+10], 23, -1094730640);
    a = md5_hh(a, b, c, d, x[i+13], 4 ,  681279174);
    d = md5_hh(d, a, b, c, x[i+ 0], 11, -358537222);
    c = md5_hh(c, d, a, b, x[i+ 3], 16, -722521979);
    b = md5_hh(b, c, d, a, x[i+ 6], 23,  76029189);
    a = md5_hh(a, b, c, d, x[i+ 9], 4 , -640364487);
    d = md5_hh(d, a, b, c, x[i+12], 11, -421815835);
    c = md5_hh(c, d, a, b, x[i+15], 16,  530742520);
    b = md5_hh(b, c, d, a, x[i+ 2], 23, -995338651);
    a = md5_ii(a, b, c, d, x[i+ 0], 6 , -198630844);
    d = md5_ii(d, a, b, c, x[i+ 7], 10,  1126891415);
    c = md5_ii(c, d, a, b, x[i+14], 15, -1416354905);
    b = md5_ii(b, c, d, a, x[i+ 5], 21, -57434055);
    a = md5_ii(a, b, c, d, x[i+12], 6 ,  1700485571);
    d = md5_ii(d, a, b, c, x[i+ 3], 10, -1894986606);
    c = md5_ii(c, d, a, b, x[i+10], 15, -1051523);
    b = md5_ii(b, c, d, a, x[i+ 1], 21, -2054922799);
    a = md5_ii(a, b, c, d, x[i+ 8], 6 ,  1873313359);
    d = md5_ii(d, a, b, c, x[i+15], 10, -30611744);
    c = md5_ii(c, d, a, b, x[i+ 6], 15, -1560198380);
    b = md5_ii(b, c, d, a, x[i+13], 21,  1309151649);
    a = md5_ii(a, b, c, d, x[i+ 4], 6 , -145523070);
    d = md5_ii(d, a, b, c, x[i+11], 10, -1120210379);
    c = md5_ii(c, d, a, b, x[i+ 2], 15,  718787259);
    b = md5_ii(b, c, d, a, x[i+ 9], 21, -343485551);
    a = safe_add(a, olda);
    b = safe_add(b, oldb);
    c = safe_add(c, oldc);
    d = safe_add(d, oldd);
  }
  return Array(a, b, c, d);
}
function md5_cmn(q, a, b, x, s, t) { return safe_add(bit_rol(safe_add(safe_add(a, q), safe_add(x, t)), s),b); }
function md5_ff(a, b, c, d, x, s, t) { return md5_cmn((b & c) | ((~b) & d), a, b, x, s, t); }
function md5_gg(a, b, c, d, x, s, t) { return md5_cmn((b & d) | (c & (~d)), a, b, x, s, t); }
function md5_hh(a, b, c, d, x, s, t) { return md5_cmn(b ^ c ^ d, a, b, x, s, t); }
function md5_ii(a, b, c, d, x, s, t) { return md5_cmn(c ^ (b | (~d)), a, b, x, s, t); }
function safe_add(x, y) { var lsw = (x & 0xFFFF) + (y & 0xFFFF); var msw = (x >> 16) + (y >> 16) + (lsw >> 16); return (msw << 16) | (lsw & 0xFFFF); }
function bit_rol(num, cnt) { return (num << cnt) | (num >>> (32 - cnt)); }
function str2binl(str) { var bin = Array(); var mask = (1 << chrsz) - 1; for(var i = 0; i < str.length * chrsz; i += chrsz) bin[i>>5] |= (str.charCodeAt(i / chrsz) & mask) << (i%32); return bin; }
function binl2hex(binarray) { var hex_tab = hexcase ? "0123456789ABCDEF" : "0123456789abcdef"; var str = ""; for(var i = 0; i < binarray.length * 4; i++) { str += hex_tab.charAt((binarray[i>>2] >> ((i%4)*8+4)) & 0xF) + hex_tab.charAt((binarray[i>>2] >> ((i%4)*8  )) & 0xF); } return str; }
"""

    return ""


def generate_hotspot_zip_bytes(params):
    """
    Creates an in-memory zip archive containing all hotspot portal files,
    packaged cleanly inside the specified folder (e.g. max-radius/).
    """
    folder_name = params.get('folder_name', 'max-radius').strip() or 'max-radius'
    files_to_pack = [
        'login.html',
        'status.html',
        'alogin.html',
        'logout.html',
        'redirect.html',
        'rlogin.html',
        'radadvert.html',
        'md5.js'
    ]

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for fname in files_to_pack:
            content = render_hotspot_template(fname, params)
            # Store inside the subfolder so unzipping creates the exact folder
            zip_path = f"{folder_name}/{fname}"
            zip_file.writestr(zip_path, content.encode('utf-8'))

    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def generate_mikrotik_hotspot_script(params):
    """
    Generates a production-ready MikroTik RouterOS setup script.
    Configures PCQ Queue Types, Parent Bandwidth Queue, Hotspot User Profile,
    HTML Directory, and Walled Garden Rules.
    """
    total_down = params.get('total_download', '100M').strip()
    total_up = params.get('total_upload', '50M').strip()
    server_host = params.get('server_host', '199.247.3.47').strip()
    folder_name = params.get('folder_name', 'max-radius').strip() or 'max-radius'
    network_name = params.get('network_name', 'شبكة ماكس نت اللاسلكية').strip()

    # Extract host / IP for walled garden
    server_ip = server_host.split(':')[0]

    script = f"""# ==============================================================================
# MAX RADIUS - MikroTik Hotspot & Dynamic PCQ Bandwidth Configuration Script
# Network: {network_name}
# Server Address: {server_host}
# Hotspot Directory: {folder_name}
# ==============================================================================

:log info ">>> Starting MAX RADIUS Hotspot & Bandwidth Queue Setup..."

# ------------------------------------------------------------------------------
# 1. CREATE DYNAMIC PCQ QUEUE TYPES (Fair-Share Bandwidth & Low-Latency Engine)
# ------------------------------------------------------------------------------
/queue type
:if ([:len [find name="MAX_PCQ_DOWNLOAD"]] = 0) do={{
    add name="MAX_PCQ_DOWNLOAD" kind=pcq pcq-classifier=dst-address pcq-total-limit=2000KiB comment="MAX RADIUS Fair-Share Download PCQ"
}} else={{
    set [find name="MAX_PCQ_DOWNLOAD"] kind=pcq pcq-classifier=dst-address pcq-total-limit=2000KiB
}}

:if ([:len [find name="MAX_PCQ_UPLOAD"]] = 0) do={{
    add name="MAX_PCQ_UPLOAD" kind=pcq pcq-classifier=src-address pcq-total-limit=2000KiB comment="MAX RADIUS Fair-Share Upload PCQ"
}} else={{
    set [find name="MAX_PCQ_UPLOAD"] kind=pcq pcq-classifier=src-address pcq-total-limit=2000KiB
}}

# ------------------------------------------------------------------------------
# 2. CREATE MASTER PARENT QUEUE (Total Network Bandwidth Control: {total_up}/{total_down})
# ------------------------------------------------------------------------------
/queue simple
:if ([:len [find name="MAX_HOTSPOT_PARENT"]] = 0) do={{
    add name="MAX_HOTSPOT_PARENT" target="" max-limit={total_up}/{total_down} queue=MAX_PCQ_UPLOAD/MAX_PCQ_DOWNLOAD comment="MAX RADIUS Hotspot Master Bandwidth Control"
}} else={{
    set [find name="MAX_HOTSPOT_PARENT"] max-limit={total_up}/{total_down} queue=MAX_PCQ_UPLOAD/MAX_PCQ_DOWNLOAD
}}

# ------------------------------------------------------------------------------
# 3. CONFIGURE HOTSPOT USER PROFILE (Attach to Parent Queue & QoS Settings)
# ------------------------------------------------------------------------------
/ip hotspot user profile
set [find default=yes] parent-queue=MAX_HOTSPOT_PARENT queue-type=default keepalive-timeout=2m status-autorefresh=1m shared-users=1 transparent-proxy=no

# ------------------------------------------------------------------------------
# 4. CONFIGURE HOTSPOT SERVER PROFILE (Set Directory to {folder_name} & HTTP Mode)
# ------------------------------------------------------------------------------
/ip hotspot profile
set [find default=yes] html-directory={folder_name} ssl-certificate=none

# ------------------------------------------------------------------------------
# 5. CONFIGURE WALLED GARDEN (Pre-Auth Access for Server Portal, CDN & Fonts)
# ------------------------------------------------------------------------------
/ip hotspot walled-garden ip
:if ([:len [find dst-address="{server_ip}"]] = 0) do={{
    add dst-address={server_ip} action=accept comment="MAX RADIUS Server Portal"
}}

/ip hotspot walled-garden
:if ([:len [find dst-host="*cloudflare.com"]] = 0) do={{
    add dst-host=*cloudflare.com action=accept comment="Cloudflare CDN & Fonts"
}}
:if ([:len [find dst-host="*googleapis.com"]] = 0) do={{
    add dst-host=*googleapis.com action=accept comment="Google Fonts API"
}}
:if ([:len [find dst-host="*gstatic.com"]] = 0) do={{
    add dst-host=*gstatic.com action=accept comment="Google Static Assets"
}}
:if ([:len [find dst-host="*fontawesome.com"]] = 0) do={{
    add dst-host=*fontawesome.com action=accept comment="FontAwesome Icons CDN"
}}

:log info ">>> MAX RADIUS Hotspot Setup Completed Successfully!"
# ==============================================================================
"""
    return script
