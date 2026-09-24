# Slay the Spire 2 Ironclad Agent

一个面向《杀戮尖塔 2》铁甲战士的 MCP 自动化与监控项目。它把游戏状态、合法动作、策略分流和动作校验放在本机控制器中；KEV 负责战斗战术，GPT-6 Luna 负责路线、奖励和商店等策略选择。

项目不会捆绑游戏文件、Steam 存档、Mod 二进制、模型权重、运行日志或 API 凭据。默认关闭游戏动作，默认示例不含 API Key。游戏数据以正在运行的游戏和 Mod 返回值为准。

## 状态与兼容性

- 当前维护目标是 Steam Main `v0.107.1`。实际版本从安装目录的 `release_info.json` 读取；版本不匹配时应暂停并重新核验。
- 游戏端需要 STS2MCP Mod 提供的本机 REST 桥；Python MCP 侧车本身不能读取游戏。
- 所选 STS2MCP 上游提交及本项目补丁记录在 [桥接兼容说明](docs/local-mod-patches.md)。第三方仓库不会复制进本仓库；可用脚本按固定提交下载并应用补丁。
- 当前回合估算器只模拟受支持的精确卡牌效果。未知目标、意图、状态或卡牌效果需要保留不确定性；它不是完整战斗模拟器。

## 安装

需要 Windows、Python 3.11+、`uv`、Git，以及已安装的游戏和兼容的游戏端 Mod。若要编译 Mod，还需 .NET SDK 和本机游戏程序集。

```powershell
git clone https://github.com/Mentat-Uran/sts2-ironclad-agent.git
Set-Location .\sts2-ironclad-agent
uv sync
Copy-Item config.example.toml config.toml
```

在 `config.toml` 中填写本机游戏、KEV 和 OpenAI 兼容 Luna 服务地址。将 `STS2_AGENT_CONFIG` 指向本地 `config.toml`。该文件已被 Git 忽略。

先运行不会提交游戏动作的演示和检查：

```powershell
uv run sts2-agent mock-demo
uv run sts2-agent preflight --config config.toml
```

`preflight` 会检查服务和版本；只有桥接服务报告兼容的被动读取协议时，控制器才读取游戏状态。要在 Codex 中使用 MCP，可将 `scripts/run-agent.ps1` 配为项目 MCP stdio 命令。示例配置应使用你自己的工作区路径，不要提交个人 `.codex` 配置。

## KEV 与 Luna

- KEV 使用结构化判断接口 `/v1/systemone`，只可从当前合法动作候选中选择；不是聊天补全文本模型。
- Luna 使用 OpenAI Chat Completions 兼容接口。`base_url` 应包含一次 `/v1`，客户端追加 `/chat/completions`。模型 ID 在配置中指定。
- 将 Luna 凭据放在启动进程的环境变量 `STS2_LUNA_API_KEY` 中，或配置你自己的 SSH 密钥文件来源。SSH 方式只在你明确设置 `STS2_LUNA_KEY_SSH_TARGET` 与 `STS2_LUNA_KEY_REMOTE_PATH` 后启用；密钥只传给子进程，不写入项目文件或日志。
- 没有 Luna 凭据时，Luna 专属策略决策会安全暂停；不会自动换成其它模型。KEV 和 Luna 的上下文相互隔离。

项目样例默认使用本机回环地址。服务地址、模型可用性、认证方式和模型 ID 都需要由使用者按自己的服务配置。

## 只读监控面板

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-agent.ps1 -Mode monitor
```

打开 `http://127.0.0.1:8765`。面板只绑定回环地址，显示当前游戏状态、合法动作、KEV/Luna 决策摘要和本机日志。日志可能包含牌组、候选项和游戏决策；默认只保存在 `runtime/`，该目录已从公开仓库排除。不要公开分享未经检查的运行日志。

## 游戏 Mod 桥接

先按 [桥接兼容说明](docs/local-mod-patches.md) 下载固定上游提交并应用补丁：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-sts2mcp.ps1
& .\vendor\STS2MCP\build.ps1 -GameDir $env:STS2_GAME_DIR
```

安装/更新脚本要求显式提供 `-GameDir`，且只更新已识别的 STS2MCP 文件；它不会改动 Steam 存档或其它 Mod。编译、安装前请关闭游戏，并确认本机分支与版本兼容。

## 实时游戏动作

仓库及样例配置保持 `game_actions_enabled = false`。自动游玩会改变游戏存档；只有在检查好当前配置、存档和合法动作后，才显式启用动作门并使用 `scripts/autoplay_mcp.py`。不要用公开日志或模型输出绕过当前状态和合法动作校验。动作 POST 超时后控制器只重读状态，不重复提交。

## 项目结构

- `src/sts2_agent/`：状态归一化、合法动作、控制器、MCP 服务、KEV/Luna/mock provider 与只读面板。
- `.agents/skills/sts2-ironclad-agent/`：铁甲战士通用 Codex Skill 和资料来源。
- `.agents/skills/sts2-ironclad-luna/`：仅供 Luna 使用的策略 Skill。
- `docs/architecture.md`：provider 分工、上下文边界与动作校验。
- `docs/sources-and-versions.md`：版本依据和中英文攻略来源。
- `docs/known-issues.md`：已知限制。
- `patches/`：针对固定 STS2MCP 上游提交的可审阅补丁。

## 许可证

本项目源代码采用 MIT 许可证。STS2MCP 补丁和其它第三方材料按其各自许可证使用；见 [第三方声明](THIRD_PARTY_NOTICES.md)。《杀戮尖塔 2》及其名称、图像和游戏内容归其权利人所有。本项目是非官方社区工具，与 Mega Crit 无隶属关系。
