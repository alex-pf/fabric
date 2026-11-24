#!/usr/bin/env python3
"""Watchdog доски продукта: поллинг без LLM.

Каждый тик: есть ли карточки в Todo на доске G1 (GitHub Projects API).
Пусто — молча выходим (стоимость: ноль).
Задача есть — один раз запускаем продуктовый цикл через hermes chat -q
(Implementation → Quality Gates → Self-Review, этапы 2–4 pipeline.md).

Окружение (секреты — в /home/factory/.env на хосте, сюда проброшены compose):
  GH_TOKEN        токен pfbot13 (scope: project, repo)
  BOARD_PROJECT_ID  node-id доски G1 (PVT_kw...), смотри gh project list
  TICK_LOCK       файл-блокировка, чтобы два тика не гоняли цикл параллельно
"""

import json
import os
import subprocess
import sys
import urllib.request

GRAPHQL = "https://api.github.com/graphql"
TICK_LOCK = os.environ.get("TICK_LOCK", "/tmp/fabric-watchdog.lock")


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


# Todo-колонка доски: поле Status == "Todo".
QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      items(first: 50) {
        nodes {
          id
          fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          content { ... on Issue { number title url repository { name } } }
        }
      }
    }
  }
}
"""


def main() -> int:
    project_id = os.environ.get("BOARD_PROJECT_ID")
    if not project_id:
        print("BOARD_PROJECT_ID не задан — пропуск тика", file=sys.stderr)
        return 0

    data = graphql(QUERY, {"projectId": project_id})
    items = (data.get("data", {}).get("node", {}) or {}).get("items", {}).get("nodes", [])
    todo = [
        it for it in items
        if (it.get("fieldValueByName") or {}).get("name") == "Todo"
        and it.get("content")
    ]
    if not todo:
        return 0  # пустой тик — без LLM, без шума

    lock = open(TICK_LOCK, "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("предыдущий цикл ещё выполняется — пропуск тика", file=sys.stderr)
        return 0

    task = todo[0]
    issue = task["content"]
    prompt = f"""Ты — раннер фабрики Fabric. Задача с доски продукта:
#{issue['number']} {issue['title']} ({issue['url']}) в репозитории {issue['repository']['name']}.

Выполни продуктовый цикл по docs/pipeline.md (этапы 2–4, без Human Review):
1. Переведи карточку проекта в In Progress (gh project item-edit, доска G1).
2. Создай ветку feature/<n>-... от свежего main, реализуй задачу, conventional commits.
3. Открой PR по шаблону .github/pull_request_template.md, заполни self-review.
4. Проверки должны быть зелёными — агент чинит сам. НЕ мёржи PR (merge — только человек).
5. Переведи карточку в In Review.
Ограничения: действуй как pfbot13; никаких действий от имени alex-pf."""

    print(f"задача в Todo: #{issue['number']} {issue['title']} — запуск цикла")
    subprocess.run(["hermes", "chat", "-q", prompt], timeout=3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
