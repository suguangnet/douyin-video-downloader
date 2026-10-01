# 抖音视频下载器

下载抖音视频，自动合并音视频轨，输出标准 mp4。有图形界面和命令行两种用法。

**核心思路：不调用抖音任何 API。** 让视频在真实浏览器里正常播放，通过 Chrome DevTools
Protocol 从浏览器内部读取播放器实际拉取的媒体地址，再用同一会话下载。因此不受抖音
签名风控（`a_bogus` / `uifid`）影响，也不需要设置系统代理或安装根证书。

---

## 快速开始

### 方式一：图形界面（推荐给非技术用户）

到 [Releases](../../releases) 下载打包版，解压后双击 `抖音视频下载器.exe`。

需要机器上装有 Chrome 或 Edge（Win10/11 自带 Edge）。`ffmpeg.exe` 必须和主程序放在
同一文件夹，否则下载出来会没声音——打包版已内置；若从源码运行，请自行从
[ffmpeg 官网](https://www.gyan.dev/ffmpeg/builds/) 下载并放到项目根目录，或用 `--ffmpeg` 指定路径。

### 方式二：命令行

```bash
pip install -r requirements.txt   # 实际只依赖 websocket-client

python douyin_dl.py https://www.douyin.com/video/7685271693463424299
python douyin_dl.py https://v.douyin.com/xxxxx/ -o D:\视频 -t 600
```

| 参数 | 说明 |
|---|---|
| `-o, --out` | 输出目录，默认当前目录 |
| `-t, --timeout` | 等待播放/人工验证的秒数，默认 300 |
| `--profile` | 浏览器配置目录，默认脚本同级 `.dy-profile`（存登录状态） |
| `--chrome` | 显式指定 `chrome.exe` / `msedge.exe` 路径 |
| `--ffmpeg` | 显式指定 ffmpeg 路径 |
| `--keep` | 保留中间分轨文件 |

### 使用流程

程序会弹出一个浏览器窗口并打开视频页。**首次运行可能要求登录、滑滑块或输手机验证码，
在窗口里手动完成即可**，之后登录状态会记住。看到视频开始播放后就不用管了，程序会自动
抓流、下载、合轨。

> 那个浏览器窗口是本程序的工作方式，不是异常。它使用独立配置目录 `.dy-profile`，
> 不会动你平时用的浏览器；也不修改系统代理、不安装根证书。

---

## 为什么要这么做

抖音的反抓取做得很彻底。下面是 2026-09-30 实测的结果：

| 方案 | 结果 | 卡在哪 |
|---|---|---|
| yt-dlp 2026.8.19 | 403 | 提取器对当前接口失效；补 cookie、模拟 TLS 指纹均无效 |
| f2 0.0.1.7 | 403 | **签名生成是对的**（`a_bogus` + `msToken` 都在请求里），仍被拒 |
| amemv-crawler | 接口已下线 | 用的是 2019 年的 `_signature`，接口返回 200 + 0 字节 |
| jiji262/douyin-downloader | 作者声明单视频被封 | 文档明确写了刷新 cookie 和重试都解决不了 |
| res-downloader 3.1.3 | 抓不到 | 全仓库无抖音专属代码，靠 goproxy 通用嗅探；HTTP 代理拿不到 QUIC 流量 |
| **本工具（CDP）** | **成功** | — |

规律很清楚：**凡是走 API 的全部失效，活下来的都是不碰 API 的。**

值得注意的两点：

- f2 能正确生成当代签名却依然 403，说明签名之上还有一层设备指纹校验。直接请求抖音接口
  会撞到 `Blocked by ArgusSecurityPlugin Uifid Not Found`。
- amemv-crawler 正是"自己实现签名"路线的结局演示：2019 年好用，抖音换了签名方案后
  静默失效，作者停更，issue 区留下大量无人回应的"无法下载"。

本工具刻意把最脆弱的环节（签名、设备指纹）外包给浏览器。抖音怎么改风控都不需要跟着改，
因为我们从来没有自己构造过请求。

---

## 工作原理

```
启动 Chrome (--remote-debugging-port)
        |
        v
直连 Chrome DevTools Protocol (websocket)
        |
        +-- Target.setDiscoverTargets   发现所有页面
        +-- Target.attachToTarget       逐个附加会话
        +-- Network.enable              开启网络域
        |
        v
订阅 Network.responseReceived  <---- 人工完成验证后，播放器开始拉流
        |
        v
收集媒体地址（按体积去重，辅以页面 performance 资源计时兜底）
        |
        v
下载各条流 -> ffmpeg 探测轨道类型 -> 无损合轨 (-c copy)
```

几个关键设计：

**为什么用 CDP 而不是代理嗅探。** CDP 读到的是浏览器内部解密后的网络事件，与 DevTools
网络面板同源。HTTP 代理方案（如 res-downloader）拿不到 QUIC/HTTP-3 流量，也会被 TLS
pinning 挡住；CDP 不存在这个问题——只要视频能播出来，事件就一定会触发。

**音视频分轨。** 抖音网页版用 MSE 播放时，视频和音频是两条独立的流，单独下载任何一条
都是残缺的（视频轨无声，音频轨无画面）。程序会把所有流都下来，**用 ffmpeg 探测每个
文件实际含有哪些轨道**，再合并。不靠 URL 命名猜测——抖音改命名就会失效。

**短路优化。** 抖音有时直接提供音视频齐全的单流。流按体积降序下载，一旦某条流探测到
音视频齐全，立即停止下载其余候选，省掉一半带宽。

**沉降窗口。** 发现首条主流后不立刻收工，而是再收集 20 秒（`SETTLE_SEC`），确保另一条
轨也被捕获。否则很容易只拿到视频轨。

---

## 项目结构

```
dy_engine.py      引擎：启动浏览器、CDP 抓流、下载、合轨      (约 660 行)
dy_gui.py         tkinter 界面，引擎跑在工作线程              (约 300 行)
douyin_dl.py      命令行入口（引擎薄壳）                      (约 66 行)
ffmpeg.exe        合轨用，静态编译版
发布包/           打包产物：exe + ffmpeg + 使用说明
```

引擎与界面完全解耦：`Engine` 通过 `on_log` / `on_status` / `on_progress` 三个回调
向外汇报，GUI 和 CLI 各自实现自己的展示方式。GUI 侧引擎跑在工作线程，消息经
`queue.Queue` 回传主线程，界面不会卡死；`threading.Event` 支持中途取消。

运行期依赖只有 `websocket-client`，其余全是标准库。

---

## 打包

```bash
pip install pyinstaller

python -m PyInstaller --noconfirm --onefile --windowed \
  --name DouyinDownloader --paths . --hidden-import websocket \
  --exclude-module imageio_ffmpeg --exclude-module imageio \
  --exclude-module playwright --exclude-module numpy --exclude-module PIL \
  dy_gui.py
```

产物约 11 MB。然后把 `ffmpeg.exe` 和 exe 放同一文件夹即可。

**两个打包要点：**

1. **不要把 ffmpeg 塞进 exe。** `--onefile` 每次运行都会把内嵌资源解压到 `%TEMP%`，
   塞进 60MB+ 的 ffmpeg 会导致每次启动干等数秒并堆积临时文件。分开放，启动是瞬时的。
   那几个 `--exclude-module` 也是为此——`imageio_ffmpeg` 会把它自带的一份 ffmpeg
   打进去，让 exe 从 11MB 涨到 43MB。

2. **冻结后路径语义会变。** `__file__` 指向 PyInstaller 的临时解压目录，每次运行都被
   清空。所以代码里区分了两个目录：

   ```python
   res_dir()   # 只读资源：打包后为 sys._MEIPASS
   app_dir()   # 可写位置：打包后为 exe 所在目录
   ```

   浏览器登录状态（`.dy-profile`）和配置文件（`dy_gui_config.json`）必须落在
   `app_dir()`，否则每次启动都要重新过验证。

---

## 已知限制

**画质和编码由抖音决定。** 实测同一视频在不同会话下拿到过三种版本：HEVC 720p、
HEVC 1080p、H.264 1080p。登录态下倾向于给更高画质。H.264 兼容性最好；HEVC 体积略小，
但在老播放器和部分剪辑软件里可能需要额外解码器。程序拿到什么用什么，不做转码。

**首次必须人工介入。** 登录、滑块、短信验证码都需要在弹出的浏览器窗口里手动完成。
这是有意的设计——自动绕过验证既不可靠也不合适。完成一次后登录状态长期有效。

**依赖本机浏览器。** 需要 Chrome 或 Edge。程序只是借用，不修改其任何设置。

**只做抖音。** 没有做多平台适配。

**杀毒误报。** PyInstaller 打包的程序常被误判，这是打包方式的通病。

---

## 常见问题

**提示「找不到 Chrome 或 Edge」** — 装一个即可，Win10/11 自带 Edge。

**等很久提示「未捕获到媒体流」** — 多半是视频没播起来。回浏览器窗口确认验证过了、
画面真的在动。验证耗时长的话把 `-t` / 界面上的「等待验证上限」调大。

**下载出来没声音** — 检查 `ffmpeg.exe` 是否和主程序同目录。启动时日志会显示
ffmpeg 有没有被找到。

**想换保存位置 / 重置设置** — 删掉 `dy_gui_config.json` 会恢复默认。

**想重新登录** — 删掉 `.dy-profile` 目录，下次运行会要求重新验证。

---

## 开发备忘

踩过的两个坑，记下来免得重犯：

- **Chrome 111+ 会校验 CDP WebSocket 的 Origin 头。** 不加 `--remote-allow-origins=*`
  启动参数，握手会直接返回 `403 Forbidden`。
- **只监听 `Network.responseReceived` 不够稳。** CDP 会话建立之前发生的请求会漏掉，
  所以额外用 `Runtime.evaluate` 读页面的 `performance.getEntriesByType('resource')`
  做兜底补捞。

验证成品完整性可以用：

```bash
ffmpeg -hide_banner -i 文件.mp4                    # 看时长和轨道
ffmpeg -hide_banner -v error -i 文件.mp4 -f null -  # 整片解码，检查坏帧
```

第二条会把整个文件解一遍，零输出才算真正完整——只看文件头和体积是不够的。
