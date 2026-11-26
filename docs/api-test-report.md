# 阶段 0.5 API 与功能测试报告

> AI 生成草稿，需 Digi 工程师审核。每项：命令、预期、实际、结论。
> 复现：`python scripts/hub_probe.py --hub hub-a read|errors`（密码从 `.env` 读取，不打印）。

## A. Hub REST API（hub-a）

| # | 项目 | 命令 | 预期 | 实际 | 结论 |
|---|---|---|---|---|---|
| A1 | 读取服务配置 | `hub_probe.py --hub hub-a read` | 200 + ok | 20 个子键；enable=true；port=18574（与 config 一致） | PASS |
| A2 | 读取分组列表 | 同上 | 返回分组及端口归属 | group01..08；**8 个端口全在 group01**，其余为空 | PASS |
| A3 | 读取客户端列表 | 同上 | 返回数组 | 空数组 | PASS（条目结构待写入后确认） |
| A4 | 端口数核对 | 同上 | 8 | 8 | PASS |
| A5 | 型号/序列号/固件 | 同上 | AW08 / AW08-017363 | `config.cgi` 下未找到 | **未解决**，另找来源 |
| A6 | 错误键名 | 同上（errors） | 报错 | HTTP **200**，body `{"error": true, "msg": "invalid path ..."}` | PASS（适配器须检查 body） |
| A7 | 错误密码 | `errors` | 401 | HTTP 401（仅试 1 次） | PASS |
| A8 | Hub 不可达 | `errors`（测试地址 10.255.255.1） | 超时异常 | `ConnectTimeout`，4s | PASS |
| A9 | 修改测试端口分组归属，写后读校验 | 先 `POST` 追加 "1" 到 `group02.ports`，再 `DELETE group01.ports.0`（备份 `hub-a-pre-port1-split-20261120-062839.json`） | 端口 1 单独成组 | group02.ports.0="1"；group01 剩 2..8（序号前移）；每步读回一致 | PASS |
| A10 | 添加 vm-win-1 Client ID | `POST /value?path=service.anywhereusb.clients&append=true&collapsed[id]=ukey-vm-win-1`（先备份 `backups/hub-a-pre-client-write-20261120-051126.json`） | 创建条目 | 返回 `clients.0`；读回 `{id, description:"", groups:[]}`；错误字段名被 schema 拒绝且不创建元素 | PASS（VM 侧可见性待测） |
| A11 | 授予分组给 Client ID；VM 是否立即可见 | `POST ...clients.0.groups append value=group02`（后改为 group06/group03） | VM 看到分组并可连接 | 授权约 30 秒内 VM 可见；`CONNECT GROUP,AW08-017363.6` 成功，U 盘在 VM 中出现为 E: | PASS |
| A12 | 撤销授权；已连接时的行为 | `DELETE ...clients.0.groups.<idx>`（group06，U 盘已连接为 E:；备份 `hub-a-pre-revoke-20261126-032912.json`） | 观察 VM 是否断开 | Hub 删除时刻 03:29:13；VM 日志同一秒 `Device 2006 UNBOUND from connection 1`；E: 与磁盘消失；group06 从 `LIST FULL` 中**完全消失**（不再可见） | PASS：**撤销即强制断开**，无需等待 |

写入类（A9–A12）仅在 `test_port`（端口 1）进行，写前备份，写后读回。

## B. Manager 安装与模式（VM，基于快照 S1）

全部待用户执行，结果记入 `docs/manager-install-notes.md`：静默安装、切换服务模式、服务名、配置目录与 ClientId、模式判断、驱动信任提示。

## C. Manager CLI（VM，服务模式）

全部待用户执行，结果记入 `docs/manager-cli-notes.md`：`LIST` / `LIST FULL`、`KNOWN HUB ADD`、`AUTOFIND,OFF`、`CONNECT/DISCONNECT GROUP`、占用/未授权报错、权限要求。

## D. 手动校验：curl 命令示例

与上面的 Python 脚本等价，便于在命令行手动复现。以下命令都是**只读**的。

先准备环境变量（密码不回显、不写入命令历史）：

```bash
cd ~/mygit/ukey-manager
set -a; . ./.env; set +a
HUB_PW="$HUB_PASSWORD_HUB_A"; HUB=10.70.1.208
API="https://$HUB/cgi-bin/config.cgi"
```

**D1. 连通与认证**（对应 A1）：返回 `"ok": true` 即成功。

```bash
curl -sk -u "admin:$HUB_PW" "$API/keys/service/anywhereusb" | python3 -m json.tool
```

**D2. 服务是否启用、监听端口**（对应 A1）：预期 `enable=true`、`port=18574`。

```bash
curl -sk -u "admin:$HUB_PW" "$API/value/service/anywhereusb/enable"
curl -sk -u "admin:$HUB_PW" "$API/value/service/anywhereusb/port"
```

**D3. 分组与端口归属**（对应 A2）：当前 8 个端口都在 `group01`。只过滤出分组相关行：

```bash
curl -sk -u "admin:$HUB_PW" "$API/value/service/anywhereusb" | python3 -m json.tool | grep '"groups\.'
```

**D4. 客户端列表**（对应 A3）：目前为空数组（`"": ""`）。

```bash
curl -sk -u "admin:$HUB_PW" "$API/value/service/anywhereusb/clients" | python3 -m json.tool
```

**D5. 端口数量**（对应 A4）：统计 `device.usb.port.portN` 的个数，预期 8。

```bash
curl -sk -u "admin:$HUB_PW" "$API/value/device" | grep -o 'usb\.port\.port[0-9]*' | sort -u | wc -l
```

**D6. 错误键名：HTTP 仍是 200**（对应 A6）：第一条只看状态码，第二条看 body 里的 `error`。

```bash
curl -sk -u "admin:$HUB_PW" -o /dev/null -w "HTTP %{http_code}\n" "$API/value/service/anywhereusb/no_such_key"
curl -sk -u "admin:$HUB_PW" "$API/value/service/anywhereusb/no_such_key"
```

**D7. 错误密码：返回 401**（对应 A7）：只试一次，不要反复尝试。

```bash
curl -sk -u "admin:wrong-password" -o /dev/null -w "HTTP %{http_code}\n" "$API/keys/service/anywhereusb"
```

**D8. Hub 不可达：超时**（对应 A8）：用不存在的地址，4 秒超时，退出码 28。

```bash
curl -sk -m 4 -u "admin:x" "https://10.255.255.1/cgi-bin/config.cgi/keys"; echo "curl exit=$?"
```

**D9. 只读备份当前 AnywhereUSB 配置**：保存到 `backups/`，写操作前建议先执行。

```bash
curl -sk -u "admin:$HUB_PW" "$API/value/service/anywhereusb" > "backups/hub-a-curl-$(date +%Y%m%d-%H%M%S).json" && chmod 600 backups/hub-a-curl-*.json && ls -l backups/
```
