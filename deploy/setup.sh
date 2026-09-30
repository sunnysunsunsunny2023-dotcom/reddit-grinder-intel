#!/usr/bin/env bash
# Reddit Grinder Intelligence 部署脚本（阿里云服务器 root 执行）
# 用法:
#   CI 模式: bash setup.sh /tmp/reddit-intel-deploy   <repo 克隆目录>
#   手动模式: bash setup.sh                            <默认 /root/deploy_tmp>
set -euo pipefail

APP_DIR=/opt/reddit-intel
SRC_DIR="${1:-/root/deploy_tmp}"
RUN_USER=reddit-intel
PORT=8086

echo "==> [0/6] 系统探测"
cat /etc/os-release 2>/dev/null | head -4 || true
echo "--> 包管理器: $(command -v apt-get dnf yum apk 2>/dev/null || echo none)"
echo "--> psql: $(command -v psql || echo none)"
echo "--> python3: $(command -v python3 || echo none) $($(command -v python3 >/dev/null 2>&1 && python3 --version || true))"

# 包管理分支
if command -v apt-get >/dev/null 2>&1; then
  PM="apt"
elif command -v dnf >/dev/null 2>&1; then
  PM="dnf"
elif command -v yum >/dev/null 2>&1; then
  PM="yum"
else
  echo "无法识别的包管理器，部署中止"; exit 1
fi
echo "--> 使用包管理器: $PM"

echo "==> [1/6] 安装 PostgreSQL（如未安装）"
if ! command -v psql >/dev/null 2>&1; then
  if [ "$PM" = "apt" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y postgresql postgresql-contrib
  else
    dnf install -y postgresql-server postgresql postgresql-contrib
    # RHEL 系需要显式 initdb
    if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
      postgresql-setup --initdb || su - postgres -c "/usr/bin/initdb -D /var/lib/pgsql/data" || true
    fi
  fi
fi
systemctl enable --now postgresql || true

echo "==> [2/6] 创建独立用户 + 目录 + 复制代码"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"
mkdir -p "$APP_DIR"/{app,collector,analyst,sql,deploy,tests,docs}
# 用 tar 复制并排除 .git/.venv/.env（不依赖 rsync）
if [ -d "$SRC_DIR/app" ]; then
  REPO_SRC="$SRC_DIR"
else
  REPO_SRC="$SRC_DIR/reddit-grinder-intel"
fi
tar --exclude='.git' --exclude='.venv' --exclude='.env' -C "$REPO_SRC" -cf - . | tar -C "$APP_DIR" -xf -
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> [3/6] 初始化数据库（幂等：CREATE ROLE/DB + schema.sql）"
if [ -f "$APP_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
  set +a
fi
python3 - "$DATABASE_URL" "$APP_DIR/sql/schema.sql" <<'PY'
import re, subprocess, sys
url, schema = sys.argv[1], sys.argv[2]
m = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+)(?::(\d+))?/([^\s?]+)", url)
if not m:
    sys.exit("invalid DATABASE_URL")
user, password, host, port, db = m.group(1), m.group(2), m.group(3), m.group(4) or "5432", m.group(5)
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
print("database role/db ready")
PY
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$APP_DIR/sql/schema.sql" >/dev/null
echo "==> schema.sql applied"

echo "==> [4/6] Python venv + 依赖（阿里云 PyPI 镜像）"
PY_BIN="${PY_BIN:-python3}"
# 版本太老的 python3 无法装 pydantic v2 / 新版 fastapi，尝试安装更高版本
PY_VER=$("$PY_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "--> 使用 python: $PY_BIN ($PY_VER)"
if [ "$PM" != "apt" ] && { [ "$PY_VER" = "3.6" ] || [ "$PY_VER" = "3.7" ]; }; then
  if command -v python3.11 >/dev/null 2>&1; then
    PY_BIN=python3.11
  else
    dnf install -y python3.11 || true
  fi
  if command -v python3.11 >/dev/null 2>&1; then PY_BIN=python3.11; fi
  PY_VER=$("$PY_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  echo "--> 升级后使用 python: $PY_BIN ($PY_VER)"
fi
if ! "$PY_BIN" -m venv --help >/dev/null 2>&1; then
  if [ "$PM" = "apt" ]; then
    apt-get update -qq && apt-get install -y -qq python3-venv
  else
    dnf install -y python3-virtualenv python3-pip || true
  fi
fi
if [ ! -d "$APP_DIR/.venv" ]; then
  "$PY_BIN" -m venv "$APP_DIR/.venv"
fi
if [ ! -x "$APP_DIR/.venv/bin/pip" ]; then
  "$APP_DIR/.venv/bin/python" -m ensurepip --upgrade --default-pip 2>/dev/null || true
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q -i https://mirrors.aliyun.com/pypi/simple/
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q -i https://mirrors.aliyun.com/pypi/simple/

echo "==> [5/6] systemd units"
cp "$APP_DIR/deploy/reddit-intel-api.service" /etc/systemd/system/
cp "$APP_DIR/deploy/reddit-intel-collector.service" /etc/systemd/system/
cp "$APP_DIR/deploy/reddit-intel-collector.timer" /etc/systemd/system/
systemctl daemon-reload

echo "==> [6/6] 启动服务"
systemctl enable --now reddit-intel-api
systemctl enable --now reddit-intel-collector.timer
systemctl restart reddit-intel-api
systemctl restart reddit-intel-collector.timer
systemctl --no-pager status reddit-intel-api --lines=5 || true

echo "==> 部署完成：API :$PORT, collector timer 2x/天"
echo "==> 手动验证："
echo "    sudo -u reddit-intel $APP_DIR/.venv/bin/python -m collector.scheduler --subreddits pourover espresso --limit 10"
