"""Читання сторінки пошуку OLX.ua.

CloudFront відхиляє звичайні HTTP-клієнти (403), тому сторінку відкриває
Chromium через Playwright. Фото з CDN завантажує aiohttp.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.async_api import Playwright, TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

# Звичайний Chrome на Windows, без позначки HeadlessChrome.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1920, "height": 1080}
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-notifications",
]

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_LISTING_RE = re.compile(r"ID[A-Za-z0-9]+\.html", re.IGNORECASE)
_AD_KEY_RE = re.compile(r"ID([A-Za-z0-9]+)")
_PHOTO_SIZE_RE = re.compile(r";s=\d+x\d+")
_WHITESPACE_RE = re.compile(r"\s+")


class OlxFetchError(Exception):
    """OLX не віддав придатний список оголошень."""


@dataclass(slots=True)
class Ad:
    ad_id: str
    title: str
    price: str
    location_date: str
    url: str
    photo_url: str | None


def clean_text(value: str) -> str:
    value = (
        value.replace("\u2014", " - ")
        .replace("\u2013", " - ")
        .replace("\u2212", "-")
        .replace("\xa0", " ")
    )
    return _WHITESPACE_RE.sub(" ", value).strip()


def normalize_search_url(raw: str) -> str | None:
    """Повертає канонічне посилання на список OLX або None, якщо адреса не підходить."""
    text = raw.strip()
    if re.match(r"^(?:www\.)?olx\.ua/", text, re.IGNORECASE):
        text = "https://" + text

    match = _URL_RE.search(text)
    if match is None:
        return None

    candidate = match.group(0).rstrip(".,);]>\"'")
    if len(candidate) > 2000:
        return None

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"}:
        return None

    host = (parts.hostname or "").lower()
    if host != "olx.ua" and not host.endswith(".olx.ua"):
        return None

    path = parts.path or "/"
    if "/obyavlenie/" in path.lower() or _LISTING_RE.search(path):
        return None

    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def normalize_photo_url(src: str | None) -> str | None:
    if not src:
        return None
    src = src.strip()
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("http"):
        return None
    src = src.replace(":443/", "/")
    return _PHOTO_SIZE_RE.sub(";s=1000x700", src, count=1)


def absolute_olx_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.olx.ua" + href
    parts = urlsplit(href)
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


def parse_search_html(html: str) -> list[Ad]:
    soup = BeautifulSoup(html, "html.parser")
    if _is_empty_results(soup):
        return []

    cards = soup.select('[data-testid="listing-grid"] [data-cy="l-card"]')
    if not cards:
        cards = soup.select('[data-cy="l-card"]')
    if not cards:
        if _page_claims_results(soup):
            raise OlxFetchError(
                "Сторінка OLX відкрилась, але картки оголошень не знайдені. Можливо, змінилася верстка."
            )
        raise OlxFetchError("Не вдалося прочитати список оголошень OLX.")

    ads: list[Ad] = []
    seen_ids: set[str] = set()
    for card in cards:
        ad = _card_to_ad(card)
        if ad is None or ad.ad_id in seen_ids:
            continue
        seen_ids.add(ad.ad_id)
        ads.append(ad)

    if not ads:
        raise OlxFetchError("Картки на сторінці є, але з них не вдалося зібрати оголошення.")
    return ads


def _is_empty_results(soup: BeautifulSoup) -> bool:
    node = soup.select_one('[data-testid="qa-header-message"]')
    if node is None:
        return False
    text = clean_text(node.get_text(" ", strip=True)).lower()
    return "нічого не знайшли" in text


def _page_claims_results(soup: BeautifulSoup) -> bool:
    node = soup.select_one('[data-testid="listing-count-msg"]')
    if node is None:
        return False
    text = clean_text(node.get_text(" ", strip=True)).lower()
    return "знайшли" in text and "нічого не знайшли" not in text


def _card_to_ad(card) -> Ad | None:
    link = card.select_one("a[data-testid='card-title-link'][href]")
    if link is None:
        link = card.select_one("a[href*='obyavlenie'], a[href*='/d/']")
    if link is None:
        return None
    href = (link.get("href") or "").strip()
    if not href:
        return None

    url = absolute_olx_url(href)
    title_node = card.select_one("h4, h6, h3")
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")
    if not title:
        title = clean_text(link.get_text(" ", strip=True))
    if not title:
        image = card.select_one("img[alt]")
        title = clean_text(image.get("alt", "") if image else "")
    if not title:
        return None

    price_node = card.select_one("[data-testid='ad-price']")
    price = clean_text(price_node.get_text(" ", strip=True) if price_node else "")
    location_node = card.select_one("[data-testid='location-date']")
    location = clean_text(location_node.get_text(" ", strip=True) if location_node else "")
    image = card.select_one("img[src]")
    photo = normalize_photo_url(image.get("src") if image else None)
    ad_id = _ad_id(card.get("id"), url)
    if not ad_id:
        return None

    return Ad(
        ad_id=ad_id,
        title=title,
        price=price,
        location_date=location,
        url=url,
        photo_url=photo,
    )


def _ad_id(raw_id: str | None, url: str) -> str:
    if raw_id and str(raw_id).strip().isdigit():
        return str(raw_id).strip()
    match = _AD_KEY_RE.search(url)
    if match:
        return match.group(1)
    if raw_id and str(raw_id).strip():
        return str(raw_id).strip()
    return ""


def _looks_blocked(html: str) -> bool:
    lowered = html.lower()
    if "the request could not be satisfied" in lowered:
        return True
    if "just a moment" in lowered and "cloudflare" in lowered:
        return True
    if "captcha" in lowered and "l-card" not in lowered:
        return True
    return False


class OlxParser:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._http: aiohttp.ClientSession | None = None
        self._stealth = Stealth(
            navigator_languages_override=("uk-UA", "uk"),
            navigator_platform_override="Win32",
            navigator_user_agent_override=CHROME_USER_AGENT,
        )
        self._fetch_lock = asyncio.Lock()

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        self._http = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": CHROME_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        try:
            self._playwright = await async_playwright().start()
            self._stealth.hook_playwright_context(self._playwright)
            self._browser = await _launch_browser(self._playwright)
            self._context = await self._browser.new_context(
                locale="uk-UA",
                user_agent=CHROME_USER_AGENT,
                viewport=_VIEWPORT,
                extra_http_headers={
                    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            await self._context.route("**/*", _skip_heavy_resources)
        except Exception as exc:
            await self.close()
            raise RuntimeError(
                "Не вдалося запустити браузер Playwright. "
                "Виконайте: python -m playwright install chromium"
            ) from exc

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def fetch_ads(self, url: str) -> list[Ad]:
        if self._context is None:
            raise OlxFetchError("Браузер для OLX ще не запущений.")

        async with self._fetch_lock:
            return await self._fetch_ads_locked(url)

    async def _fetch_ads_locked(self, url: str) -> list[Ad]:
        assert self._context is not None
        page = await self._context.new_page()
        try:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeout as exc:
                raise OlxFetchError("OLX занадто довго відкривав сторінку.") from exc
            except PlaywrightError as exc:
                logger.warning("Помилка відкриття OLX: %s", exc)
                raise OlxFetchError("Не вдалося відкрити сторінку OLX.") from exc

            status = response.status if response is not None else 0
            if status >= 400:
                raise OlxFetchError(f"OLX відповів кодом {status}.")

            await _dismiss_cookies(page)
            try:
                await page.wait_for_selector(
                    "[data-cy='l-card'], [data-testid='qa-header-message']",
                    timeout=20000,
                )
            except PlaywrightTimeout:
                logger.info("Картки OLX не з'явилися одразу, читаю те, що вже є на сторінці.")

            html = await page.content()
            title = await page.title()
            if _looks_blocked(html):
                raise OlxFetchError("OLX відхилив запит. Спробуйте пізніше.")
            if len(html) < 3000 and "l-card" not in html:
                logger.warning("Підозріло коротка сторінка OLX (%s байт), title=%s", len(html), title)
                raise OlxFetchError("OLX повернув порожню сторінку.")

            return parse_search_html(html)
        finally:
            await page.close()

    async def download_photo(self, url: str) -> bytes | None:
        if self._http is None:
            return None
        try:
            async with self._http.get(url) as response:
                if response.status != 200:
                    return None
                content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type.lower():
                    return None
                data = await response.content.read(8_000_001)
        except (aiohttp.ClientError, TimeoutError):
            logger.info("Не вдалося завантажити фото оголошення.")
            return None

        if len(data) < 1000 or len(data) > 8_000_000:
            return None
        return data


async def _launch_browser(playwright: Playwright) -> Browser:
    """Спершу системний Google Chrome, якщо його немає - вбудований Chromium."""
    launch_options = {
        "headless": True,
        "ignore_default_args": ["--enable-automation"],
        "args": list(_LAUNCH_ARGS),
    }
    try:
        browser = await playwright.chromium.launch(channel="chrome", **launch_options)
    except PlaywrightError as exc:
        logger.info("Google Chrome недоступний (%s). Запускаю вбудований Chromium.", exc)
        browser = await playwright.chromium.launch(**launch_options)
    else:
        logger.info("Запущено Google Chrome у фоновому режимі.")
    return browser


async def _skip_heavy_resources(route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
        return
    await route.continue_()


async def _dismiss_cookies(page) -> None:
    banner = page.locator(
        "[data-testid='dismiss-cookies-banner'], #onetrust-accept-btn-handler"
    )
    try:
        if await banner.count():
            await banner.first.click(timeout=2000)
    except PlaywrightError:
        return
