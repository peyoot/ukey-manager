"""python -m server：按 config.yaml 的 server 段启动（HTTPS）。"""
import uvicorn

from .config import ROOT, load_config


def main() -> None:
    server = (load_config().model_extra or {}).get("server", {})
    cert, key = ROOT / server.get("tls_cert", "certs/server.crt"), ROOT / server.get("tls_key", "certs/server.key")
    if not (cert.exists() and key.exists()):
        raise SystemExit(f"缺少证书 {cert} / {key}，先运行 bash scripts/gen_cert.sh")
    uvicorn.run(
        "server.app:create_default_app", factory=True,
        host=server.get("bind", "0.0.0.0"), port=server.get("port", 8443),
        ssl_certfile=str(cert), ssl_keyfile=str(key),
    )


if __name__ == "__main__":
    main()
