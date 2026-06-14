"""
GitHub Auto-Watch Monitor
定时检查PR/Issue评论，自动回应或请求确认
支持 search mode：全量搜索我的所有 PR/Issue vs 按仓库列表扫描
"""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable
from urllib.parse import quote

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


def _parse_cron_minutes(cron: str) -> Optional[int]:
    """简易cron解析：只处理 '分钟 小时 * * *' 格式，返回下次检查间隔秒数"""
    try:
        parts = cron.strip().split()
        if len(parts) < 2:
            return None
        cron_min = int(parts[0])
        cron_hour = int(parts[1])
        now = datetime.now()
        target = now.replace(hour=cron_hour, minute=cron_min, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
    except (ValueError, IndexError):
        return None


class GitHubMonitor:
    """GitHub PR/Issue 自动监控器"""

    def __init__(self, plugin, token: str, notify_cb: Optional[Callable] = None):
        self.plugin = plugin
        self._token = token
        self._notify = notify_cb
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_check: dict[str, str] = {}
        self._me: str = ""

    def c(self, key: str, default=None):
        return self.plugin.plugin_cfg.get(f"watch_{key}", default)

    @property
    def enabled(self) -> bool:
        return self.c("enabled", False) and bool(self._token)

    def _targets(self) -> list[str]:
        """获取通知目标列表（兼容list和string）"""
        raw = self.c("notify_target", [])
        if isinstance(raw, list):
            return [t.strip() for t in raw if t and t.strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []

    async def start(self):
        if not self.enabled:
            return
        rc, out = _curl(["-H", _auth(self._token), _url("/user")])
        if rc == 0:
            try:
                self._me = json.loads(out).get("login", "")
            except json.JSONDecodeError:
                pass
        if not self._me:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger = self.plugin.logger
        logger.info(f"[Monitor] Auto-watch started (user: {self._me}, search_mode: {self.c('search_mode', False)})")

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
                cron = self.c("fixed_cron", "0 9 * * *")
                delay = _parse_cron_minutes(cron)
                if delay:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(3600)
            else:
                minutes = self.c("interval_minutes", 60)
                await asyncio.sleep(minutes * 60)

    async def _check_all(self):
        search_mode = self.c("search_mode", False)

        if search_mode:
            await self._search_my_prs()
            if self.c("watch_issues", True):
                await self._search_my_issues()
        else:
            repos = list(self.c("repos", []))
            if self.c("own_repos", True):
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
                await self._scan_repo_prs(owner, name)
                if self.c("watch_issues", True):
                    await self._scan_repo_issues(owner, name)

    # ========== Search Mode ==========

    async def _search_my_prs(self):
        q = f"author:{self._me} type:pr state:open"
        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/search/issues?q={quote(q)}&per_page=50&sort=updated")])
        if rc != 0:
            return
        try:
            data = json.loads(out)
            items = data.get("items", [])
        except json.JSONDecodeError:
            return

        targets = self._targets()
        auto_fix = self.c("auto_fix", False)
        require_confirm = self.c("require_confirm", True)

        for pr in items:
            repo_url = pr.get("repository_url", "")
            parts = repo_url.strip("/").split("/")
            if len(parts) < 2:
                continue
            owner, name = parts[-2], parts[-1]
            pr_num = pr["number"]
            pr_title = pr["title"]
            pr_url = pr.get("html_url", "")

            rc2, out2 = _curl(["-H", _auth(self._token),
                              _url(f"/repos/{owner}/{name}/pulls/{pr_num}/comments")])
            if rc2 != 0:
                continue

            try:
                comments = json.loads(out2)
            except json.JSONDecodeError:
                continue

            last_key = f"pr_{owner}_{name}_{pr_num}"
            last = self._last_check.get(last_key, "")
            new_comments = [c for c in comments
                            if c.get("user", {}).get("login") != self._me
                            and c.get("created_at", "") > last]

            if not new_comments:
                continue

            self._last_check[last_key] = datetime.now(timezone.utc).isoformat()

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

            for t in targets:
                if self._notify:
                    await self._notify(summary, "review", t)

            if auto_fix and not require_confirm:
                await self._auto_fix_pr(owner, name, pr_num, new_comments)

    async def _search_my_issues(self):
        q = f"assignee:{self._me} state:open type:issue"
        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/search/issues?q={quote(q)}&per_page=50&sort=updated")])
        if rc != 0:
            return
        try:
            data = json.loads(out)
            items = data.get("items", [])
        except json.JSONDecodeError:
            return

        targets = self._targets()

        for issue in items:
            repo_url = issue.get("repository_url", "")
            parts = repo_url.strip("/").split("/")
            if len(parts) < 2:
                continue
            owner, name = parts[-2], parts[-1]
            issue_num = issue["number"]
            issue_title = issue["title"]
            issue_url = issue.get("html_url", "")

            rc2, out2 = _curl(["-H", _auth(self._token),
                              _url(f"/repos/{owner}/{name}/issues/{issue_num}/comments")])
            if rc2 != 0:
                continue

            try:
                comments = json.loads(out2)
            except json.JSONDecodeError:
                continue

            last_key = f"issue_{owner}_{name}_{issue_num}"
            last = self._last_check.get(last_key, "")
            new_comments = [c for c in comments
                            if c.get("user", {}).get("login") != self._me
                            and c.get("created_at", "") > last]

            if not new_comments:
                continue

            self._last_check[last_key] = datetime.now(timezone.utc).isoformat()

            lines = [f"📮 Issue #{issue_num} 「{issue_title}」有新的回复"]
            for c in new_comments:
                user = c.get("user", {}).get("login", "?")
                body = c.get("body", "")[:300]
                lines.append(f"  💬 {user}: {body}")
            lines.append(f"  🔗 {issue_url}")
            summary = "\n".join(lines)

            for t in targets:
                if self._notify:
                    await self._notify(summary, "issue", t)

    # ========== Repo Scan Mode ==========

    async def _scan_repo_prs(self, owner: str, name: str):
        targets = self._targets()
        auto_fix = self.c("auto_fix", False)
        require_confirm = self.c("require_confirm", True)

        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/repos/{owner}/{name}/pulls?state=open&per_page=20")])
        if rc != 0:
            return

        try:
            prs = json.loads(out)
        except json.JSONDecodeError:
            return

        for pr in prs:
            if pr.get("user", {}).get("login") != self._me:
                continue

            pr_num = pr["number"]
            pr_title = pr["title"]
            pr_url = pr.get("html_url", "")

            rc2, out2 = _curl(["-H", _auth(self._token),
                              _url(f"/repos/{owner}/{name}/pulls/{pr_num}/comments")])
            if rc2 != 0:
                continue

            try:
                comments = json.loads(out2)
            except json.JSONDecodeError:
                continue

            last_key = f"pr_{owner}_{name}_{pr_num}"
            last = self._last_check.get(last_key, "")
            new_comments = [c for c in comments
                            if c.get("user", {}).get("login") != self._me
                            and c.get("created_at", "") > last]

            if not new_comments:
                continue

            self._last_check[last_key] = datetime.now(timezone.utc).isoformat()

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

            for t in targets:
                if self._notify:
                    await self._notify(summary, "review", t)

            if auto_fix and not require_confirm:
                await self._auto_fix_pr(owner, name, pr_num, new_comments)

    async def _scan_repo_issues(self, owner: str, name: str):
        targets = self._targets()

        rc, out = _curl(["-H", _auth(self._token),
                         _url(f"/repos/{owner}/{name}/issues?state=open&per_page=20&assignee={self._me}")])
        if rc != 0:
            return

        try:
            issues = json.loads(out)
        except json.JSONDecodeError:
            return

        for issue in issues:
            if "pull_request" in issue:
                continue

            issue_num = issue["number"]
            issue_title = issue["title"]
            issue_url = issue.get("html_url", "")

            rc2, out2 = _curl(["-H", _auth(self._token),
                              _url(f"/repos/{owner}/{name}/issues/{issue_num}/comments")])
            if rc2 != 0:
                continue

            try:
                comments = json.loads(out2)
            except json.JSONDecodeError:
                continue

            last_key = f"issue_{owner}_{name}_{issue_num}"
            last = self._last_check.get(last_key, "")
            new_comments = [c for c in comments
                            if c.get("user", {}).get("login") != self._me
                            and c.get("created_at", "") > last]

            if not new_comments:
                continue

            self._last_check[last_key] = datetime.now(timezone.utc).isoformat()

            lines = [f"📮 Issue #{issue_num} 「{issue_title}」有新的回复"]
            for c in new_comments:
                user = c.get("user", {}).get("login", "?")
                body = c.get("body", "")[:300]
                lines.append(f"  💬 {user}: {body}")
            lines.append(f"  🔗 {issue_url}")
            summary = "\n".join(lines)

            for t in targets:
                if self._notify:
                    await self._notify(summary, "issue", t)

    # ========== Auto Fix ==========

    async def _auto_fix_pr(self, owner: str, name: str, pr_num: int,
                           comments: list[dict]):
        logger = self.plugin.logger
        logger.info(f"[Monitor] Auto-fix triggered for {owner}/{name}#{pr_num}")
        # TODO: 调用LLM分析review意见并生成修改
