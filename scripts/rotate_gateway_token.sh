#!/bin/sh
# 轮换语音网关令牌：原子写入新令牌 → 重启网关 → 提示各设备更新。
#
# 为什么需要：令牌是设备共享的 Bearer，一旦泄露（旧手机、截图、日志）没有撤销手段，
# 只能换新令牌并让所有设备重填。此脚本把"生成—落盘—重启—验证"做成一步。
#
# 用法：
#   scripts/rotate_gateway_token.sh            # 生成新令牌并重启
#   scripts/rotate_gateway_token.sh --dry-run  # 只显示将要做什么
set -eu

TOKEN_FILE="${JARVIS_TOKEN_FILE:-$HOME/.jarvis/voice-gateway.token}"
SERVICE="gui/$(id -u)/com.jarvis.voice-gateway"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

if [ ! -d "$(dirname "$TOKEN_FILE")" ]; then
  echo "目录不存在：$(dirname "$TOKEN_FILE")" >&2
  exit 1
fi

# 44 字符 ASCII（与既有令牌同量级），不含空白
NEW=$(LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 43)

if [ "$DRY" -eq 1 ]; then
  echo "将写入：${TOKEN_FILE}（内容不打印）"
  echo "将重启：launchctl kickstart -k $SERVICE"
  exit 0
fi

OLD_SUM=$( [ -f "$TOKEN_FILE" ] && shasum -a 256 "$TOKEN_FILE" | cut -c1-12 || echo "none" )

# 原子替换：同目录临时文件 + mv，权限 0600
TMP="$TOKEN_FILE.new.$$"
umask 077
printf '%s\n' "$NEW" > "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$TOKEN_FILE"
NEW_SUM=$(shasum -a 256 "$TOKEN_FILE" | cut -c1-12)

echo "令牌已更换：${OLD_SUM} → ${NEW_SUM}（不显示明文）"

if launchctl list | grep -q com.jarvis.voice-gateway; then
  launchctl kickstart -k "$SERVICE"
  sleep 3
  if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "网关已重启，端口在监听"
  else
    echo "警告：网关重启后端口未监听，请查看 ~/.jarvis/voice-gateway.log" >&2
  fi
else
  echo "提示：com.jarvis.voice-gateway 未注册，跳过重启"
fi

cat <<'EOF'

接下来需要手动完成（脚本不代劳）：
1. 在每台设备的设置页粘贴新令牌（Mac 菜单栏 App、iPhone、Watch）。
2. 确认旧令牌已被拒：用旧值连一次应返回 401。
3. 如果旧令牌曾出现在截图/聊天/日志里，视为已泄露，本次轮换即撤销它。
EOF
