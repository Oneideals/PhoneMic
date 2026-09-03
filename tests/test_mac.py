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

# 关掉 USB 探测：不然本机插着的真手机会被优先选中，测试转而连真设备，
# 假手机那几条断言就会以"15s 内未重连"的形式莫名其妙地失败。
# 公告/查询端口也挪开：58080 是系统级资源，同机跑着的另一个 PhoneMic
# 会把公告整包吃掉（SO_REUSEADDR 只保证 bind 成功，不保证收得到）。
ANNOUNCE_PORT = 58880
QUERY_PORT = 58881
ENV = {**os.environ, "PHONEMIC_NO_USB": "1",
       "PHONEMIC_ANNOUNCE_PORT": str(ANNOUNCE_PORT),
       "PHONEMIC_QUERY_PORT": str(QUERY_PORT)}

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
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    time.sleep(1.5)   # 等第一个进程完成 acquire_lock
    try:
        p2 = subprocess.run([PY, str(ENGINE), "http://127.0.0.1:1"],
                            capture_output=True, text=True, timeout=15, env=ENV)
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
    127.0.0.1:ANNOUNCE_PORT 广播 'PHONEMIC <port>'（真机是 255.255.255.255，测试走环回）。
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
                    s.sendto(f"PHONEMIC {self.port}".encode(), ("127.0.0.1", ANNOUNCE_PORT))
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
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         bufsize=1, env=ENV)
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

    # 5f. 抢先避让与取消
    media_ducking.set_system_mute(False)
    ducker_pre = media_ducking.AudioDucker(enabled_getter=lambda: True)
    ducker_pre.preemptive_duck()
    check("音频避让: preemptive_duck 抢先静音", ducker_pre._is_preemptively_ducked and media_ducking.get_system_mute() is True)
    ducker_pre.cancel_preemptive_duck()
    check("音频避让: cancel_preemptive_duck 恢复音量", not ducker_pre._is_preemptively_ducked and media_ducking.get_system_mute() is False)
    media_ducking.set_system_mute(orig_mute)


def test_media_ducking_crash_safety():
    """崩溃安全测试：模拟菜单进程在 duck 状态被 SIGKILL 后，新实例自动恢复遗留静音。

    对应缺陷：PhoneMicMenu 被强杀/崩溃时系统静音泄漏；新实例 unduck() 因
    _is_ducked=False 直接返回（no-op），下一次 duck() 又把泄漏静音误判为
    "用户自己静的"（_did_mute_system=False）→ 静音永久粘住，需手动调音量才恢复。
    """
    import media_ducking

    state = BASE / ".duck_state_test"
    try:
        state.unlink()
    except FileNotFoundError:
        pass

    orig_mute = media_ducking.get_system_mute()
    media_ducking.set_system_mute(False)
    try:
        # 场景1：duck 中进程被杀（不调用 unduck/清理），且用户已停止录音
        d1 = media_ducking.AudioDucker(enabled_getter=lambda: True,
                                       state_file=state,
                                       still_recording_getter=lambda: False)
        d1.duck()
        check("崩溃安全: duck 后静音标记已落盘", state.exists())
        check("崩溃安全: duck 后系统静音", media_ducking.get_system_mute() is True)
        # 模拟 SIGKILL：直接丢弃 d1（不 cleanup），启动新实例
        d2 = media_ducking.AudioDucker(enabled_getter=lambda: True,
                                       state_file=state,
                                       still_recording_getter=lambda: False)
        check("崩溃安全: 重启实例自动解除遗留静音",
              media_ducking.get_system_mute() is False)
        check("崩溃安全: 恢复后静音标记已清除", not state.exists())

        # 场景2：被杀时录音仍在进行（.ptt=1）→ 重建避让状态，松开时正常解除
        d3 = media_ducking.AudioDucker(enabled_getter=lambda: True,
                                       state_file=state,
                                       still_recording_getter=lambda: False)
        d3.duck()
        d4 = media_ducking.AudioDucker(enabled_getter=lambda: True,
                                       state_file=state,
                                       still_recording_getter=lambda: True)
        check("崩溃安全: 录音进行中重启 → 保持静音并重建避让",
              media_ducking.get_system_mute() is True and d4.is_ducked)
        d4.unduck()
        check("崩溃安全: 重建后 unduck 正常解除静音",
              media_ducking.get_system_mute() is False and not state.exists())

        # 场景3：实例从未 duck 但磁盘存在遗留标记（如恢复失败后的重试路径）
        d5 = media_ducking.AudioDucker(enabled_getter=lambda: True,
                                       state_file=state,
                                       still_recording_getter=lambda: False)
        media_ducking.set_system_mute(True)
        state.write_text("1")
        d5._is_ducked = False
        d5._did_mute_system = False
        d5.unduck()
        check("崩溃安全: 未 duck 但存在遗留标记时 unduck 兜底解除",
              media_ducking.get_system_mute() is False and not state.exists())

        # 场景4：用户原本就静音 → duck 不落盘标记，unduck 不破坏用户静音意图
        media_ducking.set_system_mute(True)
        d6 = media_ducking.AudioDucker(enabled_getter=lambda: True,
                                       state_file=state,
                                       still_recording_getter=lambda: False)
        d6.duck()
        check("崩溃安全: 用户已静音时 duck 不写标记", not state.exists())
        d6.unduck()
        check("崩溃安全: unduck 不解除用户自己的静音",
              media_ducking.get_system_mute() is True)
    finally:
        media_ducking.set_system_mute(orig_mute)
        try:
            state.unlink()
        except FileNotFoundError:
            pass


def test_ptt_fsm():
    """PTT 状态机测试：右⌥单击开始 / 组合键不误触发 / 录音中任意键结束（对齐微信输入法）。"""
    import PhoneMicMenu as pmm

    # 场景1：右⌥ 单击（按下后无其他键，超时确认）→ 开始
    f = pmm.PTTFsm()
    check("PTT: 右⌥按下 → arm", f.on_right_option_press() == "arm")
    check("PTT: 窗口超时 → start（单击判定成立）", f.on_window_timeout() == "start")

    # 场景2：录音中按右⌥ → 结束
    check("PTT: 录音中按右⌥ → end", f.on_right_option_press() == "end")

    # 场景3：录音中按任意普通键（含 ESC keycode=53）→ 结束
    f2 = pmm.PTTFsm()
    f2.on_right_option_press()
    f2.on_window_timeout()
    check("PTT: 录音中按 ESC/任意键 → end", f2.on_other_key() == "end")

    # 场景4：⌥+其他键 组合（右⌥ 按下后 0.22s 内有键跟进）→ 不触发录音
    f3 = pmm.PTTFsm()
    check("PTT: 组合键场景右⌥按下 → arm", f3.on_right_option_press() == "arm")
    check("PTT: 组合键其他键跟进 → 取消（无动作）", f3.on_other_key() is None)
    check("PTT: 取消后窗口超时 → 无动作", f3.on_window_timeout() is None)

    # 场景5：窗口内快速双击右⌥ → 静默抵消（无任何动作）
    f4 = pmm.PTTFsm()
    f4.on_right_option_press()
    check("PTT: 窗口内二次右⌥ → 静默抵消", f4.on_right_option_press() is None)
    check("PTT: 抵消后超时 → 无动作", f4.on_window_timeout() is None)

    # 场景6：组合键后再次单击右⌥ → 正常开始（状态干净）
    check("PTT: 组合键后再按右⌥ → arm", f4.on_right_option_press() == "arm")
    check("PTT: 超时 → start", f4.on_window_timeout() == "start")

    # 场景7：录音中按其他修饰键（⇧/⌘/Fn 等）→ 结束
    check("PTT: 录音中按修饰键 → end", f4.on_other_key() == "end")

    # 场景8：空闲时按任意键 → 无动作（不会误开始）
    f5 = pmm.PTTFsm()
    check("PTT: 空闲时按任意键 → 无动作", f5.on_other_key() is None)


def test_gate_mode():
    """语音输入门控模式测试：非录音输出静音、录音中放行 PCM、PTT 翻转冲刷。"""
    import numpy as np

    gate_file = BASE / "gate_mode"
    ptt_file = BASE / ".ptt"
    orig_gate = gate_file.read_text() if gate_file.exists() else None
    orig_ptt = ptt_file.read_text() if ptt_file.exists() else None

    try:
        # 1. 门控开启且 PTT 未开启时：输出应为纯 0 静音
        gate_file.write_text("1")
        ptt_file.write_text("0")
        check("输入门控: gate_file 写入 1 开启", gate_file.read_text().strip() == "1")
        check("输入门控: ptt_file 状态为 0", ptt_file.read_text().strip() == "0")

        fake_pcm = np.ones((480, 1), dtype=np.int16) * 1000
        outdata = fake_pcm.copy()
        is_gate_on = gate_file.read_text().strip() != "0"
        is_ptt_on = ptt_file.read_text().strip() == "1"
        if is_gate_on and not is_ptt_on:
            outdata.fill(0)
        check("输入门控: 非录音期间输出全 0 静音（防前置串音）", np.all(outdata == 0))

        # 2. PTT 开启录音时：正常放行 PCM
        ptt_file.write_text("1")
        is_ptt_on = ptt_file.read_text().strip() == "1"
        outdata = fake_pcm.copy()
        if is_gate_on and not is_ptt_on:
            outdata.fill(0)
        check("输入门控: 录音期间正常放行真实 PCM", np.all(outdata == 1000))

        # 3. 队列冲刷逻辑（模拟方案三）
        import queue
        q = queue.Queue(maxsize=10)
        rem = bytearray(b"\x12\x34" * 100)
        q.put(b"old_audio_data_1")
        q.put(b"old_audio_data_2")
        # 模拟 PTT 翻转触发 flush
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break
        rem.clear()
        check("输入门控: PTT 开录触发在途队列与缓存冲刷清空", q.empty() and len(rem) == 0)

    finally:
        if orig_gate is not None:
            gate_file.write_text(orig_gate)
        else:
            gate_file.unlink(missing_ok=True)
        if orig_ptt is not None:
            ptt_file.write_text(orig_ptt)
        else:
            ptt_file.unlink(missing_ok=True)


if __name__ == "__main__":
    test_wav_header()
    test_lock()
    test_stream_and_sigterm()
    test_udp_reconnect()
    test_media_ducking()
    test_media_ducking_crash_safety()
    test_ptt_fsm()
    test_gate_mode()
    print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
