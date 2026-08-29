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
import queue
import shutil
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

BASE = Path.home() / "GitHub" / "PhoneMic"
LAST_URL_FILE = BASE / ".phonemic_last_url"
LOCK_FILE = BASE / ".phonemic_lock"
GAIN_FILE = BASE / "gain_db"        # 数字增益（dB），菜单栏应用写入，引擎每秒读取
LEVEL_FILE = BASE / ".level"        # 引擎实时输出电平（0~100），菜单栏读取显示
RECORD_FILE = BASE / "record"       # 录音存档开关（"1"=开启）
REC_DIR = BASE / "recordings"       # 录音与指标文件目录
OUTPUT_HINT = "BlackHole"
PREFILL_SECONDS = 0.30      # 预缓冲：300ms 常数延迟换零欠载（欠载丢音节=识别错误）
CANDIDATE_PORTS = [8081, 18080, 28080, 8080]
DTYPES = {8: "uint8", 16: "int16", 32: "int32"}
GAIN_DB = {"v": 0.0}

# 绕过系统代理/环境变量，局域网直连（代理会对局域网流间歇性返回 404）
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def acquire_lock() -> bool:
    """保证全机只有一个 PhoneMic 实例在写 BlackHole。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(__import__("os").getpid()))
        fh.flush()
        return True
    except OSError:
        return False


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
                    ip = socket.inet_ntoa(info.addresses[0])
                    result[f"http://{ip}:{info.port}"] = name
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


def scan_host_for_riff(host: str, timeout: float = 1.5) -> str | None:
    """对一台主机的候选端口发探测请求，返回第一个返回 WAV 流的地址。"""
    for port in CANDIDATE_PORTS:
        url = f"http://{host}:{port}/audio.wav"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PhoneMic/1.0"})
            resp = DIRECT_OPENER.open(req, timeout=timeout)
            head = resp.read(4)
            resp.close()
            if head == b"RIFF":
                return url
        except Exception:
            continue
    return None


def resolve_url(explicit: str | None) -> str | None:
    """显式地址 > mDNS > 上次地址 > 上次主机扫端口。"""
    if explicit:
        return explicit
    print("[发现] 正在局域网寻找手机（mDNS，最多 4 秒）…", flush=True)
    url = discover_mdns()
    if url:
        print(f"[发现] mDNS 找到：{url}", flush=True)
        return url
    last = LAST_URL_FILE.read_text().strip() if LAST_URL_FILE.exists() else None
    if last:
        print(f"[发现] mDNS 未找到，尝试上次地址：{last}", flush=True)
        if probe_ok(last):
            return last
        host = last.split("//")[-1].split(":")[0]
        print(f"[发现] 上次地址失效，扫描 {host} 的候选端口…", flush=True)
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


class StreamTee:
    """代理音频流：先把头部 payload 回放给下游，之后透传；可选同步写入录音文件。"""

    def __init__(self, resp, wav, initial):
        self.resp = resp
        self.wav = wav
        self.buf = initial

    def read(self, n):
        if self.buf:
            chunk, self.buf = self.buf[:n], self.buf[n:]
        else:
            chunk = self.resp.read(n)
            if not chunk:
                return chunk
        if self.wav:
            try:
                self.wav.writeframesraw(chunk)
            except Exception:
                pass
        return chunk


def stream_once(url: str, out_idx: int, stop: threading.Event) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "PhoneMic/1.0"})
    resp = DIRECT_OPENER.open(req, timeout=6)
    payload, (ch, rate, bits) = wav_head_and_payload(resp)
    dtype = DTYPES.get(bits)
    if dtype is None:
        raise ValueError(f"不支持的位深 {bits}bit（请在手机端把音频格式设为 PCM 16bit）")
    t0 = time.time()

    # 录音存档（可选）：记录手机采集的原始音频 + 电平/指标时间线
    rec_wav, meta_fh, rec_stamp = None, None, time.strftime("%Y%m%d-%H%M%S")
    if recording_enabled():
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            import wave
            rec_wav = wave.open(str(REC_DIR / f"{rec_stamp}.wav"), "wb")
            rec_wav.setnchannels(ch)
            rec_wav.setsampwidth(bits // 8)
            rec_wav.setframerate(rate)
            rec_wav.writeframesraw(payload)
            meta_fh = open(REC_DIR / f"{rec_stamp}.meta.csv", "w")
            meta_fh.write("elapsed_sec,level_pct,underruns,clipped\n")
            print(f"[录音] 存档中：{rec_stamp}.wav（原始信号，未加增益/降噪）", flush=True)
        except Exception as e:
            print(f"[录音] 启动失败：{e}", flush=True)
            rec_wav, meta_fh = None, None
    resp = StreamTee(resp, rec_wav, payload)
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
                 "-af", "afftdn=nr=14:nf=-40:tn=1",
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
    q: queue.Queue = queue.Queue(maxsize=max(4, byte_rate // 4096))

    def producer():
        try:
            while not stop.is_set():
                chunk = source.read(4096)
                if not chunk:
                    raise ConnectionError("音频流结束")
                stat["bytes"] += len(chunk)
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    try:
                        q.get_nowait()   # 丢最旧的，保持低延迟
                    except queue.Empty:
                        pass
                    q.put_nowait(chunk)
        except Exception as e:
            if not stop.is_set():
                print(f"\n[连接] 断开：{e}", flush=True)

    rem = bytearray()

    def callback(outdata, frames, _t, _status):
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

    # 预缓冲：让队列先攒一点，抗网络抖动
    time.sleep(PREFILL_SECONDS)

    print(f"[音频] {rate} Hz / {ch} ch / {bits}bit，写入 BlackHole…（Ctrl+C 退出）", flush=True)
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
    if rec_wav:
        try:
            rec_wav.close()
        except Exception:
            pass
    if meta_fh:
        try:
            meta_fh.close()
        except Exception:
            pass
    if rec_wav:
        wav_path = REC_DIR / f"{rec_stamp}.wav"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            try:
                subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path),
                                "-c:a", "flac", str(wav_path.with_suffix(".flac"))],
                               check=True, capture_output=True)
                wav_path.unlink()
                print(f"[录音] 已转存 FLAC：{rec_stamp}.flac", flush=True)
            except Exception:
                print(f"[录音] 保留 WAV：{wav_path.name}（转码失败）", flush=True)
    if ff is not None:
        try:
            ff.kill()
        except Exception:
            pass
    raise ConnectionError("音频流结束（服务器关闭或流中断），准备重连")


def main():
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

    stop = threading.Event()
    backoff = 2
    fails = 0
    while not stop.is_set():
        # 地址解析：显式地址只在首次用；之后每次重连都重新发现（--auto 时）
        url = args.url
        if args.auto or not url:
            url = resolve_url(None if args.auto else (args.url if fails == 0 else None))
        if not url:
            print("[发现] 8 秒后重新寻找手机…（Ctrl+C 退出）", flush=True)
            stop.wait(8)
            continue
        if fails == 0 or args.auto:
            try:
                LAST_URL_FILE.write_text(url)
            except Exception:
                pass
        try:
            stream_once(url, out_idx, stop)
            return
        except KeyboardInterrupt:
            stop.set()
            print("\n已退出", flush=True)
            return
        except Exception as e:
            fails += 1
            backoff = min(2 * (2 ** min(fails, 3)), 10)
            print(f"[连接] {e}（连续失败 {fails} 次），{backoff}s 后重试", flush=True)
            stop.wait(backoff)


if __name__ == "__main__":
    main()
