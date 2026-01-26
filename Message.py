from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta

class Message:
    def __init__(self):
        self.db_id = None
        self.chat_id = None
        self.message_thread_id = None  # <-- НОВОЕ ПОЛЕ: ID топика форума
        self.text = "Вечернее соревнование"
        self.participants = []
        self.maybe_participants = []
        self.date = None
        self.day_of_week = None
        self.day_of_notice = None
        self.time = "12:00"
        self.links = ""
        self.image = "[\u200b](https://i.pinimg.com/736x/a6/86/75/a686751d639e642196346106fb868623.jpg)"
        self.pin_id = None
        self.trigger = None
    
    def add_participant(self, user_info):
        """Добавляет участника с полной информацией"""
        user = {
            'id': user_info.id,
            'username': user_info.username,
            'full_name': user_info.full_name
        }
        
        if user not in self.participants:
            self.participants.append(user)
            # Удаляем из возможных, если есть
            self.maybe_participants = [u for u in self.maybe_participants if u['id'] != user['id']]

    def add_maybe_participant(self, user_info):
        """Добавляет возможного участника с полной информацией"""
        user = {
            'id': user_info.id,
            'username': user_info.username,
            'full_name': user_info.full_name
        }
        
        if user not in self.maybe_participants:
            self.maybe_participants.append(user)
            # Удаляем из основных, если есть
            self.participants = [u for u in self.participants if u['id'] != user['id']]

    def set_trigger(self, day_of_week, time_str):
        """Создает и сохраняет CronTrigger"""
        hour, minute = map(int, time_str.split(':'))
        self.trigger = CronTrigger(
            day_of_week=day_of_week,
            hour=hour,
            minute=minute,
            timezone=pytz.timezone("Europe/Moscow")
        )
        self.time = time_str
    
    def generate_message_text(self):
        """Генерирует текст финального сообщения с Markdown форматированием"""
        # Функция для экранирования специальных символов MarkdownV2
        def escape_markdown(text):
            if not text:
                return ""
            escape_chars = r'_*[]()~`>#+-=|{}.!'
            return ''.join(f'\\{char}' if char in escape_chars else char for char in text)
        
        # Экранируем все текстовые поля
        escaped_text = escape_markdown(self.text)
        escaped_image = escape_markdown(self.image)
        escaped_links = escape_markdown(self.links)
        
        # Экранируем имена пользователей
        participants_text = '\n\t'.join(
            f"[{escape_markdown(p['full_name'])}](t\\.me/{escape_markdown(p['username'])})" if p.get('username') 
            else escape_markdown(p['full_name']) 
            for p in self.participants
        ) if self.participants else "Пока никто"

        maybe_text = '\n\t'.join(
            f"[{escape_markdown(p['full_name'])}](t\\.me/{escape_markdown(p['username'])})" if p.get('username') 
            else escape_markdown(p['full_name']) 
            for p in self.maybe_participants
        ) if self.maybe_participants else "Пока никто"
        
        hours, minute = map(int, self.time.split(":"))
        time = f"{hours:02d}:{minute:02d}"
        
        # Формируем основное сообщение
        message = (
            f"{escaped_text}\n"
            f"\n\n{self.image}"
            f"*Участвую \\({len(self.participants)}\\):*\n\t{participants_text}\n\n"
            f"*Возможно \\({len(self.maybe_participants)}\\):*\n\t{maybe_text}"
        )
        
        # Безопасно добавляем подпись
        # Используем try-except для совместимости со старыми сообщениями
        try:
            # Пробуем вызвать метод add_signature если он существует
            if hasattr(self, 'add_signature') and callable(getattr(self, 'add_signature')):
                message = self.add_signature(message)
            else:
                # Для старых сообщений добавляем подпись напрямую
                message = f"{message}\n\n━━━━━━━━━━━━━━\nПо всем вопросам писать @NikTarYT"
        except Exception:
            # На всякий случай, если что-то пошло не так
            message = f"{message}\n\n━━━━━━━━━━━━━━\nПо всем вопросам писать @NikTarYT"
        
        return message

    def add_signature(self, text):
        """Добавляет подпись к тексту сообщения"""
        # Можно настроить подпись для разных чатов или условий
        signature = "━━━━━━━━━━━━━━\nПо всем вопросам писать @NikTarYT"
        return f"{text}\n\n{signature}"
    
    # Остальные методы остаются без изменений
    def remove_participant(self, user_info):
        """Удаляет участника из списка"""
        if user_info in self.participants:
            self.participants.remove(user_info)
    
    def remove_maybe_participant(self, user_info):
        """Удаляет возможного участника из списка"""
        if user_info in self.maybe_participants:
            self.maybe_participants.remove(user_info)