import asyncio
import aiohttp
import logging
from typing import List, Tuple, Type, Dict

# 导入基础组件
from src.plugin_system import BasePlugin, register_plugin, ComponentInfo, ConfigField

from src.plugin_system.apis import send_api, chat_api, generator_api

PLUGIN_CONFIG_VERSION = "1.1.2"

@register_plugin
class GitHubMonitorPlugin(BasePlugin):
    """GitHub 仓库监控插件 - 定期扫描新 Commit 并通知"""

    # --- 插件基础信息 ---
    plugin_name = "github_monitor_plugin"
    enable_plugin = True
    dependencies = []
    # 声明依赖 aiohttp，确保环境中有安装 (pip install aiohttp)
    python_dependencies = ["aiohttp"] 
    config_file_name = "config.toml"

    # --- 配置 Schema (自动生成配置文件) ---
    config_schema = {
        "plugin": {
            "enable": ConfigField(
                type=bool,
                default=True,
                description="是否启用监控；关闭则本插件无效"
            ),
            "config_version": ConfigField(
                type=str,
                default=PLUGIN_CONFIG_VERSION,
                description="配置文件版本号，请勿修改！",
                disabled=True
            ),
        },
        "global": {
            "token": ConfigField(
                type=str,
                default="",
                description="GitHub Token，选填；建议填写以提高 API 限额 (5000次/小时)",
                input_type="password",
                required=False
            ),
            "interval": ConfigField(
                type=int,
                default=60,
                min=10,
                description="轮询间隔 (秒)"
            ),
            "enable_commentary": ConfigField(
                type=bool,
                default=True,
                description="启用 AI 锐评；开启后，Bot 将根据 Commit 内容自动生成一句评论"
            ),
        },
        "monitor": {
            "repositories": ConfigField(
                type=list,
                item_type="object",
                item_fields={
                    "owner": ConfigField(
                        type=str, 
                        default="", 
                        required=True, 
                        description="仓库拥有者 (Owner)"
                    ),
                    "repo": ConfigField(
                        type=str, 
                        default="", 
                        required=True, 
                        description="仓库名称 (Repo)"
                    ),
                    "branch": ConfigField(
                        type=str, 
                        default="master", 
                        description="分支 (Branch)"
                    )
                },
                default=[
                    {"owner": "torvalds", "repo": "linux", "branch": "master"},
                    {"owner": "python", "repo": "cpython", "branch": "main"}
                ],
                description="监控的仓库列表"
            ),
            "subscribers": ConfigField(
                type=list,
                item_type="object",
                item_fields={
                    "group_id": ConfigField(
                        type=str, 
                        default="114514", 
                        required=True, 
                        description="群ID"
                    ),
                    "platform": ConfigField(
                        type=str, 
                        default="qq", 
                        description="所属平台"
                    )
                },
                default=[
                    {"group_id": "12345678", "platform": "qq"},
                    {"group_id": "87654321", "platform": "wechat"}
                ],
                description="接收通知的群组列表 (包含 group_id, platform)"
            ),
        }
    }

    # --- 配置分节元数据 ---
    config_section_descriptions = {
        "plugin": "插件属性",
        "global": "全局设置",
        "monitor": "监控任务",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor_task = None
        self.logger = logging.getLogger(self.plugin_name)

        self.repo_states: Dict[str, str] = {}

        if not self.get_config("plugin.enable", True):
            self.logger.info(f"[{self.plugin_name}] GitHub 监控插件未启用，跳过启动监控任务。")
            return

        # 启动后台监控任务
        self.monitor_task = asyncio.create_task(self.monitor_loop())

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        # 此插件主要靠后台任务运行，没有注册额外的 Action 或 Command 组件
        return []

    async def get_latest_commits(self, session, owner, repo, branch, token):
        """获取 GitHub Commit"""
        url = f"https://api.github.com/repos/{owner}/{repo}/commits?sha={branch}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        if token:
            headers["Authorization"] = f"token {token}"
        
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    self.logger.debug(f"[{self.plugin_name}] 成功获取 {owner}/{repo} 最新commit")
                    return await response.json()
                elif response.status == 403:
                    self.logger.warning(f"[{self.plugin_name}] GitHub API 速率限制或无权访问 {owner}/{repo} (Status 403)。请检查 Token。")
                    return None
                elif response.status == 404:
                    self.logger.error(f"[{self.plugin_name}] 仓库不存在: {owner}/{repo}/{branch}")
                    return None
                else:
                    self.logger.error(f"[{self.plugin_name}] GitHub API Error {response.status}: {owner}/{repo}")
                    return None
        except Exception as e:
            self.logger.error(f"[{self.plugin_name}] 网络请求失败 {owner}/{repo}: {e}")
            return None

    async def monitor_loop(self):
        """主监控循环"""
        self.logger.info(f"[{self.plugin_name}] GitHub 监控任务已启动... 10秒后开始获取Commit")
        
        # 等待几秒确保配置已加载且 Bot 就绪
        await asyncio.sleep(10)
        
        async with aiohttp.ClientSession() as session:
            while True:
                interval = self.get_config("global.interval", 60)
                token = self.get_config("global.token", "")
                repos = self.get_config("monitor.repositories", [])

                if not repos:
                    # 如果没有配置任务，待机
                    self.logger.warning(f"[{self.plugin_name}] 未配置任何仓库，等待配置...")
                    await asyncio.sleep(interval)
                    continue
                
                for repo_conf in repos:
                    # 安全获取字段
                    owner = repo_conf.get("owner")
                    repo_name = repo_conf.get("repo")
                    branch = repo_conf.get("branch", "master")

                    if not owner or not repo_name:
                        continue

                    # 生成唯一标识符 Key
                    repo_key = f"{owner}/{repo_name}/{branch}"

                    commits = await self.get_latest_commits(session, owner, repo_name, branch, token)
                    if not commits or not isinstance(commits, list) or len(commits) == 0:
                        continue

                    current_latest_sha = commits[0]['sha']

                    if repo_key not in self.repo_states:
                        # 第一次扫描到该仓库 -> 初始化状态，不发送通知
                        self.repo_states[repo_key] = current_latest_sha
                        self.logger.info(f"[{self.plugin_name}] 监控初始化: {repo_key} -> {current_latest_sha[:7]}")

                    elif current_latest_sha != self.repo_states[repo_key]:
                        # 发现更新
                        last_sha = self.repo_states[repo_key]
                        new_items = []
                        i = 0
                        for commit in commits:
                            if commit['sha'] == last_sha:
                                break
                            new_items.append(commit)
                            i += 1

                        self.logger.debug(f"[{self.plugin_name}] {repo_key} 发现 {i} 个新 Commit")
                        
                        self.repo_states[repo_key] = current_latest_sha

                        # 发送通知 (倒序: 旧 -> 新)
                        for item in reversed(new_items):
                            await self.broadcast_notification(item, repo_name, branch)
                            await asyncio.sleep(1)  # 避免短时间内发送过多消息
                    else:
                        self.logger.debug(f"[{self.plugin_name}] {repo_key} 无新 Commit")
                    
                    await asyncio.sleep(1)

                # 轮询间隔
                await asyncio.sleep(interval)

    async def broadcast_notification(self, commit_item, repo_name, branch):
        """广播通知到所有指定群"""
        sha = commit_item['sha'][:7]
        author = commit_item['commit']['author']['name']
        message = commit_item['commit']['message']

        base_msg = (
            f"📢 [{repo_name}] 检测到新提交！\n"
            f"Commit sha: {sha}\n"
            f"提交者: {author}\n"
            f"简介:\n"
            f"{message}"
        )

        subscribers = self.get_config("monitor.subscribers", [])
        enable_ai = self.get_config("monitor.enable_commentary", True)

        if not subscribers:
            return

        for sub in subscribers:
            group_id = sub.get("group_id")
            platform = sub.get("platform", "qq")

            if not group_id:
                continue

            stream = chat_api.get_stream_by_group_id(group_id=str(group_id), platform=platform)

            if not stream:
                self.logger.warning(f"[{self.plugin_name}] 找不到聊天流: {group_id}")
                continue
            
            try:
                await send_api.text_to_stream(
                    text=base_msg,
                    stream_id=stream.stream_id,
                    typing=False,
                    storage_message=True
                )
                self.logger.info(f"[{self.plugin_name}] 已广播更新 [{repo_name}] -> 群 {group_id}")
            except Exception as e:
                self.logger.error(f"[{self.plugin_name}] 发送消息到群 {group_id} 失败: {e}")

            ai_comment = ""
            
            if enable_ai:
                try:
                    # 构建给 Bot 的上下文信息
                    # 我们告诉 Bot 这是一个 GitHub 提交，让它进行评价
                    extra_context = (
                        f"检测到 GitHub 仓库 {repo_name} 有新的代码提交。\n"
                        f"提交者: {author}\n"
                        f"提交信息:\n"
                        f"{message}"
                    )

                    # 调用生成器 API
                    # generate_reply 优先使用 chat_stream
                    success, llm_response = await generator_api.rewrite_reply(
                        chat_stream=stream,
                        raw_reply=extra_context,
                        reason="请根据提交信息用简短、有趣的风格评价一下这个提交。",
                        enable_chinese_typo=False
                    )

                    if success and llm_response:
                        # llm_data.content 包含原始生成的文本
                        ai_comment = llm_response.content
                        self.logger.info(f"[{self.plugin_name}] 为 {repo_name} 的更新生成了评价: {ai_comment}...")

                except Exception as e:
                    self.logger.error(f"[{self.plugin_name}] AI Generation Failed: {e}")
                    # 如果生成失败，仅发送基础消息，不中断流程
                    pass
            try:
                if ai_comment != "":
                    await send_api.text_to_stream(
                        text=ai_comment,
                        stream_id=stream.stream_id,
                        typing=False,
                        storage_message=True
                    )
            except Exception as e:
                self.logger.error(f"[{self.plugin_name}] 发送消息到群 {group_id} 失败: {e}")

    def __del__(self):
        # 插件卸载时取消任务
        if self.monitor_task:
            self.monitor_task.cancel()