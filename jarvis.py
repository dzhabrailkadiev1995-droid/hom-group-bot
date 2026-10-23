#!/usr/bin/env python3
"""
JARVIS — личный стратегический ассистент Джабраила Кадиева
Полный доступ к Notion: планирование, нетворкинг, партнерства, объекты, задачи, лиды
"""

import os, logging, time, json, requests, tempfile, redis as redis_lib
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv
import anthropic

MSK = timezone(timedelta(hours=3))

def today_msk() -> date:
    return datetime.now(MSK).date()

def now_msk() -> datetime:
    return datetime.now(MSK)

load_dotenv(Path(__file__).parent / ".env")

TG_TOKEN   = os.environ.get("JARVIS_TOKEN", "")
OWNER_ID   = int(os.environ.get("OWNER_CHAT_ID", "1043062064"))
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_PLANNING_TOKEN = os.environ.get("NOTION_PLANNING_TOKEN", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# Notion headers — основной токен для CRM объектов
N_HDR = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# Notion headers — токен для баз планирования
P_HDR = {
    "Authorization": f"Bearer {NOTION_PLANNING_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

N_API = "https://api.notion.com/v1"

# ─── CRM базы данных ───────────────────────────────────────────────────────────
DB_OBJECTS = "27ed68b9-d7a6-4bfb-85b1-8be6a80f1b50"
DB_WORKS   = "d7b732a4-513e-45f8-b731-2431baa6fecf"
DB_CLIENTS = "705c1d7c-b038-4518-8339-68d09dcdab3f"
DB_LEADS   = "3a2e7ba2-798c-81f7-a1d3-f8f867bfeb6c"
DB_DEALS   = "3a2e7ba2-798c-8193-9898-ed2110b2818c"
DB_EXPENSES= "30e81b7f-f4b8-4501-8722-927f1bec94ad"
DB_HISTORY = "3a3e7ba2-798c-8113-80de-e789f238b9b3"

# ─── Базы планирования ────────────────────────────────────────────────────────
DB_TASKS        = "3a3e7ba2-798c-8074-a4d0-ee0b345401ea"
DB_NETWORKING   = "3e3e7ba2-798c-809a-9f82-cf96551efe20"
DB_PARTNERSHIPS = "3e3e7ba2-798c-80ec-88ca-fe2c795a6e6c"
DB_DIARY        = "3e3e7ba2-798c-805e-9f48-dc7902d5bf67"
DB_DIRECTIONS   = "3e3e7ba2-798c-805f-8ad6-f9187ef46de8"
DB_IDEAS        = "3e3e7ba2-798c-80f7-a871-fd12fd9d6699"

REMINDERS_FILE = Path(__file__).parent / "jarvis_reminders.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "jarvis.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# Redis для персистентной истории диалога
_redis_url = os.environ.get("REDIS_URL", "")
_redis = redis_lib.from_url(_redis_url) if _redis_url else None
HISTORY_KEY = "jarvis:history:{chat_id}"
MAX_HISTORY  = 40  # последние 40 сообщений (~20 обменов)

def load_history(chat_id: int) -> list:
    if _redis:
        try:
            data = _redis.get(HISTORY_KEY.format(chat_id=chat_id))
            return json.loads(data) if data else []
        except Exception:
            pass
    return []

def save_history(chat_id: int, messages: list):
    if _redis:
        try:
            _redis.set(HISTORY_KEY.format(chat_id=chat_id), json.dumps(messages, ensure_ascii=False))
        except Exception:
            pass


# ─── Notion helpers ────────────────────────────────────────────────────────────

def n_get(path, hdrs=None, **params):
    h = hdrs or N_HDR
    return requests.get(f"{N_API}/{path}", headers=h, params=params, timeout=15).json()

def n_post(path, body, hdrs=None):
    h = hdrs or N_HDR
    return requests.post(f"{N_API}/{path}", headers=h, json=body, timeout=15).json()

def n_patch(path, body, hdrs=None):
    h = hdrs or N_HDR
    return requests.patch(f"{N_API}/{path}", headers=h, json=body, timeout=15).json()

def p_post(path, body):
    return n_post(path, body, hdrs=P_HDR)

def p_patch(path, body):
    return n_patch(path, body, hdrs=P_HDR)

def title_of(props, key=None):
    order = [key] if key else []
    order += ["Name", "Объект", "Название", "Имя клиента", "Направление"]
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

def select_val(props, key):
    return (props.get(key, {}).get("select") or {}).get("name", "")

def date_val(props, key):
    return ((props.get(key, {}).get("date")) or {}).get("start", "")

def num_val(props, key):
    return props.get(key, {}).get("number")

def mselect_val(props, key):
    return [x.get("name", "") for x in props.get(key, {}).get("multi_select", [])]


# ─── CRM инструменты (объекты, работы, лиды, сделки) ─────────────────────────

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

def get_active_objects() -> str:
    r = n_post(f"databases/{DB_OBJECTS}/query", {
        "filter": {"property": "Статус", "status": {"does_not_equal": "Сдан"}}
    })
    rows = []
    for p in r.get("results", []):
        pr = p["properties"]
        rows.append(f"- {title_of(pr, 'Объект')} | {status_val(pr)}")
    return "\n".join(rows) if rows else "Нет активных объектов"

def get_works(object_name: str) -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    r = n_post(f"databases/{DB_WORKS}/query", {
        "filter": {"property": "Объект", "relation": {"contains": obj_id}},
        "sorts": [{"property": "Дата ", "direction": "ascending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr = p["properties"]
        rows.append(f"- {date_val(pr, 'Дата ') or '?'} | {status_val(pr)} | {title_of(pr)}")
    return f"График «{obj_title}»:\n" + ("\n".join(rows) if rows else "Работы не найдены")

def get_upcoming_deadlines(days: int = 7) -> str:
    today = today_msk().isoformat()
    end   = (today_msk() + timedelta(days=days)).isoformat()
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
        pr = p["properties"]
        rel = pr.get("Объект", {}).get("relation", [])
        obj_nm = ""
        if rel:
            op = n_get(f"pages/{rel[0]['id']}")
            obj_nm = title_of(op.get("properties", {}), "Объект")
        rows.append(f"- {date_val(pr, 'Дата ')} | {obj_nm} | {title_of(pr)} ({status_val(pr)})")
    return "\n".join(rows) if rows else "Нет дедлайнов"

def get_overdue_works() -> str:
    r = n_post(f"databases/{DB_WORKS}/query", {
        "filter": {"and": [
            {"property": "Дата ", "date": {"before": today_msk().isoformat()}},
            {"property": "Статус", "status": {"does_not_equal": "Готово"}},
            {"property": "Статус", "status": {"does_not_equal": "Пауза"}}
        ]},
        "sorts": [{"property": "Дата ", "direction": "ascending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr = p["properties"]
        rel = pr.get("Объект", {}).get("relation", [])
        obj_nm = ""
        if rel:
            op = n_get(f"pages/{rel[0]['id']}")
            obj_nm = title_of(op.get("properties", {}), "Объект")
        rows.append(f"- {date_val(pr, 'Дата ')} | {obj_nm} | {title_of(pr)}")
    return "\n".join(rows) if rows else ""

def update_work_status(work_name: str, object_name: str, new_status: str) -> str:
    obj_id, _ = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    work_id, work_title = find_work_id(obj_id, work_name)
    if not work_id: return f"Работа «{work_name}» не найдена"
    n_patch(f"pages/{work_id}", {"properties": {"Статус": {"status": {"name": new_status}}}})
    return f"✅ «{work_title}» → {new_status}"

def create_work(object_name: str, work_name: str, planned_date: str, status: str = "В планах") -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    r = n_post("pages", {
        "parent": {"database_id": DB_WORKS},
        "properties": {
            "Name":   {"title": [{"type": "text", "text": {"content": work_name}}]},
            "Объект": {"relation": [{"id": obj_id}]},
            "Статус": {"status": {"name": status}},
            "Дата ":  {"date": {"start": planned_date}}
        }
    })
    return f"✅ «{work_name}» на {planned_date} для «{obj_title}»" if "id" in r else f"Ошибка: {r.get('message')}"

def create_lead(client_name: str, phone: str, source: str, object_type: str,
                area: float = None, budget: float = None, comment: str = "") -> str:
    props = {
        "Имя клиента":    {"title": [{"type": "text", "text": {"content": client_name}}]},
        "Статус":         {"status": {"name": "Новый"}},
        "Источник":       {"select": {"name": source}},
        "Тип объекта":    {"select": {"name": object_type}},
        "Дата обращения": {"date": {"start": today_msk().isoformat()}}
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
        pr = p["properties"]
        rows.append(f"- {date_val(pr, 'Дата обращения')} | {title_of(pr, 'Имя клиента')} | {select_val(pr, 'Тип объекта')} | {status_val(pr)}")
    return "\n".join(rows) if rows else "Нет активных лидов"

def add_expense(object_name: str, amount: float, description: str, expense_type: str = "Материалы") -> str:
    obj_id, obj_title = find_object_id(object_name)
    if not obj_id: return f"Объект «{object_name}» не найден"
    r = n_post("pages", {
        "parent": {"database_id": DB_EXPENSES},
        "properties": {
            "Name":       {"title": [{"type": "text", "text": {"content": description}}]},
            "Объект":     {"relation": [{"id": obj_id}]},
            "Сумма факт": {"number": amount},
            "Дата":       {"date": {"start": today_msk().isoformat()}},
            "Тип":        {"select": {"name": expense_type}},
        }
    })
    return f"✅ Расход {amount:,.0f} ₽ — «{description}» → «{obj_title}»" if "id" in r else f"Ошибка: {r.get('message')}"

def search_notion(query: str) -> str:
    r = requests.post(f"{N_API}/search", headers=N_HDR, json={
        "query": query, "filter": {"value": "page", "property": "object"}
    }, timeout=15).json()
    rows = [f"- {title_of(p.get('properties', {}))}" for p in r.get("results", []) if title_of(p.get("properties", {}))]
    return "\n".join(rows[:10]) if rows else f"Ничего не найдено по «{query}»"


# ─── Планы и задачи ────────────────────────────────────────────────────────────

def find_task_id(task_name: str):
    r = p_post(f"databases/{DB_TASKS}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if task_name.lower() in nm.lower():
            return p["id"], nm
    return None, None

def get_tasks(status_filter: str = "", only_active: bool = True) -> str:
    filters = []
    if only_active:
        filters.append({"property": "Status", "status": {"does_not_equal": "Готово"}})
    if status_filter:
        filters.append({"property": "Status", "status": {"equals": status_filter}})
    body = {"sorts": [{"property": "Date", "direction": "ascending"}]}
    if len(filters) == 1: body["filter"] = filters[0]
    elif len(filters) > 1: body["filter"] = {"and": filters}
    r = p_post(f"databases/{DB_TASKS}/query", body)
    rows = []
    for p in r.get("results", []):
        pr = p["properties"]
        name   = title_of(pr)
        status = (pr.get("Status", {}).get("status") or {}).get("name", "")
        dt     = date_val(pr, "Date")
        label  = select_val(pr, "Метки")
        row = f"- [{status}] {name}"
        if label: row += f" | {label}"
        if dt:    row += f" | {dt}"
        rows.append(row)
    return "\n".join(rows) if rows else "Активных задач нет"

def create_task(name: str, task_date: str = "", label: str = "", status: str = "Нужно сделать") -> str:
    props = {
        "Name":   {"title": [{"type": "text", "text": {"content": name}}]},
        "Status": {"status": {"name": status}},
    }
    if task_date:
        dt_str = task_date.replace(" ", "T") + ":00+03:00" if " " in task_date else task_date
        props["Date"] = {"date": {"start": dt_str}}
    if label:
        props["Метки"] = {"select": {"name": label}}
    r = p_post("pages", {"parent": {"database_id": DB_TASKS}, "properties": props})
    return f"✅ Задача «{name}» создана" if "id" in r else f"Ошибка: {r.get('message')}"

def update_task_status(task_name: str, new_status: str) -> str:
    task_id, task_title = find_task_id(task_name)
    if not task_id: return f"Задача «{task_name}» не найдена"
    p_patch(f"pages/{task_id}", {"properties": {"Status": {"status": {"name": new_status}}}})
    return f"✅ «{task_title}» → {new_status}"

def update_task(task_name: str, new_name: str = "", task_date: str = "", label: str = "") -> str:
    task_id, task_title = find_task_id(task_name)
    if not task_id: return f"Задача «{task_name}» не найдена"
    props = {}
    if new_name: props["Name"] = {"title": [{"type": "text", "text": {"content": new_name}}]}
    if task_date:
        dt_str = task_date.replace(" ", "T") + ":00+03:00" if " " in task_date else task_date
        props["Date"] = {"date": {"start": dt_str}}
    if label: props["Метки"] = {"select": {"name": label}}
    if not props: return "Нечего обновлять — укажи new_name, task_date или label"
    p_patch(f"pages/{task_id}", {"properties": props})
    return f"✅ «{task_title}» обновлена"

def delete_task(task_name: str) -> str:
    task_id, task_title = find_task_id(task_name)
    if not task_id: return f"Задача «{task_name}» не найдена"
    r = p_patch(f"pages/{task_id}", {"archived": True})
    return f"✅ «{task_title}» удалена" if r.get("archived") else f"Ошибка: {r.get('message')}"


# ─── Нетворкинг ───────────────────────────────────────────────────────────────

def get_networking(circle: str = "") -> str:
    body = {"page_size": 100}
    if circle:
        body["filter"] = {"property": "Нетворкинг", "select": {"equals": circle}}
    r = p_post(f"databases/{DB_NETWORKING}/query", body)
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr)
        krug   = select_val(pr, "Нетворкинг")
        vid    = status_val(pr, "Вид знакомства")
        biznes = select_val(pr, "Бизнес")
        phone  = pr.get("Номер телефона", {}).get("phone_number", "")
        gallup = ", ".join(o["name"] for o in pr.get("Gallup top-1", {}).get("multi_select", []))
        row    = f"- [{krug or '—'}] {name} | {vid}"
        if biznes: row += f" | {biznes}"
        if gallup: row += f" | Gallup: {gallup}"
        if phone:  row += f" | {phone}"
        rows.append(row)
    return "\n".join(rows) if rows else "Нетворкинг пуст"

def update_networking_gallup(person_name: str, strengths: list) -> str:
    r = p_post(f"databases/{DB_NETWORKING}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if person_name.lower() in nm.lower():
            props = {"Gallup top-1": {"multi_select": [{"name": s} for s in strengths]}}
            upd = requests.patch(f"{N_API}/pages/{p['id']}", headers=P_HDR, json={"properties": props})
            if upd.status_code == 200:
                return f"✅ Сильные стороны для {nm} записаны: {', '.join(strengths)}"
            return f"Ошибка: {upd.json().get('message')}"
    return f"Человек '{person_name}' не найден в нетворкинге"

def add_to_networking(name: str, circle: str, status: str, business: str = "", phone: str = "") -> str:
    valid_circles = ["1 круг", "2 круг", "3 круг"]
    valid_statuses = ["Должен познакомиться", "Клиент", "Соц сети", "Друг", "Знакомы", "Сотрудник", "Партнер"]
    if circle not in valid_circles:
        return f"Круг должен быть: {', '.join(valid_circles)}"
    if status not in valid_statuses:
        return f"Статус должен быть: {', '.join(valid_statuses)}"
    props = {
        "Name":              {"title": [{"type": "text", "text": {"content": name}}]},
        "Нетворкинг":        {"select": {"name": circle}},
        "Вид знакомства":    {"status": {"name": status}},
    }
    if business: props["Бизнес"]        = {"select": {"name": business}}
    if phone:    props["Номер телефона"] = {"phone_number": phone}
    r = p_post("pages", {"parent": {"database_id": DB_NETWORKING}, "properties": props})
    return f"✅ {name} добавлен в нетворкинг ({circle}, {status})" if "id" in r else f"Ошибка: {r.get('message')}"

def update_networking_status(person_name: str, new_status: str) -> str:
    r = p_post(f"databases/{DB_NETWORKING}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if person_name.lower() in nm.lower():
            p_patch(f"pages/{p['id']}", {"properties": {"Вид знакомства": {"status": {"name": new_status}}}})
            return f"✅ {nm} → {new_status}"
    return f"Человек «{person_name}» не найден в нетворкинге"

def update_networking(person_name: str, circle: str = "", business: str = "", phone: str = "", thinking: float = None) -> str:
    r = p_post(f"databases/{DB_NETWORKING}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if person_name.lower() in nm.lower():
            props = {}
            if circle:   props["Нетворкинг"]     = {"select": {"name": circle}}
            if business: props["Бизнес"]          = {"select": {"name": business}}
            if phone:    props["Номер телефона"]  = {"phone_number": phone}
            if thinking is not None: props["Мышление"] = {"number": thinking}
            if not props: return "Нечего обновлять"
            p_patch(f"pages/{p['id']}", {"properties": props})
            return f"✅ {nm} обновлён"
    return f"Человек «{person_name}» не найден"

def delete_networking(person_name: str) -> str:
    r = p_post(f"databases/{DB_NETWORKING}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if person_name.lower() in nm.lower():
            res = p_patch(f"pages/{p['id']}", {"archived": True})
            return f"✅ {nm} удалён из нетворкинга" if res.get("archived") else f"Ошибка: {res.get('message')}"
    return f"Человек «{person_name}» не найден"


# ─── Партнерства ──────────────────────────────────────────────────────────────

def get_partnerships() -> str:
    r = p_post(f"databases/{DB_PARTNERSHIPS}/query", {})
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = title_of(pr)
        status = status_val(pr, "Статус")
        dog    = rich_text_val(pr, "Договоренности")
        types  = ", ".join(mselect_val(pr, "Multi-select"))
        dt     = date_val(pr, "Дата партнерства")
        row    = f"- [{status}] {name}"
        if dog:   row += f" | {dog}"
        if types: row += f" | {types}"
        if dt:    row += f" | с {dt}"
        rows.append(row)
    return "\n".join(rows) if rows else "Партнёрств нет"

def add_partnership(name: str, terms: str, status: str = "В планах", types: list = None) -> str:
    props = {
        "Name":            {"title": [{"type": "text", "text": {"content": name}}]},
        "Статус":          {"status": {"name": status}},
        "Договоренности":  {"rich_text": [{"type": "text", "text": {"content": terms}}]},
        "Дата партнерства":{"date": {"start": today_msk().isoformat()}},
    }
    if types:
        props["Multi-select"] = {"multi_select": [{"name": t} for t in types]}
    r = p_post("pages", {"parent": {"database_id": DB_PARTNERSHIPS}, "properties": props})
    return f"✅ Партнёрство «{name}» добавлено" if "id" in r else f"Ошибка: {r.get('message')}"

def find_partnership_id(name: str):
    r = p_post(f"databases/{DB_PARTNERSHIPS}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if name.lower() in nm.lower():
            return p["id"], nm
    return None, None

def update_partnership(partner_name: str, terms: str = "", status: str = "", types: list = None) -> str:
    pid, nm = find_partnership_id(partner_name)
    if not pid: return f"Партнёрство «{partner_name}» не найдено"
    props = {}
    if terms:  props["Договоренности"] = {"rich_text": [{"type": "text", "text": {"content": terms}}]}
    if status: props["Статус"]         = {"status": {"name": status}}
    if types is not None: props["Multi-select"] = {"multi_select": [{"name": t} for t in types]}
    if not props: return "Нечего обновлять"
    p_patch(f"pages/{pid}", {"properties": props})
    return f"✅ Партнёрство «{nm}» обновлено"

def delete_partnership(partner_name: str) -> str:
    pid, nm = find_partnership_id(partner_name)
    if not pid: return f"Партнёрство «{partner_name}» не найдено"
    res = p_patch(f"pages/{pid}", {"archived": True})
    return f"✅ «{nm}» удалено" if res.get("archived") else f"Ошибка: {res.get('message')}"


# ─── Дневник ──────────────────────────────────────────────────────────────────

def get_diary(limit: int = 10) -> str:
    r = p_post(f"databases/{DB_DIARY}/query", {
        "page_size": limit,
        "sorts": [{"property": "Дата события", "direction": "descending"}]
    })
    rows = []
    for p in r.get("results", []):
        pr   = p["properties"]
        name = title_of(pr)
        dt   = date_val(pr, "Дата события") or "?"
        eff  = num_val(pr, "Эффективность дня")
        tegi = ", ".join(mselect_val(pr, "Тег"))
        row  = f"- {dt} | {name}"
        if eff  is not None: row += f" | эфф: {eff}/10"
        if tegi: row += f" | {tegi}"
        rows.append(row)
    return "\n".join(rows) if rows else "Дневник пуст"

def add_diary_entry(title: str, event_date: str = "", effectiveness: int = None, tags: list = None) -> str:
    props = {
        "Name": {"title": [{"type": "text", "text": {"content": title}}]},
    }
    dt = event_date or today_msk().isoformat()
    props["Дата события"] = {"date": {"start": dt}}
    if effectiveness is not None:
        props["Эффективность дня"] = {"number": effectiveness}
    if tags:
        props["Тег"] = {"multi_select": [{"name": t} for t in tags]}
    r = p_post("pages", {"parent": {"database_id": DB_DIARY}, "properties": props})
    return f"✅ Запись в дневник добавлена: «{title}»" if "id" in r else f"Ошибка: {r.get('message')}"

def find_diary_id(title: str):
    r = p_post(f"databases/{DB_DIARY}/query", {
        "sorts": [{"property": "Дата события", "direction": "descending"}]
    })
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if title.lower() in nm.lower():
            return p["id"], nm
    return None, None

def update_diary_entry(entry_title: str, effectiveness: int = None, tags: list = None, event_date: str = "") -> str:
    pid, nm = find_diary_id(entry_title)
    if not pid: return f"Запись «{entry_title}» не найдена"
    props = {}
    if effectiveness is not None: props["Эффективность дня"] = {"number": effectiveness}
    if tags is not None: props["Тег"] = {"multi_select": [{"name": t} for t in tags]}
    if event_date: props["Дата события"] = {"date": {"start": event_date}}
    if not props: return "Нечего обновлять"
    p_patch(f"pages/{pid}", {"properties": props})
    return f"✅ Запись «{nm}» обновлена"

def delete_diary_entry(entry_title: str) -> str:
    pid, nm = find_diary_id(entry_title)
    if not pid: return f"Запись «{entry_title}» не найдена"
    res = p_patch(f"pages/{pid}", {"archived": True})
    return f"✅ «{nm}» удалена из дневника" if res.get("archived") else f"Ошибка: {res.get('message')}"


# ─── Направления бизнеса ──────────────────────────────────────────────────────

def get_directions() -> str:
    r = p_post(f"databases/{DB_DIRECTIONS}/query", {})
    rows = []
    for p in r.get("results", []):
        pr     = p["properties"]
        name   = (pr.get("Направление", {}).get("title", [{}]) or [{}])[0].get("plain_text", "—")
        status = status_val(pr, "Статус")
        share  = num_val(pr, "Процент компании")
        launch = date_val(pr, "Дата запуска")
        types  = ", ".join(mselect_val(pr, "Вид работ"))
        row    = f"- [{status}] {name}"
        if share:  row += f" | доля: {share*100:.0f}%"
        if launch: row += f" | с {launch}"
        if types:  row += f" | {types}"
        rows.append(row)
    return "\n".join(rows) if rows else "Направлений нет"

def add_direction(name: str, status: str = "Не запущено", share: float = None, launch_date: str = "", types: list = None) -> str:
    props = {"Направление": {"title": [{"type": "text", "text": {"content": name}}]}}
    props["Статус"] = {"status": {"name": status}}
    if share is not None: props["Процент компании"] = {"number": share}
    if launch_date: props["Дата запуска"] = {"date": {"start": launch_date}}
    if types: props["Вид работ"] = {"multi_select": [{"name": t} for t in types]}
    r = p_post("pages", {"parent": {"database_id": DB_DIRECTIONS}, "properties": props})
    return f"✅ Направление «{name}» добавлено" if "id" in r else f"Ошибка: {r.get('message')}"

def update_direction_status(direction_name: str, new_status: str) -> str:
    r = p_post(f"databases/{DB_DIRECTIONS}/query", {})
    for p in r.get("results", []):
        nm = (p["properties"].get("Направление", {}).get("title", [{}]) or [{}])[0].get("plain_text", "")
        if direction_name.lower() in nm.lower():
            p_patch(f"pages/{p['id']}", {"properties": {"Статус": {"status": {"name": new_status}}}})
            return f"✅ «{nm}» → {new_status}"
    return f"Направление «{direction_name}» не найдено"

def get_ideas() -> str:
    r = p_post(f"databases/{DB_IDEAS}/query", {"page_size": 50})
    rows = []
    for p in r.get("results", []):
        pr   = p["properties"]
        name = title_of(pr)
        real = num_val(pr, "Реализация")
        row  = f"- {name}"
        if real is not None: row += f" | реализация: {real}%"
        rows.append(row)
    return "\n".join(rows) if rows else "Идей нет"

def add_idea(name: str, realization: int = None) -> str:
    props = {"Name": {"title": [{"type": "text", "text": {"content": name}}]}}
    if realization is not None: props["Реализация"] = {"number": realization}
    r = p_post("pages", {"parent": {"database_id": DB_IDEAS}, "properties": props})
    return f"✅ Идея «{name}» добавлена" if "id" in r else f"Ошибка: {r.get('message')}"

def update_idea(idea_name: str, new_name: str = "", realization: int = None) -> str:
    r = p_post(f"databases/{DB_IDEAS}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if idea_name.lower() in nm.lower():
            props = {}
            if new_name: props["Name"] = {"title": [{"type": "text", "text": {"content": new_name}}]}
            if realization is not None: props["Реализация"] = {"number": realization}
            if not props: return "Нечего обновлять"
            p_patch(f"pages/{p['id']}", {"properties": props})
            return f"✅ Идея «{nm}» обновлена"
    return f"Идея «{idea_name}» не найдена"

def delete_idea(idea_name: str) -> str:
    r = p_post(f"databases/{DB_IDEAS}/query", {})
    for p in r.get("results", []):
        nm = title_of(p["properties"])
        if idea_name.lower() in nm.lower():
            res = p_patch(f"pages/{p['id']}", {"archived": True})
            return f"✅ Идея «{nm}» удалена" if res.get("archived") else f"Ошибка: {res.get('message')}"
    return f"Идея «{idea_name}» не найдена"


# ─── Стратегический обзор ─────────────────────────────────────────────────────

def get_planning_overview() -> str:
    today = today_msk().isoformat()

    tasks_r        = p_post(f"databases/{DB_TASKS}/query", {
        "filter": {"property": "Status", "status": {"does_not_equal": "Готово"}}
    })
    networking_r   = p_post(f"databases/{DB_NETWORKING}/query", {"page_size": 100})
    partnerships_r = p_post(f"databases/{DB_PARTNERSHIPS}/query", {})
    diary_r        = p_post(f"databases/{DB_DIARY}/query", {
        "page_size": 5,
        "sorts": [{"property": "Дата события", "direction": "descending"}]
    })
    directions_r   = p_post(f"databases/{DB_DIRECTIONS}/query", {})
    ideas_r        = p_post(f"databases/{DB_IDEAS}/query", {"page_size": 20})

    lines = [f"=== ЗАДАЧИ ({len(tasks_r.get('results',[]))} активных) ==="]
    for p in tasks_r.get("results", []):
        pr = p["properties"]
        name   = title_of(pr)
        status = (pr.get("Status", {}).get("status") or {}).get("name", "")
        label  = select_val(pr, "Метки")
        lines.append(f"[{status}] {name}" + (f" | {label}" if label else ""))

    lines.append("\n=== НЕТВОРКИНГ ===")
    for p in networking_r.get("results", []):
        pr = p["properties"]
        lines.append(f"[{select_val(pr,'Нетворкинг')}] {title_of(pr)} | {status_val(pr,'Вид знакомства')} | {select_val(pr,'Бизнес')}")

    lines.append("\n=== ПАРТНЕРСТВА ===")
    for p in partnerships_r.get("results", []):
        pr = p["properties"]
        lines.append(f"[{status_val(pr,'Статус')}] {title_of(pr)} | {rich_text_val(pr,'Договоренности')}")

    lines.append("\n=== ДНЕВНИК (последние 5) ===")
    for p in diary_r.get("results", []):
        pr = p["properties"]
        eff = num_val(pr, "Эффективность дня")
        lines.append(f"[{date_val(pr,'Дата события')}] {title_of(pr)}" + (f" | {eff}/10" if eff else ""))

    lines.append("\n=== НАПРАВЛЕНИЯ БИЗНЕСА ===")
    for p in directions_r.get("results", []):
        pr = p["properties"]
        name = (pr.get("Направление", {}).get("title", [{}]) or [{}])[0].get("plain_text", "—")
        lines.append(f"[{status_val(pr,'Статус')}] {name} | {', '.join(mselect_val(pr,'Вид работ'))}")

    lines.append("\n=== ИДЕИ ===")
    for p in ideas_r.get("results", []):
        lines.append(f"- {title_of(p['properties'])}")

    return "\n".join(lines)


# ─── Напоминания ──────────────────────────────────────────────────────────────

def load_reminders():
    if REMINDERS_FILE.exists():
        return json.loads(REMINDERS_FILE.read_text())
    return []

def save_reminders(data):
    REMINDERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def set_reminder(text: str, remind_at: str) -> str:
    reminders = load_reminders()
    reminders.append({"text": text, "at": remind_at, "sent": False})
    save_reminders(reminders)
    return f"✅ Напоминание на {remind_at}: «{text}»"

def check_reminders():
    reminders = load_reminders()
    now = now_msk().strftime("%Y-%m-%d %H:%M")
    changed = False
    for r in reminders:
        if r.get("sent"): continue
        trigger = r["at"] if " " in r["at"] else r["at"] + " 09:00"
        if trigger <= now:
            tg_send(OWNER_ID, f"🔔 {r['text']}")
            r["sent"] = True
            changed = True
    if changed:
        save_reminders([r for r in reminders if not r.get("sent")] + [r for r in reminders if r.get("sent")][-20:])


# ─── Claude tools ──────────────────────────────────────────────────────────────

TOOLS = [
    {"name": "get_planning_overview",
     "description": "Полный обзор планирования: задачи, нетворкинг, партнерства, дневник, направления, идеи. Используй при запросе обзора, анализа или приоритетов.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "get_tasks",
     "description": "Задачи из Планы и задачи.",
     "input_schema": {"type": "object", "properties": {
         "status_filter": {"type": "string", "description": "Идеи / Хочу сделать / Нужно сделать / Запланированные / Сегодня / Завтра / Текущая неделя / В процессе / Готово"},
         "only_active":   {"type": "boolean", "description": "true = только незавершённые"}
     }, "required": []}},

    {"name": "create_task",
     "description": "Создать задачу",
     "input_schema": {"type": "object", "properties": {
         "name":      {"type": "string"},
         "task_date": {"type": "string", "description": "YYYY-MM-DD"},
         "label":     {"type": "string", "description": "Друзья / Здоровье / Личный бренд / Развитие / HOM Group / Академия / Маркетинг / Уход"},
         "status":    {"type": "string", "description": "Нужно сделать / Сегодня / Запланированные / Идеи"}
     }, "required": ["name"]}},

    {"name": "update_task_status",
     "description": "Изменить статус задачи",
     "input_schema": {"type": "object", "properties": {
         "task_name":  {"type": "string"},
         "new_status": {"type": "string", "description": "Идеи / Нужно сделать / Сегодня / В процессе / Готово"}
     }, "required": ["task_name", "new_status"]}},

    {"name": "delete_task",
     "description": "Удалить задачу",
     "input_schema": {"type": "object", "properties": {"task_name": {"type": "string"}}, "required": ["task_name"]}},

    {"name": "update_task",
     "description": "Изменить название, дату или метку задачи",
     "input_schema": {"type": "object", "properties": {
         "task_name":  {"type": "string"},
         "new_name":   {"type": "string"},
         "task_date":  {"type": "string", "description": "YYYY-MM-DD"},
         "label":      {"type": "string"}
     }, "required": ["task_name"]}},

    {"name": "get_networking",
     "description": "Нетворкинг",
     "input_schema": {"type": "object", "properties": {
         "circle": {"type": "string", "description": "1 круг / 2 круг / 3 круг (пусто = все)"}
     }, "required": []}},

    {"name": "add_to_networking",
     "description": "Добавить человека в нетворкинг",
     "input_schema": {"type": "object", "properties": {
         "name":     {"type": "string"},
         "circle":   {"type": "string", "description": "1 круг / 2 круг / 3 круг"},
         "status":   {"type": "string", "description": "Должен познакомиться / Клиент / Соц сети / Друг / Знакомы / Сотрудник / Партнер"},
         "business": {"type": "string"},
         "phone":    {"type": "string"}
     }, "required": ["name", "circle", "status"]}},

    {"name": "update_networking_status",
     "description": "Обновить статус человека в нетворкинге",
     "input_schema": {"type": "object", "properties": {
         "person_name": {"type": "string"},
         "new_status":  {"type": "string"}
     }, "required": ["person_name", "new_status"]}},

    {"name": "update_networking",
     "description": "Изменить круг, бизнес, телефон или показатель мышления человека из нетворкинга",
     "input_schema": {"type": "object", "properties": {
         "person_name": {"type": "string"},
         "circle":      {"type": "string", "description": "1 круг / 2 круг / 3 круг"},
         "business":    {"type": "string"},
         "phone":       {"type": "string"},
         "thinking":    {"type": "number", "description": "Показатель мышления (число)"}
     }, "required": ["person_name"]}},

    {"name": "update_networking_gallup",
     "description": "Записать сильные стороны (Gallup top-1) для человека из нетворкинга",
     "input_schema": {"type": "object", "properties": {
         "person_name": {"type": "string"},
         "strengths":   {"type": "array", "items": {"type": "string"}}
     }, "required": ["person_name", "strengths"]}},

    {"name": "delete_networking",
     "description": "Удалить человека из нетворкинга",
     "input_schema": {"type": "object", "properties": {"person_name": {"type": "string"}}, "required": ["person_name"]}},

    {"name": "get_partnerships",
     "description": "Партнёрства",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "add_partnership",
     "description": "Добавить партнёрство",
     "input_schema": {"type": "object", "properties": {
         "name":   {"type": "string"},
         "terms":  {"type": "string"},
         "status": {"type": "string", "description": "В планах / Переговоры / Партнеры"},
         "types":  {"type": "array", "items": {"type": "string"}}
     }, "required": ["name", "terms"]}},

    {"name": "update_partnership",
     "description": "Изменить договорённости или статус партнёрства",
     "input_schema": {"type": "object", "properties": {
         "partner_name": {"type": "string"},
         "terms":        {"type": "string"},
         "status":       {"type": "string"},
         "types":        {"type": "array", "items": {"type": "string"}}
     }, "required": ["partner_name"]}},

    {"name": "delete_partnership",
     "description": "Удалить партнёрство",
     "input_schema": {"type": "object", "properties": {"partner_name": {"type": "string"}}, "required": ["partner_name"]}},

    {"name": "get_diary",
     "description": "Записи дневника",
     "input_schema": {"type": "object", "properties": {
         "limit": {"type": "integer"}
     }, "required": []}},

    {"name": "add_diary_entry",
     "description": "Добавить запись в дневник",
     "input_schema": {"type": "object", "properties": {
         "title":         {"type": "string"},
         "event_date":    {"type": "string", "description": "YYYY-MM-DD"},
         "effectiveness": {"type": "integer", "description": "1-10"},
         "tags":          {"type": "array", "items": {"type": "string"}}
     }, "required": ["title"]}},

    {"name": "update_diary_entry",
     "description": "Изменить запись в дневнике (эффективность, теги, дату)",
     "input_schema": {"type": "object", "properties": {
         "entry_title":   {"type": "string"},
         "effectiveness": {"type": "integer"},
         "tags":          {"type": "array", "items": {"type": "string"}},
         "event_date":    {"type": "string"}
     }, "required": ["entry_title"]}},

    {"name": "delete_diary_entry",
     "description": "Удалить запись из дневника",
     "input_schema": {"type": "object", "properties": {"entry_title": {"type": "string"}}, "required": ["entry_title"]}},

    {"name": "get_directions",
     "description": "Направления бизнеса",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "add_direction",
     "description": "Добавить новое бизнес-направление",
     "input_schema": {"type": "object", "properties": {
         "name":        {"type": "string"},
         "status":      {"type": "string", "description": "Не запущено / Запущено / Пауза / Закрыто"},
         "share":       {"type": "number", "description": "Доля компании, 0.0–1.0"},
         "launch_date": {"type": "string", "description": "YYYY-MM-DD"},
         "types":       {"type": "array", "items": {"type": "string"}}
     }, "required": ["name"]}},

    {"name": "update_direction_status",
     "description": "Изменить статус бизнес-направления",
     "input_schema": {"type": "object", "properties": {
         "direction_name": {"type": "string"},
         "new_status":     {"type": "string"}
     }, "required": ["direction_name", "new_status"]}},

    {"name": "get_ideas",
     "description": "Все идеи",
     "input_schema": {"type": "object", "properties": {}, "required": []}},

    {"name": "add_idea",
     "description": "Добавить идею",
     "input_schema": {"type": "object", "properties": {
         "name":        {"type": "string"},
         "realization": {"type": "integer", "description": "Процент реализации 0-100"}
     }, "required": ["name"]}},

    {"name": "update_idea",
     "description": "Обновить идею (название или процент реализации)",
     "input_schema": {"type": "object", "properties": {
         "idea_name":   {"type": "string"},
         "new_name":    {"type": "string"},
         "realization": {"type": "integer"}
     }, "required": ["idea_name"]}},

    {"name": "delete_idea",
     "description": "Удалить идею",
     "input_schema": {"type": "object", "properties": {"idea_name": {"type": "string"}}, "required": ["idea_name"]}},

    {"name": "set_reminder",
     "description": "Установить напоминание",
     "input_schema": {"type": "object", "properties": {
         "text":      {"type": "string"},
         "remind_at": {"type": "string", "description": "YYYY-MM-DD или YYYY-MM-DD HH:MM"}
     }, "required": ["text", "remind_at"]}},
]

TOOL_FN = {
    "get_planning_overview":    lambda i: get_planning_overview(),
    "get_tasks":                lambda i: get_tasks(i.get("status_filter",""), i.get("only_active", True)),
    "create_task":              lambda i: create_task(i["name"], i.get("task_date",""), i.get("label",""), i.get("status","Нужно сделать")),
    "update_task_status":       lambda i: update_task_status(i["task_name"], i["new_status"]),
    "update_task":              lambda i: update_task(i["task_name"], i.get("new_name",""), i.get("task_date",""), i.get("label","")),
    "delete_task":              lambda i: delete_task(i["task_name"]),
    "get_networking":           lambda i: get_networking(i.get("circle","")),
    "add_to_networking":        lambda i: add_to_networking(i["name"], i["circle"], i["status"], i.get("business",""), i.get("phone","")),
    "update_networking_status": lambda i: update_networking_status(i["person_name"], i["new_status"]),
    "update_networking":        lambda i: update_networking(i["person_name"], i.get("circle",""), i.get("business",""), i.get("phone",""), i.get("thinking")),
    "update_networking_gallup": lambda i: update_networking_gallup(i["person_name"], i["strengths"]),
    "delete_networking":        lambda i: delete_networking(i["person_name"]),
    "get_partnerships":         lambda i: get_partnerships(),
    "add_partnership":          lambda i: add_partnership(i["name"], i["terms"], i.get("status","В планах"), i.get("types",[])),
    "update_partnership":       lambda i: update_partnership(i["partner_name"], i.get("terms",""), i.get("status",""), i.get("types")),
    "delete_partnership":       lambda i: delete_partnership(i["partner_name"]),
    "get_diary":                lambda i: get_diary(i.get("limit", 10)),
    "add_diary_entry":          lambda i: add_diary_entry(i["title"], i.get("event_date",""), i.get("effectiveness"), i.get("tags",[])),
    "update_diary_entry":       lambda i: update_diary_entry(i["entry_title"], i.get("effectiveness"), i.get("tags"), i.get("event_date","")),
    "delete_diary_entry":       lambda i: delete_diary_entry(i["entry_title"]),
    "get_directions":           lambda i: get_directions(),
    "add_direction":            lambda i: add_direction(i["name"], i.get("status","Не запущено"), i.get("share"), i.get("launch_date",""), i.get("types",[])),
    "update_direction_status":  lambda i: update_direction_status(i["direction_name"], i["new_status"]),
    "get_ideas":                lambda i: get_ideas(),
    "add_idea":                 lambda i: add_idea(i["name"], i.get("realization")),
    "update_idea":              lambda i: update_idea(i["idea_name"], i.get("new_name",""), i.get("realization")),
    "delete_idea":              lambda i: delete_idea(i["idea_name"]),
    "set_reminder":             lambda i: set_reminder(i["text"], i["remind_at"]),
}


# ─── System prompt ────────────────────────────────────────────────────────────

def get_system():
    return f"""Ты — Джарвис, личный ассистент Джабраила Кадиева. Работаешь только с его страницей планирования в Notion.

СЕГОДНЯ: {today_msk().isoformat()}

ЧТО УМЕЕШЬ:
Задачи, нетворкинг, партнерства, дневник, направления бизнеса — читаешь и редактируешь всё.

КАК ОБЩАЕШЬСЯ:
- Коротко. Не пиши простыни — отвечай по делу, 3-5 строк максимум если не просят обзор
- Живо. Как умный коллега, не как корпоративный бот
- Диалогово. Если задача непонятна — уточни. Если сделал — скажи что сделал и спроси что дальше
- Когда что-то меняешь в Notion — делай сразу, без лишних вопросов, потом подтверди
- Если видишь важную связь или риск — скажи коротко, одной фразой

КОГДА ПРОСЯТ ОБЗОР/ПЛАНИРОВАНИЕ:
Загрузи данные через get_planning_overview и дай структурированный анализ. Но даже тут — без воды.

ФОРМАТИРОВАНИЕ (Telegram не рендерит markdown):
- Никаких *, **, _, #, ` — будут видны как мусор
- Эмодзи и тире для структуры, пустые строки между блоками"""


# ─── Claude agent ──────────────────────────────────────────────────────────────

def ask_claude(chat_id: int, user_text: str) -> str:
    messages = load_history(chat_id)

    dated_text = f"[{today_msk().strftime('%d.%m.%Y')}] {user_text}"
    messages.append({"role": "user", "content": dated_text})
    messages = messages[-MAX_HISTORY:]

    for _ in range(8):
        resp = claude.messages.create(
            model="claude-opus-4-7",
            max_tokens=3000,
            system=get_system(),
            tools=TOOLS,
            messages=messages
        )
        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    fn     = TOOL_FN.get(block.name)
                    result = fn(block.input) if fn else f"Инструмент {block.name} не найден"
                    log.info(f"Tool {block.name} → {str(result)[:100]}")
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})
            messages = messages + [
                {"role": "assistant", "content": resp.content},
                {"role": "user",      "content": tool_results}
            ]
        else:
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            save_history(chat_id, messages + [{"role": "assistant", "content": text}])
            return text
    return "Не удалось получить ответ"


# ─── Voice ────────────────────────────────────────────────────────────────────

def transcribe_voice(file_id: str) -> str:
    if not OPENAI_KEY:
        return "[голосовые недоступны: нет OPENAI_API_KEY]"
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


# ─── Telegram ──────────────────────────────────────────────────────────────────

def tg_send(chat_id, text, parse_mode=None):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    last_msg_id = None
    for chunk in chunks:
        params = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            r = requests.post(f"{TG_API}/sendMessage", json=params, timeout=20)
            last_msg_id = r.json().get("result", {}).get("message_id")
        except Exception as e:
            log.error(f"tg_send error: {e}")
    return last_msg_id

def tg_delete(chat_id, message_id):
    try:
        requests.post(f"{TG_API}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except Exception:
        pass

def get_updates(offset=0):
    try:
        r = requests.get(f"{TG_API}/getUpdates",
                         params={"offset": offset, "timeout": 30}, timeout=40)
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates error: {e}")
        return []


# ─── Main ──────────────────────────────────────────────────────────────────────

PID_FILE = Path(__file__).parent / "jarvis.pid"

def main():
    if PID_FILE.exists():
        old_pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(old_pid, 0)
            log.warning(f"Уже запущен (PID {old_pid}), убиваю старый процесс...")
            os.kill(old_pid, 9)
            time.sleep(1)
        except OSError:
            pass
    PID_FILE.write_text(str(os.getpid()))

    # Сбрасываем очередь обновлений — защита от двойной обработки при редеплое
    requests.get(f"{TG_API}/deleteWebhook", params={"drop_pending_updates": True}, timeout=10)

    log.info("JARVIS запущен")

    offset = 0
    while True:
        try:
            check_reminders()
        except Exception as e:
            log.error(f"Reminder error: {e}")

        updates = get_updates(offset)

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]

            if user_id != OWNER_ID:
                tg_send(chat_id, "Нет доступа")
                continue

            # Voice
            if "voice" in msg:
                tg_send(chat_id, "Слушаю...")
                text = transcribe_voice(msg["voice"]["file_id"])
                log.info(f"Voice → {text[:80]}")
                if text.startswith("["):
                    tg_send(chat_id, text)
                    continue
            else:
                text = msg.get("text", "").strip()

            if not text:
                continue

            log.info(f"← {text[:100]}")

            try:
                reply = ask_claude(chat_id, text)
                tg_send(chat_id, reply)
            except Exception as e:
                log.error(f"Claude error: {e}")
                tg_send(chat_id, f"Ошибка: {e}")

        if not updates:
            time.sleep(1)


if __name__ == "__main__":
    main()
