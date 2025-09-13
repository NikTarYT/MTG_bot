import sqlite3
import pickle
from Message import Message
from typing import Union

class Database:
    def __init__(self, db_name='mtg_bot.db'):
        self.conn = sqlite3.connect(db_name)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Таблица для админов чатов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_admins (
            chat_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            FOREIGN KEY(chat_id) REFERENCES messages(chat_id),
            PRIMARY KEY(chat_id, admin_id)
        )
        ''')
        
        # Таблица для сообщений
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            date TEXT,
            day_of_week TEXT,
            time TEXT,
            links TEXT,
            image BLOB,
            pin_id INTEGER,
            trigger BLOB
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS participants (
            message_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            status TEXT, 
            FOREIGN KEY(message_id) REFERENCES messages(id),
            PRIMARY KEY(message_id, user_id)
        )
        ''')
        
        self.conn.commit()

    def save_message(self, message):
        cursor = self.conn.cursor()
        
        # Сериализуем триггер
        trigger_data = pickle.dumps(message.trigger) if message.trigger else None
        
        if hasattr(message, 'db_id') and message.db_id:
            # Обновляем существующее сообщение
            cursor.execute('''
            UPDATE messages SET
                chat_id=?, text=?, date=?, day_of_week=?, time=?, links=?, image=?, pin_id=?, trigger=?
            WHERE id=?
            ''', (
                message.chat_id, message.text, message.date, message.day_of_week,
                message.time, message.links, message.image, message.pin_id,
                trigger_data, message.db_id
            ))
        else:
            # Добавляем новое сообщение
            cursor.execute('''
            INSERT INTO messages (chat_id, text, date, day_of_week, time, links, image, pin_id, trigger)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                message.chat_id, message.text, message.date, message.day_of_week,
                message.time, message.links, message.image, message.pin_id,
                trigger_data
            ))
            message.db_id = cursor.lastrowid
        
        # Сохраняем участников
        cursor.execute('DELETE FROM participants WHERE message_id=?', (message.db_id,))
        
        for user in message.participants:
            cursor.execute('''
            INSERT INTO participants (message_id, user_id, username, full_name, status)
            VALUES (?, ?, ?, ?, ?)
            ''', (
                message.db_id, user['id'], user.get('username'), user.get('full_name'),
                'participate'
            ))
        
        for user in message.maybe_participants:
            cursor.execute('''
            INSERT INTO participants (message_id, user_id, username, full_name, status)
            VALUES (?, ?, ?, ?, ?)
            ''', (
                message.db_id, user['id'], user.get('username'), user.get('full_name'),
                'maybe'
            ))
        
        self.conn.commit()
        return message

    def load_messages(self, admin_id):
        cursor = self.conn.cursor()
        cursor.execute('''
        SELECT m.* FROM messages m
        JOIN chat_admins ca ON m.chat_id = ca.chat_id
        WHERE ca.admin_id = ?
        ''', (admin_id,))
        
        messages = []
        
        for row in cursor.fetchall():
            db_id, chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data = row
            
            # Десериализуем триггер
            trigger = pickle.loads(trigger_data) if trigger_data else None
            
            message = Message()
            message.db_id = db_id
            message.chat_id = chat_id
            message.text = text
            message.date = date
            message.day_of_week = day_of_week
            message.time = time
            message.links = links
            message.image = image
            message.pin_id = pin_id
            message.trigger = trigger
            
            # Загружаем участников
            cursor.execute('''
            SELECT user_id, username, full_name, status FROM participants
            WHERE message_id=?
            ''', (db_id,))
            
            for user_id, username, full_name, status in cursor.fetchall():
                user = {
                    'id': user_id,
                    'username': username,
                    'full_name': full_name
                }
                
                if status == 'participate':
                    message.participants.append(user)
                else:
                    message.maybe_participants.append(user)
            
            messages.append(message)
        
        return messages

    def delete_message(self, db_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM messages WHERE id=?', (db_id,))
        cursor.execute('DELETE FROM participants WHERE message_id=?', (db_id,))
        self.conn.commit()

    def load_message(self, db_id):
        """Загружает сообщение по chat_id или вызывает исключение, если не найдено"""
        cursor = self.conn.cursor()
        
        # Ищем сообщение в базе
        cursor.execute('''
        SELECT chat_id, text, date, day_of_week, time, links, image, pin_id, trigger 
        FROM messages 
        WHERE id=?
        ''', (db_id,))
        
        message_data = cursor.fetchone()
        
        if not message_data:
            raise ValueError(f"Сообщение для чата {db_id} не найдено")
        
        # Распаковываем данные сообщения
        (chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data) = message_data
        
        # Создаем объект Message
        message = Message()
        message.db_id = db_id
        message.chat_id = chat_id
        message.text = text
        message.date = date
        message.day_of_week = day_of_week
        message.time = time
        message.links = links
        message.image = image
        message.pin_id = pin_id
        message.trigger = pickle.loads(trigger_data) if trigger_data else None
        
        # Загружаем участников
        cursor.execute('''
        SELECT user_id, username, full_name, status 
        FROM participants 
        WHERE message_id=?
        ''', (db_id,))
        
        for user_id, username, full_name, status in cursor.fetchall():
            user = {
                'id': user_id,
                'username': username,
                'full_name': full_name
            }
            
            if status == 'participate':
                message.participants.append(user)
            else:
                message.maybe_participants.append(user)
        
        return message

    # Обработка админов

    def set_chat_admin(self, chat_id: int, admin_id: int) -> bool:
        """Устанавливает админа для чата. Если уже есть админ - заменяет его."""
        with self.conn:
            cursor = self.conn.cursor()
            
            # Удаляем предыдущего админа этого чата (если есть)
            cursor.execute('DELETE FROM chat_admins WHERE chat_id=?', (chat_id,))
            
            # Проверяем, не является ли пользователь уже админом другого чата
            cursor.execute('SELECT 1 FROM chat_admins WHERE admin_id=?', (admin_id,))
            if cursor.fetchone():
                return False
                
            # Добавляем нового админа
            cursor.execute('''
            INSERT INTO chat_admins (chat_id, admin_id)
            VALUES (?, ?)
            ''', (chat_id, admin_id))
            
            return True

    def add_chat_admin(self, chat_id: int, admin_id: int) -> bool:
        """Добавляет админа для чата. Возвращает True если успешно"""
        with self.conn:
            cursor = self.conn.cursor()
            # Проверяем, не является ли пользователь уже админом другого чата
            cursor.execute('SELECT 1 FROM chat_admins WHERE admin_id=?', (admin_id,))
            if cursor.fetchone():
                return False
                
            cursor.execute('''
            INSERT INTO chat_admins (chat_id, admin_id)
            VALUES (?, ?)
            ''', (chat_id, admin_id))
            return True

    def get_admin_chat(self, admin_id: int) -> Union[str , None]:
        """Возвращает chat_id для админа или None"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT chat_id FROM chat_admins WHERE admin_id=?', (admin_id,))
        result = cursor.fetchone()
        return result[0] if result else None

    def get_chat_admins(self, chat_id: int) -> list[int]:
        """Возвращает список админов чата"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT admin_id FROM chat_admins WHERE chat_id=?', (chat_id,))
        return [row[0] for row in cursor.fetchall()]
    
    def init_load_all(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM messages')
        messages = []
        
        for row in cursor.fetchall():
            db_id, chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data = row
            
            # Десериализуем триггер
            trigger = pickle.loads(trigger_data) if trigger_data else None
            
            message = Message()
            message.db_id = db_id
            message.chat_id = chat_id
            message.text = text
            message.date = date
            message.day_of_week = day_of_week
            message.time = time
            message.links = links
            message.image = image
            message.pin_id = pin_id
            message.trigger = trigger
            
            # Загружаем участников
            cursor.execute('''
            SELECT user_id, username, full_name, status FROM participants
            WHERE message_id=?
            ''', (db_id,))
            
            for user_id, username, full_name, status in cursor.fetchall():
                user = {
                    'id': user_id,
                    'username': username,
                    'full_name': full_name
                }
                
                if status == 'participate':
                    message.participants.append(user)
                else:
                    message.maybe_participants.append(user)
            
            messages.append(message)
        
        return messages
    def update_chat_id(self, prev_id, next_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE messages SET chat_id=? WHERE chat_id=?
            ''', (next_id, prev_id))
        
        cursor.execute('UPDATE chat_admins SET chat_id=? WHERE chat_id=?', 
                  (next_id, prev_id))
    
        # Обновление в таблице participants (через связанные message_id)
        cursor.execute('''
        UPDATE participants 
        SET message_id = (
            SELECT m.id 
            FROM messages m 
            WHERE m.chat_id = ? AND m.id = participants.message_id
        )
        WHERE EXISTS (
            SELECT 1 
            FROM messages m 
            WHERE m.chat_id = ? AND m.id = participants.message_id
        )
        ''', (next_id, next_id))
        self.conn.commit()

    def remove_chats_data(self, chat_id: int) -> None:
        """Удаляет все данные, связанные с указанным чатом"""
        cursor = self.conn.cursor()
        
        # Получаем все сообщения чата, чтобы удалить связанных участников
        cursor.execute('SELECT id FROM messages WHERE chat_id=?', (chat_id,))
        message_ids = [row[0] for row in cursor.fetchall()]
        
        # Удаляем участников всех сообщений чата
        for message_id in message_ids:
            cursor.execute('DELETE FROM participants WHERE message_id=?', (message_id,))
        
        # Удаляем сами сообщения чата
        cursor.execute('DELETE FROM messages WHERE chat_id=?', (chat_id,))
        
        # Удаляем админов чата
        cursor.execute('DELETE FROM chat_admins WHERE chat_id=?', (chat_id,))
        
        self.conn.commit()