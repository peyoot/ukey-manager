"""真实 Hub：AnywhereUSB Plus 本地 REST API（/cgi-bin/config.cgi）。

依据阶段 0.5 实测（docs/hub-api-notes.md、Hub 自带规格 docs/hub-config-cgi-spec.json）：
- GET  /value/<点路径用 / 分隔>        读；对象返回扁平 `collapsed`
- POST /value?path=..&value=..         写单值；`append=true` 追加数组元素（返回新元素路径）
- DELETE /value?path=<数组>.<序号>     删除数组元素（删除后序号前移，所以每次删除前都重读）
- 错误也返回 HTTP 200，body 为 {"error": true, "msg": ...}；认证失败才是 401
- 写入立即生效，无需提交
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .base import (
    Client,
    Group,
    HubAdapter,
    HubApiError,
    HubAuthError,
    HubError,
    HubHealth,
    HubIdentity,
    HubUnreachable,
    HubVerifyError,
    HubWriteNotAllowed,
    ServiceStatus,
    port_for_group,
)

BASE = "/cgi-bin/config.cgi"
SVC = "service.anywhereusb"
MAX_CLIENTS = 255  # 每台 Hub 客户端条目上限


@dataclass
class Snapshot:
    enabled: bool
    port: int
    groups: dict[str, Group]
    clients: list[Client]


def _ordered(values: dict[int, object], what: str) -> list:
    """数组键必须连续 0..n-1，否则按序号删除不安全。"""
    if sorted(values) != list(range(len(values))):
        raise HubApiError(f"{what} 的数组序号不连续: {sorted(values)}")
    return [values[i] for i in range(len(values))]


def parse_snapshot(collapsed: dict[str, str]) -> Snapshot:
    """解析 `GET /value/service/anywhereusb` 的 collapsed 字典。空数组表现为键 ""，忽略。"""
    gdesc: dict[str, str] = {}
    gports: dict[str, dict[int, int]] = {}
    cl: dict[int, dict] = {}
    for key, val in collapsed.items():
        if not key:
            continue
        p = key.split(".")
        if p[0] == "groups" and len(p) >= 3:
            gdesc.setdefault(p[1], "")
            gports.setdefault(p[1], {})
            if p[2] == "description" and len(p) == 3:
                gdesc[p[1]] = val
            elif p[2] == "ports" and len(p) == 4 and p[3].isdigit():
                gports[p[1]][int(p[3])] = int(val)
        elif p[0] == "clients" and len(p) >= 3 and p[1].isdigit():
            c = cl.setdefault(int(p[1]), {"id": "", "description": "", "groups": {}})
            if p[2] in ("id", "description") and len(p) == 3:
                c[p[2]] = val
            elif p[2] == "groups" and len(p) == 4 and p[3].isdigit():
                c["groups"][int(p[3])] = val
    groups = {
        name: Group(name, gdesc[name], _ordered(gports[name], f"{name}.ports")) for name in sorted(gdesc)
    }
    clients = [
        Client(c["id"], c["description"], _ordered(c["groups"], f"clients.{i}.groups"))
        for i, c in enumerate(_ordered(cl, "clients"))
    ]
    return Snapshot(
        enabled=collapsed.get("enable") == "true",
        port=int(collapsed.get("port") or 0),
        groups=groups,
        clients=clients,
    )


class RestHub(HubAdapter):
    def __init__(
        self,
        name: str,
        address: str,
        user: str,
        password: str,
        *,
        tls_verify: bool = False,
        ports: int | None = None,
        model: str | None = None,
        serial: str | None = None,
        write_ports: set[int] | None = None,
        timeout: float = 15.0,
        backup_dir: Path | str = "backups",
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self.address = address
        self.declared_ports = ports
        self.declared_model = model
        self.declared_serial = serial
        self.write_ports = write_ports  # None = 不限制；否则只允许这些端口（对应 groupNN）
        self.backup_dir = Path(backup_dir)
        self._lock = asyncio.Lock()  # 序号型删除不能并发
        self._c = client or httpx.AsyncClient(
            base_url=f"https://{address}", auth=(user, password), verify=tls_verify, timeout=timeout
        )

    # ------------------------------------------------------------------ 低层
    async def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        try:
            r = await self._c.request(method, f"{BASE}{path}", params=params)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            raise HubUnreachable(f"{self.name}: {type(e).__name__}: {e}") from e
        if r.status_code == 401:
            raise HubAuthError(f"{self.name}: 认证失败 (HTTP 401)")
        if r.status_code >= 400:
            raise HubApiError(f"{self.name}: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise HubApiError(f"{self.name}: 响应不是 JSON") from e
        if body.get("error"):  # Hub 的错误是 HTTP 200 + error 标志
            raise HubApiError(f"{self.name}: {body.get('msg')}")
        return body

    async def _read(self, dotted: str) -> dict:
        body = await self._call("GET", "/value/" + dotted.replace(".", "/"))
        return body["result"]

    async def _append(self, path: str, value: str | None = None, fields: dict[str, str] | None = None) -> str:
        params = {"path": path, "append": "true"}
        if value is not None:
            params["value"] = value
        for k, v in (fields or {}).items():
            params[f"collapsed[{k}]"] = v
        return (await self._call("POST", "/value", params)).get("result", "")

    async def _set(self, path: str, value: str) -> None:
        await self._call("POST", "/value", {"path": path, "value": value})

    async def _delete(self, path: str) -> None:
        await self._call("DELETE", "/value", {"path": path})

    async def _snapshot(self) -> Snapshot:
        return parse_snapshot((await self._read(SVC))["collapsed"])

    # ------------------------------------------------------------------ 写保护
    def _guard_group(self, group: str) -> None:
        if self.write_ports is None:
            return
        n = port_for_group(group)
        if n is None or n not in self.write_ports:
            raise HubWriteNotAllowed(f"{self.name}: 不允许写分组 {group}（允许端口 {sorted(self.write_ports)}）")

    def _guard_port(self, port: int) -> None:
        if self.write_ports is not None and port not in self.write_ports:
            raise HubWriteNotAllowed(f"{self.name}: 不允许写端口 {port}（允许端口 {sorted(self.write_ports)}）")

    # ------------------------------------------------------------------ 内部写原语（无锁、写后读校验、幂等）
    async def _add_port(self, g: str, p: int) -> bool:
        s = await self._snapshot()
        if g not in s.groups:
            raise HubApiError(f"{self.name}: 没有分组 {g}")
        if p in s.groups[g].ports:
            return False
        await self._append(f"{SVC}.groups.{g}.ports", value=str(p))
        if p not in (await self._snapshot()).groups[g].ports:
            raise HubVerifyError(f"{self.name}: 端口 {p} 加入 {g} 后读回不一致")
        return True

    async def _del_port(self, g: str, p: int) -> bool:
        s = await self._snapshot()
        if g not in s.groups or p not in s.groups[g].ports:
            return False
        await self._delete(f"{SVC}.groups.{g}.ports.{s.groups[g].ports.index(p)}")
        if p in (await self._snapshot()).groups[g].ports:
            raise HubVerifyError(f"{self.name}: 端口 {p} 从 {g} 删除后读回不一致")
        return True

    async def _add_client(self, cid: str, desc: str) -> bool:
        s = await self._snapshot()
        if any(c.id == cid for c in s.clients):
            return False
        if len(s.clients) >= MAX_CLIENTS:
            raise HubApiError(f"{self.name}: 客户端条目已达上限 {MAX_CLIENTS}")
        fields = {"id": cid}
        if desc:
            fields["description"] = desc
        await self._append(f"{SVC}.clients", fields=fields)
        if not any(c.id == cid for c in (await self._snapshot()).clients):
            raise HubVerifyError(f"{self.name}: 客户端 {cid} 添加后读回不一致")
        return True

    async def _del_client(self, cid: str) -> bool:
        s = await self._snapshot()
        ids = [c.id for c in s.clients]
        if cid not in ids:
            return False
        await self._delete(f"{SVC}.clients.{ids.index(cid)}")
        if any(c.id == cid for c in (await self._snapshot()).clients):
            raise HubVerifyError(f"{self.name}: 客户端 {cid} 删除后读回不一致")
        return True

    async def _grant(self, cid: str, g: str) -> bool:
        s = await self._snapshot()
        idx = [c.id for c in s.clients]
        if cid not in idx:
            raise HubApiError(f"{self.name}: 客户端 {cid} 未登记，先 add_client")
        if g not in s.groups:
            raise HubApiError(f"{self.name}: 没有分组 {g}")
        if g in s.clients[idx.index(cid)].groups:
            return False
        await self._append(f"{SVC}.clients.{idx.index(cid)}.groups", value=g)
        after = await self._snapshot()
        if g not in next(c for c in after.clients if c.id == cid).groups:
            raise HubVerifyError(f"{self.name}: 授权 {cid}→{g} 后读回不一致")
        return True

    async def _revoke(self, cid: str, g: str) -> bool:
        s = await self._snapshot()
        idx = [c.id for c in s.clients]
        if cid not in idx or g not in s.clients[idx.index(cid)].groups:
            return False
        i = idx.index(cid)
        await self._delete(f"{SVC}.clients.{i}.groups.{s.clients[i].groups.index(g)}")
        after = await self._snapshot()
        if g in next(c for c in after.clients if c.id == cid).groups:
            raise HubVerifyError(f"{self.name}: 撤销 {cid}→{g} 后读回不一致")
        return True

    # ------------------------------------------------------------------ HubAdapter 接口
    async def identify(self) -> HubIdentity:
        dev = (await self._read("device"))["collapsed"]
        nports = len({k.split(".")[2] for k in dev if k.startswith("usb.port.") and len(k.split(".")) > 2})
        fw = (await self._read("firmware")).get("collapsed", {}).get("version") or None
        return HubIdentity(
            name=self.name,
            model=self.declared_model,
            serial=self.declared_serial,
            firmware=fw,
            ports=nports,
            verified=False,
            source="端口数来自 Hub；型号/序列号来自 config.yaml 声明（config.cgi 读不到，应以 Manager LIST FULL 核对）",
        )

    async def service_status(self) -> ServiceStatus:
        s = await self._snapshot()
        return ServiceStatus(s.enabled, s.port, len(s.groups), len(s.clients))

    async def list_groups(self) -> list[Group]:
        return list((await self._snapshot()).groups.values())

    async def list_clients(self) -> list[Client]:
        return (await self._snapshot()).clients

    async def set_port_group(self, port: int, group: str) -> bool:
        self._guard_port(port)
        self._guard_group(group)
        async with self._lock:
            changed = await self._add_port(group, port)
            for g in list((await self._snapshot()).groups.values()):
                if g.name != group and port in g.ports:
                    # 只动该端口自身的归属，旧分组可以是任何分组，因此这里不再按分组名做写保护
                    changed |= await self._del_port(g.name, port)
            final = await self._snapshot()
            holders = [g.name for g in final.groups.values() if port in g.ports]
            if holders != [group]:
                raise HubVerifyError(f"{self.name}: 端口 {port} 读回分组为 {holders}，期望 [{group}]")
            return changed

    async def set_group_description(self, group: str, description: str) -> bool:
        self._guard_group(group)
        async with self._lock:
            s = await self._snapshot()
            if group not in s.groups:
                raise HubApiError(f"{self.name}: 没有分组 {group}")
            if s.groups[group].description == description:
                return False
            await self._set(f"{SVC}.groups.{group}.description", description)
            if (await self._snapshot()).groups[group].description != description:
                raise HubVerifyError(f"{self.name}: {group} 描述读回不一致")
            return True

    async def add_client(self, client_id: str, description: str = "") -> bool:
        async with self._lock:
            return await self._add_client(client_id, description)

    async def remove_client(self, client_id: str) -> bool:
        async with self._lock:
            return await self._del_client(client_id)

    async def grant(self, client_id: str, group: str) -> bool:
        self._guard_group(group)
        async with self._lock:
            return await self._grant(client_id, group)

    async def revoke(self, client_id: str, group: str) -> bool:
        self._guard_group(group)
        async with self._lock:
            return await self._revoke(client_id, group)

    async def backup(self, label: str = "") -> Path:
        body = await self._call("GET", "/value/service/anywhereusb")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{label}-" if label else ""
        path = self.backup_dir / f"{self.name}-{tag}{time.strftime('%Y%m%d-%H%M%S')}.json"
        n = 1
        while path.exists():  # 同一秒内多次备份
            path = path.with_name(f"{path.stem}-{n}.json")
            n += 1
        path.write_text(
            json.dumps(
                {"hub": self.name, "taken_at": time.strftime("%F %T"), "values": {"service/anywhereusb": body}},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        return path

    async def restore(self, path: Path, dry_run: bool = False) -> list[str]:
        """只恢复 AnywhereUSB 的启用状态、分组（描述与端口）和客户端（含授权），只做差异；
        先加后删，客户端条目最后才删除，尽量减少断开。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("hub") not in (None, self.name):
            raise HubApiError(f"备份属于 {data['hub']}，不能恢复到 {self.name}")
        want = parse_snapshot(data["values"]["service/anywhereusb"]["result"]["collapsed"])
        cur = await self._snapshot()
        actions: list[tuple] = []
        if cur.enabled != want.enabled:
            actions.append(("enable", want.enabled))
        for name, wg in want.groups.items():
            cg = cur.groups.get(name)
            if cg is None:
                raise HubApiError(f"{self.name}: 当前 Hub 没有分组 {name}，无法恢复")
            if cg.description != wg.description:
                actions.append(("desc", name, wg.description))
            actions += [("add_port", name, p) for p in wg.ports if p not in cg.ports]
        wc = {c.id: c for c in want.clients}
        cc = {c.id: c for c in cur.clients}
        actions += [("add_client", cid, c.description) for cid, c in wc.items() if cid not in cc]
        for cid, c in wc.items():
            have = cc[cid].groups if cid in cc else []
            actions += [("grant", cid, g) for g in c.groups if g not in have]
            actions += [("revoke", cid, g) for g in have if g not in c.groups]
        for name, wg in want.groups.items():
            actions += [("del_port", name, p) for p in cur.groups[name].ports if p not in wg.ports]
        actions += [("del_client", cid) for cid in cc if cid not in wc]
        text = [self._describe(a) for a in actions]
        if dry_run or not actions:
            return text
        await self.backup("pre-restore")
        async with self._lock:
            for a in actions:
                await self._run(a)
            final = await self._snapshot()
        if (
            final.enabled != want.enabled
            or {n: (g.description, sorted(g.ports)) for n, g in final.groups.items() if n in want.groups}
            != {n: (g.description, sorted(g.ports)) for n, g in want.groups.items()}
            or {c.id: sorted(c.groups) for c in final.clients} != {c.id: sorted(c.groups) for c in want.clients}
        ):
            raise HubVerifyError(f"{self.name}: 恢复后读回与备份不一致")
        return text

    @staticmethod
    def _describe(a: tuple) -> str:
        k = a[0]
        return {
            "enable": lambda: f"服务启用状态 → {a[1]}",
            "desc": lambda: f"分组 {a[1]} 描述 → {a[2]!r}",
            "add_port": lambda: f"端口 {a[2]} 加入 {a[1]}",
            "del_port": lambda: f"端口 {a[2]} 从 {a[1]} 移除",
            "add_client": lambda: f"添加客户端 {a[1]}",
            "del_client": lambda: f"删除客户端 {a[1]}",
            "grant": lambda: f"授权 {a[1]} → {a[2]}",
            "revoke": lambda: f"撤销 {a[1]} → {a[2]}（会强制断开已连接的客户端）",
        }[k]()

    async def _run(self, a: tuple) -> None:
        k = a[0]
        if k == "enable":
            await self._set(f"{SVC}.enable", "true" if a[1] else "false")
        elif k == "desc":
            await self._set(f"{SVC}.groups.{a[1]}.description", a[2])
        elif k == "add_port":
            await self._add_port(a[1], a[2])
        elif k == "del_port":
            await self._del_port(a[1], a[2])
        elif k == "add_client":
            await self._add_client(a[1], a[2])
        elif k == "del_client":
            await self._del_client(a[1])
        elif k == "grant":
            await self._grant(a[1], a[2])
        elif k == "revoke":
            await self._revoke(a[1], a[2])

    async def health(self) -> HubHealth:
        t = time.monotonic()
        try:
            await self._call("GET", "/keys/service/anywhereusb")
        except HubError as e:
            return HubHealth(False, None, str(e))
        return HubHealth(True, (time.monotonic() - t) * 1000)

    async def aclose(self) -> None:
        await self._c.aclose()
