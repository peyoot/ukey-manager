"""命令行：按备份恢复 Hub 的 AnywhereUSB 配置（启用、分组、客户端），只做差异。

  python -m server.restore backups/hub-a-xxx.json            # 只打印将执行的动作
  python -m server.restore backups/hub-a-xxx.json --apply    # 确认后执行（先自动再备份一份）
Hub 名称取自备份文件内的 "hub" 字段，可用 --hub 覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import hub_password, load_config
from .hub.factory import create_hub


async def run(args) -> int:
    cfg = load_config(args.config)
    path = Path(args.backup)
    name = args.hub or json.loads(path.read_text(encoding="utf-8")).get("hub")
    if not name:
        sys.exit("备份中没有 hub 字段，请用 --hub 指定")
    hc = cfg.hub(name)
    hub = create_hub(hc, hub_password(hc))
    try:
        actions = await hub.restore(path, dry_run=True)
        print(f"[{name}] 恢复计划（{path.name}）：" + ("无差异" if not actions else ""))
        for a in actions:
            print(f"  - {a}")
        if not args.apply or not actions:
            return 0
        if not args.yes and input("确认执行？输入 yes 继续: ").strip() != "yes":
            print("已取消")
            return 1
        await hub.restore(path)
        print("恢复完成，已读回校验（恢复前已自动备份 *-pre-restore-*）")
        return 0
    finally:
        await hub.aclose()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup")
    ap.add_argument("--hub")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--config")
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
