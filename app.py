import os
import json
import glob
import re
import shutil
import sqlite3
import requests
import subprocess
import time
import threading
import uuid
import signal
import hashlib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from cachetools import TTLCache

app = Flask(__name__)
CORS(app)

# ===================== 全局配置 =====================
API_TOKEN = "MySecretMCAPI2026"
MODRINTH_API = "https://api.modrinth.com/v2"
CACHE_TTL = 1800
REQUEST_TIMEOUT = 15
MAX_RETRY = 3
PROXY_SOCKS = None
CACHE_DIR = "./cache"
PUBLIC_DOWNLOAD_DIR = "./download_cache"
MC_ROOT = "mc_server"
CACHE_SEARCH = TTLCache(maxsize=200, ttl=1800)
CACHE_PROJECT = TTLCache(maxsize=200, ttl=1800)

# ---------- PaperMC fill v3 配置（取自 papermc_fetch.py） ----------
FILL_API_BASE = "https://fill.papermc.io/v3"
FILL_USER_AGENT = "termux-papermc-fetch/1.0.0 (+https://github.com/termux-mc/papermc-fetch)"
FILL_PROJECTS = ["paper", "folia", "waterfall", "velocity", "travertine"]
FILL_CHANNELS = {"stable", "beta", "alpha"}
FILL_MAPPINGS = ("default", "mojang")
FILL_TIMEOUT = 30
FILL_MAX_WORKERS = 8
# 内存缓存 fill 接口
fill_project_cache = dict()  # key:project, value={"data":..., "expire":timestamp}
FILL_CACHE_SEC = 3600

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(PUBLIC_DOWNLOAD_DIR, exist_ok=True)
os.makedirs(MC_ROOT, exist_ok=True)

# ========= PaperMC fill v3 工具函数（来自 papermc_fetch.py） =========
def fill_api_get(path: str, user_agent: str):
    req = urllib.request.Request(
        FILL_API_BASE + path,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FILL_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            msg = body.get("message") or body
        except Exception:
            msg = e.reason
        raise RuntimeError(f"HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}")

def fill_get_project(project: str):
    now = time.time()
    cached = fill_project_cache.get(project)
    if cached and now < cached["expire"]:
        return cached["data"]
    data = fill_api_get(f"/projects/{project}", FILL_USER_AGENT)
    if not isinstance(data.get("versions"), dict) or not data["versions"]:
        raise RuntimeError(f"项目 {project} 无可用版本")
    fill_project_cache[project] = {"data": data["versions"], "expire": now + FILL_CACHE_SEC}
    return data["versions"]

def fill_get_builds(project: str, version: str):
    data = fill_api_get(f"/projects/{project}/versions/{version}/builds", FILL_USER_AGENT)
    if not isinstance(data, list):
        raise RuntimeError(f"获取 {version} 构建失败")
    return data

def fill_pick_build(builds: list, channel: str = "stable", mapping: str = "default", fallback=True):
    wanted = [b for b in builds if b.get("channel", "").lower() == channel]
    pool = wanted or (builds if fallback else [])
    for b in pool:
        dl = (b.get("downloads") or {}).get(f"server:{mapping}")
        if dl:
            return {"build": b, "dl": dl, "fallback": not wanted}
    return None

def fill_flatten_versions(versions: dict):
    flat = []
    for family, members in versions.items():
        for v in members:
            flat.append((family, v))
    return flat
# ==================================================================

# ---------- 其他核心适配层: purpur/spigot/fabric/forge/bungeecord ----------
# 与 fill 不同, 这 5 个核心各有一套独立 API, 统一封装为:
#   get_versions(core)            -> [版本, ...] (新→旧)
#   get_download(core, version)   -> {"url", "alt_urls", "name", ...}
_UA = "termux-mc-panel/1.0 (+https://github.com/termux-mc)"
_TMO = 30
_CACHE_SEC = 1800
_cache = {}


def _get(url, headers=None, want_json=True):
    h = {"User-Agent": _UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=_TMO) as r:
            body = r.read()
            if want_json:
                return json.loads(body.decode("utf-8"))
            return body
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {url}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason} ({url})")


def _cached(key, fetch):
    now = time.time()
    c = _cache.get(key)
    if c and now < c["expire"]:
        return c["data"]
    data = fetch()
    _cache[key] = {"expire": now + _CACHE_SEC, "data": data}
    return data


def _ver_key(v):
    """把版本号转成可比较的数字元组, 如 26.3 > 1.21.11 > 1.9.4"""
    nums = []
    for p in re.split(r"[.\-]", str(v)):
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    return nums


# ---- Purpur ----
PURPUR_API = "https://api.purpurmc.org/v2/purpur"


def purpur_versions():
    data = _cached("purpur_versions", lambda: _get(PURPUR_API))
    vers = data.get("versions") or []
    return list(reversed(vers))  # 最新在前


def purpur_download(version):
    data = _cached(f"purpur_builds_{version}",
                   lambda: _get(f"{PURPUR_API}/{version}"))
    latest = (data.get("builds") or {}).get("latest")
    if not latest:
        raise RuntimeError(f"Purpur 没有版本 {version} 的构建")
    return {
        "url": f"{PURPUR_API}/{version}/{latest}/download",
        "name": f"purpur-{version}-{latest}.jar",
        "build": latest,
        "version": version,
    }


# ---- Spigot ----
SPIGOT_JARS_API = "https://getbukkit.org/api/jars"
SPIGOT_DL_BASE = "https://download.getbukkit.org"
_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
               "Chrome/120 Mobile Safari/537.36")


def _spigot_jars():
    return _cached("spigot_jars",
                   lambda: _get(SPIGOT_JARS_API, {"User-Agent": _BROWSER_UA}))


def spigot_versions():
    arr = _spigot_jars().get("spigot") or []
    return [x["version"] for x in arr]  # 接口本身即新→旧


def spigot_download(version):
    arr = _spigot_jars().get("spigot") or []
    for x in arr:
        if x.get("version") == version:
            return {
                "url": SPIGOT_DL_BASE + x["downloadUrl"],
                "name": x.get("fileName", f"spigot-{version}.jar"),
                "sha256": x.get("sha256"),
                "size": x.get("size"),
                "version": version,
            }
    raise RuntimeError(f"getbukkit 没有 Spigot 版本 {version}")


# ---- Fabric ----
FABRIC_META = "https://meta.fabricmc.net/v2"


def fabric_versions():
    data = _cached("fabric_versions",
                   lambda: _get(f"{FABRIC_META}/versions/game"))
    return [x["version"] for x in data if x.get("stable")]


def _fabric_loader_installer():
    """取最新稳定 loader 和 installer 版本号。"""
    loaders = _cached("fabric_loaders",
                      lambda: _get(f"{FABRIC_META}/versions/loader"))
    installers = _cached("fabric_installers",
                         lambda: _get(f"{FABRIC_META}/versions/installer"))
    loader = next((x["version"] for x in loaders if x.get("stable")), None)
    installer = next((x["version"] for x in installers if x.get("stable")), None)
    if not loader or not installer:
        raise RuntimeError("Fabric 官方 meta 接口返回异常")
    return loader, installer


def fabric_download(version):
    loader, installer = _fabric_loader_installer()
    url = (f"{FABRIC_META}/versions/loader/{version}/"
           f"{loader}/{installer}/server/jar")
    return {
        "url": url,
        "name": f"fabric-server-mc{version}-loader{loader}.jar",
        "loader": loader,
        "installer": installer,
        "version": version,
    }


# ---- Forge ----
FORGE_PROMOS = ("https://files.minecraftforge.net/net/minecraftforge/"
                "forge/promotions_slim.json")
FORGE_MAVEN_HOSTS = [
    "https://files.minecraftforge.net/maven/net/minecraftforge/forge",
    "https://maven.minecraftforge.net/net/minecraftforge/forge",
]


def _forge_promos():
    return _cached("forge_promos",
                   lambda: _get(FORGE_PROMOS).get("promos") or {})


def forge_versions():
    promos = _forge_promos()
    mcs = set()
    for k in promos:
        if k.endswith("-recommended") or k.endswith("-latest"):
            mcs.add(k.rsplit("-", 1)[0])
    return sorted(mcs, key=_ver_key, reverse=True)  # 新→旧


def forge_download(mc_version):
    promos = _forge_promos()
    fv = (promos.get(f"{mc_version}-recommended")
          or promos.get(f"{mc_version}-latest"))
    if not fv:
        raise RuntimeError(f"Forge 没有 {mc_version} 的构建")
    # promos 只给构建号(如 52.1.0), maven 目录名通常是 "MC版本-构建号"(如 1.21.1-52.1.0)
    ids = []
    full = f"{mc_version}-{fv}"
    if full != fv:
        ids.append(full)
    ids.append(fv)
    urls = []
    for host in FORGE_MAVEN_HOSTS:
        for aid in ids:
            urls.append(f"{host}/{aid}/forge-{aid}-installer.jar")
    # 旧版(≤1.16.x)还有 universal 直接运行包, 作最后兜底
    for aid in ids:
        urls.append(f"{FORGE_MAVEN_HOSTS[0]}/{aid}/forge-{aid}-universal.jar")
    return {
        "url": urls[0],
        "alt_urls": urls[1:],
        "name": f"forge-{full}-installer.jar",
        "forge_version": full,
        "version": mc_version,
        "note": ("Forge 安装器: 下载后需先运行一次 "
                 "java -jar <jar> --installServer 生成可运行目录"),
    }


# ---- BungeeCord ----
BC_JENKINS = "https://ci.md-5.net/job/BungeeCord"


def bungee_versions():
    data = _cached("bc_builds",
                   lambda: _get(f"{BC_JENKINS}/api/json?tree=builds[number]{{0,12}}"))
    nums = [b["number"] for b in data.get("builds", [])]
    if not nums:
        raise RuntimeError("BungeeCord Jenkins 返回为空")
    return [str(n) for n in nums]  # 新→旧


def bungee_download(version):
    if version in ("", "latest"):
        ref = "lastSuccessfulBuild"
    else:
        ref = version
    return {
        "url": f"{BC_JENKINS}/{ref}/artifact/bootstrap/target/BungeeCord.jar",
        "name": "BungeeCord.jar",
        "version": version or "latest",
        "note": "BungeeCord 无按 MC 版本的构建, 版本号即 Jenkins 构建号",
    }


# ---- 统一注册表 ----
CORE_SOURCES = {
    "purpur":     {"name": "Purpur",     "versions": purpur_versions,
                   "download": purpur_download},
    "spigot":     {"name": "Spigot",     "versions": spigot_versions,
                   "download": spigot_download},
    "fabric":     {"name": "Fabric",     "versions": fabric_versions,
                   "download": fabric_download},
    "forge":      {"name": "Forge",      "versions": forge_versions,
                   "download": forge_download},
    "bungeecord": {"name": "BungeeCord", "versions": bungee_versions,
                   "download": bungee_download},
}


def get_versions(core):
    if core not in CORE_SOURCES:
        raise RuntimeError(f"未知核心 {core}")
    return CORE_SOURCES[core]["versions"]()


def get_download(core, version):
    if core not in CORE_SOURCES:
        raise RuntimeError(f"未知核心 {core}")
    return CORE_SOURCES[core]["download"](version)
# ==================================================================

session = requests.Session()
download_tasks = {}

def background_download(task_id, download_url, save_path):
    task = download_tasks[task_id]
    try:
        resp = requests.get(download_url, stream=True, timeout=60)
        total_size = resp.headers.get("content-length")
        if total_size is not None:
            task["total"] = int(total_size)
        else:
            task["total"] = None
        task["done"] = 0
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    task["done"] += len(chunk)
        task["finished"] = True
    except Exception as e:
        task["error"] = str(e)
        task["finished"] = True

def resp(code, msg, data=None):
    return jsonify({"code": code, "msg": msg, "data": data})

def check_auth():
    token = request.args.get("token", "")
    return token == API_TOKEN

def load_cache(cache_key):
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            item = json.load(f)
        now = time.time()
        if item["expire"] > now:
            return item["data"]
        else:
            os.remove(cache_file)
            return None
    except Exception:
        return None

def save_cache(cache_key, data):
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
    cache_data = {
        "expire": time.time() + CACHE_TTL,
        "data": data
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False)

def modrinth_get(url, params=None):
    headers = {
        "User-Agent": "MCDatapackAPI/1.0 (Termux-FeiniuOS)"
    }
    proxies = None
    if PROXY_SOCKS:
        proxies = {"http": PROXY_SOCKS, "https": PROXY_SOCKS}
    for attempt in range(MAX_RETRY):
        try:
            r = session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT, proxies=proxies)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.4)
    return None

instance_logs = {}
instance_proc = {}

def init_db():
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        core_type TEXT,
        version TEXT,
        folder_path TEXT,
        port INTEGER,
        xmx TEXT,
        xms TEXT,
        status TEXT DEFAULT "stopped",
        pid INTEGER DEFAULT NULL
    )
    ''')
    conn.commit()
    conn.close()

def migrate_db():
    """旧版 panel.db 自动补列: 表里缺 pid 列时 ALTER TABLE 加上,
    否则 start_instance 的 UPDATE ... pid=? 会报 no such column: pid"""
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(instances)").fetchall()]
    if "pid" not in cols:
        cur.execute("ALTER TABLE instances ADD COLUMN pid INTEGER DEFAULT NULL")
        conn.commit()
        print("[迁移] instances 表已补上 pid 列")
    conn.close()

init_db()
migrate_db()

def log_listener(inst_id, stream):
    if inst_id not in instance_logs:
        instance_logs[inst_id] = []
    for line in stream:
        text = line.decode("utf-8", errors="ignore").strip()
        time_str = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{time_str}] {text}"
        instance_logs[inst_id].append(log_line)
        if len(instance_logs[inst_id]) > 1000:
            instance_logs[inst_id].pop(0)

# ===================== 网页页面路由 =====================
@app.route("/")
def index():
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("SELECT id,name,core_type,version,port,xmx,xms,status FROM instances")
    res = cur.fetchall()
    conn.close()
    return render_template("index.html", instances=res)

@app.route("/create")
def create_instance_page():
    return render_template("create_instance.html")

@app.route("/instance/<int:inst_id>")
def instance_page(inst_id):
    return render_template("instance.html", inst_id=inst_id)

@app.route("/market")
def market_page():
    return render_template("market.html")

@app.route("/market/mod")
def mod_download_page():
    return render_template("mod_download.html")

@app.route("/market/plugin")
def plugin_download_page():
    return render_template("plugin_download.html")

@app.route("/market/datapack")
def dp_download_page():
    return render_template("dp_download.html")

# ========== 新建实例向导：核心版本、下载地址（已替换为 fill v3） ==========
@app.get("/api/core/list")
def get_core_list():
    core_data = [
        {
            "id": "paper",
            "name": "Paper",
            "category": "插件服务端",
            "desc": "基于Spigot优化的高性能插件服务端，MC原版生态",
            "good": "性能强、插件兼容极高、更新活跃，生存/小游戏首选",
            "bad": "仅支持插件，不支持原生模组"
        },
        {
            "id": "folia",
            "name": "Folia",
            "category": "插件服务端（分片）",
            "desc": "Paper团队开发，多区域分片并行服务端",
            "good": "超大服、很多玩家场景性能爆炸",
            "bad": "大量插件不兼容，不适合普通小型服务器"
        },
        {
            "id": "waterfall",
            "name": "Waterfall",
            "category": "Bungee兼容代理",
            "desc": "PaperMC代理服务端",
            "good": "BungeeCord替代，性能更好",
            "bad": "仅代理，不能作为游戏服务端"
        },
        {
            "id": "velocity",
            "name": "Velocity",
            "category": "代理端",
            "desc": "现代高性能MC代理，多服分流",
            "good": "性能好、安全，推荐用于多服务器群组",
            "bad": "不是游戏服，只能做代理转发，不能直接进游戏"
        },
        {
            "id": "travertine",
            "name": "Travertine",
            "category": "代理端",
            "desc": "Waterfall的衍生版本",
            "good": "性能优化代理",
            "bad": "使用人群较少"
        },
        {
            "id": "purpur",
            "name": "Purpur",
            "category": "插件服务端",
            "desc": "基于Paper二次增强",
            "good": "更多额外配置项，性能略优于Paper",
            "bad": "小众，部分插件兼容性略差于Paper"
        },
        {
            "id": "spigot",
            "name": "Spigot",
            "category": "插件服务端",
            "desc": "经典老牌插件核心",
            "good": "插件生态极其丰富，老插件支持好",
            "bad": "性能弱于Paper，已经逐步减少更新"
        },
        {
            "id": "fabric",
            "name": "Fabric",
            "category": "模组服务端",
            "desc": "轻量模组加载器",
            "good": "轻量、更新快，新版本模组优先支持Fabric",
            "bad": "模组生态偏向轻量，大型整合包部分不兼容"
        },
        {
            "id": "forge",
            "name": "Forge",
            "category": "模组服务端",
            "desc": "老牌大型模组加载器",
            "good": "大型整合包、传统模组绝大多数支持Forge",
            "bad": "笨重，启动慢，内存占用高，新版本更新慢"
        },
        {
            "id": "bungeecord",
            "name": "BungeeCord",
            "category": "代理端",
            "desc": "老牌代理",
            "good": "旧群组插件兼容性好",
            "bad": "性能弱，安全方面不如Velocity"
        }
    ]
    return jsonify({"code":0,"data":core_data})

@app.get("/api/core/versions")
def get_core_versions():
    core_id = request.args.get("core")
    if not core_id:
        return jsonify({"code":-1,"msg":"缺少核心ID"})
    try:
        if core_id in FILL_PROJECTS:
            versions_dict = fill_get_project(core_id)
            flat = fill_flatten_versions(versions_dict)
            ver_list = [v for (fam,v) in flat]
        elif core_id in CORE_SOURCES:
            ver_list = get_versions(core_id)
        else:
            return jsonify({"code":0,"data":[],"msg":"未知核心"})
        return jsonify({"code":0,"data":ver_list})
    except Exception as e:
        return jsonify({"code":-1,"msg":str(e)})

@app.get("/api/core/download_url")
def get_core_download_url():
    core_id = request.args.get("core")
    ver = request.args.get("ver")
    if not core_id or not ver:
        return jsonify({"code":-1,"msg":"参数缺失"})
    try:
        if core_id in FILL_PROJECTS:
            builds = fill_get_builds(core_id, ver)
            hit = fill_pick_build(builds, channel="stable", mapping="default")
            if not hit:
                return jsonify({"code":-1,"msg":"找不到可用构建"})
            return jsonify({"code":0,"url":hit["dl"]["url"]})
        elif core_id in CORE_SOURCES:
            info = get_download(core_id, ver)
            return jsonify({
                "code":0,
                "url": info["url"],
                "name": info.get("name"),
                "sha256": info.get("sha256"),
                "alt_urls": info.get("alt_urls", []),
                "note": info.get("note")
            })
        else:
            return jsonify({"code":-1,"msg":"未知核心"})
    except Exception as e:
        return jsonify({"code":-1,"msg":str(e)})

def _resolve_core_download(core_type, version):
    """服务端解析核心下载地址(前端不传 url 时使用)"""
    if core_type in FILL_PROJECTS:
        builds = fill_get_builds(core_type, version)
        hit = fill_pick_build(builds, channel="stable", mapping="default")
        if not hit:
            raise RuntimeError("找不到可用构建")
        return {"url": hit["dl"]["url"], "alt_urls": [], "sha256": None, "note": None}
    if core_type in CORE_SOURCES:
        return get_download(core_type, version)
    raise RuntimeError(f"未知核心 {core_type}")


def _download_with_fallback(url, alt_urls, save_path):
    """流式下载, 主地址失败时依次尝试备用地址"""
    last_err = None
    for u in [url] + list(alt_urls or []):
        try:
            resp_http = requests.get(u, stream=True, timeout=(10, 300))
            resp_http.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp_http.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            return u
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"所有下载地址均失败: {last_err}")


@app.post("/api/download_core")
def download_core():
    data = request.get_json()
    core_type = data.get("core_type")
    version = data.get("version")
    download_url = data.get("url")
    inst_name = data.get("name")
    port = int(data.get("port",25565))
    xmx = data.get("xmx","2G")
    xms = data.get("xms","1G")

    # 前端没传 url 时由后端解析(兼容旧版新建页面)
    dl_info = None
    if not download_url:
        dl_info = _resolve_core_download(core_type, version)
        download_url = dl_info["url"]

    folder_name = f"{core_type}-{version}"
    instance_folder = os.path.join(MC_ROOT, folder_name)
    os.makedirs(instance_folder, exist_ok=True)
    jar_path = os.path.join(instance_folder, f"{core_type}.jar")

    try:
        _download_with_fallback(download_url,
                                dl_info.get("alt_urls") if dl_info else None,
                                jar_path)
    except Exception as e:
        return jsonify({"code":-1,"msg":f"核心下载失败: {e}"})

    # Forge 特殊处理: 安装器需先执行 --installServer 生成可运行目录
    if core_type == "forge":
        try:
            proc = subprocess.run(["java","-jar",f"{core_type}.jar","--installServer"],
                                  cwd=instance_folder, capture_output=True, timeout=600)
            if proc.returncode != 0:
                err_tail = (proc.stderr or proc.stdout).decode("utf-8","ignore")[-400:]
                return jsonify({"code":-1,"msg":f"Forge --installServer 失败: {err_tail}"})
        except subprocess.TimeoutExpired:
            return jsonify({"code":-1,"msg":"Forge 安装超时(>10分钟), 请检查网络后重试"})
        except FileNotFoundError:
            return jsonify({"code":-1,"msg":"未找到 java, 请先安装 OpenJDK"})
        # 生成 start.sh, 让面板启动时按用户设置的内存运行(读取 unix_args.txt)
        args_files = glob.glob(os.path.join(
            instance_folder, "libraries/net/minecraftforge/forge/*/unix_args.txt"))
        if args_files:
            args_file = os.path.relpath(args_files[0], instance_folder)
            start_sh = os.path.join(instance_folder, "start.sh")
            with open(start_sh, "w", encoding="utf-8") as f:
                f.write(f"#!/usr/bin/env sh\nexec java -Xms{xms} -Xmx{xmx} @{args_file} \"$@\"\n")
            os.chmod(start_sh, 0o755)

    eula_path = os.path.join(instance_folder, "eula.txt")
    with open(eula_path, "w", encoding="utf-8") as f:
        f.write("eula=true")
    server_prop = os.path.join(instance_folder, "server.properties")
    prop_text = f"""server-port={port}
max-players=20
online-mode=true
view-distance=10
"""
    with open(server_prop, "w", encoding="utf-8") as f:
        f.write(prop_text)
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO instances (name,core_type,version,folder_path,port,xmx,xms)
        VALUES (?,?,?,?,?,?,?)
    """, (inst_name, core_type, version, instance_folder, port, xmx, xms))
    conn.commit()
    conn.close()
    return jsonify({"code":0,"msg":"实例文件创建完成，请到主页点击运行实例","path":instance_folder})

# ========== 实例启停、命令、日志 ==========
@app.post("/api/instance/start/<int:inst_id>")
def start_instance(inst_id):
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("SELECT folder_path,core_type,xmx,xms FROM instances WHERE id=?",(inst_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"code":-1,"msg":"实例不存在"})
    folder,core,xmx,xms = row
    if inst_id in instance_proc and instance_proc[inst_id].poll() is None:
        return jsonify({"code":-1,"msg":"实例已经在运行"})
    jar_path = os.path.join(folder, f"{core}.jar")
    if not os.path.exists(jar_path):
        return jsonify({"code":-1,"msg":"核心jar文件不存在"})
    # Forge: 用 --installServer 生成的 start.sh 启动; 其他核心直接 java -jar
    if core == "forge" and os.path.exists(os.path.join(folder, "start.sh")):
        cmd = ["bash", "start.sh", "nogui"]
    else:
        cmd = ["java", f"-Xms{xms}", f"-Xmx{xmx}", "-jar", f"{core}.jar"]
        if core != "bungeecord":  # 代理端不接受额外参数
            cmd.append("nogui")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        return jsonify({"code":-1,"msg":"未找到 java 命令, 请在 Termux 执行: pkg install openjdk-17"})
    except Exception as e:
        return jsonify({"code":-1,"msg":f"启动进程失败: {e}"})
    instance_proc[inst_id] = proc
    instance_logs[inst_id] = []
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("UPDATE instances SET status='running', pid=? WHERE id=?",(proc.pid, inst_id))
    conn.commit()
    conn.close()
    def log_pump(stream, inst):
        """持续消费子进程输出; 同时读 stdout 和 stderr, 避免管道写满阻塞进程"""
        for line in stream:
            try:
                txt = line.decode("utf-8", errors="ignore").rstrip("\r\n")
                if not txt:
                    continue
                instance_logs[inst].append(txt)
                if len(instance_logs[inst]) > 1000:
                    instance_logs[inst].pop(0)
            except Exception:
                pass
    def log_reader(p, inst):
        t_out = threading.Thread(target=log_pump, args=(p.stdout, inst), daemon=True)
        t_err = threading.Thread(target=log_pump, args=(p.stderr, inst), daemon=True)
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        conn2 = sqlite3.connect("panel.db")
        cur2 = conn2.cursor()
        cur2.execute("UPDATE instances SET status='stopped', pid=NULL WHERE id=?",(inst,))
        conn2.commit()
        conn2.close()
    threading.Thread(target=log_reader,args=(proc,inst_id),daemon=True).start()
    return jsonify({"code":0,"msg":"实例已启动"})

@app.post("/api/instance/stop/<int:inst_id>")
def stop_instance(inst_id):
    if inst_id not in instance_proc:
        return jsonify({"code":-1,"msg":"实例未运行"})
    proc = instance_proc[inst_id]
    if proc.poll() is not None:
        return jsonify({"code":-1,"msg":"实例已经停止"})
    try:
        proc.stdin.write(b"stop\n")
        proc.stdin.flush()
    except Exception as e:
        return jsonify({"code":-1,"msg":f"发送停止指令失败: {e}"})
    # 等待进程真正退出(MC 保存世界需要几秒), 最多等 20 秒
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # 极端情况: 进程已不可响应, 交给系统回收
    return jsonify({"code":0,"msg":"实例已停止"})

# ========== 删除实例(面板内一键删除, 面向初学者) ==========
@app.post("/api/instance/delete/<int:inst_id>")
def delete_instance(inst_id):
    if inst_id in instance_proc and instance_proc[inst_id].poll() is None:
        return jsonify({"code":-1,"msg":"实例正在运行, 请先停止再删除"})
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("SELECT name,folder_path FROM instances WHERE id=?",(inst_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"code":-1,"msg":"实例不存在"})
    name, folder_path = row
    # 删除实例文件夹(安全限制: 只允许删 MC_ROOT 下的目录, 防止误删别处)
    deleted_dir = False
    if folder_path:
        abs_folder = os.path.abspath(folder_path)
        abs_root = os.path.abspath(MC_ROOT)
        if abs_folder.startswith(abs_root + os.sep) and os.path.isdir(abs_folder):
            try:
                shutil.rmtree(abs_folder)
                deleted_dir = True
            except Exception:
                pass  # 文件夹删不掉不阻塞记录删除
    cur.execute("DELETE FROM instances WHERE id=?",(inst_id,))
    conn.commit()
    conn.close()
    instance_proc.pop(inst_id, None)
    instance_logs.pop(inst_id, None)
    msg = f"实例「{name}」已删除"
    if deleted_dir:
        msg += ", 文件夹已清理"
    return jsonify({"code":0,"msg":msg})

@app.post("/api/instance/sendcmd/<int:inst_id>")
def send_cmd(inst_id):
    cmd_text = request.get_json().get("cmd","")
    if inst_id not in instance_proc:
        return jsonify({"code":-1,"msg":"实例未运行"})
    proc = instance_proc[inst_id]
    if proc.poll() is not None:
        return jsonify({"code":-1,"msg":"实例未运行"})
    proc.stdin.write((cmd_text + "\n").encode("utf-8"))
    proc.stdin.flush()
    return jsonify({"code":0,"msg":"指令发送成功"})

@app.get("/api/instance/logs/<int:inst_id>")
def get_logs(inst_id):
    logs = instance_logs.get(inst_id, [])
    return jsonify({"code":0,"data":logs})

# ========== 资源页面：后台下载带进度接口 ==========
@app.post("/api/download/start")
def download_start():
    data = request.get_json()
    download_url = data.get("download_url")
    filename = data.get("filename")
    save_path = os.path.join(PUBLIC_DOWNLOAD_DIR, filename)
    task_id = str(uuid.uuid4())
    download_tasks[task_id] = {
        "total": None,
        "done":0,
        "finished":False,
        "error":None
    }
    th = threading.Thread(target=background_download, args=(task_id, download_url, save_path), daemon=True)
    th.start()
    return jsonify({"code":0,"task_id":task_id})

@app.get("/api/download/progress")
def download_progress():
    task_id = request.args.get("task_id")
    if task_id not in download_tasks:
        return jsonify({"code":-1,"msg":"任务不存在"})
    t = download_tasks[task_id]
    pct = None
    if t["total"] is not None and t["total"]>0:
        pct = round((t["done"] / t["total"]) *100,1)
    return jsonify({
        "code":0,
        "data":{
            "total":t["total"],
            "done":t["done"],
            "percent":pct,
            "finished":t["finished"],
            "error":t["error"]
        }
    })

# ======================== datapack API 接口 ========================
@app.route("/api/datapack/search", methods=["GET"])
def api_search():
    if not check_auth():
        return resp(401, "Token鉴权失败")
    query = request.args.get("query", "")
    game_version = request.args.get("game_version", "")
    cache_key = f"search_{query}_{game_version}"
    cache_data = load_cache(cache_key)
    if cache_data:
        return resp(0, "搜索成功(缓存命中)", cache_data)
    facets = [["project_type:datapack"]]
    if game_version:
        facets.append([f"versions:{game_version}"])
    params = {"query": query,"facets": json.dumps(facets),"limit":5}
    raw = modrinth_get(f"{MODRINTH_API}/search", params=params)
    if raw is None:
        return resp(500, "Modrinth API请求超时")
    hits = []
    for hit in raw["hits"]:
        hits.append({"id": hit["slug"],"name": hit["title"],"author": hit["author"],"description": hit["description"],"icon": hit.get("icon_url", ""),"versions": hit["versions"]})
    ret_data = {"hits": hits}
    save_cache(cache_key, ret_data)
    return resp(0, "搜索成功(新请求)", ret_data)

@app.route("/api/datapack/versions", methods=["GET"])
def api_versions():
    if not check_auth():
        return resp(401, "Token错误")
    project_id = request.args.get("project_id")
    if not project_id:
        return resp(-2, "缺少project_id参数")
    cache_key = f"ver_{project_id}"
    cache_data = load_cache(cache_key)
    if cache_data:
        return resp(0, "版本列表(缓存命中)", cache_data)
    raw = modrinth_get(f"{MODRINTH_API}/project/{project_id}/version")
    if raw is None:
        return resp(500, "Modrinth请求超时")
    ver_list = []
    for v in raw:
        file_obj = v["files"][0]
        ver_list.append({"version_number": v["version_number"],"game_versions": v["game_versions"],"download_url": file_obj["url"]})
    res_data = {"versions": ver_list}
    save_cache(cache_key, res_data)
    return resp(0, "版本列表(新请求)", res_data)

@app.route("/api/datapack/health", methods=["GET"])
def datapack_health():
    if not check_auth():
        return resp(401, "Token错误")
    return resp(0, "API运行正常，Modrinth实时数据源+持久文件缓存")

# ======================== Mod API接口 ========================
@app.get("/api/mc/search")
def search_mod():
    q = request.args.get("q", "")
    ver = request.args.get("gameVersion", "")
    cache_key = f"{q}|{ver}"
    if cache_key in CACHE_SEARCH:
        return jsonify({"code":0,"data":CACHE_SEARCH[cache_key],"cache":"hit"})
    params = {"query": q, "limit": 20}
    if ver:
        params["game_versions"] = ver
    try:
        resp_http = session.get(f"{MODRINTH_API}/search", params=params, headers={"User-Agent":"MCDatapackAPI/1.0 (Termux-FeiniuOS)"}, timeout=4)
        raw = resp_http.json()
        out = []
        for hit in raw.get("hits", []):
            out.append({"id": hit.get("project_id", ""),"slug": hit.get("slug", ""),"name": hit.get("title", ""),"desc": hit.get("summary", ""),"icon": hit.get("icon_url", ""),"type": hit.get("project_type", "")})
        CACHE_SEARCH[cache_key] = out
        return jsonify({"code":0,"data":out,"cache":"new"})
    except requests.exceptions.Timeout:
        return jsonify({"code": -1, "msg": "Modrinth API 请求超时"}), 500
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500

@app.get("/api/mc/project/<pid>")
def get_project(pid):
    if pid in CACHE_PROJECT:
        return jsonify({"code":0,"data":CACHE_PROJECT[pid],"cache":"hit"})
    try:
        p_resp = session.get(f"{MODRINTH_API}/project/{pid}", headers={"User-Agent":"MCDatapackAPI/1.0 (Termux-FeiniuOS)"}, timeout=4)
        p_data = p_resp.json()
        v_resp = session.get(f"{MODRINTH_API}/project/{pid}/version", headers={"User-Agent":"MCDatapackAPI/1.0 (Termux-FeiniuOS)"}, timeout=4)
        v_data = v_resp.json()
        versions = []
        if isinstance(v_data, list):
            for v in v_data:
                files = []
                if isinstance(v.get("files"), list):
                    for f in v["files"]:
                        files.append({
                            "filename": f.get("filename", ""),
                            "url": f.get("url", ""),
                            "sha1": f.get("hashes", {}).get("sha1", "")
                        })
                versions.append({
                    "version_id": v.get("id", ""),
                    "version_number": v.get("version_number", ""),
                    "game_versions": v.get("game_versions", []),
                    "loaders": v.get("loaders", []),
                    "files": files
                })
        res = {
            "id": p_data.get("id", ""),
            "slug": p_data.get("slug", ""),
            "name": p_data.get("title", ""),
            "description": p_data.get("description", ""),
            "icon": p_data.get("icon_url", ""),
            "versions": versions
        }
        CACHE_PROJECT[pid] = res
        return jsonify({"code":0,"data":res,"cache":"new"})
    except requests.exceptions.Timeout:
        return jsonify({"code": -1, "msg": "Modrinth API 请求超时"}), 500
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)}), 500

# ======================== Plugin API接口 ========================
HEADERS = {"User-Agent": "MCPluginManager/1.0 (mcplugin@test.com)"}
@app.route("/api/plugin/search", methods=["GET"])
def plugin_search():
    if not check_auth():
        return resp(401,"Token错误")
    raw_keyword = request.args.get("keyword", "")
    keyword = raw_keyword.replace(" ", "")
    facets = [["project_type:plugin"]]
    facets_str = json.dumps(facets)
    api_url = f"https://api.modrinth.com/v2/search?query={keyword}&facets={facets_str}&limit=20&sort=downloads"
    try:
        r = requests.get(api_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
        raw = r.json()
        plugin_list = []
        for item in raw["hits"]:
            plugin_list.append({"id": item["project_id"],"name": item["title"],"icon": item.get("icon_url", ""),"description": item["description"]})
        return resp(0,"搜索完成",{"plugins":plugin_list})
    except Exception as e:
        return resp(-1,"调用Modrinth API失败",{"error":str(e)})

@app.route("/api/plugin/get_loaders", methods=["GET"])
def get_loaders():
    if not check_auth():
        return resp(401,"Token错误")
    pid = request.args.get("project_id")
    if not pid:
        return resp(-2,"缺少project_id参数")
    try:
        url = f"https://api.modrinth.com/v2/project/{pid}"
        res = requests.get(url, timeout=20, headers=HEADERS)
        res.raise_for_status()
        data = res.json()
        all_loaders = data.get("loaders", [])
        allow_loaders = {"paper", "spigot", "purpur", "folia", "bukkit", "velocity", "bungeecord"}
        filtered_loaders = [x for x in all_loaders if x in allow_loaders]
        return resp(0,"获取加载器成功",{"loaders":filtered_loaders})
    except Exception as e:
        return resp(-3,"获取项目加载器失败",{"error":str(e)})

@app.route("/api/plugin/getjar", methods=["GET"])
def get_jar_link():
    if not check_auth():
        return resp(401,"Token错误")
    pid = request.args.get("project_id")
    stype = request.args.get("server_type")
    if not pid or not stype:
        return resp(-2,"缺少参数")
    try:
        ver_url = f"https://api.modrinth.com/v2/project/{pid}/version"
        ver_res = requests.get(ver_url, timeout=20, headers=HEADERS)
        ver_res.raise_for_status()
        ver_list = ver_res.json()
        match_versions = []
        for v in ver_list:
            if stype in v.get("loaders", []):
                match_versions.append(v)
        if len(match_versions) == 0:
            return resp(-4,"该插件没有适配此加载器的版本")
        target = match_versions[0]
        jar_url = target["files"][0]["url"]
        jar_name = target["files"][0]["filename"]
        return resp(0,"✅获取成功",{"project_id": pid,"loader": stype,"jar_name": jar_name,"jar_url": jar_url})
    except Exception as e:
        return resp(-3,"获取Jar链接失败",{"error":str(e)})

# ====================== 资源监控模块 ======================
def get_folder_size(folder_path):
    total = 0
    if not os.path.exists(folder_path):
        return 0
    for dirpath, _, filenames in os.walk(folder_path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total

def bytes_human(byte_val):
    if byte_val is None:
        return "无法读取"
    if byte_val < 1024:
        return f"{byte_val} B"
    elif byte_val < 1024*1024:
        return f"{round(byte_val/1024,1)} KB"
    else:
        return f"{round(byte_val/(1024*1024),1)} MB"

def get_proc_memory(pid):
    try:
        status_path = f"/proc/{pid}/status"
        if not os.path.exists(status_path):
            return None
        with open(status_path,"r",encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.strip().split()
                    kb = int(parts[1])
                    return kb * 1024
    except Exception:
        return None

@app.get("/api/instance/status/<int:inst_id>")
def get_instance_status(inst_id):
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("SELECT folder_path FROM instances WHERE id=?",(inst_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"code":-1,"msg":"实例不存在"})
    folder_path = row[0]
    is_running = False
    mem_bytes = None
    pid = None
    if inst_id in instance_proc:
        proc = instance_proc[inst_id]
        pid = proc.pid
        if proc.poll() is None:
            is_running = True
            mem_bytes = get_proc_memory(pid)
    disk_bytes = get_folder_size(folder_path)
    return jsonify({
        "code":0,
        "data":{
            "running": is_running,
            "pid": pid,
            "mem_bytes": mem_bytes,
            "mem_human": bytes_human(mem_bytes),
            "disk_bytes": disk_bytes,
            "disk_human": bytes_human(disk_bytes)
        }
    })

@app.get("/api/instance/all_status")
def get_all_instance_status():
    conn = sqlite3.connect("panel.db")
    cur = conn.cursor()
    cur.execute("SELECT id,folder_path FROM instances")
    rows = cur.fetchall()
    conn.close()
    out = []
    for (inst_id,folder_path) in rows:
        is_running = False
        mem_bytes = None
        pid = None
        if inst_id in instance_proc:
            proc = instance_proc[inst_id]
            pid = proc.pid
            if proc.poll() is None:
                is_running = True
                mem_bytes = get_proc_memory(pid)
        disk_bytes = get_folder_size(folder_path)
        out.append({
            "id":inst_id,
            "running":is_running,
            "pid":pid,
            "mem_bytes":mem_bytes,
            "mem_human": bytes_human(mem_bytes),
            "disk_bytes":disk_bytes,
            "disk_human": bytes_human(disk_bytes)
        })
    return jsonify({"code":0,"data":out})

# ========== 全局异常处理: 任何未捕获异常都返回 JSON ==========
# 不这样做的话, Flask 500 会返回 HTML 错误页, 前端 r.json() 解析失败导致"没反应/弹JSON错误"
@app.errorhandler(Exception)
def handle_uncaught_error(e):
    try:
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({"code": e.code, "msg": e.description}), e.code
    except Exception:
        pass
    return jsonify({"code": -1, "msg": f"服务器内部错误: {type(e).__name__}: {e}"})

if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
