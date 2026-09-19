#!/usr/bin/env python
# coding: utf-8
"""
aliexpress_service.py
=======================
ONE process that does everything:

  A) Interactive bot — replies to whoever DMs it an AliExpress link
     with a full offer card (title, price, rating, orders, your
     affiliate links).

  B) Channel monitor — watches the channels in SOURCE_CHANNELS (which
     you don't administer) and, for every new post, reposts it into
     TARGET_CHANNEL **unchanged** — same wording, same image — except
     every AliExpress link in it is swapped for your affiliate link.
     If a post has no AliExpress link, it's ignored.

This replaces the previous separate `aliexpress_bot.py` +
`channel_monitor.py`. You can delete those two files.

HOW THE TWO HALVES SHARE ONE PROCESS
--------------------------------------
Part (A) uses pyTelegramBotAPI, which polls Telegram in a blocking loop.
Part (B) uses Telethon, which is asyncio-based and needs your own
Telegram account login (see channel monitor docs in README.md for why).
This script runs (A)'s polling loop in a background thread and (B)'s
Telethon client on the main asyncio event loop, and both talk to the
same TeleBot instance for sending — the bot handles DMs *and* posts to
your channel.

CATCHING UP AFTER DOWNTIME ("don't skip past links")
-------------------------------------------------------
Each source channel's last-seen message id is saved to STATE_FILE. Every
time this script starts, it fetches anything posted in each source
channel since that id (so messages that arrived while the bot was off
still get processed) before switching to live monitoring. Nothing is
skipped just because the same product was posted before — the dedup key
here is the *message*, not the *product*, so re-running the same script
twice never reprocesses a message it already handled, but a channel
posting the same product again later is reposted again, faithfully.

On the very first run for a brand-new channel (no state yet) it does
NOT walk that channel's entire history by default — see
BACKFILL_ON_FIRST_RUN below if you want that.

See .env.example for every setting.
"""

import asyncio
import io
import json
import logging
import os
import threading
from pathlib import Path

import telebot
from telebot import types

from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest

from aliexpress_common import (
    log,
    extract_first_link,
    is_aliexpress_link,
    is_shopcart_link,
    build_shopcart_link,
    get_main_affiliate_link,
    get_special_offer_links,
    get_product_info,
    build_product_caption,
    find_aliexpress_links_in_message,
    rewrite_message_links,
    prepend_text_with_entities,
    aliexpress,
    ProductsNotFoudException,
    InvalidArgumentException,
    InvalidTrackingIdException,
    ApiRequestException,
    ApiRequestResponseException,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _require(name: str, hint: str = "") -> str:
    value = os.environ.get(name)
    if not value:
        extra = f"\n  {hint}" if hint else ""
        raise SystemExit(
            f"Missing required setting: {name}\n"
            f"Set it in your .env file (see .env.example) or as an environment variable.{extra}"
        )
    return value


BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN", "The bot's token from @BotFather.")

API_ID_RAW = _require(
    "TELEGRAM_API_ID",
    "Get this from https://my.telegram.org (API development tools) — it is NOT your "
    "AliExpress APP_KEY, it's a separate number for your Telegram account.",
)
try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise SystemExit(f"TELEGRAM_API_ID should be a number, got: {API_ID_RAW!r}")

API_HASH = _require("TELEGRAM_API_HASH", "Also from https://my.telegram.org.")
PHONE = os.environ.get("TELEGRAM_PHONE")  # only needed for first login
SESSION_NAME = os.environ.get("TELETHON_SESSION", "monitor")

def _parse_channel_list(value: str) -> list:
    return [c.strip() for c in value.split(",") if c.strip()]


# Source channels grouped by region, each tagged with a flag that gets
# added to the top of every repost from that region. Add a new region by
# adding another block here + its env vars in .env.example — no other
# code needs to change.
REGIONS = []
_dz_channels = _parse_channel_list(os.environ.get("SOURCE_CHANNELS_DZ", ""))
if _dz_channels:
    REGIONS.append({"code": "dz", "flag": os.environ.get("DZ_TAG", "🇩🇿"), "channels": _dz_channels})
_fr_channels = _parse_channel_list(os.environ.get("SOURCE_CHANNELS_FR", ""))
if _fr_channels:
    REGIONS.append({"code": "fr", "flag": os.environ.get("FR_TAG", "🇫🇷"), "channels": _fr_channels})

if not REGIONS:
    raise SystemExit(
        "Set at least one of SOURCE_CHANNELS_DZ / SOURCE_CHANNELS_FR to a "
        'comma-separated list of channels, e.g. SOURCE_CHANNELS_DZ="@channel1,@channel2"'
    )

SOURCE_CHANNELS = [c for region in REGIONS for c in region["channels"]]
TARGET_CHANNEL = _require("TARGET_CHANNEL", 'Your own channel, e.g. "@my_channel".')

# First-run behaviour for a channel with no saved state yet.
BACKFILL_ON_FIRST_RUN = os.environ.get("BACKFILL_ON_FIRST_RUN", "0") == "1"
BACKFILL_LIMIT = int(os.environ.get("BACKFILL_LIMIT", "50"))
# Safety cap on catch-up after downtime, so a very long time offline
# doesn't suddenly flood your channel.
MAX_CATCHUP = int(os.environ.get("MAX_CATCHUP", "200"))

POST_DELAY_SECONDS = float(os.environ.get("POST_DELAY_SECONDS", "2"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "channel_state.json"))

KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "0") == "1"

CHANNEL_URL = "https://t.me/AliXPromotion"
DEMO_VIDEO_URL = "https://t.me/AliXPromotion/8"
APP_DOWNLOAD_URL = "https://a.aliexpress.com/_mtV0j3q"
CART_HELP_IMAGE = "https://i.postimg.cc/HkMxWS1T/photo-5893070682508606111-y.jpg"
GAMES_IMAGE = "https://i.postimg.cc/zvDbVTS0/photo-5893070682508606110-x.jpg"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# =============================================================================
# PART A — interactive bot (DMs)
# =============================================================================

def build_start_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("⭐️ألعاب لجمع العملات المعدنية⭐️", callback_data="games"),
        types.InlineKeyboardButton("⭐️تخفيض العملات على منتجات السلة 🛒⭐️", callback_data="click"),
        types.InlineKeyboardButton("❤️ اشترك في القناة للمزيد من العروض ❤️", url=CHANNEL_URL),
        types.InlineKeyboardButton("🎬 شاهد كيفية عمل البوت 🎬", url=DEMO_VIDEO_URL),
        types.InlineKeyboardButton(
            "💰 حمل تطبيق Aliexpress عبر الضغط هنا للحصول على مكافأة 5 دولار 💰",
            url=APP_DOWNLOAD_URL,
        ),
    )
    return kb


def build_main_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("⭐️ألعاب لجمع العملات المعدنية⭐️", callback_data="games"),
        types.InlineKeyboardButton("⭐️تخفيض العملات على منتجات السلة 🛒⭐️", callback_data="click"),
        types.InlineKeyboardButton("❤️ اشترك في القناة للمزيد من العروض ❤️", url=CHANNEL_URL),
    )
    return kb


def build_games_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(" ⭐️ صفحة مراجعة وجمع النقاط يوميا ⭐️", url="https://s.click.aliexpress.com/e/_on0MwkF"),
        types.InlineKeyboardButton("⭐️ لعبة Merge boss ⭐️", url="https://s.click.aliexpress.com/e/_DlCyg5Z"),
        types.InlineKeyboardButton("⭐️ لعبة Fantastic Farm ⭐️", url="https://s.click.aliexpress.com/e/_DBBkt9V"),
        types.InlineKeyboardButton("⭐️ لعبة قلب الاوراق Flip ⭐️", url="https://s.click.aliexpress.com/e/_DdcXZ2r"),
        types.InlineKeyboardButton("⭐️ لعبة GoGo Match ⭐️", url="https://s.click.aliexpress.com/e/_DDs7W5D"),
    )
    return kb


MAIN_KEYBOARD = build_main_keyboard()
START_KEYBOARD = build_start_keyboard()
GAMES_KEYBOARD = build_games_keyboard()


@bot.message_handler(commands=["start"])
def welcome_user(message):
    bot.send_message(
        message.chat.id,
        "مرحبا بك، ارسل لنا رابط المنتج الذي تريد شرائه لنوفر لك افضل سعر له 👌 \n",
        reply_markup=START_KEYBOARD,
    )


@bot.callback_query_handler(func=lambda call: call.data == "click")
def button_click(callback_query):
    bot.answer_callback_query(callback_query.id)
    try:
        bot.edit_message_text(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            text="...",
        )
    except Exception:
        pass

    text = (
        "✅1-ادخل الى السلة من هنا:\n"
        " https://s.click.aliexpress.com/e/_opGCtMf \n"
        "✅2-قم باختيار المنتجات التي تريد تخفيض سعرها\n"
        "✅3-اضغط على زر دفع ليحولك لصفحة التأكيد \n"
        "✅4-اضغط على الايقونة في الاعلى وانسخ الرابط هنا في البوت لتتحصل على رابط التخفيض"
    )
    bot.send_photo(
        callback_query.message.chat.id,
        CART_HELP_IMAGE,
        caption=text,
        reply_markup=MAIN_KEYBOARD,
    )


@bot.callback_query_handler(func=lambda call: call.data == "games")
def button_games(callback_query):
    bot.answer_callback_query(callback_query.id)
    bot.send_photo(
        callback_query.message.chat.id,
        GAMES_IMAGE,
        caption=(
            "روابط ألعاب جمع العملات المعدنية لإستعمالها في خفض السعر لبعض المنتجات، "
            "قم بالدخول يوميا لها للحصول على أكبر عدد ممكن في اليوم 👇"
        ),
        reply_markup=GAMES_KEYBOARD,
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_unknown_callback(call):
    bot.answer_callback_query(call.id)
    log.info("Unhandled callback_data received: %r", call.data)


def handle_shopcart_link(message, link: str):
    try:
        shopcart_link = build_shopcart_link(link)
        affiliate_link = aliexpress.get_affiliate_links(shopcart_link)[0].promotion_link
        bot.send_photo(
            message.chat.id,
            CART_HELP_IMAGE,
            caption=f"هذا رابط تخفيض السلة \n{affiliate_link}",
        )
    except (ProductsNotFoudException, ApiRequestResponseException, ApiRequestException) as exc:
        log.warning("Shopcart affiliate link failed: %s", exc)
        bot.send_message(message.chat.id, "حدث خطأ 🤷🏻‍♂️ تأكد من رابط السلة وأعد المحاولة.")
    except Exception:
        log.exception("Unexpected error building shopcart link")
        bot.send_message(message.chat.id, "حدث خطأ 🤷🏻‍♂️")


def handle_product_link(message, link: str, wait_message_id: int):
    try:
        main_link = get_main_affiliate_link(link)
    except InvalidTrackingIdException as exc:
        log.error("Tracking id misconfigured: %s", exc)
        _finish_with_error(message, wait_message_id, "خطأ في إعدادات البوت (Tracking ID). أبلغ المسؤول.")
        return
    except (ProductsNotFoudException, InvalidArgumentException) as exc:
        log.warning("No affiliate link for %s: %s", link, exc)
        _finish_with_error(message, wait_message_id, "لم نتمكن من العثور على هذا المنتج. تأكد من صحة الرابط.")
        return
    except ApiRequestResponseException as exc:
        log.error("AliExpress API rejected the request for %s: %s", link, exc)
        _finish_with_error(message, wait_message_id, "حدث خطأ من طرف Aliexpress 🤷🏻‍♂️ حاول لاحقا.")
        return
    except Exception:
        log.exception("Unexpected error getting affiliate link for %s", link)
        _finish_with_error(message, wait_message_id, "حدث خطأ 🤷🏻‍♂️")
        return

    product = get_product_info(link)
    special_links = get_special_offer_links(link)
    caption = build_product_caption(product, main_link, special_links)

    image_url = getattr(product, "product_main_image_url", None) if product else None

    try:
        bot.delete_message(message.chat.id, wait_message_id)
    except Exception:
        pass

    if image_url and len(caption) <= 1024:
        bot.send_photo(message.chat.id, image_url, caption=caption, reply_markup=MAIN_KEYBOARD)
    else:
        bot.send_message(message.chat.id, caption, reply_markup=MAIN_KEYBOARD)


def _finish_with_error(message, wait_message_id: int, text: str):
    try:
        bot.edit_message_text(chat_id=message.chat.id, message_id=wait_message_id, text=text)
    except Exception:
        bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda message: True, content_types=["text"])
def get_link(message):
    link = extract_first_link(message.text)

    if not link or not is_aliexpress_link(link):
        bot.send_message(
            message.chat.id,
            "الرابط غير صحيح ! تأكد من رابط المنتج أو اعد المحاولة.\n"
            " قم بإرسال <b>الرابط فقط</b> بدون عنوان المنتج",
            parse_mode="HTML",
        )
        return

    sent_message = bot.send_message(message.chat.id, "المرجو الانتظار قليلا، يتم تجهيز العروض ⏳")

    if is_shopcart_link(message.text) or is_shopcart_link(link):
        try:
            bot.delete_message(message.chat.id, sent_message.message_id)
        except Exception:
            pass
        handle_shopcart_link(message, link)
        return

    handle_product_link(message, link, sent_message.message_id)


def run_interactive_bot():
    log.info("Interactive bot starting (polling)...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)

# =============================================================================
# PART B — channel monitor (verbatim repost with swapped links)
# =============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Could not read %s, starting with empty state.", STATE_FILE)
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.exception("Could not save %s", STATE_FILE)


channel_state: dict = load_state()


async def repost_message(message, source_title: str, flag: str = None):
    """Rebuild `message` with its AliExpress links swapped for affiliate
    links, keeping every other word, and every image, exactly as posted —
    plus a region flag prepended on its own line if one applies."""
    links = find_aliexpress_links_in_message(message)
    if not links:
        return

    loop = asyncio.get_event_loop()
    link_map = {}
    for link in links:
        try:
            affiliate_link = await loop.run_in_executor(None, get_main_affiliate_link, link)
            link_map[link] = affiliate_link
        except InvalidTrackingIdException as exc:
            log.error("Tracking id misconfigured: %s", exc)
            return
        except (ProductsNotFoudException, InvalidArgumentException) as exc:
            log.info("Skipping unresolvable link %s: %s", link, exc)
        except ApiRequestResponseException as exc:
            log.error("AliExpress API rejected %s: %s", link, exc)
        except Exception:
            log.exception("Unexpected error getting affiliate link for %s", link)

    if not link_map:
        log.info("None of the AliExpress links in this message could be converted; skipping repost.")
        return

    new_text, new_entities = rewrite_message_links(message.message or "", message.entities, link_map)
    if flag:
        new_text, new_entities = prepend_text_with_entities(f"{flag}\n", new_text, new_entities)

    photo_bytes = None
    if message.photo:
        try:
            photo_bytes = await client.download_media(message, file=bytes)
        except Exception:
            log.exception("Could not download the original photo; will post text only.")

    def _publish():
        if photo_bytes and len(new_text) <= 1024:
            bot.send_photo(
                TARGET_CHANNEL,
                io.BytesIO(photo_bytes),
                caption=new_text,
                caption_entities=new_entities or None,
            )
        elif photo_bytes:
            # Caption too long for a photo caption (Telegram's 1024-char cap) —
            # send the photo, then the full text as its own message so nothing
            # gets silently truncated.
            bot.send_photo(TARGET_CHANNEL, io.BytesIO(photo_bytes))
            bot.send_message(TARGET_CHANNEL, new_text, entities=new_entities or None)
        else:
            bot.send_message(TARGET_CHANNEL, new_text, entities=new_entities or None)

    try:
        await loop.run_in_executor(None, _publish)
    except Exception:
        log.exception("Failed to publish to %s", TARGET_CHANNEL)
        return

    log.info("Reposted message %s from %s -> %s", message.id, source_title, TARGET_CHANNEL)


def _state_key(chat) -> str:
    return str(getattr(chat, "id", chat))


# Populated at startup (numeric chat id -> flag emoji) so the live handler
# can tag a message without re-resolving the channel on every event.
channel_flag_map: dict = {}


@client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def on_new_message(event):
    chat = await event.get_chat()
    source_title = getattr(chat, "title", None) or getattr(chat, "username", "")
    flag = channel_flag_map.get(chat.id)
    await repost_message(event.message, source_title, flag=flag)
    channel_state[_state_key(chat)] = event.message.id
    save_state(channel_state)


async def catch_up_channel(channel, flag: str = None):
    entity = await client.get_entity(channel)
    key = _state_key(entity)
    source_title = getattr(entity, "title", None) or getattr(entity, "username", "")

    last_id = channel_state.get(key)

    if last_id is None:
        # Brand-new channel to us.
        if not BACKFILL_ON_FIRST_RUN:
            newest = await client.get_messages(entity, limit=1)
            channel_state[key] = newest[0].id if newest else 0
            save_state(channel_state)
            log.info("First time seeing %s — starting fresh from now (no backfill).", source_title)
            return
        log.info("First time seeing %s — backfilling last %d messages.", source_title, BACKFILL_LIMIT)
        messages = await client.get_messages(entity, limit=BACKFILL_LIMIT)
        messages = list(reversed(messages))  # oldest first
    else:
        messages = await client.get_messages(entity, min_id=last_id, limit=MAX_CATCHUP + 1)
        messages = list(reversed(messages))  # oldest first
        if len(messages) > MAX_CATCHUP:
            log.warning(
                "%s has more than %d missed messages; only processing the most recent %d.",
                source_title, MAX_CATCHUP, MAX_CATCHUP,
            )
            messages = messages[-MAX_CATCHUP:]

    for msg in messages:
        await repost_message(msg, source_title, flag=flag)
        channel_state[key] = msg.id
        save_state(channel_state)
        await asyncio.sleep(POST_DELAY_SECONDS)

    if messages:
        log.info("Caught up %s (%d message(s) processed).", source_title, len(messages))


async def ensure_joined_and_caught_up():
    for region in REGIONS:
        for channel in region["channels"]:
            try:
                await client(JoinChannelRequest(channel))
            except Exception as exc:
                log.info(
                    "Could not auto-join %s (%s) — if it's private, make sure you've joined it manually.",
                    channel, exc,
                )
            await asyncio.sleep(1)  # be gentle with join requests

            try:
                entity = await client.get_entity(channel)
                channel_flag_map[entity.id] = region["flag"]
            except Exception:
                log.exception("Could not resolve %s to tag it with %s", channel, region["flag"])

            try:
                await catch_up_channel(channel, flag=region["flag"])
            except Exception:
                log.exception("Catch-up failed for %s", channel)


async def run_channel_monitor():
    await client.start(phone=PHONE)
    log.info("Logged in. Catching up on source channels...")
    await ensure_joined_and_caught_up()
    log.info("Monitoring channels: %s -> posting to %s", SOURCE_CHANNELS, TARGET_CHANNEL)
    await client.run_until_disconnected()

# =============================================================================
# Optional keep-alive web server (Replit-style hosting only)
# =============================================================================

def _start_keep_alive():
    try:
        from flask import Flask
    except ImportError:
        log.warning("KEEP_ALIVE is enabled but Flask is not installed; skipping.")
        return

    app = Flask("keep_alive")

    @app.route("/")
    def home():
        return "Service is running."

    def run():
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

    threading.Thread(target=run, daemon=True).start()
    log.info("Keep-alive web server started.")

# =============================================================================
# Entry point — run both parts in one process
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if KEEP_ALIVE:
        _start_keep_alive()

    threading.Thread(target=run_interactive_bot, daemon=True).start()

    asyncio.run(run_channel_monitor())
