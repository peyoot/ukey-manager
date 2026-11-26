# 服务器 ⇄ 代理通信协议（阶段 3）

> AI 生成草稿，需 Digi 工程师审核。实现：`common/protocol.py`（消息模型）、`server/ws.py`（服务端）。

传输：`wss://10.70.1.63:8443/ws/agent`，每条消息一个 JSON 文本帧，上限 64 KB。

```json
{"id": "uuid", "type": "CONNECT", "ts": 1763452800, "payload": {...}}
```

## 认证流程

1. **ENROLL**（新代理）：带注册令牌（`.env` 的 `AGENT_ENROLLMENT_TOKEN`）。令牌错 → ACK 失败并关闭（4401），写审计。
   - 服务器以**连接的源 IP** 为准（自报的 `ip` 仅供显示）。源 IP 在 `auto_approve_ips` 中 → 自动批准，否则 `pending`，等控制台批准。
   - `pending`：ACK 成功，`data.status="pending"`，连接关闭；代理定期重发 ENROLL。
   - 已批准且尚未发过 token：ACK `data.token` = **代理专属 token（只发这一次）**，库里只存 SHA-256。
   - 已批准且已发过 token：失败（`already enrolled`），需管理员 `POST /api/agents/{id}/reset-token`。
2. **REGISTER**（每次连接的第一条消息）：带专属 token；失败 → 关闭（4401）。成功 → ACK，随后服务器推送 **CONFIG**。同一代理重连会顶掉旧连接（旧连接关闭码 4000）。

## 消息

| 方向 | type | payload | 备注 |
|---|---|---|---|
| 代理→服务器 | ENROLL | hostname, ip, token | |
| 代理→服务器 | REGISTER | agent_id, token, hostname, client_id, ip, agent_version, manager_version, manager_mode(service/standalone/missing) | `manager_mode` ≠ service 时服务器拒绝分配端口（`manager not ready`） |
| 服务器→代理 | CONFIG | hubs[{name,address,port,serial}], heartbeat_interval_seconds, client_id_pattern | 代理据此 `KNOWN HUB ADD`、`AUTOFIND,OFF` |
| 代理→服务器 | HEARTBEAT | connected_groups[] | 本机已连接分组地址（Manager `LIST`）；持租约却不在其中 → 结束租约 |
| 代理→服务器 | REQUEST / RELEASE | port_id 或 label（二选一） | ACK.data: status(ok/busy/error), port_id, holder |
| 代理→服务器 | EVENT | kind(connected/disconnected/error), port_id, group, detail | 写审计 |
| 服务器→代理 | CONNECT / DISCONNECT | port_id, group(`AW08-017363.6`), hub | 必须 ACK |
| 服务器→代理 | RELEASE_ASK | port_id, requester, requester_host | ACK.data.accept = true/false |
| 服务器→代理 | NOTIFY / RUN_SETUP | text / 空 | 必须 ACK |
| 双向 | ACK | ref_id, ok, error, data | |

## 可靠性规则

- 服务器指令必须 ACK；超时与重试次数取自 `config.yaml` 的 `lease.command_ack_timeout_seconds` / `command_retries`。
- **重试 = 重发同一个消息 id**；接收方按 id 去重：代理对同 id 指令只执行一次；服务器对代理的重复 id 直接重发上次回复，不重复执行。
- 连接断开时，所有等待中的指令立即以 `disconnected` 失败（不用等超时）。
- 协议违规（JSON 错、未知类型、payload 校验失败）→ 返回失败 ACK 并关闭连接（4400）；未在 120 秒内有任何消息 → 关闭（4408）。
- 服务器收消息循环只做分发，REQUEST/RELEASE 在后台任务里处理（否则处理 REQUEST 时下发的 CONNECT 的 ACK 无人读取会死锁）。
- 代理断线后指数退避重连，重连后重新 REGISTER；服务器据 HEARTBEAT 对账（阶段 4 在代理侧实现）。

## 兜底（与调度器配合）

代理断线本身不立刻回收：服务器等 `heartbeat_timeout_seconds` 后由 `sweep()` 直接 `hub.revoke()`（实测：撤销授权会立即强制断开 VM 上的设备）。

## 管理接口（需管理员登录）

`GET /api/agents`、`POST /api/agents/{id}/approve`、`POST /api/agents/{id}/reset-token`、`POST /api/agents/{id}/run-setup`；
`GET /api/ports`、`POST /api/ports/{id}/request|release|force-release`、`GET /api/audit`、`GET /api/hubs`。
给第三方调用的 REST 认证（目前仅管理员会话）留待后续。

## 已知限制（演示级）

- ENROLL 未做按 IP 限速；注册令牌泄露即可让攻击者登记待批准代理（仍需管理员批准）。
- 未实现 mTLS；`wss://` 使用自签证书，代理需信任服务器证书（见阶段 4 安装脚本）。
