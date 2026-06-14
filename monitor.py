"""
GitHub Auto-Watch Monitor
定时检查PR/Issue评论，自动回应或请求确认
"""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional, Callable

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


def _auth(token: str) -> str:
    return f"Authorization: token {token}"


class GitHubMonitor:
    """GitHub PR/Issue 自动监控器"""

    def __init__(self, plugin, token: str, notify_cb: Optional[Callable] = None):
        self.plugin = plugin
        self._token = token
        self._notify = notify_cb
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_check: dict[str, str] = {}

    def c(self, key: str, default=None):
        return self.plugin.plugin_cfg.get(f"watch_{key}", default)

    @property
    def enabled(self) -> bool:
        return self.c("enabled", False) and bool(self._token)

    async def start(self):
        if not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger = self.plugin.logger
        logger.info("[Monitor] Auto-watch started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger = self.plugin.logger
        logger.info("[Monitor] Auto-watch stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._check_all()
            except Exception as e:
                logger = self.plugin.logger
                logger.error(f"[Monitor] Check failed: {e}")

            interval_type = self.c("interval_type", "interval")
            if interval_type == "fixed_time":
                break
            minutes = self.c("interval_minutes", 60)
            await asyncio.sleep(minutes * 60)

    async def _check_all(self):
        repos = list(self.c("repos", []))
        own_repos = self.c("own_repos", True)

        if own_repos:
            rc, out = _curl(["-H", _auth(self._token),
                             _url("/user/repos?per_page=100&type=owner&sort=updated")])
            if rc == 0:
                try:
                    data = json.loads(out)
                    for repo in data:
                        full = repo.get("full_name", "")
                        if full and full not in repos:
                            repos.append(full)
                except json.JSONDecodeError:
                    pass

        for full_name in repos:
            if "/" not in full_name:
                continue
            owner, name = full_name.split("/", 1)
            await self._check_repo(owner, name)

    async def _get_me(self) -> str:
        rc, out = _curl(["-H", _auth(self._token), _url("/user")])
        if rc != 0:
            return ""
        try:
            return json.loads(out).get("login", "")
        except json.JSONDecodeError:
            return ""

    async def _check_repo(self, owner: str, name: str):
        me = await self._get_me()
        if not me:
            return

        notify_target = self.c("notify_target", "")
        auto_fix = self.c("auto_fix", False)
        require_confirm = self.c("require_confirm", True)

        # 检查我的PR是否有新的review comments
        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/repos/{owner}/{name}/pulls?state=open&per_page=20")])
        if rc == 0:
            try:
                prs = json.loads(out)
                for pr in prs:
                    if pr.get("user", {}).get("login") == me:
                        await self._check_pr(owner, name, pr, me, notify_target, auto_fix, require_confirm)
            except json.JSONDecodeError:
                pass

        # 检查分配给自己的issue
        if self.c("watch_issues", True):
            rc, out = _curl(["-H", _auth(self._token),
                             _url(f"/repos/{owner}/{name}/issues?state=open&per_page=20&assignee={me}")])
            if rc == 0:
                try:
                    issues = json.loads(out)
                    for issue in issues:
                        if "pull_request" not in issue:
                            await self._check_issue(owner, name, issue, me, notify_target)
                except json.JSONDecodeError:
                    pass

    async def _check_pr(self, owner: str, name: str, pr: dict, me: str,
                        notify_target: str, auto_fix: bool, require_confirm: bool):
        pr_num = pr["number"]
        pr_title = pr["title"]
        pr_url = pr.get("html_url", "")
        full = f"{owner}/{name}"

        # 获取review comments
        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/repos/{owner}/{name}/pulls/{pr_num}/comments")])
        if rc != 0:
            return

        try:
            comments = json.loads(out)
        except json.JSONDecodeError:
            return

        last_key = f"pr_{owner}_{name}_{pr_num}"
        last = self._last_check.get(last_key, "")
        new_comments = [c for c in comments
                        if c.get("user", {}).get("login") != me
                        and c.get("created_at", "") > last]

        if not new_comments:
            return

        self._last_check[last_key] = datetime.now(timezone.utc).isoformat()

        # 构建通知
        lines = [f"📮 PR #{pr_num} 「{pr_title}」收到新的审查意见"]
        for c in new_comments:
            user = c.get("user", {}).get("login", "?")
            body = c.get("body", "")[:300]
            path = c.get("path", "")
            lines.append(f"  💬 {user}: {body}")
            if path:
                lines.append(f"    📄 {path}")
        lines.append(f"  🔗 {pr_url}")
        summary = "\n".join(lines)

        if notify_target and self._notify:
            await self._notify(summary, "review", notify_target)

        # 自动修复模式
        if auto_fix and not require_confirm:
            await self._auto_fix_pr(owner, name, pr_num, new_comments, me)

    async def _check_issue(self, owner: str, name: str, issue: dict, me: str, notify_target: str):
        issue_num = issue["number"]
        issue_title = issue["title"]
        issue_url = issue.get("html_url", "")

        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/repos/{owner}/{name}/issues/{issue_num}/comments")])
        if rc != 0:
            return

        try:
            comments = json.loads(out)
        except json.JSONDecodeError:
            return

        last_key = f"issue_{owner}_{name}_{issue_num}"
        last = self._last_check.get(last_key, "")
        new_comments = [c for c in comments
                        if c.get("user", {}).get("login") != me
                        and c.get("created_at", "") > last]

        if not new_comments:
            return

        self._last_check[last_key] = datetime.now(timezone.utc).isoformat()

        lines = [f"📮 Issue #{issue_num} 「{issue_title}」有新的回复"]
        for c in new_comments:
            user = c.get("user", {}).get("login", "?")
            body = c.get("body", "")[:300]
            lines.append(f"  💬 {user}: {body}")
        lines.append(f"  🔗 {issue_url}")
        summary = "\n".join(lines)

        if notify_target and self._notify:
            await self._notify(summary, "issue", notify_target)

    async def _auto_fix_pr(self, owner: str, name: str, pr_num: int,
                           comments: list[dict], me: str):
        """自动根据review意见修改代码（预留实现）"""
        logger = self.plugin.logger
        logger.info(f"[Monitor] Auto-fix triggered for {owner}/{name}#{pr_num}")
        # TODO: 调用LLM分析review意见并生成修改
        # 后续版本实现：提取意见→分析代码→修改→推送新commit
