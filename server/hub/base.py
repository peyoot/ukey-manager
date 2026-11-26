"""HubAdapter 抽象接口与公共数据结构。

约定（来自阶段 0.5 实测）：
- 调度单元：端口 N = Hub 预置分组 `groupNN`（如端口 6 → group06）；描述写成 `hub-a-p06`。
- Manager 的分组地址 = `<序列号>.<分组序号>`，因此端口 N 的地址是 `<序列号>.N`。
- 全局端口 ID = `<hub 名称>-p<两位端口号>`。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


class HubError(Exception):
    """Hub 适配器错误基类。"""


class HubUnreachable(HubError):
    """网络不通 / 超时。"""


class HubAuthError(HubError):
    """认证失败（HTTP 401）。"""


class HubApiError(HubError):
    """Hub 返回错误（注意：Hub 的错误常以 HTTP 200 + {"error": true} 返回）。"""


class HubVerifyError(HubError):
    """写后读校验不一致。"""


class HubWriteNotAllowed(HubError):
    """写操作超出允许的端口范围（例如只允许 test_port）。"""


def group_name_for_port(port: int) -> str:
    return f"group{port:02d}"


def port_for_group(group: str) -> int | None:
    """`group06` → 6；非标准分组名返回 None。"""
    if group.startswith("group") and group[5:].isdigit():
        return int(group[5:])
    return None


def port_id(hub: str, port: int) -> str:
    return f"{hub}-p{port:02d}"


@dataclass(frozen=True)
class HubIdentity:
    name: str
    model: str | None
    serial: str | None
    firmware: str | None
    ports: int
    verified: bool  # False：型号/序列号来自配置声明，Hub 的 config.cgi 读不到（见 hub-api-notes.md）
    source: str


@dataclass(frozen=True)
class ServiceStatus:
    enabled: bool
    port: int
    group_count: int
    client_count: int


@dataclass
class Group:
    name: str  # Hub 侧分组名，如 group06
    description: str = ""
    ports: list[int] = field(default_factory=list)


@dataclass
class Client:
    id: str
    description: str = ""
    groups: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HubHealth:
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


class HubAdapter(abc.ABC):
    """一台 Hub 的管理接口。所有写操作须"写后读"校验，并且幂等。"""

    name: str

    @abc.abstractmethod
    async def identify(self) -> HubIdentity: ...

    @abc.abstractmethod
    async def service_status(self) -> ServiceStatus: ...

    @abc.abstractmethod
    async def list_groups(self) -> list[Group]: ...

    @abc.abstractmethod
    async def set_port_group(self, port: int, group: str) -> bool:
        """把端口移入 group（先加后删，端口不会出现无分组瞬间）。返回是否有变更。"""

    @abc.abstractmethod
    async def set_group_description(self, group: str, description: str) -> bool: ...

    @abc.abstractmethod
    async def list_clients(self) -> list[Client]: ...

    @abc.abstractmethod
    async def add_client(self, client_id: str, description: str = "") -> bool: ...

    @abc.abstractmethod
    async def remove_client(self, client_id: str) -> bool: ...

    @abc.abstractmethod
    async def grant(self, client_id: str, group: str) -> bool:
        """授权：动态分配时调度引擎调用。返回是否有变更（已授权则 False）。"""

    @abc.abstractmethod
    async def revoke(self, client_id: str, group: str) -> bool:
        """撤销授权。注意：撤销会立即强制断开已连接的客户端（实测）。"""

    @abc.abstractmethod
    async def set_service_enabled(self, enabled: bool) -> bool:
        """启用/停用 AnywhereUSB 服务（向导第 3 步的可选项）。返回是否有变更。"""

    @abc.abstractmethod
    async def service_keys(self) -> list[str]:
        """`service.anywhereusb` 下的子键，用于向导检查设备配置结构是否与 hub-api-notes.md 一致。"""

    @abc.abstractmethod
    async def backup(self, label: str = "") -> Path: ...

    @abc.abstractmethod
    async def restore(self, path: Path, dry_run: bool = False) -> list[str]:
        """按备份恢复分组与客户端（只做差异）。返回动作描述列表。"""

    @abc.abstractmethod
    async def health(self) -> HubHealth: ...

    async def aclose(self) -> None:  # pragma: no cover - 默认无资源
        return None
