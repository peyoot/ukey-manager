#!/usr/bin/env python3
"""Hub REST API 逐项测试（阶段 0.5）。

用法：
  python scripts/hub_probe.py --hub hub-a read      # 只读测试
  python scripts/hub_probe.py --hub hub-a errors    # 错误路径（错误键名/错误密码/不可达）

Hub 地址、用户、密码环境变量名都来自 config.yaml；密码从 .env 读取，绝不打印。
写操作测试在确认写入语法后另行加入，且只允许作用于 config.yaml 的 test_port。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
BASE = "/cgi-bin/config.cgi"


def load_hub(name):
    cfg = YAML(typ="safe").load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for h in cfg["hubs"]:
        if h["name"] == name:
            pw = dotenv_values(ROOT / ".env").get(h["password_env"])
            if not pw:
                sys.exit(f"{h['password_env']} 未在 .env 中设置")
            return h, pw
    sys.exit(f"config.yaml 中没有 Hub: {name}")


def client(h, pw, user=None, timeout=15):
    return httpx.Client(
        base_url=f"https://{h['address']}{BASE}",
        auth=(user or h["admin_user"], pw),
        verify=h.get("tls_verify", True),
        timeout=timeout,
    )


def get(c, kind, path):
    """返回 (http_status, body)。路径错误时 HTTP 仍是 200，要看 body。"""
    r = c.get(f"/{kind}/{path}")
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:200]


results = []


def check(item, ok, detail):
    results.append((item, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}: {detail}")


def cmd_read(h, pw):
    c = client(h, pw)
    s, b = get(c, "keys", "service/anywhereusb")
    check("读取服务配置键", s == 200 and b.get("ok"), f"http={s}, {len(b.get('result', []))} 个子键")
    s, b = get(c, "value", "service/anywhereusb")
    v = b["result"]["collapsed"] if b.get("ok") else {}
    check("服务已启用", v.get("enable") == "true", f"enable={v.get('enable')}")
    check("服务端口", str(v.get("port")) == str(h.get("awusb_service_port")),
          f"Hub={v.get('port')} config={h.get('awusb_service_port')}")
    groups = sorted({k.split(".")[1] for k in v if k.startswith("groups.")})
    check("读取分组列表", bool(groups), f"{len(groups)} 个分组: {', '.join(groups)}")
    for g in groups:
        ports = [v[k] for k in sorted(v) if k.startswith(f"groups.{g}.ports.")]
        print(f"        {g}: description={v.get(f'groups.{g}.description')!r} ports={ports}")
    s, b = get(c, "value", "service/anywhereusb/clients")
    cl = b["result"]["collapsed"] if b.get("ok") else {}
    n = len([k for k in cl if k])
    check("读取客户端列表", s == 200 and b.get("ok"), f"{n} 条客户端字段")
    s, b = get(c, "value", "device")
    nports = len({k.split(".")[2] for k in b["result"]["collapsed"] if k.startswith("usb.port.")}) if b.get("ok") else 0
    check("端口数与配置一致", nports == h["ports"], f"Hub={nports} config={h['ports']}")
    for p in ("system/model", "system/serial", "system/firmware", "firmware/version"):
        s, b = get(c, "value", p)
        val = b.get("result", {}).get("value") if b.get("ok") else None
        print(f"        identity 候选 {p}: {'invalid path' if b.get('error') else repr(val)}")
    print("        型号/序列号：config.cgi 下未发现，需另找来源（见 hub-api-notes.md 第 6 节）")


def cmd_errors(h, pw):
    c = client(h, pw)
    s, b = get(c, "value", "service/anywhereusb/no_such_key")
    check("错误键名", s == 200 and bool(b.get("error")), f"http={s} body={json.dumps(b, ensure_ascii=False)}")
    bad = client(h, "wrong-password-probe")
    r = bad.get("/keys/service/anywhereusb")
    check("错误密码", r.status_code == 401, f"http={r.status_code}（仅尝试 1 次）")
    unreachable = httpx.Client(base_url="https://10.255.255.1" + BASE, verify=False, timeout=4)
    t = time.time()
    try:
        unreachable.get("/keys")
        check("Hub 不可达", False, "意外成功")
    except httpx.HTTPError as e:
        check("Hub 不可达", True, f"{type(e).__name__}，{time.time() - t:.1f}s 超时（测试地址 10.255.255.1，非真实 Hub）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True)
    ap.add_argument("test", choices=["read", "errors"])
    a = ap.parse_args()
    h, pw = load_hub(a.hub)
    {"read": cmd_read, "errors": cmd_errors}[a.test](h, pw)
    failed = [i for i, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过" + (f"，失败: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
