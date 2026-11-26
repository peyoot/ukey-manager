import asyncio
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from common import protocol as P
from common.protocol import MsgType


class FakeWS:
    """内存里的 WebSocket：服务器端用 receive_text/send_text/close，测试端用 say/hear。"""

    def __init__(self, ip="10.0.0.1"):
        self.client = SimpleNamespace(host=ip)
        self.to_server: asyncio.Queue = asyncio.Queue()
        self.to_client: asyncio.Queue = asyncio.Queue()
        self.closed_code = None

    async def accept(self):
        pass

    async def receive_text(self):
        item = await self.to_server.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    async def send_text(self, text):
        await self.to_client.put(text)

    async def close(self, code=1000):
        if self.closed_code is None:
            self.closed_code = code
            await self.to_client.put(None)
            await self.to_server.put(None)

    # --- 测试端
    async def say(self, env: P.Envelope | str):
        await self.to_server.put(env if isinstance(env, str) else P.encode(env))

    async def hear(self, timeout=1.0):
        item = await asyncio.wait_for(self.to_client.get(), timeout)
        return None if item is None else P.decode(item)

    def disconnect(self):
        self.to_server.put_nowait(None)


class MockAgent:
    """协议级模拟代理：自动 ACK 服务器指令，可配置行为；记录收到的指令和实际执行次数（按 id 去重）。"""

    def __init__(self, hub, agent_id, token, *, ip="10.0.0.1", client_id=None, mode="service"):
        self.hub, self.agent_id, self.token = hub, agent_id, token
        self.ws = FakeWS(ip)
        self.client_id = client_id or f"ukey-{agent_id}"
        self.mode = mode
        self.received: list[P.Envelope] = []
        self.executed: dict[str, int] = {}
        self.accept_release = True
        self.drop_first: set[MsgType] = set()   # 这些类型的指令第一次不 ACK（模拟丢包），等服务器重发
        self.seen_ids: set[str] = set()
        self.acks: dict[str, P.Ack] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self.task = None
        self.runner = None

    async def start(self):
        self.runner = asyncio.create_task(self.hub.handle(self.ws))
        await self.ws.say(P.make(MsgType.REGISTER, P.Register(
            agent_id=self.agent_id, token=self.token, hostname=self.agent_id, client_id=self.client_id,
            ip="9.9.9.9", agent_version="t", manager_version="3.1", manager_mode=self.mode)))
        first = await self.ws.hear()
        assert first is not None, f"closed with {self.ws.closed_code}"
        assert first.type == MsgType.ACK
        self._first = P.Ack.model_validate(first.payload)
        if not self._first.ok:
            return self
        self.task = asyncio.create_task(self._pump())
        return self

    async def _pump(self):
        while True:
            env = await self.ws.hear(timeout=30)
            if env is None:
                return
            if env.type == MsgType.ACK:
                a = P.Ack.model_validate(env.payload)
                fut = self._waiters.pop(a.ref_id, None)
                self.acks[a.ref_id] = a
                if fut and not fut.done():
                    fut.set_result(a)
                continue
            self.received.append(env)
            if env.type in self.drop_first and env.id not in self.seen_ids:
                self.seen_ids.add(env.id)
                continue
            self.seen_ids.add(env.id)
            self.executed.setdefault(env.id, 1)   # 去重：重发的同 id 指令只算执行一次
            data = {"accept": self.accept_release} if env.type == MsgType.RELEASE_ASK else {}
            await self.ws.say(P.ack(env.id, True, **data))

    def types(self):
        return [e.type for e in self.received]

    async def call(self, msg_type, payload, *, id=None, timeout=3.0) -> P.Ack:
        env = P.make(msg_type, payload, id=id)
        fut = asyncio.get_running_loop().create_future()
        self._waiters[env.id] = fut
        await self.ws.say(env)
        return await asyncio.wait_for(fut, timeout)

    async def request(self, port_id=None, label=None):
        return await self.call(MsgType.REQUEST, P.PortRef(port_id=port_id, label=label))

    async def release(self, port_id):
        return await self.call(MsgType.RELEASE, P.PortRef(port_id=port_id))

    async def heartbeat(self, groups=()):
        return await self.call(MsgType.HEARTBEAT, P.Heartbeat(connected_groups=list(groups)))

    async def stop(self):
        self.ws.disconnect()
        if self.runner:
            await asyncio.wait_for(self.runner, 2)
        if self.task:
            self.task.cancel()
