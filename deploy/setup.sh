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
echo "--> 数据库: SQLite (ADR-002，无需 PostgreSQL)"
if command -v python3 >/dev/null 2>&1; then
  echo "--> python3: $(python3 --version 2>/dev/null || echo unknown)"
else
  echo "--> python3: none"
fi
echo "--> 内存: $(free -h 2>/dev/null | awk '/Mem:/{print $2" 总, "$7" 可用"}' || echo unknown)"
free -h 2>/dev/null | grep -E "Mem|Swap" | sed 's/^/    /' || true
echo "--> CPU: $(nproc) 核"
echo "--> 磁盘: $(df -h / 2>/dev/null | awk 'NR==2{print $2" 总, "$4" 可用"}' || echo unknown)"
echo "--> 公网IP: $(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo unknown)"
echo "--> 内存占用 Top5:"
ps aux --sort=-%mem 2>/dev/null | head -6 | awk '{printf "    %s %s%% %sMB %s\n", $1, $4, int($6/1024), substr($0, index($0,$11), 60)}' || true

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

# 低内存服务器：确保至少有 2G swap 再装包（dnf/rpm 事务峰值内存）
MEM_TOTAL_MB=$(free -m | awk '/Mem:/{print $2}')
SWAP_TOTAL_MB=$(free -m | awk '/Swap:/{print $2}')
if [ "${MEM_TOTAL_MB:-0}" -lt 1024 ]; then
  echo "==> 低内存服务器（${MEM_TOTAL_MB}MB），调高 swappiness 利用 swap + 扩展 swap 到 2G"
  # 根因修复：swappiness=0 时内核不主动换出，物理内存耗尽即 global_oom，swap 形同虚设
  SWAPPINESS=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)
  if [ "$SWAPPINESS" != "unknown" ] && [ "$SWAPPINESS" -lt 20 ]; then
    echo 60 > /proc/sys/vm/swappiness 2>/dev/null || true
    echo "vm.swappiness = 60" > /etc/sysctl.d/99-reddit-swap.conf 2>/dev/null || true
    echo "--> swappiness: $SWAPPINESS -> $(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown)"
  fi
  if [ "${SWAP_TOTAL_MB:-0}" -lt 2048 ]; then
    if [ ! -f /swapfile2 ]; then
      fallocate -l 2G /swapfile2 2>/dev/null || dd if=/dev/zero of=/swapfile2 bs=1M count=2048
      chmod 600 /swapfile2
      mkswap /swapfile2 && swapon /swapfile2
    else
      swapon /swapfile2 2>/dev/null || true
    fi
    grep -q "swapfile2" /etc/fstab 2>/dev/null || echo "/swapfile2 none swap sw 0 0" >> /etc/fstab
  fi
  free -h | grep -E "Mem|Swap" | sed 's/^/    /' || true
fi

echo "==> [1/6] 存储方案：SQLite（ADR-002，单机低频，无需 PostgreSQL）"
echo "    - 数据文件: /opt/reddit-intel/data/reddit_intel.db"
echo "    - 理由: 服务器 409Mi 内存，PG 安装/常驻 OOM；SQLite 零运维、无守护进程"

echo "==> [2/6] 创建独立用户 + 目录 + 复制代码"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"
mkdir -p "$APP_DIR"/{app,collector,analyst,sql,deploy,tests,docs,data}
# 用 tar 复制并排除 .git/.venv/.env（不依赖 rsync）
if [ -d "$SRC_DIR/app" ]; then
  REPO_SRC="$SRC_DIR"
else
  REPO_SRC="$SRC_DIR/reddit-grinder-intel"
fi
tar --exclude='.git' --exclude='.venv' --exclude='.env' -C "$REPO_SRC" -cf - . | tar -C "$APP_DIR" -xf -
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> [3/6] 初始化 SQLite 数据库（幂等：schema.sql，ADR-002）"
DB_PATH="/opt/reddit-intel/data/reddit_intel.db"
mkdir -p "$(dirname "$DB_PATH")"
chown -R "$RUN_USER":"$RUN_USER" "$(dirname "$DB_PATH")"
python3 - "$DB_PATH" "$APP_DIR/sql/schema.sql" <<'PY'
import sqlite3, sys
db_path, schema = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db_path)
try:
    with open(schema, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    print("schema.sql applied ->", db_path)
finally:
    conn.close()
PY
chown "$RUN_USER":"$RUN_USER" "$DB_PATH"
echo "==> database ready (SQLite, ADR-002)"

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
