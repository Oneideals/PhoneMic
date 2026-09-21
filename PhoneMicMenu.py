#!/usr/bin/env python3
"""PhoneMicMenu — 菜单栏管理图标（图标化状态 + 录音绿色指示）。"""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# 确保即使由系统 Python 或外部调用拉起，也能自动接入本项目的 .venv 依赖
_BASE_VENV = Path(__file__).resolve().parent / ".venv"
if _BASE_VENV.exists():
    for _sp in (_BASE_VENV / "lib").glob("python*/site-packages"):
        if str(_sp) not in sys.path:
            sys.path.insert(0, str(_sp))

import media_ducking
import rumps

import debuglog
import phonemic   # 复用引擎侧的链路标签/增益上限/配对 token，避免两处各写一份而漂移

BASE = Path(__file__).resolve().parent   # 项目根（脚本所在目录），保证 clone 到任意位置都能运行
ENGINE = BASE / "phonemic.py"
_VENV_DIR = BASE / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() and os.access(_VENV_PYTHON, os.X_OK) else sys.executable
APP_NAME = "PhoneMic"
APP_BUNDLE = Path.home() / "Applications" / f"{APP_NAME}.app"
LEGACY_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.jerry.phonemic.menu.plist"
ICON_DIR = BASE / "icons"

GAIN_FILE = BASE / "gain_db"
LEVEL_FILE = BASE / ".level"
DENOISE_FILE = BASE / "denoise"
RECORD_FILE = BASE / "record"
DUCK_FILE = BASE / "auto_duck"           # 录音期间自动暂停/恢复背景音（默认开启 "1"）
GATE_FILE = BASE / "gate_mode"           # 语音输入门控模式（默认开启 "1"：仅录音时开麦，防前置串音；"0"=常开全通）
PTT_FILE = BASE / ".ptt"                 # 右 Option 按住状态（引擎读取，PTT 录音）
REC_DIR = BASE / "recordings"
RECONNECT_FILE = BASE / ".reconnect"     # 「立即重连」信号（引擎等待循环轮询消费）
SYSINPUT_FILE = BASE / "sysinput"           # 接管系统输入开关（"1"=接管）
PREV_INPUT_FILE = BASE / ".prev_input"      # 接管前的原输入设备名（用于还原）
BLACKHOLE_NAME = "BlackHole 2ch"




GAIN_CHOICES = [0, 3, 6, 9, 12, 15, 18]     # 上限与 phonemic.MAX_GAIN_DB / 手机端保持一致
RIGHT_OPTION_KEYCODE = 61                   # 右 Option 键码（调试用）
NX_DEVICERALTKEYMASK = 0x0040               # 右 Option 的设备修饰位（IOLLEvent.h: NX_DEVICERALTKEYMASK）
SINGLE_CLICK_WINDOW = 0.22                  # 右⌥ 按下后等待其他键的时间窗（秒），超过即判定单击
LOCK_FILE = BASE / ".phonemic_lock"         # 引擎单实例锁（内容为引擎 PID）
LAST_URL_FILE = BASE / ".phonemic_last_url" # 最新连通 URL 文件
BLACKHOLE_NAME = "BlackHole 2ch"
LNP_FLAG_FILE = BASE / ".lnp_blocked"   # macOS 15+ 本地网络隐私拦截标记


def find_switch_tool() -> str:
    """查找系统中 SwitchAudioSource CLI 工具路径（兼容 Apple Silicon、Intel 及各种 PATH 场景）。"""
    candidates = [
        "/opt/homebrew/bin/SwitchAudioSource",
        "/usr/local/bin/SwitchAudioSource",
        str(Path.home() / ".local/bin/SwitchAudioSource"),
    ]
    for c in candidates:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    found = shutil.which("SwitchAudioSource")
    return found or "SwitchAudioSource"


SWITCH_TOOL = find_switch_tool()


def ensure_app_bundle() -> Path:
    """确保 ~/Applications/PhoneMic.app 存在且具备正确结构与权限描述。

    解决 macOS 15 (Sequoia) 本地网络隐私限制（LNP）：
    macOS 15 对 LaunchAgent 启动的裸 Python 脚本会施加 NPOLICY 隔离并拦截私有局域网连接（Errno 65 No route to host）。
    打包为标准 App Bundle（声明 NSLocalNetworkUsageDescription + LSUIElement 纯菜单栏常驻），
    并在可执行启动程序中直接执行虚拟环境 Python，确保具备完整的 Aqua GUI 会话与菜单栏挂载。
    """
    contents = APP_BUNDLE / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    info_plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>PhoneMic</string>
    <key>CFBundleDisplayName</key>
    <string>PhoneMic</string>
    <key>CFBundleIdentifier</key>
    <string>com.jerry.phonemic</string>
    <key>CFBundleVersion</key>
    <string>1.1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.1.0</string>
    <key>CFBundleExecutable</key>
    <string>PhoneMic</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSLocalNetworkUsageDescription</key>
    <string>PhoneMic 需要连接局域网中的手机麦克风音频流与自动发现服务。</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>PhoneMic 需要访问系统音频管线以提供虚拟麦克风输入。</string>
</dict>
</plist>
"""
    (contents / "Info.plist").write_text(info_plist)

    # 启动器脚本：配置完整 PATH、进入项目目录，直接运行 Python 保持 Aqua WindowServer 渲染能力
    launcher = f"""#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
PROJECT_DIR="{BASE}"
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/PhoneMicMenu.py"
"""
    exec_path = macos_dir / "PhoneMic"
    exec_path.write_text(launcher)
    exec_path.chmod(0o755)
    return APP_BUNDLE


def is_autostart_enabled() -> bool:
    """检查开机自启状态（macOS 登录项或遗留 LaunchAgent）。"""
    try:
        script = 'tell application "System Events" to get name of every login item'
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            names = [x.strip() for x in res.stdout.split(",")]
            if APP_NAME in names:
                return True
    except Exception:
        pass
    return LEGACY_AGENT.exists()


def enable_autostart() -> bool:
    """启用开机自启：生成 App Bundle、注册 macOS 原生登录项，并清理旧 LaunchAgent。"""
    try:
        app_path = ensure_app_bundle()
        # 清理旧 LaunchAgent，杜绝重复自启与沙盒阻断
        cleanup_legacy_launchagent()
        # 移除可能存在的同名旧登录项
        disable_autostart()
        # 注册原生登录项
        script = (
            f'tell application "System Events" to make login item at end '
            f'with properties {{path:"{app_path}", hidden:false, name:"{APP_NAME}"}}'
        )
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
        return res.returncode == 0
    except Exception as e:
        debuglog.log("menu", f"开启登录项自启失败: {e}")
        return False


def disable_autostart() -> bool:
    """关闭开机自启：从 macOS 登录项中移除，并清理旧 LaunchAgent。"""
    cleanup_legacy_launchagent()
    try:
        script = f'tell application "System Events" to delete (every login item whose name is "{APP_NAME}")'
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
        return res.returncode == 0
    except Exception as e:
        debuglog.log("menu", f"移除登录项自启失败: {e}")
        return False


def cleanup_legacy_launchagent():
    """清理遗留的 LaunchAgent plist，防止旧进程干扰或双开。"""
    try:
        if LEGACY_AGENT.exists():
            subprocess.run(["launchctl", "unload", str(LEGACY_AGENT)], capture_output=True, timeout=2)
            LEGACY_AGENT.unlink(missing_ok=True)
            debuglog.log("menu", "已成功卸载并清除旧版 LaunchAgent plist")
    except Exception as e:
        debuglog.log("menu", f"清理旧版 LaunchAgent 异常: {e}")


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


def _play_sound(sound_name: str = "Sosumi"):
    """播放 macOS 原生系统提示音（AppKit NSSound 优先，系统音频服务直发）。"""
    try:
        import AppKit
        snd = AppKit.NSSound.soundNamed_(sound_name)
        if snd:
            snd.setVolume_(1.0)
            if snd.play():
                return
    except Exception:
        pass
    try:
        path = f"/System/Library/Sounds/{sound_name}.aiff"
        if os.path.exists(path):
            subprocess.Popen(["afplay", "-v", "1.0", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


class PTTFsm:
    """PTT 按键纯逻辑状态机（不依赖 Quartz，可单测）。

    对齐微信输入法（WeType）的交互语义：
    - 右⌥ 单击 → 开始录音（按下后 SINGLE_CLICK_WINDOW 内无其他键跟进才算单击，
      避免把 ⌥+方向键 等组合键误判为开始录音）；
    - 录音中按右⌥ 或任意其他键（含 ESC）→ 结束录音；
    - 窗口内快速再按一次右⌥ → 视作"开始又结束"，净效果为零（不触发任何动作）。

    状态：idle（空闲）/ pending（右⌥ 已按下，等待判定单击或组合键）/ recording。
    动作返回值："arm"=启动单击判定窗（调用方需安排超时回调）、
    "start"=判定单击，开始录音、"end"=结束录音、None=无动作。
    """

    def __init__(self, window: float = SINGLE_CLICK_WINDOW):
        self.window = window
        self.state = "idle"

    def on_right_option_press(self):
        if self.state == "pending":
            # 窗口内二次按下右⌥：视作"开始又立刻结束"，静默抵消
            self.state = "idle"
            return None
        if self.state == "recording":
            self.state = "idle"
            return "end"
        self.state = "pending"
        return "arm"

    def on_other_key(self):
        """任意普通按键 keyDown，或其他修饰键的按下沿。"""
        if self.state == "pending":
            self.state = "idle"          # 组合键（⌥+其他键）→ 取消单击判定
            return None
        if self.state == "recording":
            self.state = "idle"
            return "end"                 # 录音中任意键 → 结束（对齐 WeType）
        return None

    def on_window_timeout(self):
        """单击判定窗超时：期间无其他键跟进，确认单击。"""
        if self.state == "pending":
            self.state = "recording"
            return "start"
        return None


def start_ptt_listener(on_state, on_error, on_mode=None, on_arm=None, on_disarm=None):
    """监听录音按键（对齐微信输入法交互）：右⌥ 单击开始；录音中按任意键结束。

    优先用 HID 层 listen-only 事件 tap：在微信输入法（WeType）等拦截软件
    修改/吞掉按键之前就能看到原始硬件事件，且不受睡眠唤醒影响。
    无「输入监控」权限时降级为轮询 CGEventSourceFlagsState（公开 API 免权限，
    但被输入法拦截的按键会看不到，且无法感知"任意键结束"）。
    on_state(active) 在录音开关状态变化时回调；on_error(msg) 在两种方案都不可用时回调；
    on_mode(mode) 回调当前方案（"tap"=事件监听 / "poll"=轮询降级）；
    on_arm() 在右 Option 按下瞬间触发抢先避让；on_disarm() 在判定组合键取消时触发撤回抢先避让。
    """
    def run():
        try:
            import Quartz
        except ImportError:
            on_error("缺少 pyobjc（Quartz），PTT 不可用")
            return

        # 其他修饰键 keycode → 功能位映射（用于识别"修饰键按下沿"；
        # 左右两枚同功能键共用同一个 flag，按下沿=事件后 flag 位为 1）
        mod_flags = {
            54: Quartz.kCGEventFlagMaskCommand,      # 右⌘
            55: Quartz.kCGEventFlagMaskCommand,      # 左⌘
            56: Quartz.kCGEventFlagMaskShift,        # 左⇧
            58: Quartz.kCGEventFlagMaskAlternate,    # 左⌥
            59: Quartz.kCGEventFlagMaskControl,      # 左⌃
            60: Quartz.kCGEventFlagMaskShift,        # 右⇧
            62: Quartz.kCGEventFlagMaskControl,      # 右⌃
            63: Quartz.kCGEventFlagMaskSecondaryFn,  # Fn
            57: Quartz.kCGEventFlagMaskAlphaShift,   # Caps Lock
        }

        fsm = PTTFsm()
        if _read_ptt():
            # 进程重启时录音开关可能仍处于「开」（菜单有沿用逻辑）：
            # FSM 必须同步到 recording，否则"任意键结束"会失效
            fsm.state = "recording"
            debuglog.log("menu", "PTT 状态机启动：检测到录音开关处于「开」，FSM 同步为 recording")
        pending_timer = {"t": None}

        def _cancel_pending_timer():
            t = pending_timer["t"]
            if t is not None:
                t.cancel()
                pending_timer["t"] = None
                if on_disarm:
                    try:
                        on_disarm()
                    except Exception:
                        pass

        def _act(action, why):
            if action == "arm":
                _cancel_pending_timer()
                if on_arm:
                    try:
                        on_arm()
                    except Exception:
                        pass
                t = threading.Timer(fsm.window, _on_timeout)
                t.daemon = True
                pending_timer["t"] = t
                t.start()
            elif action == "start":
                debuglog.log("menu", f"右⌥ 单击判定成立（{why}）→ 开始录音")
                on_state(True)
            elif action == "end":
                debuglog.log("menu", f"结束录音（{why}）")
                on_state(False)

        def _on_timeout():
            pending_timer["t"] = None
            action = fsm.on_window_timeout()
            if action:
                _act(action, f"{fsm.window}s 内无其他键跟进")

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
                flags = Quartz.CGEventGetFlags(event)
                if etype == Quartz.kCGEventFlagsChanged:
                    if keycode == RIGHT_OPTION_KEYCODE:
                        # 只认按下沿（Alternate 位被置位 = 按下）；松开事件忽略
                        if flags & Quartz.kCGEventFlagMaskAlternate:
                            action = fsm.on_right_option_press()
                            if action:
                                _act(action, "按下右⌥")
                            else:
                                _cancel_pending_timer()
                        # 松开：不产生动作（维持 FSM 现状）
                    elif keycode in mod_flags and flags & mod_flags[keycode]:
                        # 其他修饰键按下沿：等价"任意键"
                        action = fsm.on_other_key()
                        if action:
                            _act(action, f"修饰键 keycode={keycode}")
                        else:
                            _cancel_pending_timer()
                elif etype == Quartz.kCGEventKeyDown:
                    action = fsm.on_other_key()
                    if action:
                        _act(action, f"按键 keycode={keycode}")
                    else:
                        _cancel_pending_timer()
            except Exception:
                pass
            return event

        tap = None
        try:
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGHIDEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
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
                debuglog.log("menu", "PTT 监听：事件 tap 模式已启用（右⌥单击开始/任意键结束）")
                Quartz.CFRunLoopRun()
                debuglog.log("menu", "⚠️ PTT 事件 tap 的 CFRunLoop 意外返回（事件监听中断）")
                return
            except Exception:
                debuglog.log("menu", "PTT 事件监听异常", exc=True)
                on_error("PTT 事件监听异常退出")
                return

        # 降级方案：轮询（20ms）。被输入法拦截的键看不到，且只能感知右⌥
        # （无法实现"任意键结束"），保持单击切换语义
        debuglog.log("menu", "PTT 监听：未取得输入监控权限，降级为轮询模式"
                             "（仅右⌥ 切换；任意键结束不可用）")
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
        # 隐藏 Dock 栏图标：将激活策略锁定为 Accessory（纯菜单栏常驻应用，彻底不进 Dock 与 Cmd+Tab）
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
        _ensure_edit_menu()
        super().__init__(name="PhoneMic", quit_button="退出 PhoneMic")
        self.paths = build_icons()
        self._current_icon = self.paths["stopped"]
        self.icon = self.paths["stopped"]
        self.proc = None
        self.should_run = False
        self.status = "stopped"
        self.speaking_until = 0.0
        self.level_hist = []
        self.ducker = media_ducking.AudioDucker(
            enabled_getter=lambda: self._flag_on(DUCK_FILE, default=1) == 1,
            state_file=BASE / ".duck_state",
            still_recording_getter=_read_ptt,
        )
        self.item_status = rumps.MenuItem(TEXTS["stopped"], callback=None)
        self.item_mode = rumps.MenuItem("传输链路：🔍 检测中…", callback=None)
        self.item_level = rumps.MenuItem("电平诊断：--", callback=None)
        self.item_toggle = rumps.MenuItem("启动", callback=self.on_toggle)
        self.item_denoise = rumps.MenuItem("降噪：过滤电脑风扇声", callback=self.on_denoise)
        self.item_denoise.state = self._flag_on(DENOISE_FILE)
        self.item_rec = rumps.MenuItem("录音存档（右⌥单击开始 / 录音中按任意键结束）", callback=self.on_record)
        self.item_rec.state = self._flag_on(RECORD_FILE)
        self.item_duck = rumps.MenuItem("录音时自动暂停背景音（防串音）", callback=self.on_duck)
        self.item_duck.state = self._flag_on(DUCK_FILE, default=1)
        self.item_gate = rumps.MenuItem("语音输入门控模式（仅录音时开麦，防前置串音）", callback=self.on_gate)
        self.item_gate.state = self._flag_on(GATE_FILE, default=1)
        self.item_ptt = rumps.MenuItem("PTT 监听：等待权限…", callback=None)
        self.ptt_error = None
        self.ptt_mode = None
        self.ptt_active = _read_ptt()
        self._last_logged_status = None
        self.item_open_rec = rumps.MenuItem("打开录音文件夹", callback=self.on_open_rec)
        self.item_reconnect = rumps.MenuItem("立即重连手机", callback=self.on_reconnect)
        self.item_heal = rumps.MenuItem("声卡与输入法一键体检自愈", callback=self.on_heal)
        self.item_lnp = rumps.MenuItem("网络权限：✅ 本地网络就绪（点此检查）", callback=self.on_open_lnp)
        self.item_pair = rumps.MenuItem("配对：检查中…", callback=self.on_pair)
        self.item_sys = rumps.MenuItem("接管系统输入（断线自动还原）", callback=self.on_sysinput)
        self.item_sys.state = self._flag_on(SYSINPUT_FILE)
        self._sys_switched = False
        self._last_input_drift_check = 0.0
        # 自愈旧版 LaunchAgent：若开启了自启且仍在使用旧 Agent，自动无缝迁移至 Login Item
        if LEGACY_AGENT.exists():
            debuglog.log("menu", "检测到旧版 LaunchAgent，自动迁移至 macOS 原生登录项")
            enable_autostart()

        self.item_autostart = rumps.MenuItem("开机自启（下次登录生效）",
                                             callback=self.on_autostart)
        self.item_autostart.state = 1 if is_autostart_enabled() else 0
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
            self.item_duck, self.item_gate, self.item_denoise, self.item_rec, self.item_open_rec,
            self.item_sys, self.item_heal, self.item_lnp, self.item_pair, self.item_reconnect,
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
            on_arm=self._on_ptt_arm,
            on_disarm=self._on_ptt_disarm,
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
        env = dict(os.environ)
        if _VENV_DIR.exists():
            env["VIRTUAL_ENV"] = str(_VENV_DIR)
            env["PATH"] = f"{_VENV_DIR / 'bin'}:{env.get('PATH', '')}"
        self.proc = subprocess.Popen(
            [PYTHON, str(ENGINE), "--auto"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            cwd=str(BASE),
            env=env,
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

    def _on_ptt_arm(self):
        """右 Option 硬件按下瞬间抢先静音系统输出（微秒级，消除 0.22s 判定等待期间扬声器漏音）。"""
        if self._flag_on(DUCK_FILE, default=1) and self.status == "streaming":
            self.ducker.preemptive_duck()

    def _on_ptt_disarm(self):
        """组合键跟进等判定取消录音时，撤销抢先静音。"""
        self.ducker.cancel_preemptive_duck()

    def _on_ptt_state_changed(self, active: bool):
        """右 Option 切换：同步更新 PTT 状态，并自动暂停/恢复背景音。"""
        self.ptt_active = active
        _write_ptt(active)
        debuglog.log("menu", f"右⌥ 切换录音 → {'开始录音' if active else '结束录音'}"
                             f"（录音存档开关={'开' if self._flag_on(RECORD_FILE) else '关'}"
                             f"，引擎={'在跑' if self.proc and self.proc.poll() is None else '未运行'}"
                             f"，状态={self.status}）")
        if active:
            # 虚拟声卡自愈守卫：录音前确保 BlackHole 未被外部会议软件静音
            try:
                self.ducker.ensure_device_unmuted(BLACKHOLE_NAME)
            except Exception:
                pass
            if self.status != "streaming":
                self.ducker.cancel_preemptive_duck()
                _play_sound("Basso")
                _show_notification("PhoneMic 未连通", "手机麦克风未连通（正在寻找中），语音输入暂不可用", sound="Basso")
                self.refresh()
                return
            _play_sound("Pop")
            self.ducker.duck()
        else:
            self.ducker.unduck()
            # 先解除静音再播提示音：既让用户听到"结束录音"反馈，
            # 也顺带确认输出管线已恢复（无声即说明异常）
            _play_sound("Tink")
        self.refresh()

    # ---------- 菜单动作 ----------

    def on_toggle(self, sender):
        if self.should_run:
            self.should_run = False
            self.ducker.unduck()   # 未 duck 时内部自动判断是否需解除遗留静音
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

    def on_gate(self, sender):
        new_state = not self._flag_on(GATE_FILE, default=1)
        try:
            GATE_FILE.write_text("1" if new_state else "0")
            sender.state = 1 if new_state else 0
        except Exception:
            pass
        debuglog.log("menu", f"切换语音输入门控模式 → {'开（仅录音时开麦，防前置串音）' if new_state else '关（常开全通）'}")

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
        # 虚拟声卡自愈守卫：接管前清除 BlackHole 上的历史静音与零音量
        try:
            self.ducker.ensure_device_unmuted(BLACKHOLE_NAME)
        except Exception:
            pass
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

    def on_heal(self, sender=None):
        """一键声卡与输入法健康体检与主动自愈。"""
        debuglog.log("menu", "触发声卡与输入法一键体检自愈")
        unmuted = media_ducking.ensure_device_unmuted(BLACKHOLE_NAME)
        diag = media_ducking.diagnose_device(BLACKHOLE_NAME)

        cur_in = self._query_input()
        takeover = self._flag_on(SYSINPUT_FILE) == 1
        if takeover and self.status == "streaming":
            self._take_sys_input()
            cur_in = self._query_input()

        status_lines = []
        if not diag["exists"]:
            status_lines.append(f"⚠️ 虚拟声卡：未找到 {BLACKHOLE_NAME}")
        else:
            in_m = "已清除静音" if diag["input_muted"] or unmuted else "正常"
            out_m = "已清除静音" if diag["output_muted"] else "正常"
            status_lines.append(f"✅ 虚拟声卡：就绪（输入={in_m}, 输出={out_m}）")

        status_lines.append(f"🎤 系统输入：{cur_in or '未知'}")
        if LNP_FLAG_FILE.exists() and self.status != "streaming":
            status_lines.append("⚠️ 本地网络：检测到 macOS 拦截局域网访问（建议插 USB 或前往设置放行）")
        else:
            status_lines.append("🌐 本地网络：正常")
        msg = " · ".join(status_lines)
        _show_notification("声卡与系统体检", msg, sound="Glass")
        debuglog.log("menu", f"体检自愈结果: {msg}")

    def on_open_lnp(self, sender=None):
        """一键打开 macOS 15+ 系统设置中的本地网络隐私面板，供用户放行或核验权限。"""
        debuglog.log("menu", "打开 macOS 本地网络隐私设置面板")
        try:
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"])
        except Exception as e:
            debuglog.log("menu", f"打开系统设置异常: {e}")

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
        currently_on = is_autostart_enabled()
        new_state = not currently_on
        if new_state:
            ok = enable_autostart()
            sender.state = 1 if ok else 0
            msg = "已开启开机自启（macOS 登录项已配置就绪）" if ok else "开启开机自启失败，请检查系统权限"
        else:
            ok = disable_autostart()
            sender.state = 0 if ok else 1
            msg = "已关闭开机自启" if ok else "关闭开机自启失败"
        debuglog.log("menu", f"切换开机自启: {msg}")
        _show_notification("PhoneMic 开机自启", msg, sound=None)

    # ---------- 状态刷新 ----------

    def _ensure_status_button(self):
        """确保在 NSApp 初始化完成后，第一时间完成现代 NSStatusBarButton 的可见性与固定正方形槽位绑定。"""
        if getattr(self, "_nsitem_initialized", False):
            return
        nsitem = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
        if nsitem:
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory
            )
            nsitem.setLength_(AppKit.NSSquareStatusItemLength)
            nsitem.setVisible_(True)
            nsitem.setImage_(None)  # 彻底清除 rumps 老接口遗留图片，杜绝双重图片/边距导致的宽度翻倍膨胀
            btn = getattr(nsitem, "button", lambda: None)()
            if btn:
                btn.setImagePosition_(AppKit.NSImageOnly)
                btn.setTitle_("")
            self._nsitem_initialized = True
            # 强制清空缓存，让下方的 _set_icon 100% 写入现代 button
            self._current_icon = None

    def _set_icon(self, path: str):
        """只在图标路径真正变化时才赋值，直通现代 NSStatusBarButton，锁定固定正方形槽位杜绝任何像素偏移。"""
        import AppKit
        if not getattr(self, "_nsitem_initialized", False):
            self._ensure_status_button()
        if getattr(self, "_current_icon", None) != path:
            self._current_icon = path
            self._icon = path
            try:
                # 现代 macOS (10.14+) NSStatusBarButton 纯色块渲染
                nsitem = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
                if nsitem:
                    nsitem.setLength_(AppKit.NSSquareStatusItemLength)
                    nsitem.setVisible_(True)
                    nsitem.setImage_(None)  # 保持老接口干净，防止系统触发 _updateButton 时重复叠加边距
                    btn = getattr(nsitem, "button", lambda: None)()
                    if btn:
                        img = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
                        if img:
                            img.setSize_((18, 18))
                            btn.setImage_(img)
                            btn.setImagePosition_(AppKit.NSImageOnly)
                        btn.setTitle_("")
            except Exception as e:
                debuglog.log("menu", f"设置状态栏按钮异常: {e}")

    @staticmethod
    def _set_title(item, title: str):
        """只在标题真正变化时才更新 MenuItem.title，减轻主线程 RunLoop 与 PyObjC 垃圾回收压力。"""
        if item.title != title:
            item.title = title

    def recording_on(self) -> bool:
        return self.status == "streaming" and self._flag_on(RECORD_FILE) == 1

    def refresh(self):
        self._ensure_status_button()
        # 配对状态
        self._set_title(self.item_pair, "配对：✅ 已配对（点此修改）" if phonemic.load_token()
                                         else "配对：⚠️ 未配对（插 USB 线自动配对，或点此手填）")

        # 本地网络隐私状态
        if LNP_FLAG_FILE.exists() and self.status != "streaming":
            self._set_title(self.item_lnp, "网络权限：⚠️ 局域网被拦截（点此去设置放行）")
        else:
            self._set_title(self.item_lnp, "网络权限：✅ 本地网络就绪（点此检查）")

        # PTT 监听状态显示
        if self.ptt_error:
            self._set_title(self.item_ptt, f"⚠️ PTT：{self.ptt_error}")
        elif self.ptt_active:
            self._set_title(self.item_ptt, "🎤 录音中（再按右⌥结束）")
        elif self.ptt_mode == "poll":
            self._set_title(self.item_ptt, "PTT：轮询模式（建议授权「输入监控」以穿透输入法拦截）")
        else:
            self._set_title(self.item_ptt, "PTT：按右⌥开始录音，再按结束")

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
                _show_notification("PhoneMic 断开连接", "手机麦克风已断开，已自动还原为 Mac 自带麦克风", sound="Sosumi")
            elif self._last_logged_status in ("connecting", None) and self.status == "streaming":
                _play_sound("Glass")
                _show_notification("PhoneMic 已连通", "手机麦克风已就绪，已接管系统音频输入", sound="Glass")
            self._last_logged_status = self.status

        # 系统输入接管：连通即接管，断线/停止自动还原
        takeover = self._flag_on(SYSINPUT_FILE) == 1
        if takeover and self.status == "streaming" and not self._sys_switched:
            self._take_sys_input()
        elif (not takeover or self.status != "streaming") and self._sys_switched:
            self._restore_sys_input()
        elif takeover and self.status == "streaming" and self._sys_switched:
            # 防系统输入设备漂移：每 5 秒巡检一次系统输入设备，防止蓝牙设备接入或系统意外重置输入设备
            now_drift = time.time()
            if now_drift - self._last_input_drift_check > 5.0:
                self._last_input_drift_check = now_drift
                cur_in = self._query_input()
                if cur_in and "blackhole" not in cur_in.lower():
                    debuglog.log("menu", f"⚠️ 检测到系统输入漂移为 {cur_in!r}，正在自动纠偏回 {BLACKHOLE_NAME}")
                    self._take_sys_input()

        if not self.should_run:
            self._set_icon(self.paths["stopped"])
            self._set_title(self.item_status, "○ PhoneMic 已停止")
            self._set_title(self.item_mode, "传输链路：⏸ 已停止")
            self._set_title(self.item_level, "电平诊断：--")
        elif self.status == "streaming":
            try:
                lv = int(LEVEL_FILE.read_text().strip() or 0)
            except Exception:
                lv = 0

            is_recording = self.ptt_active or _read_ptt()
            self._set_icon(self.paths["recording" if is_recording else "on"])
            status_tag = "（🎤 录音中）" if is_recording else ""

            last_url = ""
            try:
                if LAST_URL_FILE.exists():
                    last_url = LAST_URL_FILE.read_text().strip()
            except Exception:
                pass
            mode_tag = phonemic.link_mode_label(last_url) or "🔍 探测中…"
            self._set_title(self.item_status, "● 手机麦克风已连通" + status_tag)
            self._set_title(self.item_mode, f"传输链路：{mode_tag}")

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
            self._set_title(self.item_level, f"电平诊断：实时 {lv}% · 峰值 {wmax}% ({verdict})")
        else:
            self._set_icon(self.paths["connecting"])
            self._set_title(self.item_status, "◐ 正在寻找手机…")
            self._set_title(self.item_mode, "传输链路：🔍 正在探测 USB / UDP / Wi-Fi…")
            self._set_title(self.item_level, "电平诊断：--")
        self._set_title(self.item_toggle, "停止" if self.should_run else "启动")

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
        try:
            app.refresh()
        except Exception as e:
            debuglog.log("menu", f"定时器刷新异常: {e}", exc=True)

    timer = rumps.Timer(ticker, 0.25)
    timer.start()
    app.run()
