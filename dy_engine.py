# -*- coding: utf-8 -*-
"""
抖音视频下载引擎（原生 CDP，无 Playwright 依赖）

工作方式：
  1. 用 --remote-debugging-port 启动本机 Chrome/Edge，打开视频页
  2. 直连 CDP，订阅 Network.responseReceived，收集播放器实际拉取的媒体地址
  3. 人工完成登录/验证后，播放器开始拉流，地址即被捕获
  4. 下载各条流，用 ffmpeg 探测轨道类型，合成带声音的 mp4

全程不调用抖音 API，因此不受 a_bogus / uifid 签名风控影响；
不设置系统代理、不安装根证书。

依赖：websocket-client（纯 Python）；ffmpeg.exe（同级目录或 PATH）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

import websocket

# ---------- 常量 ----------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MEDIA_PAT = re.compile(r"(douyinvod|zjcdn|bytecdn|amemv|\.mp4|/video/tos/|/play/)", re.I)
BAD_PAT = re.compile(r"(\.jpe?g|\.png|\.webp|\.gif|\.ico|\.css|\.js(\?|$)|douyinpic)", re.I)

MIN_BYTES = 100_000   # 小于此体积不视为主流
SETTLE_SEC = 20       # 发现首条流后再多收集这么久，等另一条轨出现
CHUNK = 256 * 1024

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# ---------- 小工具 ----------

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def safe_name(s: str, fallback: str = "douyin_video") -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*抖音\s*$", "", s).strip()
    return s[:70] or fallback


def res_dir() -> str:
    """只读资源目录：打包后为 PyInstaller 解压目录，否则为脚本目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return base or os.path.dirname(os.path.abspath(__file__))


def app_dir() -> str:
    """可写目录：打包后为 exe 所在目录，否则为脚本目录。

    浏览器配置、配置文件必须落在这里——_MEIPASS 每次运行都会被清掉。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_chrome(explicit: str | None = None) -> str | None:
    if explicit and os.path.exists(explicit):
        return explicit
    for p in CHROME_PATHS:
        if p and os.path.exists(p):
            return p
    return shutil.which("chrome") or shutil.which("msedge")


def find_ffmpeg(explicit: str | None = None) -> str | None:
    """查找顺序：显式指定 -> 脚本同级 -> PATH -> imageio-ffmpeg。"""
    if explicit and os.path.exists(explicit):
        return explicit
    # app_dir 覆盖「exe 同级放一个 ffmpeg.exe」；res_dir 覆盖「打包时内嵌」
    for base in (app_dir(), res_dir()):
        for name in ("ffmpeg.exe", "ffmpeg"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        return p if os.path.exists(p) else None
    except Exception:
        return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def resolve_url(url: str, timeout: int = 20) -> str:
    """把 v.douyin.com 短链解析成 www.douyin.com/video/<id>。"""
    url = url.strip()
    m = re.search(r"https?://\S+", url)
    if m:
        url = m.group(0)
    if "v.douyin.com" not in url:
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
        vid = re.search(r"/video/(\d+)", final) or re.search(r"(\d{15,})", final)
        if vid:
            return f"https://www.douyin.com/video/{vid.group(1)}"
        return final
    except Exception:
        return url


# ---------- 媒体候选 ----------

@dataclass
class Stream:
    url: str
    size: int = 0
    mime: str = ""


class MediaCollector:
    """同一基址只保留体积最大的一条。"""

    def __init__(self) -> None:
        self._items: dict[str, Stream] = {}
        self._lock = threading.Lock()
        self.on_new: Callable[[Stream], None] | None = None

    def add(self, url: str, size: int = 0, mime: str = "") -> None:
        if not url or url.startswith(("blob:", "data:")) or BAD_PAT.search(url):
            return
        if not (mime.startswith(("video/", "audio/")) or MEDIA_PAT.search(url)):
            return
        key = url.split("?")[0]
        with self._lock:
            cur = self._items.get(key)
            if cur is not None and size <= cur.size:
                return
            fresh = cur is None
            self._items[key] = Stream(url, size, mime)
        if fresh and self.on_new:
            self.on_new(self._items[key])

    def streams(self) -> list[Stream]:
        with self._lock:
            pool = [
                s for s in self._items.values()
                if s.size >= MIN_BYTES or re.search(r"(douyinvod|/video/tos/)", s.url, re.I)
            ]
        return sorted(pool, key=lambda s: -s.size)

    def all(self) -> list[Stream]:
        with self._lock:
            return sorted(self._items.values(), key=lambda s: -s.size)


# ---------- CDP 客户端 ----------

class ChromeCDP:
    """启动浏览器并直连 CDP，订阅所有页面的网络响应事件。"""

    def __init__(self, chrome: str, profile_dir: str, log: Callable[[str], None]) -> None:
        self.chrome = chrome
        self.profile_dir = profile_dir
        self.log = log
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.ws: websocket.WebSocket | None = None
        self._id = 0
        self._id_lock = threading.Lock()
        self._results: dict[int, Any] = {}
        self._results_ev: dict[int, threading.Event] = {}
        self._sessions: set[str] = set()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self.collector = MediaCollector()
        self.page_title = ""

    # -- 进程 --

    def start(self, url: str) -> None:
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [
            self.chrome,
            f"--remote-debugging-port={self.port}",
            # Chrome 111+ 会校验 CDP WebSocket 的 Origin 头，不加这个会 403
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
            url,
        ]
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.log(f"[*] 已启动 {os.path.basename(self.chrome)}（调试端口 {self.port}）")
        self._wait_ready()

    def _wait_ready(self, timeout: int = 30) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=2
                ) as r:
                    info = json.loads(r.read().decode("utf-8"))
                ws_url = info.get("webSocketDebuggerUrl")
                if ws_url:
                    self.ws = websocket.create_connection(
                        ws_url, timeout=5, max_size=64 * 1024 * 1024
                    )
                    self.ws.settimeout(1.0)
                    self.log("[*] CDP 已连接")
                    return
            except Exception as e:
                last = e
            time.sleep(0.4)
        raise RuntimeError(f"连接 CDP 超时: {last}")

    # -- 协议 --

    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def send(self, method: str, params: dict | None = None,
             session: str | None = None, wait: bool = False, timeout: float = 10.0):
        assert self.ws is not None
        mid = self._next_id()
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        ev = threading.Event()
        if wait:
            self._results_ev[mid] = ev
        self.ws.send(json.dumps(msg))
        if not wait:
            return None
        if ev.wait(timeout):
            return self._results.pop(mid, None)
        self._results_ev.pop(mid, None)
        return None

    def begin(self) -> None:
        """开始监听：发现所有页面目标并逐个开启 Network 域。"""
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.send("Target.setDiscoverTargets", {"discover": True})

    def _read_loop(self) -> None:
        while not self._stop.is_set() and self.ws is not None:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mid = msg.get("id")
        if mid is not None:
            self._results[mid] = msg.get("result")
            ev = self._results_ev.pop(mid, None)
            if ev:
                ev.set()
            return

        method = msg.get("method", "")
        params = msg.get("params") or {}

        if method == "Target.targetCreated":
            ti = params.get("targetInfo") or {}
            if ti.get("type") == "page":
                self.send("Target.attachToTarget",
                          {"targetId": ti["targetId"], "flatten": True})
            return

        if method == "Target.attachedToTarget":
            sid = params.get("sessionId")
            if sid and sid not in self._sessions:
                self._sessions.add(sid)
                self.send("Network.enable", {}, session=sid)
                self.send("Page.enable", {}, session=sid)
            return

        if method == "Network.responseReceived":
            r = params.get("response") or {}
            hdrs = {k.lower(): v for k, v in (r.get("headers") or {}).items()}
            try:
                size = int(hdrs.get("content-length") or 0)
            except (TypeError, ValueError):
                size = 0
            self.collector.add(r.get("url", ""), size, r.get("mimeType", ""))
            return

    # -- 辅助查询 --

    def grab_title(self) -> str:
        for sid in list(self._sessions):
            res = self.send(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
                session=sid, wait=True, timeout=5,
            )
            val = ((res or {}).get("result") or {}).get("value")
            if val:
                self.page_title = str(val)
                return self.page_title
        return self.page_title

    def nudge_play(self) -> None:
        js = (
            "document.querySelectorAll('video').forEach(v=>{"
            "try{v.muted=true;v.play&&v.play()}catch(e){}});"
            "true"
        )
        for sid in list(self._sessions):
            self.send("Runtime.evaluate", {"expression": js, "returnByValue": True},
                      session=sid)

    def poll_resources(self) -> None:
        """读页面资源计时表，补捞 CDP 挂载前发生的请求。"""
        js = (
            "JSON.stringify(performance.getEntriesByType('resource')"
            ".map(e=>[e.name, e.transferSize||e.encodedBodySize||0]))"
        )
        for sid in list(self._sessions):
            res = self.send("Runtime.evaluate",
                            {"expression": js, "returnByValue": True},
                            session=sid, wait=True, timeout=5)
            val = ((res or {}).get("result") or {}).get("value")
            if not val:
                continue
            try:
                for name, size in json.loads(val):
                    self.collector.add(name, int(size or 0), "")
            except Exception:
                continue

    def cookie_header(self, for_url: str) -> str:
        """取浏览器 cookie，拼成 Cookie 头（媒体地址通常已签名，带上更稳）。"""
        host = re.sub(r"^https?://([^/]+).*$", r"\1", for_url)
        for sid in list(self._sessions):
            res = self.send("Network.getAllCookies", {}, session=sid, wait=True, timeout=8)
            cookies = (res or {}).get("cookies") or []
            if not cookies:
                continue
            keep = []
            for c in cookies:
                dom = (c.get("domain") or "").lstrip(".")
                if dom and (dom in host or host.endswith(dom) or "douyin" in dom):
                    keep.append(f"{c.get('name')}={c.get('value')}")
            if keep:
                return "; ".join(keep)
        return ""

    def close(self) -> None:
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass


# ---------- 下载 / 合轨 ----------

def download_stream(url: str, dest: str, referer: str, cookie: str,
                    on_progress: Callable[[int, int], None] | None = None,
                    cancel: threading.Event | None = None) -> int:
    hdrs = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        hdrs["Cookie"] = cookie
    req = urllib.request.Request(url, headers=hdrs)
    got = 0
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("content-length") or 0)
        with open(dest, "wb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("已取消")
                buf = r.read(CHUNK)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                if on_progress:
                    on_progress(got, total)
    return got


def probe(ffmpeg: str | None, path: str) -> set[str]:
    if not ffmpeg:
        return set()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                           capture_output=True, text=True, errors="replace", timeout=60)
    except Exception:
        return set()
    out = (r.stderr or "") + (r.stdout or "")
    return {m.group(1).lower()
            for m in re.finditer(r"Stream #\d+:\d+.*?:\s*(Video|Audio)", out)}


def mux(ffmpeg: str, video: str, audio: str | None, dest: str) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video]
    if audio:
        cmd += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c", "copy", "-movflags", "+faststart", dest]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError((r.stderr or "ffmpeg 合轨失败")[:300])


# ---------- 引擎 ----------

@dataclass
class Result:
    ok: bool
    path: str = ""
    size: int = 0
    tracks: set[str] = field(default_factory=set)
    message: str = ""


class Engine:
    """把整条流程串起来，通过回调向外汇报进度，便于 GUI 或 CLI 复用。"""

    def __init__(
        self,
        on_log: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        on_progress: Callable[[float], None] | None = None,
        chrome: str | None = None,
        ffmpeg: str | None = None,
    ) -> None:
        self._log = on_log or (lambda m: print(m, flush=True))
        self._status = on_status or (lambda m: None)
        self._progress = on_progress or (lambda p: None)
        self.chrome = find_chrome(chrome)
        self.ffmpeg = find_ffmpeg(ffmpeg)

    def download(
        self,
        url: str,
        out_dir: str,
        wait_sec: int = 300,
        profile_dir: str | None = None,
        keep_parts: bool = False,
        cancel: threading.Event | None = None,
    ) -> Result:
        if not self.chrome:
            return Result(False, message="找不到 Chrome 或 Edge，请先安装其中之一")
        if not self.ffmpeg:
            self._log("[!] 未找到 ffmpeg，无法合轨（成品可能无声）")

        os.makedirs(out_dir, exist_ok=True)
        profile_dir = profile_dir or os.path.join(app_dir(), ".dy-profile")

        self._status("解析链接...")
        page_url = resolve_url(url)
        if page_url != url.strip():
            self._log(f"[*] 短链已解析 -> {page_url}")

        cdp = ChromeCDP(self.chrome, profile_dir, self._log)
        cdp.collector.on_new = lambda s: self._log(
            f"  [候选] {human(s.size):>9} {s.mime or '?':<11} {s.url[:76]}"
        )

        tmp = ""
        try:
            self._status("启动浏览器...")
            cdp.start(page_url)
            cdp.begin()
            time.sleep(2.0)
            cdp.nudge_play()

            self._status("等待播放 / 人工验证...")
            self._log("")
            self._log("如出现登录、滑块或验证码，请在浏览器窗口里手动完成，")
            self._log("并确认视频真的开始播放。抓到音视频流后会自动下载。")
            self._log("")

            streams = self._collect(cdp, wait_sec, cancel)
            if cancel is not None and cancel.is_set():
                return Result(False, message="已取消")
            if not streams:
                return Result(False, message=f"等待 {wait_sec}s 仍未捕获到媒体流")

            title = safe_name(cdp.grab_title())
            cookie = cdp.cookie_header(streams[0].url)

            self._log("")
            self._log(f"[*] 共 {len(streams)} 条流待下载")
            tmp = tempfile.mkdtemp(prefix="dy_", dir=out_dir)
            parts = self._fetch_all(streams, tmp, page_url, cookie, cancel)
            if cancel is not None and cancel.is_set():
                return Result(False, message="已取消")
            if not parts:
                return Result(False, message="所有流下载失败")

            dest = self._assemble(parts, out_dir, title)
            size = os.path.getsize(dest)
            tracks = probe(self.ffmpeg, dest)
            self._progress(1.0)
            self._status("完成")
            msg = "" if "audio" in tracks or not self.ffmpeg else "成品没有音频轨"
            return Result(True, dest, size, tracks, msg)

        except Exception as e:
            return Result(False, message=str(e)[:300])
        finally:
            if tmp:
                if keep_parts:
                    self._log(f"[i] 中间分轨文件保留在 {tmp}")
                else:
                    shutil.rmtree(tmp, ignore_errors=True)
            cdp.close()

    # -- 分步 --

    def _collect(self, cdp: ChromeCDP, wait_sec: int,
                 cancel: threading.Event | None) -> list[Stream]:
        deadline = time.time() + wait_sec
        settle_until = None
        tick = 0
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                return []
            cdp.poll_resources()
            found = cdp.collector.streams()
            if found and settle_until is None:
                settle_until = time.time() + SETTLE_SEC
                self._log(f"  -> 已发现主流，继续收集 {SETTLE_SEC}s 以捕获音频轨...")
                self._status("已发现视频流，正在等音频轨...")
            if settle_until and time.time() >= settle_until:
                break
            tick += 1
            if settle_until is None:
                if tick % 6 == 0:
                    cdp.nudge_play()
                if tick % 10 == 0:
                    self._log(f"  ...已等待 {tick * 2}s，候选 "
                              f"{len(cdp.collector.all())} 条，仍未发现主流")
            time.sleep(2)
        return cdp.collector.streams()

    def _fetch_all(self, streams: list[Stream], tmp: str, referer: str,
                   cookie: str, cancel: threading.Event | None
                   ) -> list[tuple[str, set[str], int]]:
        parts: list[tuple[str, set[str], int]] = []
        n = len(streams)
        for i, s in enumerate(streams):
            if cancel is not None and cancel.is_set():
                return parts
            p = os.path.join(tmp, f"part{i}.mp4")
            self._status(f"下载流 {i + 1}/{n} ...")

            def prog(got: int, total: int, _i=i) -> None:
                frac = (got / total) if total else 0.0
                self._progress((_i + frac) / max(n, 1) * 0.9)

            try:
                got = download_stream(s.url, p, referer, cookie, prog, cancel)
            except Exception as e:
                self._log(f"    [!] 流 {i} 下载失败: {str(e)[:80]}")
                continue
            kinds = probe(self.ffmpeg, p)
            self._log(f"    [√] 流 {i}: {human(got)}  轨道 {sorted(kinds) or '未知'}")
            parts.append((p, kinds, got))

            # 拿到音视频齐全的单流就够了，不必再下其余候选（流按体积降序）
            if {"video", "audio"} <= kinds:
                if i + 1 < n:
                    self._log(f"    -> 该流音视频齐全，跳过剩余 {n - i - 1} 条候选")
                break
        return parts

    def _assemble(self, parts: list[tuple[str, set[str], int]],
                  out_dir: str, title: str) -> str:
        dest = os.path.join(out_dir, f"{title}.mp4")
        if os.path.exists(dest):
            dest = os.path.join(out_dir, f"{title}_{int(time.time())}.mp4")

        complete = [p for p, k, _ in parts if {"video", "audio"} <= k]
        if complete:
            self._log("[*] 有单流已含音视频，直接采用")
            shutil.move(complete[0], dest)
            return dest

        vids = [(p, n) for p, k, n in parts if "video" in k]
        auds = [(p, n) for p, k, n in parts if "audio" in k and "video" not in k]
        if not vids:
            # 探测不到轨道时（例如没有 ffmpeg），退而用体积最大的一条
            if not self.ffmpeg and parts:
                biggest = max(parts, key=lambda t: t[2])[0]
                self._log("[!] 无 ffmpeg，无法探测轨道，直接采用体积最大的流")
                shutil.move(biggest, dest)
                return dest
            raise RuntimeError("没有捕获到视频轨")

        v = max(vids, key=lambda t: t[1])[0]
        a = max(auds, key=lambda t: t[1])[0] if auds else None
        if a:
            self._status("合轨中...")
            self._log("[*] 合并视频轨 + 音频轨（ffmpeg -c copy，无损）")
            mux(self.ffmpeg, v, a, dest)  # type: ignore[arg-type]
        else:
            self._log("[!] 未捕获到独立音频轨，成品将无声")
            shutil.move(v, dest)
        return dest
