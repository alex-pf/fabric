#!/usr/bin/env bash
# Точка входа контейнера fabric-runner.
# 1. Копирует watchdog из репозитория в writable-том (том смонтирован rw, репо — ro).
# 2. Регистрирует script-only cron поллинга доски (идемпотентно).
# 3. Запускает gateway (cron-исполнитель живёт внутри него).

set -euo pipefail

DATA=/opt/data
mkdir -p "$DATA/factory"
cp /opt/fabric/runner/watchdog.py "$DATA/factory/watchdog.py"

# Регистрация cron: script-only job, без LLM на пустых тиках.
# Идемпотентность: hermes cron add сам дедуплицирует по расписанию/команде;
# при расхождении — пересоздаём (drop по имени невозможен без id, поэтому
# стратегия: всегда add; дубликаты с тем же schedule+command склеиваются).
hermes cron add \
  --name "fabric-board-watchdog" \
  --schedule "*/3 * * * *" \
  --command "python3 $DATA/factory/watchdog.py" \
  --script-only || true

exec hermes gateway run
