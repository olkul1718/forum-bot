"""
Бот для отработки контактов на форуме Движение-2026.
Поддерживает текст и голосовые. Создаёт лиды в AmoCRM.
"""
import json
import logging
import os
import re
import tempfile

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

from amocrm import AmoCRM, ContactData

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY  = os.environ["OPENAI_API_KEY"]
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

AMO_IDA_SUBDOMAIN   = os.environ["AMO_IDA_SUBDOMAIN"]
AMO_IDA_TOKEN       = os.environ["AMO_IDA_TOKEN"]
AMO_IDA_PIPELINE_ID = int(os.getenv("AMO_IDA_PIPELINE_ID", "0"))

AMO_LITE_SUBDOMAIN   = os.environ["AMO_LITE_SUBDOMAIN"]
AMO_LITE_TOKEN       = os.environ["AMO_LITE_TOKEN"]
AMO_LITE_PIPELINE_ID = int(os.getenv("AMO_LITE_PIPELINE_ID", "0"))

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = (
    {int(u.strip()) for u in ALLOWED_USERS_RAW.split(",") if u.strip()}
    if ALLOWED_USERS_RAW.strip() else set()
)

oai = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    **({"base_url": OPENAI_BASE_URL} if OPENAI_BASE_URL else {}),
)

amocrm_ida  = AmoCRM(AMO_IDA_SUBDOMAIN,  AMO_IDA_TOKEN,  AMO_IDA_PIPELINE_ID)
amocrm_lite = AmoCRM(AMO_LITE_SUBDOMAIN, AMO_LITE_TOKEN, AMO_LITE_PIPELINE_ID)

# ── States ────────────────────────────────────────────────────────────────────
COLLECTING, ASK_CRM, ASK_INTEREST, CONFIRM, EDITING = range(5)

# ── Keyboards ─────────────────────────────────────────────────────────────────
KB_CRM = ReplyKeyboardMarkup(
    [[KeyboardButton("ИдаПроджект"), KeyboardButton("Ида.Лайт")]],
    resize_keyboard=True, one_time_keyboard=True,
)
KB_CONFIRM = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Отправить в AMO", callback_data="confirm_ok"),
    InlineKeyboardButton("✏️ Редактировать",   callback_data="confirm_edit"),
]])

# ── Helpers ───────────────────────────────────────────────────────────────────

def is_allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS


def simple_extract(text: str, existing: dict) -> dict:
    """Regex-парсер — fallback без GPT."""
    result = dict(existing)
    t = text.strip()

    # Телефон
    m = re.search(r'[\+7|8][\d\s\-\(\)]{9,14}', t)
    if m and not result.get("phone"):
        result["phone"] = re.sub(r'[\s\-\(\)]', '', m.group())

    # Email
    m = re.search(r'[\w\.\-]+@[\w\.\-]+\.\w+', t)
    if m and not result.get("email"):
        result["email"] = m.group()

    # AmoCRM аккаунт
    tl = t.lower()
    if any(w in tl for w in ["идапроджект", "идапроект", "проджект", " ida", "крупн"]):
        result["crm_account"] = "ida"
    elif any(w in tl for w in ["лайт", "lite", "небольш", "маленьк"]):
        result["crm_account"] = "lite"

    # Имя — первый фрагмент с заглавной буквы
    clean = re.sub(r'[\+7|8][\d\s\-\(\)]{9,14}', '', t)
    clean = re.sub(r'[\w\.\-]+@[\w\.\-]+\.\w+', '', clean)
    parts = [p.strip() for p in re.split(r'[,\n;]', clean) if p.strip()]
    if parts and not result.get("name"):
        for part in parts:
            if part[:1].isupper():
                result["name"] = part[:60]
                break

    return result


EXTRACT_SYSTEM = """
Ты ассистент на B2B-форуме. Из текста извлеки контактные данные и верни JSON.

Поля:
- name: имя и фамилия (строка или null)
- company: название компании контакта (строка или null)
- phone: номер телефона (строка или null)
- email: почта (строка или null)
- comment: любой комментарий/заметка (строка или null)
- crm_account: "ida" если "ИдаПроджект"/"проджект"/"крупный", "lite" если "Ида.Лайт"/"лайт"/"небольшой", null если непонятно
- interest: тема/интерес (строка или null)

Верни ТОЛЬКО валидный JSON без пояснений.
""".strip()


async def extract(text: str, existing: dict) -> dict:
    user_msg = (
        f"Уже собранные данные: {json.dumps(existing, ensure_ascii=False)}\n\n"
        f"Новый текст: {text}"
    )
    resp = await oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    # Вырезаем JSON из ответа (модель может добавить markdown)
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    parsed = json.loads(m.group()) if m else {}
    merged = dict(existing)
    for k, v in parsed.items():
        if v not in (None, "", []):
            merged[k] = v
    return merged


async def smart_extract(text: str, existing: dict) -> dict:
    """GPT с fallback на regex."""
    try:
        return await extract(text, existing)
    except Exception as e:
        logger.warning("GPT недоступен (%s) — regex fallback", e)
        return simple_extract(text, existing)


def has_identity(d: dict) -> bool:
    return bool(d.get("name") or d.get("phone") or d.get("email"))


def card(d: dict) -> str:
    crm_label = {"ida": "ИдаПроджект", "lite": "Ида.Лайт"}.get(d.get("crm_account", ""), "—")
    return (
        "📋 *Новый контакт:*\n\n"
        f"👤 Имя: {d.get('name') or '—'}\n"
        f"🏢 Компания: {d.get('company') or '—'}\n"
        f"📞 Телефон: {d.get('phone') or '—'}\n"
        f"📧 Email: {d.get('email') or '—'}\n"
        f"💼 Интерес: {d.get('interest') or '—'}\n"
        f"💬 Комментарий: {d.get('comment') or '—'}\n"
        f"🗂 AmoCRM: {crm_label}"
    )


def editable_text(d: dict) -> str:
    """Текст для редактирования — один блок, который пользователь может поправить."""
    crm_label = {"ida": "ИдаПроджект", "lite": "Ида.Лайт"}.get(d.get("crm_account", ""), "")
    parts = [
        d.get("name") or "",
        d.get("company") or "",
        d.get("phone") or "",
        d.get("email") or "",
        d.get("interest") or "",
        d.get("comment") or "",
        crm_label,
    ]
    return ", ".join(p for p in parts if p)


def data_to_contact(d: dict) -> ContactData:
    return ContactData(
        name=d.get("name") or "",
        company=d.get("company") or "",
        phone=d.get("phone") or "",
        email=d.get("email") or "",
        comment=d.get("comment") or "",
        interest=d.get("interest") or "",
        crm_account=d.get("crm_account"),
    )

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Бот для записи контактов с форума Движение-2026.\n\n"
        "Напиши или надиктуй данные контакта — имя, компанию, телефон или почту. "
        "Можно всё сразу в одном сообщении.\n\n"
        "_Пример: Иван Петров, ООО Стройтех, +79161234567, веб-разработка, ИдаПроджект_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return COLLECTING


async def _process_text(text: str, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    existing = ctx.user_data.get("contact", {})
    ctx.user_data["contact"] = await smart_extract(text, existing)
    return await _check_and_proceed(update, ctx)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END
    return await _process_text(update.message.text, update, ctx)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END

    await update.message.reply_text("🎤 Транскрибирую голосовое…")
    voice = update.message.voice or update.message.audio
    tg_file = await voice.get_file()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        with open(tmp_path, "rb") as f:
            result = await oai.audio.transcriptions.create(
                model="whisper-1", file=f, language="ru"
            )
        text = result.text
        os.unlink(tmp_path)
    except Exception as e:
        os.unlink(tmp_path)
        logger.warning("Whisper error: %s", e)
        await update.message.reply_text(
            "🎤 Голосовое не удалось распознать.\nНапиши данные текстом.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return COLLECTING

    await update.message.reply_text(f"📝 Распознано: _{text}_", parse_mode="Markdown")
    return await _process_text(text, update, ctx)


async def _check_and_proceed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    d = ctx.user_data.get("contact", {})

    if not has_identity(d):
        await update.message.reply_text(
            "Не нашёл имени, телефона или почты — добавь хотя бы что-то одно.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return COLLECTING

    if not d.get("crm_account"):
        await update.message.reply_text(
            "В какую компанию записываем контакт?",
            reply_markup=KB_CRM,
        )
        return ASK_CRM

    if not d.get("interest"):
        await update.message.reply_text(
            "С чем лучше к ним зайти? (напиши текстом)",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_INTEREST

    return await _show_confirm(update, ctx)


async def handle_crm_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.strip().lower()
    if "лайт" in t or "lite" in t:
        ctx.user_data.setdefault("contact", {})["crm_account"] = "lite"
    elif "ида" in t or "проджект" in t or "ida" in t:
        ctx.user_data.setdefault("contact", {})["crm_account"] = "ida"
    else:
        await update.message.reply_text("Выбери, пожалуйста:", reply_markup=KB_CRM)
        return ASK_CRM

    d = ctx.user_data.get("contact", {})
    if not d.get("interest"):
        await update.message.reply_text(
            "С чем лучше к ним зайти?", reply_markup=KB_INTEREST
        )
        return ASK_INTEREST

    return await _show_confirm(update, ctx)


async def handle_interest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.setdefault("contact", {})["interest"] = update.message.text.strip()
    return await _show_confirm(update, ctx)


async def _show_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    d = ctx.user_data.get("contact", {})
    text = card(d) + "\n\nВсё верно?"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=KB_CONFIRM
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=KB_CONFIRM,
        )
    return CONFIRM


async def handle_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_edit":
        d = ctx.user_data.get("contact", {})
        await query.edit_message_text(
            "Отправь исправленные данные одним сообщением:\n\n"
            f"`{editable_text(d)}`",
            parse_mode="Markdown",
        )
        return EDITING

    if query.data == "confirm_ok":
        return await _send_to_amo(update, ctx)

    return CONFIRM


async def handle_editing(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END
    # Полностью переписываем данные из нового текста
    ctx.user_data["contact"] = await smart_extract(
        update.message.text, {}
    )
    return await _check_and_proceed(update, ctx)


async def _send_to_amo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    d = ctx.user_data.get("contact", {})
    contact = data_to_contact(d)
    crm = amocrm_ida if contact.crm_account == "ida" else amocrm_lite
    crm_label = "ИдаПроджект" if contact.crm_account == "ida" else "Ида.Лайт"

    query = update.callback_query
    reply = query.message.reply_text if query else update.message.reply_text

    await reply("⏳ Отправляю в AMO…")
    try:
        lead_id = crm.create_lead(contact)
        await reply(f"✅ Лид #{lead_id} создан в {crm_label}!\n\nСледующий контакт или /start.")
    except Exception as e:
        logger.exception("AmoCRM error")
        await reply(f"❌ Ошибка AMO: {e}\n\nПопробуй ещё раз или проверь настройки.")

    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            MessageHandler(filters.VOICE | filters.AUDIO, handle_voice),
        ],
        states={
            COLLECTING: [
                MessageHandler(filters.VOICE | filters.AUDIO, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            ASK_CRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_crm_choice),
            ],
            ASK_INTEREST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interest),
            ],
            CONFIRM: [
                CallbackQueryHandler(handle_confirm, pattern="^confirm_"),
            ],
            EDITING: [
                MessageHandler(filters.VOICE | filters.AUDIO, handle_voice),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_editing),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=False,
    )

    app.add_handler(conv)
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
