import copy
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

# Cache em memória simples para conexões ativas e mapeamentos
_CACHE_LOCK = threading.Lock()
_CACHE_CONEXOES = {
    'ips_ativos': set(),
    'timestamp': 0.0,
    'ttl_segundos': CACHE_TTL_PADRAO
}


def limpar_cache_conexoes_cobian():
    """Limpa o cache em memória de conexões Cobian forçando nova checagem na próxima chamada."""
    with _CACHE_LOCK:
        _CACHE_CONEXOES['ips_ativos'] = set()
        _CACHE_CONEXOES['timestamp'] = 0.0


def extrair_ips_netstat(texto_netstat, porta=PORTA_COBIAN_PADRAO):
    """
    Analisa a saída textual de 'netstat -ano -p tcp' e extrai os endereços IP remotos
    com conexões ESTABLISHED na porta local informada.
    Suporta IPv4 e IPv6.
    """
    ips = set()
    # Padrão flexível para linhas como:
    # TCP    0.0.0.0:16032          189.85.119.175:51234    ESTABLISHED    1234
    # TCP    192.168.243.2:16032    189.85.119.175:51234    ESTABLISHED    1234
    # TCP    [::]:16032             [2001:db8::1]:51234     ESTABLISHED    1234
    padrao = re.compile(
        rf'^\s*TCP\s+\S+:({porta})\s+([0-9a-fA-F\.\:\[\]]+):(\d+)\s+ESTABLISHED',
        re.IGNORECASE | re.MULTILINE
    )
    for match in padrao.finditer(texto_netstat):
        ip_bruto = match.group(2).strip('[]').strip()
        if not ip_bruto or ip_bruto in ('0.0.0.0', '127.0.0.1', '::1', '*'):
            continue
        try:
            ip_obj = ipaddress.ip_address(ip_bruto)
            if not ip_obj.is_loopback:
                ips.add(str(ip_obj))
        except ValueError:
            # Caso seja um formato válido de IP mesmo com pequenas variações
            if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', ip_bruto):
                ips.add(ip_bruto)
    return ips


def listar_ips_conectados_cobian(porta=PORTA_COBIAN_PADRAO, timeout=2.5):
    """
    Lista os endereços IP remotos atualmente conectados à porta do Cobian (padrão 16032).
    Utiliza preferencialmente netstat -ano -p tcp (extremamente rápido e nativo no Windows),
    com psutil e PowerShell como fallbacks robustos.
    """
    # 1. Tentar psutil se disponível
    try:
        import psutil  # type: ignore
        ips_psutil = set()
        for conn in psutil.net_connections(kind='inet'):
            if (conn.status == 'ESTABLISHED'
                    and conn.laddr
                    and getattr(conn.laddr, 'port', None) == porta
                    and conn.raddr
                    and getattr(conn.raddr, 'ip', None)):
                ip_remoto = conn.raddr.ip.strip('[]').strip()
                if ip_remoto and ip_remoto not in ('127.0.0.1', '0.0.0.0', '::1'):
                    ips_psutil.add(ip_remoto)
        return ips_psutil
    except Exception:
        pass

    # 2. Executar netstat nativo do Windows (resposta em milissegundos)
    try:
        resultado = subprocess.run(
            ['netstat', '-ano', '-p', 'tcp'],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
        if resultado.returncode == 0 and resultado.stdout:
            ips_netstat = extrair_ips_netstat(resultado.stdout, porta=porta)
            if ips_netstat:
                return ips_netstat
    except Exception as e:
        LOGGER.debug("Falha ao executar netstat: %s", e)

    # 3. Fallback via PowerShell Get-NetTCPConnection
    try:
        cmd_ps = (
            f"Get-NetTCPConnection -LocalPort {porta} -State Established -ErrorAction SilentlyContinue "
            f"| Select-Object -ExpandProperty RemoteAddress"
        )
        resultado_ps = subprocess.run(
            ['powershell', '-NoProfile', '-Command', cmd_ps],
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
            check=False
        )
        if resultado_ps.returncode == 0 and resultado_ps.stdout:
            ips_ps = set()
            for linha in resultado_ps.stdout.splitlines():
                linha = linha.strip().strip('[]')
                if linha and linha not in ('0.0.0.0', '127.0.0.1', '::1'):
                    try:
                        ip_obj = ipaddress.ip_address(linha)
                        if not ip_obj.is_loopback:
                            ips_ps.add(str(ip_obj))
                    except ValueError:
                        pass
            return ips_ps
    except Exception as e:
        LOGGER.debug("Falha ao executar Get-NetTCPConnection via PowerShell: %s", e)

    return set()


def obter_ips_conectados_com_cache(porta=PORTA_COBIAN_PADRAO, forcar=False):
    """
    Retorna os IPs conectados usando cache em memória com TTL de 30 a 60 segundos
    para evitar chamadas redundantes a comandos do sistema operacional.
    """
    agora = time.time()
    with _CACHE_LOCK:
        if not forcar and _CACHE_CONEXOES['timestamp'] > 0:
            if (agora - _CACHE_CONEXOES['timestamp']) < _CACHE_CONEXOES['ttl_segundos']:
                return set(_CACHE_CONEXOES['ips_ativos'])

    ips = listar_ips_conectados_cobian(porta=porta)

    with _CACHE_LOCK:
        _CACHE_CONEXOES['ips_ativos'] = set(ips)
        _CACHE_CONEXOES['timestamp'] = time.time()

    return ips


def init_db_cobian(conn):
    """Inicializa a tabela de mapeamento de clientes Cobian e seus respectivos IPs."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cobian_clientes_ips (
            alias TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            tempo_conexao TEXT,
            ultimo_visto TIMESTAMP,
            origem TEXT DEFAULT 'manual',
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_cobian_ips_ip ON cobian_clientes_ips(ip)
    ''')
    conn.commit()


def obter_mapeamento_clientes_ips(conn):
    """
    Retorna o dicionário de vínculos cadastrados:
    {alias_lower: {'alias': ..., 'ip': ..., 'tempo_conexao': ..., 'ultimo_visto': ...}}
    """
    cursor = conn.execute('''
        SELECT alias, ip, tempo_conexao, ultimo_visto, origem, atualizado_em
        FROM cobian_clientes_ips
    ''')
    mapeamento = {}
    for linha in cursor.fetchall():
        alias_chave = linha['alias'].strip().lower()
        mapeamento[alias_chave] = {
            'alias': linha['alias'],
            'ip': linha['ip'].strip(),
            'tempo_conexao': linha['tempo_conexao'],
            'ultimo_visto': linha['ultimo_visto'],
            'origem': linha['origem'],
            'atualizado_em': linha['atualizado_em']
        }

    # Também carrega arquivo estático cobian_clientes_ips.json se existir na raiz do projeto
    arquivo_config = os.path.join(os.path.dirname(__file__), 'cobian_clientes_ips.json')
    if os.path.isfile(arquivo_config):
        try:
            import json
            with open(arquivo_config, 'r', encoding='utf-8') as f:
                dados_arquivo = json.load(f)
            if isinstance(dados_arquivo, dict):
                for alias_arq, ip_arq in dados_arquivo.items():
                    k = str(alias_arq).strip().lower()
                    if k not in mapeamento and isinstance(ip_arq, str) and ip_arq.strip():
                        mapeamento[k] = {
                            'alias': str(alias_arq).strip(),
                            'ip': ip_arq.strip(),
                            'tempo_conexao': None,
                            'ultimo_visto': None,
                            'origem': 'arquivo_config',
                            'atualizado_em': None
                        }
        except Exception as e:
            LOGGER.warning("Falha ao ler cobian_clientes_ips.json: %s", e)

    return mapeamento


def salvar_vinculo_cliente_ip(conn, alias, ip, tempo_conexao=None, origem='manual'):
    """
    Salva ou atualiza o vínculo de um cliente com seu respectivo IP.
    Valida formato de IP antes da gravação.
    """
    alias_limpo = str(alias or '').strip()
    ip_limpo = str(ip or '').strip()
    if not alias_limpo:
        raise ValueError("Alias do cliente não pode ser vazio.")
    if not ip_limpo:
        raise ValueError("Endereço IP não pode ser vazio.")

    # Validar formato do IP
    try:
        ipaddress.ip_address(ip_limpo)
    except ValueError:
        raise ValueError(f"Endereço IP inválido: '{ip_limpo}'")

    agora_str = datetime.now(FUSO_LOCAL).strftime('%Y-%m-%d %H:%M:%S')

    conn.execute('''
        INSERT INTO cobian_clientes_ips (alias, ip, tempo_conexao, ultimo_visto, origem, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET
            ip = excluded.ip,
            tempo_conexao = COALESCE(excluded.tempo_conexao, cobian_clientes_ips.tempo_conexao),
            ultimo_visto = excluded.ultimo_visto,
            origem = excluded.origem,
            atualizado_em = excluded.atualizado_em
    ''', (alias_limpo, ip_limpo, tempo_conexao, agora_str, origem, agora_str))
    conn.commit()
    limpar_cache_conexoes_cobian()
    return True


def remover_vinculo_cliente_ip(conn, alias):
    """Remove o vínculo cadastrado de um cliente."""
    conn.execute('DELETE FROM cobian_clientes_ips WHERE LOWER(alias) = LOWER(?)', (str(alias).strip(),))
    conn.commit()
    limpar_cache_conexoes_cobian()
    return True


def obter_status_conexoes_cobian(clientes, conn=None, forcar=False, porta=PORTA_COBIAN_PADRAO):
    """
    Enriquece a lista de clientes com o status de conexão ("Online" / "Offline")
    e o "Endereço IP" de cada cliente Cobian Backup 11 diretamente nos dados dos cards.
    """
    ips_ativos = obter_ips_conectados_com_cache(porta=porta, forcar=forcar)

    mapeamento = {}
    if conn is not None:
        mapeamento = obter_mapeamento_clientes_ips(conn)

    # Conjunto para rastrear quais IPs ativos foram associados a clientes
    ips_associados = set()

    for c in clientes:
        alias_chave = (c.get('alias') or '').strip().lower()
        vinculo = mapeamento.get(alias_chave)

        if vinculo and vinculo.get('ip'):
            ip = vinculo['ip']
            c['ip'] = ip
            # Cliente está Online se o IP cadastrado estiver com conexão ESTABLISHED na porta 16032
            c['online'] = ip in ips_ativos
            c['tempo_conexao'] = vinculo.get('tempo_conexao')
            c['ultimo_visto'] = vinculo.get('ultimo_visto')
            if c['online']:
                ips_associados.add(ip)
        else:
            c['ip'] = None
            c['online'] = False
            c['tempo_conexao'] = None
            c['ultimo_visto'] = None

    ips_sem_vinculo = [ip for ip in ips_ativos if ip not in ips_associados]

    total_online = sum(1 for c in clientes if c.get('online'))
    total_offline = len(clientes) - total_online
    total_com_ip = sum(1 for c in clientes if c.get('ip'))

    return {
        'ips_ativos': sorted(list(ips_ativos)),
        'total_online': total_online,
        'total_offline': total_offline,
        'total_com_ip': total_com_ip,
        'ips_sem_vinculo': sorted(ips_sem_vinculo),
        'timestamp_consulta': datetime.now(FUSO_LOCAL).strftime('%d/%m/%Y %H:%M:%S')
    }
