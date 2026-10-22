#!/bin/sh
# 记忆库热备份：在线一致性快照 + 保留策略 + 可恢复性校验。
# 用法：backup_mind_db.sh [目标目录]
# 说明：sqlite3 .backup 在 WAL 模式下也能取到一致快照，不需要停服务。
set -eu

SRC="${JARVIS_HOME:-$HOME/.jarvis}/loop-data/mind.db"
DEST="${1:-${JARVIS_BACKUP_DIR:-$HOME/.jarvis/backups}}"
KEEP="${JARVIS_BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%dT%H%M%S)"

mkdir -p "$DEST"
[ -f "$SRC" ] || { echo "找不到记忆库：$SRC" >&2; exit 1; }

TMP="$DEST/mind-$STAMP.db.part"
OUT="$DEST/mind-$STAMP.db"

# 一致性快照（WAL 下也安全）
sqlite3 "$SRC" ".backup '$TMP'"
mv "$TMP" "$OUT"

# 校验：能打开、能过 integrity_check、行数与源库一致
CHECK="$(sqlite3 "$OUT" "PRAGMA integrity_check;")"
[ "$CHECK" = "ok" ] || { echo "备份损坏：$OUT（$CHECK）" >&2; exit 1; }
SRC_ROWS="$(sqlite3 "$SRC" "SELECT count(*) FROM entries;")"
DST_ROWS="$(sqlite3 "$OUT" "SELECT count(*) FROM entries;")"
[ "$SRC_ROWS" = "$DST_ROWS" ] || { echo "行数不一致：源 $SRC_ROWS / 备份 $DST_ROWS" >&2; exit 1; }

# 校验过程会建 WAL 边车文件，备份集里不留它们
rm -f "$OUT-wal" "$OUT-shm"
sha256sum "$OUT" > "$OUT.sha256"

# 保留最近 N 份
ls -1t "$DEST"/mind-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f "$old" "$old.sha256"
done

echo "OK $OUT rows=$DST_ROWS"
