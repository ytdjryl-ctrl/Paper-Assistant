# Copyright (c) 2026 South China Sea Institute of Oceanology, Chinese Academy of Sciences (SCSIO, CAS). All rights reserved.
import pathlib
import pymysql
import hashlib
import re
import os
import jwt
import datetime
from flask import Flask, request, jsonify, app, abort, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from functools import wraps
import uuid
from flask import send_file, jsonify
from pathlib import Path
from typing import Optional
import shutil
import sys

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

# 导入日志配置
from deepdiver_v2.config.logging_config import quick_setup, get_logger

# Load the same environment file used by Pipeline V2.
load_dotenv(Path(__file__).parent / 'deepdiver_v2' / 'config' / '.env')

# Database configuration
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "sciassistant")
# 初始化日志系统
quick_setup(
    environment=os.getenv('APP_ENV', 'production'),
    log_dir='logs',
    enable_file_logging=True
)

logger = get_logger(__name__)

def safe_filename_unicode(filename: str) -> str:
    """
    安全的文件名处理函数，支持中文和特殊字符
    只移除Windows系统不允许的字符和路径分隔符
    
    Args:
        filename: 原始文件名
        
    Returns:
        处理后的安全文件名
    """
    if not filename:
        return 'untitled'
    
    # Windows保留字符和路径分隔符
    forbidden_chars = r'<>:"/\|?*'
    # 控制字符（0x00-0x1F）
    forbidden_chars += ''.join(chr(i) for i in range(32))
    
    # 移除禁止字符
    safe_name = ''.join(c for c in filename if c not in forbidden_chars)
    
    # 移除首尾空格和点号
    safe_name = safe_name.strip(' .')
    
    # 如果处理后的文件名为空或只包含点号，使用默认名称
    if not safe_name or safe_name == '.':
        safe_name = 'untitled'
    
    # Windows文件名长度限制（通常255字符，但为了安全限制为200）
    if len(safe_name) > 200:
        # 保留扩展名
        if '.' in safe_name:
            name_part, ext_part = safe_name.rsplit('.', 1)
            safe_name = name_part[:200-len(ext_part)-1] + '.' + ext_part
        else:
            safe_name = safe_name[:200]
    
    # Windows保留名称检查（虽然一般不会遇到，但为了安全）
    reserved_names = {'CON', 'PRN', 'AUX', 'NUL', 
                      'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
                      'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
    if safe_name.upper().split('.')[0] in reserved_names:
        safe_name = 'file_' + safe_name
    
    return safe_name

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024
CORS(app)  # 解决跨域问题


@app.route('/research')
@app.route('/research.html')
def research_page():
    """Serve the research workflow UI without modifying the legacy ai_chat page."""
    return send_from_directory(str(Path(__file__).parent / 'chatAi'), 'research.html')

# 配置JWT密钥，实际生产环境中应使用更安全的方式存储
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key')  # 建议在.env文件中设置

# 数据库配置
DB_CONFIG = {
    'host': MYSQL_HOST,
    'port': MYSQL_PORT,
    'user': MYSQL_USER,
    'password': MYSQL_PASSWORD,
    'db': MYSQL_DATABASE,
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

#数据库连接
def get_db_connection():
    """创建数据库连接"""
    connection = pymysql.connect(**DB_CONFIG)
    return connection

#数据库处理连接和异常
def db_operation(func):
    """数据库操作装饰器，用于处理连接和异常"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        connection = get_db_connection()
        try:
            result = func(connection, *args, **kwargs)
            return result
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            connection.close()

    return wrapper

#密码加密
def hash_password(password):
    """密码加密处理"""
    sha256 = hashlib.sha256()
    sha256.update(password.encode('utf-8'))
    return sha256.hexdigest()

#验证邮箱格式
def is_valid_email(email):
    """验证邮箱格式"""
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

#生成JWT令牌
def generate_token(user_id, username, expire_hours=24):
    """生成JWT令牌（支持动态设置过期时间）
    Args:
        expire_hours: 令牌有效期（小时），默认24小时，记住我时设为168小时（7天）
    """
    # 根据传入的小时数计算过期时间
    expiration = datetime.datetime.utcnow() + datetime.timedelta(hours=expire_hours)

    # 创建令牌
    token = jwt.encode({
        'user_id': user_id,
        'username': username,
        'exp': expiration
    }, app.config['SECRET_KEY'], algorithm='HS256')

    return token


# 转换datetime对象为字符串的函数
def convert_datetime_to_string(value):
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return value


# 检查用户是否有权访问会话的函数
def has_access_to_session(user_id, session_id):

    # 从数据库查询该会话是否属于该用户
    # 或用户是否有访问权限
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            # 假设会话信息存储在conversation表中，有user_id字段
            sql = "SELECT id FROM conversation WHERE id = %s AND user_id = %s"
            cursor.execute(sql, (session_id, user_id))
            result = cursor.fetchone()
            return result is not None
    except Exception as e:
        logger.error(f"检查会话权限失败: {e}")
        return False
    finally:
        if connection:
            connection.close()


# ------------------- 密码重置相关接口 -------------------
@app.route('/api/verify-credentials', methods=['POST'])
def verify_credentials():
    """
    验证用户名和邮箱接口
    请求体：{ "username": "用户名", "email": "邮箱地址" }
    """
    data = request.get_json()

    # 1. 验证请求参数
    required_fields = ['username', 'email']
    for field in required_fields:
        if field not in data:
            return jsonify({'success': False, 'message': f'缺少必要字段: {field}'}), 400

    username = data['username'].strip()
    email = data['email'].strip()

    # 2. 验证邮箱格式
    if not is_valid_email(email):
        return jsonify({'success': False, 'message': '邮箱格式不正确'}), 400

    # 3. 验证用户名和邮箱是否匹配
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM users WHERE username = %s AND email = %s",
                (username, email)
            )
            user = cursor.fetchone()

            if not user:
                return jsonify({'success': False, 'message': '用户名或邮箱不正确'}), 400

            # 返回成功，允许进行密码重置
            return jsonify({
                'success': True,
                'message': '验证通过，请设置新密码',
                'data': {'userId': user['id']}  # 返回用户ID用于后续密码重置
            })

    except pymysql.MySQLError as e:
        logger.error(f"验证接口数据库错误: {str(e)}")
        return jsonify({'success': False, 'message': '服务器数据库错误'}), 500
    finally:
        if connection:
            connection.close()


@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    """
    重置密码接口：验证用户ID，更新密码
    请求体：{ "userId": "用户ID", "newPassword": "新密码", "confirmPassword": "确认新密码" }
    """
    data = request.get_json()

    # 1. 验证请求参数
    required_fields = ['userId', 'newPassword', 'confirmPassword']
    for field in required_fields:
        if field not in data:
            return jsonify({'success': False, 'message': f'缺少必要字段: {field}'}), 400

    user_id = data['userId']
    new_password = data['newPassword']
    confirm_password = data['confirmPassword']

    # 2. 验证密码格式
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': '新密码长度不能少于6位'}), 400

    if new_password != confirm_password:
        return jsonify({'success': False, 'message': '新密码和确认密码不匹配'}), 400

    # 3. 数据库操作：更新用户密码
    hashed_new_pwd = hash_password(new_password)
    connection = None

    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            # 更新用户密码
            cursor.execute(
                "UPDATE users SET password = %s, updatetime = NOW() WHERE id = %s",
                (hashed_new_pwd, user_id)
            )

            if cursor.rowcount == 0:
                return jsonify({'success': False, 'message': '密码更新失败，用户不存在'}), 400

        connection.commit()
        return jsonify({
            'success': True,
            'message': '密码重置成功，请返回登录页面登录'
        })

    except pymysql.MySQLError as e:
        if connection:
            connection.rollback()
        logger.error(f"重置密码接口数据库错误: {str(e)}")
        return jsonify({'success': False, 'message': '服务器数据库错误'}), 500
    finally:
        if connection:
            connection.close()


# ------------------- 用户注册与登录接口 -------------------
@app.route('/api/register', methods=['POST'])
def register():
    """用户注册接口（保持不变）"""
    data = request.get_json()

    # 验证请求数据
    required_fields = ['username', 'email', 'password', 'confirmPassword']
    for field in required_fields:
        if field not in data:
            return jsonify({'success': False, 'message': f'缺少必要字段: {field}'}), 400

    username = data['username'].strip()
    email = data['email'].strip()
    password = data['password']
    confirm_password = data['confirmPassword']

    # 验证数据格式
    if not username:
        return jsonify({'success': False, 'message': '用户名不能为空'}), 400

    if not is_valid_email(email):
        return jsonify({'success': False, 'message': '邮箱格式不正确'}), 400

    if len(password) < 6:
        return jsonify({'success': False, 'message': '密码长度不能少于6位'}), 400

    if password != confirm_password:
        return jsonify({'success': False, 'message': '密码和确认密码不匹配'}), 400

    # 数据库操作
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            # 检查用户名是否已存在
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cursor.fetchone():
                return jsonify({'success': False, 'message': '用户名已存在'}), 400

            # 检查邮箱是否已被注册
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                return jsonify({'success': False, 'message': '邮箱已被注册'}), 400

            # 插入新用户
            hashed_pwd = hash_password(password)
            cursor.execute(
                "INSERT INTO users (username, email, password, createtime, updatetime) VALUES (%s, %s, %s, NOW(), NOW())",
                (username, email, hashed_pwd)
            )
            connection.commit()

            return jsonify({'success': True, 'message': '注册成功'})

    except pymysql.MySQLError as e:
        if connection:
            connection.rollback()
        logger.error(f"数据库错误: {str(e)}")
        return jsonify({'success': False, 'message': '服务器数据库错误'}), 500

    finally:
        if connection:
            connection.close()


@app.route('/api/login', methods=['POST'])
def login():
    """用户登录接口（支持账号或邮箱登录）"""
    data = request.get_json()

    # 验证请求数据 - 修改：将username改为loginId，支持用户名或邮箱
    required_fields = ['loginId', 'password']
    for field in required_fields:
        if field not in data:
            return jsonify({'success': False, 'message': f'缺少必要字段: {field}'}), 400

    # 获取参数
    login_id = data['loginId'].strip()  # 改为loginId，可接收用户名或邮箱
    password = data['password']
    remember_me = data.get('remember_me', False)

    # 基本验证
    if not login_id or not password:
        return jsonify({'success': False, 'message': '账号/邮箱和密码不能为空'}), 400

    # 数据库操作
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            # 核心修改：查询条件支持用户名或邮箱匹配
            cursor.execute(
                "SELECT id, username, password FROM users WHERE username = %s OR email = %s",
                (login_id, login_id)  # 同时传入两个参数，匹配用户名或邮箱
            )
            user = cursor.fetchone()

            # 验证用户存在性和密码
            if not user:
                return jsonify({'success': False, 'message': '账号/邮箱或密码错误'}), 401

            # 验证密码
            hashed_pwd = hash_password(password)
            if user['password'] != hashed_pwd:
                return jsonify({'success': False, 'message': '账号/邮箱或密码错误'}), 401

            # 根据“记住我”状态设置令牌有效期
            if remember_me:
                token = generate_token(user['id'], user['username'], expire_hours=168)  # 7天
            else:
                token = generate_token(user['id'], user['username'])  # 默认24小时

            # 返回结果
            return jsonify({
                'success': True,
                'message': '登录成功',
                'data': {
                    'token': token,
                    'user_id': user['id'],
                    'username': user['username'],
                    'remember_me': remember_me
                }
            })

    except pymysql.MySQLError as e:
        logger.error(f"数据库错误: {str(e)}")
        return jsonify({'success': False, 'message': '服务器数据库错误'}), 500

    finally:
        if connection:
            connection.close()


# ------------------- 会话列表接口-------------------
@app.route('/api/chat/sessions', methods=['POST'])
@db_operation
def create_chat_session(connection):
    """创建新的聊天列表"""
    data = request.get_json()
    user_id = data.get('user_id')
    # session_id = data.get('session_id')
    session_id = str(uuid.uuid4())
    title = data.get('title', '新对话')

    if not user_id:
        return jsonify({'error': 'user_id are required'}), 400

    with connection.cursor() as cursor:
        sql = """
        INSERT INTO chat_list (user_id, session_id, title, create_time, update_time)
        VALUES (%s, %s, %s, NOW(), NOW())
        """
        cursor.execute(sql, (user_id, session_id, title))
        connection.commit()
        return jsonify({
            'id': cursor.lastrowid,
            'message': 'Chat session created successfully',
            'session_id': session_id
        }), 201


@app.route('/api/chat/sessions/<user_id>', methods=['GET'])
@db_operation
def get_chat_sessions_by_userid(connection, user_id):
    """根据用户ID获取聊天会话"""
    with connection.cursor() as cursor:
        sql = "SELECT * FROM chat_list WHERE user_id = %s order by update_time desc;"
        cursor.execute(sql, (user_id,))
        session = cursor.fetchall()
        if session:
            return jsonify(session)
        else:
            return jsonify({'error': 'User not found'}), 404


@app.route('/api/chat/sessions/<session_id>', methods=['PUT'])
@db_operation
def update_chat_session_title(connection, session_id):
    """更新聊天会话标题"""
    data = request.get_json()
    new_title = data.get('title')

    if not new_title:
        return jsonify({'error': 'title is required'}), 400

    with connection.cursor() as cursor:
        sql = "UPDATE chat_list SET title = %s, update_time = NOW() WHERE session_id = %s"
        cursor.execute(sql, (new_title, session_id))
        connection.commit()

        if cursor.rowcount == 0:
            return jsonify({'error': 'Session not found'}), 404

        return jsonify({'message': 'Title updated successfully'})


@app.route('/api/chat/sessions/<session_id>/touch', methods=['PUT'])
@db_operation
def update_chat_session_time(connection, session_id):
    """更新聊天会话的最后更新时间"""
    with connection.cursor() as cursor:
        sql = "UPDATE chat_list SET update_time = NOW() WHERE session_id = %s"
        cursor.execute(sql, (session_id,))
        connection.commit()

        if cursor.rowcount == 0:
            return jsonify({'error': 'Session not found'}), 404

        return jsonify({'message': 'Update time refreshed successfully'})


@app.route('/api/chat/sessions/<session_id>', methods=['DELETE'])
@db_operation
def delete_chat_session(connection, session_id):
    """删除 single chat session and its details"""
    with connection.cursor() as cursor:
        try:
            # First delete from conversation_detail
            sql_detail = "DELETE FROM conversation_detail WHERE session_id = %s"
            cursor.execute(sql_detail, (session_id,))

            # Then delete from chat_history
            sql_history = "DELETE FROM chat_list WHERE session_id = %s"
            cursor.execute(sql_history, (session_id,))

            # Check if any records were affected
            if cursor.rowcount == 0:
                connection.rollback()  # Rollback if no records were deleted
                return jsonify({'error': 'Session not found'}), 404

            connection.commit()  # Commit both deletions if successful
            return jsonify({'message': 'Session deleted successfully'})

        except Exception as e:
            connection.rollback()  # Rollback on any error
            return jsonify({'error': str(e)}), 500

#-----------------会话详情相关接口------------------
@app.route('/api/chat/messages', methods=['POST'])
def add_chat_message():
    """添加聊天消息"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': '没有提供JSON数据'}), 400

        # 验证必需字段
        required_fields = ['session_id', 'from_who', 'content']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'缺少必需字段: {field}'}), 400

        # 设置值
        session_id = data['session_id']
        from_who = data['from_who']
        content = data['content']
        round_num = data.get('round',1)
        think_msg = data.get('think_msg', '')
        timestamp = data.get('timestamp', datetime.datetime.now())
        message_uuid = data.get('uuid', data.get('backend_session_id'))
        has_report = data.get('has_report',0)
        report_title = data.get('report_title', '')  # 新增：获取报告标题


        # 验证from_who值
        if from_who not in ['user', 'ai']:
            return jsonify({'error': 'from_who必须是user或ai'}), 400

        connection = get_db_connection()
        if not connection:
            return jsonify({'error': '数据库连接失败'}), 500

        try:
            with connection.cursor() as cursor:
                sql = """
                    INSERT INTO conversation_detail 
                    (session_id, from_who, round, timestamp, uuid, content, think_msg, create_time, has_report, report_title)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s)
                """
                cursor.execute(sql, (
                    session_id, from_who, round_num, timestamp,
                    message_uuid, content, think_msg, has_report, report_title
                ))
            connection.commit()

            logger.info(f"成功插入消息: session_id={session_id}, from={from_who}, report_title={report_title}")
            return jsonify({
                'success': True,
                'message': '消息添加成功',
                'uuid': message_uuid
            }), 201

        except Exception as e:
            logger.error(f"插入消息失败: {e}")
            return jsonify({'error': f'插入消息失败: {str(e)}'}), 500
        finally:
            connection.close()

    except Exception as e:
        logger.info(f"处理请求时出错: {e}")
        return jsonify({'error': '服务器内部错误'}), 500

@app.route('/api/chat/messages/<session_id>', methods=['GET'])
def get_chat_messages_by_session_id(session_id):
    """根据session_id获取会话的所有消息"""
    try:
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': '数据库连接失败'}), 500

        try:
            with connection.cursor() as cursor:
                sql = """
                    SELECT id, session_id, from_who, round, timestamp, uuid,
                           content, think_msg, create_time, has_report, report_title
                    FROM conversation_detail
                    WHERE session_id = %s
                    ORDER BY round ASC, 
                     timestamp ASC, from_who DESC;
                """
                cursor.execute(sql, (session_id,))
                messages = cursor.fetchall()

                # 转换datetime对象为字符串
                converted_messages = []
                for message in messages:
                    converted_message = {}
                    for key, value in message.items():
                        converted_message[key] = convert_datetime_to_string(value)
                    converted_messages.append(converted_message)

                return jsonify({
                    'success': True,
                    'session_id': session_id,
                    'messages': converted_messages,
                    'count': len(converted_messages)
                }), 200

        except Exception as e:
            logger.error(f"查询消息失败: {e}")
            return jsonify({'error': f'查询消息失败: {str(e)}'}), 500
        finally:
            connection.close()

    except Exception as e:
        logger.info(f"处理请求时出错: {e}")
        return jsonify({'error': '服务器内部错误'}), 500


BASE_DIR = Path(__file__).parent.resolve()
# 统一使用项目根目录下的 workspaces
PDF_DIR = BASE_DIR / "workspaces"  # 统一路径

# 上传文件目录与配置
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {
    # 原有的文档和数据格式
    '.txt', '.md', '.csv', '.json', '.log', '.pdf', '.doc', '.docx',
    '.xls', '.xlsx', '.ppt', '.pptx', '.xml', '.html', '.htm',
    '.rtf', '.odt', '.epub', '.yaml', '.yml',
    # 新增：图片格式
    '.jpg', '.jpeg', '.png', '.bmp', '.webp',
    # 新增：压缩包格式
    '.zip', '.rar', '.tar', '.gz', '.7z'
}
MAX_UPLOAD_SIZE = 1024 * 1024 * 1024  # 30MB（单个文件限制，支持Chat模式；DeepDiver模式在前端限制为20MB）
MAX_TOTAL_UPLOAD_SIZE = 1024 * 1024 * 1024  # 60MB（总大小限制，前端控制）
UPLOAD_DIR.mkdir(exist_ok=True)


def _find_workspace_file(session_id: str, filename: str) -> Optional[Path]:
    # 🔧 优先在 report/ 子目录查找（PDF/MD 实际生成位置）
    report_path = PDF_DIR / session_id / "report" / filename
    if report_path.is_file():
        return report_path
    # 兼容旧路径：workspace 根目录
    root_path = PDF_DIR / session_id / filename
    if root_path.is_file():
        return root_path
    return None

@app.route('/api/context/upload', methods=['POST'])
def upload_context_file():
    """接收上文文件上传，保存到服务器并返回文件信息与下载地址
    
    支持两种模式：
    1. 临时上传：保存到uploads目录，用于当前会话
    2. 同步到文档库：如果提供user_id和save_to_library=true，同时保存到文档库
    """
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未找到文件字段: file'}), 400
    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({'success': False, 'message': '未选择文件'}), 400

    # 获取可选参数
    user_id = request.form.get('user_id')  # 用户ID（用于保存到文档库）
    save_to_library = request.form.get('save_to_library', 'false').lower() == 'true'  # 是否同步到文档库

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'success': False, 'message': f'不支持的文件类型: {ext}'}), 400

    # 计算大小（兼容不同环境的文件流）
    try:
        file.stream.seek(0, os.SEEK_END)
        size = file.stream.tell()
        file.stream.seek(0)
    except Exception:
        # 回退方案：保存后再获取大小
        size = None

    if size is not None and size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'message': f'文件大小超过{MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB限制'}), 400

    file_id = uuid.uuid4().hex
    # 处理文件名编码（浏览器可能发送URL编码的文件名）
    original_filename = file.filename
    # 尝试解码文件名（处理浏览器可能发送的URL编码）
    try:
        # Flask会自动处理URL编码的文件名，但为了安全，我们显式处理
        if isinstance(original_filename, bytes):
            original_filename = original_filename.decode('utf-8', errors='replace')
        elif '%' in original_filename:
            # 如果包含URL编码字符，尝试解码
            import urllib.parse
            original_filename = urllib.parse.unquote(original_filename, encoding='utf-8')
    except Exception:
        pass  # 如果解码失败，使用原始文件名
    
    # 使用支持中文的文件名处理函数
    safe_name = safe_filename_unicode(original_filename)
    save_name = f"{file_id}_{safe_name}"
    save_path = UPLOAD_DIR / save_name
    
    # 保存文件，处理可能的编码问题
    try:
        # 确保目录存在
        save_path.parent.mkdir(parents=True, exist_ok=True)
        # 保存文件（使用字符串路径以避免编码问题）
        file.seek(0)
        with open(save_path, "wb") as f_out:
            # 使用 shutil.copyfileobj，这比 file.save 内存占用更低，适合 1GB 文件
            import shutil
            shutil.copyfileobj(file.stream, f_out)
        # -------------------------
        logger.info(f"文件物理保存成功: {save_path}")
    except Exception as e:
        logger.error(f"文件保存失败: {e}, 文件名: {file.filename}, 保存路径: {save_path}")
        return jsonify({'success': False, 'message': f'文件保存失败: {str(e)}'}), 500

    # 若之前未能获取大小，保存后补充
    if size is None:
        size = save_path.stat().st_size
    if size > MAX_UPLOAD_SIZE:
        try:
            save_path.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({'success': False, 'message': f'文件大小超过{MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB限制'}), 400

    # 尝试读取文本内容（仅对文本类扩展名），用于前端直接合并
    text_content = ''
    TEXT_EXTENSIONS = {'.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml'}
    if ext in TEXT_EXTENSIONS:
        try:
            with open(save_path, 'r', encoding='utf-8', errors='ignore') as f:
                text_content = f.read()
        except Exception:
            text_content = ''

    # 如果需要同步到文档库
    library_file_id = None
    file_already_in_library = False
    if save_to_library and user_id:
        logger.info(f"开始同步文件到文档库: user_id={user_id}, filename={original_filename}, save_to_library={save_to_library}")
        try:
            connection = get_db_connection()
            logger.debug(f"数据库连接成功")
            try:
                with connection.cursor() as cursor:
                    # 首先检查文件名是否已存在于文档库中
                    check_sql = """
                        SELECT file_id, stored_filename, file_path 
                        FROM user_files 
                        WHERE user_id = %s AND original_filename = %s
                        LIMIT 1
                    """
                    cursor.execute(check_sql, (user_id, original_filename))
                    existing_file = cursor.fetchone()
                    
                    if existing_file:
                        # 文件已存在，使用已有的文件ID（DictCursor返回字典格式）
                        library_file_id = existing_file['file_id']
                        file_already_in_library = True
                        logger.info(f"✓ 文件已在文档库中: {original_filename}, library_file_id: {library_file_id}")
                    else:
                        # 文件不存在，新建记录
                        logger.debug(f"文件不在文档库中，开始新建记录")
                        
                        # 获取用户上传目录
                        user_dir = get_user_upload_dir(user_id)
                        logger.debug(f"用户目录: {user_dir}")
                        
                        # 生成文档库文件ID和存储文件名
                        library_file_id = str(uuid.uuid4())
                        file_extension = ext.lstrip('.')  # 移除点号
                        library_safe_filename = f"{library_file_id}.{file_extension}"
                        library_file_path = user_dir / library_safe_filename
                        logger.debug(f"目标路径: {library_file_path}")
                        
                        # 复制文件到文档库目录
                        logger.debug(f"开始复制文件: {save_path} -> {library_file_path}")
                        shutil.copy2(str(save_path), str(library_file_path))
                        logger.debug(f"✓ 文件复制成功")
                        
                        # 将文件信息存入数据库
                        sql = """
                            INSERT INTO user_files 
                            (file_id, user_id, original_filename, stored_filename, file_path, 
                             file_size, file_type, status, upload_time)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """
                        logger.debug(f"准备插入数据库: file_id={library_file_id}")
                        cursor.execute(sql, (
                            library_file_id,
                            user_id,
                            original_filename,
                            library_safe_filename,
                            str(library_file_path),
                            size,
                            file_extension,
                            'processed'  # PDF文件直接标记为已处理
                        ))
                        connection.commit()
                        logger.info(f"✓ 文件已同步到文档库: {original_filename}, library_file_id: {library_file_id}, size: {size} bytes")
            finally:
                connection.close()
        except Exception as e:
            logger.error(f"同步文件到文档库失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # 不影响主流程，继续返回临时文件信息，但标记文档库保存失败

    download_url = f"/api/context/files/{file_id}"
    response_data = {
        'success': True,
        'file_id': file_id,
        'filename': original_filename,  # 返回原始文件名（包含中文），前端显示使用
        'size': size,
        'download_url': download_url,
        'text_content': text_content,
        'file_type': ext  # 返回文件扩展名（包含点号，如 .pdf）
    }
    
    # 如果同步到了文档库，返回文档库文件ID
    if library_file_id:
        response_data['library_file_id'] = library_file_id
        response_data['saved_to_library'] = True
        response_data['already_in_library'] = file_already_in_library  # 标识文件是否已在文档库中
    elif save_to_library and user_id:
        # 用户想保存到文档库，但保存失败了
        response_data['saved_to_library'] = False
        response_data['library_save_failed'] = True
        response_data['library_error'] = '文档库保存失败，文件已保存到临时目录'
        logger.warning(f"文档库保存失败，但临时文件上传成功: {original_filename}")
    
    return jsonify(response_data), 200

@app.route('/api/context/files/<file_id>', methods=['GET'])
def download_context_file(file_id: str):
    """根据file_id下载原始文件"""
    candidates = list(UPLOAD_DIR.glob(f"{file_id}_*"))
    if not candidates:
        abort(404)
    p = candidates[0]
    # 从保存的文件名中提取原始文件名（移除file_id前缀）
    saved_name = p.name
    if '_' in saved_name:
        original_name = saved_name.split('_', 1)[1]
    else:
        original_name = saved_name
    
    return send_from_directory(
        directory=str(UPLOAD_DIR),
        path=p.name,
        as_attachment=True,
        download_name=original_name  # 使用原始文件名（包含中文）
    )
    
@app.route("/api/download_pdf")
def download_pdf():
    session_id = request.args.get("session_id") or abort(400)
    
    # 查询数据库获取报告标题
    report_title = None
    try:
        connection = get_db_connection()
        if connection:
            with connection.cursor() as cursor:
                # 查询该session_id下有报告的消息的report_title
                sql = """
                    SELECT report_title 
                    FROM conversation_detail 
                    WHERE uuid = %s AND has_report = 1 AND report_title IS NOT NULL AND report_title != ''
                    LIMIT 1
                """
                cursor.execute(sql, (session_id,))
                result = cursor.fetchone()
                if result and result.get('report_title'):
                    report_title = result['report_title']
            connection.close()
    except Exception as e:
        logger.error(f"查询报告标题失败: {e}")
    
    # 使用兼容函数查找文件（优先新路径，兼容旧路径）
    file_path = _find_workspace_file(session_id, "final_report.pdf")
    if not file_path:
        abort(404)
    
    # 构建下载文件名：如果有报告标题则使用标题，否则使用默认名称
    if report_title:
        # 使用safe_filename_unicode函数处理文件名，确保支持中文
        safe_title = safe_filename_unicode(report_title)
        download_name = f"{safe_title}.pdf"
    else:
        download_name = f"report_{session_id}.pdf"
    
    return send_from_directory(
        directory=file_path.parent,
        path=file_path.name,
        as_attachment=True,
        download_name=download_name
    )


@app.route("/api/download_md")
def download_md():
    """下载Markdown格式的报告文件"""
    session_id = request.args.get("session_id") or abort(400)
    
    # 查询数据库获取报告标题
    report_title = None
    try:
        connection = get_db_connection()
        if connection:
            with connection.cursor() as cursor:
                # 查询该session_id下有报告的消息的report_title
                sql = """
                    SELECT report_title 
                    FROM conversation_detail 
                    WHERE uuid = %s AND has_report = 1 AND report_title IS NOT NULL AND report_title != ''
                    LIMIT 1
                """
                cursor.execute(sql, (session_id,))
                result = cursor.fetchone()
                if result and result.get('report_title'):
                    report_title = result['report_title']
            connection.close()
    except Exception as e:
        logger.error(f"查询报告标题失败: {e}")
    
    # 查找MD文件（在report目录下）
    md_path = PDF_DIR / session_id / "report" / "final_report.md"
    if not md_path.is_file():
        abort(404)
    
    # 构建下载文件名：如果有报告标题则使用标题，否则使用默认名称
    if report_title:
        # 使用safe_filename_unicode函数处理文件名，确保支持中文
        safe_title = safe_filename_unicode(report_title)
        download_name = f"{safe_title}.md"
    else:
        download_name = f"report_{session_id}.md"
    
    return send_from_directory(
        directory=md_path.parent,
        path=md_path.name,
        as_attachment=True,
        download_name=download_name
    )


@app.route("/api/download_references_txt")
def download_references_txt():
    """Download the line-separated title/journal/DOI reference list."""
    session_id = request.args.get("session_id") or abort(400)
    file_path = _find_workspace_file(session_id, "reference_download_list.txt")
    if not file_path:
        abort(404)
    return send_from_directory(
        directory=file_path.parent,
        path=file_path.name,
        as_attachment=True,
        download_name="参考文献下载清单.txt",
        mimetype="text/plain; charset=utf-8",
    )


# 简易RAG检索：在上传目录中检索文本文件，切片并按查询词重合度打分
import re as _re

def _find_uploaded_path_by_id(file_id: str) -> Optional[Path]:
    logger.debug(f"[DEBUG] Searching for file_id: {file_id}")
    
    # 首先在临时上传目录中查找
    for p in UPLOAD_DIR.glob(f"{file_id}_*"):
        logger.debug(f"[DEBUG] Found in UPLOAD_DIR: {p}")
        return p
    
    logger.debug(f"[DEBUG] Not found in UPLOAD_DIR, searching database...")
    
    # 如果临时目录没找到，尝试从数据库中查找文档库文件
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # MySQL使用%s作为占位符，查询字段是file_id而不是id
        cursor.execute('SELECT file_path FROM user_files WHERE file_id = %s', (file_id,))
        row = cursor.fetchone()
        conn.close()
        
        logger.debug(f"[DEBUG] Database query result: {row}")
        
        if row:
            # 处理字典或元组两种返回格式
            file_path_str = row.get('file_path') if isinstance(row, dict) else row[0]
            if file_path_str:
                file_path = Path(file_path_str)
                logger.debug(f"[DEBUG] File path from DB: {file_path}, exists: {file_path.exists()}")
                if file_path.exists():
                    return file_path
                else:
                    logger.error(f"[ERROR] File path exists in DB but file not found on disk: {file_path}")
    except Exception as e:
        logger.error(f"[ERROR] Failed to find file in database: {e}")
        import traceback
        traceback.print_exc()
    
    logger.error(f"[ERROR] File not found anywhere: {file_id}")
    return None

def _read_text_file(path: Path) -> str:
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return ''

# 读取PDF文本（多方案回退）
def _read_pdf_text(path: Path) -> str:
    # 优先使用 PyMuPDF
    try:
        import fitz  # PyMuPDF
        text = []
        with fitz.open(str(path)) as doc:
            for page in doc:
                text.append(page.get_text())
        return "\n".join(text)
    except Exception:
        pass
    # 其次使用 pdfminer.six
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ''
    except Exception:
        pass
    # 再次使用 PyPDF2（文本质量一般）
    try:
        import PyPDF2
        text = []
        with open(path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text.append(page.extract_text() or '')
        return "\n".join(text)
    except Exception:
        pass
    return ''


def _read_image_text(path: Path) -> str:
    """
    读取图片中的文本（OCR）。
    优先使用 pytesseract，失败时返回空字符串，不阻断主流程。
    """
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(str(path))
        text = pytesseract.image_to_string(img, lang='chi_sim+eng')
        return (text or '').strip()
    except Exception:
        return ''

def _split_chunks(text: str, max_chars: int = 1500, overlap: int = 200):
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + max_chars, n)
        chunk = text[i:end]
        chunks.append(chunk)
        if end >= n:
            break
        i = max(0, end - overlap)
    return chunks

def _score_chunk(query: str, chunk: str) -> float:
    """
    计算查询与文本片段的相关性分数（改进版：支持中文）
    """
    import re
    
    # 提取查询词：支持中文和英文
    # 中文：\u4e00-\u9fff 范围
    # 英文数字：\w+
    q_words = re.findall(r'[\u4e00-\u9fff]+|\w+', query.lower())
    c_words = re.findall(r'[\u4e00-\u9fff]+|\w+', chunk.lower())
    
    if not q_words or not c_words:
        return 0.0
    
    q_set = set(q_words)
    c_set = set(c_words)
    common = len(q_set & c_set)  # 共同词汇数
    
    if common == 0:
        return 0.0
    
    # 计算分数：共同词汇数 / (片段词汇数的平方根 + 1)
    # 这样即使片段很长，也能保持一定的相关性
    score = common / ((len(c_set) ** 0.5) + 1)
    
    # 额外奖励：如果查询词在片段中出现频率高
    if len(q_set) > 0:
        match_ratio = common / len(q_set)
        score = score * (1 + match_ratio * 0.5)  # 增加匹配比例权重
    
    return min(score, 1.0)  # 限制最大值为1.0

@app.route('/api/user_files/download_and_parse', methods=['POST'])
def download_and_parse_user_files():
    """
    下载并解析用户上传的文件，供Agent系统使用
    
    请求体: {
        "file_ids": ["file_id_1", "file_id_2"],
        "target_workspace": "/path/to/workspace"
    }
    
    返回: {
        "success": true,
        "files": [
            {
                "file_id": "xxx",
                "filename": "document.pdf",
                "local_path": "./user_uploads/document.pdf",
                "content": "文档内容...",
                "metadata": {...}
            }
        ]
    }
    """
    data = request.get_json(silent=True) or {}
    file_ids = data.get('file_ids') or []
    target_workspace = data.get('target_workspace') or ''
    
    if not file_ids:
        return jsonify({'success': False, 'message': '缺少file_ids参数'}), 400
    
    results = []
    for file_id in file_ids:
        # 查找上传的文件
        p = _find_uploaded_path_by_id(file_id)
        if not p:
            results.append({
                'file_id': file_id,
                'success': False,
                'error': 'File not found'
            })
            continue
        
        # 读取文件内容
        ext = p.suffix.lower()
        content = ''
        if ext == '.pdf':
            content = _read_pdf_text(p)
        elif ext in {'.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml', '.html', '.htm'}:
            content = _read_text_file(p)
        elif ext in {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tiff'}:
            content = _read_image_text(p)
        
        # 返回文件信息 - 尝试从数据库获取原始文件名
        filename = p.name.split('_', 1)[1] if '_' in p.name else p.name
        
        # 如果文件名是UUID格式，尝试从数据库查询原始文件名
        if filename == p.name and len(filename) > 30:  # 可能是UUID
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('SELECT original_filename FROM user_files WHERE file_id = %s', (file_id,))
                row = cursor.fetchone()
                conn.close()
                if row:
                    original_filename = row.get('original_filename') if isinstance(row, dict) else row[0]
                    if original_filename:
                        filename = original_filename
                        logger.debug(f"[DEBUG] Found original filename from DB: {filename}")
            except Exception as e:
                logger.error(f"[DEBUG] Failed to get original filename: {e}")
        
        file_result = {
            'file_id': file_id,
            'filename': filename,
            'local_path': str(p.absolute()),  # 实际物理路径（绝对路径）
            'source_path': str(p.absolute()),  # 原始文件路径（用于复制，绝对路径）
            'content': content[:50000] if content else '',  # 限制大小（仅文本文件）
            'content_length': len(content),
            'file_type': ext,
            'success': True
        }
        logger.info(f"[API] Successfully processed file: {filename}, source_path: {p.absolute()}")
        results.append(file_result)
    
    return jsonify({
        'success': True,
        'files': results,
        'total_files': len(results)
    })

@app.route('/api/rag/search', methods=['POST'])
def rag_search():
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    file_ids = data.get('file_ids') or []
    # 兼容字符串入参：支持单个ID或逗号分隔多个ID
    if isinstance(file_ids, str):
        file_ids = [s.strip() for s in file_ids.split(',') if s.strip()]
    elif not isinstance(file_ids, list):
        file_ids = []
    top_k = int(data.get('top_k') or 5)
    # 新增可选模式：snippets（默认）或 full（全文作为提示词）
    mode = (data.get('mode') or 'snippets').lower()
    # 可选参数：自定义切片大小与重叠
    chunk_size = int(data.get('chunk_size') or 1500)
    overlap = int(data.get('overlap') or 200)

    if not query:
        return jsonify({'success': False, 'message': '缺少query'}), 400

    TEXT_EXTENSIONS = {'.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml', '.html', '.htm'}
    results = []

    for fid in file_ids:
        p = _find_uploaded_path_by_id(fid)
        if not p:
            continue
        ext = p.suffix.lower()
        # 支持PDF解析
        if ext == '.pdf':
            text = _read_pdf_text(p)
        elif ext in TEXT_EXTENSIONS:
            text = _read_text_file(p)
        elif ext in {'.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tiff'}:
            text = _read_image_text(p)
        else:
            # 其他二进制文档暂不解析
            text = ''
        if not text.strip():
            continue
        if mode == 'full':
            max_full_chars = int(data.get('max_full_chars') or 8000)
            full_chunk = text.strip()[:max_full_chars]
            if full_chunk:
                results.append({
                    'file_id': fid,
                    'filename': p.name.split('_', 1)[1] if '_' in p.name else p.name,
                    'chunk': full_chunk,
                    'score': 1.0
                })
        else:
            chunks = _split_chunks(text, max_chars=chunk_size, overlap=overlap)
            scored = [(chunk, _score_chunk(query, chunk)) for chunk in chunks]
            scored.sort(key=lambda x: x[1], reverse=True)
            for chunk, score in scored[:top_k]:
                results.append({
                    'file_id': fid,
                    'filename': p.name.split('_', 1)[1] if '_' in p.name else p.name,
                    'chunk': chunk,
                    'score': round(score, 4)
                })

    results.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'success': True, 'snippets': results[:top_k]})

# 文件上传配置
UPLOAD_BASE_DIR = BASE_DIR / "user_files"  # 基础文件存储目录
ALLOWED_EXTENSIONS_LIBRARY = {'pdf', 'doc', 'docx', 'png', 'jpg', 'jpeg', 'ppt', 'pptx', 'txt', 'xlsx', 'xls'}  # 文档库允许的扩展名（不带点号）
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# 确保基础上传目录存在
UPLOAD_BASE_DIR.mkdir(parents=True, exist_ok=True)

def allowed_file(filename):
    """检查文件扩展名是否允许（用于文档库上传）"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS_LIBRARY


def get_user_upload_dir(user_id):
    """获取用户专属的上传目录"""
    user_dir = UPLOAD_BASE_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_file_size_mb(size_bytes):
    """将字节转换为MB"""
    return round(size_bytes / (1024 * 1024), 2)


# ------------------- 文件管理接口 -------------------
@app.route('/api/files/upload', methods=['POST'])
def upload_files():
    """
    上传文件接口
    请求：multipart/form-data
    - files: 文件列表
    - user_id: 用户ID
    """
    try:
        # 获取用户ID
        user_id = request.form.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'message': '缺少用户ID'}), 400

        # 获取上传的文件列表
        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'message': '没有上传文件'}), 400

        # 检查文件数量
        if len(files) > 10:
            return jsonify({'success': False, 'message': '一次最多上传10个文件'}), 400

        # 获取用户上传目录
        user_dir = get_user_upload_dir(user_id)

        # 数据库连接
        connection = get_db_connection()
        uploaded_files = []
        total_size = 0

        try:
            with connection.cursor() as cursor:
                for file in files:
                    if file.filename == '':
                        continue

                    # 验证文件类型
                    if not allowed_file(file.filename):
                        return jsonify({
                            'success': False,
                            'message': f'文件 {file.filename} 格式不支持'
                        }), 400

                    # 生成安全的文件名
                    # 保存原始文件名用于显示（直接使用用户上传的文件名）
                    original_filename = file.filename
                    file_id = str(uuid.uuid4())
                    
                    # 安全地提取文件扩展名
                    if '.' in original_filename:
                        file_extension = original_filename.rsplit('.', 1)[1].lower()
                    else:
                        return jsonify({
                            'success': False,
                            'message': f'文件 {file.filename} 缺少文件扩展名'
                        }), 400
                    
                    # 生成服务器存储用的安全文件名（使用UUID避免冲突）
                    safe_filename = f"{file_id}.{file_extension}"

                    # 保存文件
                    file_path = user_dir / safe_filename
                    file.save(str(file_path))

                    # 获取文件大小
                    file_size = os.path.getsize(file_path)
                    total_size += file_size

                    # 检查总大小是否超限
                    if total_size > MAX_FILE_SIZE:
                        # 删除已上传的文件
                        os.remove(file_path)
                        return jsonify({
                            'success': False,
                            'message': '文件总大小超过50MB限制'
                        }), 400

                    # 将文件信息存入数据库
                    sql = """
                        INSERT INTO user_files 
                        (file_id, user_id, original_filename, stored_filename, file_path, 
                         file_size, file_type, status, upload_time)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """
                    cursor.execute(sql, (
                        file_id,
                        user_id,
                        original_filename,
                        safe_filename,
                        str(file_path),
                        file_size,
                        file_extension,
                        'processing'  # 初始状态为处理中
                    ))

                    uploaded_files.append({
                        'file_id': file_id,
                        'filename': original_filename,
                        'size': file_size
                    })

                connection.commit()

                # TODO: 这里可以触发文件处理任务（例如文本提取、向量化等）
                # 暂时将所有文件状态设置为已处理
                for uploaded_file in uploaded_files:
                    sql_update = "UPDATE user_files SET status = 'processed' WHERE file_id = %s"
                    cursor.execute(sql_update, (uploaded_file['file_id'],))
                connection.commit()

                return jsonify({
                    'success': True,
                    'message': '文件上传成功',
                    'uploaded_count': len(uploaded_files),
                    'files': uploaded_files
                }), 200

        except Exception as e:
            connection.rollback()
            # 清理已上传的文件
            for uploaded_file in uploaded_files:
                # 使用glob模式查找文件，因为file_extension可能未定义
                file_pattern = f"{uploaded_file['file_id']}.*"
                for file_path in user_dir.glob(file_pattern):
                    if file_path.exists():
                        os.remove(file_path)
            raise e
        finally:
            connection.close()

    except Exception as e:
        logger.error(f"文件上传失败: {str(e)}")
        return jsonify({'success': False, 'message': f'文件上传失败: {str(e)}'}), 500


@app.route('/api/files/list/<user_id>', methods=['GET'])
def get_user_files(user_id):
    """
    获取用户的文件列表
    """
    try:
        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                sql = """
                    SELECT file_id, original_filename as name, file_size as size, 
                           status, upload_time, 
                           CASE 
                               WHEN status = 'processed' THEN TRUE 
                               ELSE FALSE 
                           END as can_qa
                    FROM user_files
                    WHERE user_id = %s
                    ORDER BY upload_time DESC
                """
                cursor.execute(sql, (user_id,))
                files = cursor.fetchall()

                # 转换数据格式
                file_list = []
                for file in files:
                    file_list.append({
                        'id': file['file_id'],
                        'name': file['name'],
                        'size': file['size'],
                        'status': '已处理' if file['status'] == 'processed' else '处理中' if file[
                                                                                                 'status'] == 'processing' else '失败',
                        'upload_time': convert_datetime_to_string(file['upload_time']),
                        'can_qa': bool(file['can_qa'])
                    })

                return jsonify({
                    'success': True,
                    'files': file_list
                }), 200

        finally:
            connection.close()

    except Exception as e:
        logger.error(f"获取文件列表失败: {str(e)}")
        return jsonify({'success': False, 'message': f'获取文件列表失败: {str(e)}'}), 500


@app.route('/api/files/download/<file_id>', methods=['GET'])
def download_file(file_id):
    """
    下载文件
    """
    try:
        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                # 查询文件信息
                sql = """
                    SELECT user_id, original_filename, stored_filename, file_path
                    FROM user_files
                    WHERE file_id = %s
                """
                cursor.execute(sql, (file_id,))
                file_info = cursor.fetchone()

                if not file_info:
                    return jsonify({'success': False, 'message': '文件不存在'}), 404

                # 获取文件路径
                file_path = Path(file_info['file_path'])

                if not file_path.exists():
                    return jsonify({'success': False, 'message': '文件已被删除'}), 404

                # 发送文件
                return send_file(
                    file_path,
                    as_attachment=True,
                    download_name=file_info['original_filename']
                )

        finally:
            connection.close()

    except Exception as e:
        logger.error(f"文件下载失败: {str(e)}")
        return jsonify({'success': False, 'message': f'文件下载失败: {str(e)}'}), 500


@app.route('/api/files/delete/<file_id>', methods=['DELETE'])
def delete_file(file_id):
    """
    删除文件
    """
    try:
        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                # 查询文件信息
                sql = """
                    SELECT user_id, file_path
                    FROM user_files
                    WHERE file_id = %s
                """
                cursor.execute(sql, (file_id,))
                file_info = cursor.fetchone()

                if not file_info:
                    return jsonify({'success': False, 'message': '文件不存在'}), 404

                # 删除物理文件
                file_path = Path(file_info['file_path'])
                if file_path.exists():
                    os.remove(file_path)

                # 从数据库删除记录
                sql_delete = "DELETE FROM user_files WHERE file_id = %s"
                cursor.execute(sql_delete, (file_id,))
                connection.commit()

                return jsonify({
                    'success': True,
                    'message': '文件删除成功'
                }), 200

        except Exception as e:
            connection.rollback()
            raise e
        finally:
            connection.close()

    except Exception as e:
        logger.error(f"文件删除失败: {str(e)}")
        return jsonify({'success': False, 'message': f'文件删除失败: {str(e)}'}), 500


@app.route('/api/files/batch-delete', methods=['POST'])
def batch_delete_files():
    """
    批量删除文件
    """
    try:
        # 获取请求数据
        data = request.get_json()
        file_ids = data.get('file_ids', [])
        
        if not file_ids or not isinstance(file_ids, list):
            return jsonify({'success': False, 'message': '无效的文件ID列表'}), 400
        
        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                # 查询所有要删除的文件信息
                placeholders = ','.join(['%s'] * len(file_ids))
                sql = f"""
                    SELECT user_id, file_path, file_id
                    FROM user_files
                    WHERE file_id IN ({placeholders})
                """
                cursor.execute(sql, file_ids)
                files_to_delete = cursor.fetchall()
                
                # 检查是否有文件存在
                if not files_to_delete:
                    return jsonify({'success': False, 'message': '没有找到要删除的文件'}), 404
                
                # 收集实际要删除的文件ID
                actual_file_ids = [file['file_id'] for file in files_to_delete]
                
                # 删除物理文件
                for file_info in files_to_delete:
                    file_path = Path(file_info['file_path'])
                    if file_path.exists():
                        os.remove(file_path)
                
                # 从数据库批量删除记录
                sql_delete = f"DELETE FROM user_files WHERE file_id IN ({placeholders})"
                cursor.execute(sql_delete, actual_file_ids)
                connection.commit()
                
                return jsonify({
                    'success': True,
                    'message': f'成功删除{len(actual_file_ids)}个文件',
                    'deleted_count': len(actual_file_ids)
                }), 200
        
        except Exception as e:
            connection.rollback()
            raise e
        finally:
            connection.close()

    except Exception as e:
        logger.error(f"批量删除文件失败: {str(e)}")
        return jsonify({'success': False, 'message': f'批量删除文件失败: {str(e)}'}), 500


@app.route('/api/files/start_qa', methods=['POST'])
def start_file_qa():
    """
    开始文件问答
    """
    try:
        data = request.get_json()
        file_id = data.get('file_id')
        session_id = data.get('session_id')

        if not file_id or not session_id:
            return jsonify({'success': False, 'message': '缺少必要参数'}), 400

        connection = get_db_connection()
        try:
            with connection.cursor() as cursor:
                # 查询文件信息
                sql = """
                    SELECT file_id, original_filename, status
                    FROM user_files
                    WHERE file_id = %s
                """
                cursor.execute(sql, (file_id,))
                file_info = cursor.fetchone()

                if not file_info:
                    return jsonify({'success': False, 'message': '文件不存在'}), 404

                if file_info['status'] != 'processed':
                    return jsonify({'success': False, 'message': '文件尚未处理完成'}), 400

                # TODO: 这里可以创建文件问答会话，关联file_id和session_id
                # 例如：将file_id存入会话上下文，供后续问答使用

                return jsonify({
                    'success': True,
                    'message': f'已为文件 {file_info["original_filename"]} 启动问答模式',
                    'file_id': file_id,
                    'filename': file_info['original_filename']
                }), 200

        finally:
            connection.close()

    except Exception as e:
        logger.error(f"启动文件问答失败: {str(e)}")
        return jsonify({'success': False, 'message': f'启动文件问答失败: {str(e)}'}), 500

#健康检查接口
@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return jsonify({'status': 'healthy', 'message': '服务器运行正常'})


if __name__ == '__main__':
    # 生产环境请修改debug=False，并配置合适的host和port
    app.run(debug=False, host='0.0.0.0', port=5000)
