"""
Background notification system for payment reminders
"""

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import List
from aiogram import Bot

from bot.config.settings import Settings
from bot.database.operations import Database
from bot.utils.helpers import format_date, get_now, format_datetime
from bot.utils.keyboards import get_user_main_menu


class NotificationScheduler:
    """Handles background payment notifications"""

    def __init__(self, bot: Bot, database: Database, settings: Settings):
        self.bot = bot
        self.db = database
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.is_running = False
        self._task = None

    async def start(self):
        """Start the notification scheduler"""
        if self.is_running:
            return

        self.is_running = True
        self._task = asyncio.create_task(self._scheduler_loop())
        self.logger.info("🔔 Notification scheduler started")

    async def stop(self):
        """Stop the notification scheduler"""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("🔕 Notification scheduler stopped")

    async def _scheduler_loop(self):
        """Main scheduler loop - runs daily at 12:00 PM"""
        while self.is_running:
            try:
                # Calculate time until next 12:00 PM
                now = get_now()
                next_run = now.replace(
                    hour=12, minute=0, second=0, microsecond=0)

                # If it's already past 12 PM today, schedule for tomorrow
                if now.time() >= time(12, 0):
                    next_run = next_run + timedelta(days=1)

                sleep_seconds = (next_run - now).total_seconds()
                self.logger.info(
                    f"⏰ Next notification check scheduled for {format_datetime(next_run)}")

                # Wait until next scheduled time
                await asyncio.sleep(sleep_seconds)

                # Run notification checks
                if self.is_running:
                    await self._run_notification_checks()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ Error in scheduler loop: {e}")
                # Wait 1 hour before retrying on error
                await asyncio.sleep(3600)

    async def _run_notification_checks(self):
        """Run all notification checks"""
        self.logger.info("🔍 Running daily notification checks...")

        try:
            # Send user reminders (3 days before due date)
            await self._send_user_reminders()

            # Send admin warnings (3 days after due date)
            await self._send_admin_warnings()

            self.logger.info("✅ Daily notification checks completed")

        except Exception as e:
            self.logger.error(f"❌ Error during notification checks: {e}")

    async def _send_user_reminders(self):
        """Send payment reminders to users (on due date and up to 2 days after)"""
        if not self.db or not self.db.pool:
            return

        users_needing_reminders = await self.db.get_users_needing_reminders()

        if not users_needing_reminders:
            self.logger.info("📭 Сегодня никому не нужны напоминания об оплате")
            return

        self.logger.info(
            f"📬 Отправка напоминаний {len(users_needing_reminders)} пользователям")

        from datetime import date
        today = get_now().date()

        for status in users_needing_reminders:
            try:
                # Calculate days overdue
                payment_date = status.next_payment_date
                if isinstance(payment_date, datetime):
                    payment_date = payment_date.date()

                days_overdue = (today - payment_date).days

                # Generate appropriate message based on days overdue
                if days_overdue == 0:
                    # Payment due TODAY
                    reminder_text = (
                        f"⏰ <b>НАПОМИНАНИЕ ОБ ОПЛАТЕ</b>\n\n"
                        f"🔴 Ваш платёж за Spotify для группы <b>{status.group_name}</b> должен быть совершён <b>СЕГОДНЯ</b>!\n\n"
                        f"📅 <b>Дата платежа:</b> {format_date(status.next_payment_date)}\n"
                        f"💰 <b>Сумма:</b> {self.settings.bot_default_payment_price} ₸\n\n"
                        f"💳 <b>Оплата на Kaspi Bank:</b>\n\n"
                        f"{self.settings.bot_payment_link}\n\n"
                        f"💡 <b>Для оплаты:</b> Используйте команду /pay и загрузите чек\n"
                        f"📊 <b>Проверить статус:</b> Используйте команду /status\n\n"
                        f"⚠️ Пожалуйста, совершите платёж сегодня, чтобы сохранить доступ к Spotify!"
                    )
                elif days_overdue == 1:
                    # 1 day overdue
                    reminder_text = (
                        f"⚠️ <b>ПЛАТЁЖ ПРОСРОЧЕН</b>\n\n"
                        f"Ваш платёж за Spotify для группы <b>{status.group_name}</b> просрочен на <b>1 день</b>.\n\n"
                        f"📅 <b>Срок был:</b> {format_date(status.next_payment_date)}\n"
                        f"💰 <b>Сумма:</b> {self.settings.bot_default_payment_price} ₸\n\n"
                        f"💳 <b>Оплата на Kaspi Bank:</b>\n\n"
                        f"{self.settings.bot_payment_link}\n\n"
                        f"💡 <b>Для оплаты:</b> Используйте команду /pay и загрузите чек\n"
                        f"📊 <b>Проверить статус:</b> Используйте команду /status\n\n"
                        f"🚨 Пожалуйста, оплатите как можно скорее, чтобы избежать отключения!"
                    )
                elif days_overdue == 2:
                    # 2 days overdue - FINAL REMINDER
                    reminder_text = (
                        f"🚨 <b>ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ</b>\n\n"
                        f"Ваш платёж за Spotify для группы <b>{status.group_name}</b> просрочен на <b>2 дня</b>!\n\n"
                        f"📅 <b>Срок был:</b> {format_date(status.next_payment_date)}\n"
                        f"💰 <b>Сумма:</b> {self.settings.bot_default_payment_price} ₸\n\n"
                        f"💳 <b>Оплата на Kaspi Bank:</b>\n\n"
                        f"{self.settings.bot_payment_link}\n\n"
                        f"💡 <b>Для оплаты:</b> Используйте команду /pay и загрузите чек\n\n"
                        f"⛔ <b>ВНИМАНИЕ:</b> Если оплата не будет получена завтра, администратор будет уведомлён, "
                        f"и вы можете быть удалены из группы!\n\n"
                        f"🆘 Оплатите СРОЧНО!"
                    )
                elif days_overdue == 3:
                     # 3 days overdue - CRITICAL
                    reminder_text = (
                        f"❌ <b>КРИТИЧЕСКАЯ СИТУАЦИЯ</b>\n\n"
                        f"Ваш платёж за Spotify для группы <b>{status.group_name}</b> просрочен на <b>3 дня</b>.\n\n"
                        f"📅 <b>Срок был:</b> {format_date(status.next_payment_date)}\n"
                        f"💰 <b>Сумма:</b> {self.settings.bot_default_payment_price} ₸\n\n"
                        f"💳 <b>Оплата на Kaspi Bank:</b>\n\n"
                        f"{self.settings.bot_payment_link}\n\n"
                        f"⚠️ <b>Администратор был уведомлён о вашей задолженности.</b>\n"
                        f"Вы рискуете быть удалённым из группы в любой момент.\n\n"
                        f"🆘 <b>ПОЖАЛУЙСТА, ОПЛАТИТЕ НЕМЕДЛЕННО!</b>"
                    )
                else:
                    # Skip if outside 0-3 range (shouldn't happen with query filter)
                    continue

                await self.bot.send_message(
                    chat_id=status.user_id,
                    text=reminder_text,
                    parse_mode="HTML",
                    reply_markup=get_user_main_menu()
                )

                self.logger.info(
                    f"📤 Напоминание (день {days_overdue}) отправлено пользователю {status.user_id} для группы '{status.group_name}'")
                # Small delay between messages to avoid rate limits
                await asyncio.sleep(0.5)

            except Exception as e:
                self.logger.error(
                    f"❌ Не удалось отправить напоминание пользователю {status.user_id}: {e}")
                # Continue with next user even if one fails
                continue

    async def _send_admin_warnings(self):
        """Send overdue warnings to admins"""
        if not self.db or not self.db.pool:
            return

        warning_days = 3  # 3 days after due date
        overdue_users = await self.db.get_users_overdue_for_admin_warning(warning_days)

        if not overdue_users:
            self.logger.info(
                "📭 Нет просроченных пользователей для предупреждения администраторов")
            return

        self.logger.info(
            f"⚠️ Предупреждение администраторов о {len(overdue_users)} просроченных пользователях")

        # Group overdue users by group for better admin messages
        groups_with_overdue = {}
        for status in overdue_users:
            group_name = status.group_name
            if group_name not in groups_with_overdue:
                groups_with_overdue[group_name] = []
            groups_with_overdue[group_name].append(status)

        # Send warning to each admin
        for admin_id in self.settings.tg_admin_ids:
            try:
                warning_text = "🚨 <b>ПРЕДУПРЕЖДЕНИЕ О ПРОСРОЧЕННОЙ ОПЛАТЕ</b>\n\n"
                warning_text += f"Следующие пользователи просрочили платежи на {warning_days}+ дней:\n\n"

                for group_name, users in groups_with_overdue.items():
                    warning_text += f"<b>📂 {group_name}:</b>\n"
                    for status in users:
                        first_name = status.first_name or 'Unknown'
                        user_display = first_name
                        if status.username:
                            user_display += f" (@{status.username})"
                        user_display += f" (ID: {status.user_id})"

                        warning_text += f"• {user_display}\n"
                        warning_text += f"  📅 Срок был: {format_date(status.next_payment_date)}\n"
                        warning_text += f"  ⏱️ Просрочено: {status.days_overdue} дней\n\n"

                warning_text += "💡 <b>Действия, которые вы можете предпринять:</b>\n"
                warning_text += "• Связаться с этими пользователями напрямую\n"
                warning_text += "• Удалить их из группы при необходимости\n"
                warning_text += "• Проверить статус платежей через /admin\n"

                await self.bot.send_message(
                    chat_id=admin_id,
                    text=warning_text,
                    parse_mode="HTML"
                )

                self.logger.info(
                    f"⚠️ Предупреждение о просрочке отправлено администратору {admin_id}")

                # Small delay between messages
                await asyncio.sleep(0.5)

            except Exception as e:
                self.logger.error(
                    f"❌ Не удалось отправить предупреждение администратору {admin_id}: {e}")
                # Continue with next admin even if one fails
                continue

    async def send_test_notifications(self):
        """Send test notifications (for debugging)"""
        self.logger.info("🧪 Отправка тестовых уведомлений...")
        await self._run_notification_checks()
