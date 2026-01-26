import sqlite3

def migrate_database(db_name='mtg_bot.db'):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    # 1. Добавляем поле default_thread_id в chat_admins
    try:
        cursor.execute('ALTER TABLE chat_admins ADD COLUMN default_thread_id INTEGER')
        print("Добавлено поле default_thread_id в chat_admins")
    except sqlite3.OperationalError as e:
        print(f"Поле default_thread_id уже существует или ошибка: {e}")
    
    # 2. Добавляем поле message_thread_id в messages
    try:
        cursor.execute('ALTER TABLE messages ADD COLUMN message_thread_id INTEGER')
        print("Добавлено поле message_thread_id в messages")
    except sqlite3.OperationalError as e:
        print(f"Поле message_thread_id уже существует или ошибка: {e}")
    
    # 3. Обновляем существующие записи
    cursor.execute('UPDATE chat_admins SET default_thread_id = NULL WHERE default_thread_id IS NULL')
    cursor.execute('UPDATE messages SET message_thread_id = NULL WHERE message_thread_id IS NULL')
    
    conn.commit()
    conn.close()
    print("Миграция завершена успешно!")

if __name__ == '__main__':
    migrate_database()