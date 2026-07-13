# Token Floating Monitor

一个轻量级 Windows Token 悬浮窗，用 Python/Tk 写成，不需要额外 Python 依赖。它可以在桌面上显示当前活跃账号、并发、今日请求量、Token、成本、账号额度窗口，以及更细的用量统计面板。

> 截图使用脱敏演示数据生成，仅用于展示界面结构和功能。

## 界面预览

### 账号

显示当前活跃账号、最近请求、今日统计、Token 趋势和账号用量排行。支持 `今日 / 近5小时 / 近7天 / 周期` 切换。

![账号页](assets/screenshots/accounts.png)

### 用量统计

提供 `24h / 7d / 30d / All` 视图，包含 Token Chips、缓存命中率、Activity 热力图、Top Models 和 Provider 成本排行。

![用量统计页](assets/screenshots/usage-stats.png)

## 主要功能

- 桌面悬浮窗：支持置顶、拖动、缩放、刷新和关闭。
- 两个中文页签：`账号`、`用量统计`。
- 活跃账号与并发：显示当前正在使用的账号，以及总并发/账号并发。
- 账号排行：按今日、近 5 小时、近 7 天、周期窗口查看账号用量。
- 额度窗口：展示 5h、7d、cycle 的剩余百分比、已用比例、重置时间、无额度和 stale 状态。
- 用量统计：展示请求数、Token、成本、input/cache/output 构成、缓存命中率、Top Models 和 Provider 成本。
- Activity 热力图：支持 24h、7d、30d、All time，不同强度颜色表示用量高低，鼠标悬停可查看具体值。
- 本地历史：记录每日请求、Token 和成本快照，用于趋势和历史统计。
- 去重逻辑：保留 Codex fork replay 去重和 Sub2API mirror 扣除；Cockpit API 服务模式以去重后的
  原始请求为权威总量，`api-service-local` / `codex_local_access_runtime` 仅合并为一个展示项，不再重复扣除。

## 运行要求

- Windows
- Python 3.10+
- Tkinter，Windows 官方 Python 通常自带

项目不需要安装额外 Python 包。

## 快速开始

自动模式会先检测当前 Codex endpoint。只有当前账号指向 Sub2API 地址时，才读取 Sub2API 管理端数据；切回官方账号或其他 API 时，只读取本地客户端日志：

```powershell
.\start-monitor.ps1
```

只读取本地客户端日志：

```powershell
.\start-local-codex.ps1
```

也可以直接运行：

```powershell
python .\monitor.py
```

## 本地独立监控

如果希望完全不碰 Sub2API，可以强制本地独立模式。这个模式只读取本机 Codex/Claude 与 Antigravity Cockpit 本地日志，不请求 Sub2API 管理接口，也不使用 Sub2API 的最近请求、账号统计或并发数据。

```env
TOKEN_MONITOR_MODE=local-codex
```

如果 `.env` 里写着 `SUB2API_MONITOR_MODE=auto`，程序会按“当前 endpoint 门禁”处理：当前 Codex 指向 Sub2API 才读 Sub2API，否则走本地日志。

## Sub2API 兼容模式

显式设置为 `sub2api` 时，悬浮窗会强制请求 Sub2API 管理端接口，适合你确认当前环境就是 Sub2API 时使用：

```env
SUB2API_MONITOR_MODE=sub2api
SUB2API_BASE_URL=http://127.0.0.1:8080
SUB2API_ADMIN_EMAIL=admin@sub2api.local
SUB2API_ADMIN_PASSWORD=your-password
```

如果你的 Sub2API 有多个本地访问地址，可以配置匹配地址：

```env
SUB2API_MATCH_BASE_URLS=http://127.0.0.1:8080,http://localhost:8080
```

## 本地客户端日志模式

本地模式会扫描本机客户端日志并生成 `client_usage_today.json` 作为当天统计缓存。默认扫描路径包括：

- `%USERPROFILE%\.codex\sessions`
- `%USERPROFILE%\.claude\projects`

常用配置：

```env
SUB2API_MONITOR_MODE=local-codex
CLIENT_USAGE_CODEX_DEFAULT_MODEL=gpt-5.5
CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS=2000000
CLIENT_USAGE_CODEX_DESKTOP_LOG_ROOT=
CLIENT_USAGE_MODEL_PRICE_CACHE_SECONDS=86400
CLIENT_USAGE_OFFLINE_BACKFILL_MAX_DAYS=31
SUB2API_CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS=90
SUB2API_INCLUDE_LOCAL_USAGE=false
SUB2API_MONITOR_USAGE_SOURCE=auto
```

`CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS` 用来过滤异常大的单次 token 事件。

悬浮窗关闭期间，Codex/Claude 仍会独立写入本地日志。重新打开后，导出器会先完整重建
当天统计，再依据历史文件中的最后成功观测时间补录已经结束的日期。默认最多回看 31 天，
可通过 `CLIENT_USAGE_OFFLINE_BACKFILL_MAX_DAYS` 调整；设为 `0` 可关闭跨天补录。补录只复用
现有日志解析、去重、账号归属和计费规则，并对已有历史总量保留高水位，不会因一次日志暂缺
把旧数据降下来。导出失败或超时时，界面会明确提示正在显示上次缓存；如果今日数据已经写入、
只是历史补录未完成，则今日统计仍会正常更新。

遇到本地价格表未收录的新模型时，导出器会从结构化在线价格源查询并写入
`client_usage_model_prices.json`。缓存默认有效 24 小时；网络不可用或在线源未收录该模型时，
才会使用本地模型家族价格回退。可通过 `CLIENT_USAGE_MODEL_PRICE_URL` 替换价格源。

成本估算会优先使用在线价格表中的完整 Token 规则：标准、Priority、Flex、Batch、
缓存读取、缓存写入和输出价格。超过 272K 输入上下文时仍沿用当前 tier 的普通价格，
不应用在线价格表中的长上下文加价字段。日志包含 service tier 时按实际 tier 计费；日志
未提供 tier 或缓存写入明细时，只按能够观测到的字段估算，不补造缺失用量。

## 统计来源

`SUB2API_MONITOR_USAGE_SOURCE` 支持：

- `auto`：自动检测当前 Codex endpoint，只有匹配 Sub2API 地址时才使用 Sub2API 数据。
- `sub2api`：只使用 Sub2API 服务端统计。
- `local`：只使用本地客户端日志。
- `both`：同时展示 Sub2API 服务端统计和本地日志，适合对账，但可能重复计算。

默认建议使用 `auto` 或本地独立模式。`auto` 不会盲目依赖 Sub2API；只有当前 Codex endpoint 匹配 `SUB2API_BASE_URL` 或 `SUB2API_MATCH_BASE_URLS` 时，才会使用 Sub2API 服务端统计。

## 隐私说明

- `.env`、本地配置、当天统计缓存、历史统计缓存和归因 ledger 默认都在 `.gitignore` 中，不会提交到 Git。
- 本地模式只读取你电脑上的日志文件，不会主动上传到第三方。
- Sub2API 模式只请求你配置的 `SUB2API_BASE_URL`。
- 仓库中的截图使用脱敏演示数据，不包含真实账号或真实用量。

## 文件说明

- `monitor.py`：悬浮窗 UI、Sub2API 读取、本地统计整合和页面绘制。
- `client_usage_export.py`：本地客户端 JSONL 用量扫描器。
- `start-monitor.ps1`：自动模式启动脚本。
- `start-local-codex.ps1`：本地日志模式启动脚本。
- `run-monitor.cmd`：CMD 启动脚本。
- `run-client-usage-export.cmd`：单独导出本地用量 JSON。

## 验证

```powershell
python -m py_compile monitor.py client_usage_export.py
python client_usage_export.py --output client_usage_today.json
```
