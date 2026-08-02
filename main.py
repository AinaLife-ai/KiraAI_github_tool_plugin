import asyncio
import base64
import json
import subprocess
import sys
from typing import Optional, Any
from urllib.parse import quote

from core.plugin import BasePlugin, on, Priority
from core.plugin.plugin_registry import register
from core.logging_manager import get_logger
from core.provider.llm_model import LLMRequest
from core.prompt_manager import Prompt

logger = get_logger("github-tool", "green")

_github_token: str = ""
_github_user: str = ""
_file_max_chars: int = 10000
_expose_token: bool = False
_default_branch_cache: dict = {}  # "owner/repo" -> default_branch
_allow_delete_repo: bool = False  # 删仓库开关，默认关

# Windows用curl.exe，其他平台（Linux/macOS）用curl
_CURL_CMD = "curl.exe" if sys.platform == "win32" else "curl"


def _curl(args: list, timeout: int = 60, data: Optional[str] = None) -> tuple:
    """同步执行 curl。data 通过 stdin（-d @-）传入，避免 Windows 命令行 32KB 上限与编码问题。"""
    cmd = [_CURL_CMD, "-s", "-H", "Accept: application/vnd.github+json"] + args
    try:
        r = subprocess.run(
            cmd,
            input=data.encode("utf-8") if data is not None else None,
            capture_output=True,
            timeout=timeout,
        )
        out = r.stdout.decode("utf-8", errors="replace").strip()
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return -1, '{"message": "curl timeout", "documentation_url": ""}'
    except Exception as e:
        return -1, json.dumps({"message": f"curl error: {e}", "documentation_url": ""})


async def _acurl(args: list, timeout: int = 60, data: Optional[str] = None) -> tuple:
    """异步包装，避免同步 subprocess 阻塞事件循环。"""
    return await asyncio.to_thread(_curl, args, timeout, data)


def _url(path: str) -> str:
    return f"https://api.github.com{path}"


def _auth() -> str:
    return f"Authorization: token {_github_token}"


def _check() -> str:
    if not _github_token:
        return "GitHub Token 未配置，请在 WebUI 插件设置中填写（已自动保存，无需每次提供）。"
    return ""


async def _req(method: str, path: str, body: Optional[dict] = None, timeout: int = 60) -> tuple:
    """统一的 GitHub REST 请求入口。body 经 stdin 传输。"""
    args = []
    if method != "GET":
        args += ["-X", method]
    args += ["-H", _auth()]
    data = None
    if body is not None:
        args += ["-H", "Content-Type: application/json", "-d", "@-"]
        data = json.dumps(body, ensure_ascii=False)
    return await _acurl(args + [_url(path)], timeout=timeout, data=data)


async def _default_branch(o: str, r: str) -> str:
    """获取仓库默认分支（带缓存），失败返回空串。"""
    key = f"{o}/{r}"
    if key in _default_branch_cache:
        return _default_branch_cache[key]
    rc, out = await _req("GET", f"/repos/{o}/{r}")
    if rc == 0:
        try:
            db = json.loads(out).get("default_branch", "")
            if db:
                _default_branch_cache[key] = db
                return db
        except json.JSONDecodeError:
            pass
    return ""


async def _resolve_branch(o: str, r: str, br: str) -> str:
    """显式分支优先；留空则取仓库默认分支。"""
    if br:
        return br
    return await _default_branch(o, r) or "main"


async def _get_file_sha(o: str, r: str, p: str, br: str) -> str:
    """获取已有文件的 sha（用于更新）；文件不存在或失败返回空串。"""
    rc, out = await _req("GET", f"/repos/{o}/{r}/contents/{quote(p)}?ref={quote(br)}")
    if rc == 0:
        try:
            d = json.loads(out)
            if isinstance(d, dict) and d.get("sha"):
                return d["sha"]
        except json.JSONDecodeError:
            pass
    return ""


def _fmt(raw: str) -> str:
    if not raw:
        return "No output"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]

    if isinstance(data, dict):
        if "message" in data and ("documentation_url" in data or "errors" in data):
            msg = f"Error: {data['message']}"
            errs = data.get("errors")
            if errs:
                # 422 Validation Failed 等场景的关键细节（如 "No commits between base and head"）
                msg += "\n详情: " + json.dumps(errs, ensure_ascii=False)[:400]
            return msg
        if "total_count" in data:
            items = data.get("items", [])
            lines = [f"Total: {data['total_count']}"]
            for i in items[:10]:
                n = i.get("full_name") or i.get("name") or i.get("login", "?")
                u = i.get("html_url") or ""
                lines.append(f"  {n}  {u}")
            if len(items) > 10:
                lines.append(f"  ... and {len(items) - 10} more")
            return "\n".join(lines)
        if isinstance(data.get("content"), dict) and "sha" in data["content"]:
            return f"sha: {data['content']['sha']}"
        if "commit" in data and "sha" in data:
            return f"sha: {data['sha']}  {data['commit'].get('message', '')[:60]}"
        for k in ["sha", "full_name", "name"]:
            if k in data:
                v = data[k]
                if k == "full_name":
                    return f"{v}  {data.get('html_url', '')}"
                if k == "sha":
                    return f"sha: {v}"
                if k == "name":
                    return f"{v}  {data.get('html_url', '')}"
        if "message" in data:
            # 纯 message 的成功响应（如 "Pull Request successfully merged"）
            return str(data["message"])[:200]
        if "id" in data:
            for k in ["title", "name", "login", "message"]:
                if k in data:
                    return str(data[k])[:200]
        return json.dumps(data, ensure_ascii=False)[:500]
    elif isinstance(data, list):
        lines = []
        for item in data[:20]:
            n = (item.get("full_name") or item.get("name") or item.get("filename")
                 or item.get("login"))
            if not n and "title" in item:
                n = f"#{item.get('number', '?')} {item['title']}" if "number" in item else item["title"]
            if not n and isinstance(item.get("commit"), dict):
                # commit 列表：显示短 sha + 首行提交信息
                msg1 = (item["commit"].get("message") or "").split("\n")[0][:60]
                n = f"{item.get('sha', '')[:8]} {msg1}"
            n = n or "?"
            u = item.get("html_url") or ""
            lines.append(f"  {n}  {u}")
        if len(data) > 20:
            lines.append(f"  ... and {len(data) - 20} more")
        return "\n".join(lines) if lines else "Empty list"
    return str(data)[:500]


def _fmt_file_content(raw: str, max_chars: int, offset: int = 0) -> str:
    if not raw:
        return "No output"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]

    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)[:500]

    if "message" in data and ("documentation_url" in data or "errors" in data):
        return f"Error: {data['message']}"

    content = data.get("content")
    if not content:
        return "File content not found or empty"

    sha = data.get("sha", "unknown")
    encoding = data.get("encoding", "base64")
    name = data.get("name", "")
    path = data.get("path", "")
    size = data.get("size", 0)

    if encoding != "base64":
        return f"Unsupported encoding: {encoding}"

    try:
        decoded = base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception as e:
        return f"Failed to decode file content: {e}"

    total_chars = len(decoded)
    if offset >= total_chars:
        return (f"📄 文件: {path}\n"
                f"名称: {name}\n"
                f"SHA: {sha}\n"
                f"总字符数: {total_chars}\n"
                f"偏移量 {offset} 已超出文件结尾，无更多内容。")

    end_pos = min(offset + max_chars, total_chars)
    display_content = decoded[offset:end_pos]
    has_more = end_pos < total_chars
    read_chars = len(display_content)

    result = (f"📄 文件: {path}\n"
              f"名称: {name}\n"
              f"SHA: {sha}\n"
              f"总字符数: {total_chars}\n"
              f"本次读取: {offset} → {end_pos} (共 {read_chars} 字符)\n"
              f"还有更多: {'是' if has_more else '否'}\n"
              f"\n--- 内容开始 ---\n{display_content}\n--- 内容结束 ---")

    if has_more:
        result += f"\n\n💡 提示: 文件还有 {total_chars - end_pos} 字符未读。如需继续，请使用相同的参数并设置 offset={end_pos}。"

    return result


# ============================================================
# 自检工具
# ============================================================

@register.tool(
    name="github_check_token",
    description="Check if GitHub token is configured. Returns 'Token ready' if configured, otherwise error message. Use this if you are unsure about token status.",
    params={
        "type": "object",
        "properties": {},
        "required": []
    }
)
async def github_check_token(*args, **kw):
    if _github_token:
        msg = f"✅ GitHub Token 已配置，当前登录账号为 {_github_user or '未知'}。"
        if _expose_token:
            msg += f"\n\n⚠️ 【明文 Token】{_github_token}\n\n此 Token 仅用于调试，请勿分享或记录到对话日志中。"
        return msg
    else:
        return "❌ GitHub Token 未配置，请在 WebUI 插件设置中填写 Personal Access Token（需要 repo 权限）。"


# ============================================================
# 读取文件内容工具（支持分页）
# ============================================================

@register.tool(
    name="github_read_file",
    description=(
        "Read the content of a file from a GitHub repository. Decodes Base64 content and returns the actual text/code. "
        "Supports pagination via offset and limit. Use this when you need to see what's inside a file before modifying it. "
        "Leave b empty to use the repository's default branch (recommended). "
        "Example: first call with limit=5000, then if '还有更多' is '是', call again with offset=5000 (or the suggested offset) to read the next part."
    ),
    params={
        "type": "object",
        "properties": {
            "o": {"type": "string", "description": "Repository owner (username or organization)"},
            "r": {"type": "string", "description": "Repository name"},
            "p": {"type": "string", "description": "File path (e.g. src/main.py)"},
            "b": {"type": "string", "description": "Branch ref. Leave empty to auto-use the repository's default branch."},
            "limit": {"type": "integer", "description": "Max characters to return (default: configured in plugin settings)"},
            "offset": {"type": "integer", "description": "Character offset to start reading (for pagination, default: 0)"}
        },
        "required": ["o", "r", "p"]
    }
)
async def github_read_file(*args, **kw):
    err = _check()
    if err:
        return err

    o = kw.get("o", "")
    r = kw.get("r", "")
    p = kw.get("p", "")
    b = kw.get("b", "")
    limit = kw.get("limit", _file_max_chars)
    offset = kw.get("offset", 0)

    if not o or not r or not p:
        return "Missing required parameters: o (owner), r (repo), p (path)"

    if limit < 1:
        return "limit must be at least 1"
    if offset < 0:
        return "offset must be >= 0"

    b = await _resolve_branch(o, r, b)
    rc, out = await _req("GET", f"/repos/{o}/{r}/contents/{quote(p)}?ref={quote(b)}")
    if rc != 0:
        return f"curl failed with code {rc}"
    if '"No commit found for the ref"' in out:
        # 显式指定的分支不存在，回退到默认分支再试一次
        db = await _default_branch(o, r)
        if db and db != b:
            rc, out = await _req("GET", f"/repos/{o}/{r}/contents/{quote(p)}?ref={quote(db)}")
            if rc == 0 and '"No commit found for the ref"' not in out:
                res = _fmt_file_content(out, limit, offset)
                return f"⚠️ 分支 {b} 不存在，已自动改用默认分支 {db}。\n\n" + res
    return _fmt_file_content(out, limit, offset)


# ============================================================
# 其他工具
# ============================================================

@register.tool(
    name="github_search",
    description="Search GitHub (repositories, code, issues, users). Token already configured, no need to provide it.",
    params={
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Search query using GitHub search syntax"},
            "t": {"type": "string", "enum": ["repositories", "code", "issues", "users"], "description": "What to search for"},
            "n": {"type": "integer", "description": "Results per page (max 100)", "default": 10}
        },
        "required": ["t", "q"]
    }
)
async def github_search(*args, **kw):
    err = _check()
    if err:
        return err
    t = kw.get("t", "repositories")
    q = kw.get("q", "")
    n = min(kw.get("n", 10), 100)
    rc, out = await _req("GET", f"/search/{t}?q={quote(q)}&per_page={n}")
    return _fmt(out) if rc == 0 else f"curl failed with code {rc}"


@register.tool(
    name="github_get",
    description="Get metadata from GitHub: file SHA, issue details, pull request details, PR files, PR status, PR comments, PR reviews. For reading actual file content, use github_read_file instead. Note: writing files no longer requires fetching SHA manually — github_create/github_mutation handle it automatically.",
    params={
        "type": "object",
        "properties": {
            "t": {"type": "string", "enum": ["contents", "issue", "pull_request", "pull_request_files", "pull_request_status", "pull_request_comments", "pull_request_reviews"], "description": "Type of resource to get"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "p": {"type": "string", "description": "File path (required for contents)"},
            "i": {"type": "integer", "description": "Issue number"},
            "n": {"type": "integer", "description": "Pull request number"},
            "b": {"type": "string", "description": "Branch ref. Leave empty to auto-use the repository's default branch."}
        },
        "required": ["t", "o", "r"]
    }
)
async def github_get(*args, **kw):
    err = _check()
    if err:
        return err
    t = kw.get("t", "")
    o = kw.get("o", "")
    r = kw.get("r", "")
    p = kw.get("p", "")
    i = kw.get("i", 0)
    n = kw.get("n", 0)
    b = kw.get("b", "")

    if t == "contents":
        if not p:
            return "Missing required parameter: p (file path) for t=contents"
        b = await _resolve_branch(o, r, b)
        ep = f"/repos/{o}/{r}/contents/{quote(p)}?ref={quote(b)}"
    elif t == "pull_request_status":
        b = await _resolve_branch(o, r, b)
        ep = f"/repos/{o}/{r}/commits/{quote(b)}/status"
    else:
        ep_map = {
            "issue": f"/repos/{o}/{r}/issues/{i}",
            "pull_request": f"/repos/{o}/{r}/pulls/{n}",
            "pull_request_files": f"/repos/{o}/{r}/pulls/{n}/files",
            "pull_request_comments": f"/repos/{o}/{r}/pulls/{n}/comments",
            "pull_request_reviews": f"/repos/{o}/{r}/pulls/{n}/reviews",
        }
        ep = ep_map.get(t)
    if not ep:
        return f"Unknown target: {t}"
    rc, out = await _req("GET", ep)
    return _fmt(out) if rc == 0 else f"curl failed with code {rc}"


@register.tool(
    name="github_list",
    description="List commits, issues or pull requests from a GitHub repository.",
    params={
        "type": "object",
        "properties": {
            "t": {"type": "string", "enum": ["commits", "issues", "pull_requests"], "description": "What to list"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "s": {"type": "string", "enum": ["open", "closed", "all"], "description": "Filter by state (issues/PRs only)", "default": "open"},
            "b": {"type": "string", "description": "Branch (commits only). Leave empty to use the default branch."},
            "n": {"type": "integer", "description": "Results per page", "default": 20}
        },
        "required": ["t", "o", "r"]
    }
)
async def github_list(*args, **kw):
    err = _check()
    if err:
        return err
    t = kw.get("t", "")
    o = kw.get("o", "")
    r = kw.get("r", "")
    s = kw.get("s", "open")
    b = kw.get("b", "")
    n = min(kw.get("n", 20), 100)

    if t == "commits":
        ep = f"/repos/{o}/{r}/commits?per_page={n}"
        if b:
            ep += f"&sha={quote(b)}"
    else:
        ep_map = {
            "issues": f"/repos/{o}/{r}/issues?state={s}&per_page={n}",
            "pull_requests": f"/repos/{o}/{r}/pulls?state={s}&per_page={n}",
        }
        ep = ep_map.get(t)
    if not ep:
        return f"Unknown target: {t}"
    rc, out = await _req("GET", ep)
    return _fmt(out) if rc == 0 else f"curl failed with code {rc}"


@register.tool(
    name="github_create",
    description="Create GitHub resources: repository, file (create/update, SHA is handled automatically), issue, pull request, branch, pull request review, star, release — or delete file / repository / branch.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["repository", "file", "issue", "pull_request", "branch", "pull_request_review", "star", "unstar", "release", "delete_file", "delete_repository", "delete_branch", "delete_release", "delete_comment"], "description": "What type of resource to create or delete"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "nm": {"type": "string", "description": "Name (repo name, branch name, or release tag name)"},
            "p": {"type": "string", "description": "File path (for file action, e.g. README.md)"},
            "ct": {"type": "string", "description": "File content (for file action) or body text"},
            "msg": {"type": "string", "description": "Commit message (for file action)"},
            "br": {"type": "string", "description": "Branch name. Leave empty to auto-use the repository's default branch."},
            "ti": {"type": "string", "description": "Title (for issue/PR/release)"},
            "bd": {"type": "string", "description": "Body text (for issue/PR/release review)"},
            "hd": {"type": "string", "description": "Head branch (for PR, e.g. my-branch or MyUser:my-branch)"},
            "ba": {"type": "string", "description": "Base branch (for PR)"},
            "pn": {"type": "integer", "description": "PR number (for review)"},
            "desc": {"type": "string", "description": "Repository description"},
            "pv": {"type": "boolean", "description": "Private repository?", "default": True},
            "sh": {"type": "string", "description": "SHA of existing file (optional — auto-fetched when omitted)"},
            "dr": {"type": "boolean", "description": "Draft pull request?", "default": False},
            "ev": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"], "description": "Review event type"},
            "lb": {"type": "array", "items": {"type": "string"}, "description": "Labels to apply"},
            "as": {"type": "array", "items": {"type": "string"}, "description": "Users to assign"},
            "fb": {"type": "string", "description": "Source branch to fork from (for branch creation). Leave empty to use the default branch."},
            "target": {"type": "string", "description": "Commit SHA or branch name for the release (default: default branch)"},
            "rid": {"type": "integer", "description": "Release ID (for delete_release; optional if nm/tag is given)"},
            "cid": {"type": "integer", "description": "Comment ID (for delete_comment)"},
            "k": {"type": "string", "enum": ["issue", "review"], "description": "Comment kind (for delete_comment): issue comment or PR review comment", "default": "issue"},
            "draft": {"type": "boolean", "description": "Is draft release?", "default": False},
            "prerelease": {"type": "boolean", "description": "Is prerelease?", "default": False}
        },
        "required": ["act"]
    }
)
async def github_create(*args, **kw):
    err = _check()
    if err:
        return err
    act = kw.get("act", "")
    o = kw.get("o", "")
    r = kw.get("r", "")
    nm = kw.get("nm", "")
    # 兼容旧参数名 pp（历史上与其他工具的 p 不一致，导致 LLM 传 p 时路径为空 → 404）
    p = kw.get("p") or kw.get("pp") or ""
    ct = kw.get("ct", "")
    msg = kw.get("msg", "")
    br = kw.get("br", "")
    ti = kw.get("ti", "")
    bd = kw.get("bd", "")
    hd = kw.get("hd", "")
    ba = kw.get("ba", "")
    pn = kw.get("pn", 0)
    desc = kw.get("desc", "")
    pv = kw.get("pv", True)
    sh = kw.get("sh", "")
    dr = kw.get("dr", False)
    ev = kw.get("ev", "")
    lb = kw.get("lb", None)
    as_ = kw.get("as", None)
    fb = kw.get("fb", "")
    target = kw.get("target", "")
    rid = kw.get("rid", 0)
    cid = kw.get("cid", 0)
    k = kw.get("k", "issue")
    draft = kw.get("draft", False)
    prerelease = kw.get("prerelease", False)

    if act == "repository":
        d = {"name": nm, "description": desc, "private": pv}
        rc, out = await _req("POST", "/user/repos", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "file":
        if not o or not r or not p:
            return "act=file 缺少必填参数: o (owner), r (repo), p (文件路径，如 README.md)"
        br_eff = await _resolve_branch(o, r, br)
        # 未显式提供 sha 时自动获取：文件存在→更新，不存在→新建
        sha = sh or await _get_file_sha(o, r, p, br_eff)
        d = {"message": msg or f"Update {p}",
             "content": base64.b64encode(ct.encode()).decode(),
             "branch": br_eff}
        if sha:
            d["sha"] = sha
        rc, out = await _req("PUT", f"/repos/{o}/{r}/contents/{quote(p)}", d, timeout=120)
        if rc != 0:
            return f"curl failed with code {rc}"
        res = _fmt(out)
        action = "更新" if sha else "新建"
        return f"✅ {action}文件成功 ({o}/{r}:{br_eff}:{p})\n{res}" if not res.startswith("Error") else res

    elif act == "issue":
        d = {"title": ti}
        if bd:
            d["body"] = bd
        if lb:
            d["labels"] = lb
        if as_:
            d["assignees"] = as_
        rc, out = await _req("POST", f"/repos/{o}/{r}/issues", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request":
        if not ti or not hd or not ba:
            return "act=pull_request 缺少必填参数: ti (标题), hd (head 分支), ba (base 分支)。注意：head 分支必须至少领先 base 一个 commit，否则 GitHub 会拒绝创建。"
        d = {"title": ti, "head": hd, "base": ba}
        if bd:
            d["body"] = bd
        if dr:
            d["draft"] = True
        rc, out = await _req("POST", f"/repos/{o}/{r}/pulls", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "branch":
        if not nm:
            return "act=branch 缺少必填参数: nm (新分支名)"
        src = fb or br or await _default_branch(o, r)
        if not src:
            return "无法确定源分支，请用 fb 参数显式指定"
        rc2, ref_out = await _req("GET", f"/repos/{o}/{r}/git/refs/heads/{quote(src)}")
        if rc2 != 0:
            return f"Failed to get source branch ref: {ref_out[:200]}"
        try:
            ref_data = json.loads(ref_out)
            sha_val = ref_data["object"]["sha"]
        except (json.JSONDecodeError, KeyError):
            return f"源分支 {src} 不存在或解析失败: {ref_out[:200]}"
        d = {"ref": f"refs/heads/{nm}", "sha": sha_val}
        rc, out = await _req("POST", f"/repos/{o}/{r}/git/refs", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request_review":
        d = {"body": bd or "", "event": ev or "COMMENT"}
        rc, out = await _req("POST", f"/repos/{o}/{r}/pulls/{pn}/reviews", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "star":
        rc, out = await _req("PUT", f"/user/starred/{o}/{r}", {})
        if rc == 0 and '"message"' not in out:
            return f"⭐ Starred {o}/{r} successfully"
        return f"Star failed (code {rc}): {out[:200]}"

    elif act == "unstar":
        rc, out = await _req("DELETE", f"/user/starred/{o}/{r}")
        if rc == 0 and '"message"' not in out:
            return f"★ Unstarred {o}/{r} successfully"
        return f"Unstar failed (code {rc}): {out[:200]}"

    elif act == "release":
        if not nm:
            return "Missing required parameter: nm (tag name)"
        d = {"tag_name": nm, "name": ti or nm, "body": bd or "", "draft": draft, "prerelease": prerelease}
        if target:
            d["target_commitish"] = target
        rc, out = await _req("POST", f"/repos/{o}/{r}/releases", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "delete_file":
        if not o or not r or not p:
            return "act=delete_file 缺少必填参数: o (owner), r (repo), p (文件路径)"
        br_eff = await _resolve_branch(o, r, br)
        # GitHub 删文件必须带当前 sha，自动获取
        sha = sh or await _get_file_sha(o, r, p, br_eff)
        if not sha:
            return f"未找到文件 {p}（分支 {br_eff}），无法删除"
        d = {"message": msg or f"Delete {p}", "sha": sha, "branch": br_eff}
        rc, out = await _req("DELETE", f"/repos/{o}/{r}/contents/{quote(p)}", d)
        if rc != 0:
            return f"curl failed with code {rc}"
        res = _fmt(out)
        return f"✅ 文件已删除 ({o}/{r}:{br_eff}:{p})\n{res}" if not res.startswith("Error") else res

    elif act == "delete_repository":
        if not o or not r:
            return "act=delete_repository 缺少必填参数: o (owner), r (repo)"
        rc, out = await _req("DELETE", f"/repos/{o}/{r}")
        # 成功返回 204 空 body
        if rc == 0 and (not out or '"message"' not in out):
            return f"✅ 仓库 {o}/{r} 已删除（不可逆）"
        return f"删除仓库失败: {_fmt(out)}"

    elif act == "delete_repository":
        if not _allow_delete_repo:
            return "⚠️ 删除仓库功能已被插件开关禁用（默认关闭）。请在 WebUI 插件设置中开启「允许删除仓库」后再试。此操作不可逆，请谨慎开启。"
        if not o or not r:
            return "act=delete_repository 缺少必填参数: o (owner), r (repo)"
        rc, out = await _req("DELETE", f"/repos/{o}/{r}")
        # 成功返回 204 空 body
        if rc == 0 and (not out or '"message"' not in out):
            return f"✅ 仓库 {o}/{r} 已删除（不可逆）"
        return f"删除仓库失败: {_fmt(out)}"

    elif act == "delete_release":
        rel_id = rid
        if not rel_id and nm:
            # 支持按 tag 名定位 release
            rc2, out2 = await _req("GET", f"/repos/{o}/{r}/releases/tags/{quote(nm)}")
            try:
                rel_id = json.loads(out2).get("id", 0)
            except json.JSONDecodeError:
                rel_id = 0
        if not rel_id:
            return "act=delete_release 需要 rid（release ID）或 nm（tag 名，自动查 ID）"
        rc, out = await _req("DELETE", f"/repos/{o}/{r}/releases/{rel_id}")
        if rc == 0 and (not out or '"message"' not in out):
            return f"✅ Release 已删除（{o}/{r}，id={rel_id}）。注意：对应的 git tag 不会被删除。"
        return f"删除 Release 失败: {_fmt(out)}"

    elif act == "delete_comment":
        if not cid:
            return "act=delete_comment 缺少必填参数: cid（评论 ID，可从 github_get 的评论列表获取）"
        if k == "review":
            path = f"/repos/{o}/{r}/pulls/comments/{cid}"
        else:
            path = f"/repos/{o}/{r}/issues/comments/{cid}"
        rc, out = await _req("DELETE", path)
        if rc == 0 and (not out or '"message"' not in out):
            return f"✅ {'PR review' if k == 'review' else 'Issue'}评论 {cid} 已删除"
        return f"删除评论失败: {_fmt(out)}"

    elif act == "delete_branch":
        if not o or not r or not nm:
            return "Missing required parameters: o (owner), r (repo), nm (branch name)"
        rc, out = await _req("DELETE", f"/repos/{o}/{r}/git/refs/heads/{quote(nm)}")
        if rc == 0 and '"message"' not in out:
            return f"✅ Branch '{nm}' deleted successfully from {o}/{r}"
        else:
            return f"Delete failed (code {rc}): {out[:200]}"

    return f"Unknown action: {act}"


@register.tool(
    name="github_update",
    description="Update a GitHub issue (title, body, state, labels, assignees, milestone), edit/close/reopen a pull request, or update a pull request branch.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["issue", "pull_request", "pull_request_branch"], "description": "What to update"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "in": {"type": "integer", "description": "Issue number"},
            "pn": {"type": "integer", "description": "PR number"},
            "ti": {"type": "string", "description": "New title"},
            "bd": {"type": "string", "description": "New body"},
            "st": {"type": "string", "enum": ["open", "closed"], "description": "New state (for issue/PR)"},
            "lb": {"type": "array", "items": {"type": "string"}, "description": "New labels"},
            "as": {"type": "array", "items": {"type": "string"}, "description": "New assignees"},
            "ms": {"type": "integer", "description": "Milestone number"},
            "sh": {"type": "string", "description": "Expected head SHA (for PR branch update)"}
        },
        "required": ["act", "o", "r"]
    }
)
async def github_update(*args, **kw):
    err = _check()
    if err:
        return err
    act = kw.get("act", "")
    o = kw.get("o", "")
    r = kw.get("r", "")
    inn = kw.get("in", 0)
    pn = kw.get("pn", 0)
    ti = kw.get("ti", "")
    bd = kw.get("bd", "")
    st = kw.get("st", "")
    lb = kw.get("lb", None)
    as_ = kw.get("as", None)
    ms = kw.get("ms", 0)
    sh = kw.get("sh", "")

    if act == "issue":
        d = {}
        if ti:
            d["title"] = ti
        if bd:
            d["body"] = bd
        if st:
            d["state"] = st
        if lb is not None:
            d["labels"] = lb
        if as_ is not None:
            d["assignees"] = as_
        if ms:
            d["milestone"] = ms
        if not d:
            return "No fields to update"
        rc, out = await _req("PATCH", f"/repos/{o}/{r}/issues/{inn}", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request":
        if not pn:
            return "act=pull_request 缺少必填参数: pn (PR 编号)"
        d = {}
        if ti:
            d["title"] = ti
        if bd:
            d["body"] = bd
        if st:
            d["state"] = st
        if not d:
            return "No fields to update"
        rc, out = await _req("PATCH", f"/repos/{o}/{r}/pulls/{pn}", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request_branch":
        d = {}
        if sh:
            d["expected_head_sha"] = sh
        rc, out = await _req("PUT", f"/repos/{o}/{r}/pulls/{pn}/update-branch", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    return f"Unknown action: {act}"


@register.tool(
    name="github_mutation",
    description="Perform batched mutations: create/update multiple files in one call (SHA handled automatically, each file's real success/failure is reported), add issue comment, or merge a pull request.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["files", "issue_comment", "pull_request"], "description": "Mutation type"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "br": {"type": "string", "description": "Branch. Leave empty to auto-use the repository's default branch."},
            "msg": {"type": "string", "description": "Commit message (for files)"},
            "fs": {"type": "array", "items": {"type": "object", "properties": {"p": {"type": "string", "description": "File path"}, "c": {"type": "string", "description": "File content"}}}, "description": "Files to create/update in one batch"},
            "in": {"type": "integer", "description": "Issue number (for comment)"},
            "bd": {"type": "string", "description": "Comment body or merge message"},
            "pn": {"type": "integer", "description": "PR number (for merge)"},
            "mm": {"type": "string", "enum": ["merge", "squash", "rebase"], "description": "Merge method", "default": "merge"},
            "ct": {"type": "string", "description": "Merge commit title"}
        },
        "required": ["act", "o", "r"]
    }
)
async def github_mutation(*args, **kw):
    err = _check()
    if err:
        return err
    act = kw.get("act", "")
    o = kw.get("o", "")
    r = kw.get("r", "")
    br = kw.get("br", "")
    msg = kw.get("msg", "")
    fs = kw.get("fs", None)
    inn = kw.get("in", 0)
    bd = kw.get("bd", "")
    pn = kw.get("pn", 0)
    mm = kw.get("mm", "merge")
    ct = kw.get("ct", "")

    if act == "files":
        if not fs:
            return "No files specified"
        br_eff = await _resolve_branch(o, r, br)
        results = []
        for f in fs:
            fp = f.get("p", "")
            fc = f.get("c", "")
            if not fp:
                results.append("  (unknown): ❌ 缺少文件路径 p")
                continue
            # 自动获取已有文件 sha：存在→更新，不存在→新建
            sha = await _get_file_sha(o, r, fp, br_eff)
            d = {"message": msg or f"Update {fp}",
                 "content": base64.b64encode(fc.encode()).decode(),
                 "branch": br_eff}
            if sha:
                d["sha"] = sha
            rc, out = await _req("PUT", f"/repos/{o}/{r}/contents/{quote(fp)}", d, timeout=120)
            od = None
            try:
                od = json.loads(out)
            except Exception:
                pass
            if isinstance(od, dict) and od.get("content", {}).get("sha"):
                action = "更新" if sha else "新建"
                results.append(f"  {fp}: ✅ {action}成功 sha={od['content']['sha'][:8]}")
            elif isinstance(od, dict) and od.get("message"):
                detail = str(od["message"])
                if od.get("errors"):
                    detail += " — " + json.dumps(od["errors"], ensure_ascii=False)[:200]
                results.append(f"  {fp}: ❌ {detail}")
            else:
                results.append(f"  {fp}: ❌ {out[:120]}")
        return f"Files (branch: {br_eff}):\n" + "\n".join(results)

    elif act == "issue_comment":
        d = {"body": bd}
        rc, out = await _req("POST", f"/repos/{o}/{r}/issues/{inn}/comments", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request":
        d = {"merge_method": mm}
        if ct:
            d["commit_title"] = ct
        if bd:
            d["commit_message"] = bd
        rc, out = await _req("PUT", f"/repos/{o}/{r}/pulls/{pn}/merge", d)
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    return f"Unknown action: {act}"


@register.tool(
    name="github_fork",
    description="Fork a GitHub repository to your personal account or a specified organization. Note: GitHub forks asynchronously — the new repo may take a few seconds to be fully ready; if a subsequent write fails right after forking, wait a moment and retry.",
    params={
        "type": "object",
        "properties": {
            "o": {"type": "string", "description": "Source repository owner"},
            "r": {"type": "string", "description": "Source repository name"},
            "org": {"type": "string", "description": "Target organization (optional, forks to personal account by default)"}
        },
        "required": ["o", "r"]
    }
)
async def github_fork(*args, **kw):
    err = _check()
    if err:
        return err
    o = kw.get("o", "")
    r = kw.get("r", "")
    org = kw.get("org", "")
    d = {}
    if org:
        d["organization"] = org
    rc, out = await _req("POST", f"/repos/{o}/{r}/forks", d)
    if rc != 0:
        return f"curl failed with code {rc}"
    res = _fmt(out)
    if not res.startswith("Error"):
        res += "\n\n💡 Fork 为异步操作，仓库可能需要几秒才完全就绪；若紧接着写文件失败，请稍后重试。"
    return res


class GitHubToolPlugin(BasePlugin):
    async def initialize(self):
        global _github_token, _github_user, _file_max_chars, _expose_token, _allow_delete_repo
        _github_token = self.plugin_cfg.get("github_token", "")
        _file_max_chars = int(self.plugin_cfg.get("file_content_max_chars", 10000))
        _expose_token = bool(self.plugin_cfg.get("expose_token_in_check", False))
        _allow_delete_repo = bool(self.plugin_cfg.get("enable_delete_repository", False))
        if _allow_delete_repo:
            logger.warning("⚠️ enable_delete_repository 已开启，AI 可调用 delete_repository 删除仓库（不可逆），请注意风险！")

        if _github_token:
            rc, out = await _req("GET", "/user")
            if rc == 0:
                try:
                    data = json.loads(out)
                    if "login" in data:
                        _github_user = data["login"]
                        logger.info(f"GitHub Tool ready, logged in as {_github_user}")
                    else:
                        logger.warning("GitHub Token 有效但未能解析用户名")
                except json.JSONDecodeError:
                    logger.warning("GitHub Token 有效但响应解析失败")
            else:
                logger.warning(f"GitHub Token 可能无效，/user 返回 code {rc}")
        else:
            logger.warning("GitHub Tool loaded but no token configured")

        if _expose_token:
            logger.warning("⚠️ expose_token_in_check 已开启，github_check_token 将返回明文 Token，请注意安全！")

    async def terminate(self):
        _default_branch_cache.clear()
        logger.info("GitHub Tool terminated")

    @on.llm_request(priority=Priority.HIGH)
    async def inject_token_hint(self, event, req: LLMRequest, *args, **kwargs):
        if not _github_token:
            return
        for p in req.system_prompt:
            if p.name == "github_token_hint":
                return

        hint_text = "【GitHub Token 状态】GitHub Personal Access Token 已在插件配置中设置完毕且有效。"
        if _github_user:
            hint_text += f" 当前登录的 GitHub 账号为: {_github_user}。"
        else:
            hint_text += " 但未能获取用户名，请检查 Token 是否有效。"
        hint_text += (" 你在调用任何 GitHub 工具时，都无需提供 token 参数，插件会自动携带 token 进行认证。"
                      "所有工具的分支参数留空即自动使用仓库默认分支，无需猜测 main/master。"
                      "用 github_create(act=file) 或 github_mutation(act=files) 写文件时无需先获取文件 SHA，插件会自动判断新建还是更新。"
                      "如果对 token 状态有疑问，可以调用 github_check_token 工具确认。")

        hint = Prompt(
            hint_text,
            name="github_token_hint",
            source="github-tool",
            persist=False
        )
        req.system_prompt.append(hint)
        logger.debug("已注入 GitHub token 提示到 system prompt")
