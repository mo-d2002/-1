# -*- coding: utf-8 -*-
"""
短视频去水印解析 —— 自动适配依赖版
首次运行自动安装 flask、requests
运行：python app.py
"""

import sys
import subprocess
import importlib


# ============================================================
# 依赖自动检测与安装
# ============================================================
def _ensure_deps(packages):
    missing = []
    for pkg in packages:
        name = pkg.split("==")[0].split(">=")[0].split("<")[0].strip()
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)

    if not missing:
        return

    print("\n" + "=" * 64)
    print("  检测到缺少依赖：" + "、".join(missing))
    print("  正在自动安装，请稍候…")
    print("=" * 64)

    mirrors = [
        ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        ["-i", "https://mirrors.aliyun.com/pypi/simple/"],
        [],
    ]

    installed = False
    for i, mirror_args in enumerate(mirrors, 1):
        label = ["清华源", "阿里源", "官方源"][i - 1]
        cmd = [sys.executable, "-m", "pip", "install",
               "--quiet", "--disable-pip-version-check",
               "--no-warn-script-location"]
        cmd += mirror_args + missing

        print("  [" + str(i) + "/3] 尝试 " + label + " …")
        try:
            r = subprocess.run(cmd, timeout=300)
            if r.returncode == 0:
                installed = True
                break
        except subprocess.TimeoutExpired:
            print("       超时，换下一个源")
        except Exception as e:
            print("       失败：" + str(e))

    if not installed:
        print("\n" + "=" * 64)
        print("  [错误] 自动安装失败，请手动执行：")
        print()
        print('     "' + sys.executable + '" -m pip install ' + " ".join(missing))
        print("=" * 64 + "\n")
        sys.exit(1)

    importlib.invalidate_caches()
    print("\n  [√] 依赖安装完成，继续启动…\n")


_ensure_deps(["flask", "requests"])


# ============================================================
# 应用主代码
# ============================================================
import os
import re
import json as _json
import socket
import shutil
import threading
import time
import traceback
import webbrowser
from urllib.parse import quote

import flask
import requests
from flask import Flask, request, jsonify, Response, stream_with_context

app = Flask(__name__)

BASE_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))

IN_DOCKER = os.environ.get("IN_DOCKER") == "1"

if getattr(sys, "frozen", False):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    except Exception:
        pass

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
PROXY_HEADERS = dict(HEADERS, Referer="")

PUBLIC_URL = {"url": "", "status": "off", "error": ""}

KNOWN_HOSTS = ("v.douyin.com", "www.douyin.com", "www.iesdouyin.com",
               "h5.pipix.com", "pipix.com",
               "v.kuaishou.com", "www.kuaishou.com", "chenzhongtech.com")


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
    resp.headers["Access-Control-Expose-Headers"] = \
        "Content-Disposition, Content-Length, Content-Range, Accept-Ranges"
    return resp


def normalize_url(text):
    if not text:
        raise ValueError("链接为空")
    text = str(text).strip()
    m = re.search(
        r'https?://[^\s\u4e00-\u9fff，。！？；：、"\'<>【】《》（）()\[\]]+',
        text, re.I)
    if m:
        url = m.group(0).rstrip('.,;:!?/')
        return url if url.endswith("/") else url + "/"
    for host in KNOWN_HOSTS:
        p = (r'(?:(?:https?:)?//)?' + re.escape(host) +
             r'[^\s\u4e00-\u9fff，。！？；：、"\'<>【】《》（）()\[\]]*')
        m = re.search(p, text, re.I)
        if m:
            url = m.group(0)
            if not url.lower().startswith("http"):
                url = "https://" + url.lstrip("/")
            url = url.rstrip('.,;:!?/')
            return url if url.endswith("/") else url + "/"
    raise ValueError("无法识别链接，请粘贴完整分享地址")


def safe_json(resp, tip="接口"):
    text = resp.text or ""
    if not text.strip():
        raise RuntimeError(tip + " 返回空内容 HTTP " + str(resp.status_code))
    if text.lstrip().startswith("<"):
        raise RuntimeError(tip + " 返回网页而非 JSON，可能被风控")
    try:
        return _json.loads(text)
    except _json.JSONDecodeError as e:
        raise RuntimeError(tip + " JSON 解析失败: " + str(e))


def get_real_url(short_url, referer=""):
    short_url = normalize_url(short_url)
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(short_url, headers=headers,
                         allow_redirects=True, timeout=10)
        return r.url
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError("网络连接失败：" + str(e))


def detect_platform(url):
    u = url.lower()
    if "pipix.com" in u or "pipixia" in u:
        return "pipixia"
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "douyin"
    if "kuaishou.com" in u or "chenzhongtech.com" in u:
        return "kuaishou"
    return "unknown"


def parse_pipixia(share_url):
    real = get_real_url(share_url, "https://h5.pipix.com/")
    m = re.search(r"/item/(\d+)", real) or re.search(r"item_id=(\d+)", real)
    if not m:
        raise ValueError("无法提取 item_id")
    api = ("https://h5.pipix.com/bds/webapi/item/detail/?item_id="
           + m.group(1))
    r = requests.get(api, headers=dict(HEADERS,
                     Referer="https://h5.pipix.com/"), timeout=10)
    data = safe_json(r, "皮皮虾")
    item = data["data"]["item"]
    ov = item.get("origin_video_download", {}) or {}
    ul = ov.get("url_list", []) or []
    return {"platform": "皮皮虾",
            "title": item.get("title", "") or item.get("content", ""),
            "cover": item.get("cover_image", {}).get("url", ""),
            "video_url": ul[0]["url"] if ul else ""}


def parse_douyin(share_url):
    api = "https://video.zacao.top/api/parse"
    key = os.environ.get("DOUYIN_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    try:
        r = requests.post(api, json={"text": share_url},
                          headers=headers, timeout=30)
        data = safe_json(r, "抖音")
        if data.get("succ"):
            d = data["data"]
            return {"platform": "抖音", "title": d.get("title", ""),
                    "cover": d.get("cover", ""),
                    "video_url": d.get("video_url", "")}
    except Exception as e:
        print("[抖音] API 失败:", e)
    raise RuntimeError(
        "抖音解析失败。\n"
        "原因：抖音已升级反爬，需要第三方 API。\n"
        "解决：设置环境变量 DOUYIN_API_KEY=你的Key")


def parse_kuaishou(share_url):
    real = get_real_url(share_url, "https://v.kuaishou.com/")
    r = requests.get(real, headers=dict(HEADERS,
                     Referer="https://v.kuaishou.com/"), timeout=10)
    html = r.text
    title = ""
    m = re.search(r'"caption":"(.*?)"', html)
    if m:
        title = m.group(1)
    vurl = ""
    m = re.search(r'"srcNoMark":"(.*?)"', html)
    if m:
        vurl = m.group(1).replace("\\u002F", "/")
    else:
        m = re.search(r'"photoUrl":"(.*?)"', html)
        if m:
            vurl = m.group(1).replace("\\u002F", "/")
    if not vurl:
        raise RuntimeError("快手未找到视频地址")
    return {"platform": "快手", "title": title, "cover": "",
            "video_url": vurl}


PARSERS = {"pipixia": parse_pipixia, "douyin": parse_douyin,
           "kuaishou": parse_kuaishou}


def parse_video(share_url):
    share_url = normalize_url(share_url)
    p = detect_platform(share_url)
    if p == "unknown":
        raise ValueError("暂不支持该平台，目前仅支持：皮皮虾、抖音、快手")
    return PARSERS[p](share_url)


def _find_cloudflared():
    p = shutil.which("cloudflared")
    if p:
        return p
    for name in ("cloudflared.exe", "cloudflared"):
        c = os.path.join(BASE_DIR, name)
        if os.path.exists(c):
            return c
    return None


def start_cloudflared(port):
    exe = _find_cloudflared()
    if not exe:
        PUBLIC_URL["status"] = "missing"
        PUBLIC_URL["error"] = "未找到 cloudflared"
        return
    PUBLIC_URL["status"] = "starting"
    print("  [*] 启动 Cloudflare 隧道…")
    cmd = [exe, "tunnel", "--url", "http://localhost:" + str(port),
           "--protocol", "http2", "--no-autoupdate"]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="ignore",
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0))
    except Exception as e:
        PUBLIC_URL["status"] = "error"
        PUBLIC_URL["error"] = str(e)
        return
    found = threading.Event()

    def _reader():
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            if not found.is_set():
                m = re.search(
                    r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
                if m:
                    PUBLIC_URL["url"] = m.group(0)
                    PUBLIC_URL["status"] = "running"
                    print("\n  [√] 公网地址: " + m.group(0) + "\n")
                    found.set()

    threading.Thread(target=_reader, daemon=True).start()
    found.wait(timeout=25)


@app.route("/")
def index():
    return Response(HTML_PAGE, content_type="text/html; charset=utf-8")


@app.route("/api/ping")
def api_ping():
    return jsonify({"code": 0, "msg": "pong", "host": request.host,
                    "docker": IN_DOCKER})


@app.route("/api/public-url")
def api_public_url():
    return jsonify({"code": 0, "data": dict(PUBLIC_URL)})


@app.route("/api/parse", methods=["POST", "OPTIONS"])
def api_parse():
    if request.method == "OPTIONS":
        return Response("", 204)
    try:
        body = request.get_json(silent=True) or {}
        raw = (body.get("url") or "").strip()
        if not raw:
            return jsonify({"code": 1, "msg": "请输入分享链接"}), 400
        cleaned = normalize_url(raw)
        result = parse_video(cleaned)
        if not result.get("video_url"):
            return jsonify({"code": 1, "msg": "未找到视频地址"}), 500
        result["input_url"] = cleaned
        return jsonify({"code": 0, "data": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 1, "msg": str(e) or "未知错误"}), 500


@app.route("/api/proxy")
def api_proxy():
    url = request.args.get("url", "")
    if not url.startswith("http"):
        return jsonify({"code": 1, "msg": "url 非法"}), 400
    try:
        proxy_h = dict(PROXY_HEADERS)
        if request.headers.get("Range"):
            proxy_h["Range"] = request.headers["Range"]

        r = requests.get(url, headers=proxy_h, stream=True,
                         timeout=(10, 60), allow_redirects=True)
        ct = r.headers.get("Content-Type", "video/mp4")
        cl = r.headers.get("Content-Length")
        cr = r.headers.get("Content-Range")
        ar = r.headers.get("Accept-Ranges", "bytes")

        def gen():
            for c in r.iter_content(chunk_size=512 * 1024):
                if c:
                    yield c

        headers = {"Content-Type": ct, "Accept-Ranges": ar,
                   "Cache-Control": "public, max-age=3600"}
        if cl:
            headers["Content-Length"] = cl
        if cr:
            headers["Content-Range"] = cr

        return Response(stream_with_context(gen()),
                        status=r.status_code, headers=headers)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 1, "msg": str(e)}), 500


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0f0f13">
<title>短视频去水印解析</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#0f0f13;color:#e8e8ed;min-height:100vh;display:flex;
justify-content:center;padding:20px 16px 40px;
padding-top:calc(20px + env(safe-area-inset-top))}
.container{width:100%;max-width:680px}
h1{font-size:22px;margin-bottom:6px}
.sub{color:#8888a0;font-size:13px;margin-bottom:16px}
.row{display:flex;gap:8px;flex-direction:column}
@media(min-width:500px){.row{flex-direction:row}}
input{flex:1;padding:14px 16px;border-radius:10px;border:1px solid #2a2a38;
background:#1a1a24;color:#e8e8ed;font-size:16px;outline:none;-webkit-appearance:none}
input:focus{border-color:#6c5ce7}
button{padding:14px 24px;border:none;border-radius:10px;background:#6c5ce7;
color:#fff;font-size:16px;font-weight:600;cursor:pointer;white-space:nowrap;
-webkit-appearance:none}
button:active{background:#5b4cdb}
button:disabled{opacity:.5;cursor:not-allowed}
.status{margin-top:14px;font-size:14px;white-space:pre-wrap;min-height:20px;
line-height:1.6}
.status.error{color:#ff6b6b}
.status.ok{color:#51cf66}
.status.info{color:#74c0fc}
.result{margin-top:20px;background:#1a1a24;border-radius:12px;padding:16px;
display:none;border:1px solid #2a2a38}
.result.show{display:block}
.badge{font-size:12px;padding:3px 10px;border-radius:6px;background:#6c5ce7;
color:#fff;display:inline-block;margin-bottom:10px}
h2{font-size:15px;margin-bottom:12px;line-height:1.4;word-break:break-word}
video{width:100%;border-radius:10px;background:#000;max-height:70vh}
.hint{margin-top:12px;font-size:12px;color:#8888a0;line-height:1.6;
padding:10px;background:#0f0f13;border-radius:8px}
.tip{margin-top:14px;padding:10px 12px;background:#1a1a24;
border:1px solid #2a2a38;border-radius:8px;font-size:12px;
color:#8888a0;line-height:1.6}
.tip b{color:#74c0fc}
</style>
</head>
<body>
<div class="container">
  <h1>🎬 短视频去水印解析</h1>
  <p class="sub">粘贴链接，一键获取无水印视频</p>

  <div class="row">
    <input type="text" id="urlInput" placeholder="粘贴链接或整段分享文字"
           autocapitalize="off" autocorrect="off" spellcheck="false">
    <button id="parseBtn">解析</button>
  </div>
  <div class="status" id="status"></div>

  <div class="result" id="result">
    <span class="badge" id="platformBadge">平台</span>
    <h2 id="videoTitle"></h2>
    <video id="videoPlayer" controls playsinline webkit-playsinline
           preload="metadata" x5-playsinline></video>
    <div class="hint">
      💾 保存视频：手机长按视频 → 保存到相册；电脑右键 → 视频另存为
    </div>
  </div>

  <div class="tip">
    💡 <b>添加到桌面</b>：浏览器菜单 → 「添加到主屏幕」，像 App 一样使用
  </div>
</div>

<script>
if(location.protocol==='file:'){
  document.body.innerHTML='<div style="max-width:600px;margin:80px auto;'+
  'padding:24px;background:#1a1a24;border-radius:12px;line-height:1.8;">'+
  '<h2 style="color:#ff6b6b;">请通过服务器访问</h2>'+
  '<p>请先运行 python app.py，再通过显示的地址访问。</p></div>';
  throw new Error('file:// 访问');
}

var urlInput = document.getElementById('urlInput');
var parseBtn = document.getElementById('parseBtn');
var statusEl = document.getElementById('status');
var resultEl = document.getElementById('result');
var platformBadge = document.getElementById('platformBadge');
var videoTitle = document.getElementById('videoTitle');
var videoPlayer = document.getElementById('videoPlayer');

function setStatus(m, t) {
  statusEl.textContent = m;
  statusEl.className = 'status' + (t ? ' ' + t : '');
}

async function parseVideo() {
  var url = urlInput.value.trim();
  if (!url) { setStatus('请输入分享链接', 'error'); return; }
  parseBtn.disabled = true;
  setStatus('解析中...', 'info');
  resultEl.classList.remove('show');

  try {
    var r = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url })
    });
    var j = null;
    try { j = await r.json(); } catch (e) {}

    if (!r.ok) {
      setStatus('解析失败：' + ((j && j.msg) || ('错误 ' + r.status)), 'error');
      return;
    }
    if (!j || j.code !== 0) {
      setStatus((j && j.msg) || '解析失败', 'error');
      return;
    }

    var d = j.data;
    platformBadge.textContent = d.platform || '视频';
    videoTitle.textContent = d.title || '（无标题）';
    videoPlayer.src = '/api/proxy?url=' + encodeURIComponent(d.video_url);
    videoPlayer.load();

    resultEl.classList.add('show');
    setStatus('解析成功 ✅ 正在加载视频…', 'ok');

    videoPlayer.onerror = function() {
      setStatus('视频加载失败，可能 CDN 拒绝访问或视频已失效', 'error');
    };
    videoPlayer.oncanplay = function() {
      setStatus('解析成功 ✅', 'ok');
    };
  } catch (e) {
    setStatus('请求失败：' + (e.message || e), 'error');
  } finally {
    parseBtn.disabled = false;
  }
}

parseBtn.addEventListener('click', parseVideo);
urlInput.addEventListener('keydown', function (e) {
  if (e.key === 'Enter') parseVideo();
});

(function () {
  var p = new URLSearchParams(location.search).get('url');
  if (p) {
    urlInput.value = decodeURIComponent(p);
    setStatus('正在解析分享的链接...', 'info');
    setTimeout(parseVideo, 300);
  }
})();
</script>
</body>
</html>'''


def find_free_port(start=5000, tries=20):
    for p in range(start, start + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", p)); s.close(); return p
        except OSError:
            s.close()
    return None


def get_local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


if __name__ == "__main__":
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass

    PORT = find_free_port(5000)
    if PORT is None:
        print("[错误] 5000-5019 端口全被占用")
        sys.exit(1)

    print("\n" + "=" * 64)
    print("  短视频去水印解析服务已启动")
    print("=" * 64)
    print("  Python: " + sys.version.split()[0])
    print("  端口:   " + str(PORT))
    print("-" * 64)
    print("  💻 电脑: http://127.0.0.1:" + str(PORT))
    for ip in get_local_ips():
        print("  📱 手机: http://" + ip + ":" + str(PORT))
    print("=" * 64 + "\n")

    if not IN_DOCKER:
        threading.Thread(
            target=lambda: (time.sleep(1.5), start_cloudflared(PORT)),
            daemon=True).start()

        def open_browser():
            check = "http://127.0.0.1:" + str(PORT) + "/api/ping"
            start = time.time()
            while time.time() - start < 8:
                time.sleep(0.2)
                try:
                    if requests.get(check, timeout=1).status_code == 200:
                        webbrowser.open("http://127.0.0.1:" + str(PORT) + "/")
                        return
                except Exception:
                    continue
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, debug=False,
            threaded=True, use_reloader=False)