#!/usr/bin/env python3
"""PhoneMic Mac 端单元/集成测试（需要本机 BlackHole，无需手机）。"""
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ENGINE = BASE / "phonemic.py"
PY = str(BASE / ".venv" / "bin" / "python")
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)


# ---------- 1. WAV 头解析：模拟手机的无限 WAV 流 ----------

class FakeResp:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, n):
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


def test_wav_header():
    import phonemic
    # 构造与 MicService.buildHeader 相同的头（假巨大长度）+ 2 秒噪声数据
    data_len = 0x7FFFFF00
    hdr = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
    hdr += b"data" + struct.pack("<I", data_len)
    pcm = os.urandom(48000 * 2 * 2)
    resp = FakeResp(hdr + pcm)
    payload, (ch, rate, bits) = phonemic.wav_head_and_payload(resp)
    check("WAV 头解析: 格式 48k/1ch/16bit", (ch, rate, bits) == (1, 48000, 16), f"got {(ch, rate, bits)}")
    # 按设计只返回随头部缓冲区一起到达的初始 payload，其余走流式透传
    check("WAV 头解析: 初始 payload 是头后数据的前缀",
          len(payload) > 0 and pcm.startswith(payload), f"len={len(payload)}")


# ---------- 2. 单实例锁：两个进程，第二个必须失败 ----------

def test_lock():
    p1 = subprocess.Popen([PY, str(ENGINE), "http://127.0.0.1:1"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)   # 等第一个进程完成 acquire_lock
    try:
        p2 = subprocess.run([PY, str(ENGINE), "http://127.0.0.1:1"],
                            capture_output=True, text=True, timeout=15)
        check("单实例锁: 第二个实例被拒绝", "已有 PhoneMic 实例" in p2.stdout, p2.stdout[-200:])
    finally:
        p1.send_signal(signal.SIGTERM)
        try:
            p1.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p1.kill()


# ---------- 3. 假手机服务器（本机 HTTP WAV 流） ----------

class FakePhone(threading.Thread):
    """模拟手机端：无限 48k/16bit/mono WAV 流，供引擎拉取。

    announce=True 时复刻真机 UDP 公告行为：无客户端连接期间每 0.5s 向
    127.0.0.1:58080 广播 'PHONEMIC <port>'（真机是 255.255.255.255，测试走环回）。
    """

    def __init__(self, port, announce=True):
        super().__init__(daemon=True)
        self.port = port
        self.srv = None
        self.announce = announce
        self.clients = 0
        self.stop_flag = threading.Event()

    def run(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(2)
        if self.announce:
            threading.Thread(target=self._announce_loop, daemon=True).start()
        while not self.stop_flag.is_set():
            try:
                self.srv.settimeout(1)
                c, _ = self.srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def _announce_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self.stop_flag.is_set():
            if self.clients == 0:
                try:
                    s.sendto(f"PHONEMIC {self.port}".encode(), ("127.0.0.1", 58080))
                except OSError:
                    pass
            time.sleep(0.5)

    def serve(self, c):
        self.clients += 1
        try:
            c.settimeout(5)
            while True:
                d = c.recv(4096)
                if not d or b"\r\n\r\n" in d:
                    break
            data_len = 0x7FFFFF00
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\n"
                      b"Connection: close\r\n\r\n")
            hdr = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
            hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
            hdr += b"data" + struct.pack("<I", data_len)
            c.sendall(hdr)
            # 带周期的伪信号，按 96KB/s 实时速率发送（模拟真手机采集节奏）
            block = (b"\x10\x00" * 512 + b"\x60\x06" * 256) * 8   # 12288 字节
            while not self.stop_flag.is_set():
                c.sendall(block)
                time.sleep(len(block) / 96000)
        except OSError:
            pass
        finally:
            self.clients -= 1
            try:
                c.close()
            except OSError:
                pass

    def stop(self):
        self.stop_flag.set()
        try:
            self.srv.close()
        except OSError:
            pass


def run_engine(url, wait_for="[音频]", timeout=25):
    p = subprocess.Popen([PY, str(ENGINE), url],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        line = p.stdout.readline()
        if not line:
            break
        lines.append(line.strip())
        if wait_for in line:
            return p, lines
    return p, lines


def test_stream_and_sigterm():
    fp = FakePhone(18099)
    fp.start()
    time.sleep(0.3)
    url = "http://127.0.0.1:18099/audio.wav"
    (BASE / "record").write_text("0")   # 防止外部残留开关干扰

    # 3a. 正常拉流 + 电平文件写入
    rec_dir = BASE / "recordings"
    rec_dir.mkdir(exist_ok=True)
    stamp_before = set(rec_dir.glob("*"))
    try:
        (BASE / ".level").unlink()
    except FileNotFoundError:
        pass
    p, lines = run_engine(url, wait_for="[音频]")
    check("拉流: 引擎连上假手机并开始写 BlackHole",
          p.poll() is None and any("[音频]" in l for l in lines), " | ".join(lines[-5:]))
    time.sleep(2.5)
    try:
        level = int((BASE / ".level").read_text().strip())
    except Exception:
        level = -1
    check("拉流: 电平文件实时更新(>0)", level > 0, f"level={level}")

    # 3b. PTT 录音：只有按住右 Option（.ptt=1）期间才写入，松开转 FLAC
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=8)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
        p.kill()
    check("SIGTERM: 引擎 8 秒内退出", exited)

    (BASE / "record").write_text("1")
    PTT = BASE / ".ptt"
    try:
        PTT.write_text("0")
        stamp_before2 = set(rec_dir.glob("*"))
        p2, lines2 = run_engine(url, wait_for="[音频]")
        check("PTT: 录音开启后引擎启动（PTT 模式）",
              p2.poll() is None and any("PTT" in l for l in lines2), " | ".join(lines2[-5:]))
        time.sleep(1.5)
        new_idle = [f for f in set(rec_dir.glob("*")) - stamp_before2
                    if not f.name.endswith(".meta.csv")]
        check("PTT: 未按键期间不产生任何录音文件", len(new_idle) == 0,
              f"new={[f.name for f in new_idle]}")

        # 按住 2.5 秒 → 松开 → 等异步转码
        PTT.write_text("1")
        time.sleep(2.5)
        PTT.write_text("0")
        time.sleep(2.5)
        new2 = set(rec_dir.glob("*")) - stamp_before2
        flac = [f for f in new2 if f.suffix == ".flac"]
        wav = [f for f in new2 if f.suffix == ".wav"]
        csv = [f for f in new2 if f.suffix == ".csv"]
        check("PTT: 按住期间录音、松开即转存 FLAC", len(flac) == 1,
              f"new={[f.name for f in sorted(new2)]}")
        check("PTT: 松开后无残留 WAV", len(wav) == 0, f"wav={[f.name for f in wav]}")
        check("PTT: 指标 CSV 已生成（全程电平）", len(csv) == 1)
        if flac:
            dur = subprocess.run(["ffmpeg", "-i", str(flac[0])], capture_output=True, text=True)
            import re
            m = re.search(r"Duration: (\d+):(\d+):(\d+\.(\d+))", dur.stderr)
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else -1
            check("PTT: 段时长≈按住时长(2~4s)", 2.0 <= secs <= 4.0, f"dur={secs}")

        # 误触测试：按住 0.2 秒应被丢弃
        before_cnt = len(list(rec_dir.glob("*.flac")))
        PTT.write_text("1")
        time.sleep(0.2)
        PTT.write_text("0")
        time.sleep(2)
        after_cnt = len(list(rec_dir.glob("*.flac")))
        check("PTT: 短按(0.2s)视为误触丢弃", after_cnt == before_cnt,
              f"before={before_cnt} after={after_cnt}")

        # 按住中退出（SIGTERM）：当前段也要正确收尾
        PTT.write_text("1")
        time.sleep(2.5)
        p2.send_signal(signal.SIGTERM)
        try:
            p2.wait(timeout=10)
            exited2 = True
        except subprocess.TimeoutExpired:
            exited2 = False
            p2.kill()
        check("SIGTERM: 按住中退出，引擎 10 秒内完成收尾", exited2)
        # join 在途转码线程后进程才退出，源 WAV 删除应已完成（防 daemon 竞态残留）
        wav_left = [f.name for f in rec_dir.glob("*.wav")]
        check("SIGTERM: 退出后无残留 WAV（转码收尾完整）", len(wav_left) == 0,
              f"left={wav_left}")
        flac_all = sorted(rec_dir.glob("*.flac"))
        check("SIGTERM: 按住中的段已转存 FLAC", len(flac_all) >= 2,
              f"flacs={[f.name for f in flac_all]}")
        PTT.write_text("0")
    finally:
        (BASE / "record").write_text("0")
        try:
            PTT.write_text("0")
        except Exception:
            pass

    # 3d. 断线重连：杀掉假手机 → 引擎打印断开并退避重试
    p3, lines3 = run_engine(url, wait_for="[音频]")
    if p3.poll() is None:
        fp.stop_flag.set()
        time.sleep(2)
        t0 = time.time()
        saw_retry = False
        while time.time() - t0 < 20:
            line = p3.stdout.readline()
            if not line:
                break
            if "[连接]" in line:
                saw_retry = True
                break
        check("断线: 引擎检测断开并进入退避重试", saw_retry)
        p3.send_signal(signal.SIGTERM)
        try:
            p3.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p3.kill()
    fp.stop_flag.set()


def test_udp_reconnect():
    """UDP 公告通道：纯公告发现连接；断线后公告打断退避、自动重连。"""
    port = 18098
    url = f"http://127.0.0.1:{port}/audio.wav"
    try:
        (BASE / ".phonemic_last_url").unlink()
    except FileNotFoundError:
        pass

    # 4a. 无历史地址：靠 UDP 公告（模拟 mDNS 失效场景）完成首次发现
    fp = FakePhone(port, announce=True)
    fp.start()
    time.sleep(1.0)   # 让公告先广播几轮（引擎启动前缓存为空也无妨，启动后下一轮即命中）
    p, lines = run_engine("--auto", wait_for="[音频]", timeout=30)
    check("UDP: 纯公告通道完成首次发现",
          any("公告" in l or "[音频]" in l for l in lines), " | ".join(lines[-4:]))

    # 4b. 断线：假手机下线 → 引擎进入退避；退避等待中手机回归并公告 → 打断等待、秒级重连
    if p.poll() is None:
        fp.stop()
        t0 = time.time()
        saw_break = False
        while time.time() - t0 < 15:
            line = p.stdout.readline()
            if not line:
                break
            if "[连接]" in line or "[发现]" in line:
                saw_break = True
                break
        # 手机回归（引擎此刻在退避等待中）：公告应打断等待并立刻重连
        fp2 = FakePhone(port, announce=True)
        fp2.start()
        t1 = time.time()
        reconnected_at = None
        while time.time() - t1 < 15:
            line = p.stdout.readline()
            if not line:
                break
            if "[音频]" in line:
                reconnected_at = time.time() - t1
                break
        check("UDP: 断线后公告打断退避、自动重连", reconnected_at is not None,
              f"手机回归后 {reconnected_at}s 重连" if reconnected_at else "15s 内未重连")
        check("UDP: 重连速度（手机回归后 ≤5s）",
              reconnected_at is not None and reconnected_at <= 5,
              f"t={reconnected_at}s")
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
        fp2.stop()
    fp.stop()
    try:
        (BASE / ".phonemic_last_url").unlink()
    except FileNotFoundError:
        pass


def test_media_ducking():
    """音频避让与媒体控制测试：CoreAudio 静音、AudioDucker 状态转移与异常安全。"""
    import media_ducking

    # 5a. CoreAudio 静音获取与设置
    orig_mute = media_ducking.get_system_mute()
    try:
        # 设置静音
        ok_mute = media_ducking.set_system_mute(True)
        check("音频避让: set_system_mute(True)", ok_mute and media_ducking.get_system_mute() is True)

        # 解除静音
        ok_unmute = media_ducking.set_system_mute(False)
        check("音频避让: set_system_mute(False)", ok_unmute and media_ducking.get_system_mute() is False)
    finally:
        media_ducking.set_system_mute(orig_mute)

    # 5b. AudioDucker 状态流转与静音自动恢复
    # 模拟未静音环境下的 duck 与 unduck
    media_ducking.set_system_mute(False)
    ducker = media_ducking.AudioDucker(enabled_getter=lambda: True)
    ducker.duck()
    check("音频避让: duck 后 is_ducked 为 True 且系统静音",
          ducker.is_ducked and media_ducking.get_system_mute() is True)

    ducker.unduck()
    check("音频避让: unduck 后 is_ducked 为 False 且系统解除静音",
          not ducker.is_ducked and media_ducking.get_system_mute() is False)

    # 5c. 用户原本就静音时：duck 不会破坏用户的静音意图，unduck 不会把用户取消静音
    media_ducking.set_system_mute(True)
    ducker2 = media_ducking.AudioDucker(enabled_getter=lambda: True)
    ducker2.duck()
    check("音频避让: 原本已静音时 did_mute_system 标记为 False", not ducker2._did_mute_system)
    ducker2.unduck()
    check("音频避让: 原本已静音时 unduck 保持静音", media_ducking.get_system_mute() is True)
    media_ducking.set_system_mute(orig_mute)

    # 5d. 开关禁用时：duck 不生效
    ducker3 = media_ducking.AudioDucker(enabled_getter=lambda: False)
    media_ducking.set_system_mute(False)
    ducker3.duck()
    check("音频避让: 禁用开关时 duck() 不改变系统静音",
          not ducker3.is_ducked and media_ducking.get_system_mute() is False)
    media_ducking.set_system_mute(orig_mute)

    # 5e. MediaRemote 查询无崩溃
    mr_playing = media_ducking.check_media_remote_playing(timeout=0.05)
    check("音频避让: MediaRemote.framework isPlaying 安全调用", isinstance(mr_playing, bool))


if __name__ == "__main__":
    test_wav_header()
    test_lock()
    test_stream_and_sigterm()
    test_udp_reconnect()
    test_media_ducking()
    print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
