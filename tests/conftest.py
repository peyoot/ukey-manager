import pytest

from server.db import Db
from server.hub.base import group_name_for_port
from server.hub.mock import MockHub
from server.link import Ack
from server.scheduler import Scheduler


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeLink:
    """模拟代理通道：记录指令；可配置是否 ACK、是否同意让出、是否失联。"""

    def __init__(self):
        self.online: set[str] = set()
        self.sent: list[tuple[str, str, dict]] = []
        self.ack_ok = True
        self.drop: set[str] = set()      # 这些代理收到指令但不响应（超时）
        self.fail_types: set[str] = set()  # 这些指令类型 ACK ok=False
        self.accept_release = True
        self.timeouts: dict[str, float] = {}

    def is_online(self, agent_id):
        return agent_id in self.online

    async def send(self, agent_id, msg_type, payload, *, timeout, retries):
        self.sent.append((agent_id, msg_type, payload))
        self.timeouts[msg_type] = timeout
        if agent_id in self.drop or agent_id not in self.online:
            return Ack(False, "timeout")
        if msg_type in self.fail_types:
            return Ack(False, "agent error")
        if msg_type == "RELEASE_ASK":
            return Ack(True, payload={"accept": self.accept_release})
        return Ack(self.ack_ok)

    def types(self, agent_id=None):
        return [t for a, t, _ in self.sent if agent_id in (None, a)]


async def make_hub(name, ports, serial, tmp_path):
    hub = MockHub(name, ports=ports, serial=serial, backup_dir=tmp_path)
    for p in range(1, ports + 1):
        await hub.set_port_group(p, group_name_for_port(p))
    return hub


class Env:
    pass


@pytest.fixture
async def env(tmp_path):
    e = Env()
    e.clock = Clock()
    e.link = FakeLink()
    e.hub_a = await make_hub("hub-a", 8, "AW08-A", tmp_path)
    e.hub_b = await make_hub("hub-b", 4, "AW04-B", tmp_path)
    e.db = Db(":memory:")
    e.sched = Scheduler(
        e.db, {"hub-a": e.hub_a, "hub-b": e.hub_b}, e.link,
        lease_ttl=600, heartbeat_timeout=45, ack_timeout=0.05, retries=0, clock=e.clock,
    )

    class Cfg:  # 与 config.HubConfig 同形
        def __init__(self, name, ports):
            self.name, self.ports = name, ports

    e.sched.sync_ports([Cfg("hub-a", 8), Cfg("hub-b", 4)], [{"hub": "hub-a", "port": 6, "label": "demo disk"}])
    for aid, cid in (("vm-win-1", "ukey-vm-win-1"), ("vm-win-2", "ukey-vm-win-2")):
        e.sched.register_agent(aid, aid, "10.0.0.1", cid, auto_approve_ips=["10.0.0.1"])
        e.link.online.add(aid)
    return e
