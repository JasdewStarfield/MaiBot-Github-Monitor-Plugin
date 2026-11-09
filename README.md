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

需要 Python 3.12+、MaiBot Host 1.4.3–1.4.99、maibot-plugin-sdk 2.22.1–2.22.99。

插件所需的 Python 依赖由 MaiBot 自动解析和安装。

手动安装：

1. 将本仓库克隆或解压到当前 MaiBot 实例使用的插件目录中，保持 `_manifest.json`、`plugin.py`、`LICENSE` 位于该插件文件夹根目录。
2. 在 MaiBot WebUI 中扫描、加载插件，确认依赖安装和兼容校验成功。
3. 编辑插件配置，填写仓库和订阅群，保存配置。
4. 首次加载等待约 10 秒建立提交基线，再向监控分支推送新提交验证通知。

目标群需要已在 MaiBot 中建立聊天流，机器人也需要能向该群发送消息。插件不会自动加入群聊。

## 配置

推荐通过 MaiBot WebUI 编辑插件配置，也可以修改插件生成的 `config.toml`。以下示例中的仓库与群 ID 请替换为自己的目标：

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
| `plugin.enable` | `true` | 是否启用仓库监控 |
| `plugin.config_version` | `"2.0.0"` | 配置格式版本，通常无需手动修改 |
| `global_.token` | `""` | 可选 GitHub Token；空值使用匿名请求，私有仓库需要具备对应读取权限的 Token |
| `global_.interval` | `60` | 每轮结束后等待的秒数，最小为 10；仓库间另有 1 秒间隔 |
| `global_.enable_commentary` | `true` | 每批成功通知后触发一次点评；希望总共只发一条消息时设为 `false` |
| `global_.max_message_chars` | `2000` | 基础通知字符上限，允许 512–4000；平台实际限制可能不同 |
| `monitor.repositories` | `[]` | 监控列表；`owner`、`repo` 默认空，空值跳过；`branch` 默认 `main` |
| `monitor.subscribers` | `[]` | 订阅列表；`group_id` 默认空，空值跳过；`platform` 默认 `qq` |

兼容旧配置中的 `[global]` 别名，使用 `[global_]` 或 `[global]` 其中一个即可。旧配置未填写 `max_message_chars` 时使用 2000；填写超出范围或格式不正确的值会报配置错误。

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

## 使用说明

- 首次启动只记录最新提交，之后开始通知新更新；不同仓库、不同分支的通知分别发送。
- 发送失败的群会在后续轮询中重试，已成功群不重复发送。持续失败的群会延迟该仓库后续通知，其他仓库继续监控。
- 点评失败不会重发基础通知。如果平台已收到消息但确认结果丢失，重试仍可能产生重复消息。
- 保存配置后，移除的订阅群不再重试，新加入的群从下一批开始接收；没有有效订阅群时跳过通知。
- 重启后不会恢复待发送通知，也不会补发停机期间的更新。更新积压过多或仓库历史被改写时，可能只通知最新一条提交。

## 常见问题

- **无通知**：确认插件启用、仓库和分支有效、群聊流存在，并在首次建立基线后产生新提交；没有有效仓库或订阅群时不会发送。
- **HTTP 401 / 403**：查看日志中的鉴权、权限或限流原因，检查 Token、私有仓库读取授权，必要时增大轮询间隔。
- **HTTP 404**：检查仓库、分支和私有仓库访问权限。
- **消息发送失败**：检查机器人状态、平台限制及目标聊天流；长消息可降低 `max_message_chars`。失败目标保留待重试状态。
- **版本不兼容**：检查 Host 和 Runner 的实际 SDK 版本。升级到声明范围之外时应先验证并调整兼容范围。

## 许可证

[MIT](LICENSE)。反馈问题请使用 [GitHub Issues](https://github.com/JasdewStarfield/MaiBot-Github-Monitor-Plugin/issues)。
