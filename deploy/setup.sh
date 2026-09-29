#!/usr/bin/env bash
# Reddit Grinder Intelligence — 阿里云部署脚本（幂等，可重复执行）
# 参照 youtube-kol-monitor deploy/setup.sh 模式
set -euo pipefail

APP_DIR=/opt/reddit-intel
SRC_DIR=$APP_DIR/src
RUN_USER=reddit-intel
VENV_DIR=$APP_DIR/venv
PORT=8086

echo "==> [1/6] 安装 PostgreSQL（如未安装）"
if ! command -v psql >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y postgresql postgresql-contrib
fi
systemctl enable --now postgresql || true

echo "==> [2/6] 创建独立运行用户"
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd -r -s /bin/false -d "$APP_DIR" "$RUN_USER"
fi
mkdir -p "$APP_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> [3/6] 初始化数据库（幂等：CREATE ROLE/DB + schema.sql）"
# 从 .env 读取 DATABASE_URL，交给 Python 解析并建库
if [ -f "$SRC_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SRC_DIR/.env"
  set +a
fi
python3 - "$DATABASE_URL" "$SRC_DIR/sql/schema.sql" <<'PY'
import os, re, sys, subprocess
url = sys.argv[1]
schema = sys.argv[2]
m = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/([^\s?]+)", url)
if not m:
    sys.exit("invalid DATABASE_URL")
user, password, host, port, db = m.group(1), m.group(2), m.group(3), m.group(4) or "5432", m.group(5)
# 以 postgres 系统用户执行 psql（幂等）
def pg(sql):
    subprocess.run(["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql],
                   check=True, capture_output=True, text=True)
r = subprocess.run(["sudo", "-u", "postgres", "psql", "-tAc",
                    f"SELECT 1 FROM pg_roles WHERE rolname='{user}'"],
                   capture_output=True, text=True)
if r.stdout.strip() != "1":
    pg(f"CREATE ROLE {user} LOGIN PASSWORD '{password}'")
r = subprocess.run(["sudo", "-u", "postgres", "psql", "-tAc",
                    f"SELECT 1 FROM pg_database WHERE datname='{db}'"],
                   capture_output=True, text=True)
if r.stdout.strip() != "1":
    pg(f"CREATE DATABASE {db} OWNER {user}")
print("database ready")
PY
# 应用 schema（幂等，ON CONFLICT 安全）
PGPASSWORD="${DATABASE_URL#*://*:}"  # 占位，实际用 psql conninfo 更安全
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$SRC_DIR/sql/schema.sql" >/dev/null

echo "==> [4/6] Python venv + 依赖（阿里云 PyPI 镜像）"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SRC_DIR/requirements.txt" \
  -i https://mirrors.aliyun.com/pypi/simple/ -q

echo "==> [5/6] systemd units"
cp "$SRC_DIR/deploy/reddit-intel-api.service" /etc/systemd/system/
cp "$SRC_DIR/deploy/reddit-intel-collector.service" /etc/systemd/system/
cp "$SRC_DIR/deploy/reddit-intel-collector.timer" /etc/systemd/system/
sed -i "s|__APP_DIR__|$APP_DIR|g; s|__RUN_USER__|$RUN_USER|g; s|__PORT__|$PORT|g" \
  /etc/systemd/system/reddit-intel-api.service
systemctl daemon-reload

echo "==> [6/6] 启动服务"
systemctl enable --now reddit-intel-api
systemctl enable --now reddit-intel-collector.timer
systemctl restart reddit-intel-api
systemctl restart reddit-intel-collector.timer
systemctl --no-pager status reddit-intel-api --lines=5 || true

echo "==> 部署完成：API :$PORT, collector timer 已启用"
