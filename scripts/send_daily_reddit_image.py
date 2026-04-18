#!/usr/bin/env python3
"""Send the best Reddit image from yesterday to Telegram."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_SUBREDDITS = ("VaporwaveAesthetics",)
REDDIT_USER_AGENT = "pic-of-the-day/1.0"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class NoImageForTargetDayError(RuntimeError):
    """Raised when there are no image posts for target day."""


class RedditOAuthError(RuntimeError):
    """Raised when Reddit OAuth is required but cannot be initialized."""


def parse_subreddits(raw_value: str | None) -> list[str]:
    if not raw_value:
        return list(DEFAULT_SUBREDDITS)

    names = [item.strip() for item in raw_value.split(",")]
    return [name for name in names if name]


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
    request_headers = {"User-Agent": REDDIT_USER_AGENT}
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
                if "403" in str(error) or "Blocked" in str(error):
                    source_errors.append(
                        "hint: public Reddit is blocked; set REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET "
                        "or rely on PullPush indexing (may lag)."
                    )

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
        raise NoImageForTargetDayError(details)

    return max(all_candidates, key=lambda item: (item["score"], item["created_utc"]))


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
    lines = [
        f"Лучшее изображение за {target_day_msk.strftime('%d.%m.%Y')} (МСК)",
        f"Сабреддит: r/{item['subreddit']}",
        f"Рейтинг: {item['score']}",
        "",
        item["title"],
    ]
    if item["permalink"]:
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


def send_no_results_notice(
    token: str,
    chat_id: str,
    target_day_msk: dt.date,
    subreddits: list[str],
    details: str,
    dry_run: bool,
) -> None:
    text = (
        f"За {target_day_msk.strftime('%d.%m.%Y')} (МСК) не найдено подходящих изображений.\n"
        f"Сабреддиты: {', '.join(f'r/{name}' for name in subreddits)}\n\n"
        f"Диагностика: {details}"
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
        description="Send yesterday's top Reddit image to Telegram."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the message without sending it to Telegram.",
    )
    parser.add_argument(
        "--target-date",
        help="Target day in Moscow timezone (YYYY-MM-DD). Defaults to yesterday.",
    )
    args = parser.parse_args()

    target_day_msk = resolve_target_day(args.target_date)
    window_start_utc, window_end_utc = target_day_window_utc(target_day_msk)
    subreddits = parse_subreddits(os.getenv("SUBREDDITS"))

    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID")
    oauth_token: str | None = None
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

    try:
        best_item = choose_best_image(
            subreddits=subreddits,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            oauth_token=oauth_token,
        )
    except NoImageForTargetDayError as error:
        send_no_results_notice(
            token=token or "",
            chat_id=chat_id or "",
            target_day_msk=target_day_msk,
            subreddits=subreddits,
            details=str(error),
            dry_run=args.dry_run,
        )
        print("Done: no image posts found for target day.")
        return 0
    except Exception as error:  # noqa: BLE001
        print(
            "Failed to get best image from Reddit and fallback sources.",
            file=sys.stderr,
        )
        print(f"Details: {error}", file=sys.stderr)
        return 1

    send_to_telegram(
        token=token or "",
        chat_id=chat_id or "",
        item=best_item,
        target_day_msk=target_day_msk,
        dry_run=args.dry_run,
    )
    print(
        "Done:"
        f" r/{best_item['subreddit']} | score={best_item['score']} | {best_item['image_url']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
