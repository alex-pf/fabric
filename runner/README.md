# Раннер фабрики

Постоянно работающий Hermes-агент, который слушает доску продукта и гоняет продуктовый цикл (этапы 2–4 pipeline.md). Решения — [ADR-0002](../docs/decisions/0002-runner-infrastructure.md).

## Архитектура

- **Образ**: `nousresearch/hermes-agent` (официальный, stateless). Мы не собираем свой.
- **Стейт**: `/home/factory/hermes-data` на хосте → `/opt/data` в контейнере. Конфиг, память, навыки, cron переживают пересоздание контейнера.
- **Триггер**: `runner/watchdog.py` по cron каждые 3 минуты. Пустой тик — без LLM. Задача в Todo — один прогон `hermes chat -q` с продуктовым циклом. Прерванная задача (In Progress без живого прогона) возвращается в Todo и продолжается автоматически — [ADR-0003](../docs/decisions/0003-watchdog-auto-resume.md).
- **Обновления**: `runner/update.sh` по хостовому cron каждые 5 минут: `git pull` + `docker compose up -d`. Мерж в main = раскатка.
- **Секреты**: только `/home/factory/.env` на хосте (ключи LLM, токен pfbot13, node-id доски).

## Развёртывание (единственный ручной шаг)

```bash
sudo bash runner/bootstrap.sh   # создаст пользователя, docker, контейнер, cron
# bootstrap остановится и попросит заполнить /home/factory/.env — заполните и перезапустите
sudo bash runner/bootstrap.sh
```

## Поток задачи по доске G1

```text
Todo → (раннер: цикл этапов 2–4) → Need Review → (человек смотрит PR) → Approved → (раннер: squash-merge) → Done
```

- **Todo**: watchdog видит карточку → один прогон `hermes chat -q` (план реализации комментарием в issue → ветка, код, PR, self-review → карточка в Need Review).
- **In Progress без живого прогона** (обрыв по 429, таймауту, падению): watchdog возвращает карточку в Todo (cooldown 30 мин, до 10 попыток, дальше паркинг с комментарием); цикл продолжает существующую ветку/PR, а не начинает с нуля. При маркере квоты LLM в выводе — пауза стартов на 30 мин без расхода попыток: после сброса окна провайдера задачи продолжаются сами.
- **Need Review**: человек смотрит PR, решает. Колонка ждёт человека, раннер не трогает.
- **Approved**: решение человека. Раннер (pfbot13) squash-merge'ит связанный PR и двигает карточку в Done.
- Ворота g1: PR обязателен, approval в GitHub не требуется — его заменяет колонка Approved (ruleset `protect-main`, 0 approvals).

## Что раннер делает / не делает

- Делает: берёт задачу из Todo, ветка, код, PR, self-review; исполняет Approved (merge + Done).
- Не делает: не переводит карточки в Approved (это только человек), не меняет ruleset, не имеет root на хосте, не действует от имени alex-pf.

## Логи

- Контейнер: `docker logs fabric-runner`
- Обновления: `/home/factory/update.log`

## Troubleshooting

- **PermissionError: /opt/data/.env** — внутри образа Hermes работает под UID 10000, а файлы тома созданы от `factory`. Лечение: `chown -R 10000:10000 /home/factory/hermes-data && docker restart fabric-runner`. bootstrap делает это сам на свежих установках.

## Новый продукт / новый сервер

1. `bootstrap.sh` на чистом VPS.
2. `.env`: токен бота + `BOARD_PROJECT_ID` доски нового продукта.
3. Готово — фабрика производит продукт по тому же контуру.
