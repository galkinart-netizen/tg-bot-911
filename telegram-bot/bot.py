import asyncio
import base64
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Tuple
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import OpenAI

# Буфер фото по user_id для разового разбора (доступен из job)
_pending: Dict[int, Dict[str, Any]] = {}  # user_id -> {"chat_id": int, "file_ids": [(file_id, mime), ...]}
# Последнее заключение по user_id для кнопок «Диагноз» и «Лечение»
_user_last: Dict[int, Dict[str, str]] = {}  # user_id -> {"diagnosis": str, "treatment": str}
# Отложенные задачи батча, когда job_queue недоступен: user_id -> asyncio.Task
_pending_tasks: Dict[int, asyncio.Task] = {}

# Callback data для выбора ИИ
CB_GROQ = "ai:groq"
CB_OPENAI = "ai:openai"
# Поток после приветствия: Начать -> согласие -> опросник
CB_FLOW_START = "flow:start"
CB_CONSENT_ACCEPT = "consent:accept"
CB_CONSENT_DECLINE = "consent:decline"
# После опросника: опрос или загрузить документы
CB_NEXT_SURVEY = "next:survey"
CB_NEXT_UPLOAD = "next:upload"
# Опрос: кнопка «Отправить ответ»
CB_SURVEY_SEND = "survey:send"

# Единый блок юридического согласия (РФ) — одно сообщение, кнопка «Согласен и продолжить»
CONSENT_TEXT = """📄 <b>Единый блок юридического согласия (РФ)</b>
Информированное согласие пользователя

Нажимая кнопку «Согласен и продолжить», я подтверждаю, что:

• Настоящий сервис не является медицинской организацией, не осуществляет медицинскую деятельность и не оказывает медицинские услуги в смысле Федерального закона № 323-ФЗ «Об основах охраны здоровья граждан в Российской Федерации».

• Бот не устанавливает диагноз, не назначает лечение и не заменяет консультацию врача. Предоставляемая информация носит справочный, информационно-аналитический характер и не является медицинским заключением.

• Я осознаю необходимость обращения к врачу или в медицинскую организацию при ухудшении состояния здоровья.

• В случае возникновения экстренных симптомов (угроза жизни, выраженный болевой синдром, потеря сознания, признаки инсульта или инфаркта и др.) я обязан(а) немедленно обратиться за медицинской помощью или вызвать скорую помощь.

• Я добровольно даю согласие на обработку моих персональных данных, включая специальные категории персональных данных (сведения о состоянии здоровья), в соответствии с Федеральным законом № 152-ФЗ «О персональных данных», исключительно в целях анализа состояния здоровья и формирования информационных рекомендаций в рамках работы сервиса.

• Я подтверждаю, что предоставляю достоверную информацию о своём состоянии здоровья и понимаю, что ответственность за принятие решений о лечении лежит на мне и/или моем лечащем враче."""

# Медицинский опросник — полный список (46 вопросов), пока для теста используем первые 5
MEDICAL_QUESTIONS_FULL = [
    ("Фамилия, имя, отчество", None),
    ("Год рождения (например, 1955)", None),
    ("Пол", "мужской / женский"),
    ("Рост (см)", None),
    ("Вес (кг)", None),
    ("Социальный статус", "проживает один / с семьёй / в учреждении ухода"),
    ("Основная жалоба", "боль / слабость / одышка / повышение температуры / падение / другое"),
    ("Локализация боли", "грудная клетка / живот / голова / поясница / суставы / иное"),
    ("Характер боли", "острая / ноющая / давящая / колющая / приступообразная"),
    ("Интенсивность боли", "слабая / умеренная / выраженная / нестерпимая"),
    ("Длительность симптомов", "сегодня / 1–3 дня / более недели"),
    ("Динамика состояния", "улучшение / без изменений / ухудшение"),
    ("Связано ли ухудшение с физической нагрузкой?", "да / нет"),
    ("Предшествовал ли стресс?", "да / нет"),
    ("Были ли подобные эпизоды ранее?", "да / нет"),
    ("Вызывалась ли скорая медицинская помощь?", "да / нет"),
    ("Артериальная гипертензия?", "да / нет"),
    ("Ишемическая болезнь сердца?", "да / нет"),
    ("Инфаркт миокарда в анамнезе?", "да / нет"),
    ("Инсульт в анамнезе?", "да / нет"),
    ("Сахарный диабет?", "да / нет"),
    ("Хроническая болезнь почек?", "да / нет"),
    ("ХОБЛ / бронхиальная астма?", "да / нет"),
    ("Онкологические заболевания?", "да / нет"),
    ("Принимаете ли вы постоянную терапию?", "да / нет"),
    ("Гипотензивные препараты?", "да / нет"),
    ("Антикоагулянты / антиагреганты?", "да / нет"),
    ("Инсулин / сахароснижающие препараты?", "да / нет"),
    ("Приняты ли препараты сегодня?", "да / нет / пропустил"),
    ("Артериальное давление", "<100 / 100–130 / 130–150 / 150–180 / >180"),
    ("Температура тела", "норма / до 37.5 / 37.5–38.5 / >38.5"),
    ("Частота пульса", "<60 / 60–90 / >90 / не измерял"),
    ("Наличие отёков?", "да / нет"),
    ("Нарушение речи?", "да / нет"),
    ("Онемение конечностей?", "да / нет"),
    ("Нарушение зрения?", "да / нет"),
    ("Судорожный синдром?", "да / нет"),
    ("Потеря сознания?", "да / нет"),
    ("Факт падения?", "да / нет"),
    ("Удар головой?", "да / нет"),
    ("Потеря сознания при падении?", "да / нет"),
    ("Боль в области таза или шейки бедра?", "да / нет"),
    ("Наличие лекарственной аллергии?", "да / нет / не знаю"),
    ("Аллергия на продукты питания?", "да / нет"),
    ("Имеются ли результаты лабораторных анализов?", "да / нет"),
    ("Имеются ли заключения врачей?", "да / нет"),
    ("Имеются ли результаты инструментальных исследований (УЗИ / КТ / МРТ)?", "да / нет"),
    ("Общее самочувствие", "удовлетворительное / средней тяжести / тяжёлое / крайне тяжёлое"),
]
# Сейчас активны только первые 5 вопросов (для теста). Чтобы вернуть все 46: MEDICAL_QUESTIONS = MEDICAL_QUESTIONS_FULL
MEDICAL_QUESTIONS = MEDICAL_QUESTIONS_FULL[:5]


def _ai_choice_keyboard() -> Optional[InlineKeyboardMarkup]:
    """Инлайн-кнопки выбора ИИ (только доступные провайдеры)."""
    buttons = []
    if _use_groq():
        buttons.append(InlineKeyboardButton("Groq", callback_data=CB_GROQ))
    if get_openai_client():
        buttons.append(InlineKeyboardButton("OpenAI (GPT)", callback_data=CB_OPENAI))
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


# Основная клавиатура (показывается после опроса и в рабочих сценариях)
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Старт"), KeyboardButton("Стоп"), KeyboardButton("Перезапустить")],
        [KeyboardButton("Добавить фото"), KeyboardButton("Диагноз"), KeyboardButton("Лечение")],
    ],
    resize_keyboard=True,
)

# Во время опроса внизу только эта кнопка (вместо инлайн под сообщением)
SURVEY_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Отправить ответ")]],
    resize_keyboard=True,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Google Таблица: каждая отправка опроса — новая строка, столбец дата и время ---
SHEET_HEADER = ["id", "fio", "birth_year", "дата и время заполнения"] + [f"q{i}" for i in range(1, 46)]


def _sheet_append_and_get_id(survey_answers: Dict[str, str]) -> Tuple[Optional[int], Optional[str]]:
    """
    Каждый раз добавляет новую строку в Google Таблицу (один и тот же пользователь может заполнять опрос многократно).
    id — уникальный номер записи (увеличивается), в столбце «дата и время заполнения» — момент отправки.
    Возвращает (id, None) при успехе или (None, сообщение_об_ошибке).
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials.json")
    if not sheet_id or not sheet_id.strip():
        return None, None
    creds_path = (creds_path or "").strip()
    if not creds_path or not os.path.isfile(creds_path):
        logger.warning("Google credentials file not found: %s", creds_path)
        return None, "Файл учётных данных не найден."
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return None, "Установите: pip install gspread google-auth"
    fio = (survey_answers.get("q1") or "").strip()
    birth_year = (survey_answers.get("q2") or "").strip()
    if not fio:
        return None, "Нет ФИО (ответ на 1-й вопрос)."
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id.strip())
        wks = sh.sheet1
        rows = wks.get_all_values()
        if not rows:
            wks.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")
            rows = wks.get_all_values()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        new_id = 1
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) >= 1:
                try:
                    existing_id = int(row[0])
                    if existing_id >= new_id:
                        new_id = existing_id + 1
                except (ValueError, TypeError):
                    pass
        new_row = [new_id, fio, birth_year, now]
        for j in range(1, 46):
            new_row.append(survey_answers.get(f"q{j}", "")[:500])
        wks.append_row(new_row, value_input_option="USER_ENTERED")
        return new_id, None
    except Exception as e:
        logger.exception("Google Sheet append error: %s", e)
        return None, str(e)[:200]


def _sheet_start_row() -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """
    Создаёт новую пустую строку в таблице в начале опроса.
    Возвращает (row_index_1based, id, None) при успехе или (None, None, сообщение_об_ошибке).
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials.json")
    if not sheet_id or not sheet_id.strip():
        return None, None, None
    creds_path = (creds_path or "").strip()
    if not creds_path or not os.path.isfile(creds_path):
        logger.warning("Google credentials file not found: %s", creds_path)
        return None, None, "Файл учётных данных не найден."
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return None, None, "Установите: pip install gspread google-auth"
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id.strip())
        wks = sh.sheet1
        rows = wks.get_all_values()
        if not rows:
            wks.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")
            rows = wks.get_all_values()
        new_id = 1
        for i, row in enumerate(rows):
            if i == 0:
                continue
            if len(row) >= 1:
                try:
                    existing_id = int(row[0])
                    if existing_id >= new_id:
                        new_id = existing_id + 1
                except (ValueError, TypeError):
                    pass
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        new_row = [new_id, "", "", now] + [""] * 45
        wks.append_row(new_row, value_input_option="USER_ENTERED")
        row_index = len(rows) + 1
        return row_index, new_id, None
    except Exception as e:
        logger.exception("Google Sheet start row error: %s", e)
        return None, None, str(e)[:200]


def _sheet_update_cell(row_index: int, key: str, value: str) -> Optional[str]:
    """
    Обновляет одну ячейку в строке row_index (1-based).
    key — один из: "fio", "birth_year", "дата и время заполнения", "q1".."q45".
    Возвращает None при успехе или сообщение об ошибке.
    """
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON", "credentials.json")
    if not sheet_id or not sheet_id.strip():
        return None
    creds_path = (creds_path or "").strip()
    if not creds_path or not os.path.isfile(creds_path):
        return "Файл учётных данных не найден."
    if key not in SHEET_HEADER:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return "Установите: pip install gspread google-auth"
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id.strip())
        wks = sh.sheet1
        col_index = SHEET_HEADER.index(key) + 1
        wks.update_cell(row_index, col_index, (value or "")[:500])
        return None
    except Exception as e:
        logger.exception("Google Sheet update cell error: %s", e)
        return str(e)[:200]


# Промпт для одного документа
MEDICAL_PROMPT = """Ты помогаешь пожилым людям понять медицинские документы: анализы, заключения врачей, выписки из больницы.

Твоя задача:
1. Прочитай и разбери всё, что видишь на изображении (текст, цифры, печати).
2. Объясни результат ПРОСТЫМИ словами, без сложных медицинских терминов (или сразу поясняй их).
3. Скажи, что в норме, а на что стоит обратить внимание.
4. Если есть отклонения — объясни, что они могут значить и нужно ли срочно к врачу.
5. В конце кратко резюмируй: всё ли в порядке и что делать дальше.

Пиши по-русски, короткими предложениями, доброжелательно. Не пугай, но и не скрывай важное."""

# Промпт для нескольких документов: один общий разбор, хронология, выводы, рекомендации
MULTI_DOC_PROMPT = """Ты помогаешь пожилым людям разобраться в нескольких медицинских документах сразу: анализы, заключения, выписки.

По всем изображениям вместе сделай ОДНО короткое заключение. Уложи в ДВА структурированных абзаца (нумеруй пункты 1-2-3). Пиши максимально просто и чётко.

АБЗАЦ 1 — ЧТО ПРОИЗОШЛО И ЧТО ЭТО ЗНАЧИТ:
1) Кратко: что произошло со здоровьем (хронология, связки между документами).
2) Какие цифры и результаты анализов/исследований это подтверждают (ключевые показатели).
3) Что предполагают врачи, что уже сделали и что планируют сделать.

АБЗАЦ 2 — ЧТО ДЕЛАТЬ И К ЧЕМУ БЫТЬ ГОТОВЫМ:
1) Чего врачи не сделали или не учли — что важно спросить на приёме.
2) Как себя вести и к чему быть готовым (реалистично, без паники).
3) Что точно делать и чего точно не делать (конкретные рекомендации).

Пиши по-русски, простыми словами. Не пугай, но не скрывай важное. Без лишних деталей — только суть и выводы."""

TEXT_PROMPT = """Ты помогаешь пожилым людям разобраться в вопросах здоровья и медицинских терминах. Отвечай простым русским языком, коротко и по делу. Если спрашивают про анализы или диагнозы — объясни без страшных слов и подскажи, что делать дальше."""

# Ключи в user_data для буфера фото
PENDING_IMAGES_KEY = "pending_images"
PENDING_CHAT_ID_KEY = "pending_chat_id"
BATCH_DELAY_SEC = 10


GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"


def _use_groq() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def get_groq_client() -> Optional[OpenAI]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    return OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key)


def get_openai_client() -> Optional[OpenAI]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def _no_ai_message() -> str:
    return "Не задан ни один ключ нейросети. Добавь в .env: GROQ_API_KEY (console.groq.com) или OPENAI_API_KEY (platform.openai.com)."


def _short_error(e: Exception) -> str:
    msg = str(e).strip()
    if "429" in msg or "quota" in msg.lower() or "insufficient_quota" in msg:
        return "Закончился лимит по ключу. Groq: console.groq.com; OpenAI: platform.openai.com."
    if "401" in msg or "invalid" in msg.lower() or "api_key" in msg.lower():
        return "Неверный или недействительный API-ключ. Проверь ключ в .env."
    if "access denied" in msg.lower() or "network" in msg.lower():
        return "Доступ к API заблокирован (сеть или регион). Попробуй другой ключ или VPN."
    if len(msg) > 200:
        return msg[:197] + "..."
    return msg


def _escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_conclusion_for_elderly(raw: str) -> str:
    """Форматирует заключение для удобного чтения: заголовки, эмодзи, жирный текст, разбивка."""
    if not raw or len(raw) > 4000:
        raw = (raw or "")[:4000]
    raw = _escape_html(raw)
    # Markdown **текст** → HTML <b>текст</b> (для пожилых — лучше читается)
    raw = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", raw)
    # Заголовки абзацев — жирным и с эмодзи
    raw = raw.replace("АБЗАЦ 1 —", "\n\n<b>📋 Что произошло и что это значит</b>\n\n")
    raw = raw.replace("АБЗАЦ 2 —", "\n\n<b>💊 Рекомендации: что делать и чего не делать</b>\n\n")
    # Строки вида ### 4. Заключение или #### Подзаголовок — делаем жирными
    raw = re.sub(r"(?m)^#+\s*(.+)$", r"\n<b>\1</b>\n", raw)
    # Нумерованные пункты 1) 2) 3) — с эмодзи для наглядности
    raw = re.sub(r"(\d+)\)\s*", r"\n\1️⃣ ", raw)
    raw = re.sub(r"^(\d+)\s*\)", r"\1️⃣", raw, flags=re.MULTILINE)
    # Убираем оставшиеся # в начале строк
    raw = re.sub(r"(?m)^#+\s*", "", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = raw.strip()
    if len(raw) > 4080:
        raw = raw[:4077] + "..."
    return raw


def _save_conclusion(user_id: int, conclusion: str) -> None:
    """Сохраняет заключение по user_id: разбивает на диагноз и рекомендации по лечению."""
    conclusion = (conclusion or "").strip()
    if not conclusion:
        return
    diagnosis = conclusion
    treatment = ""
    if "АБЗАЦ 2" in conclusion:
        parts = conclusion.split("АБЗАЦ 2", 1)
        diagnosis = parts[0].strip()
        treatment = ("АБЗАЦ 2 " + parts[1]).strip() if len(parts) > 1 else ""
    if user_id not in _user_last:
        _user_last[user_id] = {"diagnosis": "", "treatment": ""}
    _user_last[user_id]["diagnosis"] = diagnosis[:4000]
    _user_last[user_id]["treatment"] = treatment[:4000] if treatment else conclusion[:4000]


async def _ask_openai_image(image_b64: str, mime: str = "image/jpeg") -> str:
    client = get_openai_client()
    if not client:
        return ""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": MEDICAL_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Объясни этот медицинский документ простыми словами по пунктам из инструкции."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            },
        ],
        max_tokens=1500,
    )
    return (response.choices[0].message.content or "").strip()


async def _ask_openai_text(user_text: str) -> str:
    client = get_openai_client()
    if not client:
        return ""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": TEXT_PROMPT},
            {"role": "user", "content": user_text},
        ],
        max_tokens=1000,
    )
    return (response.choices[0].message.content or "").strip()


def _build_multi_content(images: list) -> list:
    """Список content для OpenAI/Groq: текст + все картинки."""
    parts = [{"type": "text", "text": "По всем приложенным документам сделай одно заключение по инструкции (два абзаца, пункты 1-2-3)."}]
    for b64, mime in images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts


async def _ask_groq_images(images: list) -> str:
    """images: список кортежей (image_b64, mime)."""
    client = get_groq_client()
    if not client or not images:
        return ""
    content = _build_multi_content(images)
    response = client.chat.completions.create(
        model=GROQ_VISION_MODEL,
        messages=[
            {"role": "system", "content": MULTI_DOC_PROMPT},
            {"role": "user", "content": content},
        ],
        max_tokens=2000,
    )
    return (response.choices[0].message.content or "").strip()


async def _ask_openai_images(images: list) -> str:
    """images: список кортежей (image_b64, mime)."""
    client = get_openai_client()
    if not client or not images:
        return ""
    content = _build_multi_content(images)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": MULTI_DOC_PROMPT},
            {"role": "user", "content": content},
        ],
        max_tokens=2000,
    )
    return (response.choices[0].message.content or "").strip()


async def _ask_groq_image(image_b64: str, mime: str = "image/jpeg") -> str:
    client = get_groq_client()
    if not client:
        return ""
    response = client.chat.completions.create(
        model=GROQ_VISION_MODEL,
        messages=[
            {"role": "system", "content": MEDICAL_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Объясни этот медицинский документ простыми словами по пунктам из инструкции."},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            },
        ],
        max_tokens=1500,
    )
    return (response.choices[0].message.content or "").strip()


async def _ask_groq_text(user_text: str) -> str:
    client = get_groq_client()
    if not client:
        return ""
    response = client.chat.completions.create(
        model=GROQ_TEXT_MODEL,
        messages=[
            {"role": "system", "content": TEXT_PROMPT},
            {"role": "user", "content": user_text},
        ],
        max_tokens=1000,
    )
    return (response.choices[0].message.content or "").strip()


def _progress_bar(pct: int) -> str:
    """Полоска загрузки 0–100%."""
    n = 10
    filled = round(n * pct / 100)
    bar = "█" * filled + "░" * (n - filled)
    return f"[{bar}] {pct}%"


async def _progress_updater(bot: Any, chat_id: int, message_id: int, stop_event: asyncio.Event) -> None:
    """Обновляет сообщение с полосой загрузки каждую секунду (0→95%), пока не установлен stop_event."""
    for i in range(1, 16):
        if stop_event.is_set():
            return
        pct = min(95, i * 6)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"Анализирую ваши данные…\n\n{_progress_bar(pct)}",
            )
        except Exception:
            pass
        await asyncio.sleep(1)
    # Держим 95% пока не скажут стоп
    while not stop_event.is_set():
        await asyncio.sleep(0.5)


async def _process_pending_images(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, provider: Optional[str] = None
) -> None:
    """Скачать все фото из буфера, показать полосу загрузки, отправить в выбранную AI, выдать одно отформатированное сообщение.
    provider: "groq" | "openai" | None (None = сначала Groq, при ошибке OpenAI)."""
    data = _pending.pop(user_id, None)
    if not data:
        return
    chat_id = data["chat_id"]
    file_ids = data["file_ids"]
    if not file_ids:
        return
    bot = context.bot
    images_b64: List[tuple] = []
    for file_id, mime in file_ids:
        try:
            tg_file = await bot.get_file(file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            buf.seek(0)
            raw = buf.read()
            images_b64.append((base64.b64encode(raw).decode("utf-8"), mime))
        except Exception as e:
            logger.warning("Не удалось загрузить файл %s: %s", file_id, e)
    if not images_b64:
        await bot.send_message(chat_id, "Не удалось загрузить ни одного документа. Попробуй отправить снова.", reply_markup=MAIN_KEYBOARD)
        return

    # Одно сообщение «Анализирую ваши данные» с полосой загрузки
    progress_msg = await bot.send_message(
        chat_id,
        f"Анализирую ваши данные…\n\n{_progress_bar(0)}",
    )
    stop_event = asyncio.Event()
    progress_task = asyncio.create_task(_progress_updater(bot, chat_id, progress_msg.message_id, stop_event))

    text = ""
    last_err = None
    has_groq = _use_groq()
    has_openai = bool(get_openai_client())
    try:
        if provider == "groq" and has_groq:
            try:
                text = await _ask_groq_images(images_b64)
            except Exception as e:
                last_err = e
                logger.warning("Groq при разборе нескольких фото: %s", e)
        elif provider == "openai" and has_openai:
            try:
                text = await _ask_openai_images(images_b64)
            except Exception as e:
                last_err = e
                logger.warning("OpenAI при разборе нескольких фото: %s", e)
        else:
            # provider is None or не совпадает — пробуем Groq, потом OpenAI
            if has_groq:
                try:
                    text = await _ask_groq_images(images_b64)
                except Exception as e:
                    last_err = e
                    logger.warning("Groq при разборе нескольких фото: %s", e)
            if not text and has_openai:
                try:
                    text = await _ask_openai_images(images_b64)
                except Exception as e:
                    last_err = e
                    logger.warning("OpenAI при разборе нескольких фото: %s", e)
    finally:
        stop_event.set()
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"Анализирую ваши данные…\n\n{_progress_bar(100)}",
            )
        except Exception:
            pass
        await asyncio.sleep(0.3)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=progress_msg.message_id)
        except Exception:
            pass

    if not text:
        await bot.send_message(
            chat_id,
            "Не удалось составить заключение. " + (_short_error(last_err) if last_err else "Проверь ключи в .env."),
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if len(text) > 4000:
        text = text[:3997] + "..."
    _save_conclusion(user_id, text)
    formatted = _format_conclusion_for_elderly(text)
    await bot.send_message(
        chat_id,
        formatted,
        reply_markup=MAIN_KEYBOARD,
        parse_mode="HTML",
    )


async def _job_process_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data
    await _process_pending_images(context, user_id, provider="groq")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "<b>Привет!</b> 👋\n\n"
        "Я — помощник по медицинским документам. Разбираю анализы и заключения врачей простыми словами, "
        "чтобы вы и ваши близкие могли спокойно понять результаты и знать, что делать дальше.\n\n"
        "<b>Чем я полезен:</b>\n"
        "1️⃣ Объясняю анализы и выписки без сложных терминов\n"
        "2️⃣ Подсказываю, что в норме, а на что обратить внимание\n"
        "3️⃣ Даю понятные рекомендации: к врачу ли идти и о чём спросить\n"
        "4️⃣ Отвечаю на вопросы о здоровье простым языком\n\n"
        "<b>Что можно сделать:</b>\n"
        "• Пройти короткий опрос о здоровье\n"
        "• Прислать фото анализов или заключений — разберу по пунктам\n"
        "• Написать вопрос текстом — отвечу простым языком\n\n"
        "Нажмите кнопку ниже, чтобы начать."
    )
    await update.message.reply_text(
        welcome,
        reply_markup=_start_button_keyboard(),
        parse_mode="HTML",
    )


def _next_step_keyboard() -> InlineKeyboardMarkup:
    """Кнопки после ввода имени: опрос или загрузить документы."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Пройти опрос", callback_data=CB_NEXT_SURVEY)],
        [InlineKeyboardButton("📎 Загрузить документы", callback_data=CB_NEXT_UPLOAD)],
    ])


def _survey_send_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Отправить ответ» в опросе."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Отправить ответ", callback_data=CB_SURVEY_SEND)],
    ])


def _start_button_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Начать» после приветствия."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Начать", callback_data=CB_FLOW_START)],
    ])


def _consent_keyboard() -> InlineKeyboardMarkup:
    """Согласен и продолжить / Не согласен для единого блока согласия."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Согласен и продолжить", callback_data=CB_CONSENT_ACCEPT)],
        [InlineKeyboardButton("Не согласен", callback_data=CB_CONSENT_DECLINE)],
    ])


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — приветствие\n"
        "/help — эта справка\n\n"
        "Кнопки: Старт, Стоп, Перезапустить, Добавить фото, Диагноз (последнее заключение), Лечение (последние рекомендации).\n\n"
        "Можно прислать несколько фото подряд — через 10 сек разберу вместе (или выбери ИИ кнопкой). Или напиши «всё» / «готово». Вопрос текстом — отвечу простыми словами.",
        reply_markup=MAIN_KEYBOARD,
    )


def _schedule_pending_job(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Запланировать разбор буфера через BATCH_DELAY_SEC: через job_queue или asyncio."""
    if context.job_queue:
        job_name = f"process_pending_{user_id}"
        for job in context.job_queue.jobs():
            if job.name == job_name:
                job.schedule_removal()
                break
        context.job_queue.run_once(
            _job_process_pending,
            when=BATCH_DELAY_SEC,
            data=user_id,
            name=job_name,
        )
        return
    # Без job_queue — отложенный запуск через asyncio
    old = _pending_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    async def _delayed_batch(app: Any, uid: int) -> None:
        await asyncio.sleep(BATCH_DELAY_SEC)
        _pending_tasks.pop(uid, None)
        class _C:
            pass
        ctx = _C()
        ctx.bot = app.bot
        await _process_pending_images(ctx, uid, provider="groq")
    _pending_tasks[user_id] = asyncio.create_task(_delayed_batch(context.application, user_id))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    has_groq = _use_groq()
    has_openai = bool(get_openai_client())
    if not has_groq and not has_openai:
        await update.message.reply_text(_no_ai_message())
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]
    file_id = photo.file_id
    mime = "image/jpeg"

    if user_id not in _pending:
        _pending[user_id] = {"chat_id": chat_id, "file_ids": []}
    _pending[user_id]["chat_id"] = chat_id
    _pending[user_id]["file_ids"].append((file_id, mime))
    n = len(_pending[user_id]["file_ids"])

    _schedule_pending_job(context, user_id)
    msg = (
        f"Получил ({n} документ(ов)). Пришли ещё в течение {BATCH_DELAY_SEC} сек — разберу всё вместе. "
        f"Или напиши «всё» / «готово».\n\nВыберите ИИ для анализа:"
    )
    keyboard = _ai_choice_keyboard()
    await update.message.reply_text(
        msg,
        reply_markup=keyboard if keyboard else MAIN_KEYBOARD,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if doc.mime_type and not doc.mime_type.startswith("image/"):
        await update.message.reply_text(
            "Пока принимаю только фото и картинки (JPEG, PNG). PDF не поддерживаю — пришли, пожалуйста, скриншот страницы."
        )
        return

    has_groq = _use_groq()
    has_openai = bool(get_openai_client())
    if not has_groq and not has_openai:
        await update.message.reply_text(_no_ai_message())
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    mime = (doc.mime_type or "image/jpeg").strip()
    if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        mime = "image/jpeg"

    if user_id not in _pending:
        _pending[user_id] = {"chat_id": chat_id, "file_ids": []}
    _pending[user_id]["chat_id"] = chat_id
    _pending[user_id]["file_ids"].append((doc.file_id, mime))
    n = len(_pending[user_id]["file_ids"])

    _schedule_pending_job(context, user_id)
    msg = (
        f"Получил ({n} документ(ов)). Пришли ещё в течение {BATCH_DELAY_SEC} сек — разберу всё вместе. "
        f"Или напиши «всё» / «готово».\n\nВыберите ИИ для анализа:"
    )
    keyboard = _ai_choice_keyboard()
    await update.message.reply_text(
        msg,
        reply_markup=keyboard if keyboard else MAIN_KEYBOARD,
    )


async def handle_ai_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатия кнопки выбора ИИ (Groq / OpenAI)."""
    query = update.callback_query
    await query.answer()
    data = (query.data or "").strip()
    user_id = query.from_user.id if query.from_user else 0
    if user_id not in _pending or not _pending[user_id].get("file_ids"):
        await query.edit_message_text("Документов для разбора нет. Пришлите фото или файлы.")
        return
    provider = None
    if data == CB_GROQ:
        provider = "groq"
    elif data == CB_OPENAI:
        provider = "openai"
    if not provider:
        return
    # Отменить отложенный запуск по таймеру
    if context.job_queue:
        job_name = f"process_pending_{user_id}"
        for job in context.job_queue.jobs():
            if job.name == job_name:
                job.schedule_removal()
                break
    task = _pending_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    try:
        await query.edit_message_text("Запускаю анализ…")
    except Exception:
        pass
    await _process_pending_images(context, user_id, provider=provider)


def _format_medical_question(step: int, total: int) -> str:
    """Текст вопроса опросника с вариантами (если есть)."""
    q, variants = MEDICAL_QUESTIONS[step - 1]
    line = f"<b>Вопрос {step} из {total}</b>\n\n{q}"
    if variants:
        line += f"\n\n({variants})"
    line += "\n\nНапишите ответ в чат и нажмите Enter."
    return line


async def handle_flow_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Начать» — удаляем приветствие, отправляем единый блок согласия."""
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        query.message.chat_id,
        CONSENT_TEXT,
        parse_mode="HTML",
        reply_markup=_consent_keyboard(),
    )


async def handle_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Согласен и продолжить → первый вопрос опросника; Не согласен → стоп."""
    query = update.callback_query
    data = (query.data or "").strip()
    chat_id = query.message.chat_id
    bot = context.bot
    if data == CB_CONSENT_DECLINE:
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            "Мы уважаем ваше решение.\n\n"
            "К сожалению, без согласия на обработку персональных данных и с условиями использования сервиса мы не можем предоставить возможность пользоваться ботом. "
            "Если измените решение — нажмите /start и примите условия.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if data != CB_CONSENT_ACCEPT:
        return
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    context.user_data["survey_step"] = 1
    context.user_data["survey_answers"] = {}
    sheet_row, sheet_id, _ = await asyncio.to_thread(_sheet_start_row)
    if sheet_row is not None and sheet_id is not None:
        context.user_data["survey_sheet_row"] = sheet_row
        context.user_data["survey_sheet_id"] = sheet_id
    else:
        context.user_data.pop("survey_sheet_row", None)
        context.user_data.pop("survey_sheet_id", None)
    total_q = len(MEDICAL_QUESTIONS)
    q_text = _format_medical_question(1, total_q)
    sent = await bot.send_message(
        chat_id, q_text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    context.user_data["survey_question_message_id"] = sent.message_id


async def handle_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Пройти опрос» или «Загрузить документы» (если опросник запущен отдельно)."""
    query = update.callback_query
    await query.answer()
    data = (query.data or "").strip()
    if data == CB_NEXT_SURVEY:
        context.user_data["survey_step"] = 1
        context.user_data["survey_answers"] = {}
        sheet_row, sheet_id, _ = await asyncio.to_thread(_sheet_start_row)
        if sheet_row is not None and sheet_id is not None:
            context.user_data["survey_sheet_row"] = sheet_row
            context.user_data["survey_sheet_id"] = sheet_id
        else:
            context.user_data.pop("survey_sheet_row", None)
            context.user_data.pop("survey_sheet_id", None)
        total = len(MEDICAL_QUESTIONS)
        q_text = _format_medical_question(1, total)
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass
        sent = await context.bot.send_message(
            chat_id, q_text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["survey_question_message_id"] = sent.message_id
    elif data == CB_NEXT_UPLOAD:
        upload_text = (
            "Пришлите фото или файл с анализом/заключением. Можно несколько — "
            "через 10 сек разберу вместе (бесплатный Groq) или нажмите кнопку и выберите ИИ."
        )
        try:
            await query.edit_message_text(upload_text, reply_markup=MAIN_KEYBOARD)
        except Exception:
            await query.message.reply_text(upload_text, reply_markup=MAIN_KEYBOARD)


async def handle_survey_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Отправить ответ» — удаляем предыдущий вопрос, отправляем следующий (кнопка всегда под актуальным вопросом)."""
    query = update.callback_query
    step = context.user_data.get("survey_step", 0)
    answers = context.user_data.get("survey_answers") or {}
    key = f"q{step}"
    if not answers.get(key):
        await query.answer("Сначала напишите ответ в чат и отправьте сообщение.", show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat_id
    bot = context.bot
    # Удаляем сообщение с предыдущим вопросом — в чате оно исчезает (в части клиентов с анимацией)
    try:
        await query.message.delete()
    except Exception:
        pass
    next_step = step + 1
    total = len(MEDICAL_QUESTIONS)
    if next_step <= total:
        context.user_data["survey_step"] = next_step
        q_text = _format_medical_question(next_step, total)
        sent = await bot.send_message(
            chat_id,
            q_text,
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data["survey_question_message_id"] = sent.message_id
    else:
        context.user_data.pop("survey_answers", None)
        user_id_sheet = context.user_data.pop("survey_sheet_id", None)
        context.user_data.pop("survey_step", None)
        context.user_data.pop("survey_question_message_id", None)
        context.user_data.pop("survey_sheet_row", None)
        done_text = "Спасибо! Опрос завершён. Теперь можно присылать анализы и документы — учту ваши ответы при разборе."
        await bot.send_message(chat_id, done_text, reply_markup=MAIN_KEYBOARD)
        if user_id_sheet is not None:
            await bot.send_message(
                chat_id,
                f"<b>Ваш уникальный ID:</b> {user_id_sheet}\n\nСохраните его — он привязан к ФИО и дате рождения.",
                parse_mode="HTML",
                reply_markup=MAIN_KEYBOARD,
            )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    # Ожидаем имя клиента после /start — не отправляем в ИИ, не ищем в интернете
    if context.user_data.get("awaiting_client_name"):
        context.user_data["awaiting_client_name"] = False
        context.user_data["client_name"] = user_text[:200]
        await update.message.reply_text(
            f"Записал: <b>{_escape_html(user_text[:200])}</b>.\n\nЧто делаем дальше?",
            reply_markup=_next_step_keyboard(),
            parse_mode="HTML",
        )
        return

    # Ответ на вопрос опросника: ввёл текст и нажал Enter — сохраняем и сразу показываем следующий вопрос
    survey_step = context.user_data.get("survey_step")
    if survey_step and 1 <= survey_step <= len(MEDICAL_QUESTIONS):
        if "survey_answers" not in context.user_data:
            context.user_data["survey_answers"] = {}
        answer_val = user_text[:500]
        context.user_data["survey_answers"][f"q{survey_step}"] = answer_val
        sheet_row = context.user_data.get("survey_sheet_row")
        if sheet_row is not None:
            await asyncio.to_thread(_sheet_update_cell, sheet_row, f"q{survey_step}", answer_val)
            if survey_step == 1:
                await asyncio.to_thread(_sheet_update_cell, sheet_row, "fio", answer_val)
            elif survey_step == 2:
                await asyncio.to_thread(_sheet_update_cell, sheet_row, "birth_year", answer_val)
        chat_id = update.effective_chat.id
        bot = context.bot
        msg_id = context.user_data.get("survey_question_message_id")
        if msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        next_step = survey_step + 1
        total = len(MEDICAL_QUESTIONS)
        if next_step <= total:
            context.user_data["survey_step"] = next_step
            q_text = _format_medical_question(next_step, total)
            sent = await bot.send_message(
                chat_id, q_text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
            )
            context.user_data["survey_question_message_id"] = sent.message_id
        else:
            answers = context.user_data.get("survey_answers") or {}
            user_id_sheet = context.user_data.pop("survey_sheet_id", None)
            context.user_data.pop("survey_step", None)
            context.user_data.pop("survey_answers", None)
            context.user_data.pop("survey_question_message_id", None)
            context.user_data.pop("survey_sheet_row", None)
            await bot.send_message(
                chat_id,
                "Спасибо! Опрос завершён. Теперь можно присылать анализы и документы — учту ваши ответы при разборе.",
                reply_markup=MAIN_KEYBOARD,
            )
            if user_id_sheet is not None:
                await bot.send_message(
                    chat_id,
                    f"<b>Ваш уникальный ID:</b> {user_id_sheet}\n\nСохраните его — он привязан к ФИО и дате рождения.",
                    parse_mode="HTML",
                    reply_markup=MAIN_KEYBOARD,
                )
        return

    # Кнопки: Старт, Стоп, Перезапустить, Добавить фото, Диагноз, Лечение
    if user_text == "Старт":
        await start(update, context)
        return
    if user_text == "Стоп":
        if context.job_queue:
            job_name = f"process_pending_{user_id}"
            for job in context.job_queue.jobs():
                if job.name == job_name:
                    job.schedule_removal()
                    break
        task = _pending_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        _pending.pop(user_id, None)
        await update.message.reply_text("Остановлено. Буфер фото очищен.", reply_markup=MAIN_KEYBOARD)
        return
    if user_text == "Перезапустить":
        if context.job_queue:
            job_name = f"process_pending_{user_id}"
            for job in context.job_queue.jobs():
                if job.name == job_name:
                    job.schedule_removal()
                    break
        task = _pending_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        _pending.pop(user_id, None)
        await update.message.reply_text(
            "Перезапуск. Буфер очищен. Можешь начать заново: пришли фото или нажми «Добавить фото».",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if user_text == "Добавить фото":
        await update.message.reply_text(
            "Пришли фото или файл с анализом/заключением. Можно несколько — через 10 сек разберу вместе или напиши «всё» / «готово».",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if user_text == "Диагноз":
        last = _user_last.get(user_id, {})
        diagnosis = (last.get("diagnosis") or "").strip()
        if diagnosis:
            await update.message.reply_text(diagnosis, reply_markup=MAIN_KEYBOARD)
        else:
            await update.message.reply_text("Пока нет сохранённого заключения. Пришли фото анализов/документов — разберу и сохраню.", reply_markup=MAIN_KEYBOARD)
        return
    if user_text == "Лечение":
        last = _user_last.get(user_id, {})
        treatment = (last.get("treatment") or last.get("diagnosis") or "").strip()
        if treatment:
            await update.message.reply_text(treatment, reply_markup=MAIN_KEYBOARD)
        else:
            await update.message.reply_text("Пока нет сохранённых рекомендаций по лечению. Пришли фото — после разбора они появятся здесь.", reply_markup=MAIN_KEYBOARD)
        return

    # «Всё» / «готово» — показать выбор ИИ и ждать нажатия кнопки (или уже есть кнопки в предыдущем сообщении)
    if user_text.lower() in ("всё", "готово", "все", "готово."):
        if user_id in _pending and _pending[user_id]["file_ids"]:
            if context.job_queue:
                job_name = f"process_pending_{user_id}"
                for job in context.job_queue.jobs():
                    if job.name == job_name:
                        job.schedule_removal()
                        break
            task = _pending_tasks.pop(user_id, None)
            if task and not task.done():
                task.cancel()
            keyboard = _ai_choice_keyboard()
            if keyboard:
                await update.message.reply_text(
                    "Готово к анализу. Выберите ИИ:",
                    reply_markup=keyboard,
                )
            else:
                await _process_pending_images(context, user_id)
            return
        # иначе просто ответим, что нечего разбирать
        await update.message.reply_text("Пока нет документов для разбора. Пришли фото или файлы анализов/заключений.", reply_markup=MAIN_KEYBOARD)
        return

    # Не отправлять в ИИ текст, похожий на ФИО (имя человека) — бот не ищет информацию о людях
    words = user_text.split()
    if (
        len(user_text) < 100
        and "?" not in user_text
        and 2 <= len(words) <= 5
        and all((w.replace("-", "").replace(".", "").isalpha()) for w in words)
    ):
        await update.message.reply_text(
            "Похоже на ФИО. Я не ищу информацию о людях в интернете.\n\n"
            "Если проходите опрос — нажмите «Начать» в приветствии и отвечайте на вопросы по порядку.\n"
            "Если нужна помощь по здоровью — задайте вопрос словами (например: что значит повышенный сахар?).",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    has_groq = _use_groq()
    has_openai = bool(get_openai_client())
    if not has_groq and not has_openai:
        await update.message.reply_text(_no_ai_message())
        return

    await update.message.reply_text("Думаю…")

    text = ""
    last_err = None
    if has_groq:
        try:
            text = await _ask_groq_text(user_text)
        except Exception as e:
            last_err = e
            logger.warning("Groq при тексте: %s", e)
    if not text and has_openai:
        try:
            text = await _ask_openai_text(user_text)
        except Exception as e:
            last_err = e
            logger.warning("OpenAI при тексте: %s", e)

    if not text:
        msg = "Не удалось получить ответ. "
        if last_err:
            msg += _short_error(last_err)
        else:
            msg += "Проверь ключи в .env (Groq или OpenAI)."
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
        return
    if len(text) > 4000:
        text = text[:3997] + "..."
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("Задай BOT_TOKEN в .env или в переменных окружения")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_flow_start, pattern="^flow:"))
    app.add_handler(CallbackQueryHandler(handle_consent, pattern="^consent:"))
    app.add_handler(CallbackQueryHandler(handle_ai_choice, pattern="^ai:"))
    app.add_handler(CallbackQueryHandler(handle_next_step, pattern="^next:"))
    app.add_handler(CallbackQueryHandler(handle_survey_send, pattern="^survey:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен (опросник: %d вопросов)", len(MEDICAL_QUESTIONS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
