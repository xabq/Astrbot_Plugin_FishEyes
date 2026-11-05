#!/usr/bin/env bash
# scrcpy 守护脚本：隧道断开重连后自动 adb connect，并重启 scrcpy。
#
# 用法：
#   ./scrcpy-watchdog.sh --video-bit-rate=8M --max-fps=60 --max-size=1280
#   ADDR=127.0.0.1:5555 ./scrcpy-watchdog.sh --no-audio
#
# 依赖：adb、scrcpy 在 PATH 中。

set -uo pipefail

ADDR="${ADDR:-127.0.0.1:5555}"
SCRCPY_BIN="${SCRCPY:-scrcpy}"
RECONNECT_DELAY="${RECONNECT_DELAY:-5}"
RESTART_DELAY="${RESTART_DELAY:-3}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

device_online() {
    adb devices 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$ADDR"
}

ensure_device() {
    if device_online; then
        return 0
    fi
    log "$ADDR 不在线，尝试 adb connect ..."
    adb connect "$ADDR" >/dev/null 2>&1
    sleep 2
    device_online
}

log "守护启动，目标设备 $ADDR"

while true; do
    if ! ensure_device; then
        log "连接失败，${RECONNECT_DELAY}s 后重试"
        sleep "$RECONNECT_DELAY"
        continue
    fi

    log "启动 scrcpy $*"
    "$SCRCPY_BIN" -s "$ADDR" "$@"
    rc=$?
    log "scrcpy 退出 (rc=$rc)，${RESTART_DELAY}s 后重启"

    # 退出码非 0 时顺手把设备连接也重置一下
    if [ "$rc" -ne 0 ]; then
        adb disconnect "$ADDR" >/dev/null 2>&1 || true
    fi
    sleep "$RESTART_DELAY"
done
