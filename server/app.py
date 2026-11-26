"""FastAPI 入口：管理员登录 + REST（端口、代理、审计、Hub）+ 后台任务。

阶段 2 范围：不含 /ws/agent（阶段 3）与静态控制台页面（阶段 5）。
启动：  python -m server          （读取 config.yaml 的 TLS 证书与端口）
或      uvicorn server.app:create_default_app --factory --host 0.0.0.0 --port 8443 \\
          --ssl-certfile certs/server.crt --ssl-keyfile certs/server.key
"""

import asyncio
import contextlib
import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Annotated

from dotenv import dotenv_values
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from .config import ROOT, AppConfig, hub_password, load_config
from .db import Db
from .hub.base import HubAdapter
from .hub.factory import create_hub
from .link import AgentLink
from .ws import AgentHub
from .scheduler import Scheduler
from .wizard import Wizard, WizardError

SESSION_COOKIE = "ukey_session"
SESSION_MAX_AGE = 8 * 3600


class LoginBody(BaseModel):
    username: str
    password: str


class AgentBody(BaseModel):
    agent_id: str


def create_app(
    cfg: AppConfig,
    hubs: dict[str, HubAdapter],
    link: AgentLink,
    db: Db,
    *,
    admin_user: str,
    admin_password: str,
    session_secret: str,
    cookie_secure: bool = True,
    background: bool = True,
    base_dir: Path | None = None,
    config_path: Path | None = None,
    env_path: Path | None = None,
    backup_dir: Path | None = None,
    hub_factory=None,
) -> FastAPI:
    raw = cfg.model_extra or {}
    lease = raw.get("lease", {})
    clients = raw.get("clients", {})
    sched = Scheduler(
        db, hubs, link,
        lease_ttl=lease.get("ttl_seconds", 600),
        heartbeat_timeout=lease.get("heartbeat_timeout_seconds", 45),
        ack_timeout=lease.get("command_ack_timeout_seconds", 5),
        retries=lease.get("command_retries", 2),
        auto_revoke_orphans=lease.get("auto_revoke_orphan_grants", False),
        authorization_mode=clients.get("authorization_mode", "dynamic"),
    )
    sched.sync_ports(cfg.hubs, raw.get("ukeys", []))
    if hasattr(link, "bind"):
        link.bind(sched)
    signer = URLSafeTimedSerializer(session_secret, salt="ukey-console")
    sweep_interval = max(1.0, lease.get("heartbeat_interval_seconds", 15) / 3)

    async def loop():
        await sched.check_hubs()
        await sched.reconcile()
        while True:
            await asyncio.sleep(sweep_interval)
            try:
                await sched.sweep()
                await sched.check_hubs()
            except Exception as e:  # 后台任务不能因单次异常退出
                db.audit("system", None, None, "background_error", "error", f"{type(e).__name__}: {e}")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(loop()) if background else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for h in hubs.values():
            await h.aclose()

    app = FastAPI(title="UKey 管理服务器（演示）", lifespan=lifespan)
    app.state.scheduler = sched
    app.state.db = db

    def admin(session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> str:
        if not session:
            raise HTTPException(401, "login required")
        try:
            data = signer.loads(session, max_age=SESSION_MAX_AGE)
        except BadSignature:
            raise HTTPException(401, "invalid or expired session")
        return data["u"]

    Admin = Annotated[str, Depends(admin)]

    # ---------------------------------------------------------------- 登录
    @app.post("/api/login")
    async def login(body: LoginBody, response: Response):
        ok_u = hmac.compare_digest(body.username.encode(), admin_user.encode())
        ok_p = hmac.compare_digest(body.password.encode(), admin_password.encode())
        if not (ok_u and ok_p):
            db.audit(body.username[:64], None, None, "login", "denied")
            await asyncio.sleep(0.5)  # 简单限速
            raise HTTPException(401, "bad credentials")
        db.audit(body.username, None, None, "login", "ok")
        response.set_cookie(
            SESSION_COOKIE, signer.dumps({"u": body.username, "t": int(time.time())}),
            max_age=SESSION_MAX_AGE, httponly=True, secure=cookie_secure, samesite="strict",
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/session")
    async def session(user: Admin):
        return {"user": user}

    # ---------------------------------------------------------------- 端口
    @app.get("/api/ports")
    async def ports(user: Admin):
        return [sched.view_port(p["port_id"]) for p in db.list_ports()]

    def _result(res: dict) -> dict:
        if res["status"] == "error":
            raise HTTPException(409 if "fault" in res.get("error", "") else 400, res["error"])
        return res

    @app.post("/api/ports/{port_id}/request")
    async def request_port(port_id: str, body: AgentBody, user: Admin):
        res = await sched.request(body.agent_id, port_id, actor=user)
        if res["status"] == "busy":
            raise HTTPException(409, detail=res)
        return _result(res)

    @app.post("/api/ports/{port_id}/release")
    async def release_port(port_id: str, body: AgentBody, user: Admin):
        return _result(await sched.release(body.agent_id, port_id, actor=user))

    @app.post("/api/ports/{port_id}/force-release")
    async def force_release(port_id: str, user: Admin):
        return _result(await sched.force_release(port_id, actor=user))

    # ---------------------------------------------------------------- 代理
    @app.get("/api/agents")
    async def agents(user: Admin):
        return [{k: v for k, v in a.items() if k != "token_hash"} for a in db.list_agents()]

    @app.post("/api/agents/{agent_id}/approve")
    async def approve(agent_id: str, user: Admin):
        if not sched.approve_agent(agent_id, actor=user):
            raise HTTPException(404, "unknown agent")
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/run-setup")
    async def run_setup(agent_id: str, user: Admin):
        if not hasattr(link, "run_setup"):
            raise HTTPException(503, "agent channel not available")
        a = await link.run_setup(agent_id)
        db.audit(user, agent_id, None, "run_setup", "ok" if a.ok else "error", a.error)
        if not a.ok:
            raise HTTPException(409, a.error or "failed")
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/reset-token")
    async def reset_token(agent_id: str, user: Admin):
        if not db.get_agent(agent_id):
            raise HTTPException(404, "unknown agent")
        db.set_agent(agent_id, token_hash=None)
        db.audit(user, agent_id, None, "reset_token", "ok", "agent must ENROLL again")
        return {"ok": True}

    @app.websocket("/ws/agent")
    async def ws_agent(ws: WebSocket):
        if not hasattr(link, "handle"):
            await ws.close(1013)
            return
        await link.handle(ws)

    # ---------------------------------------------------------------- 代理下载（只读，来自 dist/，无需登录）
    base = Path(base_dir) if base_dir else ROOT
    mgr_cfg, agent_cfg = raw.get("manager_installer", {}), raw.get("agent_package", {})
    public_url = raw.get("server", {}).get("public_url", "https://localhost:8443")
    sha_cache: dict[str, tuple[tuple, str]] = {}

    def sha256_of(path: Path) -> str:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        hit = sha_cache.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha_cache[str(path)] = (key, h.hexdigest())
        return h.hexdigest()

    def agent_exe() -> Path | None:
        p = base / agent_cfg.get("file", "dist/agent.exe")
        return p if p.is_file() else None

    def manager_installer() -> tuple[Path | None, str | None]:
        p = base / mgr_cfg.get("file", "")
        want = (mgr_cfg.get("sha256") or "").lower()
        return (p if mgr_cfg.get("file") and p.is_file() else None), want or None

    @app.get("/agent/manifest.json")
    async def manifest():
        a = agent_exe()
        m, want = manager_installer()
        return {
            "server_url": public_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws/agent",
            "api_url": public_url,
            "agent": {"available": bool(a), "sha256": sha256_of(a) if a else None, "size": a.stat().st_size if a else None},
            "manager": {"available": bool(m), "sha256": want,
                        "verified": bool(m and want and sha256_of(m) == want)},
        }

    @app.get("/agent/agent.exe")
    async def dl_agent():
        a = agent_exe()
        if not a:
            raise HTTPException(404, "agent.exe 尚未构建（见 CLAUDE.md 第 4 节）")
        return FileResponse(a, media_type="application/octet-stream", filename="agent.exe")

    @app.get("/agent/manager-installer")
    async def dl_manager():
        m, want = manager_installer()
        if not m:
            raise HTTPException(404, "Digi 官方安装包不在服务器 dist/ 中")
        if not want or sha256_of(m) != want:  # 与配置的 SHA-256 不符就拒绝提供
            raise HTTPException(409, "安装包 SHA-256 与 config.yaml 不符，拒绝提供")
        return FileResponse(m, media_type="application/octet-stream", filename="manager-installer.exe")

    @app.get("/agent/install.ps1")
    async def dl_install():
        script = (Path(__file__).resolve().parent.parent / "agent" / "installer" / "install-agent.ps1").read_text(encoding="utf-8")
        return PlainTextResponse(script.replace("__SERVER_URL__", public_url), media_type="text/plain; charset=utf-8")

    # ---------------------------------------------------------------- Hub 向导
    root = Path(base_dir) if base_dir else ROOT
    bdir = Path(backup_dir) if backup_dir else root / "backups"

    def default_factory(*, name, address, user, password, tls_verify, ports, model, serial):
        from .hub.rest import RestHub

        return RestHub(name, address, user, password, tls_verify=tls_verify, ports=ports, model=model, serial=serial, backup_dir=bdir)

    wiz = Wizard(sched=sched, link=link, db=db, config_path=Path(config_path) if config_path else root / "config.yaml",
                 env_path=Path(env_path) if env_path else root / ".env", backup_dir=bdir,
                 hub_factory=hub_factory or default_factory)
    app.state.wizard = wiz

    def wz(coro_fn):
        async def run(*a, **kw):
            try:
                return await coro_fn(*a, **kw)
            except WizardError as e:
                raise HTTPException(e.status, str(e))
        return run

    class ProbeBody(BaseModel):
        name: str
        address: str = ""
        admin_user: str = "admin"
        password: str = ""
        tls_verify: bool = False
        model: str | None = None
        serial: str | None = None
        edit: bool = False

    class PlanBody(BaseModel):
        session_id: str
        grouping: str = "per-port"
        authorization: str = "dynamic"
        enable_service: bool = False
        labels: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        client_ids: list[str] = []
        static_grants: dict[str, list[str]] = {}

    class ApplyBody(BaseModel):
        session_id: str
        confirm: bool = False

    class SessionBody(BaseModel):
        session_id: str

    @app.get("/api/hubs/config")
    async def hubs_config(user: Admin):
        return {"hubs": wiz.hubs_overview(), "suggested_name": wiz.suggest_name()}

    @app.post("/api/hubs/probe")
    async def hubs_probe(body: ProbeBody, user: Admin):
        return await wz(wiz.probe)(body.model_dump(), actor=user)

    @app.post("/api/hubs/plan")
    async def hubs_plan(body: PlanBody, user: Admin):
        d = body.model_dump()
        return await wz(wiz.plan)(d.pop("session_id"), d)

    @app.post("/api/hubs/apply")
    async def hubs_apply(body: ApplyBody, user: Admin):
        return await wz(wiz.apply)(body.session_id, body.confirm, actor=user)

    @app.get("/api/hubs/wizard/{sid}")
    async def hubs_wizard_status(sid: str, user: Admin):
        try:
            return wiz.get(sid).view()
        except WizardError as e:
            raise HTTPException(e.status, str(e))

    @app.post("/api/hubs/{name}/restore")
    async def hubs_restore(name: str, body: SessionBody, user: Admin):
        try:
            if wiz.get(body.session_id).name != name:
                raise HTTPException(400, "会话与 Hub 名称不符")
        except WizardError as e:
            raise HTTPException(e.status, str(e))
        return await wz(wiz.restore)(body.session_id, actor=user)

    @app.delete("/api/hubs/{name}")
    async def hubs_delete(name: str, user: Admin):
        return await wz(wiz.remove_hub)(name, actor=user)

    # ---------------------------------------------------------------- 控制台实时推送（/ws/console）
    consoles: set[asyncio.Queue] = set()

    def fanout(ev: dict) -> None:
        for q in list(consoles):
            if q.qsize() < 200:
                q.put_nowait(ev)

    sched.subscribe(fanout)

    @app.websocket("/ws/console")
    async def ws_console(ws: WebSocket):
        origin, host = ws.headers.get("origin"), ws.headers.get("host")
        if origin and host and origin.split("://", 1)[-1] != host:      # 防跨站 WebSocket 劫持
            await ws.close(4403)
            return
        try:
            signer.loads(ws.cookies.get(SESSION_COOKIE, ""), max_age=SESSION_MAX_AGE)
        except BadSignature:
            await ws.close(4401)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        consoles.add(q)
        try:
            await ws.send_json({"event": "hello"})
            while True:
                recv = asyncio.create_task(ws.receive_text())
                get = asyncio.create_task(q.get())
                done, pending = await asyncio.wait({recv, get}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if recv in done:
                    recv.result()          # 断开时抛 WebSocketDisconnect
                if get in done:
                    await ws.send_json(get.result())
        except WebSocketDisconnect:
            pass
        finally:
            consoles.discard(q)

    # ---------------------------------------------------------------- 静态控制台页面
    static_dir = Path(__file__).resolve().parent / "static"
    CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith("/api/") and request.url.path != "/agent/agent.exe":
            resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---------------------------------------------------------------- 审计与 Hub
    @app.get("/api/audit")
    async def audit(user: Admin, limit: int = 200, port_id: str | None = None):
        return db.list_audit(min(max(limit, 1), 1000), port_id)

    @app.get("/api/hubs")
    async def hubs_view(user: Admin):
        out = []
        for name, hub in hubs.items():
            ps = db.list_ports(name)
            out.append({
                "name": name,
                "online": sched._hub_ok.get(name, True),
                "ports": len(ps),
                "states": {s: sum(1 for p in ps if p["state"] == s) for s in ("idle", "leased", "connected", "fault")},
            })
        return out

    return app


def create_default_app() -> FastAPI:
    """由 config.yaml + .env 构建真实应用（Hub 列表全部来自配置）。"""
    cfg = load_config()
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    for key in ("CONSOLE_ADMIN_PASSWORD", "SESSION_SECRET"):
        if not env.get(key):
            raise RuntimeError(f"{key} 未设置（应在 .env 中）")
    server = (cfg.model_extra or {}).get("server", {})
    hubs = {h.name: create_hub(h, hub_password(h)) for h in cfg.hubs}  # 写保护由调用方按需设置
    raw = cfg.model_extra or {}
    agents_cfg, lease, clients = raw.get("agents", {}), raw.get("lease", {}), raw.get("clients", {})
    token_env = agents_cfg.get("enrollment_token_env", "AGENT_ENROLLMENT_TOKEN")
    if not env.get(token_env):
        raise RuntimeError(f"{token_env} 未设置（应在 .env 中）")
    link = AgentHub(
        db := Db(ROOT / server.get("database", "data/ukey.db")),
        enrollment_token=env[token_env],
        auto_approve_ips=agents_cfg.get("auto_approve_ips", []),
        hubs_info=[{"name": h.name, "address": h.address, "port": h.awusb_service_port, "serial": h.serial} for h in cfg.hubs],
        heartbeat_interval=lease.get("heartbeat_interval_seconds", 15),
        client_id_pattern=clients.get("client_id_pattern", "ukey-{hostname}"),
    )
    return create_app(
        cfg, hubs, link, db,
        admin_user=server.get("console_admin_user", "admin"),
        admin_password=env["CONSOLE_ADMIN_PASSWORD"],
        session_secret=env["SESSION_SECRET"],
    )
