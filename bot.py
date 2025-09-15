# -*- coding: utf-8 -*-
import asyncio
from aiogram import F
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import WebAppInfo  # <-- для WebApp
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from datetime import datetime
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Список сотрудников: {Telegram ID: "Имя Фамилия"}
TEAM_MEMBERS = {
    693411047: "Баранов Антон",
    987654321: "Петрова Мария",
    555666777: "Сидоров Дмитрий"
}

# Подключение к Google Sheets — ИСПРАВЛЕНО!
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

# Открываем таблицу заказов
try:
    sheet = client.open("Заказы на участки").sheet1
except gspread.SpreadsheetNotFound:
    logger.error("Таблица 'Заказы на участки' не найдена.")
    raise Exception("❌ Таблица 'Заказы на участки' не найдена. Создайте её в Google Таблицах.")

# Создаём заголовки, если таблица пустая
if not sheet.get_all_values():
    sheet.append_row([
        "№ заказа", "Адрес", "Тип работы", "Срок", "Комментарий",
        "Приоритет", "Статус", "Ответственный", "Дата создания",
        "Начал работу", "Выполнил работу", "Сумма", "Способ оплаты",
        "Препарат", "Количество", "Площадь", "Фото чека"
    ])

# Открываем таблицу для учёта смен
try:
    shifts_sheet = client.open("Учёт смен").sheet1
except gspread.SpreadsheetNotFound:
    logger.error("Таблица 'Учёт смен' не найдена.")
    raise Exception("❌ Таблица 'Учёт смен' не найдена. Создайте её в Google Таблицах.")

if not shifts_sheet.get_all_values():
    shifts_sheet.append_row([
        "ID сотрудника", "Имя сотрудника", "Дата", "Начало смены",
        "Окончание смены", "Отработано (ч)", "Статус"
    ])

# Состояния для FSM
class OrderForm(StatesGroup):
    address = State()
    work_type = State()
    deadline = State()
    comment = State()
    priority = State()
    assignee = State()
    photo = State()
    amount = State()
    payment = State()
    receipt_photo = State()
    chemical = State()
    quantity = State()
    area = State()

# Генерация уникального номера заказа
def generate_order_id():
    records = sheet.get_all_values()
    if len(records) <= 1:
        return 1001
    last_id = int(records[-1][0]) if records[-1][0].isdigit() else 1000
    return last_id + 1

# Установка команд
async def set_bot_commands():
    try:
        admin_commands = [
            types.BotCommand(command="new", description="🆕 Создать заказ"),
            types.BotCommand(command="admin", description="👑 Меню администратора"),
            types.BotCommand(command="get_receipt", description="🧾 Просмотр чека"),
            types.BotCommand(command="cancel", description="🚫 Отменить действие"),
            types.BotCommand(command="start", description="🏠 Главное меню")
        ]
        worker_commands = [
            types.BotCommand(command="start", description="🏠 Главное меню"),
            types.BotCommand(command="orders", description="📋 Мои заказы"),
            types.BotCommand(command="shift_start", description="🕗 Начать смену"),
            types.BotCommand(command="shift_end", description="下班 Закончить смену"),
            types.BotCommand(command="shift_my", description="📊 Мои смены"),
            types.BotCommand(command="cancel", description="🚫 Отменить")
        ]
        for admin_id in ADMIN_IDS:
            await bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=admin_id))
        await bot.set_my_commands(worker_commands, scope=types.BotCommandScopeAllPrivateChats())
        logger.info("✅ Команды установлены")

    except Exception as e:
        logger.error(f"❌ Ошибка установки команд: {e}")

# Обработчик отмены FSM
@dp.message(Command("cancel"))
@dp.message(F.text.casefold() == "отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("🚫 Действие отменено.", reply_markup=types.ReplyKeyboardRemove())

# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        await show_admin_menu(message)
    else:
        await show_worker_menu(message)

@dp.message(Command("new"))
async def cmd_new(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 Только администратор может создавать заказы.")
        return
    await message.answer("📍 Введите адрес участка:")
    await state.set_state(OrderForm.address)

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 Доступ запрещён.")
        return
    await show_admin_menu(message)

@dp.message(Command("orders"))
async def view_all_orders(message: types.Message):
    try:
        user_id = message.from_user.id
        if user_id not in TEAM_MEMBERS:
            await message.answer("❌ Вы не зарегистрированы как сотрудник.")
            return
        records = sheet.get_all_records()
        my_orders = [
            f"🆕 #{r['№ заказа']} | {r['Адрес']} | {r['Тип работы']} | {r['Статус']}"
            for r in records
            if r['Ответственный'] == TEAM_MEMBERS[user_id] and r['Статус'] != "Выполнен"
        ]
        if my_orders:
            await message.answer("📋 Ваши назначенные заказы:\n" + "\n".join(my_orders))
        else:
            await message.answer("📭 У вас нет активных заказов.")
    except Exception as e:
        logger.error(f"Ошибка при просмотре заказов: {e}")
        await message.answer("❌ Ошибка сервера.")

@dp.message(Command("cancel"))
async def cancel_order(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 Только администратор может отменять заказы.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("❌ Укажите номер заказа: /cancel 1001")
            return
        order_id = int(parts[1])
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        cell = sheet.find(str(order_id))
        if not cell:
            await message.answer(f"❌ Заказ #{order_id} не найден.")
            return
        assignee_name = sheet.cell(cell.row, 8).value
        assignee_id = None
        for uid, name in TEAM_MEMBERS.items():
            if name == assignee_name:
                assignee_id = uid
                break
        if not assignee_id:
            await message.answer(f"❌ Не удалось найти сотрудника для заказа #{order_id}.")
            return
        sheet.update_cell(cell.row, 7, "Отменён")
        sheet.update_cell(cell.row, 11, f"Отменено {now}")
        try:
            await bot.send_message(assignee_id, f"🚫 Заказ #{order_id} отменён администратором.\n🕒 {now}")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение сотруднику: {e}")
        await message.answer(f"✅ Заказ #{order_id} отменён. Уведомление отправлено {assignee_name}.")
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте: /cancel 1001")
    except Exception as e:
        logger.error(f"Ошибка при отмене заказа: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(Command("get_receipt"))
async def get_receipt(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 Только администратор может просматривать чеки.")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("❌ Укажите номер заказа: /get_receipt 1001")
            return
        order_id = int(parts[1])
        cell = sheet.find(str(order_id))
        if not cell:
            await message.answer(f"❌ Заказ #{order_id} не найден.")
            return
        receipt_photo_id = sheet.cell(cell.row, 17).value
        if receipt_photo_id == "без чека" or not receipt_photo_id:
            await message.answer(f"🧾 Чек к заказу #{order_id} не прикреплён.")
        else:
            try:
                await bot.send_photo(message.chat.id, receipt_photo_id, caption=f"🧾 Чек к заказу #{order_id}")
            except Exception as e:
                await message.answer(f"❌ Не удалось отправить фото чека. Возможно, файл устарел.")
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте: /get_receipt 1001")
    except Exception as e:
        logger.error(f"Ошибка при получении чека: {e}")
        await message.answer("❌ Ошибка сервера.")

# Меню
async def show_worker_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🕗 Начать смену", callback_data="shift_start")
    kb.button(text="下班 Закончить смену", callback_data="shift_end")
    kb.button(text="📋 Мои заказы", callback_data="my_orders_list")
    kb.button(text="📊 Мои смены", callback_data="shift_my")
    kb.adjust(2)
    await message.answer("👷‍♂️ Меню сотрудника:", reply_markup=kb.as_markup())

async def show_admin_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Создать заказ", callback_data="admin_new_order")
    kb.button(text="📊 Отчёт по сменам", callback_data="admin_shift_report")
    kb.button(text="📋 Все заказы", callback_data="admin_all_orders")

    # 🎨 КНОПКА WEBAPP — ОТКРЫВАЕТ ТВОЙ ИНТЕРФЕЙС
    kb.button(
        text="✨ Панель как в Figma",
        web_app=WebAppInfo(url="https://ab646487-hash.github.io/telegram-bot-webapp/")
    )

    kb.adjust(2)
    await message.answer("👑 Меню администратора:", reply_markup=kb.as_markup())

@dp.callback_query(lambda c: c.data == "admin_new_order")
async def admin_new_order(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📍 Введите адрес участка:")
    await state.set_state(OrderForm.address)
    await callback.answer()

# Заглушки для админ-меню
@dp.callback_query(lambda c: c.data == "admin_shift_report")
async def admin_shift_report(callback: types.CallbackQuery):
    try:
        records = shifts_sheet.get_all_records()[-10:]
        if not records:
            await callback.message.edit_text("📭 Нет данных по сменам.")
        else:
            report = "📊 Последние 10 смен:\n\n" + "\n".join(
                f"👤 {r['Имя сотрудника']} | 📅 {r['Дата']} | ⏱ {r['Отработано (ч)']} ч."
                for r in records
            )
            await callback.message.edit_text(report)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в отчёте по сменам: {e}")
        await callback.answer("❌ Ошибка")

@dp.callback_query(lambda c: c.data == "admin_all_orders")
async def admin_all_orders(callback: types.CallbackQuery):
    try:
        records = sheet.get_all_records()[-10:]
        if not records:
            await callback.message.edit_text("📭 Нет заказов.")
        else:
            report = "📋 Последние 10 заказов:\n\n" + "\n".join(
                f"#{r['№ заказа']} | {r['Адрес']} | 👷 {r['Ответственный']} | {r['Статус']}"
                for r in records
            )
            await callback.message.edit_text(report)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в списке заказов: {e}")
        await callback.answer("❌ Ошибка")

# Учёт смен
@dp.callback_query(lambda c: c.data == "shift_start")
async def shift_start(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        if user_id not in TEAM_MEMBERS:
            await callback.answer("❌ Вы не зарегистрированы как сотрудник.")
            return
        now = datetime.now()
        today = now.strftime("%d.%m.%Y")
        time_str = now.strftime("%H:%M")
        records = shifts_sheet.get_all_records()
        for record in records[::-1]:
            if record['ID сотрудника'] == user_id and record['Дата'] == today and record['Статус'] == "В процессе":
                await callback.answer("❌ У вас уже начата смена сегодня!")
                return
        row = [user_id, TEAM_MEMBERS[user_id], today, time_str, "", "", "В процессе"]
        shifts_sheet.append_row(row)
        await callback.message.edit_text(f"✅ Смена начата в {time_str}")
        await send_next_order(user_id)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при начале смены: {e}")
        await callback.answer("❌ Ошибка сервера.")

@dp.callback_query(lambda c: c.data == "shift_end")
async def shift_end(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        if user_id not in TEAM_MEMBERS:
            await callback.answer("❌ Вы не зарегистрированы как сотрудник.")
            return
        now = datetime.now()
        today = now.strftime("%d.%m.%Y")
        end_time_str = now.strftime("%H:%M")
        records = shifts_sheet.get_all_records()
        row_index = None
        start_time_str = None
        for i, record in enumerate(records[::-1], start=2):
            if record['ID сотрудника'] == user_id and record['Статус'] == "В процессе":
                row_index = len(records) - (i - 2)
                start_time_str = record['Начало смены']
                break
        if not row_index:
            await callback.answer("❌ У вас нет активной смены!")
            return
        start_dt = datetime.strptime(f"{today} {start_time_str}", "%d.%m.%Y %H:%M")
        end_dt = datetime.strptime(f"{today} {end_time_str}", "%d.%m.%Y %H:%M")
        hours_worked = (end_dt - start_dt).total_seconds() / 3600
        shifts_sheet.update_cell(row_index, 5, end_time_str)
        shifts_sheet.update_cell(row_index, 6, round(hours_worked, 2))
        shifts_sheet.update_cell(row_index, 7, "Завершена")
        await callback.message.edit_text(f"✅ Смена завершена.\nОтработано: {round(hours_worked, 2)} ч.")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при завершении смены: {e}")
        await callback.answer("❌ Ошибка сервера.")

@dp.callback_query(lambda c: c.data == "shift_my")
async def shift_my(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        if user_id not in TEAM_MEMBERS:
            await callback.answer("❌ Вы не зарегистрированы как сотрудник.")
            return
        records = shifts_sheet.get_all_records()
        my_shifts = [
            f"📅 {r['Дата']} | 🕗 {r['Начало смены']}–{r['Окончание смены']} | ⏱ {r['Отработано (ч)']} ч."
            for r in records
            if r['ID сотрудника'] == user_id and r['Статус'] == "Завершена"
        ][-5:]
        if my_shifts:
            await callback.message.edit_text("📋 Ваши последние смены:\n" + "\n".join(my_shifts))
        else:
            await callback.message.edit_text("📭 У вас пока нет завершённых смен.")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при просмотре смен: {e}")
        await callback.answer("❌ Ошибка сервера.")

# Создание заказа
@dp.message(OrderForm.address)
async def get_address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer("⚒ Укажите тип работы:")
    await state.set_state(OrderForm.work_type)

@dp.message(OrderForm.work_type)
async def get_work_type(message: types.Message, state: FSMContext):
    await state.update_data(work_type=message.text)
    await message.answer("📅 Укажите срок выполнения (например, 10.04):")
    await state.set_state(OrderForm.deadline)

@dp.message(OrderForm.deadline)
async def get_deadline(message: types.Message, state: FSMContext):
    await state.update_data(deadline=message.text)
    await message.answer("📝 Добавьте комментарий:")
    await state.set_state(OrderForm.comment)

@dp.message(OrderForm.comment)
async def get_comment(message: types.Message, state: FSMContext):
    await state.update_data(comment=message.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="Обычный", callback_data="priority_обычный")
    kb.button(text="🚨 Срочный", callback_data="priority_срочный")
    kb.adjust(2)
    await message.answer("⏳ Выберите приоритет:", reply_markup=kb.as_markup())
    await state.set_state(OrderForm.priority)

@dp.callback_query(lambda c: c.data.startswith("priority_"))
async def set_priority(callback: types.CallbackQuery, state: FSMContext):
    priority = callback.data.split("_")[1]
    await state.update_data(priority=priority)
    await callback.message.edit_text(f"⏳ Приоритет: {priority}")
    kb = InlineKeyboardBuilder()
    for user_id, name in TEAM_MEMBERS.items():
        kb.button(text=name, callback_data=f"assign_{user_id}")
    kb.adjust(1)
    await callback.message.answer("👷 Выберите исполнителя:", reply_markup=kb.as_markup())
    await state.set_state(OrderForm.assignee)

@dp.callback_query(lambda c: c.data.startswith("assign_"))
async def set_assignee(callback: types.CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    await state.update_data(assignee=user_id)
    await callback.message.edit_text(f"👷 Исполнитель: {TEAM_MEMBERS[user_id]}")
    await callback.message.answer("📷 Пришлите фото участка (или напишите 'без фото'):")
    await state.set_state(OrderForm.photo)

@dp.message(OrderForm.photo, F.photo)
async def get_photo(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo=photo_id)
    await finalize_order(message, state)

@dp.message(OrderForm.photo)
async def skip_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo=None)
    await finalize_order(message, state)

async def finalize_order(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        order_id = generate_order_id()
        row = [
            order_id, data['address'], data['work_type'], data['deadline'], data['comment'],
            data['priority'], "Назначен, не начат", TEAM_MEMBERS[data['assignee']],
            datetime.now().strftime("%d.%m.%Y"), "", "", "", "", "", "", "", ""
        ]
        sheet.append_row(row)
        text = (
            f"🆕 Заказ #{order_id} успешно назначен!\n"
            f"📍 Адрес: {data['address']}\n"
            f"⚒ Работа: {data['work_type']}\n"
            f"📅 Срок: {data['deadline']}\n"
            f"📝 Комментарий: {data['comment']}\n"
            f"⏳ Приоритет: {data['priority'].upper()}\n\n"
            f"✅ Заказ будет автоматически отправлен сотруднику при начале смены."
        )
        await message.answer(text)
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка при создании заказа: {e}")
        await message.answer("❌ Ошибка при создании заказа.")

# Автоматическая выдача заказов
async def send_next_order(user_id: int):
    try:
        records = sheet.get_all_records()
        for record in records:
            if record['Ответственный'] == TEAM_MEMBERS[user_id] and record['Статус'] == "Назначен, не начат":
                order_id = record['№ заказа']
                text = (
                    f"▶️ НОВЫЙ ЗАКАЗ #{order_id}\n"
                    f"📍 Адрес: {record['Адрес']}\n"
                    f"⚒ Работа: {record['Тип работы']}\n"
                    f"📅 Срок: {record['Срок']}\n"
                    f"📝 Комментарий: {record['Комментарий']}\n"
                    f"⏳ Приоритет: {record['Приоритет']}"
                )
                kb = InlineKeyboardBuilder()
                kb.button(text="▶️ Начал работу", callback_data=f"start_{order_id}")
                kb.button(text="✅ Выполнил работу", callback_data=f"done_{order_id}")
                kb.adjust(2)
                await bot.send_message(user_id, text, reply_markup=kb.as_markup())
                cell = sheet.find(str(order_id))
                if cell:
                    sheet.update_cell(cell.row, 7, "В работе")
                    sheet.update_cell(cell.row, 10, datetime.now().strftime("%d.%m.%Y %H:%M"))
                return
        await bot.send_message(user_id, "🎉 Все заказы выполнены! Отдыхайте 😊")
    except Exception as e:
        logger.error(f"Ошибка при отправке следующего заказа: {e}")

# Обработка кнопок заказа
@dp.callback_query(lambda c: c.data.startswith("start_"))
async def mark_started(callback: types.CallbackQuery):
    try:
        order_id = int(callback.data.split("_")[1])
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        cell = sheet.find(str(order_id))
        if not cell:
            await callback.answer("Заказ не найден.")
            return
        sheet.update_cell(cell.row, 7, "В работе")
        sheet.update_cell(cell.row, 10, now)
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Выполнил работу", callback_data=f"done_{order_id}")
        kb.adjust(1)
        await callback.message.edit_text(f"{callback.message.text}\n\n▶️ РАБОТА НАЧАТА\n🕒 {now}", reply_markup=kb.as_markup())
        await callback.answer("Хорошей работы!")
        await bot.send_message(ADMIN_IDS[0], f"▶️ Заказ #{order_id} — начал работу!\n👷‍♂️ Исполнитель: {callback.from_user.full_name}\n🕒 {now}")
    except Exception as e:
        logger.error(f"Ошибка при начале работы: {e}")
        await callback.answer("❌ Ошибка сервера.")

@dp.callback_query(lambda c: c.data.startswith("done_"))
async def mark_done(callback: types.CallbackQuery, state: FSMContext):
    try:
        order_id = int(callback.data.split("_")[1])
        await state.update_data(order_id=order_id)
        await callback.message.edit_text("💰 Введите сумму заказа:")
        await state.set_state(OrderForm.amount)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при запросе суммы: {e}")
        await callback.answer("❌ Ошибка сервера.")

@dp.message(OrderForm.amount)
async def get_amount(message: types.Message, state: FSMContext):
    await state.update_data(amount=message.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="💵 Наличными", callback_data="payment_наличными")
    kb.button(text="📱 Переводом", callback_data="payment_переводом")
    kb.button(text="📲 QR", callback_data="payment_qr")
    kb.button(text="🧾 По счёту", callback_data="payment_по_счёту")
    kb.adjust(2)
    await message.answer("💳 Выберите способ оплаты:", reply_markup=kb.as_markup())
    await state.set_state(OrderForm.payment)

@dp.callback_query(lambda c: c.data.startswith("payment_"))
async def get_payment(callback: types.CallbackQuery, state: FSMContext):
    payment = callback.data.split("_")[1]
    await state.update_data(payment=payment)
    await callback.message.edit_text("📸 Пришлите фото чека (или напишите 'без чека'):")
    await state.set_state(OrderForm.receipt_photo)
    await callback.answer()

@dp.message(OrderForm.receipt_photo, F.photo)
async def get_receipt_photo(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(receipt_photo=photo_id)
    await message.answer("🧪 Введите название препарата:")
    await state.set_state(OrderForm.chemical)

@dp.message(OrderForm.receipt_photo)
async def skip_receipt_photo(message: types.Message, state: FSMContext):
    await state.update_data(receipt_photo="без чека")
    await message.answer("🧪 Введите название препарата:")
    await state.set_state(OrderForm.chemical)

@dp.message(OrderForm.chemical)
async def get_chemical(message: types.Message, state: FSMContext):
    await state.update_data(chemical=message.text)
    await message.answer("🔢 Введите количество препарата (в литрах/кг):")
    await state.set_state(OrderForm.quantity)

@dp.message(OrderForm.quantity)
async def get_quantity(message: types.Message, state: FSMContext):
    await state.update_data(quantity=message.text)
    await message.answer("📏 Введите площадь участка (в сотках или м²):")
    await state.set_state(OrderForm.area)

@dp.message(OrderForm.area)
async def get_area(message: types.Message, state: FSMContext):
    await state.update_data(area=message.text)
    data = await state.get_data()
    order_id = data['order_id']
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    cell = sheet.find(str(order_id))
    if not cell:
        await message.answer("❌ Заказ не найден.")
        return
    sheet.update_cell(cell.row, 7, "Выполнен")
    sheet.update_cell(cell.row, 11, now)
    sheet.update_cell(cell.row, 12, data['amount'])
    sheet.update_cell(cell.row, 13, data['payment'])
    sheet.update_cell(cell.row, 14, data['chemical'])
    sheet.update_cell(cell.row, 15, data['quantity'])
    sheet.update_cell(cell.row, 16, data['area'])
    sheet.update_cell(cell.row, 17, data.get('receipt_photo', "без чека"))
    report = (
        f"🎉 Заказ #{order_id} ВЫПОЛНЕН!\n"
        f"👷‍♂️ Исполнитель: {TEAM_MEMBERS.get(data.get('assignee'), 'Неизвестно')}\n"
        f"🕒 {now}\n\n"
        f"💰 Сумма: {data['amount']} руб.\n"
        f"💳 Оплата: {data['payment']}\n"
        f"🧾 Чек: {'Прикреплён' if data.get('receipt_photo') != 'без чека' else 'Не предоставлен'}\n"
        f"🧪 Препарат: {data['chemical']}\n"
        f"🔢 Количество: {data['quantity']}\n"
        f"📐 Площадь: {data['area']}"
    )
    await message.answer("✅ Заказ завершён! Данные сохранены.")
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, report)
    await send_next_order(message.from_user.id)
    await state.clear()

# Просмотр заказов через кнопку
@dp.callback_query(lambda c: c.data == "my_orders_list")
async def my_orders_list(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        if user_id not in TEAM_MEMBERS:
            await callback.answer("❌ Вы не зарегистрированы как сотрудник.")
            return
        records = sheet.get_all_records()
        my_orders = [
            f"🆕 #{r['№ заказа']} | {r['Адрес']} | {r['Тип работы']} | {r['Статус']}"
            for r in records
            if r['Ответственный'] == TEAM_MEMBERS[user_id] and r['Статус'] != "Выполнен"
        ]
        if my_orders:
            await callback.message.edit_text("📋 Ваши назначенные заказы:\n" + "\n".join(my_orders))
        else:
            await callback.message.edit_text("📭 У вас нет активных заказов.")
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при просмотре заказов: {e}")
        await callback.answer("❌ Ошибка сервера.")

# Глобальный обработчик ошибок
@dp.errors()
async def errors_handler(update, exception):
    logger.error(f"❌ Глобальная ошибка: {exception}")
    return True

# Главная функция
async def main():
    logger.info("🚀 Бот запускается...")
    await set_bot_commands()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
