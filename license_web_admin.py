"""
Веб-интерфейс для управления лицензиями
Админ-панель с генерацией ключей, просмотром устройств и управлением
"""
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
from flask_cors import CORS
import hashlib
import json
import os
import logging
from datetime import datetime, timedelta
import secrets
from functools import wraps

# Загрузка переменных окружения из .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv не установлен

# Проверка наличия psycopg2
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
CORS(app)

# Настройка логирования
# На Vercel не используем FileHandler
if os.getenv('VERCEL'):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('license_api.log'),
            logging.StreamHandler()
        ]
    )
logger = logging.getLogger(__name__)

# Секретный ключ из переменных окружения
SECRET_KEY = os.getenv("LICENSE_SECRET_KEY", "eb3aad213730b203eef01da1d9bbbc0c63070a008c2fba734999622ad9981479")
ADMIN_KEY = os.getenv("ADMIN_KEY", "CHANGE_THIS_ADMIN_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")  # Измените!

# Whitelist IP для доступа к админ-панели
# На Vercel whitelist отключен по умолчанию (разрешаем всем)
ADMIN_WHITELIST_ENABLED = os.getenv("ADMIN_WHITELIST_ENABLED", "false" if os.getenv('VERCEL') else "true").lower() == 'true'
ADMIN_WHITELIST = os.getenv("ADMIN_WHITELIST", "").split(",") if os.getenv("ADMIN_WHITELIST") else []
# Если whitelist пуст и не на Vercel, разрешаем доступ с localhost
if not ADMIN_WHITELIST and not os.getenv('VERCEL'):
    ADMIN_WHITELIST = ["127.0.0.1", "::1", "localhost"]

# Настройки БД
# Если есть POSTGRES_URL, DATABASE_URL или POSTGRES_PRISMA_URL, используем PostgreSQL
DATABASE_URL = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL') or os.getenv('POSTGRES_PRISMA_URL')
USE_SQLITE = os.getenv('USE_SQLITE', 'false' if DATABASE_URL else 'true').lower() == 'true'
# На Vercel используем /tmp (единственное место где можно писать)
DB_FILE = os.getenv('DB_FILE', '/tmp/licenses.db' if os.getenv('VERCEL') else 'licenses.db')

# Конфигурация PostgreSQL
if DATABASE_URL:
    # Используем строку подключения напрямую
    DB_CONFIG = {'dsn': DATABASE_URL}
else:
    # Используем отдельные параметры
    DB_CONFIG = {
        'host': os.getenv('POSTGRES_HOST') or os.getenv('DB_HOST', 'localhost'),
        'port': os.getenv('POSTGRES_PORT') or os.getenv('DB_PORT', '5432'),
        'database': os.getenv('POSTGRES_DATABASE') or os.getenv('DB_NAME', 'license_db'),
        'user': os.getenv('POSTGRES_USER') or os.getenv('DB_USER', 'postgres'),
        'password': os.getenv('POSTGRES_PASSWORD') or os.getenv('DB_PASSWORD', 'password')
    }

def get_db_connection():
    """Получение подключения к БД"""
    if USE_SQLITE:
        # Используем SQLite
        import sqlite3
        try:
            # На Vercel используем /tmp, но проверяем доступность
            db_path = DB_FILE
            if os.getenv('VERCEL'):
                # Убеждаемся что директория существует
                db_dir = os.path.dirname(db_path)
                if db_dir and not os.path.exists(db_dir):
                    try:
                        os.makedirs(db_dir, exist_ok=True)
                    except:
                        pass
                # Если /tmp недоступен, используем временную директорию
                if not os.access(os.path.dirname(db_path) if os.path.dirname(db_path) else '/tmp', os.W_OK):
                    # Fallback на временную директорию Python
                    import tempfile
                    db_path = os.path.join(tempfile.gettempdir(), 'licenses.db')
                    logger.warning(f"Используем временную директорию: {db_path}")
            
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            # Включаем WAL режим для лучшей производительности
            conn.execute('PRAGMA journal_mode=WAL;')
            return conn
        except Exception as e:
            logger.error(f"Ошибка подключения к SQLite: {e}, путь: {db_path}")
            # Пробуем in-memory БД как последний вариант (данные не сохранятся!)
            logger.warning("Пробуем in-memory БД (данные не сохранятся между запросами!)")
            try:
                conn = sqlite3.connect(':memory:', timeout=10.0)
                conn.row_factory = sqlite3.Row
                return conn
            except Exception as e2:
                logger.error(f"Ошибка создания in-memory БД: {e2}")
                return None
    else:
        # Используем PostgreSQL
        if not PSYCOPG2_AVAILABLE:
            logger.error("psycopg2 не установлен. Используйте: pip install psycopg2-binary")
            return None
        try:
            # Если есть строка подключения, используем её
            if 'dsn' in DB_CONFIG:
                conn = psycopg2.connect(DB_CONFIG['dsn'])
            else:
                conn = psycopg2.connect(**DB_CONFIG)
            return conn
        except Exception as e:
            logger.error(f"Ошибка подключения к PostgreSQL: {e}")
            logger.error(f"Конфигурация: {'dsn=***' if 'dsn' in DB_CONFIG else DB_CONFIG}")
            return None

def get_cursor(conn):
    """Получение курсора с правильным типом"""
    if USE_SQLITE:
        return conn.cursor()
    else:
        from psycopg2.extras import RealDictCursor
        return conn.cursor(cursor_factory=RealDictCursor)

def execute_query(cur, query, params=None):
    """Универсальное выполнение запроса для SQLite и PostgreSQL"""
    if USE_SQLITE:
        # SQLite использует ? вместо %s
        if params:
            # Конвертируем %s в ? для SQLite
            query = query.replace('%s', '?')
        cur.execute(query, params)
    else:
        # PostgreSQL использует %s
        cur.execute(query, params)

def init_database():
    """Инициализация БД"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("Не удалось подключиться к БД при инициализации")
            return False
        
        try:
            cur = conn.cursor()
            
            if USE_SQLITE:
                # SQLite синтаксис
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS licenses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT UNIQUE NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP,
                        device_id TEXT,
                        device_info TEXT,
                        activated_at TIMESTAMP,
                        status TEXT DEFAULT 'active',
                        last_check TIMESTAMP,
                        heartbeat_last TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS license_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        license_key TEXT,
                        action TEXT,
                        device_id TEXT,
                        ip_address TEXT,
                        user_agent TEXT,
                        message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_licenses_key ON licenses(key)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_licenses_device ON licenses(device_id)")
            else:
                # PostgreSQL синтаксис
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS licenses (
                        id SERIAL PRIMARY KEY,
                        key VARCHAR(50) UNIQUE NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP,
                        device_id VARCHAR(64),
                        device_info JSONB,
                        activated_at TIMESTAMP,
                        status VARCHAR(20) DEFAULT 'active',
                        last_check TIMESTAMP,
                        heartbeat_last TIMESTAMP
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS license_logs (
                        id SERIAL PRIMARY KEY,
                        license_key VARCHAR(50),
                        action VARCHAR(50),
                        device_id VARCHAR(64),
                        ip_address VARCHAR(45),
                        user_agent TEXT,
                        message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_licenses_key ON licenses(key)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_licenses_device ON licenses(device_id)")
            
            conn.commit()
            cur.close()
            conn.close()
            logger.info("БД успешно инициализирована")
            return True
        except Exception as e:
            logger.error(f"Ошибка выполнения SQL при инициализации БД: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass
            return False
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        return False

def verify_signature(data, signature):
    """Проверка подписи запроса"""
    data_copy = data.copy()
    data_copy.pop('signature', None)
    data_copy.pop('timestamp', None)
    data_copy.pop('nonce', None)
    data_str = json.dumps(data_copy, sort_keys=True)
    hash1 = hashlib.sha256((data_str + SECRET_KEY).encode()).hexdigest()
    expected_signature = hashlib.sha256((hash1 + SECRET_KEY).encode()).hexdigest()
    return expected_signature == signature

def check_timestamp(timestamp):
    """Проверка временной метки"""
    current_time = int(datetime.now().timestamp())
    return abs(current_time - timestamp) < 300

def check_ip_whitelist():
    """Проверка IP в whitelist"""
    # Если whitelist отключен, разрешаем всем
    if not ADMIN_WHITELIST_ENABLED:
        return True
    
    # Если whitelist пуст, разрешаем всем
    if not ADMIN_WHITELIST:
        return True
    
    client_ip = request.remote_addr
    # Проверяем также через заголовки прокси (важно для Vercel)
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        client_ip = forwarded_for.split(',')[0].strip()
    
    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        client_ip = real_ip
    
    # Проверяем Vercel заголовки
    vercel_ip = request.headers.get('X-Vercel-Forwarded-For')
    if vercel_ip:
        client_ip = vercel_ip.split(',')[0].strip()
    
    # Нормализуем IP (убираем порт если есть)
    if ':' in client_ip and not client_ip.startswith('['):
        client_ip = client_ip.split(':')[0]
    
    return client_ip in ADMIN_WHITELIST or any(ip.strip() in ADMIN_WHITELIST for ip in [client_ip])

def require_login(f):
    """Декоратор для проверки авторизации и IP whitelist"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Проверка IP whitelist
        if not check_ip_whitelist():
            client_ip = request.remote_addr
            forwarded_for = request.headers.get('X-Forwarded-For', '')
            real_ip = request.headers.get('X-Real-IP', '')
            logger.warning(f"Доступ запрещен. IP: {client_ip}, X-Forwarded-For: {forwarded_for}, X-Real-IP: {real_ip}, Whitelist: {ADMIN_WHITELIST}")
            return jsonify({"error": "Доступ запрещен", "ip": client_ip}), 403
        
        if 'admin_logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# HTML шаблоны
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Вход</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #ffffff;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            color: #000;
        }
        .login-container {
            background: #ffffff;
            padding: 60px 40px;
            width: 100%;
            max-width: 400px;
            border: 1px solid #e0e0e0;
        }
        h1 {
            text-align: center;
            margin-bottom: 40px;
            color: #000;
            font-size: 24px;
            font-weight: 300;
            letter-spacing: -0.5px;
        }
        input {
            width: 100%;
            padding: 14px;
            margin: 10px 0;
            border: 1px solid #d0d0d0;
            border-radius: 0;
            font-size: 14px;
            background: #fff;
            color: #000;
        }
        input:focus {
            outline: none;
            border-color: #000;
        }
        button {
            width: 100%;
            padding: 14px;
            background: #000;
            color: white;
            border: none;
            border-radius: 0;
            font-size: 14px;
            cursor: pointer;
            margin-top: 20px;
            font-weight: 400;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }
        button:hover { background: #333; }
        .error {
            color: #d32f2f;
            margin-top: 15px;
            text-align: center;
            font-size: 13px;
        }
        .ip-info {
            margin-top: 20px;
            padding-top: 20px;
            border-top: 1px solid #e0e0e0;
            text-align: center;
            font-size: 11px;
            color: #999;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Вход</h1>
        <form method="POST">
            <input type="password" name="password" placeholder="Пароль" required autofocus>
            <button type="submit">Войти</button>
        </form>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <div class="ip-info">IP: {{ client_ip }}</div>
    </div>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>License Manager</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #0f0f23 0%, #1a1a2e 50%, #16213e 100%);
            min-height: 100vh;
            color: #e4e4e7;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 30px 20px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .header h1 {
            font-size: 28px;
            font-weight: 600;
            background: linear-gradient(90deg, #00d4ff, #7c3aed);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .header h1::before {
            content: "🔐";
            -webkit-text-fill-color: initial;
        }
        .logout {
            padding: 10px 20px;
            background: rgba(239, 68, 68, 0.1);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.3);
            border-radius: 8px;
            text-decoration: none;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s;
        }
        .logout:hover {
            background: rgba(239, 68, 68, 0.2);
            border-color: #ef4444;
        }
        .card {
            background: rgba(30, 30, 45, 0.6);
            backdrop-filter: blur(20px);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 24px;
        }
        .card h2 {
            font-size: 16px;
            font-weight: 500;
            color: #a1a1aa;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .form-row {
            display: flex;
            gap: 12px;
            align-items: flex-end;
        }
        .form-group {
            flex: 1;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #71717a;
            font-size: 12px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        input, select {
            width: 100%;
            padding: 12px 16px;
            background: rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 10px;
            font-size: 14px;
            color: #e4e4e7;
            font-family: inherit;
            transition: all 0.2s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #7c3aed;
            box-shadow: 0 0 0 3px rgba(124, 58, 237, 0.2);
        }
        input::placeholder {
            color: #52525b;
        }
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .btn-primary {
            background: linear-gradient(135deg, #7c3aed, #5b21b6);
            color: white;
        }
        .btn-primary:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 20px rgba(124, 58, 237, 0.4);
        }
        .btn-secondary {
            background: rgba(255,255,255,0.05);
            color: #a1a1aa;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .btn-secondary:hover {
            background: rgba(255,255,255,0.1);
            color: #e4e4e7;
        }
        .btn-danger {
            background: rgba(239, 68, 68, 0.15);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.3);
        }
        .btn-danger:hover {
            background: rgba(239, 68, 68, 0.25);
        }
        .btn-success {
            background: rgba(34, 197, 94, 0.15);
            color: #22c55e;
            border: 1px solid rgba(34, 197, 94, 0.3);
        }
        .btn-success:hover {
            background: rgba(34, 197, 94, 0.25);
        }
        .btn-warning {
            background: rgba(251, 146, 60, 0.15);
            color: #fb923c;
            border: 1px solid rgba(251, 146, 60, 0.3);
        }
        .btn-warning:hover {
            background: rgba(251, 146, 60, 0.25);
        }
        .btn-small {
            padding: 8px 12px;
            font-size: 12px;
            border-radius: 8px;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: rgba(0,0,0,0.2);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
        }
        .stat-value {
            font-size: 32px;
            font-weight: 600;
            color: #e4e4e7;
        }
        .stat-label {
            font-size: 11px;
            color: #71717a;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 4px;
        }
        .stat-card.active .stat-value { color: #22c55e; }
        .stat-card.blocked .stat-value { color: #ef4444; }
        .stat-card.total .stat-value { color: #7c3aed; }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th {
            text-align: left;
            padding: 12px 16px;
            font-size: 11px;
            font-weight: 500;
            color: #71717a;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        td {
            padding: 16px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            font-size: 13px;
        }
        tr:hover {
            background: rgba(255,255,255,0.02);
        }
        .key-cell {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .key-text {
            font-family: 'JetBrains Mono', 'Courier New', monospace;
            font-size: 13px;
            color: #00d4ff;
            cursor: pointer;
        }
        .key-text:hover {
            text-decoration: underline;
        }
        .copy-btn {
            background: rgba(255,255,255,0.05);
            border: none;
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            color: #71717a;
            font-size: 12px;
            transition: all 0.2s;
        }
        .copy-btn:hover {
            background: rgba(255,255,255,0.1);
            color: #e4e4e7;
        }
        .status {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 500;
        }
        .status-active {
            background: rgba(34, 197, 94, 0.15);
            color: #22c55e;
        }
        .status-blocked {
            background: rgba(239, 68, 68, 0.15);
            color: #ef4444;
        }
        .status-expired {
            background: rgba(251, 146, 60, 0.15);
            color: #fb923c;
        }
        .device-info {
            font-size: 11px;
            color: #52525b;
            margin-top: 4px;
        }
        .action-buttons {
            display: flex;
            gap: 6px;
        }
        .result-box {
            margin-top: 16px;
            padding: 16px;
            border-radius: 10px;
            font-size: 13px;
        }
        .result-success {
            background: rgba(34, 197, 94, 0.1);
            border: 1px solid rgba(34, 197, 94, 0.3);
            color: #22c55e;
        }
        .result-error {
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
        }
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
        }
        .modal-content {
            background: #1e1e2d;
            margin: 50px auto;
            padding: 24px;
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            width: 90%;
            max-width: 550px;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .modal-header h2 {
            margin: 0;
            font-size: 18px;
            color: #e4e4e7;
        }
        .close {
            background: rgba(255,255,255,0.05);
            border: none;
            color: #71717a;
            font-size: 20px;
            cursor: pointer;
            width: 32px;
            height: 32px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .close:hover {
            background: rgba(255,255,255,0.1);
            color: #e4e4e7;
        }
        .info-row {
            display: flex;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .info-label {
            width: 140px;
            font-size: 12px;
            color: #71717a;
            text-transform: uppercase;
        }
        .info-value {
            flex: 1;
            color: #e4e4e7;
            font-size: 14px;
        }
        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 14px 20px;
            border-radius: 10px;
            font-size: 13px;
            z-index: 2000;
            animation: slideIn 0.3s ease;
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .notification.success {
            background: rgba(34, 197, 94, 0.9);
            color: white;
        }
        .notification.error {
            background: rgba(239, 68, 68, 0.9);
            color: white;
        }
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #52525b;
        }
        .empty-state svg {
            width: 64px;
            height: 64px;
            margin-bottom: 16px;
            opacity: 0.3;
        }
        .search-row {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        .search-row input {
            flex: 1;
        }
        .info-grid {
            display: grid;
            gap: 16px;
            margin-bottom: 24px;
        }
        .info-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .info-item .info-label {
            color: #71717a;
            font-size: 13px;
        }
        .manage-actions {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .manage-actions .btn {
            width: 100%;
            justify-content: center;
            padding: 14px 20px;
            font-size: 14px;
        }
        .manage-actions .btn span {
            margin-right: 8px;
        }
        #manageModal .modal-content {
            max-width: 450px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>License Manager</h1>
            <a href="/logout" class="logout">Выйти</a>
        </div>

        <div class="card">
            <h2>✨ Генерация ключа</h2>
            <form id="generateForm">
                <div class="form-row">
                    <div class="form-group">
                        <label>Срок действия (дней)</label>
                        <input type="number" name="days" placeholder="Пусто = бессрочный" min="1">
                    </div>
                    <button type="submit" class="btn btn-primary">+ Создать</button>
                </div>
            </form>
        </div>

        <div class="card">
            <h2>📋 Список лицензий</h2>
            <div id="statsContainer"></div>
            <div class="search-row">
                <input type="text" id="searchKey" placeholder="🔍 Поиск..." onkeyup="loadLicenses()">
                <button onclick="loadLicenses()" class="btn btn-secondary">↻</button>
            </div>
            <div id="licensesTable"></div>
        </div>
    </div>
    
    <!-- Модальное окно управления -->
    <div id="manageModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>⚙️ Управление ключом</h2>
                <button class="close" onclick="closeManageModal()">&times;</button>
            </div>
            <div id="manageContent"></div>
        </div>
    </div>


    <script>
        function generateKey() {
            const form = document.getElementById('generateForm');
            const formData = new FormData(form);
            const days = formData.get('days') || null;

            fetch('/api/generate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({days: days ? parseInt(days) : null})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showNotification('✅ Ключ создан: ' + data.key, 'success');
                    form.reset();
                    loadLicenses();
                } else {
                    showNotification('Ошибка: ' + data.message, 'error');
                }
            });
        }

        function loadLicenses() {
            const search = document.getElementById('searchKey').value;
            fetch('/api/licenses' + (search ? '?search=' + encodeURIComponent(search) : ''))
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    updateStats(data.licenses);
                    
                    if (data.licenses.length === 0) {
                        document.getElementById('licensesTable').innerHTML = '<div class="empty-state"><div style="font-size: 48px; margin-bottom: 16px;">📭</div><p>Нет лицензий</p></div>';
                        return;
                    }
                    
                    let html = '<table><thead><tr><th>Ключ</th><th>Статус</th><th>Создан</th><th>Истекает</th><th>Устройство</th><th></th></tr></thead><tbody>';
                    data.licenses.forEach(lic => {
                        const statusText = lic.status === 'active' ? '● Активен' : (lic.status === 'blocked' ? '● Заблокирован' : '● Истёк');
                        const expires = lic.expires_at ? new Date(lic.expires_at).toLocaleDateString('ru-RU') : '∞';
                        let device = '<span style="color: #52525b;">—</span>';
                        if (lic.device_id) {
                            device = '<span style="color: #22c55e;">● Да</span>';
                        }
                        
                        const licJson = JSON.stringify(lic).replace(/'/g, "\\'").replace(/"/g, '&quot;');
                        
                        html += '<tr>' +
                            '<td><span class="key-text">' + escapeHtml(lic.key) + '</span></td>' +
                            '<td><span class="status status-' + lic.status + '">' + statusText + '</span></td>' +
                            '<td>' + new Date(lic.created_at).toLocaleDateString('ru-RU') + '</td>' +
                            '<td>' + expires + '</td>' +
                            '<td>' + device + '</td>' +
                            '<td><button class="btn btn-secondary btn-small" onclick=\'openManage(' + licJson + ')\'>⚙️ Управление</button></td>' +
                            '</tr>';
                    });
                    html += '</tbody></table>';
                    document.getElementById('licensesTable').innerHTML = html;
                } else {
                    showNotification('Ошибка загрузки: ' + (data.message || 'Неизвестная ошибка'), 'error');
                }
            })
            .catch(err => {
                console.error('Ошибка:', err);
                showNotification('Ошибка загрузки лицензий', 'error');
            });
        }
        
        function updateStats(licenses) {
            const stats = {
                total: licenses.length,
                active: licenses.filter(l => l.status === 'active').length,
                blocked: licenses.filter(l => l.status === 'blocked').length,
                expired: licenses.filter(l => l.status === 'expired').length,
                activated: licenses.filter(l => l.device_id).length
            };
            
            document.getElementById('statsContainer').innerHTML = 
                '<div class="stats">' +
                '<div class="stat-card total"><div class="stat-value">' + stats.total + '</div><div class="stat-label">Всего</div></div>' +
                '<div class="stat-card active"><div class="stat-value">' + stats.active + '</div><div class="stat-label">Активных</div></div>' +
                '<div class="stat-card blocked"><div class="stat-value">' + stats.blocked + '</div><div class="stat-label">Заблокировано</div></div>' +
                '<div class="stat-card"><div class="stat-value">' + stats.activated + '</div><div class="stat-label">Привязано</div></div>' +
                '</div>';
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // ===== МОДАЛЬНОЕ ОКНО УПРАВЛЕНИЯ =====
        let currentLicense = null;
        
        function openManage(lic) {
            currentLicense = lic;
            const modal = document.getElementById('manageModal');
            const content = document.getElementById('manageContent');
            
            let deviceInfo = '<span style="color:#52525b">Не привязан</span>';
            if (lic.device_id) {
                deviceInfo = '<span style="color:#22c55e">● Привязан</span><br><small style="color:#71717a">' + lic.device_id.substring(0, 20) + '...</small>';
            }
            
            const expires = lic.expires_at ? new Date(lic.expires_at).toLocaleDateString('ru-RU') : '∞ Бессрочно';
            
            content.innerHTML = 
                '<div style="background:rgba(0,0,0,0.3); padding:16px; border-radius:12px; margin-bottom:20px;">' +
                '<div style="font-family:monospace; font-size:16px; color:#00d4ff; word-break:break-all;">' + lic.key + '</div>' +
                '</div>' +
                '<div class="info-grid">' +
                '<div class="info-item"><span class="info-label">Статус</span><span class="status status-' + lic.status + '">' + (lic.status === 'active' ? '● Активен' : '● Заблокирован') + '</span></div>' +
                '<div class="info-item"><span class="info-label">Истекает</span><span>' + expires + '</span></div>' +
                '<div class="info-item"><span class="info-label">Устройство</span><span>' + deviceInfo + '</span></div>' +
                '</div>' +
                '<div class="manage-actions">' +
                (lic.status === 'active' ? 
                    '<button class="btn btn-danger" onclick="doBlock()"><span>🚫</span> Заблокировать</button>' :
                    '<button class="btn btn-success" onclick="doUnblock()"><span>✅</span> Разблокировать</button>') +
                (lic.device_id ? 
                    '<button class="btn btn-warning" onclick="doUnbind()"><span>🔓</span> Отвязать устройство</button>' : '') +
                '<button class="btn btn-danger" onclick="doDelete()" style="background:rgba(220,38,38,0.2); border-color:rgba(220,38,38,0.5);"><span>🗑️</span> Удалить ключ</button>' +
                '</div>';
            
            modal.style.display = 'block';
        }
        
        function closeManageModal() {
            document.getElementById('manageModal').style.display = 'none';
            currentLicense = null;
        }
        
        function doBlock() {
            if (!currentLicense) return;
            apiAction('/api/block', currentLicense.key, 'Ключ заблокирован');
        }
        
        function doUnblock() {
            if (!currentLicense) return;
            apiAction('/api/unblock', currentLicense.key, 'Ключ разблокирован');
        }
        
        function doUnbind() {
            if (!currentLicense) return;
            if (!confirm('Отвязать устройство? Ключ можно будет активировать на другом устройстве.')) return;
            apiAction('/api/unbind', currentLicense.key, 'Устройство отвязано');
        }
        
        function doDelete() {
            if (!currentLicense) return;
            if (!confirm('Удалить ключ ' + currentLicense.key + '? Это действие нельзя отменить!')) return;
            apiAction('/api/delete', currentLicense.key, 'Ключ удален');
        }
        
        function apiAction(url, key, successMsg) {
            fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({key: key})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showNotification(successMsg, 'success');
                    closeManageModal();
                    loadLicenses();
                } else {
                    showNotification('Ошибка: ' + (data.message || 'Неизвестная ошибка'), 'error');
                }
            })
            .catch(err => {
                console.error('Ошибка:', err);
                showNotification('Ошибка сети', 'error');
            });
        }
        
        // Закрытие по клику вне модала
        window.onclick = function(event) {
            if (event.target.classList.contains('modal')) {
                event.target.style.display = 'none';
            }
        }

        function showNotification(message, type) {
            const existing = document.querySelector('.notification');
            if (existing) existing.remove();
            
            const notification = document.createElement('div');
            notification.className = 'notification ' + type;
            notification.textContent = message;
            document.body.appendChild(notification);
            setTimeout(() => {
                notification.style.opacity = '0';
                notification.style.transform = 'translateX(100%)';
                setTimeout(() => notification.remove(), 300);
            }, 3000);
        }

        document.getElementById('generateForm').addEventListener('submit', function(e) {
            e.preventDefault();
            generateKey();
        });

        document.getElementById('searchKey').addEventListener('keyup', function(e) {
            if (e.key === 'Enter') {
                loadLicenses();
            }
        });

        // Загружаем при загрузке страницы
        loadLicenses();
        setInterval(loadLicenses, 30000); // Обновление каждые 30 секунд
    </script>
</body>
</html>
"""

@app.route('/')
@require_login
def index():
    """Главная страница админ-панели"""
    return render_template_string(ADMIN_HTML)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа"""
    # Проверка IP whitelist
    if not check_ip_whitelist():
        client_ip = request.remote_addr
        forwarded_for = request.headers.get('X-Forwarded-For', '')
        real_ip = request.headers.get('X-Real-IP', '')
        logger.warning(f"Попытка входа с запрещенного IP: {client_ip}, X-Forwarded-For: {forwarded_for}, X-Real-IP: {real_ip}, Whitelist: {ADMIN_WHITELIST}")
        return jsonify({"error": "Доступ запрещен. Ваш IP не в whitelist", "ip": client_ip}), 403
    
    # Получаем IP клиента
    client_ip = request.remote_addr
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        client_ip = forwarded_for.split(',')[0].strip()
    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        client_ip = real_ip
    
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return redirect(url_for('index'))
        else:
            return render_template_string(LOGIN_HTML, error='Неверный пароль', client_ip=client_ip)
    return render_template_string(LOGIN_HTML, client_ip=client_ip)

@app.route('/logout')
def logout():
    """Выход"""
    session.pop('admin_logged_in', None)
    return redirect(url_for('login'))

# API endpoints для веб-интерфейса
@app.route('/api/generate', methods=['POST'])
@require_login
def api_generate():
    """Генерация ключа через веб-интерфейс"""
    try:
        data = request.json
        days = data.get('days')
        
        key = f"TS-{secrets.token_hex(8).upper()}"
        expires_at = None
        if days:
            expires_at = datetime.now() + timedelta(days=days)
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = conn.cursor()
        if USE_SQLITE:
            execute_query(cur, """
                INSERT INTO licenses (key, expires_at, status)
                VALUES (?, ?, 'active')
            """, (key, expires_at.isoformat() if expires_at else None))
        else:
            execute_query(cur, """
                INSERT INTO licenses (key, expires_at, status)
                VALUES (%s, %s, 'active')
            """, (key, expires_at))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "key": key}), 200
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/licenses')
@require_login
def api_licenses():
    """Получение списка лицензий"""
    try:
        search = request.args.get('search', '')
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        if search:
            if USE_SQLITE:
                execute_query(cur, "SELECT * FROM licenses WHERE key LIKE ? ORDER BY created_at DESC", (f'%{search}%',))
            else:
                execute_query(cur, "SELECT * FROM licenses WHERE key LIKE %s ORDER BY created_at DESC", (f'%{search}%',))
        else:
            execute_query(cur, "SELECT * FROM licenses ORDER BY created_at DESC")
        
        raw_licenses = cur.fetchall()
        licenses = []
        # Конвертируем строки БД в обычные dict + приводим даты к строкам
        for row in raw_licenses:
            lic = dict(row) if USE_SQLITE else row
            created = lic.get('created_at')
            expires = lic.get('expires_at')
            activated = lic.get('activated_at')
            if created and hasattr(created, 'isoformat'):
                lic['created_at'] = created.isoformat()
            if expires and hasattr(expires, 'isoformat'):
                lic['expires_at'] = expires.isoformat()
            if activated and hasattr(activated, 'isoformat'):
                lic['activated_at'] = activated.isoformat()
            licenses.append(lic)
        
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "licenses": licenses}), 200
    except Exception as e:
        logger.error(f"Ошибка получения лицензий: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/block', methods=['POST'])
@require_login
def api_block():
    """Блокировка ключа"""
    try:
        data = request.json
        key = data.get('key')
        
        if not key:
            return jsonify({"success": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        if USE_SQLITE:
            execute_query(cur, "UPDATE licenses SET status = 'blocked' WHERE key = ?", (key,))
        else:
            execute_query(cur, "UPDATE licenses SET status = 'blocked' WHERE key = %s", (key,))
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info(f"Ключ {key} заблокирован")
        return jsonify({"success": True, "message": "Ключ заблокирован"}), 200
    except Exception as e:
        logger.error(f"Ошибка блокировки: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/unblock', methods=['POST'])
@require_login
def api_unblock():
    """Разблокировка ключа"""
    try:
        data = request.json
        key = data.get('key')
        
        if not key:
            return jsonify({"success": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        if USE_SQLITE:
            execute_query(cur, "UPDATE licenses SET status = 'active' WHERE key = ?", (key,))
        else:
            execute_query(cur, "UPDATE licenses SET status = 'active' WHERE key = %s", (key,))
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info(f"Ключ {key} разблокирован")
        return jsonify({"success": True, "message": "Ключ разблокирован"}), 200
    except Exception as e:
        logger.error(f"Ошибка разблокировки: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/unbind', methods=['POST'])
@require_login
def api_unbind():
    """Отвязка устройства от ключа"""
    try:
        data = request.json
        key = data.get('key')
        
        if not key:
            return jsonify({"success": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        if USE_SQLITE:
            execute_query(cur, "UPDATE licenses SET device_id = NULL, device_info = NULL, activated_at = NULL WHERE key = ?", (key,))
        else:
            execute_query(cur, "UPDATE licenses SET device_id = NULL, device_info = NULL, activated_at = NULL WHERE key = %s", (key,))
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info(f"Устройство отвязано от ключа {key}")
        return jsonify({"success": True, "message": "Устройство отвязано"}), 200
    except Exception as e:
        logger.error(f"Ошибка отвязки: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/delete', methods=['POST'])
@require_login
def api_delete():
    """Удаление ключа"""
    try:
        data = request.json
        key = data.get('key')
        
        if not key:
            return jsonify({"success": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        if USE_SQLITE:
            execute_query(cur, "DELETE FROM licenses WHERE key = ?", (key,))
        else:
            execute_query(cur, "DELETE FROM licenses WHERE key = %s", (key,))
        conn.commit()
        deleted = cur.rowcount
        cur.close()
        conn.close()
        
        if deleted > 0:
            return jsonify({"success": True, "message": "Ключ удален"}), 200
        else:
            return jsonify({"success": False, "message": "Ключ не найден"}), 404
    except Exception as e:
        logger.error(f"Ошибка удаления ключа: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

# API endpoints для клиента (БЕЗ проверки IP whitelist - доступны всем)
@app.route('/api/v1/license/check', methods=['POST'])
def check_license():
    """Проверка лицензии (для клиента)"""
    try:
        data = request.json
        if not data:
            return jsonify({"valid": False, "message": "Пустой запрос"}), 400
        
        signature = data.pop('signature', '')
        timestamp = data.get('timestamp', 0)
        
        if not check_timestamp(timestamp):
            return jsonify({"valid": False, "message": "Устаревший запрос"}), 403
        
        if not verify_signature(data, signature):
            return jsonify({"valid": False, "message": "Неверная подпись"}), 403
        
        key = data.get('key')
        device_id = data.get('device_id')
        
        if not key:
            return jsonify({"valid": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"valid": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        execute_query(cur, "SELECT * FROM licenses WHERE key = %s", (key,))
        row = cur.fetchone()
        
        if not row:
            cur.close()
            conn.close()
            return jsonify({"valid": False, "message": "Ключ не найден"}), 200
        
        license_info = dict(row) if USE_SQLITE else row
        
        if license_info['status'] == 'blocked':
            cur.close()
            conn.close()
            return jsonify({"valid": False, "message": "Ключ заблокирован"}), 200
        
        if license_info['expires_at']:
            expires = datetime.fromisoformat(license_info['expires_at']) if isinstance(license_info['expires_at'], str) else license_info['expires_at']
            if datetime.now() > expires:
                # Блокируем истекший ключ автоматически
                execute_query(cur, "UPDATE licenses SET status = 'blocked' WHERE key = %s", (key,))
                conn.commit()
                cur.close()
                conn.close()
                return jsonify({"valid": False, "message": "Лицензия истекла и заблокирована"}), 200
        
        if license_info['device_id'] and license_info['device_id'] != device_id:
            cur.close()
            conn.close()
            return jsonify({"valid": False, "message": "Ключ привязан к другому устройству"}), 200
        
        if USE_SQLITE:
            execute_query(cur, "UPDATE licenses SET last_check = datetime('now') WHERE key = ?", (key,))
        else:
            execute_query(cur, "UPDATE licenses SET last_check = CURRENT_TIMESTAMP WHERE key = %s", (key,))
        conn.commit()
        
        cur.close()
        conn.close()
        
        # Форматируем дату истечения
        expires_str = None
        if license_info['expires_at']:
            try:
                if isinstance(license_info['expires_at'], str):
                    expires_str = license_info['expires_at']
                else:
                    expires_str = license_info['expires_at'].isoformat()
            except:
                expires_str = str(license_info['expires_at'])
        
        return jsonify({
            "valid": True,
            "message": "Лицензия активна",
            "expires": expires_str
        }), 200
        
    except Exception as e:
        logger.error(f"Ошибка проверки: {e}")
        return jsonify({"valid": False, "message": f"Ошибка сервера: {str(e)}"}), 500

@app.route('/api/v1/license/activate', methods=['POST'])
def activate_license():
    """Активация лицензии (для клиента)"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "message": "Пустой запрос"}), 400
        
        signature = data.pop('signature', '')
        timestamp = data.get('timestamp', 0)
        
        if not check_timestamp(timestamp):
            return jsonify({"success": False, "message": "Устаревший запрос"}), 403
        
        if not verify_signature(data, signature):
            return jsonify({"success": False, "message": "Неверная подпись"}), 403
        
        key = data.get('key')
        device_id = data.get('device_id')
        device_info = data.get('device_info')
        
        if not key:
            return jsonify({"success": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        execute_query(cur, "SELECT * FROM licenses WHERE key = %s", (key,))
        row = cur.fetchone()
        
        if not row:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "Ключ не найден"}), 200
        
        license_info = dict(row) if USE_SQLITE else row
        
        if license_info['status'] == 'blocked':
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "Ключ заблокирован"}), 200
        
        if license_info['expires_at']:
            expires = datetime.fromisoformat(license_info['expires_at']) if isinstance(license_info['expires_at'], str) else license_info['expires_at']
            if datetime.now() > expires:
                execute_query(cur, "UPDATE licenses SET status = 'expired' WHERE key = %s", (key,))
                conn.commit()
                cur.close()
                conn.close()
                return jsonify({"success": False, "message": "Лицензия истекла"}), 200
        
        if license_info['device_id'] and license_info['device_id'] != device_id:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "Ключ уже привязан к другому устройству"}), 200
        
        if USE_SQLITE:
            execute_query(cur, """
                UPDATE licenses 
                SET device_id = ?, device_info = ?, activated_at = datetime('now'), status = 'active'
                WHERE key = ?
            """, (device_id, json.dumps(device_info), key))
        else:
            execute_query(cur, """
                UPDATE licenses 
                SET device_id = %s, device_info = %s, activated_at = CURRENT_TIMESTAMP, status = 'active'
                WHERE key = %s
            """, (device_id, json.dumps(device_info), key))
        conn.commit()
        
        cur.close()
        conn.close()
        
        return jsonify({"success": True, "message": "Ключ активирован"}), 200
        
    except Exception as e:
        logger.error(f"Ошибка активации: {e}")
        return jsonify({"success": False, "message": f"Ошибка сервера: {str(e)}"}), 500

@app.route('/api/v1/license/deactivate', methods=['POST'])
def deactivate_license():
    """Деактивация (блокировка) лицензии"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "message": "Пустой запрос"}), 400
        
        signature = data.pop('signature', '')
        timestamp = data.get('timestamp', 0)
        
        if not check_timestamp(timestamp):
            return jsonify({"success": False, "message": "Устаревший запрос"}), 403
        
        if not verify_signature(data, signature):
            return jsonify({"success": False, "message": "Неверная подпись"}), 403
        
        key = data.get('key')
        device_id = data.get('device_id')
        
        if not key:
            return jsonify({"success": False, "message": "Ключ не указан"}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = get_cursor(conn)
        execute_query(cur, "SELECT * FROM licenses WHERE key = %s", (key,))
        row = cur.fetchone()
        
        if not row:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "Ключ не найден"}), 200
        
        license_info = dict(row) if USE_SQLITE else row
        
        # Проверяем device_id
        if license_info['device_id'] and license_info['device_id'] != device_id:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "Ключ привязан к другому устройству"}), 200
        
        # Блокируем ключ
        execute_query(cur, "UPDATE licenses SET status = 'blocked' WHERE key = %s", (key,))
        conn.commit()
        
        cur.close()
        conn.close()
        
        logger.info(f"Ключ {key} заблокирован (деактивация)")
        return jsonify({"success": True, "message": "Ключ заблокирован"}), 200
        
    except Exception as e:
        logger.error(f"Ошибка деактивации: {e}")
        return jsonify({"success": False, "message": f"Ошибка сервера: {str(e)}"}), 500

@app.route('/api/v1/license/heartbeat', methods=['POST'])
def heartbeat():
    """Heartbeat (для клиента)"""
    try:
        data = request.json
        signature = data.pop('signature', '')
        timestamp = data.get('timestamp', 0)
        
        if not check_timestamp(timestamp):
            return jsonify({"success": False, "message": "Устаревший запрос"}), 403
        
        if not verify_signature(data, signature):
            return jsonify({"success": False, "message": "Неверная подпись"}), 403
        
        key = data.get('key')
        device_id = data.get('device_id')
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = conn.cursor()
        if USE_SQLITE:
            execute_query(cur, """
                UPDATE licenses 
                SET heartbeat_last = datetime('now') 
                WHERE key = ? AND device_id = ?
            """, (key, device_id))
        else:
            execute_query(cur, """
                UPDATE licenses 
                SET heartbeat_last = CURRENT_TIMESTAMP 
                WHERE key = %s AND device_id = %s
            """, (key, device_id))
        conn.commit()
        
        cur.close()
        conn.close()
        
        return jsonify({"success": True}), 200
        
    except Exception as e:
        logger.error(f"Ошибка heartbeat: {e}")
        return jsonify({"success": False, "message": f"Ошибка: {str(e)}"}), 500

@app.route('/health', methods=['GET'])
def health():
    """Проверка работоспособности"""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()}), 200

if __name__ == '__main__':
    print("=" * 60)
    print("🌐 LICENSE WEB ADMIN + API SERVER")
    print("=" * 60)
    
    if init_database():
        print("✅ База данных инициализирована")
    else:
        print("❌ Ошибка инициализации БД")
    
    print(f"\n📝 Админ-панель: http://localhost:5000")
    print(f"🔑 Пароль по умолчанию: {ADMIN_PASSWORD}")
    print(f"🌐 Whitelist IP: {', '.join(ADMIN_WHITELIST) if ADMIN_WHITELIST else 'Все IP разрешены'}")
    print("\n⚠️  ИЗМЕНИТЕ ПАРОЛЬ в переменной окружения ADMIN_PASSWORD!")
    print("⚠️  Настройте ADMIN_WHITELIST для ограничения доступа!")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, debug=False)
