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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.request
import urllib.error
from backups import interpretar_status_backups
from gestao_bancos import listar_pastas, adicionar_banco
from seguranca import (
    senha_atende_politica, login_bloqueado, registrar_falha_login, limpar_falhas_login,
    token_expira_em, token_ainda_valido, destino_redirect_seguro,
    conectar_ssh, validar_caminho_banco,
)

# Configuração do Blueprint e Banco
bancos_bp = Blueprint('bancos', __name__)
ALLOW_PUBLIC_REGISTER = os.getenv('ALLOW_PUBLIC_REGISTER', '0') == '1'

# Nomes das 3 áreas de acesso que podem ser concedidas por usuário (o Master sempre tem as 3, sempre).
PERMISSOES_DISPONIVEIS = {
    'perm_servidores': 'Editar CNAME/Lojas na Lista de Servidores',
    'perm_gestao_bancos': 'Gestão de Bancos (Admin: enviar/criar/editar/excluir alias)',
    'perm_horarios': 'Painel de Horários',
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
    'bancos.api_historico', 'bancos.api_testar_cname',
    'bancos.admin_login', 'bancos.esqueci_senha', 'bancos.admin_register',
    'bancos.redefinir_senha_token',
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
    conn = sqlite3.connect(DB_SISTEMA)
    conn.row_factory = sqlite3.Row
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
DB_HISTORICO = 'historico_bancos.db'
DB_SISTEMA = 'sistema.db'  # FIX: Ajustado para alinhar ao arquivo real do ambiente
URL_STATUS_BACKUPS_FTP = os.getenv('URL_STATUS_BACKUPS_FTP', 'http://192.168.254.19/cgi-bin/bkp-status.web')
   

# --- BANCO DE DADOS LOCAL: USUÁRIOS, CNAME E HISTÓRICO ---
def init_db_sistema():
    """Inicializa as tabelas de suporte do sistema (Histórico, Usuários e CNAMEs customizados)."""
    conn = sqlite3.connect(DB_SISTEMA)
    conn.row_factory = sqlite3.Row  # FIX: sem isso, fetchone()['coluna'] quebra (retorna tupla, não dict) e o app nem inicia
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
    for coluna in ('perm_servidores', 'perm_horarios', 'perm_gestao_bancos'):
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
            cli_situacao TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_clientes_lojas_alias ON clientes_lojas (alias)')

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

def aplicar_lojas_clientes(lista_bancos, cache_lojas=None):
    """Anexa a lista de lojas (CodInfo/LojCód./Fantasia/Razão/CNPJ/Situação) a cada banco da lista."""
    if not lista_bancos:
        return lista_bancos
    lojas_por_alias = cache_lojas if cache_lojas is not None else carregar_lojas_clientes()

    for banco in lista_bancos:
        banco['lojas'] = lojas_por_alias.get(banco.get('alias'), [])
    return lista_bancos

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
    
    conn = sqlite3.connect(DB_SISTEMA)
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET ativo = ? WHERE id = ? AND eh_master = 0", (novo_status, user_id))
    conn.commit()
    conn.close()
    
    return jsonify({'sucesso': True, 'mensagem': 'Status do usuário atualizado com sucesso!'})
    

# --- SISTEMA DE CACHE EM MEMÓRIA ---
CACHE_DADOS = {
    'bancos_por_servidor': {},
    'orfaos': {'dados': [], 'timestamp': 0},
    'metricas': {'dados': [], 'timestamp': 0},
    'backups_ftp': {'dados': None, 'timestamp': 0},
    'ttl_segundos': 600
}

def limpar_cache_global():
    """Força a invalidação do cache em memória."""
    CACHE_DADOS['bancos_por_servidor'].clear()
    CACHE_DADOS['orfaos'] = {'dados': [], 'timestamp': 0}
    CACHE_DADOS['metricas'] = {'dados': [], 'timestamp': 0}
    CACHE_DADOS['backups_ftp'] = {'dados': None, 'timestamp': 0}

# --- RECURSO: STATUS DE BACKUPS FTP (lê e interpreta a página de texto do servidor de backups) ---
def obter_status_backups_ftp(forcar_atualizacao=False):
    """
    Busca http://.../cgi-bin/bkp-status.web (texto simples) e transforma em dados estruturados:
    cliente, status (OK / ATRASADO / SEM_ARQUIVO), horário do último backup e totais.
    Cacheado por alguns minutos, já que é uma chamada HTTP síncrona e a página muda pouco.
    """
    agora = time.time()
    cache = CACHE_DADOS['backups_ftp']
    if not forcar_atualizacao and cache['dados'] is not None and (agora - cache['timestamp'] < CACHE_DADOS['ttl_segundos']):
        return cache['dados']

    resultado = {
        "erro": None, "clientes": [], "total_ok": 0, "total_erro": 0,
        "total_atrasado": 0, "ultima_atualizacao": None
    }

    try:
        req = urllib.request.Request(URL_STATUS_BACKUPS_FTP, headers={'User-Agent': 'InfoMonitorDBClientes'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            conteudo = resp.read()
            try:
                texto = conteudo.decode('utf-8')
            except UnicodeDecodeError:
                texto = conteudo.decode('latin-1')
    except Exception as e:
        resultado["erro"] = f"Não foi possível consultar o status dos backups: {e}"
        CACHE_DADOS['backups_ftp'] = {'dados': resultado, 'timestamp': agora}
        return resultado

    resultado = interpretar_status_backups(texto)

    CACHE_DADOS['backups_ftp'] = {'dados': resultado, 'timestamp': agora}
    return resultado

# --- BANCO DE DADOS LOCAL PARA HISTÓRICO ---
def init_db_historico():
    conn = sqlite3.connect(DB_HISTORICO)
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
    conn.commit()
    conn.close()

init_db_historico()


def caminho_do_alias_no_conf(nome_servidor, alias):
    """Localiza o caminho do FDB no databases.conf do servidor para um alias já cadastrado."""
    alias_limpo = sanitizar_alias(alias)
    if not alias_limpo:
        return None
    conteudo = ler_databases_conf_remoto(nome_servidor) or ""
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
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return redirect(url_for('bancos.admin_painel', servidor=request.form.get('servidor'), erro="Senha de confirmação incorreta. Ação cancelada."))

    servidor = request.form.get('servidor')
    alias = sanitizar_alias(request.form.get('alias', ''))
    arquivo = request.files.get('arquivo_fdb')

    if not servidor or not alias or not arquivo or not arquivo.filename:
        return redirect(url_for('bancos.admin_painel', servidor=servidor, erro="Informe o Alias e selecione o arquivo .FDB!"))

    info_srv = SERVIDORES.get(servidor)
    if not info_srv:
        return redirect(url_for('bancos.admin_painel', servidor=servidor, erro="Servidor inválido."))

    try:
        caminho_destino = validar_caminho_banco(caminho_do_alias_no_conf(servidor, alias), DIRETORIO_BASE)
        informado = (request.form.get('caminho') or '').strip()
        if informado and validar_caminho_banco(informado, DIRETORIO_BASE) != caminho_destino:
            raise ValueError('O destino deve corresponder ao alias cadastrado.')
    except ValueError:
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
        # modo_busca_unificada=True retorna TODAS as entradas do databases.conf, ativas e inativas
        todos_ativos_e_inativos, total_conf, _ = obter_dados_servidor_linux(
            nome_servidor, info_servidor, modo_busca_unificada=True, forcar_atualizacao=forcar_atualizacao
        )
        # varrer_orfaos=True retorna arquivos no disco que nem constam no databases.conf
        _, _, arquivos_orfaos = obter_dados_servidor_linux(
            nome_servidor, info_servidor, varrer_orfaos=True, forcar_atualizacao=forcar_atualizacao
        )
        total_orfaos = sum(a.get('tamanho_bytes', 0) for a in arquivos_orfaos)
        return total_conf + total_orfaos
    except Exception:
        return None

def salvar_historico_diario(metricas_servidores=None):
    """Grava o snapshot diário no SQLite, contando o espaço real do disco (ativos + inativos + órfãos)."""
    if not metricas_servidores:
        metricas_servidores = obter_metricas_servidores(salvar_db=False, forcar_atualizacao=True)
        
    hoje = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()
    for m in metricas_servidores:
        info_servidor = SERVIDORES.get(m['servidor'])
        total_bytes_real = None
        if info_servidor:
            total_bytes_real = calcular_espaco_total_incluindo_sombra(m['servidor'], info_servidor, forcar_atualizacao=True)
        # Se não foi possível calcular o total real (ex: falha de SSH), usa o valor de ativos como último recurso
        total_bytes = total_bytes_real if total_bytes_real is not None else m['tamanho_gb'] * (1024 ** 3)
        cursor.execute('''
            INSERT OR REPLACE INTO historico_servidores (data, servidor, total_bytes, qtd_bancos)
            VALUES (?, ?, ?, ?)
        ''', (hoje, m['servidor'], total_bytes, m['qtd_ativos']))
    conn.commit()
    conn.close()

# --- AGENDADOR FIXO PARA AS 04:00 DA MANHÃ ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(salvar_historico_diario, 'cron', hour=4, minute=0)
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
        return {'status': 'critico', 'dias': dias_sem_uso, 'texto_dias': f"({meses} meses sem uso)"}
    elif dias_sem_uso >= 30:
        return {'status': 'atencao', 'dias': dias_sem_uso, 'texto_dias': f"({dias_sem_uso} dias sem uso)"}
    else:
        return {'status': 'normal', 'dias': dias_sem_uso, 'texto_dias': 'Ativo'}

def _banco_bate_busca(banco, termo, lojas_por_alias):
    """Verifica se o termo de busca bate no alias OU em qualquer loja cadastrada (CodInfo/LojCód./Fantasia/Razão/CNPJ/Situação)."""
    termo_lower = termo.lower()
    if termo_lower in banco['alias'].lower():
        return True
    lojas = lojas_por_alias.get(banco['alias'], [])
    for loja in lojas:
        valores = ' '.join(str(v) for v in loja.values() if v)
        if termo_lower in valores.lower():
            return True
    return False

def obter_dados_servidor_linux(nome_servidor, info_servidor, busca=None, buscar_inativos=False, modo_busca_unificada=False, varrer_orfaos=False, forcar_atualizacao=False):
    agora = time.time()
    chave_cache = nome_servidor
    lojas_por_alias = carregar_lojas_clientes() if busca else {}
    
    if not forcar_atualizacao and chave_cache in CACHE_DADOS['bancos_por_servidor']:
        item_cache = CACHE_DADOS['bancos_por_servidor'][chave_cache]
        if agora - item_cache['timestamp'] < CACHE_DADOS['ttl_segundos']:
            bancos_brutos = item_cache['dados']
            total_b = item_cache['bytes']
            orfaos = item_cache['orfaos']
            
            lista_filtrada = []
            for b in bancos_brutos:
                eh_inativo = b.get('eh_inativo', False)
                if not modo_busca_unificada and not varrer_orfaos:
                    if buscar_inativos and not eh_inativo:
                        continue
                    if not buscar_inativos and eh_inativo:
                        continue
                if busca and not _banco_bate_busca(b, busca, lojas_por_alias):
                    continue
                lista_filtrada.append(b)
            return lista_filtrada, total_b, orfaos if varrer_orfaos else []

    lista_bancos = []
    arquivos_orfaos = []
    total_bytes = 0
    
    ssh = None
    try:
        ssh = conectar_ssh(info_servidor, timeout=5)
        sftp = ssh.open_sftp()

        try:
            with sftp.open(CAMINHO_DATABASES_CONF, 'rb') as f:
                conteudo_bytes = f.read()
        except Exception as e:
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
                'erro': str(e)
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
            except Exception:
                pass

        try:
            cmd_find = f"find {DIRETORIO_BASE} -type f \\( -name '*.fdb' -o -name '*.fbk' -o -name '*.bak' \\)"
            stdin, stdout, stderr = ssh.exec_command(cmd_find, timeout=5)
            arquivos_no_disco = stdout.read().decode('utf-8').splitlines()

            for arq in arquivos_no_disco:
                arq_limpo = arq.strip()
                if arq_limpo and arq_limpo not in caminhos_oficiais:
                    try:
                        st = sftp.stat(arq_limpo)
                        arquivos_orfaos.append({
                            'servidor': nome_servidor,
                            'caminho': arq_limpo,
                            'caminho_exibicao': mascara_caminho(arq_limpo),
                            'tamanho_bytes': st.st_size,
                            'tamanho_str': formatar_tamanho(st.st_size),
                            'timestamp': st.st_mtime,
                            'data_str': datetime.fromtimestamp(st.st_mtime).strftime('%d/%m/%Y %H:%M:%S')
                        })
                    except Exception:
                        pass
        except Exception:
            pass

        sftp.close()
        ssh.close()

        CACHE_DADOS['bancos_por_servidor'][nome_servidor] = {
            'dados': lista_bancos,
            'bytes': total_bytes,
            'orfaos': arquivos_orfaos,
            'timestamp': agora
        }

    except Exception as e:
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
            'erro': f"Erro SSH: {str(e)}"
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

    with ThreadPoolExecutor(max_workers=len(SERVIDORES)) as executor:
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
    agora = time.time()
    if not forcar_atualizacao and CACHE_DADOS['metricas']['dados'] and (agora - CACHE_DADOS['metricas']['timestamp'] < CACHE_DADOS['ttl_segundos']):
        return CACHE_DADOS['metricas']['dados']

    metricas = []
    with ThreadPoolExecutor(max_workers=len(SERVIDORES)) as executor:
        futures = {
            executor.submit(obter_dados_servidor_linux, nome, info, buscar_inativos=False, forcar_atualizacao=forcar_atualizacao): nome
            for nome, info in SERVIDORES.items()
        }
        for future in futures:
            nome_srv = futures[future]
            try:
                bancos, total_bytes, _ = future.result()
                if bancos and 'erro' in bancos[0]:
                    qtd_ativos = 0
                    qtd_3meses = 0
                    tamanho_gb = 0
                    media_mb = 0
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

                pct_disco = min(round((tamanho_gb / 100) * 100, 1), 100)
                runway = calcular_runway_disco(nome_srv, tamanho_gb)

            except Exception:
                tamanho_gb = 0
                pct_disco = 0
                qtd_ativos = 0
                qtd_3meses = 0
                media_mb = 0
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
                'runway': runway
            })
    
    metricas.sort(key=lambda x: x['servidor'])
    
    CACHE_DADOS['metricas'] = {
        'dados': metricas,
        'timestamp': agora
    }

    if salvar_db:
        salvar_historico_diario(metricas)
    return metricas

def converter_para_data_iso(data_str):
    """Auxiliar para converter DD/MM/AAAA em YYYYMMDD para ordenação correta."""
    try:
        return datetime.strptime(data_str, "%d/%m/%Y").strftime("%Y%m%d")
    except Exception:
        return "00000000"

# --- OPERAÇÕES REMOTAS PARA GESTÃO DO DATABASES.CONF ---
def ler_databases_conf_remoto(nome_servidor):
    info = SERVIDORES.get(nome_servidor)
    if not info: return ""
    
    ssh = None
    try:
        ssh = conectar_ssh(info, timeout=5)
        sftp = ssh.open_sftp()
        with sftp.open(CAMINHO_DATABASES_CONF, 'rb') as f:
            conteudo = f.read()
        sftp.close()
        ssh.close()
        try:
            return conteudo.decode('utf-8')
        except UnicodeDecodeError:
            return conteudo.decode('latin-1', errors='ignore')
    except Exception as e:
        return f"# Erro ao ler databases.conf em {nome_servidor}: {str(e)}"

def salvar_databases_conf_remoto(nome_servidor, novo_conteudo):
    info = SERVIDORES.get(nome_servidor)
    if not info: return False
    
    ssh = None
    try:
        ssh = conectar_ssh(info, timeout=5)
        sftp = ssh.open_sftp()
        conteudo_unix = novo_conteudo.replace('\r\n', '\n')
        with sftp.open(CAMINHO_DATABASES_CONF, 'wb') as f:
            f.write(conteudo_unix.encode('utf-8'))
        sftp.close()
        ssh.close()
        limpar_cache_global()
        return True
    except Exception as e:
        print(f"Erro ao salvar databases.conf em {nome_servidor}: {e}")
        return False

def verificar_alias_existente_global(alias_busca):
    """Varre todos os servidores para verificar se o alias já existe ativo."""
    alias_limpo = alias_busca.strip().lower()
    for srv, info in SERVIDORES.items():
        conf = ler_databases_conf_remoto(srv)
        for linha in conf.splitlines():
            l = linha.strip()
            if not l.startswith('#') and '=' in l:
                nome_alias = l.split('=', 1)[0].strip().lower()
                if nome_alias == alias_limpo:
                    return srv
    return None
    
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
    <!-- Font Awesome Icons & Chart.js -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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

        /* Separador visual entre grupos de botões do menu (unifica visualmente sem precisar de fundo colorido) */
        .separador-menu { width: 1px; align-self: stretch; background: var(--cor-borda); margin: 2px 4px; }

        /* Coluna de CNAME compacta: só o botão de copiar aparece, o caminho vira um balão (tooltip) */
        .cname-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .cname-texto { display: none; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>

    <div class="topo-fixo" id="painelTopo">
        <div class="linha-cabecalho">
            <h2>Servidores dos Bancos Hospedados</h2>

            <form action="" method="GET" class="search-box">
                <input type="text" name="busca" class="search-input" placeholder="Pesquisar cliente (ativos e inativos)..." value="{{ busca_termo }}">
                <button type="submit" class="btn-search">🔍 Buscar</button>
                {% if busca_termo or filtro_status %}
                    <a href="{% if modo_inativos %}/inativos{% elif modo_todos %}/todos{% elif modo_orfaos %}/orfaos{% elif modo_historico %}/historico{% else %}/servidor/{{ servidor_atual }}{% endif %}" class="btn-clear">✖ Limpar</a>
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
                    <a href="/todos?filtro=sem_lojas">Clientes sem lojas cadastradas</a>
                </div>
            </details>
            {% if tem_permissao('perm_gestao_bancos') or tem_permissao('perm_servidores') %}
            <details class="menu-acoes">
                <summary class="btn">Gerenciar</summary>
                <div class="menu-painel">
                    {% if tem_permissao('perm_gestao_bancos') %}<a href="/admin?servidor={{ servidor_atual|urlencode }}">Gestão de bancos</a>{% endif %}
                    {% if tem_permissao('perm_servidores') %}<a href="/admin/importar-lojas">Importar lojas</a>{% endif %}
                </div>
            </details>
            {% endif %}
            <a href="?atualizar=1{% if busca_termo %}&busca={{ busca_termo|urlencode }}{% endif %}{% if ordem_atual %}&ordem={{ ordem_atual|urlencode }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status|urlencode }}{% endif %}" class="btn" title="Atualizar dados do servidor">↻ Atualizar</a>
            <a href="/horarios" class="btn">🗓️ Horários</a>
            {% if session.get('logged_in') %}
            <details class="menu-acoes menu-conta">
                <summary class="btn"><span class="nome-conta">{{ session.get('user_nome', '') }}</span>{% if session.get('eh_master') %}<span class="selo-master">Master</span>{% endif %}</summary>
                <div class="menu-painel">
                    {% if tem_permissao('perm_gestao_bancos') %}<a href="/admin">Meu perfil</a>{% endif %}
                    <hr>
                    <form action="/admin/logout" method="POST">
                        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        <button type="submit">Sair da conta</button>
                    </form>
                </div>
            </details>
            {% else %}
            <a href="/admin/login" class="btn">Entrar</a>
            {% endif %}
        </nav>

        <div class="header-info">
            {% if modo_historico %}
                <h3>📈 Histórico Diário de Consumo de Disco (Gravado Diariamente às 04:00 AM)</h3>
            {% elif modo_orfaos %}
                <h3>⚠️ Arquivos no diretório <span style="color: #e74c3c;">../infobrasil/*</span> Não Listados no databases.conf</h3>
            {% elif busca_termo %}
                <h3>🔍 Busca por: <span style="color: #27ae60;">"{{ busca_termo }}"</span> em todos os servidores</h3>
            {% elif modo_inativos %}
                <h3>🚫 Alias Inativados com <span style="color: #e67e22;">#</span> no databases.conf</h3>
            {% elif modo_todos %}
                <h3>Databases em: <span style="color: #3498db;">TODOS OS SERVIDORES</span></h3>
            {% else %}
                <h3>Databases em: <span style="color: #27ae60;">{{ servidor_atual }}</span></h3>
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
                    <a href="/backups-ftp" class="alert-btn {% if status_backups.total_erro > 0 %}alert-btn-critico{% elif status_backups.total_atrasado > 0 %}alert-btn-atencao{% else %}alert-btn-atencao{% endif %}" style="{% if status_backups.total_erro == 0 and status_backups.total_atrasado == 0 %}background-color:#f0fdf4;border-color:#bbf7d0;color:#166534;{% endif %}" title="Clique para ver o detalhamento por cliente">
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

    <div class="container">

        <!-- TOP 5 MAIORES BANCOS GLOBAL -->
        {% if top5_global and not modo_orfaos and not modo_historico %}
        <div class="top5-container">
            <div class="top5-title">🏆 Top 5 Bancos:</div>
            <div class="top5-items">
                {% for b in top5_global %}
                <div class="top5-item clicavel-cname" title="{{ b.cname_string }}" data-cname="{{ b.cname_string }}" onclick="copiarCnameCliente(this)">
                    <span class="top5-rank">{{ loop.index }}</span>
                    <strong>{{ b.alias }}</strong>
                    <span style="color: #7f8c8d;">({{ b.servidor }})</span>
                    <span style="color: #27ae60; font-weight: bold;">{{ b.tamanho_str }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        <!-- ÚLTIMOS 3 CLIENTES HOSPEDADOS -->
        {% if ultimos_hospedados_global and not modo_orfaos and not modo_historico %}
        <div class="top5-container" style="border-left: 4px solid #8e44ad;">
            <div class="top5-title">🆕 Últimos Clientes Hospedados:</div>
            <div class="top5-items">
                {% for b in ultimos_hospedados_global %}
                <div class="top5-item clicavel-cname" title="{{ b.cname_string }}" data-cname="{{ b.cname_string }}" onclick="copiarCnameCliente(this)">
                    <span class="top5-rank" style="background:#8e44ad;">{{ loop.index }}</span>
                    <strong>{{ b.alias }}</strong>
                    <span style="color: #7f8c8d;">({{ b.servidor }})</span>
                    <span style="color: #8e44ad; font-weight: bold;">{{ b.data_criacao }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        <!-- TABELA DE RESULTADOS -->
        <div class="card">
            {% if modo_historico %}
                <!-- FORMULÁRIO DE FILTROS -->
                <div style="margin-bottom: 20px; background: #f8f9fa; padding: 15px; border-radius: 8px; border: 1px solid #e0e0e0;">
                    <form method="GET" action="/historico" style="display: flex; flex-wrap: wrap; gap: 15px; align-items: flex-end;">
                        <div>
                            <label style="font-weight: bold; font-size: 0.85em; display: block; margin-bottom: 5px;">Servidores:</label>
                            <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                                {% for nome in servidores.keys() %}
                                    <label style="font-size: 0.9em; cursor: pointer;">
                                        <input type="checkbox" name="servidores" value="{{ nome }}" {% if nome in servidores_selecionados %}checked{% endif %}>
                                        {{ nome }}
                                    </label>
                                {% endfor %}
                            </div>
                        </div>

                        <div>
                            <label style="font-weight: bold; font-size: 0.85em; display: block; margin-bottom: 5px;">Período Inicial:</label>
                            <input type="date" name="data_inicio" value="{{ data_inicio }}" style="padding: 6px; border-radius: 4px; border: 1px solid #ccc;">
                        </div>

                        <div>
                            <label style="font-weight: bold; font-size: 0.85em; display: block; margin-bottom: 5px;">Período Final:</label>
                            <input type="date" name="data_fim" value="{{ data_fim }}" style="padding: 6px; border-radius: 4px; border: 1px solid #ccc;">
                        </div>

                        <div style="display: flex; gap: 8px;">
                            <button type="submit" class="btn-search">Filtrar</button>
                            <a href="/historico" class="btn-clear">Limpar Filtros</a>
                        </div>
                    </form>
                </div>

                <!-- CARDS DE CRESCIMENTO -->
                {% if resumo_crescimento %}
                    <p class="text-muted small mb-2">📦 Os valores abaixo incluem bancos ativos, bancos inativados (ainda ocupando disco) e arquivos órfãos — ou seja, o espaço real ocupado no servidor, não só os alias ativos.</p>
                    <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px;">
                        {% for item in resumo_crescimento %}
                            <div style="background: white; border-left: 4px solid {% if item.is_positivo %}#e74c3c{% else %}#27ae60{% endif %}; padding: 10px 15px; border-radius: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); min-width: 200px;">
                                <div style="font-size: 0.8em; color: #7f8c8d;">Crescimento <strong>{{ item.servidor }}</strong></div>
                                <div style="font-size: 1.1em; font-weight: bold; color: {% if item.is_positivo %}#c0392b{% else %}#27ae60{% endif %}; margin-top: 2px;">
                                    {% if item.is_positivo %}+{% else %}-{% endif %}{{ item.crescimento_str }}
                                </div>
                                <div style="font-size: 0.72em; color: #95a5a6; margin-top: 3px;">{{ item.data_inicial }} até {{ item.data_final }}</div>
                                {% if item.runway %}
                                <div style="font-size: 0.78em; margin-top: 6px; padding-top: 6px; border-top: 1px dashed #eee;">
                                    <span style="color: #7f8c8d;">Projeção (90% disco):</span>
                                    <strong style="color: {% if 'CRÍTICO' in item.runway.dias_restantes %}#c0392b{% else %}#e67e22{% endif %};">{{ item.runway.dias_restantes }}</strong>
                                    {% if item.runway.data_estimada %}
                                        <div style="color: #95a5a6;">≈ {{ item.runway.data_estimada }}</div>
                                    {% endif %}
                                </div>
                                {% endif %}
                            </div>
                        {% endfor %}
                    </div>
                {% endif %}

                <!-- GRÁFICO HISTÓRICO DE CRESCIMENTO -->
                {% if dados_grafico and dados_grafico.labels %}
                    <div style="background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); margin-bottom: 20px; border: 1px solid #e2e8f0;">
                        <h4 style="margin-top: 0; color: #2c3e50;">📈 EVOLUÇÃO DE CONSUMO DE DISCO (GB)</h4>
                        <div style="height: 250px; width: 100%;">
                            <canvas id="chart-historico-linha"></canvas>
                        </div>
                    </div>
                {% endif %}

                <h3>📊 Registros de Histórico Diário (`historico_bancos.db`)</h3>
                {% if dados_historico|length == 0 %}
                    <div style="text-align: center; padding: 30px; color: #7f8c8d;">
                        Nenhum registro encontrado para os filtros selecionados.
                    </div>
                {% else %}
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
                {% endif %}

            {% elif modo_orfaos %}
                {% if arquivos_orfaos|length == 0 %}
                    <div style="text-align: center; padding: 20px; color: #27ae60; font-weight: bold;">
                        ✅ Nenhum arquivo órfão/cópia manual encontrado nos diretórios!
                    </div>
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
                                <td style="font-family: monospace;">{{ arq.caminho_exibicao }}</td>
                                <td class="badge-tamanho">{{ arq.tamanho_str }}</td>
                                <td>{{ arq.data_str }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% endif %}

            {% else %}
                {% if bancos and 'erro' in bancos[0] %}
                    <div style="color: #c0392b; padding: 15px;"><strong>Erro:</strong> {{ bancos[0].erro }}</div>
                {% elif bancos|length == 0 %}
                    <div style="text-align: center; padding: 30px; color: #7f8c8d;">Nenhum registro encontrado.</div>
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
                                    <a href="?ordem=tamanho{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Tamanho do banco ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=data{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">Última Modificação ⇳</a>
                                </th>
                                <th>
                                    <a href="?ordem=cname{% if busca_termo %}&busca={{ busca_termo }}{% endif %}{% if filtro_status %}&filtro={{ filtro_status }}{% endif %}">String Conexão (CNAME) ⇳</a>
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
                                    <strong class="alias-clicavel" onclick="toggleDetalhes('{{ banco.alias }}')" title="Clique para ver os detalhes do cliente">
                                        {{ banco.alias }} <span id="seta-{{ banco.alias }}">▸</span>
                                    </strong>
                                    {% if not banco.lojas and not banco.eh_inativo %}
                                        <span class="badge bg-warning text-dark" style="font-size:0.7em;" title="Nenhuma loja cadastrada para este alias">📭 sem loja</span>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if banco.origem_data == 'pasta' %}
                                        <i class="fa-solid fa-folder icon-origem icon-folder" title="Data obtida da Pasta pai no Disco"></i>
                                    {% elif banco.origem_data == 'conf' %}
                                        <i class="fa-solid fa-file-code icon-origem icon-config" title="Data obtida da Tag no databases.conf"></i>
                                    {% else %}
                                        <i class="fa-solid fa-file-lines icon-origem" style="color:#bdc3c7;" title="Origem não identificada"></i>
                                    {% endif %}
                                    <span style="font-weight: 500; color: #34495e;">{{ banco.data_criacao }}</span>
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
                                        <div class="cname-cell" data-assinatura="{{ assinatura_cname(banco.cname_string) }}" data-alias="{{ banco.alias }}">
                                            <span class="cname-status" title="Testando conectividade...">⏳</span>
                                            <span class="cname-texto">{{ banco.cname_string }}</span>
                                            <button class="btn-copy" onclick="copiarString('{{ banco.cname_string }}', this)" title="{{ banco.cname_string }}{% if banco.cname_custom %} (editado manualmente){% endif %} — clique para copiar">
                                                📋 Copiar
                                            </button>
                                            {% if tem_permissao('perm_servidores') %}
                                                <button class="btn-copy" style="color:#b45309;" onclick="editarCname(this)" title="Editar string de conexão">
                                                    ✏️
                                                </button>
                                            {% endif %}
                                        </div>
                                    {% else %}
                                        -
                                    {% endif %}
                                </td>
                            </tr>
                            <tr id="detalhes-{{ banco.alias }}" class="linha-detalhes" style="display:none;">
                                <td colspan="{{ 7 if (busca_termo or modo_todos or modo_inativos) else 6 }}">
                                    <div class="painel-lojas" data-alias="{{ banco.alias }}">
                                        {% if banco.lojas %}
                                        <table class="tabela-lojas">
                                            <thead>
                                                <tr>
                                                    <th>CodInfo</th><th>LojCód.</th><th>Fantasia</th><th>Razão/Nome</th><th>CNPJ</th><th>Situação</th>
                                                    {% if tem_permissao('perm_servidores') %}<th></th>{% endif %}
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {% for loja in banco.lojas %}
                                                <tr class="linha-loja" data-id="{{ loja.id }}">
                                                    <td class="valor-loja" data-campo="cod_info">{{ loja.cod_info or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_codigo">{{ loja.loj_codigo or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_fantasia">{{ loja.loj_fantasia or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_nome">{{ loja.loj_nome or '-' }}</td>
                                                    <td class="valor-loja" data-campo="loj_cnpj">{{ loja.loj_cnpj or '-' }}</td>
                                                    <td class="valor-loja" data-campo="cli_situacao">{{ loja.cli_situacao or '-' }}</td>
                                                    {% if tem_permissao('perm_servidores') %}
                                                    <td class="acoes-loja">
                                                        <button class="btn-copy" style="background:#fef5e7;color:#b9770e;" onclick="editarLoja(this)">✏️</button>
                                                        <button class="btn-copy" style="background:#fdedec;color:#c0392b;" onclick="excluirLoja(this)">🗑️</button>
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
                                            <button class="btn-copy" style="background:#e8f8f0;color:#1e8449;margin-top:8px;" onclick="adicionarLoja(this, '{{ banco.alias }}')">
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
        function toggleDetalhes(alias) {
            const linha = document.getElementById('detalhes-' + alias);
            const seta = document.getElementById('seta-' + alias);
            if (!linha) return;
            const abrindo = linha.style.display === 'none';
            linha.style.display = abrindo ? '' : 'none';
            if (seta) seta.textContent = abrindo ? '▾' : '▸';
        }

        // --- Edição inline de uma loja já existente ---
        const CAMPOS_LOJA_ORDEM = ['cod_info', 'loj_codigo', 'loj_fantasia', 'loj_nome', 'loj_cnpj', 'cli_situacao'];

        function editarLoja(botao) {
            const linha = botao.closest('.linha-loja');
            const alias = linha.closest('.painel-lojas').dataset.alias;
            const id = linha.dataset.id;
            const celulas = linha.querySelectorAll('.valor-loja');
            const inputs = {};

            celulas.forEach(celula => {
                const campo = celula.dataset.campo;
                const valorAtual = celula.textContent.trim() === '-' ? '' : celula.textContent.trim();
                const input = document.createElement('input');
                input.type = 'text';
                input.value = valorAtual;
                input.className = 'form-control form-control-sm';
                inputs[campo] = input;
                celula.innerHTML = '';
                celula.appendChild(input);
            });

            const celulaAcoes = linha.querySelector('.acoes-loja');
            celulaAcoes.innerHTML = '';
            const btnSalvar = document.createElement('button');
            btnSalvar.className = 'btn-copy';
            btnSalvar.style.background = '#e8f8f0';
            btnSalvar.style.color = '#1e8449';
            btnSalvar.textContent = '💾';
            btnSalvar.title = 'Salvar';
            btnSalvar.onclick = function() {
                const payload = { id: id, alias: alias };
                CAMPOS_LOJA_ORDEM.forEach(campo => { payload[campo] = inputs[campo].value.trim(); });
                salvarLoja(payload);
            };
            celulaAcoes.appendChild(btnSalvar);
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
                wrapperHtml.innerHTML = '<thead><tr><th>CodInfo</th><th>LojCód.</th><th>Fantasia</th><th>Razão/Nome</th><th>CNPJ</th><th>Situação</th><th></th></tr></thead><tbody></tbody>';
                painel.insertBefore(wrapperHtml, botao);
                tabela = wrapperHtml.querySelector('tbody');
            }

            const novaLinha = document.createElement('tr');
            novaLinha.className = 'linha-loja';
            novaLinha.dataset.id = '';

            const inputs = {};
            CAMPOS_LOJA_ORDEM.forEach(campo => {
                const td = document.createElement('td');
                const input = document.createElement('input');
                input.type = 'text';
                input.className = 'form-control form-control-sm';
                inputs[campo] = input;
                td.appendChild(input);
                novaLinha.appendChild(td);
            });

            const tdAcoes = document.createElement('td');
            tdAcoes.className = 'acoes-loja';
            const btnSalvar = document.createElement('button');
            btnSalvar.className = 'btn-copy';
            btnSalvar.style.background = '#e8f8f0';
            btnSalvar.style.color = '#1e8449';
            btnSalvar.textContent = '💾';
            btnSalvar.title = 'Salvar';
            btnSalvar.onclick = function() {
                const payload = { id: '', alias: alias };
                CAMPOS_LOJA_ORDEM.forEach(campo => { payload[campo] = inputs[campo].value.trim(); });
                salvarLoja(payload);
            };
            tdAcoes.appendChild(btnSalvar);
            novaLinha.appendChild(tdAcoes);

            tabela.appendChild(novaLinha);
            inputs['cod_info'].focus();
        }

        function salvarLoja(payload) {
            csrfFetch('/admin/cliente-loja/salvar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(data => {
                if (data.sucesso) {
                    location.reload();
                } else {
                    alert('Erro ao salvar: ' + (data.mensagem || 'tente novamente.'));
                }
            })
            .catch(() => alert('Não foi possível salvar. Verifique sua conexão.'));
        }

        function excluirLoja(botao) {
            if (!confirm('Excluir esta loja do cadastro?')) return;
            const linha = botao.closest('.linha-loja');
            const id = linha.dataset.id;

            csrfFetch('/admin/cliente-loja/excluir', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: id })
            })
            .then(r => r.json())
            .then(data => {
                if (data.sucesso) {
                    location.reload();
                } else {
                    alert('Erro ao excluir: ' + (data.mensagem || 'tente novamente.'));
                }
            })
            .catch(() => alert('Não foi possível excluir. Verifique sua conexão.'));
        }

        // --- Edição inline da string de conexão (CNAME) ---
        function editarCname(botao) {
            const celula = botao.closest('.cname-cell');
            const span = celula.querySelector('.cname-texto');
            const alias = celula.dataset.alias;
            const valorAtual = span.textContent;

            const input = document.createElement('input');
            input.type = 'text';
            input.value = valorAtual;
            input.className = 'form-control form-control-sm';
            input.style.display = 'inline-block';
            input.style.width = '260px';

            const btnSalvar = document.createElement('button');
            btnSalvar.className = 'btn-copy';
            btnSalvar.style.background = '#e8f8f0';
            btnSalvar.style.color = '#1e8449';
            btnSalvar.textContent = '💾 Salvar';
            btnSalvar.onclick = function() {
                csrfFetch('/admin/cname/salvar', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ alias: alias, cname: input.value.trim() })
                })
                .then(r => r.json())
                .then(data => {
                    if (data.sucesso) {
                        location.reload();
                    } else {
                        alert('Erro ao salvar: ' + (data.mensagem || 'tente novamente.'));
                    }
                })
                .catch(() => alert('Não foi possível salvar. Verifique sua conexão.'));
            };

            span.replaceWith(input);
            botao.replaceWith(btnSalvar);
            input.focus();
        }

        // --- Teste de conectividade do CNAME (roda ao carregar a página / clicar em Atualizar Dados) ---
        function testarCnames() {
            document.querySelectorAll('.cname-cell').forEach(celula => {
                const span = celula.querySelector('.cname-texto');
                const statusEl = celula.querySelector('.cname-status');
                if (!span || !statusEl) return;
                const host = span.textContent;

                csrfFetch('/api/cname/testar?' + new URLSearchParams({host: host.trim(), assinatura: celula.dataset.assinatura}))
                    .then(r => r.json())
                    .then(data => {
                        if (data.ativo) {
                            statusEl.textContent = '🟢';
                            statusEl.title = 'CNAME ativo (DNS resolve corretamente)';
                        } else {
                            statusEl.textContent = '🔴';
                            statusEl.title = 'CNAME inativo (não foi possível resolver)';
                        }
                    })
                    .catch(() => {
                        statusEl.textContent = '⚪';
                        statusEl.title = 'Não foi possível testar agora';
                    });
            });
        }
        document.addEventListener('DOMContentLoaded', testarCnames);

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
            {% if modo_historico and dados_grafico and dados_grafico.labels %}
                var ctxHistorico = document.getElementById('chart-historico-linha').getContext('2d');
                new Chart(ctxHistorico, {
                    type: 'line',
                    data: {{ dados_grafico|tojson }},
                    options: {
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
                                title: { display: true, text: 'Data' }
                            }
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
        .btn-primary { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
        .btn-outline-primary { color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
        .btn-outline-primary:hover { background-color: var(--cor-primaria) !important; color: white !important; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<script src="{{ url_for('static', filename='admin-destino.js') }}" defer></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>

    <!-- CÓDIGO CORRIGIDO COM O BOTÃO DE PERFIL -->
    <div class="header">
        <h2><i class="fa-solid fa-database me-2"></i>Gestão Administrativa de Databases - {{ servidor_atual }}</h2>
        <div class="d-flex align-items-center gap-2">
            <!-- Nome do usuário logado -->
            <span style="color: var(--cor-texto-suave);" class="me-1"><i class="fa-solid fa-circle-user me-1"></i>{{ usuario_nome }}{% if session.get('eh_master') %} <span class="badge" style="background:var(--cor-perigo);">Master</span>{% endif %}</span>
            <!-- Botão de Gestão de Perfil / Alterar Senha -->
            <button type="button" onclick="abrirModalPerfil()" class="btn-voltar" style="border:1px solid var(--cor-borda); cursor:pointer;">
                <i class="fa-solid fa-user-gear me-1"></i> Perfil
            </button>
            <a href="/servidor/DB01" class="btn-voltar">Servidores</a>
            <a href="/horarios" class="btn-voltar">Horários</a>
            <form action="/admin/logout" method="POST" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit"  class="btn-logout">Sair</button></form>
        </div>
    </div>

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
                    <select id="pasta_destino" name="pasta_destino" class="form-select" style="flex:1;min-width:220px" disabled>
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
    <div class="card shadow-sm mb-4 border-warning">
        <div class="card-header bg-warning text-dark fw-bold">
            <i class="fa-solid fa-users-gear me-2"></i>Gestão de Usuários e Permissões do Sistema (Master)
        </div>
        <div class="card-body">
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
                                    <button type="submit" class="btn btn-sm btn-outline-primary ms-1">💾</button>
                                </form>
                            {% endif %}
                        </td>
                        <td>
                            {% if not u.eh_master %}
                                <!-- Botão Toggle Ativo/Inativo -->
                                <form action="/admin/usuarios/toggle/{{ u.id }}" method="POST" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit"  class="btn btn-sm {% if u.ativo %}btn-outline-warning{% else %}btn-outline-success{% endif %}">
                                    {% if u.ativo %}Inativar{% else %}Ativar{% endif %}
                                </button></form>

                                <!-- Botão Reset Senha -->
                                <form action="/admin/usuarios/reset-senha/{{ u.id }}" method="POST" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit"  class="btn btn-sm btn-outline-primary" onclick="return confirm('Enviar link de redefinição de senha para este e-mail?')">
                                    <i class="fa-solid fa-key"></i> Reset Senha
                                </button></form>

                                <!-- Botão Excluir (Abre Modal) -->
                                <button class="btn btn-sm btn-outline-danger" data-bs-toggle="modal" data-bs-target="#modalExcluir{{ u.id }}">
                                    <i class="fa-solid fa-trash"></i> Excluir
                                </button>
                            {% else %}
                                <span class="text-muted">Protegido</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>

            <!-- MODAIS DE EXCLUSÃO (POSICIONADOS FORA DA TABELA PARA EVITAR QUEBRAS DE RENDERIZAÇÃO) -->
            {% for u in usuarios_lista %}
                {% if not u.eh_master %}
                <div class="modal fade" id="modalExcluir{{ u.id }}" tabindex="-1" aria-hidden="true">
                    <div class="modal-dialog">
                        <form action="/admin/usuarios/excluir" method="POST" class="modal-content">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                            <div class="modal-header bg-danger text-white">
                                <h5 class="modal-title">Excluir Usuário: {{ u.nome|nome_pessoa }}</h5>
                                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Fechar"></button>
                            </div>
                            <div class="modal-body">
                                <input type="hidden" name="user_id" value="{{ u.id }}">
                                <p class="text-danger fw-bold mb-3">Esta ação não poderá ser desfeita!</p>
                                <div class="mb-3">
                                    <label class="form-label">Digite sua senha de MASTER para confirmar:</label>
                                    <input type="password" name="senha_master" class="form-control" required autocomplete="off">
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancelar</button>
                                <button type="submit" class="btn btn-danger">Confirmar Exclusão</button>
                            </div>
                        </form>
                    </div>
                </div>
                {% endif %}
            {% endfor %}
        </div>
    </div>
    {% endif %}


    <!-- TABELA DE GERENCIAMENTO RÁPIDO (INATIVAÇÃO E EDIÇÃO EM LINHA COM CANCELAR) -->
    <div class="card">
        <h3 class="mb-3"><i class="fa-solid fa-list me-2"></i>Registros de Databases ({{ servidor_atual }})</h3>
        <div class="tabela-registros-scroll">
        <table class="table table-hover table-striped border align-middle" id="tabelaBancos">
            <thead class="table-dark" style="position: sticky; top: 0; z-index: 1;">
                <tr>
                    <th style="width: 180px; cursor:pointer;" onclick="ordenarTabelaRegistros(0, 'data')" title="Clique para ordenar">Data Tag / Tipo ⇳</th>
                    <th style="cursor:pointer;" onclick="ordenarTabelaRegistros(1, 'texto')" title="Clique para ordenar">Alias ⇳</th>
                    <th style="cursor:pointer;" onclick="ordenarTabelaRegistros(2, 'texto')" title="Clique para ordenar">Caminho ⇳</th>
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
                            <i class="fa-solid fa-file-lines icon-type" style="color:#bdc3c7;" title="Origem não identificada"></i>
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
                                <button class="btn btn-sm btn-outline-primary me-1" onclick="editarLinha({{ loop.index }})">
                                    <i class="fa-solid fa-pen-to-square"></i> Editar
                                </button>
                                <!-- Botão Inativar -->
                                <form action="/admin/inativar" method="POST" class="d-inline" onsubmit="return confirm('Inativar o alias {{ b.alias }}?') && pedirSenhaEConfirmar(this);">
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
        <div class="card-body p-0" id="editorConf" style="display: none;">
            <form action="/admin/salvar_raw" method="POST" onsubmit="return pedirSenhaEConfirmar(this)">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="servidor" value="{{ servidor_atual }}">
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
        
    <script>
    // --- Confirmação de senha antes de ações sensíveis (criar/editar/excluir alias, salvar .conf) ---
    // Evita que outra pessoa use a sessão aberta e esquecida de um usuário ausente do computador.
    function pedirSenhaEConfirmar(form) {
        const senha = prompt('Confirme sua senha para continuar com esta ação:');
        if (!senha) {
            return false; // cancelou ou deixou em branco: não envia o formulário
        }
        const campo = form.querySelector('.campo-senha-confirmacao');
        if (campo) campo.value = senha;
        return true;
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

        statusDiv.innerHTML = '<span class="text-muted small">Enviando...</span>';

        csrfFetch('/admin/solicitar-cname', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ fantasia, alias })
        })
        .then(r => r.json())
        .then(data => {
            if (data.sucesso) {
                statusDiv.innerHTML = '<div class="alert alert-success p-2 mb-0">' + data.mensagem + '</div>';
                document.getElementById('formSolicitarCname').reset();
            } else {
                statusDiv.innerHTML = '<div class="alert alert-danger p-2 mb-0">' + data.mensagem + '</div>';
            }
        })
        .catch(() => { statusDiv.innerHTML = '<div class="alert alert-danger p-2 mb-0">Não foi possível enviar. Verifique sua conexão.</div>'; });

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
        const originalCaminho = tr.getAttribute('data-original-caminho');

        // Cria input no campo de alias
        aliasTd.innerHTML = `<input type="text" id="input-alias-${id}" class="form-control form-control-sm" value="${originalAlias}" oninput="atualizarCaminhoEdicaoLinha(${id})">`;

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
        valCaminhoTd.innerHTML = `<code>${novoCaminho}</code>`;
    }

    function cancelarEdicaoLinha(id) {
        const tr = document.getElementById(`tr-${id}`);
        const aliasTd = tr.querySelector('.val-alias');
        const caminhoTd = tr.querySelector('.val-caminho');
        const acoesTd = tr.querySelector('.val-acoes');

        const originalAlias = tr.getAttribute('data-original-alias');
        const originalCaminho = tr.getAttribute('data-original-caminho');

        // Restaura os valores originais
        aliasTd.innerHTML = `<span class="alias-text">${originalAlias}</span>`;
        caminhoTd.innerHTML = `<code>${originalCaminho}</code>`;

        // Restaura botões padrão
        const servidorAtual = document.getElementById('servidor').value;
        acoesTd.innerHTML = `
            <div class="btn-group-acoes" id="acoes-${id}">
                <button class="btn btn-sm btn-outline-primary me-1" onclick="editarLinha(${id})">
                    <i class="fa-solid fa-pen-to-square"></i> Editar
                </button>
                <form action="/admin/inativar" method="POST" class="d-inline" onsubmit="return confirm('Inativar o alias ${originalAlias}?') && pedirSenhaEConfirmar(this);">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                    <input type="hidden" name="servidor" value="${servidorAtual}">
                    <input type="hidden" name="alias" value="${originalAlias}">
                    <input type="hidden" name="senha_confirmacao" class="campo-senha-confirmacao">
                    <button type="submit" class="btn btn-sm btn-outline-danger">
                        <i class="fa-solid fa-ban me-1"></i>Inativar
                    </button>
                </form>
            </div>
        `;
    }

    function salvarEdicaoLinha(id) {
        const tr = document.getElementById(`tr-${id}`);
        const inputAlias = document.getElementById(`input-alias-${id}`);
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
        
        const regexAlias = new RegExp(`^(\\s*${originalAlias}\\s*=.*)$`, 'm');
        if (regexAlias.test(texto)) {
            texto = texto.replace(regexAlias, `${novoAlias} = ${novoCaminho}`);
            areaTexto.value = texto;
            renderizarVisualizador();
        }

        // Atualiza os dados na tabela e salva novo estado original
        tr.setAttribute('data-original-alias', novoAlias);
        tr.setAttribute('data-original-caminho', novoCaminho);
        cancelarEdicaoLinha(id);

        const secaoArquivo = document.getElementById('secaoArquivo');
        secaoArquivo.scrollIntoView({ behavior: 'smooth' });
        alert(`Alias atualizado para "${novoAlias}". Clique em "Salvar Alterações no Arquivo Remoto" abaixo para confirmar a gravação.`);
        
        document.getElementById('visualizadorConf').style.display = 'none';
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

        if (editor.style.display === 'none') {
            visualizador.style.display = 'none';
            editor.style.display = 'block';
            btn.innerHTML = '<i class="fa-solid fa-eye"></i> Visualizar com Cores';
        } else {
            renderizarVisualizador();
            editor.style.display = 'none';
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
    <div id="modalPerfil" class="modal" style="display:none;">
        <div class="modal-content" style="max-width: 450px; margin: 5% auto; padding: 25px; border-radius: 8px; background: #fff;">
            <h3 style="margin-top:0; color: #333;"><i class="fa-solid fa-id-card me-2"></i>Meu Perfil</h3>
            
            <form id="formPerfil" onsubmit="salvarPerfil(event)">
                <div style="margin-bottom: 12px;">
                    <label style="font-weight:bold; display:block; margin-bottom:4px;">Nome:</label>
                    <input type="text" id="perfilNome" class="form-control" value="{{ usuario_nome }}" required>
                </div>
                
                <div style="margin-bottom: 15px;">
                    <label style="font-weight:bold; display:block; margin-bottom:4px;">E-mail:</label>
                    <!-- Adicionado readonly e disabled -->
                    <input type="email" id="perfilEmail" class="form-control" value="{{ usuario_email }}" readonly disabled style="background-color: #e9ecef;">
                </div>

                <hr style="margin: 15px 0; border: 0; border-top: 1px solid #ccc;">
                {% if veio_de_reset %}
                    <p style="font-size: 13px; color: #198754; margin-bottom: 10px;">✅ Você acessou por um link de redefinição de senha — pode definir a nova senha sem informar a atual.</p>
                {% else %}
                    <p style="font-size: 13px; color: #666; margin-bottom: 10px;">Preencha os campos abaixo apenas se desejar alterar a senha:</p>
                {% endif %}

                <div id="grupoSenhaAtual" style="margin-bottom: 12px; {% if veio_de_reset %}display:none;{% endif %}">
                    <label style="font-weight:bold; display:block; margin-bottom:4px;">Senha Atual:</label>
                    <input type="password" id="perfilSenhaAtual" class="form-control" placeholder="Informe para autorizar mudanças">
                </div>

                <div style="margin-bottom: 12px;">
                    <label style="font-weight:bold; display:block; margin-bottom:4px;">Nova Senha:</label>
                    <input type="password" id="perfilNovaSenha" class="form-control">
                </div>

                <div style="margin-bottom: 20px;">
                    <label style="font-weight:bold; display:block; margin-bottom:4px;">Confirmar Nova Senha:</label>
                    <input type="password" id="perfilConfirmaSenha" class="form-control">
                </div>

                <div style="text-align: right; gap: 8px; display: flex; justify-content: flex-end;">
                    <button type="button" class="btn btn-secondary" onclick="fecharModalPerfil()">Cancelar</button>
                    <button type="submit" class="btn btn-success fw-bold">Salvar Alterações</button>
                </div>
            </form>
        </div>
    </div>

    <script>
    function abrirModalPerfil() {
        document.getElementById('modalPerfil').style.display = 'block';
    }
    function fecharModalPerfil() {
        document.getElementById('modalPerfil').style.display = 'none';
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
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body class="bg-light d-flex align-items-center justify-content-center" style="height: 100vh;">
    <div class="card p-4 shadow-sm" style="width: 100%; max-width: 400px;">
        <h4 class="mb-3 text-center">Redefinir Senha</h4>
        <form id="formRedefinir" onsubmit="executarRedefinicao(event)">
            <!-- Captura o token da URL -->
            <input type="hidden" id="tokenUrl">

            <div class="mb-3">
                <label class="form-label font-weight-bold">Token de Validação:</label>
                <input type="text" id="tokenExibicao" class="form-control" readonly disabled>
            </div>

            <div class="mb-3">
                <label class="form-label">Nova Senha:</label>
                <input type="password" id="resetNovaSenha" class="form-control" required>
            </div>

            <div class="mb-3">
                <label class="form-label">Confirmar Nova Senha:</label>
                <input type="password" id="resetConfirmaSenha" class="form-control" required>
            </div>

            <button type="submit" class="btn btn-primary w-100 fw-bold">Alterar Senha</button>
        </form>
    </div>

    <script>
        // Extrai o token dos parâmetros da URL ao carregar a página
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
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--cor-fundo); display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card-login { background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: 35px 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); width: 380px; }
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
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
    <div class="card-login">
        <h3 class="text-center mb-1">🔐 Acesso Admin</h3>
        <p class="text-center text-muted small mb-4">Gestão dos arquivos databases.conf</p>
        
        {% if erro %}
            <div class="alert alert-danger p-2 small mb-3 text-center">{{ erro }}</div>
        {% endif %}
        {% if sucesso %}
            <div class="alert alert-success p-2 small mb-3 text-center">{{ sucesso }}</div>
        {% endif %}

        <form method="POST" action="/admin/login">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="hidden" name="next" value="{{ next or '' }}">
            <div class="mb-3">
                <label class="form-label fw-bold">E-mail:</label>
                <input type="email" name="email" class="form-control" required>
            </div>
            <div class="mb-3">
                <label class="form-label">Senha:</label>
                <input type="password" name="senha" class="form-control" required>
                <!-- Link "Esqueci a senha" inserido abaixo da senha -->
                <div class="text-end mt-1">
                    <a href="/admin/esqueci-senha" style="font-size: 13px; color: var(--cor-primaria); text-decoration: none;">Esqueci a senha</a>
                </div>
            </div>

            <button type="submit" class="btn btn-success w-100 fw-bold mt-2">Entrar</button>
            {% if allow_register %}
            <a href="/admin/register" class="btn btn-outline-secondary w-100 mt-2">Criar Nova Conta</a>
            {% endif %}
        </form>
    </div>
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
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root {
            --cor-primaria: #2563eb; --cor-primaria-escura: #1d4ed8;
            --cor-texto: #1e293b; --cor-texto-suave: #64748b;
            --cor-fundo: #f8fafc; --cor-superficie: #ffffff; --cor-borda: #e2e8f0;
            --cor-sucesso: #16a34a; --cor-aviso: #d97706; --cor-perigo: #dc2626; --raio: 8px;
        }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: var(--cor-fundo); display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card-login { background: var(--cor-superficie); border: 1px solid var(--cor-borda); padding: 35px 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.06); width: 380px; }
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
        .btn-success:hover { background-color: var(--cor-primaria-escura) !important; border-color: var(--cor-primaria-escura) !important; }
    </style>
<meta name="csrf-token" content="{{ csrf_token() }}">
<script src="{{ url_for('static', filename='csrf.js') }}"></script>
<link rel="stylesheet" href="{{ url_for('static', filename='ui.css') }}">
<script src="{{ url_for('static', filename='ui.js') }}" defer></script>
</head>
<body>
    <div class="card-login">
        <h3 class="text-center mb-1">🔑 Recuperar Senha</h3>
        <p class="text-center text-muted small mb-4">Informe seu e-mail cadastrado para receber o link de redefinição.</p>

        {% if erro %}<div class="alert alert-danger p-2 small mb-3 text-center">{{ erro }}</div>{% endif %}
        {% if sucesso %}<div class="alert alert-success p-2 small mb-3 text-center">{{ sucesso }}</div>{% endif %}

        <form method="POST" action="/admin/esqueci-senha">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="mb-3">
                <label class="form-label fw-bold">E-mail:</label>
                <input type="email" name="email" class="form-control" required>
            </div>
            <button type="submit" class="btn btn-success w-100 fw-bold mt-2">Enviar link de redefinição</button>
            <a href="/admin/login" class="btn btn-link w-100 text-center mt-2 text-decoration-none text-muted">⬅ Voltar ao Login</a>
        </form>
    </div>
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
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
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
    <div class="card-register">
        {% if pendente %}
            <h3 class="text-center mb-3">⏳ Cadastro Recebido</h3>
            <div class="alert alert-warning p-3 small text-center">
                Sua conta foi criada com sucesso, mas está <strong>aguardando aprovação</strong> do administrador.<br>
                Você receberá acesso assim que sua conta for ativada.
            </div>
            <a href="/admin/login" class="btn btn-outline-secondary w-100 mt-2">⬅ Voltar ao Login</a>
        {% else %}
            <h3 class="text-center mb-3">📝 Cadastro de Usuário</h3>
            {% if erro %}<div class="alert alert-danger p-2 small mb-3">{{ erro }}</div>{% endif %}
            <form method="POST" action="/admin/register">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <div class="mb-3">
                    <label class="form-label fw-bold">Nome:</label>
                    <input type="text" name="nome" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold">E-mail:</label>
                    <input type="email" name="email" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold">Senha:</label>
                    <input type="password" name="senha" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold">Confirmar Senha:</label>
                    <input type="password" name="confirmar_senha" class="form-control" required>
                </div>
                <button type="submit" class="btn btn-success w-100 fw-bold">Salvar Cadastro</button>
                <a href="/admin/login" class="btn btn-link w-100 text-center mt-2 text-decoration-none text-muted">⬅ Voltar ao Login</a>
            </form>
        {% endif %}
    </div>
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
            return render_template_string(HTML_LOGIN, erro="Credenciais inválidas.")

        user_id, nome, senha_hash, ativo, eh_master = user
        senha_valida = check_password_hash(senha_hash, senha)

        if not senha_valida:
            registrar_falha_login()
            return render_template_string(HTML_LOGIN, erro="Credenciais inválidas.")

        if not ativo and not eh_master:
            return render_template_string(HTML_LOGIN, erro="Sua conta ainda não foi ativada.")

        limpar_falhas_login()
        session.clear()
        session.permanent = True
        session['user_id'] = user_id
        session['user_nome'] = nome_para_assinatura(nome)
        session['eh_master'] = eh_master
        session['logged_in'] = True

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
        except Exception:
            # Não expõe detalhes do erro de SMTP ao usuário final
            pass

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

    conn = sqlite3.connect(DB_SISTEMA)
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email,))
    if cursor.fetchone():
        conn.close()
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
    conn.close()

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
    session.clear()
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

    if nova_senha:
        if nova_senha != confirmar_senha:
            flash('As senhas não coincidem!', 'danger')
            return redirect('/admin')

        senha_hash = generate_password_hash(nova_senha)
        conn = get_db_connection()
        conn.execute('UPDATE usuarios SET senha_hash = ? WHERE id = ?', (senha_hash, session['user_id']))
        conn.commit()
        conn.close()
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
        "UPDATE usuarios SET perm_servidores = ?, perm_gestao_bancos = ?, perm_horarios = ? WHERE id = ?",
        (valores['perm_servidores'], valores['perm_gestao_bancos'], valores['perm_horarios'], user_id)
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

    srv_atual = request.args.get('servidor', 'DB01')
    if srv_atual not in SERVIDORES: 
        srv_atual = 'DB01'

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
    if session.get('eh_master'):
        conn = get_db_connection()
        usuarios_lista = conn.execute("SELECT id, nome, email, eh_master, ativo, perm_servidores, perm_gestao_bancos, perm_horarios FROM usuarios").fetchall()
        conn.close()

    conteudo_raw = ler_databases_conf_remoto(srv_atual)
    bancos_conf, _, _ = obter_dados_servidor_linux(srv_atual, SERVIDORES[srv_atual], modo_busca_unificada=True, forcar_atualizacao=True)

    return render_template_string(
        HTML_ADMIN, servidores=SERVIDORES, servidor_atual=srv_atual,
        bancos_conf=bancos_conf, conteudo_raw=conteudo_raw,
        msg_sucesso=msg_sucesso, msg_erro=msg_erro, usuarios_lista=usuarios_lista,
        usuario_nome=usuario_nome, usuario_email=usuario_email,
        veio_de_reset=session.get('reset_senha_pendente', False)
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
    srv = request.form.get('servidor', '')
    def voltar(**mensagem):
        return redirect(url_for('bancos.admin_painel', servidor=srv, **mensagem))
    if srv not in SERVIDORES:
        return voltar(erro='Servidor inválido.')
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return voltar(erro='Senha de confirmação incorreta. Ação cancelada.')
    alias = sanitizar_alias(request.form.get('alias', ''))
    envolvidos = request.form.get('envolvidos', '').strip()
    arquivo = request.files.get('arquivo_fdb')
    tem_arquivo = arquivo is not None and bool(arquivo.filename)
    if not alias or len(alias) > 100 or len(envolvidos) > 300 or any(c in envolvidos for c in '\r\n'):
        return voltar(erro='Informe um alias válido e os responsáveis em uma única linha.')
    if tem_arquivo and not arquivo.filename.lower().endswith('.fdb'):
        return voltar(erro='Selecione um arquivo de banco .fdb.')
    if verificar_alias_existente_global(alias):
        return voltar(erro=f'O alias "{alias}" já está cadastrado. Use um alias diferente.')
    conteudo_atual = ler_databases_conf_remoto(srv)
    if conteudo_atual.startswith('# Erro ao ler'):
        return voltar(erro='Não foi possível ler a configuração do servidor. Nenhum banco foi enviado.')
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
    if not salvar_databases_conf_remoto(srv, conteudo_final):
        return voltar(erro='O destino foi preparado, mas o alias não pôde ser salvo. Se enviou um banco, ele permanece no destino; tente cadastrar o alias novamente sem reenviar o arquivo.')
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
        return render_template_string(HTML_LOGIN, erro="Link de redefinição inválido ou expirado!")

    # Força o login do usuário na sessão ativa
    session['user_id'] = usuario['id']
    session['user_nome'] = nome_para_assinatura(usuario['nome'])
    session['eh_master'] = usuario['eh_master']
    session['logged_in'] = True
    # Libera a próxima alteração de senha no modal de perfil sem exigir a senha atual
    # (o próprio link de e-mail, de uso único, já comprova a identidade do usuário)
    session['reset_senha_pendente'] = True

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
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return redirect(url_for('bancos.admin_painel', servidor=request.form.get('servidor'), erro="Senha de confirmação incorreta. Ação cancelada."))

    srv = request.form.get('servidor')
    alias_target = request.form.get('alias', '').strip()
    hoje = datetime.now().strftime('%d/%m/%Y')

    conteudo_atual = ler_databases_conf_remoto(srv)
    linhas = conteudo_atual.splitlines()
    novas_linhas = []

    for i, linha in enumerate(linhas):
        linha_limpa = linha.strip()
        if not linha_limpa.startswith('#') and '=' in linha_limpa:
            partes = linha_limpa.split('=', 1)
            alias_linha = partes[0].strip()
            if alias_linha.lower() == alias_target.lower():
                novas_linhas.append(f"# INATIVADO EM {hoje}")
                novas_linhas.append(f"# {linha_limpa}")
                continue
        novas_linhas.append(linha)

    conteudo_final = "\n".join(novas_linhas) + "\n"
    salvar_databases_conf_remoto(srv, conteudo_final)

    return redirect(f'/admin?servidor={srv}&sucesso=Alias "{alias_target}" inativado com sucesso!')

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

    conn.execute("DELETE FROM usuarios WHERE id = ? AND eh_master = 0", (user_id,))
    conn.commit()
    conn.close()

    return redirect('/admin?sucesso=Usuário excluído com sucesso!')

@bancos_bp.route('/admin/salvar_raw', methods=['POST'])
def admin_salvar_raw():
    if not session.get('logged_in'): return redirect('/admin/login')
    if not tem_permissao('perm_gestao_bancos'):
        return "Você não tem permissão para esta ação.", 403
    if not verificar_senha_confirmacao(request.form.get('senha_confirmacao')):
        return redirect(url_for('bancos.admin_painel', servidor=request.form.get('servidor'), erro="Senha de confirmação incorreta. Ação cancelada."))

    srv = request.form.get('servidor')
    conteudo_raw = request.form.get('conteudo_raw', '')
    salvar_databases_conf_remoto(srv, conteudo_raw)

    return redirect(f'/admin?servidor={srv}&sucesso=Arquivo databases.conf atualizado com sucesso!')

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

    conn = get_db_connection()
    if loja_id:
        conn.execute(
            "UPDATE clientes_lojas SET loj_codigo=?, loj_fantasia=?, loj_nome=?, loj_cnpj=?, cod_info=?, cli_situacao=? "
            "WHERE id=? AND alias=?",
            (valores['loj_codigo'], valores['loj_fantasia'], valores['loj_nome'], valores['loj_cnpj'],
             valores['cod_info'], valores['cli_situacao'], loja_id, alias)
        )
    else:
        conn.execute(
            "INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj, cod_info, cli_situacao) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alias, valores['loj_codigo'], valores['loj_fantasia'], valores['loj_nome'], valores['loj_cnpj'],
             valores['cod_info'], valores['cli_situacao'])
        )
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True})


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
    conn.execute("DELETE FROM clientes_lojas WHERE id = ?", (loja_id,))
    conn.commit()
    conn.close()
    return jsonify({"sucesso": True})


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
        .btn-success { background-color: var(--cor-primaria) !important; border-color: var(--cor-primaria) !important; }
        .btn-success:hover { background-color: var(--cor-primaria-escura) !important; border-color: var(--cor-primaria-escura) !important; }
        .btn-outline-secondary { color: var(--cor-texto-suave) !important; border-color: var(--cor-borda) !important; }
        .btn-warning { background-color: var(--cor-aviso) !important; border-color: var(--cor-aviso) !important; color: white !important; }
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
            "INSERT INTO clientes_lojas (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj) VALUES (?, ?, ?, ?, ?)",
            (alias, loj_codigo, loj_fantasia, loj_nome, loj_cnpj)
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


def assinatura_cname(host):
    return hmac.new(current_app.secret_key.encode(), host.strip().encode(), hashlib.sha256).hexdigest()


bancos_bp.add_app_template_global(assinatura_cname, name='assinatura_cname')


@bancos_bp.route('/api/cname/testar')
def api_testar_cname():
    host = request.args.get('host', '').strip()
    assinatura = request.args.get('assinatura', '')
    if not host or len(host) > 1024 or not hmac.compare_digest(assinatura_cname(host).encode(), assinatura.encode()):
        return jsonify(ativo=False, erro='CNAME inválido.'), 400
    host_limpo = host.split('/')[0].split(':')[0]
    try:
        socket.gethostbyname(host_limpo)
        ativo = True
    except (OSError, UnicodeError):
        ativo = False
    return jsonify(ativo=ativo)


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
        .btn { padding: 8px 14px; background: var(--cor-superficie); color: var(--cor-texto); border: 1px solid var(--cor-borda); border-radius: var(--raio); cursor: pointer; font-weight: 600; text-decoration: none; font-size: 0.85em; }
        .btn:hover { background: #f1f5f9; }
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
        .card-cliente { background: var(--cor-superficie); border: 1px solid var(--cor-borda); border-left: 4px solid var(--cor-borda); border-radius: var(--raio); padding: 12px 14px; }
        .card-cliente.status-OK { border-left-color: var(--cor-sucesso); }
        .card-cliente.status-ATRASADO { border-left-color: var(--cor-aviso); }
        .card-cliente.status-SEM_ARQUIVO, .card-cliente.status-ERRO { border-left-color: var(--cor-perigo); }
        .card-cliente .alias { font-weight: 700; word-break: break-word; }
        .card-cliente .info { font-size: 0.8em; color: var(--cor-texto-suave); margin-top: 4px; }
        .badge-status { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.72em; font-weight: 700; margin-top: 6px; }
        .badge-OK { background: #f0fdf4; color: #166534; }
        .badge-ATRASADO { background: #fffbeb; color: #92400e; }
        .badge-SEM_ARQUIVO, .badge-ERRO { background: #fef2f2; color: #991b1b; }
        .rodape-info { color: var(--cor-texto-suave); font-size: 0.85em; margin-top: 20px; }
        .alerta-erro { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 14px 18px; border-radius: var(--raio); margin-bottom: 18px; }
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

    {% if status.erro %}
        <div class="alerta-erro">⚠️ {{ status.erro }}</div>
    {% else %}
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
                <div class="card-cliente status-{{ c.status }}">
                    <div class="alias">{{ c.alias }}</div>
                    {% if c.timestamp %}<div class="info">🕒 {{ c.timestamp }}{% if c.idade %} ({{ c.idade }}){% endif %}</div>{% endif %}
                    {% if c.caminho %}<div class="info" title="{{ c.caminho }}">📁 {{ c.caminho }}</div>{% endif %}
                    <span class="badge-status badge-{{ c.status }}">
                        {% if c.status == 'OK' %}✅ Em dia
                        {% elif c.status == 'ATRASADO' %}🕒 Atrasado
                        {% elif c.status == 'SEM_ARQUIVO' %}❌ Sem arquivo
                        {% else %}❌ Erro{% endif %}
                    </span>
                </div>
            {% else %}
                <p style="color: var(--cor-texto-suave);">Nenhum cliente encontrado para este filtro.</p>
            {% endfor %}
        </div>

        <p class="rodape-info">Última atualização informada pelo servidor de backups: {{ status.ultima_atualizacao or '—' }}</p>
    {% endif %}
</body>
</html>
"""


@bancos_bp.route('/backups-ftp')
def exibir_backups_ftp():
    forcar = request.args.get('atualizar') == '1'
    status = obter_status_backups_ftp(forcar_atualizacao=forcar)
    filtro = request.args.get('filtro', '')  # '', 'ok', 'atrasado', 'sem_arquivo'

    clientes = status['clientes']
    if filtro == 'ok':
        clientes = [c for c in clientes if c['status'] == 'OK']
    elif filtro == 'atrasado':
        clientes = [c for c in clientes if c['status'] == 'ATRASADO']
    elif filtro == 'sem_arquivo':
        clientes = [c for c in clientes if c['status'] in ('SEM_ARQUIVO', 'ERRO')]

    clientes = sorted(clientes, key=lambda c: c['alias'].lower())

    return render_template_string(HTML_BACKUPS_FTP, status=status, clientes=clientes, filtro=filtro)


@bancos_bp.route('/')
@bancos_bp.route('/servidor/<nome_servidor>')
def exibir_servidor(nome_servidor='DB01'):
    if nome_servidor not in SERVIDORES: nome_servidor = 'DB01'
    ordem = request.args.get('ordem', 'nome')
    busca_termo = request.args.get('busca', '').strip()
    filtro_status = request.args.get('filtro', '').strip()
    forcar_atualizacao = request.args.get('atualizar') == '1'
    if forcar_atualizacao: limpar_cache_global()

    metricas_servidores = obter_metricas_servidores(salvar_db=False, forcar_atualizacao=forcar_atualizacao)
    
    if busca_termo:
        bancos_brutos, _, _ = buscar_em_todos_servidores(busca_termo, modo_busca_unificada=True, forcar_atualizacao=forcar_atualizacao)
    else:
        info = SERVIDORES[nome_servidor]
        bancos_brutos, _, _ = obter_dados_servidor_linux(nome_servidor, info, buscar_inativos=False, forcar_atualizacao=forcar_atualizacao)

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

    total_tamanho = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))

    if ordem == 'servidor': bancos.sort(key=lambda x: (x.get('servidor', ''), x['alias'].lower()))
    elif ordem == 'tamanho': bancos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': bancos.sort(key=lambda x: x['timestamp'], reverse=True)
    elif ordem == 'data_criacao': bancos.sort(key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True)
    elif ordem == 'cname': bancos.sort(key=lambda x: x['cname_string'])
    else: bancos.sort(key=lambda x: x['alias'].lower())

    bancos = aplicar_cnames_customizados(bancos)
    bancos = aplicar_lojas_clientes(bancos)

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
    forcar_atualizacao = request.args.get('atualizar') == '1'
    if forcar_atualizacao: limpar_cache_global()

    metricas_servidores = obter_metricas_servidores(salvar_db=False, forcar_atualizacao=forcar_atualizacao)
    bancos_brutos, _, _ = buscar_em_todos_servidores(busca_termo if busca_termo else None, modo_busca_unificada=bool(busca_termo), forcar_atualizacao=forcar_atualizacao)

    todos_bancos_validos = sorted([b for b in bancos_brutos if b.get('arquivo_existe')], key=lambda x: x['tamanho_bytes'], reverse=True)
    top5_global = todos_bancos_validos[:5]
    ultimos_hospedados_global = sorted(
        [b for b in bancos_brutos if b.get('arquivo_existe')],
        key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True
    )[:3]

    total_atencao = sum(1 for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == 'atencao')
    total_critico = sum(1 for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == 'critico')

    bancos = [b for b in bancos_brutos if b.get('status_inatividade', {}).get('status') == filtro_status] if filtro_status in ['atencao', 'critico'] else bancos_brutos
    total_tamanho = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))

    if ordem == 'servidor': bancos.sort(key=lambda x: (x.get('servidor', ''), x['alias'].lower()))
    elif ordem == 'tamanho': bancos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': bancos.sort(key=lambda x: x['timestamp'], reverse=True)
    elif ordem == 'data_criacao': bancos.sort(key=lambda x: converter_para_data_iso(x.get('data_criacao', '')), reverse=True)
    elif ordem == 'cname': bancos.sort(key=lambda x: x['cname_string'])
    else: bancos.sort(key=lambda x: x['alias'].lower())

    bancos = aplicar_cnames_customizados(bancos)
    bancos = aplicar_lojas_clientes(bancos)

    # FIX: filtro "sem_lojas" precisa ser aplicado depois de anexar as lojas, já que depende desse dado
    if filtro_status == 'sem_lojas':
        bancos = [b for b in bancos if not b.get('lojas')]
        total_tamanho = formatar_tamanho(sum(b['tamanho_bytes'] for b in bancos))

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
    forcar_atualizacao = request.args.get('atualizar') == '1'
    if forcar_atualizacao: limpar_cache_global()

    metricas_servidores = obter_metricas_servidores(salvar_db=False, forcar_atualizacao=forcar_atualizacao)
    bancos, total_bytes, _ = buscar_em_todos_servidores(busca_termo, buscar_inativos=True, modo_busca_unificada=bool(busca_termo), forcar_atualizacao=forcar_atualizacao)
    total_tamanho = formatar_tamanho(total_bytes)

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
    forcar_atualizacao = request.args.get('atualizar') == '1'
    if forcar_atualizacao: limpar_cache_global()

    metricas_servidores = obter_metricas_servidores(salvar_db=False, forcar_atualizacao=forcar_atualizacao)
    _, _, arquivos_orfaos = buscar_em_todos_servidores(varrer_orfaos=True, forcar_atualizacao=forcar_atualizacao)
    total_tamanho = formatar_tamanho(sum(a['tamanho_bytes'] for a in arquivos_orfaos))

    if ordem == 'tamanho': arquivos_orfaos.sort(key=lambda x: x['tamanho_bytes'], reverse=True)
    elif ordem == 'data': arquivos_orfaos.sort(key=lambda x: x['timestamp'], reverse=True)
    else: arquivos_orfaos.sort(key=lambda x: (x['servidor'], x['caminho_exibicao']))

    return render_template_string(
        HTML_LAYOUT, servidores=SERVIDORES, servidor_atual='ORFAOS', bancos=[],
        total_tamanho=total_tamanho, total_atencao=0, total_critico=0,
        ordem_atual=ordem, busca_termo='', filtro_status='',
        modo_todos=False, modo_inativos=False, modo_orfaos=True, modo_historico=False,
        metricas_servidores=metricas_servidores, top5_global=[], ultimos_hospedados_global=[], arquivos_orfaos=arquivos_orfaos, dados_historico=[]
    )

@bancos_bp.route('/historico')
def exibir_historico():
    servidores_selecionados = request.args.getlist('servidores')
    data_inicio = request.args.get('data_inicio', '').strip()
    data_fim = request.args.get('data_fim', '').strip()

    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()

    if not servidores_selecionados and not data_inicio and not data_fim:
        cursor.execute('SELECT MAX(data) FROM historico_servidores')
        ultima_data = cursor.fetchone()[0]
        
        if ultima_data:
            cursor.execute('''
                SELECT data, servidor, total_bytes, qtd_bancos 
                FROM historico_servidores 
                WHERE data = ? 
                ORDER BY servidor ASC
            ''', (ultima_data,))
            rows = cursor.fetchall()
        else:
            rows = []
    else:
        query = 'SELECT data, servidor, total_bytes, qtd_bancos FROM historico_servidores WHERE 1=1'
        params = []

        if servidores_selecionados:
            placeholders = ','.join(['?'] * len(servidores_selecionados))
            query += f' AND servidor IN ({placeholders})'
            params.extend(servidores_selecionados)

        if data_inicio:
            query += ' AND data >= ?'
            params.append(data_inicio)

        if data_fim:
            query += ' AND data <= ?'
            params.append(data_fim)

        query += ' ORDER BY data DESC, servidor ASC'
        cursor.execute(query, params)
        rows = cursor.fetchall()

    conn.close()

    dados_historico = []
    for r in rows:
        dados_historico.append({
            'data': r[0],
            'servidor': r[1],
            'total_bytes': r[2],
            'tamanho_str': formatar_tamanho(r[2]),
            'qtd_bancos': r[3]
        })

    resumo_crescimento = []
    dados_grafico = {}

    if dados_historico:
        por_servidor = {}
        for item in dados_historico:
            srv = item['servidor']
            if srv not in por_servidor:
                por_servidor[srv] = []
            por_servidor[srv].append(item)

        for srv, registros in por_servidor.items():
            registros_ordenados = sorted(registros, key=lambda x: x['data'])
            primeiro = registros_ordenados[0]
            ultimo = registros_ordenados[-1]
            diff_bytes = ultimo['total_bytes'] - primeiro['total_bytes']

            resumo_crescimento.append({
                'servidor': srv,
                'data_inicial': primeiro['data'],
                'data_final': ultimo['data'],
                'crescimento_str': formatar_tamanho(abs(diff_bytes)),
                'is_positivo': diff_bytes >= 0,
                'runway': calcular_runway_disco(srv, 0)
            })

        datas_unicas = sorted(list(set(item['data'] for item in dados_historico)))
        datasets_grafico = []
        
        cores = {
            'DB01': '#2ecc71', 'DB02': '#3498db', 'DB03': '#9b59b6',
            'DB04': '#f1c40f', 'DB05': '#e67e22', 'DB06': '#e74c3c'
        }

        for srv in sorted(por_servidor.keys()):
            valores_gb = []
            mapa_datas = {item['data']: round(item['total_bytes'] / (1024**3), 2) for item in por_servidor[srv]}
            
            for d in datas_unicas:
                valores_gb.append(mapa_datas.get(d, None))

            datasets_grafico.append({
                'label': srv,
                'data': valores_gb,
                'borderColor': cores.get(srv, '#34495e'),
                'backgroundColor': cores.get(srv, '#34495e'),
                'fill': False,
                'tension': 0.1
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
        metricas_servidores=[], top5_global=[], ultimos_hospedados_global=[], arquivos_orfaos=[], dados_historico=dados_historico,
        servidores_selecionados=servidores_selecionados, data_inicio=data_inicio, data_fim=data_fim,
        resumo_crescimento=resumo_crescimento, dados_grafico=dados_grafico
    )

@bancos_bp.route('/api/historico')
def api_historico():
    conn = sqlite3.connect(DB_HISTORICO)
    cursor = conn.cursor()
    cursor.execute('SELECT data, servidor, total_bytes, qtd_bancos FROM historico_servidores ORDER BY data DESC')
    rows = cursor.fetchall()
    conn.close()
    
    resultado = [{'data': r[0], 'servidor': r[1], 'tamanho_bytes': r[2], 'qtd_bancos': r[3]} for r in rows]
    return jsonify(resultado)


if __name__ == '__main__':
    raise SystemExit("Inicie pelo InfoMonitorDBClientes.py, não por bancos.py.")
