# -*- coding: utf-8 -*-
"""
抖音视频下载器 —— 图形界面

双击运行（或 python dy_gui.py）。引擎在工作线程里跑，界面不会卡死。
核心逻辑见 dy_engine.py。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dy_engine import Engine, Result, app_dir, human  # noqa: E402

# 打包后 HERE 指向临时解压目录，配置必须写到 exe 同级
CONFIG = os.path.join(app_dir(), "dy_gui_config.json")
APP_TITLE = "抖音视频下载器"


def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def default_out_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else app_dir()


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.master: tk.Tk = master
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_path = ""

        self._build()
        self._probe_env()
        self.after(120, self._drain)

    # ---------- 界面 ----------

    def _build(self) -> None:
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(self, text="视频链接").grid(row=r, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar()
        e = ttk.Entry(self, textvariable=self.url_var)
        e.grid(row=r, column=1, sticky="ew", padx=(8, 4), pady=4)
        ttk.Button(self, text="粘贴", width=6, command=self._paste).grid(
            row=r, column=2, pady=4
        )
        e.focus_set()

        r += 1
        ttk.Label(self, text="保存到").grid(row=r, column=0, sticky="w", pady=4)
        self.out_var = tk.StringVar(value=self.cfg.get("out_dir") or default_out_dir())
        ttk.Entry(self, textvariable=self.out_var).grid(
            row=r, column=1, sticky="ew", padx=(8, 4), pady=4
        )
        ttk.Button(self, text="浏览", width=6, command=self._browse).grid(
            row=r, column=2, pady=4
        )

        r += 1
        opt = ttk.Frame(self)
        opt.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(2, 8))
        ttk.Label(opt, text="等待验证上限").pack(side="left")
        self.wait_var = tk.IntVar(value=int(self.cfg.get("wait_sec", 300)))
        ttk.Spinbox(opt, from_=60, to=1800, increment=30, width=6,
                    textvariable=self.wait_var).pack(side="left", padx=(6, 2))
        ttk.Label(opt, text="秒").pack(side="left")
        self.keep_var = tk.BooleanVar(value=bool(self.cfg.get("keep_parts", False)))
        ttk.Checkbutton(opt, text="保留中间分轨文件",
                        variable=self.keep_var).pack(side="left", padx=16)

        r += 1
        btns = ttk.Frame(self)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.start_btn = ttk.Button(btns, text="开始下载", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(btns, text="打开所在文件夹",
                                   command=self._reveal, state="disabled")
        self.open_btn.pack(side="left")

        r += 1
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, foreground="#17549a").grid(
            row=r, column=0, columnspan=3, sticky="w"
        )

        r += 1
        self.pb = ttk.Progressbar(self, mode="determinate", maximum=1000)
        self.pb.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 8))

        r += 1
        box = ttk.Frame(self)
        box.grid(row=r, column=0, columnspan=3, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.rowconfigure(r, weight=1)

        self.log = tk.Text(box, height=15, wrap="none", state="disabled",
                           font=("Consolas", 9), background="#1e1e1e",
                           foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.log.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ttk.Scrollbar(box, orient="horizontal", command=self.log.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)

        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 辅助 ----------

    def _probe_env(self) -> None:
        eng = Engine()
        self._append(f"浏览器: {eng.chrome or '未找到 —— 请安装 Chrome 或 Edge'}")
        self._append(f"ffmpeg : {eng.ffmpeg or '未找到 —— 无法合轨，成品会无声'}")
        if not eng.chrome:
            self.status_var.set("缺少 Chrome / Edge，无法运行")
            self.start_btn.configure(state="disabled")
        elif not eng.ffmpeg:
            self.status_var.set("缺少 ffmpeg.exe（放到本程序同目录即可）")
        self._append("-" * 60)

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _paste(self) -> None:
        try:
            self.url_var.set(self.master.clipboard_get().strip())
        except Exception:
            pass

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get() or HERE)
        if d:
            self.out_var.set(d)

    def _reveal(self) -> None:
        if not self.last_path:
            return
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(self.last_path)])
        except Exception:
            pass

    def _busy(self, on: bool) -> None:
        self.start_btn.configure(state="disabled" if on else "normal")
        self.stop_btn.configure(state="normal" if on else "disabled")

    # ---------- 运行 ----------

    def _start(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "请先填入抖音视频链接")
            return
        out = self.out_var.get().strip() or default_out_dir()
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"输出目录不可用：{e}")
            return

        self.cfg.update({
            "out_dir": out,
            "wait_sec": int(self.wait_var.get()),
            "keep_parts": bool(self.keep_var.get()),
        })
        save_config(self.cfg)

        self.cancel.clear()
        self.pb.configure(value=0)
        self.open_btn.configure(state="disabled")
        self._busy(True)
        self._append("")
        self._append(f">>> 开始: {url}")

        eng = Engine(
            on_log=lambda m: self.q.put(("log", m)),
            on_status=lambda m: self.q.put(("status", m)),
            on_progress=lambda p: self.q.put(("progress", p)),
        )

        def work() -> None:
            try:
                r = eng.download(
                    url, out,
                    wait_sec=int(self.wait_var.get()),
                    keep_parts=bool(self.keep_var.get()),
                    cancel=self.cancel,
                )
            except Exception as e:
                r = Result(False, message=str(e)[:300])
            self.q.put(("done", r))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.cancel.set()
        self.status_var.set("正在停止...")
        self._append("[!] 已请求停止")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self.pb.configure(value=max(0.0, min(1.0, float(payload))) * 1000)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _finish(self, r: Result) -> None:
        self._busy(False)
        if not r.ok:
            self.pb.configure(value=0)
            self.status_var.set(f"失败：{r.message}")
            self._append(f"[X] 失败: {r.message}")
            return

        self.last_path = r.path
        self.open_btn.configure(state="normal")
        self.pb.configure(value=1000)
        tracks = "+".join(sorted(r.tracks)) or "未知"
        self.status_var.set(f"完成  {human(r.size)}  轨道 {tracks}")
        self._append(f"[√] 完成: {r.path}")
        self._append(f"    体积 {human(r.size)}   轨道 {tracks}")
        if r.message:
            self._append(f"    [!] {r.message}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(APP_TITLE, "下载还在进行，确定退出吗？"):
                return
            self.cancel.set()
        self.master.destroy()


def center_window(win: tk.Misc, width: int, height: int) -> None:
    """把窗口摆到屏幕正中。先定好位置再显示，避免先在左上角闪一下。"""
    win.update_idletasks()
    screen_w = win.winfo_screenwidth()
    screen_h = win.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    root.title(APP_TITLE)
    root.minsize(720, 520)
    center_window(root, 860, 620)
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
