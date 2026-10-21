from flask import Blueprint, render_template, render_template_string, request, jsonify, session, redirect, url_for, flash, g, current_app
import paramiko
import posixpath
import shlex
from contextlib import closing
from datetime import datetime, timedelta
import time
import sqlite3
import os
import re
import unicodedata
import socket
import smtplib
import secrets
import hashlib
import hmac
import json  # FIX: Módulo json importado para interpretar a variável SERVIDORES_CONFIG
import threading
import stat
import copy
import logging
from urllib.parse import urlencode
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import urllib.error
from certificados import configurar_certificados
from backups import (
    interpretar_status_backups,
    analisar_status_para_alerta,
    gerar_relatorio_email_backups,
    enviar_email_alerta_backups,
    extrair_dias_atraso,
    montar_linhas_historico_alertas,
    EMAIL_DESTINATARIO_PADRAO,
    ASSUNTO_ALERTA_PADRAO,
    FUSO_LOCAL,
)
from gestao_bancos import listar_pastas, adicionar_banco
from seguranca import (
    senha_atende_politica, login_bloqueado, registrar_falha_login, limpar_falhas_login,
    token_expira_em, token_ainda_valido, destino_redirect_seguro,
    conectar_ssh, validar_caminho_banco,
    regenerar_session_id, registrar_login_sucesso, registrar_login_falha,
    registrar_logout, registrar_acao_admin,
)
from infobrasil_api import (
    obter_configuracao_api,
    salvar_variaveis_env,
    testar_conexao_api,
    sincronizar_lojas_com_api,
    obter_ultimo_status_sincronizacao,
)

# Configuração do Blueprint e Banco
bancos_bp = Blueprint('bancos', __name__, template_folder='templates')
LOGGER = logging.getLogger(__name__)
ALLOW_PUBLIC_REGISTER = os.getenv('ALLOW_PUBLIC_REGISTER', '0') == '1'

# Permissões concedidas por usuário; o Master tem acesso total.
PERMISSOES_DISPONIVEIS = {
    'perm_servidores': 'Editar CNAME/Lojas na Lista de Servidores',
    'perm_gestao_bancos': 'Gestão de Bancos (Admin: enviar/criar/editar/excluir alias)',
    'perm_horarios': 'Painel de Horários',
    'perm_certificados': 'Gerenciar certificados (adicionar e verificar subdomínios)',
}

@bancos_bp.before_request
def revalidar_sessao_a_cada_requisicao():
    """
    FIX: recarrega o usuário logado direto do banco a cada requisição (em vez de confiar
    apenas no que foi gravado na sessão no momento do login). Isso garante que, se o Master
    inativar um usuário ou mudar suas permissões, o efeito é imediato na próxima ação/clique
    dele, sem precisar esperar ele deslogar/logar de novo.
    """
    g.usuario_atual = None
    if session.get('logged_in') and session.get('user_id'):
        conn = get_db_connection()
        usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (session['user_id'],)).fetchone()
        conn.close()
        if not usuario or not usuario['ativo']:
            # Usuário foi excluído ou inativado enquanto a sessão estava aberta: derruba a sessão agora.
            session.clear()
            return
        g.usuario_atual = usuario
        # Mantém a sessão sincronizada com o que está no banco (nome/master podem ter mudado)
        session['user_nome'] = nome_para_assinatura(usuario['nome'])
        session['eh_master'] = usuario['eh_master']

PAGINAS_PUBLICAS = {
    'bancos.exibir_servidor', 'bancos.exibir_todos', 'bancos.exibir_inativos',
    'bancos.exibir_orfaos', 'bancos.exibir_historico', 'bancos.exibir_backups_ftp',
    'bancos.api_historico', 'bancos.api_testar_cname', 'bancos.api_consulta_cnpj',
    'bancos.admin_login', 'bancos.esqueci_senha', 'bancos.admin_register',
    'bancos.redefinir_senha_token', 'bancos.certificados', 'bancos.consulta_cnpj',
    'bancos.rota_relatorio_clientes_lojas',
}


@bancos_bp.before_request
def proteger_gestao():
    if request.endpoint in PAGINAS_PUBLICAS or g.get('usuario_atual'):
        return
    if request.is_json or request.path.startswith('/api/'):
        return jsonify(erro='Autenticação necessária.'), 401
    return redirect(url_for('bancos.admin_login', next=request.path))


def tem_permissao(codigo_permissao):
    """Master sempre tem acesso total. Demais usuários dependem da coluna de permissão específica."""
    usuario = g.get('usuario_atual')
    if not usuario:
        return False
    if usuario['eh_master']:
        return True
    try:
        return bool(usuario[codigo_permissao])
    except (IndexError, KeyError):
        return False

bancos_bp.add_app_template_global(tem_permissao, name='tem_permissao')

@bancos_bp.app_context_processor
def inject_flags_ui():
    return {'allow_register': ALLOW_PUBLIC_REGISTER}

def exigir_permissao(codigo_permissao):
    """Para uso em rotas: True se autorizado, senão já devolve a resposta de erro/redirecionamento pronta."""
    if not session.get('logged_in'):
        return False, redirect('/admin/login')
    if not tem_permissao(codigo_permissao):
        return False, ("Você não tem permissão para esta ação. Fale com o administrador Master.", 403)
    return True, None

def verificar_senha_confirmacao(senha_informada):
    """Reconfirma a senha do usuário logado antes de ações sensíveis (criar/editar/excluir alias),
    evitando que outra pessoa use uma sessão aberta e esquecida no computador."""
    usuario = g.get('usuario_atual')
    if not usuario or not senha_informada:
        return False
    return check_password_hash(usuario['senha_hash'], senha_informada)

def get_db_connection():
    """Retorna uma conexão ativa com o banco de dados sistema.db com acesso por nome de coluna."""
    conn = sqlite3.connect(DB_SISTEMA, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

# --- AUTENTICAÇÃO DO PAINEL ADMIN ---
ADMIN_USER = os.getenv('ADMIN_USER')
ADMIN_PASS = os.getenv('ADMIN_PASS')

# --- CONFIGURAÇÕES DE EMAIL (SMTP) ---
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USER = os.getenv('SMTP_USER')
SMTP_PASS = os.getenv('SMTP_PASS')

# Lê o dicionário de servidores diretamente do arquivo .env
servidores_json = os.getenv('SERVIDORES_CONFIG')
if servidores_json:
    SERVIDORES = json.loads(servidores_json)
else:
    SERVIDORES = {}

CAMINHO_DATABASES_CONF = '/opt/firebird/databases.conf'
DIRETORIO_BASE = '/opt/infobrasil'

def diretorio_base_servidor(servidor):
    return posixpath.join(DIRETORIO_BASE, '3.0') if servidor == 'DB05' else DIRETORIO_BASE

TIMEOUT_FIND_ORFAOS = int(os.getenv('TIMEOUT_FIND_ORFAOS', '30'))
TIMEOUT_CONEXAO_SSH = int(os.getenv('TIMEOUT_CONEXAO_SSH', '10'))

def _sanitizar_texto_log(texto, credenciais=None):
    """Remove senhas e segredos conhecidos antes de registrar no log."""
    if not texto:
        return ''
    resultado = str(texto)
    if credenciais:
        for cred in credenciais:
            if cred and isinstance(cred, str) and len(cred) >= 3 and cred in resultado:
                resultado = resultado.replace(cred, '***')
    return resultado

DATA_DIR = os.path.abspath(os.getenv('DATA_DIR', '.'))
os.makedirs(DATA_DIR, exist_ok=True)
DB_HISTORICO = os.path.join(DATA_DIR, 'historico_bancos.db')
DB_SISTEMA = os.path.join(DATA_DIR, 'sistema.db')
URL_STATUS_BACKUPS_FTP = os.getenv('URL_STATUS_BACKUPS_FTP', 'http://192.168.254.19/cgi-bin/bkp-status.web')
   

# --- BANCO DE DADOS LOCAL: USUÁRIOS, CNAME E HISTÓRICO ---
def init_db_sistema():
    """Inicializa as tabelas de suporte do sistema (Histórico, Usuários e CNAMEs customizados)."""
    conn = get_db_connection()
    conn.execute("PRAGMA journal_mode = WAL")
    cursor = conn.cursor()
    
    # Tabela de Histórico
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS historico_servidores (
            data TEXT, servidor TEXT, total_bytes REAL, qtd_bancos INTEGER, PRIMARY KEY (data, servidor)
        )
    ''')
    
    # Tabela de Usuários com controle de Status (Ativo/Inativo) e Criptografia
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL,
            eh_master INTEGER DEFAULT 0,
            ativo INTEGER DEFAULT 1
        )
    ''')

    # Garante migração para bancos já existentes que não tinham essa coluna
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN token_ativacao TEXT;")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN token_expira TEXT;")
    except sqlite3.OperationalError:
        pass

    # Colunas de nível de acesso por página (1 = tem acesso). O Master sempre tem acesso a tudo, independente destas colunas.
    for coluna in PERMISSOES_DISPONIVEIS:
        if not coluna.isidentifier():
            continue
        try:
            cursor.execute(f"ALTER TABLE usuarios ADD COLUMN {coluna} INTEGER DEFAULT 0;")
        except sqlite3.OperationalError:
            pass

    # Linhas ignoradas (duplicadas) na última importação de lojas via .xlsx, para revisão manual do usuário
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS import_duplicatas_pendentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            linha INTEGER, alias TEXT, loj_codigo TEXT, loj_fantasia TEXT, loj_nome TEXT, loj_cnpj TEXT, motivo TEXT
        )
    ''')
    for coluna_dup in ('cod_info', 'cli_situacao'):
        if not coluna_dup.isidentifier():
            continue
        try:
            cursor.execute(f"ALTER TABLE import_duplicatas_pendentes ADD COLUMN {coluna_dup} TEXT;")
        except sqlite3.OperationalError:
            pass
    # Tabela para personalizar CNAMEs editados por clientes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cnames_custom (
            alias TEXT PRIMARY KEY,
            cname_customizado TEXT NOT NULL
        )
    ''')
    
    # Tabela antiga (1 registro por alias) - mantida apenas para migração automática de dados já cadastrados
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clientes_info (
            alias TEXT PRIMARY KEY,
            cod_info TEXT,
            loj_cod TEXT,
            cnpj TEXT,
            razao_social TEXT,
            fantasia TEXT
        )
    ''')

    # Tabela atual: cada alias pode ter várias lojas (CodInfo, LojCód., Fantasia, Razão/Nome, CNPJ, Situação)
    # Alimentada manualmente pelo card de detalhes OU em lote pela importação de planilha .xlsx
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clientes_lojas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL,
            loj_codigo TEXT,
            loj_fantasia TEXT,
            loj_nome TEXT,
            loj_cnpj TEXT,
            cod_info TEXT,
            cli_situacao TEXT,
            ignorar_incompleto INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_clientes_lojas_alias ON clientes_lojas (alias)')
    try:
        cursor.execute("ALTER TABLE clientes_lojas ADD COLUMN ignorar_incompleto INTEGER DEFAULT 0;")
    except sqlite3.OperationalError:
        pass

    # Tabela de Histórico e Marcadores de Alertas de Backups por E-mail
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS backups_alertas_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_envio TEXT NOT NULL,
            data_referencia TEXT NOT NULL,
            destinatario TEXT NOT NULL,
            assunto TEXT NOT NULL,
            status_envio TEXT NOT NULL,
            total_ok INTEGER DEFAULT 0,
            total_atrasado INTEGER DEFAULT 0,
            total_critico INTEGER DEFAULT 0,
            clientes_criticos TEXT,
            detalhes TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_backups_alertas_data_ref ON backups_alertas_emails (data_referencia)')

    # Migração automática (roda uma única vez): copia os registros da tabela antiga para a nova, como 1ª loja de cada alias
    cursor.execute("SELECT COUNT(*) as total FROM clientes_lojas")
    if cursor.fetchone()['total'] == 0:
        cursor.execute("SELECT * FROM clientes_info")
        antigos = cursor.fetchall()
        for c in antigos:
            cursor.execute('''
                INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, cod_info)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (c['alias'], c['loj_cod'], c['fantasia'], c['razao_social'], c['cnpj'], c['cod_info']))

    # Cadastra Usuário Master somente se ADMIN_MASTER_PASSWORD estiver no .env
    # (não cria mais senha padrão fraca "admin123").
    master_email = os.getenv('ADMIN_MASTER_EMAIL', '').strip().lower()
    senha_master_inicial = os.getenv('ADMIN_MASTER_PASSWORD', '').strip()
    if master_email and senha_master_inicial:
        cursor.execute("SELECT id FROM usuarios WHERE email = ?", (master_email,))
        if not cursor.fetchone():
            ok_senha, msg_senha = senha_atende_politica(senha_master_inicial)
            if not ok_senha:
                conn.close()
                raise RuntimeError(f'ADMIN_MASTER_PASSWORD inválida: {msg_senha}')
            senha_criptografada = generate_password_hash(senha_master_inicial)
            cursor.execute('''
                INSERT INTO usuarios (nome, email, senha_hash, eh_master, ativo)
                VALUES (?, ?, ?, 1, 1)
            ''', ("Administrador Master", master_email, senha_criptografada))
        
    conn.commit()
    conn.close()

init_db_sistema()

# --- RECURSO 2: TESTADOR DE CNAME (VERIFICA SE ESTÁ ATIVO) ---
def verificar_status_cname(host_cname):
    """Testa se o CNAME resolve via DNS e responde conexão."""
    try:
        # Extrai apenas o hostname (remove portas ou caminhos se houver)
        host_limpo = host_cname.split('/')[0].split(':')[0]
        socket.gethostbyname(host_limpo)
        return True # 🟢 Ativo
    except Exception:
        return False # 🔴 Inativo

def aplicar_cnames_customizados(lista_bancos):
    """Sobrepõe o cname_string padrão pelo valor editado manualmente (tabela cnames_custom), quando existir."""
    if not lista_bancos:
        return lista_bancos
    try:
        conn = get_db_connection()
        linhas = conn.execute("SELECT alias, cname_customizado FROM cnames_custom").fetchall()
        conn.close()
        customizados = {l['alias']: l['cname_customizado'] for l in linhas}
    except Exception:
        customizados = {}

    for banco in lista_bancos:
        alias = banco.get('alias')
        if alias in customizados:
            banco['cname_string'] = customizados[alias]
            banco['cname_custom'] = True
        else:
            banco['cname_custom'] = False
    return lista_bancos

def nome_para_assinatura(nome):
    """Remove a palavra Master do nome usado na assinatura de e-mail."""
    texto = (nome or '').strip()
    texto = re.sub(r'\s*\(\s*master\s*\)\s*', ' ', texto, flags=re.IGNORECASE)
    texto = re.sub(r'\bmaster\b', '', texto, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', texto).strip()

bancos_bp.add_app_template_filter(nome_para_assinatura, 'nome_pessoa')

CAMPOS_LOJA = ['loj_codigo', 'loj_fantasia', 'loj_nome', 'loj_cnpj', 'cod_info', 'cli_situacao']

def carregar_lojas_clientes():
    """Carrega todas as lojas cadastradas, agrupadas por alias. Um alias pode ter N lojas."""
    try:
        conn = get_db_connection()
        linhas = conn.execute("SELECT * FROM clientes_lojas ORDER BY loj_codigo").fetchall()
        conn.close()
        agrupado = {}
        for l in linhas:
            agrupado.setdefault(l['alias'], []).append(dict(l))
        return agrupado
    except Exception:
        return {}

def loja_incompleta(loja):
    """Retorna True se faltar CodInfo, Situação ou LojCód., a menos que a loja esteja marcada para ignorar."""
    if loja.get('ignorar_incompleto') in (1, '1', True):
        return False
    cod = (loja.get('cod_info') or '').strip()
    sit = (loja.get('cli_situacao') or '').strip()
    loj = (loja.get('loj_codigo') or '').strip()
    return not cod or not sit or not loj


def aplicar_lojas_clientes(lista_bancos, cache_lojas=None):
    """Anexa a lista de lojas (CodInfo/LojCód./Fantasia/Razão/CNPJ/Situação) a cada banco da lista e sinaliza pendências."""
    if not lista_bancos:
        return lista_bancos
    lojas_por_alias = cache_lojas if cache_lojas is not None else carregar_lojas_clientes()

    for banco in lista_bancos:
        lojas = lojas_por_alias.get(banco.get('alias'), [])
        incompletas = 0
        for l in lojas:
            is_inc = loja_incompleta(l)
            l['incompleta'] = is_inc
            if is_inc:
                incompletas += 1
        banco['lojas'] = lojas
        banco['tem_lojas_incompletas'] = (incompletas > 0)
        banco['total_lojas_incompletas'] = incompletas
    return lista_bancos


def marcar_lojas_sem_cnpj_como_ignoradas():
    """Marca como ignoradas (ignorar_incompleto = 1) todas as lojas sem CNPJ cadastrado.
    Retorna a quantidade de lojas atualizadas.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE clientes_lojas
        SET ignorar_incompleto = 1
        WHERE (loj_cnpj IS NULL OR TRIM(loj_cnpj) = '' OR TRIM(loj_cnpj) = '-')
          AND (ignorar_incompleto IS NULL OR ignorar_incompleto = 0)
    """)
    total_afetadas = cursor.rowcount
    conn.commit()
    conn.close()
    return total_afetadas

# --- RECURSO 3: DISPARO DE EMAIL SOLICITANDO CNAME ---
def enviar_solicitacao_cname(nome_fantasia, alias, usuario_solicitante):
    """Envia um e-mail para a empresa parceira solicitando a criação de um novo CNAME."""
    destinatario = "atendimento@nossatelecom.com.br"
    cc = "comercial@infobrasilsistemas.com.br"
    
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = destinatario
    msg['Cc'] = cc
    msg['Subject'] = f"Novo CNAME - {nome_fantasia}"

    sugestao = f"db{alias}.iprojectti.com.br" if alias else "db<alias>.iprojectti.com.br"

    corpo = (
        f"Boa tarde.\n\n"
        f"Solicito a criação de um CNAME para a empresa {nome_fantasia}\n"
        f"Sugestão: {sugestao}\n\n\n"
        f"__\n"
        f"{nome_para_assinatura(usuario_solicitante)}\n"
        f"Infobrasil Sistemas"
    )
    msg.attach(MIMEText(corpo, 'plain'))
    
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        recipients = [destinatario] + [cc]
        server.sendmail(SMTP_USER, recipients, msg.as_string())
        server.quit()
        return True, "E-mail de solicitação enviado com sucesso!"
    except Exception as e:
        return False, f"Falha ao enviar e-mail: {str(e)}"

@bancos_bp.route('/admin/solicitar-cname', methods=['POST'])
def solicitar_cname():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not tem_permissao('perm_gestao_bancos'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para esta ação."}), 403

    dados = request.get_json(silent=True) or {}
    fantasia = (dados.get('fantasia') or '').strip()
    alias = (dados.get('alias') or '').strip().lower()
    if not fantasia:
        return jsonify({"sucesso": False, "mensagem": "Informe o nome fantasia da empresa."}), 400

    usuario = g.get('usuario_atual')
    nome_assinatura = nome_para_assinatura(usuario['nome'] if usuario else session.get('user_nome', ''))
    ok, msg = enviar_solicitacao_cname(fantasia, alias, nome_assinatura)
    return jsonify({"sucesso": ok, "mensagem": msg})

@bancos_bp.route('/api/lojas/buscar-alias')
def api_buscar_alias_por_fantasia():
    """Tenta localizar o alias de um cliente a partir do nome fantasia digitado, usando os dados já cadastrados em Lojas."""
    if not session.get('logged_in'):
        return jsonify({"encontrado": False}), 401

    termo = request.args.get('fantasia', '').strip()
    if not termo or len(termo) < 3:
        return jsonify({"encontrado": False})

    conn = get_db_connection()
    linha = conn.execute(
        "SELECT alias, loj_fantasia FROM clientes_lojas WHERE loj_fantasia LIKE ? OR loj_nome LIKE ? LIMIT 1",
        (f"%{termo}%", f"%{termo}%")
    ).fetchone()
    conn.close()

    if linha:
        return jsonify({"encontrado": True, "alias": linha['alias']})
    return jsonify({"encontrado": False})

# --- ROTAS DE GESTÃO DE USUÁRIOS (SÓ O MASTER PODE ALTERAR) ---
@bancos_bp.route('/admin/usuarios/toggle_status', methods=['POST'])
def toggle_status_usuario():
    """Permite ao usuário Master inativar ou ativar acessos de qualquer outro cadastro."""
    if not session.get('eh_master'):
        return jsonify({'sucesso': False, 'erro': 'Acesso negado. Apenas o usuário Master pode realizar esta operação.'}), 403
        
    user_id = request.form.get('id')
    novo_status = request.form.get('ativo') # 1 para Ativar, 0 para Inativar
    
    with closing(get_db_connection()) as conn, conn:
        conn.execute("UPDATE usuarios SET ativo = ? WHERE id = ? AND eh_master = 0", (novo_status, user_id))
        conn.commit()
    
    return jsonify({'sucesso': True, 'mensagem': 'Status do usuário atualizado com sucesso!'})
    

# --- SISTEMA DE CACHE EM MEMÓRIA ---
CACHE_DADOS = {
    'bancos_por_servidor': {},
    'orfaos': {'dados': [], 'timestamp': 0},
    'metricas': {'dados': [], 'timestamp': 0},
    'backups_ftp': {'dados': None, 'timestamp': 0},
    'ttl_segundos': 600,
    'generation': 0,
}
CACHE_LOCK = threading.RLock()
METRICAS_LOCK = threading.Lock()
BACKUPS_LOCK = threading.Lock()
LOCKS_SERVIDORES = {}
CONFIG_LOCKS_SERVIDORES = {}
REFRESH_LOCK = threading.Lock()
ULTIMO_REFRESH = {}
REFRESH_COOLDOWN_SEGUNDOS = 15

def _lock_servidor(nome_servidor):
    """Retorna o lock single-flight do servidor sem criar dois locks em corrida."""
    with CACHE_LOCK:
        return LOCKS_SERVIDORES.setdefault(nome_servidor, threading.Lock())

def _lock_config_servidor(nome_servidor):
    with CACHE_LOCK:
        return CONFIG_LOCKS_SERVIDORES.setdefault(nome_servidor, threading.Lock())

def solicitar_refresh(recurso, limpar_cache=False):
    """Coalesce cliques/requisições repetidas de refresh público."""
    if request.args.get('atualizar') != '1':
        return False
    agora = time.monotonic()
    with REFRESH_LOCK:
        anterior = ULTIMO_REFRESH.get(recurso, 0)
        if agora - anterior < REFRESH_COOLDOWN_SEGUNDOS:
            return False
        ULTIMO_REFRESH[recurso] = agora
        if limpar_cache:
            limpar_cache_global()
        return True

def limpar_cache_global():
    """Força a invalidação do cache em memória."""
    with CACHE_LOCK:
        CACHE_DADOS['generation'] += 1
        CACHE_DADOS['bancos_por_servidor'].clear()
        CACHE_DADOS['orfaos'] = {'dados': [], 'timestamp': 0}
        CACHE_DADOS['metricas'] = {'dados': [], 'timestamp': 0}
        CACHE_DADOS['backups_ftp'] = {'dados': None, 'timestamp': 0}
        return CACHE_DADOS['generation']

# --- RECURSO: STATUS DE BACKUPS FTP (lê e interpreta a página de texto do servidor de backups) ---
def obter_status_backups_ftp(forcar_atualizacao=False):
    """Single-flight da consulta HTTP de backups."""
    with BACKUPS_LOCK:
        return _obter_status_backups_ftp(forcar_atualizacao)


def _obter_status_backups_ftp(forcar_atualizacao=False):
    """
    Busca http://.../cgi-bin/bkp-status.web (texto simples) e transforma em dados estruturados:
    cliente, status (OK / ATRASADO / SEM_ARQUIVO), horário do último backup e totais.
    Cacheado por alguns minutos, já que é uma chamada HTTP síncrona e a página muda pouco.
    """
    agora = time.time()
    with CACHE_LOCK:
        geracao_consulta = CACHE_DADOS['generation']
        cache = CACHE_DADOS['backups_ftp']
    if not forcar_atualizacao and cache['dados'] is not None and (agora - cache['timestamp'] < CACHE_DADOS['ttl_segundos']):
        return copy.deepcopy(cache['dados'])

    resultado = {
        "erro": None, "clientes": [], "total_ok": 0, "total_erro": 0,
        "total_atrasado": 0, "ultima_atualizacao": None
    }

    try:
        if not URL_STATUS_BACKUPS_FTP.startswith(('http://', 'https://')):
            raise ValueError('URL_STATUS_BACKUPS_FTP deve iniciar com http:// ou https://')
        req = urllib.request.Request(URL_STATUS_BACKUPS_FTP, headers={'User-Agent': 'InfoMonitorDBClientes'})
        with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310
            conteudo = resp.read()
            try:
                texto = conteudo.decode('utf-8')
            except UnicodeDecodeError:
                texto = conteudo.decode('latin-1')
    except Exception as e:
        LOGGER.warning('Falha ao consultar status remoto de backups: %s', e)
        resultado["erro"] = "Não foi possível consultar o status dos backups."
        timestamp_resultado = time.time()
        with CACHE_LOCK:
            if CACHE_DADOS['generation'] == geracao_consulta:
                CACHE_DADOS['backups_ftp'] = {'dados': copy.deepcopy(resultado), 'timestamp': timestamp_resultado}
        return copy.deepcopy(resultado)

    resultado = interpretar_status_backups(texto)
    timestamp_resultado = time.time()
    with CACHE_LOCK:
        if CACHE_DADOS['generation'] == geracao_consulta:
            CACHE_DADOS['backups_ftp'] = {'dados': copy.deepcopy(resultado), 'timestamp': timestamp_resultado}
    return copy.deepcopy(resultado)

# --- BANCO DE DADOS LOCAL PARA HISTÓRICO ---
def init_db_historico():
    conn = sqlite3.connect(DB_HISTORICO, timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS historico_servidores (
            data TEXT,
            servidor TEXT,
            total_bytes REAL,
            qtd_bancos INTEGER,
            PRIMARY KEY (data, servidor)
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_historico_servidor_data
        ON historico_servidores (servidor, data DESC)
    ''')
    conn.commit()
    conn.close()

init_db_historico()


def caminho_do_alias_no_conf(nome_servidor, alias):
    """Localiza o caminho do FDB no databases.conf do servidor para um alias já cadastrado."""
    alias_limpo = sanitizar_alias(alias)
    if not alias_limpo:
        return None
    leitura = ler_databases_conf_remoto_estruturado(nome_servidor)
    if not leitura['ok']:
        raise RuntimeError('Não foi possível ler databases.conf.')
    conteudo = leitura['conteudo']
    for linha in conteudo.splitlines():
        linha_limpa = linha.strip()
        if not linha_limpa or linha_limpa.startswith('#') or '=' not in linha_limpa:
            continue
        nome_alias, caminho = linha_limpa.split('=', 1)
        if sanitizar_alias(nome_alias) == alias_limpo:
            return caminho.strip()
    return None


# --- RECURSO 4: UPLOAD DE BANCO DE DADOS A QUALQUER MOMENTO ---
@bancos_bp.route('/admin/upload_banco', methods=['POST'])
def upload_banco_avulso():
    """Envia um FDB para um alias já cadastrado, somente se ainda não existir arquivo no destino."""
    if not session.get('logged_in'):
        return redirect('/admin/login')
    if not tem_permissao('perm_gestao_bancos'):
        return "Você não tem permissão para esta ação.", 403
    servidor_req = request.form.get('servidor')
    servidor = servidor_req if servidor_req in SERVIDORES else None
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return redirect(destino_redirect_seguro(url_for('bancos.admin_painel', servidor=servidor, erro="Senha de confirmação incorreta. Ação cancelada.")))

    alias = sanitizar_alias(request.form.get('alias', ''))
    arquivo = request.files.get('arquivo_fdb')

    if not servidor or not alias or not arquivo or not arquivo.filename:
        return redirect(destino_redirect_seguro(url_for('bancos.admin_painel', servidor=servidor, erro="Informe o Alias e selecione o arquivo .FDB!")))

    info_srv = SERVIDORES.get(servidor)
    if not info_srv:
        return redirect(destino_redirect_seguro(url_for('bancos.admin_painel', servidor=servidor, erro="Servidor inválido.")))

    try:
        caminho_destino = validar_caminho_banco(caminho_do_alias_no_conf(servidor, alias), DIRETORIO_BASE)
        informado = (request.form.get('caminho') or '').strip()
        if informado and validar_caminho_banco(informado, DIRETORIO_BASE) != caminho_destino:
            raise ValueError('O destino deve corresponder ao alias cadastrado.')
    except (ValueError, RuntimeError):
        return redirect(url_for('bancos.admin_painel', servidor=servidor, erro='Caminho do alias inválido.'))

    pasta_destino = posixpath.dirname(caminho_destino)

    try:
        ssh = conectar_ssh(info_srv, timeout=10)
        sftp = ssh.open_sftp()
        try:
            sftp.stat(caminho_destino)
            sftp.close()
            ssh.close()
            return redirect(url_for(
                'bancos.admin_painel', servidor=servidor,
                erro=f'Já existe um banco de dados no alias "{alias}". Envio bloqueado.'
            ))
        except FileNotFoundError:
            pass

        ssh.exec_command(f'mkdir -p -- {shlex.quote(pasta_destino)} && chmod 775 -- {shlex.quote(pasta_destino)}')
        sftp.putfo(arquivo.stream, caminho_destino)
        sftp.chmod(caminho_destino, 0o775)
        sftp.close()
        ssh.close()
        limpar_cache_global()
        
        # Auditoria: upload de banco (Opção 3)
        registrar_acao_admin('upload_banco', {
            'servidor': servidor,
            'alias': alias,
            'arquivo': arquivo.filename,
        })
        
        return redirect(url_for('bancos.admin_painel', servidor=servidor, sucesso=f'Banco enviado com sucesso para o alias "{alias}".'))
    except Exception as e:
        return redirect(url_for('bancos.admin_painel', servidor=servidor, erro=f"Erro no envio: {str(e)}"))


def calcular_espaco_total_incluindo_sombra(nome_servidor, info_servidor, forcar_atualizacao=False):
    """
    Soma o espaço realmente ocupado no disco: bancos ATIVOS + bancos INATIVOS (ainda no disco,
    só removidos do databases.conf) + arquivos ÓRFÃOS (nem estão mais no databases.conf).
    Usado para o histórico/gráfico de crescimento, que precisa refletir o disco real e não só
    os alias ativos — senão o crescimento "some" quando um cliente é inativado mas o arquivo
    continua ocupando espaço físico no servidor ("armazenamento sombra").
    """
    try:
        # A mesma sessão SSH traz entradas ativas/inativas e órfãos.
        todos_ativos_e_inativos, total_conf, arquivos_orfaos = obter_dados_servidor_linux(
            nome_servidor, info_servidor, modo_busca_unificada=True,
            varrer_orfaos=True, forcar_atualizacao=forcar_atualizacao
        )
        if todos_ativos_e_inativos and 'erro' in todos_ativos_e_inativos[0]:
            return None
        total_orfaos = sum(a.get('tamanho_bytes', 0) for a in arquivos_orfaos)
        return total_conf + total_orfaos
    except Exception:
        return None

def salvar_historico_diario(metricas_servidores=None):
    """Grava o snapshot diário no SQLite, contando o espaço real do disco (ativos + inativos + órfãos)."""
    metricas_por_servidor = {
        metrica['servidor']: metrica for metrica in (metricas_servidores or [])
    }
    hoje = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()
    nomes_servidores = metricas_por_servidor.keys() if metricas_por_servidor else SERVIDORES.keys()
    for nome_servidor in nomes_servidores:
        m = metricas_por_servidor.get(nome_servidor)
        info_servidor = SERVIDORES.get(nome_servidor)
        total_bytes_real = None
        qtd_ativos = m['qtd_ativos'] if m else 0
        if info_servidor:
            try:
                bancos, total_conf, orfaos = obter_dados_servidor_linux(
                    nome_servidor, info_servidor, modo_busca_unificada=True,
                    varrer_orfaos=True, forcar_atualizacao=True
                )
                if not (bancos and 'erro' in bancos[0]):
                    total_bytes_real = total_conf + sum(a.get('tamanho_bytes', 0) for a in orfaos)
                    qtd_ativos = sum(
                        1 for banco in bancos
                        if banco.get('arquivo_existe') and not banco.get('eh_inativo')
                    )
            except Exception as e:
                LOGGER.warning('Falha ao coletar dados completos do servidor %s para histórico: %s', nome_servidor, e)
        # Sem uma varredura completa de órfãos, não grave um total sabidamente
        # parcial: preserve o último snapshot confiável em vez de subcontá-lo.
        if total_bytes_real is None:
            continue
        total_bytes = total_bytes_real
        cursor.execute('''
            INSERT OR REPLACE INTO historico_servidores (data, servidor, total_bytes, qtd_bancos)
            VALUES (?, ?, ?, ?)
        ''', (hoje, nome_servidor, total_bytes, qtd_ativos))
    conn.commit()
    conn.close()

# --- ROTINA DE ALERTA DIÁRIO DE BACKUPS FTP POR E-MAIL (09:00) ---
def obter_marcador_alerta_hoje(conn, analise=None):
    """
    Retorna o marcador de status do envio do alerta diário de backups para a data de hoje.
    Garante o cumprimento da regra de enviar no máximo 1 e-mail por dia e evita envios repetidos.
    """
    hoje_str = datetime.now(FUSO_LOCAL).strftime('%Y-%m-%d')
    hora_atual = datetime.now(FUSO_LOCAL).hour

    row = conn.execute('''
        SELECT * FROM backups_alertas_emails
        WHERE data_referencia = ? AND status_envio = 'ENVIADO'
        ORDER BY id DESC LIMIT 1
    ''', (hoje_str,)).fetchone()

    if row:
        horario = row['data_envio'].split(' ')[1][:5] if ' ' in row['data_envio'] else row['data_envio']
        return {
            'enviado_hoje': True,
            'agendado': False,
            'sem_pendencias': False,
            'data_envio': row['data_envio'],
            'destinatario': row['destinatario'],
            'total_critico': row['total_critico'],
            'clientes_criticos': row['clientes_criticos'] or '',
            'badge_tipo': 'sucesso',
            'badge_rotulo': '✅ Alerta enviado hoje',
            'texto_status': f"Alerta diário enviado hoje às {horario} para {row['destinatario']}.",
            'detalhes': f"{row['total_critico']} cliente(s) com atraso crítico notificados."
        }

    total_critico = analise.get('total_critico', 0) if analise else 0
    if total_critico > 0:
        horario_texto = "às 09:00" if hora_atual < 9 else "pendente de disparo (após 09h)"
        return {
            'enviado_hoje': False,
            'agendado': True,
            'sem_pendencias': False,
            'total_critico': total_critico,
            'clientes_criticos': ', '.join(c['alias'] for c in analise.get('clientes_criticos', [])),
            'badge_tipo': 'aviso',
            'badge_rotulo': '🕒 Agendado às 09h',
            'texto_status': f"Envio agendado para {horario_texto} ({total_critico} cliente(s) com ≥ 3 dias de atraso).",
            'detalhes': f"Destinatário configurado: {EMAIL_DESTINATARIO_PADRAO}."
        }

    return {
        'enviado_hoje': False,
        'agendado': False,
        'sem_pendencias': True,
        'total_critico': 0,
        'clientes_criticos': '',
        'badge_tipo': 'info',
        'badge_rotulo': 'ℹ️ Sem pendências',
        'texto_status': "Nenhum alerta necessário hoje (nenhum cliente com 3 ou mais dias de atraso).",
        'detalhes': "Disparo automático monitorado para às 09:00 caso ocorra atraso crítico."
    }


def obter_horario_agendado_alerta_backups(scheduler_instance=None):
    """Lê o horário real configurado no agendador para o alerta diário de backups."""
    try:
        s = scheduler_instance if scheduler_instance is not None else scheduler
        job = s.get_job('alerta_backups_diario') if s else None
        if job and hasattr(job, 'trigger') and hasattr(job.trigger, 'fields'):
            campos = {f.name: str(f) for f in job.trigger.fields}
            h = campos.get('hour', '9')
            m = campos.get('minute', '0')
            if m and m != '0':
                return f"{h}h{m.zfill(2)}"
            return f"{h}h"
    except Exception:
        pass
    return "9h"


def obter_historico_alertas_backups(conn, limite=100, agora_dt=None, limite_dias=30):
    """Retorna os registros mais recentes de envios e tentativas de alertas de backups com dias dispensados."""
    cursor = conn.execute('''
        SELECT id, data_envio, data_referencia, destinatario, assunto,
               status_envio, total_ok, total_atrasado, total_critico,
               clientes_criticos, detalhes
        FROM backups_alertas_emails
        ORDER BY data_envio DESC, id DESC
    ''')
    registros = [dict(linha) for linha in cursor.fetchall()]
    return montar_linhas_historico_alertas(registros, agora_dt=agora_dt, limite_dias=limite_dias)


def executar_rotina_alerta_backups(forcar=False, usuario_solicitante=None):
    """
    Executa a verificação e envio do alerta diário de backups em atraso.
    - Destinatário: suporte@infobrasilsistemas.com.br
    - Assunto: Alerta de backup em atraso
    - Condição: Envia e-mail quando houver algum cliente com 3 ou mais dias de atraso.
    - Limite: Apenas 1 e-mail por dia (exceto em disparo manual com forcar=True).
    - Horário de agendamento: 09:00.
    """
    agora_dt = datetime.now(FUSO_LOCAL)
    hoje_str = agora_dt.strftime('%Y-%m-%d')
    data_formatada = agora_dt.strftime('%d/%m/%Y %H:%M')

    conn = get_db_connection()
    try:
        if not forcar:
            enviado = conn.execute('''
                SELECT id, data_envio FROM backups_alertas_emails
                WHERE data_referencia = ? AND status_envio = 'ENVIADO'
                LIMIT 1
            ''', (hoje_str,)).fetchone()
            if enviado:
                LOGGER.info("Alerta de backups diário já enviado hoje (%s) às %s. Limite de 1 por dia respeitado.",
                            hoje_str, enviado['data_envio'])
                return False, f"Alerta diário já enviado hoje ({hoje_str}). Limite de 1 envio por dia mantido."

        status = obter_status_backups_ftp(forcar_atualizacao=True)
        if status.get('erro'):
            LOGGER.warning("Falha ao obter status de backups para rotina de alerta: %s", status['erro'])
            conn.execute('''
                INSERT INTO backups_alertas_emails
                (data_envio, data_referencia, destinatario, assunto, status_envio, detalhes)
                VALUES (?, ?, ?, ?, 'FALHA', ?)
            ''', (
                agora_dt.strftime('%Y-%m-%d %H:%M:%S'),
                hoje_str,
                EMAIL_DESTINATARIO_PADRAO,
                ASSUNTO_ALERTA_PADRAO,
                f"Falha na consulta remota: {status['erro']}"
            ))
            conn.commit()
            return False, f"Não foi possível consultar os backups: {status['erro']}"

        analise = analisar_status_para_alerta(status.get('clientes', []), data_referencia=agora_dt.date(), dias_corte=3)

        if not forcar and not analise['tem_alerta']:
            LOGGER.info("Rotina de alerta de backups: nenhum cliente com 3 ou mais dias de atraso. Nenhum e-mail enviado.")
            return False, "Nenhum cliente com 3 ou mais dias de atraso identificado."

        corpo_html, corpo_texto = gerar_relatorio_email_backups(
            analise,
            ultima_atualizacao=status.get('ultima_atualizacao'),
            data_envio=data_formatada
        )

        criticos_desc = ', '.join(
            f"{c['alias']} ({c['dias_atraso']} dias)" if c['dias_atraso'] < 999 else f"{c['alias']} (sem arquivo)"
            for c in analise['clientes_criticos']
        ) if analise['clientes_criticos'] else 'Nenhum crítico (envio forçado)'

        try:
            enviar_email_alerta_backups(
                destinatario=EMAIL_DESTINATARIO_PADRAO,
                assunto=ASSUNTO_ALERTA_PADRAO,
                corpo_html=corpo_html,
                corpo_texto=corpo_texto,
                servidor_smtp=SMTP_SERVER or os.getenv('SMTP_SERVER', 'smtp.gmail.com'),
                porta_smtp=SMTP_PORT or int(os.getenv('SMTP_PORT', 587)),
                usuario_smtp=SMTP_USER or os.getenv('SMTP_USER'),
                senha_smtp=SMTP_PASS or os.getenv('SMTP_PASS')
            )
            status_envio = 'ENVIADO'
            detalhes = 'Disparo automático das 14h' if not usuario_solicitante else f'Disparo manual por {usuario_solicitante}'
            sucesso = True
            msg_retorno = "E-mail de alerta de backups enviado com sucesso!"
        except Exception as exc:
            LOGGER.exception("Falha ao enviar e-mail de alerta de backups: %s", exc)
            status_envio = 'FALHA'
            detalhes = f"Erro no envio SMTP: {str(exc)}"
            sucesso = False
            msg_retorno = f"Falha ao enviar e-mail: {str(exc)}"

        conn.execute('''
            INSERT INTO backups_alertas_emails
            (data_envio, data_referencia, destinatario, assunto, status_envio,
             total_ok, total_atrasado, total_critico, clientes_criticos, detalhes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            agora_dt.strftime('%Y-%m-%d %H:%M:%S'),
            hoje_str,
            EMAIL_DESTINATARIO_PADRAO,
            ASSUNTO_ALERTA_PADRAO,
            status_envio,
            analise['total_ok'],
            analise['total_atrasado'],
            analise['total_critico'],
            criticos_desc,
            detalhes
        ))
        conn.commit()
        return sucesso, msg_retorno
    finally:
        conn.close()


def rotina_diaria_sincronizacao_api():
    """Rotina diária que consulta a API InfoBrasil e atualiza CodInfo e Situação das lojas."""
    LOGGER.info("[API InfoBrasil] Iniciando rotina diária agendada de sincronização de lojas...")
    try:
        resultado = sincronizar_lojas_com_api(get_db_connection)
        if resultado.get('sucesso'):
            LOGGER.info("[API InfoBrasil] Sincronização diária concluída: %s loja(s) atualizada(s) de %s retornada(s) pela API.",
                        resultado.get('atualizados', 0), resultado.get('total_api', 0))
        else:
            LOGGER.warning("[API InfoBrasil] Falha na sincronização diária agendada: %s", resultado.get('mensagem'))
    except Exception as e:
        LOGGER.exception("[API InfoBrasil] Exceção durante sincronização diária agendada: %s", e)


# --- AGENDADORES FIXOS: 04:00 (HISTÓRICO), 05:00 (API INFOBRASIL) E 09:00 (ALERTA DE BACKUPS) ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(salvar_historico_diario, 'cron', hour=4, minute=0)
scheduler.add_job(
    rotina_diaria_sincronizacao_api,
    'cron',
    hour=5,
    minute=0,
    timezone='America/Fortaleza',
    id='sincronizacao_api_infobrasil',
    replace_existing=True,
    max_instances=1,
    coalesce=True
)
scheduler.add_job(
    executar_rotina_alerta_backups,
    'cron',
    hour=9,
    minute=0,
    timezone='America/Fortaleza',
    id='alerta_backups_diario',
    replace_existing=True,
    max_instances=1,
    coalesce=True
)
configurar_certificados(bancos_bp, get_db_connection, tem_permissao, scheduler)
scheduler.start()

def calcular_runway_disco(servidor, tamanho_gb_atual, capacidade_total_gb=100):
    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()
    
    data_30d_atras = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    cursor.execute('''
        SELECT data, total_bytes FROM historico_servidores 
        WHERE servidor = ? AND data >= ? ORDER BY data ASC
    ''', (servidor, data_30d_atras))
    
    registros = cursor.fetchall()
    conn.close()

    if len(registros) < 2:
        return {"crescimento_diario_mb": 0, "dias_restantes": "N/D (sem histórico)"}

    primeiro = registros[0]
    ultimo = registros[-1]
    
    d1 = datetime.strptime(primeiro[0], '%Y-%m-%d')
    d2 = datetime.strptime(ultimo[0], '%Y-%m-%d')
    dias_diff = (d2 - d1).days

    if dias_diff <= 0:
        return {"crescimento_diario_mb": 0, "dias_restantes": "Calculando..."}

    bytes_diff = ultimo[1] - primeiro[1]
    crescimento_diario_bytes = bytes_diff / dias_diff
    crescimento_diario_mb = round(crescimento_diario_bytes / (1024 ** 2), 2)

    # Usa o último total gravado no histórico (que já inclui ativos + inativos + órfãos) em vez do
    # parâmetro "tamanho_gb_atual" (que reflete só os alias ativos), para a projeção bater com o gráfico.
    tamanho_gb_real_atual = ultimo[1] / (1024 ** 3)

    espaco_livre_gb = (capacidade_total_gb * 0.9) - tamanho_gb_real_atual
    if espaco_livre_gb <= 0:
        return {"crescimento_diario_mb": crescimento_diario_mb, "dias_restantes": "⚠️ CRÍTICO (>=90%)"}

    if crescimento_diario_bytes > 0:
        espaco_livre_bytes = espaco_livre_gb * (1024 ** 3)
        dias_restantes = int(espaco_livre_bytes / crescimento_diario_bytes)
        data_estimada = (datetime.now() + timedelta(days=dias_restantes)).strftime('%d/%m/%Y')
        return {
            "crescimento_diario_mb": crescimento_diario_mb,
            "dias_restantes": f"~{dias_restantes} dias para 90%",
            "data_estimada": data_estimada if dias_restantes <= 3650 else None
        }
    else:
        return {"crescimento_diario_mb": 0, "dias_restantes": "Estável / Sem crescimento", "data_estimada": None}

# --- FUNÇÕES DE SUPORTE ---
def formatar_tamanho(tamanho_bytes):
    if not isinstance(tamanho_bytes, (int, float)):
        return "0 MB"
    if tamanho_bytes >= 1024 * 1024 * 1024:
        return f"{round(tamanho_bytes / (1024 ** 3), 2)} GB"
    return f"{round(tamanho_bytes / (1024 ** 2), 2)} MB"

def mascara_caminho(caminho):
    """Remove /opt e substitui por ../infobrasil/"""
    if caminho.startswith('/opt/infobrasil'):
        return caminho.replace('/opt/infobrasil', '../infobrasil')
    elif caminho.startswith('/opt'):
        return caminho.replace('/opt', '..')
    return caminho

def calcular_status_inatividade(timestamp_mod):
    if not timestamp_mod:
        return {'status': 'desconhecido', 'dias': 0, 'texto_dias': '-'}
    
    agora = time.time()
    dias_sem_uso = int((agora - timestamp_mod) / 86400)

    if dias_sem_uso >= 90:
        meses = dias_sem_uso // 30
        return {'status': 'critico', 'dias': dias_sem_uso, 'texto_dias': f"({meses} meses)"}
    elif dias_sem_uso >= 30:
        return {'status': 'atencao', 'dias': dias_sem_uso, 'texto_dias': f"({dias_sem_uso} dias)"}
    else:
        return {'status': 'normal', 'dias': dias_sem_uso, 'texto_dias': 'Ativo'}

def remover_acentos(texto):
    """Remove acentos e converte para minúsculas para busca insensível a acentuação."""
    if not texto:
        return ''
    return unicodedata.normalize('NFKD', str(texto)).encode('ascii', 'ignore').decode('utf-8').lower()


def _banco_bate_busca(banco, termo, lojas_por_alias):
    """Verifica se o termo de busca bate no alias OU em qualquer loja cadastrada (CodInfo/LojCód./Fantasia/Razão/CNPJ/Situação).
    Suporta busca por CNPJ com caracteres/pontuação (pesquisando somente os números), além de normalizar acentos.
    """
    if not termo:
        return True

    termo_str = str(termo).strip()
    if not termo_str:
        return True

    termo_norm = remover_acentos(termo_str)
    alias_norm = remover_acentos(banco.get('alias', ''))

    if termo_norm in alias_norm:
        return True

    # Extrai somente dígitos do termo de busca para comparação de CNPJ/CPF/documentos
    termo_digitos = re.sub(r'\D', '', termo_str)

    # Identifica se a busca pode ser por CNPJ/documento numérico:
    # 1. Termo composto apenas por dígitos e caracteres típicos de formatação (., /, -, espaços)
    # 2. Ou possui 6 ou mais dígitos mesmo contendo texto (ex.: 'CNPJ 09.524.393/0001-89')
    apenas_formato_doc = (re.sub(r'[\d\s./\-]', '', termo_str) == '')
    busca_doc_numerico = (
        (apenas_formato_doc and len(termo_digitos) >= 2) or
        (len(termo_digitos) >= 6)
    )

    lojas = lojas_por_alias.get(banco.get('alias'), [])
    for loja in lojas:
        # 1. Busca textual padrão em todos os campos da loja (com acentos normalizados)
        valores_texto = ' '.join(str(v) for v in loja.values() if v is not None)
        valores_norm = remover_acentos(valores_texto)
        if termo_norm in valores_norm:
            return True

        # 2. Busca por CNPJ considerando SOMENTE os números (ignora pontuação/caracteres especiais)
        if busca_doc_numerico:
            cnpj_loja = str(loja.get('loj_cnpj') or '')
            cnpj_digitos = re.sub(r'\D', '', cnpj_loja)
            if cnpj_digitos and termo_digitos in cnpj_digitos:
                return True

    return False

def obter_dados_servidor_linux(nome_servidor, info_servidor, busca=None, buscar_inativos=False, modo_busca_unificada=False, varrer_orfaos=False, forcar_atualizacao=False):
    """Executa no máximo uma coleta SSH simultânea por servidor."""
    if not forcar_atualizacao:
        resultado_cache = _obter_resultado_cache_servidor(
            nome_servidor, busca, buscar_inativos, modo_busca_unificada, varrer_orfaos
        )
        if resultado_cache is not None:
            return resultado_cache
    with _lock_servidor(nome_servidor):
        return _obter_dados_servidor_linux(
            nome_servidor, info_servidor, busca, buscar_inativos,
            modo_busca_unificada, varrer_orfaos, forcar_atualizacao
        )


def _obter_resultado_cache_servidor(nome_servidor, busca, buscar_inativos,
                                    modo_busca_unificada, varrer_orfaos):
    """Seleciona referências sob lock e copia somente os objetos devolvidos."""
    agora = time.time()
    lojas_por_alias = carregar_lojas_clientes() if busca else {}
    with CACHE_LOCK:
        item = CACHE_DADOS['bancos_por_servidor'].get(nome_servidor)
        ttl = CACHE_DADOS['ttl_segundos']
        if not item or agora - item.get('timestamp', 0) >= ttl:
            return None
        if varrer_orfaos and (
                not item.get('orfaos_coletados') or
                agora - item.get('orfaos_timestamp', 0) >= ttl):
            return None
        total_b = item['bytes']
        referencias_filtradas = []
        for banco in item['dados']:
            eh_inativo = banco.get('eh_inativo', False)
            if not modo_busca_unificada and not varrer_orfaos:
                if buscar_inativos and not eh_inativo:
                    continue
                if not buscar_inativos and eh_inativo:
                    continue
            if busca and not _banco_bate_busca(banco, busca, lojas_por_alias):
                continue
            referencias_filtradas.append(banco)
        lista_filtrada = copy.deepcopy(referencias_filtradas)
        orfaos = copy.deepcopy(item.get('orfaos', [])) if varrer_orfaos else []

    return lista_filtrada, total_b, orfaos


def _obter_dados_servidor_linux(nome_servidor, info_servidor, busca=None, buscar_inativos=False, modo_busca_unificada=False, varrer_orfaos=False, forcar_atualizacao=False):
    chave_cache = nome_servidor
    lojas_por_alias = carregar_lojas_clientes() if busca else {}

    with CACHE_LOCK:
        geracao_coleta = CACHE_DADOS['generation']

    if not forcar_atualizacao:
        resultado_cache = _obter_resultado_cache_servidor(
            chave_cache, busca, buscar_inativos, modo_busca_unificada, varrer_orfaos
        )
        if resultado_cache is not None:
            return resultado_cache

    lista_bancos = []
    arquivos_orfaos = []
    total_bytes = 0
    
    ssh = None
    try:
        timeout_ssh = int(os.getenv('TIMEOUT_CONEXAO_SSH', str(TIMEOUT_CONEXAO_SSH)))
        ssh = conectar_ssh(info_servidor, timeout=timeout_ssh)
        sftp = ssh.open_sftp()

        try:
            with sftp.open(CAMINHO_DATABASES_CONF, 'rb') as f:
                conteudo_bytes = f.read()
        except Exception as e:
            LOGGER.warning('Falha ao ler databases.conf em %s durante coleta: %s',
                           nome_servidor, e)
            sftp.close()
            ssh.close()
            return [{
                'servidor': nome_servidor,
                'porta': info_servidor['porta_fb'],
                'alias': 'ERRO_CONEXAO',
                'caminho': CAMINHO_DATABASES_CONF,
                'caminho_exibicao': mascara_caminho(CAMINHO_DATABASES_CONF),
                'tamanho_bytes': 0,
                'tamanho_str': "Erro de leitura",
                'timestamp': 0,
                'data_str': "-",
                'data_criacao': "-",
                'origem_data': "-",
                'status_inatividade': {'status': 'desconhecido', 'dias': 0, 'texto_dias': '-'},
                'arquivo_existe': False,
                'eh_inativo': False,
                'erro': 'Não foi possível ler a configuração remota.'
            }], 0, []
            
        try:
            conteudo_texto = conteudo_bytes.decode('utf-8')
        except UnicodeDecodeError:
            conteudo_texto = conteudo_bytes.decode('latin-1', errors='ignore')
            
        linhas = conteudo_texto.splitlines()
        caminhos_oficiais = set()
        ultima_data_criacao = "-"

        for linha in linhas:
            linha_limpa = linha.strip()
            
            # Aceita qualquer caractere (ex: # Data: DD/MM/AAAA) até achar a data
            match_data = re.search(r'#.*?(\d{2}/\d{2}/\d{4})', linha_limpa)
            if match_data:
                ultima_data_criacao = match_data.group(1)

            if not linha_limpa or '=' not in linha_limpa:
                continue

            eh_inativo = linha_limpa.startswith('#')
            linha_processar = linha_limpa.lstrip('#').strip()
            partes = linha_processar.split('=', 1)
            alias = partes[0].strip()
            caminho_fdb = partes[1].strip()

            data_criacao_atual = ultima_data_criacao
            ultima_data_criacao = "-"
            origem_data = "conf" if data_criacao_atual != "-" else "pasta"

            if not caminho_fdb.startswith(DIRETORIO_BASE):
                continue

            caminhos_oficiais.add(caminho_fdb)
            cname_string = f"db{alias}.iprojectti.com.br/{info_servidor['porta_fb']}:{alias}"

            # Fallback para a data de modificação da pasta pai no disco
            if data_criacao_atual == "-":
                try:
                    pasta_destino = os.path.dirname(caminho_fdb)
                    stat_pasta = sftp.stat(pasta_destino)
                    data_criacao_atual = datetime.fromtimestamp(stat_pasta.st_mtime).strftime('%d/%m/%Y')
                    origem_data = "pasta"
                except Exception:
                    data_criacao_atual = "-"
                    origem_data = "-"

            try:
                stat_info = sftp.stat(caminho_fdb)
                tamanho_bytes = stat_info.st_size
                total_bytes += tamanho_bytes
                
                timestamp_mod = stat_info.st_mtime
                data_mod_str = datetime.fromtimestamp(timestamp_mod).strftime('%d/%m/%Y %H:%M:%S')
                status_info = calcular_status_inatividade(timestamp_mod)

                lista_bancos.append({
                    'servidor': nome_servidor,
                    'porta': info_servidor['porta_fb'],
                    'alias': alias,
                    'caminho': caminho_fdb,
                    'caminho_exibicao': mascara_caminho(caminho_fdb),
                    'cname_string': cname_string,
                    'tamanho_bytes': tamanho_bytes,
                    'tamanho_str': formatar_tamanho(tamanho_bytes),
                    'timestamp': timestamp_mod,
                    'data_str': data_mod_str,
                    'data_criacao': data_criacao_atual,
                    'origem_data': origem_data,
                    'status_inatividade': status_info,
                    'arquivo_existe': True,
                    'eh_inativo': eh_inativo
                })
            except FileNotFoundError:
                lista_bancos.append({
                    'servidor': nome_servidor,
                    'porta': info_servidor['porta_fb'],
                    'alias': alias,
                    'caminho': caminho_fdb,
                    'caminho_exibicao': mascara_caminho(caminho_fdb),
                    'cname_string': cname_string,
                    'tamanho_bytes': 0,
                    'tamanho_str': "Arquivo Removido" if eh_inativo else "Não encontrado",
                    'timestamp': 0,
                    'data_str': "FDB não existe" if eh_inativo else "-",
                    'data_criacao': data_criacao_atual,
                    'origem_data': origem_data,
                    'status_inatividade': {'status': 'desconhecido', 'dias': 0, 'texto_dias': '-'},
                    'arquivo_existe': False,
                    'eh_inativo': eh_inativo
                })
            except Exception as exc:
                if varrer_orfaos or modo_busca_unificada:
                    raise RuntimeError(
                        f"Não foi possível medir banco configurado: {mascara_caminho(caminho_fdb)}"
                    ) from exc

        if varrer_orfaos:
            diretorio_base = diretorio_base_servidor(nome_servidor)
            cmd_find = f"find {shlex.quote(diretorio_base)} -type f \\( -name '*.fdb' -o -name '*.fbk' -o -name '*.bak' \\)"
            timeout_find = int(os.getenv('TIMEOUT_FIND_ORFAOS', str(TIMEOUT_FIND_ORFAOS)))
            try:
                stdin, stdout, stderr = ssh.exec_command(cmd_find, timeout=timeout_find)
                saida_bytes = stdout.read()
                erro_bytes = stderr.read()
                status_saida = stdout.channel.recv_exit_status()
            except (socket.timeout, TimeoutError) as exc:
                raise TimeoutError(
                    f"Tempo limite ({timeout_find}s) excedido ao varrer arquivos órfãos com find em {nome_servidor}"
                ) from exc
            if status_saida != 0:
                detalhe = erro_bytes.decode('utf-8', errors='replace').strip()
                raise RuntimeError(f"Falha ao varrer arquivos órfãos (status {status_saida}): {detalhe}")
            try:
                arquivos_no_disco = saida_bytes.decode('utf-8').splitlines()
            except UnicodeDecodeError as exc:
                raise RuntimeError("Resposta inválida ao varrer arquivos órfãos") from exc

            for arq in arquivos_no_disco:
                arq_limpo = arq.strip()
                if arq_limpo and arq_limpo not in caminhos_oficiais:
                    try:
                        st = sftp.stat(arq_limpo)
                    except Exception as exc:
                        raise RuntimeError(f"Não foi possível medir arquivo órfão: {mascara_caminho(arq_limpo)}") from exc
                    arquivos_orfaos.append({
                        'servidor': nome_servidor,
                        'caminho': arq_limpo,
                        'caminho_exibicao': mascara_caminho(arq_limpo),
                        'tamanho_bytes': st.st_size,
                        'tamanho_str': formatar_tamanho(st.st_size),
                        'timestamp': st.st_mtime,
                        'data_str': datetime.fromtimestamp(st.st_mtime).strftime('%d/%m/%Y %H:%M:%S')
                    })

        sftp.close()
        ssh.close()
        timestamp_cache = time.time()

        novo_cache = {
            'dados': copy.deepcopy(lista_bancos),
            'bytes': total_bytes,
            'orfaos': copy.deepcopy(arquivos_orfaos),
            'orfaos_coletados': varrer_orfaos,
            'orfaos_timestamp': timestamp_cache if varrer_orfaos else 0,
            'timestamp': timestamp_cache
        }
        with CACHE_LOCK:
            # Uma coleta iniciada antes de ?atualizar=1 nunca repovoa a nova geração.
            if CACHE_DADOS['generation'] == geracao_coleta:
                atual = CACHE_DADOS['bancos_por_servidor'].get(nome_servidor)
                # Uma coleta comum nunca rebaixa um cache da mesma geração que já
                # contém a varredura de órfãos, mesmo quando foi solicitada à força.
                if atual and atual.get('orfaos_coletados') and not varrer_orfaos:
                    novo_cache['orfaos'] = atual['orfaos']
                    novo_cache['orfaos_coletados'] = True
                    novo_cache['orfaos_timestamp'] = atual.get('orfaos_timestamp', 0)
                CACHE_DADOS['bancos_por_servidor'][nome_servidor] = novo_cache

    except Exception as e:
        senha = info_servidor.get('senha') if isinstance(info_servidor, dict) else None
        segredos = [s for s in (senha, os.getenv('SECRET_KEY')) if s and len(s) >= 3]
        curr = e
        while curr:
            if senha and isinstance(senha, str) and len(senha) >= 3 and hasattr(curr, 'args') and curr.args:
                try:
                    curr.args = tuple(
                        arg.replace(senha, '***') if isinstance(arg, str) else arg
                        for arg in curr.args
                    )
                except Exception:
                    pass
            curr = getattr(curr, '__cause__', None) or getattr(curr, '__context__', None)
        causa_detalhada = _sanitizar_texto_log(
            f"{type(e).__name__}: {e}" + (f" (causa: {e.__cause__})" if getattr(e, '__cause__', None) else ""),
            segredos
        )
        LOGGER.warning(
            'Falha SSH ao coletar dados de %s: %s',
            nome_servidor, causa_detalhada,
            exc_info=True
        )
        try:
            if ssh:
                ssh.close()
        except Exception as e_close:
            LOGGER.debug('Erro ao fechar conexao SSH com %s: %s', nome_servidor, e_close)
        return [{
            'servidor': nome_servidor,
            'porta': info_servidor['porta_fb'],
            'alias': 'ERRO_SSH',
            'caminho': info_servidor['ip'],
            'caminho_exibicao': info_servidor['ip'],
            'tamanho_bytes': 0,
            'tamanho_str': "Falha de Conexão",
            'timestamp': 0,
            'data_str': "-",
            'data_criacao': "-",
            'origem_data': "-",
            'status_inatividade': {'status': 'desconhecido', 'dias': 0, 'texto_dias': '-'},
            'arquivo_existe': False,
            'eh_inativo': False,
            'erro': "Falha na conexão ou coleta SSH."
        }], 0, []

    lista_filtrada = []
    for b in lista_bancos:
        if not modo_busca_unificada and not varrer_orfaos:
            if buscar_inativos and not b['eh_inativo']:
                continue
            if not buscar_inativos and b['eh_inativo']:
                continue
        if busca and not _banco_bate_busca(b, busca, lojas_por_alias):
            continue
        lista_filtrada.append(b)

    return lista_filtrada, total_bytes, arquivos_orfaos if varrer_orfaos else []

def buscar_em_todos_servidores(termo_busca=None, buscar_inativos=False, modo_busca_unificada=False, varrer_orfaos=False, forcar_atualizacao=False):
    todos_bancos = []
    todos_orfaos = []
    total_bytes_global = 0

    if not SERVIDORES:
        return todos_bancos, total_bytes_global, todos_orfaos

    with ThreadPoolExecutor(max_workers=min(8, len(SERVIDORES))) as executor:
        futures = [
            executor.submit(obter_dados_servidor_linux, nome, info, termo_busca, buscar_inativos, modo_busca_unificada, varrer_orfaos, forcar_atualizacao)
            for nome, info in SERVIDORES.items()
        ]
        for future in futures:
            bancos, bytes_srv, orfaos = future.result()
            todos_bancos.extend(bancos)
            todos_orfaos.extend(orfaos)
            total_bytes_global += bytes_srv

    return todos_bancos, total_bytes_global, todos_orfaos

def obter_metricas_servidores(salvar_db=True, forcar_atualizacao=False):
    """Single-flight para impedir recomputações concorrentes das mesmas métricas."""
    with METRICAS_LOCK:
        return _obter_metricas_servidores(salvar_db, forcar_atualizacao)


def _obter_metricas_servidores(salvar_db=True, forcar_atualizacao=False):
    agora = time.time()
    with CACHE_LOCK:
        geracao_coleta = CACHE_DADOS['generation']
        cache_metricas = CACHE_DADOS['metricas']
    if not forcar_atualizacao and cache_metricas['dados'] and (agora - cache_metricas['timestamp'] < CACHE_DADOS['ttl_segundos']):
        return copy.deepcopy(cache_metricas['dados'])

    metricas = []
    if not SERVIDORES:
        with CACHE_LOCK:
            if CACHE_DADOS['generation'] == geracao_coleta:
                CACHE_DADOS['metricas'] = {'dados': [], 'timestamp': agora}
        return metricas

    with ThreadPoolExecutor(max_workers=min(8, len(SERVIDORES))) as executor:
        futures = {
            executor.submit(obter_dados_servidor_linux, nome, info, buscar_inativos=False, forcar_atualizacao=forcar_atualizacao): nome
            for nome, info in SERVIDORES.items()
        }
        for future in futures:
            nome_srv = futures[future]
            erro_metrica = None
            try:
                bancos, total_bytes, _ = future.result()
                if bancos and 'erro' in bancos[0]:
                    erro_metrica = bancos[0]['erro']
                    qtd_ativos = None
                    qtd_3meses = None
                    tamanho_gb = None
                    media_mb = None
                    maior_banco_alias = "-"
                    maior_banco_tamanho = "-"
                else:
                    qtd_ativos = len(bancos)
                    qtd_3meses = sum(1 for b in bancos if b.get('status_inatividade', {}).get('status') == 'critico')
                    tamanho_gb = round(total_bytes / (1024 ** 3), 2)
                    
                    if qtd_ativos > 0:
                        media_mb = round((total_bytes / (1024 ** 2)) / qtd_ativos, 1)
                        banco_max = max(bancos, key=lambda x: x.get('tamanho_bytes', 0))
                        maior_banco_alias = banco_max.get('alias', '-')
                        maior_banco_tamanho = banco_max.get('tamanho_str', '-')
                    else:
                        media_mb = 0
                        maior_banco_alias = "-"
                        maior_banco_tamanho = "-"

                pct_disco = None if erro_metrica else min(round((tamanho_gb / 100) * 100, 1), 100)
                runway = ({"crescimento_diario_mb": None, "dias_restantes": "Indisponível"}
                          if erro_metrica else calcular_runway_disco(nome_srv, tamanho_gb))

            except Exception as exc:
                erro_metrica = f'Falha ao coletar métricas: {exc}'
                tamanho_gb = None
                pct_disco = None
                qtd_ativos = None
                qtd_3meses = None
                media_mb = None
                maior_banco_alias = "-"
                maior_banco_tamanho = "-"
                runway = {"crescimento_diario_mb": 0, "dias_restantes": "Erro"}

            metricas.append({
                'servidor': nome_srv,
                'tamanho_gb': tamanho_gb,
                'pct_disco': pct_disco,
                'qtd_ativos': qtd_ativos,
                'qtd_3meses': qtd_3meses,
                'media_mb': media_mb,
                'maior_banco_alias': maior_banco_alias,
                'maior_banco_tamanho': maior_banco_tamanho,
                'runway': runway,
                'erro': erro_metrica,
            })
    
    metricas.sort(key=lambda x: x['servidor'])
    
    with CACHE_LOCK:
        if CACHE_DADOS['generation'] == geracao_coleta:
            CACHE_DADOS['metricas'] = {
                'dados': copy.deepcopy(metricas),
                'timestamp': time.time()
            }

    if salvar_db:
        salvar_historico_diario(metricas)
    return copy.deepcopy(metricas)

def converter_para_data_iso(data_str):
    """Auxiliar para converter DD/MM/AAAA em YYYYMMDD para ordenação correta."""
    try:
        return datetime.strptime(data_str, "%d/%m/%Y").strftime("%Y%m%d")
    except Exception:
        return "00000000"

# --- OPERAÇÕES REMOTAS PARA GESTÃO DO DATABASES.CONF ---
def ler_databases_conf_remoto_estruturado(nome_servidor):
    """Lê a configuração sem confundir conteúdo válido com mensagem de erro."""
    info = SERVIDORES.get(nome_servidor)
    if not info:
        return {'ok': False, 'conteudo': None, 'erro': 'Servidor inválido.'}
    try:
        with closing(conectar_ssh(info, timeout=5)) as ssh, closing(ssh.open_sftp()) as sftp:
            with sftp.open(CAMINHO_DATABASES_CONF, 'rb') as arquivo:
                conteudo = arquivo.read()
        try:
            texto = conteudo.decode('utf-8')
        except UnicodeDecodeError:
            texto = conteudo.decode('latin-1')
        return {
            'ok': True, 'conteudo': texto, 'erro': None,
            'hash': hashlib.sha256(conteudo).hexdigest(),
        }
    except Exception as e:
        LOGGER.warning('Falha ao ler databases.conf em %s: %s', nome_servidor, e)
        return {'ok': False, 'conteudo': None,
                'erro': f'Não foi possível ler databases.conf em {nome_servidor}.'}


def ler_databases_conf_remoto(nome_servidor):
    """Compatibilidade para telas somente leitura; mutações usam o resultado estruturado."""
    resultado = ler_databases_conf_remoto_estruturado(nome_servidor)
    return resultado['conteudo'] if resultado['ok'] else f"# {resultado['erro']}"


def validar_conteudo_databases_conf(conteudo):
    if not isinstance(conteudo, str) or '\x00' in conteudo:
        return False, 'Conteúdo inválido.'
    # Não tente reinterpretar a gramática do Firebird: databases.conf aceita
    # blocos e diretivas que não são simples pares alias=caminho.
    inicio = conteudo.lstrip().lower()
    if (inicio.startswith('# erro ao ler databases.conf') or
            inicio.startswith('# não foi possível ler databases.conf')):
        return False, 'Conteúdo de erro não pode substituir databases.conf.'
    return True, None


def _salvar_databases_conf_direto_com_rollback(sftp, destino, novos_bytes, bytes_anteriores, hash_base):
    """Fallback quando o usuário pode editar o arquivo, mas não criar/renomear no diretório."""
    hash_novo = hashlib.sha256(novos_bytes).hexdigest()
    try:
        with sftp.open(destino, 'rb') as atual:
            bytes_antes_escrita = atual.read()
        hash_atual = hashlib.sha256(bytes_antes_escrita).hexdigest()
        if hmac.compare_digest(hash_atual, hash_novo):
            return True
        if not hmac.compare_digest(hash_atual, hash_base):
            LOGGER.warning('Conflito antes do fallback direto de databases.conf em %s', destino)
            return False

        with sftp.open(destino, 'wb') as arquivo:
            arquivo.write(novos_bytes)
            arquivo.flush()
        with sftp.open(destino, 'rb') as confirmado:
            if hmac.compare_digest(hashlib.sha256(confirmado.read()).hexdigest(), hash_novo):
                return True
        raise RuntimeError('A verificação da escrita direta divergiu.')
    except Exception as exc:
        LOGGER.warning('Falha na escrita direta de databases.conf em %s: %s', destino, exc)
        try:
            with sftp.open(destino, 'wb') as restaurado:
                restaurado.write(bytes_anteriores)
                restaurado.flush()
            with sftp.open(destino, 'rb') as confirmado:
                restauracao_ok = hmac.compare_digest(
                    hashlib.sha256(confirmado.read()).hexdigest(),
                    hashlib.sha256(bytes_anteriores).hexdigest(),
                )
            if not restauracao_ok:
                LOGGER.critical('Não foi possível confirmar a restauração de %s', destino)
        except Exception as restore_exc:
            LOGGER.critical('Falha ao restaurar databases.conf em %s: %s', destino, restore_exc)
        return False

def salvar_databases_conf_remoto(nome_servidor, novo_conteudo, hash_esperado=None):
    info = SERVIDORES.get(nome_servidor)
    valido, erro = validar_conteudo_databases_conf(novo_conteudo)
    if not info or not valido:
        return False
    with _lock_config_servidor(nome_servidor):
        temporario = None
        try:
            with closing(conectar_ssh(info, timeout=5)) as ssh, closing(ssh.open_sftp()) as sftp:
                # normalize resolve o alvo real quando databases.conf é um symlink.
                sftp.lstat(CAMINHO_DATABASES_CONF)
                destino_real = sftp.normalize(CAMINHO_DATABASES_CONF)
                metadados = sftp.stat(destino_real)
                diretorio = posixpath.dirname(destino_real)
                temporario = posixpath.join(
                    diretorio, f'.{posixpath.basename(destino_real)}.{secrets.token_hex(8)}.tmp'
                )
                with sftp.open(destino_real, 'rb') as atual:
                    bytes_atuais = atual.read()
                hash_base = hashlib.sha256(bytes_atuais).hexdigest()
                if hash_esperado is not None and not hmac.compare_digest(hash_base, hash_esperado):
                    LOGGER.warning('Conflito de edição de databases.conf em %s', nome_servidor)
                    return False

                conteudo_unix = novo_conteudo.replace('\r\n', '\n').replace('\r', '\n')
                novos_bytes = conteudo_unix.encode('utf-8')
                try:
                    with sftp.open(temporario, 'x') as arquivo:
                        arquivo.write(novos_bytes)
                        arquivo.flush()
                    # Falha fechada: o arquivo nunca é trocado sem preservar dono,
                    # grupo e permissões do alvo real.
                    sftp.chown(temporario, metadados.st_uid, metadados.st_gid)
                    sftp.chmod(temporario, stat.S_IMODE(metadados.st_mode))
                    with sftp.open(destino_real, 'rb') as atual:
                        hash_antes_swap = hashlib.sha256(atual.read()).hexdigest()
                    if not hmac.compare_digest(hash_antes_swap, hash_base):
                        LOGGER.warning('Conflito de edição concorrente em databases.conf de %s', nome_servidor)
                        return False
                    sftp.posix_rename(temporario, destino_real)
                    temporario = None
                except Exception as atomic_exc:
                    LOGGER.warning(
                        'Gravação atômica indisponível em %s; tentando escrita direta verificada: %s',
                        nome_servidor, atomic_exc,
                    )
                    if temporario:
                        try:
                            sftp.remove(temporario)
                        except Exception as exc:
                            LOGGER.debug('Erro ao remover temporário em %s: %s', nome_servidor, exc)
                        temporario = None
                    if not _salvar_databases_conf_direto_com_rollback(
                            sftp, destino_real, novos_bytes, bytes_atuais, hash_base):
                        return False
                finally:
                    if temporario:
                        try:
                            sftp.remove(temporario)
                        except Exception as exc:
                            LOGGER.warning('Falha ao remover temporário remoto em %s: %s',
                                           nome_servidor, exc)
            limpar_cache_global()
            return True
        except Exception as exc:
            LOGGER.warning('Falha ao salvar databases.conf em %s: %s', nome_servidor, exc)
            return False

def verificar_alias_existente_global(alias_busca):
    """Retorna (servidor_encontrado, erro); qualquer leitura incompleta falha fechada."""
    alias_limpo = sanitizar_alias(alias_busca)
    for srv in SERVIDORES:
        resultado = ler_databases_conf_remoto_estruturado(srv)
        if not resultado['ok']:
            return None, resultado['erro']
        conf = resultado['conteudo']
        for linha in conf.splitlines():
            l = linha.strip()
            if not l.startswith('#') and '=' in l:
                nome_alias = sanitizar_alias(l.split('=', 1)[0])
                if nome_alias == alias_limpo:
                    return srv, None
    return None, None
    
def sanitizar_alias(texto):
    if not texto:
        return ""
    # Remove acentos e caracteres especiais
    texto_nfkd = unicodedata.normalize('NFKD', texto)
    texto_sem_acento = "".join([c for c in texto_nfkd if not unicodedata.combining(c)])
    # Mantém apenas letras e números, convertendo para minúsculo
    return re.sub(r'[^a-z0-9]', '', texto_sem_acento.lower())

# --- TEMPLATE HTML PRINCIPAL ---
HTML_LAYOUT = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Servidores dos Bancos Hospedados</title>
    <!-- Font Awesome Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    {% if modo_historico %}
    <script src="/static/vendor/chart.js/chart.umd.min.js" defer></script>
    {% endif %}
    <style>
        :root {
            --cor-primaria: #2563eb;
            --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b;
            --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc;
            --cor-superficie: #ffffff;
            --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a;
            --cor-aviso: #d97706;
            --cor-perigo: #dc2626;
            --cor-roxo: #7c3aed;
            --cor-teal: #0891b2;
            --raio: 8px;
        }
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--cor-fundo); color: var(--cor-texto); margin: 0; padding: 0; }
        .topo-fixo { position: sticky; top: 0; z-index: 1000; background-color: var(--cor-fundo); padding: 15px 20px 10px 20px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.06); }
        .linha-cabecalho { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .topo-fixo h2 { margin: 0; color: var(--cor-texto); font-weight: 700; }
        .search-box { display: flex; gap: 6px; }
        .search-input { padding: 8px 12px; font-size: 0.95em; border: 1px solid var(--cor-borda); border-radius: var(--raio); width: 240px; outline: none; }
        .search-input:focus { border-color: var(--cor-primaria); }
        .btn-search { padding: 8px 16px; background-color: var(--cor-primaria); color: white; border: none; border-radius: var(--raio); cursor: pointer; font-weight: 600; }
        .btn-search:hover { background-color: var(--cor-primaria-escura); }
        .btn-clear { padding: 8px 12px; background-color: var(--cor-superficie); color: var(--cor-perigo); border: 1px solid var(--cor-borda); border-radius: var(--raio); text-decoration: none; font-size: 0.85em; font-weight: 600; display: flex; align-items: center; }

        /* Menu unificado: todos os botões seguem o mesmo estilo neutro por padrão, diferenciando por
           borda/texto colorido (sutil) em vez de fundo colorido "chapado" por botão. */
        .botoes { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
        .btn { padding: 8px 14px; background-color: var(--cor-superficie); color: var(--cor-texto); border: 1px solid var(--cor-borda); border-radius: var(--raio); cursor: pointer; font-weight: 600; text-decoration: none; font-size: 0.85em; transition: background-color .15s, border-color .15s; }
        .btn:hover { background-color: #f1f5f9; border-color: #cbd5e1; }
        .btn.ativo { background-color: var(--cor-primaria); color: white; border-color: var(--cor-primaria); }

        .header-info { display: flex; justify-content: space-between; align-items: center; background: var(--cor-superficie); padding: 10px 18px; border-radius: var(--raio); box-shadow: 0 1px 3px rgba(0,0,0,0.05); border: 1px solid var(--cor-borda); }
        .header-info h3 { margin: 0; font-size: 1.1em; }
        .resumo-boxes { display: flex; gap: 10px; align-items: center; }
        .total-box { background-color: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; padding: 6px 14px; border-radius: var(--raio); font-size: 0.9em; font-weight: 600; }
        .alert-btn { text-decoration: none; padding: 6px 12px; border-radius: var(--raio); font-size: 0.9em; font-weight: 600; display: inline-flex; align-items: center; }
        .alert-btn-atencao { background-color: #fffbeb; border: 1px solid #fde68a; color: #92400e; }
        .alert-btn-critico { background-color: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }

        .container { padding: 15px 20px 30px 20px; }
        .card { background: var(--cor-superficie); padding: 20px; border-radius: var(--raio); box-shadow: 0 1px 3px rgba(0,0,0,0.06); border: 1px solid var(--cor-borda); margin-bottom: 20px; }

        .top5-container { display: flex; gap: 10px; margin-bottom: 15px; background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: 12px 18px; border-radius: var(--raio); }
        .top5-title { font-weight: 600; color: var(--cor-texto); display: flex; align-items: center; gap: 6px; font-size: 0.95em; min-width: 130px; }
        .top5-items { display: flex; gap: 12px; flex-wrap: wrap; width: 100%; }
        .top5-item { background: var(--cor-fundo); border: 1px solid var(--cor-borda); border-radius: 6px; padding: 4px 10px; font-size: 0.85em; display: flex; gap: 6px; align-items: center; }
        .top5-item.clicavel-cname { cursor: pointer; }
        .top5-item.clicavel-cname:hover { background: #f3e8ff; }
        .top5-rank { background: var(--cor-primaria); color: white; border-radius: 50%; width: 18px; height: 18px; display: flex; align-items: center; justify-content: center; font-size: 0.75em; font-weight: 600; }

        table { width: 100%; border-collapse: collapse; }
        th, td { border: 1px solid var(--cor-borda); padding: 10px 12px; text-align: left; vertical-align: middle; }
        th { position: sticky; top: var(--topo-altura, 160px); background-color: #f8fafc; color: var(--cor-texto); font-weight: 600; z-index: 900; }
        .historico-scroll { max-height: 65vh; overflow: auto; border: 1px solid var(--cor-borda); border-radius: var(--raio); }
        .historico-scroll table { min-width: 640px; }
        .historico-scroll th { top: 0; z-index: 2; }
        th a { color: var(--cor-texto); text-decoration: none; display: block; width: 100%; }
        th a:hover { color: var(--cor-primaria); text-decoration: underline; }
        tr:nth-child(even) { background-color: #fafbfc; }
        tr:hover { background-color: #f1f5f9; }
        
        .badge-servidor { background-color: var(--cor-primaria); color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.85em; font-weight: 600; }
        .badge-tamanho { font-weight: 600; color: var(--cor-primaria); }
        .badge-inativo-tag { background-color: var(--cor-aviso); color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.75em; font-weight: 600; margin-right: 5px; }

        .icon-origem { font-size: 1.1rem; margin-right: 6px; vertical-align: middle; }
        .icon-folder { color: var(--cor-aviso); }
        .icon-config { color: var(--cor-primaria); }

        .status-atencao { background-color: #fffbeb; color: #92400e; border: 1px solid #fde68a; padding: 4px 8px; border-radius: 5px; font-weight: 600; }
        .status-critico { background-color: #fef2f2; color: #991b1b; border: 1px solid #fecaca; padding: 4px 8px; border-radius: 5px; font-weight: 600; }

        .btn-copy { background: var(--cor-superficie); border: 1px solid var(--cor-borda); color: var(--cor-primaria); padding: 5px 9px; border-radius: 6px; cursor: pointer; font-size: 0.8em; font-weight: 600; transition: all 0.15s; }
        .btn-copy:hover { background: var(--cor-primaria); color: white; border-color: var(--cor-primaria); }
        .resalva-cname { font-size: 0.75em; color: var(--cor-texto-suave); margin-top: 2px; display: block; }
        .alias-clicavel { cursor: pointer; }
        .alias-clicavel:hover { color: var(--cor-primaria); text-decoration: underline; }
        .linha-detalhes td { background: #f8fafc; border-top: none; padding: 14px 20px; }
        .painel-cliente-info .grid-cliente-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px 20px; }
        .painel-cliente-info label { font-weight: 600; color: var(--cor-texto-suave); font-size: 0.85em; margin-right: 4px; }
        .painel-cliente-info .valor-info { color: var(--cor-texto); }
        .painel-cliente-info input.form-control-sm { font-size: 0.85em; padding: 3px 6px; }
        .tabela-lojas { width: 100%; border-collapse: collapse; background: var(--cor-superficie); font-size: 0.85em; }
        .tabela-lojas th { text-align: left; color: var(--cor-texto-suave); font-weight: 600; padding: 6px 8px; border-bottom: 2px solid var(--cor-borda); }
        .tabela-lojas td { padding: 5px 8px; border-bottom: 1px solid #f1f5f9; }
        .sem-lojas { color: var(--cor-texto-suave); font-style: italic; margin: 8px 0; }
        .acoes-loja { white-space: nowrap; }
        .linha-loja input.form-control-sm { font-size: 0.85em; padding: 2px 5px; width: 100%; min-width: 90px; }
        .icone-alerta-incompleto { display: inline-block; margin-left: 5px; font-size: 0.85em; cursor: help; vertical-align: middle; line-height: 1; background: transparent !important; border: none !important; }
        .tag-ignorado { display: inline-block; font-size: 0.75em; padding: 1px 6px; border-radius: 4px; background: var(--cor-bg-hover, #f1f5f9); color: var(--cor-texto-suave, #64748b); border: 1px solid var(--cor-borda, #cbd5e1); }

        /* Separador visual entre grupos de botões do menu (unifica visualmente sem precisar de fundo colorido) */
        .separador-menu { width: 1px; align-self: stretch; background: var(--cor-borda); margin: 2px 4px; }

        /* Coluna de CNAME compacta: só o botão de copiar aparece, o caminho vira um balão (tooltip) */
        .cname-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .cname-texto { display: none; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css', v='public-20260925-4') }}">
<script src="{{ url_for('static', filename='ui.js', v='public-20260925-4') }}" defer></script>
</head>
<body>
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <div class="topo-fixo" id="painelTopo">
        <div class="linha-cabecalho">
            <h2>Servidores dos Bancos Hospedados</h2>

            <form action="" method="GET" class="search-box">
                <input type="text" name="busca" class="search-input" placeholder="Pesquisar cliente (ativos e inativos)..." value="{{ busca_termo }}">
                {% if filtro_status %}
                    <input type="hidden" name="filtro" value="{{ filtro_status }}">
                {% endif %}
                <button type="submit" class="btn-search">🔍 Buscar</button>
                {% if busca_termo or filtro_status %}
                    <a href="{% if modo_inativos %}/inativos{% elif modo_todos %}/todos{% elif modo_orfaos %}/orfaos{% elif modo_historico %}/historico{% else %}/servidor/{{ servidor_atual }}{% endif %}{% if filtro_status and busca_termo %}?filtro={{ filtro_status|urlencode }}{% endif %}" class="btn-clear" title="{% if busca_termo and filtro_status %}Limpar busca (manter filtro){% else %}Limpar busca e filtros{% endif %}">✖ Limpar</a>
                {% endif %}
            </form>
        </div>

        <nav class="navegacao-servidores" aria-label="Navegação dos servidores">
            <label class="seletor-servidor" for="servidor-navegacao">Visualização
                <select id="servidor-navegacao">
                    {% if modo_inativos or modo_orfaos or modo_historico %}<option value="" selected>Selecione um servidor</option>{% endif %}
                    <option value="/todos?ordem={{ ordem_atual|urlencode }}" {% if modo_todos %}selected{% endif %}>Todos os servidores</option>
                    {% for nome, info in servidores.items() %}
                    <option value="/servidor/{{ nome|urlencode }}?ordem={{ ordem_atual|urlencode }}" {% if nome == servidor_atual and not modo_todos and not modo_inativos and not modo_orfaos and not modo_historico %}selected{% endif %}>{{ info.rotulo }}</option>
                    {% endfor %}
                </select>
            </label>
            <details class="menu-acoes">
                <summary class="btn">Consultas</summary>
                <div class="menu-painel">
                    <a href="/inativos" {% if modo_inativos %}aria-current="page"{% endif %}>Alias inativados</a>
                    <a href="/orfaos" {% if modo_orfaos %}aria-current="page"{% endif %}>Arquivos órfãos no disco</a>
                    <a href="/historico" {% if modo_historico %}aria-current="page"{% endif %}>Histórico de disco</a>
                    <a href="/backups-ftp">Backups FTP</a>
                    <a href="/certificados">Certificados</a>
                    <a href="/cnpj">Consulta CNPJ</a>
                    <a href="/todos?filtro=sem_lojas" {% if filtro_status == 'sem_lojas' %}aria-current="page"{% endif %}>Clientes sem lojas cadastradas</a>
                    <a href="/todos?filtro=lojas_incompletas" {% if filtro_status == 'lojas_incompletas' %}aria-current="page"{% endif %}>Clientes com lojas incompletas</a>
                    <a href="/relatorio-clientes-lojas" target="_blank" title="Baixar relatório em Excel com resumo executivo e lista detalhada de clientes e lojas">📊 Relatório Clientes & Lojas (.xlsx)</a>
                </div>
            </details>
            {% if session.get('logged_in') %}
            <details class="menu-acoes">
                <summary class="btn">Gerenciar</summary>
                <div class="menu-painel">
                    {% if tem_permissao('perm_gestao_bancos') %}
                        <a href="/admin?servidor={{ servidor_atual|urlencode }}">Gestão de bancos</a>
                    {% else %}
                        <button type="button" onclick="return avisarSemPermissao('Gestão de Bancos')">Gestão de bancos</button>
                    {% endif %}
                    {% if tem_permissao('perm_servidores') %}
                        <a href="/admin/importar-lojas">Importar lojas</a>
                        <a href="/admin/atualizar-lojas-info">Atualizar CodInfo / Situação</a>
                        <button type="button" onclick="marcarSemCnpjIgnorados()">Ignorar lojas sem CNPJ</button>
                        <a href="/relatorio-clientes-lojas" target="_blank">📊 Relatório Clientes & Lojas (.xlsx)</a>
                    {% else %}
                        <button type="button" onclick="return avisarSemPermissao('Importação de Lojas')">Importar lojas</button>
                        <button type="button" onclick="return avisarSemPermissao('Atualização de Lojas')">Atualizar CodInfo / Situação</button>
                        <button type="button" onclick="return avisarSemPermissao('Editar Lojas')">Ignorar lojas sem CNPJ</button>
                        <a href="/relatorio-clientes-lojas" target="_blank">📊 Relatório Clientes & Lojas (.xlsx)</a>
                    {% endif %}
                    <a href="http://{{ request.host.split(':')[0] }}:8501" target="_blank" rel="noopener noreferrer">🎧 Transcritor Call Center</a>
                </div>
            </details>
            {% endif %}
            <a href="?atualizar=1{% if busca_termo %}&busca={{ busca_termo|urlencode }}{% endif %}{% if ordem_atual %}&ordem={{ ordem_atual|urlencode }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status|urlencode }}{% endif %}" class="btn" title="Atualizar dados do servidor">↻ Atualizar</a>
            <a href="/horarios" class="btn">🗓️ Horários</a>
            <a href="http://{{ request.host.split(':')[0] }}:8501" target="_blank" rel="noopener noreferrer" class="btn" title="Transcrição e Auditoria de Chamadas Call Center">🎧 Transcritor Call Center</a>
            <button id="theme-toggle" class="theme-toggle" aria-label="Alternar tema" title="Alternar tema claro/escuro">
                <svg class="moon-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
                <svg class="sun-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
            </button>
            {% if session.get('logged_in') %}
            <details class="menu-acoes menu-conta">
                <summary class="btn"><span class="nome-conta">{{ session.get('user_nome', '') }}</span>{% if session.get('eh_master') %}<span class="selo-master">Master</span>{% endif %}</summary>
                <div class="menu-painel">
                    {% if tem_permissao('perm_gestao_bancos') %}
                        <a href="/admin">Meu perfil</a>
                    {% else %}
                        <button type="button" onclick="return avisarSemPermissao('Gestão de Bancos')">Meu perfil</button>
                    {% endif %}
                    <hr>
                    <form action="/admin/logout" method="POST">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <button type="submit">Sair da conta</button>
                    </form>
                </div>
            </details>
            {% else %}
            <a href="/admin/login" class="btn menu-conta">Entrar</a>
            {% endif %}
        </nav>

        <div class="header-info">
            {% if modo_historico %}
                <h3>📈 Histórico Diário de Consumo de Disco (Gravado Diariamente às 04:00 AM)</h3>
            {% elif modo_orfaos %}
                <h3>⚠️ Arquivos no diretório <span class="text-danger">../infobrasil/*</span> Não Listados no databases.conf</h3>
            {% elif filtro_status == 'sem_lojas' %}
                <h3>Databases em: <span class="text-primary">{{ 'TODOS OS SERVIDORES' if modo_todos else servidor_atual }}</span> <span class="badge bg-warning text-dark" style="font-size:0.65em; vertical-align: middle;">📭 Sem lojas cadastradas</span> {% if busca_termo %}<span class="badge bg-success text-white ms-1" style="font-size:0.65em; vertical-align: middle;">🔍 "{{ busca_termo }}"</span>{% endif %} <a href="{% if modo_todos %}/todos{% else %}/servidor/{{ servidor_atual }}{% endif %}{% if busca_termo %}?busca={{ busca_termo }}{% endif %}" class="btn-clear" style="display:inline-flex; font-size:0.5em; vertical-align: middle; margin-left: 8px;">✖ Limpar Filtro</a></h3>
            {% elif filtro_status == 'lojas_incompletas' %}
                <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
                    <h3 style="margin:0;">Databases em: <span class="text-primary">{{ 'TODOS OS SERVIDORES' if modo_todos else servidor_atual }}</span> <span class="badge border" style="font-size:0.65em; vertical-align: middle; background: var(--cor-card); color: var(--cor-texto);">⚠️ Lojas com dados incompletos</span> {% if busca_termo %}<span class="badge bg-success text-white ms-1" style="font-size:0.65em; vertical-align: middle;">🔍 "{{ busca_termo }}"</span>{% endif %} <a href="{% if modo_todos %}/todos{% else %}/servidor/{{ servidor_atual }}{% endif %}{% if busca_termo %}?busca={{ busca_termo }}{% endif %}" class="btn-clear" style="display:inline-flex; font-size:0.5em; vertical-align: middle; margin-left: 8px;">✖ Limpar Filtro</a></h3>
                    <div style="display:inline-flex; gap:8px; align-items:center;">
                        {% if session.get('logged_in') and tem_permissao('perm_servidores') %}
                        <button type="button" onclick="marcarSemCnpjIgnorados()" class="btn btn-sm" style="font-size:0.8em; padding:5px 10px; border:1px solid var(--cor-borda); background:var(--cor-superficie); color:var(--cor-texto); cursor:pointer;" title="Marcar todas as lojas sem CNPJ como ignoradas">
                            🚫 Ignorar lojas sem CNPJ
                        </button>
                        {% endif %}
                        <a href="/relatorio-clientes-lojas" class="btn btn-sm" style="font-size:0.8em; padding:5px 10px; border:1px solid var(--cor-borda); background:var(--cor-superficie); color:var(--cor-texto); text-decoration:none;" title="Baixar relatório completo em Excel">
                            📊 Baixar Relatório (.xlsx)
                        </a>
                    </div>
                </div>
            {% elif busca_termo %}
                <h3>🔍 Busca por: <span class="text-success">"{{ busca_termo }}"</span> em todos os servidores</h3>
            {% elif modo_inativos %}
                <h3>🚫 Alias Inativados com <span class="text-warning">#</span> no databases.conf</h3>
            {% elif modo_todos %}
                <h3>Databases em: <span class="text-primary">TODOS OS SERVIDORES</span></h3>
            {% else %}
                <h3>Databases em: <span class="text-success">{{ servidor_atual }}</span></h3>
            {% endif %}

            <div class="resumo-boxes">
                {% if not modo_inativos and not modo_orfaos and not modo_historico %}
                    {% set base_url = '/todos' if modo_todos or busca_termo else '/servidor/' ~ servidor_atual %}
                    <a href="{{ base_url }}?filtro={% if filtro_status == 'atencao' %}{% else %}atencao{% endif %}&ordem={{ ordem_atual }}" class="alert-btn alert-btn-atencao">
                        ⏳ {{ total_atencao }} (> 30 dias)
                    </a>
                    <a href="{{ base_url }}?filtro={% if filtro_status == 'critico' %}{% else %}critico{% endif %}&ordem={{ ordem_atual }}" class="alert-btn alert-btn-critico">
                        ⚠️ {{ total_critico }} (> 3 meses)
                    </a>
                {% endif %}
                <div class="total-box">💾 Espaço Ocupado: {{ total_tamanho }}</div>
                {% if status_backups and not status_backups.erro %}
                    <a href="/backups-ftp" class="alert-btn {% if status_backups.total_erro > 0 %}alert-btn-critico{% elif status_backups.total_atrasado > 0 %}alert-btn-atencao{% else %}alert-btn-success{% endif %}" title="Clique para ver o detalhamento por cliente">
                        💽 Backups FTP: {{ status_backups.total_ok }} OK
                        {% if status_backups.total_atrasado %}, {{ status_backups.total_atrasado }} atrasado(s){% endif %}
                        {% if status_backups.total_erro %}, {{ status_backups.total_erro }} com erro{% endif %}
                    </a>
                {% elif status_backups and status_backups.erro %}
                    <a href="/backups-ftp" class="alert-btn alert-btn-critico" title="{{ status_backups.erro }}">💽 Backups FTP: indisponível</a>
                {% endif %}
            </div>
        </div>
    </div>

    <div class="container" id="conteudo-principal" role="main">

        <!-- TOP 5 MAIORES BANCOS GLOBAL -->
        {% if top5_global and not modo_orfaos and not modo_historico %}
        <div class="top5-container">
            <div class="top5-title">🏆 Top 5 Bancos:</div>
            <div class="top5-items">
                {% for b in top5_global %}
                <div class="top5-item clicavel-cname" title="{{ b.cname_string }}" data-cname="{{ b.cname_string }}" onclick="copiarCnameCliente(this)">
                    <span class="top5-rank">{{ loop.index }}</span>
                    <strong>{{ b.alias }}</strong>
                    <span class="text-muted">({{ b.servidor }})</span>
                    <span class="text-success font-bold">{{ b.tamanho_str }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        <!-- ÚLTIMOS 3 CLIENTES HOSPEDADOS -->
        {% if ultimos_hospedados_global and not modo_orfaos and not modo_historico %}
        <div class="top5-container" style="border-left: 4px solid var(--cor-roxo);">
            <div class="top5-title">🆕 Novos Hospedados:</div>
            <div class="top5-items">
                {% for b in ultimos_hospedados_global %}
                <div class="top5-item clicavel-cname" title="{{ b.cname_string }}" data-cname="{{ b.cname_string }}" onclick="copiarCnameCliente(this)">
                    <span class="top5-rank" style="background:var(--cor-roxo);">{{ loop.index }}</span>
                    <strong>{{ b.alias }}</strong>
                    <span class="text-muted">({{ b.servidor }})</span>
                    <span class="text-purple font-bold">{{ b.data_criacao }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        <!-- TABELA DE RESULTADOS -->
        <div class="card">
            {% if modo_historico %}
                <!-- FORMULÁRIO DE FILTROS -->
                <div class="card" style="margin-bottom: 20px; background: var(--cor-fundo); padding: 15px; border-radius: 8px; border: 1px solid var(--cor-borda);">
                    <form method="GET" action="/historico" class="d-flex flex-wrap gap-3 items-end">
                        <div>
                            <label class="form-label fw-bold text-sm">Servidores:</label>
                            <div class="d-flex gap-2 flex-wrap">
                                {% for nome in servidores.keys() %}
                                    <label class="cursor-pointer" style="font-size: 0.9em;">
                                        <input type="checkbox" name="servidores" value="{{ nome }}" {% if nome in servidores_selecionados %}checked{% endif %}>
                                        {{ nome }}
                                    </label>
                                {% endfor %}
                            </div>
                        </div>

                        <div>
                            <label class="form-label fw-bold text-sm">Período Inicial:</label>
                            <input type="date" name="data_inicio" value="{{ data_inicio }}" class="form-control" style="padding: 6px; border-radius: 4px; border: 1px solid var(--cor-borda);">
                        </div>

                        <div>
                            <label class="form-label fw-bold text-sm">Período Final:</label>
                            <input type="date" name="data_fim" value="{{ data_fim }}" class="form-control" style="padding: 6px; border-radius: 4px; border: 1px solid var(--cor-borda);">
                        </div>

                        <div class="d-flex gap-2">
                            <button type="submit" class="btn-search">Filtrar</button>
                            <a href="/historico" class="btn-clear">Limpar Filtros</a>
                        </div>
                    </form>
                </div>

                <!-- CARDS DE CRESCIMENTO -->
                {% if resumo_crescimento %}
                    <p class="text-muted small mb-2">📦 Os valores abaixo incluem bancos ativos, bancos inativados (ainda ocupando disco) e arquivos órfãos — ou seja, o espaço real ocupado no servidor, não só os alias ativos.</p>
                    <div class="d-flex gap-2 flex-wrap mb-4">
                        {% for item in resumo_crescimento %}
                            <div class="card" style="border-left: 4px solid {% if item.is_positivo %}var(--cor-perigo){% else %}var(--cor-sucesso){% endif %}; padding: 10px 15px; border-radius: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); min-width: 200px;">
                                <div class="text-muted text-xs">Crescimento <strong>{{ item.servidor }}</strong></div>
                                <div class="font-bold" style="font-size: 1.1em; color: {% if item.is_positivo %}var(--cor-perigo){% else %}var(--cor-sucesso){% endif %}; margin-top: 2px;">
                                    {% if item.is_positivo %}+{% else %}-{% endif %}{{ item.crescimento_str }}
                                </div>
                                <div class="text-muted" style="font-size: 0.72em; margin-top: 3px;">{{ item.data_inicial }} até {{ item.data_final }}</div>
                                {% if item.runway %}
                                <div style="font-size: 0.78em; margin-top: 6px; padding-top: 6px; border-top: 1px dashed var(--cor-borda);">
                                    <span class="text-muted">Projeção (90% disco):</span>
                                    <strong style="color: {% if 'CRÍTICO' in item.runway.dias_restantes %}var(--cor-perigo){% else %}var(--cor-aviso){% endif %};">{{ item.runway.dias_restantes }}</strong>
                                    {% if item.runway.data_estimada %}
                                        <div class="text-muted" style="font-size: 0.72em;">≈ {{ item.runway.data_estimada }}</div>
                                    {% endif %}
                                </div>
                                {% endif %}
                            </div>
                        {% endfor %}
                    </div>
                {% endif %}

                <!-- GRÁFICO HISTÓRICO DE CRESCIMENTO -->
                {% if dados_grafico and dados_grafico.labels %}
                    {% if grafico_amostrado %}
                    <p class="text-muted" role="status">
                        Gráfico amostrado: {{ datas_exibidas_grafico }} de {{ total_datas_grafico }} datas,
                        com seleção determinística que preserva a primeira e a última data.
                        Use o período para visualizar maior detalhe.
                    </p>
                    {% endif %}
                    <div class="card" style="padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); margin-bottom: 20px; border: 1px solid var(--cor-borda);">
                        <h4 style="margin-top: 0; color: var(--cor-texto);">📈 EVOLUÇÃO DE CONSUMO DE DISCO (GB)</h4>
                        <div style="height: 250px; width: 100%;">
                            <canvas id="chart-historico-linha" role="img" aria-label="Evolução do consumo de disco em gigabytes por servidor">Seu navegador não suporta gráficos em canvas.</canvas>
                        </div>
                    </div>
                    <div class="card" style="padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); margin-bottom: 20px; border: 1px solid var(--cor-borda);">
                        <h4 style="margin-top: 0; color: var(--cor-texto);">📊 COMPOSIÇÃO DO ESPAÇO POR SERVIDOR (GB)</h4>
                        <div style="height: 250px; width: 100%;">
                            <canvas id="chart-historico-area" role="img" aria-label="Composição empilhada do espaço ocupado em gigabytes por servidor">Seu navegador não suporta gráficos em canvas.</canvas>
                        </div>
                    </div>
                {% endif %}

                <h3>📊 Registros de Histórico Diário (`historico_bancos.db`)</h3>
                {% if total_registros_historico %}
                    <p class="text-muted" aria-live="polite">
                        Exibindo {{ dados_historico|length }} de {{ total_registros_historico }} registros.
                        {% if historico_tabela_limitado %}
                            Para consultar registros além dos {{ limite_tabela_historico }} mais recentes, selecione um período menor nos filtros.
                        {% endif %}
                    </p>
                {% endif %}
                {% if dados_historico|length == 0 %}
                    <div class="text-center text-muted" style="padding: 30px;">
                        Nenhum registro encontrado para os filtros selecionados.
                    </div>
                {% else %}
                    <div class="historico-scroll" role="region" aria-label="Registros do histórico diário" tabindex="0">
                    <table>
                        <thead>
                            <tr>
                                <th>Data Registro</th>
                                <th>Servidor</th>
                                <th>Total Ocupado</th>
                                <th>Qtd Bancos Ativos</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for h in dados_historico %}
                            <tr>
                                <td><strong>{{ h.data }}</strong></td>
                                <td><span class="badge-servidor">{{ h.servidor }}</span></td>
                                <td class="badge-tamanho">{{ h.tamanho_str }}</td>
                                <td>{{ h.qtd_bancos }} clientes</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    </div>
                {% endif %}

            {% elif modo_orfaos %}
                {% if erros_orfaos %}
                    <div role="alert" style="padding: 15px; margin-bottom: 15px; color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;">
                        <strong>Não foi possível concluir a varredura em {{ erros_orfaos|length }} servidor(es):</strong>
                        <ul style="margin-bottom: 0;">
                            {% for erro in erros_orfaos %}
                                <li><strong>{{ erro.servidor }}</strong>: {{ erro.mensagem }}</li>
                            {% endfor %}
                        </ul>
                    </div>
                {% endif %}
                {% if arquivos_orfaos|length == 0 %}
                    {% if not erros_orfaos %}
                    <div style="text-align: center; padding: 20px; color: #27ae60; font-weight: bold;">
                        ✅ Nenhum arquivo órfão/cópia manual encontrado nos diretórios!
                    </div>
                    {% endif %}
                {% else %}
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 40px;">#</th>
                                <th>
                                    <a href="?ordem=servidor">Servidor ⇳</a>
                                </th>
                                <th>Caminho do Arquivo</th>
                                <th>
                                    <a href="?ordem=tamanho">Tamanho do Arquivo ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=data">Última Modificação ⇳</a>
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for arq in arquivos_orfaos %}
                            <tr>
                                <td>{{ loop.index }}</td>
                                <td><span class="badge-servidor">{{ arq.servidor }}</span></td>
                                <td class="font-monospace">{{ arq.caminho_exibicao }}</td>
                                <td class="badge-tamanho">{{ arq.tamanho_str }}</td>
                                <td>{{ arq.data_str }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% endif %}

            {% else %}
                {% if bancos and 'erro' in bancos[0] %}
                    <div class="alert alert-danger" style="padding: 15px;"><strong>Erro:</strong> {{ bancos[0].erro }}</div>
                {% elif bancos|length == 0 %}
                    <div class="text-center text-muted" style="padding: 30px;">Nenhum registro encontrado.</div>
                {% else %}
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 40px;">#</th>
                                {% if busca_termo or modo_todos or modo_inativos %}
                                    <th>
                                        <a href="?ordem=servidor{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Servidor / Porta ⇳</a>
                                    </th>
                                {% endif %}
                                <th>
                                    <a href="?ordem=nome{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Cliente (Alias) ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=data_criacao{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Data Criação ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=tamanho{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Tamanho ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=data{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Última Modificação ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=cname{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Conexão (CNAME) ⇳</a>
                                </th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for banco in bancos %}
                            <tr>
                                <td>{{ loop.index }}</td>
                                {% if busca_termo or modo_todos or modo_inativos %}
                                    <td><span class="badge-servidor">{{ banco.servidor }} ({{ banco.porta }})</span></td>
                                {% endif %}
                                <td>
                                    {% if banco.eh_inativo %}<span class="badge-inativo-tag">INATIVO</span>{% endif %}
                                    <button type="button" class="alias-clicavel" data-alias="{{ banco.alias }}" onclick="toggleDetalhes(this)" aria-expanded="false" title="Clique para ver os detalhes do cliente">
                                        {{ banco.alias }} <span class="seta-detalhes">▸</span>
                                    </button>
                                    {% if not banco.lojas and not banco.eh_inativo %}
                                        <span class="badge bg-warning text-dark" style="font-size:0.7em;" title="Nenhuma loja cadastrada para este alias">📭 sem loja</span>
                                    {% elif banco.tem_lojas_incompletas and not banco.eh_inativo %}
                                        <span class="icone-alerta-incompleto" title="{{ banco.total_lojas_incompletas }} loja(s) com dados incompletos">⚠️</span>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if banco.origem_data == 'pasta' %}
                                        <i class="fa-solid fa-folder icon-origem icon-folder" title="Data obtida da Pasta pai no Disco"></i>
                                    {% elif banco.origem_data == 'conf' %}
                                        <i class="fa-solid fa-file-code icon-origem icon-config" title="Data obtida da Tag no databases.conf"></i>
                                    {% else %}
                                        <i class="fa-solid fa-file-lines icon-origem icon-unknown" title="Origem não identificada"></i>
                                    {% endif %}
                                    <span class="fw-semibold" style="color: #34495e;">{{ banco.data_criacao }}</span>
                                </td>
                                <td class="badge-tamanho">{{ banco.tamanho_str }}</td>
                                <td>
                                    {% if banco.status_inatividade.status == 'critico' %}
                                        <span class="status-critico">⚠️ {{ banco.data_str }} {{ banco.status_inatividade.texto_dias }}</span>
                                    {% elif banco.status_inatividade.status == 'atencao' %}
                                        <span class="status-atencao">⏳ {{ banco.data_str }} {{ banco.status_inatividade.texto_dias }}</span>
                                    {% else %}
                                        {{ banco.data_str }}
                                    {% endif %}
                                </td>
                                <td>
                                    {% if banco.arquivo_existe and not banco.eh_inativo %}
                                        <div class="cname-cell" data-host="{{ banco.cname_string }}" data-assinatura="{{ assinatura_cname(banco.cname_string) }}" data-alias="{{ banco.alias }}">
                                            <span class="cname-status testando" role="status" aria-label="Testando status do CNAME" title="Testando conectividade..."></span>
                                            <span class="cname-texto">{{ banco.cname_string }}</span>
                                            <button class="btn-copy" data-cname="{{ banco.cname_string }}" onclick="copiarCnameCliente(this)" title="{{ banco.cname_string }}{% if banco.cname_custom %} (editado manualmente){% endif %} — clique para copiar">
                                                📋 Copiar
                                            </button>
                                            {% if tem_permissao('perm_servidores') %}
                                                <button class="btn-icon btn-icon-warning" onclick="editarCname(this)" title="Editar string de conexão" aria-label="Editar string de conexão">
                                                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
                                                </button>
                                            {% endif %}
                                        </div>
                                    {% else %}
                                        -
                                    {% endif %}
                                </td>
                            </tr>
                            <tr class="linha-detalhes d-none" data-alias="{{ banco.alias }}">
                                <td colspan="{{ 7 if (busca_termo or modo_todos or modo_inativos) else 6 }}">
                                    <div class="painel-lojas" data-alias="{{ banco.alias }}">
                                        {% if banco.lojas %}
                                        <table class="tabela-lojas">
                                            <thead>
                                                <tr>
                                                    <th>CodInfo</th><th>LojCód.</th><th>Fantasia</th><th>Razão/Nome</th><th>CNPJ</th><th>Situação</th>
                                                    {% if tem_permissao('perm_servidores') %}
                                                    <th style="text-align: center; width: 85px;" title="Ignorar pendências desta loja (ex: sem CNPJ)">Ignorar?</th>
                                                    {% endif %}
                                                    {% if tem_permissao('perm_servidores') %}<th></th>{% endif %}
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {% for loja in banco.lojas %}
                                                <tr class="linha-loja" data-id="{{ loja.id }}" data-ignorar="{{ 1 if loja.ignorar_incompleto else 0 }}">
                                                    <td class="valor-loja" data-campo="cod_info">{{ loja.cod_info or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_codigo">{{ loja.loj_codigo or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_fantasia">{{ loja.loj_fantasia or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_nome">{{ loja.loj_nome or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_cnpj">{{ loja.loj_cnpj or '-' }}</td>
                                                    <td class="valor-loja" data-campo="cli_situacao">{{ loja.cli_situacao or '-' }}</td>
                                                    {% if tem_permissao('perm_servidores') %}
                                                    <td class="col-ignorar text-center">
                                                        {% if loja.ignorar_incompleto %}
                                                            <span class="tag-ignorado" title="Loja marcada para ignorar pendências de preenchimento">Ignorado</span>
                                                        {% else %}
                                                            <span style="color: var(--cor-texto-suave); opacity: 0.5;">-</span>
                                                        {% endif %}
                                                    </td>
                                                    {% endif %}
                                                    {% if tem_permissao('perm_servidores') %}
                                                    <td class="acoes-loja">
                                                        <button class="btn-icon btn-icon-warning" onclick="editarLoja(this)" aria-label="Editar loja"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg></button>
                                                        <button class="btn-icon btn-icon-danger" onclick="excluirLoja(this)" aria-label="Excluir loja"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>
                                                    </td>
                                                    {% endif %}
                                                </tr>
                                                {% endfor %}
                                            </tbody>
                                        </table>
                                        {% else %}
                                            <p class="sem-lojas">Nenhuma loja cadastrada para este alias ainda.</p>
                                        {% endif %}
                                        {% if tem_permissao('perm_servidores') %}
                                            <button class="btn btn-success btn-sm mt-2" onclick="adicionarLoja(this, this.closest('.painel-lojas').dataset.alias)">
                                                ➕ Adicionar Loja
                                            </button>
                                        {% endif %}
                                    </div>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% endif %}
            {% endif %}
        </div>
    </div>

    <script>
        // --- Expande/recolhe o card de detalhes do cliente (CodInfo, LojCód., CNPJ, Razão, Fantasia) ---
        function toggleDetalhes(elemento) {
            const trPrincipal = elemento.closest('tr');
            const linha = trPrincipal ? trPrincipal.nextElementSibling : null;
            const seta = elemento.querySelector('.seta-detalhes');
            if (!linha) return;
            const abrindo = linha.classList.contains('d-none');
            linha.classList.toggle('d-none', !abrindo);
            elemento.setAttribute('aria-expanded', abrindo ? 'true' : 'false');
            if (seta) seta.textContent = abrindo ? '▾' : '▸';

            const alias = elemento.dataset.alias || (linha.querySelector('.painel-lojas')?.dataset.alias);
            atualizarStorageDetalhes(alias, abrindo);
        }

        function atualizarStorageDetalhes(alias, aberto) {
            if (!alias) return;
            try {
                let abertos = JSON.parse(sessionStorage.getItem('monitor_aliases_abertos') || '[]');
                if (!Array.isArray(abertos)) abertos = [];
                if (aberto) {
                    if (!abertos.includes(alias)) abertos.push(alias);
                } else {
                    abertos = abertos.filter(a => a !== alias);
                }
                sessionStorage.setItem('monitor_aliases_abertos', JSON.stringify(abertos));
            } catch (e) {}
        }

        function restaurarStorageDetalhes() {
            try {
                const abertos = JSON.parse(sessionStorage.getItem('monitor_aliases_abertos') || '[]');
                if (!Array.isArray(abertos) || abertos.length === 0) return;
                document.querySelectorAll('.painel-lojas').forEach(painel => {
                    const alias = painel.dataset.alias;
                    if (alias && abertos.includes(alias)) {
                        const trDetalhes = painel.closest('.linha-detalhes');
                        if (trDetalhes) {
                            trDetalhes.classList.remove('d-none');
                            const trPrincipal = trDetalhes.previousElementSibling;
                            if (trPrincipal) {
                                const btn = trPrincipal.querySelector('.alias-clicavel');
                                if (btn) {
                                    btn.setAttribute('aria-expanded', 'true');
                                    const seta = btn.querySelector('.seta-detalhes');
                                    if (seta) seta.textContent = '▾';
                                }
                            }
                        }
                    }
                });
            } catch (e) {}
        }
        window.addEventListener('DOMContentLoaded', restaurarStorageDetalhes);

        {% if tem_permissao('perm_servidores') %}
        // --- Edição inline de uma loja já existente ---
        const CAMPOS_LOJA_ORDEM = ['cod_info', 'loj_codigo', 'loj_fantasia', 'loj_nome', 'loj_cnpj', 'cli_situacao'];

        function renderizarLinhaLojaVisualizacao(linha, valores, estaIgnorado, id) {
            if (id) linha.dataset.id = id;
            linha.dataset.ignorar = estaIgnorado ? '1' : '0';

            linha.querySelectorAll('.valor-loja').forEach(celula => {
                const campo = celula.dataset.campo;
                const val = (valores[campo] !== undefined && valores[campo] !== null) ? String(valores[campo]).trim() : '';
                celula.innerHTML = '';
                celula.textContent = val || '-';
            });

            const colIgnorar = linha.querySelector('.col-ignorar');
            if (colIgnorar) {
                if (estaIgnorado) {
                    colIgnorar.innerHTML = '<span class="tag-ignorado" title="Loja marcada para ignorar pendências de preenchimento">Ignorado</span>';
                } else {
                    colIgnorar.innerHTML = '<span style="color: var(--cor-texto-suave); opacity: 0.5;">-</span>';
                }
            }

            const colAcoes = linha.querySelector('.acoes-loja');
            if (colAcoes) {
                colAcoes.innerHTML = `
                    <button class="btn-icon btn-icon-warning" onclick="editarLoja(this)" aria-label="Editar loja"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg></button>
                    <button class="btn-icon btn-icon-danger" onclick="excluirLoja(this)" aria-label="Excluir loja"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>
                `;
            }
        }

        function editarLoja(botao) {
            const linha = botao.closest('.linha-loja');
            const alias = linha.closest('.painel-lojas').dataset.alias;
            const id = linha.dataset.id;
            const celulas = linha.querySelectorAll('.valor-loja');
            const inputs = {};
            const valoresOriginais = {};

            celulas.forEach(celula => {
                const campo = celula.dataset.campo;
                const valorAtual = celula.textContent.trim() === '-' ? '' : celula.textContent.trim();
                valoresOriginais[campo] = valorAtual;
                const input = document.createElement('input');
                input.type = 'text';
                input.value = valorAtual;
                input.className = 'form-control form-control-sm';
                inputs[campo] = input;
                celula.innerHTML = '';
                celula.appendChild(input);
            });

            const celulaIgnorar = linha.querySelector('.col-ignorar');
            if (celulaIgnorar) {
                const estaIgnorado = linha.dataset.ignorar === '1';
                celulaIgnorar.innerHTML = `
                    <label style="display:inline-flex;align-items:center;gap:4px;font-size:0.8em;cursor:pointer;margin:0;white-space:nowrap;" title="Marcar para ignorar pendências caso a loja não tenha CNPJ/dados">
                        <input type="checkbox" class="chk-ignorar" ${estaIgnorado ? 'checked' : ''}> Ignorar
                    </label>
                `;
            }

            const celulaAcoes = linha.querySelector('.acoes-loja');
            celulaAcoes.innerHTML = '';
            
            const btnSalvar = document.createElement('button');
            btnSalvar.className = 'btn-icon btn-icon-success';
            btnSalvar.textContent = '💾';
            btnSalvar.title = 'Salvar alterações';
            btnSalvar.onclick = function() {
                const payload = { id: id, alias: alias };
                CAMPOS_LOJA_ORDEM.forEach(campo => { payload[campo] = inputs[campo].value.trim(); });
                const chk = linha.querySelector('.chk-ignorar');
                payload.ignorar_incompleto = (chk && chk.checked) ? 1 : 0;
                salvarLoja(payload, linha);
            };

            const btnCancelar = document.createElement('button');
            btnCancelar.className = 'btn-icon btn-icon-secondary';
            btnCancelar.textContent = '✖';
            btnCancelar.title = 'Cancelar edição';
            btnCancelar.style.marginLeft = '4px';
            btnCancelar.onclick = function() {
                renderizarLinhaLojaVisualizacao(linha, valoresOriginais, linha.dataset.ignorar === '1', id);
            };

            celulaAcoes.appendChild(btnSalvar);
            celulaAcoes.appendChild(btnCancelar);
        }

        // --- Adiciona uma nova loja (linha em branco editável) para o alias ---
        function adicionarLoja(botao, alias) {
            const painel = botao.closest('.painel-lojas');
            let tabela = painel.querySelector('.tabela-lojas tbody');

            if (!tabela) {
                const semLojas = painel.querySelector('.sem-lojas');
                if (semLojas) semLojas.remove();
                const wrapperHtml = document.createElement('table');
                wrapperHtml.className = 'tabela-lojas';
                wrapperHtml.innerHTML = '<thead><tr><th>CodInfo</th><th>LojCód.</th><th>Fantasia</th><th>Razão/Nome</th><th>CNPJ</th><th>Situação</th><th style="text-align: center; width: 85px;" title="Ignorar pendências desta loja">Ignorar?</th><th></th></tr></thead><tbody></tbody>';
                painel.insertBefore(wrapperHtml, botao);
                tabela = wrapperHtml.querySelector('tbody');
            }

            const novaLinha = document.createElement('tr');
            novaLinha.className = 'linha-loja';
            novaLinha.dataset.id = '';
            novaLinha.dataset.ignorar = '0';

            const inputs = {};
            CAMPOS_LOJA_ORDEM.forEach(campo => {
                const td = document.createElement('td');
                td.className = 'valor-loja';
                td.dataset.campo = campo;
                const input = document.createElement('input');
                input.type = 'text';
                input.className = 'form-control form-control-sm';
                inputs[campo] = input;
                td.appendChild(input);
                novaLinha.appendChild(td);
            });

            const tdIgnorar = document.createElement('td');
            tdIgnorar.className = 'col-ignorar text-center';
            tdIgnorar.innerHTML = '<label style="display:inline-flex;align-items:center;gap:4px;font-size:0.8em;cursor:pointer;margin:0;white-space:nowrap;" title="Ignorar pendências nesta loja"><input type="checkbox" class="chk-ignorar"> Ignorar</label>';
            novaLinha.appendChild(tdIgnorar);

            const tdAcoes = document.createElement('td');
            tdAcoes.className = 'acoes-loja';
            
            const btnSalvar = document.createElement('button');
            btnSalvar.className = 'btn-icon btn-icon-success';
            btnSalvar.textContent = '💾';
            btnSalvar.title = 'Salvar Loja';
            btnSalvar.onclick = function() {
                const payload = { id: '', alias: alias };
                CAMPOS_LOJA_ORDEM.forEach(campo => { payload[campo] = inputs[campo].value.trim(); });
                const chk = novaLinha.querySelector('.chk-ignorar');
                payload.ignorar_incompleto = (chk && chk.checked) ? 1 : 0;
                salvarLoja(payload, novaLinha);
            };

            const btnCancelar = document.createElement('button');
            btnCancelar.className = 'btn-icon btn-icon-secondary';
            btnCancelar.textContent = '✖';
            btnCancelar.title = 'Cancelar adição';
            btnCancelar.style.marginLeft = '4px';
            btnCancelar.onclick = function() {
                novaLinha.remove();
                if (tabela.children.length === 0) {
                    const wrapperTabela = painel.querySelector('.tabela-lojas');
                    if (wrapperTabela) wrapperTabela.remove();
                    if (!painel.querySelector('.sem-lojas')) {
                        const p = document.createElement('p');
                        p.className = 'sem-lojas';
                        p.textContent = 'Nenhuma loja cadastrada para este alias ainda.';
                        painel.insertBefore(p, botao);
                    }
                }
            };

            tdAcoes.appendChild(btnSalvar);
            tdAcoes.appendChild(btnCancelar);
            novaLinha.appendChild(tdAcoes);

            tabela.appendChild(novaLinha);
            inputs['cod_info'].focus();
        }

        function salvarLoja(payload, linhaElemento) {
            csrfFetch('/admin/cliente-loja/salvar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(data => {
                if (data.sucesso) {
                    const novoId = data.id || payload.id;
                    const estaIgnorado = payload.ignorar_incompleto === 1;
                    if (linhaElemento) {
                        renderizarLinhaLojaVisualizacao(linhaElemento, payload, estaIgnorado, novoId);

                        // Se o cliente possuía o badge "sem loja", remove-o da linha principal
                        const painel = linhaElemento.closest('.painel-lojas');
                        if (painel) {
                            const trDetalhes = painel.closest('.linha-detalhes');
                            if (trDetalhes && trDetalhes.previousElementSibling) {
                                const badgeSemLoja = trDetalhes.previousElementSibling.querySelector('.badge.bg-warning');
                                if (badgeSemLoja) badgeSemLoja.remove();
                            }
                        }

                        // Feedback visual suave de sucesso
                        linhaElemento.style.transition = 'background-color 0.4s ease';
                        linhaElemento.style.backgroundColor = '#d4edda';
                        setTimeout(() => { linhaElemento.style.backgroundColor = ''; }, 900);
                    } else {
                        location.reload();
                    }
                } else {
                    alert('Erro ao salvar: ' + (data.mensagem || 'tente novamente.'));
                }
            })
            .catch(() => alert('Não foi possível salvar. Verifique sua conexão.'));
        }

        function excluirLoja(botao) {
            if (!confirm('Excluir esta loja do cadastro?')) return;
            const linha = botao.closest('.linha-loja');
            const painel = linha.closest('.painel-lojas');
            const id = linha.dataset.id;

            csrfFetch('/admin/cliente-loja/excluir', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: id })
            })
            .then(r => r.json())
            .then(data => {
                if (data.sucesso) {
                    const tabela = linha.closest('tbody');
                    linha.remove();
                    if (tabela && tabela.children.length === 0) {
                        const wrapper = painel.querySelector('.tabela-lojas');
                        if (wrapper) wrapper.remove();
                        const btnAdd = painel.querySelector('button');
                        if (!painel.querySelector('.sem-lojas')) {
                            const p = document.createElement('p');
                            p.className = 'sem-lojas';
                            p.textContent = 'Nenhuma loja cadastrada para este alias ainda.';
                            painel.insertBefore(p, btnAdd);
                        }

                        // Recoloca o badge "sem loja" se todas foram excluídas
                        const trDetalhes = painel.closest('.linha-detalhes');
                        if (trDetalhes && trDetalhes.previousElementSibling) {
                            const celulaAlias = trDetalhes.previousElementSibling.querySelectorAll('td')[1];
                            if (celulaAlias && !celulaAlias.querySelector('.badge.bg-warning')) {
                                const span = document.createElement('span');
                                span.className = 'badge bg-warning text-dark';
                                span.style.fontSize = '0.7em';
                                span.title = 'Nenhuma loja cadastrada para este alias';
                                span.textContent = '📭 sem loja';
                                celulaAlias.appendChild(span);
                            }
                        }
                    }
                } else {
                    alert('Erro ao excluir: ' + (data.mensagem || 'tente novamente.'));
                }
            })
            .catch(() => alert('Não foi possível excluir. Verifique sua conexão.'));
        }

        function marcarSemCnpjIgnorados() {
            if (!confirm('Deseja marcar como IGNORADOS todos os lançamentos de lojas sem CNPJ cadastrado?')) return;
            csrfFetch('/admin/lojas/ignorar-sem-cnpj', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            })
            .then(r => r.json())
            .then(data => {
                if (data.sucesso) {
                    alert(data.mensagem || 'Lojas atualizadas com sucesso!');
                    location.reload();
                } else {
                    alert('Erro: ' + (data.mensagem || 'tente novamente.'));
                }
            })
            .catch(() => alert('Não foi possível concluir a ação. Verifique sua conexão.'));
        }
        {% endif %}

        // --- Edição inline da string de conexão (CNAME) ---
        function editarCname(botao) {
            const celula = botao.closest('.cname-cell');
            const span = celula.querySelector('.cname-texto');
            const btnCopy = celula.querySelector('.btn-copy');
            const alias = celula.dataset.alias;
            const valorAtual = span.textContent;

            const input = document.createElement('input');
            input.type = 'text';
            input.value = valorAtual;
            input.className = 'form-control form-control-sm';
            input.style.display = 'inline-block';
            input.style.width = '240px';

            const btnSalvar = document.createElement('button');
            btnSalvar.className = 'btn-copy';
            btnSalvar.style.background = '#e8f8f0';
            btnSalvar.style.color = '#1e8449';
            btnSalvar.textContent = '💾 Salvar';

            const btnCancelar = document.createElement('button');
            btnCancelar.className = 'btn-copy';
            btnCancelar.style.background = '#fde8e8';
            btnCancelar.style.color = '#c0392b';
            btnCancelar.textContent = '✖';
            btnCancelar.title = 'Cancelar';

            function finalizarEdicao(val) {
                span.textContent = val;
                input.replaceWith(span);
                btnSalvar.replaceWith(botao);
                if (btnCancelar.parentNode) btnCancelar.remove();
                if (btnCopy) {
                    btnCopy.dataset.cname = val;
                    btnCopy.title = val + ' — clique para copiar';
                }
                celula.dataset.host = val;
            }

            btnSalvar.onclick = function() {
                const novoCname = input.value.trim();
                csrfFetch('/admin/cname/salvar', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ alias: alias, cname: novoCname })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.sucesso) {
                        finalizarEdicao(novoCname);
                        celula.style.transition = 'background-color 0.4s ease';
                        celula.style.backgroundColor = '#d4edda';
                        setTimeout(() => { celula.style.backgroundColor = ''; }, 900);
                    } else {
                        alert('Erro ao salvar: ' + (data.mensagem || 'tente novamente.'));
                    }
                })
                .catch(() => alert('Não foi possível salvar. Verifique sua conexão.'));
            };

            btnCancelar.onclick = function() {
                input.replaceWith(span);
                btnSalvar.replaceWith(botao);
                btnCancelar.remove();
            };

            span.replaceWith(input);
            botao.replaceWith(btnSalvar);
            btnSalvar.after(btnCancelar);
            input.focus();
        }

        // --- Teste de conectividade de todos os CNAMEs em lotes controlados ---
        async function testarCnames() {
            const cells = Array.from(document.querySelectorAll('.cname-cell'));
            if (cells.length === 0) return;

            const MAX_CONCURRENT = 100;
            const REQUEST_TIMEOUT = 12000;

            function atualizarStatus(statusEl, estado, titulo) {
                statusEl.classList.remove('ativo', 'inativo', 'testando', 'indefinido');
                statusEl.classList.add(estado);
                statusEl.textContent = '';

                if (estado === 'ativo') {
                    statusEl.setAttribute('aria-label', 'CNAME ativo');
                } else if (estado === 'inativo') {
                    statusEl.setAttribute('aria-label', 'CNAME inativo');
                } else {
                    statusEl.setAttribute('aria-label', 'Não foi possível testar o CNAME');
                }
                statusEl.title = titulo;
            }

            async function testarUm(celula, tentativa = 0) {
                const statusEl = celula.querySelector('.cname-status');
                const host = (celula.dataset.host || celula.querySelector('.cname-texto')?.textContent || '').trim();
                const assinatura = celula.dataset.assinatura || '';
                if (!statusEl) return;

                if (!host || !assinatura) {
                    atualizarStatus(statusEl, 'indefinido', 'Dados incompletos para testar o CNAME');
                    return;
                }

                statusEl.classList.remove('ativo', 'inativo', 'indefinido');
                statusEl.classList.add('testando');
                statusEl.textContent = '';
                statusEl.setAttribute('aria-label', 'Testando status do CNAME');
                statusEl.title = 'Testando conectividade...';

                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
                try {
                    const params = new URLSearchParams({host, assinatura});
                    const response = await csrfFetch('/api/cname/testar?' + params, {signal: controller.signal});
                    const data = await response.json().catch(() => ({}));

                    if (response.status === 429) {
                        const esperaInformada = Number(data.retry_after || data.cooldown || 2);
                        const espera = Math.min(60, Math.max(1, Number.isFinite(esperaInformada) ? esperaInformada : 2));
                        clearTimeout(timeout);
                        statusEl.classList.remove('ativo', 'inativo', 'indefinido');
                        statusEl.classList.add('testando');
                        statusEl.textContent = '';
                        statusEl.setAttribute('aria-label', 'CNAME aguardando nova tentativa');
                        statusEl.title = `Limite temporário. Nova tentativa em ${espera}s.`;
                        const dispersao = Math.min(tentativa * 250, 2000) + Math.floor(Math.random() * 300);
                        await new Promise(resolve => setTimeout(resolve, espera * 1000 + dispersao));
                        return testarUm(celula, tentativa + 1);
                    }

                    if (!response.ok) {
                        atualizarStatus(statusEl, 'indefinido', data.erro || `Falha ao testar (HTTP ${response.status})`);
                        return;
                    }

                    if (data.ativo) {
                        atualizarStatus(statusEl, 'ativo', data.cached ? 'CNAME ativo (cache)' : 'CNAME ativo: DNS resolvido corretamente');
                    } else {
                        atualizarStatus(statusEl, 'inativo', data.erro || 'CNAME inativo: não foi possível resolver o DNS');
                    }
                } catch (erro) {
                    const mensagem = erro.name === 'AbortError'
                        ? 'O teste do CNAME excedeu 12 segundos'
                        : `Erro ao testar o CNAME: ${erro.message}`;
                    atualizarStatus(statusEl, 'indefinido', mensagem);
                } finally {
                    clearTimeout(timeout);
                }
            }

            const fila = [];
            let emAndamento = 0;

            function processarFila() {
                while (emAndamento < MAX_CONCURRENT && fila.length > 0) {
                    const celula = fila.shift();
                    emAndamento += 1;
                    testarUm(celula).finally(() => {
                        celula.dataset.cnameTeste = 'concluido';
                        emAndamento -= 1;
                        processarFila();
                    });
                }
            }

            function agendarTeste(celula) {
                if (celula.dataset.cnameTeste) return;
                celula.dataset.cnameTeste = 'agendado';
                fila.push(celula);
                processarFila();
            }

            if ('IntersectionObserver' in window) {
                const observador = new IntersectionObserver(entries => {
                    entries.forEach(entry => {
                        if (!entry.isIntersecting) return;
                        observador.unobserve(entry.target);
                        agendarTeste(entry.target);
                    });
                }, {root: null, rootMargin: '240px 0px', threshold: 0.01});
                cells.forEach(celula => observador.observe(celula));
            } else {
                cells.forEach(agendarTeste);
            }
        }

        // --- Copiar CNAME para clipboard ---
        function copiarCnameCliente(elemento) {
            const texto = elemento.getAttribute('data-cname') || '';
            if (!texto) return;
            const tituloOrig = elemento.getAttribute('title') || texto;
            const marcar = function() {
                elemento.title = 'Copiado!';
                elemento.style.outline = '2px solid #27ae60';
                setTimeout(function() {
                    elemento.title = tituloOrig;
                    elemento.style.outline = '';
                }, 1500);
            };
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(texto).then(marcar).catch(function() { copiarFallback(texto, elemento); });
            } else {
                copiarFallback(texto, elemento);
            }
        }

        function copiarString(texto, elemento) {
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(texto).then(() => {
                    marcarComoCopiado(elemento);
                }).catch(() => {
                    copiarFallback(texto, elemento);
                });
            } else {
                copiarFallback(texto, elemento);
            }
        }

        function copiarFallback(texto, elemento) {
            var textArea = document.createElement("textarea");
            textArea.value = texto;
            textArea.style.position = "fixed";
            textArea.style.left = "-999999px";
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            try {
                document.execCommand('copy');
                marcarComoCopiado(elemento);
            } catch (err) {
                alert("Não foi possível copiar: " + texto);
            }
            document.body.removeChild(textArea);
        }

        function marcarComoCopiado(elemento) {
            let textoOriginal = elemento.innerHTML;
            elemento.innerHTML = "✅ Copiado!";
            elemento.style.background = "#27ae60";
            elemento.style.color = "white";
            setTimeout(() => {
                elemento.innerHTML = textoOriginal;
                elemento.style.background = "#ebf5fb";
                elemento.style.color = "#2980b9";
            }, 1800);
        }

        document.addEventListener("DOMContentLoaded", function() {
            testarCnames();
            {% if modo_historico and dados_grafico and dados_grafico.labels %}
                const dadosHistorico = {{ dados_grafico|tojson }};
                var ctxHistorico = document.getElementById('chart-historico-linha').getContext('2d');
                new Chart(ctxHistorico, {
                    type: 'line',
                    data: dadosHistorico,
                    options: {
                        animation: false,
                        normalized: true,
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: {
                            mode: 'index',
                            intersect: false,
                        },
                        scales: {
                            y: {
                                beginAtZero: false,
                                title: { display: true, text: 'Tamanho Total (GB)' }
                            },
                            x: {
                                title: { display: true, text: 'Data' },
                                ticks: { autoSkip: true, maxTicksLimit: 12 }
                            }
                        },
                        elements: {
                            point: { radius: 0, hitRadius: 8 },
                            line: { tension: 0 }
                        }
                    }
                });

                const dadosArea = {
                    labels: dadosHistorico.labels,
                    datasets: dadosHistorico.datasets.map(dataset => ({
                        ...dataset,
                        data: dataset.data.slice(),
                        fill: true,
                        stack: 'espaco-servidores',
                        backgroundColor: dataset.borderColor + '66'
                    }))
                };
                var ctxArea = document.getElementById('chart-historico-area').getContext('2d');
                new Chart(ctxArea, {
                    type: 'line',
                    data: dadosArea,
                    options: {
                        animation: false,
                        normalized: true,
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: 'index', intersect: false },
                        scales: {
                            y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Tamanho Total (GB)' } },
                            x: { stacked: true, title: { display: true, text: 'Data' }, ticks: { autoSkip: true, maxTicksLimit: 12 } }
                        },
                        elements: {
                            point: { radius: 0, hitRadius: 8 },
                            line: { tension: 0 }
                        }
                    }
                });
            {% endif %}
        });
    </script>
</body>
</html>
"""

# --- TEMPLATE HTML ÁREA ADMINISTRATIVA ---
HTML_ADMIN = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Painel de Gestão Admin - Databases.conf</title>
    <!-- Bootstrap 5 CSS -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <!-- Font Awesome Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--cor-fundo); color: var(--cor-texto); margin: 0; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: var(--cor-superficie); color: var(--cor-texto); padding: 15px 20px; border-radius: var(--raio); margin-bottom: 20px; border: 1px solid var(--cor-borda); }
        .header h2 { margin: 0; font-size: 1.3em; }
        .btn-voltar { background: var(--cor-superficie); color: var(--cor-primaria); border: 1px solid var(--cor-borda); text-decoration: none; padding: 8px 14px; border-radius: var(--raio); font-weight: 600; }
        .btn-voltar:hover { background: #eff6ff; color: var(--cor-primaria-escura); }
        .btn-logout { background: var(--cor-superficie); color: var(--cor-perigo); border: 1px solid var(--cor-borda); text-decoration: none; padding: 8px 14px; border-radius: var(--raio); font-weight: 600; margin-left: 10px; }
        .btn-logout:hover { background: #fef2f2; }

        .tabs { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
        .tab { padding: 8px 16px; background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-radius: var(--raio); text-decoration: none; color: var(--cor-texto); font-weight: 600; font-size: 0.9em; }
        .tab.ativa { background: var(--cor-primaria); color: white; border-color: var(--cor-primaria); }

        .card { background: var(--cor-superficie); padding: 20px; border-radius: var(--raio); box-shadow: 0 1px 3px rgba(0,0,0,0.06); border: 1px solid var(--cor-borda); margin-bottom: 20px; }
        
        .msg-sucesso { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; padding: 10px; border-radius: var(--raio); margin-bottom: 15px; }
        .msg-erro { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 10px; border-radius: var(--raio); margin-bottom: 15px; }

        table { width: 100%; border-collapse: collapse; margin-top: 10px; vertical-align: middle; }
        th, td { border: 1px solid var(--cor-borda); padding: 8px 12px; text-align: left; }
        th { background: #f8fafc; }
        
        /* VISUALIZADOR COM DESTAQUE DE CORES */
        .tabela-registros-scroll { max-height: 420px; overflow-y: auto; border: 1px solid var(--cor-borda); border-radius: var(--raio); }
        .conf-viewer-container {
            background-color: #1e1e1e;
            color: #d4d4d4;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 0.9em;
            border-radius: var(--raio);
            padding: 12px;
            max-height: 400px;
            overflow-y: auto;
            border: 1px solid #333;
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .conf-line { padding: 2px 6px; border-radius: 3px; display: block; }
        .conf-line-comentario { color: #ffc107; }
        .conf-line-ativo { 
            color: #4ade80; 
            font-weight: bold; 
            background: rgba(74, 222, 128, 0.12); 
            border-left: 3px solid #4ade80; 
            padding-left: 8px;
        }
        .conf-line-inativo { color: #888888; text-decoration: line-through; }
        
        textarea.editor-raw { 
            width: 100%; 
            height: 350px; 
            font-family: 'Consolas', 'Courier New', monospace; 
            padding: 12px; 
            border: 1px solid var(--cor-borda); 
            border-radius: var(--raio); 
            box-sizing: border-box; 
            background-color: #1e1e1e; 
            color: #e2e8f0; 
            font-size: 0.95em;
            line-height: 1.4;
        }

        .icon-type { font-size: 1.1rem; margin-right: 8px; vertical-align: middle; }
        .icon-folder { color: var(--cor-aviso); }
        .icon-config { color: var(--cor-primaria); }

        /* Botões de ação neutralizados para acompanhar a mesma paleta do resto do site */
        .btn-primary { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; color: var(--cor-texto-invertido) !important; }
        .btn-outline-primary { color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
        .btn-outline-primary:hover { background-color: var(--cor-primaria) !important; color: white !important; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<script src="{{ url_for('static', filename='admin-destino.js') }}" defer></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css', v='admin-20260925-2') }}">
<script src="{{ url_for('static', filename='ui.js', v='admin-20260925-2') }}" defer></script>
</head>
<body class="admin-page">
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <!-- CÓDIGO CORRIGIDO COM O BOTÃO DE PERFIL -->
    <div class="header admin-header">
        <h2><i class="fa-solid fa-database me-2"></i>Gestão Administrativa de Databases - {{ servidor_atual }}</h2>
        <div class="admin-header-actions">
            <!-- Nome do usuário logado -->
            <span class="text-muted me-1"><i class="fa-solid fa-circle-user me-1"></i>{{ usuario_nome }}{% if session.get('eh_master') %} <span class="badge badge-danger">Master</span>{% endif %}</span>
            <!-- Botão de Gestão de Perfil / Alterar Senha -->
            <button type="button" onclick="abrirModalPerfil()" class="btn btn-warning">
                <i class="fa-solid fa-user-gear me-1"></i> Perfil
            </button>
            <a href="/servidor/DB01" class="btn btn-primary">Servidores</a>
            <a href="/horarios" class="btn btn-primary">Horários</a>
            <button id="theme-toggle" class="theme-toggle" aria-label="Alternar tema" title="Alternar tema claro/escuro">
                <svg class="moon-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
                <svg class="sun-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
            </button>
            <form action="/admin/logout" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="btn btn-danger">Sair</button></form>
        </div>
    </div>

    <div id="conteudo-principal" role="main">

    {% if msg_sucesso %}<div class="msg-sucesso">✅ {{ msg_sucesso }}</div>{% endif %}
    {% if msg_erro %}<div class="msg-erro">❌ {{ msg_erro }}</div>{% endif %}

    <!-- NAVEGAÇÃO DE SERVIDORES NA ADMIN -->
    <div class="tabs">
        {% for srv in servidores.keys() %}
            <a href="/admin?servidor={{ srv }}" class="tab {% if srv == servidor_atual %}ativa{% endif %}">{{ srv }}</a>
        {% endfor %}
    </div>

    <!-- FORMULÁRIO DE NOVO BANCO COM PREENCHIMENTO AUTOMÁTICO DO CAMINHO COM NOME DO ALIAS -->
    <div class="card">
        <h3 class="mb-3 text-primary"><i class="fa-solid fa-plus me-1"></i> Adicionar Novo Alias ao {{ servidor_atual }}</h3>
        <form action="/admin/adicionar" method="POST" enctype="multipart/form-data" id="dbForm" class="row g-3" onsubmit="return pedirSenhaEConfirmar(this)">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="servidor" id="servidor" value="{{ servidor_atual }}">
            <input type="hidden" name="senha_confirmacao" class="campo-senha-confirmacao">

            <div class="col-md-4">
                <label class="form-label fw-bold">Alias do Cliente:</label>
                <input type="text" id="alias" name="alias" class="form-control" placeholder="ex: novaloja" oninput="atualizarCaminhoPadrao()" required autocomplete="off">
            </div>

            <div class="col-md-8">
                <label class="form-label fw-bold">Pessoas / Responsáveis Envolvidos:</label>
                <input type="text" id="envolvidos" name="envolvidos" class="form-control" placeholder="ex: João / Maria" required>
            </div>

            <div class="col-md-6">
                <label for="modo_destino" class="form-label fw-bold">Pasta de destino:</label>
                <select name="modo_destino" id="modo_destino" class="form-select">
                    <option value="alias">Usar pasta com o nome do alias</option>
                    <option value="existente">Selecionar uma pasta existente</option>
                </select>
                <small class="text-muted">A pasta do alias será criada somente se ainda não existir.</small>
            </div>
            <div class="col-md-6">
                <label for="nome_arquivo" class="form-label fw-bold">Nome do banco no destino:</label>
                <input type="text" id="nome_arquivo" name="nome_arquivo" class="form-control" value="dados.fdb" required maxlength="150">
                <small class="text-muted">Use outro nome para adicionar um banco à mesma pasta.</small>
            </div>
            <div class="col-12" id="pastasExistentes" hidden>
                <label for="pasta_destino" class="form-label fw-bold">Pastas do servidor:</label>
                <div class="d-flex gap-2 flex-wrap">
                    <select id="pasta_destino" name="pasta_destino" class="form-select flex-1" style="min-width:220px" disabled>
                        <option value="">Selecione uma pasta</option>
                    </select>
                    <button type="button" id="abrirPasta" class="btn btn-outline-secondary" disabled>Ver subpastas</button>
                    <button type="button" id="voltarPasta" class="btn btn-outline-secondary" disabled>Voltar</button>
                    <button type="button" id="recarregarPastas" class="btn btn-outline-secondary">Atualizar pastas</button>
                </div>
                <small id="statusPastas" class="text-muted" role="status"></small>
            </div>
            <div class="col-12">
                <label for="caminho" class="form-label fw-bold">Caminho final do banco:</label>
                <input type="text" id="caminho" name="caminho" class="form-control bg-light" readonly required placeholder="Informe o alias ou selecione a pasta">
                <small class="text-muted">Arquivos existentes não serão substituídos. Para cadastrar um arquivo já no servidor, deixe o envio vazio.</small>
            </div>

            <div class="col-12">
                <label class="form-label fw-bold">Enviar arquivo de banco (.fdb) [Opcional]:</label>
                <input type="file" name="arquivo_fdb" id="arquivo_fdb" class="form-control" accept=".fdb">
            </div>
        
            <div class="col-12 text-end">
                <button type="submit" class="btn btn-primary fw-bold">
                    <i class="fa-solid fa-plus me-1"></i> Adicionar Alias
                </button>
            </div>
        </form>
    </div>

    <!-- SOLICITAÇÃO DE NOVO CNAME POR E-MAIL -->
    <div class="card mt-3">
        <h3 class="mb-3 text-primary"><i class="fa-solid fa-envelope me-1"></i> Solicitar Criação de Novo CNAME</h3>
        <form id="formSolicitarCname" class="row g-3 align-items-end" onsubmit="return enviarSolicitacaoCname(event)">
            <div class="col-md-5">
                <label class="form-label fw-bold">Nome Fantasia da Empresa:</label>
                <input type="text" id="cnameFantasia" name="fantasia" class="form-control" placeholder="ex: Blend Top Colchões" required autocomplete="off" oninput="agendarBuscaAlias()">
            </div>
            <div class="col-md-4">
                <label class="form-label fw-bold">Alias:</label>
                <input type="text" id="cnameAlias" name="alias" class="form-control" placeholder="preenchido automaticamente, se encontrado" autocomplete="off">
            </div>
            <div class="col-md-3 d-flex align-items-end">
                <button type="submit" class="btn btn-outline-primary fw-bold w-100">
                    <i class="fa-solid fa-paper-plane me-1"></i> Enviar Solicitação
                </button>
            </div>
            <div class="col-12 mt-1"><small class="text-muted">O alias é buscado pelo nome fantasia cadastrado. Se não encontrar, digite manualmente. Destinatário: atendimento@nossatelecom.com.br.</small></div>
            <div class="col-12"><div id="statusSolicitarCname" role="status"></div></div>
        </form>
    </div>
    
    <!-- TABELA DE GESTÃO DE USUÁRIOS (EXIBIDA APENAS PARA O USUÁRIO MASTER) -->
    {% if session.get('eh_master') %}
    <div class="card admin-users-card shadow-sm mb-4">
        <div class="admin-section-header">
            <i class="fa-solid fa-users-gear me-2"></i>Gestão de Usuários e Permissões do Sistema (Master)
        </div>
        <div class="admin-card-body">
            <div class="table-responsive">
            <table class="table table-hover align-middle">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Nome</th>
                        <th>E-mail</th>
                        <th>Tipo</th>
                        <th>Status</th>
                        <th>Acessos</th>
                        <th>Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for u in usuarios_lista %}
                    <tr>
                        <td>{{ u.id }}</td>
                        <td><strong>{{ u.nome|nome_pessoa }}</strong></td>
                        <td>{{ u.email }}</td>
                        <td>
                            {% if u.eh_master %}
                                <span class="badge bg-danger">Master</span>
                            {% else %}
                                <span class="badge bg-secondary">Usuário</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if u.ativo %}
                                <span class="badge bg-success">Ativo</span>
                            {% else %}
                                <span class="badge bg-danger">Inativo</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if u.eh_master %}
                                <span class="text-muted small">Acesso total</span>
                            {% else %}
                                <form class="form-permissoes" data-user-id="{{ u.id }}" onsubmit="return salvarPermissoes(event, {{ u.id }})">
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input" type="checkbox" name="perm_servidores" id="perm_servidores_{{ u.id }}" {% if u.perm_servidores %}checked{% endif %}>
                                        <label class="form-check-label small" for="perm_servidores_{{ u.id }}" title="Editar CNAME/Lojas na lista de servidores">Servidores</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input" type="checkbox" name="perm_gestao_bancos" id="perm_gestao_bancos_{{ u.id }}" {% if u.perm_gestao_bancos %}checked{% endif %}>
                                        <label class="form-check-label small" for="perm_gestao_bancos_{{ u.id }}" title="Enviar/criar/editar/excluir alias em /admin">Gestão Bancos</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input" type="checkbox" name="perm_horarios" id="perm_horarios_{{ u.id }}" {% if u.perm_horarios %}checked{% endif %}>
                                        <label class="form-check-label small" for="perm_horarios_{{ u.id }}" title="Painel de Horários">Horários</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input" type="checkbox" name="perm_certificados" id="perm_certificados_{{ u.id }}" {% if u.perm_certificados %}checked{% endif %}>
                                        <label class="form-check-label small" for="perm_certificados_{{ u.id }}">Certificados</label>
                                    </div>
                                    <button type="submit" class="btn btn-sm btn-outline-primary ms-1">💾</button>
                                </form>
                            {% endif %}
                        </td>
                        <td>
                            {% if not u.eh_master %}
                                <div class="acoes-container admin-user-actions">
                                <!-- Botão Toggle Ativo/Inativo -->
                                <form action="/admin/usuarios/toggle/{{ u.id }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit"  class="btn btn-sm {% if u.ativo %}btn-outline-warning{% else %}btn-outline-success{% endif %}">
                                    {% if u.ativo %}Inativar{% else %}Ativar{% endif %}
                                </button></form>

                                <!-- Botão Reset Senha -->
                                <form action="/admin/usuarios/reset-senha/{{ u.id }}" method="POST" class="form-inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit"  class="btn btn-sm btn-outline-primary" onclick="return confirm('Enviar link de redefinição de senha para este e-mail?')">
                                    <i class="fa-solid fa-key"></i> Reset Senha
                                </button></form>

                                <!-- Botão Excluir (Abre Modal) -->
                                <button type="button" class="btn btn-sm btn-outline-danger" onclick="openModal('modalExcluir{{ u.id }}')" aria-haspopup="dialog">
                                    <i class="fa-solid fa-trash"></i> Excluir
                                </button>
                                </div>
                            {% else %}
                                <span class="text-muted">Protegido</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            </div>

            <!-- MODAIS DE EXCLUSÃO (POSICIONADOS FORA DA TABELA PARA EVITAR QUEBRAS DE RENDERIZAÇÃO) -->
            {% for u in usuarios_lista %}
                {% if not u.eh_master %}
                <div class="app-modal" id="modalExcluir{{ u.id }}" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="tituloExcluir{{ u.id }}">
                    <form action="/admin/usuarios/excluir" method="POST" class="modal-content admin-delete-modal">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <div class="modal-header admin-danger-header">
                            <h5 class="modal-title" id="tituloExcluir{{ u.id }}">Excluir usuário: {{ u.nome|nome_pessoa }}</h5>
                            <button type="button" class="modal-close" onclick="closeModal('modalExcluir{{ u.id }}')" aria-label="Fechar">&times;</button>
                        </div>
                        <div class="modal-body">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <p class="text-danger fw-bold mb-3">Esta ação não poderá ser desfeita.</p>
                            <div class="mb-3">
                                <label class="form-label">Digite sua senha de master para confirmar:</label>
                                <input type="password" name="senha_master" class="form-control" required autocomplete="current-password">
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" onclick="closeModal('modalExcluir{{ u.id }}')">Cancelar</button>
                            <button type="submit" class="btn btn-solid-danger">Confirmar exclusão</button>
                        </div>
                    </form>
                </div>
                {% endif %}
            {% endfor %}
        </div>
    </div>

    <!-- INTEGRAÇÃO COM API INFOBRASIL (APENAS MASTER) -->
    <div class="card admin-api-card shadow-sm mb-4">
        <div class="admin-section-header d-flex justify-content-between align-items-center">
            <span><i class="fa-solid fa-cloud-arrow-down me-2"></i>Integração API InfoBrasil (Sincronização de Lojas)</span>
            <span class="badge bg-primary">Master</span>
        </div>
        <div class="admin-card-body" style="padding: 20px;">
            <p class="text-muted small mb-3">
                Configure as credenciais de acesso da API InfoBrasil para enriquecimento diário automático dos dados de lojas (CodInfo e Situação) via CNPJ. As credenciais são salvas de forma segura no arquivo <code>.env</code>.
            </p>
            <form id="formConfigApi" onsubmit="return salvarConfigApi(event)">
                <div class="row g-3">
                    <div class="col-md-5">
                        <label class="form-label fw-bold small">URL Base da API</label>
                        <input type="url" id="api_url" class="form-control form-control-sm" value="{{ api_config.url if api_config else 'https://info-api.infobrasilsistemas.com.br' }}" placeholder="https://info-api.infobrasilsistemas.com.br" required>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label fw-bold small">Usuário (Username)</label>
                        <input type="text" id="api_usuario" class="form-control form-control-sm" value="{{ api_config.usuario if api_config else '' }}" placeholder="ex: infobrasil" required autocomplete="off">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label fw-bold small">Senha (Password)</label>
                        <div class="input-group input-group-sm">
                            <input type="password" id="api_senha" class="form-control" value="{{ api_config.senha if api_config else '' }}" placeholder="Digite a senha" required autocomplete="off">
                            <button class="btn btn-outline-secondary" type="button" onclick="toggleSenhaApi()" title="Mostrar/Ocultar Senha">
                                <i class="fa-solid fa-eye" id="iconeSenhaApi"></i>
                            </button>
                        </div>
                    </div>
                </div>
                <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between mt-3 pt-2 border-top">
                    <div class="d-flex gap-2">
                        <button type="submit" class="btn btn-primary btn-sm" id="btnSalvarApi">
                            <i class="fa-solid fa-floppy-disk me-1"></i> Gravar no .env
                        </button>
                        <button type="button" class="btn btn-outline-secondary btn-sm" id="btnTestarApi" onclick="testarConexaoApi()">
                            <i class="fa-solid fa-plug me-1"></i> Testar Conexão
                        </button>
                        <button type="button" class="btn btn-outline-success btn-sm" id="btnSincronizarApi" onclick="sincronizarApiAgora()">
                            <i class="fa-solid fa-arrows-rotate me-1"></i> Sincronizar Agora
                        </button>
                    </div>
                    <div id="statusExecucaoApi" class="small text-muted">
                        {% if api_ultimo_status %}
                            <span class="badge {% if api_ultimo_status.status == 'sucesso' %}bg-success{% else %}bg-danger{% endif %} me-1">
                                {{ api_ultimo_status.status|upper }}
                            </span>
                            Última execução: <strong>{{ api_ultimo_status.data_hora }}</strong> ({{ api_ultimo_status.detalhes }})
                        {% else %}
                            Nenhuma sincronização registrada ainda.
                        {% endif %}
                    </div>
                </div>
            </form>
            <div id="feedbackApi" class="alert d-none mt-3 mb-0 small py-2" role="alert"></div>
        </div>
    </div>
    {% endif %}


    <!-- TABELA DE GERENCIAMENTO RÁPIDO (INATIVAÇÃO E EDIÇÃO EM LINHA COM CANCELAR) -->
    <div class="card">
        <h3 class="mb-3"><i class="fa-solid fa-list me-2"></i>Registros de Databases ({{ servidor_atual }})</h3>
        <div class="tabela-registros-scroll">
        <table class="table table-hover table-striped border align-middle" id="tabelaBancos">
            <thead class="table-dark sticky-top" style="z-index: 1;">
                <tr>
                    <th class="cursor-pointer" style="width: 180px;" onclick="ordenarTabelaRegistros(0, 'data')" title="Clique para ordenar">Data Tag / Tipo ⇳</th>
                    <th class="cursor-pointer" onclick="ordenarTabelaRegistros(1, 'texto')" title="Clique para ordenar">Alias ⇳</th>
                    <th class="cursor-pointer" onclick="ordenarTabelaRegistros(2, 'texto')" title="Clique para ordenar">Caminho ⇳</th>
                    <th style="width: 220px;">Ação</th>
                </tr>
            </thead>
            <tbody>
                {% for b in bancos_conf %}
                <tr id="tr-{{ loop.index }}" data-original-alias="{{ b.alias }}" data-original-caminho="{{ b.caminho }}">
                    <td>
                        {% if b.origem_data == 'pasta' %}
                            <i class="fa-solid fa-folder icon-type icon-folder" title="Origem: Pasta"></i>
                        {% elif b.origem_data == 'conf' %}
                            <i class="fa-solid fa-file-code icon-type icon-config" title="Origem: .CONF"></i>
                        {% else %}
                            <i class="fa-solid fa-file-lines icon-type icon-unknown" title="Origem não identificada"></i>
                        {% endif %}
                        <span class="val-data">{{ b.data_criacao }}</span>
                    </td>
                    <td class="val-alias fw-bold">
                        {% if b.eh_inativo %}
                            <del>{{ b.alias }}</del> <span class="badge bg-secondary">Inativo</span>
                        {% else %}
                            <span class="alias-text">{{ b.alias }}</span>
                        {% endif %}
                    </td>
                    <td class="val-caminho"><code>{{ b.caminho }}</code></td>
                    <td class="val-acoes">
                        {% if not b.eh_inativo %}
                            <div class="btn-group-acoes" id="acoes-{{ loop.index }}">
                                <!-- Botão Editar -->
                                <button class="btn btn-sm btn-outline-primary" onclick="editarLinha({{ loop.index }})">
                                    <i class="fa-solid fa-pen-to-square"></i> Editar
                                </button>
                                <!-- Botão Inativar -->
                                <form action="/admin/inativar" method="POST" class="d-inline" data-alias="{{ b.alias }}" onsubmit="return confirmarInativacao(this);">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                                    <input type="hidden" name="servidor" value="{{ servidor_atual }}">
                                    <input type="hidden" name="alias" value="{{ b.alias }}">
                                    <input type="hidden" name="senha_confirmacao" class="campo-senha-confirmacao">
                                    <button type="submit" class="btn btn-sm btn-outline-danger">
                                        <i class="fa-solid fa-ban me-1"></i>Inativar
                                    </button>
                                </form>
                            </div>
                        {% else %}
                            <span class="text-muted">-</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        </div>
    </div>

    <!-- VISUALIZADOR COM DESTAQUE DE CORES E EDITOR DE TEXTO BRUTO -->
    <div class="card shadow-sm mb-4" id="secaoArquivo">
        <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center">
            <span><i class="fa-solid fa-code me-2"></i>Exibição do Arquivo (databases.conf)</span>
            <div>
                <button class="btn btn-sm btn-outline-info me-2" onclick="alternarModoEdicao()" id="btnModo">
                    <i class="fa-solid fa-pen"></i> Abrir Modo Edição Direta
                </button>
                <button class="btn btn-sm btn-outline-light" onclick="copiarConteudo()">
                    <i class="fa-solid fa-copy"></i> Copiar
                </button>
            </div>
        </div>
        
        <!-- VISUALIZAÇÃO COM DESTAQUE DE SINTAXE E CORES -->
        <div class="card-body p-3" id="visualizadorConf">
            <p class="text-muted small mb-2">
                <span class="badge bg-success me-1">Verde com Destaque:</span> Alias Ativo | 
                <span class="badge bg-warning text-dark me-1">Amarelo:</span> Comentários/Datas | 
                <span class="badge bg-secondary">Cinza Tachado:</span> Inativos
            </p>
            <div class="conf-viewer-container" id="confViewerLines">
            </div>
        </div>

        <!-- EDITOR RAW PARA SALVAMENTO DIRETO -->
        <div class="card-body p-0 d-none" id="editorConf">
            <form action="/admin/salvar_raw" method="POST" onsubmit="return pedirSenhaEConfirmar(this)">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="servidor" value="{{ servidor_atual }}">
                <input type="hidden" name="hash_original" value="{{ hash_conteudo_raw }}">
                <input type="hidden" name="senha_confirmacao" class="campo-senha-confirmacao">
                <textarea id="conteudoArquivo" name="conteudo_raw" class="editor-raw p-3" oninput="renderizarVisualizador()">{{ conteudo_raw }}</textarea>
                <div class="p-3 bg-light text-end border-top d-flex justify-content-between align-items-center">
                    <span class="text-muted small">Altere o texto e clique em salvar para gravar no servidor remoto.</span>
                    <button type="submit" class="btn btn-success fw-bold">
                        <i class="fa-solid fa-floppy-disk me-1"></i> Salvar Alterações no Arquivo Remoto
                    </button>
                </div>
            </form>
        </div>
    </div>

    <!-- Confirmação protegida para ações administrativas sensíveis. -->
    <div id="modalConfirmacaoSenha" class="app-modal" role="dialog" aria-modal="true" aria-hidden="true"
         aria-labelledby="tituloConfirmacaoSenha">
        <div class="modal-content" style="max-width:420px; margin:10vh auto; padding:24px; border-radius:10px; background:var(--cor-superficie);">
            <form id="formConfirmacaoSenha" onsubmit="confirmarSenhaAcao(event)">
                <h3 id="tituloConfirmacaoSenha" style="margin-top:0; color:var(--cor-texto);">
                    <i class="fa-solid fa-lock me-2"></i>Confirmar ação
                </h3>
                <p class="text-muted">Digite sua senha para continuar com esta ação.</p>
                <label for="senhaConfirmacaoAcao" class="form-label fw-bold">Senha:</label>
                <input type="password" id="senhaConfirmacaoAcao" class="form-control" required
                       autocomplete="current-password">
                <div id="erroConfirmacaoSenha" class="text-danger small mt-2" role="alert" aria-live="polite"></div>
                <div class="d-flex justify-end gap-2 mt-4">
                    <button type="button" class="btn btn-secondary" onclick="fecharConfirmacaoSenha()">Cancelar</button>
                    <button type="submit" class="btn btn-success fw-bold">Confirmar</button>
                </div>
            </form>
        </div>
    </div>
        
    <script>
    // --- Confirmação de senha antes de ações sensíveis (criar/editar/excluir alias, salvar .conf) ---
    // Evita que outra pessoa use a sessão aberta e esquecida de um usuário ausente do computador.
    let formularioAguardandoSenha = null;

    function pedirSenhaEConfirmar(form) {
        formularioAguardandoSenha = form;
        const modal = document.getElementById('modalConfirmacaoSenha');
        const campoSenha = document.getElementById('senhaConfirmacaoAcao');
        campoSenha.value = '';
        document.getElementById('erroConfirmacaoSenha').textContent = '';
        openModal('modalConfirmacaoSenha');
        requestAnimationFrame(() => campoSenha.focus());
        return false;
    }

    function fecharConfirmacaoSenha() {
        closeModal('modalConfirmacaoSenha');
        document.getElementById('senhaConfirmacaoAcao').value = '';
        document.getElementById('erroConfirmacaoSenha').textContent = '';
        formularioAguardandoSenha = null;
    }

    function confirmarSenhaAcao(event) {
        event.preventDefault();
        const senha = document.getElementById('senhaConfirmacaoAcao').value;
        const form = formularioAguardandoSenha;
        const campo = form && form.querySelector('.campo-senha-confirmacao');
        if (!senha) {
            document.getElementById('erroConfirmacaoSenha').textContent = 'Informe sua senha.';
            return;
        }
        if (!form || !campo) {
            document.getElementById('erroConfirmacaoSenha').textContent = 'Não foi possível confirmar esta ação.';
            return;
        }
        campo.value = senha;
        closeModal('modalConfirmacaoSenha');
        document.getElementById('senhaConfirmacaoAcao').value = '';
        formularioAguardandoSenha = null;
        HTMLFormElement.prototype.submit.call(form);
    }

    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && formularioAguardandoSenha) fecharConfirmacaoSenha();
    });

    function confirmarInativacao(form) {
        return confirm('Inativar o alias ' + form.dataset.alias + '?') && pedirSenhaEConfirmar(form);
    }

    // --- Solicitação de novo CNAME por e-mail ---
    let timeoutBuscaAlias = null;
    function agendarBuscaAlias() {
        clearTimeout(timeoutBuscaAlias);
        timeoutBuscaAlias = setTimeout(buscarAliasPorFantasia, 500);
    }
    function buscarAliasPorFantasia() {
        const fantasia = document.getElementById('cnameFantasia').value.trim();
        const campoAlias = document.getElementById('cnameAlias');
        if (fantasia.length < 3 || campoAlias.value) return; // não sobrescreve se o usuário já digitou algo

        csrfFetch('/api/lojas/buscar-alias?fantasia=' + encodeURIComponent(fantasia))
            .then(r => r.json())
            .then(data => {
                if (data.encontrado) {
                    campoAlias.value = data.alias;
                    campoAlias.style.background = '#eafaf1';
                }
            })
            .catch(() => {});
    }

    function enviarSolicitacaoCname(evento) {
        evento.preventDefault();
        const statusDiv = document.getElementById('statusSolicitarCname');
        const fantasia = document.getElementById('cnameFantasia').value.trim();
        let alias = document.getElementById('cnameAlias').value.trim();

        if (!fantasia) {
            alert('Informe o nome fantasia da empresa.');
            return false;
        }
        if (!alias) {
            alias = prompt('Não encontrei o alias automaticamente. Digite o alias desta empresa:') || '';
            document.getElementById('cnameAlias').value = alias;
        }

        statusDiv.className = 'text-muted small';
        statusDiv.textContent = 'Enviando...';

        csrfFetch('/admin/solicitar-cname', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fantasia, alias })
        })
        .then(r => r.json())
        .then(data => {
            if (data.sucesso) {
                statusDiv.className = 'alert alert-success p-2 mb-0';
                statusDiv.textContent = data.mensagem;
                document.getElementById('formSolicitarCname').reset();
            } else {
                statusDiv.className = 'alert alert-danger p-2 mb-0';
                statusDiv.textContent = data.mensagem;
            }
        })
        .catch(() => {
            statusDiv.className = 'alert alert-danger p-2 mb-0';
            statusDiv.textContent = 'Não foi possível enviar. Verifique sua conexão.';
        });

        return false;
    }

    // --- Ordenação por título de coluna na tabela "Registros de Databases" ---
    function ordenarTabelaRegistros(colIndex, tipo) {
        const tbody = document.querySelector('#tabelaBancos tbody');
        if (!tbody) return;
        const linhas = Array.from(tbody.querySelectorAll('tr'));
        const mesmaColuna = tbody.dataset.ordCol == colIndex;
        const asc = mesmaColuna ? tbody.dataset.ordDir !== 'asc' : true;

        linhas.sort((a, b) => {
            let va = (a.children[colIndex].innerText || '').trim();
            let vb = (b.children[colIndex].innerText || '').trim();
            if (tipo === 'data') {
                // dd/mm/aaaa -> aaaammdd, para ordenar cronologicamente e não por texto
                const partsA = va.split('/'); const partsB = vb.split('/');
                va = partsA.length === 3 ? partsA[2] + partsA[1] + partsA[0] : va;
                vb = partsB.length === 3 ? partsB[2] + partsB[1] + partsB[0] : vb;
            } else {
                va = va.toLowerCase(); vb = vb.toLowerCase();
            }
            if (va < vb) return asc ? -1 : 1;
            if (va > vb) return asc ? 1 : -1;
            return 0;
        });

        linhas.forEach(l => tbody.appendChild(l));
        tbody.dataset.ordCol = colIndex;
        tbody.dataset.ordDir = asc ? 'asc' : 'desc';
    }

    // 2. Edição em Linha com Botões Salvar e Cancelar
    function editarLinha(id) {
        const tr = document.getElementById(`tr-${id}`);
        const aliasTd = tr.querySelector('.val-alias');
        const acoesTd = tr.querySelector('.val-acoes');
        
        const originalAlias = tr.getAttribute('data-original-alias');
        if (!acoesTd._conteudoOriginal) acoesTd._conteudoOriginal = acoesTd.cloneNode(true);

        // Cria input no campo de alias
        const inputAlias = document.createElement('input');
        inputAlias.type = 'text';
        inputAlias.id = `input-alias-${id}`;
        inputAlias.className = 'form-control form-control-sm';
        inputAlias.value = originalAlias;
        inputAlias.addEventListener('input', () => atualizarCaminhoEdicaoLinha(id));
        aliasTd.replaceChildren(inputAlias);

        // Troca os botões de ação para [Salvar] e [Cancelar]
        acoesTd.innerHTML = `
            <button class="btn btn-sm btn-success me-1" onclick="salvarEdicaoLinha(${id})">
                <i class="fa-solid fa-check"></i> Salvar
            </button>
            <button class="btn btn-sm btn-secondary" onclick="cancelarEdicaoLinha(${id})">
                <i class="fa-solid fa-xmark"></i> Cancelar
            </button>
        `;

        document.getElementById(`input-alias-${id}`).focus();
    }

    function atualizarCaminhoEdicaoLinha(id) {
        const tr = document.getElementById(`tr-${id}`);
        const inputAlias = document.getElementById(`input-alias-${id}`);
        const valCaminhoTd = tr.querySelector('.val-caminho');
        const servidor = document.getElementById('servidor').value;
        const novoAlias = inputAlias.value.trim();

        if (!novoAlias) return;

        let novoCaminho = `/opt/infobrasil/${novoAlias}/dados.fdb`;
        if (servidor === 'DB05') {
            novoCaminho = `/opt/infobrasil/3.0/${novoAlias}/dados.fdb`;
        }
        const code = document.createElement('code');
        code.textContent = novoCaminho;
        valCaminhoTd.replaceChildren(code);
    }

    function cancelarEdicaoLinha(id) {
        const tr = document.getElementById(`tr-${id}`);
        const aliasTd = tr.querySelector('.val-alias');
        const caminhoTd = tr.querySelector('.val-caminho');
        const acoesTd = tr.querySelector('.val-acoes');

        const originalAlias = tr.getAttribute('data-original-alias');
        const originalCaminho = tr.getAttribute('data-original-caminho');

        // Restaura os valores originais
        const aliasSpan = document.createElement('span');
        aliasSpan.className = 'alias-text';
        aliasSpan.textContent = originalAlias;
        aliasTd.replaceChildren(aliasSpan);
        const caminhoCode = document.createElement('code');
        caminhoCode.textContent = originalCaminho;
        caminhoTd.replaceChildren(caminhoCode);

        // Restaura botões padrão
        if (acoesTd._conteudoOriginal) {
            acoesTd.replaceChildren(...Array.from(acoesTd._conteudoOriginal.childNodes, no => no.cloneNode(true)));
        }
    }

    function salvarEdicaoLinha(id) {
        const tr = document.getElementById(`tr-${id}`);
        const inputAlias = document.getElementById(`input-alias-${id}`);
        const acoesTd = tr.querySelector('.val-acoes');
        const novoAlias = inputAlias.value.trim();
        const originalAlias = tr.getAttribute('data-original-alias');
        const servidor = document.getElementById('servidor').value;

        if (!novoAlias) {
            alert('Por favor, informe o nome do Alias.');
            return;
        }

        let novoCaminho = `/opt/infobrasil/${novoAlias}/dados.fdb`;
        if (servidor === 'DB05') {
            novoCaminho = `/opt/infobrasil/3.0/${novoAlias}/dados.fdb`;
        }

        // Atualiza no textarea de configuração
        const areaTexto = document.getElementById('conteudoArquivo');
        let texto = areaTexto.value;
        
        const aliasRegexSeguro = originalAlias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regexAlias = new RegExp('^(\\s*' + aliasRegexSeguro + '\\s*=.*)$', 'm');
        if (regexAlias.test(texto)) {
            texto = texto.replace(regexAlias, () => novoAlias + ' = ' + novoCaminho);
            areaTexto.value = texto;
            renderizarVisualizador();
        }

        // Atualiza os dados na tabela e salva novo estado original
        tr.setAttribute('data-original-alias', novoAlias);
        tr.setAttribute('data-original-caminho', novoCaminho);
        if (acoesTd._conteudoOriginal) {
            const formOriginal = acoesTd._conteudoOriginal.querySelector('form');
            if (formOriginal) {
                formOriginal.dataset.alias = novoAlias;
                const campoAlias = formOriginal.querySelector('input[name="alias"]');
                if (campoAlias) campoAlias.value = novoAlias;
            }
        }
        cancelarEdicaoLinha(id);

        const secaoArquivo = document.getElementById('secaoArquivo');
        secaoArquivo.scrollIntoView({ behavior: 'smooth' });
        alert(`Alias atualizado para "${novoAlias}". Clique em "Salvar Alterações no Arquivo Remoto" abaixo para confirmar a gravação.`);
        
        document.getElementById('visualizadorConf').style.display = 'none';
        document.getElementById('editorConf').classList.remove('d-none');
        document.getElementById('editorConf').style.display = 'block';
        document.getElementById('btnModo').innerHTML = '<i class="fa-solid fa-eye"></i> Visualizar com Cores';
    }

    // 3. Renderização com Destaque de Cores para Linhas do databases.conf
    function renderizarVisualizador() {
        const texto = document.getElementById('conteudoArquivo').value;
        const container = document.getElementById('confViewerLines');
        container.innerHTML = '';

        const linhas = texto.split('\n');
        linhas.forEach((linha, idx) => {
            const linhaLimpa = linha.trim();
            const div = document.createElement('span');
            div.className = 'conf-line';

            if (!linhaLimpa) {
                div.innerHTML = '&nbsp;';
            } else if (linhaLimpa.startsWith('#')) {
                if (linhaLimpa.includes('INATIVADO') || linhaLimpa.includes('=')) {
                    div.className += ' conf-line-inativo';
                    div.textContent = linha;
                } else {
                    div.className += ' conf-line-comentario';
                    div.textContent = linha;
                }
            } else if (linhaLimpa.includes('=')) {
                // ALIAS ATIVO -> DESTAQUE EM VERDE/CIANO
                div.className += ' conf-line-ativo';
                div.textContent = linha;
            } else {
                div.textContent = linha;
            }
            container.appendChild(div);
        });
    }

    function alternarModoEdicao() {
        const visualizador = document.getElementById('visualizadorConf');
        const editor = document.getElementById('editorConf');
        const btn = document.getElementById('btnModo');
        const editorOculto = editor.classList.contains('d-none') || window.getComputedStyle(editor).display === 'none';

        if (editorOculto) {
            visualizador.style.display = 'none';
            editor.classList.remove('d-none');
            editor.style.display = 'block';
            btn.innerHTML = '<i class="fa-solid fa-eye"></i> Visualizar com Cores';
        } else {
            renderizarVisualizador();
            editor.style.display = 'none';
            editor.classList.add('d-none');
            visualizador.style.display = 'block';
            btn.innerHTML = '<i class="fa-solid fa-pen"></i> Abrir Modo Edição Direta';
        }
    }

    function copiarConteudo() {
        const areaTexto = document.getElementById('conteudoArquivo');
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(areaTexto.value).then(() => {
                alert('Conteúdo do databases.conf copiado para a área de transferência!');
            }).catch(() => {
                areaTexto.select();
                document.execCommand('copy');
                alert('Conteúdo copiado!');
            });
        } else {
            areaTexto.select();
            document.execCommand('copy');
            alert('Conteúdo copiado!');
        }
    }

    document.addEventListener("DOMContentLoaded", function() {
        renderizarVisualizador();
    });
    </script>

    <!-- Modal Perfil Completo -->
    <div id="modalPerfil" class="app-modal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="tituloModalPerfil">
        <div class="modal-content" style="max-width: 450px; margin: 5% auto; padding: 25px; border-radius: 8px; background: var(--cor-superficie);">
            <h3 id="tituloModalPerfil" style="margin-top:0; color: var(--cor-texto);"><i class="fa-solid fa-id-card me-2"></i>Meu Perfil</h3>
            
            <form id="formPerfil" onsubmit="salvarPerfil(event)">
                <div class="form-group" style="margin-bottom: 12px;">
                    <label class="form-label fw-bold">Nome:</label>
                    <input type="text" id="perfilNome" class="form-control" value="{{ usuario_nome }}" required>
                </div>
                
                <div class="form-group" style="margin-bottom: 15px;">
                    <label class="form-label fw-bold">E-mail:</label>
                    <input type="email" id="perfilEmail" class="form-control" value="{{ usuario_email }}" readonly disabled style="background-color: var(--cor-fundo);">
                </div>

                <hr class="divider" style="margin: 15px 0;">
                {% if veio_de_reset %}
                    <p class="text-success" style="font-size: 13px; margin-bottom: 10px;">✅ Você acessou por um link de redefinição de senha — pode definir a nova senha sem informar a atual.</p>
                {% else %}
                    <p class="text-muted" style="font-size: 13px; margin-bottom: 10px;">Preencha os campos abaixo apenas se desejar alterar a senha:</p>
                {% endif %}

                <div id="grupoSenhaAtual" class="form-group" style="margin-bottom: 12px; {% if veio_de_reset %}display:none;{% endif %}">
                    <label class="form-label fw-bold">Senha Atual:</label>
                    <input type="password" id="perfilSenhaAtual" class="form-control" placeholder="Informe para autorizar mudanças">
                </div>

                <div class="form-group" style="margin-bottom: 12px;">
                    <label class="form-label fw-bold">Nova Senha:</label>
                    <input type="password" id="perfilNovaSenha" class="form-control">
                </div>

                <div class="form-group" style="margin-bottom: 20px;">
                    <label class="form-label fw-bold">Confirmar Nova Senha:</label>
                    <input type="password" id="perfilConfirmaSenha" class="form-control">
                </div>

                <div class="d-flex justify-end gap-2">
                    <button type="button" class="btn btn-secondary" onclick="fecharModalPerfil()">Cancelar</button>
                    <button type="submit" class="btn btn-success fw-bold">Salvar Alterações</button>
                </div>
            </form>
        </div>
    </div>

    <script>
    function abrirModalPerfil() {
        openModal('modalPerfil');
    }
    function fecharModalPerfil() {
        closeModal('modalPerfil');
    }

    // --- Salva as permissões (checkboxes) de um usuário, sem recarregar a página ---
    function salvarPermissoes(evento, userId) {
        evento.preventDefault();
        const form = evento.target;
        const payload = { user_id: userId };
        form.querySelectorAll('input[type="checkbox"]').forEach(chk => { payload[chk.name] = chk.checked; });

        csrfFetch('/admin/usuarios/permissoes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(r => r.json())
        .then(data => {
            const botao = form.querySelector('button[type="submit"]');
            if (data.sucesso) {
                botao.textContent = '✅';
                setTimeout(() => { botao.textContent = '💾'; }, 1500);
            } else {
                alert('Erro ao salvar permissões: ' + (data.mensagem || 'tente novamente.'));
            }
        })
        .catch(() => alert('Não foi possível salvar. Verifique sua conexão.'));

        return false;
    }

    function toggleSenhaApi() {
        const input = document.getElementById('api_senha');
        const icone = document.getElementById('iconeSenhaApi');
        if (!input || !icone) return;
        if (input.type === 'password') {
            input.type = 'text';
            icone.className = 'fa-solid fa-eye-slash';
        } else {
            input.type = 'password';
            icone.className = 'fa-solid fa-eye';
        }
    }

    function mostrarFeedbackApi(mensagem, tipo = 'info') {
        const div = document.getElementById('feedbackApi');
        if (!div) return;
        div.className = `alert alert-${tipo} mt-3 mb-0 small py-2`;
        div.textContent = mensagem;
        div.classList.remove('d-none');
    }

    function salvarConfigApi(e) {
        if (e) e.preventDefault();
        const url = (document.getElementById('api_url')?.value || '').trim();
        const usuario = (document.getElementById('api_usuario')?.value || '').trim();
        const senha = (document.getElementById('api_senha')?.value || '').trim();
        const btn = document.getElementById('btnSalvarApi');
        if (btn) btn.disabled = true;

        csrfFetch('/admin/config-api', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, usuario, senha })
        })
        .then(async r => {
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok, status: r.status, data };
        })
        .then(({ ok, status, data }) => {
            if (btn) btn.disabled = false;
            const msg = data.mensagem || data.erro || data.error || (ok ? 'Configurações salvas no arquivo .env com sucesso!' : `Erro no servidor (${status})`);
            if (ok && data.sucesso !== false) {
                mostrarFeedbackApi(msg, 'success');
            } else {
                mostrarFeedbackApi('Erro ao salvar no .env: ' + msg, 'danger');
            }
        })
        .catch(err => {
            if (btn) btn.disabled = false;
            mostrarFeedbackApi('Erro de comunicação ao salvar: ' + (err.message || err), 'danger');
        });
        return false;
    }

    function testarConexaoApi() {
        const url = (document.getElementById('api_url')?.value || '').trim();
        const usuario = (document.getElementById('api_usuario')?.value || '').trim();
        const senha = (document.getElementById('api_senha')?.value || '').trim();
        const btn = document.getElementById('btnTestarApi');
        if (btn) btn.disabled = true;
        mostrarFeedbackApi('Testando autenticação e consulta de dados na API InfoBrasil...', 'info');

        csrfFetch('/admin/config-api/testar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, usuario, senha })
        })
        .then(async r => {
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok, status: r.status, data };
        })
        .then(({ ok, status, data }) => {
            if (btn) btn.disabled = false;
            const msg = data.mensagem || data.erro || data.error || (ok ? 'Conexão testada com sucesso!' : `Erro no servidor (${status})`);
            if (ok && data.sucesso) {
                mostrarFeedbackApi('✅ ' + msg, 'success');
            } else {
                mostrarFeedbackApi('❌ Falha no teste: ' + msg, 'danger');
            }
        })
        .catch(err => {
            if (btn) btn.disabled = false;
            mostrarFeedbackApi('❌ Erro de requisição: ' + (err.message || err), 'danger');
        });
    }

    function sincronizarApiAgora() {
        if (!confirm('Deseja iniciar a sincronização de lojas com a API InfoBrasil agora?')) return;
        const btn = document.getElementById('btnSincronizarApi');
        if (btn) btn.disabled = true;
        mostrarFeedbackApi('Sincronização em andamento... Buscando clientes na API e atualizando base local...', 'info');

        csrfFetch('/admin/config-api/sincronizar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        })
        .then(async r => {
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok, status: r.status, data };
        })
        .then(({ ok, status, data }) => {
            if (btn) btn.disabled = false;
            const msg = data.mensagem || data.erro || data.error || (ok ? 'Sincronização concluída!' : `Erro no servidor (${status})`);
            if (ok && data.sucesso) {
                mostrarFeedbackApi('✅ ' + msg, 'success');
                const statusDiv = document.getElementById('statusExecucaoApi');
                if (statusDiv) {
                    statusDiv.innerHTML = `<span class="badge bg-success me-1">SUCESSO</span> Atualizado agora (${data.atualizados ?? 0} lojas)`;
                }
            } else {
                mostrarFeedbackApi('❌ Falha na sincronização: ' + msg, 'danger');
            }
        })
        .catch(err => {
            if (btn) btn.disabled = false;
            mostrarFeedbackApi('❌ Erro durante sincronização: ' + (err.message || err), 'danger');
        });
    }

    const veioDeReset = {{ 'true' if veio_de_reset else 'false' }};

    function salvarPerfil(e) {
        e.preventDefault();
        const nome = document.getElementById('perfilNome').value;
        const email = document.getElementById('perfilEmail').value;
        const senhaAtual = document.getElementById('perfilSenhaAtual').value;
        const novaSenha = document.getElementById('perfilNovaSenha').value;
        const confirmaSenha = document.getElementById('perfilConfirmaSenha').value;

        if (!veioDeReset && (novaSenha || senhaAtual)) {
            if (!senhaAtual) {
                alert("Informe a senha atual para autorizar as alterações.");
                return;
            }
        }
        if (novaSenha || confirmaSenha) {
            if (novaSenha !== confirmaSenha) {
                alert("A nova senha e a confirmação não coincidem!");
                return;
            }
        }

        csrfFetch('/admin/perfil/atualizar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ nome, email, senhaAtual, novaSenha })
        })
        .then(r => r.json().then(data => ({ status: r.status, data })))
        .then(({ status, data }) => {
            if (data.sucesso) {
                alert("Perfil atualizado com sucesso!");
                fecharModalPerfil();
                location.reload();
            } else {
                alert("Erro: " + data.mensagem);
            }
        })
        .catch(() => alert("Não foi possível salvar. Verifique sua conexão e tente novamente."));
    }

    // Abre a modal automaticamente caso receba a mensagem para alterar a senha
    window.onload = function() {
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.has('sucesso') && urlParams.get('sucesso').includes('Altere sua senha')) {
            abrirModalPerfil();
        }
    };
    </script>

    <!-- FIX: Bundle JS do Bootstrap (faltava) - necessário para os modais (ex: "Excluir" usuário) funcionarem -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

</body>
</html>
"""


HTML_REDEFINIR = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Redefinir Senha</title>
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <script src="{{ url_for('static', filename='csrf.js') }}"></script>
    <link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
    <script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <main id="conteudo-principal" class="d-flex justify-center items-center" style="min-height: 100vh; padding: var(--space-4);">
        <div class="card" style="width: 100%; max-width: 400px; padding: var(--space-6);">
            <h4 class="text-center" style="margin: 0 0 var(--space-5);">Redefinir Senha</h4>
            <form id="formRedefinir" onsubmit="executarRedefinicao(event)">
                <input type="hidden" id="tokenUrl">

                <div class="form-group" style="margin-bottom: var(--space-4);">
                    <label class="form-label font-weight-bold">Token de Validação:</label>
                    <input type="text" id="tokenExibicao" class="form-control" readonly disabled>
                </div>

                <div class="form-group" style="margin-bottom: var(--space-4);">
                    <label class="form-label">Nova Senha:</label>
                    <input type="password" id="resetNovaSenha" class="form-control" required>
                </div>

                <div class="form-group" style="margin-bottom: var(--space-4);">
                    <label class="form-label">Confirmar Nova Senha:</label>
                    <input type="password" id="resetConfirmaSenha" class="form-control" required>
                </div>

                <button type="submit" class="btn btn-solid btn-full">Alterar Senha</button>
            </form>
        </div>
    </main>

    <script>
        const params = new URLSearchParams(window.location.search);
        const token = params.get('token') || '';
        
        document.getElementById('tokenUrl').value = token;
        document.getElementById('tokenExibicao').value = token;

        if (!token) {
            alert("Token inválido ou ausente no link!");
        }

        function executarRedefinicao(e) {
            e.preventDefault();
            const novaSenha = document.getElementById('resetNovaSenha').value;
            const confirmaSenha = document.getElementById('resetConfirmaSenha').value;

            if (novaSenha !== confirmaSenha) {
                alert("As senhas não coincidem!");
                return;
            }

            csrfFetch('/admin/redefinir-senha/confirmar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: token, novaSenha: novaSenha })
            })
            .then(r => r.json())
            .then(data => {
                if (data.sucesso) {
                    alert("Senha alterada com sucesso! Redirecionando para o login...");
                    window.location.href = "/admin/login";
                } else {
                    alert("Erro: " + data.mensagem);
                }
            });
        }
    </script>
</body>
</html>
"""


# --- LOGIN ADMIN TEMPLATE ---
# --- TEMPLATE HTML LOGIN ---
HTML_LOGIN = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Acesso Admin</title>
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <script src="{{ url_for('static', filename='csrf.js') }}"></script>
    <link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
    <script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <main id="conteudo-principal" class="d-flex justify-center items-center" style="min-height: 100vh; padding: var(--space-4);">
        <div class="card" style="width: 100%; max-width: 380px; padding: var(--space-6);">
            <h3 class="text-center" style="margin: 0 0 var(--space-1);">🔐 Acesso Admin</h3>
            <p class="text-center text-muted" style="font-size: var(--font-size-sm); margin: 0 0 var(--space-5);">Gestão dos arquivos databases.conf</p>
            
            {% if erro %}
                <div class="alert alert-danger text-center" style="margin-bottom: var(--space-4);">{{ erro }}</div>
            {% endif %}
            {% if sucesso %}
                <div class="alert alert-success text-center" style="margin-bottom: var(--space-4);">{{ sucesso }}</div>
            {% endif %}

            <form method="POST" action="/admin/login">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="next" value="{{ next or '' }}">
                <div class="form-group" style="margin-bottom: var(--space-4);">
                    <label class="form-label fw-bold">E-mail:</label>
                    <input type="email" name="email" class="form-control" required>
                </div>
                <div class="form-group" style="margin-bottom: var(--space-4);">
                    <label class="form-label">Senha:</label>
                    <input type="password" name="senha" class="form-control" required>
                    <div class="text-right" style="margin-top: var(--space-1);">
                        <a href="/admin/esqueci-senha" class="text-primary" style="font-size: var(--font-size-xs); text-decoration: none;">Esqueci a senha</a>
                    </div>
                </div>

                <button type="submit" class="btn btn-solid btn-full" style="margin-top: var(--space-2);">Entrar</button>
                {% if allow_register %}
                <a href="/admin/register" class="btn btn-primary btn-full" style="margin-top: var(--space-2);">Criar Nova Conta</a>
                {% endif %}
            </form>
        </div>
    </main>
</body>
</html>
"""

# --- TEMPLATE HTML ESQUECI SENHA ---
HTML_ESQUECI_SENHA = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Recuperar Senha</title>
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <script src="{{ url_for('static', filename='csrf.js') }}"></script>
    <link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
    <script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <main id="conteudo-principal" class="d-flex justify-center items-center" style="min-height: 100vh; padding: var(--space-4);">
        <div class="card" style="width: 100%; max-width: 380px; padding: var(--space-6);">
            <h3 class="text-center" style="margin: 0 0 var(--space-1);">🔑 Recuperar Senha</h3>
            <p class="text-center text-muted" style="font-size: var(--font-size-sm); margin: 0 0 var(--space-5);">Informe seu e-mail cadastrado para receber o link de redefinição.</p>

            {% if erro %}<div class="alert alert-danger text-center" style="margin-bottom: var(--space-4);">{{ erro }}</div>{% endif %}
            {% if sucesso %}<div class="alert alert-success text-center" style="margin-bottom: var(--space-4);">{{ sucesso }}</div>{% endif %}

            <form method="POST" action="/admin/esqueci-senha">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <div class="form-group" style="margin-bottom: var(--space-4);">
                    <label class="form-label fw-bold">E-mail:</label>
                    <input type="email" name="email" class="form-control" required>
                </div>
                <button type="submit" class="btn btn-solid btn-full" style="margin-top: var(--space-2);">Enviar link de redefinição</button>
                <a href="/admin/login" class="btn btn-primary btn-full" style="margin-top: var(--space-2);">⬅ Voltar ao Login</a>
            </form>
        </div>
    </main>
</body>
</html>
"""

# --- TEMPLATE HTML CADASTRO ---
HTML_REGISTER = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8"><title>Cadastro de Usuário</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--cor-fundo); display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card-register { background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: 35px 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); width: 380px; }
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; color: var(--cor-texto-invertido) !important; }
        .btn-success:hover { background-color: var(--cor-primaria-escura) !important; border-color: var(--cor-primaria-escura) !important; }
        .btn-outline-secondary { color: var(--cor-texto-suave) !important; border-color: var(--cor-borda) !important; }
        .btn-outline-secondary:hover { background-color: #f1f5f9 !important; color: var(--cor-texto) !important; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <!-- Skip link para acessibilidade -->
    <a href="#conteudo-principal" class="skip-link">Pular para o conteúdo principal</a>

    <!-- Overlay para focus trap em modais -->
    <div class="focus-trap-overlay" aria-hidden="true"></div>

    <main id="conteudo-principal" class="d-flex justify-center items-center" style="min-height: 100vh; padding: var(--space-4);">
        <div class="card" style="width: 100%; max-width: 380px; padding: var(--space-6);">
            {% if pendente %}
                <h3 class="text-center" style="margin: 0 0 var(--space-3);">⏳ Cadastro Recebido</h3>
                <div class="alert alert-warning text-center" style="margin-bottom: var(--space-4);">
                    Sua conta foi criada com sucesso, mas está <strong>aguardando aprovação</strong> do administrador.<br>
                    Você receberá acesso assim que sua conta for ativada.
                </div>
                <a href="/admin/login" class="btn btn-primary btn-full">⬅ Voltar ao Login</a>
            {% else %}
                <h3 class="text-center" style="margin: 0 0 var(--space-3);">📝 Cadastro de Usuário</h3>
                {% if erro %}<div class="alert alert-danger" style="margin-bottom: var(--space-3);">{{ erro }}</div>{% endif %}
                <form method="POST" action="/admin/register">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <div class="form-group" style="margin-bottom: var(--space-4);">
                        <label class="form-label fw-bold">Nome:</label>
                        <input type="text" name="nome" class="form-control" required>
                    </div>
                    <div class="form-group" style="margin-bottom: var(--space-4);">
                        <label class="form-label fw-bold">E-mail:</label>
                        <input type="email" name="email" class="form-control" required>
                    </div>
                    <div class="form-group" style="margin-bottom: var(--space-4);">
                        <label class="form-label fw-bold">Senha:</label>
                        <input type="password" name="senha" class="form-control" required>
                    </div>
                    <div class="form-group" style="margin-bottom: var(--space-4);">
                        <label class="form-label fw-bold">Confirmar Senha:</label>
                        <input type="password" name="confirmar_senha" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-solid btn-full">Salvar Cadastro</button>
                    <a href="/admin/login" class="btn btn-primary btn-full" style="margin-top: var(--space-2);">⬅ Voltar ao Login</a>
                </form>
            {% endif %}
        </div>
    </main>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# ROTAS DE AUTENTICAÇÃO E CADASTRO
# -----------------------------------------------------------------------------

@bancos_bp.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if login_bloqueado():
            registrar_login_falha(request.form.get('email', ''), 'rate_limit')
            return render_template_string(HTML_LOGIN, erro="Muitas tentativas. Aguarde alguns minutos e tente de novo.")

        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT id, nome, senha_hash, ativo, eh_master FROM usuarios WHERE LOWER(email) = ?", 
            (email,)
        )
        user = cursor.fetchone()
        conn.close()

        if not user:
            registrar_falha_login()
            registrar_login_falha(email, 'usuario_inexistente')
            return render_template_string(HTML_LOGIN, erro="Credenciais inválidas.")

        user_id, nome, senha_hash, ativo, eh_master = user
        senha_valida = check_password_hash(senha_hash, senha)

        if not senha_valida:
            registrar_falha_login()
            registrar_login_falha(email, 'senha_invalida')
            return render_template_string(HTML_LOGIN, erro="Credenciais inválidas.")

        if not ativo and not eh_master:
            registrar_login_falha(email, 'conta_inativa')
            return render_template_string(HTML_LOGIN, erro="Sua conta ainda não foi ativada.")

        limpar_falhas_login()
        session.clear()
        session.permanent = True
        session['user_id'] = user_id
        session['user_nome'] = nome_para_assinatura(nome)
        session['eh_master'] = eh_master
        session['logged_in'] = True
        
        # Regenera ID da sessão para prevenir session fixation (Opção 3)
        regenerar_session_id()
        
        # Registra login bem-sucedido na auditoria (Opção 3)
        registrar_login_sucesso(user_id, nome, eh_master)

        destino = destino_redirect_seguro(request.args.get('next') or request.form.get('next'))
        return redirect(destino)

    return render_template_string(HTML_LOGIN, next=request.args.get('next', ''))


@bancos_bp.route('/admin/esqueci-senha', methods=['GET', 'POST'])
def esqueci_senha():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()

        conn = get_db_connection()
        usuario = conn.execute("SELECT * FROM usuarios WHERE LOWER(email) = ?", (email,)).fetchone()

        # Mensagem sempre genérica, para não revelar quais e-mails existem na base
        msg_generica = "Se este e-mail estiver cadastrado, um link de redefinição foi enviado."

        if not usuario or not usuario['ativo']:
            conn.close()
            return render_template_string(HTML_ESQUECI_SENHA, sucesso=msg_generica)

        token_reset = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE usuarios SET token_ativacao = ?, token_expira = ? WHERE id = ?",
            (token_reset, token_expira_em(), usuario['id'])
        )
        conn.commit()
        conn.close()

        link_reset = url_for('bancos.redefinir_senha_token', token=token_reset, _external=True)

        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = usuario['email']
        msg['Subject'] = "Recuperação de Senha"
        corpo_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                <h2>Olá, {usuario['nome']}!</h2>
                <p>Recebemos uma solicitação para redefinir a senha da sua conta.</p>
                <p>Clique no botão abaixo para acessar o painel e definir uma nova senha:</p>
                <p style="margin: 20px 0;">
                    <a href="{link_reset}"
                       style="background-color: #27ae60; color: white; padding: 12px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">
                       Redefinir Minha Senha Agora
                    </a>
                </p>
                <p style="font-size: 0.85em; color: #7f8c8d;">
                    Se você não solicitou isso, ignore este e-mail.<br>
                    Ou copie este link no seu navegador:<br>
                    <a href="{link_reset}">{link_reset}</a>
                </p>
            </body>
        </html>
        """
        msg.attach(MIMEText(corpo_html, 'html'))

        try:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [usuario['email']], msg.as_string())
            server.quit()
        except Exception as e_smtp:
            # Registra o erro internamente sem expor detalhes ao usuário final
            LOGGER.warning('Falha ao enviar e-mail de recuperação de senha: %s', e_smtp)

        return render_template_string(HTML_ESQUECI_SENHA, sucesso=msg_generica)

    return render_template_string(HTML_ESQUECI_SENHA)


@bancos_bp.route('/admin/register', methods=['GET', 'POST'])
def admin_register():
    if not ALLOW_PUBLIC_REGISTER:
        return render_template_string(HTML_LOGIN, erro="Cadastro público desativado. Peça acesso ao administrador Master.")

    if request.method == 'GET':
        return render_template_string(HTML_REGISTER)

    nome = request.form.get('nome')
    email = request.form.get('email')
    senha = request.form.get('senha')
    confirmar_senha = request.form.get('confirmar_senha')

    if senha != confirmar_senha:
        return render_template_string(HTML_REGISTER, erro="As senhas não coincidem!")

    ok_senha, msg_senha = senha_atende_politica(senha)
    if not ok_senha:
        return render_template_string(HTML_REGISTER, erro=msg_senha)

    with closing(get_db_connection()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email,))
        if cursor.fetchone():
            return render_template_string(HTML_REGISTER, erro="E-mail já cadastrado!")

        senha_hash = generate_password_hash(senha)
        token = secrets.token_urlsafe(32)
        eh_master = 0
        ativo = 0

        cursor.execute('''
            INSERT INTO usuarios (nome, email, senha_hash, token_ativacao, ativo, eh_master)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (nome, email, senha_hash, token, ativo, eh_master))
        conn.commit()

    if eh_master:
        return render_template_string(HTML_LOGIN, sucesso="Cadastro realizado! Faça login para continuar.")

    # Notifica o(s) usuário(s) Master por e-mail sobre o novo cadastro pendente de aprovação
    try:
        conn = get_db_connection()
        masters = conn.execute("SELECT email FROM usuarios WHERE eh_master = 1").fetchall()
        conn.close()
        emails_master = [m['email'] for m in masters if m['email']]

        if emails_master:
            link_admin = url_for('bancos.admin_painel', _external=True)
            msg = MIMEMultipart()
            msg['From'] = SMTP_USER
            msg['To'] = ", ".join(emails_master)
            msg['Subject'] = "Novo cadastro aguardando aprovação"
            corpo_html = f"""
            <html>
                <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
                    <h2>Novo usuário cadastrado</h2>
                    <p>Um novo usuário se cadastrou no InfoMonitorDBClientes e está aguardando aprovação:</p>
                    <ul>
                        <li><strong>Nome:</strong> {nome}</li>
                        <li><strong>E-mail:</strong> {email}</li>
                    </ul>
                    <p style="margin: 20px 0;">
                        <a href="{link_admin}"
                           style="background-color: #27ae60; color: white; padding: 12px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">
                           Ativar no Painel
                        </a>
                    </p>
                </body>
            </html>
            """
            msg.attach(MIMEText(corpo_html, 'html'))

            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, emails_master, msg.as_string())
            server.quit()
    except Exception as e:
        # Não impede o cadastro caso o envio de e-mail falhe
        print(f"Erro ao notificar master sobre novo cadastro: {e}")

    return render_template_string(HTML_REGISTER, pendente=True)

    

@bancos_bp.route('/admin/logout', methods=['POST'])
def admin_logout():
    user_id = session.get('user_id')
    user_nome = session.get('user_nome')
    session.clear()
    if user_id and user_nome:
        registrar_logout(user_id, user_nome)
    return redirect('/')


# -----------------------------------------------------------------------------
# ROTAS GESTÃO E PERFIL (PAINEL ADMIN)
# -----------------------------------------------------------------------------

@bancos_bp.route('/admin/perfil/alterar-senha', methods=['POST'])
def alterar_senha_perfil():
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/admin?erro=Sessão expirada. Faça login novamente.')

    senha_atual = request.form.get('senha_atual') or request.form.get('senha_atual_perfil')
    nova_senha = request.form.get('nova_senha')
    confirma_senha = request.form.get('confirma_senha')

    if not nova_senha or nova_senha != confirma_senha:
        return redirect('/admin?erro=As senhas digitadas não coincidem!')

    ok_senha, msg_senha = senha_atende_politica(nova_senha)
    if not ok_senha:
        return redirect(f'/admin?erro={msg_senha}')

    conn = get_db_connection()
    usuario = conn.execute("SELECT senha_hash FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    if not usuario or not senha_atual or not check_password_hash(usuario['senha_hash'], senha_atual):
        conn.close()
        return redirect('/admin?erro=Senha atual incorreta.')

    senha_hash = generate_password_hash(nova_senha)
    conn.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (senha_hash, user_id))
    conn.commit()
    conn.close()

    return redirect('/admin?sucesso=Sua senha foi alterada com sucesso!')

@bancos_bp.route('/admin/perfil', methods=['POST'])
def admin_perfil():
    if not session.get('logged_in'):
        return redirect('/admin/login')

    nova_senha = request.form.get('nova_senha')
    confirmar_senha = request.form.get('confirmar_nova_senha')
    senha_atual = request.form.get('senha_atual') or request.form.get('senha_atual_perfil')

    if nova_senha:
        if nova_senha != confirmar_senha:
            flash('As senhas não coincidem!', 'danger')
            return redirect('/admin')

        ok_senha, msg_senha = senha_atende_politica(nova_senha)
        if not ok_senha:
            flash(msg_senha, 'danger')
            return redirect('/admin')

        conn = get_db_connection()
        usuario = conn.execute(
            'SELECT senha_hash FROM usuarios WHERE id = ?', (session['user_id'],)
        ).fetchone()
        veio_de_reset = session.get('reset_senha_pendente', False)
        if not usuario or (not veio_de_reset and (
                not senha_atual or not check_password_hash(usuario['senha_hash'], senha_atual))):
            conn.close()
            flash('Senha atual incorreta.', 'danger')
            return redirect('/admin')

        senha_hash = generate_password_hash(nova_senha)
        conn.execute('UPDATE usuarios SET senha_hash = ? WHERE id = ?', (senha_hash, session['user_id']))
        conn.commit()
        conn.close()
        session.pop('reset_senha_pendente', None)
        flash('Senha alterada com sucesso!', 'success')

    return redirect('/admin')
    
@bancos_bp.route('/admin/usuarios/toggle/<int:user_id>', methods=['POST'])
def toggle_usuario(user_id):
    # Apenas o usuário Master tem permissão para alterar status
    if not session.get('logged_in') or not session.get('eh_master'):
        return "Acesso Negado", 403

    conn = get_db_connection()
    user = conn.execute('SELECT ativo FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    if user:
        novo_status = 0 if user['ativo'] else 1
        conn.execute('UPDATE usuarios SET ativo = ? WHERE id = ?', (novo_status, user_id))
        conn.commit()

    conn.close()
    return redirect('/admin')

@bancos_bp.route('/admin/usuarios/permissoes', methods=['POST'])
def salvar_permissoes_usuario():
    # Apenas o Master pode conceder/revogar acessos
    if not session.get('logged_in') or not session.get('eh_master'):
        return jsonify({"sucesso": False, "mensagem": "Acesso negado."}), 403

    dados = request.get_json(silent=True) or {}
    user_id = dados.get('user_id')
    if not user_id:
        return jsonify({"sucesso": False, "mensagem": "Usuário não informado."}), 400

    valores = {chave: (1 if dados.get(chave) else 0) for chave in PERMISSOES_DISPONIVEIS}

    conn = get_db_connection()
    alvo = conn.execute("SELECT eh_master FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    if not alvo:
        conn.close()
        return jsonify({"sucesso": False, "mensagem": "Usuário não encontrado."}), 404
    if alvo['eh_master']:
        conn.close()
        return jsonify({"sucesso": False, "mensagem": "O usuário Master já tem acesso a tudo."}), 400

    conn.execute(
        "UPDATE usuarios SET perm_servidores = ?, perm_gestao_bancos = ?, perm_horarios = ?, perm_certificados = ? WHERE id = ?",
        (valores['perm_servidores'], valores['perm_gestao_bancos'], valores['perm_horarios'], valores['perm_certificados'], user_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True})
    

@bancos_bp.route('/admin')
def admin_painel():
    if not session.get('logged_in'):
        return redirect('/admin/login')
    if not tem_permissao('perm_gestao_bancos'):
        return "Você não tem permissão para acessar a Gestão de Bancos. Fale com o administrador Master. <a href='/'>Voltar</a>", 403
    if not SERVIDORES:
        return "Nenhum servidor está configurado em SERVIDORES_CONFIG.", 503

    srv_atual = request.args.get('servidor', 'DB01')
    if srv_atual not in SERVIDORES: 
        srv_atual = next(iter(SERVIDORES))

    msg_sucesso = request.args.get('sucesso')
    msg_erro = request.args.get('erro')

    # Busca os dados atuais do usuário logado (nome/e-mail sempre atualizados a partir do banco)
    conn = get_db_connection()
    usuario_atual = conn.execute("SELECT nome, email FROM usuarios WHERE id = ?", (session.get('user_id'),)).fetchone()
    conn.close()
    usuario_nome = nome_para_assinatura(usuario_atual['nome'] if usuario_atual else session.get('user_nome', ''))
    usuario_email = usuario_atual['email'] if usuario_atual else ''

    # Busca a lista de usuários caso seja Master
    usuarios_lista = []
    api_config = {}
    api_ultimo_status = None
    if session.get('eh_master'):
        conn = get_db_connection()
        usuarios_lista = conn.execute("SELECT id, nome, email, eh_master, ativo, perm_servidores, perm_gestao_bancos, perm_horarios, perm_certificados FROM usuarios").fetchall()
        conn.close()
        api_config = obter_configuracao_api()
        api_ultimo_status = obter_ultimo_status_sincronizacao(get_db_connection)

    leitura_raw = ler_databases_conf_remoto_estruturado(srv_atual)
    conteudo_raw = (leitura_raw['conteudo'] if leitura_raw['ok'] else
                    '# Não foi possível ler databases.conf neste momento.')
    hash_conteudo_raw = leitura_raw.get('hash', '') if leitura_raw['ok'] else ''
    bancos_conf, _, _ = obter_dados_servidor_linux(srv_atual, SERVIDORES[srv_atual], modo_busca_unificada=True, forcar_atualizacao=True)

    return render_template_string(
        HTML_ADMIN, servidores=SERVIDORES, servidor_atual=srv_atual,
        bancos_conf=bancos_conf, conteudo_raw=conteudo_raw,
        hash_conteudo_raw=hash_conteudo_raw,
        msg_sucesso=msg_sucesso, msg_erro=msg_erro, usuarios_lista=usuarios_lista,
        usuario_nome=usuario_nome, usuario_email=usuario_email,
        veio_de_reset=session.get('reset_senha_pendente', False),
        api_config=api_config, api_ultimo_status=api_ultimo_status
    )

@bancos_bp.route('/editar-perfil', methods=['POST'])
def editar_perfil():
    if not session.get('logged_in') or not session.get('user_id'):
        return redirect('/admin/login')

    senha_antiga = request.form.get('senha_antiga')
    novo_nome = request.form.get('novo_nome')
    nova_senha = request.form.get('nova_senha')

    conn = get_db_connection()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (session['user_id'],)).fetchone()

    if not usuario or not check_password_hash(usuario['senha_hash'], senha_antiga or ''):
        conn.close()
        return redirect('/admin?erro=Senha atual incorreta!')

    if nova_senha:
        ok_senha, msg_senha = senha_atende_politica(nova_senha)
        if not ok_senha:
            conn.close()
            return redirect(f'/admin?erro={msg_senha}')

    hash_final = generate_password_hash(nova_senha) if nova_senha else usuario['senha_hash']
    nome_final = novo_nome if novo_nome else usuario['nome']

    conn.execute("UPDATE usuarios SET nome = ?, senha_hash = ? WHERE id = ?", (nome_final, hash_final, usuario['id']))
    conn.commit()
    conn.close()

    return redirect('/admin?sucesso=Perfil atualizado com sucesso!')

def diretorio_base_servidor(servidor):
    return posixpath.join(DIRETORIO_BASE, '3.0') if servidor == 'DB05' else DIRETORIO_BASE


@bancos_bp.get('/admin/pastas-destino')
def pastas_destino():
    ok, resposta = exigir_permissao('perm_gestao_bancos')
    if not ok:
        return resposta
    servidor = request.args.get('servidor', '')
    if servidor not in SERVIDORES:
        return jsonify(erro='Servidor inválido.'), 400
    base = diretorio_base_servidor(servidor)
    try:
        with closing(conectar_ssh(SERVIDORES[servidor])) as ssh, closing(ssh.open_sftp()) as sftp:
            return jsonify(listar_pastas(sftp, request.args.get('caminho') or base, base))
    except ValueError as erro:
        return jsonify(erro=str(erro)), 400
    except Exception:
        return jsonify(erro='Não foi possível listar as pastas. Verifique a conexão SSH e as permissões do servidor.'), 502


@bancos_bp.route('/admin/adicionar', methods=['POST'])
def admin_adicionar():
    ok, resposta = exigir_permissao('perm_gestao_bancos')
    if not ok:
        return resposta
    srv_req = request.form.get('servidor', '')
    srv = srv_req if srv_req in SERVIDORES else None
    def voltar(**mensagem):
        return redirect(destino_redirect_seguro(url_for('bancos.admin_painel', servidor=srv, **mensagem)))
    if srv not in SERVIDORES:
        return voltar(erro='Servidor inválido.')
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return voltar(erro='Senha de confirmação incorreta. Ação cancelada.')
    alias = sanitizar_alias(request.form.get('alias', ''))
    envolvidos = request.form.get('envolvidos', '').strip()
    arquivo = request.files.get('arquivo_fdb')
    tem_arquivo = arquivo is not None and len((arquivo.filename or '').strip()) > 0
    if not alias or len(alias) > 100 or len(envolvidos) > 300 or any(c in envolvidos for c in '\r\n'):
        return voltar(erro='Informe um alias válido e os responsáveis em uma única linha.')
    if tem_arquivo and not arquivo.filename.lower().endswith('.fdb'):
        return voltar(erro='Selecione um arquivo de banco .fdb.')
    servidor_existente, erro_verificacao = verificar_alias_existente_global(alias)
    if erro_verificacao:
        return voltar(erro='Não foi possível verificar a unicidade do alias em todos os servidores. Nenhuma alteração foi feita.')
    if servidor_existente:
        return voltar(erro=f'O alias "{alias}" já está cadastrado. Use um alias diferente.')
    leitura = ler_databases_conf_remoto_estruturado(srv)
    if not leitura['ok']:
        return voltar(erro='Não foi possível ler a configuração do servidor. Nenhum banco foi enviado.')
    conteudo_atual = leitura['conteudo']
    try:
        with closing(conectar_ssh(SERVIDORES[srv])) as ssh, closing(ssh.open_sftp()) as sftp:
            caminho = adicionar_banco(
                sftp, diretorio_base_servidor(srv), alias,
                request.form.get('modo_destino', 'alias'), request.form.get('pasta_destino', ''),
                request.form.get('nome_arquivo', 'dados.fdb').strip(), arquivo.stream if tem_arquivo else None)
    except ValueError as erro:
        return voltar(erro=str(erro))
    except Exception:
        return voltar(erro='Não foi possível preparar o destino ou enviar o banco. Verifique a conexão, as permissões e se o arquivo já existe. O alias não foi cadastrado.')
    hoje = datetime.now().strftime('%d/%m/%Y')
    comentario = f"\n# Data: {hoje}" + (f" | Envolvidos: {envolvidos}" if envolvidos else '')
    conteudo_final = conteudo_atual.rstrip() + comentario + f"\n{alias} = {caminho}\n"
    if not salvar_databases_conf_remoto(srv, conteudo_final, leitura.get('hash')):
        return voltar(erro='O destino foi preparado, mas o alias não pôde ser salvo. Se enviou um banco, ele permanece no destino; tente cadastrar o alias novamente sem reenviar o arquivo.')
    
    # Auditoria: adição de alias (Opção 3)
    registrar_acao_admin('adicionar_alias', {
        'servidor': srv,
        'alias': alias,
        'envolvidos': envolvidos,
        'tem_arquivo': tem_arquivo,
    })
    
    return voltar(sucesso=f'Alias "{alias}" cadastrado com sucesso!')

@bancos_bp.route('/admin/usuarios/reset-senha/<int:user_id>', methods=['POST'])
def solicitar_reset_senha(user_id):
    if not session.get('eh_master'):
        return redirect('/admin?erro=Acesso não autorizado!')

    conn = get_db_connection()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    
    if not usuario:
        conn.close()
        return redirect('/admin?erro=Usuário não encontrado.')

    # Gerador de token aleatório e temporário
    token_reset = secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE usuarios SET token_ativacao = ?, token_expira = ? WHERE id = ?",
        (token_reset, token_expira_em(), user_id)
    )
    conn.commit()
    conn.close()

    # =========================================================================
    # OPÇÃO 1: LINK DIRETO PARA O FLUXO DE REDEFINIÇÃO DE SENHA COM TOKEN
    # =========================================================================
    link_reset = url_for('bancos.redefinir_senha_token', token=token_reset, _external=True)

    # Configuração do E-mail
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = usuario['email']
    msg['Subject'] = "Acesso Direto - Redefinição de Senha"

    # =========================================================================
    # OPÇÃO 2: CORPO DO E-MAIL EM FORMATO HTML COM BOTÃO DE ACESSO DIRETO
    # =========================================================================
    corpo_html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <h2>Olá, {usuario['nome']}!</h2>
            <p>O administrador solicitou a redefinição de senha da sua conta.</p>
            <p>Clique no botão abaixo para acessar o painel e alterar sua senha diretamente:</p>
            <p style="margin: 20px 0;">
                <a href="{link_reset}" 
                   style="background-color: #27ae60; color: white; padding: 12px 20px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;">
                   Redefinir Minha Senha Agora
                </a>
            </p>
            <p style="font-size: 0.85em; color: #7f8c8d;">
                Ou copie este link no seu navegador:<br>
                <a href="{link_reset}">{link_reset}</a>
            </p>
        </body>
    </html>
    """
    msg.attach(MIMEText(corpo_html, 'html'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [usuario['email']], msg.as_string())
        server.quit()
        return redirect('/admin?sucesso=E-mail com link direto enviado com sucesso!')
    except Exception as e:
        return redirect(f'/admin?erro=Falha ao enviar e-mail: {str(e)}')


@bancos_bp.route('/admin/redefinir-senha/<token>', methods=['GET'])
def redefinir_senha_token(token):
    """Valida o token do link direto, efetua o login do usuário e direciona ao painel."""
    conn = get_db_connection()
    usuario = conn.execute("SELECT * FROM usuarios WHERE token_ativacao = ?", (token,)).fetchone()

    if not usuario or not token_ainda_valido(usuario['token_expira'] if 'token_expira' in usuario.keys() else None):
        conn.close()
        registrar_login_falha('', 'token_invalido_expirado')
        return render_template_string(HTML_LOGIN, erro="Link de redefinição inválido ou expirado!")

    # Força o login do usuário na sessão ativa
    session['user_id'] = usuario['id']
    session['user_nome'] = nome_para_assinatura(usuario['nome'])
    session['eh_master'] = usuario['eh_master']
    session['logged_in'] = True
    # Libera a próxima alteração de senha no modal de perfil sem exigir a senha atual
    # (o próprio link de e-mail, de uso único, já comprova a identidade do usuário)
    session['reset_senha_pendente'] = True
    
    # Regenera ID da sessão para prevenir session fixation (Opção 3)
    regenerar_session_id()
    
    # Registra login via token de redefinição na auditoria (Opção 3)
    registrar_login_sucesso(usuario['id'], usuario['nome'], usuario['eh_master'])

    # Consome/Invalida o token de uso único por segurança
    conn.execute("UPDATE usuarios SET token_ativacao = NULL, token_expira = NULL WHERE id = ?", (usuario['id'],))
    conn.commit()
    conn.close()

    # Redireciona o usuário logado para alterar sua senha no painel
    return redirect('/admin?sucesso=Você foi autenticado com sucesso. Altere sua senha no painel!')
    
@bancos_bp.post('/admin/perfil/atualizar')
def atualizar_perfil():
    if not session.get('logged_in'):
        return {"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}, 401

    dados = request.get_json(silent=True) or {}
    nome = (dados.get('nome') or '').strip()
    senha_atual = dados.get('senhaAtual')
    nova_senha = dados.get('novaSenha')
    user_id = session['user_id']

    conn = get_db_connection()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    if not usuario:
        conn.close()
        return {"sucesso": False, "mensagem": "Usuário não encontrado."}

    # Se o acesso veio de um link de redefinição de senha por e-mail, dispensa a senha atual
    veio_de_reset = session.get('reset_senha_pendente', False)

    if nova_senha:
        ok_senha, msg_senha = senha_atende_politica(nova_senha)
        if not ok_senha:
            conn.close()
            return {"sucesso": False, "mensagem": msg_senha}

        if not veio_de_reset:
            if not senha_atual or not check_password_hash(usuario['senha_hash'], senha_atual):
                conn.close()
                return {"sucesso": False, "mensagem": "A senha atual está incorreta!"}
    elif senha_atual:
        if not check_password_hash(usuario['senha_hash'], senha_atual):
            conn.close()
            return {"sucesso": False, "mensagem": "A senha atual está incorreta!"}

    novo_nome = nome if nome else usuario['nome']
    novo_hash = generate_password_hash(nova_senha) if nova_senha else usuario['senha_hash']

    conn.execute("UPDATE usuarios SET nome = ?, senha_hash = ? WHERE id = ?", (novo_nome, novo_hash, user_id))
    conn.commit()
    conn.close()

    # Mantém a sessão sincronizada e consome o "passe livre" do reset por e-mail
    session['user_nome'] = nome_para_assinatura(novo_nome)
    session.pop('reset_senha_pendente', None)

    return {"sucesso": True}

@bancos_bp.route('/admin/inativar', methods=['POST'])
def admin_inativar():
    if not session.get('logged_in'): return redirect('/admin/login')
    if not tem_permissao('perm_gestao_bancos'):
        return "Você não tem permissão para esta ação.", 403
    srv_req = request.form.get('servidor')
    srv = srv_req if srv_req in SERVIDORES else None
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return redirect(destino_redirect_seguro(url_for('bancos.admin_painel', servidor=srv, erro="Senha de confirmação incorreta. Ação cancelada.")))
    alias_target = request.form.get('alias', '').strip()
    if (srv not in SERVIDORES or not alias_target or len(alias_target) > 100 or
            any(ord(caractere) < 32 for caractere in alias_target)):
        return redirect(url_for('bancos.admin_painel', servidor=srv, erro='Servidor ou alias inválido.'))
    hoje = datetime.now().strftime('%d/%m/%Y')

    leitura = ler_databases_conf_remoto_estruturado(srv)
    if not leitura['ok']:
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro='Não foi possível ler databases.conf. Nenhuma alteração foi feita.'
        ))
    conteudo_atual = leitura['conteudo']
    linhas = conteudo_atual.splitlines()
    novas_linhas = []
    encontrado = False

    for i, linha in enumerate(linhas):
        linha_limpa = linha.strip()
        if not linha_limpa.startswith('#') and '=' in linha_limpa:
            partes = linha_limpa.split('=', 1)
            alias_linha = partes[0].strip()
            if alias_linha.lower() == alias_target.lower():
                encontrado = True
                novas_linhas.append(f"# INATIVADO EM {hoje}")
                novas_linhas.append(f"# {linha_limpa}")
                continue
        novas_linhas.append(linha)

    if not encontrado:
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro=f'Alias "{alias_target}" não foi encontrado; nenhuma alteração foi feita.'
        ))

    conteudo_final = "\n".join(novas_linhas) + "\n"
    if not salvar_databases_conf_remoto(srv, conteudo_final, leitura.get('hash')):
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro='Não foi possível salvar databases.conf. Nenhuma confirmação de sucesso foi emitida.'
        ))
    
    # Auditoria: inativação de alias (Opção 3)
    registrar_acao_admin('inativar_alias', {
        'servidor': srv,
        'alias': alias_target,
    })

    return redirect(url_for(
        'bancos.admin_painel', servidor=srv,
        sucesso=f'Alias "{alias_target}" inativado com sucesso!'
    ))

@bancos_bp.route('/admin/usuarios/excluir', methods=['POST'])
def excluir_usuario():
    if not session.get('eh_master'):
        return redirect('/admin?erro=Acesso negado!')

    user_id = request.form.get('user_id')
    senha_master = request.form.get('senha_master')

    conn = get_db_connection()
    master = conn.execute("SELECT * FROM usuarios WHERE id = ?", (session.get('user_id'),)).fetchone()

    # Valida senha do Master
    if not check_password_hash(master['senha_hash'], senha_master):
        conn.close()
        return redirect('/admin?erro=Senha do Master incorreta! Exclusão cancelada.')

    # Busca info do usuário a ser excluído para auditoria
    usuario_excluir = conn.execute("SELECT nome, email FROM usuarios WHERE id = ?", (user_id,)).fetchone()
    
    conn.execute("DELETE FROM usuarios WHERE id = ? AND eh_master = 0", (user_id,))
    conn.commit()
    conn.close()
    
    # Auditoria: exclusão de usuário (Opção 3)
    if usuario_excluir:
        registrar_acao_admin('excluir_usuario', {
            'usuario_excluido_id': user_id,
            'usuario_excluido_nome': usuario_excluir['nome'],
            'usuario_excluido_email': usuario_excluir['email'],
        })

    return redirect('/admin?sucesso=Usuário excluído com sucesso!')

@bancos_bp.route('/admin/salvar_raw', methods=['POST'])
def admin_salvar_raw():
    if not session.get('logged_in'): return redirect('/admin/login')
    if not tem_permissao('perm_gestao_bancos'):
        return "Você não tem permissão para esta ação.", 403
    srv_req = request.form.get('servidor')
    srv = srv_req if srv_req in SERVIDORES else None
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return redirect(destino_redirect_seguro(url_for('bancos.admin_painel', servidor=srv, erro="Senha de confirmação incorreta. Ação cancelada.")))
    conteudo_raw = request.form.get('conteudo_raw', '')
    hash_original = request.form.get('hash_original', '').strip()
    if srv not in SERVIDORES:
        return redirect(url_for('bancos.admin_painel', servidor=srv, erro='Servidor inválido.'))
    if not re.fullmatch(r'[0-9a-fA-F]{64}', hash_original):
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro='Versão original inválida ou ausente. Recarregue a página antes de salvar.'
        ))
    hash_original = hash_original.lower()
    leitura = ler_databases_conf_remoto_estruturado(srv)
    if not leitura['ok']:
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro='Não foi possível validar o estado atual de databases.conf. Nenhuma alteração foi feita.'
        ))
    if not hmac.compare_digest(leitura.get('hash', '').lower(), hash_original):
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro='Conflito: databases.conf foi alterado desde que a página foi carregada. Recarregue e revise suas alterações.'
        ))
    valido, erro_validacao = validar_conteudo_databases_conf(conteudo_raw)
    if not valido:
        return redirect(url_for('bancos.admin_painel', servidor=srv, erro=erro_validacao))
    if not salvar_databases_conf_remoto(srv, conteudo_raw, hash_original):
        return redirect(url_for(
            'bancos.admin_painel', servidor=srv,
            erro='Conflito ou falha ao salvar databases.conf. O arquivo anterior foi preservado; recarregue a página e tente novamente.'
        ))
    
    # Auditoria: edição direta de databases.conf (Opção 3)
    registrar_acao_admin('salvar_raw_databases_conf', {
        'servidor': srv,
    })

    return redirect(url_for(
        'bancos.admin_painel', servidor=srv,
        sucesso='Arquivo databases.conf atualizado com sucesso!'
    ))

# --- ROTAS PÚBLICAS REAPROVEITADAS ---
@bancos_bp.route('/admin/cliente-loja/salvar', methods=['POST'])
def salvar_cliente_loja():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para editar dados de lojas."}), 403

    dados = request.get_json(silent=True) or {}
    alias = (dados.get('alias') or '').strip()
    loja_id = (dados.get('id') or '').strip()
    if not alias:
        return jsonify({"sucesso": False, "mensagem": "Alias não informado."}), 400

    valores = {campo: (dados.get(campo) or '').strip() for campo in CAMPOS_LOJA}
    ignorar_incompleto = 1 if dados.get('ignorar_incompleto') in (1, '1', True, 'true', 'on') else 0

    conn = get_db_connection()
    if loja_id:
        conn.execute(
            "UPDATE clientes_lojas SET loj_codigo=?, loj_fantasia=?, loj_nome=?, loj_cnpj=?, cod_info=?, cli_situacao=?, ignorar_incompleto=? "
            "WHERE id=? AND alias=?",
            (valores['loj_codigo'], valores['loj_fantasia'], valores['loj_nome'], valores['loj_cnpj'],
             valores['cod_info'], valores['cli_situacao'], ignorar_incompleto, loja_id, alias)
        )
    else:
        cursor = conn.execute(
            "INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, cod_info, cli_situacao, ignorar_incompleto) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (alias, valores['loj_codigo'], valores['loj_fantasia'], valores['loj_nome'], valores['loj_cnpj'],
             valores['cod_info'], valores['cli_situacao'], ignorar_incompleto)
        )
        loja_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True, "id": loja_id})


@bancos_bp.route('/admin/cliente-loja/excluir', methods=['POST'])
def excluir_cliente_loja():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para editar dados de lojas."}), 403

    dados = request.get_json(silent=True) or {}
    loja_id = (dados.get('id') or '').strip()
    if not loja_id:
        return jsonify({"sucesso": False, "mensagem": "ID não informado."}), 400

    conn = get_db_connection()
    # Busca info da loja para auditoria
    loja = conn.execute("SELECT alias, loj_codigo, loj_fantasia FROM clientes_lojas WHERE id = ?", (loja_id,)).fetchone()
    
    conn.execute("DELETE FROM clientes_lojas WHERE id = ?", (loja_id,))
    conn.commit()
    conn.close()
    
    # Auditoria: exclusão de cliente/loja (Opção 3)
    if loja:
        registrar_acao_admin('excluir_cliente_loja', {
            'loja_id': loja_id,
            'alias': loja['alias'],
            'loj_codigo': loja['loj_codigo'],
            'loj_fantasia': loja['loj_fantasia'],
        })

    return jsonify({"sucesso": True})


@bancos_bp.route('/admin/lojas/ignorar-sem-cnpj', methods=['POST'])
def rota_ignorar_lojas_sem_cnpj():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para esta ação."}), 403

    total = marcar_lojas_sem_cnpj_como_ignoradas()
    registrar_acao_admin('ignorar_lojas_sem_cnpj', {
        'total_marcadas': total,
        'usuario': session.get('username') or session.get('user_nome')
    })
    return jsonify({
        "sucesso": True,
        "mensagem": f"{total} loja(s) sem CNPJ marcada(s) como ignorada(s) com sucesso.",
        "total": total
    })


# --- ROTAS DA API INFOBRASIL (ACESSO EXCLUSIVO MASTER) ---

@bancos_bp.route('/admin/config-api', methods=['POST'])
def salvar_configuracao_api():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not session.get('eh_master') and not (g.get('usuario_atual') and g.usuario_atual['eh_master']):
        return jsonify({"sucesso": False, "mensagem": "Apenas o usuário Master tem permissão para alterar as configurações da API."}), 403

    dados = request.get_json(silent=True) or {}
    url = (dados.get('url') or '').strip()
    usuario = (dados.get('usuario') or '').strip()
    senha = (dados.get('senha') or '').strip()

    if not url or not usuario:
        return jsonify({"sucesso": False, "mensagem": "A URL base e o usuário da API são obrigatórios."}), 400

    try:
        retorno = salvar_variaveis_env({
            'INFOBRASIL_API_URL': url,
            'INFOBRASIL_API_USER': usuario,
            'INFOBRASIL_API_PASS': senha,
        })
        if isinstance(retorno, tuple):
            sucesso, msg = retorno
        elif isinstance(retorno, bool):
            sucesso, msg = retorno, ("Credenciais salvas no arquivo .env com sucesso!" if retorno else "Falha ao gravar arquivo .env")
        else:
            sucesso, msg = True, "Credenciais salvas no arquivo .env com sucesso!"
    except Exception as e:
        LOGGER.exception("Erro ao salvar configuração da API: %s", e)
        return jsonify({"sucesso": False, "mensagem": f"Erro interno ao salvar .env: {str(e)}"}), 500

    if sucesso:
        registrar_acao_admin('config_api_infobrasil', {
            'usuario': session.get('username') or session.get('user_nome'),
            'api_url': url,
            'api_user': usuario
        })
        return jsonify({"sucesso": True, "mensagem": "Credenciais salvas no arquivo .env com sucesso!"})
    else:
        return jsonify({"sucesso": False, "mensagem": msg}), 500


@bancos_bp.route('/admin/config-api/testar', methods=['POST'])
def rota_testar_conexao_api():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not session.get('eh_master') and not (g.get('usuario_atual') and g.usuario_atual['eh_master']):
        return jsonify({"sucesso": False, "mensagem": "Apenas o usuário Master tem permissão para testar a API."}), 403

    dados = request.get_json(silent=True) or {}
    url = (dados.get('url') or '').strip() or None
    usuario = (dados.get('usuario') or '').strip() or None
    senha = (dados.get('senha') or '').strip() or None

    try:
        resultado = testar_conexao_api(url=url, user=usuario, password=senha, usuario=usuario, senha=senha)
        if isinstance(resultado, tuple):
            sucesso = bool(resultado[0])
            mensagem = str(resultado[1]) if len(resultado) > 1 else ''
            user_info = resultado[2] if len(resultado) > 2 else None
            resultado = {"sucesso": sucesso, "mensagem": mensagem, "user_info": user_info}
        return jsonify(resultado)
    except Exception as e:
        LOGGER.exception("Erro ao testar conexão com API: %s", e)
        return jsonify({"sucesso": False, "mensagem": f"Erro interno ao testar conexão: {str(e)}"}), 500


@bancos_bp.route('/admin/config-api/sincronizar', methods=['POST'])
def rota_sincronizar_api():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not session.get('eh_master') and not (g.get('usuario_atual') and g.usuario_atual['eh_master']):
        return jsonify({"sucesso": False, "mensagem": "Apenas o usuário Master tem permissão para executar a sincronização da API."}), 403

    resultado = sincronizar_lojas_com_api(get_db_connection, usuario_solicitante=session.get('user_nome'))
    if resultado.get('sucesso'):
        registrar_acao_admin('sincronizar_api_infobrasil', {
            'usuario': session.get('username') or session.get('user_nome'),
            'atualizados': resultado.get('atualizados', 0),
            'total_api': resultado.get('total_api', 0)
        })
    return jsonify(resultado)


# --- IMPORTAÇÃO EM LOTE DE LOJAS VIA PLANILHA .XLSX ---
COLUNAS_IMPORTACAO_XLSX = ['Servidor', 'Host', 'Porta', 'Alias', 'CNAME', 'LOJ_CODIGO', 'LOJ_FANTASIA', 'LOJ_NOME', 'LOJ_CNPJ']

HTML_IMPORTAR_LOJAS = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8"><title>Importar Lojas</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--cor-fundo); padding: 30px; }
        .card-import { background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); max-width: 700px; margin: 0 auto; }
        table.tabela-resumo { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 0.9em; }
        table.tabela-resumo th, table.tabela-resumo td { border: 1px solid var(--cor-borda); padding: 6px 10px; text-align: left; }
        table.tabela-resumo th { background: #f8fafc; }
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; color: var(--cor-texto-invertido) !important; }
        .btn-success:hover { background-color: var(--cor-primaria-escura) !important; border-color: var(--cor-primaria-escura) !important; }
        .btn-outline-secondary { color: var(--cor-texto-suave) !important; border-color: var(--cor-borda) !important; }
        .btn-warning { background-color: var(--cor-aviso) !important; border-color: var(--cor-aviso) !important; color: var(--cor-texto-invertido) !important; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <div class="card-import" style="{% if duplicatas %}max-width: 1100px;{% endif %}">
        <h3 class="mb-1">📥 Importar Lojas (.xlsx)</h3>
        <p class="text-muted small">A planilha deve ter as colunas: <code>{{ colunas }}</code> em uma aba (a coluna <strong>CNAME</strong> é lida mas ignorada na importação). Se um alias já tiver lojas cadastradas, apenas as novas são adicionadas — identificadas pelo CNPJ (ou pelo LOJ_CODIGO quando a linha não tem CNPJ) — nada é apagado.</p>

        {% if erro %}<div class="alert alert-danger">{{ erro }}</div>{% endif %}

        {% if resultado %}
            <div class="alert alert-success">
                Importação concluída: <strong>{{ resultado.inseridas }}</strong> loja(s) nova(s) inserida(s),
                <strong>{{ resultado.ignoradas }}</strong> já existente(s) (CNPJ repetido) ignorada(s),
                em <strong>{{ resultado.aliases }}</strong> alias(es) distintos.
            </div>
            {% if resultado.erros %}
                <p class="fw-bold mt-3">Linhas com problema (não importadas):</p>
                <table class="tabela-resumo">
                    <thead><tr><th>Linha</th><th>Motivo</th></tr></thead>
                    <tbody>
                        {% for erro_linha in resultado.erros %}
                        <tr><td>{{ erro_linha.linha }}</td><td>{{ erro_linha.motivo }}</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            {% endif %}
        {% endif %}

        {% if duplicatas %}
            <div class="mt-3 p-3" style="background:#fffbeb; border:1px solid #fde68a; border-radius:8px;">
                <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
                    <p class="fw-bold mb-0">⚠️ {{ duplicatas|length }} linha(s) ignorada(s) por já existir(em) — revise abaixo:</p>
                    <button class="btn btn-sm btn-warning fw-bold" onclick="forcarTodasDuplicatas()">Importar todas mesmo assim</button>
                </div>
                <div style="max-height: 320px; overflow-y: auto; margin-top: 10px;">
                    <table class="tabela-resumo" id="tabelaDuplicatas">
                        <thead>
                            <tr><th>Linha</th><th>Alias</th><th>LojCód.</th><th>Fantasia</th><th>Nome/Razão</th><th>CNPJ</th><th>Motivo</th><th>Ação</th></tr>
                        </thead>
                        <tbody>
                            {% for d in duplicatas %}
                            <tr id="dup-{{ d.id }}">
                                <td>{{ d.linha }}</td>
                                <td>{{ d.alias }}</td>
                                <td>{{ d.loj_codigo }}</td>
                                <td>{{ d.loj_fantasia }}</td>
                                <td>{{ d.loj_nome }}</td>
                                <td>{{ d.loj_cnpj or '-' }}</td>
                                <td class="text-muted small">{{ d.motivo }}</td>
                                <td class="text-nowrap">
                                    <button class="btn btn-sm btn-outline-success" onclick="acaoDuplicata({{ d.id }}, 'forcar')" title="Importar esta linha mesmo assim">✔ Importar</button>
                                    <button class="btn btn-sm btn-outline-secondary" onclick="acaoDuplicata({{ d.id }}, 'ignorar')" title="Manter ignorada">✖ Ignorar</button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        {% endif %}

        <form method="POST" enctype="multipart/form-data" class="mt-3">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="mb-3">
                <label class="form-label fw-bold">Arquivo .xlsx:</label>
                <input type="file" name="arquivo" accept=".xlsx" class="form-control" required>
            </div>
            <button type="submit" class="btn btn-success fw-bold">Importar</button>
            <a href="/admin/importar-lojas/modelo" class="btn btn-outline-secondary">⬇ Baixar Modelo</a>
            <a href="/" class="btn btn-link text-muted">⬅ Voltar ao Painel</a>
        </form>
        <div class="mt-4 pt-3 border-top d-flex justify-content-between align-items-center flex-wrap gap-2">
            <span class="text-muted small">Já cadastrou as lojas e só quer preencher <strong>CodInfo</strong> e <strong>Situação</strong>?</span>
            <a href="/admin/atualizar-lojas-info" class="btn btn-sm btn-outline-primary fw-bold">🔄 Atualizar CodInfo / Situação (.xlsx)</a>
        </div>
    </div>

    <script>
        function acaoDuplicata(id, tipo) {
            csrfFetch(`/admin/importar-lojas/${tipo}/${id}`, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.sucesso) {
                        const linha = document.getElementById('dup-' + id);
                        if (linha) linha.remove();
                    } else {
                        alert('Erro: ' + (data.mensagem || 'tente novamente.'));
                    }
                })
                .catch(() => alert('Não foi possível concluir a ação. Verifique sua conexão.'));
        }

        function forcarTodasDuplicatas() {
            if (!confirm('Importar todas as linhas listadas mesmo estando duplicadas?')) return;
            csrfFetch('/admin/importar-lojas/forcar-todas', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.sucesso) {
                        location.reload();
                    } else {
                        alert('Erro: ' + (data.mensagem || 'tente novamente.'));
                    }
                })
                .catch(() => alert('Não foi possível concluir a ação. Verifique sua conexão.'));
        }
    </script>
</body>
</html>
"""

@bancos_bp.route('/admin/importar-lojas', methods=['GET', 'POST'])
def importar_lojas():
    if not session.get('logged_in'):
        return redirect('/admin/login')
    if not tem_permissao('perm_servidores'):
        return "Você não tem permissão para importar lojas. Fale com o administrador Master. <a href='/'>Voltar</a>", 403

    colunas_str = ', '.join(COLUNAS_IMPORTACAO_XLSX)

    if request.method == 'GET':
        conn_dup = get_db_connection()
        duplicatas_pendentes = [dict(l) for l in conn_dup.execute("SELECT * FROM import_duplicatas_pendentes ORDER BY linha")]
        conn_dup.close()
        return render_template_string(HTML_IMPORTAR_LOJAS, colunas=colunas_str, duplicatas=duplicatas_pendentes)

    arquivo = request.files.get('arquivo')
    if not arquivo or not arquivo.filename.lower().endswith('.xlsx'):
        return render_template_string(HTML_IMPORTAR_LOJAS, colunas=colunas_str, erro="Envie um arquivo .xlsx válido.")

    try:
        import openpyxl
        wb = openpyxl.load_workbook(arquivo, read_only=True, data_only=True)
    except Exception as e:
        return render_template_string(HTML_IMPORTAR_LOJAS, colunas=colunas_str, erro=f"Não foi possível ler o arquivo: {e}")

    # Usa a aba "LOJAS" se existir; senão usa a aba ativa
    aba = wb['LOJAS'] if 'LOJAS' in wb.sheetnames else wb.active

    linhas = list(aba.iter_rows(values_only=True))
    if not linhas:
        return render_template_string(HTML_IMPORTAR_LOJAS, colunas=colunas_str, erro="A planilha está vazia.")

    # FIX: remove BOM (\ufeff) que alguns exportadores colocam no início do arquivo/primeira coluna
    cabecalho = [str(c).strip().lstrip('\ufeff') if c else '' for c in linhas[0]]
    try:
        idx_alias = cabecalho.index('Alias')
        idx_loj_codigo = cabecalho.index('LOJ_CODIGO')
        idx_loj_fantasia = cabecalho.index('LOJ_FANTASIA')
        idx_loj_nome = cabecalho.index('LOJ_NOME')
        idx_loj_cnpj = cabecalho.index('LOJ_CNPJ')
    except ValueError as e:
        return render_template_string(
            HTML_IMPORTAR_LOJAS, colunas=colunas_str,
            erro=f"Coluna obrigatória não encontrada na planilha ({e}). Colunas esperadas: {colunas_str}"
        )

    idx_cod_info = None
    for cand in ('CodInfo', 'CODINFO', 'COD_INFO', 'cod_info'):
        if cand in cabecalho:
            idx_cod_info = cabecalho.index(cand)
            break

    idx_cli_situacao = None
    for cand in ('Situação', 'Situacao', 'SITUACAO', 'cli_situacao', 'Status'):
        if cand in cabecalho:
            idx_cli_situacao = cabecalho.index(cand)
            break

    conn = get_db_connection()

    # Limpa a lista de duplicatas pendentes da importação anterior (cada importação gera sua própria lista para revisão)
    conn.execute("DELETE FROM import_duplicatas_pendentes")

    # Carrega o que já existe por alias, para não duplicar em reimportações:
    # - CNPJ é a chave preferencial (quando preenchido)
    # - LOJ_CODIGO serve de fallback para linhas sem CNPJ (ex: "PESSOA FISICA"), que também não podem duplicar
    cnpjs_existentes = {}
    codigos_existentes = {}
    for linha in conn.execute("SELECT alias, loj_codigo, loj_cnpj FROM clientes_lojas"):
        if linha['loj_cnpj']:
            cnpjs_existentes.setdefault(linha['alias'], set()).add(str(linha['loj_cnpj']).strip())
        if linha['loj_codigo']:
            codigos_existentes.setdefault(linha['alias'], set()).add(str(linha['loj_codigo']).strip())

    inseridas = 0
    ignoradas = 0
    aliases_processados = set()
    erros = []

    for num_linha, linha in enumerate(linhas[1:], start=2):
        if linha is None or all(c is None for c in linha):
            continue

        alias = str(linha[idx_alias]).strip() if idx_alias < len(linha) and linha[idx_alias] else ''
        if not alias:
            erros.append({"linha": num_linha, "motivo": "Alias em branco"})
            continue

        loj_codigo = str(linha[idx_loj_codigo]).strip() if idx_loj_codigo < len(linha) and linha[idx_loj_codigo] is not None else ''
        loj_fantasia = str(linha[idx_loj_fantasia]).strip() if idx_loj_fantasia < len(linha) and linha[idx_loj_fantasia] else ''
        loj_nome = str(linha[idx_loj_nome]).strip() if idx_loj_nome < len(linha) and linha[idx_loj_nome] else ''
        loj_cnpj = str(linha[idx_loj_cnpj]).strip() if idx_loj_cnpj < len(linha) and linha[idx_loj_cnpj] else ''

        cod_info = str(linha[idx_cod_info]).strip() if idx_cod_info is not None and idx_cod_info < len(linha) and linha[idx_cod_info] is not None else ''
        if cod_info.endswith('.0') and cod_info[:-2].isdigit():
            cod_info = cod_info[:-2]
        cli_situacao = str(linha[idx_cli_situacao]).strip() if idx_cli_situacao is not None and idx_cli_situacao < len(linha) and linha[idx_cli_situacao] is not None else ''

        aliases_processados.add(alias)

        # Evita duplicar: CNPJ já cadastrado para este alias -> ignora (mas registra para revisão).
        # Sem CNPJ na linha, usa o LOJ_CODIGO já cadastrado para este alias como critério -> ignora.
        motivo_duplicata = None
        if loj_cnpj and loj_cnpj in cnpjs_existentes.get(alias, set()):
            motivo_duplicata = "CNPJ já cadastrado para este alias"
        elif not loj_cnpj and loj_codigo and loj_codigo in codigos_existentes.get(alias, set()):
            motivo_duplicata = "LOJ_CODIGO já cadastrado para este alias (linha sem CNPJ)"

        if motivo_duplicata:
            ignoradas += 1
            conn.execute(
                "INSERT INTO import_duplicatas_pendentes (linha, alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, motivo) VALUES (?,?,?,?,?,?,?)",
                (num_linha, alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, motivo_duplicata)
            )
            continue

        conn.execute(
            "INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, cod_info, cli_situacao) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, cod_info, cli_situacao)
        )
        if loj_cnpj:
            cnpjs_existentes.setdefault(alias, set()).add(loj_cnpj)
        if loj_codigo:
            codigos_existentes.setdefault(alias, set()).add(loj_codigo)
        inseridas += 1

    conn.commit()
    duplicatas_pendentes = [dict(l) for l in conn.execute("SELECT * FROM import_duplicatas_pendentes ORDER BY linha")]
    conn.close()

    resultado = {
        "inseridas": inseridas,
        "ignoradas": ignoradas,
        "aliases": len(aliases_processados),
        "erros": erros
    }
    return render_template_string(HTML_IMPORTAR_LOJAS, colunas=colunas_str, resultado=resultado, duplicatas=duplicatas_pendentes)


@bancos_bp.route('/admin/importar-lojas/forcar/<int:duplicata_id>', methods=['POST'])
def forcar_duplicata_individual(duplicata_id):
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para esta ação."}), 403

    conn = get_db_connection()
    linha = conn.execute("SELECT * FROM import_duplicatas_pendentes WHERE id = ?", (duplicata_id,)).fetchone()
    if not linha:
        conn.close()
        return jsonify({"sucesso": False, "mensagem": "Registro não encontrado (talvez já tenha sido tratado)."}), 404

    conn.execute(
        "INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj) VALUES (?, ?, ?, ?, ?)",
        (linha['alias'], linha['loj_codigo'], linha['loj_fantasia'], linha['loj_nome'], linha['loj_cnpj'])
    )
    conn.execute("DELETE FROM import_duplicatas_pendentes WHERE id = ?", (duplicata_id,))
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True})


@bancos_bp.route('/admin/importar-lojas/ignorar/<int:duplicata_id>', methods=['POST'])
def ignorar_duplicata_individual(duplicata_id):
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para esta ação."}), 403

    conn = get_db_connection()
    conn.execute("DELETE FROM import_duplicatas_pendentes WHERE id = ?", (duplicata_id,))
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True})


@bancos_bp.route('/admin/importar-lojas/forcar-todas', methods=['POST'])
def forcar_todas_duplicatas():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para esta ação."}), 403

    conn = get_db_connection()
    pendentes = conn.execute("SELECT * FROM import_duplicatas_pendentes").fetchall()
    for linha in pendentes:
        conn.execute(
            "INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj) VALUES (?, ?, ?, ?, ?)",
            (linha['alias'], linha['loj_codigo'], linha['loj_fantasia'], linha['loj_nome'], linha['loj_cnpj'])
        )
    conn.execute("DELETE FROM import_duplicatas_pendentes")
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True, "total": len(pendentes)})


@bancos_bp.route('/admin/importar-lojas/modelo')
def modelo_importar_lojas():
    if not session.get('logged_in'):
        return redirect('/admin/login')

    try:
        import openpyxl
        from io import BytesIO
        from flask import send_file
    except ImportError:
        return render_template_string(
            HTML_IMPORTAR_LOJAS, colunas=', '.join(COLUNAS_IMPORTACAO_XLSX),
            erro="A biblioteca 'openpyxl' não está instalada neste servidor. "
                 "Abra um terminal no servidor e rode: pip install openpyxl"
        )

    wb = openpyxl.Workbook()
    aba = wb.active
    aba.title = "LOJAS"
    aba.append(COLUNAS_IMPORTACAO_XLSX)
    aba.append(['DB01', 'dbexemplo.iprojectti.com.br', '3050', 'exemplo', 'dbexemplo.iprojectti.com.br/3050:exemplo', '1', 'LOJA EXEMPLO', 'EXEMPLO COMERCIO LTDA', '12345678000199'])

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer, as_attachment=True, download_name='modelo_importacao_lojas.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


# --- ATUALIZAÇÃO EM LOTE DE CODINFO E SITUAÇÃO VIA PLANILHA .XLSX ---
HTML_ATUALIZAR_LOJAS_INFO = r"""
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8"><title>Atualizar CodInfo e Situação</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--cor-fundo); padding: 30px; }
        .card-atualizar { background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); max-width: 900px; margin: 0 auto; }
        table.tabela-resumo { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 0.9em; }
        table.tabela-resumo th, table.tabela-resumo td { border: 1px solid var(--cor-borda); padding: 6px 10px; text-align: left; }
        table.tabela-resumo th { background: #f8fafc; }
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; color: #fff !important; }
        .btn-success:hover { background-color: var(--cor-primaria-escura) !important; border-color: var(--cor-primaria-escura) !important; }
        .btn-outline-secondary { color: var(--cor-texto-suave) !important; border-color: var(--cor-borda) !important; }
        .badge-alias { background: #e0f2fe; color: #0369a1; font-weight: 600; padding: 2px 6px; border-radius: 4px; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <div class="card-atualizar">
        <h3 class="mb-1">🔄 Atualizar CodInfo e Situação (.xlsx)</h3>
        <p class="text-muted small">
            Envie uma planilha com as colunas <code>CODINFO</code>, <code>CNPJ</code> e <code>Situação</code>.
            O sistema localizará as lojas já cadastradas através do CNPJ/CPF (com ou sem pontuação) e preencherá automaticamente as colunas <strong>CodInfo</strong> e <strong>Situação</strong> correspondentes.
        </p>

        {% if erro %}<div class="alert alert-danger">{{ erro }}</div>{% endif %}

        {% if resultado %}
            <div class="alert alert-success">
                <h5 class="alert-heading mb-2">✅ Atualização concluída com sucesso!</h5>
                <div><strong>{{ resultado.atualizadas }}</strong> loja(s) atualizada(s) no sistema em <strong>{{ resultado.aliases_afetados }}</strong> alias(es) distintos.</div>
                <div class="text-muted small mt-1">Linhas processadas da planilha: {{ resultado.linhas_processadas }} | CNPJs da planilha não encontrados no site: {{ resultado.nao_encontrados|length }}</div>
            </div>

            {% if resultado.amostra %}
                <div class="mt-3">
                    <p class="fw-bold mb-1">📋 Amostra de Lojas Atualizadas (primeiras {{ resultado.amostra|length }}):</p>
                    <div style="max-height: 250px; overflow-y: auto;">
                        <table class="tabela-resumo">
                            <thead>
                                <tr><th>Alias</th><th>LojCód.</th><th>Fantasia</th><th>CNPJ</th><th>CodInfo</th><th>Situação</th></tr>
                            </thead>
                            <tbody>
                                {% for a in resultado.amostra %}
                                <tr>
                                    <td><span class="badge-alias">{{ a.alias }}</span></td>
                                    <td>{{ a.loj_codigo }}</td>
                                    <td>{{ a.loj_fantasia }}</td>
                                    <td>{{ a.loj_cnpj }}</td>
                                    <td><strong>{{ a.cod_info }}</strong></td>
                                    <td><strong>{{ a.cli_situacao }}</strong></td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            {% endif %}

            {% if resultado.nao_encontrados %}
                <details class="mt-3 p-3 bg-light border rounded">
                    <summary class="fw-bold text-secondary" style="cursor: pointer;">
                        ⚠️ Ver {{ resultado.nao_encontrados|length }} CNPJ(s) da planilha que não foram encontrados nas lojas do site
                    </summary>
                    <div style="max-height: 250px; overflow-y: auto; margin-top: 10px;">
                        <table class="tabela-resumo">
                            <thead>
                                <tr><th>Linha Planilha</th><th>CNPJ na Planilha</th><th>CodInfo</th><th>Situação</th></tr>
                            </thead>
                            <tbody>
                                {% for ne in resultado.nao_encontrados %}
                                <tr>
                                    <td>{{ ne.linha }}</td>
                                    <td><code>{{ ne.cnpj }}</code></td>
                                    <td>{{ ne.cod_info }}</td>
                                    <td>{{ ne.situacao }}</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </details>
            {% endif %}
        {% endif %}

        <form method="POST" enctype="multipart/form-data" class="mt-3">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="mb-3">
                <label class="form-label fw-bold">Arquivo .xlsx com CodInfo, CNPJ e Situação:</label>
                <input type="file" name="arquivo" accept=".xlsx" class="form-control" required>
            </div>
            <div class="d-flex gap-2 align-items-center flex-wrap">
                <button type="submit" class="btn btn-success fw-bold">Atualizar Lojas</button>
                <a href="/admin/atualizar-lojas-info/modelo" class="btn btn-outline-secondary">⬇ Baixar Planilha Modelo</a>
                <a href="/" class="btn btn-link text-muted">⬅ Voltar ao Painel</a>
            </div>
        </form>

        <div class="mt-4 pt-3 border-top d-flex justify-content-between align-items-center flex-wrap gap-2">
            <span class="text-muted small">Precisa cadastrar novas lojas inteiras?</span>
            <a href="/admin/importar-lojas" class="btn btn-sm btn-outline-primary fw-bold">📥 Importar Novas Lojas (.xlsx)</a>
        </div>
    </div>
</body>
</html>
"""

@bancos_bp.route('/admin/atualizar-lojas-info', methods=['GET', 'POST'])
def atualizar_lojas_info():
    if not session.get('logged_in'):
        return redirect('/admin/login')
    if not tem_permissao('perm_servidores'):
        return "Você não tem permissão para atualizar dados de lojas. Fale com o administrador Master. <a href='/'>Voltar</a>", 403

    if request.method == 'GET':
        return render_template_string(HTML_ATUALIZAR_LOJAS_INFO)

    arquivo = request.files.get('arquivo')
    if not arquivo or not arquivo.filename.lower().endswith('.xlsx'):
        return render_template_string(HTML_ATUALIZAR_LOJAS_INFO, erro="Envie um arquivo .xlsx válido.")

    try:
        import openpyxl
        wb = openpyxl.load_workbook(arquivo, read_only=True, data_only=True)
    except Exception as e:
        return render_template_string(HTML_ATUALIZAR_LOJAS_INFO, erro=f"Não foi possível ler o arquivo: {e}")

    aba = wb.active
    linhas = list(aba.iter_rows(values_only=True))
    if not linhas:
        return render_template_string(HTML_ATUALIZAR_LOJAS_INFO, erro="A planilha está vazia.")

    # Detecção flexível e inteligente de cabeçalhos
    cabecalho = [str(c).strip().lstrip('\ufeff') if c is not None else '' for c in linhas[0]]
    idx_cod, idx_cnpj, idx_sit = None, None, None
    for i, col in enumerate(cabecalho):
        raw = str(col).strip()
        norm = unicodedata.normalize('NFKD', raw).encode('ascii', 'ignore').decode('utf-8').upper()
        norm_clean = re.sub(r'[^A-Z0-9]', '', norm)
        if idx_cod is None and ('CODINFO' in norm_clean or norm_clean in ('COD', 'CODIGO', 'CODCLIENTE', 'COD_INFO', 'COD_CLIENTE')):
            idx_cod = i
        elif idx_cnpj is None and ('CNPJ' in norm_clean or 'CPF' in norm_clean or 'DOCUMENTO' in norm_clean):
            idx_cnpj = i
        elif idx_sit is None and ('SITUAC' in norm_clean or 'STATUS' in norm_clean or 'SIT' in norm_clean or 'SITUA' in raw.upper()):
            idx_sit = i

    if idx_cnpj is None or (idx_cod is None and idx_sit is None):
        return render_template_string(
            HTML_ATUALIZAR_LOJAS_INFO,
            erro="Não foi possível identificar as colunas necessárias na planilha. "
                 "Certifique-se de que a planilha contenha ao menos as colunas 'CNPJ' e 'CodInfo' (ou 'Situação')."
        )

    conn = get_db_connection()
    todas_lojas = [dict(l) for l in conn.execute("SELECT id, alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, cod_info, cli_situacao FROM clientes_lojas")]

    # Mapeia lojas existentes por CNPJ normalizado (somente dígitos)
    lojas_por_cnpj = {}
    for l in todas_lojas:
        raw_cnpj = l.get('loj_cnpj') or ''
        digs = re.sub(r'\D', '', str(raw_cnpj))
        if digs:
            lojas_por_cnpj.setdefault(digs, []).append(l)
            if len(digs) == 13:
                lojas_por_cnpj.setdefault(digs.zfill(14), []).append(l)
            elif len(digs) == 10:
                lojas_por_cnpj.setdefault(digs.zfill(11), []).append(l)

    aliases_afetados = set()
    amostra = []
    nao_encontrados = []
    linhas_processadas = 0
    updates = []
    ids_atualizados = set()

    for num_linha, linha in enumerate(linhas[1:], start=2):
        if linha is None or all(c is None for c in linha):
            continue

        raw_cnpj = str(linha[idx_cnpj]).strip() if idx_cnpj < len(linha) and linha[idx_cnpj] is not None else ''
        cnpj_digs = re.sub(r'\D', '', raw_cnpj)
        if not cnpj_digs:
            continue

        linhas_processadas += 1

        cod_val = str(linha[idx_cod]).strip() if idx_cod is not None and idx_cod < len(linha) and linha[idx_cod] is not None else ''
        if cod_val.endswith('.0') and cod_val[:-2].isdigit():
            cod_val = cod_val[:-2]

        sit_val = str(linha[idx_sit]).strip() if idx_sit is not None and idx_sit < len(linha) and linha[idx_sit] is not None else ''

        lojas_alvo = lojas_por_cnpj.get(cnpj_digs)
        if not lojas_alvo:
            if len(cnpj_digs) == 13:
                lojas_alvo = lojas_por_cnpj.get(cnpj_digs.zfill(14))
            elif len(cnpj_digs) == 10:
                lojas_alvo = lojas_por_cnpj.get(cnpj_digs.zfill(11))

        if not lojas_alvo:
            nao_encontrados.append({
                "linha": num_linha,
                "cnpj": raw_cnpj,
                "cod_info": cod_val,
                "situacao": sit_val
            })
            continue

        for loja in lojas_alvo:
            novo_cod = cod_val if cod_val != '' else (loja['cod_info'] or '')
            nova_sit = sit_val if sit_val != '' else (loja['cli_situacao'] or '')

            updates.append((novo_cod, nova_sit, loja['id']))
            aliases_afetados.add(loja['alias'])

            if loja['id'] not in ids_atualizados:
                ids_atualizados.add(loja['id'])
                if len(amostra) < 25:
                    amostra.append({
                        "alias": loja['alias'],
                        "loj_codigo": loja['loj_codigo'],
                        "loj_fantasia": loja['loj_fantasia'],
                        "loj_cnpj": loja['loj_cnpj'],
                        "cod_info": novo_cod,
                        "cli_situacao": nova_sit
                    })

    if updates:
        conn.executemany("UPDATE clientes_lojas SET cod_info = ?, cli_situacao = ? WHERE id = ?", updates)
        conn.commit()

    conn.close()

    resultado = {
        "atualizadas": len(ids_atualizados),
        "aliases_afetados": len(aliases_afetados),
        "linhas_processadas": linhas_processadas,
        "amostra": amostra,
        "nao_encontrados": nao_encontrados
    }

    return render_template_string(HTML_ATUALIZAR_LOJAS_INFO, resultado=resultado)


@bancos_bp.route('/admin/atualizar-lojas-info/modelo')
def modelo_atualizar_lojas_info():
    if not session.get('logged_in'):
        return redirect('/admin/login')
    if not tem_permissao('perm_servidores'):
        return "Você não tem permissão para esta ação. <a href='/'>Voltar</a>", 403

    try:
        import openpyxl
        from io import BytesIO
        from flask import send_file
    except ImportError:
        return render_template_string(
            HTML_ATUALIZAR_LOJAS_INFO,
            erro="A biblioteca 'openpyxl' não está instalada no servidor."
        )

    wb = openpyxl.Workbook()
    aba = wb.active
    aba.title = "ATUALIZAR_LOJAS"
    aba.append(['CODINFO', 'CNPJ', 'Situação'])
    aba.append(['1', '12345678000199', 'A'])
    aba.append(['2', '98765432000188', 'I'])

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer, as_attachment=True, download_name='modelo_atualizacao_codinfo_situacao.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


# --- RELATÓRIO CONSOLIDADO DE CLIENTES E LOJAS HOSPEDADAS (.XLSX) ---
def gerar_planilha_relatorio_clientes_lojas(usuario_nome=None):
    """Gera um documento Excel (.xlsx) contendo:
    - Aba 1: Resumo Executivo com indicadores de clientes, lojas, CNPJs e distribuição por servidor.
    - Aba 2: Detalhamento completo de todos os clientes e suas respectivas lojas com auto-filtro.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    bancos_ativos, _, _ = buscar_em_todos_servidores(buscar_inativos=False, forcar_atualizacao=False)
    bancos_inativos, _, _ = buscar_em_todos_servidores(buscar_inativos=True, forcar_atualizacao=False)
    lojas_por_alias = carregar_lojas_clientes()
    bancos_ativos = aplicar_cnames_customizados(bancos_ativos)
    bancos_ativos = aplicar_lojas_clientes(bancos_ativos, cache_lojas=lojas_por_alias)
    bancos_inativos = aplicar_lojas_clientes(bancos_inativos, cache_lojas=lojas_por_alias)

    total_clientes_ativos = len(bancos_ativos)
    total_clientes_inativos = len(bancos_inativos)
    total_clientes_geral = total_clientes_ativos + total_clientes_inativos

    clientes_sem_lojas = 0
    clientes_ok = 0
    clientes_incompletos = 0
    clientes_ignorados = 0

    for b in bancos_ativos:
        lojas = b.get('lojas', [])
        if not lojas:
            clientes_sem_lojas += 1
            b['status_relatorio'] = 'Sem Lojas'
        elif b.get('tem_lojas_incompletas'):
            clientes_incompletos += 1
            b['status_relatorio'] = 'Lojas Incompletas'
        elif any(l.get('ignorar_incompleto') in (1, '1', True) for l in lojas):
            clientes_ignorados += 1
            b['status_relatorio'] = 'Lojas Ignoradas'
        else:
            clientes_ok += 1
            b['status_relatorio'] = 'Cadastro OK'

    for b in bancos_inativos:
        b['status_relatorio'] = 'Inativo'

    conn = get_db_connection()
    todas_lojas = [dict(r) for r in conn.execute("SELECT * FROM clientes_lojas ORDER BY alias, loj_codigo").fetchall()]
    conn.close()

    total_lojas = len(todas_lojas)
    lojas_com_cnpj = sum(1 for l in todas_lojas if (l.get('loj_cnpj') or '').strip() and (l.get('loj_cnpj') or '').strip() != '-')
    lojas_sem_cnpj = total_lojas - lojas_com_cnpj
    cnpjs_unicos = len(set(
        (l.get('loj_cnpj') or '').strip() for l in todas_lojas
        if (l.get('loj_cnpj') or '').strip() and (l.get('loj_cnpj') or '').strip() != '-'
    ))
    lojas_ignoradas = sum(1 for l in todas_lojas if l.get('ignorar_incompleto') in (1, '1', True))
    lojas_incompletas = sum(1 for l in todas_lojas if loja_incompleta(l))
    lojas_completas = sum(1 for l in todas_lojas if not loja_incompleta(l) and not l.get('ignorar_incompleto'))

    por_servidor = {}
    for b in bancos_ativos:
        srv = b.get('servidor') or 'Desconhecido'
        if srv not in por_servidor:
            por_servidor[srv] = {
                'servidor': srv,
                'total_clientes': 0,
                'clientes_ok': 0,
                'clientes_incompletos': 0,
                'clientes_ignorados': 0,
                'clientes_sem_loja': 0,
                'bytes': 0
            }
        info_srv = por_servidor[srv]
        info_srv['total_clientes'] += 1
        info_srv['bytes'] += b.get('tamanho_bytes', 0)
        st = b.get('status_relatorio')
        if st == 'Cadastro OK':
            info_srv['clientes_ok'] += 1
        elif st == 'Lojas Incompletas':
            info_srv['clientes_incompletos'] += 1
        elif st == 'Lojas Ignoradas':
            info_srv['clientes_ignorados'] += 1
        elif st == 'Sem Lojas':
            info_srv['clientes_sem_loja'] += 1

    wb = openpyxl.Workbook()

    fonte_titulo = Font(name='Segoe UI', size=13, bold=True, color='FFFFFF')
    fonte_secao = Font(name='Segoe UI', size=11, bold=True, color='1F4E79')
    fonte_cabecalho = Font(name='Segoe UI', size=10, bold=True, color='FFFFFF')
    fonte_dados = Font(name='Segoe UI', size=10, color='1E293B')
    fonte_dados_bold = Font(name='Segoe UI', size=10, bold=True, color='1E293B')
    fonte_subtitulo = Font(name='Segoe UI', size=9, italic=True, color='64748B')

    fill_topo = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
    fill_secao = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')
    fill_cabecalho = PatternFill(start_color='2563EB', end_color='2563EB', fill_type='solid')
    fill_zebra = PatternFill(start_color='F8FAFC', end_color='F8FAFC', fill_type='solid')
    fill_total = PatternFill(start_color='E2E8F0', end_color='E2E8F0', fill_type='solid')

    borda_fina = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    alinhamento_centro = Alignment(horizontal='center', vertical='center')
    alinhamento_esquerda = Alignment(horizontal='left', vertical='center')

    # =========================================================================
    # ABA 1: RESUMO EXECUTIVO
    # =========================================================================
    ws_resumo = wb.active
    ws_resumo.title = "Resumo Executivo"
    ws_resumo.views.sheetView[0].showGridLines = True

    ws_resumo.merge_cells('A1:E1')
    c_titulo = ws_resumo['A1']
    c_titulo.value = "RELATÓRIO CONSOLIDADO - CLIENTES E LOJAS HOSPEDADAS"
    c_titulo.font = fonte_titulo
    c_titulo.fill = fill_topo
    c_titulo.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws_resumo.row_dimensions[1].height = 32

    ws_resumo.merge_cells('A2:E2')
    c_sub = ws_resumo['A2']
    gerado_por = f"Gerado por: {usuario_nome} | " if usuario_nome else ""
    c_sub.value = f"{gerado_por}Gerado em: {datetime.now().strftime('%d/%m/%Y às %H:%M')} | Sistema InfoMonitor"
    c_sub.font = fonte_subtitulo
    c_sub.alignment = Alignment(horizontal='left', vertical='center', indent=1)
    ws_resumo.row_dimensions[2].height = 20

    pct = lambda n, d: f"{(n / d * 100):.1f}%" if d > 0 else "0.0%"

    # 1. Indicadores de Clientes
    ws_resumo.merge_cells('A4:E4')
    c_sec1 = ws_resumo['A4']
    c_sec1.value = "1. INDICADORES DE CLIENTES HOSPEDADOS"
    c_sec1.font = fonte_secao
    c_sec1.fill = fill_secao
    ws_resumo.row_dimensions[4].height = 24

    cab_cli = ["Métrica", "Quantidade", "% s/ Ativos", "Situação / Conceito", "Ação Recomendada"]
    for col_idx, texto in enumerate(cab_cli, 1):
        cell = ws_resumo.cell(row=5, column=col_idx, value=texto)
        cell.font = fonte_cabecalho
        cell.fill = fill_cabecalho
        cell.alignment = alinhamento_centro if col_idx in (2, 3) else alinhamento_esquerda
        cell.border = borda_fina
    ws_resumo.row_dimensions[5].height = 22

    linhas_kpi_cli = [
        ("Clientes c/ Cadastro 100% OK", clientes_ok, pct(clientes_ok, total_clientes_ativos), "Todas as lojas preenchidas e regulares", "Manter atualizado"),
        ("Clientes c/ Lojas Incompletas", clientes_incompletos, pct(clientes_incompletos, total_clientes_ativos), "Falta CodInfo ou Situação em loja(s)", "Preencher via site ou importar planilha"),
        ("Clientes c/ Lojas Ignoradas", clientes_ignorados, pct(clientes_ignorados, total_clientes_ativos), "Lojas sem pendências ativas (ignoradas)", "Revisar periodicamente"),
        ("Clientes Sem Lojas Cadastradas", clientes_sem_lojas, pct(clientes_sem_lojas, total_clientes_ativos), "Nenhuma loja vinculada ao alias", "Cadastrar lojas ou importar planilha"),
        ("Total de Clientes Ativos Hospedados", total_clientes_ativos, "100.0%", "Bases ativas nos servidores monitorados", "-"),
        ("Total de Clientes Inativados (#)", total_clientes_inativos, "-", "Bancos com prefixo # no databases.conf", "-"),
        ("Total Geral de Bases Registradas", total_clientes_geral, "-", "Soma de ativos e inativos", "-"),
    ]

    for idx, (metrica, qtd, p, desc, acao) in enumerate(linhas_kpi_cli, 6):
        ws_resumo.cell(row=idx, column=1, value=metrica).font = fonte_dados_bold if "Total" in metrica else fonte_dados
        ws_resumo.cell(row=idx, column=2, value=qtd).font = fonte_dados_bold if "Total" in metrica else fonte_dados
        ws_resumo.cell(row=idx, column=3, value=p).font = fonte_dados
        ws_resumo.cell(row=idx, column=4, value=desc).font = fonte_dados
        ws_resumo.cell(row=idx, column=5, value=acao).font = fonte_dados

        for c in range(1, 6):
            cell = ws_resumo.cell(row=idx, column=c)
            cell.border = borda_fina
            if "Total" in metrica:
                cell.fill = fill_total
            elif idx % 2 == 1:
                cell.fill = fill_zebra
            if c in (2, 3):
                cell.alignment = alinhamento_centro
        ws_resumo.row_dimensions[idx].height = 20

    # 2. Indicadores de Lojas e CNPJs
    linha_atual = len(linhas_kpi_cli) + 8
    ws_resumo.merge_cells(f'A{linha_atual}:E{linha_atual}')
    c_sec2 = ws_resumo[f'A{linha_atual}']
    c_sec2.value = "2. INDICADORES DE LOJAS E CNPJS"
    c_sec2.font = fonte_secao
    c_sec2.fill = fill_secao
    ws_resumo.row_dimensions[linha_atual].height = 24

    linha_atual += 1
    cab_loj = ["Métrica", "Quantidade", "% s/ Total Lojas", "Detalhe / Critério", "Status"]
    for col_idx, texto in enumerate(cab_loj, 1):
        cell = ws_resumo.cell(row=linha_atual, column=col_idx, value=texto)
        cell.font = fonte_cabecalho
        cell.fill = fill_cabecalho
        cell.alignment = alinhamento_centro if col_idx in (2, 3) else alinhamento_esquerda
        cell.border = borda_fina
    ws_resumo.row_dimensions[linha_atual].height = 22

    linhas_kpi_loj = [
        ("Total de Lojas Cadastradas", total_lojas, "100.0%", "Registros na tabela clientes_lojas", "Consolidado"),
        ("Lojas com Cadastro Completo (OK)", lojas_completas, pct(lojas_completas, total_lojas), "CodInfo, LojCód e Situação preenchidos", "Regular"),
        ("Lojas com Pendências (Incompletas)", lojas_incompletas, pct(lojas_incompletas, total_lojas), "Faltando CodInfo, Situação ou LojCód", "Atenção"),
        ("Lojas Marcadas como Ignoradas", lojas_ignoradas, pct(lojas_ignoradas, total_lojas), "Desconsideradas de alertas de pendência", "Ignorada"),
        ("Lojas com CNPJ Preenchido", lojas_com_cnpj, pct(lojas_com_cnpj, total_lojas), "Possuem CNPJ informado", "Identificadas"),
        ("Lojas sem CNPJ (Identificadas)", lojas_sem_cnpj, pct(lojas_sem_cnpj, total_lojas), "Não possuem CNPJ cadastrado", "Sem CNPJ"),
        ("Total de CNPJs Únicos Distintos", cnpjs_unicos, "-", "CNPJs únicos distintos no cadastro", "Empresas distintas"),
    ]

    for item in linhas_kpi_loj:
        linha_atual += 1
        metrica, qtd, p, desc, acao = item
        ws_resumo.cell(row=linha_atual, column=1, value=metrica).font = fonte_dados_bold if "Total" in metrica else fonte_dados
        ws_resumo.cell(row=linha_atual, column=2, value=qtd).font = fonte_dados_bold if "Total" in metrica else fonte_dados
        ws_resumo.cell(row=linha_atual, column=3, value=p).font = fonte_dados
        ws_resumo.cell(row=linha_atual, column=4, value=desc).font = fonte_dados
        ws_resumo.cell(row=linha_atual, column=5, value=acao).font = fonte_dados

        for c in range(1, 6):
            cell = ws_resumo.cell(row=linha_atual, column=c)
            cell.border = borda_fina
            if "Total" in metrica:
                cell.fill = fill_total
            elif linha_atual % 2 == 1:
                cell.fill = fill_zebra
            if c in (2, 3):
                cell.alignment = alinhamento_centro
        ws_resumo.row_dimensions[linha_atual].height = 20

    # 3. Distribuição por Servidor
    if por_servidor:
        linha_atual += 3
        ws_resumo.merge_cells(f'A{linha_atual}:G{linha_atual}')
        c_sec3 = ws_resumo[f'A{linha_atual}']
        c_sec3.value = "3. DISTRIBUIÇÃO POR SERVIDOR HOSPEDADO"
        c_sec3.font = fonte_secao
        c_sec3.fill = fill_secao
        ws_resumo.row_dimensions[linha_atual].height = 24

        linha_atual += 1
        cab_srv = ["Servidor", "Total Clientes", "Clientes OK", "Incompletos", "Ignorados", "Sem Lojas", "Espaço Ocupado"]
        for col_idx, texto in enumerate(cab_srv, 1):
            cell = ws_resumo.cell(row=linha_atual, column=col_idx, value=texto)
            cell.font = fonte_cabecalho
            cell.fill = fill_cabecalho
            cell.alignment = alinhamento_centro if col_idx > 1 else alinhamento_esquerda
            cell.border = borda_fina
        ws_resumo.row_dimensions[linha_atual].height = 22

        for srv_nome, srv_data in sorted(por_servidor.items()):
            linha_atual += 1
            tam_str = formatar_tamanho(srv_data['bytes'])
            dados_srv = [
                srv_nome,
                srv_data['total_clientes'],
                srv_data['clientes_ok'],
                srv_data['clientes_incompletos'],
                srv_data['clientes_ignorados'],
                srv_data['clientes_sem_loja'],
                tam_str
            ]
            for c_idx, val in enumerate(dados_srv, 1):
                cell = ws_resumo.cell(row=linha_atual, column=c_idx, value=val)
                cell.font = fonte_dados
                cell.border = borda_fina
                if c_idx > 1:
                    cell.alignment = alinhamento_centro
                if linha_atual % 2 == 1:
                    cell.fill = fill_zebra
            ws_resumo.row_dimensions[linha_atual].height = 20

    # Ajuste de largura das colunas no resumo
    for col in ws_resumo.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            if cell.coordinate in ['A1', 'A2', 'A4']:
                continue
            val = str(cell.value or '')
            if len(val) > max_len:
                max_len = len(val)
        ws_resumo.column_dimensions[col_letter].width = max(max_len + 4, 15)

    # =========================================================================
    # ABA 2: CLIENTES E LOJAS DETALHADO
    # =========================================================================
    ws_detalhe = wb.create_sheet(title="Clientes e Lojas")
    ws_detalhe.views.sheetView[0].showGridLines = True

    cab_detalhe = [
        "Servidor", "Alias (Cliente)", "Status Banco", "Tamanho", "Último Acesso", "Data Criação",
        "Situação Cliente", "CodInfo", "LojCód.", "Nome Fantasia", "Razão Social", "CNPJ", "Situação Loja",
        "Status Loja", "Ignorada?", "Detalhes da Pendência", "String CNAME"
    ]

    for col_idx, texto in enumerate(cab_detalhe, 1):
        cell = ws_detalhe.cell(row=1, column=col_idx, value=texto)
        cell.font = fonte_cabecalho
        cell.fill = fill_topo
        cell.alignment = alinhamento_centro if col_idx in (1, 3, 7, 8, 9, 12, 13, 14, 15) else alinhamento_esquerda
        cell.border = borda_fina
    ws_detalhe.row_dimensions[1].height = 25

    linha_det = 1
    todos_bancos = bancos_ativos + bancos_inativos
    for b in todos_bancos:
        lojas = b.get('lojas', [])
        alias = b.get('alias', '')
        servidor = b.get('servidor', '')
        status_banco = 'Inativo' if b.get('eh_inativo') else 'Ativo'
        tamanho = b.get('tamanho_str', '')
        ultimo_acesso = b.get('data_str', '')
        data_criacao = b.get('data_criacao', '')
        status_cliente = b.get('status_relatorio', '')
        cname = b.get('cname_string', '')

        if not lojas:
            linha_det += 1
            dados_linha = [
                servidor, alias, status_banco, tamanho, ultimo_acesso, data_criacao,
                status_cliente, "-", "-", "-", "-", "-", "-",
                "Sem Loja Cadastrada", "Não", "Nenhuma loja vinculada a este alias", cname
            ]
            for col_idx, valor in enumerate(dados_linha, 1):
                cell = ws_detalhe.cell(row=linha_det, column=col_idx, value=valor)
                cell.font = fonte_dados
                cell.border = borda_fina
                if col_idx in (1, 3, 7, 8, 9, 12, 13, 14, 15):
                    cell.alignment = alinhamento_centro
            ws_detalhe.row_dimensions[linha_det].height = 19
        else:
            for l in lojas:
                linha_det += 1
                cod_info = l.get('cod_info') or '-'
                loj_codigo = l.get('loj_codigo') or '-'
                fantasia = l.get('loj_fantasia') or '-'
                razao = l.get('loj_nome') or '-'
                cnpj = l.get('loj_cnpj') or '-'
                situacao = l.get('cli_situacao') or '-'
                eh_ignorado = l.get('ignorar_incompleto') in (1, '1', True)
                texto_ignorado = "Sim" if eh_ignorado else "Não"

                if eh_ignorado:
                    status_loja = "Ignorada"
                    detalhe_loja = "Marcada para ignorar pendências"
                elif loja_incompleta(l):
                    status_loja = "Incompleta"
                    faltantes = []
                    if not (l.get('cod_info') or '').strip(): faltantes.append('CodInfo')
                    if not (l.get('cli_situacao') or '').strip(): faltantes.append('Situação')
                    if not (l.get('loj_codigo') or '').strip(): faltantes.append('LojCód.')
                    if not (l.get('loj_cnpj') or '').strip() or (l.get('loj_cnpj') or '').strip() == '-': faltantes.append('Sem CNPJ')
                    detalhe_loja = "Pendente: " + ", ".join(faltantes)
                else:
                    status_loja = "Completa"
                    detalhe_loja = "Cadastro completo"

                dados_linha = [
                    servidor, alias, status_banco, tamanho, ultimo_acesso, data_criacao,
                    status_cliente, cod_info, loj_codigo, fantasia, razao, cnpj, situacao,
                    status_loja, texto_ignorado, detalhe_loja, cname
                ]
                for col_idx, valor in enumerate(dados_linha, 1):
                    cell = ws_detalhe.cell(row=linha_det, column=col_idx, value=valor)
                    cell.font = fonte_dados
                    cell.border = borda_fina
                    if col_idx in (1, 3, 7, 8, 9, 12, 13, 14, 15):
                        cell.alignment = alinhamento_centro
                ws_detalhe.row_dimensions[linha_det].height = 19

    # Inclui lojas cadastradas que não estejam em nenhum alias ativo/inativo
    aliases_mapeados = set(b.get('alias', '').strip().lower() for b in todos_bancos)
    for l in todas_lojas:
        alias_loja = (l.get('alias') or '').strip()
        if alias_loja.lower() not in aliases_mapeados:
            linha_det += 1
            cod_info = l.get('cod_info') or '-'
            loj_codigo = l.get('loj_codigo') or '-'
            fantasia = l.get('loj_fantasia') or '-'
            razao = l.get('loj_nome') or '-'
            cnpj = l.get('loj_cnpj') or '-'
            situacao = l.get('cli_situacao') or '-'
            eh_ignorado = l.get('ignorar_incompleto') in (1, '1', True)
            texto_ignorado = "Sim" if eh_ignorado else "Não"
            status_loja = "Ignorada" if eh_ignorado else ("Incompleta" if loja_incompleta(l) else "Completa")
            dados_linha = [
                "-", alias_loja, "Sem banco no servidor", "-", "-", "-",
                "Desconhecido", cod_info, loj_codigo, fantasia, razao, cnpj, situacao,
                status_loja, texto_ignorado, "Loja cadastrada sem banco correspondente nos servidores", "-"
            ]
            for col_idx, valor in enumerate(dados_linha, 1):
                cell = ws_detalhe.cell(row=linha_det, column=col_idx, value=valor)
                cell.font = fonte_dados
                cell.border = borda_fina
                if col_idx in (1, 3, 7, 8, 9, 12, 13, 14, 15):
                    cell.alignment = alinhamento_centro
            ws_detalhe.row_dimensions[linha_det].height = 19

    max_col_letra = get_column_letter(len(cab_detalhe))
    ws_detalhe.auto_filter.ref = f"A1:{max_col_letra}{linha_det}"

    for col in ws_detalhe.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val = str(cell.value or '')
            if len(val) > max_len:
                max_len = len(val)
        ws_detalhe.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@bancos_bp.route('/relatorio-clientes-lojas')
def rota_relatorio_clientes_lojas():
    try:
        from flask import send_file
    except ImportError:
        return "Erro: Flask send_file indisponível", 500

    usuario_nome = session.get('user_nome')
    try:
        buffer = gerar_planilha_relatorio_clientes_lojas(usuario_nome=usuario_nome)
    except Exception as e:
        LOGGER.exception("Erro ao gerar relatório consolidado de clientes e lojas: %s", e)
        return f"Não foi possível gerar a planilha no momento: {e}", 500

    nome_arquivo = f"relatorio_clientes_lojas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


def assinatura_cname(host):
    return hmac.new(current_app.secret_key.encode(), host.strip().encode(), hashlib.sha256).hexdigest()


bancos_bp.add_app_template_global(assinatura_cname, name='assinatura_cname')

# Cache para resultados de CNAME (evita sobrecarga DNS)
_cname_cache = {}
_cname_cache_lock = threading.Lock()
CNAME_CACHE_TTL = 300  # 5 minutos
CNAME_RATE_LIMIT_POR_SEGUNDO = 100
CNAME_RATE_LIMIT_COOLDOWN = 10
_cname_rate_lock = threading.Lock()
_cname_rate_limit = {}
_cname_cooldown = {}


def _get_cached_cname(host):
    """Obtém resultado do cache se válido."""
    agora = time.time()
    with _cname_cache_lock:
        if host in _cname_cache:
            resultado, timestamp = _cname_cache[host]
            if agora - timestamp < CNAME_CACHE_TTL:
                return resultado
            else:
                del _cname_cache[host]
    return None


def _set_cached_cname(host, resultado):
    """Armazena resultado no cache."""
    with _cname_cache_lock:
        _cname_cache[host] = (resultado, time.time())


@bancos_bp.route('/api/cname/testar')
def api_testar_cname():
    host = request.args.get('host', '').strip()
    assinatura = request.args.get('assinatura', '')
    if not host or len(host) > 1024 or not hmac.compare_digest(assinatura_cname(host).encode(), assinatura.encode()):
        return jsonify(ativo=False, erro='CNAME inválido.'), 400
    
    host_limpo = host.split('/')[0].split(':')[0]
    
    # Verifica cache primeiro
    cached = _get_cached_cname(host_limpo)
    if cached is not None:
        return jsonify(ativo=cached, cached=True)
    
    # Limite alto para suportar telas grandes; o navegador só consulta linhas visíveis.
    ip = request.remote_addr or 'unknown'
    agora = time.time()
    with _cname_rate_lock:
        cooldown_ate = _cname_cooldown.get(ip, 0)
        if agora < cooldown_ate:
            tempo_restante = max(1, int(cooldown_ate - agora))
            return jsonify(
                ativo=False,
                erro=f'Limite de consultas atingido. Aguarde {tempo_restante}s.',
                cooldown=tempo_restante,
                retry_after=tempo_restante
            ), 429

        recentes = [t for t in _cname_rate_limit.get(ip, []) if agora - t < 1.0]
        if len(recentes) >= CNAME_RATE_LIMIT_POR_SEGUNDO:
            _cname_cooldown[ip] = agora + CNAME_RATE_LIMIT_COOLDOWN
            _cname_rate_limit[ip] = recentes
            return jsonify(
                ativo=False,
                erro=f'Limite de {CNAME_RATE_LIMIT_POR_SEGUNDO} consultas por segundo atingido.',
                cooldown=CNAME_RATE_LIMIT_COOLDOWN,
                retry_after=CNAME_RATE_LIMIT_COOLDOWN
            ), 429

        recentes.append(agora)
        _cname_rate_limit[ip] = recentes
    
    try:
        socket.gethostbyname(host_limpo)
        ativo = True
    except (OSError, UnicodeError):
        ativo = False
    
    # Armazena no cache
    _set_cached_cname(host_limpo, ativo)
    
    return jsonify(ativo=ativo, cached=False)


@bancos_bp.route('/admin/cname/salvar', methods=['POST'])
def salvar_cname_customizado():
    if not session.get('logged_in'):
        return jsonify({"sucesso": False, "mensagem": "Sessão expirada. Faça login novamente."}), 401
    if not tem_permissao('perm_servidores'):
        return jsonify({"sucesso": False, "mensagem": "Você não tem permissão para editar o CNAME."}), 403

    dados = request.get_json(silent=True) or {}
    alias = (dados.get('alias') or '').strip()
    cname = (dados.get('cname') or '').strip()

    if not alias or not cname:
        return jsonify({"sucesso": False, "mensagem": "Alias e CNAME são obrigatórios."}), 400

    conn = get_db_connection()
    conn.execute(
        "INSERT INTO cnames_custom (alias, cname_customizado) VALUES (?, ?) "
        "ON CONFLICT(alias) DO UPDATE SET cname_customizado = excluded.cname_customizado",
        (alias, cname)
    )
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True})


# --- TEMPLATE HTML: STATUS DE BACKUPS FTP (versão estilizada da página de texto simples) ---
HTML_BACKUPS_FTP = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Status de Backups FTP</title>
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--cor-fundo); color: var(--cor-texto); margin: 0; padding: 20px; }
        .topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; flex-wrap: wrap; gap: 10px; }
        .topo h2 { margin: 0; }
        .btn { padding: 8px 14px; background: var(--cor-superficie); color: var(--cor-texto); border: 1px solid var(--cor-borda); border-radius: var(--raio); cursor: pointer; font-weight: 600; text-decoration: none; font-size: 0.85em; display: inline-flex; align-items: center; gap: 6px; }
        .btn:hover { background: #f1f5f9; }
        .btn-disparo { background: var(--cor-primaria); color: #ffffff; border-color: var(--cor-primaria); }
        .btn-disparo:hover { background: #1d4ed8; }
        .stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
        .stat-card { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-radius: var(--raio); padding: 16px 22px; min-width: 150px; flex: 1; }
        .stat-card .valor { font-size: 2em; font-weight: 700; }
        .stat-card .rotulo { color: var(--cor-texto-suave); font-size: 0.85em; font-weight: 600; margin-top: 4px; }
        .stat-ok .valor { color: var(--cor-sucesso); }
        .stat-atraso .valor { color: var(--cor-aviso); }
        .stat-erro .valor { color: var(--cor-perigo); }
        .filtros { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
        .filtros a.ativo { background: var(--cor-primaria); color: white; border-color: var(--cor-primaria); }
        .grid-clientes { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
        .card-cliente { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-left: 4px solid var(--cor-borda); border-radius: var(--raio); padding: 12px 14px; transition: transform 0.15s ease, box-shadow 0.15s ease; }
        .card-cliente:hover { transform: translateY(-2px); box-shadow: 0 4px 10px rgba(0,0,0,0.06); }
        .card-cliente.status-OK { border-left-color: var(--cor-sucesso); }
        .card-cliente.status-ATRASADO { border-left-color: var(--cor-aviso); }
        .card-cliente.status-SEM_ARQUIVO, .card-cliente.status-ERRO { border-left-color: var(--cor-perigo); }
        .card-cliente.critico { border-left: 5px solid var(--cor-perigo); background: #fffdf5; box-shadow: 0 0 0 1px #fed7aa; }
        .card-cliente .alias { font-weight: 700; word-break: break-word; }
        .card-cliente .info { font-size: 0.8em; color: var(--cor-texto-suave); margin-top: 4px; }
        .badge-status { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.72em; font-weight: 700; margin-top: 6px; }
        .badge-OK { background: #f0fdf4; color: #166534; }
        .badge-ATRASADO { background: #fffbeb; color: #92400e; }
        .badge-SEM_ARQUIVO, .badge-ERRO { background: #fef2f2; color: #991b1b; }
        .badge-CRITICO { background: #fee2e2; color: #991b1b; font-weight: 800; border: 1px solid #fecaca; }
        .rodape-info { color: var(--cor-texto-suave); font-size: 0.85em; margin-top: 20px; }
        .alerta-erro { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 14px 18px; border-radius: var(--raio); margin-bottom: 18px; }
        .alerta-sucesso { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; padding: 14px 18px; border-radius: var(--raio); margin-bottom: 18px; }

        /* Card do Marcador de E-mail Diário */
        .card-alerta-email { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-radius: var(--raio); padding: 16px 20px; margin-bottom: 20px; box-shadow: 0 2px 6px rgba(0,0,0,0.03); }
        .alerta-email-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 10px; }
        .alerta-email-info { display: flex; align-items: center; gap: 12px; }
        .icone-email { font-size: 1.8em; }
        .badge-marcador { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 0.78em; font-weight: 700; }
        .badge-marcador-sucesso { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }
        .badge-marcador-aviso { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
        .badge-marcador-info { background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }

        /* Bloco de Histórico */
        .bloco-historico { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-radius: var(--raio); padding: 18px 20px; margin-top: 26px; }
        .bloco-historico-topo { margin-bottom: 14px; }
        .bloco-historico-topo h3 { margin: 0 0 4px 0; font-size: 1.1em; }
        .historico-scroll {
            max-height: 440px;
            overflow-y: auto;
            overflow-x: auto;
            border: 1px solid var(--cor-borda);
            border-radius: var(--raio-md, 6px);
        }
        .tabela-historico { width: 100%; border-collapse: collapse; font-size: 0.85em; text-align: left; }
        .tabela-historico thead th {
            position: sticky;
            top: 0;
            z-index: 2;
            background: var(--cor-fundo);
            padding: 10px 12px;
            border-bottom: 1px solid var(--cor-borda);
            color: var(--cor-texto-suave);
            font-weight: 600;
            box-shadow: 0 1px 0 var(--cor-borda);
        }
        .tabela-historico td { padding: 10px 12px; border-bottom: 1px solid var(--cor-borda); vertical-align: middle; }
        .tabela-historico tr:hover td { background: var(--cor-superficie-hover); }
        .badge-DISPENSADO {
            background: var(--cor-sucesso-bg);
            color: var(--cor-sucesso);
            border: 1px solid var(--cor-sucesso-borda);
        }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <div class="topo">
        <h2>💽 Status de Backups FTP</h2>
        <div style="display:flex; gap:8px;">
            <a href="/" class="btn">⬅ Voltar</a>
            <a href="/backups-ftp?atualizar=1{% if filtro %}&filtro={{ filtro }}{% endif %}" class="btn">🔄 Atualizar</a>
        </div>
    </div>

    {% if msg_sucesso %}
        <div class="alerta-sucesso">✅ {{ msg_sucesso }}</div>
    {% endif %}
    {% if msg_erro %}
        <div class="alerta-erro">⚠️ {{ msg_erro }}</div>
    {% endif %}

    {% if status.erro %}
        <div class="alerta-erro">⚠️ {{ status.erro }}</div>
    {% else %}
        <!-- Marcador de E-mail Diário (09:00) -->
        <div class="card-alerta-email">
            <div class="alerta-email-header">
                <div class="alerta-email-info">
                    <span class="icone-email">✉️</span>
                    <div>
                        <h3 style="margin:0; font-size: 1.05em; color: var(--cor-texto);">Alerta Diário de Backups em Atraso</h3>
                        <div style="font-size: 0.82em; color: var(--cor-texto-suave); margin-top: 3px;">
                            Destinatário: <code>suporte@infobrasilsistemas.com.br</code> • Envio diário às 09:00 quando houver clientes com atraso ≥ 3 dias (máximo 1 por dia)
                        </div>
                    </div>
                </div>
                <div style="display:flex; align-items:center; gap: 10px; flex-wrap: wrap;">
                    <span class="badge-marcador badge-marcador-{{ marcador_email.badge_tipo }}">{{ marcador_email.badge_rotulo }}</span>
                    {% if pode_disparar %}
                        <form action="/admin/backups-ftp/disparar-alerta" method="POST" style="margin:0;">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <input type="hidden" name="forcar" value="1">
                            <button type="submit" class="btn btn-disparo" onclick="return confirm('Confirmar o envio manual do relatório de alertas para suporte@infobrasilsistemas.com.br?');" title="Dispara o envio de e-mail imediatamente">
                                ⚡ Disparar Alerta Agora
                            </button>
                        </form>
                    {% endif %}
                </div>
            </div>
            <div class="alerta-email-corpo">
                <p style="margin: 0; font-size: 0.88em; color: var(--cor-texto);">
                    <strong>Status de hoje:</strong> {{ marcador_email.texto_status }}
                </p>
                {% if marcador_email.detalhes %}
                    <div style="font-size: 0.8em; color: var(--cor-texto-suave); margin-top: 4px;">{{ marcador_email.detalhes }}</div>
                {% endif %}
            </div>
        </div>

        <div class="stats">
            <div class="stat-card stat-ok">
                <div class="valor">{{ status.total_ok }}</div>
                <div class="rotulo">✅ Backups em dia</div>
            </div>
            <div class="stat-card stat-atraso">
                <div class="valor">{{ status.total_atrasado }}</div>
                <div class="rotulo">🕒 Atrasados (1 dia ou mais)</div>
            </div>
            <div class="stat-card stat-erro">
                <div class="valor">{{ status.total_erro }}</div>
                <div class="rotulo">❌ Sem arquivo / erro</div>
            </div>
        </div>

        <div class="filtros">
            <a href="/backups-ftp" class="btn {% if not filtro %}ativo{% endif %}">Todos ({{ status.clientes|length }})</a>
            <a href="/backups-ftp?filtro=ok" class="btn {% if filtro == 'ok' %}ativo{% endif %}">✅ OK</a>
            <a href="/backups-ftp?filtro=atrasado" class="btn {% if filtro == 'atrasado' %}ativo{% endif %}">🕒 Atrasados</a>
            <a href="/backups-ftp?filtro=sem_arquivo" class="btn {% if filtro == 'sem_arquivo' %}ativo{% endif %}">❌ Sem arquivo</a>
        </div>

        <div class="grid-clientes">
            {% for c in clientes %}
                <div class="card-cliente status-{{ c.status }} {% if c.critico %}critico{% endif %}">
                    <div class="alias">
                        {{ c.alias }}
                        {% if c.critico %}<span style="font-size: 0.8em; color: var(--cor-perigo); margin-left: 4px;">⚠️ ({{ c.dias_atraso }} dias)</span>{% endif %}
                    </div>
                    {% if c.timestamp %}<div class="info">🕒 {{ c.timestamp }}{% if c.idade %} ({{ c.idade }}){% endif %}</div>{% endif %}
                    {% if c.caminho %}<div class="info" title="{{ c.caminho }}">📁 {{ c.caminho }}</div>{% endif %}
                    <span class="badge-status badge-{{ c.status }}">
                        {% if c.status == 'OK' %}✅ Em dia
                        {% elif c.status == 'ATRASADO' %}
                            {% if c.critico %}🚨 Atraso Crítico ({{ c.dias_atraso }}d)
                            {% else %}🕒 Atrasado{% endif %}
                        {% elif c.status == 'SEM_ARQUIVO' %}❌ Sem arquivo
                        {% else %}❌ Erro{% endif %}
                    </span>
                </div>
            {% else %}
                <p style="color: var(--cor-texto-suave);">Nenhum cliente encontrado para este filtro.</p>
            {% endfor %}
        </div>

        <p class="rodape-info">Última atualização informada pelo servidor de backups: {{ status.ultima_atualizacao or '—' }}</p>

        <!-- Histórico de Alertas de Backups Enviados -->
        <div class="bloco-historico">
            <div class="bloco-historico-topo">
                <h3>📜 Histórico de Alertas de Backup Enviados</h3>
                <span style="font-size: 0.82em; color: var(--cor-texto-suave);">Registros de disparos automáticos às {{ horario_alerta or '9h' }} e manuais</span>
            </div>
            {% if historico_emails %}
                <div class="historico-scroll" tabindex="0" role="region" aria-label="Histórico de alertas de backup">
                    <table class="tabela-historico">
                        <thead>
                            <tr>
                                <th>Data / Hora</th>
                                <th>Situação</th>
                                <th>Destinatário</th>
                                <th>Totais</th>
                                <th>Clientes Críticos</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for h in historico_emails %}
                                <tr>
                                    <td style="font-weight: 600; white-space: nowrap;">{{ h.data_envio }}</td>
                                    <td>
                                        {% if h.status_envio == 'DISPENSADO' %}
                                            <span class="badge-status badge-DISPENSADO">Dispensado (tudo em dia)</span>
                                        {% elif h.status_envio == 'ENVIADO' %}
                                            <span class="badge-status badge-OK">✅ {{ h.situacao_texto or ('Enviado (manual)' if h.eh_manual else 'Enviado') }}</span>
                                        {% else %}
                                            <span class="badge-status badge-ERRO">❌ {{ h.situacao_texto or ('Falha (manual)' if h.eh_manual else 'Falha') }}</span>
                                        {% endif %}
                                    </td>
                                    <td style="color: var(--cor-texto-suave);">
                                        {% if h.destinatario %}
                                            <code>{{ h.destinatario }}</code>
                                        {% else %}
                                            —
                                        {% endif %}
                                    </td>
                                    <td style="white-space: nowrap; font-size: 0.85em;">
                                        {% if h.total_ok is not none or h.total_atrasado is not none or h.total_critico is not none %}
                                            <span style="color: var(--cor-sucesso);">OK: {{ h.total_ok or 0 }}</span> |
                                            <span style="color: var(--cor-aviso);">Atraso: {{ h.total_atrasado or 0 }}</span> |
                                            <span style="color: var(--cor-perigo); font-weight: 700;">Críticos: {{ h.total_critico or 0 }}</span>
                                        {% else %}
                                            —
                                        {% endif %}
                                    </td>
                                    <td style="font-size: 0.85em; max-width: 280px; word-break: break-word;">
                                        {{ h.clientes_criticos or '—' }}
                                    </td>
                                </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% else %}
                <p style="color: var(--cor-texto-suave); font-size: 0.85em; margin: 10px 0;">Nenhum envio registrado no histórico ainda.</p>
            {% endif %}
        </div>
    {% endif %}
</body>
</html>
"""


@bancos_bp.route('/backups-ftp')
def exibir_backups_ftp():
    forcar = solicitar_refresh('backups_ftp')
    status = obter_status_backups_ftp(forcar_atualizacao=forcar)
    filtro = request.args.get('filtro', '')  # '', 'ok', 'atrasado', 'sem_arquivo'
    msg_sucesso = request.args.get('msg_sucesso')
    msg_erro = request.args.get('msg_erro')

    clientes = list(status.get('clientes') or [])
    analise = analisar_status_para_alerta(clientes)

    conn = get_db_connection()
    try:
        marcador_email = obter_marcador_alerta_hoje(conn, analise)
        historico_emails = obter_historico_alertas_backups(conn, limite=100, limite_dias=30)
    finally:
        conn.close()

    mapa_analise = {c['alias']: c for c in analise.get('todos_ordenados', [])}
    for c in clientes:
        info_c = mapa_analise.get(c['alias'])
        if info_c:
            c['dias_atraso'] = info_c['dias_atraso']
            c['critico'] = info_c['critico']
        else:
            c['dias_atraso'] = 0
            c['critico'] = False

    if filtro == 'ok':
        clientes = [c for c in clientes if c['status'] == 'OK']
    elif filtro == 'atrasado':
        clientes = [c for c in clientes if c['status'] == 'ATRASADO']
    elif filtro == 'sem_arquivo':
        clientes = [c for c in clientes if c['status'] in ('SEM_ARQUIVO', 'ERRO')]

    clientes = sorted(clientes, key=lambda c: c['alias'].lower())

    pode_disparar = bool(session.get('logged_in') and (session.get('eh_master') or tem_permissao('perm_servidores')))

    return render_template_string(
        HTML_BACKUPS_FTP,
        status=status,
        clientes=clientes,
        filtro=filtro,
        marcador_email=marcador_email,
        historico_emails=historico_emails,
        analise=analise,
        pode_disparar=pode_disparar,
        horario_alerta=obter_horario_agendado_alerta_backups(),
        msg_sucesso=msg_sucesso,
        msg_erro=msg_erro
    )


@bancos_bp.route('/admin/backups-ftp/disparar-alerta', methods=['POST'])
def disparar_alerta_backups_manual():
    """Disparo manual de alerta de backups para administradores autorizados."""
    if not session.get('logged_in'):
        return redirect(url_for('bancos.admin_login', next='/backups-ftp'))
    if not (session.get('eh_master') or tem_permissao('perm_servidores')):
        return "Você não tem permissão para esta ação.", 403

    usuario_nome = nome_para_assinatura(session.get('user_nome', 'Admin'))
    forcar = request.form.get('forcar') == '1'
    sucesso, msg = executar_rotina_alerta_backups(forcar=forcar, usuario_solicitante=usuario_nome)
    if sucesso:
        return redirect(url_for('bancos.exibir_backups_ftp', msg_sucesso=msg))
    return redirect(url_for('bancos.exibir_backups_ftp', msg_erro=msg))



@bancos_bp.route('/')
@bancos_bp.route('/servidor/<nome_servidor>')
def exibir_servidor(nome_servidor='DB01'):
    if nome_servidor not in SERVIDORES:
        nome_servidor = next(iter(SERVIDORES), '')
    ordem = request.args.get('ordem', 'nome')
    busca_termo = request.args.get('busca', '').strip()
    filtro_status = request.args.get('filtro', '').strip()
    forcar_atualizacao = solicitar_refresh('dados_servidores', limpar_cache=True)

    metricas_servidores = obter_metricas_servidores(salvar_db=False)
    
    if busca_termo:
        bancos_brutos, _, _ = buscar_em_todos_servidores(busca_termo, modo_busca_unificada=True)
    elif not nome_servidor:
        bancos_brutos = []
    else:
        info = SERVIDORES[nome_servidor]
        bancos_brutos, _, _ = obter_dados_servidor_linux(nome_servidor, info, buscar_inativos=False)

    todos_bancos, _, _ = buscar_em_todos_servidores(buscar_inativos=False, forcar_atualizacao=False)
    todos_bancos_validos = sorted([b for b in todos_bancos if b.get('arquivo_existe')], key=lambda x: x['tamanho_bytes'], reverse=True)
    top5_global = todos_bancos_validos[:5]
    ultimos_hospedados_global = sorted(
        [b for b in todos_bancos if b.get('arquivo_existe')],
        key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True
    )[:3]

    total_atencao = sum(1 for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == 'atencao')
    total_critico = sum(1 for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == 'critico')

    if filtro_status in ['atencao', 'critico']:
        bancos = [b for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == filtro_status]
    else: bancos = bancos_brutos

    total_calculado = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))
    total_tamanho = ('Indisponível' if any(b.get('erro') for b in bancos_brutos)
                     else total_calculado)

    if ordem == 'servidor': bancos.sort(key=lambda x: (x.get('servidor', ''), x['alias'].lower()))
    elif ordem == 'tamanho': bancos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': bancos.sort(key=lambda x: x['timestamp'], reverse=True)
    elif ordem == 'data_criacao': bancos.sort(key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True)
    elif ordem == 'cname': bancos.sort(key=lambda x: x['cname_string'])
    else: bancos.sort(key=lambda x: x['alias'].lower())

    bancos = aplicar_cnames_customizados(bancos)
    bancos = aplicar_lojas_clientes(bancos)

    if filtro_status == 'sem_lojas':
        bancos = [b for b in bancos if not b.get('lojas')]
        total_calculado = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))
        total_tamanho = ('Indisponível' if any(b.get('erro') for b in bancos_brutos)
                         else total_calculado)
    elif filtro_status == 'lojas_incompletas':
        bancos = [b for b in bancos if b.get('tem_lojas_incompletas')]
        total_calculado = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))
        total_tamanho = ('Indisponível' if any(b.get('erro') for b in bancos_brutos)
                         else total_calculado)

    return render_template_string(
        HTML_LAYOUT, servidores=SERVIDORES, servidor_atual=nome_servidor, bancos=bancos,
        total_tamanho=total_tamanho, total_atencao=total_atencao, total_critico=total_critico,
        ordem_atual=ordem, busca_termo=busca_termo, filtro_status=filtro_status,
        modo_todos=False, modo_inativos=False, modo_orfaos=False, modo_historico=False,
        metricas_servidores=metricas_servidores, top5_global=top5_global, ultimos_hospedados_global=ultimos_hospedados_global, arquivos_orfaos=[], dados_historico=[],
        status_backups=obter_status_backups_ftp()
    )

@bancos_bp.route('/todos')
def exibir_todos():
    ordem = request.args.get('ordem', 'nome')
    busca_termo = request.args.get('busca', '').strip()
    filtro_status = request.args.get('filtro', '').strip()
    forcar_atualizacao = solicitar_refresh('dados_servidores', limpar_cache=True)

    metricas_servidores = obter_metricas_servidores(salvar_db=False)
    bancos_brutos, _, _ = buscar_em_todos_servidores(busca_termo if busca_termo else None, modo_busca_unificada=bool(busca_termo))

    todos_bancos_validos = sorted([b for b in bancos_brutos if b.get('arquivo_existe')], key=lambda x: x['tamanho_bytes'], reverse=True)
    top5_global = todos_bancos_validos[:5]
    ultimos_hospedados_global = sorted(
        [b for b in bancos_brutos if b.get('arquivo_existe')],
        key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True
    )[:3]

    total_atencao = sum(1 for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == 'atencao')
    total_critico = sum(1 for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == 'critico')

    bancos = [b for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == filtro_status] if filtro_status in ['atencao', 'critico'] else bancos_brutos
    total_calculado = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))
    total_tamanho = (f'Parcial: {total_calculado}' if any(b.get('erro') for b in bancos_brutos)
                     else total_calculado)

    if ordem == 'servidor': bancos.sort(key=lambda x: (x.get('servidor', ''), x['alias'].lower()))
    elif ordem == 'tamanho': bancos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': bancos.sort(key=lambda x: x['timestamp'], reverse=True)
    elif ordem == 'data_criacao': bancos.sort(key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True)
    elif ordem == 'cname': bancos.sort(key=lambda x: x['cname_string'])
    else: bancos.sort(key=lambda x: x['alias'].lower())

    bancos = aplicar_cnames_customizados(bancos)
    bancos = aplicar_lojas_clientes(bancos)

    # FIX: filtros baseados em lojas precisam ser aplicados depois de anexar as lojas
    if filtro_status == 'sem_lojas':
        bancos = [b for b in bancos if not b.get('lojas')]
        total_calculado = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))
        total_tamanho = (f'Parcial: {total_calculado}' if any(b.get('erro') for b in bancos_brutos)
                         else total_calculado)
    elif filtro_status == 'lojas_incompletas':
        bancos = [b for b in bancos if b.get('tem_lojas_incompletas')]
        total_calculado = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))
        total_tamanho = (f'Parcial: {total_calculado}' if any(b.get('erro') for b in bancos_brutos)
                         else total_calculado)

    return render_template_string(
        HTML_LAYOUT, servidores=SERVIDORES, servidor_atual='TODOS', bancos=bancos,
        total_tamanho=total_tamanho, total_atencao=total_atencao, total_critico=total_critico,
        ordem_atual=ordem, busca_termo=busca_termo, filtro_status=filtro_status,
        modo_todos=True, modo_inativos=False, modo_orfaos=False, modo_historico=False,
        metricas_servidores=metricas_servidores, top5_global=top5_global, ultimos_hospedados_global=ultimos_hospedados_global, arquivos_orfaos=[], dados_historico=[],
        status_backups=obter_status_backups_ftp()
    )

@bancos_bp.route('/inativos')
def exibir_inativos():
    ordem = request.args.get('ordem', 'nome')
    busca_termo = request.args.get('busca', '').strip()
    forcar_atualizacao = solicitar_refresh('dados_servidores', limpar_cache=True)

    metricas_servidores = obter_metricas_servidores(salvar_db=False)
    bancos, total_bytes, _ = buscar_em_todos_servidores(busca_termo, buscar_inativos=True, modo_busca_unificada=bool(busca_termo))
    total_formatado = formatar_tamanho(total_bytes)
    total_tamanho = (f'Parcial: {total_formatado}' if any(b.get('erro') for b in bancos)
                     else total_formatado)

    if ordem == 'servidor': bancos.sort(key=lambda x: (x.get('servidor', ''), x['alias'].lower()))
    elif ordem == 'tamanho': bancos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': bancos.sort(key=lambda x: x['timestamp'], reverse=True)
    elif ordem == 'data_criacao': bancos.sort(key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True)
    elif ordem == 'cname': bancos.sort(key=lambda x: x['cname_string'])
    else: bancos.sort(key=lambda x: x['alias'].lower())

    bancos = aplicar_cnames_customizados(bancos)
    bancos = aplicar_lojas_clientes(bancos)

    return render_template_string(
        HTML_LAYOUT, servidores=SERVIDORES, servidor_atual='INATIVOS', bancos=bancos,
        total_tamanho=total_tamanho, total_atencao=0, total_critico=0,
        ordem_atual=ordem, busca_termo=busca_termo, filtro_status='',
        modo_todos=False, modo_inativos=True, modo_orfaos=False, modo_historico=False,
        metricas_servidores=metricas_servidores, top5_global=[], ultimos_hospedados_global=[], arquivos_orfaos=[], dados_historico=[]
    )

@bancos_bp.route('/orfaos')
def exibir_orfaos():
    ordem = request.args.get('ordem', 'servidor')
    forcar_atualizacao = solicitar_refresh('dados_servidores', limpar_cache=True)

    # A coleta de órfãos também preenche o cache de bancos; as métricas reutilizam
    # esse resultado e não abrem uma segunda conexão SSH na mesma requisição.
    bancos_coleta, _, arquivos_orfaos = buscar_em_todos_servidores(varrer_orfaos=True)
    erros_orfaos = [
        {'servidor': banco.get('servidor', 'desconhecido'), 'mensagem': banco.get('erro', 'Falha desconhecida')}
        for banco in bancos_coleta if banco.get('erro')
    ]
    metricas_servidores = obter_metricas_servidores(salvar_db=False)
    total_formatado = formatar_tamanho(sum(a['tamanho_bytes'] for a in arquivos_orfaos))
    total_tamanho = f'Parcial: {total_formatado}' if erros_orfaos else total_formatado

    if ordem == 'tamanho': arquivos_orfaos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': arquivos_orfaos.sort(key=lambda x: x['timestamp'], reverse=True)
    else: arquivos_orfaos.sort(key=lambda x: (x['servidor'], x['caminho_exibicao']))

    return render_template_string(
        HTML_LAYOUT, servidores=SERVIDORES, servidor_atual='ORFAOS', bancos=[],
        total_tamanho=total_tamanho, total_atencao=0, total_critico=0,
        ordem_atual=ordem, busca_termo='', filtro_status='',
        modo_todos=False, modo_inativos=False, modo_orfaos=True, modo_historico=False,
        metricas_servidores=metricas_servidores, top5_global=[], ultimos_hospedados_global=[],
        arquivos_orfaos=arquivos_orfaos, erros_orfaos=erros_orfaos, dados_historico=[]
    )

def _data_iso_valida(valor):
    try:
        datetime.strptime(valor, '%Y-%m-%d')
        return True
    except (TypeError, ValueError):
        return False


@bancos_bp.route('/historico')
def exibir_historico():
    servidores_solicitados = request.args.getlist('servidores')
    servidores_selecionados = list(dict.fromkeys(servidores_solicitados))
    if (len(servidores_selecionados) > 50 or
            any(servidor not in SERVIDORES for servidor in servidores_selecionados)):
        return 'Filtro de servidor inválido.', 400
    data_inicio = request.args.get('data_inicio', '').strip()
    data_fim = request.args.get('data_fim', '').strip()
    if ((data_inicio and not _data_iso_valida(data_inicio)) or
            (data_fim and not _data_iso_valida(data_fim))):
        return 'Período inválido. Use datas no formato AAAA-MM-DD.', 400
    if data_inicio and data_fim and data_inicio > data_fim:
        return 'Período inválido: a data inicial deve anteceder a data final.', 400
    limite_tabela_historico = 500
    limite_datas_grafico = 1000
    filtros = []
    params = []
    if servidores_selecionados:
        placeholders = ','.join(['?'] * len(servidores_selecionados))
        filtros.append(f'servidor IN ({placeholders})')
        params.extend(servidores_selecionados)
    if data_inicio:
        filtros.append('data >= ?')
        params.append(data_inicio)
    if data_fim:
        filtros.append('data <= ?')
        params.append(data_fim)
    # Preserva a visão inicial existente: sem qualquer filtro, mostra o último dia.
    if not filtros:
        filtros.append('data = (SELECT MAX(data) FROM historico_servidores)')
    where_sql = ' WHERE ' + ' AND '.join(filtros)

    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM historico_servidores' + where_sql, params)
    total_registros_historico = cursor.fetchone()[0]
    cursor.execute(
        'SELECT data, servidor, total_bytes, qtd_bancos FROM historico_servidores' +
        where_sql + ' ORDER BY data DESC, servidor ASC LIMIT ?',
        [*params, limite_tabela_historico]
    )
    dados_historico_tabela = [{
            'data': r[0],
            'servidor': r[1],
            'total_bytes': r[2],
            'tamanho_str': formatar_tamanho(r[2]),
            'qtd_bancos': r[3]
        } for r in cursor.fetchall()]

    # O resumo usa extremos agregados no SQLite; não materializa o histórico inteiro.
    cursor.execute(f'''
        WITH filtrados AS (
            SELECT data, servidor, total_bytes FROM historico_servidores{where_sql}
        ), ordenados AS (
            SELECT data, servidor, total_bytes,
                   ROW_NUMBER() OVER (PARTITION BY servidor ORDER BY data ASC) AS rn_asc,
                   ROW_NUMBER() OVER (PARTITION BY servidor ORDER BY data DESC) AS rn_desc
            FROM filtrados
        )
        SELECT servidor,
               MAX(CASE WHEN rn_asc = 1 THEN data END),
               MAX(CASE WHEN rn_desc = 1 THEN data END),
               MAX(CASE WHEN rn_asc = 1 THEN total_bytes END),
               MAX(CASE WHEN rn_desc = 1 THEN total_bytes END)
        FROM ordenados GROUP BY servidor ORDER BY servidor
    ''', params)
    linhas_resumo = cursor.fetchall()

    cursor.execute(
        'SELECT COUNT(DISTINCT data) FROM historico_servidores' + where_sql, params
    )
    total_datas_grafico = cursor.fetchone()[0]
    grafico_amostrado = total_datas_grafico > limite_datas_grafico
    numeracao_amostra = (
        'SELECT data, NTILE(?) OVER (ORDER BY data) AS grupo FROM datas'
        if grafico_amostrado else 'SELECT data FROM datas'
    )
    selecao_amostra = (
        'SELECT CASE WHEN grupo = ? THEN MAX(data) ELSE MIN(data) END AS data '
        'FROM numeradas GROUP BY grupo'
        if grafico_amostrado else 'SELECT data FROM datas'
    )
    params_grafico = [*params]
    if grafico_amostrado:
        params_grafico.extend([limite_datas_grafico, limite_datas_grafico])
    params_grafico.extend(params)
    cursor.execute(f'''
        WITH datas AS (
            SELECT DISTINCT data FROM historico_servidores{where_sql}
        ), numeradas AS (
            {numeracao_amostra}
        ), amostra AS ({selecao_amostra})
        SELECT h.data, h.servidor, h.total_bytes
        FROM (
            SELECT data, servidor, total_bytes FROM historico_servidores{where_sql}
        ) h
        JOIN amostra a ON a.data = h.data
        ORDER BY h.data ASC, h.servidor ASC
    ''', params_grafico)
    rows_grafico = cursor.fetchall()
    conn.close()

    resumo_crescimento = []
    dados_grafico = {}
    for srv, data_primeira, data_ultima, bytes_primeiro, bytes_ultimo in linhas_resumo:
        diff_bytes = bytes_ultimo - bytes_primeiro
        resumo_crescimento.append({
            'servidor': srv,
            'data_inicial': data_primeira,
            'data_final': data_ultima,
            'crescimento_str': formatar_tamanho(abs(diff_bytes)),
            'is_positivo': diff_bytes >= 0,
            'runway': calcular_runway_disco(srv, 0)
        })

    if rows_grafico:
        por_servidor = {}
        for data, srv, total_bytes in rows_grafico:
            por_servidor.setdefault(srv, {})[data] = round(total_bytes / (1024 ** 3), 2)
        datas_unicas = sorted({r[0] for r in rows_grafico})
        datasets_grafico = []
        
        cores = {
            'DB01': '#2ecc71', 'DB02': '#3498db', 'DB03': '#9b59b6',
            'DB04': '#f1c40f', 'DB05': '#e67e22', 'DB06': '#e74c3c'
        }

        for srv in sorted(por_servidor.keys()):
            datasets_grafico.append({
                'label': srv,
                'data': [por_servidor[srv].get(data) for data in datas_unicas],
                'borderColor': cores.get(srv, '#34495e'),
                'backgroundColor': cores.get(srv, '#34495e'),
                'fill': False,
                'pointRadius': 0,
                'tension': 0
            })

        dados_grafico = {
            'labels': datas_unicas,
            'datasets': datasets_grafico
        }
    return render_template_string(
        HTML_LAYOUT, servidores=SERVIDORES, servidor_atual='HISTORICO', bancos=[],
        total_tamanho="-", total_atencao=0, total_critico=0,
        ordem_atual='', busca_termo='', filtro_status='',
        modo_todos=False, modo_inativos=False, modo_orfaos=False, modo_historico=True,
        metricas_servidores=[], top5_global=[], ultimos_hospedados_global=[], arquivos_orfaos=[], dados_historico=dados_historico_tabela,
        servidores_selecionados=servidores_selecionados, data_inicio=data_inicio, data_fim=data_fim,
        resumo_crescimento=resumo_crescimento, dados_grafico=dados_grafico,
        total_registros_historico=total_registros_historico,
        limite_tabela_historico=limite_tabela_historico,
        historico_tabela_limitado=total_registros_historico > limite_tabela_historico,
        grafico_amostrado=grafico_amostrado, total_datas_grafico=total_datas_grafico,
        limite_datas_grafico=limite_datas_grafico,
        datas_exibidas_grafico=len(dados_grafico.get('labels', []))
    )

@bancos_bp.route('/api/historico')
def api_historico():
    servidores = list(dict.fromkeys(
        request.args.getlist('servidor') + request.args.getlist('servidores')
    ))
    if len(servidores) > 50 or any(servidor not in SERVIDORES for servidor in servidores):
        return jsonify(erro='Filtro de servidor inválido.'), 400
    data_inicio = request.args.get('data_inicio', '').strip()
    data_fim = request.args.get('data_fim', '').strip()
    if ((data_inicio and not _data_iso_valida(data_inicio)) or
            (data_fim and not _data_iso_valida(data_fim)) or
            (data_inicio and data_fim and data_inicio > data_fim)):
        return jsonify(erro='Período inválido. Use AAAA-MM-DD e data inicial anterior à final.'), 400
    try:
        limite = int(request.args.get('limite', '500'))
        deslocamento = int(request.args.get('offset', '0'))
    except ValueError:
        return jsonify(erro='limite e offset devem ser números inteiros.'), 400
    max_int64 = (1 << 63) - 1
    max_offset = 10_000_000
    if (limite < 1 or limite > 1000 or deslocamento < 0 or
            limite > max_int64 or deslocamento > max_int64 or deslocamento > max_offset):
        return jsonify(
            erro=f'limite deve estar entre 1 e 1000 e offset entre 0 e {max_offset}.'
        ), 400

    filtros = []
    params = []
    if servidores:
        filtros.append('servidor IN (' + ','.join('?' for _ in servidores) + ')')
        params.extend(servidores)
    if data_inicio:
        filtros.append('data >= ?')
        params.append(data_inicio)
    if data_fim:
        filtros.append('data <= ?')
        params.append(data_fim)
    where_sql = (' WHERE ' + ' AND '.join(filtros)) if filtros else ''
    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM historico_servidores' + where_sql, params)
    total = cursor.fetchone()[0]
    cursor.execute(
        'SELECT data, servidor, total_bytes, qtd_bancos FROM historico_servidores' +
        where_sql + ' ORDER BY data DESC, servidor ASC LIMIT ? OFFSET ?',
        [*params, limite, deslocamento]
    )
    rows = cursor.fetchall()
    conn.close()
    
    resultado = [{'data': r[0], 'servidor': r[1], 'tamanho_bytes': r[2], 'qtd_bancos': r[3]} for r in rows]
    resposta = jsonify(resultado)
    resposta.headers['X-Total-Count'] = str(total)
    resposta.headers['X-Limit'] = str(limite)
    resposta.headers['X-Offset'] = str(deslocamento)
    links = []
    args_base = request.args.to_dict(flat=False)
    args_base['limite'] = [str(limite)]
    if deslocamento > 0:
        args_anterior = dict(args_base)
        args_anterior['offset'] = [str(max(0, deslocamento - limite))]
        links.append(f'<{request.path}?{urlencode(args_anterior, doseq=True)}>; rel="prev"')
    if deslocamento + len(resultado) < total:
        args_proximo = dict(args_base)
        args_proximo['offset'] = [str(deslocamento + limite)]
        links.append(f'<{request.path}?{urlencode(args_proximo, doseq=True)}>; rel="next"')
    if links:
        resposta.headers['Link'] = ', '.join(links)
    return resposta


@bancos_bp.route('/cnpj')
@bancos_bp.route('/consulta-cnpj')
def consulta_cnpj():
    """Página interativa para consulta detalhada de CNPJs na Receita Federal."""
    return render_template('cnpj.html')


@bancos_bp.route('/api/cnpj/<cnpj>')
def api_consulta_cnpj(cnpj):
    """Proxy local para consulta de CNPJ (evita problemas de CORS ou falta de internet direta nas estações)."""
    clean = re.sub(r'\D', '', cnpj)
    if len(clean) != 14:
        return jsonify(erro='CNPJ deve ter 14 dígitos numéricos.'), 400
    try:
        req = urllib.request.Request(
            f'https://publica.cnpj.ws/cnpj/{clean}',
            headers={'User-Agent': 'InfoMonitor/1.0', 'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:  # nosec B310
            data = json.loads(resp.read().decode('utf-8'))
            return jsonify(data)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return jsonify(erro='CNPJ não encontrado na base da Receita Federal.'), 404
        if e.code == 429:
            return jsonify(erro='Limite de consultas excedido na API pública gratuita (3 consultas por minuto). Aguarde alguns instantes.'), 429
        return jsonify(erro=f'Erro {e.code} ao consultar a API da Receita Federal.'), e.code
    except urllib.error.URLError as e:
        return jsonify(erro=f'Falha ao conectar com o serviço da Receita Federal: {str(e.reason)}'), 502
    except Exception as e:
        return jsonify(erro=f'Erro interno ao processar consulta de CNPJ: {str(e)}'), 500


if __name__ == '__main__':
    raise SystemExit("Inicie pelo InfoMonitorDBClientes.py, não por bancos.py.")

