import anthropic
import requests
import json
import os
import time
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OWNER_CHAT_ID  = int(os.environ.get("OWNER_CHAT_ID", "1043062064"))
LEADS_FILE     = "/Users/dzabrailkadiev/ai-agents/leads.json"
MEMORY_DIR     = "/Users/dzabrailkadiev/ai-agents/memories"
os.makedirs(MEMORY_DIR, exist_ok=True)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

RENOVATION_VIDEO_URL = "https://www.instagram.com/reel/DYSOdZotIWa/"
RENOVATION_BUTTON = {
    "inline_keyboard": [[
        {"text": "▶️ Стоимость ремонта и что входит", "url": RENOVATION_VIDEO_URL}
    ]]
}

def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json=payload
    )

def tg_send_with_button(chat_id, text):
    tg_send(chat_id, text, reply_markup=RENOVATION_BUTTON)

def tg_typing(chat_id):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
        json={"chat_id": chat_id, "action": "typing"}
    )

def tg_get_updates(offset=0):
    r = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 10}, timeout=15
    )
    return r.json().get("result", [])

def load_memory(user_id):
    f = f"{MEMORY_DIR}/{user_id}.json"
    return json.load(open(f, encoding="utf-8")) if os.path.exists(f) else {}

def save_memory(user_id, memory):
    with open(f"{MEMORY_DIR}/{user_id}.json", "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def remember(user_id, key, value):
    m = load_memory(user_id)
    m[key] = value
    save_memory(user_id, m)
    return f"Запомнил: {key} = {value}"

def recall(user_id):
    m = load_memory(user_id)
    return m if m else "Новый клиент"

def save_lead(user_id, username, tg_username, service, city, area, timeline, budget, phone="не указан", comment=""):
    leads = []
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, encoding="utf-8") as f:
            leads = json.load(f)

    lead = {
        "id":           len(leads) + 1,
        "date":         datetime.now().strftime("%Y-%m-%d %H:%M"),
        "user_id":      user_id,
        "name":         username,
        "tg_username":  tg_username,
        "phone":        phone,
        "service":      service,
        "city":         city,
        "area":         area,
        "timeline":     timeline,
        "budget":       budget,
        "comment":      comment,
        "status":       "новая"
    }
    leads.append(lead)

    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)

    notify_owner(lead)
    return f"Заявка №{lead['id']} сохранена"

def notify_owner(lead):
    tg = f"@{lead['tg_username']}" if lead['tg_username'] and lead['tg_username'] != "None" else f"ID: {lead['user_id']}"
    phone_line = f"📞 Телефон: {lead['phone']}\n" if lead['phone'] != "не указан" else ""

    msg = (
        f"🔔 *Новая заявка №{lead['id']}*\n\n"
        f"👤 Имя: {lead['name']}\n"
        f"💬 Telegram: {tg}\n"
        f"{phone_line}"
        f"🔧 Услуга: {lead['service']}\n"
        f"📍 Город: {lead['city']}\n"
        f"📐 Объём: {lead['area']}\n"
        f"⏰ Срок: {lead['timeline']}\n"
        f"💰 Бюджет: {lead['budget']}\n"
        f"💬 Доп. инфо: {lead['comment']}\n\n"
        f"🕐 {lead['date']}"
    )
    tg_send(OWNER_CHAT_ID, msg)

def get_services():
    return {
        "услуги": [
            {
                "название": "Механизированная штукатурка",
                "цена": "от 350 руб/м² (без материала), от 700 руб/м² (с материалом)",
                "описание": "Машинное нанесение, ровные стены, быстрые сроки"
            },
            {
                "название": "Плиточные работы",
                "цена": "цена зависит от формата плитки и объёма работ — уточняется индивидуально",
                "описание": "Укладка плитки в ванных, кухнях, на полу и стенах"
            },
            {
                "название": "Отделочные работы",
                "цена": "от 1 000 руб/м²",
                "описание": "Шпаклёвка, покраска, обои"
            },
            {
                "название": "Ремонт квартиры под ключ (с материалом)",
                "цена": "базовый от 30 000 руб/м², средний от 55 000 руб/м², премиум от 75 000 руб/м²",
                "описание": "Полный ремонт от черновых работ до финишной отделки, три уровня комплектации",
                "видео": "https://www.instagram.com/reel/DYSOdZotIWa/",
                "видео_описание": "Отправь эту ссылку клиенту если он спрашивает что входит в стоимость ремонта (базовый, стандарт или премиум)"
            },
            {
                "название": "Строительство частного дома",
                "цена": "из пеплоблока от 25 000 руб/м², из кирпича или газоблока от 32 000 руб/м²",
                "описание": "Строительство под ключ, все виды работ"
            }
        ],
        "гарантия": "1 год на все виды работ",
        "опыт": "Более 5 лет, 200+ объектов",
        "районы": "Грозный и вся Чеченская Республика"
    }

def build_tools(user_id):
    return [
        {
            "name": "remember",
            "description": "Сохраняет данные о клиенте",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key":   {"type": "string"},
                    "value": {"type": "string"}
                },
                "required": ["key", "value"]
            }
        },
        {
            "name": "recall",
            "description": "Загружает данные о клиенте. Вызывай в начале каждого разговора.",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "save_lead",
            "description": "Сохраняет заявку и уведомляет владельца. Вызывай только когда собраны: услуга, город, объём, срок и бюджет.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "service":      {"type": "string"},
                    "city":         {"type": "string"},
                    "area":         {"type": "string"},
                    "timeline":     {"type": "string"},
                    "budget":       {"type": "string"},
                    "phone":        {"type": "string", "description": "Телефон если клиент дал, иначе 'не указан'"},
                    "comment":      {"type": "string", "description": "Любая дополнительная информация"}
                },
                "required": ["service", "city", "area", "timeline", "budget"]
            }
        },
        {
            "name": "get_services",
            "description": "Возвращает список услуг HOM Group с ценами. Используй когда клиент спрашивает о ценах или услугах.",
            "input_schema": {"type": "object", "properties": {}}
        }
    ]

SYSTEM = """Ты — дружелюбный помощник строительной компании HOM Group.
Компания работает в Чеченской Республике. Занимается штукатуркой, ремонтами, строительством домов.

Твоя задача — пообщаться с клиентом и собрать заявку.

Правила общения:
- Если клиент написал "Салам" или "Ассаламу алайкум" — отвечай "Ва алайкум ассалам!" и представляйся
- Если "Привет" или "Здравствуйте" — отвечай соответственно, тепло и дружелюбно
- Будь живым, не роботом — короткие фразы, без официоза
- Задавай по одному вопросу за раз
- Если клиент сам дал несколько данных — не переспрашивай, двигайся дальше
- Если клиент назвал номер телефона — обязательно сохрани его (remember, key="phone")

Порядок сбора данных — по контексту, не строго по списку:
- Какая услуга нужна
- Город / район
- Площадь или объём работ
- Когда планирует / срок
- Бюджет (если откажется назвать — запиши "не указан", не настаивай)

Когда все данные собраны → сохрани заявку (save_lead) → скажи:
"Отлично! Передал заявку менеджеру, свяжутся с тобой в ближайшее время 🤝"

Дополнительно:
- Если спрашивают цены — используй get_services
- Если клиент спрашивает что входит в стоимость ремонта (базовый, стандарт или премиум) — используй get_services и в конце ответа добавь точно эту строку на отдельной строке: [[RENOVATION_BUTTON]]
- В начале разговора загружай память (recall)
- Сохраняй данные по ходу (remember)"""

user_sessions = {}

def process_message(user_id, username, tg_username, user_text):
    if user_id not in user_sessions:
        user_sessions[user_id] = []

    messages = user_sessions[user_id]
    tools = build_tools(user_id)

    if len(messages) == 0:
        messages.append({
            "role": "user",
            "content": f"[Новый клиент. Имя: {username}, Telegram: @{tg_username}] {user_text}"
        })
    else:
        messages.append({"role": "user", "content": user_text})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    print(f"  🔧 {block.name}({block.input})")

                    if block.name == "remember":
                        result = remember(user_id, block.input["key"], block.input["value"])
                    elif block.name == "recall":
                        result = recall(user_id)
                    elif block.name == "save_lead":
                        result = save_lead(user_id, username, tg_username, **block.input)
                    elif block.name == "get_services":
                        result = get_services()
                    else:
                        result = "ok"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            answer = response.content[0].text
            messages.append({"role": "assistant", "content": answer})
            if len(messages) > 30:
                user_sessions[user_id] = messages[-30:]
            return answer

if __name__ == "__main__":
    print("🏗 HOM Group бот запущен")
    print(f"📬 Уведомления → {OWNER_CHAT_ID}\n")

    offset = 0
    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg or "text" not in msg:
                    continue

                user_id     = msg["from"]["id"]
                username    = msg["from"].get("first_name", "Клиент")
                tg_username = msg["from"].get("username", "")
                text        = msg["text"]
                chat_id     = msg["chat"]["id"]

                print(f"📩 {username} (@{tg_username}): {text}")
                tg_typing(chat_id)

                answer = process_message(user_id, username, tg_username, text)

                if "[[RENOVATION_BUTTON]]" in answer:
                    clean = answer.replace("[[RENOVATION_BUTTON]]", "").strip()
                    tg_send_with_button(chat_id, clean)
                else:
                    tg_send(chat_id, answer)

                print(f"✅ {answer[:70]}...")

        except Exception as e:
            print(f"⚠️ {e}")
            time.sleep(3)
