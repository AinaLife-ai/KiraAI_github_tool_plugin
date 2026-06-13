# GitHub Tool — KiraAI 插件

curl 直调 GitHub REST API，零 MCP 桥接，WebUI 配置 token。

## 功能

| 工具 | 说明 |
|------|------|
| `github_search` | 搜仓库/代码/Issues/用户 |
| `github_get` | 读文件内容、Issue 详情、PR 详情、PR 文件列表、状态、评论、Review |
| `github_list` | 列 commits / issues / pull requests |
| `github_create` | 创建仓库/文件/Issue/PR/分支/Review |
| `github_update` | 更新 Issue（标题/正文/状态/标签/指派人/里程碑）、更新 PR 分支 |
| `github_mutation` | 批量文件操作、发 Issue 评论、合并 PR |
| `github_fork` | Fork 仓库到个人或组织 |

## 安装

1. 将 `github-tool` 文件夹放入 KiraAI 的 `data/plugins/` 目录
2. 在 WebUI 插件设置中填写 `GitHub Token`（需 `repo` 权限）
3. 重启 KiraAI，自动加载

## Token 配置

- 在 GitHub 生成 Personal Access Token：`Settings → Developer settings → Personal access tokens → Tokens (classic)`
- 勾选 `repo` 权限（完整 API 访问）
- 复制 token 到 WebUI 插件页的 `GitHub Token` 输入框

## 工作原理

- 所有 API 调用走 `curl.exe`，不依赖 `PyGithub`、`httpx` 等第三方库
- 只依赖 Python 标准库 + Windows 自带的 `curl.exe`
- 输出精简为 token 友好的格式，避免 LLM 上下文被刷爆

## 注意事项

- 默认分支参数为 `main`，部分仓库（如 Alife）使用 `master`，调用时需指定 `b: "master"`
- 搜索 query 中的空格/特殊字符自动 URL 编码
- Windows 环境下 subprocess 的 stdout 使用 UTF-8 解码，避免 GBK 乱码

## 许可证

AGPL-3.0
