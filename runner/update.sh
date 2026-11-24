#!/usr/bin/env bash
# Обновление раннера: раскатывает merge'и из GitHub без человека.
# Вызывается хостовым cron каждые 5 минут (ставит bootstrap.sh).

set -euo pipefail

FACTORY_HOME="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${FACTORY_HOME}/fabric"

OLD_HEAD=$(git rev-parse HEAD)
git pull --ff-only
NEW_HEAD=$(git rev-parse HEAD)

# Образ stateless: пересоздаём контейнер, стейт в томе /home/factory/hermes-data сохраняется.
docker compose -f runner/docker-compose.yml pull
if [ "${OLD_HEAD}" != "${NEW_HEAD}" ] || [ -n "${FORCE_RECREATE:-}" ]; then
  docker compose -f runner/docker-compose.yml up -d --force-recreate
  echo "$(date -Is) обновлено: ${OLD_HEAD:0:8} -> ${NEW_HEAD:0:8}"
else
  docker compose -f runner/docker-compose.yml up -d
fi
