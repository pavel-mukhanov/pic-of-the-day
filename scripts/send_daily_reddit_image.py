#!/usr/bin/env python3
"""Send a daily image to Telegram: from Reddit subreddits or from web image search (Openverse)."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_SUBREDDITS = ("VaporwaveAesthetics", "pics")
HTTP_USER_AGENT = "pic-of-the-day/1.0 (+https://github.com/pavel-mukhanov/pic-of-the-day)"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
DEFAULT_COMMONS_QUERY_VARIANTS = (
    "vaporwave aesthetic",
    "vaporwave city night",
    "retro wave neon",
    "synthwave sunset",
    "outrun neon",
)


class NoImageForTargetDayError(RuntimeError):
    """Raised when there are no suitable images for the run."""


class RedditOAuthError(RuntimeError):
    """Raised when Reddit OAuth is configured but cannot be initialized."""


def parse_subreddits(raw_value: str | None) -> list[str]:
    if not raw_value:
        return list(DEFAULT_SUBREDDITS)

    names = [item.strip() for item in raw_value.split(",")]
    return [name for name in names if name]


def parse_query_variants(raw_value: str | None) -> list[str]:
    if not raw_value:
        return list(DEFAULT_COMMONS_QUERY_VARIANTS)
    variants = [item.strip() for item in raw_value.split("|")]
    return [variant for variant in variants if variant]


def image_source_mode() -> str:
    raw = (os.getenv("IMAGE_SOURCE") or "reddit").strip().lower()
    # "web" / legacy openverse aliases → Wikimedia Commons search (no API keys).
    if raw in ("commons", "web", "search", "wikimedia", "openverse", "openverse_search"):
        return "commons"
    return "reddit"


def target_day_window_utc(target_day_msk: dt.date) -> tuple[int, int]:
    start_local = dt.datetime.combine(target_day_msk, dt.time.min, tzinfo=MOSCOW_TZ)
    end_local = start_local + dt.timedelta(days=1)
    return int(start_local.timestamp()), int(end_local.timestamp())


def yesterday_moscow() -> dt.date:
    now_moscow = dt.datetime.now(MOSCOW_TZ)
    return now_moscow.date() - dt.timedelta(days=1)


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request_headers = {"User-Agent": HTTP_USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=request_headers,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def get_reddit_oauth_token(client_id: str, client_secret: str) -> str:
    encoded_credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode(
        "ascii"
    )
    payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
    result = fetch_json(
        "https://www.reddit.com/api/v1/access_token",
        method="POST",
        data=payload,
        headers={
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Failed to get Reddit OAuth token.")
    return token


def init_reddit_oauth_token() -> str | None:
    reddit_client_id = os.getenv("REDDIT_CLIENT_ID")
    reddit_client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    if not reddit_client_id or not reddit_client_secret:
        return None

    try:
        return get_reddit_oauth_token(reddit_client_id, reddit_client_secret)
    except Exception as error:  # noqa: BLE001
        raise RedditOAuthError(str(error)) from error


def fetch_reddit_top_posts(
    subreddit: str,
    *,
    oauth_token: str | None,
    period: str = "week",
    limit: int = 100,
) -> list[dict[str, Any]]:
    encoded_subreddit = urllib.parse.quote(subreddit, safe="")
    query = urllib.parse.urlencode({"t": period, "limit": limit, "raw_json": 1})

    if oauth_token:
        url = f"https://oauth.reddit.com/r/{encoded_subreddit}/top?{query}"
        payload = fetch_json(url, headers={"Authorization": f"Bearer {oauth_token}"})
    else:
        url = f"https://www.reddit.com/r/{encoded_subreddit}/top.json?{query}"
        payload = fetch_json(url)

    children = payload.get("data", {}).get("children", [])
    return [item.get("data", {}) for item in children if isinstance(item, dict)]


def fetch_pullpush_posts(
    subreddit: str,
    *,
    after_utc: int,
    before_utc: int,
    limit: int = 200,
    before_cursor: int | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "subreddit": subreddit,
        "size": limit,
        "after": after_utc,
        "before": before_utc,
        "sort": "desc",
        "sort_type": "created_utc",
    }
    if before_cursor is not None:
        params["before"] = before_cursor
    query = urllib.parse.urlencode(params)
    url = f"https://api.pullpush.io/reddit/search/submission/?{query}"
    payload = fetch_json(url)
    items = payload.get("data", [])
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def fetch_pullpush_posts_window(
    subreddit: str,
    *,
    after_utc: int,
    before_utc: int,
    page_size: int = 200,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    all_posts: list[dict[str, Any]] = []
    before_cursor: int | None = None

    for _ in range(max_pages):
        page = fetch_pullpush_posts(
            subreddit=subreddit,
            after_utc=after_utc,
            before_utc=before_utc,
            limit=page_size,
            before_cursor=before_cursor,
        )
        if not page:
            break

        all_posts.extend(page)
        last_created = page[-1].get("created_utc")
        if not isinstance(last_created, (int, float)):
            break

        next_before = int(last_created)
        if before_cursor is not None and next_before >= before_cursor:
            break
        before_cursor = next_before

        if len(page) < page_size:
            break

    return all_posts


def extract_image_url(post: dict[str, Any]) -> str | None:
    candidates: list[str] = []
    for key in ("url_overridden_by_dest", "url"):
        value = post.get(key)
        if isinstance(value, str):
            candidates.append(value)

    preview = post.get("preview", {})
    if isinstance(preview, dict):
        images = preview.get("images", [])
        for image in images:
            if not isinstance(image, dict):
                continue
            for variant in ("source", "resolutions"):
                items = image.get(variant)
                if not isinstance(items, list):
                    continue
                for entry in items:
                    if not isinstance(entry, dict):
                        continue
                    preview_url = entry.get("url")
                    if isinstance(preview_url, str):
                        candidates.append(preview_url)

    if post.get("is_gallery") and isinstance(post.get("media_metadata"), dict):
        metadata = post["media_metadata"]
        gallery_data = post.get("gallery_data", {})
        items = gallery_data.get("items", []) if isinstance(gallery_data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            media_id = item.get("media_id")
            if not isinstance(media_id, str):
                continue
            meta = metadata.get(media_id)
            if not isinstance(meta, dict):
                continue
            status = meta.get("status")
            if status not in (None, "valid"):
                continue
            media_type = meta.get("e")
            if media_type == "Image":
                source = meta.get("s", {})
                if isinstance(source, dict):
                    url = source.get("u")
                    if isinstance(url, str):
                        candidates.append(url)

    for raw_url in candidates:
        normalized = raw_url.replace("&amp;", "&")
        lowered = normalized.lower().split("?", 1)[0]
        if lowered.endswith(IMAGE_EXTENSIONS):
            return normalized

    return None


def collect_candidates(
    subreddit: str,
    window_start_utc: int,
    window_end_utc: int,
    oauth_token: str | None,
) -> list[dict[str, Any]]:
    posts = fetch_reddit_top_posts(
        subreddit=subreddit,
        oauth_token=oauth_token,
        period="week",
        limit=100,
    )
    candidates: list[dict[str, Any]] = []

    for post in posts:
        created_utc = int(post.get("created_utc", 0))
        if not (window_start_utc <= created_utc < window_end_utc):
            continue

        image_url = extract_image_url(post)
        if not image_url:
            continue

        permalink = post.get("permalink", "")
        full_permalink = f"https://reddit.com{permalink}" if permalink else ""
        candidates.append(
            {
                "kind": "reddit",
                "subreddit": subreddit,
                "title": str(post.get("title", "Untitled")),
                "score": int(post.get("score", 0)),
                "created_utc": created_utc,
                "image_url": image_url,
                "permalink": full_permalink,
            }
        )

    return candidates


def collect_candidates_pullpush(
    subreddit: str,
    window_start_utc: int,
    window_end_utc: int,
) -> list[dict[str, Any]]:
    posts = fetch_pullpush_posts_window(
        subreddit=subreddit,
        after_utc=window_start_utc,
        before_utc=window_end_utc,
        page_size=200,
        max_pages=10,
    )
    candidates: list[dict[str, Any]] = []

    for post in posts:
        created_utc = int(post.get("created_utc", 0))
        if not (window_start_utc <= created_utc < window_end_utc):
            continue

        image_url = extract_image_url(post)
        if not image_url:
            continue

        permalink = post.get("permalink", "")
        full_permalink = f"https://reddit.com{permalink}" if permalink else ""
        candidates.append(
            {
                "kind": "reddit",
                "subreddit": subreddit,
                "title": str(post.get("title", "Untitled")),
                "score": int(post.get("score", 0)),
                "created_utc": created_utc,
                "image_url": image_url,
                "permalink": full_permalink,
            }
        )

    return candidates


def choose_best_image(
    subreddits: list[str],
    window_start_utc: int,
    window_end_utc: int,
    oauth_token: str | None,
) -> dict[str, Any]:
    all_candidates: list[dict[str, Any]] = []
    errors: list[str] = []

    for subreddit in subreddits:
        source_errors: list[str] = []
        subreddit_candidates: list[dict[str, Any]] = []

        if oauth_token:
            try:
                subreddit_candidates = collect_candidates(
                    subreddit=subreddit,
                    window_start_utc=window_start_utc,
                    window_end_utc=window_end_utc,
                    oauth_token=oauth_token,
                )
            except Exception as error:  # noqa: BLE001
                source_errors.append(f"oauth: {error}")
        else:
            try:
                subreddit_candidates = collect_candidates(
                    subreddit=subreddit,
                    window_start_utc=window_start_utc,
                    window_end_utc=window_end_utc,
                    oauth_token=None,
                )
            except Exception as error:  # noqa: BLE001
                source_errors.append(f"public: {error}")

        if not subreddit_candidates:
            try:
                subreddit_candidates = collect_candidates_pullpush(
                    subreddit=subreddit,
                    window_start_utc=window_start_utc,
                    window_end_utc=window_end_utc,
                )
            except Exception as error:  # noqa: BLE001
                source_errors.append(f"pullpush: {error}")

        if subreddit_candidates:
            all_candidates.extend(subreddit_candidates)
        else:
            source_errors.append("No image posts found in the target window.")
            errors.append(f"r/{subreddit}: {'; '.join(source_errors)}")

    if not all_candidates:
        details = "; ".join(errors) if errors else "No image posts found in the target window."
        if not oauth_token and ("403" in details or "Blocked" in details):
            details += (
                " | Note: public Reddit is often blocked from CI; use Reddit OAuth secrets "
                "or set IMAGE_SOURCE=commons for web image search without Reddit."
            )
        raise NoImageForTargetDayError(details)

    return max(all_candidates, key=lambda item: (item["score"], item["created_utc"]))


def fetch_commons_file_pages(query: str, *, limit: int = 24, offset: int = 0) -> list[dict[str, Any]]:
    """Search Wikimedia Commons file namespace; returns page dicts with imageinfo."""
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "6",
            "gsrlimit": str(limit),
            "gsroffset": str(offset),
            "prop": "imageinfo",
            "iiprop": "url|mime|size|dimensions",
            "format": "json",
            "formatversion": "2",
        }
    )
    url = f"https://commons.wikimedia.org/w/api.php?{params}"
    payload = fetch_json(url)
    pages = payload.get("query", {}).get("pages", [])
    if not isinstance(pages, list):
        return []
    return [page for page in pages if isinstance(page, dict)]


def fetch_commons_potd(target_day_msk: dt.date) -> dict[str, Any] | None:
    params = urllib.parse.urlencode(
        {
            "action": "featuredfeed",
            "feed": "potd",
            "feedformat": "atom",
            "language": "en",
            "year": str(target_day_msk.year),
            "month": f"{target_day_msk.month:02d}",
            "day": f"{target_day_msk.day:02d}",
        }
    )
    url = f"https://commons.wikimedia.org/w/api.php?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        xml_text = response.read().decode("utf-8", "replace")

    root = ET.fromstring(xml_text)
    atom_ns = "{http://www.w3.org/2005/Atom}"
    media_ns = "{http://search.yahoo.com/mrss/}"

    entry = root.find(f"{atom_ns}entry")
    if entry is None:
        return None

    title_node = entry.find(f"{atom_ns}title")
    title = title_node.text.strip() if title_node is not None and title_node.text else "Picture of the day"

    page_link = ""
    for link in entry.findall(f"{atom_ns}link"):
        href = link.attrib.get("href", "")
        rel = link.attrib.get("rel", "")
        if href and rel in ("alternate", ""):
            page_link = href
            break

    content_node = entry.find(f"{media_ns}content")
    image_url = content_node.attrib.get("url", "") if content_node is not None else ""
    if not image_url.startswith("http"):
        return None

    lowered = image_url.lower().split("?", 1)[0]
    if not lowered.endswith(IMAGE_EXTENSIONS):
        return None

    return {
        "kind": "commons",
        "search_query": "Wikimedia Commons Picture of the Day",
        "title": title,
        "image_url": image_url,
        "permalink": page_link,
        "attribution": "Wikimedia Commons — проверьте лицензию на странице файла.",
        "license": "см. страницу файла на Commons",
        "score": 0,
        "created_utc": 0,
    }


def choose_best_commons_image(query: str, target_day_msk: dt.date) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for offset in (0, 24, 48):
        pages.extend(fetch_commons_file_pages(query, limit=24, offset=offset))
    candidates: list[dict[str, Any]] = []

    for page in pages:
        title = str(page.get("title", ""))
        if not title.startswith("File:"):
            continue
        imageinfo = page.get("imageinfo")
        if not isinstance(imageinfo, list) or not imageinfo:
            continue
        info = imageinfo[0]
        if not isinstance(info, dict):
            continue
        mime = info.get("mime", "")
        if not isinstance(mime, str) or not mime.startswith("image/"):
            continue
        if mime == "image/svg+xml":
            continue

        url = info.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        lowered = url.lower().split("?", 1)[0]
        if not lowered.endswith(IMAGE_EXTENSIONS):
            continue

        width = info.get("width")
        height = info.get("height")
        w = int(width) if isinstance(width, (int, float)) else 0
        h = int(height) if isinstance(height, (int, float)) else 0
        page_url = info.get("descriptionurl", "")
        page_url_str = str(page_url) if isinstance(page_url, str) else ""

        display_title = title.removeprefix("File:").replace("_", " ")

        candidates.append(
            {
                "kind": "commons",
                "search_query": query,
                "title": display_title,
                "image_url": url,
                "permalink": page_url_str,
                "attribution": "Wikimedia Commons — проверьте лицензию на странице файла.",
                "license": "см. страницу файла на Commons",
                "score": w * h,
                "created_utc": 0,
            }
        )

    if not candidates:
        raise NoImageForTargetDayError(f"commons: no image results for query {query!r}")
    # Rotate within top candidates by date so neighboring days are not identical.
    ranked = sorted(candidates, key=lambda item: (item["score"], len(item["title"])), reverse=True)
    pool_size = min(len(ranked), 7)
    index = target_day_msk.toordinal() % pool_size
    return ranked[index]


def choose_commons_image_for_day(target_day_msk: dt.date, query_variants: list[str]) -> dict[str, Any]:
    if not query_variants:
        query_variants = list(DEFAULT_COMMONS_QUERY_VARIANTS)

    collected: list[dict[str, Any]] = []
    for query in query_variants:
        try:
            collected.append(choose_best_commons_image(query, target_day_msk))
        except NoImageForTargetDayError:
            continue

    if not collected:
        raise NoImageForTargetDayError(
            "commons: no image results for provided queries. "
            "Try updating COMMONS_QUERY_VARIANTS or IMAGE_SEARCH_QUERY."
        )

    # Day-based rotation over available query results.
    index = target_day_msk.toordinal() % len(collected)
    return collected[index]


def telegram_api_request(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def build_caption(item: dict[str, Any], target_day_msk: dt.date) -> str:
    if item.get("kind") == "commons":
        lines = [
            f"Картинка дня за {target_day_msk.strftime('%d.%m.%Y')} (МСК)",
            f"Поиск (Wikimedia Commons): {item.get('search_query', '')}",
            "",
            item.get("title", "Untitled"),
        ]
        if item.get("license"):
            lines.append("")
            lines.append(f"Лицензия: {item['license']}")
        if item.get("attribution"):
            lines.append("")
            lines.append(item["attribution"])
        if item.get("permalink"):
            lines.extend(["", item["permalink"]])
        return "\n".join(lines)

    lines = [
        f"Лучшее изображение за {target_day_msk.strftime('%d.%m.%Y')} (МСК)",
        f"Сабреддит: r/{item['subreddit']}",
        f"Рейтинг: {item['score']}",
        "",
        item["title"],
    ]
    if item.get("permalink"):
        lines.extend(["", item["permalink"]])
    return "\n".join(lines)


def send_to_telegram(
    token: str,
    chat_id: str,
    item: dict[str, Any],
    target_day_msk: dt.date,
    dry_run: bool,
) -> None:
    caption = build_caption(item=item, target_day_msk=target_day_msk)
    if dry_run:
        print("DRY RUN: message prepared but not sent")
        print(caption)
        print(item["image_url"])
        return

    try:
        telegram_api_request(
            token=token,
            method="sendPhoto",
            payload={
                "chat_id": chat_id,
                "photo": item["image_url"],
                "caption": caption[:1024],
            },
        )
        return
    except Exception as error:  # noqa: BLE001
        fallback_text = f"{caption}\n\n{item['image_url']}"
        telegram_api_request(
            token=token,
            method="sendMessage",
            payload={"chat_id": chat_id, "text": fallback_text},
        )
        print(f"sendPhoto failed, fallback to sendMessage: {error}")


def no_results_user_hint(details: str, *, has_oauth: bool, source_mode: str) -> str:
    if source_mode == "commons":
        return (
            "На выбранную дату не удалось подобрать картинку из Wikimedia Commons. "
            "Попробуйте изменить переменную COMMONS_QUERY_VARIANTS."
        )
    if not has_oauth and ("403" in details or "Blocked" in details):
        return (
            "Публичный Reddit из GitHub Actions часто отвечает 403. "
            "Варианты: секреты Reddit OAuth, или режим IMAGE_SOURCE=commons (поиск на Wikimedia Commons)."
        )
    if "pullpush" in details.lower():
        return (
            "Архив PullPush за выбранный день не вернул постов с картинкой. "
            "Позже индекс может догнать дату, либо используйте IMAGE_SOURCE=commons."
        )
    return "За этот день среди выбранных сабреддитов не нашлось постов с прямой ссылкой на изображение."


def send_no_results_notice(
    token: str,
    chat_id: str,
    target_day_msk: dt.date,
    subreddits: list[str],
    details: str,
    *,
    has_oauth: bool,
    source_mode: str,
    dry_run: bool,
) -> None:
    subs = ", ".join(f"r/{name}" for name in subreddits)
    hint = no_results_user_hint(details, has_oauth=has_oauth, source_mode=source_mode)
    if source_mode == "commons":
        text = (
            f"За {target_day_msk.strftime('%d.%m.%Y')} (МСК) не удалось подобрать картинку.\n\n"
            f"{hint}"
        )
    else:
        text = (
            f"За {target_day_msk.strftime('%d.%m.%Y')} (МСК) не найдено подходящих изображений.\n"
            f"Сабреддиты: {subs}\n\n"
            f"{hint}"
        )
    if dry_run:
        print("DRY RUN: no-results notification prepared but not sent")
        print(text)
        return

    telegram_api_request(
        token=token,
        method="sendMessage",
        payload={"chat_id": chat_id, "text": text},
    )


def resolve_target_day(cli_value: str | None) -> dt.date:
    if not cli_value:
        return yesterday_moscow()
    return dt.datetime.strptime(cli_value, "%Y-%m-%d").date()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a daily image to Telegram (Reddit subreddits or Commons web search)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the message without sending it to Telegram.",
    )
    parser.add_argument(
        "--target-date",
        help="Label date in Moscow timezone (YYYY-MM-DD). Defaults to yesterday.",
    )
    args = parser.parse_args()

    target_day_msk = resolve_target_day(args.target_date)
    window_start_utc, window_end_utc = target_day_window_utc(target_day_msk)
    subreddits = parse_subreddits(os.getenv("SUBREDDITS"))
    source_mode = image_source_mode()

    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")

    oauth_token: str | None = None
    if source_mode == "reddit":
        try:
            oauth_token = init_reddit_oauth_token()
        except RedditOAuthError as error:
            print(f"Failed to initialize Reddit OAuth: {error}", file=sys.stderr)
            return 1

    if not args.dry_run and (not token or not chat_id):
        print(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "(or TG_BOT_TOKEN / TG_CHAT_ID).",
            file=sys.stderr,
        )
        return 2

    best_item: dict[str, Any] | None = None
    try:
        if source_mode == "commons":
            query_variants = parse_query_variants(os.getenv("COMMONS_QUERY_VARIANTS"))
            best_item = choose_commons_image_for_day(
                target_day_msk=target_day_msk,
                query_variants=query_variants,
            )
        else:
            best_item = choose_best_image(
                subreddits=subreddits,
                window_start_utc=window_start_utc,
                window_end_utc=window_end_utc,
                oauth_token=oauth_token,
            )
    except NoImageForTargetDayError as error:
        print("No image for this run — full diagnostic:", file=sys.stderr)
        print(str(error), file=sys.stderr)
        send_no_results_notice(
            token=token or "",
            chat_id=chat_id or "",
            target_day_msk=target_day_msk,
            subreddits=subreddits,
            details=str(error),
            has_oauth=oauth_token is not None,
            source_mode=source_mode,
            dry_run=args.dry_run,
        )
        print("Done: no suitable image found.")
        return 0
    except Exception as error:  # noqa: BLE001
        print("Failed to get an image.", file=sys.stderr)
        print(f"Details: {error}", file=sys.stderr)
        return 1

    if best_item is None:
        print("Internal error: no item selected.", file=sys.stderr)
        return 1

    send_to_telegram(
        token=token or "",
        chat_id=chat_id or "",
        item=best_item,
        target_day_msk=target_day_msk,
        dry_run=args.dry_run,
    )
    if best_item.get("kind") == "commons":
        print(f"Done: commons | {best_item['image_url']}")
    else:
        print(
            "Done:"
            f" r/{best_item['subreddit']} | score={best_item['score']} | {best_item['image_url']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
