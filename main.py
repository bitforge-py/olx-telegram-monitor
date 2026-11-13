"""Запуск бота і фонова перевірка збережених пошуків OLX."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from database import Database
from handlers import router, send_new_ad
from parser import OlxFetchError, OlxParser

logger = logging.getLogger(__name__)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def check_once(bot: Bot, db: Database, olx: OlxParser) -> None:
    watches = await db.list_active_watches()
    sent = 0
    for watch in watches:
        if not await db.monitoring_enabled(watch.user_id):
            continue
        try:
            ads = await olx.fetch_ads(watch.url)
        except OlxFetchError as exc:
            logger.warning("Посилання %s не перевірено: %s", watch.id, exc)
            await db.set_watch_error(watch.id, str(exc))
            continue
        except Exception:
            logger.exception("Збій перевірки посилання %s", watch.id)
            await db.set_watch_error(watch.id, "Не вдалося відкрити сторінку OLX.")
            continue

        if not watch.seeded:
            await db.remember_ads(watch, ads)
            await db.mark_seeded(watch.id)
            logger.info(
                "Посилання %s: початковий список з %s оголошень без розсилки.",
                watch.id,
                len(ads),
            )
            continue

        await db.mark_checked(watch.id)
        pending = []
        for ad in ads:
            if await db.is_seen(watch.user_id, ad.ad_id, ad.url):
                continue
            pending.append(ad)

        for ad in reversed(pending):
            if not await db.monitoring_enabled(watch.user_id):
                break
            delivered = await send_new_ad(bot, db, olx, watch.user_id, ad)
            if not delivered:
                logger.info(
                    "Розсилку для посилання %s перервано. Повтор буде в наступному циклі.",
                    watch.id,
                )
                break
            await db.remember_ads(watch, [ad])
            sent += 1
            await asyncio.sleep(0.35)

    logger.info("Перевірено посилань: %s. Надіслано нових оголошень: %s.", len(watches), sent)


async def monitor_loop(bot: Bot, db: Database, olx: OlxParser) -> None:
    logger.info("Моніторинг запущено. Інтервал: %s с.", config.CHECK_INTERVAL_SECONDS)
    while True:
        started = time.monotonic()
        try:
            await check_once(bot, db, olx)
        except Exception:
            logger.exception("Помилка циклу перевірки")
        elapsed = time.monotonic() - started
        delay = max(1.0, config.CHECK_INTERVAL_SECONDS - elapsed)
        await asyncio.sleep(delay)


async def run() -> None:
    db = Database(config.DB_PATH)
    olx = OlxParser()
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)

    await db.connect()
    await olx.start()
    monitor = asyncio.create_task(monitor_loop(bot, db, olx))
    try:
        await dispatcher.start_polling(bot, db=db, olx=olx)
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        await olx.close()
        await db.close()
        await bot.session.close()


def main() -> None:
    _configure_stdio()
    _configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Бота зупинено.")


if __name__ == "__main__":
    main()
