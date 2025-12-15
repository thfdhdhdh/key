"""
Vercel serverless handler for Flask app
"""
import sys
import os

# Устанавливаем переменную окружения для Vercel
os.environ['VERCEL'] = '1'

# Добавляем родительскую директорию в путь
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Импортируем Flask приложение
from license_web_admin import app, init_database

# Инициализируем БД при импорте (для Vercel)
try:
    init_database()
except Exception as e:
    print(f"Ошибка инициализации БД: {e}")

# Vercel ожидает функцию handler
def handler(request):
    """Vercel serverless handler"""
    # Используем WSGI адаптер от Vercel
    from vercel import WSGI
    
    # Создаем WSGI адаптер для Flask
    wsgi = WSGI(app)
    
    # Обрабатываем запрос и возвращаем ответ
    return wsgi(request)
