"""
Vercel serverless handler for Flask app
"""
import sys
import os

# Добавляем родительскую директорию в путь
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Импортируем Flask приложение
from license_web_admin import app

# Vercel ожидает функцию handler
def handler(request):
    """Vercel serverless handler"""
    # Vercel автоматически предоставляет WSGI адаптер
    # Просто возвращаем Flask app как WSGI приложение
    return app(request.environ, lambda status, headers: None)
