# GitHub Tool — KiraAI 插件

curl 直调 GitHub REST API，零 MCP 桥接，WebUI 配置 token。

## 功能

| 工具 | 说明 |
|------|------|
| `github_search` | 搜仓库/代码/Issues/用户 |
| `github_get` | 读文件 SHA、Issue 详情、PR 详情、PR 文件列表、状态、评论、Review |
| `github_read_file` | 读取文件实际内容（Base64 解码），返回内容 + SHA，支持截断控制 |
| `github_list` | 列 commits / issues / pull requests |
| `github_create` | 创建仓库/文件/Issue/PR/分支/Review/Star/Release |
| `github_update` | 更新 Issue（标题/正文/状态/标签/指派人/里程碑）、更新 PR 分支 |
| `github_mutation` | 批量文件操作、发 Issue 评论、合并 PR |
| `github_fork` | Fork 仓库到个人或组织 |
| `github_check_token` | 检查 GitHub Token 是否已配置（可配置是否返回明文 Token） |

## 安装

1. 将 `github-tool` 文件夹放入 KiraAI 的 `data/plugins/` 目录
2. 在 WebUI 插件设置中填写 `GitHub Token`（需 `repo` 权限）
3. 可选调整 `文件内容最大返回字符数`（默认 5000）
4. 可选开启 `在 github_check_token 中返回明文 Token`（默认关闭，仅调试使用）
5. 重启 KiraAI，自动加载

## Token 配置

- 在 GitHub 生成 Personal Access Token：`Settings → Developer settings → Personal access tokens → Tokens (classic)`
- 勾选 `repo` 权限（完整 API 访问）
- 复制 token 到 WebUI 插件页的 `GitHub Token` 输入框
- 插件会自动识别当前登录的 GitHub 用户名，并在每轮对话中提示 AI token 已配置
- 如果开启 `expose_token_in_check`，AI 可以通过 `github_check_token` 获取明文 token（请谨慎使用）

## 工作原理

- 所有 API 调用走 `curl`，无需 `PyGithub`、`httpx` 等第三方库
- 仅依赖 Python 标准库 + 系统自带 `curl`
- 自动识别运行平台：Windows 使用 `curl.exe`，Linux/macOS 使用 `curl`
- 输出精简为 token 友好的格式，避免 LLM 上下文被刷爆
- 文件内容读取支持 Base64 解码和截断，可配置最大返回字符数

## 注意事项

- 默认分支参数为 `main`，部分仓库（如 [Alife](https://github.com/BDFFZI/Alife) —— 我的老家，推荐大家看看）使用 `master`，调用时需指定 `b: "master"`
- 搜索 query 中的空格/特殊字符自动 URL 编码
- 跨平台兼容：Windows / Linux / macOS 均可运行
- stdout 使用 UTF-8 解码，避免 GBK 乱码
- 修改文件前建议先用 `github_read_file` 查看内容并获取 SHA
- 开启明文 Token 返回功能后，AI 会在对话中输出 Token，请注意日志安全

## 许可证

AGPL-3.0

---

⭐ 觉得好用？不妨试试用本插件的点赞功能给本项目点个星 —— 自己给自己点一颗星，GitHub 史上最卷的 Star 获取方式。
