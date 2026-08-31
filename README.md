# PhoneMic — 把安卓手机变成 Mac 的专业麦克风

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

用一部安卓手机 + 一台 Mac，搭建一条**低延迟、可自愈、可存档**的无线麦克风链路，
专为语音输入（微信输入法语音转文字等 ASR 场景）优化信号质量。

> 本项目以 MIT 许可证公开发布，欢迎使用、修改与贡献。

## 特性

| 能力 | 说明 |
|---|---|
| 自动发现 | 手机端 mDNS 广播 + Mac 端三级回退（发现 → 缓存地址 → UDP 端口扫描），换 IP/端口免疫 |
| 自愈重连 | 断流自动重连、进程异常自动拉起、单实例锁防重复 |
| 双端增益 | 手机端 0~+18dB（带削波保护）/ Mac 端 0~12dB，文件热更新即时生效 |
| 双层降噪 | 手机端系统 NoiseSuppressor + Mac 端 ffmpeg afftdn 稳态噪声过滤 |
| 磁贴控制 | Android 下拉快捷磁贴一键启停（免解锁） |
| 菜单栏管理 | Mac 菜单栏图标：绿点=录音存档中 / 白点=连通 / 空心环=寻找手机，含电平诊断与开机关停 |
| 录音存档 | 原始音频 WAV（自动转 FLAC）+ 电平/欠载/削波指标时间线 CSV |
| PTT 录音 | 单击右 ⌥ 开始/停止录音（按住说话），松开自动转存 FLAC |
| 媒体闪避 | 录音时自动暂停背景音乐并静音系统，结束自动恢复 |
| 系统输入接管 | 一键把系统默认输入切到手机麦克风，断线自动还原 |

## 架构

```
手机（PhoneMic App：AudioRecord 采集 → 增益/降噪 → HTTP WAV 流）
        │  局域网 Wi-Fi（mDNS + UDP 公告自动发现，端口漂移免疫）
        ▼
Mac（phonemic.py 引擎：拉流 → 降噪 afftdn → 增益 → 写入 BlackHole 虚拟声卡）
        │
        ▼
任意 Mac 应用把输入设备选成 "BlackHole 2ch"（如微信输入法语音输入）
```

## 环境要求

- **Mac 端**：macOS 12+；Python 3.10+；[BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole) 虚拟声卡；[ffmpeg](https://ffmpeg.org)（降噪与 FLAC 转码，可选但推荐）；[SwitchAudioSource](https://github.com/deweller/switchaudio-osx)（系统输入接管，可选）
- **安卓端**：Android 8.0+（API 26+），无需 Google Play

## 安装

### Mac 端

```bash
git clone https://github.com/Oneideals/PhoneMic.git
cd PhoneMic
python3 -m venv .venv
source .venv/bin/activate
pip install sounddevice numpy zeroconf rumps
brew install blackhole-2ch ffmpeg
# 可选：系统输入一键接管需要
brew install switchaudio-osx
```

启动：

```bash
./PhoneMic.command        # 双击也行：若已在运行不会重复启动
# 或
python PhoneMicMenu.py
```

菜单栏出现状态圆点后即就绪。

### 安卓端

用 Android Studio 打开本仓库的 `android/` 目录，构建并安装 `app` 模块到手机（Minimum SDK 26）。
或自行用 Gradle 构建 APK 后侧载。

> 也可从 Release 页下载预编译 APK（若提供）。

## 使用

1. 手机端启动 PhoneMic 服务（或下拉快捷磁贴一键启停）
2. Mac 端菜单栏应自动发现手机并显示白点；连通后保持稳定
3. 在需要麦克风的应用里，把输入设备选成 **BlackHole 2ch**
   - 跟随系统默认的 App：开启菜单栏「接管系统输入」即可自动切换
   - 自管设备的 App（Zoom / 腾讯会议 / OBS 等）：在它们的音频设置里手动选一次 BlackHole 2ch，之后会记住
4. 说话即可；菜单栏图标绿点表示正在录音存档

## 工作原理

1. 手机通过 `AudioRecord` 采集麦克风，做增益与 NoiseSuppressor 降噪，再以无限长 WAV 的 HTTP 流推送。
2. Mac 端引擎 `phonemic.py` 拉流，经 ffmpeg `afftdn` 二次降噪与数字增益，写入 BlackHole 虚拟声卡。
3. BlackHole 是系统级音频输入设备，任何能选麦克风的 App 都能用它。

发现采用 mDNS + UDP 公告双通道；断流后引擎退避重试，手机回归即秒级重连。

## 隐私

- 所有音频**只在本机处理**，不连接任何外部服务器（仅需手机与 Mac 在同一局域网）。
- 录音存档默认关闭；开启后数据仅保存在本机 `recordings/` 目录（已 gitignore），不会上传。
- 仓库不含任何密钥、令牌或个人数据。

## 诊断日志（可选）

引擎与菜单栏内置可选的调试日志，落盘到项目根目录的 `.debug.log`（已被 gitignore，不会入库），
用于排查「断流 / 录不上」等问题。开销极低（约 165KB/小时），如不需要可删除 `debuglog.py` 及其两处 `import`。

```bash
tail -f .debug.log
```

## 开发

```bash
# 运行 Mac 端集成测试（需要本机已装 BlackHole）
python tests/test_mac.py
```

提交请遵循仓库既有的结构化提交规范（见 `git log` 历史：含「核心问题诊断」「Release Notes」「变更模块」等分段）。
建议先从 Issue 讨论入手。

## 许可证

[MIT](LICENSE) © 2026 Oneideals
