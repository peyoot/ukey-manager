# AnywhereUSB Plus 本地 REST API 探测笔记（hub-a 实测）

> 用途：演示与手动校验。读者可以照着下面的命令，在命令行里复现每一个结论。
> 状态：AI 生成草稿，需 Digi 工程师审核后再对外展示。
> 探测对象：hub-a，`10.70.1.208`，AW08（8 口）。探测日期 2026-11-20，**全部为只读 GET，未对 Hub 做任何写入**。
> 配置备份：`backups/hub-a-20261120-033138.json`（不提交仓库）。

---

## 1. 准备：密码从环境变量读取（不回显、不写进命令历史）

```bash
cd ~/mygit/ukey-manager
read -rs -p "Hub admin 密码: " HUB_PW; echo; export HUB_PW
HUB=10.70.1.208
```

也可以从 `.env` 载入（同样不会打印）：

```bash
set -a; . ./.env; set +a; HUB_PW="$HUB_PASSWORD_HUB_A"; HUB=10.70.1.208
```

后面的命令都用 `$HUB_PW`，不要把密码直接写在命令行里。

## 2. API 基本规则

| 项 | 说明 |
|---|---|
| 基址 | `https://<hub>/cgi-bin/config.cgi` |
| 认证 | HTTP Basic（用户名 `admin`） |
| 证书 | Hub 自签，curl 用 `-k`，Python 用 `verify=False`（仅限演示环境） |
| 读取子键 | `GET /keys/<路径>`，路径用 `/` 分隔，如 `service/anywhereusb/groups` |
| 读取值 | `GET /value/<路径>` |
| 成功 | `{"ok": true, "result": ...}` |
| 路径不存在 | **HTTP 仍为 200**，body 是 `{"error": true, "msg": "invalid path '...'"}` |

要点：判断成败必须检查 body 里的 `ok` / `error`，不能只看 HTTP 状态码。

`/value/` 对对象返回扁平化的 `collapsed` 字典（键为点分路径），对字符串返回 `value`，对数组同样返回 `collapsed`。

## 3. 探测命令（curl 版）

下面每条命令都是只读的。需要更易读的输出时，在末尾加 `| python3 -m json.tool`。

```bash
# 3.1 连通与认证：返回 {"ok": true, "result": [...]} 即成功
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/keys/service/anywhereusb"
```

```bash
# 3.2 AnywhereUSB 服务全部配置（启用状态、端口、分组、客户端等）
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/service/anywhereusb" | python3 -m json.tool
```

```bash
# 3.3 分组列表与单个分组的结构
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/keys/service/anywhereusb/groups"
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/keys/service/anywhereusb/groups/group01"
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/service/anywhereusb/groups/group01"
```

```bash
# 3.4 客户端列表（数组；空数组时 collapsed 为 {"": ""}）
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/service/anywhereusb/clients" | python3 -m json.tool
```

```bash
# 3.5 服务开关与监听端口
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/service/anywhereusb/enable"
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/service/anywhereusb/port"
```

```bash
# 3.6 设备 USB 端口拓扑（确认 8 个端口）与固件、schema 版本
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/device" | python3 -m json.tool
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/firmware"
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/schema/version"
```

```bash
# 3.7 顶层配置树有哪些分支
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/keys"
```

```bash
# 3.8 只看 HTTP 状态码（演示"路径错误仍返回 200"）
curl -sk -u "admin:$HUB_PW" -o /dev/null -w "%{http_code}\n" "https://$HUB/cgi-bin/config.cgi/value/system/model"
curl -sk -u "admin:$HUB_PW" "https://$HUB/cgi-bin/config.cgi/value/system/model"
```

## 4. 探测命令（Python 版）

在仓库根目录、已激活 venv 的环境下运行（依赖 `httpx`，已在 `requirements-server.txt` 中）。

```bash
source .venv/bin/activate
python - <<'EOF'
import json, os, httpx

HUB = "10.70.1.208"
PW = os.environ["HUB_PW"]            # 第 1 节已导出，不会打印
c = httpx.Client(
    base_url=f"https://{HUB}/cgi-bin/config.cgi",
    auth=("admin", PW),
    verify=False,                    # 自签证书，仅限演示
    timeout=15,
)

def get(kind, path):
    """kind: 'keys' 或 'value'。检查 body 而不是只看状态码。"""
    r = c.get(f"/{kind}/{path}")
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"{path}: {body.get('msg')}")
    return body["result"]

print("服务子键:", get("keys", "service/anywhereusb"))
v = get("value", "service/anywhereusb")["collapsed"]
print("服务启用:", v["enable"], " 端口:", v["port"])
print("分组:", get("keys", "service/anywhereusb/groups"))
for k, val in sorted(v.items()):
    if k.startswith("groups."):
        print(" ", k, "=", val)
print("客户端:", get("value", "service/anywhereusb/clients")["collapsed"])
EOF
```

一次性导出完整备份（只读；与阶段 0 保存备份的做法一致）：

```bash
python - <<'EOF'
import json, os, time, httpx
c = httpx.Client(base_url="https://10.70.1.208/cgi-bin/config.cgi",
                 auth=("admin", os.environ["HUB_PW"]), verify=False, timeout=15)
paths = ["service/anywhereusb", "device", "schema/version", "firmware"]
data = {p: c.get(f"/value/{p}").json() for p in paths}
fn = f"backups/hub-a-manual-{time.strftime('%Y%m%d-%H%M%S')}.json"
json.dump({"taken_at": time.strftime("%F %T"), "values": data}, open(fn, "w"), ensure_ascii=False, indent=1)
os.chmod(fn, 0o600); print(fn)
EOF
```

## 5. 实测结果（hub-a）

### 5.1 `service.anywhereusb` 子键

acl, autoreg, client_block_duration, clients, custom_config, enable, groups, identity,
keep_alive_interval, keep_alive_timeout, mdns, minimum_tls_version, port, power_cycle_on_unbind,
protocol, tls_cipher_list, tls_ciphersuites, usb_allow_list, usb_dev_quirks, usb_ignore_list

### 5.2 当前取值（节选）

| 键 | 值 | 备注 |
|---|---|---|
| enable | `true` | 服务已启用 |
| port | `18574` | 与 `config.yaml` 一致 |
| protocol.0 | `tcp` | |
| autoreg.enable | `false` | |
| client_block_duration | `10m` | |
| keep_alive_interval / keep_alive_timeout | `3s` / `20s` | |
| minimum_tls_version | `TLS-1_2` | |
| power_cycle_on_unbind.enable | `true` | 解绑时端口会断电重启，演示时留意对 UKey 的影响 |
| mdns.enable | `true` | |

### 5.3 分组

- 预置 `group01` … `group08`，每组有 `description` 和 `ports`（数组，元素为端口号字符串）。
- **当前 8 个端口全部在 `group01`**（`groups.group01.ports.0..7` = "1".."8"），`group02..08` 的 ports 为空。
- 本项目的"每口一组"需要把端口分到各自的分组里（写法待阶段 0.5 验证）。

### 5.4 客户端列表

- `clients` 是数组，目前为空。
- 条目的字段名（Client ID）与"分组访问"的格式，要在端口 1 上做一次受控写入后才能看到。

### 5.5 设备信息

- `device.usb.port.port1..port8` 共 8 个端口，每口两条 USB 路径（USB 2 / USB 3），确认为 8 口设备。
- `schema.version` = `1312`。`firmware.version`、`system.name`、`system.description` 均为空。
- 型号（AW08）、序列号（AW08-017363）、固件版本在 `config.cgi` 下**没有找到**；`system.model`、`system.serial`、`system.firmware` 均返回 invalid path，`status.cgi` 为 404。

### 5.6 配置树顶层分支

action, actiond, auth, cloud, device, firewall, firmware, modem, monitoring, network, schema, service, system, vpn。
按项目安全边界，除 `service.anywhereusb` 外**不得写入**。

## 6. 仍不确定的点（阶段 0.5 解决）

| # | 问题 | 计划 |
|---|---|---|
| 1 | `clients` 条目字段名与分组访问格式 | 在 test_port（端口 1）上受控写入，写前备份，写后读回 |
| 2 | 数组与分组 ports 的写法（POST / PUT / DELETE 路径，是否需要提交动作） | 同上 |
| 3 | 分组名能否自定义为 `hub-a-p01`，还是只能用 `groupNN` + description | 写入 description 验证 |
| 4 | 型号、序列号、固件版本的读取来源 | 先看 vm-win-1 上 Manager 的 `LIST FULL`；必要时再探测其他路径 |
| 5 | 撤销授权时已连接的客户端会怎样 | 阶段 0.5 实测 |
| 6 | 连接状态（哪个客户端占用了哪个分组）从哪里读 | 待查 |

## 7. 演示讲解提示

1. 先跑 3.1：一条 curl 证明"服务器可以用 REST 直接管理 Hub"。
2. 再跑 3.2 / 3.3：展示"分组"和"端口归属"，说明调度单元是一个分组对应一个端口。
3. 跑 3.8：说明"错误也返回 200"，所以服务器端要检查 body，这是适配器里必须处理的细节。
4. 强调本阶段只读；写操作只会发生在 test_port，且先备份、写后读回。
