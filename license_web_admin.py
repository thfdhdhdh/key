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
SECRET_KEY = os.getenv("LICENSE_SECRET_KEY", "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION")
ADMIN_KEY = os.getenv("ADMIN_KEY", "CHANGE_THIS_ADMIN_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")  # Измените!

# Whitelist IP для доступа к админ-панели
ADMIN_WHITELIST = os.getenv("ADMIN_WHITELIST", "").split(",") if os.getenv("ADMIN_WHITELIST") else []
# Если whitelist пуст, разрешаем доступ с localhost
if not ADMIN_WHITELIST:
    ADMIN_WHITELIST = ["127.0.0.1", "::1", "localhost"]

# Настройки БД
# По умолчанию используем SQLite (не требует установки PostgreSQL)
USE_SQLITE = os.getenv('USE_SQLITE', 'true').lower() == 'true'
# На Vercel используем /tmp (единственное место где можно писать)
DB_FILE = os.getenv('DB_FILE', '/tmp/licenses.db' if os.getenv('VERCEL') else 'licenses.db')

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', '5432'),
    'database': os.getenv('DB_NAME', 'license_db'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', 'password')
}

def get_db_connection():
    """Получение подключения к БД"""
    if USE_SQLITE:
        # Используем SQLite
        import sqlite3
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn
    else:
        # Используем PostgreSQL
        if not PSYCOPG2_AVAILABLE:
            logger.error("psycopg2 не установлен. Используйте: pip install psycopg2-binary")
            return None
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            return conn
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}")
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
    conn = get_db_connection()
    if not conn:
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
        return True
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
    if not ADMIN_WHITELIST:
        return True  # Если whitelist пуст, разрешаем всем
    
    client_ip = request.remote_addr
    # Проверяем также через заголовки прокси
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        client_ip = forwarded_for.split(',')[0].strip()
    
    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        client_ip = real_ip
    
    return client_ip in ADMIN_WHITELIST or any(ip.strip() in ADMIN_WHITELIST for ip in [client_ip])

def require_login(f):
    """Декоратор для проверки авторизации и IP whitelist"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Проверка IP whitelist
        if not check_ip_whitelist():
            return jsonify({"error": "Доступ запрещен"}), 403
        
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
    <title>Лицензии</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #ffffff;
            padding: 40px 20px;
            color: #000;
        }
        .header {
            background: #ffffff;
            padding: 30px 0;
            margin-bottom: 40px;
            border-bottom: 1px solid #e0e0e0;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .card {
            background: #ffffff;
            padding: 30px;
            margin-bottom: 30px;
            border: 1px solid #e0e0e0;
        }
        h1 {
            color: #000;
            margin-bottom: 10px;
            font-size: 28px;
            font-weight: 300;
            letter-spacing: -0.5px;
        }
        h2 {
            color: #000;
            margin-bottom: 20px;
            font-size: 18px;
            font-weight: 400;
            letter-spacing: -0.3px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #000;
            font-weight: 400;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        input, select {
            width: 100%;
            padding: 12px;
            border: 1px solid #d0d0d0;
            border-radius: 0;
            font-size: 14px;
            background: #fff;
            color: #000;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #000;
        }
        button {
            padding: 12px 24px;
            background: #000;
            color: white;
            border: none;
            border-radius: 0;
            cursor: pointer;
            font-size: 13px;
            font-weight: 400;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }
        button:hover { background: #333; }
        .btn-danger {
            background: #000;
            border: 1px solid #000;
        }
        .btn-danger:hover {
            background: #fff;
            color: #000;
        }
        .btn-success {
            background: #000;
        }
        .btn-success:hover { background: #333; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 15px;
            text-align: left;
            border-bottom: 1px solid #e0e0e0;
            font-size: 13px;
        }
        th {
            font-weight: 500;
            color: #000;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-size: 11px;
        }
        tr:hover { background: #fafafa; }
        .status-active { color: #000; font-weight: 400; }
        .status-blocked { color: #999; font-weight: 400; }
        .status-expired { color: #999; font-weight: 400; }
        .key-code {
            font-family: 'Courier New', monospace;
            background: #fafafa;
            padding: 6px 10px;
            border: 1px solid #e0e0e0;
            font-weight: 400;
            font-size: 12px;
        }
        .device-info {
            font-size: 11px;
            color: #999;
            margin-top: 4px;
        }
        .logout {
            float: right;
            background: transparent;
            color: #000;
            border: 1px solid #000;
            text-decoration: none;
            padding: 10px 20px;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .logout:hover {
            background: #000;
            color: #fff;
        }
        .result-box {
            margin-top: 20px;
            padding: 15px;
            border: 1px solid #e0e0e0;
            background: #fafafa;
            font-size: 13px;
        }
        .result-success {
            border-color: #000;
            background: #000;
            color: #fff;
        }
        .result-error {
            border-color: #d32f2f;
            background: #fff;
            color: #d32f2f;
        }
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            overflow: auto;
        }
        .modal-content {
            background: #fff;
            margin: 50px auto;
            padding: 30px;
            border: 1px solid #e0e0e0;
            width: 90%;
            max-width: 600px;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid #e0e0e0;
        }
        .close {
            color: #000;
            font-size: 28px;
            font-weight: 300;
            cursor: pointer;
            background: none;
            border: none;
            padding: 0;
            width: 30px;
            height: 30px;
            line-height: 30px;
        }
        .close:hover {
            background: #f0f0f0;
        }
        .info-row {
            display: flex;
            padding: 12px 0;
            border-bottom: 1px solid #f0f0f0;
        }
        .info-label {
            font-weight: 500;
            width: 150px;
            color: #666;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .info-value {
            flex: 1;
            color: #000;
            font-size: 14px;
        }
        .key-clickable {
            cursor: pointer;
            text-decoration: underline;
            color: #000;
        }
        .key-clickable:hover {
            color: #666;
        }
        .btn-small {
            padding: 6px 12px;
            font-size: 11px;
            margin: 2px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Лицензии</h1>
            <a href="/logout" class="logout">Выйти</a>
            <div style="clear: both;"></div>
        </div>

        <div class="card">
            <h2>Генерация ключа</h2>
            <form id="generateForm">
                <div class="form-group">
                    <label>Срок действия (дней)</label>
                    <input type="number" name="days" placeholder="Оставьте пустым для бессрочной" min="1">
                </div>
                <button type="submit">Сгенерировать</button>
            </form>
            <div id="generateResult"></div>
        </div>

        <div class="card">
            <h2>Список лицензий</h2>
            <div class="form-group" style="display: flex; gap: 10px;">
                <input type="text" id="searchKey" placeholder="Поиск по ключу..." style="flex: 1;">
                <button onclick="loadLicenses()">Обновить</button>
            </div>
            <div id="licensesTable"></div>
        </div>
    </div>

    <!-- Модальное окно с информацией о ключе -->
    <div id="keyModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Информация о ключе</h2>
                <button class="close" onclick="closeModal()">&times;</button>
            </div>
            <div id="keyInfo"></div>
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
                    const keyText = data.key;
                    // Экранируем кавычки для JavaScript
                    const keyEscaped = keyText.replace(/'/g, "\\'").replace(/"/g, '\\"');
                    document.getElementById('generateResult').innerHTML = 
                        '<div class="result-box result-success">' +
                        '<strong>Ключ сгенерирован:</strong><br>' +
                        '<div style="display: flex; align-items: center; gap: 10px; margin-top: 10px;">' +
                        '<span class="key-code" id="generatedKey" style="display: inline-block; background: rgba(255,255,255,0.2); border-color: rgba(255,255,255,0.3); color: #fff; flex: 1; word-break: break-all;">' + keyText + '</span>' +
                        '<button onclick="copyKey(' + JSON.stringify(keyText) + ')" style="padding: 8px 16px; background: rgba(255,255,255,0.3); border: 1px solid rgba(255,255,255,0.5); color: #fff; cursor: pointer; text-transform: uppercase; font-size: 11px;">Копировать</button>' +
                        '</div>' +
                        '</div>';
                    form.reset();
                    loadLicenses();
                } else {
                    document.getElementById('generateResult').innerHTML = 
                        '<div class="result-box result-error">Ошибка: ' + data.message + '</div>';
                }
            });
        }

        function loadLicenses() {
            const search = document.getElementById('searchKey').value;
            fetch('/api/licenses' + (search ? '?search=' + search : ''))
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    if (data.licenses.length === 0) {
                        document.getElementById('licensesTable').innerHTML = '<p style="padding: 20px; color: #999; text-align: center;">Нет лицензий</p>';
                        return;
                    }
                    
                    let html = '<table><tr><th>Ключ</th><th>Статус</th><th>Создан</th><th>Истекает</th><th>Устройство</th><th>Действия</th></tr>';
                    data.licenses.forEach(lic => {
                        const statusClass = 'status-' + lic.status;
                        const expires = lic.expires_at ? new Date(lic.expires_at).toLocaleDateString('ru-RU') : 'Бессрочно';
                        let device = 'Не активирован';
                        if (lic.device_id) {
                            device = '<div class="device-info">ID: ' + lic.device_id.substring(0, 16) + '...</div>';
                            if (lic.device_info) {
                                try {
                                    const devInfo = JSON.parse(lic.device_info);
                                    device += '<div class="device-info">' + (devInfo.hostname || '') + '</div>';
                                } catch(e) {}
                            }
                        }
                        
                        // Экранируем ключ для JavaScript
                        const keyEscaped = JSON.stringify(lic.key);
                        
                        html += '<tr>' +
                            '<td><div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">' +
                            '<span class="key-code key-clickable" onclick="showKeyInfo(' + JSON.stringify(lic) + ')" style="cursor: pointer; flex: 1; min-width: 200px; word-break: break-all;">' + lic.key + '</span>' +
                            '<button onclick="copyKey(' + keyEscaped + ')" class="btn-small" style="padding: 4px 8px; font-size: 10px; background: #000; color: #fff; border: none; cursor: pointer; white-space: nowrap;">Копировать</button>' +
                            '</div></td>' +
                            '<td><span class="' + statusClass + '">' + lic.status + '</span></td>' +
                            '<td>' + new Date(lic.created_at).toLocaleDateString('ru-RU') + '</td>' +
                            '<td>' + expires + '</td>' +
                            '<td>' + device + '</td>' +
                            '<td>' +
                            (lic.status === 'active' ? 
                                '<button class="btn-danger btn-small" onclick="blockKey(' + keyEscaped + ')">Заблокировать</button>' :
                                '<button class="btn-success btn-small" onclick="unblockKey(' + keyEscaped + ')">Разблокировать</button>') +
                            '</td>' +
                            '</tr>';
                    });
                    html += '</table>';
                    document.getElementById('licensesTable').innerHTML = html;
                }
            });
        }

        function showKeyInfo(license) {
            const modal = document.getElementById('keyModal');
            const infoDiv = document.getElementById('keyInfo');
            
            let deviceInfo = 'Не активирован';
            if (license.device_id) {
                deviceInfo = 'ID: ' + license.device_id;
                if (license.device_info) {
                    try {
                        const devInfo = JSON.parse(license.device_info);
                        deviceInfo += '<br>Хост: ' + (devInfo.hostname || 'N/A');
                        deviceInfo += '<br>Платформа: ' + (devInfo.platform || 'N/A');
                        deviceInfo += '<br>Архитектура: ' + (devInfo.architecture || 'N/A');
                    } catch(e) {
                        deviceInfo += '<br>Инфо: ' + license.device_info;
                    }
                }
            }
            
            const created = license.created_at ? new Date(license.created_at).toLocaleString('ru-RU') : 'N/A';
            const expires = license.expires_at ? new Date(license.expires_at).toLocaleString('ru-RU') : 'Бессрочно';
            const activated = license.activated_at ? new Date(license.activated_at).toLocaleString('ru-RU') : 'Не активирован';
            const lastCheck = license.last_check ? new Date(license.last_check).toLocaleString('ru-RU') : 'Никогда';
            
            infoDiv.innerHTML = 
                '<div class="info-row">' +
                '<div class="info-label">Ключ:</div>' +
                '<div class="info-value"><span class="key-code">' + license.key + '</span> <button onclick="copyKey(' + JSON.stringify(license.key) + ')" class="btn-small">Копировать</button></div>' +
                '</div>' +
                '<div class="info-row">' +
                '<div class="info-label">Статус:</div>' +
                '<div class="info-value"><span class="status-' + license.status + '">' + license.status + '</span></div>' +
                '</div>' +
                '<div class="info-row">' +
                '<div class="info-label">Создан:</div>' +
                '<div class="info-value">' + created + '</div>' +
                '</div>' +
                '<div class="info-row">' +
                '<div class="info-label">Истекает:</div>' +
                '<div class="info-value">' + expires + '</div>' +
                '</div>' +
                '<div class="info-row">' +
                '<div class="info-label">Активирован:</div>' +
                '<div class="info-value">' + activated + '</div>' +
                '</div>' +
                '<div class="info-row">' +
                '<div class="info-label">Последняя проверка:</div>' +
                '<div class="info-value">' + lastCheck + '</div>' +
                '</div>' +
                '<div class="info-row">' +
                '<div class="info-label">Устройство:</div>' +
                '<div class="info-value">' + deviceInfo + '</div>' +
                '</div>' +
                '<div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid #e0e0e0;">' +
                (license.status === 'active' ? 
                    '<button class="btn-danger" onclick="blockKey(' + JSON.stringify(license.key) + '); closeModal();">Заблокировать</button>' :
                    '<button class="btn-success" onclick="unblockKey(' + JSON.stringify(license.key) + '); closeModal();">Разблокировать</button>') +
                ' <button onclick="closeModal()" style="background: #999; margin-left: 10px;">Закрыть</button>' +
                '</div>';
            
            modal.style.display = 'block';
        }

        function closeModal() {
            document.getElementById('keyModal').style.display = 'none';
        }

        window.onclick = function(event) {
            const modal = document.getElementById('keyModal');
            if (event.target == modal) {
                closeModal();
            }
        }

        function blockKey(key) {
            if (confirm('Заблокировать ключ ' + key + '?')) {
                fetch('/api/block', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: key})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        loadLicenses();
                    } else {
                        alert('Ошибка: ' + data.message);
                    }
                });
            }
        }

        function unblockKey(key) {
            fetch('/api/unblock', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({key: key})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    loadLicenses();
                } else {
                    alert('Ошибка: ' + data.message);
                }
            });
        }

        function copyKey(key) {
            navigator.clipboard.writeText(key).then(function() {
                // Показываем уведомление вместо alert
                const notification = document.createElement('div');
                notification.textContent = 'Ключ скопирован!';
                notification.style.cssText = 'position: fixed; top: 20px; right: 20px; background: #000; color: #fff; padding: 12px 24px; z-index: 10000; font-size: 13px;';
                document.body.appendChild(notification);
                setTimeout(() => notification.remove(), 2000);
            }, function(err) {
                // Fallback для старых браузеров
                const textArea = document.createElement('textarea');
                textArea.value = key;
                textArea.style.position = 'fixed';
                textArea.style.opacity = '0';
                document.body.appendChild(textArea);
                textArea.select();
                try {
                    document.execCommand('copy');
                    const notification = document.createElement('div');
                    notification.textContent = 'Ключ скопирован!';
                    notification.style.cssText = 'position: fixed; top: 20px; right: 20px; background: #000; color: #fff; padding: 12px 24px; z-index: 10000; font-size: 13px;';
                    document.body.appendChild(notification);
                    setTimeout(() => notification.remove(), 2000);
                } catch (err) {
                    alert('Ошибка копирования. Ключ: ' + key);
                }
                document.body.removeChild(textArea);
            });
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
        return jsonify({"error": "Доступ запрещен. Ваш IP не в whitelist"}), 403
    
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
            execute_query(cur, "SELECT * FROM licenses WHERE key LIKE %s ORDER BY created_at DESC", (f'%{search}%',))
        else:
            execute_query(cur, "SELECT * FROM licenses ORDER BY created_at DESC")
        
        licenses = cur.fetchall()
        # Конвертируем datetime в строки
        for lic in licenses:
            if lic['created_at']:
                lic['created_at'] = lic['created_at'].isoformat()
            if lic['expires_at']:
                lic['expires_at'] = lic['expires_at'].isoformat()
            if lic['activated_at']:
                lic['activated_at'] = lic['activated_at'].isoformat()
        
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
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = conn.cursor()
        execute_query(cur, "UPDATE licenses SET status = 'blocked' WHERE key = %s", (key,))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/unblock', methods=['POST'])
@require_login
def api_unblock():
    """Разблокировка ключа"""
    try:
        data = request.json
        key = data.get('key')
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"success": False, "message": "Ошибка сервера"}), 500
        
        cur = conn.cursor()
        execute_query(cur, "UPDATE licenses SET status = 'active' WHERE key = %s", (key,))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"success": True}), 200
    except Exception as e:
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
                execute_query(cur, "UPDATE licenses SET status = 'expired' WHERE key = %s", (key,))
                conn.commit()
                cur.close()
                conn.close()
                return jsonify({"valid": False, "message": "Лицензия истекла"}), 200
        
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
        
        return jsonify({
            "valid": True,
            "message": "Лицензия активна",
            "expires": license_info['expires_at'].isoformat() if license_info['expires_at'] else None
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
