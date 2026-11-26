"""「添加 / 编辑 Hub」向导后端：探测（只读）→ 规划（不写）→ 应用（逐项写后读）→ 失败可恢复。

- 在 apply 之前**不对 Hub 做任何写操作**（probe 只读；plan 只比较）。
- 探测阶段自动备份到 backups/；apply 失败即停，restore 恢复到该备份。
- 成功后：ruamel 改写 config.yaml（先备份，保留注释）、密码写入 .env（600，HUB_PASSWORD_<名称>）、热加载、写审计。
- 密码只存在于服务端内存里的会话对象中，任何响应都不回显。
"""
from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import (
    HUB_NAME_RE, HubConfig, backup_file, hub_password, load_config, password_env_name, remove_hub_in_data,
    set_env_var, update_config_file, upsert_hub_in_data,
)
from .hub.base import HubAdapter, HubError, group_name_for_port, port_for_group
from .hub.rest import MAX_CLIENTS

ADDRESS_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REQUIRED_KEYS = {"enable", "port", "groups", "clients"}   # 与 docs/hub-api-notes.md 一致的最小结构
SESSION_TTL = 3600


class WizardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    id: str
    mode: str                       # add | edit
    name: str
    params: dict
    hub: HubAdapter
    probe: dict
    backup: Path
    created: float
    plan: dict | None = None
    status: str = "probed"          # probed | planned | applying | done | failed | restored
    progress: list[dict] = field(default_factory=list)
    error: str | None = None
    config_backup: Path | None = None
    config_written: bool = False
    adopted: bool = False           # 已交给调度器，不能再关闭
    task: asyncio.Task | None = None
    password: str = field(default="", repr=False)   # 仅内存；绝不出现在任何响应里

    def view(self) -> dict:
        return {"session_id": self.id, "mode": self.mode, "name": self.name, "status": self.status,
                "progress": self.progress, "error": self.error, "backup": self.backup.name,
                "can_restore": self.status == "failed"}


class Wizard:
    def __init__(self, *, sched, link, db, config_path: Path, env_path: Path, backup_dir: Path,
                 hub_factory: Callable[..., HubAdapter], clock: Callable[[], float] = time.time):
        self.sched, self.link, self.db = sched, link, db
        self.config_path, self.env_path, self.backup_dir = Path(config_path), Path(env_path), Path(backup_dir)
        self.hub_factory, self.clock = hub_factory, clock
        self.sessions: dict[str, Session] = {}
        self._apply_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 公共辅助
    def _cfg(self):
        return load_config(self.config_path)

    def hubs_overview(self) -> list[dict]:
        out = []
        for h in self._cfg().hubs:
            ports = self.db.list_ports(h.name)
            out.append({"name": h.name, "address": h.address, "model": h.model, "serial": h.serial, "ports": h.ports,
                        "online": self.sched._hub_ok.get(h.name, True),
                        "active_leases": len([p for p in ports if p["state"] in ("leased", "connected")])})
        return out

    def suggest_name(self) -> str:
        used = {h.name for h in self._cfg().hubs}
        for c in "bcdefghijklmnopqrstuvwxyza":
            if f"hub-{c}" not in used:
                return f"hub-{c}"
        n = 2
        while f"hub-{n}" in used:
            n += 1
        return f"hub-{n}"

    async def _purge(self) -> None:
        now = self.clock()
        for sid, s in list(self.sessions.items()):
            if now - s.created > SESSION_TTL and s.status != "applying":
                self.sessions.pop(sid)
                if not s.adopted:
                    await s.hub.aclose()

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s:
            raise WizardError("向导会话不存在或已过期，请从第 1 步重新开始", 404)
        return s

    # ------------------------------------------------------------------ 第 1-2 步：探测（只读）
    async def probe(self, params: dict, actor: str = "admin") -> dict:
        await self._purge()
        name = str(params.get("name", "")).strip()
        address = str(params.get("address", "")).strip()
        user = str(params.get("admin_user") or "admin").strip()
        if not HUB_NAME_RE.match(name):
            raise WizardError("名称只能含小写字母、数字、连字符，且以字母开头（≤32 位）")
        existing = next((h for h in self._cfg().hubs if h.name == name), None)
        if existing and params.get("edit") and not address:
            address = existing.address                         # 编辑时地址可留空：沿用配置
        if not ADDRESS_RE.match(address):
            raise WizardError("地址只能是 IP 或主机名")
        mode = "edit" if existing else "add"
        if existing and not params.get("edit"):
            raise WizardError(f"名称 {name} 已存在；要修改请使用「编辑」")
        password = params.get("password") or ""
        if not password:
            if mode == "add":
                raise WizardError("请输入 Hub 管理员密码")
            password = hub_password(existing, self.env_path)       # 编辑：沿用 .env 中的密码
        tls_verify = bool(params.get("tls_verify", False))
        hub = self.hub_factory(name=name, address=address, user=user, password=password, tls_verify=tls_verify,
                               ports=None, model=params.get("model") or None, serial=params.get("serial") or None)
        try:
            health = await hub.health()
            if not health.ok:
                raise WizardError(f"连接 Hub 失败：{health.error}", 502)
            keys = await hub.service_keys()
            ident = await hub.identify()
            status = await hub.service_status()
            groups = await hub.list_groups()
            clients = await hub.list_clients()
            backup = await hub.backup("wizard-probe")
        except WizardError:
            await hub.aclose()
            raise
        except HubError as e:
            await hub.aclose()
            raise WizardError(f"读取 Hub 失败：{e}", 502)
        warnings, blocked = [], False
        missing = sorted(REQUIRED_KEYS - set(keys))
        if missing:
            blocked = True
            warnings.append(f"Hub 的 AnywhereUSB 配置缺少键 {missing}，与 docs/hub-api-notes.md 记录的结构不一致（固件版本不同？），"
                            "为安全起见禁止进入写入步骤。")
        if not groups:
            blocked = True
            warnings.append("Hub 上没有任何分组，无法规划。")
        odd = [g.name for g in groups if port_for_group(g.name) is None]
        if odd:
            blocked = True
            warnings.append(f"分组名不符合 groupNN 约定：{odd}，与调度器的端口 ↔ 分组映射不兼容。")
        if groups and len(groups) < ident.ports:
            warnings.append(f"Hub 有 {ident.ports} 个端口但只有 {len(groups)} 个分组，超出的端口无法单独成组。")
        if not status.enabled:
            warnings.append("AnywhereUSB 服务当前未启用（可在第 3 步勾选启用）。")
        warnings.append("型号 / 序列号 / 固件版本在 Hub 的 config.cgi 里读不到，上面显示的是你填写的值；以 Manager `LIST FULL` 为准。")
        sess = Session(
            id=secrets.token_urlsafe(16), mode=mode, name=name, hub=hub, backup=backup, created=self.clock(),
            params={"name": name, "address": address, "admin_user": user, "tls_verify": tls_verify,
                    "model": params.get("model") or (existing.model if existing else None),
                    "serial": params.get("serial") or (existing.serial if existing else None),
                    "ports": ident.ports},
            probe={}, password=password,
        )
        sess.probe = {
            "identity": {"model": sess.params["model"], "serial": sess.params["serial"], "firmware": ident.firmware,
                         "ports": ident.ports, "verified": ident.verified},
            "service": {"enabled": status.enabled, "port": status.port},
            "groups": [{"name": g.name, "description": g.description, "ports": g.ports} for g in groups],
            "clients": [{"id": c.id, "description": c.description, "groups": c.groups} for c in clients],
            "client_limit": MAX_CLIENTS,
            "warnings": warnings, "blocked": blocked,
        }
        self.sessions[sess.id] = sess
        self.db.audit(actor, None, None, "wizard_probe", "ok", f"{mode} {name} ({address}) 备份 {backup.name}")
        return {**sess.view(), **sess.probe, "approved_clients": self._approved_client_ids()}

    def _approved_client_ids(self) -> list[dict]:
        return [{"agent_id": a["agent_id"], "client_id": a["client_id"], "hostname": a["hostname"]}
                for a in self.db.list_agents() if a["status"] == "approved" and a["client_id"]]

    # ------------------------------------------------------------------ 第 3-5 步：规划（只比较，不写）
    async def plan(self, sid: str, options: dict) -> dict:
        s = self.get(sid)
        if s.status in ("applying", "done"):
            raise WizardError("该会话已在应用或已完成")
        if s.probe["blocked"]:
            raise WizardError("探测发现结构不一致，禁止规划写入")
        grouping = options.get("grouping", "per-port")
        if grouping not in ("per-port", "keep"):
            raise WizardError("grouping 只能是 per-port 或 keep")
        auth_mode = options.get("authorization", "dynamic")
        if auth_mode not in ("dynamic", "static"):
            raise WizardError("authorization 只能是 dynamic 或 static")
        pattern = self._cfg().grouping.name_pattern
        nports = s.params["ports"]
        labels = {int(k): str(v).strip() for k, v in (options.get("labels") or {}).items()}
        descs = {int(k): str(v).strip() for k, v in (options.get("descriptions") or {}).items()}
        for p, text in {**labels, **descs}.items():
            if not 1 <= p <= nports:
                raise WizardError(f"端口号 {p} 超出范围 1-{nports}")
            if len(text) > 64 or any(ord(ch) < 32 for ch in text):
                raise WizardError("名称/描述最长 64 字符且不能含控制字符")
        client_ids = []
        for cid in options.get("client_ids") or []:
            cid = str(cid).strip()
            if not CLIENT_ID_RE.match(cid):
                raise WizardError(f"非法的 Client ID：{cid!r}（只允许字母数字 . _ -，≤64）")
            if cid not in client_ids:
                client_ids.append(cid)
        groups = {g["name"]: g for g in s.probe["groups"]}
        grants = {}
        if auth_mode == "static":
            for cid, gl in (options.get("static_grants") or {}).items():
                if cid not in client_ids:
                    raise WizardError(f"静态授权的 Client ID {cid!r} 不在客户端列表中")
                bad = [g for g in gl if g not in groups]
                if bad:
                    raise WizardError(f"不存在的分组：{bad}")
                grants[cid] = list(dict.fromkeys(gl))

        changes: list[dict] = []
        unchanged = {"groups": 0, "clients": 0}
        leased = {p["port"] for p in self.db.list_ports(s.name) if p["state"] in ("leased", "connected")} if s.mode == "edit" else set()
        if options.get("enable_service") and not s.probe["service"]["enabled"]:
            changes.append({"kind": "enable_service", "text": "启用 AnywhereUSB 服务"})
        if grouping == "per-port":
            for port in range(1, nports + 1):
                g = group_name_for_port(port)
                if g not in groups:
                    changes.append({"kind": "missing", "port": port, "text": f"Hub 上没有分组 {g}，无法为端口 {port} 规划"})
                    continue
                holders = [n for n, gg in groups.items() if port in gg["ports"]]
                want = descs.get(port) or pattern.format(hub=s.name, port=port)
                if holders != [g]:
                    if port in leased:
                        raise WizardError(f"端口 {port} 有活动租约，请先回收后再修改分组")
                    changes.append({"kind": "move_port", "port": port, "group": g,
                                    "text": f"端口 {port}：{holders or '无分组'} → {g}"})
                else:
                    unchanged["groups"] += 1
                if groups[g]["description"] != want:
                    changes.append({"kind": "describe", "group": g, "description": want,
                                    "text": f"{g} 描述：{groups[g]['description']!r} → {want!r}"})
        have = {c["id"]: c for c in s.probe["clients"]}
        for cid in client_ids:
            if cid not in have:
                changes.append({"kind": "add_client", "client": cid, "text": f"添加客户端 {cid}"})
            else:
                unchanged["clients"] += 1
        total = len(set(have) | set(client_ids))
        if total > MAX_CLIENTS:
            raise WizardError(f"客户端数量 {total} 超过每台 Hub 的上限 {MAX_CLIENTS}")
        for cid, gl in grants.items():
            for g in gl:
                if g not in (have.get(cid, {}).get("groups") or []):
                    changes.append({"kind": "grant", "client": cid, "group": g, "text": f"授权 {cid} → {g}"})
        if s.mode == "add":
            changes.append({"kind": "register", "text": f"登记到 config.yaml（备份旧文件），密码写入 .env 变量 {password_env_name(s.name)}"})
        else:
            changes.append({"kind": "register", "text": "更新 config.yaml 中该 Hub 与 UKey 显示名（备份旧文件）"})
        warnings = list(s.probe["warnings"][:-1])
        if auth_mode == "dynamic":
            warnings.append("动态分配：只加入客户端列表，不预先授予分组，由调度引擎按需授权。")
        s.plan = {"options": {"grouping": grouping, "authorization": auth_mode, "labels": labels,
                              "client_ids": client_ids, "enable_service": bool(options.get("enable_service"))},
                  "changes": changes}
        s.status = "planned"
        return {"changes": changes, "unchanged": unchanged, "warnings": warnings,
                "clients": {"count": total, "limit": MAX_CLIENTS}}

    # ------------------------------------------------------------------ 第 5 步：应用
    async def apply(self, sid: str, confirm: bool, actor: str = "admin") -> dict:
        s = self.get(sid)
        if not confirm:
            raise WizardError("需要再次确认（confirm=true）")
        if s.status != "planned" or not s.plan:
            raise WizardError("请先完成规划（第 3-4 步）")
        s.status, s.progress, s.error = "applying", [], None
        s.task = asyncio.create_task(self._run_apply(s, actor))
        return s.view()

    def _step(self, s: Session, text: str, status: str, detail: str = "") -> dict:
        row = {"text": text, "status": status, "detail": detail}
        s.progress.append(row)
        return row

    async def _run_apply(self, s: Session, actor: str) -> None:
        async with self._apply_lock:
            try:
                for ch in s.plan["changes"]:
                    if ch["kind"] in ("missing", "register"):
                        continue
                    row = self._step(s, ch["text"], "running")
                    await self._do_change(s, ch)
                    row["status"] = "ok"
                row = self._step(s, "写入 config.yaml / .env 并热加载", "running")
                await self._register(s, actor)
                row["status"] = "ok"
                s.status = "done"
                self.db.audit(actor, None, None, "wizard_apply", "ok", f"{s.mode} {s.name}: {len(s.plan['changes'])} 项")
            except Exception as e:
                if s.progress and s.progress[-1]["status"] == "running":
                    s.progress[-1].update(status="failed", detail=str(e)[:300])
                s.status, s.error = "failed", f"{type(e).__name__}: {e}"[:400]
                self.db.audit(actor, None, None, "wizard_apply", "error", f"{s.name}: {s.error}")

    async def _do_change(self, s: Session, ch: dict) -> None:
        hub, k = s.hub, ch["kind"]
        if k == "enable_service":
            await hub.set_service_enabled(True)
        elif k == "move_port":
            await hub.set_port_group(ch["port"], ch["group"])
        elif k == "describe":
            await hub.set_group_description(ch["group"], ch["description"])
        elif k == "add_client":
            await hub.add_client(ch["client"])
        elif k == "grant":
            await hub.grant(ch["client"], ch["group"])
        else:
            raise WizardError(f"未知变更类型 {k}")

    async def _register(self, s: Session, actor: str) -> None:
        p = s.params
        env_name = password_env_name(s.name)
        entry = {"name": s.name, "address": p["address"], "model": p["model"], "serial": p["serial"], "ports": p["ports"],
                 "admin_user": p["admin_user"], "password_env": env_name, "tls_verify": p["tls_verify"],
                 "awusb_service_port": s.probe["service"]["port"]}
        if s.mode == "edit":
            entry = {k: v for k, v in entry.items() if k not in ("password_env",)}
        entry = {k: v for k, v in entry.items() if v is not None}
        labels = s.plan["options"]["labels"]
        if s.mode == "add":
            # 密码写入 .env（权限 600）；config.yaml 只记变量名
            set_env_var(self.env_path, env_name, s.password)
        s.config_backup = update_config_file(self.config_path, self.backup_dir, lambda d: upsert_hub_in_data(d, entry, labels))
        s.config_written = True
        cfg = self._cfg()
        hub_cfg = cfg.hub(s.name)
        old = self.sched.hubs.get(s.name)
        ukeys = (cfg.model_extra or {}).get("ukeys", [])
        self.sched.add_hub(hub_cfg, s.hub, ukeys)
        s.adopted = True
        for pr in self.db.list_ports(s.name):                       # 标签更新（sync_ports 只在为空时覆盖）
            lab = labels.get(pr["port"])
            if lab:
                self.db.set_port(pr["port_id"], label=lab)
        if old is not None and old is not s.hub:
            await old.aclose()
        if hasattr(self.link, "set_hubs_info"):
            await self.link.set_hubs_info(self._hubs_info(cfg))

    @staticmethod
    def _hubs_info(cfg) -> list[dict]:
        return [{"name": h.name, "address": h.address, "port": h.awusb_service_port, "serial": h.serial} for h in cfg.hubs]

    # ------------------------------------------------------------------ 失败后恢复
    async def restore(self, sid: str, actor: str = "admin") -> dict:
        s = self.get(sid)
        if s.status not in ("failed",):
            raise WizardError("只有应用失败的会话才能恢复")
        if s.task and not s.task.done():
            raise WizardError("应用仍在进行")
        actions = await s.hub.restore(s.backup)
        if s.config_written and s.config_backup and s.config_backup.exists():
            shutil.copy2(s.config_backup, self.config_path)
        s.status = "restored"
        self.db.audit(actor, None, None, "wizard_restore", "ok", f"{s.name}: {len(actions)} 项 ← {s.backup.name}")
        return {**s.view(), "actions": actions}

    # ------------------------------------------------------------------ 移除 Hub（只动服务器配置）
    async def remove_hub(self, name: str, actor: str = "admin") -> dict:
        cfg = self._cfg()
        if not any(h.name == name for h in cfg.hubs):
            raise WizardError(f"没有 Hub {name}", 404)
        busy = [p["port_id"] for p in self.db.list_ports(name) if p["state"] in ("leased", "connected") or self.db.active_lease(p["port_id"])]
        if busy:
            raise WizardError(f"{name} 有活动租约（{', '.join(busy)}），请先回收", 409)
        bak = update_config_file(self.config_path, self.backup_dir, lambda d: remove_hub_in_data(d, name))
        adapter = self.sched.remove_hub(name)
        if adapter is not None:
            await adapter.aclose()
        if hasattr(self.link, "set_hubs_info"):
            await self.link.set_hubs_info(self._hubs_info(self._cfg()))
        self.db.audit(actor, None, None, "hub_remove", "ok", f"{name}（只从服务器配置移除，Hub 上的配置未改动）；旧配置备份 {bak.name}")
        return {"ok": True, "config_backup": bak.name,
                "note": "Hub 上的配置未改动；.env 中的密码变量与历史数据已保留"}
