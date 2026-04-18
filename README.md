# pic-of-the-day

Сервис отправляет в Telegram лучшую картинку за прошлый день из Reddit.
На текущем этапе используется только один сабреддит: `r/VaporwaveAesthetics`.

## Как это работает

- Скрипт берет посты из `top` (за неделю) и фильтрует только те, что были
  опубликованы **вчера по Москве**.
- Из найденных image-постов выбирается лучший по `score`.
- Результат отправляется в Telegram (фото, а при ошибке — текст + ссылка).

## Запуск по расписанию (GitHub Actions)

Файл workflow: `.github/workflows/daily-reddit-image.yml`

- Запуск каждый день в `06:00 UTC` (что соответствует `09:00 Europe/Moscow`).
- Workflow использует `environment: news-agent`.

### Что нужно настроить в environment `news-agent`

Секреты:

- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота
- `TELEGRAM_CHAT_ID` — chat id для отправки
- `REDDIT_CLIENT_ID` — client id Reddit app (script)
- `REDDIT_CLIENT_SECRET` — client secret Reddit app

Переменная (опционально):

- `SUBREDDITS` — список сабреддитов через запятую
  (по умолчанию `VaporwaveAesthetics`)

## Локальный запуск

```bash
python3 scripts/send_daily_reddit_image.py --dry-run
```

Для принудительной даты:

```bash
python3 scripts/send_daily_reddit_image.py --target-date 2026-04-17 --dry-run
```
