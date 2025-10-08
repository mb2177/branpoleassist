import os, json, yaml
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)
import httpx

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hook")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # ID канала/чата/юзера, куда падёт бриф
AUTO_ZOHO = False  # Zoho отключён — сделаем позже

# ========= LOAD QUESTIONS (ONLY ECOMMERCE) =========
with open("questions.yaml", "r", encoding="utf-8") as f:
    Q = yaml.safe_load(f)["ecommerce"]

# ========= STATE (in-memory) =========
# добавили поддержку редактирования: sess["editing"] = qid | None
SESSIONS: Dict[int, Dict[str, Any]] = {}

# ========= FASTAPI =========
app = FastAPI()
TG_APP = None  # заполним на старте, если BOT_TOKEN валиден

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    return PlainTextResponse("E-commerce TZ Bot is running.")

# безопасный старт: не валим сервис, если токен не задан/битый
@app.on_event("startup")
async def startup():
    global TG_APP
    token = os.getenv("BOT_TOKEN")
    if not token:
        print("WARNING: BOT_TOKEN is not set. Telegram handlers NOT initialized yet.")
        TG_APP = None
        return
    try:
        TG_APP = ApplicationBuilder().token(token).build()
        TG_APP.add_handler(CommandHandler("start", start))
        TG_APP.add_handler(CommandHandler("setwebhook", setwebhook))
        TG_APP.add_handler(CallbackQueryHandler(on_callback))
        TG_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        await TG_APP.initialize()
        print("Telegram app initialized.")
    except Exception as e:
        print(f"ERROR: Telegram init failed: {e}")
        TG_APP = None  # даём FastAPI подняться, healthcheck пройдёт

@app.post(f"/telegram/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    if TG_APP is None:
        return JSONResponse({"ok": False, "error": "Bot not initialized"}, status_code=503)
    data = await request.json()
    update = Update.de_json(data, TG_APP.bot)
    await TG_APP.process_update(update)
    return JSONResponse({"ok": True})

# ========= HELPERS =========
def kb_options(options: List[str], multi: bool, qid: str):
    rows, row = [], []
    for i, opt in enumerate(options):
        row.append(InlineKeyboardButton(opt, callback_data=f"opt:{qid}:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row: rows.append(row)
    if multi:
        rows.append([InlineKeyboardButton("Далее ▶️", callback_data=f"next:{qid}")])
    return InlineKeyboardMarkup(rows)

def kb_confirm_and_edit(sess: Dict[str, Any]):
    """Внизу сводки показываем кнопки: редактировать конкретный вопрос + подтвердить."""
    edit_rows, row = [], []
    for q in Q["questions"]:
        label = q["text"].split(" ")[0]  # возьмём первый смайлик как ярлык
        btn = InlineKeyboardButton(f"✏️ {label}", callback_data=f"edit:{q['id']}")
        row.append(btn)
        if len(row) == 2:
            edit_rows.append(row); row = []
    if row: edit_rows.append(row)
    edit_rows.append([
        InlineKeyboardButton("✅ Подтвердить", callback_data="confirm:yes"),
        InlineKeyboardButton("↩️ Сбросить", callback_data="confirm:reset"),
    ])
    return InlineKeyboardMarkup(edit_rows)

def format_summary_user(data: Dict[str, Any]) -> str:
    """Сводка для пользователя — с исходными вопросами + эмодзи."""
    lines = [f"🐾 Проверим и доведём до идеала! \n\n*Тип проекта:* {Q['title']}"]
    for q in Q["questions"]:
        qid = q["id"]
        val = data["answers"].get(qid)
        if val is None: continue
        if isinstance(val, list):
            val = ", ".join(map(str, val))
        # берём первый эмодзи из текста вопроса
        label_emoji = q["text"].split(" ")[0]
        pretty_label = q["text"]
        lines.append(f"{label_emoji} *{pretty_label}* \n— _{val}_")
    return "\n\n".join(lines)

def format_summary_admin(data: Dict[str, Any]) -> str:
    """Компактная, читабельная сводка для админ-канала/чата."""
    a = data["answers"]
    def get(id): 
        v = a.get(id)
        return ", ".join(v) if isinstance(v, list) else (v or "—")
    return (
        "🐾 *Новый e-commerce проект!*\n\n"
        f"🏷️ *Бренд:* {get('company_name')}\n"
        f"🌍 *Регионы:* {get('region')}\n"
        f"🛒 *Каталог:* {get('catalog_size')}\n"
        f"🧱 *Платформа:* {get('platform')}\n"
        f"💳 *Платежи:* {get('payments')}\n"
        f"💱 *Валюты:* {get('currencies')}\n"
        f"🚚 *Логистика:* {get('shipping')}\n"
        f"🗣️ *Языки:* {get('languages')}\n"
        f"📅 *Дедлайн:* {get('deadline')}\n"
        f"💰 *Бюджет:* {get('budget_range')}\n\n"
        f"🏷️ Категории: {get('categories')}\n"
        f"🧵 Атрибуты/фильтры: {get('attributes')}\n"
        f"📦 Доставка/правила: {get('shipping_rules')}\n"
        f"🧾 Налоги: {get('taxes')}\n"
        f"⚖️ Legal: {get('legal')}\n"
        f"📈 Маркетинг: {get('marketing')}\n"
        f"🎨 Бренд-ассеты: {get('brand_assets')}\n"
        f"📷 Контент: {get('content')}\n"
        f"🏷️ Скидки: {get('discount_logic')}\n"
        f"🔁 Возвраты: {get('return_policy')}\n"
    )

async def send_to_admin(text: str):
    """Шлём бриф в ADMIN_CHAT_ID (личка/группа/канал)."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await TG_APP.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"send_to_admin error: {e}")

def find_question(qid: str) -> Dict[str, Any]:
    for q in Q["questions"]:
        if q["id"] == qid:
            return q
    raise KeyError(qid)

def question_index(qid: str) -> int:
    for i, q in enumerate(Q["questions"]):
        if q["id"] == qid:
            return i
    return 0

# ========= FLOW =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    SESSIONS[user.id] = {"answers": {}, "q_index": 0, "multi_buffer": {}, "editing": None}
    await update.message.reply_text(
        "🐾 Привет! Я *BranPole Assistant*. Помогу быстро собрать ТЗ для твоего интернет-магазина.\n"
        "Отвечай коротко — а дальше мы всё структурируем. Поехали! 🚀",
        parse_mode=ParseMode.MARKDOWN
    )
    await ask_next(update, SESSIONS[user.id])

async def setwebhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PUBLIC_BASE_URL:
        await update.message.reply_text("PUBLIC_BASE_URL не задан в Variables.")
        return
    ok = await context.bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/{WEBHOOK_SECRET}")
    await update.message.reply_text(f"Webhook set: {ok} → {PUBLIC_BASE_URL}/telegram/{WEBHOOK_SECRET}")

def current_question(sess):
    idx = sess["q_index"]
    if idx >= len(Q["questions"]):
        return None
    return Q["questions"][idx]

async def show_summary(target, sess: Dict[str, Any]):
    # Сводка для пользователя + кнопки «Редактировать X» и «Подтвердить»
    summary = format_summary_user(sess)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_and_edit(sess))
    else:
        await target.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_and_edit(sess))

async def ask_next(target, sess: Dict[str, Any]):
    q = current_question(sess)
    if not q:
        await show_summary(target, sess)
        return
    if "options" in q:
        markup = kb_options(q["options"], q.get("multi", False), q["id"])
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(q["text"], reply_markup=markup)
        else:
            await target.message.reply_text(q["text"], reply_markup=markup)
    else:
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(q["text"])
        else:
            await target.message.reply_text(q["text"])

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    sess = SESSIONS.setdefault(uid, {"answers": {}, "q_index": 0, "multi_buffer": {}, "editing": None})
    data = query.data

    # Режим редактирования конкретного вопроса
    if data.startswith("edit:"):
        _, qid = data.split(":")
        sess["editing"] = qid
        sess["q_index"] = question_index(qid)
        sess["multi_buffer"] = {}  # сброс локального выбора для multi
        await ask_next(query, sess)
        return

    if data.startswith("opt:"):
        _, qid, idx = data.split(":")
        q = find_question(qid)
        opt = q["options"][int(idx)]
        if q.get("multi"):
            selected = sess["multi_buffer"].setdefault(qid, set())
            if opt in selected: selected.remove(opt)
            else: selected.add(opt)
            await query.edit_message_reply_markup(reply_markup=kb_options(q["options"], True, qid))
        else:
            sess["answers"][qid] = opt
            if sess.get("editing"):
                # если редактируем — сразу к сводке
                sess["editing"] = None
                await show_summary(query, sess)
            else:
                sess["q_index"] += 1
                await ask_next(query, sess)
        return

    if data.startswith("next:"):
        _, qid = data.split(":")
        sess["answers"][qid] = list(sess["multi_buffer"].get(qid, []))
        if sess.get("editing"):
            sess["editing"] = None
            await show_summary(query, sess)
        else:
            sess["q_index"] += 1
            await ask_next(query, sess)
        return

    if data == "confirm:reset":
        sess["answers"] = {}
        sess["q_index"] = 0
        sess["multi_buffer"] = {}
        sess["editing"] = None
        await ask_next(query, sess)
        return

    if data == "confirm:yes":
        # Клиент подтвердил → шлём красиво оформленный бриф в админ-канал/чат
        admin_text = format_summary_admin(sess)
        await send_to_admin(admin_text)

        await query.edit_message_text(
            "✅ Готово! Спасибо — мы получили ТЗ. Наш менеджер свяжется с тобой в ближайшее время. 🙌"
        )
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sess = SESSIONS.setdefault(uid, {"answers": {}, "q_index": 0, "multi_buffer": {}, "editing": None})
    q = current_question(sess)
    if not q:
        await update.message.reply_text("Пожалуйста, воспользуйтесь кнопками ниже.")
        return
    sess["answers"][q["id"]] = update.message.text.strip()

    # Если редактировали — сразу показываем сводку, иначе продолжаем
    if sess.get("editing"):
        sess["editing"] = None
        await show_summary(update, sess)
    else:
        sess["q_index"] += 1
        await ask_next(update, sess)
