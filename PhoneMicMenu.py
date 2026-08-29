#!/usr/bin/env python3
"""PhoneMicMenu — 菜单栏管理图标（图标化状态 + 录音绿色指示）。"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

BASE = Path.home() / "LocalStorage" / "GitHub" / "PhoneMic"
ENGINE = BASE / "phonemic.py"
PYTHON = sys.executable
AGENT = Path.home() / "Library" / "LaunchAgents" / "com.jerry.phonemic.menu.plist"
ICON_DIR = BASE / "icons"

GAIN_FILE = BASE / "gain_db"
LEVEL_FILE = BASE / ".level"
DENOISE_FILE = BASE / "denoise"
RECORD_FILE = BASE / "record"
PTT_FILE = BASE / ".ptt"                 # 右 Option 按住状态（引擎读取，PTT 录音）
REC_DIR = BASE / "recordings"
RECONNECT_FILE = BASE / ".reconnect"     # 「立即重连」信号（引擎等待循环轮询消费）
SYSINPUT_FILE = BASE / "sysinput"           # 接管系统输入开关（"1"=接管）
PREV_INPUT_FILE = BASE / ".prev_input"      # 接管前的原输入设备名（用于还原）
BLACKHOLE_NAME = "BlackHole 2ch"
SWITCH_TOOL = "/opt/homebrew/bin/SwitchAudioSource"
GAIN_CHOICES = [0, 3, 6, 9, 12]
RIGHT_OPTION_KEYCODE = 61                   # 右 Option 键码（调试用）
NX_DEVICERALTKEYMASK = 0x0040               # 右 Option 的设备修饰位（IOLLEvent.h: NX_DEVICERALTKEYMASK）
LOCK_FILE = BASE / ".phonemic_lock"         # 引擎单实例锁（内容为引擎 PID）

PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jerry.phonemic.menu</string>
  <key>ProgramArguments</key><array>
    <string>{PYTHON}</string>
    <string>{BASE / "PhoneMicMenu.py"}</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>"""


def build_icons():
    """生成四种状态圆点图标：recording红/on白/connecting环/stopped暗环。"""
    import AppKit

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    size = 18
    paths = {}
    specs = {
        "recording": ("fill", (0.20, 0.85, 0.35, 1.0)),
        "on": ("fill", (1.0, 1.0, 1.0, 1.0)),
        "connecting": ("ring", (1.0, 1.0, 1.0, 0.9)),
        "stopped": ("ring", (1.0, 1.0, 1.0, 0.45)),
    }
    for kind, (mode, color) in specs.items():
        img = AppKit.NSImage.alloc().initWithSize_((size, size))
        img.lockFocus()
        oval = AppKit.NSBezierPath.bezierPathWithOvalInRect_(((2, 2), (size - 4, size - 4)))
        c = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color)
        if mode == "fill":
            c.setFill()
            oval.fill()
        else:
            c.setStroke()
            oval.setLineWidth_(2.0)
            oval.stroke()
        img.unlockFocus()
        img.setTemplate_(False)   # 保留颜色，不跟随菜单栏明暗模板
        p = ICON_DIR / f"{kind}.png"
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
        ftype = getattr(AppKit, "NSBitmapImageFileTypePNG", AppKit.NSPNGFileType)
        rep.representationUsingType_properties_(ftype, {}).writeToFile_atomically_(
            str(p), True)
        paths[kind] = str(p)
    return paths


def _read_ptt() -> bool:
    """读取当前录音开关状态。"""
    try:
        return PTT_FILE.exists() and PTT_FILE.read_text().strip() == "1"
    except Exception:
        return False


def _write_ptt(active: bool) -> None:
    """把录音开关状态写入 .ptt（引擎读取，决定是否录音）。"""
    try:
        PTT_FILE.write_text("1" if active else "0")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_ptt_listener(on_state, on_error, on_mode=None):
    """监听右 Option：单击切换录音（微信语音输入式），非按住。

    优先用 HID 层 listen-only 事件 tap：在微信输入法（WeType）等拦截软件
    修改/吞掉按键之前就能看到原始硬件事件，且不受睡眠唤醒影响。
    无「输入监控」权限时降级为轮询 CGEventSourceFlagsState（公开 API 免权限，
    但被输入法拦截的按键会看不到）。
    on_state(active) 在录音开关状态变化时回调；on_error(msg) 在两种方案都不可用时回调；
    on_mode(mode) 回调当前方案（"tap"=事件监听 / "poll"=轮询降级）。
    """
    def run():
        try:
            import Quartz
        except ImportError:
            on_error("缺少 pyobjc（Quartz），PTT 不可用")
            return

        def tap_callback(_proxy, _etype, event, _refcon):
            try:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                # 只认按下沿（Alternate 位被置位 = 按下）；松开事件忽略
                if keycode == RIGHT_OPTION_KEYCODE and \
                        Quartz.CGEventGetFlags(event) & Quartz.kCGEventFlagMaskAlternate:
                    on_state(not _read_ptt())
            except Exception:
                pass
            return event

        tap = None
        try:
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGHIDEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
                tap_callback, None)
        except Exception:
            tap = None
        if tap is not None:
            try:
                source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
                Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(),
                                          source, Quartz.kCFRunLoopCommonModes)
                Quartz.CGEventTapEnable(tap, True)
                if on_mode:
                    on_mode("tap")
                Quartz.CFRunLoopRun()
                return
            except Exception:
                on_error("PTT 事件监听异常退出")
                return

        # 降级方案：轮询（20ms）。被输入法拦截的键看不到，但免系统权限
        if on_mode:
            on_mode("poll")
        pressed = False
        while True:
            try:
                cur = bool(Quartz.CGEventSourceFlagsState(
                    Quartz.kCGEventSourceStateHIDSystemState) & NX_DEVICERALTKEYMASK)
                if cur and not pressed:     # 按下沿：单击切换
                    on_state(not _read_ptt())
                pressed = cur
            except Exception:
                try:
                    time.sleep(1)   # 睡眠唤醒等瞬时异常：等一会重试，不退出线程
                    continue
                except Exception:
                    on_error("PTT 状态轮询异常")
                    return
            time.sleep(0.02)

    threading.Thread(target=run, daemon=True).start()


TEXTS = {
    "streaming": "● 手机麦克风已连通",
    "connecting": "◐ 正在寻找手机…",
    "stopped": "○ 已停止",
}


class PhoneMicMenu(rumps.App):

    def __init__(self):
        super().__init__(name="PhoneMic", quit_button="退出 PhoneMic")
        self.paths = build_icons()
        self.icon = self.paths["stopped"]
        self.proc = None
        self.should_run = False
        self.status = "stopped"
        self.level_hist = []
        self.item_status = rumps.MenuItem(TEXTS["stopped"], callback=None)
        self.item_toggle = rumps.MenuItem("启动", callback=self.on_toggle)
        self.item_denoise = rumps.MenuItem("降噪：过滤电脑风扇声", callback=self.on_denoise)
        self.item_denoise.state = self._flag_on(DENOISE_FILE)
        self.item_rec = rumps.MenuItem("录音存档（单击右⌥开始/再单击结束）", callback=self.on_record)
        self.item_rec.state = self._flag_on(RECORD_FILE)
        self.item_ptt = rumps.MenuItem("PTT 监听：等待权限…", callback=None)
        self.ptt_error = None
        self.ptt_mode = None
        self.ptt_active = _read_ptt()
        self.item_open_rec = rumps.MenuItem("打开录音文件夹", callback=self.on_open_rec)
        self.item_reconnect = rumps.MenuItem("立即重连手机", callback=self.on_reconnect)
        self.item_sys = rumps.MenuItem("接管系统输入（断线自动还原）", callback=self.on_sysinput)
        self.item_sys.state = self._flag_on(SYSINPUT_FILE)
        self._sys_switched = False
        self.item_autostart = rumps.MenuItem("开机自启（下次登录生效）",
                                             callback=self.on_autostart)
        self.item_autostart.state = AGENT.exists()
        gain_items = []
        for db in GAIN_CHOICES:
            it = rumps.MenuItem(f"输出增益 +{db}dB", callback=self.on_gain)
            it._db = db
            gain_items.append(it)
        self.gain_items = gain_items
        self.menu = [self.item_status, None,
                     ["输出增益（电脑侧微调）", gain_items], None,
                     self.item_ptt,
                     self.item_denoise, self.item_rec, self.item_open_rec,
                     self.item_sys, self.item_reconnect,
                     self.item_toggle, self.item_autostart, None]
        self.sync_gain_state()
        # PTT：单击右 Option 切换录音开关，状态写 .ptt 供引擎读取
        try:
            PTT_FILE.write_text("1" if self.ptt_active else "0")
        except Exception:
            pass
        start_ptt_listener(
            on_state=lambda active: (setattr(self, "ptt_active", active),
                                     _write_ptt(active)),
            on_error=lambda msg: setattr(self, "ptt_error", msg),
            on_mode=lambda m: setattr(self, "ptt_mode", m),
        )
        # 孤儿状态清理：若上次异常退出把系统输入留在 BlackHole 且未开启接管，则还原
        try:
            cur = self._query_input()
            if cur and "BlackHole" in cur:
                if self._flag_on(SYSINPUT_FILE):
                    self._sys_switched = True
                elif PREV_INPUT_FILE.exists():
                    self._set_input(PREV_INPUT_FILE.read_text().strip())
        except Exception:
            pass
        self.spawn()

    # ---------- 工具 ----------

    @staticmethod
    def _flag_on(path: Path) -> int:
        try:
            return 1 if (path.exists() and path.read_text().strip() == "1") else 0
        except Exception:
            return 0

    # ---------- 引擎管理 ----------

    def spawn(self):
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [PYTHON, str(ENGINE), "--auto"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self.should_run = True
        self.status = "connecting"
        threading.Thread(target=self.watch, daemon=True).start()

    def _engine_pid(self) -> int:
        """读单实例锁里的引擎 PID（0=无）。"""
        try:
            return int(LOCK_FILE.read_text().strip() or 0)
        except Exception:
            return 0

    def watch(self):
        """跟随引擎输出刷新状态；进程退出则视情况接管或重拉。"""
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line.startswith("[音频]"):
                    self.status = "streaming"
                elif line.startswith(("[连接]", "[发现]")):
                    # 断线/重新寻找时必须回落：否则菜单假显"已连通"，
                    # 系统输入也不会在断流期间还原（手机离线 = 全系统哑麦）
                    self.status = "connecting"
            while self.should_run:
                time.sleep(2)
                if not self.should_run or self.proc.poll() is None:
                    continue
                # 本实例引擎已退出。若存在存活的孤儿引擎（如菜单栏被强杀后遗留，
                # stdout 管道断裂不再归我们管），重复拉起只会被单实例锁拒绝空转；
                # 等孤儿退出后再接管（期间 refresh() 依据 .level 仍能正确显示状态）
                orphan = self._engine_pid()
                if orphan and orphan != self.proc.pid and _pid_alive(orphan):
                    continue
                self.spawn()
                return
        except Exception:
            pass
        if not self.should_run:
            self.status = "stopped"

    def _stop_engines(self):
        """停掉本实例引擎与可能存在的孤儿引擎（写 BlackHole 的只能有一个）。"""
        pids = set()
        if self.proc:
            pids.add(self.proc.pid)
        orphan = self._engine_pid()
        if orphan:
            pids.add(orphan)
        for pid in pids:
            if pid and _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    # ---------- 菜单动作 ----------

    def on_toggle(self, sender):
        if self.should_run:
            self.should_run = False
            self._stop_engines()
            self.status = "stopped"
        else:
            self.spawn()
        self.refresh()

    def on_gain(self, sender):
        try:
            GAIN_FILE.write_text(str(sender._db))
        except Exception:
            pass
        self.sync_gain_state()

    def sync_gain_state(self):
        cur = 0
        try:
            if GAIN_FILE.exists():
                cur = int(float(GAIN_FILE.read_text().strip() or 0))
        except Exception:
            pass
        for it in self.gain_items:
            it.state = 1 if it._db == cur else 0

    def on_denoise(self, sender):
        new_state = not self._flag_on(DENOISE_FILE)
        try:
            DENOISE_FILE.write_text("1" if new_state else "0")
            sender.state = 1 if new_state else 0
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()   # watch 线程 2 秒内自动重启引擎
        except Exception:
            pass

    def on_record(self, sender):
        new_state = not self._flag_on(RECORD_FILE)
        try:
            RECORD_FILE.write_text("1" if new_state else "0")
            sender.state = 1 if new_state else 0
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()   # watch 线程 2 秒内自动重启引擎
        except Exception:
            pass

    # ---------- 系统输入接管 ----------

    def _query_input(self):
        try:
            r = subprocess.run([SWITCH_TOOL, "-t", "input", "-c"],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip()
        except Exception:
            return ""

    def _set_input(self, name):
        try:
            r = subprocess.run([SWITCH_TOOL, "-t", "input", "-s", name],
                               capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def _take_sys_input(self):
        prev = self._query_input()
        if prev and "BlackHole" not in prev:
            try:
                PREV_INPUT_FILE.write_text(prev)
            except Exception:
                pass
        if self._set_input(BLACKHOLE_NAME):
            self._sys_switched = True

    def _restore_sys_input(self):
        prev = "MacBook Air麦克风"
        try:
            if PREV_INPUT_FILE.exists() and PREV_INPUT_FILE.read_text().strip():
                prev = PREV_INPUT_FILE.read_text().strip()
        except Exception:
            pass
        self._set_input(prev)
        self._sys_switched = False

    def on_sysinput(self, sender):
        new_state = not self._flag_on(SYSINPUT_FILE)
        try:
            SYSINPUT_FILE.write_text("1" if new_state else "0")
            sender.state = 1 if new_state else 0
        except Exception:
            pass
        if new_state and self.status == "streaming" and not self._sys_switched:
            self._take_sys_input()
        elif not new_state and self._sys_switched:
            self._restore_sys_input()

    def on_open_rec(self, sender):
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["open", str(REC_DIR)])
        except Exception:
            pass

    def on_reconnect(self, sender):
        """跳过引擎的重连退避，立即重新寻找手机（写信号文件，引擎轮询消费）。"""
        if not (self.proc and self.proc.poll() is None):
            self.spawn()   # 引擎没在跑：直接拉起（含完整发现流程）
            return
        try:
            RECONNECT_FILE.write_text("1")
        except Exception:
            pass

    def on_autostart(self, sender):
        try:
            if AGENT.exists():
                subprocess.run(["launchctl", "unload", str(AGENT)],
                               capture_output=True)
                AGENT.unlink()
                sender.state = 0
            else:
                AGENT.parent.mkdir(parents=True, exist_ok=True)
                AGENT.write_text(PLIST)
                subprocess.run(["launchctl", "load", str(AGENT)], capture_output=True)
                sender.state = 1
        except Exception:
            pass

    # ---------- 状态刷新 ----------

    def recording_on(self) -> bool:
        return self.status == "streaming" and self._flag_on(RECORD_FILE) == 1

    def refresh(self):
        # PTT 监听状态显示
        if self.ptt_error:
            self.item_ptt.title = f"⚠️ PTT：{self.ptt_error}"
        elif self.ptt_active:
            self.item_ptt.title = "🎤 录音中（再按右⌥结束）"
        elif self.ptt_mode == "poll":
            self.item_ptt.title = "PTT：轮询模式（建议授权「输入监控」以穿透输入法拦截）"
        else:
            self.item_ptt.title = "PTT：按右⌥开始录音，再按结束"

        # 引擎连通性以 .level 新鲜度为准（引擎每 0.5s 写一次）：
        # 孤儿引擎在写也算连通；标记 streaming 但 .level 停更则立即回落
        if self.should_run:
            try:
                fresh = time.time() - LEVEL_FILE.stat().st_mtime < 1.5
            except Exception:
                fresh = False
            if fresh:
                self.status = "streaming"
            elif self.status == "streaming":
                self.status = "connecting"

        # 系统输入接管：连通即接管，断线/停止自动还原
        takeover = self._flag_on(SYSINPUT_FILE) == 1
        if takeover and self.status == "streaming" and not self._sys_switched:
            self._take_sys_input()
        elif (not takeover or self.status != "streaming") and self._sys_switched:
            self._restore_sys_input()

        if not self.should_run:
            self.icon = self.paths["stopped"]
            status_text = TEXTS["stopped"]
        elif self.status == "streaming":
            live = self.recording_on() and self.ptt_active   # 录音开启且开关激活才录
            self.icon = self.paths["recording" if live else "on"]
            status_text = "● 手机麦克风已连通" + ("（🎤录音中）" if live else "")
        else:
            self.icon = self.paths["connecting"]
            status_text = TEXTS["connecting"]

        if self.status == "streaming":
            try:
                lv = int(LEVEL_FILE.read_text().strip() or 0)
            except Exception:
                lv = 0
            self.level_hist.append(lv)
            if len(self.level_hist) > 15:
                self.level_hist = self.level_hist[-15:]
            wmax = max(self.level_hist) if self.level_hist else 0
            cur_gain = 0
            try:
                if GAIN_FILE.exists():
                    cur_gain = int(float(GAIN_FILE.read_text().strip() or 0))
            except Exception:
                pass
            if wmax < 8:
                verdict = "静音或未说话（对着手机说一句试试）"
            elif wmax < 15:
                verdict = f"峰值 {wmax}% 偏小 → 建议加增益（手机＋3dB 或本菜单选高档）"
            elif wmax <= 90:
                verdict = f"峰值 {wmax}% ✓ 合适，保持即可"
            elif cur_gain > 0:
                verdict = f"峰值 {wmax}% 过大 → 建议降增益（有削波风险）"
            else:
                # 两端增益均已 0dB：削波发生在手机麦克风采集端，数字增益无法挽救，
                # 只能声学手段（录音数据实测结论）
                verdict = f"峰值 {wmax}% 过大：麦克风本体过载 → 手机放远些或离嘴远一点"
            self.item_status.title = "电平诊断: " + verdict
        else:
            self.item_status.title = status_text
        self.item_toggle.title = "停止" if self.should_run else "启动"


    def quit(self, sender=None):
        """退出前归还原系统输入设备并停掉引擎，避免留下哑麦状态和孤儿引擎进程。"""
        if self._sys_switched:
            self._restore_sys_input()
        self.should_run = False
        self._stop_engines()
        super().quit(sender)


if __name__ == "__main__":
    import fcntl
    import os
    _lock_fh = open(str(BASE / ".menu_lock"), "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[锁] 菜单栏图标已在运行，本次退出。")
        sys.exit(0)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()

    app = PhoneMicMenu()

    def ticker(_):
        app.refresh()

    timer = rumps.Timer(ticker, 1)
    timer.start()
    app.run()
