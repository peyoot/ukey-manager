"""命令行：把 Hub 配成"每口一组"（端口 N = groupNN，描述按 name_pattern）。向导的命令行等价物。

  python -m server.setup_groups                      # 只打印所有 Hub 的变更计划
  python -m server.setup_groups --hub hub-a          # 只打印 hub-a 的计划
  python -m server.setup_groups --hub hub-a --apply  # 确认后执行（默认只写 test_port）
  python -m server.setup_groups --hub hub-a --apply --all-ports   # 写全部端口（需用户确认过）
  python -m server.setup_groups --hub hub-a --apply --ports 3,6
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from .config import AppConfig, HubConfig, hub_password, load_config
from .hub.base import HubAdapter, group_name_for_port
from .hub.factory import create_hub


@dataclass(frozen=True)
class Change:
    kind: str  # move | describe
    port: int
    group: str
    detail: str


async def plan_hub(hub: HubAdapter, hub_name: str, nports: int, pattern: str) -> list[Change]:
    groups = {g.name: g for g in await hub.list_groups()}
    changes: list[Change] = []
    for port in range(1, nports + 1):
        gname = group_name_for_port(port)
        if gname not in groups:
            changes.append(Change("missing", port, gname, f"Hub 上没有分组 {gname}，无法规划"))
            continue
        holders = [g.name for g in groups.values() if port in g.ports]
        if holders != [gname]:
            changes.append(Change("move", port, gname, f"端口 {port}: {holders or '无分组'} → {gname}"))
        want = pattern.format(hub=hub_name, port=port)
        if groups[gname].description != want:
            changes.append(Change("describe", port, gname, f"{gname} 描述: {groups[gname].description!r} → {want!r}"))
    return changes


async def apply_changes(hub: HubAdapter, hub_name: str, changes: list[Change], pattern: str) -> None:
    for ch in changes:
        if ch.kind == "move":
            await hub.set_port_group(ch.port, ch.group)
        elif ch.kind == "describe":
            await hub.set_group_description(ch.group, pattern.format(hub=hub_name, port=ch.port))
        else:
            raise RuntimeError(ch.detail)
        print(f"  ✔ {ch.detail}")


def _write_ports(args, cfg: HubConfig) -> set[int] | None:
    if args.all_ports:
        return None
    if args.ports:
        return {int(x) for x in args.ports.split(",")}
    if cfg.test_port is None:
        sys.exit(f"{cfg.name}: config.yaml 未设置 test_port，且未指定 --ports / --all-ports")
    return {cfg.test_port}


async def run(args, cfg: AppConfig) -> int:
    hubs = [cfg.hub(args.hub)] if args.hub else cfg.hubs
    if args.apply and not args.hub:
        sys.exit("--apply 必须同时指定 --hub")
    rc = 0
    for hc in hubs:
        allowed = _write_ports(args, hc) if args.apply else None
        hub = create_hub(hc, hub_password(hc), write_ports=allowed)
        try:
            changes = await plan_hub(hub, hc.name, hc.ports, cfg.grouping.name_pattern)
            if allowed is not None:
                skipped = [c for c in changes if c.port not in allowed]
                changes = [c for c in changes if c.port in allowed]
                if skipped:
                    print(f"[{hc.name}] 另有 {len(skipped)} 项变更不在允许端口 {sorted(allowed)} 内，本次不执行")
            print(f"[{hc.name}] 变更计划：" + ("无变更" if not changes else ""))
            for ch in changes:
                print(f"  - {ch.detail}")
            if not args.apply or not changes:
                continue
            if not args.yes and input("确认执行？先会备份。输入 yes 继续: ").strip() != "yes":
                print("已取消")
                rc = 1
                continue
            print(f"备份: {await hub.backup('pre-setup-groups')}")
            await apply_changes(hub, hc.name, changes, cfg.grouping.name_pattern)
            print("完成，已写后读校验")
        finally:
            await hub.aclose()
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--ports", help="逗号分隔的端口，如 3,6")
    ap.add_argument("--all-ports", action="store_true")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    return asyncio.run(run(args, load_config(args.config)))


if __name__ == "__main__":
    raise SystemExit(main())
