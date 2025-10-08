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
BOT_TOKEN = os.getenv("BOT_TOKEN")                      # токен телеграм-бота
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")          # https://xxx.up.railway.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hook")    # путь для вебхука
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")              # ID канала/чата/пользователя (может быть -100...)

# Горячий лид: сразу отправляем в канал + в Zoho (если настроено)
AUTO_ZOHO = os.getenv("AUTO_ZOHO", "true").lower() == "true"

# Zoho CRM (опционально)
ZOHO_ACCESS_TOKEN = os.getenv("ZOHO_ACCESS_TOKEN")      # OAuth токен
ZOHO_DC = os.getenv("ZOHO_DC", "eu")                    # eu | com | in | au
ZOHO_MODULE = os.getenv("ZOHO_MODULE", "Leads")         # обычный модуль для создания записей

# ========= LOAD QUESTIONS (ONLY ECOMMERCE) =========
with open("questions.yaml", "r", encoding="utf-8") as f:
    Q = yaml.safe_load(f)["ecommerce"]

# ========= STATE (in-memory) =========
SESSIONS: Dict[int, Dict[str, Any]] = {}

# ========= FASTAPI =========
app = FastAPI()
TG_APP = None  # будет установлен на старте, если BOT_TOKEN валиден

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    return PlainTextResponse("E-commerce TZ Bot is running.")

# Безопасный старт: не валим сервис, если токен не задан/битый
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
    # если бот не инициализирован — не падаем 500, а отдаём 503
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

def kb_confirm():
    return InlineKeyboardMarkup([[  # итоговое подтверждение клиентом
        InlineKeyboardButton("✅ Подтвердить", callback_data="confirm:yes"),
        InlineKeyboardButton("✏️ Редактировать", callback_data="confirm:edit")
    ]])

def format_summary(data: Dict[str, Any]) -> str:
    lines = [f"*Тип проекта:* {Q['title']}"]
    for q in Q["questions"]:
        qid = q["id"]
        val = data["answers"].get(qid)
        if val is None: continue
        if isinstance(val, list):
            val = ", ".join(map(str, val))
        lines.append(f"*{q['text']}* — {val}")
    return "\n".join(lines)

async def send_to_admin(text: str):
    """Шлём бриф в ADMIN_CHAT_ID (личка/группа/канал)."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await TG_APP.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"send_to_admin error: {e}")

async def create_zoho_lead(data: Dict[str, Any], summary_md: str) -> Optional[dict]:
    """Создаёт лид в Zoho, если есть токен. Возвращает ответ Zoho."""
    if not ZOHO_ACCESS_TOKEN:
        return None
    base = f"https://www.zohoapis.{ZOHO_DC}/crm/v2/{ZOHO_MODULE}"
    record = {
        # подставь под свои поля в Zoho:
        "Company": data["answers"].get("company_name") or "N/A",
        "Last_Name": data["answers"].get("company_name") or "Client",
        "Lead_Source": "Telegram Bot",
        "Description": summary_md,  # вся сводка в Description для старта
    }
    headers = {"Authorization": f"Zoho-oauthtoken {ZOHO_ACCESS_TOKEN}"}
    payload = {"data": [record], "trigger": ["workflow"]}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(base, headers=headers, json=payload)
            return r.json()
    except Exception as e:
        print(f"create_zoho_lead error: {e}")
        return {"error": str(e)}

# ========= FLOW =========
async def start(update, context):
    user = update.effective_user
    SESSIONS[user.id] = {"answers": {}, "q_index": 0, "multi_buffer": {}}
    await update.message.reply_text("Привет! 👋 Составим ТЗ для интернет-магазина.")
    await ask_next(update, SESSIONS[user.id])

async def setwebhook(update, context):
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

async def ask_next(target, sess):
    q = current_question(sess)
    if not q:
        summary = format_summary(sess)
        text = "Проверьте, всё ли верно:\n\n" + summary
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm())
        else:
            await target.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm())
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

async def on_callback(update, context):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    sess = SESSIONS.setdefault(uid, {"answers": {}, "q_index": 0, "multi_buffer": {}})
    data = query.data

    if data.startswith("opt:"):
        _, qid, idx = data.split(":")
        q = next(x for x in Q["questions"] if x["id"] == qid)
        opt = q["options"][int(idx)]
        if q.get("multi"):
            selected = sess["multi_buffer"].setdefault(qid, set())
            if opt in selected: selected.remove(opt)
            else: selected.add(opt)
            await query.edit_message_reply_markup(reply_markup=kb_options(q["options"], True, qid))
        else:
            sess["answers"][qid] = opt
            sess["q_index"] += 1
            await ask_next(query, sess)
        return

    if data.startswith("next:"):
        _, qid = data.split(":")
        sess["answers"][qid] = list(sess["multi_buffer"].get(qid, []))
        sess["q_index"] += 1
        await ask_next(query, sess)
        return

    if data == "confirm:yes":
        # Клиент подтвердил → горячий лид: сразу в канал + Zoho
        summary = format_summary(sess)

        # 1) в админ-канал/чат
        await send_to_admin("🆕 *Новый e-commerce бриф:*\n\n" + summary)

        # 2) в Zoho (если включено / есть токен)
        zoho_msg = ""
        if AUTO_ZOHO:
            res = await create_zoho_lead(sess, summary)
            if res is not None:
                try:
                    details = res["data"][0]["details"]
                    zoho_id = details.get("id")
                    zoho_msg = f"\n\n✅ Zoho Lead создан: `{zoho_id}`"
                except Exception:
                    zoho_msg = f"\n\n⚠️ Zoho ответ: `{json.dumps(res, ensure_ascii=False)}`"

        # Финальный ответ клиенту
        await query.edit_message_text("Спасибо! ✅ ТЗ подтверждено. Мы свяжемся с вами в ближайшее время.")
        # И доп. уведомление менеджерам (в том же админ-чате)
        if zoho_msg:
            await send_to_admin(zoho_msg)
        return

    if data == "confirm:edit":
        sess["answers"] = {}
        sess["q_index"] = 0
        sess["multi_buffer"] = {}
        await ask_next(query, sess)
        return

async def on_text(update, context):
    uid = update.effective_user.id
    sess = SESSIONS.setdefault(uid, {"answers": {}, "q_index": 0, "multi_buffer": {}})
    q = current_question(sess)
    if not q:
        await update.message.reply_text("Пожалуйста, подтвердите ТЗ выше.")
        return
    sess["answers"][q["id"]] = update.message.text.strip()
    sess["q_index"] += 1
    await ask_next(update, sess)
