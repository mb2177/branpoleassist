import os, json, yaml
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
import httpx

BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hook")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Zoho — внедрим позже, пока заглушка
ZOHO_ENABLED = False

with open("questions.yaml", "r", encoding="utf-8") as f:
    Q = yaml.safe_load(f)["ecommerce"]

SESSIONS: Dict[int, Dict[str, Any]] = {}

app = FastAPI()
TG_APP = None

@app.get("/health")
def health():
    return {"ok": True}

@app.on_event("startup")
async def startup():
    global TG_APP
    TG_APP = ApplicationBuilder().token(BOT_TOKEN).build()
    TG_APP.add_handler(CommandHandler("start", start))
    TG_APP.add_handler(CommandHandler("setwebhook", setwebhook))
    TG_APP.add_handler(CallbackQueryHandler(on_callback))
    TG_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    await TG_APP.initialize()

@app.post(f"/telegram/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, TG_APP.bot)
    await TG_APP.process_update(update)
    return JSONResponse({"ok": True})

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
    return InlineKeyboardMarkup([[
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
    if ADMIN_CHAT_ID:
        try:
            await TG_APP.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

async def start(update, context):
    user = update.effective_user
    SESSIONS[user.id] = {"answers": {}, "q_index": 0, "multi_buffer": {}}
    await update.message.reply_text("Привет! 👋 Составим ТЗ для интернет‑магазина.")
    await ask_next(update, SESSIONS[user.id])

async def setwebhook(update, context):
    if not PUBLIC_BASE_URL:
        await update.message.reply_text("PUBLIC_BASE_URL не задан в переменных.")
        return
    ok = await context.bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/{WEBHOOK_SECRET}")
    await update.message.reply_text(f"Webhook set: {ok}")

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
        summary = format_summary(sess)
        await query.edit_message_text("Спасибо! ✅ ТЗ подтверждено. Мы свяжемся с вами в ближайшее время.")
        await send_to_admin("🆕 *Новый e‑commerce бриф:*\n\n" + summary)
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

@app.get("/")
def root():
    return PlainTextResponse("E‑commerce TZ Bot is running.")
