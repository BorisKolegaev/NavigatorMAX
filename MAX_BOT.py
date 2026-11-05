import asyncio
import json
import logging
import os
import sys

from gigachat import GigaChat
from maxapi import Bot, Dispatcher, F
from maxapi.types import MessageCreated, BotStarted

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = "f9LHodD0cOKrULQgA7zXPuir0oP9kEWf-YUt2mep5y-4fIMVuWjfFYF4jXn8y4k70W5YImQLw-8xR0SD2Z2O"
API_CREDENTIALS = os.getenv(
    "GIGACHAT_CREDENTIALS",
    "ВСТАВИТЬ ТОКЕН ГИГА-ЧАТА",
)

try:
    with open("courses_ai.json", "r", encoding="utf-8") as _f:
        COURSES_DATA = json.load(_f)
except Exception as e:
    print(f"[!] Не удалось загрузить courses_ai.json: {e}")
    COURSES_DATA = []

EXCLUDED_TAGS = {"право", "художественное", "проектная деятельность", "дошкольники"}
ALL_TAGS = sorted({tag for course in COURSES_DATA for tag in course.get("tags", []) if tag and tag not in EXCLUDED_TAGS})
ALL_DAYS = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
ALLOWED_TAGS = set(ALL_TAGS)
ALLOWED_DAYS = set(ALL_DAYS)
FORBIDDEN_WORDS = {"программа", "курс", "занятие", "обучение", "урок", "кружок", "школа"}

PROMPT_TEMPLATE = f"""
Ты — парсер поисковых запросов для навигатора дополнительного образования.
Преобразуй запрос пользователя в JSON со следующими полями:
- age (int или null) — возраст ребёнка
- tags (список строк) — ТОЛЬКО из этого списка:
  {", ".join(ALL_TAGS)}
  Если в запросе нет ни одного тега из списка — верни пустой список [].
  НИКОГДА не добавляй слова "программа", "курс", "занятие", "обучение", "урок", "кружок", "школа".
- teachers (список строк) — ФИО преподавателей, если упомянуты
- days (список строк) — ТОЛЬКО из: {", ".join(ALL_DAYS)}
- time_from (строка "HH:MM" или null)
- organization (строка или null) — название организации

Верни ТОЛЬКО валидный JSON без пояснений, без markdown, без ```.
Запрос пользователя:
"""

ORG_MAPPING = {
    2: "центр дополнительного образования до",
    3: "дополнительное образование рисование",
    4: "гбу до ро ступени успеха региональный центр",
    5: "исторический парк россия моя история музей",
    91: "мбу до сш 1 спортшкола спортивная школа спорт",
    123: "мбу до сш 1 школа баскетбола спортивная борьба греко-римская",
    146: "цдод центр детского творчества малюгиной борисоглебская мечникова кропоткина дранко фрунзе соборный",
}

ORG_NAMES_DISPLAY = {
    2: "Центр ДО",
    3: "Центр ДО (Дистанционно)",
    4: "ГБУ ДО РО 'Ступени успеха'",
    5: "Исторический парк 'Россия – Моя история'",
    91: "МБУ ДО СШ № 1 (Корпус 1)",
    123: "МБУ ДО СШ № 1 (Корпус 2)",
    146: "Центр детского творчества (ЦДОД)",
}

WELCOME_TEXT = (
    "👋 Здравствуйте! Я навигатор дополнительного образования.\n\n"
    "Напишите, что хотите найти, и я подберу программы.\n\n"
    "Например:\n"
    "• «Рисование для ребёнка 10 лет»\n"
    "• «Баскетбол в СШ №1»\n"
    "• «Физика для 15 лет по субботам»\n\n"
    "🎤 Если у вас настроен Webhook, можете отправить голосовое сообщение — я постараюсь его распознать."
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def parse_ai_json(text):
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None


def sanitize_parsed(parsed: dict) -> dict:
    tags = parsed.get("tags") or []
    cleaned_tags = []
    for t in tags:
        t_clean = str(t).strip().lower()
        if not t_clean or t_clean in FORBIDDEN_WORDS:
            continue
        for allowed in ALLOWED_TAGS:
            if t_clean == allowed.lower() or t_clean in allowed.lower():
                cleaned_tags.append(allowed)
                break
    parsed["tags"] = list(dict.fromkeys(cleaned_tags))

    days = parsed.get("days") or []
    parsed["days"] = [str(d).upper() for d in days if str(d).upper() in ALLOWED_DAYS]

    age = parsed.get("age")
    if age is not None:
        try:
            parsed["age"] = int(age)
        except (TypeError, ValueError):
            parsed["age"] = None

    return parsed


def search_courses(parsed: dict) -> list:
    age = parsed.get("age")
    tgt_tags = [str(t).lower() for t in (parsed.get("tags") or [])]
    tgt_teachers = [str(t).strip().lower() for t in (parsed.get("teachers") or []) if str(t).strip()]
    tgt_org = (parsed.get("organization") or "").strip().lower()
    tgt_time = (parsed.get("time_from") or "").strip()
    sel_days = [str(d).upper() for d in (parsed.get("days") or [])]

    results = []
    for item in COURSES_DATA:
        if age is not None and not (item.get("age_from", 0) <= age <= item.get("age_to", 99)):
            continue
        if tgt_tags:
            tags = [t.lower() for t in item.get("tags", [])]
            text = f"{item.get('name', '')} {item.get('category', '')} {item.get('summary', '')}".lower()
            if not any(tg in text or any(tg in t for t in tags) for tg in tgt_tags):
                continue
        if tgt_teachers:
            teachers = [t.lower() for t in item.get("teachers", [])]
            if not any(any(tg in t for t in teachers) for tg in tgt_teachers):
                continue
        if tgt_org:
            item_org_id = item.get("organizer_id")
            matched_org = False
            if item_org_id in ORG_MAPPING and tgt_org in ORG_MAPPING[item_org_id]:
                matched_org = True
            if not matched_org and tgt_org in item.get("location", "").lower():
                matched_org = True
            if not matched_org:
                continue
        sked = item.get("schedule", [])
        if sel_days or tgt_time:
            if not sked:
                continue
            match = False
            for slot in sked:
                day_ok = not sel_days or any(d in slot.get("days", []) for d in sel_days)
                time_ok = not tgt_time or slot.get("time_from", "") == tgt_time
                if day_ok and time_ok:
                    match = True
                    break
            if not match:
                continue
        results.append(item)
    return results


def format_course(item: dict) -> str:
    org_title = ORG_NAMES_DISPLAY.get(item.get("organizer_id"), "Не указана")
    lines = [f"» {item.get('name', 'Без названия').upper()}"]
    lines.append(f"  Организация: {org_title}")
    summary = item.get("summary", "")
    if summary:
        lines.append(f"  Описание: {summary[:200]}{'...' if len(summary) > 200 else ''}")
    teachers = ", ".join(item.get("teachers", []))
    if teachers:
        lines.append(f"  Преподаватели: {teachers}")
    lines.append(f"  Возраст: {item.get('age_from')}-{item.get('age_to')} лет")
    lines.append(f"  Формат: {item.get('format')} | Цена: {item.get('price_description')}")
    lines.append(f"  Адрес: {item.get('location')}")
    url = item.get("url", "")
    if url:
        lines.append(f"  Ссылка: {url}")
    sked = item.get("schedule", [])
    if sked:
        lines.append("  Расписание:")
        for slot in sked:
            lines.append(
                f"    [{', '.join(slot.get('days', []))}] "
                f"{slot.get('time_from')}-{slot.get('time_to')}"
            )
    return "\n".join(lines)


def format_results(results: list, max_items: int = 5) -> str:
    if not results:
        return "По вашему запросу ничего не найдено. Попробуйте изменить условия."
    header = f"Найдено программ: {len(results)}\n" + "=" * 40 + "\n"
    body = "\n\n".join(format_course(c) for c in results[:max_items])
    if len(results) > max_items:
        body += f"\n\n... и ещё {len(results) - max_items}. Уточните запрос."
    return header + body


def ask_gigachat_sync(user_query: str) -> str:
    messages = [{"role": "user", "content": PROMPT_TEMPLATE + user_query}]
    with GigaChat(credentials=API_CREDENTIALS, verify_ssl_certs=False, model="GigaChat-3-Ultra") as giga:
        response = giga.chat({"messages": messages})
        return response.choices[0].message.content


async def send_long(event, text: str, max_len: int = 3800):
    if len(text) <= max_len:
        await event.message.answer(text)
        return
    for i in range(0, len(text), max_len):
        await event.message.answer(text[i:i + max_len])


@dp.bot_started()
async def bot_started_handler(event: BotStarted):
    await event.bot.send_message(
        chat_id=event.chat_id,
        text=WELCOME_TEXT
    )


@dp.message_created(F.message.body.text == "/start")
async def start_handler(event: MessageCreated):
    await event.message.answer(WELCOME_TEXT)


@dp.message_created()
async def search_handler(event: MessageCreated):
    user_query = (event.message.body.text or "").strip() if event.message.body else ""

    if not user_query:
        voice_attachment = None
        if event.message.body and event.message.body.attachments:
            for att in event.message.body.attachments:
                if att.type == "audio" or att.type == "voice":
                    voice_attachment = att
                    break

        if voice_attachment:
            await event.message.answer(
                "🎤 Я получил голосовое сообщение, но в текущем режиме (Long Polling) "
                "не могу его надёжно обработать.\n\n"
                "Пожалуйста, напишите ваш запрос текстом."
            )
        else:
            await event.message.answer(
                "Пожалуйста, напишите ваш запрос текстом. Например: «Рисование для ребёнка 10 лет»"
            )
        return

    await event.message.answer("Отправляю запрос в GigaChat, подождите...")
    try:
        bot_reply = await asyncio.to_thread(ask_gigachat_sync, user_query)
        print("[GigaChat raw]", bot_reply)
        parsed = parse_ai_json(bot_reply)
        if not parsed:
            await event.message.answer("Не удалось разобрать ответ ИИ. Попробуйте переформулировать запрос.")
            return
        parsed = sanitize_parsed(parsed)
        results = search_courses(parsed)
        await send_long(event, format_results(results))
    except Exception as e:
        logging.exception("Ошибка при обработке запроса")
        await event.message.answer(f"Ошибка при обращении к ИИ:\n{e}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    asyncio.run(main())