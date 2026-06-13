import asyncio
import base64
import json
import subprocess
from typing import Optional, Any
from urllib.parse import quote

from core.plugin import BasePlugin
from core.plugin.plugin_registry import register
from core.logging_manager import get_logger

logger = get_logger("github-tool", "green")

_github_token: str = ""


def _curl(args: list[str], timeout: int = 30) -> tuple[int, str]:
    cmd = ["curl.exe", "-s", "-H", "Accept: application/vnd.github+json"] + args
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
        return "GitHub Token 未配置，请在 WebUI 插件设置中填写"
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


@register.tool(
    name="github_search",
    description="Search GitHub (repositories, code, issues, users). Use when user wants to search for repos, code, issues on GitHub. Returns concise token-efficient results.",
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
    description="Get content from GitHub: file contents, issue details, pull request details, PR files, PR status, PR comments, PR reviews.",
    params={
        "type": "object",
        "properties": {
            "t": {"type": "string", "enum": ["contents", "issue", "pull_request", "pull_request_files", "pull_request_status", "pull_request_comments", "pull_request_reviews"], "description": "Type of resource to get"},
            "o": {"type": "string", "description": "Repository owner (username or organization)"},
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
    description="Create GitHub resources: repository, file (create/update), issue, pull request, branch, or pull request review.",
    params={
        "type": "object",
        "properties": {
            "act": {"type": "string", "enum": ["repository", "file", "issue", "pull_request", "branch", "pull_request_review"], "description": "What type of resource to create"},
            "o": {"type": "string", "description": "Repository owner"},
            "r": {"type": "string", "description": "Repository name"},
            "nm": {"type": "string", "description": "Name (repo name, branch name)"},
            "pp": {"type": "string", "description": "File path (for file action)"},
            "ct": {"type": "string", "description": "File content (for file action) or body text"},
            "msg": {"type": "string", "description": "Commit message (for file action)"},
            "br": {"type": "string", "description": "Branch name", "default": "main"},
            "ti": {"type": "string", "description": "Title (for issue/PR)"},
            "bd": {"type": "string", "description": "Body text (for issue/PR/review)"},
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
            "fb": {"type": "string", "description": "Source branch to fork from (for branch creation)"}
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
    """GitHub Tool Plugin — curl直接调用GitHub API"""

    async def initialize(self):
        global _github_token
        _github_token = self.plugin_cfg.get("github_token", "")
        if _github_token:
            logger.info(f"GitHub Tool ready (token: {_github_token[:6]}...{_github_token[-4:]})")
        else:
            logger.warning("GitHub Tool loaded but no token configured")

    async def terminate(self):
        logger.info("GitHub Tool terminated")
