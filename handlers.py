"""Команди, кнопки і повідомлення бота."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from database import Database, Watch
from parser import Ad, OlxFetchError, OlxParser, normalize_search_url

logger = logging.getLogger(__name__)

router = Router()
MAX_WATCHES = 20
PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)

HOME_TEXT = (
    "Вітаю. Я стежу за новими оголошеннями на OLX і пишу, коли з'являється нове.\n\n"
    "Надішліть посилання на сторінку пошуку OLX.ua з уже вибраними фільтрами ціни та міста. "
    "Те, що вже є на сторінці, я лише запам'ятаю. Сповіщення надсилаю тільки про нові оголошення.\n\n"
    "Оберіть дію кнопкою нижче."
)

ADD_PROMPT = (
    "Надішліть посилання на пошук OLX.ua. Фільтри ціни та міста мають уже бути в адресі.\n\n"
    "Приклад: https://www.olx.ua/uk/list/q-iphone/\n\n"
    "Щоб скасувати, надішліть /cancel."
)


class AddLinkState(StatesGroup):
    waiting_for_url = State()


def main_keyboard(monitoring_enabled: bool) -> InlineKeyboardMarkup:
    toggle = "Зупинити моніторинг" if monitoring_enabled else "Запустити моніторинг"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Додати посилання", callback_data="add_link")],
            [InlineKeyboardButton(text="Мої відстеження", callback_data="my_watches")],
            [InlineKeyboardButton(text=toggle, callback_data="toggle_monitoring")],
        ]
    )


def ad_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Відкрити оголошення", url=url)],
        ]
    )


def ads_count_label(count: int) -> str:
    remainder_100 = abs(count) % 100
    remainder_10 = remainder_100 % 10
    if 11 <= remainder_100 <= 14:
        word = "оголошень"
    elif remainder_10 == 1:
        word = "оголошення"
    elif 2 <= remainder_10 <= 4:
        word = "оголошення"
    else:
        word = "оголошень"
    return f"{count} {word}"


def format_ad_caption(ad: Ad) -> str:
    title = ad.title.strip()
    if len(title) > 400:
        title = title[:400].rstrip() + "..."
    price = ad.price.strip() or "не вказана"
    lines = [f"<b>{escape(title)}</b>", "", f"Ціна: {escape(price)}"]
    location = ad.location_date.strip()
    if location:
        lines.append(escape(location))
    return "\n".join(lines)


def _private_chat(message: Message) -> bool:
    return message.chat.type == "private"


async def _reply_home(message: Message, db: Database, user_id: int) -> None:
    enabled = await db.monitoring_enabled(user_id)
    await message.answer(HOME_TEXT, reply_markup=main_keyboard(enabled))


def _watches_view(watches: list[Watch]) -> tuple[str, InlineKeyboardMarkup]:
    if not watches:
        text = (
            "У вас ще немає збережених посилань. "
            "Натисніть «Додати посилання» і надішліть адресу пошуку OLX."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Додати посилання", callback_data="add_link")],
                [InlineKeyboardButton(text="На головну", callback_data="home")],
            ]
        )
        return text, markup

    lines = ["Ваші відстеження:"]
    rows: list[list[InlineKeyboardButton]] = []
    for index, watch in enumerate(watches, start=1):
        short = watch.url if len(watch.url) <= 80 else watch.url[:77] + "..."
        lines.append(
            f"\n{index}. <a href=\"{escape(watch.url, quote=True)}\">{escape(short)}</a>\n"
            f"Запам'ятано: {watch.seen_count}"
        )
        if not watch.seeded:
            lines.append("Поточний список ще збираю, старі оголошення не надсилаю.")
        if watch.last_error:
            lines.append("Остання перевірка не вдалася. Спробую ще раз.")
        rows.append(
            [InlineKeyboardButton(text=f"Видалити {index}", callback_data=f"del:{watch.id}")]
        )

    rows.append([InlineKeyboardButton(text="Додати посилання", callback_data="add_link")])
    rows.append([InlineKeyboardButton(text="На головну", callback_data="home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def store_search_link(
    message: Message,
    db: Database,
    olx: OlxParser,
    state: FSMContext,
) -> None:
    user = message.from_user
    if user is None or not _private_chat(message):
        return

    url = normalize_search_url(message.text or "")
    if url is None:
        await message.answer(
            "Це не посилання на список оголошень OLX.ua. "
            "Скопіюйте адресу сторінки пошуку з фільтрами. Окреме оголошення не підходить.",
            reply_markup=main_keyboard(await db.monitoring_enabled(user.id)),
        )
        return

    await db.ensure_user(user.id)
    enabled = await db.monitoring_enabled(user.id)
    if await db.has_watch(user.id, url):
        await state.clear()
        await message.answer(
            "Таке посилання вже є у ваших відстеженнях.",
            reply_markup=main_keyboard(enabled),
        )
        return

    if await db.count_watches(user.id) >= MAX_WATCHES:
        await state.clear()
        await message.answer(
            "Можна зберегти не більше 20 посилань. "
            "Видаліть зайве в розділі «Мої відстеження».",
            reply_markup=main_keyboard(enabled),
        )
        return

    await state.clear()
    await message.answer(
        "Відкриваю сторінку OLX і запам'ятовую поточні оголошення. Зачекайте, будь ласка."
    )

    ads: list[Ad] | None
    try:
        ads = await olx.fetch_ads(url)
    except OlxFetchError:
        logger.warning("Не вдалося одразу прочитати %s", url)
        ads = None
    except Exception:
        logger.exception("Збій читання OLX для %s", url)
        ads = None

    watch = await db.add_watch(user.id, url, ads)
    enabled = await db.monitoring_enabled(user.id)
    if watch is None:
        await message.answer(
            "Таке посилання вже є у ваших відстеженнях.",
            reply_markup=main_keyboard(enabled),
        )
        return

    if ads is None:
        await message.answer(
            "Посилання збережено, але зараз не вдалося відкрити OLX. "
            "Я повторю спробу під час перевірки. Старі оголошення не надішлю: "
            "спершу лише запам'ятаю те, що вже є на сторінці.",
            reply_markup=main_keyboard(enabled),
        )
        return

    if not ads:
        await message.answer(
            "Збережено. Зараз на сторінці немає оголошень за цим пошуком. "
            "Щойно з'явиться нове, я надішлю його.",
            reply_markup=main_keyboard(enabled),
        )
        return

    pronoun = "його" if len(ads) == 1 else "їх"
    await message.answer(
        f"Збережено. Запам'ятав {ads_count_label(len(ads))} і не надсилаю {pronoun}. "
        "Далі писатиму лише про нові.",
        reply_markup=main_keyboard(enabled),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        return
    if not _private_chat(message):
        await message.answer("Я працюю в приватному чаті. Напишіть мені напряму.")
        return
    await state.clear()
    await db.ensure_user(user.id)
    await _reply_home(message, db, user.id)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: Database, state: FSMContext) -> None:
    user = message.from_user
    if user is None or not _private_chat(message):
        return
    await state.clear()
    await db.ensure_user(user.id)
    await message.answer("Гаразд, скасовано.")
    await _reply_home(message, db, user.id)


@router.callback_query(F.data == "home")
async def show_home(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    user = callback.from_user
    await state.clear()
    await db.ensure_user(user.id)
    enabled = await db.monitoring_enabled(user.id)
    await callback.answer()
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(HOME_TEXT, reply_markup=main_keyboard(enabled))
    except TelegramBadRequest:
        await callback.message.answer(HOME_TEXT, reply_markup=main_keyboard(enabled))


@router.callback_query(F.data == "add_link")
async def ask_for_link(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddLinkState.waiting_for_url)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(ADD_PROMPT)


@router.callback_query(F.data == "my_watches")
async def show_watches(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    user = callback.from_user
    await state.clear()
    await db.ensure_user(user.id)
    watches = await db.list_watches(user.id)
    text, markup = _watches_view(watches)
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.answer(
        text,
        reply_markup=markup,
        link_preview_options=PREVIEW_OFF,
    )


@router.callback_query(F.data == "toggle_monitoring")
async def toggle_monitoring(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    user = callback.from_user
    await state.clear()
    await db.ensure_user(user.id)
    enabled = not await db.monitoring_enabled(user.id)
    await db.set_monitoring(user.id, enabled)
    await callback.answer("Моніторинг запущено" if enabled else "Моніторинг зупинено")
    if callback.message is None:
        return
    if enabled:
        text = "Моніторинг запущено. Збережені посилання знову перевіряються."
    else:
        text = (
            "Моніторинг зупинено. Нові оголошення не надсилатиму, "
            "доки ви знову його не запустите."
        )
    await callback.message.answer(text, reply_markup=main_keyboard(enabled))


@router.callback_query(F.data.startswith("del:"))
async def delete_watch(callback: CallbackQuery, db: Database) -> None:
    user = callback.from_user
    raw_id = (callback.data or "").split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer("Некоректна дія.")
        return

    deleted = await db.delete_watch(user.id, int(raw_id))
    await callback.answer("Посилання видалено." if deleted else "Це посилання вже видалене.")
    watches = await db.list_watches(user.id)
    text, markup = _watches_view(watches)
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            reply_markup=markup,
            link_preview_options=PREVIEW_OFF,
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text,
            reply_markup=markup,
            link_preview_options=PREVIEW_OFF,
        )


@router.message(AddLinkState.waiting_for_url)
async def receive_link(
    message: Message,
    db: Database,
    olx: OlxParser,
    state: FSMContext,
) -> None:
    if message.text and message.text.startswith("/"):
        return
    await store_search_link(message, db, olx, state)


@router.message(F.text)
async def receive_link_anytime(
    message: Message,
    db: Database,
    olx: OlxParser,
    state: FSMContext,
) -> None:
    text = message.text or ""
    if "olx.ua" not in text.lower() and not text.lower().startswith("http"):
        user = message.from_user
        if user is None or not _private_chat(message):
            return
        await message.answer(
            "Надішліть посилання на пошук OLX або оберіть дію кнопкою нижче.",
            reply_markup=main_keyboard(await db.monitoring_enabled(user.id)),
        )
        return
    await store_search_link(message, db, olx, state)


async def send_new_ad(bot: Bot, db: Database, olx: OlxParser, user_id: int, ad: Ad) -> bool:
    caption = format_ad_caption(ad)
    keyboard = ad_keyboard(ad.url)
    try:
        await _deliver(bot, olx, user_id, ad, caption, keyboard)
        return True
    except TelegramForbiddenError:
        logger.info("Користувач %s заблокував бота. Моніторинг для нього вимкнено.", user_id)
        await db.set_monitoring(user_id, False)
        return False
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        try:
            await _deliver(bot, olx, user_id, ad, caption, keyboard)
            return True
        except TelegramForbiddenError:
            await db.set_monitoring(user_id, False)
            return False
        except Exception:
            logger.exception("Повторна відправка оголошення %s не вдалася.", ad.ad_id)
            return False
    except Exception:
        logger.exception("Не вдалося надіслати оголошення %s користувачу %s.", ad.ad_id, user_id)
        return False


async def _deliver(
    bot: Bot,
    olx: OlxParser,
    user_id: int,
    ad: Ad,
    caption: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if ad.photo_url:
        try:
            await bot.send_photo(
                user_id,
                ad.photo_url,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except (TelegramForbiddenError, TelegramRetryAfter):
            raise
        except Exception:
            logger.info("Фото за посиланням не надіслалось, пробую файл.")

        photo = await olx.download_photo(ad.photo_url)
        if photo:
            try:
                await bot.send_photo(
                    user_id,
                    BufferedInputFile(photo, filename="ad.jpg"),
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception:
                logger.info("Файл фото не надіслався, надсилаю текст.")

    await bot.send_message(user_id, caption, reply_markup=keyboard)
