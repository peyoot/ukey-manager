#!/usr/bin/env bash
# 生成服务器自签证书（仅限演示；生产环境请使用受信 CA 签发的证书）。
# SAN 含 IP:10.70.1.63，可用 SERVER_IP / SERVER_NAME 覆盖；已存在则不覆盖（加 --force 重新生成）。
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER_IP="${SERVER_IP:-10.70.1.63}"
SERVER_NAME="${SERVER_NAME:-ubuntu-srv}"
mkdir -p certs
if [[ -f certs/server.crt && "${1:-}" != "--force" ]]; then
  echo "certs/server.crt 已存在，未覆盖（用 --force 重新生成）"; exit 0
fi
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 825 \
  -keyout certs/server.key -out certs/server.crt \
  -subj "/O=Digi Demo/CN=${SERVER_NAME}" \
  -addext "subjectAltName=IP:${SERVER_IP},DNS:${SERVER_NAME},DNS:localhost,IP:127.0.0.1"
chmod 600 certs/server.key
echo "已生成 certs/server.crt / certs/server.key（SAN: IP:${SERVER_IP}, DNS:${SERVER_NAME}）"
openssl x509 -in certs/server.crt -noout -fingerprint -sha256 -ext subjectAltName
