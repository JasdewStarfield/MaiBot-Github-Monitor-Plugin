"""MaiBot GitHub 仓库监控插件。

定期轮询指定仓库分支上的最新提交，并把提交通知广播到配置好的群聊中。
在开启点评功能时，插件会先发送一条固定通知，再触发 Maisaka 基于当前上下文补一句简短点评。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

import aiohttp

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase

PLUGIN_CONFIG_VERSION = "2.0.0"
GITHUB_API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 15


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__: ClassVar[str] = "插件"

    enable: bool = Field(default=True, description="是否启用 GitHub 监控插件")
    config_version: str = Field(default=PLUGIN_CONFIG_VERSION, description="配置版本号")


class GlobalSectionConfig(PluginConfigBase):
    """全局运行配置。"""

    __ui_label__: ClassVar[str] = "全局设置"

    token: str = Field(
        default="",
        description="GitHub Token，可选；填写后可以提高 GitHub API 速率限制",
        json_schema_extra={"placeholder": "ghp_xxx 或 github_pat_xxx"},
    )
    interval: int = Field(default=60, ge=10, description="轮询间隔（秒）")
    enable_commentary: bool = Field(
        default=True,
        description="是否让 Maisaka 在通知后补一句简短点评",
    )


class RepositoryConfig(PluginConfigBase):
    """单个仓库监控项。"""

    owner: str = Field(default="", description="仓库拥有者")
    repo: str = Field(default="", description="仓库名")
    branch: str = Field(default="main", description="监控的分支名")


class SubscriberConfig(PluginConfigBase):
    """单个通知目标。"""

    group_id: str = Field(default="", description="接收通知的群 ID")
    platform: str = Field(default="qq", description="群所属的平台")


class MonitorSectionConfig(PluginConfigBase):
    """监控目标与订阅者配置。"""

    __ui_label__: ClassVar[str] = "监控任务"

    repositories: list[RepositoryConfig] = Field(default_factory=list, description="需要监控的仓库列表")
    subscribers: list[SubscriberConfig] = Field(default_factory=list, description="接收通知的群列表")


class GitHubMonitorConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    global_: GlobalSectionConfig = Field(
        default_factory=GlobalSectionConfig,
        alias="global",
        validation_alias="global",
        serialization_alias="global",
        description="全局配置",
        json_schema_extra={"group": "global"},
    )
    monitor: MonitorSectionConfig = Field(default_factory=MonitorSectionConfig)


class GitHubMonitorPlugin(MaiBotPlugin):
    """定期检查 GitHub 提交并广播到群聊。"""

    config_model = GitHubMonitorConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: aiohttp.ClientSession | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        # 记录每个仓库分支最近一次看到的提交 SHA，避免首次加载时把历史提交全部重发。
        self._repo_states: dict[str, str] = {}

    async def on_load(self) -> None:
        """插件加载后按当前配置启动后台轮询任务。"""
        await self._sync_monitor_task()

    async def on_unload(self) -> None:
        """插件卸载时停止后台任务并关闭 HTTP 会话。"""
        await self._stop_monitor_task()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热重载后同步后台任务状态。"""
        del config_data

        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("GitHub 监控插件配置已更新，version=%s", version)
            await self._sync_monitor_task()

    async def _sync_monitor_task(self) -> None:
        """根据启用状态启动或停止监控任务。"""
        if self.config.plugin.enable:
            await self._ensure_monitor_task()
            self.ctx.logger.info("GitHub 监控插件已启用")
            return

        await self._stop_monitor_task()
        self.ctx.logger.info("GitHub 监控插件当前处于停用状态")

    async def _ensure_monitor_task(self) -> None:
        """确保轮询任务与 HTTP 会话已经就绪。"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(timeout=timeout)

        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="github-monitor-loop")

    async def _stop_monitor_task(self) -> None:
        """停止轮询任务，并清理网络资源。"""
        task = self._monitor_task
        self._monitor_task = None

        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _monitor_loop(self) -> None:
        """后台轮询 GitHub 提交。"""
        self.ctx.logger.info("GitHub 监控任务已启动，10 秒后开始首次轮询")
        await asyncio.sleep(10)

        while True:
            interval = max(self.config.global_.interval, 10)
            repositories = list(self.config.monitor.repositories)

            if not repositories:
                self.ctx.logger.warning("未配置任何 GitHub 仓库，等待下一次轮询")
                await asyncio.sleep(interval)
                continue

            for repository in repositories:
                await self._poll_repository(repository)
                await asyncio.sleep(1)

            await asyncio.sleep(interval)

    async def _poll_repository(self, repository: RepositoryConfig) -> None:
        """轮询单个仓库，并在有新提交时发送通知。"""
        owner = repository.owner.strip()
        repo_name = repository.repo.strip()
        branch = repository.branch.strip() or "main"

        if not owner or not repo_name:
            return

        commits = await self._fetch_latest_commits(owner=owner, repo=repo_name, branch=branch)
        if not commits:
            return

        repo_key = f"{owner}/{repo_name}/{branch}"
        current_latest_sha = str(commits[0].get("sha") or "").strip()
        if not current_latest_sha:
            return

        last_seen_sha = self._repo_states.get(repo_key)
        if last_seen_sha is None:
            self._repo_states[repo_key] = current_latest_sha
            self.ctx.logger.info("初始化监控状态：%s -> %s", repo_key, current_latest_sha[:7])
            return

        if current_latest_sha == last_seen_sha:
            self.ctx.logger.debug("%s 无新提交", repo_key)
            return

        new_commits: list[dict[str, Any]] = []
        previous_sha_found = False
        for commit in commits:
            commit_sha = str(commit.get("sha") or "").strip()
            if commit_sha == last_seen_sha:
                previous_sha_found = True
                break
            new_commits.append(commit)

        # 如果旧 SHA 不在当前结果页里，说明提交跨距过大或历史被重写。
        # 这里退化为只通知最新一条，避免一次性刷屏。
        if not previous_sha_found and new_commits:
            self.ctx.logger.warning("%s 的历史基线未命中，本次仅广播最新一条提交", repo_key)
            new_commits = [new_commits[0]]

        self._repo_states[repo_key] = current_latest_sha
        self.ctx.logger.info("%s 检测到 %d 条新提交", repo_key, len(new_commits))

        for commit in reversed(new_commits):
            await self._broadcast_notification(commit_item=commit, repo_name=repo_name, branch=branch)
            await asyncio.sleep(1)

    async def _fetch_latest_commits(self, owner: str, repo: str, branch: str) -> list[dict[str, Any]]:
        """读取指定仓库分支的最新提交列表。"""
        if self._session is None:
            return []

        url = f"https://api.github.com/repos/{owner}/{repo}/commits?sha={branch}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "MaiBot-GitHub-Monitor-Plugin",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

        token = self.config.global_.token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with self._session.get(url, headers=headers) as response:
                if response.status == 200:
                    payload = await response.json()
                    if isinstance(payload, list):
                        return payload
                    self.ctx.logger.warning("GitHub API 返回了非列表数据：%s/%s", owner, repo)
                    return []

                if response.status == 403:
                    self.ctx.logger.warning(
                        "GitHub API 访问受限：%s/%s（可能是速率限制或 Token 权限不足）",
                        owner,
                        repo,
                    )
                    return []

                if response.status == 404:
                    self.ctx.logger.error("仓库或分支不存在：%s/%s@%s", owner, repo, branch)
                    return []

                error_text = await response.text()
                self.ctx.logger.error(
                    "GitHub API 请求失败：%s/%s -> HTTP %s, body=%s",
                    owner,
                    repo,
                    response.status,
                    error_text,
                )
                return []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.error("获取 GitHub 提交失败：%s/%s -> %s", owner, repo, exc, exc_info=True)
            return []

    async def _broadcast_notification(self, commit_item: dict[str, Any], repo_name: str, branch: str) -> None:
        """把新提交广播到所有已配置的订阅群。"""
        subscribers = list(self.config.monitor.subscribers)
        if not subscribers:
            self.ctx.logger.warning("没有配置任何订阅群，跳过本次 GitHub 提交通知")
            return

        sha = str(commit_item.get("sha") or "")[:7]
        commit_block = commit_item.get("commit") or {}
        author_block = commit_block.get("author") or {}
        author = str(author_block.get("name") or "unknown")
        message = str(commit_block.get("message") or "").strip()
        commit_url = str(commit_item.get("html_url") or "").strip()
        base_message = self._build_commit_notification_message(
            repo_name=repo_name,
            branch=branch,
            sha=sha,
            author=author,
            message=message,
            commit_url=commit_url,
        )

        for subscriber in subscribers:
            group_id = subscriber.group_id.strip()
            platform = subscriber.platform.strip() or "qq"
            if not group_id:
                continue

            stream = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform=platform)
            if not stream:
                self.ctx.logger.warning("未找到订阅群对应的聊天流：platform=%s group_id=%s", platform, group_id)
                continue

            stream_id = str(stream.get("stream_id") or "").strip()
            if not stream_id:
                self.ctx.logger.warning("聊天流缺少 stream_id：platform=%s group_id=%s", platform, group_id)
                continue

            # 显式把通知写入 Maisaka 历史，这样后续的点评就能直接参考刚发出去的播报内容。
            sent = await self.ctx.send.text(
                base_message,
                stream_id,
                sync_to_maisaka_history=True,
                maisaka_source_kind="plugin:github_monitor:notification",
            )
            if not sent:
                self.ctx.logger.error("基础提交通知发送失败：platform=%s group_id=%s", platform, group_id)
                continue

            self.ctx.logger.info("已推送 GitHub 提交通知：[%s] -> %s/%s", repo_name, platform, group_id)

            if self.config.global_.enable_commentary:
                await self._trigger_maisaka_commentary(
                    stream_id=stream_id,
                    repo_name=repo_name,
                    branch=branch,
                    sha=sha,
                    author=author,
                    message=message,
                    group_id=group_id,
                    platform=platform,
                )

    def _build_commit_notification_message(
        self,
        *,
        repo_name: str,
        branch: str,
        sha: str,
        author: str,
        message: str,
        commit_url: str,
    ) -> str:
        """构造固定的提交通知文本。"""
        notification_text = (
            f"[{repo_name}] 检测到新提交\n"
            f"分支: {branch}\n"
            f"Commit: {sha}\n"
            f"作者: {author}\n"
            f"说明:\n{message or '(无提交说明)'}"
        )
        if commit_url:
            notification_text += f"\n链接: {commit_url}"
        return notification_text

    def _build_commentary_intent(
        self,
        *,
        repo_name: str,
        branch: str,
        sha: str,
        author: str,
        message: str,
    ) -> str:
        """构造交给 Maisaka 的主动点评意图。"""
        return (
            "请基于刚刚这条 GitHub 提交通知，在当前群里自然接一句简短中文点评。"
            "不要复述整段通知，不要使用 Markdown，最好控制在 1 句、40 字以内。"
            f"\n仓库: {repo_name}"
            f"\n分支: {branch}"
            f"\nCommit: {sha}"
            f"\n作者: {author}"
            f"\n提交说明: {message or '(无提交说明)'}"
        )

    async def _trigger_maisaka_commentary(
        self,
        *,
        stream_id: str,
        repo_name: str,
        branch: str,
        sha: str,
        author: str,
        message: str,
        group_id: str,
        platform: str,
    ) -> None:
        """触发 Maisaka 在当前群里补一句点评。"""
        intent = self._build_commentary_intent(
            repo_name=repo_name,
            branch=branch,
            sha=sha,
            author=author,
            message=message,
        )

        try:
            proactive_result = await self.ctx.maisaka.proactive.trigger(
                stream_id=stream_id,
                intent=intent,
                reason="github_commit_commentary",
                metadata={
                    "source": "github_monitor_plugin",
                    "repo_name": repo_name,
                    "branch": branch,
                    "commit_sha": sha,
                    "author": author,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.error("触发 GitHub 提交点评失败：%s", exc, exc_info=True)
            return

        if isinstance(proactive_result, dict) and proactive_result.get("success") is False:
            self.ctx.logger.warning(
                "Maisaka 未接受 GitHub 提交点评任务：platform=%s group_id=%s result=%s",
                platform,
                group_id,
                proactive_result,
            )
            return

        self.ctx.logger.info("已触发 Maisaka 生成 GitHub 提交点评：[%s] -> %s/%s", repo_name, platform, group_id)


def create_plugin() -> GitHubMonitorPlugin:
    """创建插件实例。"""
    return GitHubMonitorPlugin()
