# PhoneMic — 把安卓手机变成 Mac 的专业无线高保真麦克风

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/Oneideals/PhoneMic/actions/workflows/ci.yml/badge.svg)](https://github.com/Oneideals/PhoneMic/actions/workflows/ci.yml)
[![macOS](https://img.shields.io/badge/macOS-12%2B%20(Apple%20Silicon%20%2F%20Intel)-black.svg)](https://www.apple.com/macos/)
[![Android](https://img.shields.io/badge/Android-8.0%2B-green.svg)](https://developer.android.com)

用一部安卓手机 + 一台 Mac，搭建一条**超低延迟、AI 智能降噪、自愈重连、开箱即用**的专业级无线麦克风链路。  
专为**日常语音输入**（微信输入法、飞书、剪映等 ASR 语音转文字）、**线上会议**（腾讯会议、Zoom、Teams）、**直播推流与语音通话**量身打造。

> 💡 **PhoneMic 现已作为独立桌面应用（macOS App Bundle）发布**，免除任何复杂的命令行或 Python 环境搭建，双击即可无感常驻。

---

## ⚡ 30 秒极速上手

### 步骤 1：Mac 端（下载安装）

1. 前往 **[Releases 页面](https://github.com/Oneideals/PhoneMic/releases)** 下载最新的 `PhoneMic.app`（或打包文件），解压并拖入 `访达 (Finder) -> 应用程序 (Applications)`。
2. 安装虚拟声卡驱动（用于将手机声音路由至系统音频输入）：
   ```bash
   brew install blackhole-2ch
   ```
   *（亦可直接在 [BlackHole 官网](https://existential.audio/blackhole/) 获取官方 `.pkg` 安装包一键安装）*
3. 双击启动 **PhoneMic.app**，屏幕右上角顶部菜单栏将立即出现纯色圆形图标。

### 步骤 2：安卓端（安装并开启服务）

1. 前往 **[Releases 页面](https://github.com/Oneideals/PhoneMic/releases)** 下载最新 `PhoneMic-v*.apk` 并安装至手机。
2. 打开 App 点击**「启动服务」**（亦可直接下拉系统通知栏，添加**快捷设置磁贴**一键免解锁启停）。

### 步骤 3：自动连通与使用

- **无线自动连接**：确保手机与 Mac 处于同一 Wi-Fi 局域网，无需手动填写 IP，系统通过 mDNS 与广播自动秒连，Mac 菜单栏图标将亮起 **🔵 蓝色实心圆**。
  - *初次无线连接*：将手机界面的 4 位配对码输入 Mac 菜单栏「配对...」中即可（只需一次）。
- **有线极速连接（免配对）**：直接用 USB 数据线连接手机与 Mac，即插即用，零配置免鉴权秒连。
- **开始使用**：
  - 在菜单栏中勾选**「接管系统输入」**，系统默认麦克风将自动切为手机输入，断开时自动无缝还原。
  - 在 Zoom / 腾讯会议 / 剪映 / 微信输入法等 App 中，直接将麦克风选择为 **BlackHole 2ch** 即可享受清晰无线输入。

---

## ✨ 核心亮点与产品优势

### 🎧 现代轻量级 AI 语音降噪管线
- **RNNoise 深度学习降噪**：内置高精度神经网络模型，精准剥离机械键盘敲击声、鼠标点击声与室内突发杂音，还原自然人声。
- **双模型实时切换**：内置 `std`（标准平衡）与 `cb`（人声清晰度增强）双模型，支持在 macOS 菜单栏子菜单中**零延时热切换**。
- **极致能效**：基于 Apple Silicon 深度优化，处理流速高达 **100x+ 实时速率**，单核 CPU 占用通常小于 1%，绝不拖慢整机性能。

### 📶 电信级链路诊断与网络自愈 (RFC 3550)
- **毫秒级质量监测**：严格遵循 RFC 3550 协议标准，实时统计**网络传输抖动（Jitter ms）**、**100 报文滑动窗口丢包率（Packet Loss %）**与缓冲水位。
- **菜单栏轻量呈现**：菜单栏与体检面板直观展示实时传输健康度（如 `📶 抖动: 1.2ms | 丢包: 0.0%`），遇到网络扰动与断流自动毫秒级平滑重连。

### 🍏 专为新版 macOS (Sequoia / Sonoma) 深度适配
- **纯净静默常驻**：原生采用 `NSApplicationActivationPolicyAccessory` 模式，**彻底消除 Dock 栏 Python 图标与 `Command + Tab` 切换干扰**。
- **像素级零位移**：采用 macOS 原生固定尺寸正方形槽位（22×22 pt），待命与工作状态切换时**杜绝任何像素抖动与位移**。
- **本地网络隐私（LNP）自愈**：针对 macOS 15+ 严格的网络沙箱限制，遇到局域网阻断时智能预警，并支持**一键直达系统设置面板授权**。
- **开机静默自启**：在菜单中勾选「开机自启」即可自动写入 macOS 系统登录项，开机直接待命。

### 🎙️ 贴心的生产力集成
- **一键录音与 PTT (Push-to-Talk)**：单击或长按键盘右 `Option (⌥)` 键即可快速开始/停止录音，松开自动转存为无损 FLAC 音频。
- **智能媒体闪避 (Audio Ducking)**：语音录制或通话时，自动暂停系统音乐播放并降低环境音，录制结束自动恢复。

---

## 🧭 菜单栏图标色块速查

Mac 顶部菜单栏通过纯色块实时反映链路工作状态：

| 状态色块 | 当前状态 | 行为说明 |
| :---: | :--- | :--- |
| 🟠 **橙色圆环** | **探测手机中** | 正在通过 USB 回环、mDNS 发现与 UDP 广播多通道并行寻找手机端 |
| 🔵 **蓝色实心圆** | **手机已连通就绪** | 链路已建立，音频实时推流中，系统默认输入已自动接管 |
| 🟢 **绿色实心圆** | **PTT 录音 / 锁定中** | 正在按住说话或锁定录音中，触发媒体背景音自动闪避 |
| ⭕ **暗灰圆环** | **已手动停止** | 用户在菜单中点击了「停止」，进程处于低功耗待命休眠状态 |

---

## 🔒 隐私与安全性

- **全链路本地直连**：所有音频流仅在手机与 Mac 之间通过内网点对点传输，**绝不上传任何云端服务器**，从物理层面杜绝窃听。
- **动态配对码鉴权**：未在本地配对的非授权局域网设备请求一律直接拒绝，防止公用 Wi-Fi 环境下的非法监听。
- **透明开源**：核心源码完全透明公开，无任何私有商业闭源模块或后台统计追踪。

---

## 🛠️ 开发者指南 (从源码构建)

如果您希望对 PhoneMic 进行二次开发、扩展功能或自行从源码打包，请参考以下指引：

### 1. Mac 端环境初始化

```bash
# 克隆工程
git clone https://github.com/Oneideals/PhoneMic.git
cd PhoneMic

# 配置虚拟环境与依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 安装系统依赖
brew install blackhole-2ch ffmpeg switchaudio-osx

# 源码模式启动调试
python PhoneMicMenu.py
```

### 2. 安卓端源码编译

安卓客户端基于原生 Android SDK 编写（无需安装复杂第三方 NDK）：

```bash
cd android
./gradlew assembleDebug
# 构建产物位于 android/app/build/outputs/apk/debug/app-debug.apk
```
*亦可使用 Android Studio 直接打开 `android/` 目录进行真机运行与调试。*

### 3. 运行自动化测试套件

工程包含 26 项单元测试与集成测试，覆盖网络协议边界、RFC 3550 抖动算法、LNP 沙箱防御与 BlackHole 音频路由：

```bash
# 运行全部测试
pytest
```

---

## 📄 开源许可证

本项目基于 [MIT 许可证](LICENSE) 发布。  
特别致谢开源项目 [BlackHole](https://github.com/ExistentialAudio/BlackHole)、[FFmpeg](https://ffmpeg.org)、[RNNoise](https://github.com/xiph/rnnoise) 以及社区先驱 [MicYou](https://github.com/LanRhyme/MicYou) 提供的灵感与贡献。
