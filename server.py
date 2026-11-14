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

import ctypes
import json
import os
import re
import shutil
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


# ─────────────────────── Git 写操作：提交 / 推送 / 拉取 ───────────────────────
#
# 这一段的每处「多此一举」都是有原因的，改之前先看注释：
#
# 1. 所有联网命令都设了 GIT_TERMINAL_PROMPT=0。git 需要账号密码时会去读终端，
#    而我们的进程根本没有终端 —— 它会一直等下去，接口就永远不返回。
#    设成 0 后它立刻失败返回，我们就能把「需要凭据」翻译成人话告诉用户。
# 2. 提交/推送必须拿到 stderr 才能说清失败原因（_git() 只回 stdout，不够用）。
# 3. 文件列表不信任前端传来的内容，一律先用 git status 重新读一遍再求交集，
#    否则一个构造出来的 "../../.ssh" 就能让 git add 到仓库外面去。

# porcelain 状态字符 → 中文说明
_ST_KIND = {
    "M": "修改",
    "A": "新增",
    "D": "删除",
    "R": "重命名",
    "C": "复制",
    "T": "类型变更",
    "U": "冲突",
    "?": "未跟踪",
}


def _git_env() -> dict:
    """给 git 子进程准备环境：禁用一切交互式等待。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"   # 不要问凭据，直接失败
    env["GIT_EDITOR"] = "true"         # 万不得已要拉编辑器时直接放行，别卡住
    env["GIT_SEQUENCE_EDITOR"] = "true"
    env["GIT_PAGER"] = "cat"
    env.pop("GIT_ASKPASS", None)
    return env


def _git_run(repo: str, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    """执行 git，返回 (返回码, stdout, stderr)。超时返回 124。"""
    if not repo or not os.path.isdir(repo):
        return 127, "", "路径不存在"
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, timeout=timeout, env=_git_env(), **NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超过 {timeout} 秒没返回"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


_GH_HELPER: list[str] | None = None
_GH_CHECKED = False


def _gh_helper_args() -> list[str]:
    """
    借用 gh CLI 里已经存好的 token 当凭据，**只对这一次命令生效**。

    为什么不干脆帮用户跑 `gh auth setup-git`：那会写进他的全局 ~/.gitconfig，
    而我们只是他机器上的一个工具，没理由替他改全局配置。
    找不到 gh 或没登录时返回空列表，git 就退回默认行为。
    """
    global _GH_HELPER, _GH_CHECKED
    if _GH_CHECKED:
        return _GH_HELPER or []
    _GH_CHECKED = True

    gh = shutil.which("gh")
    if not gh:
        for cand in (
            r"C:\Program Files\GitHub CLI\gh.exe",
            r"C:\Program Files (x86)\GitHub CLI\gh.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe"),
        ):
            if os.path.isfile(cand):
                gh = cand
                break
    if not gh:
        return []

    # 路径里有空格，必须用单引号裹住 —— 这段字符串会被 git 交给 sh 执行
    posix = gh.replace("\\", "/")
    _GH_HELPER = [
        "-c", "credential.helper=",
        "-c", f"credential.https://github.com.helper=!'{posix}' auth git-credential",
    ]
    return _GH_HELPER


def _tail(text: str, lines: int = 2) -> str:
    """
    取错误信息最后几行 —— git 的真正原因通常在末尾。

    用空格而不是换行拼：这段话最终会进 toast，而 toast 是单行的，
    换行符在那里会变成一块糊在一起的文字。
    """
    body = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return " ".join(body[-lines:]) if body else ""


def _friendly_git_error(text: str) -> str:
    """把 git 的英文报错翻成能照着做的中文。"""
    raw = _tail(text)
    low = (text or "").lower()
    if any(k in low for k in ("could not read username", "terminal prompts disabled",
                              "authentication failed", "invalid username or password")):
        return "需要 GitHub 凭据。在终端执行一次 gh auth setup-git，或配置 credential.helper 后再试。"
    if any(k in low for k in ("could not resolve host", "connection timed out", "failed to connect",
                              "network is unreachable", "connection reset", "operation timed out")):
        return "连不上 GitHub。检查网络，或确认代理可用（git config --global http.proxy）。"
    if "repository not found" in low or "does not appear to be a git repository" in low:
        return "GitHub 上找不到这个仓库。确认登记地址有没有写错、仓库是否已删除，或当前账号有没有访问权限。"
    if "nothing to commit" in low or "no changes added" in low:
        return "选中的文件没有可提交的改动"
    if "please tell me who you are" in low or "unable to auto-detect email" in low:
        return "还没配置 Git 用户名/邮箱：git config --global user.name \"你的名字\""
    if "unmerged" in low or "needs merge" in low:
        return "有文件处于冲突未解决状态，先在编辑器里解决冲突再提交"
    if any(k in low for k in ("rejected", "non-fast-forward", "fetch first", "updates were rejected")):
        return "远程有你本地没有的提交，先点「拉取」再推送"
    if "permission denied" in low or "403" in low:
        return "没有推送权限，确认这个仓库属于当前 GitHub 账号"
    if "not a git repository" in low:
        return "这个目录不是 Git 仓库"
    return (raw[:200] + "…") if len(raw) > 200 else (raw or "未知错误")


def _parse_status_z(raw: str) -> list[tuple[str, str, bool]]:
    """
    解析 `git status --porcelain -z` 的输出，返回 [(状态字符, 路径, 是否已暂存)]。

    为什么用 -z：默认输出会把中文路径转义成 "\\344\\270\\255" 这种八进制，
    既难显示也没法直接拿去 git add。-z 用 \\0 分隔，原样给 UTF-8，一步到位。

    状态码是两列：第一列=暂存区相对上次提交，第二列=工作区相对暂存区。
    ` M` 是"改了没 stage"，`M ` 是"stage 了没 commit" —— 别混。
    """
    out: list[tuple[str, str, bool]] = []
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        ent = parts[i]
        i += 1
        if not ent or len(ent) < 4:
            continue
        xy, fp = ent[:2], ent[3:]
        # 重命名/复制会多跟一个原始路径，跳过它（我们加了 --no-renames，正常不会出现）
        if xy[0] in "RC" or xy[1] in "RC":
            i += 1
        if xy == "??":
            out.append(("?", fp, False))
        elif "U" in xy or xy in ("AA", "DD"):
            out.append(("U", fp, True))
        else:
            ch = xy[0] if xy[0] != " " else xy[1]
            out.append((ch if ch in _ST_KIND else "M", fp, xy[0] != " "))
    return out


def _numstat(repo: str, *args: str) -> dict[str, tuple[int, int]]:
    """取每个文件的新增/删除行数。--no-renames 让 -z 的格式变简单，重命名按删+增算。"""
    code, out, _ = _git_run(repo, "diff", "--numstat", "-z", "--no-renames", *args, timeout=25)
    res: dict[str, tuple[int, int]] = {}
    if code != 0:
        return res
    for rec in out.split("\0"):
        if not rec:
            continue
        seg = rec.split("\t")
        if len(seg) < 3:
            continue
        add, dele, fp = seg[0], seg[1], seg[2]
        # 二进制文件的增删是 "-"，按 0 算
        res[fp] = (int(add) if add.isdigit() else 0, int(dele) if dele.isdigit() else 0)
    return res


def _count_new_lines(path: str, limit: int = 512 * 1024) -> int:
    """
    数一个未跟踪文件有多少行，用于显示 "+N"。

    git diff 看不到未跟踪文件，所以这部分得自己数。超过 512KB 或看着像二进制
    就返回 0 —— 面板是用来扫一眼的，不值得为一个 20MB 的日志卡住三秒。
    """
    try:
        if os.path.getsize(path) > limit:
            return 0
        with open(path, "rb") as f:
            head = f.read(8192)
            if b"\0" in head:
                return 0
            f.seek(0)
            return sum(1 for _ in f)
    except OSError:
        return 0


def git_detail(record: dict) -> dict:
    """
    打开提交面板时要的全部信息：改动文件、行数、分支、领先/落后、最近提交。
    一次请求凑齐，避免前端连打好几个接口。
    """
    path = (record.get("path") or "").strip()
    info: dict = {
        "is_git": False,
        "branch": None,
        "detached": False,
        "has_upstream": False,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "remote": None,
        "files": [],
        "stat": {"files": 0, "add": 0, "del": 0},
        "recent": [],
        "identity": {"name": "", "email": ""},
        "error": None,
    }

    if not path or not os.path.isdir(path):
        info["error"] = "本地路径不存在"
        return info
    if not os.path.exists(os.path.join(path, ".git")):
        info["error"] = "这个目录还不是 Git 仓库"
        return info
    info["is_git"] = True

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=8) or ""
    info["branch"] = branch or None
    info["detached"] = branch == "HEAD"

    info["upstream"] = _git(path, "rev-parse", "--abbrev-ref", "@{u}", timeout=8) or None
    info["has_upstream"] = bool(info["upstream"])
    counts = _git(path, "rev-list", "--left-right", "--count", "@{u}...HEAD", timeout=10)
    if counts:
        parts = counts.split()
        if len(parts) == 2:
            info["behind"], info["ahead"] = int(parts[0]), int(parts[1])

    info["remote"] = _git(path, "config", "--get", "remote.origin.url", timeout=6) \
        or read_remote_from_config(path)

    info["identity"] = {
        "name": _git(path, "config", "user.name", timeout=6) or "",
        "email": _git(path, "config", "user.email", timeout=6) or "",
    }

    # ── 改动文件 ──
    code, out, err = _git_run(
        path, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames", timeout=25)
    if code != 0:
        info["error"] = _friendly_git_error(err or out)
        return info

    pairs = _parse_status_z(out)
    work = _numstat(path)              # 工作区 vs 暂存区
    staged = _numstat(path, "--cached")  # 暂存区 vs 上次提交

    files = []
    total_add = total_del = 0
    for kind, fp, is_staged in pairs:
        if kind == "?":
            # 未跟踪文件不在 diff 里，行数自己数
            add, dele = _count_new_lines(os.path.join(path, fp)), 0
        else:
            a1, d1 = work.get(fp, (0, 0))
            a2, d2 = staged.get(fp, (0, 0))
            add, dele = a1 + a2, d1 + d2
        total_add += add
        total_del += dele
        files.append({
            "path": fp,
            "kind": kind,
            "label": _ST_KIND.get(kind, "改动"),
            "add": add,
            "del": dele,
            "untracked": kind == "?",
            "staged": is_staged,
        })
    # 有冲突的排最前，其余按路径，扫一眼就知道哪些要先处理
    files.sort(key=lambda f: (0 if f["kind"] == "U" else 1, f["path"].lower()))
    info["files"] = files
    info["stat"] = {"files": len(files), "add": total_add, "del": total_del}

    # ── 最近提交，写提交信息时可以照着风格来 ──
    code, out, _ = _git_run(path, "log", "-6", "--format=%h%x1f%cI%x1f%an%x1f%s", timeout=10)
    if code == 0 and out:
        recent = []
        for line in out.splitlines():
            seg = line.split("\x1f")
            if len(seg) >= 4:
                recent.append({"hash": seg[0], "at": seg[1], "author": seg[2], "msg": seg[3]})
        info["recent"] = recent

    return info


def git_commit(record: dict, message: str, files=None, push: bool = False) -> dict:
    """
    提交（可选顺带推送）。

    勾选语义：勾了哪些文件，这次就只提交哪些。
    为此在「只勾了一部分」时会先 `git reset` 清空暂存区 —— 否则你手动 stage 过的
    其他文件会被一起带进这次提交。reset 只动暂存区不动工作区，文件内容不会丢。
    """
    path = (record.get("path") or "").strip()
    msg = (message or "").strip()
    if not msg:
        return {"ok": False, "error": "先写一句提交信息"}
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": "本地路径不存在"}

    detail = git_detail(record)
    if not detail["is_git"]:
        return {"ok": False, "error": detail["error"] or "不是 Git 仓库"}

    existing = {f["path"] for f in detail["files"]}
    if isinstance(files, list) and files:
        picked = {str(f) for f in files} & existing   # 只认仓库里真实存在的改动
    else:
        picked = set(existing)
    if not picked:
        return {"ok": False, "error": "没有可提交的改动"}

    if len(picked) < len(existing):
        # 部分提交：清空暂存区，保证「勾了什么就提交什么」。首次提交时没有 HEAD，
        # reset 会报错，忽略即可（那时暂存区本来就是空的）。
        _git_run(path, "reset", "-q", "--mixed", timeout=25)

    if len(picked) == len(existing):
        code, out, err = _git_run(path, "add", "-A", timeout=90)
    else:
        # `--` 明确告诉 git 后面都是路径，防止文件名被当成选项
        code, out, err = _git_run(path, "add", "--", *sorted(picked), timeout=90)
    if code != 0:
        return {"ok": False, "error": "暂存失败：" + _friendly_git_error(err or out)}

    code, out, err = _git_run(path, "commit", "-m", msg, timeout=180)
    if code != 0:
        return {"ok": False, "error": _friendly_git_error(err or out)}

    # 提交成功。如果还要推送，就接着推 —— 推失败时提交是留着的，要如实告诉用户
    result = {"ok": True, "msg": f"已提交 {len(picked)} 个文件", "error": "", "pushed": False}
    if push:
        pr = git_push(record)
        result["pushed"] = pr["ok"]
        if pr["ok"]:
            result["msg"] = f"已提交并推送 {len(picked)} 个文件"
        else:
            result["error"] = "提交成功，但推送失败：" + pr["error"]
    result["detail"] = git_detail(record)
    return result


def git_push(record: dict) -> dict:
    """推送到远程。没有 upstream 时自动 -u origin <当前分支>。"""
    path = (record.get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": "本地路径不存在"}

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=8) or ""
    if not branch:
        return {"ok": False, "error": "读不到当前分支"}
    if branch == "HEAD":
        return {"ok": False, "error": "当前处于游离 HEAD 状态，没有可推送的分支"}

    has_up = bool(_git(path, "rev-parse", "--abbrev-ref", "@{u}", timeout=8))
    args = ["push"] if has_up else ["push", "-u", "origin", branch]

    # 180 秒：走代理的大仓库首次推送可能要一会儿，但也不能无限等
    code, out, err = _git_run(path, *_gh_helper_args(), *args, timeout=180)
    if code == 0:
        up_to_date = any(
            k in (out + err) for k in ("Everything up-to-date", "Everything up to date"))
        if up_to_date:
            result = {"ok": True, "msg": "没有需要推送的提交", "error": ""}
        else:
            result = {
                "ok": True,
                "msg": f"已推送到 {branch}" + ("" if has_up else "（已设为上游）"),
                "error": "",
            }
    else:
        result = {"ok": False, "error": _friendly_git_error(err or out)}
    result["detail"] = git_detail(record)
    return result


def git_pull(record: dict) -> dict:
    """
    拉取远程更新。只做快进（--ff-only）：
    真需要合并时交给用户在编辑器里处理，这里要是自动 merge，出了冲突
    就会留下一堆 <<<<<<< 在文件里，界面又帮不上忙，反而更糟。
    """
    path = (record.get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": "本地路径不存在"}
    if not _git(path, "rev-parse", "--abbrev-ref", "@{u}", timeout=8):
        return {"ok": False, "error": "这个分支还没有上游，先推送一次建立跟踪关系"}

    code, out, err = _git_run(path, *_gh_helper_args(), "pull", "--ff-only", timeout=180)
    if code != 0:
        raw = err or out
        if "not possible to fast-forward" in raw.lower() or "diverging" in raw.lower():
            why = "本地和远程各有新提交，不能自动快进，需要在终端手动合并"
        else:
            why = _friendly_git_error(raw)
        result = {"ok": False, "error": why}
    else:
        changed = "已经是最新的" if "Already up to date" in (out + err) or "Already up-to-date" in (out + err) \
            else "已拉取更新"
        result = {"ok": True, "msg": changed, "error": ""}
    result["detail"] = git_detail(record)
    return result


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


# ────────────────────── 把新开的窗口顶到最前 ──────────────────────
#
# 为什么需要这一段：
# 我们是**后台服务进程**，从它启动 explorer.exe / VS Code 时，Windows 的「前台锁」
# （foreground lock）不允许非前台进程抢焦点 —— 窗口其实开出来了，但排在浏览器窗口
# 下面，只在任务栏闪一下。这不是 explorer 的问题，任何后台进程启动 GUI 都这样。
# 做法：等窗口真的出现后，用 SetWindowPos 先钉成 TOPMOST 再撤销（这一步不受前台锁
# 限制），窗口就落到 z 序最上层；想连焦点一起给，再补一次模拟 Alt 键 —— 系统认定
# "刚有用户输入"才会放行 SetForegroundWindow。

EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")   # 资源管理器
VSCODE_CLASSES = ("Chrome_WidgetWin_1",)                # VS Code 是 Electron，类名跟 Chrome 一样

_ENUM_PROC = None   # 回调类型缓存，别每次调用都重建


def _user32():
    """取一个声明好原型的 user32 —— 不声明的话 64 位下 HWND 会被截成 32 位。"""
    u = ctypes.windll.user32
    if getattr(u, "_ph_typed", False):
        return u
    P, I, U = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
    W = ctypes.POINTER(ctypes.c_wchar)
    u.SetWindowPos.argtypes = [P, P, I, I, I, I, U]
    u.SetForegroundWindow.argtypes = [P]
    u.SetForegroundWindow.restype = ctypes.c_int
    u.GetForegroundWindow.restype = P
    u.IsIconic.argtypes = [P]
    u.IsIconic.restype = ctypes.c_int
    u.ShowWindow.argtypes = [P, I]
    u.GetClassNameW.argtypes = [P, W, I]
    u.GetWindowTextW.argtypes = [P, W, I]
    u.BringWindowToTop.argtypes = [P]
    u.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, U, P]
    u._ph_typed = True
    return u


def _windows_by_class(classes: tuple[str, ...]) -> dict[int, str]:
    """枚举当前所有指定类名的顶层窗口 → {hwnd: 窗口标题}"""
    global _ENUM_PROC
    if not IS_WIN:
        return {}
    if _ENUM_PROC is None:
        _ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u = _user32()
    found: dict[int, str] = {}

    def cb(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(128)
        if u.GetClassNameW(hwnd, cls, 128) and cls.value in classes:
            title = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, title, 512)
            found[int(hwnd or 0)] = title.value
        return True

    u.EnumWindows(_ENUM_PROC(cb), 0)
    return found


def _raise_window(hwnd: int) -> None:
    """把某个窗口提到最前，并尽量把焦点也给它。失败也只是"窗口在后面"，不影响功能。"""
    if not IS_WIN or not hwnd:
        return
    u = _user32()
    SW_RESTORE = 9
    HWND_TOPMOST, HWND_NOTOPMOST = ctypes.c_void_p(-1), ctypes.c_void_p(-2)
    flags = 0x0002 | 0x0001 | 0x0010 | 0x0040  # NOMOVE | NOSIZE | NOACTIVATE | SHOWWINDOW
    h = ctypes.c_void_p(hwnd)
    try:
        if u.IsIconic(h):
            u.ShowWindow(h, SW_RESTORE)
        # 钉成置顶再撤销：绕开前台锁，窗口稳稳落到 z 序最上层
        u.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, flags)
        u.SetWindowPos(h, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        u.BringWindowToTop(h)
        if u.GetForegroundWindow() != hwnd:
            u.SetForegroundWindow(h)
        if u.GetForegroundWindow() != hwnd:
            # 还抢不到焦点：模拟一次 Alt（单独按 Alt 不触发任何操作），
            # 让系统认为刚发生过用户输入，前台锁才会松手
            VK_MENU, KEYUP = 0x12, 0x0002
            u.keybd_event(VK_MENU, 0, 0, None)
            u.keybd_event(VK_MENU, 0, KEYUP, None)
            u.SetForegroundWindow(h)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 窗口置顶失败：{exc}")


def promote_window_later(
    classes: tuple[str, ...], title_hint: str = "",
    before: dict[int, str] | None = None, timeout: float = 4.0,
) -> None:
    """
    等新窗口真的创建出来，再把它顶到最前。

    必须异步：explorer 从启动到窗口出现有几百毫秒延迟，同步等会把 HTTP 响应拖住。
    优先认「启动前不存在、现在新出现」的窗口；找不到再按标题匹配兜底
    （explorer 设了「在同一窗口打开」时会复用旧窗口，那种情况没有新窗口）。
    """
    if not IS_WIN:
        return
    base = before or {}
    hint = (title_hint or "").strip().lower()

    def work() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.1)
            now = _windows_by_class(classes)
            fresh = [h for h in now if h not in base]
            if fresh:
                _raise_window(fresh[0])
                return
            if hint:
                hits = [h for h, t in now.items() if hint in t.lower()]
                if hits:
                    _raise_window(hits[0])
                    return

    threading.Thread(target=work, daemon=True).start()


def open_target(record: dict, target: str) -> tuple[bool, str]:
    path = record.get("path") or ""
    github = record.get("github") or ""

    if target == "folder":
        if not os.path.isdir(path):
            return False, "本地路径不存在"
        before = _windows_by_class(EXPLORER_CLASSES)
        os.startfile(path)  # noqa: S606  Windows 专用，打开资源管理器
        promote_window_later(EXPLORER_CLASSES, os.path.basename(os.path.normpath(path)), before)
        return True, "已打开文件夹"

    if target == "github":
        if not github:
            return False, "还没关联 GitHub 仓库"
        webbrowser.open(github)
        return True, "已打开 GitHub"

    if target == "vscode":
        if not os.path.isdir(path):
            return False, "本地路径不存在"
        before = _windows_by_class(VSCODE_CLASSES)
        try:
            subprocess.Popen(f'code "{path}"', shell=True, **NO_WINDOW)
            # 只按标题里的 "Visual Studio Code" 认窗口 —— 拿项目名去匹配的话，
            # 浏览器（同为 Chrome_WidgetWin_1）的标签页标题也会被误伤
            promote_window_later(VSCODE_CLASSES, "visual studio code", before, timeout=8.0)
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
        m = re.fullmatch(r"/api/projects/([0-9a-f]+)/git", path)
        if m:
            rec = find_project(m.group(1))
            if not rec:
                return self._fail("项目不存在", 404)
            return self._ok(git_detail(rec))
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

        # 提交 / 推送 / 拉取：一个入口，用 action 区分
        m = re.fullmatch(r"/api/projects/([0-9a-f]+)/git", path)
        if m:
            rec = find_project(m.group(1))
            if not rec:
                return self._fail("项目不存在", 404)
            action = (payload.get("action") or "").strip()
            if action == "detail":
                return self._ok(git_detail(rec))
            if action == "commit":
                res = git_commit(
                    rec,
                    payload.get("message") or "",
                    payload.get("files"),
                    bool(payload.get("push")),
                )
            elif action == "push":
                res = git_push(rec)
            elif action == "pull":
                res = git_pull(rec)
            else:
                return self._fail("未知操作：" + (action or "（空）"))

            # 状态变了，顺手刷新缓存 —— 卡片上的徽章不用等下一次全量探测
            _PROBE_CACHE[rec["id"]] = probe_project(rec)
            if not res.get("ok"):
                return self._fail(res.get("error") or "操作失败")
            # 注意：提交成功但推送失败时 ok=True 而 error 有值，前端要能看到这段
            return self._ok(res, res.get("msg") or "")

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
