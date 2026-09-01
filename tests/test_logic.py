#!/usr/bin/env python3
"""PhoneMic 纯逻辑测试（无需 BlackHole、无需手机、无需声卡）。

与 test_mac.py 的分工：
  - test_mac.py   集成测试，需要本机 BlackHole，验证端到端拉流
  - test_logic.py 纯逻辑测试，CI 可直接跑，覆盖状态管理与协议解析

运行： python tests/test_logic.py
"""
import fcntl
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ---------- 1. 单实例锁：抢锁失败者不得抹掉持锁者的 PID ----------

def test_lock_preserves_holder_pid():
    """P0-1 回归测试：后来者 open(w) 截断会把持锁者 PID 擦成空文件，
    导致菜单栏的孤儿引擎检测永久失效、每 2 秒空转拉起一个新引擎。"""
    import phonemic

    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / "lock"
        orig, phonemic.LOCK_FILE = phonemic.LOCK_FILE, lock
        orig_fh, phonemic._LOCK_FH = phonemic._LOCK_FH, None
        try:
            first = phonemic.acquire_lock()
            held_pid = lock.read_text().strip()
            keep_fh = phonemic._LOCK_FH      # 持住，别让 GC 释放 flock

            phonemic._LOCK_FH = None
            second = phonemic.acquire_lock()
            after = lock.read_text().strip()

            check("单实例锁: 首个实例拿到锁", first is True)
            check("单实例锁: 第二个实例被拒绝", second is False)
            check("单实例锁: 锁文件写入了持锁者 PID", held_pid == str(os.getpid()), f"got {held_pid!r}")
            check("单实例锁: 抢锁失败后持锁者 PID 仍在（孤儿检测依赖它）",
                  after == str(os.getpid()), f"got {after!r}，被截断了")
            keep_fh.close()
        finally:
            phonemic.LOCK_FILE, phonemic._LOCK_FH = orig, orig_fh


# ---------- 2. udp:// 地址的存活探测 ----------

class FakeUdpPhone(threading.Thread):
    """假手机的 UDP 音频端：收到带正确 token 的 START 就回一个 PMIC 音频包。"""

    daemon = True

    def __init__(self, token=None):
        super().__init__()
        self.token = token
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self.rejected = []
        self.running = True

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            except Exception:
                return
            msg = data.decode("utf-8", "ignore").strip()
            if not msg.startswith("PHONEMIC_UDP_START"):
                continue
            parts = msg.split()
            got = parts[1] if len(parts) >= 2 else None
            if self.token is not None and got != self.token:
                self.rejected.append(got)
                continue
            pkt = b"PMIC" + struct.pack(">I", 1) + b"\x00" * 960
            try:
                self.sock.sendto(pkt, addr)
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


def test_probe_ok_accepts_udp_url():
    """P0-3 回归测试：main() 会把 udp:// 写进 .phonemic_last_url，
    若 probe_ok 不认这个 scheme，「上次地址」快车道就永久失效。"""
    import phonemic

    phone = FakeUdpPhone()
    phone.start()
    try:
        ok = phonemic.probe_ok(f"udp://127.0.0.1:{phone.port}")
        check("udp 探活: 活着的 UDP 端被判为可用", ok is True)
    finally:
        phone.stop()

    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    t0 = time.time()
    ok = phonemic.probe_ok(f"udp://127.0.0.1:{dead_port}")
    check("udp 探活: 死掉的 UDP 端被判为不可用", ok is False)
    check("udp 探活: 探测不超过 3 秒", time.time() - t0 < 3.0, f"{time.time() - t0:.1f}s")


# ---------- 3. 增益 + 软限幅 ----------

def test_gain_limit_counts_engagements():
    """P2-1 回归测试：软限幅上限 32000 < 削波判定阈值 32600，
    导致「削波次数」这个诊断指标恒为 0，等于没有。"""
    import numpy as np
    import phonemic

    hot = np.array([30000, -30000, 100, 0], dtype=np.int16).tobytes()
    out, limited, peak = phonemic.process_pcm16(hot, 0.0)
    check("限幅: 超阈值采样被计入限幅次数", limited == 2, f"got {limited}")
    check("限幅: 输出不超过 int16 上限", int(np.abs(out).max()) <= 32767)
    check("限幅: 峰值百分比合理", 80 <= peak <= 100, f"got {peak}")

    quiet = np.array([1000, -1000, 500], dtype=np.int16).tobytes()
    _, limited_q, peak_q = phonemic.process_pcm16(quiet, 0.0)
    check("限幅: 未超阈值时不计限幅", limited_q == 0, f"got {limited_q}")
    check("限幅: 小信号峰值约 3%", peak_q == 3, f"got {peak_q}")

    _, limited_g, _ = phonemic.process_pcm16(
        np.array([5000, -5000], dtype=np.int16).tobytes(), 18.0)
    check("限幅: 增益推高后触发限幅", limited_g == 2, f"got {limited_g}")


def test_clamp_gain():
    """P2-9：增益上下限必须与手机端一致（0~18dB）。"""
    import phonemic
    check("增益钳位: 负值归零", phonemic.clamp_gain(-5) == 0.0)
    check("增益钳位: 超上限截到 MAX_GAIN_DB",
          phonemic.clamp_gain(99) == phonemic.MAX_GAIN_DB)
    check("增益钳位: 上限与手机端一致为 18dB", phonemic.MAX_GAIN_DB == 18.0)
    check("增益钳位: 正常值原样通过", phonemic.clamp_gain(6) == 6.0)


# ---------- 4. ADB 自动唤醒冷却 ----------

def test_auto_wake_cooldown():
    """P0-2 回归测试：发现循环与 watchdog 都会调 check_usb_device，
    无冷却时实测约 1 次/秒把手机主界面拉到前台。"""
    import phonemic

    phonemic._LAST_WAKE["at"] = 0.0
    check("唤醒冷却: 首次允许", phonemic.auto_wake_allowed() is True)
    check("唤醒冷却: 紧接着的第二次被拒", phonemic.auto_wake_allowed() is False)
    check("唤醒冷却: 冷却窗口至少 30 秒", phonemic.AUTO_WAKE_COOLDOWN >= 30)

    phonemic._LAST_WAKE["at"] = time.time() - phonemic.AUTO_WAKE_COOLDOWN - 1
    check("唤醒冷却: 冷却期满后重新允许", phonemic.auto_wake_allowed() is True)


# ---------- 5. UDP 公告解析（含采样率协商） ----------

def test_parse_announce():
    """P2-3：UDP 分支此前硬编码 48000Hz，与手机端零协商。"""
    import phonemic

    new = phonemic.parse_announce("PHONEMIC 8081 UDP 58082 RATE 48000")
    check("公告解析: 取到 TCP 端口", new["tcp_port"] == 8081, f"got {new}")
    check("公告解析: 取到 UDP 端口", new["udp_port"] == 58082, f"got {new}")
    check("公告解析: 取到采样率", new["rate"] == 48000, f"got {new}")

    legacy = phonemic.parse_announce("PHONEMIC 8080")
    check("公告解析: 旧版公告仍可解析", legacy["tcp_port"] == 8080, f"got {legacy}")
    check("公告解析: 旧版缺省 UDP 端口 58082", legacy["udp_port"] == 58082, f"got {legacy}")
    check("公告解析: 旧版缺省采样率 48000", legacy["rate"] == 48000, f"got {legacy}")

    check("公告解析: 非 PHONEMIC 报文返回 None",
          phonemic.parse_announce("HELLO 1234") is None)
    check("公告解析: 垃圾端口返回 None",
          phonemic.parse_announce("PHONEMIC abc") is None)


# ---------- 6. 配对 token ----------

def test_token_pairing():
    """P1-1：无线连接必须带 token；USB(回环) 首次连接自动取回 token 存本地。"""
    import phonemic

    with tempfile.TemporaryDirectory() as d:
        tf = Path(d) / "token"
        orig, phonemic.TOKEN_FILE = phonemic.TOKEN_FILE, tf
        try:
            check("配对: 无 token 文件时读到空", phonemic.load_token() == "")
            check("配对: 无 token 时请求头为空", phonemic.auth_headers() == {})
            check("配对: 无 token 时 UDP 注册包不带尾巴",
                  phonemic.udp_start_payload() == b"PHONEMIC_UDP_START")

            phonemic.save_token("abc123XYZ")
            check("配对: token 落盘后可读回", phonemic.load_token() == "abc123XYZ")
            check("配对: 请求头带上 token",
                  phonemic.auth_headers() == {"X-PhoneMic-Token": "abc123XYZ"},
                  f"got {phonemic.auth_headers()}")
            check("配对: UDP 注册包带上 token",
                  phonemic.udp_start_payload() == b"PHONEMIC_UDP_START abc123XYZ",
                  f"got {phonemic.udp_start_payload()!r}")

            phonemic.save_token("  \n")
            check("配对: 空白 token 视为未配对", phonemic.load_token() == "")
        finally:
            phonemic.TOKEN_FILE = orig


def test_udp_receiver_sends_token():
    """UdpAudioReceiver 注册时必须带 token，否则手机端应拒绝推流。"""
    import phonemic

    phone = FakeUdpPhone(token="secret42")
    phone.start()
    with tempfile.TemporaryDirectory() as d:
        tf = Path(d) / "token"
        orig, phonemic.TOKEN_FILE = phonemic.TOKEN_FILE, tf
        try:
            phonemic.save_token("secret42")
            stop = threading.Event()
            rcv = phonemic.UdpAudioReceiver("127.0.0.1", phone.port, stop)
            data = rcv.read(960)
            stop.set()
            rcv.close()
            check("UDP 鉴权: 带正确 token 能收到音频", len(data) == 960, f"got {len(data)}")
            check("UDP 鉴权: 手机端未拒绝任何注册", phone.rejected == [], f"{phone.rejected}")
        finally:
            phonemic.TOKEN_FILE = orig
            phone.stop()


# ---------- 7. 链路标签 ----------

def test_link_mode_label():
    """P2-6：菜单栏面板此前直接按 .phonemic_last_url 前缀猜链路。"""
    import phonemic
    check("链路标签: 回环判为 USB", "USB" in phonemic.link_mode_label("http://127.0.0.1:58083"))
    check("链路标签: udp 判为 UDP", "UDP" in phonemic.link_mode_label("udp://192.168.1.7:58082"))
    check("链路标签: http 判为 Wi-Fi", "Wi-Fi" in phonemic.link_mode_label("http://192.168.1.7:8081"))
    check("链路标签: 空地址不谎报链路", phonemic.link_mode_label("") == "")


# ---------- 8. AppleScript 参数化（防注入） ----------

def test_notify_argv_is_parameterised():
    """P2-7：通知文案此前直接 f-string 拼进 AppleScript。

    菜单栏模块依赖 rumps/AppKit，非 macOS（如 CI）上跳过。"""
    try:
        import PhoneMicMenu as menu
    except Exception as e:
        print(f"SKIP 通知转义（菜单栏模块不可用：{type(e).__name__}）", flush=True)
        return

    evil = 'x" & (do shell script "touch /tmp/pwned") & "'
    argv = menu.notify_argv("标题", evil, sound="Glass")
    joined = " ".join(argv)
    check("通知转义: 恶意文案不出现在任何 -e 脚本片段里",
          not any(evil in a for i, a in enumerate(argv) if i > 0 and argv[i - 1] == "-e"),
          joined[:160])
    check("通知转义: 恶意文案作为独立 argv 传入", evil in argv, joined[:160])
    check("通知转义: 命令是 osascript", argv[0] == "osascript")


# ---------- 9. 录音写失败不得静默 ----------

def test_recorder_write_failure_is_visible():
    """P2-5：writeframesraw 失败被 except: pass 吞掉，用户以为录上了。"""
    import phonemic

    rec = phonemic.PttRecorder(1, 48000, 16)

    class BrokenWav:
        def writeframesraw(self, _):
            raise OSError("No space left on device")

    rec.wav = BrokenWav()
    rec.stamp = "test"
    rec._cache = (time.time() + 3600, True)   # 假装 PTT 常开，跳过文件读
    rec.write(b"\x00" * 64)
    check("录音: 写失败被记录而非静默吞掉", rec.write_failed is True)


def test_token_must_be_wellformed():
    """回归：旧版 APK 对任何路径都回 WAV 流，GET /token 拿到的是音频数据。

    无脑存下来的话，之后每个 HTTP 请求都会 ValueError: Invalid header value，
    Mac 端彻底连不上，且不删 .phonemic_token 永远好不了。
    """
    import phonemic

    wav = b"RIFF$\x7fWAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    check("token 校验: WAV 流不是合法 token",
          phonemic.is_valid_token(wav.decode("latin-1")) is False)
    check("token 校验: 空串不是合法 token", phonemic.is_valid_token("") is False)
    check("token 校验: 带空格不是合法 token", phonemic.is_valid_token("ab cd1234") is False)
    check("token 校验: 过短不是合法 token", phonemic.is_valid_token("abc") is False)
    check("token 校验: 过长不是合法 token", phonemic.is_valid_token("a" * 200) is False)
    check("token 校验: Base64-URL 串是合法 token",
          phonemic.is_valid_token("aB3-_xYz9Q") is True)

    # 已被污染的 token 文件必须自愈，而不是把每个请求都毒死
    with tempfile.TemporaryDirectory() as d:
        tf = Path(d) / "token"
        orig, phonemic.TOKEN_FILE = phonemic.TOKEN_FILE, tf
        try:
            tf.write_bytes(wav)
            check("token 校验: 读到损坏 token 视为未配对", phonemic.load_token() == "")
            check("token 校验: 损坏 token 不会污染请求头", phonemic.auth_headers() == {})
        finally:
            phonemic.TOKEN_FILE = orig


def test_fetch_token_rejects_non_token_body():
    """USB 取 token 时，手机端是旧版本就会回音频流，必须当场拒绝而不是存盘。"""
    import http.server
    import threading as th

    import phonemic

    class WavHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):          # 旧版 APK 的行为：任何路径都回 WAV
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.end_headers()
            self.wfile.write(b"RIFF$\x7fWAVEfmt \x10\x00\x00\x00" * 8)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), WavHandler)
    th.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as d:
        tf = Path(d) / "token"
        orig, phonemic.TOKEN_FILE = phonemic.TOKEN_FILE, tf
        try:
            ok = phonemic.fetch_token_over_usb(f"http://127.0.0.1:{srv.server_port}")
            check("USB 取 token: 旧版手机回音频流时判为失败", ok is False)
            check("USB 取 token: 不把音频流存成 token", phonemic.load_token() == "",
                  f"got {phonemic.load_token()!r}")
        finally:
            phonemic.TOKEN_FILE = orig
            srv.shutdown()


if __name__ == "__main__":
    for fn in (test_lock_preserves_holder_pid,
               test_probe_ok_accepts_udp_url,
               test_gain_limit_counts_engagements,
               test_clamp_gain,
               test_auto_wake_cooldown,
               test_parse_announce,
               test_token_pairing,
               test_udp_receiver_sends_token,
               test_link_mode_label,
               test_notify_argv_is_parameterised,
               test_recorder_write_failure_is_visible,
               test_token_must_be_wellformed,
               test_fetch_token_rejects_non_token_body):
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} 未抛异常", False, f"{type(e).__name__}: {e}")

    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for n in FAIL:
            print(f"  FAIL {n}")
        sys.exit(1)
