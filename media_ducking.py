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
from typing import List, Optional

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
    return success


# ---------- MediaRemote 媒体控制 ----------

_mr = None
try:
    _mr_path = "/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote"
    _mr = ctypes.CDLL(_mr_path)
    _mr.MRMediaRemoteSendCommand.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _mr.MRMediaRemoteSendCommand.restype = ctypes.c_bool
    _mr.MRMediaRemoteGetNowPlayingApplicationIsPlaying.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _mr.MRMediaRemoteGetNowPlayingApplicationIsPlaying.restype = None
except Exception:
    _mr = None

_kMRPlay = 0
_kMRPause = 1
_kMRTogglePlayPause = 2
_kMRStop = 3


class _BlockDescriptor(ctypes.Structure):
    _fields_ = [
        ("reserved", ctypes.c_ulong),
        ("size", ctypes.c_ulong),
        ("copy_helper", ctypes.c_void_p),
        ("dispose_helper", ctypes.c_void_p),
        ("signature", ctypes.c_char_p),
    ]


class _BlockLiteral(ctypes.Structure):
    pass


_BlockInvoke = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)

_BlockLiteral._fields_ = [
    ("isa", ctypes.c_void_p),
    ("flags", ctypes.c_int32),
    ("reserved", ctypes.c_int32),
    ("invoke", _BlockInvoke),
    ("descriptor", ctypes.POINTER(_BlockDescriptor)),
]

_descriptor = _BlockDescriptor()
_descriptor.reserved = 0
_descriptor.size = ctypes.sizeof(_BlockLiteral)
_descriptor.copy_helper = None
_descriptor.dispose_helper = None
_descriptor.signature = b"v12@?0B8"


def check_media_remote_playing(timeout: float = 0.08) -> bool:
    """查询 macOS MediaRemote (Now Playing) 当前是否正在播放音频/视频。"""
    if not _mr:
        return False
    try:
        system_lib = ctypes.CDLL(ctypes.util.find_library("System"))
        _NSConcreteGlobalBlock = ctypes.c_void_p.in_dll(system_lib, "_NSConcreteGlobalBlock")

        is_playing_val = [False]
        done_event = threading.Event()

        def block_fn(block_ptr, is_playing):
            is_playing_val[0] = bool(is_playing)
            done_event.set()

        c_block_fn = _BlockInvoke(block_fn)

        block = _BlockLiteral()
        block.isa = ctypes.addressof(_NSConcreteGlobalBlock)
        block.flags = 0x50000000
        block.reserved = 0
        block.invoke = c_block_fn
        block.descriptor = ctypes.pointer(_descriptor)

        libdispatch = system_lib
        libdispatch.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
        libdispatch.dispatch_get_global_queue.restype = ctypes.c_void_p
        queue = libdispatch.dispatch_get_global_queue(0, 0)

        _mr.MRMediaRemoteGetNowPlayingApplicationIsPlaying(queue, ctypes.byref(block))
        done_event.wait(timeout=timeout)
        return is_playing_val[0]
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

class AudioDucker:
    """智能音频避让控制器：负责在语音识别时静音与暂停播放，识别结束时恢复。"""

    def __init__(self, enabled_getter=lambda: True):
        self.enabled_getter = enabled_getter
        self._lock = threading.Lock()
        self._is_ducked = False
        self._did_mute_system = False
        self._was_playing_media = False
        self._paused_apps: List[str] = []

    @property
    def is_ducked(self) -> bool:
        return self._is_ducked

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
                set_system_mute(True)
                self._did_mute_system = True
            else:
                self._did_mute_system = False

    def unduck(self) -> None:
        """退出录音状态：恢复系统静音与媒体播放。"""
        with self._lock:
            if not self._is_ducked:
                return
            self._is_ducked = False

            # 1. 恢复系统静音状态
            if self._did_mute_system:
                set_system_mute(False)
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
