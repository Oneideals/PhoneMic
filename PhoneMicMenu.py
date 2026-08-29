#!/usr/bin/env python3
"""PhoneMicMenu — 菜单栏管理图标（图标化状态 + 录音红色指示）。"""
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

BASE = Path.home() / "GitHub" / "PhoneMic"
ENGINE = BASE / "phonemic.py"
PYTHON = sys.executable
AGENT = Path.home() / "Library" / "LaunchAgents" / "com.jerry.phonemic.menu.plist"
ICON_DIR = BASE / "icons"

GAIN_FILE = BASE / "gain_db"
LEVEL_FILE = BASE / ".level"
DENOISE_FILE = BASE / "denoise"
RECORD_FILE = BASE / "record"
REC_DIR = BASE / "recordings"
GAIN_CHOICES = [0, 3, 6, 9, 12]

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
        self.item_rec = rumps.MenuItem("录音存档（原始音频+电平CSV）", callback=self.on_record)
        self.item_rec.state = self._flag_on(RECORD_FILE)
        self.item_open_rec = rumps.MenuItem("打开录音文件夹", callback=self.on_open_rec)
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
                     self.item_denoise, self.item_rec, self.item_open_rec,
                     self.item_toggle, self.item_autostart, None]
        self.sync_gain_state()
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

    def watch(self):
        """跟随引擎输出刷新状态；进程退出则标记停止。"""
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line.startswith("[音频]"):
                    self.status = "streaming"
                elif line.startswith(("[连接]", "[发现]")):
                    if self.status != "streaming":
                        self.status = "connecting"
            if self.should_run:
                time.sleep(2)
                if self.should_run and self.proc.poll() is not None:
                    self.spawn()
                    return
        except Exception:
            pass
        if not self.should_run:
            self.status = "stopped"

    # ---------- 菜单动作 ----------

    def on_toggle(self, sender):
        if self.should_run:
            self.should_run = False
            try:
                self.proc.terminate()
            except Exception:
                pass
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

    def on_open_rec(self, sender):
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["open", str(REC_DIR)])
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
        if not self.should_run:
            self.icon = self.paths["stopped"]
            status_text = TEXTS["stopped"]
        elif self.status == "streaming":
            self.icon = self.paths["recording" if self.recording_on() else "on"]
            status_text = "● 手机麦克风已连通" + ("（录音存档中）" if self.recording_on() else "")
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
            if wmax < 8:
                verdict = "静音或未说话（对着手机说一句试试）"
            elif wmax < 15:
                verdict = f"峰值 {wmax}% 偏小 → 建议加增益（手机＋3dB 或本菜单选高档）"
            elif wmax <= 90:
                verdict = f"峰值 {wmax}% ✓ 合适，保持即可"
            else:
                verdict = f"峰值 {wmax}% 过大 → 建议降增益（有削波风险）"
            self.item_status.title = "电平诊断: " + verdict
        else:
            self.item_status.title = status_text
        self.item_toggle.title = "停止" if self.should_run else "启动"


if __name__ == "__main__":
    app = PhoneMicMenu()

    def ticker(_):
        app.refresh()

    timer = rumps.Timer(ticker, 1)
    timer.start()
    app.run()
