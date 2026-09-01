# PhoneMic — 把安卓手机变成 Mac 的专业无线麦克风

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/Oneideals/PhoneMic/actions/workflows/ci.yml/badge.svg)](https://github.com/Oneideals/PhoneMic/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Android 8.0+](https://img.shields.io/badge/Android-8.0+-green.svg)](https://developer.android.com)

用一部安卓手机 + 一台 Mac，搭建一条**低延迟、可自愈、可存档**的无线麦克风链路，
专为语音输入（微信输入法语音转文字等 ASR 场景）优化信号质量。

> 本项目以 MIT 许可证公开发布，欢迎使用、修改与贡献。

## 特性

| 能力 | 说明 |
|---|---|
| 自动发现 | 手机端 mDNS 广播 + Mac 端三级回退（发现 → 缓存地址 → UDP 端口扫描），换 IP/端口免疫 |
| 安全配对 | 手机生成配对码，无线连接凭码鉴权；插 USB 线自动完成配对，全程无感 |
| 自愈重连 | 断流自动重连、进程异常自动拉起、单实例锁防重复 |
| 双端增益 | 手机端 0~+18dB / Mac 端 0~+18dB（均带软限幅保护），文件热更新即时生效 |
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

## 快速安装

### 1. Mac 端配置

```bash
# 克隆仓库
git clone https://github.com/Oneideals/PhoneMic.git
cd PhoneMic

# 安装 Python 虚拟环境与依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 安装系统级依赖
brew install blackhole-2ch ffmpeg

# 可选：系统输入一键接管工具
brew install switchaudio-osx
```

启动菜单栏管理应用：

```bash
./PhoneMic.command        # 双击即可启动（后台守护，防重复拉起）
# 或手动运行
python PhoneMicMenu.py
```

菜单栏出现状态圆点后即就绪。

### 2. 安卓端安装

- **直接下载**：前往 [Releases 页面](https://github.com/Oneideals/PhoneMic/releases) 下载最新预编译 `PhoneMic-v*.apk` 并安装到手机。
- **源码构建**：
  ```bash
  cd android
  ./gradlew assembleDebug
  # 构建产物位于 android/app/build/outputs/apk/debug/app-debug.apk
  ```
  或使用 Android Studio 直接打开 `android/` 项目构建安装。

## 使用说明

1. 手机端启动 PhoneMic 服务（或下拉通知栏快捷磁贴一键启停）
2. **配对（只需一次）**
   - **插过 USB 数据线**：无需任何操作，Mac 端会自动取回配对码并存档
   - **纯 Wi-Fi**：把手机主界面上的「配对码」抄到 Mac 菜单栏 →「配对」里
3. Mac 端菜单栏自动发现手机并显示状态圆点；连通后保持稳定
4. 在需要麦克风的应用里，把输入设备选成 **BlackHole 2ch**
   - **跟随系统默认的 App**：开启菜单栏「接管系统输入」即可自动切换
   - **自管设备的 App**（Zoom / 腾讯会议 / 微信 / OBS 等）：在设置中手动选择一次 BlackHole 2ch
5. 说话即可开始语音输入；菜单栏图标绿点表示正在录音存档

## 隐私与安全

- **纯局域网处理**：所有音频流仅在手机与 Mac 之间的局域网直连传输，绝不经过外部中转或云端。
- **配对码访问控制**：局域网可达 ≠ 任何人可听。无线连接必须携带手机生成的配对码，
  未配对的 HTTP 请求返回 401、未配对的 UDP 注册直接丢弃。回环（USB/`adb forward`）
  连接免鉴权，因为该通道本身已要求设备本地访问权。详见 [SECURITY.md](SECURITY.md)。
- **本地存储控制**：录音存档默认关闭；开启后的音频文件与指标仅存储在本机 `recordings/` 目录（已加入 `.gitignore`）。
- **开源合规**：仓库不包含任何私有证书、密钥或隐私数据；配对码存于 `.phonemic_token`（`0600`，已忽略）。

## 工作原理

1. **音频采集与服务端**：手机端通过 `AudioRecord` 以 48kHz/16bit/单声道采集，应用硬件 NoiseSuppressor 降噪与数字增益后，启动极轻量 HTTP 服务推送无限长 WAV 数据流。
2. **零配置自动发现**：手机端通过 mDNS (`_phonemic._tcp.local.`) 和 UDP 广播 (`255.255.255.255:58080`) 发布服务地址，Mac 端三级自动发现与毫秒级故障自愈。
3. **音频流水线**：Mac 端引擎 `phonemic.py` 拉流后经由 ffmpeg `afftdn` 稳态二次降噪与动态增益，实时写入 BlackHole 虚拟声卡供各应用低延迟消费。

## 诊断日志（可选）

引擎与菜单栏内置轻量级调试日志（位于项目根目录 `.debug.log`，已被 `.gitignore` 忽略），开销约 165KB/小时：

```bash
tail -f .debug.log
```

## 开发者指南

```bash
# 纯逻辑测试（无需任何硬件，CI 也跑这个）
python tests/test_logic.py

# Mac 端集成测试（需已安装 BlackHole）
# 注意：请先在菜单栏「退出 PhoneMic」。集成测试要独占单实例锁与 BlackHole，
# 应用在跑的时候这几条会以「已有 PhoneMic 实例在运行」的形式失败。
python tests/test_mac.py
```

欢迎提交 PR 或 Issue！提交代码前请参考 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 许可证

[MIT](LICENSE) © 2026 Oneideals
