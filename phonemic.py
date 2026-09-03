#!/usr/bin/env python3
"""PhoneMic — 把安卓手机的麦克风变成 Mac 的输入设备（经 BlackHole）。

架构: 手机(PhoneMic App) --HTTP/WAV流--> 本程序 --> BlackHole 2ch(虚拟声卡)
任何 Mac 应用把麦克风选成 BlackHole 2ch，收到的就是手机的现场声音。

地址解析三级回退（--auto 模式，菜单栏应用默认使用）:
  1. mDNS 自动发现（手机 App 广播 _phonemic._tcp）
  2. 上次成功地址（~/.phonemic_last_url）
  3. 对上次 IP 扫描候选端口（8080/8081/18080/28080）
连续失败会自动重新解析，手机换端口也能自愈。

用法:
  phonemic.py --auto              # 推荐：全自动
  phonemic.py http://192.168.31.15:8081   # 指定地址
  phonemic.py --list              # 列出输出设备
"""
import argparse
import fcntl
import ipaddress
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import sounddevice as sd

import debuglog

BASE = Path(__file__).resolve().parent   # 项目根（脚本所在目录），保证 clone 到任意位置都能运行
LAST_URL_FILE = BASE / ".phonemic_last_url"
LOCK_FILE = BASE / ".phonemic_lock"
GAIN_FILE = BASE / "gain_db"        # 数字增益（dB），菜单栏应用写入，引擎每秒读取
LEVEL_FILE = BASE / ".level"        # 引擎实时输出电平（0~100），菜单栏读取显示
RECORD_FILE = BASE / "record"       # 录音存档开关（"1"=开启）
PTT_FILE = BASE / ".ptt"            # 录音开关状态（"1"=录音中，单击右⌥切换），菜单栏监听写入
GATE_FILE = BASE / "gate_mode"      # 语音输入门控模式（"1"=开启：仅录音时向声卡输出音频，防串音；"0"=常开全通）
REC_DIR = BASE / "recordings"       # 录音与指标文件目录
PTT_MIN_SEGMENT = 0.3               # 短于此秒数的录音段视为误触，丢弃
RECONNECT_FILE = BASE / ".reconnect"   # 菜单栏「立即重连」信号（存在即触发）
TOKEN_FILE = BASE / ".phonemic_token"  # 配对 token（USB 首次连接自动取回，无线连接凭它鉴权）
# 发现通道端口。做成可覆盖是为了测试自洽：这两个是**系统级**资源，
# 同机另一个 PhoneMic 实例绑着 58080 时，SO_REUSEADDR 只保证 bind 不报错，
# 并不保证收得到包 —— 公告会被另一个实例整个吃掉，症状是"死活发现不了手机"。
UDP_ANNOUNCE_PORT = int(os.environ.get("PHONEMIC_ANNOUNCE_PORT") or 58080)  # 电脑监听：手机公告
UDP_QUERY_PORT = int(os.environ.get("PHONEMIC_QUERY_PORT") or 58081)        # 手机监听：电脑查询
OUTPUT_HINT = "BlackHole"
PREFILL_SECONDS = 0.30      # 预缓冲：300ms 常数延迟换零欠载（欠载丢音节=识别错误）
CANDIDATE_PORTS = [8081, 18080, 28080, 8080]
DTYPES = {8: "uint8", 16: "int16", 32: "int32"}
GAIN_DB = {"v": 0.0}
MAX_GAIN_DB = 18.0          # 与手机端 MainActivity.adjustGain 的上限保持一致
LIMIT_THRESHOLD = 26000.0   # 软限幅起压点（约 -2dBFS）
LIMIT_CEILING = 32000.0     # 软限幅输出天花板
AUTO_WAKE_COOLDOWN = 30.0   # ADB 自动唤醒手机 App 的最小间隔（秒）
USB_PROBE_INTERVAL = 1.0    # 发现循环里 USB 探测的最小间隔（秒），避免每 100ms 打一次 adb
USB_PROBE_TIMEOUT = 0.3     # 回环探测超时：本机端口转发不需要 2 秒
_LAST_WAKE = {"at": 0.0}
_LAST_USB_PROBE = {"at": 0.0, "url": None}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")   # 手机端 Base64-URL(9 字节) = 12 字符


# ---------- 配对 token：USB(回环) 自动取回，无线凭它鉴权 ----------

def load_token() -> str:
    """读取已配对的 token（空串=尚未配对或内容损坏）。"""
    try:
        tok = TOKEN_FILE.read_text(errors="replace").strip()
    except Exception:
        return ""
    # 损坏的 token 当作未配对：否则一旦存进非法字符，
    # 后续每个 HTTP 请求都会死在 "Invalid header value" 上且永远无法自愈
    return tok if is_valid_token(tok) else ""


def is_valid_token(tok: str) -> bool:
    """配对码必须是 Base64-URL 短串（手机端用 SecureRandom 生成 9 字节编码而来）。"""
    return bool(tok) and bool(TOKEN_RE.match(tok))


def save_token(tok: str) -> None:
    try:
        TOKEN_FILE.write_text((tok or "").strip())
        TOKEN_FILE.chmod(0o600)
    except Exception:
        pass


def auth_headers() -> dict:
    """HTTP 拉流的鉴权头；未配对时为空（手机端会拒绝非回环请求）。"""
    tok = load_token()
    return {"X-PhoneMic-Token": tok} if tok else {}


def udp_start_payload() -> bytes:
    """UDP 推流注册包；带 token 才会被手机端接受。"""
    tok = load_token()
    return f"PHONEMIC_UDP_START {tok}".encode() if tok else b"PHONEMIC_UDP_START"


def fetch_token_over_usb(base_url: str) -> bool:
    """USB(回环) 通道免鉴权，首次连上时把 token 取回本地存档。"""
    if load_token():
        return True
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/token",
                                     headers={"User-Agent": "PhoneMic/1.0"})
        resp = DIRECT_OPENER.open(req, timeout=2)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        body = resp.read(128)
        resp.close()
    except Exception as e:
        debuglog.log("engine", f"USB 配对：取 token 失败（手机端可能是旧版本）：{e}")
        return False
    tok = body.decode("ascii", "ignore").strip()
    # 旧版 APK 对任何路径都回 WAV 流。不校验就会把音频数据存成 token，
    # 于是之后每个 HTTP 请求都带着非法头挂掉 —— 必须内容类型 + 字符集双重校验。
    if not ctype.startswith("text/plain") or not is_valid_token(tok):
        debuglog.log("engine", "USB 配对：/token 响应不是合法配对码"
                               f"（Content-Type={ctype!r} 长度={len(body)}），"
                               "手机端需升级到带配对功能的版本")
        return False
    save_token(tok)
    print("[配对] ⚡ 已通过 USB 自动完成配对，之后无线连接免手动输入", flush=True)
    debuglog.log("engine", "USB 配对成功，token 已存档")
    return True


# ---------- 纯函数：增益 / 限幅 / 公告解析 / 链路标签 ----------

def clamp_gain(v) -> float:
    return max(0.0, min(MAX_GAIN_DB, float(v)))


def process_pcm16(data: bytes, gain_db: float):
    """int16 PCM 加增益 + tanh 软限幅。

    返回 (int16 数组, 触发限幅的采样数, 峰值百分比 0~100)。

    「触发限幅数」统计的是**限幅之前**超过阈值的采样。软限幅把输出压在
    LIMIT_CEILING(32000) 以下，若像早期版本那样在限幅之后按 >=32600 统计，
    该指标数学上恒为 0，等于没有这个诊断。
    """
    arr = np.frombuffer(data, dtype="int16").astype(np.float32)
    if gain_db:
        arr *= 10 ** (gain_db / 20.0)
    mask = np.abs(arr) > LIMIT_THRESHOLD
    limited = int(np.count_nonzero(mask))
    if limited:
        excess = np.abs(arr[mask]) - LIMIT_THRESHOLD
        headroom = (32767.0 - LIMIT_THRESHOLD) * 0.8
        compressed = (LIMIT_THRESHOLD
                      + (LIMIT_CEILING - LIMIT_THRESHOLD) * np.tanh(excess / headroom))
        arr[mask] = np.sign(arr[mask]) * compressed
    np.clip(arr, -32768, 32767, out=arr)
    peak = int(np.abs(arr).max()) * 100 // 32768 if arr.size else 0
    return arr.astype("int16"), limited, peak


def parse_announce(msg: str) -> dict | None:
    """解析手机公告 'PHONEMIC <tcp> [UDP <udp>] [RATE <hz>]'。

    旧版手机只发 'PHONEMIC <tcp>'，缺省字段按老默认值补齐（向后兼容）。
    """
    parts = msg.split()
    if len(parts) < 2 or parts[0] != "PHONEMIC":
        return None
    try:
        tcp_port = int(parts[1])
    except ValueError:
        return None
    out = {"tcp_port": tcp_port, "udp_port": 58082, "rate": 48000}
    for i in range(2, len(parts) - 1, 2):
        key, val = parts[i].upper(), parts[i + 1]
        try:
            if key == "UDP":
                out["udp_port"] = int(val)
            elif key == "RATE":
                out["rate"] = int(val)
        except ValueError:
            pass
    return out


def link_mode_label(url: str) -> str:
    """把连接地址翻译成菜单栏显示的链路名（空地址返回空串，不谎报链路）。"""
    if not url:
        return ""
    if url.startswith("udp://"):
        return "📡 UDP 极速无线流 (15ms 低延迟)"
    if "127.0.0.1" in url or "localhost" in url:
        return "⚡ USB 物理直连 (<1ms 极速)"
    return "📶 Wi-Fi 局域网流 (40ms)"


def parse_udp_url(url: str) -> tuple[str, int]:
    body = url[6:] if url.startswith("udp://") else url
    host, _, port = body.partition(":")
    return host, (int(port) if port.isdigit() else 58082)


def auto_wake_allowed() -> bool:
    """ADB 自动唤醒的冷却闸。

    发现循环与 watchdog 都会调 check_usb_device，无冷却时实测约 1 次/秒
    执行 `am start`，把手机主界面反复拉到前台。
    """
    now = time.time()
    if now - _LAST_WAKE["at"] < AUTO_WAKE_COOLDOWN:
        return False
    _LAST_WAKE["at"] = now
    return True


# 绕过系统代理/环境变量，局域网直连（代理会对局域网流间歇性返回 404）
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 必须持有文件对象引用：否则函数返回后被垃圾回收，文件关闭，flock 随之释放
_LOCK_FH = None


class _PipeSafeStdout:
    """菜单栏父进程被强杀后 stdout 管道断裂，继续打印会抛 BrokenPipeError 杀死引擎。

    管道断裂后自动切换为静默丢弃：引擎继续拉流/录音，等菜单栏回来再由
    单实例锁自然接管，音频不中断。
    """

    def __init__(self, fh):
        self._fh = fh
        self._dead = False

    def write(self, s):
        if self._dead:
            return len(s)
        try:
            return self._fh.write(s)
        except (BrokenPipeError, ValueError, OSError):
            self._dead = True
            return len(s)

    def flush(self):
        if not self._dead:
            try:
                self._fh.flush()
            except Exception:
                self._dead = True

    def isatty(self):
        return False


def acquire_lock() -> bool:
    """保证全机只有一个 PhoneMic 实例在写 BlackHole。

    必须用 "a+" 而不是 "w" 打开："w" 会在 flock 之前就截断文件，让每个抢锁
    失败的实例都把持锁者的 PID 擦成空文件。菜单栏靠读这个 PID 识别孤儿引擎，
    PID 一丢，孤儿守卫就永久失效 —— 表现为每 2 秒空转拉起一个新引擎，且
    「停止 / 退出」杀不掉真正在写 BlackHole 的那个进程。
    """
    global _LOCK_FH
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()   # 没拿到锁就别占着 fd，更别碰文件内容
        debuglog.log("engine", "单实例锁已被占用，本次退出")
        return False
    fh.seek(0)
    fh.truncate()    # 拿到锁之后才允许改内容
    fh.write(str(__import__("os").getpid()))
    fh.flush()
    _LOCK_FH = fh
    debuglog.log("engine", f"拿到单实例锁 pid={__import__('os').getpid()}")
    return True


def discover_mdns(timeout: float = 4.0) -> str | None:
    """在局域网里寻找手机广播的 _phonemic._tcp 服务。"""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return None
    result = {}

    class Listener:
        def add_service(self, zc, type_, name):
            try:
                info = zc.get_service_info(type_, name, 3000)
                if info and info.addresses and info.port:
                    for raw in info.addresses:
                        try:
                            ip = socket.inet_ntoa(raw)
                            o = ipaddress.ip_address(ip)
                            if not (o.is_private and not o.is_loopback and not o.is_link_local):
                                continue   # 跳过回环/链路本地等非法解析结果
                        except Exception:
                            continue
                        result[f"http://{ip}:{info.port}"] = name
                        break
            except Exception:
                pass

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = None
    try:
        zc = Zeroconf()
        ServiceBrowser(zc, "_phonemic._tcp.local.", Listener())
        deadline = time.time() + timeout
        while time.time() < deadline and not result:
            time.sleep(0.2)
    except Exception:
        pass
    finally:
        try:
            if zc:
                zc.close()
        except Exception:
            pass
    return next(iter(result), None)


def scan_host_for_riff(host: str, timeout: float = 0.6) -> str | None:
    """并行探测候选端口（手机在已知主机但 mDNS/UDP 都失效时的最后手段）。"""
    hits: dict = {}

    def probe(port):
        url = f"http://{host}:{port}/audio.wav"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "PhoneMic/1.0", **auth_headers()})
            resp = DIRECT_OPENER.open(req, timeout=timeout)
            head = resp.read(4)
            resp.close()
            if head == b"RIFF":
                hits[port] = url
        except Exception:
            pass

    threads = [threading.Thread(target=probe, args=(p,), daemon=True)
               for p in CANDIDATE_PORTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 0.3)
    return hits[min(hits)] if hits else None   # 端口号小者优先，与原串行顺序一致


# 最新手机 UDP 公告缓存：手机无客户端时每秒广播一次，
# 是 mDNS 失效（Android 回网后 NsdManager 重注册不可靠）时的兜底发现通道
_ANNOUNCE = {"url": None, "http_url": None, "rate": 48000, "at": 0.0}


def udp_announce_listener():
    """常驻后台：监听手机 'PHONEMIC <port> [UDP <p>] [RATE <hz>]' 公告，更新缓存。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", UDP_ANNOUNCE_PORT))
    except Exception:
        return   # 端口被占（如另一实例）：公告通道让位于锁持有者
    while True:
        try:
            data, addr = s.recvfrom(256)
            info = parse_announce(data.decode("utf-8", "ignore").strip())
            if info:
                _ANNOUNCE["url"] = f"udp://{addr[0]}:{info['udp_port']}"
                _ANNOUNCE["http_url"] = f"http://{addr[0]}:{info['tcp_port']}"
                _ANNOUNCE["rate"] = info["rate"]
                _ANNOUNCE["at"] = time.time()
        except Exception:
            time.sleep(0.5)


def send_udp_query():
    """广播查询：手机收到后立即回公告，绕过 mDNS 直接定位（手动重连快车道）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(b"PHONEMIC_QUERY", ("255.255.255.255", UDP_QUERY_PORT))
        s.close()
    except Exception:
        pass


def reconnect_requested() -> bool:
    """菜单栏「立即重连」信号：存在即消费。"""
    try:
        if RECONNECT_FILE.exists():
            RECONNECT_FILE.unlink()
            return True
    except Exception:
        pass
    return False


def find_adb_path() -> str | None:
    """查找系统中的 adb 可执行文件路径。"""
    if shutil.which("adb"):
        return shutil.which("adb")
    candidates = [
        Path.home() / "Library/Android/sdk/platform-tools/adb",
        Path("/opt/homebrew/bin/adb"),
        Path("/usr/local/bin/adb"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


USB_URL = "http://127.0.0.1:58083"


def _forward_and_probe(adb: str, dev_id: str) -> str | None:
    """逐个候选端口建端口转发并探活；命中即顺便完成 USB 免密配对。"""
    for port in CANDIDATE_PORTS:
        subprocess.run([adb, "-s", dev_id, "forward", "tcp:58083", f"tcp:{port}"],
                       capture_output=True, timeout=0.5)
        # 回环转发不需要 2 秒超时：端口没服务时立刻 RST，有服务时立刻回 RIFF
        if probe_ok(USB_URL, timeout=USB_PROBE_TIMEOUT):
            fetch_token_over_usb(USB_URL)
            return USB_URL
    return None


def check_usb_device(auto_wake: bool = True) -> str | None:
    """检测 USB 物理连接的 Android 手机，建立极速端口映射。

    带 1 秒结果缓存：发现循环每 100ms 调一次、watchdog 每 2 秒调一次，
    没有缓存的话光 `adb devices` 就能把 4.5 秒的发现窗口拖到 20 秒以上。
    """
    now = time.time()
    if os.environ.get("PHONEMIC_NO_USB") == "1":
        return None   # 集成测试用：别让插着的真手机劫持测试里的假手机
    if now - _LAST_USB_PROBE["at"] < USB_PROBE_INTERVAL:
        return _LAST_USB_PROBE["url"]
    _LAST_USB_PROBE["at"] = now

    url = None
    adb = find_adb_path()
    if adb:
        try:
            res = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=1.2)
            for line in res.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device" and not parts[0].startswith("emulator"):
                    dev_id = parts[0]
                    url = _forward_and_probe(adb, dev_id)
                    # 手机在线但服务没起：拉起主界面补启动。必须走冷却闸，
                    # 否则发现循环与 watchdog 会以约 1 次/秒的频率反复把 App 弹到前台
                    if not url and auto_wake and auto_wake_allowed():
                        debuglog.log("engine", "USB 设备在线但服务未响应，通过 ADB 拉起 PhoneMic"
                                               f"（{AUTO_WAKE_COOLDOWN:.0f}s 内不再重试）")
                        subprocess.run([adb, "-s", dev_id, "shell", "am", "start", "-n",
                                        "com.jerry.phonemic/.MainActivity"],
                                       capture_output=True, timeout=1.0)
                        time.sleep(0.4)
                        url = _forward_and_probe(adb, dev_id)
                    if url:
                        break
        except Exception:
            url = None
    _LAST_USB_PROBE["url"] = url
    return url


def announced_url() -> str | None:
    """把最新公告翻成可用地址：优先 UDP（15ms），UDP 不通就退回公告里的 HTTP 地址。

    公告同时带了 TCP 与 UDP 端口，但早期版本只用 UDP 那个，http_url 存了从不读 ——
    于是 AP 隔离 / 防火墙挡住 UDP 时，发现会返回一个永远连不通的 udp:// 地址。
    """
    udp_url, http_url = _ANNOUNCE.get("url"), _ANNOUNCE.get("http_url")
    if udp_url:
        host, port = parse_udp_url(udp_url)
        if probe_udp(host, port, timeout=0.8):
            return udp_url
        debuglog.log("engine", f"UDP 不通（{udp_url}），退回公告里的 HTTP 地址 {http_url}")
    return http_url or udp_url


def resolve_url(explicit: str | None) -> str | None:
    """显式地址 > USB 物理直连优先 > 新公告 > （上次地址 ∥ mDNS ∥ UDP 查询）并行 > 上次主机扫端口。"""
    if explicit:
        return explicit

    # 1. 优先级最高：USB 物理线直连探测（<1ms 延迟，0 抖动，100% 稳定）
    usb_url = check_usb_device()
    if usb_url:
        print(f"[发现] ⚡ 检测到 USB 连接，已优先启用 USB 极速直连模式：{usb_url}", flush=True)
        debuglog.log("engine", f"检测到 USB 直连设备，优先启用 USB 极速模式：{usb_url}")
        return usb_url

    t_start = time.time()
    if _ANNOUNCE["url"] and t_start - _ANNOUNCE["at"] < 5:
        hit = announced_url()
        if hit:
            print(f"[发现] UDP 公告命中：{hit}", flush=True)
            return hit

    print("[发现] 正在寻找手机（USB/公告/上次地址/mDNS 并行）…", flush=True)
    send_udp_query()
    last = LAST_URL_FILE.read_text().strip() if LAST_URL_FILE.exists() else ""
    box: dict = {"mdns": None, "last": None}

    def mdns_task():
        box["mdns"] = discover_mdns(4.0)

    def last_task():
        box["last"] = last if (last and probe_ok(last)) else None

    if last:
        # 有历史地址：并行跑 mDNS 和直连探测，谁先命中用谁
        threading.Thread(target=last_task, daemon=True).start()
    threading.Thread(target=mdns_task, daemon=True).start()

    deadline = time.time() + 4.5
    while time.time() < deadline:
        # 寻找期间随时优先检查 USB 是否刚插上
        u = check_usb_device()
        if u:
            print(f"[发现] ⚡ 检测到 USB 插入，优先启用 USB 极速直连模式：{u}", flush=True)
            return u
        if box["last"]:
            print(f"[发现] 上次地址仍可用：{box['last']}", flush=True)
            return box["last"]                      # IP 未变，最快路径
        if _ANNOUNCE["url"] and _ANNOUNCE["at"] >= t_start:
            hit = announced_url()                    # 查询带回的新公告
            if hit:
                print(f"[发现] UDP 查询应答：{hit}", flush=True)
                return hit
        if box["mdns"]:
            print(f"[发现] mDNS 找到：{box['mdns']}", flush=True)
            return box["mdns"]
        time.sleep(0.1)

    # 超时兜底：扫上次主机的候选端口（IP 变了但主机在线的情况）
    if last:
        # 扫描前再查一次公告（等待循环结束到这里的间隙里手机可能刚好回来）
        if _ANNOUNCE["url"] and _ANNOUNCE["at"] >= t_start:
            hit = announced_url()
            if hit:
                print(f"[发现] UDP 查询应答：{hit}", flush=True)
                return hit
        host = last.split("//")[-1].split(":")[0]
        url = scan_host_for_riff(host)
        if url:
            print(f"[发现] 扫描命中：{url}", flush=True)
            return url
    print("[发现] 找不到手机（App 是否已启动？是否同一 Wi-Fi 或插上 USB 线？）", flush=True)
    return None


def probe_udp(host: str, port: int, timeout: float = 1.2) -> bool:
    """向手机 UDP 音频端注册一次，收到 PMIC 包即判活，收尾时撤销注册。"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(udp_start_payload(), (host, port))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data, _ = s.recvfrom(2048)
            if len(data) >= 8 and data[:4] == b"PMIC":
                return True
    except Exception:
        pass
    finally:
        if s is not None:
            try:
                s.sendto(b"PHONEMIC_UDP_STOP", (host, port))
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
    return False


def probe_ok(url: str, timeout: float = 2.0) -> bool:
    # udp:// 也必须能探活：main() 会把它写进 .phonemic_last_url，
    # 若这里只认 http，「上次地址」快车道在无线模式下永久失效
    if url.startswith("udp://"):
        return probe_udp(*parse_udp_url(url), timeout=min(timeout, 1.2))
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "PhoneMic/1.0", **auth_headers()})
        resp = DIRECT_OPENER.open(req, timeout=timeout)
        head = resp.read(4)
        resp.close()
        return head == b"RIFF"
    except Exception:
        return False


def find_output(hint: str) -> tuple[int, str]:
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0 and hint.lower() in d["name"].lower():
            return i, d["name"]
    sys.exit(f"错误：找不到包含 '{hint}' 的输出设备。请确认已安装 BlackHole（--list 可查看全部设备）")


def read_more(resp, buf: bytearray, n: int) -> None:
    while len(buf) < n:
        chunk = resp.read(4096)
        if not chunk:
            raise ConnectionError("音频流中断（手机端服务是否还在运行？）")
        buf += chunk


def wav_head_and_payload(resp) -> tuple[bytes, tuple[int, int, int]]:
    """解析（可能无限长的）WAV 流头，返回 (已收到的PCM数据, (声道, 采样率, 位深))。"""
    buf = bytearray()
    fmt = None
    read_more(resp, buf, 12)
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError("不是 WAV 音频流，请检查手机端音频设置")
    pos = 12
    while True:
        read_more(resp, buf, pos + 8)
        cid, size = struct.unpack("<4sI", buf[pos:pos + 8])
        if cid == b"fmt ":
            read_more(resp, buf, pos + 8 + 16)
            _tag, ch, rate, _bps, _align, bits = struct.unpack("<HHIIHH", buf[pos + 8:pos + 24])
            fmt = (ch, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("WAV 流异常：data 块在 fmt 之前")
            return bytes(buf[pos + 8:]), fmt
        pos += 8 + size + (size & 1)
        if pos > 65536:
            raise ValueError("WAV 头解析异常")


def gain_watcher():
    """每秒读取增益配置，改动即时生效，无需重启。"""
    while True:
        try:
            if GAIN_FILE.exists():
                v = float(GAIN_FILE.read_text().strip() or 0)
                GAIN_DB["v"] = clamp_gain(v)
        except Exception:
            pass
        time.sleep(1)


def recording_enabled() -> bool:
    try:
        return RECORD_FILE.exists() and RECORD_FILE.read_text().strip() == "1"
    except Exception:
        return False


def save_flac(stamp: str) -> None:
    """把一段录音 WAV 无损转存为 FLAC 并删除源文件（后台线程执行）。"""
    wav_path = REC_DIR / f"{stamp}.wav"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"[录音] 保留 WAV：{wav_path.name}（未安装 ffmpeg）", flush=True)
        debuglog.log("engine", f"转码跳过：未找到 ffmpeg，保留 {wav_path.name}")
        return
    try:
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path),
                            "-c:a", "flac", str(wav_path.with_suffix(".flac"))],
                           check=True, capture_output=True)
        wav_path.unlink()
        print(f"[录音] 已存档：{stamp}.flac", flush=True)
        debuglog.log("engine", f"转码成功：{stamp}.flac（已删源 WAV）")
    except Exception as e:
        print(f"[录音] 保留 WAV：{wav_path.name}（转码失败）", flush=True)
        debuglog.log("engine", f"转码失败，保留 {wav_path.name}：{e}"
                               + (f" | stderr={r.stderr[-300:]!r}" if 'r' in dir() else ""))


class PttRecorder:
    """PTT 录音：仅录音开关激活（.ptt=1，单击右⌥切换）期间写入音频。

    每段生成一个独立文件，开关关闭即收尾并后台转 FLAC；
    短于 PTT_MIN_SEGMENT 的段视为误触直接丢弃。不激活时完全不占空间。
    """

    def __init__(self, ch: int, rate: int, bits: int):
        self.ch, self.rate, self.bits = ch, rate, bits
        self.wav = None
        self.stamp = None
        self.write_failed = False   # 落盘失败过：收尾时据此告警，别让用户以为录上了
        self._cache = (0.0, False)
        self._savers = []   # 在途的 WAV→FLAC 转码线程

    def _ptt_on(self) -> bool:
        # 50ms 缓存：读文件频率足够低，开关延迟足够小
        now = time.time()
        if now - self._cache[0] > 0.05:
            try:
                on = PTT_FILE.exists() and PTT_FILE.read_text().strip() == "1"
            except Exception:
                on = False
            if on != self._cache[1]:     # 只在翻转时记一行，避免刷屏
                debuglog.log("engine", f"PTT 开关 → {'开（开始录音）' if on else '关（收尾存档）'}")
            self._cache = (now, on)
        return self._cache[1]

    def write(self, chunk: bytes) -> None:
        if self._ptt_on():
            if self.wav is None:
                try:
                    import wave
                    REC_DIR.mkdir(parents=True, exist_ok=True)
                    self.stamp = (time.strftime("%Y%m%d-%H%M%S")
                                  + f"-{int(time.time() * 1000) % 1000:03d}")
                    self.wav = wave.open(str(REC_DIR / f"{self.stamp}.wav"), "wb")
                    self.wav.setnchannels(self.ch)
                    self.wav.setsampwidth(self.bits // 8)
                    self.wav.setframerate(self.rate)
                    print(f"[录音] 开始记录：{self.stamp}.wav", flush=True)
                    debuglog.log("engine", f"录音开段：{self.stamp}.wav "
                                           f"（{self.rate}Hz {self.ch}ch {self.bits}bit）")
                except Exception as e:
                    print(f"[录音] 开段失败：{e}", flush=True)
                    debuglog.log("engine", f"录音开段失败：{e}", exc=True)
                    self.wav = None
            if self.wav is not None:
                try:
                    self.wav.writeframesraw(chunk)
                except Exception as e:
                    # 静默吞掉的话，磁盘满/权限问题会让录音变成空文件而用户毫不知情
                    if not self.write_failed:
                        self.write_failed = True
                        print(f"[录音] ⚠️ 写入失败，本段可能不完整：{e}", flush=True)
                        debuglog.log("engine", f"录音写入失败（{self.stamp}）：{e}", exc=True)
        elif self.wav is not None:
            self.close_segment()

    def close_segment(self) -> None:
        wav, stamp = self.wav, self.stamp
        failed, self.write_failed = self.write_failed, False
        self.wav, self.stamp = None, None
        if wav is None:
            return
        try:
            dur = wav.getnframes() / self.rate
            wav.close()
        except Exception:
            return
        if failed:
            print(f"[录音] ⚠️ {stamp} 期间有写入失败，存档可能缺片段", flush=True)
        if dur < PTT_MIN_SEGMENT:
            try:
                (REC_DIR / f"{stamp}.wav").unlink()
                print(f"[录音] 段过短（{dur:.1f}s），已丢弃", flush=True)
                debuglog.log("engine", f"录音段过短丢弃：{stamp}（{dur:.1f}s）")
            except Exception:
                pass
            return
        debuglog.log("engine", f"录音段收尾：{stamp}.wav {dur:.1f}s，转入 FLAC 转码")
        t = threading.Thread(target=save_flac, args=(stamp,), daemon=True)
        self._savers.append(t)
        t.start()

    def close(self) -> None:
        """退出/断流前收尾当前段，并等待在途转码完成。

        转码线程是 daemon：不 join 的话进程退出时线程被杀，ffmpeg 孤儿进程
        虽能产出 FLAC，但源 WAV 的删除不会执行（残留 .wav）。
        """
        self.close_segment()
        for t in self._savers:
            if t.is_alive():
                t.join(timeout=10)
        self._savers = [t for t in self._savers if t.is_alive()]


class StreamTee:
    """代理音频流：先把头部 payload 回放给下游，之后透传；可选交给录音器写入。"""

    def __init__(self, resp, recorder, initial):
        self.resp = resp
        self.recorder = recorder
        self.buf = initial

    def read(self, n):
        if self.buf:
            chunk, self.buf = self.buf[:n], self.buf[n:]
        else:
            chunk = self.resp.read(n)
            if not chunk:
                return chunk
        if self.recorder:
            self.recorder.write(chunk)
        return chunk


class UdpAudioReceiver:
    """UDP 极速音频接收器：10ms 帧直接接收，带丢包补偿与序列号检查，无阻塞零延迟。"""

    def __init__(self, host: str, port: int, stop: threading.Event):
        self.host = host
        self.port = port
        self.stop = stop
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", 0))
        self.sock.settimeout(1.0)
        self.last_seq = None
        self.lost_packets = 0
        self.received_packets = 0
        self.buf = bytearray()
        self._send_ping()

        def pinger():
            while not self.stop.is_set():
                self._send_ping()
                for _ in range(20):
                    if self.stop.is_set():
                        break
                    time.sleep(0.1)
            try:
                self.sock.sendto(b"PHONEMIC_UDP_STOP", (self.host, self.port))
            except Exception:
                pass

        threading.Thread(target=pinger, daemon=True).start()

    def _send_ping(self):
        try:
            self.sock.sendto(udp_start_payload(), (self.host, self.port))
        except Exception:
            pass

    def read(self, n: int) -> bytes:
        while len(self.buf) < n and not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
                if len(data) >= 8 and data[:4] == b"PMIC":
                    seq = struct.unpack(">I", data[4:8])[0]
                    pcm = data[8:]
                    if self.last_seq is not None:
                        diff = (seq - self.last_seq) & 0xFFFFFFFF
                        if 1 < diff < 50:
                            self.lost_packets += (diff - 1)
                            self.buf.extend(b"\x00" * ((diff - 1) * len(pcm)))
                    self.last_seq = seq
                    self.received_packets += 1
                    self.buf.extend(pcm)
            except socket.timeout:
                if self.stop.is_set():
                    break
                continue
            except Exception:
                break
        res, self.buf = bytes(self.buf[:n]), self.buf[n:]
        return res

    def close(self):
        try:
            self.sock.sendto(b"PHONEMIC_UDP_STOP", (self.host, self.port))
            self.sock.close()
        except Exception:
            pass


def stream_once(url: str, out_idx: int, stop: threading.Event) -> None:
    if url.startswith("udp://"):
        host, port = parse_udp_url(url)
        udp_rcv = UdpAudioReceiver(host, port, stop)
        resp = udp_rcv
        raw_resp = resp
        payload = b""
        # 采样率来自手机公告（parse_announce 的 RATE 字段），旧版手机缺省 48000。
        # 早期版本这里硬编码 48000，手机端一改格式就会静默按错误采样率解码。
        ch, rate, bits = 1, int(_ANNOUNCE.get("rate") or 48000), 16
        dtype = "int16"
        print(f"[传输] 采用 UDP 极速低延迟模式 (目标={host}:{port} {rate}Hz)", flush=True)
        debuglog.log("engine", f"启动 UDP 推流会话 (目标={host}:{port} {rate}Hz)")
    else:
        req = urllib.request.Request(
            url, headers={"User-Agent": "PhoneMic/1.0", **auth_headers()})
        # timeout 同时约束连接与每次 read：手机离开 WiFi 范围时不会有 TCP RST，
        # 靠这个读超时把"无限挂起"压到 3 秒内检测（正常流每 ~128ms 就有一块数据）
        resp = DIRECT_OPENER.open(req, timeout=3)
        raw_resp = resp
        payload, (ch, rate, bits) = wav_head_and_payload(resp)
        dtype = DTYPES.get(bits)
        if dtype is None:
            raise ValueError(f"不支持的位深 {bits}bit（请在手机端把音频格式设为 PCM 16bit）")
    # 连上了才把地址写进缓存：写在尝试之前的话，失败的尝试也会污染
    # 「上次可用地址」，菜单栏的链路面板也会跟着显示一条从未接通的链路
    try:
        LAST_URL_FILE.write_text(url)
    except Exception:
        pass
    t0 = time.time()

    # 录音存档（可选，PTT 模式）：只有按住右 Option 期间的音频才落盘；
    # 电平/指标时间线仍全程记录（体积可忽略）
    recorder, meta_fh, rec_stamp = None, None, time.strftime("%Y%m%d-%H%M%S")
    if recording_enabled():
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            recorder = PttRecorder(ch, rate, bits)
            meta_fh = open(REC_DIR / f"{rec_stamp}.meta.csv", "w")
            meta_fh.write("elapsed_sec,level_pct,underruns,limited\n")
            print("[录音] PTT 模式：按右⌥开始记录，再按结束并存档 FLAC", flush=True)
            debuglog.log("engine", f"录音器就绪：PTT 模式，指标文件 {rec_stamp}.meta.csv")
        except Exception as e:
            print(f"[录音] 启动失败：{e}", flush=True)
            debuglog.log("engine", f"录音器启动失败：{e}", exc=True)
            recorder, meta_fh = None, None
    else:
        debuglog.log("engine", "录音存档未开启（record != 1）：PTT 期间不落盘")

    # 降噪（可选）：ffmpeg 黄金人声降噪链（二阶高通切除桌面共振 + 自适应对齐底噪平滑降噪 + 高频平滑）
    ff = None
    denoise = False
    try:
        flag = BASE / "denoise"
        denoise = flag.exists() and flag.read_text().strip() == "1"
    except Exception:
        pass
    if denoise and ch == 1 and bits == 16:
        try:
            # highpass=f=85:poles=2: 二阶 Butterworth 高通彻底切除 <85Hz 桌面震动与握持风噪；
            # afftdn=nr=12:nf=-48:tn=1:gs=4: 噪声底 -48dBFS 对齐实测底噪，tn=1 跟踪风扇变化，gs=4 平滑频域彻底消除金属电音；
            # lowpass=f=12000:poles=1: 滤除 >12kHz 开关电源与高频杂散底噪，听感更沉静温暖
            flt_chain = "highpass=f=85:poles=2,afftdn=nr=12:nf=-48:tn=1:gs=4,lowpass=f=12000:poles=1"
            ff = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 "-probesize", "32", "-analyzeduration", "0",
                 "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                 "-af", flt_chain,
                 "-f", "s16le", "-ar", str(rate), "-ac", "1", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

            def feeder():
                try:
                    while not stop.is_set():
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        ff.stdin.write(chunk)
                except Exception:
                    pass
                finally:
                    try:
                        ff.stdin.close()
                    except Exception:
                        pass

            threading.Thread(target=feeder, daemon=True).start()
            source = ff.stdout
            print("[降噪] 已启用：黄金人声降噪链（二阶高通85Hz+自适应底噪对齐+平滑增益gs=4+高频修整）", flush=True)
            debuglog.log("engine", f"降噪管道已启动: {flt_chain}")
        except Exception as e:
            ff = None
            source = resp
            debuglog.log("engine", f"降噪启动异常: {e}")
    else:
        source = resp

    # 录音器挂在最终流（开启降噪时录下纯净降噪人声，所听即所录）
    source = StreamTee(source, recorder, payload)
    payload = b""

    frame_bytes = ch * bits // 8
    byte_rate = rate * frame_bytes
    stat = {"underruns": 0, "bytes": 0}
    # 黄金抗抖动低延迟队列：容量 10 个 chunk（约 426ms），平稳时维持在 3~6 个 chunk（130~250ms）
    # 既有充足抗抖动裕量（0 欠载、音频连续不碎断），又严格将最大延迟锁死在 0.4 秒内（不截断、实时同步）
    q: queue.Queue = queue.Queue(maxsize=10)
    # 心跳：记录"最后一次收到数据"与"最后一次回调被声卡驱动调用"的时刻，
    # 用于区分「手机没送数据」与「声卡回调卡死/系统睡眠唤醒后未恢复」
    hb = {"data": time.time(), "cb": time.time(), "cb_count": 0,
          "cb_started": False, "t_start": time.time(),
          "src": "ffmpeg-pipe" if ff is not None else "http"}

    def producer():
        try:
            while not stop.is_set():
                chunk = source.read(4096)
                if not chunk:
                    raise ConnectionError("音频流结束")
                stat["bytes"] += len(chunk)
                now = time.time()
                gap = now - hb["data"]
                if gap > 2:   # 长时间无数据后恢复：记录一次，便于定位手机/Wi-Fi 抖动
                    debuglog.log("engine", f"数据恢复：断流 {gap:.1f}s 后重新收到音频"
                                           f"（源={hb['src']}）")
                hb["data"] = now
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    try:
                        q.get_nowait()   # 满队时仅丢弃 1 个最旧的 chunk，保持队列在 400ms 上限
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        pass
        except (TimeoutError, socket.timeout):
            if not stop.is_set():
                print("\n[连接] 断开：3 秒无数据（手机离网或 Wi-Fi 中断）", flush=True)
                debuglog.log("engine", "producer 读超时：3 秒无数据（手机离网/Wi-Fi 中断）")
        except Exception as e:
            if not stop.is_set():
                print(f"\n[连接] 断开：{e}", flush=True)
                debuglog.log("engine", f"producer 异常退出：{type(e).__name__}: {e}", exc=True)

    rem = bytearray()
    ptt_sync = {"on": False, "t": 0.0}
    gate_sync = {"on": True, "t": 0.0}

    def callback(outdata, frames, _t, _status):
        if not hb["cb_started"]:
            hb["cb_started"] = True
            debuglog.log("engine",
                         f"声卡首次回调就位（距会话开始 "
                         f"{time.time() - hb['t_start']:.1f}s）")
        now = time.time()
        hb["cb"] = now
        hb["cb_count"] += 1

        # 1. 快速同步 PTT 状态（20ms 周期）
        if now - ptt_sync["t"] > 0.02:
            ptt_sync["t"] = now
            try:
                cur_ptt = PTT_FILE.exists() and PTT_FILE.read_text().strip() == "1"
            except Exception:
                cur_ptt = False
            # 方案三：检测到 PTT 翻转为开（录音开始瞬间）→ 立即冲刷清空在途队列与缓存
            if cur_ptt and not ptt_sync["on"]:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                rem.clear()
            ptt_sync["on"] = cur_ptt

        # 2. 定期同步门控开关配置（200ms 周期，默认开启）
        if now - gate_sync["t"] > 0.2:
            gate_sync["t"] = now
            try:
                gate_sync["on"] = (not GATE_FILE.exists()) or (GATE_FILE.read_text().strip() != "0")
            except Exception:
                gate_sync["on"] = True

        need = frames * frame_bytes
        while len(rem) < need:
            try:
                rem.extend(q.get_nowait())
            except queue.Empty:
                stat["underruns"] += 1
                rem.extend(b"\x00" * (need - len(rem)))
                break
        data = rem[:need]
        del rem[:need]
        if dtype == "int16":
            out, limited, peak = process_pcm16(bytes(data), GAIN_DB["v"])
            stat["limited"] = stat.get("limited", 0) + limited
            # 方案一：语音输入门控模式
            # 若门控开启且当前未处于录音状态，向 BlackHole 输出全 0（绝对静音），
            # 彻底清空外部输入法（微信输入法等）的 Lookback 回溯缓冲
            if gate_sync["on"] and not ptt_sync["on"]:
                outdata.fill(0)
            else:
                outdata[:] = out.reshape(frames, ch)
        else:
            peak = 0
            if gate_sync["on"] and not ptt_sync["on"]:
                outdata.fill(0)
            else:
                outdata[:] = np.frombuffer(data, dtype=dtype).reshape(frames, ch)
        stat["peak"] = max(peak, stat.get("peak", 0) * 85 // 100)

    t = threading.Thread(target=producer, daemon=True)
    t.start()

    def watchdog():
        """每 2 秒体检一次：检测 USB 插入无缝升级、信号文件、断流超时与声卡卡死。"""
        warned_data = warned_cb = False
        while not stop.is_set() and t.is_alive():
            time.sleep(2)
            if stop.is_set():
                return

            # 1. 响应菜单栏「立即重连」信号
            if RECONNECT_FILE.exists():
                try:
                    RECONNECT_FILE.unlink()
                except Exception:
                    pass
                debuglog.log("engine", "watchdog 消费立即重连信号，主动中断当前会话")
                print("\n[连接] 收到立即重连信号，正在重新发现手机…", flush=True)
                stop.set()
                return

            # 2. 运行期间 USB 热插拔无缝升级：无线流下插入 USB 线立即切到 USB 直连
            if not url.startswith("http://127.0.0.1:"):
                now_usb = check_usb_device()
                if now_usb:
                    print("\n[连接] ⚡ 检测到 USB 数据线插入，正在自动升级为 USB 极速模式…", flush=True)
                    debuglog.log("engine", "检测到 USB 插入，触发升级至 USB 极速模式")
                    stop.set()
                    return

            now = time.time()
            since_data, since_cb = now - hb["data"], now - hb["cb"]
            buffered = q.qsize() * 4096 / byte_rate * 1000

            if not hb["cb_started"]:
                waited = now - hb["t_start"]
                if waited > 15:
                    debuglog.log("engine",
                                 f"⚠️ 会话已启动 {waited:.0f}s 但声卡从未回调"
                                 f"（源={hb['src']} 缓冲{buffered:.0f}ms "
                                 f"producer存活={t.is_alive()}）")
                    debuglog.dump_thread("engine", t.ident, "producer")
                    stop.set()
                    return
                continue

            # 3. 声卡驱动回调停摆检测（睡眠唤醒/声卡被卸载）
            if since_cb > 4:
                debuglog.log("engine", f"⚠️ 声卡回调停摆 {since_cb:.1f}s，主动重置声卡输出流")
                print("\n[音频] 声卡回调停摆，自动重建声卡输出…", flush=True)
                stop.set()
                return

            # 4. 音频流断流熔断：手机离线或读阻塞超过 6 秒，主动断开触发重连
            if since_data > 6:
                debuglog.log("engine",
                             f"⚠️ 断流超时：无数据 {since_data:.1f}s "
                             f"（源={hb['src']} 缓冲{buffered:.0f}ms 峰值{stat.get('peak', 0)}% "
                             f"欠载{stat['underruns']} producer存活={t.is_alive()}），主动熔断重连")
                print(f"\n[连接] 超过 {since_data:.1f}s 未收到音频，自动重新寻找手机…", flush=True)
                stop.set()
                return

            if since_data > 3:
                debuglog.log("engine",
                             f"⚠️ 抖动预警：无数据 {since_data:.1f}s "
                             f"（源={hb['src']} 缓冲{buffered:.0f}ms 峰值{stat.get('peak', 0)}%）")
                continue

            warned_data = warned_cb = False
            debuglog.log("engine",
                         f"心跳正常：缓冲{buffered:.0f}ms 峰值{stat.get('peak', 0)}% "
                         f"欠载{stat['underruns']} 回调{hb['cb_count']} "
                         f"累计{stat['bytes'] // 1024}KB 源={hb['src']}")

    threading.Thread(target=watchdog, daemon=True).start()

    # 预缓冲：让队列先攒一点，抗网络抖动
    time.sleep(PREFILL_SECONDS)

    print(f"[音频] {rate} Hz / {ch} ch / {bits}bit，写入 BlackHole…（Ctrl+C 退出）", flush=True)
    debuglog.log("engine", f"开始推流：{rate}Hz {ch}ch {bits}bit 源={hb['src']} "
                           f"降噪={denoise} 输出设备={out_idx} url={url}")
    try:
        with sd.OutputStream(device=out_idx, samplerate=rate, channels=ch,
                             dtype=dtype, callback=callback):
            last_report = 0.0
            while not stop.is_set() and t.is_alive():
                time.sleep(0.5)
                if RECONNECT_FILE.exists():
                    try:
                        RECONNECT_FILE.unlink()
                    except Exception:
                        pass
                    debuglog.log("engine", "收到立即重连信号，主动中断当前推流")
                    print("\n[连接] 收到立即重连信号，正在重新寻找手机…", flush=True)
                    stop.set()
                    break
                now = time.time()
                try:
                    LEVEL_FILE.write_text(str(stat.get("peak", 0)))
                except Exception:
                    pass
                if meta_fh:
                    try:
                        meta_fh.write(f"{now - t0:.1f},{stat.get('peak', 0)},"
                                      f"{stat['underruns']},{stat.get('limited', 0)}\n")
                        meta_fh.flush()
                    except Exception:
                        pass
                buffered_ms = q.qsize() * 4096 / byte_rate * 1000
                if now - last_report > 10:
                    last_report = now
                    print(f"[状态] 运行中 缓冲≈{buffered_ms:.0f}ms 峰值{stat.get('peak', 0)}% "
                          f"欠载{stat['underruns']}次 限幅{stat.get('limited', 0)}次", flush=True)
    finally:
        # 无论正常结束还是异常/SIGTERM 退出，录音段都要正确收尾（回写头部长度）
        if recorder:
            try:
                recorder.close()
            except Exception:
                pass
        if meta_fh:
            try:
                meta_fh.close()
            except Exception:
                pass
        if ff is not None:
            try:
                ff.kill()
            except Exception:
                pass
        try:
            raw_resp.close()   # 主动关流：手机端收 RST 后清掉客户端、恢复 UDP 公告
        except Exception:
            pass
    reason = "收到停止信号" if stop.is_set() else (
        "producer 已退出（流中断）" if not t.is_alive() else "主循环未知原因退出")
    debuglog.log("engine", f"推流结束：{reason}，会话 {time.time() - t0:.0f}s "
                           f"累计{stat['bytes'] // 1024}KB 欠载{stat['underruns']} "
                           f"回调{hb['cb_count']}")
    raise ConnectionError("音频流结束（服务器关闭或流中断），准备重连")


def main():
    debuglog.install("engine")
    sys.stdout = _PipeSafeStdout(sys.stdout)
    sys.stderr = _PipeSafeStdout(sys.stderr)
    ap = argparse.ArgumentParser(description="把手机麦克风变成 Mac 输入设备")
    ap.add_argument("url", nargs="?", help="手机音频流地址（不填则自动发现）")
    ap.add_argument("--auto", action="store_true", help="自动发现模式（发现→缓存→扫描 三级回退）")
    ap.add_argument("--list", action="store_true", help="列出输出设备")
    ap.add_argument("--device", help="输出设备名（默认自动找 BlackHole）")
    args = ap.parse_args()

    if args.list:
        print(sd.query_devices())
        return

    if not acquire_lock():
        print("[锁] 已有 PhoneMic 实例在运行，本次退出。", flush=True)
        return

    out_idx, out_name = find_output(args.device or OUTPUT_HINT)
    print(f"[输出] {out_name}", flush=True)
    threading.Thread(target=gain_watcher, daemon=True).start()
    threading.Thread(target=udp_announce_listener, daemon=True).start()

    stop = threading.Event()
    # 菜单栏应用用 SIGTERM 结束引擎：置 stop 让录音收尾/资源释放走正常路径，而不是被硬杀
    signal.signal(signal.SIGTERM, lambda *_: (debuglog.log("engine", "收到 SIGTERM，停止推流"),
                                              stop.set()))
    fails = 0

    def wait_interruptible(seconds: float, reason: str = ""):
        """可被 打断的等待：Ctrl+C / 菜单「立即重连」/ 手机新公告。"""
        if reason:
            print(f"{reason}（Ctrl+C 退出，菜单「立即重连」可跳过）", flush=True)
        ann_at = _ANNOUNCE["at"]
        end = time.time() + seconds
        while time.time() < end and not stop.is_set():
            if reconnect_requested() or _ANNOUNCE["at"] != ann_at:
                return   # 手机回来了 / 用户点了立即重连：立刻进入发现
            time.sleep(0.1)

    try:
        while not stop.is_set():
            # 地址解析：显式地址只在首次用；之后每次重连都重新发现（--auto 时）
            url = args.url
            if args.auto or not url:
                url = resolve_url(None if args.auto else (args.url if fails == 0 else None))
            if not url:
                wait_interruptible(2, "[发现] 2 秒后重新寻找手机…")
                continue
            # 地址缓存由 stream_once 在真正连上之后写，见那里的注释
            t_stream = time.time()
            debuglog.log("engine", f"尝试连接 {url}（第 {fails + 1} 次尝试）")
            try:
                stream_once(url, out_idx, stop)
                # stream_once 结束（如手机端重载或断流），重置计数并继续循环自动发现重连
                fails = 0
                time.sleep(0.5)
            except KeyboardInterrupt:
                stop.set()
                print("\n已退出", flush=True)
                debuglog.log("engine", "用户 Ctrl+C 退出")
                return
            except Exception as e:
                # 上次是真会话（连上并正常跑了一阵）则不算连接失败，退避重新计数
                dur = time.time() - t_stream
                if dur > 15:
                    fails = 0
                fails += 1
                backoff = min(fails, 6)   # 1,2,3…6：首次失败 1 秒后就重试（发现本身很快）
                debuglog.log("engine", f"连接失败（本次会话 {dur:.1f}s，第 {fails} 次）："
                                       f"{type(e).__name__}: {e}", exc=True)
                wait_interruptible(backoff, f"[连接] {e}（第 {fails} 次失败），{backoff}s 后重试")
    except KeyboardInterrupt:
        stop.set()
        print("\n已退出", flush=True)


if __name__ == "__main__":
    main()
