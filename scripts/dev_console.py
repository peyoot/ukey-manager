#!/usr/bin/env python3
"""用 mock Hub 起一个完整的控制台（HTTP、无真实设备），用来看页面 / 排练演示 / 浏览器里点向导。

  python scripts/dev_console.py [--port 8470]
登录：admin / demo（仅本机演示）。数据全在临时目录，不会碰真实 Hub、config.yaml 和 .env。
"""
import argparse
import asyncio
import shutil
import tempfile
from pathlib import Path

import httpx
import uvicorn

from server.app import create_app
from server.config import ROOT, load_config
from server.db import Db
from server.hub.base import group_name_for_port
from server.hub.mock import FakeCgi, MockHub
from server.hub.rest import RestHub
from server.link import Ack
from server.ws import AgentHub


class DemoLink(AgentHub):
    """演示用：代理"在线"但不真连，指令一律成功 ACK。"""

    def is_online(self, agent_id):
        return True

    async def send(self, agent_id, msg_type, payload, *, timeout, retries):
        return Ack(True, payload={"accept": True})


async def main(port: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ukey-dev-"))
    shutil.copy(ROOT / "config.yaml", tmp / "config.yaml")
    (tmp / ".env").write_text("HUB_PASSWORD_HUB_A=demo\nCONSOLE_ADMIN_PASSWORD=demo\n")
    cfg = load_config(tmp / "config.yaml")
    hub_a = MockHub("hub-a", ports=8, serial="AW08-017363", backup_dir=tmp / "backups")
    for p in range(1, 9):
        await hub_a.set_port_group(p, group_name_for_port(p))
        await hub_a.set_group_description(group_name_for_port(p), f"hub-a-p{p:02d}")
    fakes = {"10.70.1.209": FakeCgi(password="hubb", ports=24)}   # 向导里添加的第二台：地址 10.70.1.209，密码 hubb

    def factory(*, name, address, user, password, tls_verify, ports, model, serial):
        fk = fakes.get(address)
        if fk is None:
            fk = FakeCgi(); fk.offline = True
        client = httpx.AsyncClient(base_url="https://x", transport=fk.transport(), auth=(user, password))
        return RestHub(name, address, user, password, ports=ports, model=model, serial=serial, backup_dir=tmp / "backups", client=client)

    db = Db(tmp / "ukey.db")
    link = DemoLink(db, enrollment_token="demo", hubs_info=[])
    app = create_app(cfg, {"hub-a": hub_a}, link, db, admin_user="admin", admin_password="demo", session_secret="d" * 32,
                     cookie_secure=False, background=True, base_dir=tmp, config_path=tmp / "config.yaml",
                     env_path=tmp / ".env", backup_dir=tmp / "backups", hub_factory=factory)
    sched = app.state.scheduler
    sched.register_agent("vm-win-1", "vm-win-1", "10.70.1.92", "ukey-vm-win-1", ["10.70.1.92"], {"manager_mode": "service", "manager_version": "3.1.39.6"})
    sched.register_agent("vm-win-2", "vm-win-2", "10.70.1.93", "ukey-vm-win-2", [], {"manager_mode": "standalone"})
    await sched.request("vm-win-1", "hub-a-p06")
    print(f"控制台： http://127.0.0.1:{port}/   登录 admin / demo   （添加 Hub 向导：地址 10.70.1.209 密码 hubb）\n临时目录：{tmp}")
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")).serve()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8470)
    asyncio.run(main(ap.parse_args().port))
