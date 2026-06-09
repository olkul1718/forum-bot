"""
Бот для отработки контактов на форуме Движение-2026.
"""
import json
import logging
import os
import re
import tempfile

from dotenv import load_dotenv
from openai import AsyncOpenAI
import httpx
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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.DEBUG,
)
# Убираем spam от httpx/telegram на уровне INFO
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
logger.info("=== BOT STARTING ===")
logger.info("ENV KEYS: %s", list(os.environ.keys()))

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

AMO_IDA_SUBDOMAIN   = os.getenv("AMO_IDA_SUBDOMAIN", "").strip()
AMO_IDA_TOKEN       = os.getenv("AMO_IDA_TOKEN", "").strip()
AMO_IDA_PIPELINE_ID = int(os.getenv("AMO_IDA_PIPELINE_ID", "0") or "0")

AMO_LITE_SUBDOMAIN   = os.getenv("AMO_LITE_SUBDOMAIN", "").strip()
AMO_LITE_TOKEN       = os.getenv("AMO_LITE_TOKEN", "").strip()
AMO_LITE_PIPELINE_ID = int(os.getenv("AMO_LITE_PIPELINE_ID", "0") or "0")

GOOGLE_SHEET_WEBHOOK = os.getenv("GOOGLE_SHEET_WEBHOOK", "").strip()

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = (
    {int(u.strip()) for u in ALLOWED_USERS_RAW.split(",") if u.strip()}
    if ALLOWED_USERS_RAW.strip() else set()
)

logger.info("TELEGRAM_TOKEN loaded: %s", bool(TELEGRAM_TOKEN))
logger.info("TELEGRAM_TOKEN first 10 chars: %s", repr(TELEGRAM_TOKEN[:15]))
logger.info("OPENAI_API_KEY loaded: %s", bool(OPENAI_API_KEY))
logger.info("AMO_IDA_SUBDOMAIN: %s", AMO_IDA_SUBDOMAIN)
logger.info("AMO_LITE_SUBDOMAIN: %s", AMO_LITE_SUBDOMAIN)
logger.info("GOOGLE_SHEET_WEBHOOK loaded: %s", bool(GOOGLE_SHEET_WEBHOOK))

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is empty! Check .env or Railway Variables.")

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
    result = dict(existing)
    t = text.strip()

    m = re.search(r'[\+7|8][\d\s\-\(\)]{9,14}', t)
    if m and not result.get("phone"):
        result["phone"] = re.sub(r'[\s\-\(\)]', '', m.group())

    m = re.search(r'[\w\.\-]+@[\w\.\-]+\.\w+', t)
    if m and not result.get("email"):
        result["email"] = m.group()

    tl = t.lower()
    if any(w in tl for w in ["идапроджект", "идапроект", "проджект", " ida", "крупн"]):
        result["crm_account"] = "ida"
    elif any(w in tl for w in ["лайт", "lite", "небольш", "маленьк"]):
        result["crm_account"] = "lite"

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
- name: имя и фамилия контакта (строка или null)
- company: название компании контакта. Компания часто начинается с: ООО, АО, ИП, ЖК, СЗ, ГК, ТЦ, ТРЦ, МФЦ (строка или null)
- phone: номер телефона (строка или null)
- email: почта (строка или null)
- comment: комментарий (строка или null)
- crm_account: "ida" если ИдаПроджект/крупный, "lite" если Ида.Лайт/небольшой, null если непонятно
- interest: тема/интерес (строка или null)

Верни ТОЛЬКО валидный JSON.
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
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    parsed = json.loads(m.group()) if m else {}
    merged = dict(existing)
    for k, v in parsed.items():
        if v not in (None, "", []):
            merged[k] = v
    return merged


async def smart_extract(text: str, existing: dict) -> dict:
    try:
        return await extract(text, existing)
    except Exception as e:
        logger.warning("GPT недоступен (%s) — regex fallback", e)
        return simple_extract(text, existing)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        return f"+7{digits}"
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return f"+7{digits[1:]}"
    return phone


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
    raw_phone = d.get("phone") or ""
    phone = normalize_phone(raw_phone) if raw_phone else ""
    return ContactData(
        name=d.get("name") or "",
        company=d.get("company") or "",
        phone=phone,
        email=d.get("email") or "",
        comment=d.get("comment") or "",
        interest=d.get("interest") or "",
        crm_account=d.get("crm_account"),
    )


async def push_to_sheets(contact: ContactData) -> None:
    if not GOOGLE_SHEET_WEBHOOK:
        logger.info("Google Sheets webhook not configured, skipping")
        return
    payload = {
        "name": contact.name, "company": contact.company,
        "phone": contact.phone, "email": contact.email,
        "interest": contact.interest, "comment": contact.comment,
        "crm_account": contact.crm_account or "",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            resp = await client.post(GOOGLE_SHEET_WEBHOOK, json=payload)
            if resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("location", "")
                if redirect_url:
                    resp = await client.post(redirect_url, json=payload)
        logger.info("Google Sheets: %s %s", resp.status_code, resp.text[:100])
    except Exception as e:
        logger.warning("Google Sheets ошибка: %s", e)

# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception handling update %s:", update, exc_info=context.error)

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("cmd_start from user %s", update.effective_user.id)
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END

    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Бот для записи контактов с форума Движение-2026.\n\n"
        "Напиши или надиктуй данные контакта — имя, компанию, телефон или почту.\n\n"
        "_Пример: Иван Петров, ООО Стройтех, +79161234567, ИдаПроджект_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return COLLECTING


async def _process_text(text: str, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("_process_text: %s", text[:50])
    existing = ctx.user_data.get("contact", {})
    data = await smart_extract(text, existing)
    if data.get("phone"):
        data["phone"] = normalize_phone(data["phone"])
    ctx.user_data["contact"] = data
    logger.info("Extracted contact: %s", data)
    return await _check_and_proceed(update, ctx)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("handle_text from user %s: %s", update.effective_user.id, update.message.text[:50])
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END
    return await _process_text(update.message.text, update, ctx)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("handle_voice from user %s", update.effective_user.id)
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
    logger.info("_check_and_proceed: %s", d)

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

    return await _show_confirm(update, ctx)


async def handle_crm_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.strip().lower()
    logger.info("handle_crm_choice: %s", t)
    if "лайт" in t or "lite" in t:
        ctx.user_data.setdefault("contact", {})["crm_account"] = "lite"
    elif "ида" in t or "проджект" in t or "ida" in t:
        ctx.user_data.setdefault("contact", {})["crm_account"] = "ida"
    else:
        await update.message.reply_text("Выбери, пожалуйста:", reply_markup=KB_CRM)
        return ASK_CRM

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
            text, parse_mode="Markdown", reply_markup=KB_CONFIRM,
        )
    return CONFIRM


async def handle_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    logger.info("handle_confirm: %s", query.data)

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
    ctx.user_data["contact"] = await smart_extract(update.message.text, {})
    return await _check_and_proceed(update, ctx)


async def _send_to_amo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    d = ctx.user_data.get("contact", {})
    contact = data_to_contact(d)
    crm = amocrm_ida if contact.crm_account == "ida" else amocrm_lite
    crm_label = "ИдаПроджект" if contact.crm_account == "ida" else "Ида.Лайт"
    logger.info("_send_to_amo: %s -> %s", contact.name, crm_label)

    query = update.callback_query
    reply = query.message.reply_text if query else update.message.reply_text

    await reply("⏳ Отправляю в AMO…")
    try:
        lead_id = crm.create_lead(contact)
        await push_to_sheets(contact)
        await reply(f"✅ Лид #{lead_id} создан в {crm_label}!\n\nСледующий контакт или /start.")
        logger.info("Lead %s created successfully", lead_id)
    except Exception as e:
        logger.exception("AmoCRM error")
        await reply(f"❌ Ошибка AMO: {e}\n\nПопробуй ещё раз.")

    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(error_handler)

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
    logger.info("Bot started. Token: %s... Webhook: %s",
                TELEGRAM_TOKEN[:15], bool(GOOGLE_SHEET_WEBHOOK))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
