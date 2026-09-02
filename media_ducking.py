#!/usr/bin/env python3
"""media_ducking — 语音识别/录音期间的背景音频避让与自动暂停/恢复控制。

当用户开启语音识别/录音时：
1. 检测当前媒体播放状态并暂停（MediaRemote + 原生播放器）；
2. 瞬时将系统输出设备静音（CoreAudio C API 微秒级生效，防任何网页/应用声音漏出干扰麦克风）；
当用户结束语音识别/录音时：
1. 瞬时解除系统输出静音；
2. 仅在录音前确实有媒体播放时，自动恢复媒体播放（无播放时不误触发）。
"""
import ctypes
import ctypes.util
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

# ---------- CoreAudio 系统输出静音控制 ----------

_core_audio = None
try:
    _ca_path = ctypes.util.find_library("CoreAudio")
    if _ca_path:
        _core_audio = ctypes.CDLL(_ca_path)
except Exception:
    _core_audio = None


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_kAudioHardwarePropertyDefaultOutputDevice = 0x644F7574  # 'dOut'
_kAudioObjectSystemObject = 1
_kAudioObjectPropertyScopeGlobal = 0x676C6F62           # 'glob'
_kAudioObjectPropertyScopeOutput = 0x6F757470           # 'outp'
_kAudioDevicePropertyMute = 0x6D757465                  # 'mute'
_kAudioDevicePropertyVolumeScalar = 0x766F6C6D          # 'volm'
_kAudioObjectPropertyElementMain = 0                    # 0


def get_default_output_device_id() -> Optional[int]:
    if not _core_audio:
        return None
    try:
        addr = _AudioObjectPropertyAddress(
            _kAudioHardwarePropertyDefaultOutputDevice,
            _kAudioObjectPropertyScopeGlobal,
            _kAudioObjectPropertyElementMain,
        )
        dev_id = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(dev_id))
        status = _core_audio.AudioObjectGetPropertyData(
            _kAudioObjectSystemObject,
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(dev_id),
        )
        return dev_id.value if status == 0 else None
    except Exception:
        return None


def poke_system_volume() -> bool:
    """唤醒并刷新系统默认输出设备的音量管线。

    关键点：虚拟声卡（Boom 3D 等）与 USB DAC 通常只对"真实的音量变化"作出反应，
    写回同一个值会被硬件/驱动直接忽略（等于没唤醒）。因此这里先小幅改变音量
    （±0.02）再立即还原，等效于用户手动"调一下音量"，随后立即恢复原值。
    """
    success = False
    dev_id = get_default_output_device_id()
    if dev_id and _core_audio:
        for elem in [_kAudioObjectPropertyElementMain, 1, 2]:
            try:
                addr = _AudioObjectPropertyAddress(
                    _kAudioDevicePropertyVolumeScalar,
                    _kAudioObjectPropertyScopeOutput,
                    elem,
                )
                vol = ctypes.c_float(0.0)
                size = ctypes.c_uint32(ctypes.sizeof(vol))
                status = _core_audio.AudioObjectGetPropertyData(
                    dev_id,
                    ctypes.byref(addr),
                    0,
                    None,
                    ctypes.byref(size),
                    ctypes.byref(vol),
                )
                if status == 0 and size.value == ctypes.sizeof(vol):
                    # 计算一个与原值不同的"轻推"值（音量接近上限时向下轻推）
                    nudged = vol.value + 0.02 if vol.value < 0.98 else vol.value - 0.02
                    nudged = min(max(nudged, 0.0), 1.0)
                    for target in (nudged, vol.value):
                        t = ctypes.c_float(target)
                        set_status = _core_audio.AudioObjectSetPropertyData(
                            dev_id,
                            ctypes.byref(addr),
                            0,
                            None,
                            size,
                            ctypes.byref(t),
                        )
                        if set_status == 0:
                            success = True
            except Exception:
                pass
    if not success:
        # 兜底 AppleScript 广播当前音量以激活声卡管线
        try:
            subprocess.run(
                ["osascript", "-e", "set volume output volume (output volume of (get volume settings))"],
                capture_output=True,
                timeout=0.2,
            )
            success = True
        except Exception:
            pass
    return success


def get_system_mute() -> bool:
    """获取当前系统默认输出是否静音。"""
    dev_id = get_default_output_device_id()
    if dev_id and _core_audio:
        for elem in [_kAudioObjectPropertyElementMain, 1]:
            try:
                addr = _AudioObjectPropertyAddress(
                    _kAudioDevicePropertyMute,
                    _kAudioObjectPropertyScopeOutput,
                    elem,
                )
                muted = ctypes.c_uint32(0)
                size = ctypes.c_uint32(ctypes.sizeof(muted))
                status = _core_audio.AudioObjectGetPropertyData(
                    dev_id,
                    ctypes.byref(addr),
                    0,
                    None,
                    ctypes.byref(size),
                    ctypes.byref(muted),
                )
                if status == 0:
                    return bool(muted.value)
            except Exception:
                pass
    # 兜底 AppleScript
    try:
        r = subprocess.run(
            ["osascript", "-e", "output muted of (get volume settings)"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False


def set_system_mute(mute: bool) -> bool:
    """设置系统默认输出静音状态。"""
    success = False
    dev_id = get_default_output_device_id()
    if dev_id and _core_audio:
        for elem in [_kAudioObjectPropertyElementMain, 1]:
            try:
                addr = _AudioObjectPropertyAddress(
                    _kAudioDevicePropertyMute,
                    _kAudioObjectPropertyScopeOutput,
                    elem,
                )
                val = ctypes.c_uint32(1 if mute else 0)
                size = ctypes.c_uint32(ctypes.sizeof(val))
                status = _core_audio.AudioObjectSetPropertyData(
                    dev_id,
                    ctypes.byref(addr),
                    0,
                    None,
                    size,
                    ctypes.byref(val),
                )
                if status == 0:
                    success = True
            except Exception:
                pass
    if not success:
        # 兜底 AppleScript
        try:
            val_str = "true" if mute else "false"
            subprocess.run(
                ["osascript", "-e", f"set volume output muted {val_str}"],
                capture_output=True,
                timeout=1,
            )
            success = True
        except Exception:
            pass

    # 解除静音时，唤醒并同步音量管线（防止 Boom 3D / USB DAC 处于休眠或增益为 0 状态）
    if not mute:
        poke_system_volume()

    return success


# ---------- MediaRemote 媒体控制 ----------

_mr = None
try:
    _mr_path = "/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote"
    _mr = ctypes.CDLL(_mr_path)
    _mr.MRMediaRemoteSendCommand.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _mr.MRMediaRemoteSendCommand.restype = ctypes.c_bool
except Exception:
    _mr = None

_kMRPlay = 0
_kMRPause = 1
_kMRTogglePlayPause = 2
_kMRStop = 3


def check_media_remote_playing(timeout: float = 0.08) -> bool:
    """查询 macOS 当前是否正在播放音频/视频（安全无崩溃检测）。"""
    try:
        active = get_applescript_active_players()
        return len(active) > 0
    except Exception:
        return False


def get_applescript_active_players() -> List[str]:
    """查询当前处于播放状态的已知原生播放器（Music, Spotify, QuickTime）。"""
    script = """
    set active_list to {}
    if application "Music" is running then
        tell application "Music"
            try
                if player state is playing then set end of active_list to "Music"
            end try
        end tell
    end if
    if application "Spotify" is running then
        tell application "Spotify"
            try
                if player state is playing then set end of active_list to "Spotify"
            end try
        end tell
    end if
    if application "QuickTime Player" is running then
        tell application "QuickTime Player"
            try
                repeat with doc in documents
                    if playing of doc is true then
                        set end of active_list to "QuickTime Player"
                        exit repeat
                    end if
                end repeat
            end try
        end tell
    end if
    return active_list
    """
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=0.25,
        )
        out = r.stdout.strip()
        if not out:
            return []
        return [p.strip() for p in out.split(",") if p.strip()]
    except Exception:
        return []


def resume_applescript_players(players: List[str]) -> None:
    for app in players:
        try:
            if app in ("Music", "Spotify"):
                subprocess.Popen(["osascript", "-e", f'tell application "{app}" to play'])
            elif app == "QuickTime Player":
                subprocess.Popen(["osascript", "-e", 'tell application "QuickTime Player" to play every document'])
        except Exception:
            pass


# ---------- AudioDucker 控制器 ----------

def _ducker_log(msg: str) -> None:
    """避让控制器日志（落盘 .debug.log，缺失时静默）。"""
    try:
        import debuglog
        debuglog.log("ducker", msg)
    except Exception:
        pass


class AudioDucker:
    """智能音频避让控制器：负责在语音识别时静音与暂停播放，识别结束时恢复。

    崩溃安全设计：当传入 state_file 时，「由本控制器执行的系统静音」会先落盘
    标记再执行；进程若在静音期间被 SIGKILL/崩溃（atexit 不会运行），下一次
    启动会通过该标记检测到遗留静音并自动解除（或重建避让状态），杜绝
    "用过 PhoneMic 后系统无声、要手动调音量才恢复"的泄漏。
    """

    def __init__(self,
                 enabled_getter: Callable[[], bool] = lambda: True,
                 state_file: Optional[str] = None,
                 still_recording_getter: Callable[[], bool] = lambda: False):
        self.enabled_getter = enabled_getter
        self._lock = threading.Lock()
        self._is_ducked = False
        self._did_mute_system = False
        self._was_playing_media = False
        self._paused_apps: List[str] = []
        self._state_file = Path(state_file) if state_file else None
        self._still_recording_getter = still_recording_getter
        self._recover_orphaned_mute()

    @property
    def is_ducked(self) -> bool:
        return self._is_ducked

    # ---------- 持久化标记 ----------

    def _mark_mute_by_us(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.write_text("1")
        except Exception:
            pass

    def _clear_mute_mark(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _has_mute_mark(self) -> bool:
        if self._state_file is None:
            return False
        try:
            return self._state_file.exists()
        except Exception:
            return False

    def _recover_orphaned_mute(self) -> None:
        """启动时检测上次进程异常退出遗留的系统静音（duck 泄漏）。"""
        if not self._has_mute_mark():
            return
        try:
            if self._still_recording_getter():
                # 用户仍处于录音状态（例如按着 PTT 键期间进程被杀重启）：
                # 重建避让状态，保证松开按键时能正常解除静音
                self._is_ducked = True
                self._did_mute_system = True
                set_system_mute(True)
                _ducker_log("检测到上次异常退出遗留的静音且录音仍在进行——已重建避让状态")
            else:
                set_system_mute(False)
                self._clear_mute_mark()
                _ducker_log("检测到上次异常退出遗留的系统静音，已自动解除")
        except Exception:
            _ducker_log("遗留静音恢复失败（下次 unduck/启动时重试）")

    # ---------- duck / unduck ----------

    def duck(self) -> None:
        """进入录音状态：暂停背景播放并系统静音。"""
        if not self.enabled_getter():
            return
        with self._lock:
            if self._is_ducked:
                return
            self._is_ducked = True

            # 1. 检查媒体播放状态
            mr_playing = check_media_remote_playing(timeout=0.08)
            as_players = get_applescript_active_players()
            self._was_playing_media = mr_playing or bool(as_players)
            self._paused_apps = as_players

            # 2. 如果正在播放，发送暂停指令
            if self._was_playing_media:
                if _mr:
                    try:
                        _mr.MRMediaRemoteSendCommand(_kMRPause, None)
                    except Exception:
                        pass
                for app in as_players:
                    try:
                        subprocess.Popen(["osascript", "-e", f'tell application "{app}" to pause'])
                    except Exception:
                        pass

            # 3. 检查并设置系统输出静音（微秒级消除所有应用外放音，防麦克风串音）
            was_muted = get_system_mute()
            if not was_muted:
                # 先落盘标记再静音：即使进程在两步之间被杀，下次启动也能恢复
                self._mark_mute_by_us()
                set_system_mute(True)
                self._did_mute_system = True
            else:
                self._did_mute_system = False

    def unduck(self) -> None:
        """退出录音状态：恢复系统静音与媒体播放。"""
        with self._lock:
            # 兜底：本实例未 duck，但磁盘上存在遗留静音标记（进程重启后的首次
            # 释放）→ 仍然解除静音，避免泄漏的静音永久粘在系统上
            orphan_release = False
            if not self._is_ducked:
                if self._has_mute_mark():
                    orphan_release = True
                else:
                    return
            self._is_ducked = False

            # 1. 恢复系统静音状态
            if self._did_mute_system or orphan_release:
                if set_system_mute(False):
                    self._clear_mute_mark()
                # 解除失败时保留标记，下次 unduck/启动时重试
                self._did_mute_system = False

            # 2. 仅在录音前确实有媒体播放时，恢复播放
            if self._was_playing_media:
                if _mr:
                    try:
                        _mr.MRMediaRemoteSendCommand(_kMRPlay, None)
                    except Exception:
                        pass
                if self._paused_apps:
                    resume_applescript_players(self._paused_apps)
                self._was_playing_media = False
                self._paused_apps = []

    def cleanup(self) -> None:
        """进程退出前清理状态，防止系统遗留静音。"""
        self.unduck()
