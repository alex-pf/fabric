#!/usr/bin/env bash
# Единственный ручной шаг: bootstrap раннера фабрики на чистый VPS.
# Запуск: sudo bash runner/bootstrap.sh
# Всё остальное (обновления, пересоздание контейнера) — потоком PR через runner/update.sh.

set -euo pipefail

FACTORY_USER="${FACTORY_USER:-factory}"
FACTORY_HOME="/home/${FACTORY_USER}"
REPO_URL="${REPO_URL:-https://github.com/alex-pf/fabric.git}"

echo "== 1/6 Пользователь ${FACTORY_USER}"
if ! id -u "${FACTORY_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${FACTORY_USER}"
fi

echo "== 2/6 Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | bash
fi
usermod -aG docker "${FACTORY_USER}"

echo "== 3/6 Репозиторий fabric"
if [ ! -d "${FACTORY_HOME}/fabric/.git" ]; then
  sudo -u "${FACTORY_USER}" git clone "${REPO_URL}" "${FACTORY_HOME}/fabric"
else
  sudo -u "${FACTORY_USER}" git -C "${FACTORY_HOME}/fabric" pull
fi

echo "== 4/6 Секреты (${FACTORY_HOME}/.env)"
if [ ! -f "${FACTORY_HOME}/.env" ]; then
  cat > "${FACTORY_HOME}/.env" << 'ENV'
# Ключи LLM-провайдера Hermes (см. https://hermes-agent.nousresearch.com/docs/getting-started/installation)
# OPENAI_API_KEY=...
# ANTHROPIC_API_KEY=...
# KIMI_API_KEY=...
# Токен бота GitHub (pfbot13): scope repo + project
GH_TOKEN=
# Node-id доски G1 (узнать: sudo -u factory gh project list --owner alex-pf)
BOARD_PROJECT_ID=
ENV
  chown "${FACTORY_USER}:${FACTORY_USER}" "${FACTORY_HOME}/.env"
  chmod 600 "${FACTORY_HOME}/.env"
  echo "!! Заполни ${FACTORY_HOME}/.env (ключи LLM + GH_TOKEN + BOARD_PROJECT_ID), затем перезапусти bootstrap."
  exit 1
fi

echo "== 5/6 Первичная настройка Hermes (том данных)"
mkdir -p "${FACTORY_HOME}/hermes-data"
chown -R "${FACTORY_USER}:${FACTORY_USER}" "${FACTORY_HOME}/hermes-data"
if [ ! -f "${FACTORY_HOME}/hermes-data/.env" ]; then
  # Ключи Hermes лежат в томе /opt/data — копируем из хостового .env при первом старте.
  sudo -u "${FACTORY_USER}" cp "${FACTORY_HOME}/.env" "${FACTORY_HOME}/hermes-data/.env"
fi

echo "== 6/6 Контейнер + cron обновлений"
sudo -u "${FACTORY_USER}" bash -c "
  cd ${FACTORY_HOME}/fabric/runner
  docker compose up -d
"
CRON_LINE="*/5 * * * * ${FACTORY_HOME}/fabric/runner/update.sh >> ${FACTORY_HOME}/update.log 2>&1"
( crontab -u "${FACTORY_USER}" -l 2>/dev/null | grep -v 'runner/update.sh'; echo "${CRON_LINE}" ) | crontab -u "${FACTORY_USER}" -

echo "Готово. Логи: docker logs fabric-runner; обновления: ${FACTORY_HOME}/update.log"
echo "Следующий шаг — прогон-монтаж v1: задача в Todo на доске G1 уходит в реализацию сама."
