"""
aliexpress_common.py
=====================
Shared code between `aliexpress_bot.py` (the interactive bot that replies
to whoever sends it a product link) and `channel_monitor.py` (the userbot
that watches other channels and reposts AliExpress deals into your own
channel with your affiliate links).

Keeping this in one place means both scripts always build links / format
captions exactly the same way.
"""

import logging
import os
import re
from urllib.parse import urlencode, quote

import requests

try:
    from dotenv import load_dotenv
    # Loads a .env file placed next to the scripts, if present, so you don't
    # have to fiddle with `set`/`$env:` on Windows every session. Real
    # environment variables (if you did set any) still take priority.
    load_dotenv()
except ImportError:
    pass

from aliexpress_api import AliexpressApi, models
from aliexpress_api.errors import (
    ProductsNotFoudException,
    InvalidArgumentException,
    InvalidTrackingIdException,
    ApiRequestException,
    ApiRequestResponseException,
)

# Telethon is a hard dependency of this project (used by the channel monitor
# half of the merged service) — importing its entity types here too keeps
# all the "rewrite a channel post" logic in one place.
from telethon.extensions.markdown import add_surrogate, del_surrogate
from telethon.tl import types as tl_types

try:
    from aliexpress_api.tools.get_product_id import get_product_id as _get_product_id
except ImportError:  # keep working even if the library reorganizes this internal path
    _get_product_id = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("aliexpress_common")

# ---------------------------------------------------------------------------
# Configuration (shared across both scripts)
# ---------------------------------------------------------------------------

APP_KEY = os.environ.get("ALIEXPRESS_APP_KEY", "502336")
APP_SECRET = os.environ.get("ALIEXPRESS_APP_SECRET", "qW3MlLGKtt7jnZOg8KkHpfCbTaac2LOq")
TRACKING_ID = os.environ.get("ALIEXPRESS_TRACKING_ID", "default")

aliexpress = AliexpressApi(
    APP_KEY,
    APP_SECRET,
    models.Language.EN,
    models.Currency.EUR,
    TRACKING_ID,
)

# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

LINK_PATTERN = re.compile(r"https?://\S+|www\.\S+")
ALIEXPRESS_HOST_HINT = "aliexpress."


def extract_first_link(text: str):
    if not text:
        return None
    match = LINK_PATTERN.search(text)
    return match.group(0) if match else None


def extract_all_links(text: str) -> list:
    if not text:
        return []
    return [m.rstrip(").,،!؟") for m in LINK_PATTERN.findall(text)]


def is_aliexpress_link(link: str) -> bool:
    return bool(link) and ALIEXPRESS_HOST_HINT in link.lower()


def is_shopcart_link(link: str) -> bool:
    return "shoppingcart" in link.lower() or "availableproductshopcartids" in link.lower()


_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def resolve_redirect(link: str, timeout: int = 8) -> str:
    """Follow redirects on short/share links (s.click.aliexpress.com,
    a.aliexpress.com, star.aliexpress.com share pages, …) to reach the
    canonical product page URL that actually contains the numeric
    product ID. Falls back to the original link if resolution fails for
    any reason — callers should already be prepared to handle a link
    that still can't be resolved to product details."""
    try:
        resp = requests.head(link, headers=_HTTP_HEADERS, allow_redirects=True, timeout=timeout)
        if resp.url and resp.url != link:
            return resp.url
    except Exception:
        pass  # some servers reject HEAD; fall through to GET

    try:
        resp = requests.get(link, headers=_HTTP_HEADERS, allow_redirects=True, timeout=timeout, stream=True)
        resp.close()
        if resp.url:
            return resp.url
    except Exception as exc:
        log.info("Could not resolve redirect for %s: %s", link, exc)

    return link


def normalize_product_key(link: str) -> str:
    """Best-effort stable key for deduplication. Falls back to the raw
    link when the numeric product ID can't be parsed out (e.g. short
    s.click.aliexpress.com / a.aliexpress.com redirect links)."""
    if _get_product_id:
        try:
            return _get_product_id(link)
        except Exception:
            pass
    return link.strip().lower()


# ---------------------------------------------------------------------------
# Verbatim message rewriting (used by the channel monitor to repost a
# source message unchanged except for swapping AliExpress links)
# ---------------------------------------------------------------------------

def find_aliexpress_links_in_message(message) -> list:
    """All distinct AliExpress links in a Telethon message: ones written
    out as plain text, and ones hidden behind a different clickable
    text (MessageEntityTextUrl)."""
    links = set(extract_all_links(message.message or ""))
    for ent in (message.entities or []):
        if isinstance(ent, tl_types.MessageEntityTextUrl):
            links.add(ent.url)
    return [l for l in links if is_aliexpress_link(l)]


_FORMAT_ENTITY_MAP = {
    tl_types.MessageEntityBold: "bold",
    tl_types.MessageEntityItalic: "italic",
    tl_types.MessageEntityUnderline: "underline",
    tl_types.MessageEntityStrike: "strikethrough",
    tl_types.MessageEntitySpoiler: "spoiler",
    tl_types.MessageEntityCode: "code",
    tl_types.MessageEntityBlockquote: "blockquote",
}


def rewrite_message_links(raw_text: str, telethon_entities, link_map: dict):
    """Reproduce `raw_text` (+ its formatting) with every AliExpress link in
    `link_map` swapped for its affiliate link — nothing else about the
    wording or formatting changes. Hidden links (button-style text) keep
    their original visible words and only get a new destination; a bare
    URL written out in the text gets replaced in place.

    Works in "UTF-16 surrogate space" throughout (matching how Telegram's
    entity offsets are defined) so formatting stays correctly aligned even
    around emoji.

    Returns (new_text: str, new_entities: list[telebot.types.MessageEntity]).
    """
    text = add_surrogate(raw_text or "")
    entities = list(telethon_entities or [])
    out_entities = []  # working dicts: offset, length, type, url?, language?

    for ent in entities:
        if isinstance(ent, tl_types.MessageEntityTextUrl):
            new_url = link_map.get(ent.url, ent.url)
            out_entities.append({"offset": ent.offset, "length": ent.length, "type": "text_link", "url": new_url})
            continue
        if isinstance(ent, tl_types.MessageEntityPre):
            entry = {"offset": ent.offset, "length": ent.length, "type": "pre"}
            if getattr(ent, "language", None):
                entry["language"] = ent.language
            out_entities.append(entry)
            continue
        mapped = _FORMAT_ENTITY_MAP.get(type(ent))
        if mapped:
            out_entities.append({"offset": ent.offset, "length": ent.length, "type": mapped})
        # Anything else (mentions, hashtags, bot commands, plain-url auto
        # entities...) is intentionally dropped — Telegram still renders
        # hashtags/mentions/emails as tappable on delivery even with no
        # explicit entity for them.

    # Plain-text occurrences of a link (not hidden behind different words)
    # need the text itself spliced, which shifts everything after it.
    matches = []
    for original_link in link_map:
        start = 0
        while True:
            idx = text.find(original_link, start)
            if idx == -1:
                break
            matches.append((idx, idx + len(original_link), original_link))
            start = idx + len(original_link)
    matches.sort(key=lambda m: m[0], reverse=True)  # right-to-left keeps earlier offsets valid

    for start, end, original_link in matches:
        already_covered = any(
            e["type"] == "text_link" and e["offset"] == start and e["offset"] + e["length"] == end
            for e in out_entities
        )
        if already_covered:
            continue  # this exact span was already handled as a hidden-link entity above

        affiliate = link_map[original_link]
        delta = len(affiliate) - (end - start)

        for e in out_entities:
            if e["offset"] >= end:
                e["offset"] += delta
            elif e["offset"] + e["length"] > start:
                # An unrelated entity overlaps the URL we're replacing (rare) —
                # clip it rather than risk sending a malformed entity.
                e["length"] = max(0, start - e["offset"])

        text = text[:start] + affiliate + text[end:]
        out_entities.append({"offset": start, "length": len(affiliate), "type": "text_link", "url": affiliate})

    out_entities = [e for e in out_entities if e["length"] > 0]
    out_entities.sort(key=lambda e: e["offset"])

    final_text = del_surrogate(text)

    from telebot import types as tb_types

    tb_entities = []
    for e in out_entities:
        kwargs = {"type": e["type"], "offset": e["offset"], "length": e["length"]}
        if e.get("url"):
            kwargs["url"] = e["url"]
        if e.get("language"):
            kwargs["language"] = e["language"]
        tb_entities.append(tb_types.MessageEntity(**kwargs))

    return final_text, tb_entities


def build_share_link(product_link: str, source_type: str) -> str:
    """Build an AliExpress 'star' share link carrying a specific
    promotion sourceType, so it can be turned into a special-offer
    affiliate link (coin discount / super deal / limited deal)."""
    redirect = quote(product_link, safe="")
    return (
        "https://star.aliexpress.com/share/share.htm"
        f"?platform=AE&businessType=ProductDetail&redirectUrl={redirect}"
        f"&sourceType={source_type}"
    )


def build_shopcart_link(link: str) -> str:
    from urllib.parse import urlparse, parse_qs

    parsed_url = urlparse(link)
    params = parse_qs(parsed_url.query)
    product_ids = params.get("availableProductShopcartIds")
    if not product_ids:
        raise ValueError("Link has no availableProductShopcartIds parameter")

    query = {
        "availableProductShopcartIds": ",".join(product_ids),
        "extraParams": '{"channelInfo":{"sourceType":"620"}}',
    }
    return "https://www.aliexpress.com/p/trade/confirm.html?" + urlencode(query)

# ---------------------------------------------------------------------------
# AliExpress API calls
# ---------------------------------------------------------------------------

def get_main_affiliate_link(product_link: str) -> str:
    """The one link that must succeed. Raises on failure — callers decide
    how to react (see the exception classes imported above)."""
    links = aliexpress.get_affiliate_links(product_link)
    return links[0].promotion_link


def get_special_offer_links(product_link: str) -> dict:
    """Best-effort extra promo links. Each is optional — AliExpress can
    reject a given sourceType (they change these periodically), so a
    failure here just means that one offer is skipped, not a crash."""
    offers = {
        "عرض العملات (خصم بالعملات المعدنية)": "620",
        "عرض السوبر": "562",
        "عرض محدود": "561",
    }
    results = {}
    for label, source_type in offers.items():
        try:
            share_link = build_share_link(product_link, source_type)
            link = aliexpress.get_affiliate_links(share_link)[0].promotion_link
            results[label] = link
        except Exception as exc:
            log.info("Special offer link '%s' unavailable: %s", label, exc)
    return results


def get_product_info(product_link: str):
    """Best-effort product details (title/price/rating/image). Returns a
    Product object or None.

    AliExpress product IDs are extracted *locally* by the library via a
    regex that expects something like `/1005003091506814.html` in the
    URL. Short/share links (s.click.aliexpress.com, a.aliexpress.com,
    star.aliexpress.com/share/...) don't contain that, so we resolve the
    redirect to the real product page first."""
    try:
        resolved = resolve_redirect(product_link)
        products = aliexpress.get_products_details(resolved)
        return products[0] if products else None
    except Exception as exc:
        log.warning("Could not fetch product details for %s: %s", product_link, exc)
        return None

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_rating(evaluate_rate):
    if not evaluate_rate:
        return None
    try:
        pct = float(str(evaluate_rate).replace("%", "").strip())
    except ValueError:
        return None
    stars_filled = max(0, min(5, round(pct / 100 * 5)))
    return "⭐" * stars_filled + "☆" * (5 - stars_filled) + f" ({pct:.0f}%)"


def format_price(product) -> str:
    sale_price = getattr(product, "target_sale_price", None) or getattr(product, "sale_price", None)
    currency = getattr(product, "target_sale_price_currency", None) or getattr(product, "sale_price_currency", "")
    original_price = getattr(product, "target_original_price", None) or getattr(product, "original_price", None)
    discount = getattr(product, "discount", None)

    if not sale_price:
        return "غير متوفر"

    price_line = f"{sale_price} {currency}".strip()
    if original_price and original_price != sale_price:
        price_line += f" (بدلا من {original_price} {currency})"
    if discount:
        price_line += f" 🔥 خصم {discount}"
    return price_line


def _utf16_len(s: str) -> int:
    """Length of `s` in UTF-16 code units — matches how Telegram defines
    entity offsets, so this is safe to use even with flag emoji or other
    astral (outside-BMP) characters."""
    return len(s.encode("utf-16-le")) // 2


def prepend_text_with_entities(prefix: str, text: str, entities: list):
    """Add `prefix` (plain text, no formatting) before `text`, shifting
    every entity's offset so existing formatting/links stay aligned."""
    if not prefix:
        return text, entities
    shift = _utf16_len(prefix)
    for e in entities or []:
        e.offset += shift
    return prefix + text, entities


def build_product_caption(product, main_link: str, special_links: dict, source_note: str = None) -> str:
    lines = ["🛒 منتجك هو: 🔥"]

    if product is not None:
        title = getattr(product, "product_title", None)
        if title:
            if len(title) > 200:
                title = title[:197] + "..."
            lines.append(f"{title} 🛍")

        lines.append(f"💵 السعر: {format_price(product)}")

        rating = format_rating(getattr(product, "evaluate_rate", None))
        if rating:
            lines.append(f"📊 التقييم: {rating}")

        orders = getattr(product, "lastest_volume", None)
        if orders:
            lines.append(f"📦 عدد الطلبات: {orders}")

    lines.append("")
    lines.append("قارن بين الاسعار واشتري 🔥")
    lines.append(f"🔗 رابط الشراء (سعر تنافسي):\n{main_link}")

    for label, link in special_links.items():
        emoji = "💰" if "عملات" in label else ("💎" if "سوبر" in label else "♨️")
        lines.append(f"\n{emoji} {label}:\n{link}")

    if source_note:
        lines.append(f"\n{source_note}")

    lines.append("\n#AliXPromotion ✅")
    return "\n".join(lines)
