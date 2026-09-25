"""Utilitários de segurança compartilhados (sessão, senha, SSH, rate-limit, auditoria)."""
import os
import time
import functools
import secrets
import hmac
import posixpath
import logging
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from threading import Lock

import paramiko
from flask import session, redirect, url_for, request, jsonify, g

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


# =============================================================================
# RATE LIMITING (Opção 3)
# =============================================================================
# Estrutura: {chave: [timestamps]}
_rate_limit_store = defaultdict(list)
_rate_limit_lock = Lock()

# Configurações de rate limiting (podem ser sobrescritas por variáveis de ambiente)
RATE_LIMIT_API_POR_MINUTO = int(os.getenv('RATE_LIMIT_API_POR_MINUTO', '60'))
RATE_LIMIT_ADMIN_POR_MINUTO = int(os.getenv('RATE_LIMIT_ADMIN_POR_MINUTO', '30'))
RATE_LIMIT_LOGIN_POR_MINUTO = int(os.getenv('RATE_LIMIT_LOGIN_POR_MINUTO', '10'))
RATE_LIMIT_JANELA_SEGUNDOS = 60  # 1 minuto


def _get_rate_limit_key(prefix: str, identifier: str = None) -> str:
    """Gera uma chave única para rate limiting."""
    if identifier is None:
        identifier = ip_cliente()
    return f"{prefix}:{identifier}"


def _limpar_rate_limit_antigo(chave: str, agora: float):
    """Remove timestamps antigos da janela de rate limiting."""
    _rate_limit_store[chave] = [t for t in _rate_limit_store[chave] if agora - t < RATE_LIMIT_JANELA_SEGUNDOS]


def verificar_rate_limit(prefix: str, limite: int, identifier: str = None) -> tuple[bool, dict]:
    """
    Verifica se o rate limit foi excedido.
    Retorna (excedido, info) onde info contém detalhes para headers de resposta.
    """
    chave = _get_rate_limit_key(prefix, identifier)
    agora = time.time()
    
    with _rate_limit_lock:
        _limpar_rate_limit_antigo(chave, agora)
        atual = len(_rate_limit_store[chave])
        excedido = atual >= limite
        
        if not excedido:
            _rate_limit_store[chave].append(agora)
            atual += 1
        
        reset_em = int(agora + RATE_LIMIT_JANELA_SEGUNDOS)
        info = {
            'limite': limite,
            'restante': max(0, limite - atual),
            'reset': reset_em,
            'chave': chave,
        }
    
    return excedido, info


def rate_limit_api(f):
    """Decorator para rate limiting de APIs (60 req/min por IP)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        excedido, info = verificar_rate_limit('api', RATE_LIMIT_API_POR_MINUTO)
        if excedido:
            return jsonify({
                'erro': 'Rate limit excedido. Tente novamente em breve.',
                'retry_after': RATE_LIMIT_JANELA_SEGUNDOS
            }), 429
        response = f(*args, **kwargs)
        # Adiciona headers de rate limit na resposta
        if hasattr(response, 'headers'):
            response.headers['X-RateLimit-Limit'] = str(info['limite'])
            response.headers['X-RateLimit-Remaining'] = str(info['restante'])
            response.headers['X-RateLimit-Reset'] = str(info['reset'])
        return response
    return wrapper


def rate_limit_admin(f):
    """Decorator para rate limiting de área admin (30 req/min por usuário/IP)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        # Usa user_id se logado, senão IP
        identifier = session.get('user_id') or ip_cliente()
        excedido, info = verificar_rate_limit('admin', RATE_LIMIT_ADMIN_POR_MINUTO, identifier)
        if excedido:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({
                    'erro': 'Rate limit excedido. Tente novamente em breve.',
                    'retry_after': RATE_LIMIT_JANELA_SEGUNDOS
                }), 429
            return jsonify({
                'erro': 'Muitas requisições. Aguarde um momento.'
            }), 429
        response = f(*args, **kwargs)
        if hasattr(response, 'headers'):
            response.headers['X-RateLimit-Limit'] = str(info['limite'])
            response.headers['X-RateLimit-Remaining'] = str(info['restante'])
            response.headers['X-RateLimit-Reset'] = str(info['reset'])
        return response
    return wrapper


def rate_limit_login(f):
    """Decorator para rate limiting de login (10 req/min por IP)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        excedido, info = verificar_rate_limit('login', RATE_LIMIT_LOGIN_POR_MINUTO)
        if excedido:
            return jsonify({
                'erro': 'Muitas tentativas de login. Aguarde um momento.',
                'retry_after': RATE_LIMIT_JANELA_SEGUNDOS
            }), 429
        return f(*args, **kwargs)
    return wrapper


# =============================================================================
# AUDITORIA ESTRUTURADA (Opção 3)
# =============================================================================
AUDIT_LOGGER = logging.getLogger('auditoria')
AUDIT_LOGGER.setLevel(logging.INFO)

# Handler para arquivo de auditoria (se configurado)
_audit_handler = None


def configurar_auditoria(log_file: str = None):
    """Configura o logger de auditoria para escrever em arquivo."""
    global _audit_handler
    if _audit_handler:
        AUDIT_LOGGER.removeHandler(_audit_handler)
    
    if log_file:
        _audit_handler = logging.FileHandler(log_file)
        _audit_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        AUDIT_LOGGER.addHandler(_audit_handler)
        AUDIT_LOGGER.propagate = False


def _get_user_info() -> dict:
    """Extrai informações do usuário atual para auditoria."""
    return {
        'user_id': session.get('user_id'),
        'user_nome': session.get('user_nome'),
        'eh_master': session.get('eh_master', False),
        'ip': ip_cliente(),
        'user_agent': request.headers.get('User-Agent', '')[:200],
    }


def registrar_auditoria(acao: str, detalhes: dict = None, nivel: str = 'INFO'):
    """
    Registra uma ação de auditoria estruturada.
    
    Args:
        acao: Nome da ação (ex: 'login', 'logout', 'upload_banco', 'excluir_alias')
        detalhes: Dicionário com detalhes adicionais da ação
        nivel: Nível de log (INFO, WARNING, ERROR)
    """
    user_info = _get_user_info()
    
    log_entry = {
        'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'acao': acao,
        'usuario': user_info,
        'detalhes': detalhes or {},
        'request_id': getattr(g, 'request_id', None),
    }
    
    # Log estruturado como JSON
    log_message = json.dumps(log_entry, ensure_ascii=False)
    
    if nivel == 'WARNING':
        AUDIT_LOGGER.warning(log_message)
    elif nivel == 'ERROR':
        AUDIT_LOGGER.error(log_message)
    else:
        AUDIT_LOGGER.info(log_message)


def registrar_login_sucesso(usuario_id: int, usuario_nome: str, eh_master: bool):
    """Registra login bem-sucedido."""
    registrar_auditoria('login_sucesso', {
        'usuario_id': usuario_id,
        'usuario_nome': usuario_nome,
        'eh_master': eh_master,
    })


def registrar_login_falha(email: str, motivo: str):
    """Registra tentativa de login falhada."""
    registrar_auditoria('login_falha', {
        'email': email,
        'motivo': motivo,
    }, nivel='WARNING')


def registrar_logout(usuario_id: int, usuario_nome: str):
    """Registra logout."""
    registrar_auditoria('logout', {
        'usuario_id': usuario_id,
        'usuario_nome': usuario_nome,
    })


def registrar_acao_admin(acao: str, detalhes: dict = None):
    """Registra ação administrativa sensível."""
    registrar_auditoria(f'admin_{acao}', detalhes or {})


# =============================================================================
# SESSION HARDENING (Opção 3)
# =============================================================================
def regenerar_session_id():
    """
    Regenera o ID da sessão mantendo os dados.
    Deve ser chamado após login bem-sucedido para prevenir session fixation.
    """
    # Salva dados atuais da sessão
    session_data = dict(session)
    # Limpa a sessão atual
    session.clear()
    # Regenera o ID da sessão
    session.modified = True
    # Restaura os dados
    session.update(session_data)


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

    # Configurar auditoria (Opção 3)
    audit_log_file = os.getenv('AUDIT_LOG_FILE')
    if audit_log_file:
        configurar_auditoria(audit_log_file)

    # Headers de segurança (Opção 1 - sem HSTS pois roda em intranet)
    @app.after_request
    def _security_headers(response):
        # Impede clickjacking (site embutido em iframe)
        response.headers['X-Frame-Options'] = 'DENY'
        # Impede o navegador de "adivinhar" o tipo de arquivo (MIME sniffing)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Controla quanto da URL de origem é enviada em requisições cruzadas
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # Desabilita APIs de navegador desnecessárias (geolocalização, microfone, câmera)
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        # Content Security Policy: restringe de onde scripts, estilos, imagens podem vir
        # 'unsafe-inline' necessário para templates atuais; 'unsafe-eval' não usado
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers['Content-Security-Policy'] = csp
        # HSTS (Strict-Transport-Security) NÃO adicionado: roda em intranet HTTP
        return response

    # Request ID para rastreamento em auditoria
    @app.before_request
    def _gerar_request_id():
        g.request_id = secrets.token_hex(8)

    # Rate limiting global para APIs (Opção 3)
    @app.before_request
    def _rate_limit_global():
        # Rate limit para APIs
        # O teste público de CNAME possui assinatura HMAC, cache e limitador próprio
        # de 100 consultas/s; não deve consumir a cota genérica compartilhada das APIs.
        if request.path.startswith('/api/') and request.endpoint != 'bancos.api_testar_cname':
            excedido, info = verificar_rate_limit('api', RATE_LIMIT_API_POR_MINUTO)
            if excedido:
                return jsonify({
                    'erro': 'Rate limit excedido. Tente novamente em breve.',
                    'retry_after': RATE_LIMIT_JANELA_SEGUNDOS
                }), 429
            # Adiciona headers de rate limit
            g._rate_limit_info = info
        
        # Rate limit para área admin (rotas /admin/* exceto login)
        if request.path.startswith('/admin/') and request.endpoint not in {
            'bancos.admin_login', 'bancos.esqueci_senha', 'bancos.admin_register',
            'bancos.redefinir_senha_token'
        }:
            identifier = session.get('user_id') or ip_cliente()
            excedido, info = verificar_rate_limit('admin', RATE_LIMIT_ADMIN_POR_MINUTO, identifier)
            if excedido:
                if request.is_json:
                    return jsonify({
                        'erro': 'Rate limit excedido. Tente novamente em breve.',
                        'retry_after': RATE_LIMIT_JANELA_SEGUNDOS
                    }), 429
                return jsonify({'erro': 'Muitas requisições. Aguarde um momento.'}), 429
            g._rate_limit_info = info

    # Adiciona headers de rate limit nas respostas
    @app.after_request
    def _adicionar_rate_limit_headers(response):
        info = getattr(g, '_rate_limit_info', None)
        if info:
            response.headers['X-RateLimit-Limit'] = str(info['limite'])
            response.headers['X-RateLimit-Remaining'] = str(info['restante'])
            response.headers['X-RateLimit-Reset'] = str(info['reset'])
        return response

    # Validação global de CSRF para rotas POST (exceto páginas públicas)
    @app.before_request
    def _validar_csrf_global():
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return
        # Pular validação para endpoints públicos conhecidos
        endpoint = request.endpoint or ''
        paginas_publicas_sem_csrf = {
            'bancos.admin_login', 'bancos.esqueci_senha', 'bancos.admin_register',
            'bancos.redefinir_senha_token', 'bancos.api_testar_cname',
        }
        if endpoint in paginas_publicas_sem_csrf:
            return
        # Pular para rotas de API que usam autenticação por token/header próprio
        if request.path.startswith('/api/') and request.is_json:
            return
        esperado = session.get('_csrf_token')
        recebido = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        if not esperado or not recebido or not hmac.compare_digest(esperado.encode(), recebido.encode()):
            return jsonify(erro='Requisição inválida. Recarregue a página e tente novamente.'), 400

    return app
