# Раннер фабрики

Постоянно работающий Hermes-агент, который слушает доску продукта и гоняет продуктовый цикл (этапы 2–4 pipeline.md). Решения — [ADR-0002](../docs/decisions/0002-runner-infrastructure.md).

## Архитектура

- **Образ**: `nousresearch/hermes-agent` (официальный, stateless). Мы не собираем свой.
- **Стейт**: `/home/factory/hermes-data` на хосте → `/opt/data` в контейнере. Конфиг, память, навыки, cron переживают пересоздание контейнера.
- **Триггер**: `runner/watchdog.py` по cron каждые 3 минуты. Пустой тик — без LLM. Задача в Todo — один прогон `hermes chat -q` с продуктовым циклом.
- **Обновления**: `runner/update.sh` по хостовому cron каждые 5 минут: `git pull` + `docker compose up -d`. Мерж в main = раскатка.
- **Секреты**: только `/home/factory/.env` на хосте (ключи LLM, токен pfbot13, node-id доски).

## Развёртывание (единственный ручной шаг)

```bash
sudo bash runner/bootstrap.sh   # создаст пользователя, docker, контейнер, cron
# bootstrap остановится и попросит заполнить /home/factory/.env — заполните и перезапустите
sudo bash runner/bootstrap.sh
```

## Что раннер делает / не делает

- Делает: берёт задачу из Todo, ветка, код, PR по шаблону, self-review, карточку в In Review.
- Не делает: не мёржит PR (merge — только alex-pf), не меняет ruleset, не имеет root на хосте.

## Логи

- Контейнер: `docker logs fabric-runner`
- Обновления: `/home/factory/update.log`

## Новый продукт / новый сервер

1. `bootstrap.sh` на чистом VPS.
2. `.env`: токен бота + `BOARD_PROJECT_ID` доски нового продукта.
3. Готово — фабрика производит продукт по тому же контуру.
