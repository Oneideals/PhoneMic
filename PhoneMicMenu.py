#!/usr/bin/env python3
"""PhoneMicMenu — 菜单栏管理图标（图标化状态 + 录音绿色指示）。"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import media_ducking
import rumps

import debuglog
import phonemic   # 复用引擎侧的链路标签/增益上限/配对 token，避免两处各写一份而漂移

BASE = Path(__file__).resolve().parent   # 项目根（脚本所在目录），保证 clone 到任意位置都能运行
ENGINE = BASE / "phonemic.py"
PYTHON = sys.executable
AGENT = Path.home() / "Library" / "LaunchAgents" / "com.jerry.phonemic.menu.plist"
ICON_DIR = BASE / "icons"

GAIN_FILE = BASE / "gain_db"
LEVEL_FILE = BASE / ".level"
DENOISE_FILE = BASE / "denoise"
RECORD_FILE = BASE / "record"
DUCK_FILE = BASE / "auto_duck"           # 录音期间自动暂停/恢复背景音（默认开启 "1"）
PTT_FILE = BASE / ".ptt"                 # 右 Option 按住状态（引擎读取，PTT 录音）
REC_DIR = BASE / "recordings"
RECONNECT_FILE = BASE / ".reconnect"     # 「立即重连」信号（引擎等待循环轮询消费）
SYSINPUT_FILE = BASE / "sysinput"           # 接管系统输入开关（"1"=接管）
PREV_INPUT_FILE = BASE / ".prev_input"      # 接管前的原输入设备名（用于还原）
BLACKHOLE_NAME = "BlackHole 2ch"
SWITCH_TOOL = "/opt/homebrew/bin/SwitchAudioSource"
GAIN_CHOICES = [0, 3, 6, 9, 12, 15, 18]     # 上限与 phonemic.MAX_GAIN_DB / 手机端保持一致
RIGHT_OPTION_KEYCODE = 61                   # 右 Option 键码（调试用）
NX_DEVICERALTKEYMASK = 0x0040               # 右 Option 的设备修饰位（IOLLEvent.h: NX_DEVICERALTKEYMASK）
LOCK_FILE = BASE / ".phonemic_lock"         # 引擎单实例锁（内容为引擎 PID）
LAST_URL_FILE = BASE / ".phonemic_last_url" # 最新连通 URL 文件

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
    """生成四种状态圆点图标：recording绿/on蓝/connecting橙环/stopped暗环。"""
    import AppKit

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    size = 18
    paths = {}
    specs = {
        "recording": ("fill", (0.15, 0.85, 0.35, 1.0)),     # 🟢 录音/语音输入锁定：亮绿实心圆
        "on": ("fill", (0.20, 0.60, 1.0, 1.0)),            # 🔵 连通待命：清澈亮天蓝实心圆（常驻显色）
        "connecting": ("ring", (0.96, 0.60, 0.15, 0.95)),  # 🟠 探测寻找中：亮橙色空心圆环
        "stopped": ("ring", (0.55, 0.55, 0.55, 0.50)),      # ⭕ 手动停止：暗灰色空心圆环
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


def _play_sound(sound_name: str = "Basso"):
    try:
        path = f"/System/Library/Sounds/{sound_name}.aiff"
        if os.path.exists(path):
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def notify_argv(title: str, message: str, sound: str = None) -> list:
    """构造 osascript 命令行：文案走 `on run` 参数，不拼进脚本正文。

    直接 f-string 拼接的话，文案里的引号会破坏脚本，`" & (do shell script "…")`
    这类内容还能直接注入执行。现在调用点都是常量，但这个函数迟早会被喂上
    设备名或 URL，参数化是唯一不用每次提心吊胆的写法。
    """
    body = "display notification m with title t"
    if sound:
        body += " sound name s"
    return ["osascript",
            "-e", "on run {t, m, s}",
            "-e", body,
            "-e", "end run",
            "--", title, message, sound or ""]


def _show_notification(title: str, message: str, sound: str = None):
    try:
        subprocess.Popen(notify_argv(title, message, sound),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


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
                etype = Quartz.CGEventGetType(event)
                # 系统会在回调超时或用户输入打乱顺序时悄悄禁用 tap：
                # 不处理的话，右⌥ 会毫无征兆地失灵（表现为"按下没反应、录不上音"）
                if etype in (Quartz.kCGEventTapDisabledByTimeout,
                             Quartz.kCGEventTapDisabledByUserInput):
                    debuglog.log("menu", f"⚠️ 事件 tap 被系统禁用（type={etype}），自动重新启用")
                    try:
                        Quartz.CGEventTapEnable(tap, True)
                    except Exception:
                        debuglog.log("menu", "重新启用 tap 失败", exc=True)
                    return event
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
                debuglog.log("menu", "PTT 监听：事件 tap 模式已启用")
                Quartz.CFRunLoopRun()
                debuglog.log("menu", "⚠️ PTT 事件 tap 的 CFRunLoop 意外返回（事件监听中断）")
                return
            except Exception:
                debuglog.log("menu", "PTT 事件监听异常", exc=True)
                on_error("PTT 事件监听异常退出")
                return

        # 降级方案：轮询（20ms）。被输入法拦截的键看不到，但免系统权限
        debuglog.log("menu", "PTT 监听：未取得输入监控权限，降级为轮询模式"
                             "（微信输入法等可能拦截按键导致失效）")
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


def _ensure_edit_menu():
    """为无 Dock 图标的菜单栏应用注入标准 Edit 菜单（⌘C/⌘V/⌘A/⌘X/⌘Z）。

    macOS 输入框快捷键依赖 NSApp.mainMenu 中的 Edit 子菜单；
    Accessory 辅助型应用默认无 mainMenu，会导致输入框无法使用 ⌘V 粘贴。
    """
    import AppKit

    app = AppKit.NSApplication.sharedApplication()
    if app.mainMenu() is not None:
        return
    main_menu = AppKit.NSMenu.alloc().init()
    edit_item = AppKit.NSMenuItem.alloc().init()
    edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
    edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Undo", "undo:", "z"))
    edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Redo", "redo:", "Z"))
    edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Cut", "cut:", "x"))
    edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Copy", "copy:", "c"))
    edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Paste", "paste:", "v"))
    edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a"))
    edit_item.setSubmenu_(edit_menu)
    main_menu.addItem_(edit_item)
    app.setMainMenu_(main_menu)


class PhoneMicMenu(rumps.App):

    def __init__(self):
        debuglog.install("menu")
        _ensure_edit_menu()
        super().__init__(name="PhoneMic", quit_button="退出 PhoneMic")
        self.paths = build_icons()
        self.icon = self.paths["stopped"]
        self.proc = None
        self.should_run = False
        self.status = "stopped"
        self.speaking_until = 0.0
        self.level_hist = []
        self.ducker = media_ducking.AudioDucker(
            enabled_getter=lambda: self._flag_on(DUCK_FILE, default=1) == 1
        )
        self.item_status = rumps.MenuItem(TEXTS["stopped"], callback=None)
        self.item_mode = rumps.MenuItem("传输链路：🔍 检测中…", callback=None)
        self.item_level = rumps.MenuItem("电平诊断：--", callback=None)
        self.item_toggle = rumps.MenuItem("启动", callback=self.on_toggle)
        self.item_denoise = rumps.MenuItem("降噪：过滤电脑风扇声", callback=self.on_denoise)
        self.item_denoise.state = self._flag_on(DENOISE_FILE)
        self.item_rec = rumps.MenuItem("录音存档（单击右⌥开始/再单击结束）", callback=self.on_record)
        self.item_rec.state = self._flag_on(RECORD_FILE)
        self.item_duck = rumps.MenuItem("录音时自动暂停背景音（防串音）", callback=self.on_duck)
        self.item_duck.state = self._flag_on(DUCK_FILE, default=1)
        self.item_ptt = rumps.MenuItem("PTT 监听：等待权限…", callback=None)
        self.ptt_error = None
        self.ptt_mode = None
        self.ptt_active = _read_ptt()
        self._last_logged_status = None
        self.item_open_rec = rumps.MenuItem("打开录音文件夹", callback=self.on_open_rec)
        self.item_reconnect = rumps.MenuItem("立即重连手机", callback=self.on_reconnect)
        self.item_pair = rumps.MenuItem("配对：检查中…", callback=self.on_pair)
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
        self.menu = [
            self.item_status,
            self.item_mode,
            self.item_level,
            None,
            ["输出增益（电脑侧微调）", gain_items],
            None,
            self.item_ptt,
            self.item_duck, self.item_denoise, self.item_rec, self.item_open_rec,
            self.item_sys, self.item_pair, self.item_reconnect,
            self.item_toggle, self.item_autostart, None
        ]
        self.sync_gain_state()
        # PTT：单击右 Option 切换录音开关，状态写 .ptt 供引擎读取
        try:
            PTT_FILE.write_text("1" if self.ptt_active else "0")
            if self.ptt_active:
                debuglog.log("menu", "⚠️ 启动时发现录音开关处于「开」，已沿用（上次可能未正常结束录音）")
        except Exception:
            pass
        def _on_ptt_error(msg):
            self.ptt_error = msg
            debuglog.log("menu", f"⚠️ PTT 异常：{msg}")

        start_ptt_listener(
            on_state=self._on_ptt_state_changed,
            on_error=_on_ptt_error,
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
    def _flag_on(path: Path, default: int = 0) -> int:
        try:
            if not path.exists():
                return default
            return 1 if path.read_text().strip() == "1" else 0
        except Exception:
            return default

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
        debuglog.log("menu", f"拉起引擎 pid={self.proc.pid}（{PYTHON} {ENGINE} --auto）")
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
                if line:
                    debuglog.log("engine-out", line)   # 引擎打印进日志文件，避免管道丢失
                if line.startswith("[音频]"):
                    self.status = "streaming"
                elif line.startswith(("[连接]", "[发现]")):
                    # 断线/重新寻找时必须回落：否则菜单假显"已连通"，
                    # 系统输入也不会在断流期间还原（手机离线 = 全系统哑麦）
                    self.status = "connecting"
            code = self.proc.poll()
            debuglog.log("menu", f"引擎已退出 pid={self.proc.pid} 退出码={code} "
                                 f"should_run={self.should_run}（0=正常退出，"
                                 f"负数为被信号杀死，如 -15=SIGTERM -9=SIGKILL）")
            while self.should_run:
                time.sleep(2)
                if not self.should_run or self.proc.poll() is None:
                    continue
                # 本实例引擎已退出。若存在存活的孤儿引擎（如菜单栏被强杀后遗留，
                # stdout 管道断裂不再归我们管），重复拉起只会被单实例锁拒绝空转；
                # 等孤儿退出后再接管（期间 refresh() 依据 .level 仍能正确显示状态）
                orphan = self._engine_pid()
                if orphan and orphan != self.proc.pid and _pid_alive(orphan):
                    debuglog.log("menu", f"检测到孤儿引擎 pid={orphan} 仍在运行，暂不拉起新引擎")
                    continue
                if not self.should_run:
                    break
                debuglog.log("menu", "准备重新拉起引擎")
                self.spawn()
                return
        except Exception:
            debuglog.log("menu", "watch 线程异常", exc=True)
        if not self.should_run:
            self.status = "stopped"
            debuglog.log("menu", "引擎停止（用户点击停止或退出）")

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

    def _on_ptt_state_changed(self, active: bool):
        """右 Option 切换：同步更新 PTT 状态，并自动暂停/恢复背景音。"""
        self.ptt_active = active
        _write_ptt(active)
        debuglog.log("menu", f"右⌥ 切换录音 → {'开始录音' if active else '结束录音'}"
                             f"（录音存档开关={'开' if self._flag_on(RECORD_FILE) else '关'}"
                             f"，引擎={'在跑' if self.proc and self.proc.poll() is None else '未运行'}"
                             f"，状态={self.status}）")
        if active:
            if self.status != "streaming":
                _play_sound("Basso")
                _show_notification("PhoneMic 未连通", "手机麦克风未连通（正在寻找中），语音输入暂不可用", sound="Basso")
                self.refresh()
                return
            self.ducker.duck()
        else:
            self.ducker.unduck()
        self.refresh()

    # ---------- 菜单动作 ----------

    def on_toggle(self, sender):
        if self.should_run:
            self.should_run = False
            if self.ducker.is_ducked:
                self.ducker.unduck()
            self._stop_engines()
            self.status = "stopped"
        else:
            self.spawn()
        self.refresh()

    def on_duck(self, sender):
        new_state = not self._flag_on(DUCK_FILE, default=1)
        try:
            DUCK_FILE.write_text("1" if new_state else "0")
            sender.state = 1 if new_state else 0
        except Exception:
            pass
        if not new_state and self.ducker.is_ducked:
            self.ducker.unduck()

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
        ok = self._set_input(BLACKHOLE_NAME)
        debuglog.log("menu", f"接管系统输入 → {BLACKHOLE_NAME}（原设备={prev!r}，结果={'成功' if ok else '失败'}）")
        if ok:
            self._sys_switched = True

    def _restore_sys_input(self):
        prev = "MacBook Air麦克风"
        try:
            if PREV_INPUT_FILE.exists() and PREV_INPUT_FILE.read_text().strip():
                prev = PREV_INPUT_FILE.read_text().strip()
        except Exception:
            pass
        debuglog.log("menu", f"还原系统输入 → {prev!r}")
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

    def on_pair(self, sender):
        """手动配对：输入手机 App 上显示的配对码。

        插过 USB 线的话引擎会自动配对，这里只服务于「从没插过线、纯无线」的场景。
        """
        import AppKit

        cur = phonemic.load_token()
        try:
            _ensure_edit_menu()
            app = AppKit.NSApplication.sharedApplication()
            app.activateIgnoringOtherApps_(True)

            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("PhoneMic 配对")
            alert.setInformativeText_(
                "打开手机上的 PhoneMic，把界面显示的「配对码」输入到这里。\n"
                "（插过 USB 数据线会自动配对，无需手动输入）\n"
                "若要解除配对，清空输入框点击保存即可。"
            )
            alert.addButtonWithTitle_("保存")
            alert.addButtonWithTitle_("取消")

            field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 280, 24))
            field.setStringValue_(cur or "")
            alert.setAccessoryView_(field)

            win = alert.window()
            win.setLevel_(AppKit.NSFloatingWindowLevel)
            win.setInitialFirstResponder_(field)
            win.makeKeyAndOrderFront_(None)
            # 窗口渲染完成后延迟触发全选，确保 FieldEditor 绑定完毕并高亮全选当前配对码
            field.performSelector_withObject_afterDelay_("selectText:", None, 0.05)

            res = alert.runModal()
            if res != AppKit.NSAlertFirstButtonReturn:
                return  # 取消或关闭
            field.validateEditing()
            tok = str(field.stringValue()).strip()
        except Exception as e:
            debuglog.log("menu", f"配对弹窗异常: {e}")
            return

        phonemic.save_token(tok)
        debuglog.log("menu", f"手动配对：{'已保存配对码' if tok else '已解除配对'}")
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()   # 让引擎带着新 token 重连
        self.refresh()

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
        # 配对状态
        self.item_pair.title = ("配对：✅ 已配对（点此修改）" if phonemic.load_token()
                                else "配对：⚠️ 未配对（插 USB 线自动配对，或点此手填）")

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

        # 状态变迁只在翻转时记一行，避免每秒刷屏
        if self.status != self._last_logged_status:
            debuglog.log("menu", f"状态变迁：{self._last_logged_status} → {self.status}"
                                 f"（.level 新鲜={'是' if self.should_run and self.status == 'streaming' else '否'}）")
            if self._last_logged_status == "streaming" and self.status == "connecting":
                _play_sound("Sosumi")
                _show_notification("PhoneMic 断开连接", "手机麦克风已断开，正在自动寻找重连…", sound="Sosumi")
            elif self._last_logged_status in ("connecting", None) and self.status == "streaming" and self._last_logged_status is not None:
                _play_sound("Glass")
                _show_notification("PhoneMic 已连通", "手机麦克风已就绪，可按右⌥进行语音输入", sound="Glass")
            self._last_logged_status = self.status

        # 系统输入接管：连通即接管，断线/停止自动还原
        takeover = self._flag_on(SYSINPUT_FILE) == 1
        if takeover and self.status == "streaming" and not self._sys_switched:
            self._take_sys_input()
        elif (not takeover or self.status != "streaming") and self._sys_switched:
            self._restore_sys_input()

        if not self.should_run:
            self.icon = self.paths["stopped"]
            self.item_status.title = "○ PhoneMic 已停止"
            self.item_mode.title = "传输链路：⏸ 已停止"
            self.item_level.title = "电平诊断：--"
        elif self.status == "streaming":
            try:
                lv = int(LEVEL_FILE.read_text().strip() or 0)
            except Exception:
                lv = 0

            is_recording = self.ptt_active
            self.icon = self.paths["recording" if is_recording else "on"]
            status_tag = "（🎤 录音中）" if is_recording else ""

            last_url = ""
            try:
                if LAST_URL_FILE.exists():
                    last_url = LAST_URL_FILE.read_text().strip()
            except Exception:
                pass
            mode_tag = phonemic.link_mode_label(last_url) or "🔍 探测中…"
            self.item_status.title = "● 手机麦克风已连通" + status_tag
            self.item_mode.title = f"传输链路：{mode_tag}"

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
                verdict = "静音或未说话"
            elif wmax < 15:
                verdict = f"峰值 {wmax}% 偏小（可＋3dB）"
            elif wmax <= 90:
                verdict = f"峰值 {wmax}% ✓ 良好"
            elif cur_gain > 0:
                verdict = f"峰值 {wmax}% 过大（建议降增益）"
            else:
                verdict = f"峰值 {wmax}% 过大（离嘴远一点）"
            self.item_level.title = f"电平诊断：实时 {lv}% · 峰值 {wmax}% ({verdict})"
        else:
            self.icon = self.paths["connecting"]
            self.item_status.title = "◐ 正在寻找手机…"
            self.item_mode.title = "传输链路：🔍 正在探测 USB / UDP / Wi-Fi…"
            self.item_level.title = "电平诊断：--"
        self.item_toggle.title = "停止" if self.should_run else "启动"

        # 每分钟一次状态快照：便于把"某时刻的现象"和日志时间线对齐
        now = time.time()
        if now - getattr(self, "_last_snapshot", 0) > 60:
            self._last_snapshot = now
            try:
                age = now - LEVEL_FILE.stat().st_mtime
                lv = LEVEL_FILE.read_text().strip()
            except Exception:
                age, lv = -1, "?"
            engine = "存活" if (self.proc and self.proc.poll() is None) else "已退出"
            debuglog.log("menu", f"快照：状态={self.status} 引擎={engine} 电平={lv}%"
                                 f"（{age:.1f}s 前更新）录音存档={'开' if self._flag_on(RECORD_FILE) else '关'}"
                                 f" 右⌥={'录音中' if self.ptt_active else '待机'}"
                                 f" 系统输入接管={self._sys_switched}")


    def quit(self, sender=None):
        """退出前归还原系统输入设备与静音状态并停掉引擎，避免留下哑麦状态和孤儿引擎进程。"""
        debuglog.log("menu", "菜单栏退出：清理引擎与系统输入")
        try:
            self.ducker.cleanup()
        except Exception:
            pass
        if self._sys_switched:
            self._restore_sys_input()
        self.should_run = False
        self._stop_engines()
        super().quit(sender)


if __name__ == "__main__":
    import atexit
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
    atexit.register(app.ducker.cleanup)

    def ticker(_):
        app.refresh()

    timer = rumps.Timer(ticker, 0.25)
    timer.start()
    app.run()
