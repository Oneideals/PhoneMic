# 贡献指南

感谢你对 PhoneMic 感兴趣！无论是报 Bug、提建议还是提交代码，都欢迎。

## 提 Issue 前

- 先搜索是否已存在相同或相关的 Issue。
- Bug 报告请尽量附上环境信息与日志片段（见下）。

## 开发环境

与 README 的「安装」一致：

```bash
git clone https://github.com/Oneideals/PhoneMic.git
cd PhoneMic
python3 -m venv .venv && source .venv/bin/activate
pip install sounddevice numpy zeroconf rumps
brew install blackhole-2ch ffmpeg
```

运行 Mac 端集成测试（需本机已装 BlackHole）：

```bash
python tests/test_mac.py
```

## 提交规范

本仓库使用**结构化提交信息**（由提交钩子校验），历史 commit 即为范例，核心分段：

1. 标题：`<type>(<scope>): <简短描述>`
2. `Agent Tool` / `Timestamp` 元数据
3. `## 🎯 核心问题诊断与技术复盘`：问题现象 / 根本原因 / 解决方案
4. `## 📢 官网/用户端发布日志`：可直接对外发布的变更摘要
5. `## 📦 变更模块与统计` + `### 📝 核心改动明细`

若不便遵循上述格式，也请至少使用 [Conventional Commits](https://www.conventionalcommits.org/)（如 `feat:`、`fix:`），
并在正文中说明动机与测试方式。

## Pull Request 流程

1. Fork 本仓库并基于 `main` 切出特性分支。
2. 保持改动聚焦、自测通过（`tests/test_mac.py` 全绿）。
3. 提交 PR，在模板中说明关联 Issue、改动与测试情况。
4. 等待 Review；讨论通过即合并。

## 代码风格

- 与现有代码保持一致即可；中文注释在本项目是被接受的。
- 涉及音频链路、单实例锁、发现逻辑时请格外谨慎，并补充测试或日志。

## 行为准则

参与本项目的所有人须遵守 [Code of Conduct](CODE_OF_CONDUCT.md)。
