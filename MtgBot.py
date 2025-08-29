import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants, LinkPreviewOptions
from telegram.ext import ApplicationBuilder, ChatMemberHandler, CallbackContext, CallbackQueryHandler, filters, MessageHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
import pytz
import locale
from DB import Database
from Message import Message
from enum import Enum, auto

locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8')  # Для корректного отображения дат/времени
"""logging init"""
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
    LINKS = auto()
    TIME = auto()
class MtgBot:
    """Init and common methods"""
    def escape_markdown_v2(self, text: str) -> str:
        """
        Экранирует специальные символы MarkdownV2 для Telegram.
        Список символов: _ * [ ] ( ) ~ ` > # + - = | { } . !
        """
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
        await update.message.reply_text(
            "Привет! Я бот для организации мероприятий. Добавьте меня в группу как администратора."
        )

    async def init_scheduler(self, application):
        self.scheduler=AsyncIOScheduler()
        self.bot=application.bot
        self.scheduler.start()

        # Восстанавливаем триггеры из базы
        for message in self.db.init_load_all():
            if message.trigger:
                self.scheduler.add_job(
                    self.send_scheduled_message,
                    trigger=message.trigger,
                    args=[message.chat_id]
                )

    async def reschedule(self, day_of_week: str, hour: int, minute: int = 0, db_id: int = None):
        """Обновляет расписание с учётом GMT+3"""
        self.scheduler.remove_all_jobs()
            
        moscow_tz = pytz.timezone("Europe/Moscow")
            
        self.scheduler.add_job(
            self.send_scheduled_message,
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=moscow_tz,
            ),
            args=[db_id]
        )
        logger.info(f"Расписание обновлено: {day_of_week} в {hour}:{minute:02d} (GMT+3)")



    async def send_scheduled_message(self, db_id):
        """Отправка запланированного сообщения для конкретного чата"""
        message = self.db.load_message(db_id)
        if not message:
            logger.error(f"Сообщение {db_id} не найдено!")
            return
        
        try:
            msg = await self.bot.send_message(
                text=message.generate_message_text(),
                chat_id=message.chat_id,
                reply_markup=self.get_keyboard(message),
                parse_mode=constants.ParseMode.MARKDOWN_V2,
            )
            message.pin_id = msg.message_id
            # Сохраняем изменения в БД
            self.db.save_message(message)

            await self.bot.pin_chat_message(
                chat_id=message.chat_id,
                message_id=message.pin_id,
            )

        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения: {e}")

    def get_keyboard(self, message):
        keyboard = [
            [
                InlineKeyboardButton(f"{len(message.participants)} 👍", callback_data=f'participate_{message.db_id}'),
                InlineKeyboardButton(f"{len(message.maybe_participants)} ❓", callback_data=f'participatemaybe_{message.db_id}'),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    """handlers of lists"""
    async def update_lists(self, update: Update, context: CallbackContext):
        query = update.callback_query
        action, db_id = query.data.split('_')
        db_id = int(db_id)
        
        message = self.db.load_message(db_id)
        if not message:
            await query.answer("Это сообщение больше не активно")
            return
        
        if action == 'participate':
            if any(u['id'] == query.from_user.id for u in message.participants):
                message.participants = [u for u in message.participants if u['id'] != query.from_user.id]
            else:
                message.add_participant(query.from_user)
        elif action == 'participatemaybe':
            if any(u['id'] == query.from_user.id for u in message.maybe_participants):
                message.maybe_participants = [u for u in message.maybe_participants if u['id'] != query.from_user.id]
            else:
                message.add_maybe_participant(query.from_user)
        
        self.db.save_message(message)
        await self.update_message(context, message)

    

    async def update_message(self, context: CallbackContext, message: Message):
        try:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=message.pin_id,
                text=message.generate_message_text(),
                reply_markup=self.get_keyboard(message),
                parse_mode=constants.ParseMode.MARKDOWN_V2,
                )
        except Exception as e:
            logger.error(f"Ошибка при обновлении сообщения: {e}")

    """Test section | Admin panel"""

    async def send_admin_panel(self, update: Update, context: CallbackContext, chat_id: int = None):
        if not self.db.get_admin_chat(chat_id):
            logger.info(f"[ADMIN_PANEL] user {chat_id} attempt to call admin_panel, but was not detected in database!")
            await context.bot.send_message(text="Перед началом работы вы должны добавить меня в чат либо быть его админом!",chat_id=chat_id)
            return
        if not context.user_data.get( 'started' ):
            logger.info(f"[ADMIN_PANEL] user {chat_id} can't send message before start")
            return
        # TODO 
        # Emoji!!!!
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Добавить", callback_data="a_create"), InlineKeyboardButton("Список", callback_data="a_messages")],
            # [InlineKeyboardButton("Настройки", callback_data="a_options")]
        ])
        try:
            if context.chat_data['panel_state']:
                replayer = update.callback_query.message or update.message
                await replayer.edit_text(
                    text=f"*Админ панель*\n_"
                         f"Список \\~\\> Список активных мероприятий\n"
                         f"Добавить \\~\\> Добавление мероприятия_\n\n"
                        ,reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2)
                return
        except KeyError:
            logger.warning("[ADMIN_PANEL] Cannot edit message. Sending new one. Are you from \"a_return\" callback?")
        except Exception as e:
            logger.error(f"[ADMIN_PANEL] Cannot edit: {e}")
        
        await context.bot.send_message(
                        chat_id=chat_id,
                        parse_mode=constants.ParseMode.MARKDOWN_V2,
                        text=f"*Админ панель*\n_"
                            # f"_Настройки \\~\\> Настроить бота для чата\n"
                             f"Список \\~\\> Список активных мероприятий\n"
                             f"Добавить \\~\\> Добавление мероприятия_\n\n",
                        reply_markup=keyboard
                    )

    async def admin_panel(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT

        context.chat_data['admin_id']=update.effective_user.id
        _, command = update.callback_query.data.split('_')
        try:
            if command == "messages":
                await self.message_list(update, context)
            if command == "create":
                await self.create_message(update, context)
            if command == "return":
                context.chat_data['panel_state'] = True
                await self.send_admin_panel(update, context, context.chat_data['admin_id'])
            else:
                logger.warning(f"[ADMIN_PANEL] Cannot parse command \"{command}\" : undefined command")
                return
            logger.info(f"[ADMIN_PANEL] Parsing {command}")
        except Exception as e:
            logger.error(f"[ADMIN_PANEL] Cannot parse command: {e}")

    async def message_list(self, update: Update, context: CallbackContext):
        context.chat_data['db_id'] = None
        context.chat_data['week']={'mon': 'Пн','tue': 'Вт','wed': 'Ср','thu': 'Чт','fri': 'Пт','sat': 'Сб','sun': 'Вс'}
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
        text_list = [f"{i+1}\\. {message.text} {self.format_time(message.time)} {context.chat_data['week'].get(message.day_of_week)}" for i, message in enumerate(messages)]
        text = '\n'.join(text_list)
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=constants.ParseMode.MARKDOWN_V2)

    async def message_render(self, update: Update, context: CallbackContext):
        context.chat_data['week']={'mon': 'Пн','tue': 'Вт','wed': 'Ср','thu': 'Чт','fri': 'Пт','sat': 'Сб','sun': 'Вс'}
        try:
            _, message_id = update.callback_query.data.split('_')
        except:
            logger.info(f"[MESSAGE_RENDER] Returned from [ADMIN_RESCHEDULE]. The db_id is {context.chat_data['db_id']}")
        try:
            if context.chat_data['db_id']:
                db_id = context.chat_data['db_id']
                if self.db.load_message(db_id):
                   message_id = db_id
        except:
            logger.warning(f"[MESSAGE_RENDER] Cannot parse context.chat_data['db_id']. Instead using \"{message_id}\"")

        context.chat_data['db_id'] = message_id
        message = self.db.load_message(message_id)

        keyboard=[
            [InlineKeyboardButton("Текст", callback_data=f"m_text"), InlineKeyboardButton("Удалить", callback_data=f"m_delete")],
            [InlineKeyboardButton("Ссылки", callback_data=f"m_links"),InlineKeyboardButton("Перенести", callback_data=f"m_reschedule")],
            [InlineKeyboardButton("Список", callback_data="a_messages"),InlineKeyboardButton("Меню", callback_data="a_return")]
            ]
        if self.message_state == MessageState.DEFAULT:
            await update.callback_query.edit_message_text(
                text=message.generate_message_text(),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=constants.ParseMode.MARKDOWN_V2)
        else:
            await context.bot.edit_message_text(
                chat_id=update.message.chat_id,
                message_id=context.chat_data['edit_id'].id,
                text=message.generate_message_text(),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=constants.ParseMode.MARKDOWN_V2)

    async def message_menu(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT
        _, command = update.callback_query.data.split('_')
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Меню", callback_data='a_return')]])
        try:
            # TODO
            if command == "delete":
                await self.delete_message(update, context)
            if command == "text":
                self.message_state = MessageState.TEXT
                context.chat_data['edit_id'] = await update.callback_query.edit_message_text("Введите текст: ", reply_markup=keyboard)
            if command == "links":
                self.message_state = MessageState.LINKS
                context.chat_data['edit_id'] = await update.callback_query.edit_message_text("Введите ссылки: ", reply_markup=keyboard)
            if command == "reschedule":
                self.message_state = MessageState.TIME
                await self.admin_reschedule(update, context)

            logger.info(f"[MESSAGE_MENU] Parsing {command}")
        except Exception as e:
            logger.error(f"[MESSAGE_MENU] Cannot parse command: {e}")

    async def create_message(self, update: Update, context: CallbackContext):
            """Создает сообщение для группы, связанной с админом"""
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
            message = self.db.save_message(message)
            context.chat_data['db_id'] = message.db_id
            self.message_state = MessageState.TIME
            await self.admin_reschedule(update, context)

    
    async def delete_message(self, update: Update, context: CallbackContext):
        """Удаляет сообщение из текущего чата"""
        replayer = update.message or update.callback_query.message
        db_id = context.chat_data['db_id']
        if not self.db.get_admin_chat(update.effective_user.id):
            await replayer.reply_text("Эта команда доступна только админам")
            return
        message = self.db.load_message(db_id)
        if not message:
            await replayer.reply_text("В этом чате нет активного сообщения")
            return
        
        # Удаляем из базы и из активных
        self.db.delete_message(db_id)
        
        # Пытаемся удалить закрепленное сообщение
        try:
            if message.pin_id:
                await context.bot.unpin_chat_message(chat_id=message.chat_id, message_id=message.pin_id)
        except Exception as e:
            logger.error(f"[DELETER] Cannot unpin message: {e}")
        await self.message_list(update, context)

    async def admin_reschedule(self, update: Update, context: CallbackContext):
        replayer = update.message or update.callback_query.message
        """Обработчик команды /reschedule"""
        if update.effective_chat.type != "private":
            await replayer.reply_text("Эта команда работает только в личных сообщениях")
            return
        message_id = context.chat_data['db_id']
        # Сохраняем chat_id группы в контексте
        context.chat_data['message'] = self.db.load_message(message_id)
        
        days = [
            ["Пн", "mon"],
            ["Вт", "tue"],
            ["Ср", "wed"],
            ["Чт", "thu"],
            ["Пт", "fri"],
            ["Сб", "sat"],
            ["Вс", "sun"],
            ["Сегодня", "to"]]
        keyboard = [[InlineKeyboardButton(day[0], callback_data=f"day_{day[1]}")] for day in days]
        keyboard.append([InlineKeyboardButton("Меню", callback_data="a_return")])
        context.chat_data['edit_id'] = await replayer.edit_text(
            "Выберите день недели:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def day_callback(self, update: Update, context: CallbackContext):
        """Обработчик выбора дня недели"""
        query = update.callback_query
        await query.answer()
    
        selected_day = query.data.split('_')[1]
        week = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    
        if selected_day == "to":
            selected_day = week[datetime.now(pytz.timezone('Europe/Moscow')).weekday()]
    
        context.chat_data['message'].day_of_week = selected_day

        selected_day_index = week.index(selected_day)
        notice_day_index = (selected_day_index - 2) % 7  # %7 для циклического перехода
        context.chat_data['message'].day_of_notice = week[notice_day_index]
    
        h, m = context.chat_data['message'].time.split(":")
        h, m = int(h), int(m)
        context.chat_data['message'].time = f"{h:02d}:{m:02d}"
    
        await query.edit_message_text(
            text=f"Пожалуйста напишите час отправки\\!\n_в формате ЧЧ:ММ_",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Оставить нынешнее ({context.chat_data['message'].time})", callback_data="keep_time")]
            ])
        )

    async def admin_input(self, update: Update, context: CallbackContext):
        """Обработчик ввода времени"""
        # Проверяем формат времени
        try:
            message_id = context.chat_data['db_id']
        except Exception as e:
            logger.error(f"[ADMIN_INPUT] Cannot retrive message_id: {e}")
            return
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

        if self.message_state == MessageState.LINKS:
            message = self.db.load_message(message_id)
            message.links = update.message.text_markdown_v2
            await context.bot.delete_message(chat_id=update.effective_chat.id,message_id=update.message.id)
            self.db.save_message(message)
            await self.message_render(update, context)
        
        if self.message_state == MessageState.TEXT:
            message = self.db.load_message(message_id)
            message.text = update.message.text
            await context.bot.delete_message(chat_id=update.effective_chat.id,message_id=update.message.id)
            self.db.save_message(message)
            await self.message_render(update, context)
        
        if self.message_state == MessageState.DEFAULT:
            await self.send_admin_panel(update, context, update.effective_user.id)

    async def keep_time_callback(self, update: Update, context: CallbackContext):
        """Обработчик сохранения текущего времени"""
        await update.callback_query.answer()
        await self.finish_reschedule(update=update, context=context)

    async def finish_reschedule(self, update: Update, context: CallbackContext):
        self.message_state = MessageState.DEFAULT
        """Завершение настройки расписания"""
        message_obj = update.callback_query.message if update.callback_query else update.message
        await context.bot.delete_message(
                chat_id=message_obj.chat_id,
                message_id=message_obj.message_id
        )
        # Получаем chat_id группы из данных админа
        admin_chat_id = message_obj.chat_id
        group_chat_id = context.chat_data['message'].chat_id
        
        if not group_chat_id:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text="Ошибка: не найдена группа для настройки"
            )
            return
        
        current_message = context.chat_data['message']
        if not current_message:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text="Ошибка: не найдено активное сообщение для этой группы"
            )
            return

        hour, minute = map(int, current_message.time.split(':'))
        current_message.set_trigger(current_message.day_of_notice, f"{hour:02d}:{minute:02d}")
        
        # Обновляем сообщение в базе
        self.db.save_message(current_message)
        # Обновляем расписание
        await self.reschedule(current_message.day_of_notice, hour, minute, current_message.db_id)
        await context.bot.send_message(text="Мероприятие успешно создано!",chat_id=admin_chat_id)
        await self.send_admin_panel(update, context, update.effective_user.id)

    async def added_to_chat(self, update: Update, context: CallbackContext):
            """Обрабатывает добавление бота в группу"""
            for user in update.message.new_chat_members:
                if user.id == context.bot.id:
                    chat_id = update.effective_chat.id
                    added_by = update.message.from_user.id

                    if not self.db.add_chat_admin(chat_id, added_by):
                        logger.error("Cannot add admin to database!")
                    else:
                        logger.info("Successed adding admin to database")
                    
                    # TODO:
                    # Menu with keyboards for bot.admin_panel
                    await context.bot.send_message(
                        chat_id=added_by,
                        parse_mode=constants.ParseMode.MARKDOWN_V2,
                        text=f"Благодарю за добавление меня в чат\\!\nЧтобы я мог закреплять мероприятия назначьте меня админом\\."
                    )
                    await self.send_admin_panel(update, context, added_by)
    """Улетаем бля"""
    async def handle_migration(self, update: Update, context: CallbackContext):
        """Обновляет chat_id при миграции в супергруппу"""
        old_chat_id = update.message.migrate_from_chat_id  # Старый ID (уже не работает)
        new_chat_id = update.message.chat.id               # Новый ID (начинается на -100)
        
        logger.info(f"Группа мигрировала. Старый ID: {old_chat_id}, новый ID: {new_chat_id}")
        
        # Обновляем chat_id в БД
        if self.db.update_chat_id(old_chat_id, new_chat_id):
            logger.info("Chat_id успешно обновлён в базе данных")
        else:
            logger.error("Ошибка при обновлении chat_id в БД")

        # Перезапускаем триггеры с новым chat_id
        await self.reschedule_all_events(new_chat_id)
    async def reschedule_all_events(self, new_chat_id: int):
        """Перезапускает все запланированные сообщения для нового chat_id"""
        messages = self.db.load_messages()
        for msg in messages:
            if msg.trigger:
                self.scheduler.add_job(
                    self.send_scheduled_message,
                    trigger=msg.trigger,
                    args=[msg.db_id]
                )
        logger.info(f"Все события перезапущены для chat_id: {new_chat_id}")
    """Z T<FYENsQ"""
    async def handle_chat_member_update(self, update: Update, context: CallbackContext):
        chat_member = update.my_chat_member
        new_status = chat_member.new_chat_member.status

        # Если бота удалили или он вышел сам
        if new_status in ('left', 'kicked'):
            chat_id = update.effective_chat.id
            self.db.remove_chats_data(chat_id)
            logger.info(f"Бот удалён из чата {chat_id}")
"""main loop"""
if __name__ == '__main__':
    bot = MtgBot()

    token = os.getenv('MTG_BOT')
    if token is None:
        logging.error("Не удалось найти переменную окружения 'MTG_BOT'. Проверьте настройки на PythonAnywhere.")
        exit(1)

    application.add_handler(CommandHandler("start", bot.start_command))
    application = ApplicationBuilder().token(os.getenv('MTG_BOT')).build()
    application.post_init = bot.init_scheduler
    application.add_error_handler(error_handler)

    application.add_handlers([
        CallbackQueryHandler(bot.admin_panel, pattern='^a_'),
        CallbackQueryHandler(bot.message_render, pattern='^s_'),
        CallbackQueryHandler(bot.message_menu, pattern='^m_'),
        # depricated reschedule_admin calls
        CallbackQueryHandler(bot.day_callback, pattern='^day_'),
        CallbackQueryHandler(bot.keep_time_callback, pattern='^keep_time'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.admin_input),
        MessageHandler(filters.StatusUpdate.MIGRATE, bot.handle_migration)
    ])
    
    application.add_handlers([
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bot.added_to_chat),
        ChatMemberHandler(bot.handle_chat_member_update),
        CallbackQueryHandler(bot.update_lists, pattern="^participate")
    ])

    application.run_polling()