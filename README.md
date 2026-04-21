# pic-of-the-day

Сервис раз в день отправляет в Telegram **одну картинку**.

## Режимы

### 1) Поиск по вебу (по умолчанию в GitHub Actions)

`IMAGE_SOURCE=commons` — поиск файлов на **Wikimedia Commons** через публичный
[MediaWiki API](https://commons.wikimedia.org/wiki/Commons:API). **Ключи Reddit не нужны.**

- Сначала используется **Wikimedia Picture of the Day** для целевой даты (гарантированно разная картинка по дням).
- Если POTD недоступна/не подходит, включается поиск по файлам Commons.
- Переменная **`IMAGE_SEARCH_QUERY`** — текст поиска для fallback-поиска
  (например `vaporwave aesthetic photography`).
- Для разнообразия по дням скрипт автоматически добавляет ротацию по набору близких запросов
  (`vaporwave`, `retrowave`, `synthwave`, `outrun`, `neon city` и т.д.) и выбирает картинку
  детерминированно по дате.
- Берутся первые результаты в пространстве имён «файл»; отбрасываются не-картинки и SVG.
- Среди оставшихся формируется пул лучших кандидатов, затем по дате выбирается
  детерминированно разный элемент (дневная ротация без повторов «день за днём»).
- В подписи указывается ссылка на страницу файла на Commons — **там указана лицензия**;
  агрегатор не подставляет юридический текст лицензии автоматически.

Алиасы без ключей: `IMAGE_SOURCE=web`, `search`, `wikimedia` (и устаревшие `openverse` —
теперь тоже ведут на Commons, т.к. публичный Openverse API стал требовать авторизацию).

### 2) Reddit по сабреддитам (вчера по Москве)

`IMAGE_SOURCE=reddit` (или переменная не задана).

- По умолчанию сабреддиты: `VaporwaveAesthetics`, `pics` (список `SUBREDDITS` через запятую).
- Берутся посты из `top` за неделю, фильтр по **вчерашнему календарному дню по МСК**,
  лучший image-пост по `score`.
- Если публичный Reddit из CI отвечает **403**, можно добавить секреты
  `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` (OAuth), иначе используется fallback **PullPush**
  (индекс может отставать от «вчера»).

Если за день ничего не найдено, в Telegram уходит **короткое** служебное сообщение,
job завершается успешно; полная диагностика — в логе GitHub Actions.

## Запуск по расписанию (GitHub Actions)

Файл: `.github/workflows/daily-reddit-image.yml`

- Каждый день в `06:00 UTC` (= `09:00` по Москве).
- Environment: `news-agent`.

### Секреты в `news-agent`

Обязательно:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Опционально (только для режима Reddit + OAuth):

- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`

Переменные workflow / environment (примеры):

- `IMAGE_SOURCE` — `commons` или `reddit`
- `IMAGE_SEARCH_QUERY` — строка поиска для Commons

## Локальный запуск

Режим Commons (без Telegram):

```bash
IMAGE_SOURCE=commons IMAGE_SEARCH_QUERY="vaporwave sunset" \
  python3 scripts/send_daily_reddit_image.py --dry-run
```

Режим Reddit:

```bash
IMAGE_SOURCE=reddit python3 scripts/send_daily_reddit_image.py --dry-run
```

Дата в подписи (метка «за какой день»):

```bash
python3 scripts/send_daily_reddit_image.py --dry-run --target-date 2026-04-17
```
