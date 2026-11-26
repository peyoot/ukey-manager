"""FastAPI 入口：管理员登录 + REST（端口、代理、审计、Hub）+ 后台任务。

阶段 2 范围：不含 /ws/agent（阶段 3）与静态控制台页面（阶段 5）。
启动：  python -m server          （读取 config.yaml 的 TLS 证书与端口）
或      uvicorn server.app:create_default_app --factory --host 0.0.0.0 --port 8443 \\
          --ssl-certfile certs/server.crt --ssl-keyfile certs/server.key
"""

import asyncio
import contextlib
import hmac
import os
import time
from typing import Annotated

from dotenv import dotenv_values
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from .config import ROOT, AppConfig, hub_password, load_config
from .db import Db
from .hub.base import HubAdapter
from .hub.factory import create_hub
from .link import AgentLink, NullLink
from .scheduler import Scheduler

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
    return create_app(
        cfg, hubs, NullLink(), Db(ROOT / server.get("database", "data/ukey.db")),
        admin_user=server.get("console_admin_user", "admin"),
        admin_password=env["CONSOLE_ADMIN_PASSWORD"],
        session_secret=env["SESSION_SECRET"],
    )
