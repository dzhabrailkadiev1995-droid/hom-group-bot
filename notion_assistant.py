#!/usr/bin/env python3
"""
HOM Assistant — Telegram-бот с доступом к Notion через Claude AI
"""

import os, logging, time, json, requests, tempfile
from pathlib import Path
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
import anthropic

load_dotenv(Path(__file__).parent / ".env")

TG_TOKEN      = os.environ.get("NOTION_ASSISTANT_TOKEN", "")
OWNER_ID      = int(os.environ.get("OWNER_CHAT_ID", "1043062064"))
NOTION_TOKEN  = os.environ.get("NOTION_TOKEN", "")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
N_API  = "https://api.notion.com/v1"
N_HDR  = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

DB_OBJECTS = "27ed68b9-d7a6-4bfb-85b1-8be6a80f1b50"
DB_WORKS   = "d7b732a4-513e-45f8-b731-2431baa6fecf"
DB_CLIENTS = "705c1d7c-b038-4518-8339-68d09dcdab3f"
DB_LEADS   = "3a2e7ba2-798c-81f7-a1d3-f8f867bfeb6c"
DB_DEALS   = "3a2e7ba2-798c-8193-9898-ed2110b2818c"
DB_EXPENSES= "30e81b7f-f4b8-4501-8722-927f1bec94ad"
DB_TASKS   = "3a3e7ba2-798c-8074-a4d0-ee0b345401ea"

PHOTOS_DIR     = Path.home() / "ai-agents/object_photos"
REMINDERS_FILE = Path(__file__).parent / "reminders.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

history = {}
last_briefing_day    = None
last_evening_day     = None
last_weekly_monday   = None


# ─── Notion helpers ────────────────────────────────────────────────────────────

def n_get(path, **params):
    return requests.get(f"{N_API}/{path}", headers=N_HDR, params=params, timeout=15).json()

def n_post(path, body):
    return requests.post(f"{N_API}/{path}", headers=N_HDR, json=body, timeout=15).json()

def n_patch(path, body):
    return requests.patch(f"{N_API}/{path}", headers=N_HDR, json=body, timeout=15).json()

def title_of(props, key=None):
    order = [key] if key else []
    order += ["Name", "Объект", "Название", "Имя клиента"]
    for k in order:
        if not k: continue
        p = props.get(k, {})
        if isinstance(p, dict) and p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", []))
    return ""

def rich_text_val(props, key):
    return "".join(t.get("plain_text", "") for t in props.get(key, {}).get("rich_text", []))

def status_val(props, key="Статус"):
    return (props.get(key, {}).get("status") or {}).get("name", "")

def date_val(props, key):
    return ((props.get(key, {}).get("date")) or {}).get("start", "")

def find_object_id(name: str):
    r = n_post(f"databases/{DB_OBJECTS}/query", {})
    for p in r.get("results", []):
        if name.lower() in title_of(p["properties"], "Объект").lower():
            return p["id"], title_of(p["properties"], "Объект")
    return None, None

def find_work_id(object_id: str, work_name: str):
    r = n_post(f"databases/{DB_WORKS}/query", {
        "filter": {"property": "Объект", "relation": {"contains": object_id}}
    })
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if work_name.lower() in nm.lower():
            return p["id"], nm
    return None, None


# ─── Notion tools ──────────────────────────────────────────────────────────────

def get_active_objects() -> str:
    r = n_post(f"databases/{DB_OBJECTS}/query", {
        "filter": {"property": "Статус", "status": {"does_not_equal": "Сдан"}}
    })
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr, "Объект")
        status = status_val(pr)
        addr   = rich_text_val(pr, "Адрес")
        rows.append(f"- {name} | {status}" + (f" | {addr}" if addr else ""))
    return "\n".join(rows) if rows else "Нет активных объектов"


def get_works(object_name: str) -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id:
        return f"Объект «{object_name}» не найден"
    r = n_post(f"databases/{DB_WORKS}/query", {
        "filter": {"property": "Объект", "relation": {"contains": obj_id}},
        "sorts": [{"property": "Дата ", "direction": "ascending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr)
        status = status_val(pr)
        dt     = date_val(pr, "Дата ")
        rows.append(f"- {dt or '?'} | {status} | {name}")
    return f"График «{obj_title}»:\n" + ("\n".join(rows) if rows else "Работы не найдены")


def get_upcoming_deadlines(days: int = 7) -> str:
    today = date.today().isoformat()
    end   = (date.today() + timedelta(days=days)).isoformat()
    r = n_post(f"databases/{DB_WORKS}/query", {
        "filter": {"and": [
            {"property": "Дата ", "date": {"on_or_after": today}},
            {"property": "Дата ", "date": {"on_or_before": end}},
            {"property": "Статус", "status": {"does_not_equal": "Готово"}}
        ]},
        "sorts": [{"property": "Дата ", "direction": "ascending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr)
        dt     = date_val(pr, "Дата ")
        status = status_val(pr)
        rel    = pr.get("Объект", {}).get("relation", [])
        obj_nm = ""
        if rel:
            op = n_get(f"pages/{rel[0]['id']}")
            obj_nm = title_of(op.get("properties", {}), "Объект")
        rows.append(f"- {dt} | {obj_nm} | {name} ({status})")
    return "\n".join(rows) if rows else "Нет дедлайнов"


def get_overdue_works() -> str:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = n_post(f"databases/{DB_WORKS}/query", {
        "filter": {"and": [
            {"property": "Дата ", "date": {"before": date.today().isoformat()}},
            {"property": "Статус", "status": {"does_not_equal": "Готово"}},
            {"property": "Статус", "status": {"does_not_equal": "Пауза"}}
        ]},
        "sorts": [{"property": "Дата ", "direction": "ascending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr)
        dt     = date_val(pr, "Дата ")
        status = status_val(pr)
        rel    = pr.get("Объект", {}).get("relation", [])
        obj_nm = ""
        if rel:
            op = n_get(f"pages/{rel[0]['id']}")
            obj_nm = title_of(op.get("properties", {}), "Объект")
        rows.append(f"- {dt} | {obj_nm} | {name} ({status})")
    return "\n".join(rows) if rows else ""


def update_work_status(work_name: str, object_name: str, new_status: str) -> str:
    valid = ["В планах", "Делаю", "Готово", "Пауза"]
    if new_status not in valid:
        return f"Неверный статус. Допустимые: {', '.join(valid)}"
    obj_id, _ = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    work_id, work_title = find_work_id(obj_id, work_name)
    if not work_id: return f"Работа «{work_name}» не найдена"
    n_patch(f"pages/{work_id}", {"properties": {"Статус": {"status": {"name": new_status}}}})
    return f"✅ «{work_title}» → {new_status}"


def update_work_date(work_name: str, object_name: str, new_date: str) -> str:
    obj_id, _ = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    work_id, work_title = find_work_id(obj_id, work_name)
    if not work_id: return f"Работа «{work_name}» не найдена"
    n_patch(f"pages/{work_id}", {"properties": {"Дата ": {"date": {"start": new_date}}}})
    return f"✅ «{work_title}» перенесена на {new_date}"


def add_work_comment(work_name: str, object_name: str, comment: str) -> str:
    obj_id, _ = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    work_id, work_title = find_work_id(obj_id, work_name)
    if not work_id: return f"Работа «{work_name}» не найдена"
    # append comment as block
    r = n_patch(f"blocks/{work_id}/children", {})  # just add paragraph block
    requests.patch(f"{N_API}/blocks/{work_id}/children", headers=N_HDR, json={
        "children": [{"type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": f"📝 {date.today()} — {comment}"}}]
        }}]
    }, timeout=15)
    return f"✅ Комментарий добавлен к «{work_title}»"


def create_work(object_name: str, work_name: str, planned_date: str, status: str = "В планах") -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    r = n_post("pages", {
        "parent": {"database_id": DB_WORKS},
        "properties": {
            "Name":    {"title": [{"type": "text", "text": {"content": work_name}}]},
            "Объект":  {"relation": [{"id": obj_id}]},
            "Статус":  {"status": {"name": status}},
            "Дата ":   {"date": {"start": planned_date}}
        }
    })
    return f"✅ Создана работа «{work_name}» на {planned_date} для «{obj_title}»" if "id" in r else f"Ошибка: {r.get('message')}"


def delete_work(work_name: str, object_name: str) -> str:
    obj_id, _ = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    work_id, work_title = find_work_id(obj_id, work_name)
    if not work_id: return f"Работа «{work_name}» не найдена"
    r = n_patch(f"pages/{work_id}", {"archived": True})
    return f"✅ «{work_title}» удалена" if r.get("archived") else f"Ошибка: {r.get('message')}"


def add_expense(object_name: str, amount: float, description: str, expense_type: str = "Материалы") -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    r = n_post("pages", {
        "parent": {"database_id": DB_EXPENSES},
        "properties": {
            "Name":        {"title": [{"type": "text", "text": {"content": description}}]},
            "Объект":      {"relation": [{"id": obj_id}]},
            "Сумма факт":  {"number": amount},
            "Дата":        {"date": {"start": date.today().isoformat()}},
            "Тип":         {"select": {"name": expense_type}},
            "Оплачен":     {"checkbox": True},
            "Примечание":  {"rich_text": [{"type": "text", "text": {"content": description}}]}
        }
    })
    return f"✅ Расход {amount:,.0f} ₽ — «{description}» добавлен к «{obj_title}»" if "id" in r else f"Ошибка: {r.get('message')}"


def get_expenses(object_name: str) -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    r = n_post(f"databases/{DB_EXPENSES}/query", {
        "filter": {"property": "Объект", "relation": {"contains": obj_id}},
        "sorts": [{"property": "Дата", "direction": "descending"}]
    })
    rows = []
    total = 0
    for p in r.get("results", []):
        pr    = p["properties"]
        name  = title_of(pr)
        amt   = pr.get("Сумма факт", {}).get("number") or 0
        dt    = date_val(pr, "Дата")
        total += amt
        rows.append(f"- {dt} | {amt:,.0f} ₽ | {name}")
    if not rows: return f"Расходы по «{obj_title}» не найдены"
    return f"Расходы «{obj_title}»:\n" + "\n".join(rows) + f"\n\nИтого: {total:,.0f} ₽"


def get_deals_list() -> str:
    r = n_post(f"databases/{DB_DEALS}/query", {
        "filter": {"property": "Статус", "status": {"does_not_equal": "Отказ"}},
        "sorts": [{"property": "Дата договора", "direction": "descending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr, "Название")
        status = status_val(pr)
        amt    = pr.get("Сумма договора (₽)", {}).get("number") or 0
        dt     = date_val(pr, "Дата договора")
        rows.append(f"- {name} | {status} | {amt:,.0f} ₽" + (f" | {dt}" if dt else ""))
    return "\n".join(rows) if rows else "Нет активных сделок"


def create_deal(deal_name: str, client_name: str, deal_type: str, amount: float = 0, deal_date: str = "") -> str:
    # find client
    rc = n_post(f"databases/{DB_CLIENTS}/query", {})
    client_id = None
    for p in rc.get("results", []):
        nm = title_of(p["properties"])
        if client_name.lower() in nm.lower():
            client_id = p["id"]; break

    props = {
        "Название":           {"title": [{"type": "text", "text": {"content": deal_name}}]},
        "Статус":             {"status": {"name": "Переговоры"}},
        "Тип работ":          {"select": {"name": deal_type}},
    }
    if amount:
        props["Сумма договора (₽)"] = {"number": amount}
    if deal_date:
        props["Дата договора"] = {"date": {"start": deal_date}}
    if client_id:
        props["Заказчик"] = {"relation": [{"id": client_id}]}

    r = n_post("pages", {"parent": {"database_id": DB_DEALS}, "properties": props})
    return f"✅ Сделка «{deal_name}» создана" if "id" in r else f"Ошибка: {r.get('message')}"


def update_deal_status(deal_name: str, new_status: str) -> str:
    valid = ["Переговоры", "КП отправлено", "Договор", "В работе", "Сдан", "Отказ"]
    if new_status not in valid:
        return f"Неверный статус. Допустимые: {', '.join(valid)}"
    r = n_post(f"databases/{DB_DEALS}/query", {})
    deal_id = None
    deal_title = ""
    for p in r.get("results", []):
        nm = title_of(p["properties"], "Название")
        if deal_name.lower() in nm.lower():
            deal_id = p["id"]; deal_title = nm; break
    if not deal_id: return f"Сделка «{deal_name}» не найдена"
    n_patch(f"pages/{deal_id}", {"properties": {"Статус": {"status": {"name": new_status}}}})
    return f"✅ Сделка «{deal_title}» → {new_status}"


def create_lead(client_name: str, phone: str, source: str, object_type: str, area: float = None, budget: float = None, comment: str = "") -> str:
    props = {
        "Имя клиента":    {"title": [{"type": "text", "text": {"content": client_name}}]},
        "Статус":         {"status": {"name": "Новый"}},
        "Источник":       {"select": {"name": source}},
        "Тип объекта":    {"select": {"name": object_type}},
        "Дата обращения": {"date": {"start": date.today().isoformat()}}
    }
    if phone:   props["Телефон"]      = {"phone_number": phone}
    if area:    props["Площадь (м²)"] = {"number": area}
    if budget:  props["Бюджет (₽)"]   = {"number": budget}
    if comment: props["Комментарий"]  = {"rich_text": [{"type": "text", "text": {"content": comment}}]}
    r = n_post("pages", {"parent": {"database_id": DB_LEADS}, "properties": props})
    return f"✅ Лид «{client_name}» добавлен" if "id" in r else f"Ошибка: {r.get('message')}"


def get_leads_list() -> str:
    r = n_post(f"databases/{DB_LEADS}/query", {
        "filter": {"property": "Статус", "status": {"does_not_equal": "Отказ"}},
        "sorts": [{"property": "Дата обращения", "direction": "descending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr, "Имя клиента")
        status = status_val(pr)
        src    = (pr.get("Источник", {}).get("select") or {}).get("name", "")
        otype  = (pr.get("Тип объекта", {}).get("select") or {}).get("name", "")
        dt     = date_val(pr, "Дата обращения")
        rows.append(f"- {dt} | {name} | {otype} | {src} | {status}")
    return "\n".join(rows) if rows else "Нет активных лидов"


def search_notion(query: str) -> str:
    r = requests.post(f"{N_API}/search", headers=N_HDR, json={
        "query": query, "filter": {"value": "page", "property": "object"}
    }, timeout=15).json()
    rows = []
    for p in r.get("results", []):
        props = p.get("properties", {})
        name  = title_of(props)
        url   = p.get("url", "")
        if name:
            rows.append(f"- {name}")
    return "\n".join(rows[:10]) if rows else f"Ничего не найдено по запросу «{query}»"


# ─── Reminders ─────────────────────────────────────────────────────────────────

def load_reminders():
    if REMINDERS_FILE.exists():
        return json.loads(REMINDERS_FILE.read_text())
    return []

def save_reminders(data):
    REMINDERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def set_reminder(text: str, remind_at: str) -> str:
    """remind_at: YYYY-MM-DD HH:MM или YYYY-MM-DD"""
    reminders = load_reminders()
    reminders.append({"text": text, "at": remind_at, "sent": False})
    save_reminders(reminders)
    return f"✅ Напоминание установлено на {remind_at}: «{text}»"

def check_reminders():
    reminders = load_reminders()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = date.today().isoformat()
    changed = False
    for r in reminders:
        if r.get("sent"): continue
        at = r["at"]
        # trigger if at <= now (supports both date and datetime)
        trigger = at if " " in at else at + " 09:00"
        if trigger <= now:
            tg_send(OWNER_ID, f"🔔 Напоминание: {r['text']}")
            r["sent"] = True
            changed = True
    if changed:
        # keep only unsent + last 20 sent
        unsent = [r for r in reminders if not r.get("sent")]
        sent   = [r for r in reminders if r.get("sent")][-20:]
        save_reminders(unsent + sent)


# ─── Планы и задачи ────────────────────────────────────────────────────────────

def get_tasks(status_filter: str = "") -> str:
    body = {"sorts": [{"property": "Date", "direction": "ascending"}]}
    if status_filter:
        body["filter"] = {"property": "Status", "status": {"equals": status_filter}}
    r = n_post(f"databases/{DB_TASKS}/query", body)
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr)
        status = (pr.get("Status", {}).get("status") or {}).get("name", "")
        dt     = date_val(pr, "Date")
        label  = (pr.get("Метки", {}).get("select") or {}).get("name", "")
        row    = f"- {dt or '?'} | {name}"
        if status: row += f" | {status}"
        if label:  row += f" | {label}"
        rows.append(row)
    return "\n".join(rows) if rows else "Задач нет"


def create_task(name: str, task_date: str, label: str = "", status: str = "Нужно сделать") -> str:
    # поддержка времени: "2026-07-21 14:00" → "2026-07-21T14:00:00+03:00"
    if " " in task_date:
        dt_str = task_date.replace(" ", "T") + ":00+03:00"
    else:
        dt_str = task_date
    props = {
        "Name":   {"title": [{"type": "text", "text": {"content": name}}]},
        "Status": {"status": {"name": status}},
        "Date":   {"date": {"start": dt_str}}
    }
    if label:
        props["Метки"] = {"select": {"name": label}}
    r = n_post("pages", {"parent": {"database_id": DB_TASKS}, "properties": props})
    return f"✅ Задача «{name}» создана на {task_date}" if "id" in r else f"Ошибка: {r.get('message')}"


def find_task_id(task_name: str):
    r = n_post(f"databases/{DB_TASKS}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if task_name.lower() in nm.lower():
            return p["id"], nm
    return None, None


def update_task_status(task_name: str, new_status: str) -> str:
    task_id, task_title = find_task_id(task_name)
    if not task_id: return f"Задача «{task_name}» не найдена"
    n_patch(f"pages/{task_id}", {"properties": {"Status": {"status": {"name": new_status}}}})
    return f"✅ «{task_title}» → {new_status}"


def update_task_date(task_name: str, new_date: str) -> str:
    """new_date: YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS+03:00"""
    task_id, task_title = find_task_id(task_name)
    if not task_id: return f"Задача «{task_name}» не найдена"
    # если передано время — форматируем как datetime
    if " " in new_date:
        dt_str = new_date.replace(" ", "T") + ":00+03:00"
    elif "T" in new_date:
        dt_str = new_date
    else:
        dt_str = new_date
    n_patch(f"pages/{task_id}", {"properties": {"Date": {"date": {"start": dt_str}}}})
    return f"✅ Дата «{task_title}» → {new_date}"


def delete_task(task_name: str) -> str:
    task_id, task_title = find_task_id(task_name)
    if not task_id: return f"Задача «{task_name}» не найдена"
    r = n_patch(f"pages/{task_id}", {"archived": True})
    return f"✅ «{task_title}» удалена" if r.get("archived") else f"Ошибка: {r.get('message')}"


def update_task_name(old_name: str, new_name: str) -> str:
    task_id, task_title = find_task_id(old_name)
    if not task_id: return f"Задача «{old_name}» не найдена"
    n_patch(f"pages/{task_id}", {"properties": {"Name": {"title": [{"type": "text", "text": {"content": new_name}}]}}})
    return f"✅ «{task_title}» переименована в «{new_name}»"


# ─── Claude tools ──────────────────────────────────────────────────────────────

TOOLS = [
    {"name": "get_active_objects",     "description": "Список активных объектов HOM Group",                        "input_schema": {"type":"object","properties":{},"required":[]}},
    {"name": "get_works",              "description": "График работ по объекту",                                    "input_schema": {"type":"object","properties":{"object_name":{"type":"string"}},"required":["object_name"]}},
    {"name": "get_upcoming_deadlines", "description": "Ближайшие дедлайны по всем объектам",                       "input_schema": {"type":"object","properties":{"days":{"type":"integer"}},"required":[]}},
    {"name": "get_overdue_works",      "description": "Просроченные работы (дата прошла, не готово)",               "input_schema": {"type":"object","properties":{},"required":[]}},
    {"name": "update_work_status",     "description": "Изменить статус работы: В планах / Делаю / Готово / Пауза", "input_schema": {"type":"object","properties":{"work_name":{"type":"string"},"object_name":{"type":"string"},"new_status":{"type":"string"}},"required":["work_name","object_name","new_status"]}},
    {"name": "update_work_date",       "description": "Перенести дату работы (YYYY-MM-DD)",                        "input_schema": {"type":"object","properties":{"work_name":{"type":"string"},"object_name":{"type":"string"},"new_date":{"type":"string"}},"required":["work_name","object_name","new_date"]}},
    {"name": "add_work_comment",       "description": "Добавить комментарий к работе на объекте",                  "input_schema": {"type":"object","properties":{"work_name":{"type":"string"},"object_name":{"type":"string"},"comment":{"type":"string"}},"required":["work_name","object_name","comment"]}},
    {"name": "create_work",            "description": "Создать новую работу на объекте",                           "input_schema": {"type":"object","properties":{"object_name":{"type":"string"},"work_name":{"type":"string"},"planned_date":{"type":"string"},"status":{"type":"string"}},"required":["object_name","work_name","planned_date"]}},
    {"name": "delete_work",            "description": "Удалить работу с объекта",                                  "input_schema": {"type":"object","properties":{"work_name":{"type":"string"},"object_name":{"type":"string"}},"required":["work_name","object_name"]}},
    {"name": "add_expense",            "description": "Добавить расход по объекту",                                "input_schema": {"type":"object","properties":{"object_name":{"type":"string"},"amount":{"type":"number"},"description":{"type":"string"},"expense_type":{"type":"string","description":"Материалы / Работа / Аренда / Прочее"}},"required":["object_name","amount","description"]}},
    {"name": "get_expenses",           "description": "Получить расходы по объекту",                               "input_schema": {"type":"object","properties":{"object_name":{"type":"string"}},"required":["object_name"]}},
    {"name": "get_deals_list",         "description": "Список активных сделок",                                    "input_schema": {"type":"object","properties":{},"required":[]}},
    {"name": "create_deal",            "description": "Создать новую сделку",                                      "input_schema": {"type":"object","properties":{"deal_name":{"type":"string"},"client_name":{"type":"string"},"deal_type":{"type":"string","description":"Под ключ / Частичный ремонт / Строительство / Коммерция"},"amount":{"type":"number"},"deal_date":{"type":"string"}},"required":["deal_name","client_name","deal_type"]}},
    {"name": "update_deal_status",     "description": "Обновить статус сделки",                                    "input_schema": {"type":"object","properties":{"deal_name":{"type":"string"},"new_status":{"type":"string"}},"required":["deal_name","new_status"]}},
    {"name": "create_lead",            "description": "Добавить нового лида",                                      "input_schema": {"type":"object","properties":{"client_name":{"type":"string"},"phone":{"type":"string"},"source":{"type":"string"},"object_type":{"type":"string"},"area":{"type":"number"},"budget":{"type":"number"},"comment":{"type":"string"}},"required":["client_name","phone","source","object_type"]}},
    {"name": "get_leads_list",         "description": "Список активных лидов",                                     "input_schema": {"type":"object","properties":{},"required":[]}},
    {"name": "search_notion",          "description": "Поиск по всем базам Notion",                                "input_schema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name": "set_reminder",            "description": "Установить напоминание. Формат даты: YYYY-MM-DD или YYYY-MM-DD HH:MM", "input_schema": {"type":"object","properties":{"text":{"type":"string"},"remind_at":{"type":"string"}},"required":["text","remind_at"]}},
    {"name": "get_tasks",              "description": "Получить задачи из доски Планы и задачи",                            "input_schema": {"type":"object","properties":{"status_filter":{"type":"string","description":"Фильтр по статусу, например: Нужно сделать / В процессе / Готово"}},"required":[]}},
    {"name": "create_task",            "description": "Создать новую задачу в доске Планы и задачи",                       "input_schema": {"type":"object","properties":{"name":{"type":"string"},"task_date":{"type":"string","description":"YYYY-MM-DD"},"label":{"type":"string"},"status":{"type":"string"}},"required":["name","task_date"]}},
    {"name": "update_task_status",     "description": "Обновить статус задачи в доске Планы и задачи",                    "input_schema": {"type":"object","properties":{"task_name":{"type":"string"},"new_status":{"type":"string"}},"required":["task_name","new_status"]}},
    {"name": "update_task_date",       "description": "Изменить дату/время задачи. Формат: YYYY-MM-DD или YYYY-MM-DD HH:MM","input_schema": {"type":"object","properties":{"task_name":{"type":"string"},"new_date":{"type":"string"}},"required":["task_name","new_date"]}},
    {"name": "delete_task",            "description": "Удалить задачу из доски Планы и задачи",                            "input_schema": {"type":"object","properties":{"task_name":{"type":"string"}},"required":["task_name"]}},
    {"name": "update_task_name",       "description": "Переименовать задачу",                                               "input_schema": {"type":"object","properties":{"old_name":{"type":"string"},"new_name":{"type":"string"}},"required":["old_name","new_name"]}},
]

TOOL_FN = {
    "get_active_objects":     lambda i: get_active_objects(),
    "get_works":              lambda i: get_works(i["object_name"]),
    "get_upcoming_deadlines": lambda i: get_upcoming_deadlines(i.get("days", 7)),
    "get_overdue_works":      lambda i: get_overdue_works(),
    "update_work_status":     lambda i: update_work_status(i["work_name"], i["object_name"], i["new_status"]),
    "update_work_date":       lambda i: update_work_date(i["work_name"], i["object_name"], i["new_date"]),
    "add_work_comment":       lambda i: add_work_comment(i["work_name"], i["object_name"], i["comment"]),
    "create_work":            lambda i: create_work(i["object_name"], i["work_name"], i["planned_date"], i.get("status","В планах")),
    "delete_work":            lambda i: delete_work(i["work_name"], i["object_name"]),
    "add_expense":            lambda i: add_expense(i["object_name"], i["amount"], i["description"], i.get("expense_type","Материалы")),
    "get_expenses":           lambda i: get_expenses(i["object_name"]),
    "get_deals_list":         lambda i: get_deals_list(),
    "create_deal":            lambda i: create_deal(i["deal_name"], i["client_name"], i["deal_type"], i.get("amount",0), i.get("deal_date","")),
    "update_deal_status":     lambda i: update_deal_status(i["deal_name"], i["new_status"]),
    "create_lead":            lambda i: create_lead(i["client_name"], i.get("phone",""), i["source"], i["object_type"], i.get("area"), i.get("budget"), i.get("comment","")),
    "get_leads_list":         lambda i: get_leads_list(),
    "search_notion":          lambda i: search_notion(i["query"]),
    "set_reminder":           lambda i: set_reminder(i["text"], i["remind_at"]),
    "get_tasks":              lambda i: get_tasks(i.get("status_filter", "")),
    "create_task":            lambda i: create_task(i["name"], i["task_date"], i.get("label",""), i.get("status","Нужно сделать")),
    "update_task_status":     lambda i: update_task_status(i["task_name"], i["new_status"]),
    "update_task_date":       lambda i: update_task_date(i["task_name"], i["new_date"]),
    "delete_task":            lambda i: delete_task(i["task_name"]),
    "update_task_name":       lambda i: update_task_name(i["old_name"], i["new_name"]),
}

SYSTEM = f"""Ты — Ассистент HOM Group, личный помощник Джабраила Кадиева (руководитель строительной компании, Грозный, Чечня).
Сегодня: {date.today().isoformat()}

Отвечай кратко и по делу. Используй инструменты чтобы читать и обновлять Notion.
Если не указана дата — уточни. Даты всегда YYYY-MM-DD. Отвечай на русском."""


# ─── Claude agent ──────────────────────────────────────────────────────────────

def ask_claude(chat_id: int, user_text: str) -> str:
    if chat_id not in history:
        history[chat_id] = []
    history[chat_id].append({"role": "user", "content": user_text})
    messages = history[chat_id][-20:]

    for _ in range(6):
        resp = claude.messages.create(
            model="claude-opus-4-7",
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages
        )
        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    fn     = TOOL_FN.get(block.name)
                    result = fn(block.input) if fn else f"Инструмент {block.name} не найден"
                    log.info(f"Tool {block.name} → {str(result)[:80]}")
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user",      "content": tool_results}
            ]
        else:
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            history[chat_id] = messages + [{"role": "assistant", "content": text}]
            return text
    return "Не удалось получить ответ"


# ─── Voice transcription ───────────────────────────────────────────────────────

def transcribe_voice(file_id: str) -> str:
    if not OPENAI_KEY:
        return "[голосовые недоступны: добавь OPENAI_API_KEY в Railway Variables]"
    try:
        import openai as oai
        oai.api_key = OPENAI_KEY
        info = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=15).json()
        file_path = info["result"]["file_path"]
        audio_data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}", timeout=30).content
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            result = oai.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
        os.unlink(tmp_path)
        return result.text
    except Exception as e:
        log.error(f"Whisper error: {e}")
        return f"[ошибка расшифровки: {e}]"


# ─── Photo handler ─────────────────────────────────────────────────────────────

def handle_photo(msg: dict):
    chat_id = msg["chat"]["id"]
    caption = msg.get("caption", "").strip()
    photo   = msg["photo"][-1]
    file_id = photo["file_id"]

    # download photo
    info      = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=15).json()
    file_path = info["result"]["file_path"]
    img_data  = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}", timeout=30).content

    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"photo_{ts}.jpg"
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    dest  = PHOTOS_DIR / fname
    dest.write_bytes(img_data)

    tg_send(chat_id, f"📷 Фото сохранено: {fname}\n\nЕсли хочешь привязать к объекту — напиши «привяжи {fname} к [объект]»")
    log.info(f"Фото сохранено: {dest}")


# ─── Scheduled reports ─────────────────────────────────────────────────────────

def maybe_morning_briefing():
    global last_briefing_day
    today = date.today()
    now   = datetime.now()
    if last_briefing_day == today or not (8 <= now.hour < 9):
        return
    last_briefing_day = today
    objects   = get_active_objects()
    deadlines = get_upcoming_deadlines(3)
    tg_send(OWNER_ID,
        f"☀️ *Доброе утро, Джабраил!*\n\n"
        f"*Активные объекты:*\n{objects}\n\n"
        f"*Ближайшие 3 дня:*\n{deadlines}",
        parse_mode="Markdown"
    )
    log.info("Утренний брифинг отправлен")


def maybe_evening_overdue():
    global last_evening_day
    today = date.today()
    now   = datetime.now()
    if last_evening_day == today or not (18 <= now.hour < 19):
        return
    last_evening_day = today
    overdue = get_overdue_works()
    if overdue:
        tg_send(OWNER_ID, f"⚠️ *Просроченные работы:*\n\n{overdue}", parse_mode="Markdown")
        log.info("Вечернее уведомление о просрочках отправлено")


def maybe_weekly_report():
    global last_weekly_monday
    today = date.today()
    now   = datetime.now()
    if today.weekday() != 0 or last_weekly_monday == today or not (8 <= now.hour < 9):
        return
    last_weekly_monday = today
    objects   = get_active_objects()
    overdue   = get_overdue_works()
    leads     = get_leads_list()
    text = (
        f"📊 *Еженедельный отчёт HOM Group*\n\n"
        f"*Объекты в работе:*\n{objects}\n\n"
        f"*Просрочки:*\n{overdue or 'Нет'}\n\n"
        f"*Лиды:*\n{leads}"
    )
    tg_send(OWNER_ID, text, parse_mode="Markdown")
    log.info("Еженедельный отчёт отправлен")


# ─── Telegram ──────────────────────────────────────────────────────────────────

def tg_send(chat_id, text, parse_mode=None):
    params = {"chat_id": chat_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    try:
        requests.post(f"{TG_API}/sendMessage", json=params, timeout=20)
    except Exception as e:
        log.error(f"tg_send error: {e}")


def get_updates(offset=0):
    r = requests.get(f"{TG_API}/getUpdates",
                     params={"offset": offset, "timeout": 30}, timeout=40)
    return r.json().get("result", [])


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("HOM Assistant запущен")
    offset = 0

    while True:
        try:
            maybe_morning_briefing()
            maybe_evening_overdue()
            maybe_weekly_report()
            check_reminders()
        except Exception as e:
            log.error(f"scheduled task error: {e}")

        try:
            updates = get_updates(offset)
        except Exception as e:
            log.error(f"getUpdates: {e}")
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]

            if user_id != OWNER_ID:
                tg_send(chat_id, "❌ Нет доступа")
                continue

            # Photo
            if "photo" in msg:
                try:
                    handle_photo(msg)
                except Exception as e:
                    log.error(f"photo error: {e}")
                    tg_send(chat_id, f"❌ Ошибка фото: {e}")
                continue

            # Voice
            if "voice" in msg:
                tg_send(chat_id, "🎙 Расшифровываю...")
                text = transcribe_voice(msg["voice"]["file_id"])
                log.info(f"Voice → {text[:80]}")
                if text.startswith("["):
                    tg_send(chat_id, text)
                    continue
                tg_send(chat_id, f"📝 _{text}_", parse_mode="Markdown")
            else:
                text = msg.get("text", "").strip()

            if not text:
                continue

            log.info(f"← {text[:80]}")

            try:
                reply = ask_claude(chat_id, text)
                tg_send(chat_id, reply)
            except Exception as e:
                log.error(f"Claude error: {e}")
                tg_send(chat_id, f"❌ Ошибка: {e}")


if __name__ == "__main__":
    main()
