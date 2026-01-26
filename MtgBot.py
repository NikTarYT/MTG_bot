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

log_filename = "logs\\" + datetime.now().strftime("%d-%m-%Y") + ".log"
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler(log_filename),logging.StreamHandler()]
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
            await self.send_admin_panel(update, context, user_id)
        else:
            await update.message.reply_text(
                "Привет! Я бот для организации мероприятий. Добавьте меня в группу как администратора.\n\n"
                "После добавления в группу используйте команду /set_admin в групповом чате, чтобы стать администратором бота."
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
        max_retries = 3
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
                        await self.bot.unpin_chat_message(
                            chat_id=message.chat_id,
                            message_id=message.pin_id
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось открепить старое сообщение: {e}")

                msg = await self.bot.send_message(
                    text=message.generate_message_text(),
                    chat_id=message.chat_id,
                    reply_markup=self.get_keyboard(message),
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                )
                
                message.pin_id = msg.message_id
                self.db.save_message(message)

                await self.bot.pin_chat_message(
                    chat_id=message.chat_id,
                    message_id=message.pin_id,
                )
                logger.info(f"Запланированное сообщение отправлено в чат {message.chat_id}")
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

    async def send_admin_panel(self, update: Update, context: CallbackContext, chat_id: int = None):
        if not self.db.get_admin_chat(chat_id):
            logger.info(f"[ADMIN_PANEL] user {chat_id} attempt to call admin_panel, but was not detected in database!")
            await context.bot.send_message(text="Перед началом работы вы должны добавить меня в чат либо быть его админом!",chat_id=chat_id)
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Добавить", callback_data="a_create"), InlineKeyboardButton("Список", callback_data="a_messages")],
        ])
        
        try:
            if context.chat_data.get('panel_state'):
                replayer = update.callback_query.message or update.message
                await replayer.edit_text(
                    text=f"*Админ панель*\n_Список \\~\\> Список активных мероприятий\nДобавить \\~\\> Добавление мероприятия_\n\n",
                    reply_markup=keyboard,
                    parse_mode=constants.ParseMode.MARKDOWN_V2)
                return
        except Exception as e:
            logger.error(f"[ADMIN_PANEL] Cannot edit: {e}")
        
        await context.bot.send_message(
            chat_id=chat_id,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            text=f"*Админ панель*\n_Список \\~\\> Список активных мероприятий\nДобавить \\~\\> Добавление мероприятия_\n\n",
            reply_markup=keyboard
        )

    async def admin_panel(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT
        context.chat_data['admin_id'] = update.effective_user.id
        _, command = update.callback_query.data.split('_')
        
        try:
            if command == "messages":
                await self.message_list(update, context)
            elif command == "create":
                await self.create_message(update, context)
            elif command == "return":
                context.chat_data['panel_state'] = True
                await self.send_admin_panel(update, context, context.chat_data['admin_id'])
            else:
                logger.warning(f"[ADMIN_PANEL] Cannot parse command \"{command}\"")
        except Exception as e:
            logger.error(f"[ADMIN_PANEL] Cannot parse command: {e}")

    async def message_list(self, update: Update, context: CallbackContext):
        context.chat_data['db_id'] = None
        context.chat_data['week'] = {'mon': 'Пн','tue': 'Вт','wed': 'Ср','thu': 'Чт','fri': 'Пт','sat': 'Сб','sun': 'Вс'}
        messages = self.db.load_messages(context.chat_data['admin_id'])
        
        if len(messages) <= 0:
            await update.callback_query.edit_message_text(
                text="Вы пока не создали ни одного мероприятия!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Меню", callback_data="a_return")]])
            )
            return
            
        keyboard = [[InlineKeyboardButton(f"{i+1}", callback_data=f"s_{message.db_id}")] for i, message in enumerate(messages)]
        keyboard.append([InlineKeyboardButton("Меню", callback_data="a_return")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text_list = []
        for i, message in enumerate(messages):
            escaped_text = self.escape_markdown_v2(message.text)
            text_list.append(f"{i+1}\\. {escaped_text} {self.format_time(message.time)} {context.chat_data['week'].get(message.day_of_week)}")
    
        text = '\n'.join(text_list)
        await update.callback_query.edit_message_text(
            text=text, 
            reply_markup=reply_markup, 
            parse_mode=constants.ParseMode.MARKDOWN_V2
        )

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

    # Остальные методы остаются без значительных изменений, но добавьте retry логику везде где есть сетевые запросы

    async def message_menu(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT
        _, command = update.callback_query.data.split('_')
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
        messager = update.message or update.callback_query
        if update.effective_chat.type != "private":
            await messager.reply_text("Эта команда работает только в личных сообщениях")
            return
            
        admin_id = update.effective_user.id
        if not self.db.get_admin_chat(admin_id):
            await messager.reply_text("Эта команда доступна только админам")
            return

        chat_id = self.db.get_admin_chat(admin_id)
        message = Message()
        message.chat_id = chat_id
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
            # Теперь всегда успешно, если пользователь админ чата
            self.db.set_chat_admin(chat.id, user.id)
            
            await update.message.reply_text(
                f"✅ Вы теперь администратор бота для этого чата!\n"
                f"ID чата: {chat.id}\n\n"
                f"Используйте /start в личных сообщениях с ботом для управления мероприятиями.",
                reply_to_message_id=update.message.message_id
                # Без parse_mode - обычный текст
            )
            
            # Отправляем приветственное сообщение в личку
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"✅ Вы назначены администратором для чата:\n"
                        f"Название: {chat.title}\n"
                        f"ID: {chat.id}\n\n"
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
        CallbackQueryHandler(bot.admin_panel, pattern='^a_'),
        CallbackQueryHandler(bot.message_render, pattern='^s_'),
        CallbackQueryHandler(bot.message_menu, pattern='^m_'),
        CallbackQueryHandler(bot.day_callback, pattern='^day_'),
        CallbackQueryHandler(bot.keep_time_callback, pattern='^keep_time'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.admin_input),
        MessageHandler(filters.StatusUpdate.MIGRATE, bot.handle_migration)
    ])
    
    application.add_handlers([
        ChatMemberHandler(bot.handle_chat_member_update),
        CallbackQueryHandler(bot.update_lists, pattern="^participate")
    ])

    print("Бот запускается...")
    application.run_polling()