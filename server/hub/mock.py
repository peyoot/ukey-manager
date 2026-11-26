"""测试用 Hub：FakeCgi 在内存里复刻 Hub 的 config.cgi 行为，MockHub 是跑在它上面的 RestHub。

这样 mock 测试同时覆盖了真实适配器的解析和写后读逻辑。FakeCgi 复刻的实测行为：
- 认证失败 → HTTP 401；错误（路径或值）→ HTTP 200 + {"error": true, "msg": ...}
- 数组 DELETE 后序号前移；空数组 GET 返回 {"": ""}，嵌套空数组在对象里不出现
- clients 条目只接受字段 id、description；出厂状态：全部端口在 group01
"""
from __future__ import annotations

import base64
from typing import Any

import httpx

from .rest import BASE, RestHub


def _flatten(node: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _flatten(v, f"{prefix}.{i}" if prefix else str(i), out)
    else:
        out[prefix] = str(node)


class FakeCgi:
    def __init__(self, *, password: str = "pw", ports: int = 8):
        groups = {f"group{n:02d}": {"description": f"Group {n}", "ports": []} for n in range(1, ports + 1)}
        groups["group01"]["ports"] = [str(n) for n in range(1, ports + 1)]  # 出厂：全部端口在 group01
        self.tree: dict = {
            "service": {
                "anywhereusb": {"enable": "true", "port": "18574", "groups": groups, "clients": []}
            },
            "device": {"usb": {"port": {f"port{n}": {"path": ["1-1", "2-1"]} for n in range(1, ports + 1)}}},
            "firmware": {"base_url": "https://fw.invalid/", "version": ""},
        }
        self.password = password
        self.offline = False
        self.calls: list[tuple[str, dict]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- 内部
    def _nav(self, dotted: str) -> Any:
        node: Any = self.tree
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise KeyError(dotted)
        return node

    @staticmethod
    def _err(msg: str) -> httpx.Response:
        return httpx.Response(200, json={"error": True, "msg": msg})

    def _bad(self, path: str, value: str = "") -> httpx.Response:
        return self._err(f"invalid path or value setting '{path} {value}' - check the schema")

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        auth = request.headers.get("authorization", "")
        ok = False
        if auth.startswith("Basic "):
            ok = base64.b64decode(auth[6:]).decode().split(":", 1)[-1] == self.password
        if not ok:
            return httpx.Response(401, text="<HTML><HEAD><TITLE>401 Unauthorized</TITLE></HEAD></HTML>")
        path = request.url.path
        assert path.startswith(BASE), path
        rest = path[len(BASE):]
        params = dict(request.url.params)
        self.calls.append((request.method, {"path": rest, **params}))
        try:
            if request.method == "GET":
                return self._get(rest)
            if request.method == "POST" and rest == "/value":
                return self._post(params)
            if request.method == "DELETE" and rest == "/value":
                return self._delete(params)
        except KeyError:
            return self._bad(params.get("path", rest.replace("/", ".")), params.get("value", ""))
        return httpx.Response(404, text="404")

    def _get(self, rest: str) -> httpx.Response:
        kind, _, p = rest.lstrip("/").partition("/")
        dotted = p.replace("/", ".")
        node = self._nav(dotted)
        if kind == "keys":
            if not isinstance(node, dict):
                return self._bad(dotted)
            return httpx.Response(200, json={"ok": True, "result": list(node.keys())})
        if kind != "value":
            return httpx.Response(404, text="404")
        if isinstance(node, (dict, list)):
            col: dict[str, str] = {}
            _flatten(node, "", col)
            result = {"type": "object" if isinstance(node, dict) else "array", "path": dotted, "collapsed": col or {"": ""}}
        else:
            result = {"type": "string", "path": dotted, "value": str(node)}
        return httpx.Response(200, json={"ok": True, "result": result})

    def _post(self, params: dict) -> httpx.Response:
        path = params["path"]
        if params.get("append") == "true":
            lst = self._nav(path)
            if not isinstance(lst, list):
                return self._bad(path, params.get("value", ""))
            if path.endswith(".clients"):
                fields = {k[len("collapsed["):-1]: v for k, v in params.items() if k.startswith("collapsed[")}
                if "id" not in fields or set(fields) - {"id", "description"}:
                    bad = next(iter(set(fields) - {"id", "description"}), "id")
                    return self._bad(f"{path}.{len(lst)}.{bad}", fields.get(bad, ""))
                lst.append({"id": fields["id"], "description": fields.get("description", ""), "groups": []})
            elif "value" in params:
                lst.append(params["value"])
            else:
                return self._bad(path)
            return httpx.Response(200, json={"ok": True, "result": f"{path}.{len(lst) - 1}"})
        parent_path, _, key = path.rpartition(".")
        parent = self._nav(parent_path)
        if isinstance(parent, dict) and isinstance(parent.get(key), str):
            parent[key] = params["value"]
        elif isinstance(parent, list) and key.isdigit() and int(key) < len(parent) and isinstance(parent[int(key)], str):
            parent[int(key)] = params["value"]
        else:
            return self._bad(path, params.get("value", ""))
        return httpx.Response(200, json={"ok": True})

    def _delete(self, params: dict) -> httpx.Response:
        parent_path, _, key = params["path"].rpartition(".")
        parent = self._nav(parent_path)
        if not (isinstance(parent, list) and key.isdigit() and int(key) < len(parent)):
            return self._bad(params["path"])
        parent.pop(int(key))
        return httpx.Response(200, json={"ok": True})


class MockHub(RestHub):
    def __init__(
        self,
        name: str = "hub-mock",
        *,
        ports: int = 8,
        model: str = "AW08",
        serial: str = "AW08-MOCK",
        password: str = "pw",
        **kw,
    ):
        self.fake = FakeCgi(password=password, ports=ports)
        client = httpx.AsyncClient(
            base_url="https://mock.invalid", transport=self.fake.transport(), auth=("admin", password)
        )
        super().__init__(name, "mock.invalid", "admin", password, ports=ports, model=model, serial=serial, client=client, **kw)
