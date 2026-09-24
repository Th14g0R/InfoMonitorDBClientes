"""Utilitários de segurança compartilhados (sessão, senha, SSH, rate-limit)."""
import os
import time
import functools
import secrets
import hmac
import posixpath
from datetime import datetime, timedelta
from collections import defaultdict

import paramiko
from flask import session, redirect, url_for, request, jsonify

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_HOSTS_PATH = os.getenv('SSH_KNOWN_HOSTS') or os.path.join(BASE_DIR, 'known_hosts')


def csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


def validar_caminho_banco(caminho, base):
    if not isinstance(caminho, str) or not caminho.startswith('/'):
        raise ValueError('Caminho de banco inválido.')
    if any(ord(c) < 32 or ord(c) == 127 for c in caminho) or '\\' in caminho or '..' in caminho.split('/'):
        raise ValueError('Caminho de banco inválido.')
    normalizado = posixpath.normpath(caminho)
    if not normalizado.startswith(posixpath.normpath(base) + '/') or not normalizado.lower().endswith('.fdb'):
        raise ValueError('Caminho de banco inválido.')
    return normalizado

# Tentativas de login por IP: {ip: [timestamps]}
_falhas_login = defaultdict(list)
LOGIN_MAX_TENTATIVAS = int(os.getenv('LOGIN_MAX_TENTATIVAS', '8'))
LOGIN_JANELA_SEGUNDOS = int(os.getenv('LOGIN_JANELA_SEGUNDOS', '900'))  # 15 min
SENHA_MIN_CHARS = int(os.getenv('SENHA_MIN_CHARS', '8'))
SECRET_KEY_MIN_CHARS = 32
TOKEN_RESET_HORAS = int(os.getenv('TOKEN_RESET_HORAS', '1'))


def senha_atende_politica(senha):
    if not senha or len(senha) < SENHA_MIN_CHARS:
        return False, f"A senha deve ter no mínimo {SENHA_MIN_CHARS} caracteres."
    return True, None


def ip_cliente():
    return request.remote_addr or '0.0.0.0'


def login_bloqueado(ip=None):
    ip = ip or ip_cliente()
    agora = time.time()
    _falhas_login[ip] = [t for t in _falhas_login[ip] if agora - t < LOGIN_JANELA_SEGUNDOS]
    return len(_falhas_login[ip]) >= LOGIN_MAX_TENTATIVAS


def registrar_falha_login(ip=None):
    _falhas_login[ip or ip_cliente()].append(time.time())


def limpar_falhas_login(ip=None):
    _falhas_login.pop(ip or ip_cliente(), None)


def token_expira_em():
    return (datetime.utcnow() + timedelta(hours=TOKEN_RESET_HORAS)).strftime('%Y-%m-%d %H:%M:%S')


def token_ainda_valido(expira_str):
    if not expira_str:
        return False
    try:
        return datetime.utcnow() <= datetime.strptime(expira_str, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return False


def destino_redirect_seguro(destino, fallback='/admin'):
    """Evita open-redirect: só caminhos relativos internos."""
    if not destino:
        return fallback
    destino = destino.strip()
    if not destino.startswith('/') or destino.startswith('//') or '\\' in destino:
        return fallback
    if destino.lower().startswith(('/\\', '/http')):
        return fallback
    return destino


def exigir_login_bancos():
    """True se a sessão está autenticada; senão devolve resposta de redirect/401."""
    if session.get('logged_in') and session.get('user_id'):
        return True, None
    if request.path.startswith('/api/') or request.is_json:
        return False, (jsonify({"erro": "nao autenticado"}), 401)
    return False, redirect(url_for('bancos.admin_login', next=request.path))


def login_obrigatorio(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        ok, resp = exigir_login_bancos()
        if not ok:
            return resp
        return f(*args, **kwargs)
    return wrapper


def criar_cliente_ssh():
    """Aceita somente chaves de servidores previamente verificadas."""
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if os.path.isfile(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    return client


def conectar_ssh(info_servidor, timeout=10):
    client = criar_cliente_ssh()
    client.connect(
        hostname=info_servidor['ip'],
        port=22,
        username=info_servidor['usuario'],
        password=info_servidor['senha'],
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def aplicar_config_flask(app):
    secret = os.getenv('SECRET_KEY', '').strip()
    if len(secret) < SECRET_KEY_MIN_CHARS or secret == 'chave_secreta_para_sessoes_admin':
        raise RuntimeError(
            f"Defina SECRET_KEY no arquivo .env com pelo menos {SECRET_KEY_MIN_CHARS} "
            "caracteres aleatórios. O sistema recusa chaves ausentes, curtas ou padrão."
        )
    app.secret_key = secret
    app.jinja_env.globals['csrf_token'] = csrf_token
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', '0') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.getenv('SESSION_HORAS', '8'))),
        MAX_CONTENT_LENGTH=int(os.getenv('MAX_UPLOAD_MB', '2048')) * 1024 * 1024,
    )

    @app.before_request
    def validar_csrf():
        if request.method in {'GET', 'HEAD', 'OPTIONS'}:
            return
        esperado = session.get('_csrf_token')
        recebido = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        if not esperado or not recebido or not hmac.compare_digest(esperado.encode(), recebido.encode()):
            return jsonify(erro='Requisição inválida. Recarregue a página e tente novamente.'), 400

    @app.after_request
    def cabecalhos_seguranca(resp):
        if resp.mimetype == 'text/html':
            resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
        resp.headers['Referrer-Policy'] = 'same-origin'
        resp.headers['X-XSS-Protection'] = '1; mode=block'
        return resp

    return app
