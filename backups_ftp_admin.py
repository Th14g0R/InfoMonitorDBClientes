"""
Módulo de Gestão Administrativa de Backups FTP
Propriedade técnica: Antigravity Thiago #2

Fornece backend administrativo privado sob /admin/backups-ftp/... com:
- Autenticação restrita a session.get('user_id')/session.get('user_nome') e session.get('eh_master')
- Criptografia AEAD AES-256-GCM com envelope {'v': 'v1', 'kid': 'v1', 'n': '...', 'c': '...'}
- Chave externa via BACKUPS_FTP_KEY_PATH (44 chars Base64 URL-safe, estritamente FORA do repositório)
- Auditoria fail-closed com COMMIT síncrono no SQLite antes de descriptografar segredos
- Zero vazamento de dados_extras em GET/POST/PUT (máscara {k: null} e array extras_configurados)
- Endpoints específicos revelar-extra e copiar-extra com auditoria protegida (apenas 'campo_extra')
- Campo lancamento_em (YYYY-MM-DD opcional) criptografado por rotina
- Versionamento com BEGIN IMMEDIATE e constraint UNIQUE(rotina_id, versao_numero)
- Validação de gramática sintática de tarefas Cobian com BOM obrigatório (UTF-16LE ou UTF-8)
- Cabeçalhos de cache restritivos: Cache-Control: no-store, private e Vary: Cookie
"""

import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, request, jsonify, session, redirect, Response
from werkzeug.utils import secure_filename
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


LIMITE_LISTA_BYTES = 2 * 1024 * 1024  # 2 MiB (File bytes)
MAX_VERSOES_RETENCAO = 10
KEY_ID_PADRAO = "v1"


class CryptoKeyError(Exception):
    """Exceção levantada quando a chave externa está ausente, inacessível, dentro do repo ou inválida."""
    pass


class PayloadTooLargeError(Exception):
    """Exceção levantada quando o arquivo enviado excede 2 MiB."""
    pass


class FormatoInvalidoError(Exception):
    """Exceção levantada quando o formato do arquivo .list/.lst é rejeitado."""
    pass


# ---------------------------------------------------------------------------
# Gestão de Chave Externa e Criptografia AEAD AES-256-GCM
# ---------------------------------------------------------------------------

def obter_chave_aead() -> AESGCM:
    """
    Lê a chave de 32 bytes do caminho apontado em BACKUPS_FTP_KEY_PATH.
    Rejeita terminantemente se o arquivo estiver localizado dentro do repositório Git.
    """
    key_path_env = os.environ.get('BACKUPS_FTP_KEY_PATH')
    if not key_path_env:
        raise CryptoKeyError("Configuração de chave criptográfica não encontrada.")

    key_path = Path(key_path_env).resolve()
    if not key_path.is_file():
        raise CryptoKeyError("Arquivo de chave criptográfica inacessível.")

    # Rejeição estrita se a chave residir dentro do repositório da aplicação
    repo_root = Path(__file__).resolve().parent
    try:
        key_path.relative_to(repo_root)
        raise CryptoKeyError("A chave criptográfica externa não pode estar localizada dentro do repositório.")
    except ValueError:
        # Caminho está fora do repo (comportamento correto)
        pass

    # Limita tamanho máximo do arquivo para evitar leitura excessiva
    if key_path.stat().st_size > 1024:
        raise CryptoKeyError("Arquivo de chave inválido ou com tamanho excessivo.")

    try:
        with open(key_path, 'r', encoding='utf-8') as f:
            chave_str = f.read().strip()

        if len(chave_str) != 44:
            raise CryptoKeyError("A chave deve conter exatamente 44 caracteres Base64 URL-safe.")

        chave_bytes = base64.urlsafe_b64decode(chave_str)
        if len(chave_bytes) != 32:
            raise CryptoKeyError("A chave criptográfica deve conter exatamente 32 bytes.")

        return AESGCM(chave_bytes)
    except Exception as e:
        if isinstance(e, CryptoKeyError):
            raise
        raise CryptoKeyError("Falha na validação da chave criptográfica externa.") from e


def cifrar_dado(texto_plano: str | None, contexto_aad: str = "") -> str | None:
    """Cifra um texto com AES-256-GCM gerando nonce aleatório e empacotando com key_id em JSON."""
    if texto_plano is None:
        return None

    aesgcm = obter_chave_aead()
    nonce = os.urandom(12)
    dados = texto_plano.encode('utf-8')
    aad = contexto_aad.encode('utf-8')

    ciphertext = aesgcm.encrypt(nonce, dados, aad)

    payload = {
        'v': 'v1',
        'kid': KEY_ID_PADRAO,
        'n': base64.b64encode(nonce).decode('ascii'),
        'c': base64.b64encode(ciphertext).decode('ascii')
    }
    return json.dumps(payload, separators=(',', ':'))


def decifrar_dado(cifrado_json: str | None, contexto_aad: str = "") -> str:
    """Decifra um texto cifrado com AES-256-GCM verificando envelope, nonce, AAD e integridade."""
    if not cifrado_json:
        return ""

    aesgcm = obter_chave_aead()
    try:
        obj = json.loads(cifrado_json)
        if not isinstance(obj, dict):
            raise ValueError("Payload de criptografia inválido.")
        if obj.get('v') != 'v1' or obj.get('kid') != KEY_ID_PADRAO:
            raise ValueError("Versão de envelope ou identificador de chave incompatível.")

        nonce = base64.b64decode(obj['n'])
        ciphertext = base64.b64decode(obj['c'])
        aad = contexto_aad.encode('utf-8')

        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
        return plaintext.decode('utf-8')
    except Exception as e:
        raise ValueError("Falha de autenticação ou adulteração na descriptografia AEAD.") from e


# ---------------------------------------------------------------------------
# Identificação de Usuário e IP do Cliente
# ---------------------------------------------------------------------------

def _obter_identificador_usuario() -> str:
    """Extrai identificador do operador da sessão priorizando user_id e user_nome."""
    for chave in ('user_id', 'user_nome'):
        val = session.get(chave)
        if val is not None and str(val).strip():
            return str(val).strip()
    return 'master'


def _obter_ip_cliente() -> str:
    """Extrai o IP do cliente via remote_addr sem confiar em headers arbitrários."""
    return request.remote_addr or '127.0.0.1'


# ---------------------------------------------------------------------------
# Inicialização Idempotente das Tabelas SQLite
# ---------------------------------------------------------------------------

def _inicializar_banco(conn: sqlite3.Connection):
    """Cria as tabelas e índices de administração de backups FTP caso não existam."""
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS backups_ftp_rotinas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_alias TEXT NOT NULL,
            nome_rotina_cifrado TEXT,
            servidor_cifrado TEXT,
            porta_cifrada TEXT,
            usuario_cifrado TEXT,
            senha_cifrada TEXT,
            responsavel_cifrado TEXT,
            lancamento_em_cifrado TEXT,
            notas_cifradas TEXT,
            dados_extras_json_cifrado TEXT,
            status INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL,
            atualizado_por TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_backups_ftp_rotinas_alias ON backups_ftp_rotinas(cliente_alias);

        CREATE TABLE IF NOT EXISTS backups_ftp_listas_versoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rotina_id INTEGER NOT NULL,
            versao_numero INTEGER NOT NULL,
            tamanho_bytes INTEGER NOT NULL,
            encoding_detectado TEXT NOT NULL,
            checksum_sha256 TEXT NOT NULL,
            nome_original_cifrado TEXT,
            comentario_cifrado TEXT,
            conteudo_cifrado TEXT NOT NULL,
            enviado_por TEXT NOT NULL,
            enviado_em TEXT NOT NULL,
            UNIQUE(rotina_id, versao_numero),
            FOREIGN KEY(rotina_id) REFERENCES backups_ftp_rotinas(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_backups_ftp_listas_rotina ON backups_ftp_listas_versoes(rotina_id);

        CREATE TABLE IF NOT EXISTS backups_ftp_auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id TEXT NOT NULL,
            cliente_alias TEXT NOT NULL,
            rotina_id INTEGER,
            versao_id INTEGER,
            campo_ou_segredo TEXT NOT NULL,
            acao TEXT NOT NULL,
            ip_origem TEXT NOT NULL,
            horario_utc TEXT NOT NULL,
            resultado TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_backups_ftp_auditoria_alias ON backups_ftp_auditoria(cliente_alias);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Auditoria Fail-Closed com Commit Síncrono Obrigatório
# ---------------------------------------------------------------------------

def _gravar_auditoria_commit(conn: sqlite3.Connection, usuario_id: str, cliente_alias: str,
                             rotina_id: int | None, versao_id: int | None, campo_ou_segredo: str,
                             acao: str, ip_origem: str, resultado: str):
    """
    Grava metadados de auditoria em backups_ftp_auditoria e executa COMMIT síncrono.
    Se o commit falhar por erro de disco, lock ou permissão, a exceção é propagada
    e o fluxo chamador interrompe a execução sem revelar nenhum segredo.
    """
    cursor = conn.cursor()
    agora_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    cursor.execute("""
        INSERT INTO backups_ftp_auditoria (
            usuario_id, cliente_alias, rotina_id, versao_id, campo_ou_segredo,
            acao, ip_origem, horario_utc, resultado
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(usuario_id), str(cliente_alias), rotina_id, versao_id, campo_ou_segredo, acao, ip_origem, agora_iso, resultado))
    conn.commit()


def _gravar_auditoria_na_transacao(cursor: sqlite3.Cursor, usuario_id: str, cliente_alias: str,
                                   rotina_id: int | None, versao_id: int | None, campo_ou_segredo: str,
                                   acao: str, ip_origem: str, resultado: str):
    """Grava auditoria dentro de uma transação aberta já existente (sem commit prematuro)."""
    agora_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    cursor.execute("""
        INSERT INTO backups_ftp_auditoria (
            usuario_id, cliente_alias, rotina_id, versao_id, campo_ou_segredo,
            acao, ip_origem, horario_utc, resultado
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (str(usuario_id), str(cliente_alias), rotina_id, versao_id, campo_ou_segredo, acao, ip_origem, agora_iso, resultado))


# ---------------------------------------------------------------------------
# Normalização e Formatação de Dados Extras (Sensível / Não-Sensível)
# ---------------------------------------------------------------------------

def _deserializar_extras_internos(extras_str: str) -> dict[str, dict]:
    """
    Decodifica o JSON de extras e normaliza para o formato {'v': str, 's': bool}.
    Formatos legados são interpretados como sensíveis (s=True).
    Se extras_str não estiver vazio mas o JSON for inválido ou não for dict,
    levanta ValueError para impedir que falha silenciosa apague os extras.
    """
    if not extras_str or not extras_str.strip():
        return {}

    try:
        dados = json.loads(extras_str)
    except Exception as e:
        raise ValueError("Falha de integridade ao decodificar JSON interno de extras.") from e

    if not isinstance(dados, dict):
        raise ValueError("Payload interno de extras deve ser um objeto JSON.")

    normalizado = {}
    for k, item in dados.items():
        chave = str(k)
        # Reconhecimento estrito do formato interno: dict com chaves 'v' e 's' (sendo 's' bool estrito)
        if isinstance(item, dict) and 'v' in item and 's' in item and type(item['s']) is bool:
            valor = str(item['v']) if item['v'] is not None else ''
            sensivel = item['s']
            normalizado[chave] = {'v': valor, 's': sensivel}
        elif isinstance(item, dict):
            # Dict legado arbitrário: preservar serializado e s=True para não expor legado
            normalizado[chave] = {'v': json.dumps(item), 's': True}
        else:
            # Formato legado primitivo: string/número -> sensivel = True por padrão
            normalizado[chave] = {'v': str(item if item is not None else ''), 's': True}
    return normalizado


def _formatar_extras_para_resposta(extras_normalizados: dict[str, dict]):
    """
    Formata extras para payload de resposta JSON:
    - dados_extras: {k: None se sensivel s=True senão v}
    - extras_sensiveis: {k: bool(s)}
    - extras_configurados: [k1, k2, ...]
    """
    dados_extras = {}
    extras_sensiveis = {}
    extras_configurados = list(extras_normalizados.keys())

    for k, item in extras_normalizados.items():
        sensivel = item['s']
        extras_sensiveis[k] = sensivel
        if sensivel:
            dados_extras[k] = None
        else:
            dados_extras[k] = item['v']

    return dados_extras, extras_sensiveis, extras_configurados


# ---------------------------------------------------------------------------
# Helpers de Resposta e Cabeçalhos HTTP
# ---------------------------------------------------------------------------

def _resposta_com_headers(resp, status_code=200):
    """Garante Cache-Control: no-store, private e Vary: Cookie em todas as respostas."""
    if not isinstance(resp, Response):
        resp = jsonify(resp)
    resp.status_code = status_code
    resp.headers['Cache-Control'] = 'no-store, private'
    resp.headers['Vary'] = 'Cookie'
    return resp


def _requer_master(f):
    """Guardião de autenticação e perfil Master."""
    @wraps(f)
    def decorado(*args, **kwargs):
        if not session.get('logged_in'):
            resp = redirect('/admin/login')
            resp.headers['Cache-Control'] = 'no-store, private'
            resp.headers['Vary'] = 'Cookie'
            return resp

        if not session.get('eh_master'):
            return _resposta_com_headers({'erro': 'Acesso negado. Requer perfil Master.'}, 403)

        return f(*args, **kwargs)
    return decorado


# ---------------------------------------------------------------------------
# Validação e Processamento Sintático de Listas Cobian (.list / .lst)
# ---------------------------------------------------------------------------

def _validar_e_processar_lista(file_bytes: bytes, filename: str):
    """
    Valida tamanho (2 MiB), extensão (.list / .lst), ausência de caracteres de controle no nome,
    BOM obrigatório (UTF-16LE ou UTF-8) e gramática estrutural de tarefas do Cobian Backup.
    """
    # Checagem de quebras de linha ou caracteres de controle no nome do arquivo
    if re.search(r'[\r\n\x00-\x1f]', filename):
        raise FormatoInvalidoError("Nome de arquivo contém caracteres de controle não permitidos.")

    extensoes_validas = ('.list', '.lst')
    fn_lower = filename.lower()
    if not any(fn_lower.endswith(ext) for ext in extensoes_validas):
        raise FormatoInvalidoError("Extensão não permitida. Apenas arquivos .list ou .lst são aceitos.")

    if len(file_bytes) > LIMITE_LISTA_BYTES:
        raise PayloadTooLargeError("Arquivo excede o limite máximo permitido de 2 MiB.")

    # Rejeição imediata de executáveis Windows PE
    if file_bytes.startswith(b'MZ'):
        raise FormatoInvalidoError("Formato inválido: arquivos executáveis não são permitidos.")

    # Exige BOM obrigatório: UTF-16LE (\xff\xfe) das amostras aprovadas ou UTF-8 (\xef\xbb\xbf)
    encoding = None
    texto_decodificado = None

    if file_bytes.startswith(b'\xff\xfe'):
        encoding = 'UTF-16LE'
        try:
            texto_decodificado = file_bytes[2:].decode('utf-16le')
        except UnicodeDecodeError:
            raise FormatoInvalidoError("Falha na decodificação UTF-16LE do arquivo.")
    elif file_bytes.startswith(b'\xef\xbb\xbf'):
        encoding = 'UTF-8'
        try:
            texto_decodificado = file_bytes[3:].decode('utf-8')
        except UnicodeDecodeError:
            raise FormatoInvalidoError("Falha na decodificação UTF-8 com BOM do arquivo.")
    else:
        raise FormatoInvalidoError("Arquivo rejeitado: formato exige codificação com BOM (UTF-16LE ou UTF-8).")

    # Rejeita caracteres de controle NUL após a decodificação
    if '\0' in texto_decodificado:
        raise FormatoInvalidoError("Arquivo contém caracteres nulos de controle não permitidos.")

    # Validação sintática da gramática Cobian:
    # 1. Delimitadores de bloco { e }
    linhas = [ln.strip() for ln in texto_decodificado.splitlines() if ln.strip()]
    if not linhas or not linhas[0].startswith('{') or not linhas[-1].endswith('}'):
        raise FormatoInvalidoError("Arquivo de lista Cobian deve conter delimitadores válidos de tarefas ({ ... }).")

    # 2. Presença de atributos estruturais Cobian (linhas chave=valor, comentários // ou diretivas de tarefa)
    linhas_propriedades = [ln for ln in linhas if '=' in ln or ln.startswith('//') or ln.startswith('[')]
    if len(linhas_propriedades) < 1:
        raise FormatoInvalidoError("Arquivo não possui a estrutura sintática válida de tarefas do Cobian.")

    checksum_sha256 = hashlib.sha256(file_bytes).hexdigest()
    return texto_decodificado, encoding, checksum_sha256


# ---------------------------------------------------------------------------
# Registro de Rotas no Blueprint
# ---------------------------------------------------------------------------

def configurar_backups_ftp_admin(bp: Blueprint, conectar, obter_status):
    """
    Ponto de entrada obrigatório para integração em bancos.py.
    Inicializa as tabelas no banco e registra todas as rotas administrativas em bp.
    """
    conn_init = conectar()
    try:
        _inicializar_banco(conn_init)
    finally:
        conn_init.close()

    if getattr(bp, '_backups_ftp_admin_rotas_configuradas', False):
        return
    bp._backups_ftp_admin_rotas_configuradas = True

    # -----------------------------------------------------------------------
    # GET: Listar todos os clientes conhecidos (feed público + banco privado)
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/clientes', methods=['GET'])
    @_requer_master
    def listar_todos_clientes():
        aliases = set()

        # 1. Extrai do feed de monitoramento com proteção contra falhas externas
        try:
            status_info = obter_status()
            if isinstance(status_info, dict):
                for c in status_info.get('clientes', []):
                    if isinstance(c, dict) and c.get('alias'):
                        aliases.add(str(c['alias']).strip())
        except Exception:
            pass

        # 2. Extrai clientes com rotinas cadastradas no banco SQLite (inclui clientes fora do feed)
        conn = conectar()
        try:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT DISTINCT cliente_alias FROM backups_ftp_rotinas").fetchall()
            for r in rows:
                if r[0]:
                    aliases.add(str(r[0]).strip())
        finally:
            conn.close()

        return _resposta_com_headers({
            'sucesso': True,
            'clientes': sorted(list(aliases))
        }, 200)

    # -----------------------------------------------------------------------
    # GET: Listar rotinas de um cliente
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/cliente/<alias>/rotinas', methods=['GET'])
    @_requer_master
    def listar_rotinas_cliente(alias):
        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            try:
                obter_chave_aead()
            except CryptoKeyError:
                return _resposta_com_headers({'erro': 'Serviço de criptografia temporariamente indisponível.'}, 503)

            # Auditoria prévia obrigatória antes de descriptografar qualquer payload
            usuario_id = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()
            try:
                _gravar_auditoria_commit(
                    conn,
                    usuario_id=usuario_id,
                    cliente_alias=alias,
                    rotina_id=None,
                    versao_id=None,
                    campo_ou_segredo='dados_extras',
                    acao='consultar_controle',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )
            except Exception:
                return _resposta_com_headers({'erro': 'Falha ao registrar auditoria de segurança.'}, 500)

            cursor = conn.cursor()
            rotinas_raw = cursor.execute("""
                SELECT * FROM backups_ftp_rotinas
                WHERE cliente_alias = ?
                ORDER BY id ASC
            """, (alias,)).fetchall()

            resultado = []
            for r in rotinas_raw:
                rid = r['id']
                try:
                    nome_rotina = decifrar_dado(r['nome_rotina_cifrado'], f"rotina:{rid}:nome")
                    servidor = decifrar_dado(r['servidor_cifrado'], f"rotina:{rid}:servidor")
                    porta = decifrar_dado(r['porta_cifrada'], f"rotina:{rid}:porta")
                    usuario = decifrar_dado(r['usuario_cifrado'], f"rotina:{rid}:usuario")
                    responsavel = decifrar_dado(r['responsavel_cifrado'], f"rotina:{rid}:responsavel")
                    lancamento_em = decifrar_dado(r['lancamento_em_cifrado'], f"rotina:{rid}:lancamento") if r['lancamento_em_cifrado'] else None
                    notas = decifrar_dado(r['notas_cifradas'], f"rotina:{rid}:notas")
                    extras_str = decifrar_dado(r['dados_extras_json_cifrado'], f"rotina:{rid}:extras")
                    extras_normalizados = _deserializar_extras_internos(extras_str)
                except Exception:
                    return _resposta_com_headers({'erro': 'Falha de integridade ao decifrar dados da rotina.'}, 503)

                dados_extras_resp, extras_sensiveis_resp, extras_configurados_resp = _formatar_extras_para_resposta(extras_normalizados)

                ultima_lista_raw = cursor.execute("""
                    SELECT versao_numero, tamanho_bytes, encoding_detectado, checksum_sha256, enviado_em, enviado_por
                    FROM backups_ftp_listas_versoes
                    WHERE rotina_id = ?
                    ORDER BY versao_numero DESC LIMIT 1
                """, (rid,)).fetchone()

                total_listas = cursor.execute("""
                    SELECT COUNT(*) FROM backups_ftp_listas_versoes WHERE rotina_id = ?
                """, (rid,)).fetchone()[0]

                resultado.append({
                    'id': rid,
                    'cliente_alias': r['cliente_alias'],
                    'nome_rotina': nome_rotina,
                    'servidor': servidor,
                    'porta': porta,
                    'usuario': usuario,
                    'senha_configurada': bool(r['senha_cifrada']),
                    'responsavel': responsavel,
                    'lancamento_em': lancamento_em,
                    'notas': notas,
                    'dados_extras': dados_extras_resp,
                    'extras_sensiveis': extras_sensiveis_resp,
                    'extras_configurados': extras_configurados_resp,
                    'status': r['status'],
                    'revision': r['revision'],
                    'criado_em': r['criado_em'],
                    'atualizado_em': r['atualizado_em'],
                    'atualizado_por': r['atualizado_por'],
                    'total_listas': total_listas,
                    'ultima_lista': dict(ultima_lista_raw) if ultima_lista_raw else None
                })

            return _resposta_com_headers({
                'sucesso': True,
                'cliente_alias': alias,
                'rotinas': resultado
            }, 200)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Criar nova rotina
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina', methods=['POST'])
    @_requer_master
    def criar_rotina():
        payload = request.get_json(silent=True)
        if payload is None:
            payload = request.form.to_dict() if request.form else None

        if not isinstance(payload, dict):
            return _resposta_com_headers({'erro': 'Payload deve ser um objeto JSON válido.'}, 400)

        cliente_alias = payload.get('cliente_alias')
        if not isinstance(cliente_alias, str) or not cliente_alias.strip():
            return _resposta_com_headers({'erro': 'O alias do cliente é obrigatório e deve ser um texto válido.'}, 400)
        cliente_alias = cliente_alias.strip()

        # Validação do campo status
        status_val = payload.get('status', 1)
        try:
            status = int(status_val)
        except (ValueError, TypeError):
            return _resposta_com_headers({'erro': 'O status deve ser um número inteiro.'}, 400)

        # Validação de data real do campo lancamento_em (YYYY-MM-DD opcional)
        lancamento_em = payload.get('lancamento_em')
        if lancamento_em:
            if not isinstance(lancamento_em, str):
                return _resposta_com_headers({'erro': 'Data de lançamento deve ser um texto no formato YYYY-MM-DD.'}, 400)
            lancamento_em = lancamento_em.strip()
            try:
                datetime.strptime(lancamento_em, '%Y-%m-%d')
            except ValueError:
                return _resposta_com_headers({'erro': 'Data de lançamento inválida. Utilize uma data real no formato YYYY-MM-DD.'}, 400)
        else:
            lancamento_em = None

        nome_rotina = payload.get('nome_rotina') or ''
        servidor = payload.get('servidor') or ''
        porta = payload.get('porta') or ''
        usuario = payload.get('usuario') or ''
        senha = payload.get('senha')
        responsavel = payload.get('responsavel') or ''
        notas = payload.get('notas') or ''

        dados_extras = payload.get('dados_extras')
        if dados_extras is None:
            dados_extras = {}
        elif isinstance(dados_extras, str):
            try:
                dados_extras = json.loads(dados_extras)
            except Exception:
                return _resposta_com_headers({'erro': 'dados_extras deve ser um objeto válido.'}, 400)
        elif not isinstance(dados_extras, dict):
            return _resposta_com_headers({'erro': 'dados_extras deve ser um objeto dicionário.'}, 400)

        extras_sensiveis_input = payload.get('extras_sensiveis')
        if extras_sensiveis_input is not None:
            if isinstance(extras_sensiveis_input, str):
                try:
                    extras_sensiveis_input = json.loads(extras_sensiveis_input)
                except Exception:
                    return _resposta_com_headers({'erro': 'extras_sensiveis deve ser um objeto válido.'}, 400)
            if not isinstance(extras_sensiveis_input, dict):
                return _resposta_com_headers({'erro': 'extras_sensiveis deve ser um objeto dicionário.'}, 400)

            for k_sens, v_sens in extras_sensiveis_input.items():
                if v_sens is not None and type(v_sens) is not bool:
                    return _resposta_com_headers({'erro': f'O valor de sensibilidade para a chave "{k_sens}" deve ser um booleano estrito (true/false).'}, 400)
        else:
            extras_sensiveis_input = {}

        extras_normalizados = {}
        for k, v in dados_extras.items():
            chave_str = str(k)
            # Default para novos campos é False, a menos que especificado explicitamente em extras_sensiveis
            if chave_str in extras_sensiveis_input and extras_sensiveis_input[chave_str] is not None:
                sensivel = extras_sensiveis_input[chave_str]
            else:
                sensivel = False
            extras_normalizados[chave_str] = {
                'v': str(v) if v is not None else '',
                's': sensivel
            }

        try:
            obter_chave_aead()
        except CryptoKeyError:
            return _resposta_com_headers({'erro': 'Serviço de criptografia temporariamente indisponível.'}, 503)

        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            agora_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            operador = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            cursor.execute("""
                INSERT INTO backups_ftp_rotinas (
                    cliente_alias, status, revision, criado_em, atualizado_em, atualizado_por
                ) VALUES (?, ?, 1, ?, ?, ?)
            """, (cliente_alias, status, agora_iso, agora_iso, operador))
            rotina_id = cursor.lastrowid

            nome_cifrado = cifrar_dado(nome_rotina, f"rotina:{rotina_id}:nome")
            servidor_cifrado = cifrar_dado(servidor, f"rotina:{rotina_id}:servidor")
            porta_cifrada = cifrar_dado(porta, f"rotina:{rotina_id}:porta")
            usuario_cifrado = cifrar_dado(usuario, f"rotina:{rotina_id}:usuario")
            senha_cifrada = cifrar_dado(str(senha), f"rotina:{rotina_id}:senha") if senha else None
            resp_cifrado = cifrar_dado(responsavel, f"rotina:{rotina_id}:responsavel")
            lanc_cifrado = cifrar_dado(lancamento_em, f"rotina:{rotina_id}:lancamento") if lancamento_em else None
            notas_cifradas = cifrar_dado(notas, f"rotina:{rotina_id}:notas")
            extras_cifrados = cifrar_dado(json.dumps(extras_normalizados), f"rotina:{rotina_id}:extras")

            cursor.execute("""
                UPDATE backups_ftp_rotinas SET
                    nome_rotina_cifrado = ?,
                    servidor_cifrado = ?,
                    porta_cifrada = ?,
                    usuario_cifrado = ?,
                    senha_cifrada = ?,
                    responsavel_cifrado = ?,
                    lancamento_em_cifrado = ?,
                    notas_cifradas = ?,
                    dados_extras_json_cifrado = ?
                WHERE id = ?
            """, (nome_cifrado, servidor_cifrado, porta_cifrada, usuario_cifrado, senha_cifrada,
                  resp_cifrado, lanc_cifrado, notas_cifradas, extras_cifrados, rotina_id))

            # Auditoria gravada na mesma transação
            _gravar_auditoria_na_transacao(
                cursor,
                usuario_id=operador,
                cliente_alias=cliente_alias,
                rotina_id=rotina_id,
                versao_id=None,
                campo_ou_segredo='rotina',
                acao='criar_rotina',
                ip_origem=ip_origem,
                resultado='sucesso'
            )
            conn.commit()

            dados_extras_resp, extras_sensiveis_resp, extras_configurados_resp = _formatar_extras_para_resposta(extras_normalizados)
            return _resposta_com_headers({
                'sucesso': True,
                'mensagem': 'Rotina criada com sucesso.',
                'rotina': {
                    'id': rotina_id,
                    'cliente_alias': cliente_alias,
                    'nome_rotina': nome_rotina,
                    'servidor': servidor,
                    'porta': porta,
                    'usuario': usuario,
                    'senha_configurada': bool(senha_cifrada),
                    'responsavel': responsavel,
                    'lancamento_em': lancamento_em,
                    'notas': notas,
                    'dados_extras': dados_extras_resp,
                    'extras_sensiveis': extras_sensiveis_resp,
                    'extras_configurados': extras_configurados_resp,
                    'status': status,
                    'revision': 1
                }
            }, 201)
        except Exception:
            conn.rollback()
            return _resposta_com_headers({'erro': 'Falha interna ao processar operação.'}, 500)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # PUT: Atualizar rotina existente
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>', methods=['PUT'])
    @_requer_master
    def atualizar_rotina(id):
        payload = request.get_json(silent=True)
        if payload is None:
            payload = request.form.to_dict() if request.form else None

        if not isinstance(payload, dict):
            return _resposta_com_headers({'erro': 'Payload deve ser um objeto JSON válido.'}, 400)

        expected_revision = payload.get('expected_revision')
        if expected_revision is None:
            return _resposta_com_headers({'erro': 'O campo expected_revision é obrigatório.'}, 400)

        try:
            expected_revision = int(expected_revision)
        except (ValueError, TypeError):
            return _resposta_com_headers({'erro': 'expected_revision deve ser um número inteiro.'}, 400)

        # Validação do campo status se fornecido
        status_input = payload.get('status')
        status = None
        if status_input is not None:
            try:
                status = int(status_input)
            except (ValueError, TypeError):
                return _resposta_com_headers({'erro': 'O status deve ser um número inteiro.'}, 400)

        # Validação de lancamento_em
        lancamento_em_input = payload.get('lancamento_em')
        validar_lancamento = False
        if lancamento_em_input:
            if not isinstance(lancamento_em_input, str):
                return _resposta_com_headers({'erro': 'Data de lançamento deve ser um texto no formato YYYY-MM-DD.'}, 400)
            lancamento_em_input = lancamento_em_input.strip()
            try:
                datetime.strptime(lancamento_em_input, '%Y-%m-%d')
            except ValueError:
                return _resposta_com_headers({'erro': 'Data de lançamento inválida. Utilize uma data real no formato YYYY-MM-DD.'}, 400)
            validar_lancamento = True

        try:
            obter_chave_aead()
        except CryptoKeyError:
            return _resposta_com_headers({'erro': 'Serviço de criptografia temporariamente indisponível.'}, 503)

        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                conn.rollback()
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            # Validação de concorrência otimista
            if rotina['revision'] != expected_revision:
                conn.rollback()
                return _resposta_com_headers({
                    'erro': 'Registro modificado por outro operador. Recarregue os dados e tente novamente.'
                }, 409)

            nome_rotina = payload.get('nome_rotina', decifrar_dado(rotina['nome_rotina_cifrado'], f"rotina:{id}:nome"))
            servidor = payload.get('servidor', decifrar_dado(rotina['servidor_cifrado'], f"rotina:{id}:servidor"))
            porta = payload.get('porta', decifrar_dado(rotina['porta_cifrada'], f"rotina:{id}:porta"))
            usuario = payload.get('usuario', decifrar_dado(rotina['usuario_cifrado'], f"rotina:{id}:usuario"))
            responsavel = payload.get('responsavel', decifrar_dado(rotina['responsavel_cifrado'], f"rotina:{id}:responsavel"))
            notas = payload.get('notas', decifrar_dado(rotina['notas_cifradas'], f"rotina:{id}:notas"))

            if status is None:
                status = rotina['status']

            if validar_lancamento:
                novo_lancamento = lancamento_em_input
            elif lancamento_em_input is None and 'lancamento_em' in payload:
                novo_lancamento = None
            else:
                novo_lancamento = decifrar_dado(rotina['lancamento_em_cifrado'], f"rotina:{id}:lancamento") if rotina['lancamento_em_cifrado'] else None

            # Preserva espaços EXATOS da senha (sem .strip())
            remover_senha = bool(payload.get('remover_senha', False))
            senha_input = payload.get('senha')

            if remover_senha:
                nova_senha_cifrada = None
            elif senha_input is not None and senha_input != "":
                nova_senha_cifrada = cifrar_dado(str(senha_input), f"rotina:{id}:senha")
            else:
                nova_senha_cifrada = rotina['senha_cifrada']

            # Gestão de dados_extras com preservação de chaves existentes, toggle de sensibilidade e remoção explícita
            try:
                extras_str = decifrar_dado(rotina['dados_extras_json_cifrado'], f"rotina:{id}:extras")
                extras_atuais = _deserializar_extras_internos(extras_str)
            except Exception:
                conn.rollback()
                return _resposta_com_headers({'erro': 'Falha de integridade ao decifrar ou deserializar dados extras.'}, 503)

            dados_extras_payload = payload.get('dados_extras')
            if dados_extras_payload is not None:
                if isinstance(dados_extras_payload, str):
                    try:
                        dados_extras_payload = json.loads(dados_extras_payload)
                    except Exception:
                        dados_extras_payload = {}
                if not isinstance(dados_extras_payload, dict):
                    dados_extras_payload = {}
            else:
                dados_extras_payload = None

            extras_sensiveis_payload = payload.get('extras_sensiveis')
            if extras_sensiveis_payload is not None:
                if isinstance(extras_sensiveis_payload, str):
                    try:
                        extras_sensiveis_payload = json.loads(extras_sensiveis_payload)
                    except Exception:
                        conn.rollback()
                        return _resposta_com_headers({'erro': 'extras_sensiveis deve ser um objeto válido.'}, 400)
                if not isinstance(extras_sensiveis_payload, dict):
                    conn.rollback()
                    return _resposta_com_headers({'erro': 'extras_sensiveis deve ser um objeto dicionário.'}, 400)

                for k_sens, v_sens in extras_sensiveis_payload.items():
                    if v_sens is not None and type(v_sens) is not bool:
                        conn.rollback()
                        return _resposta_com_headers({'erro': f'O valor de sensibilidade para a chave "{k_sens}" deve ser um booleano estrito (true/false) ou null.'}, 400)
            else:
                extras_sensiveis_payload = None

            mudou_sensibilidade = False

            # Reunir todas as chaves mencionadas nos payloads de extras
            todas_chaves_payload = set()
            if dados_extras_payload is not None:
                todas_chaves_payload.update(dados_extras_payload.keys())
            if extras_sensiveis_payload is not None:
                todas_chaves_payload.update(extras_sensiveis_payload.keys())

            for k in todas_chaves_payload:
                k_str = str(k)
                tem_valor_input = (dados_extras_payload is not None and k in dados_extras_payload)
                valor_input = dados_extras_payload[k] if tem_valor_input else None

                tem_sensivel_input = (extras_sensiveis_payload is not None and k in extras_sensiveis_payload)
                sensivel_input = extras_sensiveis_payload[k] if tem_sensivel_input else None

                if k_str in extras_atuais:
                    antigo_v = extras_atuais[k_str]['v']
                    antigo_s = extras_atuais[k_str]['s']

                    # Regra de sensibilidade: null mantém o booleano anterior
                    if tem_sensivel_input and sensivel_input is not None:
                        novo_s = sensivel_input
                    else:
                        novo_s = antigo_s

                    # Regra de preservação e edição para vazio:
                    # null preserva sempre;
                    # vazio ("") preserva para confidencial carregado mascarado (antigo_s == True);
                    # nao-confidencial (antigo_s == False) aceita string vazia como edicao direta
                    if tem_valor_input:
                        if valor_input is None:
                            novo_v = antigo_v
                        elif valor_input == "":
                            if antigo_s:
                                novo_v = antigo_v
                            else:
                                novo_v = ""
                        else:
                            novo_v = str(valor_input)
                    else:
                        novo_v = antigo_v

                    if novo_s != antigo_s:
                        mudou_sensibilidade = True

                    extras_atuais[k_str] = {'v': novo_v, 's': novo_s}
                else:
                    # Nova chave adicionada na atualização
                    novo_v = str(valor_input) if (tem_valor_input and valor_input is not None) else ""
                    novo_s = sensivel_input if (tem_sensivel_input and sensivel_input is not None) else False
                    extras_atuais[k_str] = {'v': novo_v, 's': novo_s}

            remover_extras = payload.get('remover_extras') or []
            if isinstance(remover_extras, list):
                for k_rem in remover_extras:
                    extras_atuais.pop(str(k_rem), None)

            nova_revision = rotina['revision'] + 1
            agora_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            operador = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            cursor.execute("""
                UPDATE backups_ftp_rotinas SET
                    nome_rotina_cifrado = ?,
                    servidor_cifrado = ?,
                    porta_cifrada = ?,
                    usuario_cifrado = ?,
                    senha_cifrada = ?,
                    responsavel_cifrado = ?,
                    lancamento_em_cifrado = ?,
                    notas_cifradas = ?,
                    dados_extras_json_cifrado = ?,
                    status = ?,
                    revision = ?,
                    atualizado_em = ?,
                    atualizado_por = ?
                WHERE id = ?
            """, (
                cifrar_dado(nome_rotina, f"rotina:{id}:nome"),
                cifrar_dado(servidor, f"rotina:{id}:servidor"),
                cifrar_dado(porta, f"rotina:{id}:porta"),
                cifrar_dado(usuario, f"rotina:{id}:usuario"),
                nova_senha_cifrada,
                cifrar_dado(responsavel, f"rotina:{id}:responsavel"),
                cifrar_dado(novo_lancamento, f"rotina:{id}:lancamento") if novo_lancamento else None,
                cifrar_dado(notas, f"rotina:{id}:notas"),
                cifrar_dado(json.dumps(extras_atuais), f"rotina:{id}:extras"),
                status,
                nova_revision,
                agora_iso,
                operador,
                id
            ))

            if mudou_sensibilidade:
                _gravar_auditoria_na_transacao(
                    cursor,
                    usuario_id=operador,
                    cliente_alias=rotina['cliente_alias'],
                    rotina_id=id,
                    versao_id=None,
                    campo_ou_segredo='campo_extra',
                    acao='alterar_sensibilidade_extra',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )

            _gravar_auditoria_na_transacao(
                cursor,
                usuario_id=operador,
                cliente_alias=rotina['cliente_alias'],
                rotina_id=id,
                versao_id=None,
                campo_ou_segredo='rotina',
                acao='atualizar_rotina',
                ip_origem=ip_origem,
                resultado='sucesso'
            )
            conn.commit()

            dados_extras_resp, extras_sensiveis_resp, extras_configurados_resp = _formatar_extras_para_resposta(extras_atuais)
            return _resposta_com_headers({
                'sucesso': True,
                'mensagem': 'Rotina atualizada com sucesso.',
                'rotina': {
                    'id': id,
                    'cliente_alias': rotina['cliente_alias'],
                    'nome_rotina': nome_rotina,
                    'servidor': servidor,
                    'porta': porta,
                    'usuario': usuario,
                    'senha_configurada': bool(nova_senha_cifrada),
                    'responsavel': responsavel,
                    'lancamento_em': novo_lancamento,
                    'notas': notas,
                    'dados_extras': dados_extras_resp,
                    'extras_sensiveis': extras_sensiveis_resp,
                    'extras_configurados': extras_configurados_resp,
                    'status': status,
                    'revision': nova_revision
                }
            }, 200)
        except Exception:
            conn.rollback()
            return _resposta_com_headers({'erro': 'Falha interna ao processar operação.'}, 500)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # DELETE: Excluir rotina
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>', methods=['DELETE'])
    @_requer_master
    def excluir_rotina(id):
        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            rotina = cursor.execute("SELECT cliente_alias FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                conn.rollback()
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            operador = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            cursor.execute("DELETE FROM backups_ftp_listas_versoes WHERE rotina_id = ?", (id,))
            cursor.execute("DELETE FROM backups_ftp_rotinas WHERE id = ?", (id,))

            _gravar_auditoria_na_transacao(
                cursor,
                usuario_id=operador,
                cliente_alias=rotina['cliente_alias'],
                rotina_id=id,
                versao_id=None,
                campo_ou_segredo='rotina',
                acao='excluir_rotina',
                ip_origem=ip_origem,
                resultado='sucesso'
            )
            conn.commit()
            return _resposta_com_headers({'sucesso': True, 'mensagem': 'Rotina excluída com sucesso.'}, 200)
        except Exception:
            conn.rollback()
            return _resposta_com_headers({'erro': 'Falha interna ao processar operação.'}, 500)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Revelar senha (Auditado Fail-Closed)
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/revelar-senha', methods=['POST'])
    @_requer_master
    def revelar_senha(id):
        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            if not rotina['senha_cifrada']:
                return _resposta_com_headers({'sucesso': True, 'senha': ''}, 200)

            usuario_id = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            try:
                _gravar_auditoria_commit(
                    conn,
                    usuario_id=str(usuario_id),
                    cliente_alias=rotina['cliente_alias'],
                    rotina_id=id,
                    versao_id=None,
                    campo_ou_segredo='senha',
                    acao='revelar_senha',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )
            except Exception:
                return _resposta_com_headers({'erro': 'Falha ao registrar auditoria de segurança.'}, 500)

            try:
                senha_plana = decifrar_dado(rotina['senha_cifrada'], f"rotina:{id}:senha")
            except Exception:
                return _resposta_com_headers({'erro': 'Erro ao descriptografar senha.'}, 503)

            return _resposta_com_headers({'sucesso': True, 'senha': senha_plana}, 200)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Copiar senha (Auditado Fail-Closed)
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/copiar-senha', methods=['POST'])
    @_requer_master
    def copiar_senha(id):
        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            if not rotina['senha_cifrada']:
                return _resposta_com_headers({'sucesso': True, 'senha': '', 'mensagem': 'Nenhuma senha cadastrada.'}, 200)

            usuario_id = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            try:
                _gravar_auditoria_commit(
                    conn,
                    usuario_id=str(usuario_id),
                    cliente_alias=rotina['cliente_alias'],
                    rotina_id=id,
                    versao_id=None,
                    campo_ou_segredo='senha',
                    acao='copiar_senha',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )
            except Exception:
                return _resposta_com_headers({'erro': 'Falha ao registrar auditoria de segurança.'}, 500)

            try:
                senha_plana = decifrar_dado(rotina['senha_cifrada'], f"rotina:{id}:senha")
            except Exception:
                return _resposta_com_headers({'erro': 'Erro ao descriptografar senha.'}, 503)

            return _resposta_com_headers({
                'sucesso': True,
                'senha': senha_plana,
                'mensagem': 'Ação de cópia auditada com sucesso.'
            }, 200)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Revelar valor de campo extra (Auditado Fail-Closed)
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/revelar-extra', methods=['POST'])
    @_requer_master
    def revelar_extra(id):
        payload = request.get_json(silent=True) or request.form
        if not isinstance(payload, dict):
            return _resposta_com_headers({'erro': 'Payload deve ser um objeto JSON.'}, 400)

        chave = (payload.get('chave') or '').strip()
        if not chave:
            return _resposta_com_headers({'erro': 'O nome da chave extra é obrigatório.'}, 400)

        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            # Auditoria fail-closed: registra 'campo_extra' de forma genérica sem vazar a chave
            usuario_id = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()
            try:
                _gravar_auditoria_commit(
                    conn,
                    usuario_id=str(usuario_id),
                    cliente_alias=rotina['cliente_alias'],
                    rotina_id=id,
                    versao_id=None,
                    campo_ou_segredo='campo_extra',
                    acao='revelar_extra',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )
            except Exception:
                return _resposta_com_headers({'erro': 'Falha ao registrar auditoria de segurança.'}, 500)

            try:
                extras_str = decifrar_dado(rotina['dados_extras_json_cifrado'], f"rotina:{id}:extras")
                extras_dict = _deserializar_extras_internos(extras_str)
            except Exception:
                return _resposta_com_headers({'erro': 'Erro ao descriptografar dados extras.'}, 503)

            if chave not in extras_dict:
                return _resposta_com_headers({'erro': 'Chave extra não encontrada nesta rotina.'}, 404)

            return _resposta_com_headers({
                'sucesso': True,
                'chave': chave,
                'valor': extras_dict[chave]['v']
            }, 200)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Copiar valor de campo extra (Auditado Fail-Closed)
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/copiar-extra', methods=['POST'])
    @_requer_master
    def copiar_extra(id):
        payload = request.get_json(silent=True) or request.form
        if not isinstance(payload, dict):
            return _resposta_com_headers({'erro': 'Payload deve ser um objeto JSON.'}, 400)

        chave = (payload.get('chave') or '').strip()
        if not chave:
            return _resposta_com_headers({'erro': 'O nome da chave extra é obrigatório.'}, 400)

        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            usuario_id = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()
            try:
                _gravar_auditoria_commit(
                    conn,
                    usuario_id=str(usuario_id),
                    cliente_alias=rotina['cliente_alias'],
                    rotina_id=id,
                    versao_id=None,
                    campo_ou_segredo='campo_extra',
                    acao='copiar_extra',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )
            except Exception:
                return _resposta_com_headers({'erro': 'Falha ao registrar auditoria de segurança.'}, 500)

            try:
                extras_str = decifrar_dado(rotina['dados_extras_json_cifrado'], f"rotina:{id}:extras")
                extras_dict = _deserializar_extras_internos(extras_str)
            except Exception:
                return _resposta_com_headers({'erro': 'Erro ao descriptografar dados extras.'}, 503)

            if chave not in extras_dict:
                return _resposta_com_headers({'erro': 'Chave extra não encontrada nesta rotina.'}, 404)

            return _resposta_com_headers({
                'sucesso': True,
                'chave': chave,
                'valor': extras_dict[chave]['v'],
                'mensagem': 'Ação de cópia auditada com sucesso.'
            }, 200)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # GET: Listar versões de listas Cobian
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/listas', methods=['GET'])
    @_requer_master
    def listar_listas_rotina(id):
        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            versoes_raw = cursor.execute("""
                SELECT id, rotina_id, versao_numero, tamanho_bytes, encoding_detectado, checksum_sha256,
                       enviado_por, enviado_em
                FROM backups_ftp_listas_versoes
                WHERE rotina_id = ?
                ORDER BY versao_numero ASC
            """, (id,)).fetchall()

            return _resposta_com_headers({
                'sucesso': True,
                'rotina_id': id,
                'versoes': [dict(v) for v in versoes_raw]
            }, 200)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Upload e versionamento de lista Cobian
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/upload-list', methods=['POST'])
    @_requer_master
    def upload_lista_rotina(id):
        if 'arquivo' not in request.files:
            return _resposta_com_headers({'erro': 'Nenhum arquivo enviado.'}, 400)

        f = request.files['arquivo']
        if not f or not f.filename:
            return _resposta_com_headers({'erro': 'Nome de arquivo inválido.'}, 400)

        # Rejeição imediata se houver quebras de linha ou caracteres de controle no nome
        if re.search(r'[\r\n\x00-\x1f]', f.filename):
            return _resposta_com_headers({'erro': 'Nome de arquivo contém caracteres de controle inválidos.'}, 400)

        nome_seguro = secure_filename(f.filename)

        # Leitura limitada a exatamente LIMITE_LISTA_BYTES + 1 para proteção contra DoS
        file_bytes = f.read(LIMITE_LISTA_BYTES + 1)
        if len(file_bytes) > LIMITE_LISTA_BYTES:
            return _resposta_com_headers({'erro': 'Arquivo excede o limite máximo permitido de 2 MiB.'}, 413)

        comentario = request.form.get('comentario', '')

        try:
            texto_decodificado, encoding, checksum_sha256 = _validar_e_processar_lista(file_bytes, nome_seguro)
        except PayloadTooLargeError as e:
            return _resposta_com_headers({'erro': str(e)}, 413)
        except FormatoInvalidoError as e:
            return _resposta_com_headers({'erro': str(e)}, 400)

        try:
            obter_chave_aead()
        except CryptoKeyError:
            return _resposta_com_headers({'erro': 'Serviço de criptografia temporariamente indisponível.'}, 503)

        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                conn.rollback()
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            max_v = cursor.execute("""
                SELECT MAX(versao_numero) FROM backups_ftp_listas_versoes WHERE rotina_id = ?
            """, (id,)).fetchone()[0]
            proxima_versao = (max_v or 0) + 1

            nome_cifrado = cifrar_dado(nome_seguro, f"list:{id}:{proxima_versao}:nome")
            comentario_cifrado = cifrar_dado(comentario, f"list:{id}:{proxima_versao}:comentario")
            conteudo_cifrado = cifrar_dado(texto_decodificado, f"list:{id}:{proxima_versao}:conteudo")

            agora_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            operador = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            cursor.execute("""
                INSERT INTO backups_ftp_listas_versoes (
                    rotina_id, versao_numero, tamanho_bytes, encoding_detectado, checksum_sha256,
                    nome_original_cifrado, comentario_cifrado, conteudo_cifrado,
                    enviado_por, enviado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (id, proxima_versao, len(file_bytes), encoding, checksum_sha256,
                  nome_cifrado, comentario_cifrado, conteudo_cifrado, operador, agora_iso))
            nova_versao_id = cursor.lastrowid

            # Política de retenção atômica: mantém no máximo MAX_VERSOES_RETENCAO mais recentes
            versoes_existentes = cursor.execute("""
                SELECT id FROM backups_ftp_listas_versoes
                WHERE rotina_id = ?
                ORDER BY versao_numero DESC
            """, (id,)).fetchall()

            if len(versoes_existentes) > MAX_VERSOES_RETENCAO:
                ids_para_excluir = [v['id'] for v in versoes_existentes[MAX_VERSOES_RETENCAO:]]
                placeholders = ','.join('?' for _ in ids_para_excluir)
                cursor.execute(f"DELETE FROM backups_ftp_listas_versoes WHERE id IN ({placeholders})", ids_para_excluir)

            _gravar_auditoria_na_transacao(
                cursor,
                usuario_id=operador,
                cliente_alias=rotina['cliente_alias'],
                rotina_id=id,
                versao_id=nova_versao_id,
                campo_ou_segredo='lista_cobian',
                acao='upload_lista',
                ip_origem=ip_origem,
                resultado='sucesso'
            )
            conn.commit()

            return _resposta_com_headers({
                'sucesso': True,
                'mensagem': 'Lista Cobian enviada e versionada com sucesso.',
                'versao': {
                    'id': nova_versao_id,
                    'versao_numero': proxima_versao,
                    'tamanho_bytes': len(file_bytes),
                    'encoding_detectado': encoding,
                    'checksum_sha256': checksum_sha256,
                    'enviado_em': agora_iso,
                    'enviado_por': operador
                }
            }, 201)
        except Exception:
            conn.rollback()
            return _resposta_com_headers({'erro': 'Falha interna ao processar operação.'}, 500)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # POST: Visualizar conteúdo de versão Cobian (Auditado Fail-Closed)
    # -----------------------------------------------------------------------
    @bp.route('/admin/backups-ftp/rotina/<int:id>/lista/<int:versao_id>/ver-conteudo', methods=['POST'])
    @_requer_master
    def ver_conteudo_lista(id, versao_id):
        conn = conectar()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            rotina = cursor.execute("SELECT * FROM backups_ftp_rotinas WHERE id = ?", (id,)).fetchone()
            if not rotina:
                return _resposta_com_headers({'erro': 'Rotina não encontrada.'}, 404)

            versao = cursor.execute("""
                SELECT * FROM backups_ftp_listas_versoes WHERE id = ? AND rotina_id = ?
            """, (versao_id, id)).fetchone()
            if not versao:
                return _resposta_com_headers({'erro': 'Versão de lista não encontrada.'}, 404)

            usuario_id = _obter_identificador_usuario()
            ip_origem = _obter_ip_cliente()

            try:
                _gravar_auditoria_commit(
                    conn,
                    usuario_id=str(usuario_id),
                    cliente_alias=rotina['cliente_alias'],
                    rotina_id=id,
                    versao_id=versao_id,
                    campo_ou_segredo='conteudo_lista',
                    acao='inspecionar_list',
                    ip_origem=ip_origem,
                    resultado='sucesso'
                )
            except Exception:
                return _resposta_com_headers({'erro': 'Falha ao registrar auditoria de segurança.'}, 500)

            try:
                conteudo = decifrar_dado(versao['conteudo_cifrado'], f"list:{id}:{versao['versao_numero']}:conteudo")
            except Exception:
                return _resposta_com_headers({'erro': 'Erro ao descriptografar conteúdo da lista.'}, 503)

            return _resposta_com_headers({
                'sucesso': True,
                'versao_numero': versao['versao_numero'],
                'conteudo': conteudo
            }, 200)
        finally:
            conn.close()
