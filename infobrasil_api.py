import os
import re
import json
import base64
import ssl
import logging
import urllib.request
import urllib.error
from datetime import datetime

LOGGER = logging.getLogger(__name__)

URL_API_PADRAO = "https://info-api.infobrasilsistemas.com.br"


def limpar_cnpj(cnpj):
    """Remove caracteres não numéricos de um CNPJ/CPF."""
    return re.sub(r'\D', '', str(cnpj or ''))


def obter_caminho_env():
    """Retorna o caminho absoluto do arquivo .env."""
    caminho_diretorio = os.path.dirname(os.path.abspath(__file__))
    caminho_env = os.path.join(caminho_diretorio, '.env')
    if os.path.exists(caminho_env):
        return caminho_env
    return os.path.abspath('.env')


def salvar_variaveis_env(novas_variaveis):
    """Atualiza ou insere variáveis de ambiente no arquivo .env e no os.environ em runtime."""
    caminho_env = obter_caminho_env()
    chaves_restantes = dict(novas_variaveis)
    novas_linhas = []

    if os.path.exists(caminho_env):
        with open(caminho_env, 'r', encoding='utf-8') as f:
            for line in f:
                linha_limpa = line.strip()
                if linha_limpa and not linha_limpa.startswith('#') and '=' in linha_limpa:
                    chave = linha_limpa.split('=', 1)[0].strip()
                    if chave in chaves_restantes:
                        valor = chaves_restantes.pop(chave)
                        novas_linhas.append(f"{chave}={valor}\n")
                        continue
                novas_linhas.append(line if line.endswith('\n') else line + '\n')

    for chave, valor in chaves_restantes.items():
        novas_linhas.append(f"{chave}={valor}\n")

    with open(caminho_env, 'w', encoding='utf-8') as f:
        f.writelines(novas_linhas)

    for chave, valor in novas_variaveis.items():
        os.environ[chave] = str(valor)

    return True


def obter_configuracao_api():
    """Retorna a configuração atual da API InfoBrasil a partir do ambiente."""
    url = (os.getenv('INFOBRASIL_API_URL') or URL_API_PADRAO).strip().rstrip('/')
    user = (os.getenv('INFOBRASIL_API_USER') or '').strip()
    senha = (os.getenv('INFOBRASIL_API_PASS') or '').strip()
    return {
        'url': url,
        'user': user,
        'pass': senha,
        'configurado': bool(user and senha)
    }


def autenticar_api(url=None, user=None, password=None, timeout=12):
    """Realiza autenticação Basic Auth na API InfoBrasil e retorna (access_token, user_info, erro)."""
    cfg = obter_configuracao_api()
    api_url = (url or cfg['url']).strip().rstrip('/')
    api_user = (user if user is not None else cfg['user']).strip()
    api_pass = (password if password is not None else cfg['pass']).strip()

    if not api_user or not api_pass:
        return None, None, "Usuário e senha da API InfoBrasil não configurados."

    login_url = f"{api_url}/api/v1/auth/login"
    credenciais = f"{api_user}:{api_pass}"
    b64_creds = base64.b64encode(credenciais.encode('utf-8')).decode('ascii')

    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        login_url,
        data=b"",
        headers={
            'Authorization': f'Basic {b64_creds}',
            'User-Agent': 'InfoMonitor/1.0',
            'Accept': 'application/json'
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            token = data.get('access_token')
            if not token:
                return None, None, "API não retornou access_token na resposta de login."
            return token, data.get('user', {}), None
    except urllib.error.HTTPError as e:
        corpo = e.read().decode('utf-8', errors='ignore')
        LOGGER.warning("Falha de autenticação na API InfoBrasil (%s): %s", e.code, corpo)
        if e.code in (401, 403):
            return None, None, "Credenciais inválidas (usuário ou senha incorretos)."
        return None, None, f"Erro HTTP {e.code} retornado pela API: {corpo[:150]}"
    except urllib.error.URLError as e:
        LOGGER.warning("Falha de rede ao conectar à API InfoBrasil: %s", e)
        return None, None, f"Falha ao conectar com o servidor da API: {str(e.reason)}"
    except Exception as e:
        LOGGER.exception("Exceção ao autenticar na API InfoBrasil: %s", e)
        return None, None, f"Erro inesperado ao autenticar: {str(e)}"


def testar_conexao_api(url=None, user=None, password=None, timeout=12):
    """Testa a conectividade e autenticação com a API InfoBrasil, validando o acesso ao endpoint de clientes."""
    token, user_info, erro = autenticar_api(url=url, user=user, password=password, timeout=timeout)
    if erro:
        return False, erro, None

    cfg = obter_configuracao_api()
    api_url = (url or cfg['url']).strip().rstrip('/')
    clients_url = f"{api_url}/api/v1/clients?page=1&pageSize=1"

    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        clients_url,
        headers={
            'Authorization': f'Bearer {token}',
            'User-Agent': 'InfoMonitor/1.0',
            'Accept': 'application/json'
        }
    )

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            if resp.status == 200:
                nome_usuario = (user_info or {}).get('username') or 'usuário autenticado'
                return True, f"Conexão com a API InfoBrasil OK! Autenticado como '{nome_usuario}'.", user_info
            return False, f"API retornou status HTTP {resp.status} na consulta de clientes.", user_info
    except Exception as e:
        LOGGER.warning("Erro ao validar endpoint de clientes da API: %s", e)
        return False, f"Login efetuado com sucesso, mas falhou ao consultar clientes: {str(e)}", user_info


def buscar_todos_clientes_api(token, api_url, page_size=1000, timeout=20):
    """Percorre todas as páginas de clientes da API InfoBrasil e retorna um dicionário cnpj_limpo -> info."""
    ctx = ssl.create_default_context()
    mapa_cnpj = {}
    page = 1
    total_lidos = 0

    while True:
        url = f"{api_url}/api/v1/clients?page={page}&pageSize={page_size}"
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'User-Agent': 'InfoMonitor/1.0',
                'Accept': 'application/json'
            }
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            itens = data if isinstance(data, list) else data.get('items', data.get('data', []))
            if not itens:
                break
            for cliente in itens:
                cnpj_raw = cliente.get('CLI_CPF_CNPJ')
                cnpj_limpo = limpar_cnpj(cnpj_raw)
                cod_info = str(cliente.get('CLI_CODIGO') or '').strip()
                situacao = str(cliente.get('CLI_SITUACAO') or '').strip().upper()
                if cnpj_limpo and cod_info:
                    mapa_cnpj[cnpj_limpo] = {
                        'cod_info': cod_info,
                        'cli_situacao': situacao,
                        'nome': cliente.get('CLI_NOME') or '',
                        'fantasia': cliente.get('CLI_FANTASIA') or ''
                    }
            total_lidos += len(itens)
            if len(itens) < page_size:
                break
            page += 1

    return mapa_cnpj, total_lidos


def _obter_conexao_padrao():
    banco_path = os.getenv('DB_SISTEMA_PATH', 'sistema.db')
    conn = sqlite3.connect(banco_path)
    conn.row_factory = sqlite3.Row
    return conn


def sincronizar_lojas_com_api(conn_getter=None, url=None, user=None, password=None, usuario_solicitante=None):
    """Executa a sincronização entre a API InfoBrasil e o banco de dados sistema.db (clientes_lojas).
    Preenche e atualiza CodInfo e Situação com base no CNPJ.
    Registra o resultado no histórico de sincronização.
    """
    if conn_getter is None:
        conn_getter = _obter_conexao_padrao

    agora_dt = datetime.now()
    data_formatada = agora_dt.strftime('%d/%m/%Y às %H:%M:%S')

    token, user_info, erro = autenticar_api(url=url, user=user, password=password)
    if erro:
        _registrar_status_db(
            conn_getter, data_formatada, 0, 0, 0, 0,
            status='ERRO', mensagem=erro, solicitante=usuario_solicitante
        )
        return {
            'sucesso': False,
            'mensagem': f"Falha na autenticação da API: {erro}",
            'encontradas': 0,
            'atualizadas': 0,
            'total_api': 0
        }

    cfg = obter_configuracao_api()
    api_url = (url or cfg['url']).strip().rstrip('/')

    try:
        mapa_api, total_api = buscar_todos_clientes_api(token, api_url)
    except Exception as e:
        msg_erro = f"Falha ao carregar registros de clientes da API: {str(e)}"
        LOGGER.exception(msg_erro)
        _registrar_status_db(
            conn_getter, data_formatada, 0, 0, 0, 0,
            status='ERRO', mensagem=msg_erro, solicitante=usuario_solicitante
        )
        return {
            'sucesso': False,
            'mensagem': msg_erro,
            'encontradas': 0,
            'atualizadas': 0,
            'total_api': 0
        }

    conn = conn_getter()
    try:
        lojas = conn.execute("SELECT id, alias, loj_codigo, loj_cnpj, cod_info, cli_situacao FROM clientes_lojas").fetchall()
        total_lojas_banco = len(lojas)
        encontradas = 0
        atualizadas = 0

        for l in lojas:
            cnpj_limpo = limpar_cnpj(l['loj_cnpj'])
            if not cnpj_limpo:
                continue

            if cnpj_limpo in mapa_api:
                encontradas += 1
                dados_api = mapa_api[cnpj_limpo]
                novo_cod = dados_api['cod_info']
                nova_sit = dados_api['cli_situacao']

                cod_atual = (l['cod_info'] or '').strip()
                sit_atual = (l['cli_situacao'] or '').strip().upper()

                if cod_atual != novo_cod or sit_atual != nova_sit:
                    conn.execute(
                        "UPDATE clientes_lojas SET cod_info = ?, cli_situacao = ? WHERE id = ?",
                        (novo_cod, nova_sit, l['id'])
                    )
                    atualizadas += 1

        conn.commit()

        msg = (
            f"Sincronização concluída: {len(mapa_api)} clientes na API, {encontradas} lojas correspondentes no banco, "
            f"{atualizadas} loja(s) atualizada(s)."
        )

        _registrar_status_db(
            conn_getter, data_formatada, total_api, total_lojas_banco, encontradas, atualizadas,
            status='SUCESSO', mensagem=msg, solicitante=usuario_solicitante
        )

        return {
            'sucesso': True,
            'mensagem': msg,
            'total_api': total_api,
            'total_lojas_banco': total_lojas_banco,
            'encontradas': encontradas,
            'atualizadas': atualizadas
        }
    finally:
        conn.close()


def _registrar_status_db(conn_getter, data_str, total_api, total_banco, encontradas, atualizadas, status, mensagem, solicitante):
    """Grava o registro de execução na tabela api_sincronizacao_status."""
    if conn_getter is None:
        conn_getter = _obter_conexao_padrao
    try:
        conn = conn_getter()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS api_sincronizacao_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_execucao TEXT NOT NULL,
                total_api INTEGER DEFAULT 0,
                total_banco INTEGER DEFAULT 0,
                encontradas INTEGER DEFAULT 0,
                atualizadas INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                mensagem TEXT,
                usuario_solicitante TEXT
            )
        ''')
        conn.execute('''
            INSERT INTO api_sincronizacao_status
            (data_execucao, total_api, total_banco, encontradas, atualizadas, status, mensagem, usuario_solicitante)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (data_str, total_api, total_banco, encontradas, atualizadas, status, mensagem, solicitante or 'Automático (Cron)'))
        conn.commit()
        conn.close()
    except Exception as e:
        LOGGER.warning("Falha ao gravar status de sincronização no banco: %s", e)


def obter_ultimo_status_sincronizacao(conn_getter=None):
    """Retorna o registro mais recente da tabela api_sincronizacao_status."""
    if conn_getter is None:
        conn_getter = _obter_conexao_padrao
    try:
        conn = conn_getter()
        linha = conn.execute('''
            SELECT * FROM api_sincronizacao_status ORDER BY id DESC LIMIT 1
        ''').fetchone()
        conn.close()
        return dict(linha) if linha else None
    except Exception:
        return None
