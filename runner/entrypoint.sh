#!/usr/bin/env bash
# Точка входа контейнера fabric-runner.
# 1. Копирует watchdog из репозитория (ro-маунт) в ~/.hermes/scripts/ (rw-том) —
#    hermes cron --script принимает скрипты только из ~/.hermes/scripts/.
# 2. Регистрирует script-only cron поллинга доски (идемпотентно: проверка по имени).
# 3. Запускает gateway (cron-исполнитель живёт внутри него).

set -euo pipefail

DATA=/opt/data
mkdir -p "$DATA/scripts"
cp /opt/fabric/runner/watchdog.py "$DATA/scripts/watchdog.py"

# Регистрация cron, только если job с таким именем ещё не существует.
if ! hermes cron list 2>/dev/null | grep -q "fabric-board-watchdog"; then
  hermes cron create "*/3 * * * *" \
    --name "fabric-board-watchdog" \
    --script watchdog.py \
    --no-agent \
    --deliver local || true
fi

exec hermes gateway run
