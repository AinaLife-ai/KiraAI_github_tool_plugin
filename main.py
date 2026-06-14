"""
GitHub Tool Plugin for KiraAI
curl 直调 GitHub REST API，零 MCP 桥接
"""
import json
import re
from pathlib import Path
from typing import Any, Optional
from core.plugin import BasePlugin
from core.logging_manager import get_logger

logger = get_logger("github-tool", "blue")


class GitHubClient:
    """Curl-based GitHub REST client, zero third-party deps"""

    def __init__(self, token: str):
        self._token = token
        import sys
        self._curl = "curl.exe" if sys.platform == "win32" else "curl"

    def _run(self, args: list[str], timeout: int = 30) -> tuple[int, str]:
        import subprocess
        cmd = [self._curl, "-s", "-H", "Accept: application/vnd.github+json"]
        if self._token:
            cmd += ["-H", f"Authorization: token {self._token}"]
        cmd += args
        try:
            r = subprocess.run(cmd, capture_output=True, text=False, timeout=timeout)
            return r.returncode, r.stdout.decode("utf-8", errors="replace").strip()
        except subprocess.TimeoutExpired:
            return -1, '{"error": "curl timeout"}'
        except Exception as e:
            return -1, json.dumps({"error": str(e)})

    def _url(self, path: str) -> str:
        return f"https://api.github.com{path}"

    def get(self, path: str, timeout: int = 30) -> tuple[int, str]:
        return self._run([self._url(path)], timeout)

    def post(self, path: str, body: dict, timeout: int = 30) -> tuple[int, str]:
        return self._run(["-X", "POST", self._url(path),
                         "-d", json.dumps(body)], timeout)

    def patch(self, path: str, body: dict, timeout: int = 30) -> tuple[int, str]:
        return self._run(["-X", "PATCH", self._url(path),
                         "-d", json.dumps(body)], timeout)

    def put(self, path: str, body: dict, timeout: int = 30) -> tuple[int, str]:
        return self._run(["-X", "PUT", self._url(path),
                         "-d", json.dumps(body)], timeout)

    def delete(self, path: str, timeout: int = 30) -> tuple[int, str]:
        return self._run(["-X", "DELETE", self._url(path)], timeout)

    def search(self, q: str, t: str = "repositories", n: int = 10) -> tuple[int, str]:
        from urllib.parse import quote
        return self.get(f"/search/{t}?q={quote(q)}&per_page={n}")

    def list(self, path: str, n: int = 20) -> tuple[int, str]:
        sep = "&" if "?" in path else "?"
        return self.get(f"{path}{sep}per_page={n}")

    def download(self, url: str) -> tuple[int, str]:
        """Download a file from GitHub raw content URL (no auth needed for public)."""
        import subprocess
        try:
            r = subprocess.run([self._curl, "-sL", url],
                              capture_output=True, text=False, timeout=30)
            return r.returncode, r.stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return -1, "download timeout"
        except Exception as e:
            return -1, str(e)


class GitHubToolPlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.logger = logger
        self.cfg = cfg
        token = cfg.get("github_token", "")
        if not token:
            self.client = None
            self.monitor = None
            return
        self.client = GitHubClient(token)
        self.monitor = None
        if cfg.get("watch_enabled", False):
            from .monitor import GitHubMonitor
            self.monitor = GitHubMonitor(self, token, self._notify_cb)

    async def initialize(self):
        if self.monitor:
            await self.monitor.start()

    async def terminate(self):
        if self.monitor:
            await self.monitor.stop()
        self.logger.info("github-tool terminated")

    async def _notify_cb(self, summary: str, msg_type: str, target: str):
        """跨会话发送通知"""
        if not target or not summary:
            return
        try:
            parts = target.split(":")
            if len(parts) < 3:
                self.logger.warning(f"Invalid notify target: {target}")
                return
            adapter = parts[0]
            stype = parts[1]
            sid = ":".join(parts[2:])
            if adapter == "qq" and stype in ("dm", "gm"):
                from core.tools import session_send
                await session_send(
                    target=f"{adapter}:{stype}:{sid}",
                    description=f"GitHub通知: {msg_type}",
                    msg=summary,
                )
        except Exception as e:
            self.logger.error(f"Notify failed: {e}")

    # ========== Search ==========

    async def github_search(
        self, event, q: str, t: str = "repositories", n: int = 10
    ) -> str:
        """Search GitHub (repositories, code, issues, users)."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        rc, out = self.client.search(q, t, n)
        if rc:
            return f"❌ Search failed: {out[:200]}"
        try:
            data = json.loads(out)
            items = data.get("items", [])
            if not items:
                return "无结果"
        except json.JSONDecodeError:
            return f"❌ 解析失败: {out[:200]}"
        lines = []
        for item in items[:n]:
            if t == "repositories":
                name = item.get("full_name", "")
                desc = item.get("description", "") or ""
                stars = item.get("stargazers_count", 0)
                lang = item.get("language") or ""
                lines.append(f"📦 {name} ⭐{stars} {lang}\n  {desc[:100]}")
            elif t == "issues":
                title = item.get("title", "")
                repo_url = item.get("repository_url", "")
                state = item.get("state", "")
                num = item.get("number", "")
                labels = ", ".join(l["name"] for l in item.get("labels", [])[:3])
                lines.append(f"#{num} {title} [{state}] {labels}\n  {repo_url}")
            elif t == "users":
                login = item.get("login", "")
                uid = item.get("id", "")
                url = item.get("html_url", "")
                lines.append(f"👤 {login} (uid:{uid})\n  {url}")
            elif t == "code":
                name = item.get("name", "")
                path = item.get("path", "")
                repo = item.get("repository", {}).get("full_name", "")
                lines.append(f"📄 {repo}/{path}")
        if not lines:
            return "无结果"
        lines.append(f"\n共 {len(items)} 条结果，显示前 {min(n, len(items))} 条")
        return "\n\n".join(lines)

    # ========== Get ==========

    async def github_get(
        self, event,
        t: str = "contents",
        o: str = "",
        r: str = "",
        p: str = "",
        i: int = 0,
        n: int = 0,
        b: str = "main",
    ) -> str:
        """Get content from GitHub."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        if t == "contents":
            if not p:
                # list repo root
                rc, out = self.client.list(f"/repos/{o}/{r}/contents/{p}", n)
            else:
                rc, out = self.client.get(f"/repos/{o}/{r}/contents/{p}?ref={b}")
            if rc:
                return f"❌ Failed: {out[:200]}"
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                return f"❌ 解析失败: {out[:200]}"
            if isinstance(data, list):
                lines = [f"📂 {o}/{r}/{p or ''}"]
                for item in data:
                    tp = "📁" if item["type"] == "dir" else "📄"
                    lines.append(f"  {tp} {item['name']}")
                return "\n".join(lines)
            else:
                content_b64 = data.get("content", "")
                import base64
                try:
                    content = base64.b64decode(content_b64).decode("utf-8")
                except Exception:
                    content = content_b64[:200]
                return content[:2000]
        elif t == "issue":
            rc, out = self.client.get(f"/repos/{o}/{r}/issues/{i}")
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_issue(out)
        elif t == "pull_request":
            rc, out = self.client.get(f"/repos/{o}/{r}/pulls/{n}")
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_pr(out)
        elif t == "pull_request_files":
            rc, out = self.client.get(f"/repos/{o}/{r}/pulls/{n}/files")
            if rc:
                return f"❌ Failed: {out[:200]}"
            try:
                files = json.loads(out)
            except json.JSONDecodeError:
                return f"❌ 解析失败: {out[:200]}"
            lines = []
            for f in files[:20]:
                fn = f.get("filename", "")
                status = f.get("status", "")
                add = f.get("additions", 0)
                del_ = f.get("deletions", 0)
                lines.append(f"  {status}: {fn} (+{add}/-{del_})")
            return f"📂 {o}/{r} PR #{n} files ({len(files)} total)\n" + "\n".join(lines)
        elif t == "pull_request_status":
            rc, out = self.client.get(f"/repos/{o}/{r}/commits/{n}/status")
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_status(out)
        elif t == "pull_request_comments":
            rc, out = self.client.get(f"/repos/{o}/{r}/pulls/{n}/comments")
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_comments(out, "review")
        elif t == "pull_request_reviews":
            rc, out = self.client.get(f"/repos/{o}/{r}/pulls/{n}/reviews")
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_reviews(out)

    # ========== List ==========

    async def github_list(
        self, event,
        t: str = "commits",
        o: str = "",
        r: str = "",
        s: str = "open",
        b: str = "main",
        n: int = 20,
    ) -> str:
        """List commits, issues or pull requests."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        if t == "commits":
            rc, out = self.client.list(f"/repos/{o}/{r}/commits?sha={b}", n)
            if rc:
                return f"❌ Failed: {out[:200]}"
            try:
                commits = json.loads(out)
            except json.JSONDecodeError:
                return f"❌ 解析失败: {out[:200]}"
            lines = [f"📜 {o}/{r} ({b}) - recent commits"]
            for c in commits[:n]:
                sha = c.get("sha", "")[:7]
                msg = (c.get("commit", {}).get("message", "") or "").split("\n")[0]
                author = c.get("commit", {}).get("author", {}).get("name", "")
                lines.append(f"  {sha} {msg[:60]} — {author}")
            return "\n".join(lines)
        elif t == "issues":
            rc, out = self.client.list(f"/repos/{o}/{r}/issues?state={s}", n)
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_issues_list(out, o, r, s)
        elif t == "pull_requests":
            rc, out = self.client.list(f"/repos/{o}/{r}/pulls?state={s}", n)
            if rc:
                return f"❌ Failed: {out[:200]}"
            return self._format_prs_list(out, o, r, s)

    # ========== Create ==========

    async def github_create(
        self, event,
        act: str = "issue",
        o: str = "",
        r: str = "",
        nm: str = "",
        pp: str = "",
        ct: str = "",
        msg: str = "",
        br: str = "main",
        ti: str = "",
        bd: str = "",
        hd: str = "",
        ba: str = "",
        pn: int = 0,
        desc: str = "",
        pv: bool = True,
        sh: str = "",
        dr: bool = False,
        ev: str = "COMMENT",
        lb: list = None,
        as_: list = None,
        fb: str = "",
    ) -> str:
        """Create GitHub resources."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        if act == "repository":
            data = {"name": nm, "private": pv, "description": desc}
            rc, out = self.client.post("/user/repos", data)
            if rc:
                return f"❌ 创建仓库失败: {out[:200]}"
            return f"✅ 仓库 {nm} 创建成功"
        elif act == "file":
            import base64
            content_b64 = base64.b64encode(ct.encode()).decode()
            data = {"message": msg, "content": content_b64, "branch": br}
            if sh:
                data["sha"] = sh
            rc, out = self.client.put(f"/repos/{o}/{r}/contents/{pp}", data)
            if rc:
                return f"❌ 文件操作失败: {out[:200]}"
            return f"✅ {pp} 已{'更新' if sh else '创建'}"
        elif act == "issue":
            data = {"title": ti, "body": bd}
            if lb:
                data["labels"] = lb
            if as_:
                data["assignees"] = as_
            rc, out = self.client.post(f"/repos/{o}/{r}/issues", data)
            if rc:
                return f"❌ 创建Issue失败: {out[:200]}"
            return f"✅ Issue #{json.loads(out).get('number', '?')} 已创建"
        elif act == "pull_request":
            data = {"title": ti, "head": hd, "base": ba, "body": bd, "draft": dr}
            rc, out = self.client.post(f"/repos/{o}/{r}/pulls", data)
            if rc:
                return f"❌ 创建PR失败: {out[:200]}"
            return f"✅ PR #{json.loads(out).get('number', '?')} 已创建"
        elif act == "branch":
            # Get SHA of source branch
            fb_ref = f"heads/{fb}" if fb else f"heads/{br}"
            rc, out = self.client.get(f"/repos/{o}/{r}/git/ref/{fb_ref}")
            if rc:
                return f"❌ 获取源分支失败: {out[:200]}"
            sha = json.loads(out).get("object", {}).get("sha", "")
            if not sha:
                return "❌ 无法获取源分支SHA"
            data = {"ref": f"refs/heads/{nm}", "sha": sha}
            rc, out = self.client.post(f"/repos/{o}/{r}/git/refs", data)
            if rc:
                return f"❌ 创建分支失败: {out[:200]}"
            return f"✅ 分支 {nm} 已创建（基于 {fb or br}）"
        elif act == "pull_request_review":
            data = {"body": bd, "event": ev}
            rc, out = self.client.post(
                f"/repos/{o}/{r}/pulls/{pn}/reviews", data)
            if rc:
                return f"❌ 提交Review失败: {out[:200]}"
            return f"✅ Review 已提交 ({ev})"
        elif act == "star":
            rc, out = self.client.put(f"/user/starred/{o}/{r}", {})
            if rc:
                return f"❌ Star失败: {out[:200]}"
            return f"⭐ 已为 {o}/{r} 点亮Star"

    # ========== Update ==========

    async def github_update(
        self, event,
        act: str = "issue",
        o: str = "",
        r: str = "",
        in_: int = 0,
        pn: int = 0,
        ti: str = "",
        bd: str = "",
        st: str = "open",
        lb: list = None,
        as_: list = None,
        ms: int = 0,
        sh: str = "",
    ) -> str:
        """Update an issue or PR branch."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        if act == "issue":
            data = {}
            if ti:
                data["title"] = ti
            if bd:
                data["body"] = bd
            if st:
                data["state"] = st
            if lb:
                data["labels"] = lb
            if as_:
                data["assignees"] = as_
            if ms:
                data["milestone"] = ms
            rc, out = self.client.patch(f"/repos/{o}/{r}/issues/{in_}", data)
            if rc:
                return f"❌ 更新Issue失败: {out[:200]}"
            return f"✅ Issue #{in_} 已更新"
        elif act == "pull_request_branch":
            data = {}
            if sh:
                data["expected_head_sha"] = sh
            rc, out = self.client.put(
                f"/repos/{o}/{r}/pulls/{pn}/update-branch", data)
            if rc:
                return f"❌ 更新PR分支失败: {out[:200]}"
            return f"✅ PR #{pn} 分支已更新"

    # ========== Mutation ==========

    async def github_mutation(
        self, event,
        act: str = "files",
        o: str = "",
        r: str = "",
        br: str = "main",
        msg: str = "",
        fs: list = None,
        in_: int = 0,
        bd: str = "",
        pn: int = 0,
        mm: str = "merge",
        ct: str = "",
    ) -> str:
        """Batch mutations: create/update files, comment, merge PR."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        if act == "files":
            if not fs:
                return "❌ 未指定文件"
            import base64
            results = []
            for f in fs:
                fp = f.get("p", "")
                fc = f.get("c", "")
                content_b64 = base64.b64encode(fc.encode()).decode()
                # Try to get existing file SHA
                rc_exist, out_exist = self.client.get(
                    f"/repos/{o}/{r}/contents/{fp}?ref={br}")
                sha = ""
                if rc_exist == 0:
                    try:
                        sha = json.loads(out_exist).get("sha", "")
                    except json.JSONDecodeError:
                        pass
                data = {
                    "message": msg or f"Update {fp}",
                    "content": content_b64,
                    "branch": br,
                }
                if sha:
                    data["sha"] = sha
                rc, out = self.client.put(
                    f"/repos/{o}/{r}/contents/{fp}", data)
                if rc:
                    results.append(f"❌ {fp}: 失败")
                else:
                    results.append(f"✅ {fp}: {'更新' if sha else '创建'}")
            return "\n".join(results)
        elif act == "issue_comment":
            data = {"body": bd}
            rc, out = self.client.post(
                f"/repos/{o}/{r}/issues/{in_}/comments", data)
            if rc:
                return f"❌ 评论失败: {out[:200]}"
            return "✅ 评论已添加"
        elif act == "pull_request":
            data = {"merge_method": mm, "commit_title": ct or bd[:72]}
            rc, out = self.client.put(
                f"/repos/{o}/{r}/pulls/{pn}/merge", data)
            if rc:
                return f"❌ 合并失败: {out[:200]}"
            return f"✅ PR #{pn} 已合并 ({mm})"

    # ========== Fork ==========

    async def github_fork(
        self, event,
        o: str = "",
        r: str = "",
        org: str = "",
    ) -> str:
        """Fork a repository."""
        if not self.client:
            return "❌ GitHub Token 未配置"
        path = f"/repos/{o}/{r}/forks"
        data = {}
        if org:
            data["organization"] = org
        rc, out = self.client.post(path, data)
        if rc:
            return f"❌ Fork失败: {out[:200]}"
        return f"✅ {o}/{r} 已Fork{' 到 ' + org if org else ''}"

    # ========== Formatters ==========

    def _format_issue(self, raw: str) -> str:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:500]
        title = d.get("title", "")
        state = d.get("state", "")
        num = d.get("number", "")
        body = (d.get("body") or "")[:300]
        user = d.get("user", {}).get("login", "")
        labels = ", ".join(l["name"] for l in d.get("labels", [])[:5])
        comments = d.get("comments", 0)
        url = d.get("html_url", "")
        return f"#{num} {title} [{state}] by {user}\n🏷️ {labels or '无标签'} 💬 {comments}条评论\n{body}\n🔗 {url}"

    def _format_pr(self, raw: str) -> str:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:500]
        title = d.get("title", "")
        state = d.get("state", "")
        num = d.get("number", "")
        body = (d.get("body") or "")[:300]
        user = d.get("user", {}).get("login", "")
        base = d.get("base", {}).get("ref", "")
        head = d.get("head", {}).get("ref", "")
        url = d.get("html_url", "")
        mergeable = d.get("mergeable")
        status = "可合并" if mergeable else "不可合并" if mergeable is False else "待检测"
        return f"PR #{num} {title} [{state}] by {user}\n{head} → {base} 状态: {status}\n{body}\n🔗 {url}"

    def _format_status(self, raw: str) -> str:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:500]
        state = d.get("state", "")
        statuses = d.get("statuses", [])
        lines = [f"合并状态: {state}"]
        for s in statuses[:10]:
            context = s.get("context", "")
            state_s = s.get("state", "")
            desc = s.get("description", "")
            lines.append(f"  {('✅' if state_s == 'success' else '❌')} {context}: {desc}")
        return "\n".join(lines)

    def _format_comments(self, raw: str, tp: str) -> str:
        try:
            comments = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300]
        lines = [f"💬 {len(comments)} 条{tp}评论"]
        for c in comments[:20]:
            user = c.get("user", {}).get("login", "?")
            body = (c.get("body") or "")[:200]
            path = c.get("path", "")
            lines.append(f"  💬 {user}: {body}")
            if path:
                lines.append(f"    📄 {path}")
        return "\n".join(lines)

    def _format_reviews(self, raw: str) -> str:
        try:
            reviews = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300]
        lines = [f"📋 {len(reviews)} 条Review"]
        for rv in reviews[:10]:
            user = rv.get("user", {}).get("login", "?")
            state = rv.get("state", "")
            body = (rv.get("body") or "")[:200]
            lines.append(f"  {user}: [{state}] {body}")
        return "\n".join(lines)

    def _format_issues_list(self, raw: str, o: str, r: str, state: str) -> str:
        try:
            issues = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300]
        # Filter out PRs (GitHub returns PRs in /issues)
        real_issues = [i for i in issues if "pull_request" not in i]
        lines = [f"📋 {o}/{r} ({state}) - {len(real_issues)} issues"]
        for i in real_issues[:20]:
            num = i.get("number", "")
            title = i.get("title", "")
            labels = ", ".join(l["name"] for l in i.get("labels", [])[:3])
            lines.append(f"  #{num} {title[:50]} [{labels}]")
        return "\n".join(lines)

    def _format_prs_list(self, raw: str, o: str, r: str, state: str) -> str:
        try:
            prs = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:300]
        lines = [f"📋 {o}/{r} ({state}) - {len(prs)} PRs"]
        for pr in prs[:20]:
            num = pr.get("number", "")
            title = pr.get("title", "")
            user = pr.get("user", {}).get("login", "?")
            lines.append(f"  #{num} {title[:50]} — {user}")
        return "\n".join(lines)
