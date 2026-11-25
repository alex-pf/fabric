#!/usr/bin/env python3
"""Watchdog доски продукта: поллинг без LLM.

Логика по колонкам доски G1:
- Todo        → один прогон продуктового цикла через hermes chat -q
                (Implementation → Quality Gates → Self-Review, этапы 2–4 pipeline.md;
                карточку агент двигает в Need Review).
- Need Review → ничего: ждёт решения человека.
- Approved    → человек принял решение; бот исполняет: squash-merge связанного PR
                (pfbot13) и двигает карточку в Done.

Пустой тик — выход без LLM, стоимость ноль.

Окружение (секреты — в /home/factory/.env на хосте, проброшены compose):
  GH_TOKEN          токен pfbot13 (scope: project, repo)
  BOARD_PROJECT_ID  node-id доски G1 (PVT_kw...)
  TICK_LOCK         файл-блокировка от параллельных тиков
"""

import json
import os
import subprocess
import sys
import urllib.request

API = "https://api.github.com"
GRAPHQL = API + "/graphql"
TICK_LOCK = os.environ.get("TICK_LOCK", "/tmp/fabric-watchdog.lock")

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
2. Создай ветку feature/{number}-... от свежего main, реализуй задачу, conventional commits, коммиты от pfbot13.
3. Открой PR (структура: что сделано / как проверено / критерии приёмки / self-review), заполни self-review.
4. Проверки должны быть зелёными — чини сам. НЕ мёржи PR.
5. Переведи карточку в «Need Review».
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
    nodes = (data.get("data", {}).get("node", {}) or {}).get("items", {}).get("nodes", [])
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
    repo = issue["repository"]
    merge = api("PUT", f"/repos/{repo['owner']['login']}/{repo['name']}/pulls/{pr['number']}/merge",
                {"merge_method": "squash"})
    print(f"#{issue['number']}: PR #{pr['number']} merged ({str(merge.get('sha', ''))[:8]})")


def main() -> int:
    project_id = os.environ.get("BOARD_PROJECT_ID")
    if not project_id:
        print("BOARD_PROJECT_ID не задан — пропуск тика", file=sys.stderr)
        return 0

    items = fetch_items(project_id)
    todo = [it for it in items if status_of(it) == "Todo"]
    approved = [it for it in items if status_of(it).lower() == "approved"]

    if not todo and not approved:
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
            move_card(project_id, item, "Done")
        except Exception as exc:  # noqa: BLE001 — watchdog не должен умирать от одной карточки
            print(f"Approved: ошибка {exc}", file=sys.stderr)

    if todo:
        issue = todo[0]["content"]
        print(f"задача в Todo: #{issue['number']} {issue['title']} — запуск цикла")
        prompt = CYCLE_PROMPT.format(
            number=issue["number"], title=issue["title"], url=issue["url"],
            repo=issue["repository"]["name"], board=project_id,
        )
        subprocess.run(["hermes", "chat", "-q", prompt], timeout=3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
