"""Windows 客户端代理（主程序）。作为 Windows 服务运行；Ubuntu 上可用 --mock-manager 模拟。

子命令：  run（默认） | enroll | setup-manager | ui | service-run（SCM 启动用） | version
  python agent/agent.py --config agent/agent.yaml --mock-manager
"""
import sys

if __name__ == "__main__" and not getattr(sys, "frozen", False):
    # 直接运行本文件时，把仓库根放到 sys.path 最前并去掉 agent/ 目录，避免 `import agent` 指向本文件
    import os

    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [os.path.dirname(_here)] + [p for p in sys.path if os.path.abspath(p or ".") != _here]

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import random
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from agent import manager_setup
from agent.manager_cli import ADDR_RE, DEFAULT_EXE, ManagerCli
from common import protocol as P
from common.protocol import MsgType

VERSION = "0.1.0"
IS_WINDOWS = sys.platform == "win32"
DEFAULT_CONFIG = r"C:\ProgramData\UKeyAgent\agent.yaml" if IS_WINDOWS else str(Path(__file__).with_name("agent.yaml"))
log = logging.getLogger("ukey-agent")


# ====================================================================== 配置
@dataclass
class AgentConfig:
    server_url: str = "wss://10.70.1.63:8443/ws/agent"
    api_url: str = "https://10.70.1.63:8443"
    server_ca_cert: str | None = None
    enrollment_token: str = ""
    agent_token: str = ""
    manager_exe: str = DEFAULT_EXE
    manager_client_id: str | None = None
    local_ui_port: int = 47800
    log_dir: str = r"C:\ProgramData\UKeyAgent\logs" if IS_WINDOWS else "agent/logs"
    release_policy: str = "accept"   # RELEASE_ASK 时：accept 同意让出 | deny 拒绝
    hostname: str = field(default_factory=lambda: socket.gethostname())
    path: str | None = None

    @property
    def agent_id(self) -> str:
        return self.hostname.strip().lower()

    @classmethod
    def load(cls, path: str | Path) -> "AgentConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
        mgr = raw.get("manager") or {}
        return cls(
            server_url=raw.get("server_url", cls.server_url), api_url=raw.get("api_url", cls.api_url),
            server_ca_cert=raw.get("server_ca_cert"), enrollment_token=raw.get("enrollment_token") or "",
            agent_token=raw.get("agent_token") or "", manager_exe=mgr.get("exe_path", DEFAULT_EXE),
            manager_client_id=mgr.get("client_id"), local_ui_port=int(raw.get("local_ui_port", 47800)),
            log_dir=raw.get("log_dir", cls.log_dir), release_policy=raw.get("release_policy", "accept"),
            hostname=raw.get("hostname") or socket.gethostname(), path=str(path),
        )

    def save_tokens(self) -> None:
        """批准后写回专属 token 并清空注册令牌；文件权限收紧（Windows 的 ACL 由安装脚本设置）。"""
        if not self.path:
            return
        raw = yaml.safe_load(Path(self.path).read_text(encoding="utf-8-sig")) or {}
        raw["agent_token"], raw["enrollment_token"] = self.agent_token, ""
        tmp = Path(self.path).with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def setup_logging(log_dir: str | None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(Path(log_dir) / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
        except OSError as e:
            print(f"无法写日志目录 {log_dir}: {e}", file=sys.stderr)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)


# ====================================================================== 代理
class Agent:
    def __init__(self, cfg: AgentConfig, manager, system, *, connect=ws_connect, base_backoff: float = 1.0,
                 max_backoff: float = 60.0, enroll_poll: float = 15.0):
        self.cfg, self.manager, self.system = cfg, manager, system
        self._connect = connect
        self.base_backoff, self.max_backoff, self.enroll_poll = base_backoff, max_backoff, enroll_poll
        self.ws = None
        self.registered = asyncio.Event()
        self.heartbeat_interval = 15.0
        self.hubs: list[dict] = []
        self.notices: list[str] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._done: dict[str, P.Envelope] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ 工具
    def _ssl(self) -> ssl.SSLContext | None:
        if not self.cfg.server_url.startswith("wss://"):
            return None
        return ssl.create_default_context(cafile=self.cfg.server_ca_cert) if self.cfg.server_ca_cert else ssl.create_default_context()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def stop(self) -> None:
        self._stop.set()

    async def _send(self, env: P.Envelope) -> None:
        await self.ws.send(P.encode(env))

    async def call(self, msg_type: MsgType, payload: Any = None, timeout: float = 15.0) -> P.Ack:
        """向服务器发消息并等 ACK（REQUEST / RELEASE / LIST / EVENT）。"""
        if self.ws is None or not self.registered.is_set():
            return P.Ack(ref_id="", ok=False, error="not connected")
        env = P.make(msg_type, payload)
        fut = asyncio.get_running_loop().create_future()
        self._pending[env.id] = fut
        try:
            await self._send(env)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return P.Ack(ref_id=env.id, ok=False, error="timeout")
        except Exception as e:
            return P.Ack(ref_id=env.id, ok=False, error=f"{type(e).__name__}")
        finally:
            self._pending.pop(env.id, None)

    def manager_mode(self) -> str:
        return manager_setup.detect_state(self.system)

    # ------------------------------------------------------------------ 主循环
    async def run(self) -> None:
        delay = self.base_backoff
        while not self._stop.is_set():
            try:
                if not self.cfg.agent_token:
                    if not await self.enroll_once():
                        await self._sleep(self.enroll_poll)
                        continue
                await self.session()
                delay = self.base_backoff  # 注册成功后断开：从头退避
            except (OSError, WebSocketException, asyncio.TimeoutError, InvalidURI) as e:
                log.warning("连接失败/中断: %s: %s", type(e).__name__, e)
            except Exception:
                log.exception("会话异常")
            self.registered.clear()
            if self._stop.is_set():
                break
            wait = min(delay, self.max_backoff) * (0.5 + random.random())
            log.info("%.1fs 后重连", wait)
            await self._sleep(wait)
            delay = min(delay * 2, self.max_backoff)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), s)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ ENROLL
    async def enroll_once(self) -> bool:
        """ENROLL 一次。已批准 → 保存专属 token 返回 True；待批准 → False（稍后重试）。"""
        if not self.cfg.enrollment_token:
            log.error("没有 agent_token，也没有 enrollment_token，无法登记")
            return False
        async with self._connect(self.cfg.server_url, ssl=self._ssl(), max_size=P.MAX_MESSAGE_BYTES, open_timeout=10) as ws:
            env = P.make(MsgType.ENROLL, P.Enroll(hostname=self.cfg.hostname, ip=_local_ip(), token=self.cfg.enrollment_token))
            await ws.send(P.encode(env))
            ack = await self._wait_ack(ws, env.id)
        if not ack.ok:
            log.error("ENROLL 被拒绝: %s", ack.error)
            return False
        if ack.data.get("status") == "approved" and ack.data.get("token"):
            self.cfg.agent_token = ack.data["token"]
            self.cfg.save_tokens()
            log.info("已批准，专属 token 已保存")
            return True
        log.info("ENROLL 成功，等待管理员批准")
        return False

    @staticmethod
    async def _wait_ack(ws, ref_id: str, timeout: float = 15.0) -> P.Ack:
        async def go():
            async for raw in ws:
                m = P.decode(raw)
                if m.type == MsgType.ACK and m.payload.get("ref_id") == ref_id:
                    return P.Ack.model_validate(m.payload)
            raise ConnectionClosed(None, None)

        return await asyncio.wait_for(go(), timeout)

    # ------------------------------------------------------------------ 会话
    async def session(self) -> None:
        async with self._connect(self.cfg.server_url, ssl=self._ssl(), max_size=P.MAX_MESSAGE_BYTES, open_timeout=10) as ws:
            self.ws = ws
            info = await asyncio.to_thread(self.manager.info)
            reg = P.Register(
                agent_id=self.cfg.agent_id, token=self.cfg.agent_token, hostname=self.cfg.hostname,
                client_id=info.client_id or self.cfg.manager_client_id, ip=_local_ip(), agent_version=VERSION,
                manager_version=info.version or None, manager_mode=self.manager_mode(),
            )
            env = P.make(MsgType.REGISTER, reg)
            await ws.send(P.encode(env))
            ack = await self._wait_ack(ws, env.id)
            if not ack.ok:
                if ack.error == "unauthorized" and self.cfg.enrollment_token:
                    log.error("专属 token 无效，将重新 ENROLL")
                    self.cfg.agent_token = ""
                else:
                    log.error("REGISTER 被拒绝: %s", ack.error)
                return
            self.registered.set()
            log.info("已注册到服务器")
            hb = asyncio.create_task(self._heartbeat_loop())
            try:
                async for raw in ws:
                    await self._on_message(raw)
            except ConnectionClosed:
                pass
            finally:
                hb.cancel()
                self.registered.clear()
                self.ws = None
                for f in self._pending.values():
                    if not f.done():
                        f.set_result(P.Ack(ref_id="", ok=False, error="disconnected"))

    async def _heartbeat_loop(self) -> None:
        while True:
            info = await asyncio.to_thread(self.manager.info)
            await self.call(MsgType.HEARTBEAT, P.Heartbeat(connected_groups=info.connected_groups()), timeout=10)
            await asyncio.sleep(self.heartbeat_interval)

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            env = P.decode(raw)
        except P.ProtocolError as e:
            log.warning("丢弃非法消息: %s", e)
            return
        if env.type == MsgType.ACK:
            a = P.Ack.model_validate(env.payload)
            fut = self._pending.get(a.ref_id)
            if fut and not fut.done():
                fut.set_result(a)
            return
        if env.id in self._done:  # 服务器重发（重试）：回上次的 ACK，不重复执行
            await self._send(self._done[env.id])
            return
        self._spawn(self._handle(env))

    async def _handle(self, env: P.Envelope) -> None:
        try:
            reply = await self._execute(env)
        except Exception as e:
            log.exception("处理 %s 失败", env.type.value)
            reply = P.ack(env.id, False, f"{type(e).__name__}: {e}"[:200])
        self._done[env.id] = reply
        while len(self._done) > 500:
            self._done.pop(next(iter(self._done)))
        try:
            await self._send(reply)
        except Exception:
            pass

    async def _execute(self, env: P.Envelope) -> P.Envelope:
        t, pl = env.type, env.payload
        if t == MsgType.CONFIG:
            cfg = P.Config.model_validate(pl)
            self.heartbeat_interval = cfg.heartbeat_interval_seconds
            self.hubs = [h.model_dump() for h in cfg.hubs]
            acts = await asyncio.to_thread(manager_setup.apply_hubs, self.manager, self.hubs)
            for a in acts:
                log.info("CONFIG: %s", a)
            return P.ack(env.id, all("FAILED" not in a for a in acts), None, actions=acts)
        if t in (MsgType.CONNECT, MsgType.DISCONNECT):
            cmd = P.GroupCommand.model_validate(pl)
            if not ADDR_RE.match(cmd.group):
                return P.ack(env.id, False, "invalid group address")
            fn = self.manager.connect_group if t == MsgType.CONNECT else self.manager.disconnect_group
            res = await asyncio.to_thread(fn, cmd.group)
            log.info("%s %s -> %s", t.value, cmd.group, "OK" if res.ok else f"FAILED rc={res.rc}")
            kind = "connected" if t == MsgType.CONNECT else "disconnected"
            self._spawn(self.call(MsgType.EVENT, P.Event(kind=kind if res.ok else "error", port_id=cmd.port_id,
                                                         group=cmd.group, detail="" if res.ok else f"rc={res.rc}")))
            return P.ack(env.id, res.ok, None if res.ok else f"manager rc={res.rc} {res.err or res.out}".strip()[:200])
        if t == MsgType.RELEASE_ASK:
            ask = P.ReleaseAsk.model_validate(pl)
            accept = self.cfg.release_policy == "accept"
            self.notices.append(f"{ask.requester_host or ask.requester} 申请 {ask.port_id}：已{'同意' if accept else '拒绝'}让出")
            log.info("RELEASE_ASK %s from %s -> %s", ask.port_id, ask.requester, "accept" if accept else "deny")
            return P.ack(env.id, True, accept=accept)
        if t == MsgType.NOTIFY:
            self.notices.append(P.Notify.model_validate(pl).text)
            return P.ack(env.id)
        if t == MsgType.RUN_SETUP:
            rep = await asyncio.to_thread(self.run_setup)
            return P.ack(env.id, rep.ok, None if rep.ok else "setup failed", setup=rep.summary())
        return P.ack(env.id, False, f"unexpected {t.value}")

    def run_setup(self) -> manager_setup.SetupReport:
        return manager_setup.setup_manager(
            self.system, self.manager, hostname=self.cfg.hostname, desired_client_id=self.cfg.manager_client_id,
            hubs=self.hubs or None, log=log.info, assume_yes=False,
        )

    # ------------------------------------------------------------------ 本机 UI 接口（127.0.0.1）
    async def start_ui(self, port: int | None = None) -> asyncio.AbstractServer:
        srv = await asyncio.start_server(self._ui_client, "127.0.0.1", self.cfg.local_ui_port if port is None else port)
        return srv

    async def _ui_client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(r.readline(), 10)
            req = json.loads(line or b"{}")
            res = await self._ui_command(req)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        w.write((json.dumps(res, ensure_ascii=False) + "\n").encode())
        await w.drain()
        w.close()

    async def _ui_command(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            info = await asyncio.to_thread(self.manager.info)
            n, self.notices = self.notices, []
            return {"ok": True, "connected": self.registered.is_set(), "agent_id": self.cfg.agent_id,
                    "client_id": info.client_id, "manager_mode": self.manager_mode(),
                    "groups": info.connected_groups(), "notices": n}
        if cmd == "ports":
            a = await self.call(MsgType.LIST)
            return {"ok": a.ok, "error": a.error, "ports": a.data.get("ports", [])}
        if cmd in ("request", "release"):
            ref = {k: req[k] for k in ("port_id", "label") if req.get(k)}
            a = await self.call(MsgType.REQUEST if cmd == "request" else MsgType.RELEASE, P.PortRef(**ref), timeout=30)
            return {"ok": a.ok, "error": a.error, "data": a.data}
        return {"ok": False, "error": "unknown command"}


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return ""


# ====================================================================== `agent.exe ui`
def ui_ask(port: int, req: dict, create=socket.create_connection) -> dict:
    """向本机代理服务发一条请求（一行 JSON）。编码固定 UTF-8：中文 Windows 的默认编码是 GBK，
    不显式指定会在解码服务器返回的中文时崩溃（实测）。"""
    try:
        with create(("127.0.0.1", port), timeout=10) as s:
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            line = s.makefile("r", encoding="utf-8", errors="replace").readline()
            return json.loads(line or "{}")
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"连接不上本机代理服务（端口 {port}）: {type(e).__name__}: {e}"}


def ui_main(port: int) -> int:
    """命令行菜单：列出可用 UKey / 申请 / 释放 / 状态（经 127.0.0.1 与代理服务通信）。"""
    for stream in (sys.stdout, sys.stderr):   # 控制台编码放不下的字符用 ? 代替，而不是崩溃
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ask = lambda req: ui_ask(port, req)  # noqa: E731
    while True:
        print("\n1) 列出 UKey  2) 申请  3) 释放  4) 本机状态  0) 退出")
        try:
            c = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if c == "0":
            return 0
        if c == "1":
            r = ask({"cmd": "ports"})
            for p in r.get("ports", []):
                print(f"  {p['port_id']:<12} {p['state']:<10} {p.get('label') or ''}{'  (我持有)' if p.get('mine') else ''}")
            if not r.get("ok"):
                print("  失败:", r.get("error"))
        elif c in ("2", "3"):
            target = input("端口 ID 或 UKey 名称: ").strip()
            ref = {"port_id": target} if "-p" in target else {"label": target}
            r = ask({"cmd": "request" if c == "2" else "release", **ref})
            print("  成功" if r.get("ok") else f"  失败: {r.get('error')} {r.get('data') or ''}")
        elif c == "4":
            print(json.dumps(ask({"cmd": "status"}), ensure_ascii=False, indent=2))


# ====================================================================== 入口
def build_runtime(cfg: AgentConfig, mock: bool):
    if mock:
        from agent.manager_mock import MockManager, MockSystem

        return MockManager(client_id=cfg.manager_client_id or f"ukey-{cfg.agent_id}"), MockSystem("service")
    return ManagerCli(cfg.manager_exe), manager_setup.WindowsSystem(cfg.manager_exe)


async def run_agent(cfg: AgentConfig, mock: bool, stop: asyncio.Event | None = None) -> None:
    manager, system = build_runtime(cfg, mock)
    agent = Agent(cfg, manager, system)
    ui = await agent.start_ui()
    if stop is not None:
        asyncio.get_running_loop().create_task(_stop_when(stop, agent))
    try:
        await agent.run()
    finally:
        ui.close()


async def _stop_when(ev: asyncio.Event, agent: Agent) -> None:
    await ev.wait()
    agent.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "enroll", "setup-manager", "ui", "service-run", "version"])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--mock-manager", action="store_true", help="用假 Manager（Linux 开发/演示）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--yes", action="store_true", help="setup-manager：确认切换 stand-alone → 服务模式")
    ap.add_argument("--installer", help="setup-manager：本地 Digi 安装包路径")
    ap.add_argument("--sha256", help="setup-manager：安装包期望的 SHA-256")
    a = ap.parse_args(argv)
    if a.command == "version":
        print(VERSION)
        return 0
    if a.command == "service-run":
        from agent import winservice

        return winservice.run_service(a.config)
    cfg = AgentConfig.load(a.config)
    setup_logging(cfg.log_dir, a.verbose)
    if a.command == "ui":
        return ui_main(cfg.local_ui_port)
    manager, system = build_runtime(cfg, a.mock_manager)
    if a.command == "setup-manager":
        base = cfg.api_url.rstrip("/")
        rep = manager_setup.setup_manager(
            system, manager, hostname=cfg.hostname, desired_client_id=cfg.manager_client_id,
            installer_path=a.installer, installer_url=f"{base}/agent/manager-installer", expected_sha256=a.sha256,
            ca_cert=cfg.server_ca_cert, assume_yes=a.yes, log=print,
        )
        for s in rep.steps:
            print(f"[{'OK' if s.ok else '失败'}] {s.name}: {s.detail}")
        print(json.dumps(rep.summary(), ensure_ascii=False))
        print(f"日志目录: {cfg.log_dir}")
        return 0 if rep.ok else (3 if rep.needs_confirmation else 1)
    if a.command == "enroll":
        agent = Agent(cfg, manager, system)
        ok = asyncio.run(agent.enroll_once())
        print("已批准并保存 token" if ok else "已提交，等待管理员批准（或被拒绝，见日志）")
        return 0 if ok else 2
    try:
        asyncio.run(run_agent(cfg, a.mock_manager))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
