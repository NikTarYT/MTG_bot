import os
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import ApplicationBuilder, ChatMemberHandler, CallbackContext, CallbackQueryHandler, filters, MessageHandler, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
import pytz
import locale
from DB import Database
from Message import Message
from enum import Enum, auto

locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8')

def get_bot_token():
    try:
        with open('token.txt', 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.error("Файл token.txt не найден!")
        return None

log_filename = "logs/" + datetime.now().strftime("%d-%m-%Y") + ".log"
os.makedirs("logs", exist_ok=True)

file_handler = logging.FileHandler(log_filename, encoding='utf-8')
stream_handler = logging.StreamHandler()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[file_handler, stream_handler]
)

logger = logging.getLogger(__name__)

async def error_handler(update: Update, context: CallbackContext):
    logger.error(msg="Ошибка в обработчике Telegram:", exc_info=context.error)

class MessageState(Enum):
    DEFAULT = auto()
    TEXT = auto()
    TIME = auto()

class MtgBot:
    def escape_markdown_v2(self, text: str) -> str:
        if not text:
            return ""
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

    def format_time(self, str_to_f: str):
        hours, minutes = map(int, str_to_f.split(':'))
        return f"{hours:02d}:{minutes:02d}"
    
    def __init__(self):
        self.db = Database()
        self.scheduler = None
        self.message_state = MessageState.DEFAULT

    async def start_command(self, update: Update, context: CallbackContext):
        context.user_data['started'] = True
        user_id = update.effective_user.id
        chat_id = self.db.get_admin_chat(user_id)
        
        if chat_id:
            await self.send_admin_panel(update, context, user_id)  # Исправленный вызов
        else:
            await update.message.reply_text(
                "Привет! Я бот для организации мероприятий. Добавьте меня в группу как администратора.\n\n"
                "После добавления в группу используйте команду /set_admin в групповом чате, чтобы стать администратором бота."
            )
    
    async def send_admin_panel(self, update: Update, context: CallbackContext, user_id: int):
        """Показывает админ-панель"""
        keyboard = [
            [InlineKeyboardButton("📋 Мои мероприятия", callback_data="a_messages")],
            [InlineKeyboardButton("➕ Создать мероприятие", callback_data="a_create")],
        ]
        
        text = "🎮 **Админ-панель**\n\nВыберите действие:"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=text,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def init_scheduler(self, application):
        self.scheduler = AsyncIOScheduler()
        self.bot = application.bot
        self.scheduler.start()

        for message in self.db.init_load_all():
            if message.trigger:
                self.scheduler.add_job(
                    self.send_scheduled_message,
                    trigger=message.trigger,
                    args=[message.db_id],
                    id=f"message_{message.db_id}"
                )

    async def reschedule(self, day_of_week: str, hour: int, minute: int = 0, db_id: int = None):
        if db_id is None:
            logger.error("reschedule вызван без db_id")
            return
        
        job_id = f"message_{db_id}"
        try:
            existing = self.scheduler.get_job(job_id)
            if existing:
                self.scheduler.remove_job(job_id)
        except Exception:
            pass
            
        moscow_tz = pytz.timezone("Europe/Moscow")
            
        self.scheduler.add_job(
            self.send_scheduled_message,
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=moscow_tz,
            ),
            args=[db_id],
            id=job_id
        )
        logger.info(f"Расписание обновлено: {day_of_week} в {hour}:{minute:02d} (GMT+3)")

    async def send_scheduled_message(self, db_id):
        max_retries = 1
        for attempt in range(max_retries):
            try:
                message = self.db.load_message(db_id)
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Сообщение {db_id} не найдено после {max_retries} попыток: {e}")
                    return
                await asyncio.sleep(1)
        
        if not message:
            return

        if message.participants or message.maybe_participants:
            message.participants = []
            message.maybe_participants = []
            try:
                self.db.save_message(message)
            except Exception as e:
                logger.error(f"Не удалось очистить голоса для message {db_id}: {e}")

        for attempt in range(max_retries):
            try:
                if message.pin_id:
                    try:
                        # Открепляем старое сообщение - БЕЗ message_thread_id
                        await self.bot.unpin_chat_message(
                            chat_id=message.chat_id,
                            message_id=message.pin_id
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось открепить старое сообщение: {e}")

                # Подготавливаем параметры для отправки
                send_params = {
                    'text': message.generate_message_text(),
                    'chat_id': message.chat_id,
                    'reply_markup': self.get_keyboard(message),
                    'parse_mode': constants.ParseMode.MARKDOWN_V2,
                }
                
                # Добавляем message_thread_id если указан
                if message.message_thread_id:
                    send_params['message_thread_id'] = message.message_thread_id
                
                msg = await self.bot.send_message(**send_params)
                
                message.pin_id = msg.message_id
                self.db.save_message(message)

                # Закрепляем сообщение
                # Сообщение автоматически закрепится в том топике, куда было отправлено
                await self.bot.pin_chat_message(
                    chat_id=message.chat_id,
                    message_id=message.pin_id,
                    disable_notification=True  # Необязательно, чтобы не беспокоить участников
                )
                
                logger.info(f"Запланированное сообщение отправлено в чат {message.chat_id}, топик: {message.message_thread_id or 'нет'}")
                break
                
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Не удалось отправить запланированное сообщение после {max_retries} попыток: {e}")
                else:
                    logger.warning(f"Ошибка при отправке, попытка {attempt + 1}: {e}")
                    await asyncio.sleep(2)

    def get_keyboard(self, message):
        keyboard = [
            [
                InlineKeyboardButton(f"{len(message.participants)} 👍", callback_data=f'participate_{message.db_id}'),
                InlineKeyboardButton(f"{len(message.maybe_participants)} ❓", callback_data=f'participatemaybe_{message.db_id}'),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    async def update_lists(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()  # Сразу подтверждаем нажатие
        
        try:
            action, db_id = query.data.split('_')
            db_id = int(db_id)
        except:
            await query.edit_message_text("Ошибка: неверный формат данных")
            return
        
        try:
            message = self.db.load_message(db_id)
            if not message:
                await query.edit_message_text("Это сообщение больше не активно")
                return
        except Exception as e:
            logger.error(f"Ошибка загрузки сообщения {db_id}: {e}")
            await query.edit_message_text("Ошибка загрузки сообщения")
            return
        
        user = query.from_user
        
        if action == 'participate':
            if any(u['id'] == user.id for u in message.participants):
                message.participants = [u for u in message.participants if u['id'] != user.id]
            else:
                message.add_participant(user)
        elif action == 'participatemaybe':
            if any(u['id'] == user.id for u in message.maybe_participants):
                message.maybe_participants = [u for u in message.maybe_participants if u['id'] != user.id]
            else:
                message.add_maybe_participant(user)
        
        try:
            self.db.save_message(message)
            await self.update_message(context, message)
            logger.info(f"Пользователь {user.id} проголосовал в сообщении {db_id}")
        except Exception as e:
            logger.error(f"Ошибка сохранения голоса: {e}")
            await query.edit_message_text("✅ Голос учтен!")

    async def update_message(self, context: CallbackContext, message: Message):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await context.bot.edit_message_text(
                    chat_id=message.chat_id,
                    message_id=message.pin_id,
                    text=message.generate_message_text(),
                    reply_markup=self.get_keyboard(message),
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                )
                break
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Ошибка при обновлении сообщения после {max_retries} попыток: {e}")
                else:
                    logger.warning(f"Ошибка обновления, попытка {attempt + 1}: {e}")
                    await asyncio.sleep(1)

    async def admin_panel(self, update: Update, context: CallbackContext):
        logger.info(f"[ADMIN_PANEL] Called by user_id: {update.effective_user.id}, data: {update.callback_query.data if update.callback_query else 'None'}")        
        try:
            self.message_state = MessageState.DEFAULT
            context.chat_data['admin_id'] = update.effective_user.id
            
            if not update.callback_query:
                logger.error("[ADMIN_PANEL] No callback_query in update")
                return
                
            data = update.callback_query.data
            logger.info(f"[ADMIN_PANEL] Raw data: '{data}'")
            
            # Создаем простой и понятный обработчик
            if data == "a_messages":
                logger.info("[ADMIN_PANEL] Calling message_list")
                await self.message_list(update, context)
            elif data == "a_create":
                logger.info("[ADMIN_PANEL] Calling create_message")
                await self.create_message(update, context)
            elif data == "a_change_topic":
                logger.info("[ADMIN_PANEL] Calling change_topic_command")
                await self.change_topic_command(update, context)
            elif data == "a_return":
                logger.info("[ADMIN_PANEL] Calling send_admin_panel")
                await self.send_admin_panel(update, context, update.effective_user.id)
            else:
                logger.warning(f"[ADMIN_PANEL] Unknown command: {data}")
                await update.callback_query.answer(f"Неизвестная команда: {data}")
                
        except Exception as e:
            logger.error(f"[ADMIN_PANEL] Exception details:", exc_info=True)
            logger.error(f"[ADMIN_PANEL] Exception type: {type(e).__name__}")
            logger.error(f"[ADMIN_PANEL] Exception message: {str(e)}")
            
            # Отправим более детальную информацию
            if update.callback_query:
                try:
                    await update.callback_query.answer(f"Ошибка: {type(e).__name__}: {str(e)[:50]}...")
                except:
                    pass

    async def message_list(self, update: Update, context: CallbackContext, admin_id: int = None):
        """Показывает все мероприятия из всех чатов администратора"""
        logger.info(f"[MESSAGE_LIST] Called for admin_id: {admin_id}")
        
        if admin_id is None:
            if update.callback_query:
                admin_id = update.callback_query.from_user.id
            else:
                admin_id = update.effective_user.id
        
        try:
            # Загружаем все мероприятия администратора
            messages = self.db.load_messages(admin_id)
            
            if not messages:
                # Проверяем, есть ли у пользователя вообще чаты
                if not self.db.user_has_chats(admin_id):
                    text = "❌ Вы не являетесь администратором ни в одном чате.\n\n"
                    text += "Попросите владельца чата добавить вас как администратора через команду:\n"
                    text += "/set_admin @ваш_юзернейм"
                else:
                    text = "📭 У вас пока нет созданных мероприятий.\n\n"
                    text += "Создайте первое мероприятие через админ-панель."
                
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text=text,
                        reply_markup=self.create_back_button("a_return")
                    )
                else:
                    await update.message.reply_text(text)
                return
            
            # Форматируем список мероприятий
            text = f"📋 **Ваши мероприятия ({len(messages)})**\n\n"
            
            for i, msg in enumerate(messages, 1):
                # Получаем информацию о чате
                try:
                    chat = await context.bot.get_chat(msg['chat_id'])
                    chat_title = chat.title or f"Чат {msg['chat_id']}"
                except Exception as e:
                    logger.error(f"Error getting chat info: {e}")
                    chat_title = f"Чат {msg['chat_id']}"
                
                # Форматируем день недели
                days_translation = {
                    'mon': 'Понедельник', 'tue': 'Вторник', 'wed': 'Среда',
                    'thu': 'Четверг', 'fri': 'Пятница', 'sat': 'Суббота', 'sun': 'Воскресенье'
                }
                day_name = days_translation.get(msg['day_of_week'], msg['day_of_week'])
                
                # Обрезаем название события
                event_name = msg['text'].split('\n')[0] if msg['text'] else "Без названия"
                if len(event_name) > 30:
                    event_name = event_name[:27] + "..."
                
                text += f"{i}. **{event_name}**\n"
                text += f"   🗓 {day_name} в {msg['time']}\n"
                text += f"   👥 {msg['participants_count']} участников\n"
                text += f"   💬 {chat_title}"
                
                if msg['topic_id']:
                    text += f" (топик: {msg['topic_id']})"
                text += "\n\n"
            
            # Создаем инлайн-клавиатуру
            keyboard = []
            
            # Кнопки для каждого мероприятия
            for msg in messages:
                event_name = msg['text'].split('\n')[0] if msg['text'] else "Без названия"
                if len(event_name) > 15:
                    event_name = event_name[:12] + "..."
                
                keyboard.append([
                    InlineKeyboardButton(
                        f"✏️ {event_name}",
                        callback_data=f"s_{msg['id']}"
                    )
                ])
            
            # Кнопка возврата
            keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="a_return")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    text=text,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
                
        except Exception as e:
            logger.error(f"[MESSAGE_LIST] Error: {e}")
            text = "❌ Произошла ошибка при загрузке мероприятий."
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=text,
                    reply_markup=self.create_back_button("a_return")
                )

    def create_back_button(self, callback_data: str = "a_return"):
        """Создает кнопку возврата"""
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=callback_data)]]
        return InlineKeyboardMarkup(keyboard)

    async def message_render(self, update: Update, context: CallbackContext):
        context.chat_data['week'] = {'mon': 'Пн','tue': 'Вт','wed': 'Ср','thu': 'Чт','fri': 'Пт','sat': 'Сб','sun': 'Вс'}
        
        message_id = None
        if update.callback_query and update.callback_query.data:
            try:
                _, message_id = update.callback_query.data.split('_')
            except ValueError:
                pass
        
        if not message_id:
            message_id = context.chat_data.get('db_id')
        
        if not message_id:
            logger.error("[MESSAGE_RENDER] Cannot retrieve message_id")
            await update.callback_query.answer("Ошибка: не найден ID сообщения")
            return
        
        context.chat_data['db_id'] = message_id
        
        try:
            message = self.db.load_message(int(message_id))
        except Exception as e:
            logger.error(f"[MESSAGE_RENDER] Error loading message {message_id}: {e}")
            await update.callback_query.answer("Ошибка загрузки сообщения")
            return

        keyboard = [
            [InlineKeyboardButton("Текст", callback_data=f"m_text"), InlineKeyboardButton("Удалить", callback_data=f"m_delete")],
            [InlineKeyboardButton("Список", callback_data="a_messages"),InlineKeyboardButton("Перенести", callback_data=f"m_reschedule")],
            [InlineKeyboardButton("Меню", callback_data="a_return")]
        ]
        
        message_text = message.generate_message_text()
        
        try:
            if self.message_state == MessageState.DEFAULT:
                await update.callback_query.edit_message_text(
                    text=message_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=constants.ParseMode.MARKDOWN_V2)
            else:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=context.chat_data['edit_id'].message_id,
                    text=message_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=constants.ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.error(f"[MESSAGE_RENDER] Error displaying message: {e}")

    async def message_menu(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT
        data = update.callback_query.data
        
        # Проверяем, что данные начинаются с 'm_'
        if not data.startswith('m_'):
            await update.callback_query.answer("Неверный формат команды")
            return
            
        _, command = data.split('_', 1)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Меню", callback_data='a_return')]])
        
        try:
            if command == "delete":
                await self.delete_message(update, context)
            elif command == "text":
                self.message_state = MessageState.TEXT
                context.chat_data['edit_id'] = await update.callback_query.edit_message_text("Введите текст: ", reply_markup=keyboard)
            elif command == "reschedule":
                self.message_state = MessageState.TIME
                await self.admin_reschedule(update, context)
                
            logger.info(f"[MESSAGE_MENU] Parsing {command}")
        except Exception as e:
            logger.error(f"[MESSAGE_MENU] Cannot parse command: {e}")

    async def create_message(self, update: Update, context: CallbackContext):
        # Проверяем тип чата
        if update.effective_chat.type != "private":
            # Для callback_query используем answer, для message - reply_text
            if update.callback_query:
                await update.callback_query.answer("Эта команда работает только в личных сообщениях")
            else:
                await update.message.reply_text("Эта команда работает только в личных сообщениях")
            return
        
        admin_id = update.effective_user.id
        
        # Получаем все чаты админа с топиками по умолчанию
        admin_chats = self.db.get_admin_chats_with_threads(admin_id)
        
        if not admin_chats:
            if update.callback_query:
                await update.callback_query.edit_message_text("У вас нет привязанных чатов. Используйте /set_admin в группе.")
            else:
                await update.message.reply_text("У вас нет привязанных чатов. Используйте /set_admin в группе.")
            return

        # Остальной код метода остается без изменений...

                # Если у админа только один чат - используем его
        if len(admin_chats) == 1:
            admin_chat_info = admin_chats[0]
            if isinstance(admin_chat_info, tuple) and len(admin_chat_info) == 2:
                chat_id, thread_id = admin_chat_info
            else:
                chat_id = admin_chat_info
                thread_id = None

            context.chat_data['selected_chat_id'] = chat_id
            context.chat_data['selected_thread_id'] = thread_id
            
            message = Message()
            message.chat_id = chat_id
            message.message_thread_id = thread_id
            message.participants = []
            message.maybe_participants = []

            message = self.db.save_message(message)
            context.chat_data['db_id'] = message.db_id
            self.message_state = MessageState.TIME
            await self.admin_reschedule(update, context)
        else:
            # Если несколько чатов - показываем выбор с информацией о топиках
            await self.show_chat_selection(update, context, admin_chats)
    
    async def show_chat_selection(self, update: Update, context: CallbackContext, admin_chats):
        """Показывает список чатов для выбора при создании мероприятия с информацией о топиках"""
        keyboard = []
        
        # Получаем информацию о чатах
        for chat_id, thread_id in admin_chats:
            try:
                chat = await context.bot.get_chat(chat_id)
                chat_title = chat.title or f"Чат {chat_id}"
                
                # Определяем тип чата
                is_forum = chat.is_forum if hasattr(chat, 'is_forum') else False
                
                if thread_id and is_forum:
                    try:
                        # Пробуем получить информацию о топике
                        topic = await context.bot.get_forum_topic(chat_id, thread_id)
                        thread_info = f"Топик: {topic.name}"
                    except:
                        thread_info = f"Топик ID: {thread_id}"
                    button_text = f"{chat_title} ({thread_info})"
                elif is_forum:
                    button_text = f"{chat_title} (форум, без топика)"
                else:
                    button_text = f"{chat_title} (обычный чат)"
                    
            except Exception as e:
                logger.error(f"Ошибка получения информации о чате {chat_id}: {e}")
                button_text = f"Чат {chat_id}"
            
            keyboard.append([InlineKeyboardButton(
                button_text, 
                callback_data=f"create_chat_{chat_id}_{thread_id if thread_id else 'none'}"
            )])
        
        keyboard.append([InlineKeyboardButton("Отмена", callback_data="a_return")])
        
        await update.callback_query.edit_message_text(
            text="*Выберите чат для мероприятия:*\n_Информация о топиках указана в скобках_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )
    
    async def handle_create_chat_selection(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()
        
        _, _, chat_id_str, thread_id_str = query.data.split('_')
        chat_id = int(chat_id_str)
        thread_id = None if thread_id_str == 'none' else int(thread_id_str)
        
        # Сохраняем выбранный чат и топик
        context.chat_data['selected_chat_id'] = chat_id
        context.chat_data['selected_thread_id'] = thread_id
        
        # Создаем сообщение для выбранного чата
        message = Message()
        message.chat_id = chat_id
        message.message_thread_id = thread_id
        message.participants = []
        message.maybe_participants = []

        message = self.db.save_message(message)
        context.chat_data['db_id'] = message.db_id
        self.message_state = MessageState.TIME
        await self.admin_reschedule(update, context)

    async def delete_message(self, update: Update, context: CallbackContext):
        replayer = update.message or update.callback_query.message
        db_id = context.chat_data['db_id']
        
        if not self.db.get_admin_chat(update.effective_user.id):
            await replayer.reply_text("Эта команда доступна только админам")
            return
            
        message = self.db.load_message(db_id)
        if not message:
            await replayer.reply_text("В этом чате нет активного сообщения")
            return
        
        self.db.delete_message(db_id)
        
        try:
            if message.pin_id:
                await context.bot.unpin_chat_message(chat_id=message.chat_id, message_id=message.pin_id)
        except Exception as e:
            logger.error(f"[DELETER] Cannot unpin message: {e}")
            
        await self.message_list(update, context)

    async def admin_reschedule(self, update: Update, context: CallbackContext):
        replayer = update.message or update.callback_query.message
        if update.effective_chat.type != "private":
            await replayer.reply_text("Эта команда работает только в личных сообщениях")
            return
            
        message_id = context.chat_data['db_id']
        context.chat_data['message'] = self.db.load_message(message_id)
        
        days = [
            ["Пн", "mon"], ["Вт", "tue"], ["Ср", "wed"], ["Чт", "thu"],
            ["Пт", "fri"], ["Сб", "sat"], ["Вс", "sun"], ["Сегодня", "to"]
        ]
        keyboard = [[InlineKeyboardButton(day[0], callback_data=f"day_{day[1]}")] for day in days]
        keyboard.append([InlineKeyboardButton("Меню", callback_data="a_return")])
        
        context.chat_data['edit_id'] = await replayer.edit_text(
            "Выберите день недели:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def day_callback(self, update: Update, context: CallbackContext):
        query = update.callback_query
        await query.answer()
    
        selected_day = query.data.split('_')[1]
        week = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    
        if selected_day == "to":
            selected_day = week[datetime.now(pytz.timezone('Europe/Moscow')).weekday()]
    
        context.chat_data['message'].day_of_week = selected_day
        notice_day_index = week.index(selected_day)
        context.chat_data['message'].day_of_notice = week[notice_day_index]
    
        h, m = context.chat_data['message'].time.split(":")
        context.chat_data['message'].time = f"{int(h):02d}:{int(m):02d}"
    
        await query.edit_message_text(
            text=f"Пожалуйста напишите час отправки\\!\n_в формате ЧЧ:ММ_",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Оставить нынешнее ({context.chat_data['message'].time})", callback_data="keep_time")]
            ])
        )

    async def admin_input(self, update: Update, context: CallbackContext):
        # Проверяем, что сообщение в приватном чате
        if update.effective_chat.type != "private":
            # Игнорируем сообщения в групповых чатах
            return
        
        # Если мы в процессе изменения топика, передаем управление handle_topic_input
        if 'change_topic_chat' in context.chat_data:
            await self.handle_topic_input(update, context)
            return
        
        try:
            message_id = context.chat_data['db_id']
        except KeyError as e:
            logger.error(f"[ADMIN_INPUT] Cannot retrieve message_id: {e}")
            return
        
        if self.message_state == MessageState.TIME:
            try:
                time_str = update.message.text
                hours, minutes = map(int, time_str.split(':'))
                if not (0 <= hours < 24 and 0 <= minutes < 60):
                    raise ValueError
            except:
                await update.message.reply_text("Неверный формат времени! Используйте ЧЧ:ММ")
                return
            context.chat_data['message'].time = f"{hours:02d}:{minutes:02d}"
            await self.finish_reschedule(update=update, context=context)
        
        elif self.message_state == MessageState.TEXT:
            message = self.db.load_message(message_id)
            message.text = update.message.text
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
            self.db.save_message(message)
            await self.message_render(update, context)
        
        else:
            await self.send_admin_panel(update, context, update.effective_user.id)

    async def keep_time_callback(self, update: Update, context: CallbackContext):
        await update.callback_query.answer()
        await self.finish_reschedule(update=update, context=context)

    async def finish_reschedule(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT
        message_obj = update.callback_query.message if update.callback_query else update.message
        
        try:
            await context.bot.delete_message(
                chat_id=message_obj.chat_id,
                message_id=message_obj.message_id
            )
        except:
            pass

        current_message = context.chat_data['message']
        if not current_message:
            return

        hour, minute = map(int, current_message.time.split(':'))
        current_message.set_trigger(current_message.day_of_notice, f"{hour:02d}:{minute:02d}")
        
        self.db.save_message(current_message)
        await self.reschedule(current_message.day_of_notice, hour, minute, current_message.db_id)
        
        await context.bot.send_message(
            text="Мероприятие успешно создано!",
            chat_id=update.effective_user.id
        )
        await self.send_admin_panel(update, context, update.effective_user.id)

    async def set_admin_command(self, update: Update, context: CallbackContext):
        chat = update.effective_chat
        user = update.effective_user
        
        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("Эта команда работает только в групповых чатах!")
            return
        
        try:
            chat_member = await context.bot.get_chat_member(chat.id, user.id)
            if chat_member.status not in ['administrator', 'creator']:
                await update.message.reply_text("Только администраторы чата могут использовать эту команду!")
                return
        except Exception as e:
            logger.error(f"Ошибка при проверке прав администратора: {e}")
            await update.message.reply_text("Ошибка при проверке прав доступа!")
            return
        
        try:
            # Получаем message_thread_id если сообщение из топика
            message_thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
            
            # Сохраняем админа с топиком по умолчанию
            self.db.set_chat_admin(chat.id, user.id, message_thread_id)
            
            thread_info = ""
            if message_thread_id:
                thread_info = f"\nТопик по умолчанию: ID {message_thread_id}"
            
            await update.message.reply_text(
                f"✅ Вы теперь администратор бота для этого чата!\n"
                f"ID чата: {chat.id}{thread_info}\n\n"
                f"Используйте /start в личных сообщениях с ботом для управления мероприятиями.",
                reply_to_message_id=update.message.message_id
            )
            
            # Отправляем приветственное сообщение в личку
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"✅ Вы назначены администратором для чата:\n"
                        f"Название: {chat.title}\n"
                        f"ID: {chat.id}{thread_info}\n\n"
                        f"Используйте /start для доступа к админ-панели."
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение в личку: {e}")
                
        except Exception as e:
            logger.error(f"Ошибка при назначении администратора: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при назначении администратора!",
                reply_to_message_id=update.message.message_id
            )

    async def handle_migration(self, update: Update, context: CallbackContext):
        old_chat_id = update.message.migrate_from_chat_id
        new_chat_id = update.message.chat.id
        
        logger.info(f"Группа мигрировала. Старый ID: {old_chat_id}, новый ID: {new_chat_id}")
        
        if self.db.update_chat_id(old_chat_id, new_chat_id):
            logger.info("Chat_id успешно обновлён в базе данных")
        else:
            logger.error("Ошибка при обновлении chat_id в БД")

    async def handle_chat_member_update(self, update: Update, context: CallbackContext):
        chat_member = update.my_chat_member
        new_status = chat_member.new_chat_member.status

        if new_status in ('left', 'kicked'):
            chat_id = update.effective_chat.id
            self.db.remove_chats_data(chat_id)
            logger.info(f"Бот удалён из чата {chat_id}")

    async def change_topic_command(self, update: Update, context: CallbackContext):
        """Команда для изменения топика по умолчанию для чата"""
        admin_id = update.effective_user.id
        
        if update.effective_chat.type != "private":
            await update.message.reply_text("Эта команда работает только в личных сообщениях")
            return
        
        # Получаем все чаты админа
        admin_chats = self.db.get_admin_chats_with_threads(admin_id)
        
        if not admin_chats:
            await update.message.reply_text("У вас нет привязанных чатов.")
            return
        
        # Создаем клавиатуру для выбора чата
        keyboard = []
        for chat_id, thread_id in admin_chats:
            try:
                chat = await context.bot.get_chat(chat_id)
                chat_title = chat.title or f"Чат {chat_id}"
                
                if thread_id:
                    button_text = f"{chat_title} (текущий топик: {thread_id})"
                else:
                    button_text = f"{chat_title} (без топика)"
                    
                keyboard.append([InlineKeyboardButton(
                    button_text, 
                    callback_data=f"change_topic_{chat_id}"
                )])
            except:
                keyboard.append([InlineKeyboardButton(
                    f"Чат {chat_id}", 
                    callback_data=f"change_topic_{chat_id}"
                )])
        
        keyboard.append([InlineKeyboardButton("Отмена", callback_data="a_return")])
        
        await update.message.reply_text(
            "Выберите чат для изменения топика:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_topic_change(self, update: Update, context: CallbackContext):
        """Обработчик выбора чата для смены топика"""
        query = update.callback_query
        await query.answer()
        
        _, _, chat_id_str = query.data.split('_')
        chat_id = int(chat_id_str)
        admin_id = update.effective_user.id
        
        # Сохраняем chat_id в контексте
        context.chat_data['change_topic_chat'] = chat_id
        context.chat_data['change_topic_admin'] = admin_id
        
        await query.edit_message_text(
            "Введите ID нового топика для этого чата.\n\n"
            "Как получить ID топика:\n"
            "1. Перешлите любое сообщение из нужного топика боту @getidsbot\n"
            "2. Или введите 0 для сброса топика (сообщения будут в общий чат)\n"
            "3. Или введите 'same' чтобы использовать топик текущего сообщения (если команда вызвана из топика)\n\n"
            "Введите ID топика или 'отмена' для отмены:"
        )

    async def handle_topic_input(self, update: Update, context: CallbackContext):
        """Обработчик ввода ID топика"""
        user_input = update.message.text.strip().lower()
        admin_id = update.effective_user.id
        
        if user_input == 'отмена':
            await update.message.reply_text("Отменено.")
            await self.send_admin_panel(update, context, admin_id)
            return
        
        chat_id = context.chat_data.get('change_topic_chat')
        
        if not chat_id:
            await update.message.reply_text("Ошибка: чат не выбран.")
            return
        
        try:
            if user_input == 'same':
                # В будущем можно реализовать получение из контекста
                thread_id = None
                await update.message.reply_text("Функция 'same' пока не реализована. Введите числовой ID.")
                return
            elif user_input == '0':
                thread_id = None
                message_text = f"Топик сброшен для чата {chat_id}. Новые мероприятия будут создаваться в общем чате."
            else:
                thread_id = int(user_input)
                message_text = f"Топик по умолчанию для чата {chat_id} установлен: {thread_id}"
            
            # Обновляем в базе данных
            self.db.update_chat_thread(chat_id, admin_id, thread_id)
            
            await update.message.reply_text(message_text)
            await self.send_admin_panel(update, context, admin_id)
            
        except ValueError:
            await update.message.reply_text("Неверный формат. Введите числовой ID топика или '0' или 'отмена'.")
        except Exception as e:
            logger.error(f"Ошибка при обновлении топика: {e}")
            await update.message.reply_text(f"Ошибка: {str(e)}")

if __name__ == '__main__':
    bot = MtgBot()
    token = get_bot_token()
    if not token:
        exit("Ошибка: не удалось загрузить токен бота")

    # Пробуем с прокси, если не работает - без прокси
    https_proxy = os.environ.get('HTTPS_PROXY')
    
    try:
        if https_proxy:
            application = ApplicationBuilder().token(token).proxy(https_proxy).build()
            print("Using proxy for connection")
        else:
            application = ApplicationBuilder().token(token).build()
            print("Using direct connection")
    except Exception as e:
        print(f"Error with proxy, trying without: {e}")
        application = ApplicationBuilder().token(token).build()
        print("Using direct connection (fallback)")

    application.post_init = bot.init_scheduler
    application.add_error_handler(error_handler)

    application.add_handlers([
        CommandHandler("start", bot.start_command),
        CommandHandler("set_admin", bot.set_admin_command),
        CommandHandler("change_topic", bot.change_topic_command),
        CallbackQueryHandler(bot.admin_panel, pattern='^a_'),
        CallbackQueryHandler(bot.message_render, pattern='^s_'),
        CallbackQueryHandler(bot.message_menu, pattern='^m_'),
        CallbackQueryHandler(bot.day_callback, pattern='^day_'),
        CallbackQueryHandler(bot.keep_time_callback, pattern='^keep_time'),
        CallbackQueryHandler(bot.handle_create_chat_selection, pattern='^create_chat_'),
        CallbackQueryHandler(bot.handle_topic_change, pattern='^change_topic_'),
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, bot.admin_input),
        MessageHandler(filters.StatusUpdate.MIGRATE, bot.handle_migration)
    ])
    
    application.add_handlers([
        ChatMemberHandler(bot.handle_chat_member_update),
        CallbackQueryHandler(bot.update_lists, pattern="^participate")
    ])

    print("Бот запускается...")
    application.run_polling()