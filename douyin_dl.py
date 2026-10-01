# -*- coding: utf-8 -*-
"""
抖音视频下载 —— 命令行入口

用法:
    python douyin_dl.py <视频链接> [-o 输出目录] [-t 等待秒数]
    python douyin_dl.py https://www.douyin.com/video/7685271693463424299
    python douyin_dl.py https://v.douyin.com/jYQZ8HwyqEY/ -o D:\\视频

图形界面见 dy_gui.py，核心逻辑见 dy_engine.py。
"""
from __future__ import annotations

import argparse
import os
import sys

from dy_engine import Engine, human


def main() -> int:
    ap = argparse.ArgumentParser(
        description="抖音视频下载（浏览器嗅探 + 音视频合轨，不调用抖音 API）"
    )
    ap.add_argument("url", help="抖音视频链接（支持 v.douyin.com 短链）")
    ap.add_argument("-o", "--out", default=".", help="输出目录，默认当前目录")
    ap.add_argument("-t", "--timeout", type=int, default=300,
                    help="等待播放/人工验证的秒数，默认 300")
    ap.add_argument("--profile", default=None,
                    help="浏览器配置目录，默认脚本同级 .dy-profile（保存登录状态）")
    ap.add_argument("--chrome", default=None, help="显式指定 chrome.exe / msedge.exe 路径")
    ap.add_argument("--ffmpeg", default=None, help="显式指定 ffmpeg 路径")
    ap.add_argument("--keep", action="store_true", help="保留中间分轨文件")
    a = ap.parse_args()

    def status(s: str) -> None:
        print(f"[状态] {s}", flush=True)

    eng = Engine(on_status=status, chrome=a.chrome, ffmpeg=a.ffmpeg)
    print(f"[*] 浏览器: {eng.chrome or '未找到'}")
    print(f"[*] ffmpeg : {eng.ffmpeg or '未找到'}")

    try:
        r = eng.download(
            a.url, a.out, wait_sec=a.timeout,
            profile_dir=a.profile, keep_parts=a.keep,
        )
    except KeyboardInterrupt:
        print("\n[!] 已中断")
        return 130

    if not r.ok:
        print(f"[X] 失败: {r.message}")
        return 2

    print("")
    print(f"[√] 完成: {r.path}")
    print(f"    体积 {human(r.size)}   轨道 {sorted(r.tracks) or '未知'}")
    if r.message:
        print(f"    [!] {r.message}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
