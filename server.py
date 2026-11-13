#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目管家 · Project Hub
─────────────────────────────────────────────
用途：管理「本地项目目录」和「对应 GitHub 仓库」的对应关系。

设计要点（都是为了让它在 Windows 上跑得稳）：
1. 纯 Python 标准库，零第三方依赖 —— 拷到任何装了 Python 的机器上都能跑。
2. 只用 http.server 起一个本地服务，绑定 127.0.0.1，外部访问不到。
3. 端口从 18800 起试。**为什么不用 8000/3000？**
   因为这台机器的 Windows 动态端口范围被改成了 1024-15000，
   落在里面的端口随时可能被系统当成"出站临时源端口"占走，
   之后再 bind 就会报 WinError 10013。18800 在这个范围之外，安全。
4. 所有 git 子进程都加了 CREATE_NO_WINDOW，否则每次探测都会闪黑框。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ─────────────────────────── 路径与常量 ───────────────────────────

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
WEB_DIR = APP_DIR / "web"
STORE_FILE = DATA_DIR / "projects.json"
RUNTIME_FILE = DATA_DIR / "runtime.json"

HOST = "127.0.0.1"
PORT_CANDIDATES = list(range(18800, 18840))
APP_NAME = "项目管家"
AUTO_OPEN = "--no-open" not in sys.argv  # 调试时加 --no-open 就不弹窗口

IS_WIN = os.name == "nt"
# Windows 上隐藏子进程控制台窗口（0x08000000 = CREATE_NO_WINDOW）
NO_WINDOW = {"creationflags": 0x08000000} if IS_WIN else {}

# 用 pythonw.exe 静默启动时 sys.stdout 是 None，print 会直接抛异常 —— 兜一下
if sys.stdout is None or sys.stderr is None:
    class _Sink:
        def write(self, *_a):
            return 0

        def flush(self):
            pass

        def isatty(self):
            return False

    if sys.stdout is None:
        sys.stdout = _Sink()
    if sys.stderr is None:
        sys.stderr = _Sink()

_LOCK = threading.RLock()
_PROBE_CACHE: dict[str, dict] = {}
_STORE: dict = {"version": 1, "projects": [], "scan_roots": [], "settings": {}}


# ─────────────────────────── 通用小工具 ───────────────────────────

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_store() -> dict:
    """从磁盘读项目清单；文件不存在或损坏时返回空结构，不让程序崩。"""
    global _STORE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STORE_FILE.exists():
        try:
            raw = json.loads(STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("projects"), list):
                raw.setdefault("scan_roots", [])
                raw.setdefault("settings", {})
                _STORE = raw
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 清单文件读取失败，已从空白开始：{exc}")
    return _STORE


def save_store() -> None:
    """先写临时文件再替换，避免写一半断电把清单写坏。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_STORE, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_FILE)


def _git(repo: str, *args: str, timeout: int = 12) -> str | None:
    """在指定仓库里执行 git 命令，失败一律返回 None（调用方据此降级）。"""
    if not repo or not os.path.isdir(repo):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, timeout=timeout, **NO_WINDOW,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001  超时 / git 不存在 / 权限
        return None


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _norm_host(host: str) -> str:
    """ssh.github.com / www.github.com 都是同一个 GitHub，去掉这层前缀再比。"""
    h = host.lower()
    for pre in ("ssh.", "git.", "www."):
        if h.startswith(pre):
            return h[len(pre):]
    return h


def split_remote(url: str | None) -> tuple[str, str, str] | None:
    """
    把各种写法的 Git 远程地址拆成 (host, owner, repo)，认不出来返回 None。

    要兼容的写法比想象中多，少认一种就会误报「远程不一致」：
        https://github.com/owner/repo.git
        git@github.com:owner/repo.git          ← scp 风格
        ssh://git@github.com/owner/repo.git
        ssh://git@ssh.github.com:443/owner/repo.git   ← SSH over 443，端口会挤进 owner
        https://user:token@github.com/owner/repo
        https://gitee.com/owner/repo
    本地路径（D:\\xxx、file:///...）不算远程地址，直接返回 None。
    """
    if not url:
        return None
    u = _SCHEME_RE.sub("", url.strip())
    # [user[:pass]@]host[:port][/:]owner/repo
    m = re.match(r"^(?:[^@/\s]+@)?([^/:\s]+)(?::(\d+))?[/:](.+?)/?$", u)
    if not m:
        return None
    host = m.group(1).lower()
    # 域名一定带点；这样能挡住 D:\xxx 这种被误认成 host 的盘符
    if "." not in host:
        return None
    segs = [s for s in m.group(3).split("/") if s]
    if len(segs) < 2:
        return None
    owner, repo = segs[-2], segs[-1]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    return _norm_host(host), owner, repo


def normalize_github(url: str | None) -> str | None:
    """
    把各种写法的 GitHub 地址统一成 https://github.com/owner/repo。
    不是 GitHub 的（Gitee / 自建 GitLab / 本地路径）原样保留，别乱改。
    """
    if not url or not url.strip():
        return None
    parts = split_remote(url)
    if parts:
        host, owner, repo = parts
        if host == "github.com" or host.endswith(".github.com"):
            return f"https://github.com/{owner}/{repo}"
    return url.strip().rstrip("/") or None


def repo_slug(url: str | None) -> str:
    """归一成 'host/owner/repo'，用于判断两个地址是不是同一个仓库。"""
    parts = split_remote(url)
    return "/".join(parts).lower() if parts else ""


def read_remote_from_config(repo: str) -> str | None:
    """
    直接读 .git/config 解析 origin 地址 —— 比跑 git 命令快 10 倍以上。
    扫描 400 个仓库时，这个差别是「4 秒」和「40 秒」。
    """
    git_dir = os.path.join(repo, ".git")
    if os.path.isfile(git_dir):  # worktree / submodule：.git 是个文本文件
        try:
            text = Path(git_dir).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"gitdir:\s*(.+)", text)
            if m:
                git_dir = m.group(1).strip()
                if not os.path.isabs(git_dir):
                    git_dir = os.path.join(repo, git_dir)
        except Exception:  # noqa: BLE001
            return None
    cfg = os.path.join(git_dir, "config")
    if not os.path.isfile(cfg):
        return None
    try:
        text = Path(cfg).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    current_remote = None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'\[remote\s+"([^"]+)"\]', line)
        if m:
            current_remote = m.group(1)
            continue
        if line.startswith("[") and not line.startswith('[remote'):
            current_remote = None
            continue
        m = re.match(r"url\s*=\s*(.+)", line)
        if m and current_remote in ("origin", None):
            return m.group(1).strip()
    return None


def infer_language(repo: str) -> str:
    """按标志性文件猜项目主语言，猜不准也没关系，用户可以手改。"""
    try:
        names = {n.lower() for n in os.listdir(repo)}
    except Exception:  # noqa: BLE001
        return ""
    table = [
        ({"package.json"}, "JavaScript"),
        ({"tsconfig.json"}, "TypeScript"),
        ({"pyproject.toml", "requirements.txt", "setup.py", "manage.py"}, "Python"),
        ({"cargo.toml"}, "Rust"),
        ({"go.mod"}, "Go"),
        ({"pom.xml", "build.gradle"}, "Java"),
        ({"composer.json"}, "PHP"),
        ({"gemfile"}, "Ruby"),
        ({"pubspec.yaml"}, "Dart"),
        ({"cmakelists.txt"}, "C++"),
        ({"index.html"}, "HTML"),
    ]
    for keys, lang in table:
        if names & keys:
            return lang
    return ""


# ─────────────────────────── 单仓库状态探测 ───────────────────────────

def probe_project(record: dict) -> dict:
    """
    探测一个项目的真实状态。返回的字典会被前端直接用来渲染徽章。
    任何一步失败都只是让对应字段变成 None，不抛异常。
    """
    path = (record.get("path") or "").strip()
    st: dict = {
        "exists": False,
        "is_git": False,
        "branch": None,
        "dirty": None,          # True=有未提交改动，False=干净，None=未知
        "changed": 0,           # 改动文件数
        "untracked": 0,         # 未跟踪文件数
        "ahead": 0,             # 领先远程多少提交（待推送）
        "behind": 0,            # 落后远程多少提交（待拉取）
        "has_upstream": False,
        "last_commit_at": None,
        "last_commit_msg": None,
        "remote": None,
        "match": None,          # 本地 remote 与登记的 GitHub 是否是同一个仓库
        "checked_at": time.time(),
        "error": None,
    }

    if not path:
        st["error"] = "未填写路径"
        return st
    if not os.path.isdir(path):
        st["error"] = "路径不存在"
        return st
    st["exists"] = True

    if not os.path.exists(os.path.join(path, ".git")):
        st["error"] = "不是 Git 仓库"
        return st
    st["is_git"] = True

    st["branch"] = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=8)

    # 提交信息用 \x1f 分隔，一次拿到时间+标题
    log = _git(path, "log", "-1", "--format=%cI%x1f%s", timeout=8)
    if log and "\x1f" in log:
        st["last_commit_at"], st["last_commit_msg"] = log.split("\x1f", 1)

    # 工作区状态：porcelain 每行一个改动，?? 开头是未跟踪文件
    porcelain = _git(path, "status", "--porcelain", timeout=15)
    if porcelain is not None:
        lines = [ln for ln in porcelain.splitlines() if ln.strip()]
        st["untracked"] = sum(1 for ln in lines if ln.startswith("??"))
        st["changed"] = len(lines)
        st["dirty"] = bool(lines)

    # 与上游的差异：输出 "behind<TAB>ahead"
    counts = _git(path, "rev-list", "--left-right", "--count", "@{u}...HEAD", timeout=10)
    if counts:
        parts = counts.split()
        if len(parts) == 2:
            st["behind"], st["ahead"] = int(parts[0]), int(parts[1])
            st["has_upstream"] = True

    remote = _git(path, "config", "--get", "remote.origin.url", timeout=6) or read_remote_from_config(path)
    st["remote"] = remote
    if remote and record.get("github"):
        st["match"] = repo_slug(remote) == repo_slug(record.get("github"))

    return st


def probe_many(records: list[dict], workers: int = 8) -> dict[str, dict]:
    """
    并发探测。git status 在大仓库上可能要几秒，
    串行探 20 个项目就是一分多钟 —— 所以必须开线程池。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, dict] = {}
    if not records:
        return out
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(records)))) as pool:
        futures = {pool.submit(probe_project, r): r["id"] for r in records}
        for fut, pid in futures.items():
            try:
                out[pid] = fut.result()
            except Exception as exc:  # noqa: BLE001
                out[pid] = {"error": str(exc), "checked_at": time.time()}
    return out


# ─────────────────────────── 目录扫描 ───────────────────────────

SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "env", "site-packages",
    "windows", "program files", "program files (x86)", "programdata",
    "appdata", "$recycle.bin", "system volume information", "recovery",
    ".cache", ".git", ".idea", ".vscode", ".conda", "dist", "build", ".next",
    ".nuxt", "vendor", "target", "bin", "obj", "packages",
}


def scan_for_repos(root: str, max_depth: int = 3, limit: int = 500) -> list[dict]:
    """
    从 root 往下广度优先找「含 .git 的目录」。
    找到就记录并**不再往里钻**（仓库内部不会再有独立仓库，除非 submodule，通常也不需要）。
    """
    root = os.path.abspath(root)
    found: list[dict] = []
    queue: list[tuple[str, int]] = [(root, 0)]
    seen: set[str] = set()

    while queue and len(found) < limit:
        cur, depth = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except (PermissionError, OSError):
            continue

        if any(e.name == ".git" for e in entries):
            remote = read_remote_from_config(cur)
            found.append({
                "path": cur,
                "name": os.path.basename(cur) or cur,
                "remote": remote,
                "github": normalize_github(remote),
                "language": infer_language(cur),
            })
            continue

        if depth >= max_depth:
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            low = e.name.lower()
            if low in SKIP_DIRS or low.startswith("."):
                continue
            queue.append((e.path, depth + 1))

    found.sort(key=lambda x: x["path"].lower())
    return found


# ─────────────────────────── 数据操作 ───────────────────────────

def find_project(pid: str) -> dict | None:
    for p in _STORE["projects"]:
        if p.get("id") == pid:
            return p
    return None


def upsert_project(payload: dict, pid: str | None = None) -> dict:
    path = (payload.get("path") or "").strip().strip('"')
    if not path:
        raise ValueError("本地路径不能为空")
    path = os.path.normpath(path)
    github = normalize_github(payload.get("github"))

    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,，\s]+", tags) if t.strip()]

    with _LOCK:
        # 带了 id 就更新已有记录，否则新建
        target_id = pid or (payload.get("id") or "")
        rec = find_project(target_id) if target_id else None
        if rec is None:
            rec = {
                "id": uuid.uuid4().hex[:10],
                "created_at": now_iso(),
                "last_opened": None,
                "favorite": False,
            }
            _STORE["projects"].append(rec)

        rec["name"] = (payload.get("name") or os.path.basename(path) or path).strip()
        rec["path"] = path
        rec["github"] = github or ""
        rec["description"] = (payload.get("description") or "").strip()
        rec["tags"] = tags
        lang = (payload.get("language") or "").strip() if isinstance(payload.get("language"), str) else ""
        if not lang:
            lang = rec.get("language") or (infer_language(path) if os.path.isdir(path) else "")
        rec["language"] = str(lang).strip()
        if "favorite" in payload:
            rec["favorite"] = bool(payload["favorite"])
        save_store()
        _PROBE_CACHE.pop(rec["id"], None)
    return rec


def delete_project(pid: str) -> bool:
    with _LOCK:
        before = len(_STORE["projects"])
        _STORE["projects"] = [p for p in _STORE["projects"] if p.get("id") != pid]
        if len(_STORE["projects"]) != before:
            save_store()
            _PROBE_CACHE.pop(pid, None)
            return True
    return False


def state_payload(probe: bool = False) -> dict:
    with _LOCK:
        projects = [dict(p) for p in _STORE["projects"]]
        scan_roots = list(_STORE["scan_roots"])

    if probe:
        fresh = probe_many(projects)
        _PROBE_CACHE.update(fresh)

    items = []
    for p in projects:
        item = dict(p)
        item["status"] = _PROBE_CACHE.get(p["id"])
        items.append(item)

    # 汇总统计，前端顶部那排数字直接用它
    stats = {
        "total": len(items),
        "dirty": sum(1 for i in items if (i["status"] or {}).get("dirty")),
        "ahead": sum(1 for i in items if (i["status"] or {}).get("ahead", 0) > 0),
        "missing": sum(1 for i in items if i["status"] and not i["status"].get("exists")),
        "mismatch": sum(1 for i in items if (i["status"] or {}).get("match") is False),
        "linked": sum(1 for i in items if i.get("github")),
        "unlinked": sum(1 for i in items if not i.get("github")),
    }
    return {"projects": items, "stats": stats, "scan_roots": scan_roots, "web_dir": str(WEB_DIR)}


# ─────────────────────────── 执行外部动作 ───────────────────────────

def list_drives() -> list[str]:
    """枚举本机盘符（Windows 下枚举 A-Z，存在的留下）。"""
    if not IS_WIN:
        return ["/"]
    drives = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            if os.path.exists(root):
                drives.append(root)
        except OSError:
            continue
    return drives


def shortcuts_of() -> list[dict]:
    """目录选择器左侧那排快捷入口。"""
    out: list[dict] = []
    home = os.path.expanduser("~")
    for label, p in (
        ("主目录", home),
        ("桌面", os.path.join(home, "Desktop")),
        ("下载", os.path.join(home, "Downloads")),
        ("文档", os.path.join(home, "Documents")),
    ):
        if os.path.isdir(p):
            out.append({"label": label, "path": p})
    for r in _STORE.get("scan_roots", [])[:4]:
        if os.path.isdir(r):
            out.append({"label": os.path.basename(r.rstrip("\\/")) or r, "path": r})
    for d in list_drives():
        out.append({"label": d, "path": d})
    return out


def browse_dir(target: str = "") -> dict:
    """
    给界面里的「内置目录选择器」用：列出一个目录下的子文件夹。

    ── 为什么不用系统自带的文件夹对话框？
       那要让服务进程起一个 PowerShell 子进程去弹 WinForms 的 FolderBrowserDialog，
       而这个子进程还带着 CREATE_NO_WINDOW 标志 —— 它没有真正的宿主窗口，
       结果就是：对话框不显示、进程也不退出，前端按钮永远停在「弹窗中…」。
       顺便还会留下一堆僵尸 powershell.exe。
       内置浏览器不依赖任何外部 GUI，最省事也最稳。
    """
    if not target:
        # 空白起点 = 盘符列表
        return {
            "path": "",
            "parent": None,
            "is_drive_root": True,
            "self_repo": False,
            "self_github": "",
            "dirs": [{"name": d, "path": d, "is_repo": False} for d in list_drives()],
            "shortcuts": shortcuts_of(),
        }

    target = os.path.normpath(target)
    if not os.path.isdir(target):
        raise ValueError(f"目录不存在：{target}")

    parent = os.path.dirname(target)
    if parent == target or len(target.rstrip("\\/")) <= 2:
        parent = ""  # 已经在盘符根，再往上就是盘符列表

    dirs = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name in ("$Recycle.Bin", "System Volume Information", "Recovery", ".git"):
                    continue
                dirs.append({
                    "name": e.name,
                    "path": e.path,
                    "is_repo": os.path.exists(os.path.join(e.path, ".git")),
                })
    except PermissionError:
        raise ValueError("没有权限读取这个目录")
    except OSError as exc:
        raise ValueError(f"读取失败：{exc}")

    # Git 仓库排前面 —— 这个工具的用途就是找仓库，让它先露头
    dirs.sort(key=lambda x: (not x["is_repo"], x["name"].lower()))

    self_repo = os.path.exists(os.path.join(target, ".git"))
    remote = read_remote_from_config(target) if self_repo else None

    return {
        "path": target,
        "parent": parent,
        "is_drive_root": len(target.rstrip("\\/")) <= 2,
        "self_repo": self_repo,
        "self_github": normalize_github(remote) or "",
        "self_language": infer_language(target) if self_repo else "",
        "dirs": dirs[:600],
        "shortcuts": shortcuts_of(),
    }


def open_target(record: dict, target: str) -> tuple[bool, str]:
    path = record.get("path") or ""
    github = record.get("github") or ""

    if target == "folder":
        if not os.path.isdir(path):
            return False, "本地路径不存在"
        os.startfile(path)  # noqa: S606  Windows 专用，打开资源管理器
        return True, "已打开文件夹"

    if target == "github":
        if not github:
            return False, "还没关联 GitHub 仓库"
        webbrowser.open(github)
        return True, "已打开 GitHub"

    if target == "vscode":
        if not os.path.isdir(path):
            return False, "本地路径不存在"
        try:
            subprocess.Popen(f'code "{path}"', shell=True, **NO_WINDOW)
            return True, "已在 VS Code 打开"
        except Exception as exc:  # noqa: BLE001
            return False, f"VS Code 启动失败：{exc}"

    if target == "terminal":
        if not os.path.isdir(path):
            return False, "本地路径不存在"
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "cmd", "/k", f"cd /d {path}"],
                shell=False, **NO_WINDOW,
            )
            return True, "已打开命令行"
        except Exception as exc:  # noqa: BLE001
            return False, f"命令行启动失败：{exc}"

    if target == "gitbash":
        if not os.path.isdir(path):
            return False, "本地路径不存在"
        for cand in (
            r"C:\Program Files\Git\git-bash.exe",
            r"C:\Program Files (x86)\Git\git-bash.exe",
        ):
            if os.path.isfile(cand):
                subprocess.Popen([cand, f"--cd={path}"], **NO_WINDOW)
                return True, "已打开 Git Bash"
        return False, "没找到 git-bash.exe"

    return False, f"未知操作：{target}"


# ─────────────────────────── HTTP 服务 ───────────────────────────

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectHub/1.0"
    protocol_version = "HTTP/1.1"

    # —— 基础响应工具 ——
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _ok(self, data=None, msg: str = "") -> None:
        self._json({"ok": True, "data": data, "msg": msg})

    def _fail(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def log_message(self, fmt, *args):  # 保持控制台干净
        pass

    # —— 路由 ——
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_get(path)
        return self._static(path)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        return self._api_post(path)

    def do_PATCH(self):  # noqa: N802
        return self.do_POST(True)

    def do_DELETE(self):  # noqa: N802
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/projects/([0-9a-f]+)", path)
        if m:
            ok = delete_project(m.group(1))
            return self._ok({"deleted": ok}) if ok else self._fail("项目不存在", 404)
        return self._fail("未知接口", 404)

    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        # 防目录穿越：确保解析出来的路径还在 web 目录里
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._send(404, b"Not Found", "text/plain; charset=utf-8")
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _api_get(self, path: str) -> None:
        if path == "/api/state":
            probe = "probe=1" in (self.path.split("?", 1) + [""])[1]
            return self._ok(state_payload(probe=probe))
        if path == "/api/ping":
            return self._ok({"app": APP_NAME, "pid": os.getpid()})
        return self._fail("未知接口", 404)

    def _api_post(self, path: str, patched: bool = False) -> None:
        payload = self._body()

        # 新增 / 编辑
        if path == "/api/projects":
            try:
                rec = upsert_project(payload)
                return self._ok(rec, "已保存")
            except ValueError as exc:
                return self._fail(str(exc))

        m = re.fullmatch(r"/api/projects/([0-9a-f]+)", path)
        if m:
            if not find_project(m.group(1)):
                return self._fail("项目不存在", 404)
            try:
                return self._ok(upsert_project(payload, m.group(1)), "已更新")
            except ValueError as exc:
                return self._fail(str(exc))

        m = re.fullmatch(r"/api/projects/([0-9a-f]+)/probe", path)
        if m:
            rec = find_project(m.group(1))
            if not rec:
                return self._fail("项目不存在", 404)
            st = probe_project(rec)
            _PROBE_CACHE[rec["id"]] = st
            return self._ok(st)

        if path == "/api/probe-all":
            return self._ok(state_payload(probe=True))

        if path == "/api/scan":
            root = (payload.get("root") or "").strip()
            if not root or not os.path.isdir(root):
                return self._fail("目录不存在：" + root)
            depth = int(payload.get("depth") or 3)
            found = scan_for_repos(root, max_depth=max(1, min(depth, 6)))
            with _LOCK:
                roots = _STORE.setdefault("scan_roots", [])
                if root not in roots:
                    roots.insert(0, root)
                    del roots[8:]  # 最多记住 8 个常用根目录
                save_store()
            known = {os.path.normpath(p.get("path", "")).lower() for p in _STORE["projects"]}
            for f in found:
                f["imported"] = os.path.normpath(f["path"]).lower() in known
            return self._ok({"root": root, "items": found})

        if path == "/api/scan/import":
            items = payload.get("items") or []
            added = 0
            for it in items:
                try:
                    upsert_project({
                        "path": it.get("path"),
                        "name": it.get("name"),
                        "github": it.get("github"),
                        "language": it.get("language"),
                        "tags": it.get("tags") or [],
                    })
                    added += 1
                except ValueError:
                    continue
            return self._ok({"added": added}, f"已导入 {added} 个项目")

        if path == "/api/pick-folder":
            # 旧接口：系统对话框，已废弃（会卡死，见 browse_dir 里的说明）。保留空返回以免老页面报错。
            return self._fail("请改用界面里的目录选择器", 410)

        if path == "/api/browse":
            try:
                return self._ok(browse_dir((payload.get("path") or "").strip()))
            except ValueError as exc:
                return self._fail(str(exc))

        if path == "/api/open":
            rec = find_project(payload.get("id") or "")
            if not rec:
                return self._fail("项目不存在", 404)
            ok, msg = open_target(rec, payload.get("target") or "folder")
            if ok:
                rec["last_opened"] = now_iso()
                with _LOCK:
                    save_store()
            return self._ok({"done": ok}, msg) if ok else self._fail(msg)

        return self._fail("未知接口", 404)


# ─────────────────────────── 启动 ───────────────────────────

def port_in_use(port: int) -> bool:
    """短超时探一下端口有没有人在监听（本机探测，300ms 足够）。"""
    try:
        with socket.create_connection((HOST, port), timeout=0.3):
            return True
    except OSError:
        return False


def pick_port() -> int:
    for port in PORT_CANDIDATES:
        if port_in_use(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if IS_WIN:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit("18800-18839 全被占用，请检查是不是开了太多实例。")


def open_window(url: str) -> None:
    """优先用 Edge 的 --app 模式：没有地址栏、没有标签页，看起来就是个独立软件。"""
    if IS_WIN:
        for exe in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        ):
            if os.path.isfile(exe):
                try:
                    subprocess.Popen(
                        [exe, f"--app={url}", "--window-size=1360,900"],
                        **NO_WINDOW,
                    )
                    return
                except Exception:  # noqa: BLE001
                    break
    webbrowser.open(url)


def main() -> None:
    load_store()

    # 已有实例在跑？直接再开一个窗口，不再起新服务
    if RUNTIME_FILE.exists():
        try:
            info = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
            old_port = int(info.get("port") or 0)
            if old_port and port_in_use(old_port):
                url = f"http://{HOST}:{old_port}/"
                print(f"{APP_NAME} 已在运行：{url}")
                open_window(url)
                return
        except Exception:  # noqa: BLE001
            pass

    port = pick_port()
    url = f"http://{HOST}:{port}/"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_FILE.write_text(
        json.dumps({"port": port, "pid": os.getpid(), "started_at": now_iso()}),
        encoding="utf-8",
    )

    httpd = ThreadingHTTPServer((HOST, port), Handler)
    httpd.daemon_threads = True

    print("=" * 56)
    print(f"  {APP_NAME} 已启动")
    print(f"  地址：{url}")
    print(f"  数据：{STORE_FILE}")
    print("  关掉这个黑窗口即退出（或按 Ctrl+C）")
    print("=" * 56)

    # 后台预热：先把已有项目的状态探一遍，界面打开时就是热数据
    def warm():
        time.sleep(0.4)
        with _LOCK:
            recs = [dict(p) for p in _STORE["projects"]]
        if recs:
            _PROBE_CACHE.update(probe_many(recs))
            print(f"[ready] 已预检 {len(recs)} 个项目")

    threading.Thread(target=warm, daemon=True).start()
    if AUTO_OPEN:
        threading.Thread(target=lambda: (time.sleep(0.6), open_window(url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见 🌿")
    finally:
        httpd.server_close()
        try:
            RUNTIME_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
