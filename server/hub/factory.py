from __future__ import annotations

from .rest import RestHub


def create_hub(hub_cfg, password: str, *, write_ports: set[int] | None = None, backup_dir="backups") -> RestHub:
    """由 config.yaml 的一条 hubs 记录创建适配器；Hub 的个数、名称、地址完全由配置决定。"""
    return RestHub(
        hub_cfg.name,
        hub_cfg.address,
        hub_cfg.admin_user,
        password,
        tls_verify=hub_cfg.tls_verify,
        ports=hub_cfg.ports,
        model=hub_cfg.model,
        serial=hub_cfg.serial,
        write_ports=write_ports,
        backup_dir=backup_dir,
    )
