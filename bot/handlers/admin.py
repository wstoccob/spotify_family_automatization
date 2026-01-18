"""
Admin command handlers for the Spotify Payment Bot
"""

import logging
import tempfile
import os
from datetime import datetime, timedelta
from aiogram import Router, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatType
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

from bot.database.operations import Database
from bot.config.settings import Settings
from bot.utils.states import AdminStates
from bot.utils.keyboards import get_admin_main_keyboard, get_confirmation_keyboard, get_pagination_keyboard
from bot.utils.helpers import (
    format_date, calculate_days_until,
    get_payment_status_emoji, get_payment_status_text,
    is_admin, get_now, format_datetime
)

# Initialize router
admin_router = Router()
logger = logging.getLogger(__name__)

# Global database instance (will be injected)
db: Database = None
settings: Settings = None


def init_admin_handlers(database: Database, bot_settings: Settings):
    """Initialize handlers with database and settings"""
    global db, settings
    db = database
    settings = bot_settings


@admin_router.message(Command("admin"))
async def admin_command(message: types.Message, state: FSMContext):
    """Handle /admin command"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not db or not db.pool:
        await message.answer("❌ База данных в настоящее время недоступна.")
        return

    # Clear any active state (allows canceling any operation)
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("✅ Предыдущая операция отменена.\n")

    await message.answer(
        "🔧 **Панель администратора**\n\n"
        "Выберите действие:",
        reply_markup=get_admin_main_keyboard(),
        parse_mode="Markdown"
    )


@admin_router.message(Command("check_notifications"))
async def check_notifications_command(message: types.Message):
    """Check what notifications would be sent (without actually sending them)"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not db or not db.pool:
        await message.answer("❌ База данных в настоящее время недоступна.")
        return

    try:
        # Check users needing reminders
        reminder_users = await db.get_users_needing_reminders(settings.bot_payment_reminder_days)

        # Check users overdue for admin warnings
        overdue_users = await db.get_users_overdue_for_admin_warning(3)

        response = "🔍 *Проверка статуса уведомлений*\n\n"

        if reminder_users:
            response += f"📬 *Пользователи, нуждающиеся в напоминаниях* ({len(reminder_users)}):\n"
            for status in reminder_users:
                response += f"• Пользователь {status.user_id} в '{status.group_name}'\n"
                response += f"  📅 Срок: {format_date(status.next_payment_date)}\n"
            response += "\n"
        else:
            response += "📭 Сегодня никому не нужны напоминания об оплате\n\n"

        if overdue_users:
            response += f"🚨 *Пользователи с просроченными платежами для предупреждения администратора* ({len(overdue_users)}):\n"
            for status in overdue_users:
                user_name = getattr(status, 'first_name',
                                    f'Пользователь {status.user_id}')
                response += f"• {user_name} в '{status.group_name}'\n"
                response += f"  📅 Срок был: {format_date(status.next_payment_date)}\n"
            response += "\n"
        else:
            response += "✅ Нет пользователей с просроченными предупреждениями\n\n"

        response += "💡 Используйте /test\\_notifications для фактической отправки этих уведомлений"

        await message.answer(response, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Check notifications failed: {e}")
        await message.answer(
            f"❌ *Проверка не удалась:*\n\n`{str(e)}`",
            parse_mode="Markdown"
        )


@admin_router.message(Command("test_notifications"))
async def test_notifications_command(message: types.Message):
    """Handle /test_notifications command"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not db or not db.pool:
        await message.answer("❌ База данных в настоящее время недоступна.")
        return

    await message.answer("🧪 Запуск тестовых уведомлений...")

    try:
        # Import here to avoid circular imports
        from bot.utils.notifications import NotificationScheduler

        # Create a temporary scheduler for testing
        test_scheduler = NotificationScheduler(message.bot, db, settings)

        # Run the notification checks
        await test_scheduler.send_test_notifications()

        await message.answer(
            "✅ **Тестовые уведомления завершены!**\n\n"
            "Проверьте логи бота, чтобы увидеть, были ли отправлены уведомления.\n\n"
            "💡 **Напоминание:** Уведомления отправляются автоматически ежедневно в 9:00 утра.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ Test notifications failed: {e}")
        await message.answer(
            f"❌ **Тестовые уведомления не удались:**\n\n"
            f"`{str(e)}`\n\n"
            f"Проверьте логи бота для получения дополнительной информации.",
            parse_mode="Markdown"
        )


@admin_router.message(Command("test_admin_notification"))
async def test_admin_notification_command(message: types.Message):
    """Handle /test_admin_notification command - instantly trigger admin warnings"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not db or not db.pool:
        await message.answer("❌ База данных в настоящее время недоступна.")
        return

    await message.answer("🧪 Запуск теста уведомлений администраторов...")

    try:
        # Import here to avoid circular imports
        from bot.utils.notifications import NotificationScheduler

        # Create a temporary scheduler for testing
        test_scheduler = NotificationScheduler(message.bot, db, settings)

        # Run only the admin warning check
        await test_scheduler._send_admin_warnings()

        await message.answer(
            "✅ **Тест уведомлений администраторов завершен!**\n\n"
            "Если есть пользователи, просроченные на 3+ дня, вы должны были получить уведомление.\n\n"
            "💡 **Напоминание:** Уведомления администраторов отправляются автоматически ежедневно в 9:00 утра.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ Test admin notification failed: {e}")
        await message.answer(
            f"❌ **Тест не удался:**\n\n"
            f"`{str(e)}`\n\n"
            f"Проверьте логи бота для получения дополнительной информации.",
            parse_mode="Markdown"
        )


@admin_router.message(Command("update_due_date"))
async def update_due_date_command(message: types.Message, state: FSMContext):
    """Handle /update_due_date command"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not db or not db.pool:
        await message.answer("❌ База данных в настоящее время недоступна.")
        return

    # Show available groups
    groups = await db.get_all_groups()
    if not groups:
        await message.answer("❌ Нет доступных групп.")
        return

    groups_text = "📅 **Обновить дату платежа группы**\n\n"
    groups_text += "Доступные группы:\n\n"

    for group in groups:
        days_until = calculate_days_until(group.next_payment_date)
        status_emoji = get_payment_status_emoji(days_until)

    for group in groups:
        days_until = calculate_days_until(group.next_payment_date)
        status_emoji = get_payment_status_emoji(days_until)

        groups_text += (
            f"{status_emoji} **{group.group_name}** (ID: {group.display_id})\n"
            f"📅 Текущая дата платежа: {format_date(group.next_payment_date)}\n"
            f"📊 Статус: {get_payment_status_text(days_until)}\n\n"
        )

    groups_text += "Пожалуйста, введите название группы или ID (например: 'spotify 001' или '001'):"

    await message.answer(groups_text, parse_mode="Markdown")
    await state.set_state(AdminStates.updating_due_date_group)


@admin_router.callback_query(F.data == "admin_view_groups")
async def view_groups(callback: types.CallbackQuery):
    """View all payment groups with fill status summary - page 0"""
    await view_groups_page(callback, page=0)


@admin_router.callback_query(F.data.startswith("view_groups_page_"))
async def view_groups_page_handler(callback: types.CallbackQuery):
    """Handle pagination for view groups"""
    page = int(callback.data.split("_")[-1])
    await view_groups_page(callback, page)


async def view_groups_page(callback: types.CallbackQuery, page: int = 0):
    """View all payment groups with fill status summary"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    groups = await db.get_all_groups()

    if not groups:
        await callback.message.edit_text("📭 Группы оплаты не найдены.")
        return

    # Pagination settings
    GROUPS_PER_PAGE = 5
    total_groups = len(groups)
    total_pages = (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE

    # Validate page number
    if page < 0 or page >= total_pages:
        page = 0

    # Get groups for current page
    start_idx = page * GROUPS_PER_PAGE
    end_idx = min(start_idx + GROUPS_PER_PAGE, total_groups)
    page_groups = groups[start_idx:end_idx]

    response = f"👥 <b>Все группы оплаты с участниками (стр. {page + 1}/{total_pages}):</b>\n\n"

    for group in page_groups:
        # Get members for this group
        members = await db.get_group_members(group.group_id)
        total_members = len(members) if members else 0

        # Count members who have paid (not overdue)
        paid_count = 0
        if members:
            for member in members:
                if not member['is_overdue']:
                    paid_count += 1

        # Determine emoji based on member payment status
        if total_members == 0:
            emoji = "📭"  # Empty group
        elif paid_count == total_members:
            emoji = "✅"  # All paid
        elif paid_count == 0:
            emoji = "❌"  # None paid
        else:
            emoji = "⚠️"  # Partially paid

        response += (
            f"{emoji} <b>Группа: {group.group_name}</b> (ID: {group.display_id})\n"
            f"📅 Следующий платёж: {format_date(group.next_payment_date)}\n"
            f"👥 {paid_count}/{total_members}\n\n"
        )

    # Add pagination keyboard
    keyboard = get_pagination_keyboard(
        page, total_pages, "view_groups", show_back=True)

    await callback.message.edit_text(response, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data == "admin_import_groups")
async def import_groups_start(callback: types.CallbackQuery, state: FSMContext):
    """Start group import process"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.importing_groups_file)
    await callback.message.edit_text(
        "📊 **Импорт групп из Excel файла**\n\n"
        "Отправьте Excel файл (.xlsx) с данными для импорта групп.\n\n"
        "**Формат файла:**\n"
        "• Столбец A: Названия групп (например: spotify 001)\n"
        "• Столбец B: ID групп (например: 001)\n\n"
        "**Пример:**\n"
        "```\n"
        "spotify 001 | 001\n"
        "spotify 002 | 002\n"
        "```\n\n"
        "📎 Прикрепите файл к следующему сообщению или используйте /admin для отмены:",
        parse_mode="Markdown"
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_create_group")
async def create_group_start(callback: types.CallbackQuery, state: FSMContext):
    """Start group creation process"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "➕ **Создать новую группу**\n\n"
        "Пожалуйста, введите название группы:\n\n"
        "💡 *Используйте /admin для отмены операции*",
        parse_mode="Markdown"
    )

    await state.set_state(AdminStates.creating_group)
    await callback.answer()


@admin_router.message(StateFilter(AdminStates.creating_group))
async def create_group_finish(message: types.Message, state: FSMContext):
    """Ask for next payment date after group name"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    group_name = message.text.strip()

    if not group_name:
        await message.answer(
            "❌ Название группы не может быть пустым. "
            "Пожалуйста, попробуйте снова."
        )
        return

    # Check if group already exists (either by full name or display ID)
    existing_group = await db.get_group_by_name_or_id(group_name)
    if existing_group:
        await message.answer(
            f"❌ Группа с таким названием или ID уже существует:\n\n"
            f"👥 Название: <b>{existing_group.group_name}</b>\n"
            f"🆔 ID: <b>{existing_group.display_id}</b>\n\n"
            f"Пожалуйста, введите другое название или используйте /admin для отмены.",
            parse_mode="HTML"
        )
        return

    # Save group name and ask for next payment date
    await state.update_data(group_name=group_name)

    await message.answer(
        f"✅ Название группы: <b>{group_name}</b>\n\n"
        f"📅 Теперь введите дату следующего платежа:\n\n"
        f"Формат: <b>ДД.ММ.ГГГГ</b>\n"
        f"Пример: <code>15.01.2026</code>\n\n"
        f"💡 Или отправьте <code>+30</code> для автоматической даты "
        f"(через 30 дней)\n\n"
        f"<i>Используйте /admin для отмены</i>",
        parse_mode="HTML"
    )

    await state.set_state(AdminStates.creating_group_date)


@admin_router.message(StateFilter(AdminStates.creating_group_date))
async def create_group_with_date(message: types.Message, state: FSMContext):
    """Create group with specified next payment date"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    date_input = message.text.strip()

    # Handle automatic date (+30)
    if date_input == "+30":
        next_payment_date = get_now() + timedelta(days=30)
    else:
        # Parse date in DD.MM.YYYY format
        try:
            next_payment_date = datetime.strptime(date_input, "%d.%m.%Y")

            # Check if date is not in the past
            if next_payment_date.date() <= get_now().date():
                await message.answer(
                    "❌ Дата не может быть в прошлом или сегодня. "
                    "Пожалуйста, введите будущую дату."
                )
                return
        except ValueError:
            await message.answer(
                f"❌ Неверный формат даты.\n\n"
                f"Используйте формат <b>ДД.ММ.ГГГГ</b>\n"
                f"Пример: <code>15.01.2026</code>\n\n"
                f"Или отправьте <code>+30</code> для автоматической даты",
                parse_mode="HTML"
            )
            return

    # Get saved group name
    data = await state.get_data()
    group_name = data.get('group_name')

    # Create group
    group_id = await db.create_group(group_name, next_payment_date)

    if group_id:
        # Get the created group to show display_id
        created_groups = await db.get_all_groups() if db else []
        created_group = next(
            (g for g in created_groups if g.group_id == group_id), None
        )

        await message.answer(
            f"✅ <b>Группа успешно создана!</b>\n\n"
            f"👥 Название группы: {created_group.group_name if created_group else 'N/A'}\n"
            f"🆔 ID группы: {created_group.display_id if created_group else 'N/A'}\n"
            f"📅 Дата следующего платежа: {format_date(next_payment_date)}",
            parse_mode="HTML"
        )
        logger.info(
            f"Admin {user_id} created group (ID: {group_id}) "
            f"with payment date {format_date(next_payment_date)}"
        )
    else:
        await message.answer(
            "❌ Не удалось создать группу. Пожалуйста, попробуйте снова."
        )

    await state.clear()


@admin_router.callback_query(F.data == "admin_add_user")
async def add_user_start(callback: types.CallbackQuery, state: FSMContext):
    """Start user addition process"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "👤 **Добавить пользователя в группу**\n\n"
        "Пожалуйста, введите Telegram ID пользователя (числовой):\n\n"
        "💡 *Используйте /admin для отмены операции*",
        parse_mode="Markdown"
    )

    await state.set_state(AdminStates.adding_user_username)
    await callback.answer()


@admin_router.message(StateFilter(AdminStates.adding_user_username))
async def add_user_get_group(message: types.Message, state: FSMContext):
    """Get group for user addition"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Please enter a valid numeric user ID.")
        return

    await state.update_data(target_user_id=target_user_id)

    # Show available groups
    groups = await db.get_all_groups()
    if not groups:
        await message.answer("❌ No groups available. Create a group first.")
        await state.clear()
        return

    groups_text = "Available groups:\n\n"
    for group in groups:
        groups_text += f"• {group.group_name} (ID: {group.group_id})\n"

    await message.answer(
        f"{groups_text}\n"
        f"Please enter the group name to add user {target_user_id} to:\n\n"
        f"💡 *Используйте /admin для отмены операции*",
        parse_mode="Markdown"
    )

    await state.set_state(AdminStates.adding_user_group)


@admin_router.message(StateFilter(AdminStates.adding_user_group))
async def add_user_finish(message: types.Message, state: FSMContext):
    """Finish user addition"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    group_name = message.text.strip()

    # Get group
    group = await db.get_group_by_name(group_name)
    if not group:
        await message.answer(f"❌ Group '{group_name}' not found.")
        await state.clear()
        return

    # Add user (this will create user record if it doesn't exist)
    await db.add_user(target_user_id, f"user_{target_user_id}", None)

    # Add user to group
    success = await db.add_user_to_group(target_user_id, group.group_id)

    if success:
        await message.answer(
            f"✅ **User Added Successfully!**\n\n"
            f"👤 User ID: {target_user_id}\n"
            f"👥 Group: {group_name}\n"
            f"📅 Next payment due: {format_date(group.next_payment_date)}",
            parse_mode="Markdown"
        )
        logger.info(
            f"Admin {user_id} added user {target_user_id} to group '{group_name}'")
    else:
        await message.answer("❌ Failed to add user to group. Please try again.")

    await state.clear()


@admin_router.callback_query(F.data == "admin_update_due_date")
async def update_due_date_start(callback: types.CallbackQuery, state: FSMContext):
    """Start due date update process"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Access denied", show_alert=True)
        return

    # Show available groups
    groups = await db.get_all_groups()
    if not groups:
        await callback.message.edit_text("❌ No groups available.")
        return

    groups_text = "📅 **Update Group Due Date**\n\n"
    groups_text += "Available groups:\n\n"

    for group in groups:
        days_until = calculate_days_until(group.next_payment_date)
        status_emoji = get_payment_status_emoji(days_until)

        groups_text += (
            f"{status_emoji} **{group.group_name}** (ID: {group.group_id})\n"
            f"📅 Current due date: {format_date(group.next_payment_date)}\n"
            f"📊 Status: {get_payment_status_text(days_until)}\n\n"
        )

    groups_text += "Please enter the group name you want to update:\n\n"
    groups_text += "💡 *Используйте /admin для отмены операции*"

    await callback.message.edit_text(groups_text, parse_mode="Markdown")
    await state.set_state(AdminStates.updating_due_date_group)
    await callback.answer()


@admin_router.message(StateFilter(AdminStates.updating_due_date_group))
async def update_due_date_get_date(message: types.Message, state: FSMContext):
    """Get new due date for the group"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    identifier = message.text.strip()

    # Get group by name or display ID
    group = await db.get_group_by_name_or_id(identifier)
    if not group:
        await message.answer(f"❌ Группа '{identifier}' не найдена. Пожалуйста, попробуйте снова.")
        return

    await state.update_data(group=group)

    await message.answer(
        f"📅 **Update Due Date for {group.group_name}**\n\n"
        f"Current due date: {format_date(group.next_payment_date)}\n\n"
        f"Please enter the new due date in format: **YYYY-MM-DD**\n\n"
        f"Examples:\n"
        f"• `2025-11-15` (November 15, 2025)\n"
        f"• `2025-12-01` (December 1, 2025)\n\n"
        f"💡 *Используйте /admin для отмены операции*",
        parse_mode="Markdown"
    )

    await state.set_state(AdminStates.updating_due_date_date)


@admin_router.message(StateFilter(AdminStates.updating_due_date_date))
async def update_due_date_finish(message: types.Message, state: FSMContext):
    """Finish due date update"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    if message.text.strip().lower() == 'cancel':
        await message.answer("❌ Due date update cancelled.")
        await state.clear()
        return

    data = await state.get_data()
    group = data.get('group')

    if not group:
        await message.answer("❌ Error: Group data not found. Please start over.")
        await state.clear()
        return

    # Parse the date
    try:
        from datetime import datetime
        date_str = message.text.strip()
        new_due_date = datetime.strptime(date_str, "%d.%m.%Y")

        # Check if date is not in the past or today
        if new_due_date.date() <= get_now().date():
            await message.answer("❌ Due date cannot be in the past or today. Please enter a future date.")
            return

    except ValueError:
        await message.answer(
            "❌ Invalid date format. Please use <b>DD.MM.YYYY</b> format.\n\n"
            "Example: 15.11.2025",
            parse_mode="HTML"
        )
        return

    # Update the due date
    success = await db.update_group_due_date(group.group_id, new_due_date)

    if success:
        days_until = calculate_days_until(new_due_date)
        status_emoji = get_payment_status_emoji(days_until)

        await message.answer(
            f"✅ **Due Date Updated Successfully!**\n\n"
            f"👥 Group: {group.group_name}\n"
            f"📅 Old due date: {format_date(group.next_payment_date)}\n"
            f"📅 New due date: {format_date(new_due_date.date())}\n"
            f"{status_emoji} Status: {get_payment_status_text(days_until)}\n\n"
            f"💡 All users in this group will now be reminded based on the new date.",
            parse_mode="Markdown"
        )

        logger.info(
            f"Admin {user_id} updated due date for group '{group.group_name}' from {group.next_payment_date} to {new_due_date.date()}")
    else:
        await message.answer(
            "❌ Failed to update due date. Please try again or check the logs for errors."
        )

    await state.clear()


@admin_router.callback_query(F.data == "admin_stats")
async def view_statistics(callback: types.CallbackQuery):
    """Show ordering selection for statistics"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    # Create ordering selection keyboard
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 По ID (001, 002, ...)", callback_data="admin_stats_order_id")
    builder.button(text="📅 По дате платежа (01-28)", callback_data="admin_stats_order_date")
    builder.button(text="🔙 Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        "📈 <b>Статистика - Выберите порядок сортировки:</b>\n\n"
        "📋 <b>По ID</b> - группы будут отсортированы по их идентификатору (001, 002, 003, ...)\n\n"
        "📅 <b>По дате платежа</b> - группы будут отсортированы по дню оплаты в месяце (01-28), затем по ID",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin_stats_order_id")
async def view_statistics_by_id(callback: types.CallbackQuery):
    """View statistics ordered by ID - page 0"""
    await view_statistics_page(callback, page=0, order_by="id")


@admin_router.callback_query(F.data == "admin_stats_order_date")
async def view_statistics_by_date(callback: types.CallbackQuery):
    """View statistics ordered by payment date - page 0"""
    await view_statistics_page(callback, page=0, order_by="date")


@admin_router.callback_query(F.data.startswith("admin_stats_page_"))
async def view_statistics_page_handler(callback: types.CallbackQuery):
    """Handle pagination for statistics"""
    # Format: admin_stats_page_{order}_{page}
    parts = callback.data.split("_")
    order_by = parts[3]  # 'id' or 'date'
    page = int(parts[4])
    await view_statistics_page(callback, page, order_by)


async def view_statistics_page(callback: types.CallbackQuery, page: int = 0, order_by: str = "date"):
    """View detailed statistics with all payment groups and members"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    # Get groups with selected ordering
    if order_by == "id":
        groups = await db.get_all_groups()
        order_text = "по ID"
    else:  # date
        groups = await db.get_all_groups_for_statistics()
        order_text = "по дате платежа"

    if not groups:
        await callback.message.edit_text("📭 Группы оплаты не найдены.")
        return

    # Pagination settings
    # Fewer groups per page for statistics (more detailed info)
    GROUPS_PER_PAGE = 3
    total_groups = len(groups)
    total_pages = (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE

    # Validate page number
    if page < 0 or page >= total_pages:
        page = 0

    # Get groups for current page
    start_idx = page * GROUPS_PER_PAGE
    end_idx = min(start_idx + GROUPS_PER_PAGE, total_groups)
    page_groups = groups[start_idx:end_idx]

    response = f"📈 <b>Статистика ({order_text}) - Все группы оплаты с участниками (стр. {page + 1}/{total_pages}):</b>\n\n"

    for group in page_groups:
        # Get members for this group
        members = await db.get_group_members(group.group_id)
        total_members = len(members) if members else 0

        # Count members who have paid (not overdue)
        paid_count = 0
        if members:
            for member in members:
                if not member['is_overdue']:
                    paid_count += 1

        # Determine emoji based on member payment status
        if total_members == 0:
            emoji = "📭"  # Empty group
        elif paid_count == total_members:
            emoji = "✅"  # All paid
        elif paid_count == 0:
            emoji = "❌"  # None paid
        else:
            emoji = "⚠️"  # Partially paid

        response += (
            f"{emoji} <b>Группа: {group.group_name}</b> (ID: {group.display_id})\n"
            f"📅 Следующий платёж: {format_date(group.next_payment_date)}\n"
        )

        if members:
            response += f"👥 <b>Участники ({len(members)}):</b>\n"
            for idx, member in enumerate(members, 1):
                first_name = member['first_name'] or 'N/A'
                username_display = f"@{member['username']}" if member['username'] else 'нет username'

                # Determine payment status
                if member['is_overdue']:
                    status = "❌ Просрочено"
                else:
                    member_days = (
                        member['next_payment_date'] - get_now().date()).days
                    if member_days <= 3:
                        status = "⚠️ Скоро срок"
                    else:
                        status = "✅ Оплачено"

                response += (
                    f"   {idx}. {first_name} - {username_display} - {status}\n"
                )
        else:
            response += "👥 <i>Участников нет</i>\n"

        response += "\n"

    # Add enhanced pagination keyboard with jump-by-4 buttons
    from bot.utils.keyboards import get_statistics_pagination_keyboard
    keyboard = get_statistics_pagination_keyboard(page, total_pages, order_by)
    
    await callback.message.edit_text(response, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data == "admin_test_notifications")
async def handle_test_notifications(callback: types.CallbackQuery):
    """Handle test notifications request"""
    user_id = callback.from_user.id

    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("❌ Access denied.", show_alert=True)
        return

    await callback.answer("🔔 Testing notifications...")

    # Import here to avoid circular imports
    from bot.utils.notifications import NotificationScheduler

    if not db or not db.pool:
        await callback.message.edit_text(
            "❌ **Test Notifications Failed**\n\n"
            "Database is not available.",
            parse_mode="Markdown"
        )
        return

    try:
        # Create a temporary scheduler for testing
        test_scheduler = NotificationScheduler(callback.bot, db, settings)

        await callback.message.edit_text(
            "🧪 **Running Test Notifications**\n\n"
            "Checking for users needing reminders and overdue warnings...\n"
            "This may take a few seconds.",
            parse_mode="Markdown"
        )

        # Run the notification checks
        await test_scheduler.send_test_notifications()

        await callback.message.edit_text(
            "✅ **Test Notifications Complete**\n\n"
            "Check the bot logs for details about sent notifications.\n\n"
            "💡 **Note:** Notifications are sent automatically daily at 9:00 AM.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ Test notifications failed: {e}")
        await callback.message.edit_text(
            f"❌ **Test Notifications Failed**\n\n"
            f"Error: {str(e)}\n\n"
            f"Check the bot logs for more details.",
            parse_mode="Markdown"
        )


@admin_router.message(Command("test_receipt_storage"))
async def test_receipt_storage_command(message: types.Message):
    """Test receipt storage configuration"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not settings.tg_receipt_storage_chat_id:
        await message.answer(
            "⚠️ **Хранилище чеков не настроено**\n\n"
            "Для включения функции хранения чеков:\n"
            "1. Создайте приватный чат/группу для хранения чеков\n"
            "2. Получите Chat ID этого чата\n"
            "3. Добавьте в .env файл:\n"
            "`TG_RECEIPT_STORAGE_CHAT_ID=ваш_chat_id`\n\n"
            "📖 Подробные инструкции в файле RECEIPT_STORAGE_README.md",
            parse_mode="Markdown"
        )
        return

    try:
        # Test sending message to storage chat
        test_message = (
            f"🧪 <b>ТЕСТ ХРАНИЛИЩА ЧЕКОВ</b>\n\n"
            f"✅ Соединение с хранилищем чеков успешно!\n"
            f"📅 Время теста: {format_datetime(get_now())}\n"
            f"👤 Инициатор: {message.from_user.first_name} (ID: {message.from_user.id})\n\n"
            f"💡 Это тестовое сообщение для проверки настроек."
        )

        await message.bot.send_message(
            chat_id=settings.tg_receipt_storage_chat_id,
            text=test_message,
            parse_mode="HTML"
        )

        await message.answer(
            f"✅ **Тест хранилища чеков прошёл успешно!**\n\n"
            f"📊 Chat ID: `{settings.tg_receipt_storage_chat_id}`\n"
            f"📨 Тестовое сообщение отправлено в хранилище\n\n"
            f"🔧 Все новые чеки будут автоматически пересылаться в это хранилище.",
            parse_mode="Markdown"
        )

        logger.info(
            f"Receipt storage test successful for chat_id: {settings.tg_receipt_storage_chat_id}")

    except Exception as e:
        error_msg = str(e)
        await message.answer(
            f"❌ **Ошибка теста хранилища чеков**\n\n"
            f"📊 Chat ID: `{settings.tg_receipt_storage_chat_id}`\n"
            f"⚠️ Ошибка: {error_msg}\n\n"
            f"**Возможные причины:**\n"
            f"• Неверный Chat ID\n"
            f"• Бот не добавлен в целевой чат\n"
            f"• Нет прав на отправку сообщений\n"
            f"• Чат заблокирован или удалён\n\n"
            f"📖 Проверьте инструкции в RECEIPT_STORAGE_README.md",
            parse_mode="Markdown"
        )

        logger.error(f"Receipt storage test failed: {e}")


@admin_router.message(Command("import_groups"))
async def import_groups_command(message: types.Message, state: FSMContext):
    """Handle /import_groups command for bulk group creation from Excel"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await message.answer("❌ Доступ запрещён. Команда только для администраторов.")
        return

    if not db or not db.pool:
        await message.answer("❌ База данных в настоящее время недоступна.")
        return

    await state.set_state(AdminStates.importing_groups_file)
    await message.answer(
        "📊 **Импорт групп из Excel файла**\n\n"
        "Отправьте Excel файл (.xlsx) с данными для импорта групп.\n\n"
        "**Формат файла:**\n"
        "• Столбец A: Названия групп (например: spotify 001)\n"
        "• Столбец B: ID групп (например: 001)\n\n"
        "**Пример:**\n"
        "```\n"
        "spotify 001 | 001\n"
        "spotify 002 | 002\n"
        "```\n\n"
        "📎 Прикрепите файл к следующему сообщению:",
        parse_mode="Markdown"
    )


@admin_router.message(AdminStates.importing_groups_file, F.document)
async def handle_import_file(message: types.Message, state: FSMContext):
    """Handle Excel file upload for group import"""
    file_path = None
    try:
        if not message.document:
            await message.answer("❌ Пожалуйста, отправьте файл.")
            return

        # Check file extension
        file_name = message.document.file_name
        if not file_name or not file_name.lower().endswith('.xlsx'):
            await message.answer(
                "❌ Неподдерживаемый формат файла.\n"
                "Пожалуйста, отправьте Excel файл (.xlsx)."
            )
            return

        # Check file size (limit to 10MB)
        if message.document.file_size > 10 * 1024 * 1024:
            await message.answer("❌ Файл слишком большой. Максимальный размер: 10 MB.")
            return

        await message.answer("⏳ Обработка файла...")

        # Download file
        file = await message.bot.get_file(message.document.file_id)

        # Create temporary file with proper extension
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as temp_file:
            file_path = temp_file.name

        await message.bot.download_file(file.file_path, file_path)

        # Parse Excel file
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(file_path)
            sheet = workbook.active

            groups_data = []
            errors = []

            for row_num, row in enumerate(sheet.iter_rows(min_row=1, values_only=True), 1):
                if not row or len(row) < 2:
                    continue

                group_name = str(row[0]).strip() if row[0] else ""
                group_id = str(row[1]).strip() if row[1] else ""

                # Skip header row (if first row contains non-numeric ID)
                if row_num == 1 and not group_id.isdigit():
                    continue

                if not group_name or not group_id:
                    errors.append(f"Строка {row_num}: пустые данные")
                    continue

                # Validate group ID format (should be 3 digits)
                if not group_id.isdigit() or len(group_id) != 3:
                    errors.append(
                        f"Строка {row_num}: ID должен быть 3-значным числом")
                    continue

                groups_data.append({
                    'name': group_name,
                    'display_id': group_id,  # Keep as string for VARCHAR(3)
                    'row': row_num
                })

            if not groups_data:
                await message.answer("❌ В файле не найдено валидных данных для импорта.")
                await state.clear()
                return

            # Check for duplicate IDs in file
            display_ids = [group['display_id'] for group in groups_data]
            if len(display_ids) != len(set(display_ids)):
                errors.append("Обнаружены дублирующиеся ID в файле")

            # Check for existing groups in database
            existing_display_ids = []
            for group_data in groups_data:
                existing_group = await db.get_group_by_display_id(group_data['display_id'])
                if existing_group:
                    existing_display_ids.append(group_data['display_id'])

            if existing_display_ids:
                errors.append(
                    f"ID уже существуют в базе: {', '.join(map(str, existing_display_ids))}")

            # Store data for confirmation
            await state.update_data(groups_data=groups_data, errors=errors)

            # Show preview
            preview_text = "📋 **Предварительный просмотр импорта:**\n\n"
            preview_text += f"✅ Найдено групп для импорта: {len(groups_data)}\n\n"

            if errors:
                preview_text += f"⚠️ **Ошибки ({len(errors)}):**\n"
                for error in errors[:5]:  # Show first 5 errors
                    preview_text += f"• {error}\n"
                if len(errors) > 5:
                    preview_text += f"• ... и ещё {len(errors) - 5} ошибок\n"
                preview_text += "\n"

            if groups_data and not errors:
                preview_text += "**Группы для создания:**\n"
                for i, group in enumerate(groups_data[:10]):  # Show first 10
                    preview_text += f"• {group['name']} (ID: {group['display_id']})\n"
                if len(groups_data) > 10:
                    preview_text += f"• ... и ещё {len(groups_data) - 10} групп\n"

                await state.set_state(AdminStates.importing_groups_confirm)

                # Create custom keyboard for import confirmation
                builder = InlineKeyboardBuilder()
                builder.row(
                    types.InlineKeyboardButton(
                        text="✅ Да", callback_data="confirm_yes"),
                    types.InlineKeyboardButton(
                        text="❌ Нет", callback_data="confirm_no")
                )

                await message.answer(
                    preview_text,
                    parse_mode="Markdown",
                    reply_markup=builder.as_markup()
                )
            else:
                await message.answer(
                    preview_text + "\n❌ Импорт невозможен из-за ошибок в данных.",
                    parse_mode="Markdown"
                )
                await state.clear()

        except Exception as e:
            logger.error(f"Error parsing Excel file: {e}")
            await message.answer(
                "❌ Ошибка при обработке файла.\n"
                "Убедитесь, что файл не повреждён и соответствует требуемому формату."
            )
            await state.clear()

        finally:
            # Clean up temp file
            if file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except Exception as cleanup_error:
                    logger.warning(
                        f"Failed to cleanup temp file {file_path}: {cleanup_error}")

    except Exception as e:
        logger.error(f"Error in handle_import_file: {e}")
        await message.answer("❌ Произошла ошибка при обработке файла.")
        await state.clear()
        # Cleanup file if it exists
        if 'file_path' in locals() and file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except:
                pass


@admin_router.message(AdminStates.importing_groups_file)
async def handle_import_file_invalid(message: types.Message):
    """Handle invalid file uploads during import"""
    await message.answer(
        "❌ Пожалуйста, отправьте Excel файл (.xlsx).\n"
        "Или используйте /admin для отмены операции."
    )


@admin_router.callback_query(AdminStates.importing_groups_confirm, F.data == "confirm_yes")
async def confirm_import_groups(callback: types.CallbackQuery, state: FSMContext):
    """Confirm and execute group import"""
    try:
        data = await state.get_data()
        groups_data = data.get('groups_data', [])

        if not groups_data:
            await callback.message.edit_text("❌ Данные для импорта не найдены.")
            await state.clear()
            return

        await callback.message.edit_text("⏳ Импорт групп в процессе...")

        # Import groups
        success_count = await db.bulk_import_groups(groups_data)

        await callback.message.edit_text(
            f"✅ **Импорт завершён!**\n\n"
            f"Успешно создано групп: {success_count} из {len(groups_data)}\n\n"
            f"Используйте /admin для управления группами.",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error in confirm_import_groups: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при импорте групп.")

    finally:
        await state.clear()


@admin_router.callback_query(AdminStates.importing_groups_confirm, F.data == "confirm_no")
async def cancel_import_groups(callback: types.CallbackQuery, state: FSMContext):
    """Cancel group import"""
    await callback.message.edit_text("❌ Импорт групп отменён.")
    await state.clear()


@admin_router.callback_query(F.data == "admin_delete_group")
async def delete_group_start(callback: types.CallbackQuery, state: FSMContext):
    """Start group deletion process - page 0"""
    await delete_group_page(callback, state, page=0)


@admin_router.callback_query(F.data.startswith("delete_group_page_"))
async def delete_group_page_handler(callback: types.CallbackQuery, state: FSMContext):
    """Handle pagination for delete group"""
    page = int(callback.data.split("_")[-1])
    await delete_group_page(callback, state, page)


async def delete_group_page(callback: types.CallbackQuery, state: FSMContext, page: int = 0):
    """Show paginated group list for deletion"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    # Show available groups
    groups = await db.get_all_groups()
    if not groups:
        await callback.message.edit_text("❌ Нет доступных групп для удаления.")
        return

    # Pagination settings
    GROUPS_PER_PAGE = 5
    total_groups = len(groups)
    total_pages = (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE

    # Validate page number
    if page < 0 or page >= total_pages:
        page = 0

    # Get groups for current page
    start_idx = page * GROUPS_PER_PAGE
    end_idx = min(start_idx + GROUPS_PER_PAGE, total_groups)
    page_groups = groups[start_idx:end_idx]

    groups_text = f"🗑️ **Удаление группы (стр. {page + 1}/{total_pages})**\n\n"
    groups_text += "⚠️ **ВНИМАНИЕ**: Удаление группы необратимо!\n"
    groups_text += "Будут удалены:\n• Все участники группы\n• История платежей\n• Все связанные данные\n\n"
    groups_text += "Доступные группы:\n\n"

    for group in page_groups:
        # Get member count
        members = await db.get_group_members(group.group_id)
        member_count = len(members)

        groups_text += (
            f"🏷️ **{group.group_name}** (ID: {group.display_id})\n"
            f"👥 Участников: {member_count}\n"
            f"📅 Следующий платёж: {format_date(group.next_payment_date)}\n\n"
        )

    groups_text += "Введите название группы или ID для удаления (например: 'spotify 001' или '001'):\n\n"
    groups_text += "💡 *Используйте /admin для отмены операции*"

    # Add pagination keyboard
    keyboard = get_pagination_keyboard(
        page, total_pages, "delete_group", show_back=True)

    await callback.message.edit_text(groups_text, parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(AdminStates.deleting_group_select)
    await callback.answer()


@admin_router.message(StateFilter(AdminStates.deleting_group_select))
async def delete_group_confirm(message: types.Message, state: FSMContext):
    """Confirm group deletion"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    identifier = message.text.strip()

    # Find the group by name or display ID
    group = await db.get_group_by_name_or_id(identifier)
    if not group:
        await message.answer(
            f"❌ Группа '{identifier}' не найдена.\n\n"
            f"Проверьте название или ID и попробуйте снова, или используйте /admin для отмены."
        )
        return

    # Get members for confirmation
    members = await db.get_group_members(group.group_id)
    member_count = len(members)

    # Store group info for confirmation
    await state.update_data(group=group, member_count=member_count)

    confirmation_text = (
        f"⚠️ **ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ**\n\n"
        f"Вы действительно хотите удалить группу?\n\n"
        f"🏷️ **Группа**: {group.group_name} (ID: {group.display_id})\n"
        f"👥 **Участников**: {member_count}\n"
        f"📅 **Дата платежа**: {format_date(group.next_payment_date)}\n\n"
        f"🚨 **ЭТО ДЕЙСТВИЕ НЕОБРАТИМО!**\n"
        f"Будут удалены:\n"
        f"• Все {member_count} участников\n"
        f"• Вся история платежей\n"
        f"• Все связанные данные\n\n"
        f"Вы уверены?"
    )

    # Create confirmation keyboard
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(
            text="🗑️ ДА, УДАЛИТЬ", callback_data="confirm_delete_group"),
        types.InlineKeyboardButton(
            text="❌ Отменить", callback_data="cancel_delete_group")
    )

    await message.answer(
        confirmation_text,
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )

    await state.set_state(AdminStates.deleting_group_confirm)


@admin_router.callback_query(AdminStates.deleting_group_confirm, F.data == "confirm_delete_group")
async def execute_group_deletion(callback: types.CallbackQuery, state: FSMContext):
    """Execute group deletion"""
    try:
        data = await state.get_data()
        group = data.get('group')
        member_count = data.get('member_count', 0)

        if not group:
            await callback.message.edit_text("❌ Данные группы не найдены.")
            await state.clear()
            return

        await callback.message.edit_text("⏳ Удаление группы...")

        # Delete the group
        success = await db.delete_group(group.group_id)

        if success:
            await callback.message.edit_text(
                f"✅ <b>Группа успешно удалена!</b>\n\n"
                f"🗑️ Удалена группа: {group.group_name} (ID: {group.display_id})\n"
                f"👥 Удалено участников: {member_count}\n"
                f"📅 Время удаления: {format_datetime(get_now())}\n\n"
                f"Все связанные данные были удалены из базы данных.",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                f"❌ **Ошибка при удалении группы**\n\n"
                f"Не удалось удалить группу {group.group_name}.\n"
                f"Пожалуйста, попробуйте позже или обратитесь к техподдержке."
            )

    except Exception as e:
        logger.error(f"Error in execute_group_deletion: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при удалении группы.")

    finally:
        await state.clear()


@admin_router.callback_query(AdminStates.deleting_group_confirm, F.data == "cancel_delete_group")
async def cancel_group_deletion(callback: types.CallbackQuery, state: FSMContext):
    """Cancel group deletion"""
    await callback.message.edit_text("❌ Удаление группы отменено.")
    await state.clear()


@admin_router.callback_query(F.data == "admin_manage_members")
async def manage_members_start(callback: types.CallbackQuery, state: FSMContext):
    """Start member management - page 0"""
    await manage_members_page(callback, state, page=0)


@admin_router.callback_query(F.data.startswith("manage_members_page_"))
async def manage_members_page_handler(callback: types.CallbackQuery, state: FSMContext):
    """Handle pagination for manage members"""
    page = int(callback.data.split("_")[-1])
    await manage_members_page(callback, state, page)


async def manage_members_page(callback: types.CallbackQuery, state: FSMContext, page: int = 0):
    """Show paginated group list for member management"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    groups = await db.get_all_groups()
    if not groups:
        await callback.message.edit_text("❌ Нет доступных групп.")
        return

    # Pagination settings
    GROUPS_PER_PAGE = 5
    total_groups = len(groups)
    total_pages = (total_groups + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE

    # Validate page number
    if page < 0 or page >= total_pages:
        page = 0

    # Get groups for current page
    start_idx = page * GROUPS_PER_PAGE
    end_idx = min(start_idx + GROUPS_PER_PAGE, total_groups)
    page_groups = groups[start_idx:end_idx]

    groups_text = f"👥 **Управление участниками групп (стр. {page + 1}/{total_pages})**\n\n"
    groups_text += "Выберите группу для просмотра участников:\n\n"

    for group in page_groups:
        members = await db.get_group_members(group.group_id)
        member_count = len(members)

        groups_text += (
            f"🏷️ **{group.group_name}** (ID: {group.display_id})\n"
            f"👥 Участников: {member_count}\n\n"
        )

    groups_text += "Введите название группы или ID (например: 'spotify 001' или '001'):\n\n"
    groups_text += "💡 *Используйте /admin для отмены операции*"

    # Add pagination keyboard
    keyboard = get_pagination_keyboard(
        page, total_pages, "manage_members", show_back=True)

    await callback.message.edit_text(groups_text, parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(AdminStates.removing_user_select_group)
    await callback.answer()


@admin_router.message(StateFilter(AdminStates.removing_user_select_group))
async def show_group_members(message: types.Message, state: FSMContext):
    """Show group members and management options"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    identifier = message.text.strip()

    group = await db.get_group_by_name_or_id(identifier)
    if not group:
        await message.answer(
            f"❌ Группа '{identifier}' не найдена.\n\n"
            f"Проверьте название или ID и попробуйте снова, или используйте /admin для отмены."
        )
        return

    members = await db.get_group_members(group.group_id)

    if not members:
        await message.answer(
            f"📭 **Группа {group.group_name} пуста**\n\n"
            f"В этой группе пока нет участников."
        )
        await state.clear()
        return

    members_text = (
        f"👥 <b>Участники группы {group.group_name}</b>\n"
        f"🆔 ID группы: {group.display_id}\n\n"
    )

    for member in members:
        last_payment = "Никогда" if not member['last_payment'] else member['last_payment'].strftime(
            '%Y-%m-%d')
        username_display = f"@{member['username']}" if member['username'] else 'нет username'
        first_name = member['first_name'] or 'N/A'

        members_text += (
            f"👤 <b>{first_name}</b> ({username_display})\n"
            f"🆔 ID: {member['display_id']} | Telegram ID: <code>{member['user_id']}</code>\n"
            f"💳 Платежей: {member['total_payments']} | Последний: {last_payment}\n\n"
        )

    members_text += f"Всего участников: {len(members)}\n\n"
    members_text += "Для удаления пользователя из группы введите его Telegram ID:"

    await message.answer(members_text, parse_mode="HTML")
    await state.update_data(group=group)
    await state.set_state(AdminStates.removing_user_select_user)


@admin_router.message(StateFilter(AdminStates.removing_user_select_user))
async def remove_user_from_group(message: types.Message, state: FSMContext):
    """Remove selected user from group"""
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        return

    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат ID. Введите числовой Telegram ID пользователя.")
        return

    data = await state.get_data()
    group = data.get('group')

    if not group:
        await message.answer("❌ Данные группы не найдены. Попробуйте снова.")
        await state.clear()
        return

    # Check if user is in the group
    members = await db.get_group_members(group.group_id)
    target_member = next(
        (m for m in members if m['user_id'] == target_user_id), None)

    if not target_member:
        await message.answer(
            f"❌ Пользователь с ID {target_user_id} не найден в группе {group.group_name}."
        )
        return

    # Remove user from group
    success = await db.remove_user_from_group(target_user_id, group.group_id)

    if success:
        await message.answer(
            f"✅ <b>Пользователь удалён из группы!</b>\n\n"
            f"👤 Пользователь: {target_member['first_name']} (@{target_member['username'] or 'нет'})\n"
            f"🆔 ID: {target_member['display_id']}\n"
            f"🏷️ Группа: {group.group_name}\n"
            f"📅 Время удаления: {format_datetime(get_now())}",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"❌ **Ошибка при удалении пользователя**\n\n"
            f"Не удалось удалить пользователя из группы."
        )

    await state.clear()


@admin_router.callback_query(F.data == "back_to_admin_menu")
async def back_to_admin_menu(callback: types.CallbackQuery):
    """Return to admin main menu"""
    user_id = callback.from_user.id
    if not is_admin(user_id, settings.tg_admin_ids):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await callback.message.edit_text(
        "🔧 **Панель администратора**\n\n"
        "Выберите действие:",
        reply_markup=get_admin_main_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()


@admin_router.callback_query(F.data == "noop")
async def noop_handler(callback: types.CallbackQuery):
    """Handle no-operation callbacks (like page indicators)"""
    await callback.answer()


# Handle any unrecognized admin callback
@admin_router.callback_query(F.data.startswith("admin_"))
async def handle_unknown_admin_action(callback: types.CallbackQuery):
    """Handle unknown admin actions"""
    await callback.answer("This feature is not implemented yet.", show_alert=True)
