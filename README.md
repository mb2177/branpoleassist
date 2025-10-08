# E‑commerce Brief Bot (Railway)

Мини‑бот для опроса клиента по интернет‑магазину. Собирает ТЗ, показывает сводку и шлёт в админ‑чат.

## Деплой за 5 шагов
1) Импортируйте проект на Railway.
2) В Variables добавьте:
   - `BOT_TOKEN` — токен вашего бота
   - `PUBLIC_BASE_URL` — домен Railway, например `https://xxx.up.railway.app`
   - `WEBHOOK_SECRET` — любое слово (например `hook123`)
   - `ADMIN_CHAT_ID` — ID чата/канала/пользователя, куда слать бриф
3) Деплой.
4) В Telegram напишите боту `/setwebhook` — он пропишет webhook.
5) Команда `/start` — запускает опрос.

## Правки
- Вопросы редактируются в `questions.yaml`.
- Интеграцию с Zoho можно добавить в `app.py` (позиция send_to_admin/после подтверждения).

## Что дальше
- Добавить PDF экспорт брифа.
- Подключить Zoho (создание Lead/Deal).
- Хранить ответы в Postgres/Redis (Railway plugins).
