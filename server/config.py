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
