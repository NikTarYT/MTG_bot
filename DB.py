import sqlite3
import pickle
import logging
from Message import Message
from typing import Union

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_name='mtg_bot.db'):
        self.conn = sqlite3.connect(db_name)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Таблица для админов чатов с топиком по умолчанию
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_admins (
            chat_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            default_thread_id INTEGER,
            PRIMARY KEY(chat_id, admin_id)
        )
        ''')
        
        # Таблица для сообщений с поддержкой топиков
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_thread_id INTEGER,
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
                chat_id=?, text=?, date=?, day_of_week=?,
                time=?, links=?, image=?, pin_id=?, trigger=?, message_thread_id=?
            WHERE id=?
            ''', (
                message.chat_id, message.text, message.date,
                message.day_of_week, message.time, message.links, message.image,
                message.pin_id, trigger_data, message.message_thread_id, message.db_id
            ))
        else:
            # Добавляем новое сообщение - ВАЖНО: message_thread_id теперь последний
            cursor.execute('''
            INSERT INTO messages (chat_id, text, date, day_of_week, time, links, image, pin_id, trigger, message_thread_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                message.chat_id, message.text, message.date,
                message.day_of_week, message.time, message.links, message.image,
                message.pin_id, trigger_data, message.message_thread_id
            ))
            message.db_id = cursor.lastrowid
        
        # ВАЖНО: Удаляем всех старых участников перед добавлением новых
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
        """Загружает все мероприятия для администратора из ВСЕХ его чатов"""
        try:
            cursor = self.conn.cursor()
            
            # Ищем все чаты, где пользователь является администратором
            cursor.execute('''
                SELECT DISTINCT chat_id FROM chat_admins WHERE admin_id = ?
            ''', (admin_id,))
            
            admin_chats = cursor.fetchall()
            
            if not admin_chats:
                logger.info(f"[DATABASE] Admin {admin_id} has no chats")
                return []
            
            # Формируем список chat_id для запроса
            chat_ids = [chat[0] for chat in admin_chats]
            
            # Получаем все мероприятия из всех чатов администратора
            placeholders = ','.join('?' * len(chat_ids))
            query = f'''
                SELECT id, chat_id, message_thread_id, text, date, day_of_week, 
                    time, links, image, pin_id, trigger
                FROM messages 
                WHERE chat_id IN ({placeholders})
                ORDER BY id DESC
            '''
            
            cursor.execute(query, chat_ids)
            rows = cursor.fetchall()
            
            messages = []
            for row in rows:
                try:
                    # Распаковываем 11 полей
                    message_id, chat_id, message_thread_id, text, date, day_of_week, \
                    time, links, image, pin_id, trigger_data = row
                    
                    # Загружаем участников для этого мероприятия
                    cursor.execute('''
                        SELECT user_id FROM participants WHERE message_id = ?
                    ''', (message_id,))
                    
                    participants = [row[0] for row in cursor.fetchall()]
                    
                    # Обрабатываем триггер (с защитой от ошибок)
                    trigger = None
                    if trigger_data is not None:
                        if isinstance(trigger_data, bytes):
                            try:
                                trigger = pickle.loads(trigger_data)
                            except Exception as e:
                                logger.error(f"Cannot unpickle trigger: {e}")
                                trigger = None
                    
                    messages.append({
                        'id': message_id,
                        'chat_id': chat_id,
                        'topic_id': message_thread_id,
                        'text': text,
                        'date': date,
                        'day_of_week': day_of_week,
                        'time': time,
                        'links': links,
                        'image': image,
                        'pin_id': pin_id,
                        'participants': participants,
                        'participants_count': len(participants),
                        'trigger': trigger
                    })
                    
                except Exception as e:
                    logger.error(f"Error processing row: {e}")
                    continue
            
            logger.info(f"[DATABASE] Loaded {len(messages)} messages for admin {admin_id}")
            return messages
            
        except Exception as e:
            logger.error(f"[DATABASE] Error in load_messages: {e}")
            return []

    def user_has_chats(self, admin_id):
        """Проверяет, есть ли у пользователя привязанные чаты"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM chat_admins WHERE admin_id = ?', (admin_id,))
            count = cursor.fetchone()[0]
            return count > 0
        except Exception as e:
            logger.error(f"Error checking user chats: {e}")
            return False

    def delete_message(self, db_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM messages WHERE id=?', (db_id,))
        cursor.execute('DELETE FROM participants WHERE message_id=?', (db_id,))
        self.conn.commit()

    def load_message(self, db_id):
        """Загружает сообщение по id или вызывает исключение, если не найдено"""
        cursor = self.conn.cursor()
        
        # Ищем сообщение в базе - все 11 полей
        cursor.execute('SELECT * FROM messages WHERE id=?', (db_id,))
        
        message_data = cursor.fetchone()
        
        if not message_data:
            raise ValueError(f"Сообщение с ID {db_id} не найдено")
        
        # Распаковываем данные сообщения - 11 значений
        if len(message_data) == 11:
            db_id, chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data, message_thread_id = message_data
        else:
            # Для обратной совместимости
            db_id, chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data = message_data
            message_thread_id = None
        
        # Создаем объект Message
        message = Message()
        message.db_id = db_id
        message.chat_id = chat_id
        message.message_thread_id = message_thread_id  # <-- 11-й столбец
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

    def set_chat_admin(self, chat_id: int, admin_id: int, default_thread_id: int = None) -> bool:
        """Устанавливает админа для чата. Если уже есть - обновляет запись."""
        with self.conn:
            cursor = self.conn.cursor()
            
            # Проверяем, существует ли уже запись
            cursor.execute('SELECT * FROM chat_admins WHERE chat_id=? AND admin_id=?', (chat_id, admin_id))
            existing = cursor.fetchone()
            
            if existing:
                # Обновляем существующую запись
                cursor.execute('''
                UPDATE chat_admins 
                SET default_thread_id = ?
                WHERE chat_id = ? AND admin_id = ?
                ''', (default_thread_id, chat_id, admin_id))
            else:
                # Добавляем новую запись
                cursor.execute('''
                INSERT INTO chat_admins (chat_id, admin_id, default_thread_id)
                VALUES (?, ?, ?)
                ''', (chat_id, admin_id, default_thread_id))
            
            return True

    def add_chat_admin(self, chat_id: int, admin_id: int) -> bool:
        """Добавляет админа для чата. Возвращает True если успешно"""
        with self.conn:
            cursor = self.conn.cursor()
            
            # Просто добавляем, даже если уже существует
            cursor.execute('''
            INSERT OR IGNORE INTO chat_admins (chat_id, admin_id)
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
            # Теперь 11 полей, message_thread_id - последний
            if len(row) == 11:
                db_id, chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data, message_thread_id = row
            else:
                db_id, chat_id, text, date, day_of_week, time, links, image, pin_id, trigger_data = row
                message_thread_id = None
            
            # Десериализуем триггер
            trigger = pickle.loads(trigger_data) if trigger_data else None
            
            message = Message()
            message.db_id = db_id
            message.chat_id = chat_id
            message.message_thread_id = message_thread_id  # <-- 11-й столбец
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

    def get_admin_chats(self, admin_id: int) -> list:
        """Возвращает список чатов, где пользователь является админом"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT DISTINCT chat_id FROM chat_admins WHERE admin_id=?', (admin_id,))
        return [row[0] for row in cursor.fetchall()]
    
    def get_admin_chats_with_threads(self, admin_id: int) -> list:
        """Возвращает список чатов админа с default_thread_id"""
        cursor = self.conn.cursor()
        cursor.execute('''
        SELECT chat_id, default_thread_id 
        FROM chat_admins 
        WHERE admin_id = ?
        ''', (admin_id,))
        return cursor.fetchall()  # [(chat_id, thread_id), ...]
    
    def update_chat_thread(self, chat_id: int, admin_id: int, thread_id: int = None) -> bool:
        """Обновляет топик по умолчанию для чата админа"""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute('''
            UPDATE chat_admins 
            SET default_thread_id = ?
            WHERE chat_id = ? AND admin_id = ?
            ''', (thread_id, chat_id, admin_id))
            return cursor.rowcount > 0