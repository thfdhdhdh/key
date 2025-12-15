"""
Vercel serverless handler for Flask app
Vercel автоматически обрабатывает Flask app как WSGI приложение
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
try:
    from license_web_admin import app, init_database
    
    # Инициализируем БД при импорте (для Vercel)
    try:
        init_database()
        print("✅ БД инициализирована")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        import traceback
        traceback.print_exc()
except Exception as e:
    print(f"❌ Ошибка импорта: {e}")
    import traceback
    traceback.print_exc()
    raise

# Vercel автоматически обрабатывает Flask app
# Экспортируем app напрямую, без функции handler
# Vercel сам создаст WSGI адаптер
