#!/usr/bin/env python3
"""
Публикация поста из Obsidian в Telegram-канал @HoomGroup.

Использование:
    python publish.py "Дыра в 450 млрд"          # ищет по имени в Posts Telegram/
    python publish.py /полный/путь/файла.md       # точный путь
    python publish.py "имя" --dry                 # только показать HTML, не отправлять
    python publish.py "имя" --channel @другой     # в другой канал
"""

import os
import re
import sys
import html
import json
import requests
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(ENV_FILE)

CHANNEL_DEFAULT = "@HoomGroup"
POSTS_DIR = Path(
    "/Users/dzabrailkadiev/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault /💡 Контент/02 Контент/Посты Telegram"
)
TG_MAX_LEN = 4096


def strip_frontmatter(md: str) -> str:
    if md.startswith("---\n"):
        end = md.find("\n---\n", 4)
        if end != -1:
            return md[end + 5:].lstrip()
    return md.lstrip()


def strip_metadata_footer(text: str) -> str:
    parts = text.rsplit("\n---\n", 1)
    markers = ("📌", "📅", "📥", "🔗", "📝")
    if len(parts) == 2 and any(m in parts[1] for m in markers):
        return parts[0].rstrip()
    return text.rstrip()


def strip_h1_title(text: str) -> str:
    lines = text.split("\n", 1)
    if lines and lines[0].startswith("# "):
        return lines[1].lstrip() if len(lines) > 1 else ""
    return text


PLACEHOLDER_RE = re.compile(r"(\d+)")


def convert_inline(text: str) -> str:
    stash: List[str] = []

    def keep(tag: str) -> str:
        idx = len(stash)
        stash.append(tag)
        return f"{idx}"

    def link_sub(m):
        url = html.escape(m.group(2), quote=True)
        txt = html.escape(m.group(1))
        return keep(f'<a href="{url}">{txt}</a>')

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_sub, text)

    def bold_sub(m):
        return keep(f"<b>{html.escape(m.group(1))}</b>")

    text = re.sub(r"\*\*([^*]+?)\*\*", bold_sub, text)

    def italic_sub(m):
        return keep(f"<i>{html.escape(m.group(1))}</i>")

    text = re.sub(r"__([^_]+?)__", italic_sub, text)
    text = re.sub(r"\*([^*]+?)\*", italic_sub, text)

    text = html.escape(text)

    def restore(m):
        return stash[int(m.group(1))]

    return PLACEHOLDER_RE.sub(restore, text)


def md_to_html(md: str) -> str:
    out: List[str] = []
    bq: List[str] = []

    def flush_bq():
        if bq:
            inner = "\n".join(convert_inline(l) for l in bq)
            out.append(f"<blockquote>{inner}</blockquote>")
            bq.clear()

    for line in md.split("\n"):
        if line.startswith("> "):
            bq.append(line[2:])
            continue
        if line.strip() == ">":
            bq.append("")
            continue
        flush_bq()
        out.append(convert_inline(line))
    flush_bq()

    return "\n".join(out)


def extract_title_from_stem(stem: str) -> str:
    """Strip leading time slot 'HH-MM — ' or 'HH-MM ' from filename."""
    cleaned = re.sub(r"^\d{2}-\d{2}\s*[—–-]+\s*", "", stem).strip()
    return cleaned if cleaned else stem


def find_post(arg: str) -> Optional[Path]:
    p = Path(arg)
    if p.exists() and p.is_file():
        return p

    candidates = list(POSTS_DIR.rglob("*.md"))
    needle = arg.lower().replace(".md", "")

    for f in candidates:
        if f.stem.lower() == needle:
            return f
    for f in candidates:
        if needle in f.stem.lower():
            return f
    return None


def send(html_text: str, channel: str, token: str) -> dict:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": channel,
            "text": html_text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
        timeout=30,
    )
    return r.json()


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    name = args[0]
    dry = "--dry" in args
    channel = CHANNEL_DEFAULT
    if "--channel" in args:
        i = args.index("--channel")
        if i + 1 < len(args):
            channel = args[i + 1]

    f = find_post(name)
    if not f:
        print(f"❌ Не нашёл файл: {name}")
        print(f"   Искал в: {POSTS_DIR}")
        sys.exit(1)

    print(f"📄 {f.name}")
    raw = f.read_text(encoding="utf-8")
    body = strip_h1_title(strip_metadata_footer(strip_frontmatter(raw)))
    title = extract_title_from_stem(f.stem)
    html_out = f"<b>{html.escape(title)}</b>\n\n" + md_to_html(body)

    print(f"📏 {len(html_out)} символов / лимит {TG_MAX_LEN}")
    if len(html_out) > TG_MAX_LEN:
        print(f"⚠️  Превышен лимит Telegram на {len(html_out) - TG_MAX_LEN} символов — будет ошибка")

    if dry:
        print("─" * 60)
        print(html_out)
        print("─" * 60)
        print("Dry-run, не отправил.")
        return

    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("❌ TELEGRAM_TOKEN не найден (нет в .env?)")
        sys.exit(1)

    print(f"📤 Отправляю в {channel}…")
    result = send(html_out, channel, token)

    if result.get("ok"):
        msg = result["result"]
        print(f"✅ Опубликовано. message_id = {msg['message_id']}")
        chat = msg.get("chat", {})
        if chat.get("username"):
            print(f"🔗 https://t.me/{chat['username']}/{msg['message_id']}")
    else:
        print(f"❌ Telegram вернул ошибку:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
