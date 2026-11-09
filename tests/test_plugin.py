"""验证通知交付行为；真实 SDK 配置配合模拟聊天接口，不连接外部服务。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import plugin


def commit(sha, message="提交说明", author="Alice"):
    """构造与 GitHub 提交列表一致的最小输入。"""
    return {
        "sha": sha,
        "commit": {"author": {"name": author}, "message": message},
        "html_url": f"https://github.com/owner/repo/commit/{sha}",
    }


@pytest.fixture
def monitor():
    """通过 SDK 正常配置入口初始化插件，仅替换与 Host 通信的接口。"""
    instance = plugin.create_plugin()
    instance.set_plugin_config({
        "plugin": {"config_version": plugin.PLUGIN_CONFIG_VERSION},
        "monitor": {
            "repositories": [{"owner": "owner", "repo": "repo"}],
            "subscribers": [{"group_id": "a"}, {"group_id": "b"}],
        },
    })
    instance._set_context(SimpleNamespace(
        logger=Mock(),
        chat=SimpleNamespace(get_stream_by_group_id=AsyncMock(
            side_effect=lambda group_id, platform: {"stream_id": group_id}
        )),
        send=SimpleNamespace(text=AsyncMock(return_value=True)),
        maisaka=SimpleNamespace(proactive=SimpleNamespace(trigger=AsyncMock(return_value={"success": True}))),
    ))
    instance._fetch_latest_commits = AsyncMock(return_value=[commit("new2"), commit("new1"), commit("old")])
    instance._repo_states["owner/repo/main"] = "old"
    return instance


async def poll(instance):
    """使用与轮询任务相同的仓库配置执行一轮。"""
    await instance._poll_repository(instance.config.monitor.repositories[0])


@pytest.mark.asyncio
async def test_batch_order_and_no_duplicate_poll(monitor):
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2
    assert monitor.ctx.maisaka.proactive.trigger.await_count == 2
    message = monitor.ctx.send.text.await_args.args[0]
    assert "2 条新提交" in message
    assert message.index("new1") < message.index("new2")
    assert "https://github.com/owner/repo/commit/new1" in message
    assert monitor._repo_states["owner/repo/main"] == "new2"
    assert not monitor._pending_notifications
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("send failed")])
async def test_retry_only_failed_group_with_frozen_batch(monitor, failure):
    # 第二个群失败后，即使远端历史已变化，重试仍必须发送原批次。
    monitor.ctx.send.text.side_effect = [True, failure, True]
    await poll(monitor)
    original = monitor.ctx.send.text.await_args.args[0]
    assert monitor._repo_states["owner/repo/main"] == "old"
    assert monitor._pending_notifications["owner/repo/main"].remaining == {("qq", "b")}
    monitor._fetch_latest_commits.return_value = [commit("future"), commit("new2")]
    await poll(monitor)
    assert monitor._fetch_latest_commits.await_count == 1
    assert [call.args[1] for call in monitor.ctx.send.text.await_args_list] == ["a", "b", "b"]
    assert monitor.ctx.send.text.await_args.args[0] == original
    assert monitor._repo_states["owner/repo/main"] == "new2"
    assert monitor.ctx.maisaka.proactive.trigger.await_count == 2
    monitor.ctx.send.text.side_effect = None
    await poll(monitor)
    assert monitor._repo_states["owner/repo/main"] == "future"
    assert monitor.ctx.send.text.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [None, {}, {"stream_id": ""}, RuntimeError("lookup failed")])
async def test_lookup_failures_do_not_block_other_groups(monitor, stream):
    monitor.ctx.chat.get_stream_by_group_id.side_effect = [stream, {"stream_id": "b"}, {"stream_id": "a"}]
    await poll(monitor)
    assert monitor.ctx.send.text.await_args.args[1] == "b"
    assert monitor._pending_notifications["owner/repo/main"].remaining == {("qq", "a")}
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2
    assert not monitor._pending_notifications


@pytest.mark.asyncio
async def test_delivery_timeout_preserves_retry_and_continues(monitor, monkeypatch):
    monkeypatch.setattr(plugin, "DELIVERY_TIMEOUT_SECONDS", 0.01)

    async def send(message, stream_id, **kwargs):
        if stream_id == "a":
            await asyncio.Event().wait()
        return True

    monitor.ctx.send.text.side_effect = send
    await asyncio.wait_for(poll(monitor), timeout=1)
    assert monitor.ctx.send.text.await_count == 2
    assert monitor._pending_notifications["owner/repo/main"].remaining == {("qq", "a")}
    monitor.ctx.send.text.side_effect = None
    await poll(monitor)
    assert not monitor._pending_notifications


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("commentary failed"), {"success": False}])
async def test_commentary_failure_does_not_retry_delivered_notification(monitor, failure):
    monitor.ctx.maisaka.proactive.trigger.side_effect = [failure, failure]
    await poll(monitor)
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2
    assert not monitor._pending_notifications


@pytest.mark.asyncio
async def test_cancel_after_delivery_preserves_success(monitor):
    monitor.ctx.maisaka.proactive.trigger.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await poll(monitor)
    assert monitor._pending_notifications["owner/repo/main"].remaining == {("qq", "b")}
    monitor.ctx.maisaka.proactive.trigger.side_effect = None
    await poll(monitor)
    assert [call.args[1] for call in monitor.ctx.send.text.await_args_list] == ["a", "b"]


@pytest.mark.asyncio
async def test_commentary_timeout_does_not_repeat_notification(monitor, monkeypatch):
    monkeypatch.setattr(plugin, "DELIVERY_TIMEOUT_SECONDS", 0.01)

    async def stalled_commentary(**kwargs):
        await asyncio.Event().wait()

    monitor.ctx.maisaka.proactive.trigger.side_effect = stalled_commentary
    await asyncio.wait_for(poll(monitor), timeout=1)
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2
    assert not monitor._pending_notifications


@pytest.mark.asyncio
async def test_cancel_during_send_preserves_pending(monitor):
    monitor.ctx.send.text.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await poll(monitor)
    assert monitor._repo_states["owner/repo/main"] == "old"
    assert monitor._pending_notifications["owner/repo/main"].remaining == {("qq", "a"), ("qq", "b")}


@pytest.mark.asyncio
async def test_repository_failure_isolated_and_loop_cancel_propagates(monitor, monkeypatch):
    other = plugin.RepositoryConfig(owner="owner", repo="other")
    monitor.config.monitor.repositories.append(other)
    monitor._poll_repository = AsyncMock(side_effect=[RuntimeError("broken repo"), None])

    async def sleep(seconds):
        if seconds == monitor.config.global_.interval:
            raise asyncio.CancelledError

    monkeypatch.setattr(plugin.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor._monitor_loop()
    assert monitor._poll_repository.await_count == 2
    assert monitor._poll_repository.await_args.args[0] == other
    monitor.ctx.logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_initialization_and_missing_baseline_behavior(monitor):
    monitor._repo_states.clear()
    await poll(monitor)
    monitor.ctx.send.text.assert_not_awaited()
    assert monitor._repo_states["owner/repo/main"] == "new2"
    monitor._fetch_latest_commits.return_value = [commit("reset"), commit("other")]
    await poll(monitor)
    expected = monitor._build_commit_notification_message(
        repo_name="repo", branch="main", sha="reset", author="Alice",
        message="提交说明", commit_url="https://github.com/owner/repo/commit/reset",
    )
    assert monitor.ctx.send.text.await_args.args[0] == expected


@pytest.mark.asyncio
async def test_subscriber_reload_and_deduplication(monitor):
    monitor.config.monitor.subscribers.append(plugin.SubscriberConfig(group_id=" a ", platform=" qq "))
    monitor.ctx.send.text.side_effect = [True, False]
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2
    # 移除失败群并新增群：旧批次不追发给新增群，也不会永久卡在已删除目标。
    monitor.config.monitor.subscribers = [plugin.SubscriberConfig(group_id="a"), plugin.SubscriberConfig(group_id="c")]
    await poll(monitor)
    assert monitor.ctx.send.text.await_count == 2
    assert not monitor._pending_notifications
    assert monitor._repo_states["owner/repo/main"] == "new2"


@pytest.mark.asyncio
async def test_empty_subscribers_and_disabled_commentary(monitor):
    monitor.config.global_.enable_commentary = False
    await poll(monitor)
    monitor.ctx.maisaka.proactive.trigger.assert_not_awaited()
    monitor.config.monitor.subscribers = []
    monitor._fetch_latest_commits.return_value = [commit("new3"), commit("new2")]
    await poll(monitor)
    assert monitor._repo_states["owner/repo/main"] == "new3"
    assert not monitor._pending_notifications
    assert monitor.ctx.send.text.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [512, 2000, 4000])
@pytest.mark.parametrize("count", [1, 30])
async def test_long_messages_are_bounded_and_keep_complete_link(monitor, limit, count):
    monitor.config.global_.max_message_chars = limit
    items = [commit(f"{number:040x}", "标题" * 100 + "\n" + "正文" * 5000, "作者" * 100)
             for number in range(count, 0, -1)]
    monitor._fetch_latest_commits.return_value = items + [commit("old")]
    await poll(monitor)
    text = monitor.ctx.send.text.await_args.args[0]
    assert len(text) <= limit
    assert "内容已压缩" in text
    assert text.endswith(f"https://github.com/owner/repo/commits/{count:040x}")
    assert "正文" not in text
    if count == 30:
        assert "条提交未展示" in text


def test_pathological_fields_still_respect_length(monitor):
    monitor.config.global_.max_message_chars = 512
    text = monitor._limit_notification(
        "x" * 10000,
        [{"sha": "a" * 7, "author": "A", "message": "title", "commit_url": ""}],
        "owner" * 1000, "repo" * 1000, "branch" * 1000, "b" * 40,
    )
    assert len(text) <= 512
    assert "https://github.com" in text


@pytest.mark.parametrize("alias", ["global", "global_"])
def test_config_backwards_compatibility_and_validation(alias):
    instance = plugin.create_plugin()
    instance.set_plugin_config({
        "plugin": {"config_version": plugin.PLUGIN_CONFIG_VERSION},
        alias: {"interval": 90, "token": "example", "enable_commentary": False},
    })
    assert instance.config.global_.interval == 90
    assert instance.config.global_.token == "example"
    assert instance.config.global_.max_message_chars == 2000
    for bad_value in (511, 4001, "not-a-number"):
        with pytest.raises(ValidationError):
            plugin.GitHubMonitorConfig.model_validate({alias: {"max_message_chars": bad_value}})


@pytest.mark.asyncio
async def test_lifecycle_closes_session_and_retains_in_memory_state(monitor):
    await monitor.on_load()
    session = monitor._session
    task = monitor._monitor_task
    await monitor.on_unload()
    assert session.closed
    assert task.done()
    assert monitor._session is None
    assert monitor._monitor_task is None
    assert monitor._repo_states["owner/repo/main"] == "old"


def test_manifest_dependencies_match_installed_versions():
    """依赖约束必须覆盖当前验证环境，不靠主程序碰巧装有依赖。"""
    from importlib.metadata import version

    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    manifest = json.loads((Path(__file__).parents[1] / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    for dependency in manifest["dependencies"]:
        assert version(dependency["name"]) in SpecifierSet(dependency["version_spec"])
    assert Version(manifest["sdk"]["min_version"]) <= Version(version("maibot-plugin-sdk")) <= Version(manifest["sdk"]["max_version"])


def test_readme_example_loads_and_sdk_generates_schema():
    """公开配置示例必须能通过 SDK 的完整加载路径。"""
    import tomllib

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    example = readme.split("```toml\n", 1)[1].split("```", 1)[0]
    instance = plugin.create_plugin()
    instance.set_plugin_config(tomllib.loads(example))
    assert instance.config.global_.max_message_chars == 2000
    assert instance.config.monitor.repositories[0].branch == "main"
    assert instance.get_webui_config_schema()


@pytest.mark.asyncio
async def test_branch_is_passed_as_query_parameter(monitor):
    """分支中的斜线与 & 必须由 HTTP 库编码，不能拼接成额外查询参数。"""
    response = SimpleNamespace(status=200, json=AsyncMock(return_value=[commit("new")]))
    request = AsyncMock()
    request.__aenter__.return_value = response
    monitor._session = SimpleNamespace(get=Mock(return_value=request))
    result = await plugin.GitHubMonitorPlugin._fetch_latest_commits(monitor, "owner", "repo", "feature/a&b")
    assert result == [commit("new")]
    assert monitor._session.get.call_args.kwargs["params"] == {"sha": "feature/a&b"}
