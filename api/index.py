"""
Vercel serverless handler for Flask app
"""
import sys
import os
import json

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

# Vercel ожидает функцию handler
def handler(request):
    """Vercel serverless handler"""
    try:
        # Пробуем использовать WSGI адаптер от Vercel
        try:
            from vercel import WSGI
            wsgi = WSGI(app)
            return wsgi(request)
        except ImportError:
            # Если vercel модуль недоступен, используем прямой WSGI вызов
            print("⚠️ vercel модуль недоступен, используем прямой WSGI")
            
            # Получаем данные из request
            # Vercel передает request как объект с атрибутами или словарь
            method = getattr(request, 'method', None) or (request.get('method') if isinstance(request, dict) else 'GET')
            path = getattr(request, 'path', None) or (request.get('path') if isinstance(request, dict) else '/')
            query = getattr(request, 'queryString', None) or (request.get('queryString') if isinstance(request, dict) else '')
            headers = getattr(request, 'headers', None) or (request.get('headers') if isinstance(request, dict) else {})
            body = getattr(request, 'body', None) or (request.get('body') if isinstance(request, dict) else '')
            
            # Создаем WSGI environ
            environ = {
                'REQUEST_METHOD': method or 'GET',
                'PATH_INFO': path or '/',
                'QUERY_STRING': query or '',
                'wsgi.version': (1, 0),
                'wsgi.url_scheme': 'https',
                'wsgi.input': None,
                'wsgi.errors': sys.stderr,
                'wsgi.multithread': False,
                'wsgi.multiprocess': True,
                'wsgi.run_once': False,
                'SERVER_NAME': 'localhost',
                'SERVER_PORT': '443',
                'CONTENT_TYPE': headers.get('content-type', '') if headers else '',
                'CONTENT_LENGTH': str(len(body)) if body else '0',
            }
            
            # Добавляем HTTP заголовки
            if headers:
                for key, value in headers.items():
                    env_key = f'HTTP_{key.upper().replace("-", "_")}'
                    environ[env_key] = value
            
            # Переменные для ответа
            status = [None]
            response_headers = []
            
            def start_response(wsgi_status, wsgi_headers):
                status[0] = wsgi_status
                response_headers[:] = wsgi_headers
            
            # Вызываем Flask app
            response = app(environ, start_response)
            
            # Собираем тело ответа
            body_parts = []
            for chunk in response:
                if isinstance(chunk, bytes):
                    body_parts.append(chunk)
                else:
                    body_parts.append(chunk.encode('utf-8'))
            
            body_bytes = b''.join(body_parts)
            
            # Парсим статус код
            status_code = 200
            if status[0]:
                try:
                    status_code = int(status[0].split()[0])
                except:
                    pass
            
            # Формируем заголовки
            headers_dict = {}
            for header in response_headers:
                if len(header) == 2:
                    headers_dict[header[0]] = header[1]
            
            # Возвращаем ответ в формате Vercel
            return {
                'statusCode': status_code,
                'headers': headers_dict,
                'body': body_bytes.decode('utf-8', errors='ignore')
            }
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(f"❌ Ошибка handler: {error_msg}")
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'error': str(e),
                'type': type(e).__name__,
                'traceback': error_msg
            })
        }
