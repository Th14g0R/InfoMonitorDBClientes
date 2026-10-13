import ipaddress
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
FUSO_LOCAL = ZoneInfo('America/Fortaleza')
PORTA_COBIAN_PADRAO = 16032
CACHE_TTL_PADRAO = 45  # TTL em segundos (entre 30 e 60s)
CAMINHOS_LOGS_COBIAN = [
    r'C:\Program Files (x86)\Cobian Backup 11\Logs',
    r'C:\Program Files\Cobian Backup 11\Logs',
]

# Cache em memória simples para conexões ativas do Cobian Remote Manager
_CACHE_LOCK = threading.Lock()
_CACHE_CONEXOES = {
    'clientes_remotos': [],
    'ips_ativos_porta': set(),
    'janela_encontrada': False,
    'timestamp': 0.0,
    'ttl_segundos': CACHE_TTL_PADRAO
}


def limpar_cache_conexoes_cobian():
    """Limpa o cache em memória de conexões Cobian forçando nova checagem na próxima requisição."""
    with _CACHE_LOCK:
        _CACHE_CONEXOES['clientes_remotos'] = []
        _CACHE_CONEXOES['ips_ativos_porta'] = set()
        _CACHE_CONEXOES['janela_encontrada'] = False
        _CACHE_CONEXOES['timestamp'] = 0.0


def normalizar_nome(texto):
    """Normaliza texto para comparação removendo espaços e caracteres especiais."""
    if not texto:
        return ''
    return re.sub(r'[^a-zA-Z0-9]', '', str(texto).lower())


def extrair_clientes_janela_cobian(timeout_busca=1.0):
    """
    Localiza a janela do Cobian Backup 11 - Gerenciador remoto (cbRemoteManager.exe)
    e extrai os itens da ListView da aba "Clientes" (Cliente, Endereço IP, Tempo de conexão, ID).
    Utiliza UIAutomation (uiautomation) com fallback nativo.
    Retorna uma lista de dicionários: [{'cliente': ..., 'ip': ..., 'tempo_conexao': ..., 'id_cliente': ...}]
    """
    registros = []
    try:
        import uiautomation as auto  # type: ignore

        # Desabilitar criação de arquivo @AutomationLog.txt
        auto.Logger.SetLogFile('')

        # Definir timeout rápido para não prender a resposta web
        auto.SetGlobalSearchTimeout(timeout_busca)

        # 1. Localizar janela principal por classe ou título
        window = auto.WindowControl(searchDepth=1, ClassName='TformCb11RemoteMain')
        if not window.Exists(0, 0):
            window = auto.WindowControl(searchDepth=1, RegexName=r'.*Cobian.*Gerenciador remoto.*')
            if not window.Exists(0, 0):
                return []

        # 2. Localizar o controle TListView dentro da aba "Clientes"
        list_ctrl = window.ListControl(searchDepth=3)
        if not list_ctrl.Exists(0, 0):
            list_ctrl = window.Control(searchDepth=4, ClassName='TListView')
            if not list_ctrl.Exists(0, 0):
                return []

        # 3. Iterar sobre os itens da ListView
        children = list_ctrl.GetChildren()
        for item in children:
            if item.ControlTypeName in ('HeaderControl', 'ScrollBarControl'):
                continue

            nome_cliente = (item.Name or '').strip()
            ip_cliente = ''
            tempo_conexao = ''
            id_cliente = ''

            # Buscar subitens/células filhas da linha
            sub_itens = [c for c in item.GetChildren() if c.ControlTypeName not in ('HeaderControl', 'ScrollBarControl')]
            textos_sub = [c.Name.strip() for c in sub_itens if c.Name]

            # Analisar os textos extraídos dos subitens
            for t in textos_sub:
                if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', t):
                    ip_cliente = t
                elif re.match(r'^\d{1,2}:\d{2}(?::\d{2})?$', t):
                    tempo_conexao = t
                elif not id_cliente and t != nome_cliente and not ip_cliente:
                    id_cliente = t

            # Fallback para LegacyIAccessiblePattern (caso os subitens venham agregados)
            if not ip_cliente:
                try:
                    legacy = item.GetLegacyIAccessiblePattern()
                    if legacy:
                        val = str(legacy.Value or legacy.Description or '')
                        match_ip = re.search(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', val)
                        if match_ip:
                            ip_cliente = match_ip.group(0)
                        match_tempo = re.search(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', val)
                        if match_tempo:
                            tempo_conexao = match_tempo.group(0)
                except Exception:
                    pass

            # Caso os valores venham tabulados no nome do item
            if '\t' in nome_cliente:
                partes = [p.strip() for p in nome_cliente.split('\t') if p.strip()]
                if partes:
                    nome_cliente = partes[0]
                    for p in partes[1:]:
                        if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', p) and not ip_cliente:
                            ip_cliente = p
                        elif re.match(r'^\d{1,2}:\d{2}(?::\d{2})?$', p) and not tempo_conexao:
                            tempo_conexao = p
                        elif not id_cliente:
                            id_cliente = p

            if nome_cliente or ip_cliente:
                registros.append({
                    'cliente': nome_cliente,
                    'ip': ip_cliente,
                    'tempo_conexao': tempo_conexao,
                    'id_cliente': id_cliente
                })

    except Exception as e:
        LOGGER.debug("Não foi possível extrair dados da janela do Cobian Remote Manager: %s", e)

    return registros


def extrair_conexoes_logs_cobian(limite_arquivos=3):
    """
    Fallback: Varre os arquivos de log recentes do Cobian procurando
    eventos de conexão de clientes com seus respectivos endereços IP.
    """
    conexoes = []
    pastas_candidatas = list(CAMINHOS_LOGS_COBIAN)

    appdata = os.environ.get('APPDATA', '')
    if appdata:
        pastas_candidatas.append(os.path.join(appdata, 'Cobian Backup 11', 'Logs'))

    arquivos_log = []
    for pasta in pastas_candidatas:
        if os.path.isdir(pasta):
            try:
                for nome in os.listdir(pasta):
                    if nome.lower().startswith('log') and nome.lower().endswith('.txt'):
                        caminho_completo = os.path.join(pasta, nome)
                        arquivos_log.append((os.path.getmtime(caminho_completo), caminho_completo))
            except Exception:
                pass

    arquivos_log.sort(reverse=True, key=lambda x: x[0])

    for _, caminho in arquivos_log[:limite_arquivos]:
        try:
            with open(caminho, 'r', encoding='utf-16-le', errors='ignore') as f:
                linhas = f.readlines()
        except Exception:
            try:
                with open(caminho, 'r', encoding='utf-8', errors='ignore') as f:
                    linhas = f.readlines()
            except Exception:
                continue

        # Ler de trás para frente para pegar conexões mais recentes
        for linha in reversed(linhas[-300:]):
            texto = linha.strip()
            # Padrões comuns de log de conexões remotas:
            # Ex: 2026-10-13 09:30 Cliente casabonita-1887 conectado de 189.85.119.175
            # Ex: 2026-10-13 09:30 Conexão aceita: 189.85.119.175 (Cliente: casabonita-1887)
            match_ip = re.search(r'\b(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\b', texto)
            if match_ip:
                ip = match_ip.group('ip')
                # Tentar extrair nome do cliente associado na mesma linha
                match_cliente = re.search(r'(?:cliente|client|de|from)\s*[:=]?\s*["\']?(?P<cli>[\w\.\-]+)', texto, re.IGNORECASE)
                cliente = match_cliente.group('cli') if match_cliente else ''
                if ip and ip not in ('127.0.0.1', '0.0.0.0'):
                    conexoes.append({
                        'cliente': cliente,
                        'ip': ip,
                        'tempo_conexao': '',
                        'id_cliente': ''
                    })
    return conexoes


def listar_ips_conectados_cobian(porta=PORTA_COBIAN_PADRAO, timeout=1.5):
    """
    Lista os endereços IP remotos com conexões ESTABLISHED na porta TCP 16032.
    Utiliza psutil ou netstat nativo do Windows.
    """
    ips = set()
    # 1. psutil se disponível
    try:
        import psutil  # type: ignore
        for conn in psutil.net_connections(kind='inet'):
            if (conn.status == 'ESTABLISHED'
                    and conn.laddr
                    and getattr(conn.laddr, 'port', None) == porta
                    and conn.raddr
                    and getattr(conn.raddr, 'ip', None)):
                ip_remoto = conn.raddr.ip.strip('[]').strip()
                if ip_remoto and ip_remoto not in ('127.0.0.1', '0.0.0.0', '::1'):
                    ips.add(ip_remoto)
        if ips:
            return ips
    except Exception:
        pass

    # 2. netstat do Windows
    try:
        res = subprocess.run(
            ['netstat', '-ano', '-p', 'tcp'],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
        if res.returncode == 0 and res.stdout:
            padrao = re.compile(
                rf'^\s*TCP\s+\S+:({porta})\s+([0-9a-fA-F\.\:\[\]]+):(\d+)\s+ESTABLISHED',
                re.IGNORECASE | re.MULTILINE
            )
            for m in padrao.finditer(res.stdout):
                ip_bruto = m.group(2).strip('[]').strip()
                if ip_bruto and ip_bruto not in ('0.0.0.0', '127.0.0.1', '::1', '*'):
                    try:
                        ip_obj = ipaddress.ip_address(ip_bruto)
                        if not ip_obj.is_loopback:
                            ips.add(str(ip_obj))
                    except ValueError:
                        if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', ip_bruto):
                            ips.add(ip_bruto)
    except Exception:
        pass

    return ips


def obter_clientes_remotos_cobian(forcar=False):
    """
    Retorna os clientes extraídos da janela do cbRemoteManager.exe com cache em memória (TTL 45s).
    Não bloqueia requisições se a janela estiver fechada ou minimizada.
    """
    agora = time.time()
    with _CACHE_LOCK:
        if not forcar and _CACHE_CONEXOES['timestamp'] > 0:
            if (agora - _CACHE_CONEXOES['timestamp']) < _CACHE_CONEXOES['ttl_segundos']:
                return (
                    list(_CACHE_CONEXOES['clientes_remotos']),
                    set(_CACHE_CONEXOES['ips_ativos_porta']),
                    bool(_CACHE_CONEXOES['janela_encontrada'])
                )

    # 1. Tentar extrair diretamente da janela do cbRemoteManager
    clientes_janela = extrair_clientes_janela_cobian(timeout_busca=1.0)
    janela_encontrada = bool(clientes_janela)

    # 2. Listar IPs conectados na porta TCP 16032
    ips_porta = listar_ips_conectados_cobian(porta=PORTA_COBIAN_PADRAO)

    # 3. Fallback para logs caso a janela não retorne itens
    if not clientes_janela:
        clientes_logs = extrair_conexoes_logs_cobian()
        if clientes_logs:
            clientes_janela = clientes_logs

    with _CACHE_LOCK:
        _CACHE_CONEXOES['clientes_remotos'] = list(clientes_janela)
        _CACHE_CONEXOES['ips_ativos_porta'] = set(ips_porta)
        _CACHE_CONEXOES['janela_encontrada'] = janela_encontrada
        _CACHE_CONEXOES['timestamp'] = time.time()

    return clientes_janela, ips_porta, janela_encontrada


def init_db_cobian(conn):
    """Inicializa a tabela de histórico de clientes Cobian e seus respectivos IPs."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cobian_clientes_ips (
            alias TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            tempo_conexao TEXT,
            ultimo_visto TIMESTAMP,
            origem TEXT DEFAULT 'cbRemoteManager',
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_cobian_ips_ip ON cobian_clientes_ips(ip)
    ''')
    conn.commit()


def obter_historico_ips_clientes(conn):
    """Retorna o histórico dos últimos IPs vistos de cada cliente no banco de dados."""
    cursor = conn.execute('''
        SELECT alias, ip, tempo_conexao, ultimo_visto, origem, atualizado_em
        FROM cobian_clientes_ips
    ''')
    historico = {}
    for linha in cursor.fetchall():
        alias_chave = linha['alias'].strip().lower()
        historico[alias_chave] = {
            'alias': linha['alias'],
            'ip': linha['ip'].strip(),
            'tempo_conexao': linha['tempo_conexao'],
            'ultimo_visto': linha['ultimo_visto'],
            'origem': linha['origem'],
            'atualizado_em': linha['atualizado_em']
        }
    return historico


def salvar_historico_conexoes(conn, clientes_ativos):
    """Salva ou atualiza os IPs ativos dos clientes detectados online no SQLite."""
    agora_str = datetime.now(FUSO_LOCAL).strftime('%Y-%m-%d %H:%M:%S')
    for item in clientes_ativos:
        alias = str(item.get('alias') or '').strip()
        ip = str(item.get('ip') or '').strip()
        tempo = item.get('tempo_conexao')
        if not alias or not ip:
            continue
        try:
            conn.execute('''
                INSERT INTO cobian_clientes_ips (alias, ip, tempo_conexao, ultimo_visto, origem, atualizado_em)
                VALUES (?, ?, ?, ?, 'cbRemoteManager', ?)
                ON CONFLICT(alias) DO UPDATE SET
                    ip = excluded.ip,
                    tempo_conexao = COALESCE(excluded.tempo_conexao, cobian_clientes_ips.tempo_conexao),
                    ultimo_visto = excluded.ultimo_visto,
                    origem = 'cbRemoteManager',
                    atualizado_em = excluded.atualizado_em
            ''', (alias, ip, tempo, agora_str, agora_str))
        except Exception as e:
            LOGGER.debug("Erro ao registrar histórico de IP para %s: %s", alias, e)
    conn.commit()


def corresponder_cliente(alias_card, remotos_disponiveis):
    """
    Cruza o identificador de um card (ex: 'casabonita-1887', 'costela-2259')
    com a lista de clientes remotos extraída do cbRemoteManager.
    Retorna o registro remoto correspondente ou None.
    """
    if not alias_card:
        return None

    alias_limpo = str(alias_card).strip().lower()
    alias_norm = normalizar_nome(alias_limpo)

    # Extrair possível ID numérico do alias (ex: '1887' de 'casabonita-1887')
    match_id = re.search(r'(\d+)$', alias_limpo)
    id_card = match_id.group(1) if match_id else ''

    # 1. Correspondência exata de alias / nome do cliente
    for rem in remotos_disponiveis:
        nome_rem = (rem.get('cliente') or '').strip().lower()
        if nome_rem and nome_rem == alias_limpo:
            return rem

    # 2. Correspondência normalizada (ignorando traços e pontuação)
    for rem in remotos_disponiveis:
        nome_norm = normalizar_nome(rem.get('cliente') or '')
        if nome_norm and (nome_norm == alias_norm or alias_norm in nome_norm or nome_norm in alias_norm):
            return rem

    # 3. Correspondência por ID do cliente se disponível
    if id_card:
        for rem in remotos_disponiveis:
            id_rem = str(rem.get('id_cliente') or '').strip()
            nome_rem = (rem.get('cliente') or '').strip().lower()
            if id_rem and id_rem == id_card:
                return rem
            if nome_rem.endswith(f'-{id_card}') or nome_rem.endswith(id_card):
                return rem

    return None


def obter_status_conexoes_cobian(clientes, conn=None, forcar=False):
    """
    Enriquece a lista de clientes de backups FTP com o status de conexão em tempo real:
    - Se constar na lista da janela do cbRemoteManager: '🟢 Online' e o respectivo IP.
    - Se não constar: '⚪ Offline' e o último IP visto registrado no histórico (ou '-').
    """
    remotos_janela, ips_ativos_porta, janela_ativa = obter_clientes_remotos_cobian(forcar=forcar)

    historico = {}
    if conn is not None:
        historico = obter_historico_ips_clientes(conn)

    clientes_ativos_para_salvar = []
    ips_online_utilizados = set()

    for c in clientes:
        alias_card = c.get('alias') or ''
        remoto = corresponder_cliente(alias_card, remotos_janela)

        if remoto and remoto.get('ip'):
            # Cliente presente na lista ativa do Cobian Remote Manager
            c['online'] = True
            c['ip'] = remoto['ip']
            c['tempo_conexao'] = remoto.get('tempo_conexao')
            ips_online_utilizados.add(remoto['ip'])
            clientes_ativos_para_salvar.append({
                'alias': alias_card,
                'ip': remoto['ip'],
                'tempo_conexao': remoto.get('tempo_conexao')
            })
        else:
            # Não está ativo no momento: buscar último IP conhecido no histórico
            c['online'] = False
            hist = historico.get(alias_card.lower())
            if hist and hist.get('ip'):
                c['ip'] = hist['ip']
                c['tempo_conexao'] = hist.get('tempo_conexao')
                # Caso o IP histórico esteja com conexão TCP ativa na porta 16032
                if hist['ip'] in ips_ativos_porta:
                    c['online'] = True
                    ips_online_utilizados.add(hist['ip'])
            else:
                c['ip'] = None
                c['tempo_conexao'] = None

    # Atualizar histórico com os clientes online confirmados
    if conn is not None and clientes_ativos_para_salvar:
        salvar_historico_conexoes(conn, clientes_ativos_para_salvar)

    total_online = sum(1 for c in clientes if c.get('online'))
    total_offline = len(clientes) - total_online
    total_com_ip = sum(1 for c in clientes if c.get('ip'))
    ips_sem_vinculo = [ip for ip in ips_ativos_porta if ip not in ips_online_utilizados]

    return {
        'total_online': total_online,
        'total_offline': total_offline,
        'total_com_ip': total_com_ip,
        'janela_ativa': janela_ativa,
        'ips_sem_vinculo': sorted(ips_sem_vinculo),
        'timestamp_consulta': datetime.now(FUSO_LOCAL).strftime('%d/%m/%Y %H:%M:%S')
    }
