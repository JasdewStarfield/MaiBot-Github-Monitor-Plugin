# GitHub 监控插件

定期检查 GitHub 仓库指定分支的提交，将同一次轮询发现的多条更新合并推送到 MaiBot 群聊，可选让 Maisaka 补一句简短点评。

## 功能

- 监控多个仓库和分支，向多个平台的群聊发送通知。
- 同一仓库分支的新提交按旧到新合并为一条消息，短消息保留作者、完整提交说明和链接。
- 长消息自动缩为标题摘要，并提供完整提交记录链接。
- 失败群在下一轮重试，已成功群不会因其他群失败而重复收到该批通知。
- 支持配置热重载，以及 GitHub Token、鉴权与限流诊断。

本插件监控分支提交，不监控 Issue、PR 和 GitHub Release。

## 环境与安装

当前源码面向 Python 3.12+、MaiBot Host 1.4.3–1.4.99、maibot-plugin-sdk 2.22.1–2.22.99。
版本范围依据 [MaiBot 官方 main 分支依赖](https://github.com/Mai-with-u/MaiBot/blob/main/pyproject.toml) 收窄；真实 Host 加载和群聊发送仍需在部署环境验收，不能仅凭版本声明判断已通过实测。

Python 直接依赖通过 `_manifest.json` 声明：`aiohttp>=3.14.3,<4`、`pydantic>=2.11.7,<3`。由 MaiBot 的插件依赖流程解析和安装；SDK 由 Host/Runner 环境提供。

当前源码尚未发布到插件市场。手动安装：

1. 将本仓库克隆或解压到当前 MaiBot 实例使用的插件目录中，保持 `_manifest.json`、`plugin.py`、`LICENSE` 位于该插件文件夹根目录。
2. 在 MaiBot WebUI 中扫描、加载插件，确认依赖安装和兼容校验成功。
3. 编辑插件配置，填写仓库和订阅群，保存配置。
4. 首次加载等待约 10 秒建立提交基线，再向监控分支推送新提交验证通知。

目标群需要已在 MaiBot 中建立聊天流，机器人也需要能向该群发送消息。插件不会自动加入群聊。

## 配置

推荐通过 WebUI 编辑。Runner 根据配置模型生成 `config.toml`。以下示例中的仓库与群 ID 请替换为自己的目标：

```toml
[plugin]
enable = true
config_version = "2.0.0"

[global_]
token = ""
interval = 60
enable_commentary = true
max_message_chars = 2000

[[monitor.repositories]]
owner = "JasdewStarfield"
repo = "MaiBot-Github-Monitor-Plugin"
branch = "main"

[[monitor.subscribers]]
group_id = "123456789"
platform = "qq"
```

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enable` | `true` | 开关；关闭会取消后台任务并关闭 HTTP 会话 |
| `plugin.config_version` | `"2.0.0"` | 配置格式版本；新增可选字段无需迁移旧配置 |
| `global_.token` | `""` | 可选 GitHub Token；空值使用匿名请求，私有仓库需要具备对应读取权限的 Token |
| `global_.interval` | `60` | 每轮结束后等待的秒数，最小为 10；仓库间另有 1 秒间隔 |
| `global_.enable_commentary` | `true` | 每批成功通知后触发一次点评；希望总共只发一条消息时设为 `false` |
| `global_.max_message_chars` | `2000` | 基础通知字符上限，允许 512–4000；平台实际限制可能不同 |
| `monitor.repositories` | `[]` | 监控列表；`owner`、`repo` 默认空，空值跳过；`branch` 默认 `main` |
| `monitor.subscribers` | `[]` | 订阅列表；`group_id` 默认空，空值跳过；`platform` 默认 `qq` |

兼容旧配置中的 `[global]` 别名，使用 `[global_]` 或 `[global]` 其中一个即可。旧配置未填写 `max_message_chars` 时使用 2000；越界值或不可解析的类型由配置模型报错，不会静默修改为另一个值。

所有监控项共用订阅群列表。同一平台和群 ID 的重复配置只发送一次。Token 属于本地运行配置，请勿提交到仓库。

## 通知示例

```text
[example] 检测到 2 条新提交
分支: main

1. abc1234 | Alice
修复配置加载
链接: https://github.com/owner/example/commit/abc1234

2. def5678 | Bob
增加通知汇总
链接: https://github.com/owner/example/commit/def5678
```

超过字符上限时改用提交标题摘要，省略正文及逐条链接；若标题列表仍过长，会显示未展示条数，并保留本批末尾提交的历史记录链接。字符上限包含链接与省略提示，不限制 Maisaka 另外生成的点评。

## 轮询与失败处理

- 每个仓库分支独立建立基线，首次运行不补发历史提交。同一轮询发现的提交合并；不同分支、不同仓库不合并。
- 当前只读取 GitHub 返回的第一页提交。旧 SHA 未出现在本页时，沿用只通知最新一条的行为并记录警告；这不保证补齐全部历史更新。
- 检测到更新后固定本批提交和目标群，所有仍有效的目标成功后才推进 SHA。失败、缺少聊天流或查询异常的群会在下一轮重试，已成功群不会重发。
- 同一仓库有待发送批次时，先重试该批，完成后的下一轮再读取后续提交。一个持续失败的群会延迟该仓库后续更新；其他仓库继续轮询。长期积压仍受上述第一页限制影响。
- 群聊查询和发送合计最多等待 30 秒；点评另有 30 秒超时。普通异常记录日志并继续其他群或仓库，插件卸载时取消会正常传播。
- 点评失败不会重发基础通知，也不单独重试点评。如果平台已收到消息但返回结果丢失或超时，重试仍可能产生重复；插件以发送接口确认成功为准。
- 热重载移除的订阅群不再重试，新加入的群从下一批开始接收。没有有效订阅群时跳过本批并推进基线。移除监控项后该仓库停止轮询；当前进程内重新加入会继续原有状态。
- 基线和待发送状态仅保存在内存。同一实例停用再启用可保留状态；重启、卸载后创建新实例会重新建立基线，未完成批次和停机期间更新不会补发。

## 常见问题

- **无通知**：确认插件启用、仓库和分支有效、群聊流存在，并在首次建立基线后产生新提交；没有有效仓库或订阅群时不会发送。
- **HTTP 401 / 403**：查看日志中的鉴权、权限或限流原因，检查 Token、私有仓库读取授权，必要时增大轮询间隔。
- **HTTP 404**：检查仓库、分支和私有仓库访问权限。
- **消息发送失败**：检查机器人状态、平台限制及目标聊天流；长消息可降低 `max_message_chars`。失败目标保留待重试状态。
- **版本不兼容**：检查 Host 和 Runner 的实际 SDK 版本。升级到声明范围之外时应先验证并调整兼容范围。

## 开发验证

在隔离环境安装声明依赖、`maibot-plugin-sdk==2.22.1`、`pytest` 和 `pytest-asyncio` 后运行：

```sh
python -m pytest -q
```

自动化测试使用真实 SDK 配置模型和模拟聊天接口，不向真实群聊发消息。发布前还需在 MaiBot 中验证加载、热重载、卸载以及平台实际发送效果。

## 许可证

[MIT](LICENSE)。反馈问题请使用 [GitHub Issues](https://github.com/JasdewStarfield/MaiBot-Github-Monitor-Plugin/issues)。
