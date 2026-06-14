# GitHub Tool — KiraAI 插件

curl 直调 GitHub REST API，零 MCP 桥接，WebUI 配置 token。  
内置 **Auto-Watch 后台监控**，定时检查 PR/Issue 动态。
支持全量搜索模式，自动覆盖所有仓库。

## 功能

| 工具 | 说明 |
|------|------|
| `github_search` | 搜仓库/代码/Issues/用户 |
| `github_get` | 读文件内容、Issue 详情、PR 详情、PR 文件列表、状态、评论、Review |
| `github_list` | 列 commits / issues / pull requests |
| `github_create` | 创建仓库/文件/Issue/PR/分支/Review/Star |
| `github_update` | 更新 Issue（标题/正文/状态/标签/指派人/里程碑）、更新 PR 分支 |
| `github_mutation` | 批量文件操作、发 Issue 评论、合并 PR |
| `github_fork` | Fork 仓库到个人或组织 |

## Auto-Watch 后台监控（v2.0）

自动检查你提交的 PR 是否收到 review 意见、assign 给你的 Issue 是否有新回复。

### 工作流程

1. 按设定的间隔或固定时间扫描
2. **搜索模式（推荐）**：通过 Search API 全量扫描所有你提的 PR 和 Issue，无需配置仓库列表
3. **仓库模式**：按配置的仓库列表逐个扫描
4. 发现有新动态：
   - **require_confirm=true（默认）** → 发送摘要到指定会话，等主人确认
   - **auto_fix=true + require_confirm=false** → 自动分析意见并修改代码推送新 commit

### 配置项一览（WebUI 插件设置页）

| 配置 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `watch_enabled` | switch | false | 总开关 |
| `watch_search_mode` | switch | false | 全量搜索模式：直接搜所有我提的PR/Issue，不依赖仓库列表 |
| `watch_interval_type` | enum | interval | interval=固定间隔 / fixed_time=时间点模式，下拉选择 |
| `watch_interval_minutes` | integer | 60 | 固定间隔模式：检查间隔（分钟），最小5分钟 |
| `watch_fixed_cron` | string | "0 9 * * *" | 固定时间点模式：cron表达式 |
| `watch_own_repos` | switch | true | 仓库模式：自动监控所有自有仓库 |
| `watch_repos` | list | [] | 仓库模式：额外监控仓库列表（每行一个 owner/repo） |
| `watch_issues` | switch | true | 同时监控 Issue 回复 |
| `watch_auto_fix` | switch | false | 收到 review 后自动修改代码 |
| `watch_require_confirm` | switch | true | auto_fix开启时：操作前是否需要确认 |
| `watch_notify_target` | list | [] | 通知目标会话，每行一个（qq:dm:QQ号 / qq:gm:群号） |

## 安装

1. 将 `github-tool` 文件夹放入 KiraAI 的 `data/plugins/` 目录
2. 在 WebUI 插件设置中填写 `GitHub Token`（需 `repo` 权限）
3. 如需启用后台监控，在配置页打开 `watch_enabled` 并设置通知目标
4. 重启 KiraAI，自动加载

## Token 配置

- 在 GitHub 生成 Personal Access Token：`Settings → Developer settings → Personal access tokens → Tokens (classic)`
- 勾选 `repo` 权限（完整 API 访问）
- 复制 token 到 WebUI 插件页的 `GitHub Token` 输入框

## 工作原理

- 所有 API 调用走 `curl`，无需 `PyGithub`、`httpx` 等第三方库
- 仅依赖 Python 标准库 + 系统自带 `curl`
- 自动识别运行平台：Windows 使用 `curl.exe`，Linux/macOS 使用 `curl`
- 输出精简为 token 友好的格式，避免 LLM 上下文被刷爆
- 后台监控基于 asyncio 协程，不阻塞主线程

## 注意事项

- 默认分支参数为 `main`，部分仓库（如 [Alife](https://github.com/BDFFZI/Alife) —— 我的老家，推荐大家看看）使用 `master`，调用时需指定 `b: "master"`
- 搜索 query 中的空格/特殊字符自动 URL 编码
- 跨平台兼容：Windows / Linux / macOS 均可运行
- stdout 使用 UTF-8 解码，避免 GBK 乱码
- Auto-Watch 需要 token 有 `repo` 权限才能访问私有仓库

## 许可证

AGPL-3.0

---
⭐ 觉得好用？不妨试试用本插件的点赞功能给本项目点个星 —— 自己给自己点一颗星，GitHub 史上最卷的 Star 获取方式。