"""读取 config.yaml 与 .env。Hub 的数量、名称、地址只来自配置，代码中不写死。

热加载与向导写回（ruamel.yaml 保留注释、写前备份）在后续阶段加入。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent


class HubConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    address: str
    model: str | None = None
    serial: str | None = None
    ports: int
    admin_user: str = "admin"
    password_env: str
    tls_verify: bool = False
    awusb_service_port: int = 18574
    test_port: int | None = None


class GroupingConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: str = "per-port"
    name_pattern: str = "{hub}-p{port:02d}"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    hubs: list[HubConfig]
    grouping: GroupingConfig = GroupingConfig()

    def hub(self, name: str) -> HubConfig:
        for h in self.hubs:
            if h.name == name:
                return h
        raise KeyError(f"config.yaml 中没有 Hub: {name}")


def load_config(path: Path | str | None = None) -> AppConfig:
    path = Path(path) if path else ROOT / "config.yaml"
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)


def hub_password(hub: HubConfig, env_path: Path | str | None = None) -> str:
    """密码只从 .env（或进程环境变量）读取，绝不写入日志。"""
    env = dotenv_values(Path(env_path) if env_path else ROOT / ".env")
    pw = env.get(hub.password_env) or os.environ.get(hub.password_env)
    if not pw:
        raise RuntimeError(f"{hub.password_env} 未设置（应在 .env 中）")
    return pw


# ====================================================================== 向导写回（保留注释、写前备份）
import re
import shutil
import time
from io import StringIO

HUB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _rt_yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    return y


def backup_file(path: Path, backup_dir: Path, tag: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}"
    n = 1
    while dest.exists():
        dest = backup_dir / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}-{n}{path.suffix}"
        n += 1
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    return dest


def password_env_name(hub_name: str) -> str:
    return "HUB_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", hub_name).upper()


def set_env_var(env_path: Path, key: str, value: str) -> None:
    """写入/更新 .env 的一个变量（权限 600）。值里不允许换行。"""
    if "\n" in value or "\r" in value:
        raise ValueError("值不能含换行")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, done = [], False
    for ln in lines:
        if re.match(rf"^{re.escape(key)}=", ln):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)


def update_config_file(path: Path, backup_dir: Path, mutate) -> Path:
    """用 ruamel 往返改写 config.yaml：先备份到 backups/config-<时间>.yaml，mutate(data) 就地修改。返回备份路径。"""
    y = _rt_yaml()
    bak = backup_file(path, backup_dir, "config")
    data = y.load(path.read_text(encoding="utf-8"))
    mutate(data)
    buf = StringIO()
    y.dump(data, buf)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)
    return bak


def _flow_map(**kw):
    from ruamel.yaml.comments import CommentedMap

    m = CommentedMap(kw)
    m.fa.set_flow_style()
    return m


def upsert_hub_in_data(data, hub: dict, labels: dict[int, str]) -> None:
    """新增或更新 hubs 中的一台 Hub，并重写该 Hub 的 ukeys 标签。"""
    from ruamel.yaml.comments import CommentedMap

    hubs = data["hubs"]
    existing = next((h for h in hubs if h["name"] == hub["name"]), None)
    if existing is None:
        hubs.append(CommentedMap(hub))
    else:
        for k, v in hub.items():
            existing[k] = v
    ukeys = data.get("ukeys")
    if ukeys is None:
        data["ukeys"] = ukeys = []
    keep = [u for u in ukeys if u.get("hub") != hub["name"]]
    del ukeys[:]
    ukeys.extend(keep)
    for port in sorted(labels):
        if labels[port]:
            ukeys.append(_flow_map(hub=hub["name"], port=port, label=labels[port]))


def remove_hub_in_data(data, name: str) -> None:
    hubs = data["hubs"]
    for i, h in enumerate(list(hubs)):
        if h["name"] == name:
            del hubs[i]
    ukeys = data.get("ukeys")
    if ukeys is not None:
        keep = [u for u in ukeys if u.get("hub") != name]
        del ukeys[:]
        ukeys.extend(keep)
