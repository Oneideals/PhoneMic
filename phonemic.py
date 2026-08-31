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
import queue
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
REC_DIR = BASE / "recordings"       # 录音与指标文件目录
PTT_MIN_SEGMENT = 0.3               # 短于此秒数的录音段视为误触，丢弃
RECONNECT_FILE = BASE / ".reconnect"   # 菜单栏「立即重连」信号（存在即触发）
UDP_ANNOUNCE_PORT = 58080           # 电脑监听：手机 UDP 公告（mDNS 失效时的兜底通道）
UDP_QUERY_PORT = 58081              # 手机监听：电脑 UDP 查询（手动重连快车道）
OUTPUT_HINT = "BlackHole"
PREFILL_SECONDS = 0.30      # 预缓冲：300ms 常数延迟换零欠载（欠载丢音节=识别错误）
CANDIDATE_PORTS = [8081, 18080, 28080, 8080]
DTYPES = {8: "uint8", 16: "int16", 32: "int32"}
GAIN_DB = {"v": 0.0}

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
    """保证全机只有一个 PhoneMic 实例在写 BlackHole。"""
    global _LOCK_FH
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(__import__("os").getpid()))
        fh.flush()
    except OSError:
        debuglog.log("engine", "单实例锁已被占用，本次退出")
        return False
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
            req = urllib.request.Request(url, headers={"User-Agent": "PhoneMic/1.0"})
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
_ANNOUNCE = {"url": None, "at": 0.0}


def udp_announce_listener():
    """常驻后台：监听手机 'PHONEMIC <port>' 公告，更新缓存。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", UDP_ANNOUNCE_PORT))
    except Exception:
        return   # 端口被占（如另一实例）：公告通道让位于锁持有者
    while True:
        try:
            data, addr = s.recvfrom(256)
            msg = data.decode("utf-8", "ignore").strip()
            if msg.startswith("PHONEMIC ") and msg[8:].strip().isdigit():
                _ANNOUNCE["url"] = f"http://{addr[0]}:{msg[8:].strip()}"
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


def resolve_url(explicit: str | None) -> str | None:
    """显式地址 > 新公告 > （上次地址 ∥ mDNS ∥ UDP 查询）并行 > 上次主机扫端口。"""
    if explicit:
        return explicit
    t_start = time.time()
    if _ANNOUNCE["url"] and t_start - _ANNOUNCE["at"] < 5:
        print(f"[发现] UDP 公告命中：{_ANNOUNCE['url']}", flush=True)
        return _ANNOUNCE["url"]

    print("[发现] 正在寻找手机（公告/上次地址/mDNS 并行）…", flush=True)
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
        if box["last"]:
            print(f"[发现] 上次地址仍可用：{box['last']}", flush=True)
            return box["last"]                      # IP 未变，最快路径
        if _ANNOUNCE["url"] and _ANNOUNCE["at"] >= t_start:
            print(f"[发现] UDP 查询应答：{_ANNOUNCE['url']}", flush=True)
            return _ANNOUNCE["url"]                  # 查询带回的新公告
        if box["mdns"]:
            print(f"[发现] mDNS 找到：{box['mdns']}", flush=True)
            return box["mdns"]
        time.sleep(0.1)

    # 超时兜底：扫上次主机的候选端口（IP 变了但主机在线的情况）
    if last:
        # 扫描前再查一次公告（等待循环结束到这里的间隙里手机可能刚好回来）
        if _ANNOUNCE["url"] and _ANNOUNCE["at"] >= t_start:
            print(f"[发现] UDP 查询应答：{_ANNOUNCE['url']}", flush=True)
            return _ANNOUNCE["url"]
        host = last.split("//")[-1].split(":")[0]
        url = scan_host_for_riff(host)
        if url:
            print(f"[发现] 扫描命中：{url}", flush=True)
            return url
    print("[发现] 找不到手机（App 是否已启动？是否同一 Wi-Fi？）", flush=True)
    return None


def probe_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PhoneMic/1.0"})
        resp = DIRECT_OPENER.open(req, timeout=2)
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
                GAIN_DB["v"] = max(0.0, min(18.0, v))
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
                except Exception:
                    pass
        elif self.wav is not None:
            self.close_segment()

    def close_segment(self) -> None:
        wav, stamp = self.wav, self.stamp
        self.wav, self.stamp = None, None
        if wav is None:
            return
        try:
            dur = wav.getnframes() / self.rate
            wav.close()
        except Exception:
            return
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


def stream_once(url: str, out_idx: int, stop: threading.Event) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "PhoneMic/1.0"})
    # timeout 同时约束连接与每次 read：手机离开 WiFi 范围时不会有 TCP RST，
    # 靠这个读超时把"无限挂起"压到 3 秒内检测（正常流每 ~128ms 就有一块数据）
    resp = DIRECT_OPENER.open(req, timeout=3)
    raw_resp = resp
    payload, (ch, rate, bits) = wav_head_and_payload(resp)
    dtype = DTYPES.get(bits)
    if dtype is None:
        raise ValueError(f"不支持的位深 {bits}bit（请在手机端把音频格式设为 PCM 16bit）")
    t0 = time.time()

    # 录音存档（可选，PTT 模式）：只有按住右 Option 期间的音频才落盘；
    # 电平/指标时间线仍全程记录（体积可忽略）
    recorder, meta_fh, rec_stamp = None, None, time.strftime("%Y%m%d-%H%M%S")
    if recording_enabled():
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            recorder = PttRecorder(ch, rate, bits)
            meta_fh = open(REC_DIR / f"{rec_stamp}.meta.csv", "w")
            meta_fh.write("elapsed_sec,level_pct,underruns,clipped\n")
            print("[录音] PTT 模式：按右⌥开始记录，再按结束并存档 FLAC", flush=True)
            debuglog.log("engine", f"录音器就绪：PTT 模式，指标文件 {rec_stamp}.meta.csv")
        except Exception as e:
            print(f"[录音] 启动失败：{e}", flush=True)
            debuglog.log("engine", f"录音器启动失败：{e}", exc=True)
            recorder, meta_fh = None, None
    else:
        debuglog.log("engine", "录音存档未开启（record != 1）：PTT 期间不落盘")
    resp = StreamTee(resp, recorder, payload)
    payload = b""

    # 降噪（可选）：ffmpeg afftdn 过滤电脑风扇等稳态噪声
    ff = None
    denoise = False
    try:
        flag = BASE / "denoise"
        denoise = flag.exists() and flag.read_text().strip() == "1"
    except Exception:
        pass
    if denoise and ch == 1 and bits == 16:
        try:
            ff = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                 "-af", "highpass=f=80,afftdn=nr=8:nf=-60:tn=0",
                 "-f", "s16le", "-ar", str(rate), "-ac", "1", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            def feeder():
                try:
                    ff.stdin.write(payload)
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
            source, payload = ff.stdout, b""
            print("[降噪] 已启用：过滤风扇等稳态噪声（afftdn）", flush=True)
        except Exception:
            ff = None
            source = resp
    else:
        source = resp

    frame_bytes = ch * bits // 8
    byte_rate = rate * frame_bytes
    stat = {"underruns": 0, "bytes": 0}
    # 队列容量提升至 3 秒（约 70 个 chunk），平滑 Wi-Fi 突发抖动，防止中间丢字
    q: queue.Queue = queue.Queue(maxsize=max(8, (byte_rate * 3) // 4096))
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
                        q.get_nowait()   # 丢最旧的，保持低延迟
                    except queue.Empty:
                        pass
                    q.put_nowait(chunk)
        except (TimeoutError, socket.timeout):
            if not stop.is_set():
                print("\n[连接] 断开：3 秒无数据（手机离网或 Wi-Fi 中断）", flush=True)
                debuglog.log("engine", "producer 读超时：3 秒无数据（手机离网/Wi-Fi 中断）")
        except Exception as e:
            if not stop.is_set():
                print(f"\n[连接] 断开：{e}", flush=True)
                debuglog.log("engine", f"producer 异常退出：{type(e).__name__}: {e}", exc=True)

    rem = bytearray()

    def callback(outdata, frames, _t, _status):
        if not hb["cb_started"]:
            hb["cb_started"] = True
            debuglog.log("engine",
                         f"声卡首次回调就位（距会话开始 "
                         f"{time.time() - hb['t_start']:.1f}s）")
        hb["cb"] = time.time()
        hb["cb_count"] += 1
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
            arr = np.frombuffer(data, dtype=dtype).astype(np.float32) * (10 ** (GAIN_DB["v"] / 20.0))
            np.clip(arr, -32768, 32767, out=arr)
            stat["clipped"] = stat.get("clipped", 0) + int(np.count_nonzero(np.abs(arr) >= 32600))
            peak = int(np.abs(arr).max()) * 100 // 32768
            outdata[:] = arr.astype(dtype).reshape(frames, ch)
        else:
            peak = 0
            outdata[:] = np.frombuffer(data, dtype=dtype).reshape(frames, ch)
        stat["peak"] = max(peak, stat.get("peak", 0) * 85 // 100)

    t = threading.Thread(target=producer, daemon=True)
    t.start()

    def watchdog():
        """每 5 秒体检一次：区分「没数据」和「声卡回调卡死」，卡死时打印线程栈。"""
        warned_data = warned_cb = False
        while not stop.is_set() and t.is_alive():
            time.sleep(5)
            if stop.is_set():
                return
            now = time.time()
            since_data, since_cb = now - hb["data"], now - hb["cb"]
            buffered = q.qsize() * 4096 / byte_rate * 1000
            if not hb["cb_started"]:
                # 会话启动阶段（发现手机→建流→开 OutputStream 都在首次回调之前），
                # 此时"尚无回调"属正常，不能误判为停摆；超过 15s 才算真异常
                waited = now - hb["t_start"]
                if waited > 15:
                    debuglog.log("engine",
                                 f"⚠️ 会话已启动 {waited:.0f}s 但声卡从未回调"
                                 f"（源={hb['src']} 缓冲{buffered:.0f}ms "
                                 f"producer存活={t.is_alive()}）")
                    debuglog.dump_thread("engine", t.ident, "producer")
                continue
            if since_data > 5 or since_cb > 2:
                debuglog.log("engine",
                             f"⚠️ 异常：无数据 {since_data:.1f}s / 回调停滞 {since_cb:.1f}s "
                             f"（源={hb['src']} 缓冲{buffered:.0f}ms 峰值{stat.get('peak', 0)}% "
                             f"欠载{stat['underruns']} 回调数{hb['cb_count']} "
                             f"producer存活={t.is_alive()}）")
                if since_data > 8 and not warned_data:
                    warned_data = True
                    debuglog.dump_thread("engine", t.ident, "producer")
                if since_cb > 3 and not warned_cb:
                    warned_cb = True
                    debuglog.log("engine",
                                 "声卡回调停摆：PortAudio 未再调用 callback"
                                 "（常见原因：系统睡眠唤醒后设备失效 / 设备被切换 / 输出设备被占用）")
                continue
            warned_data = warned_cb = False
            debuglog.log("engine",
                         f"心跳正常：缓冲{buffered:.0f}ms 峰值{stat.get('peak', 0)}% "
                         f"欠载{stat['underruns']} 回调{hb['cb_count']}/5s "
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
                now = time.time()
                try:
                    LEVEL_FILE.write_text(str(stat.get("peak", 0)))
                except Exception:
                    pass
                if meta_fh:
                    try:
                        meta_fh.write(f"{now - t0:.1f},{stat.get('peak', 0)},"
                                      f"{stat['underruns']},{stat.get('clipped', 0)}\n")
                        meta_fh.flush()
                    except Exception:
                        pass
                buffered_ms = q.qsize() * 4096 / byte_rate * 1000
                if now - last_report > 10:
                    last_report = now
                    print(f"[状态] 运行中 缓冲≈{buffered_ms:.0f}ms 峰值{stat.get('peak', 0)}% "
                          f"欠载{stat['underruns']}次 削波{stat.get('clipped', 0)}次", flush=True)
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
            if fails == 0 or args.auto:
                try:
                    LAST_URL_FILE.write_text(url)
                except Exception:
                    pass
            t_stream = time.time()
            debuglog.log("engine", f"尝试连接 {url}（第 {fails + 1} 次尝试）")
            try:
                stream_once(url, out_idx, stop)
                return
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
