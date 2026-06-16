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
_file_max_chars: int = 5000
_expose_token: bool = False

# Windows用curl.exe，其他平台（Linux/macOS）用curl
_CURL_CMD = "curl.exe" if sys.platform == "win32" else "curl"


def _curl(args: list[str], timeout: int = 30) -> tuple[int, str]:
    cmd = [_CURL_CMD, "-s", "-H", "Accept: application/vnd.github+json"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=False, timeout=timeout)
        out = r.stdout.decode("utf-8", errors="replace").strip()
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return -1, '{"error": "curl timeout"}'
    except Exception as e:
        return -1, json.dumps({"error": str(e)})


def _url(path: str) -> str:
    return f"https://api.github.com{path}"


def _auth() -> str:
    return f"Authorization: token {_github_token}"


def _check() -> str:
    if not _github_token:
        return "GitHub Token 未配置，请在 WebUI 插件设置中填写（已自动保存，无需每次提供）。"
    return ""


def _fmt(raw: str) -> str:
    if not raw:
        return "No output"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]

    if isinstance(data, dict):
        if "message" in data and "documentation_url" in data:
            return f"Error: {data['message']}"
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
        for k in ["sha", "full_name", "name"]:
            if k in data:
                v = data[k]
                if k == "full_name":
                    return f"{v}  {data.get('html_url', '')}"
                if k == "sha":
                    return f"sha: {v}"
                if k == "name":
                    return f"{v}  {data.get('html_url', '')}"
        if "commit" in data and "sha" in data:
            return f"sha: {data['sha']}  {data['commit'].get('message','')[:60]}"
        if "id" in data:
            for k in ["title", "name", "login", "message"]:
                if k in data:
                    return str(data[k])[:200]
        return json.dumps(data, ensure_ascii=False)[:500]
    elif isinstance(data, list):
        lines = []
        for item in data[:20]:
            n = item.get("full_name") or item.get("name") or item.get("title") or item.get("filename") or item.get("login", "?")
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

    if "message" in data and "documentation_url" in data:
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
        "Example: first call with limit=5000, then if '还有更多' is '是', call again with offset=5000 (or the suggested offset) to read the next part."
    ),
    params={
        "type": "object",
        "properties": {
            "o": {"type": "string", "description": "Repository owner (username or organization)"},
            "r": {"type": "string", "description": "Repository name"},
            "p": {"type": "string", "description": "File path (e.g. src/main.py)"},
            "b": {"type": "string", "description": "Branch ref (default: main)", "default": "main"},
            "limit": {"type": "integer", "description": "Max characters to return (default: 5000, configurable in plugin settings)"},
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
    b = kw.get("b", "main")
    limit = kw.get("limit", _file_max_chars)
    offset = kw.get("offset", 0)

    if not o or not r or not p:
        return "Missing required parameters: o (owner), r (repo), p (path)"

    if limit < 1:
        return "limit must be at least 1"
    if offset < 0:
        return "offset must be >= 0"

    rc, out = _curl(["-H", _auth(), _url(f"/repos/{o}/{r}/contents/{p}?ref={b}")])
    if rc != 0:
        return f"curl failed with code {rc}"
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
    rc, out = _curl(["-H", _auth(), _url(f"/search/{t}?q={quote(q)}&per_page={n}")])
    return _fmt(out) if rc == 0 else f"curl failed with code {rc}"


@register.tool(
    name="github_get",
    description="Get metadata from GitHub: file SHA, issue details, pull request details, PR files, PR status, PR comments, PR reviews. For reading actual file content, use github_read_file instead.",
    params={
        "type": "object",
        "properties": {
            "t": {"type": "string", "enum": ["contents", "issue", "pull_request", "pull_request_files", "pull_request_status", "pull_request_comments", "pull_request_reviews"], "description": "Type of resource to get"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "p": {"type": "string", "description": "File path (for contents)"},
            "i": {"type": "integer", "description": "Issue number"},
            "n": {"type": "integer", "description": "Pull request number"},
            "b": {"type": "string", "description": "Branch ref (default: main)", "default": "main"}
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
    b = kw.get("b", "main")

    ep_map = {
        "contents": f"/repos/{o}/{r}/contents/{p}?ref={b}",
        "issue": f"/repos/{o}/{r}/issues/{i}",
        "pull_request": f"/repos/{o}/{r}/pulls/{n}",
        "pull_request_files": f"/repos/{o}/{r}/pulls/{n}/files",
        "pull_request_status": f"/repos/{o}/{r}/commits/{b}/status",
        "pull_request_comments": f"/repos/{o}/{r}/pulls/{n}/comments",
        "pull_request_reviews": f"/repos/{o}/{r}/pulls/{n}/reviews",
    }
    ep = ep_map.get(t)
    if not ep:
        return f"Unknown target: {t}"
    rc, out = _curl(["-H", _auth(), _url(ep)])
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
            "b": {"type": "string", "description": "Branch (commits only)", "default": "main"},
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
    b = kw.get("b", "main")
    n = min(kw.get("n", 20), 100)

    ep_map = {
        "commits": f"/repos/{o}/{r}/commits?sha={b}&per_page={n}",
        "issues": f"/repos/{o}/{r}/issues?state={s}&per_page={n}",
        "pull_requests": f"/repos/{o}/{r}/pulls?state={s}&per_page={n}",
    }
    ep = ep_map.get(t)
    if not ep:
        return f"Unknown target: {t}"
    rc, out = _curl(["-H", _auth(), _url(ep)])
    return _fmt(out) if rc == 0 else f"curl failed with code {rc}"


@register.tool(
    name="github_create",
    description="Create GitHub resources: repository, file (create/update), issue, pull request, branch, pull request review, star, or release.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["repository", "file", "issue", "pull_request", "branch", "pull_request_review", "star", "release"], "description": "What type of resource to create"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "nm": {"type": "string", "description": "Name (repo name, branch name, or release tag name)"},
            "pp": {"type": "string", "description": "File path (for file action)"},
            "ct": {"type": "string", "description": "File content (for file action) or body text"},
            "msg": {"type": "string", "description": "Commit message (for file action)"},
            "br": {"type": "string", "description": "Branch name", "default": "main"},
            "ti": {"type": "string", "description": "Title (for issue/PR/release)"},
            "bd": {"type": "string", "description": "Body text (for issue/PR/release review)"},
            "hd": {"type": "string", "description": "Head branch (for PR)"},
            "ba": {"type": "string", "description": "Base branch (for PR)"},
            "pn": {"type": "integer", "description": "PR number (for review)"},
            "desc": {"type": "string", "description": "Repository description"},
            "pv": {"type": "boolean", "description": "Private repository?", "default": True},
            "sh": {"type": "string", "description": "SHA of existing file (required when updating)"},
            "dr": {"type": "boolean", "description": "Draft pull request?", "default": False},
            "ev": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"], "description": "Review event type"},
            "lb": {"type": "array", "items": {"type": "string"}, "description": "Labels to apply"},
            "as": {"type": "array", "items": {"type": "string"}, "description": "Users to assign"},
            "fb": {"type": "string", "description": "Source branch to fork from (for branch creation)"},
            "target": {"type": "string", "description": "Commit SHA or branch name for the release (default: default branch)"},
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
    pp = kw.get("pp", "")
    ct = kw.get("ct", "")
    msg = kw.get("msg", "")
    br = kw.get("br", "main")
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
    draft = kw.get("draft", False)
    prerelease = kw.get("prerelease", False)

    if act == "repository":
        d = {"name": nm, "description": desc, "private": pv}
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url("/user/repos")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "file":
        encoded = base64.b64encode(ct.encode()).decode()
        d = {"message": msg or f"Update {pp}", "content": encoded, "branch": br}
        if sh:
            d["sha"] = sh
        rc, out = _curl(["-X", "PUT", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/contents/{pp}")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "issue":
        d = {"title": ti}
        if bd:
            d["body"] = bd
        if lb:
            d["labels"] = lb
        if as_:
            d["assignees"] = as_
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/issues")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request":
        d = {"title": ti, "head": hd, "base": ba}
        if bd:
            d["body"] = bd
        if dr:
            d["draft"] = True
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/pulls")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "branch":
        src = fb or br
        rc2, ref_out = _curl(["-H", _auth(), _url(f"/repos/{o}/{r}/git/refs/heads/{src}")])
        if rc2 != 0:
            return f"Failed to get source branch ref: {ref_out[:200]}"
        try:
            sha_val = json.loads(ref_out)["object"]["sha"]
        except (json.JSONDecodeError, KeyError):
            return f"Failed to parse ref: {ref_out[:200]}"
        d = {"ref": f"refs/heads/{nm}", "sha": sha_val}
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/git/refs")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request_review":
        d = {"body": bd or "", "event": ev or "COMMENT"}
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/pulls/{pn}/reviews")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "star":
        rc, out = _curl(["-X", "PUT", "-H", _auth(), "-H", "Content-Length: 0",
                         _url(f"/user/starred/{o}/{r}")])
        if rc == 0:
            return f"⭐ Starred {o}/{r} successfully"
        return f"Star failed (code {rc}): {out[:200]}"

    elif act == "release":
        if not nm:
            return "Missing required parameter: nm (tag name)"
        d = {"tag_name": nm, "name": ti or nm, "body": bd or "", "draft": draft, "prerelease": prerelease}
        if target:
            d["target_commitish"] = target
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/releases")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    return f"Unknown action: {act}"


@register.tool(
    name="github_update",
    description="Update a GitHub issue (title, body, state, labels, assignees, milestone) or update a pull request branch.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["issue", "pull_request_branch"], "description": "What to update"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "in": {"type": "integer", "description": "Issue number"},
            "pn": {"type": "integer", "description": "PR number"},
            "ti": {"type": "string", "description": "New title"},
            "bd": {"type": "string", "description": "New body"},
            "st": {"type": "string", "enum": ["open", "closed"], "description": "New state"},
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
        rc, out = _curl(["-X", "PATCH", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/issues/{inn}")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request_branch":
        d = {}
        if sh:
            d["expected_head_sha"] = sh
        rc, out = _curl(["-X", "PUT", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d) if d else "{}",
                         _url(f"/repos/{o}/{r}/pulls/{pn}/update-branch")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    return f"Unknown action: {act}"


@register.tool(
    name="github_mutation",
    description="Perform batched mutations: create/update multiple files, add issue comment, or merge a pull request.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["files", "issue_comment", "pull_request"], "description": "Mutation type"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "br": {"type": "string", "description": "Branch", "default": "main"},
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
    br = kw.get("br", "main")
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
        results = []
        for f in fs:
            fp = f.get("p", "")
            fc = f.get("c", "")
            encoded = base64.b64encode(fc.encode()).decode()
            d = {"message": msg or f"Update {fp}", "content": encoded, "branch": br}
            rc, out = _curl(["-X", "PUT", "-H", _auth(), "-H", "Content-Type: application/json",
                             "-d", json.dumps(d), _url(f"/repos/{o}/{r}/contents/{fp}")])
            try:
                od = json.loads(out)
                s = od.get("content", {}).get("sha", "?")
            except Exception:
                s = out[:100]
            results.append(f"  {fp}: {s}")
        return "Files:\n" + "\n".join(results)

    elif act == "issue_comment":
        d = {"body": bd}
        rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/issues/{inn}/comments")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    elif act == "pull_request":
        d = {"merge_method": mm}
        if ct:
            d["commit_title"] = ct
        if bd:
            d["commit_message"] = bd
        rc, out = _curl(["-X", "PUT", "-H", _auth(), "-H", "Content-Type: application/json",
                         "-d", json.dumps(d), _url(f"/repos/{o}/{r}/pulls/{pn}/merge")])
        return _fmt(out) if rc == 0 else f"curl failed with code {rc}"

    return f"Unknown action: {act}"


@register.tool(
    name="github_fork",
    description="Fork a GitHub repository to your personal account or a specified organization.",
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
    body = json.dumps(d) if d else "{}"
    rc, out = _curl(["-X", "POST", "-H", _auth(), "-H", "Content-Type: application/json",
                     "-d", body, _url(f"/repos/{o}/{r}/forks")])
    return _fmt(out) if rc == 0 else f"curl failed with code {rc}"


class GitHubToolPlugin(BasePlugin):
    async def initialize(self):
        global _github_token, _github_user, _file_max_chars, _expose_token
        _github_token = self.plugin_cfg.get("github_token", "")
        _file_max_chars = int(self.plugin_cfg.get("file_content_max_chars", 5000))
        _expose_token = bool(self.plugin_cfg.get("expose_token_in_check", False))

        if _github_token:
            rc, out = _curl(["-H", _auth(), _url("/user")])
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
        hint_text += " 你在调用任何 GitHub 工具时，都无需提供 token 参数，插件会自动携带 token 进行认证。如果对 token 状态有疑问，可以调用 github_check_token 工具确认。"

        hint = Prompt(
            hint_text,
            name="github_token_hint",
            source="github-tool",
            persist=False
        )
        req.system_prompt.append(hint)
        logger.debug("已注入 GitHub token 提示到 system prompt")
