#!/usr/bin/env python3
"""Watchdog доски продукта: поллинг без LLM.

Логика по колонкам доски G1:
- Todo        → один прогон продуктового цикла через hermes chat -q
                (Implementation → Quality Gates → Self-Review, этапы 2–4 pipeline.md;
                перед кодингом агент публикует план реализации комментарием в issue,
                карточку двигает в Need Review).
- In Progress → при свободном локе карточка считается прерванной (429, таймаут,
                падение прогона): возврат в Todo с cooldown и лимитом попыток
                (ADR-0003). Дальше она проходит штатный путь, а промпт цикла
                умеет продолжать существующую ветку/PR.
- Need Review → ничего: ждёт решения человека.
- Approved    → человек принял решение; бот исполняет: squash-merge связанного PR
                (pfbot13) и двигает карточку в Done.

Квота LLM: маркеры лимита в выводе прогона (429 / usage limit) → пауза стартов
циклов на QUOTA_BACKOFF_MIN, попытка возобновления при этом не расходуется —
после сброса окна провайдера задачи продолжаются сами.

Пустой тик — выход без LLM, стоимость ноль.

Окружение (секреты — в /home/factory/.env на хосте, проброшены compose):
  GH_TOKEN             токен pfbot13 (scope: project, repo)
  BOARD_PROJECT_ID     node-id доски G1 (PVT_kw...)
  TICK_LOCK            файл-блокировка от параллельных тиков
  RESUME_STATE         JSON состояния возобновлений (default /opt/data/factory/resume-state.json)
  RESUME_COOLDOWN_MIN  мин. пауза между возобновлениями одной карточки (default 30)
  MAX_RESUME_ATTEMPTS  лимит возобновлений карточки до паркинга (default 10)
  QUOTA_BACKOFF_MIN    пауза стартов циклов после маркера квоты (default 30)
  CYCLE_TIMEOUT        таймаут одного цикла, сек (default 3600)
  WATCHDOG_DRY_RUN=1   без побочных эффектов: логирует решения, ничего не меняет
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
GRAPHQL = API + "/graphql"
TICK_LOCK = os.environ.get("TICK_LOCK", "/tmp/fabric-watchdog.lock")
RESUME_STATE = os.environ.get("RESUME_STATE", "/opt/data/factory/resume-state.json")
RESUME_COOLDOWN_MIN = int(os.environ.get("RESUME_COOLDOWN_MIN", "30"))
MAX_RESUME_ATTEMPTS = int(os.environ.get("MAX_RESUME_ATTEMPTS", "10"))
QUOTA_BACKOFF_MIN = int(os.environ.get("QUOTA_BACKOFF_MIN", "30"))
CYCLE_TIMEOUT = int(os.environ.get("CYCLE_TIMEOUT", "3600"))
DRY_RUN = os.environ.get("WATCHDOG_DRY_RUN") == "1"
# Маркеры исчерпания квоты LLM в выводе hermes (нижний регистр).
QUOTA_MARKERS = ("usage limit", "ratelimiterror", "http 429", "error code: 429", "status code: 429")


def load_env_fallback(path: str = "/opt/data/.env") -> None:
    """Cron-скрипты получают санитизированное окружение — подтягиваем .env сами."""
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    except OSError:
        pass


load_env_fallback()

ITEMS_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      items(first: 50) {
        nodes {
          id
          fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          content {
            ... on Issue { id number title url repository { name owner { login } } }
          }
        }
      }
    }
  }
}
"""

# PR, ссылку на который нашёл issue (events CROSS_REFERENCE).
LINKED_PR_QUERY = """
query($issueId: ID!) {
  node(id: $issueId) {
    ... on Issue {
      timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
        nodes {
          ... on CrossReferencedEvent {
            source { ... on PullRequest { number state url } }
          }
        }
      }
    }
  }
}
"""

FIELD_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}
"""

CYCLE_PROMPT = """Ты — раннер фабрики Fabric. Задача с доски продукта:
#{number} {title} ({url}) в репозитории {repo}.

Выполни продуктовый цикл по docs/pipeline.md (этапы 2–4, без Human Review):
1. Переведи карточку проекта (id доски: {board}) в «In Progress» через gh api (GH_TOKEN в env).
2. До начала кодинга опубликуй комментарием в issue подробный план реализации (gh issue comment):
   как понял задачу и критерии приёмки, пошаговый план с затрагиваемыми файлами/модулями,
   подход к тестам и проверке. Человек должен видеть намерение агента до появления кода.
3. Реализуй задачу в ветке feature/{number}-... от свежего main: conventional commits, коммиты от pfbot13.
4. Открой PR (структура: что сделано / как проверено / критерии приёмки / self-review), заполни self-review,
   сошлись в PR на комментарий с планом.
5. Проверки должны быть зелёными — чини сам. НЕ мёржи PR.
6. Переведи карточку в «Need Review».

Возобновление прерванной работы (карточка уже в In Progress, ветка или PR существуют):
- Рабочая папка: /opt/data/cache/scratch/{repo}-{number}. Если она есть — работай в ней, не клонируй заново.
- Ветка feature/{number}-* уже существует (локально или на origin) — продолжай в ней с текущего состояния
  (git status, git log), не начинай с нуля и не пересоздавай ветку.
- PR по задаче уже открыт — НЕ создавай новый: доведи checks до зелёных и переведи карточку в «Need Review».
- Комментарий с планом уже опубликован — не дублируй; если фактический план изменился,
  опубликуй уточняющий комментарий.

Ограничения: действуй как pfbot13; никаких действий от имени alex-pf. Если задача неясна — комментарием в issue спроси автора и оставь карточку в In Progress."""


def graphql(query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        GRAPHQL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {os.environ['GH_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def api(method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={
            "Authorization": f"bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body else {}


def fetch_items(project_id: str) -> list:
    data = graphql(ITEMS_QUERY, {"projectId": project_id})
    node = data.get("data", {}).get("node")
    if node is None:
        # Молчаливый выход здесь маскировал недоступность доски (бот не в коллабораторах проекта).
        raise RuntimeError(
            f"доска {project_id} недоступна для токена (node=null): "
            "проверь, что бот добавлен в коллабораторы проекта"
        )
    nodes = node.get("items", {}).get("nodes", [])
    return [it for it in nodes if it.get("content")]


def status_of(item: dict) -> str:
    return (item.get("fieldValueByName") or {}).get("name") or ""


def status_field(project_id: str) -> dict:
    data = graphql(FIELD_QUERY, {"projectId": project_id})
    return (data.get("data", {}).get("node", {}) or {}).get("field") or {}


def move_card(project_id: str, item: dict, column: str) -> None:
    field = status_field(project_id)
    opt = next((o for o in field.get("options", []) if o["name"].lower() == column.lower()), None)
    if not opt:
        print(f"колонка '{column}' не найдена на доске", file=sys.stderr)
        return
    graphql(
        """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId,
            fieldId: $fieldId, value: { singleSelectOptionId: $optionId }
          }) { clientMutationId }
        }
        """,
        {
            "projectId": project_id,
            "itemId": item["id"],
            "fieldId": field["id"],
            "optionId": opt["id"],
        },
    )


def linked_open_pr(issue_node_id: str) -> dict | None:
    data = graphql(LINKED_PR_QUERY, {"issueId": issue_node_id})
    nodes = ((data.get("data", {}).get("node", {}) or {}).get("timelineItems", {}) or {}).get("nodes", [])
    for ev in nodes:
        pr = (ev or {}).get("source") or {}
        if pr and pr.get("state") == "OPEN":
            return pr
    return None


def merge_approved(item: dict) -> None:
    issue = item["content"]
    pr = linked_open_pr(issue["id"])
    if not pr:
        print(f"#{issue['number']}: в Approved нет открытого PR — пропуск", file=sys.stderr)
        return
    if DRY_RUN:
        print(f"dry-run: #{issue['number']} — merge PR #{pr['number']} + карточка в Done")
        return
    repo = issue["repository"]
    merge = api("PUT", f"/repos/{repo['owner']['login']}/{repo['name']}/pulls/{pr['number']}/merge",
                {"merge_method": "squash"})
    print(f"#{issue['number']}: PR #{pr['number']} merged ({str(merge.get('sha', ''))[:8]})")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def load_state() -> dict:
    try:
        with open(RESUME_STATE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    state.setdefault("cards", {})
    state.setdefault("quota_until", None)
    return state


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    os.makedirs(os.path.dirname(RESUME_STATE), exist_ok=True)
    tmp = RESUME_STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, RESUME_STATE)


def quota_active(state: dict) -> bool:
    until = parse_ts(state.get("quota_until"))
    if until is None:
        return False
    if utcnow() < until:
        return True
    state["quota_until"] = None  # пауза истекла
    return False


def requeue_interrupted(project_id: str, items: list, state: dict) -> None:
    """In Progress при свободном локе = прогон умер (429, таймаут, падение).

    Возвращаем карточку в Todo — дальше штатный путь; промпт цикла умеет
    продолжать существующую ветку/PR (ADR-0003).
    """
    now = utcnow()
    cards = state["cards"]
    in_progress = {str(it["content"]["number"]): it for it in items if status_of(it) == "In Progress"}
    # Чистим состояние по карточкам, вышедшим из In Progress (завершились или ушли человеку).
    for num in list(cards):
        if num not in in_progress:
            del cards[num]
    for num, item in in_progress.items():
        entry = cards.setdefault(num, {"attempts": 0, "last_requeue": None, "parked": False})
        if entry.get("parked"):
            continue
        last = parse_ts(entry.get("last_requeue"))
        if last and (now - last) < timedelta(minutes=RESUME_COOLDOWN_MIN):
            continue
        issue = item["content"]
        if entry["attempts"] >= MAX_RESUME_ATTEMPTS:
            entry["parked"] = True
            print(f"#{num}: {MAX_RESUME_ATTEMPTS} возобновлений не помогли — паркинг, нужен человек",
                  file=sys.stderr)
            if not DRY_RUN:
                repo = issue["repository"]
                api("POST",
                    f"/repos/{repo['owner']['login']}/{repo['name']}/issues/{num}/comments",
                    {"body": f"Автовозобновление исчерпало {MAX_RESUME_ATTEMPTS} попыток — задача "
                             "остаётся в In Progress, нужен человек (см. alex-pf/fabric#21)."})
            continue
        entry["attempts"] += 1
        entry["last_requeue"] = now.isoformat()
        print(f"#{num}: прерванная задача — возврат в Todo (попытка {entry['attempts']})")
        if not DRY_RUN:
            move_card(project_id, item, "Todo")


def run_cycle(project_id: str, item: dict, state: dict) -> None:
    issue = item["content"]
    print(f"задача в Todo: #{issue['number']} {issue['title']} — запуск цикла")
    if DRY_RUN:
        print("dry-run: цикл не запускается")
        return
    prompt = CYCLE_PROMPT.format(
        number=issue["number"], title=issue["title"], url=issue["url"],
        repo=issue["repository"]["name"], board=project_id,
    )
    try:
        proc = subprocess.run(["hermes", "chat", "-q", prompt],
                              timeout=CYCLE_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        print(f"#{issue['number']}: цикл превысил {CYCLE_TIMEOUT}с — карточка останется "
              "в In Progress и будет возобновлена следующими тиками", file=sys.stderr)
        return
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    print("\n".join(output.splitlines()[-15:]))  # хвост прогона — в лог тика
    if any(marker in output.lower() for marker in QUOTA_MARKERS):
        state["quota_until"] = (utcnow() + timedelta(minutes=QUOTA_BACKOFF_MIN)).isoformat()
        print(f"квота LLM исчерпана — пауза стартов циклов на {QUOTA_BACKOFF_MIN} мин "
              "(попытка возобновления не расходуется)", file=sys.stderr)
    elif proc.returncode != 0:
        print(f"#{issue['number']}: цикл завершился с кодом {proc.returncode}", file=sys.stderr)


def main() -> int:
    project_id = os.environ.get("BOARD_PROJECT_ID")
    if not project_id:
        print("BOARD_PROJECT_ID не задан — пропуск тика", file=sys.stderr)
        return 0

    items = fetch_items(project_id)
    state = load_state()
    todo = [it for it in items if status_of(it) == "Todo"]
    approved = [it for it in items if status_of(it).lower() == "approved"]
    interrupted = [it for it in items if status_of(it) == "In Progress"]

    if not todo and not approved and not interrupted:
        return 0  # пустой тик — без LLM

    lock = open(TICK_LOCK, "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("предыдущий цикл ещё выполняется — пропуск тика", file=sys.stderr)
        return 0

    for item in approved:  # решения человека исполняем в первую очередь
        try:
            merge_approved(item)
            if not DRY_RUN:
                move_card(project_id, item, "Done")
        except Exception as exc:  # noqa: BLE001 — watchdog не должен умирать от одной карточки
            print(f"Approved: ошибка {exc}", file=sys.stderr)

    requeue_interrupted(project_id, items, state)

    if todo and quota_active(state):
        print("пауза по квоте LLM — циклы не стартуют", file=sys.stderr)
    elif todo:
        run_cycle(project_id, todo[0], state)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
