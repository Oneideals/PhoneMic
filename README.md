# PhoneMic — 把安卓手机变成 Mac 的专业麦克风

用一部安卓手机 + 一台 Mac，搭建一条**低延迟、可自愈、可存档**的无线麦克风链路，
专为语音输入（微信输入法语音转文字等 ASR 场景）优化信号质量。

## 架构

```
手机（PhoneMic App：AudioRecord 采集 → 增益/降噪 → HTTP WAV 流）
        │  局域网 Wi-Fi（mDNS 自动发现，端口漂移免疫）
        ▼
Mac（phonemic.py 引擎：拉流 → 降噪 afftdn → 增益 → 写入 BlackHole 虚拟声卡）
        │
        ▼
任意 Mac 应用把输入设备选成 "BlackHole 2ch"（如微信输入法语音输入）
```

## 能力清单

| 能力 | 说明 |
|---|---|
| 自动发现 | 手机端 mDNS 广播 + Mac 端三级回退（发现 → 缓存地址 → 端口扫描），换 IP/端口免疫 |
| 自愈重连 | 断流自动重连、进程异常自动拉起、单实例锁防重复 |
| 双端增益 | 手机端 0~+18dB（带削波保护）/ Mac 端 0~12dB，文件热更新即时生效 |
| 双层降噪 | 手机端系统 NoiseSuppressor + Mac 端 ffmpeg afftdn 稳态噪声过滤（实测风扇底噪 -30dB） |
| 磁贴控制 | Android 下拉快捷磁贴一键启停（免解锁） |
| 菜单栏管理 | Mac 菜单栏图标：绿点=录音存档中 / 白点=连通 / 空心环=寻找手机，含电平诊断与开机关停 |
| 录音存档 | 原始音频 WAV（自动转 FLAC）+ 电平/欠载/削波指标时间线 CSV，供后期比对分析 |
| 全量埋点 | 电平诊断、削波计数、连接日志，配合录音可定位每一次识别异常 |

## 目录结构

```
PhoneMic/
├── phonemic.py            # Mac 端引擎（拉流/降噪/增益/写 BlackHole/录音）
├── PhoneMicMenu.py        # Mac 端菜单栏应用（rumps，状态图标 + 管理）
├── PhoneMic.command       # 双击启动器
└── android/               # 安卓端 App（纯 Java + Material 3，零第三方运行时依赖）
    └── app/src/main/java/com/jerry/phonemic/
        ├── MainActivity.java   # Material You 动态取色界面
        ├── MicService.java     # 前台服务：采集 + 增益 + NoiseSuppressor + HTTP 流
        └── MicTileService.java # 下拉快捷磁贴
```

## 快速开始

**手机端**：安装 `android/` 构建出的 APK → 启动服务（或下拉磁贴）
**Mac 端**：双击 `PhoneMic.command` → 菜单栏出现状态圆点
**使用**：任意应用把麦克风选成 `BlackHole 2ch`

## 依赖

- Mac：Python 3.10+（venv：sounddevice / numpy / zeroconf / rumps）、BlackHole、ffmpeg（可选：降噪与转码）
- Android：API 26+（Android 8.0+），无需 Google Play

## 隐私声明

录音存档默认关闭；开启后所有数据仅保存在本机 `recordings/` 目录（已 gitignore），不会上传任何服务器。
