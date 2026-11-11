"""Monitor de integração PBX para o Painel de Agentes (/agentes).

Consulta a API REST do PBX em segundo plano de forma independente e paralela à NPX,
obtendo o registro SIP dos dispositivos (MicroSIP), ramais e chamadas ativas.

Segurança e Privacidade:
- Base URL obrigatoriamente HTTPS, sem userinfo, query ou fragment.
- Token e chave sanitizados e codificados com quote(safe='').
- Redirecionamentos HTTP são estritamente bloqueados para impedir vazamento de credenciais.
- Logs são genéricos e nunca registram URLs completas, tokens, chaves ou corpos.
- Verificação TLS habilitada por padrão (desativável exclusivamente com PBX_TLS_REJECT_UNAUTHORIZED='false').
- Na exibição pública (/agentes/dados), números de telefone são omitidos para usuários sem permissão.
"""
import json
import logging
import os
import re
import ssl
import threading
import time
from datetime import datetime
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
FUSO = ZoneInfo('America/Fortaleza')
INTERVALO_PBX = 3.0
TIMEOUT_PADRAO = 2.5
VALIDADE_PBX = 15.0

FILA_PBX_SEM_FILA = 'PBX_Sem_Fila'
NOME_PBX_SEM_FILA = 'PBX (sem fila)'

# Mapeamento para filas oficiais conhecidas pelo sistema
TRADUCAO_FILAS = {
    'infobrasil_suporte': 'InfoBrasil_Suporte',
    'suporte': 'InfoBrasil_Suporte',
    'infobrasil_comercial': 'InfoBrasil_Comercial',
    'comercial': 'InfoBrasil_Comercial',
    'infobrasil_finan': 'Infobrasil_finan',
    'financeiro': 'Infobrasil_finan',
    'infobrasil_sped_fiscal': 'InfoBrasil_SPED_Fiscal',
    'sped fiscal': 'InfoBrasil_SPED_Fiscal',
    'sped': 'InfoBrasil_SPED_Fiscal',
    'infobrasil_ponto': 'InfoBrasil_Ponto',
    'ponto': 'InfoBrasil_Ponto',
    'infobrasil_transbordo': 'InfoBrasil_Transbordo',
    'transbordo': 'InfoBrasil_Transbordo',
}


def validar_pbx_base_url(url_raw):
    """Valida URL base do PBX: obrigatoriamente HTTPS, sem userinfo, query ou fragment."""
    if not url_raw:
        return ''
    url_limpa = str(url_raw).strip()
    try:
        parsed = urlparse.urlsplit(url_limpa)
    except Exception:
        return ''
    
    if parsed.scheme.lower() != 'https':
        LOGGER.warning('[PBX] PBX_BASE_URL deve utilizar obrigatoriamente HTTPS.')
        return ''
    if not parsed.hostname or parsed.username or parsed.password:
        LOGGER.warning('[PBX] PBX_BASE_URL inválida (userinfo não permitido).')
        return ''
    if parsed.query or parsed.fragment:
        LOGGER.warning('[PBX] PBX_BASE_URL inválida (query ou fragment não permitidos).')
        return ''
    
    caminho = parsed.path.rstrip('/')
    porta = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname}{porta}{caminho}"


def traduzir_departamento(departamento):
    """Traduz departamento do mapa para fila InfoBrasil_* oficial ou FILA_PBX_SEM_FILA."""
    if not departamento:
        return FILA_PBX_SEM_FILA
    dept_limpo = str(departamento).strip().lower()
    return TRADUCAO_FILAS.get(dept_limpo, FILA_PBX_SEM_FILA)


class BlockRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Bloqueia qualquer redirecionamento para impedir que tokens/chaves na rota vazem."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        LOGGER.warning('[PBX] Redirecionamento HTTP (%d) bloqueado por segurança.', code)
        raise urlerror.HTTPError(req.full_url, code, 'Redirecionamento não permitido para API PBX', headers, fp)


class ClientePbx:
    """Cliente HTTP REST para os endpoints da API PBX."""
    def __init__(self, base_url=None, token=None, key=None, tls_reject_unauthorized=None, timeout=TIMEOUT_PADRAO):
        if base_url is None:
            base_url = os.environ.get('PBX_BASE_URL', '')
        if token is None:
            token = os.environ.get('PBX_API_TOKEN', '')
        if key is None:
            key = os.environ.get('PBX_API_KEY', '')

        self.base_url = validar_pbx_base_url(base_url)
        self.token = str(token or '').strip()
        self.key = str(key or '').strip()
        
        # P9: Parâmetro explícito vence a variável de ambiente.
        # Variável de ambiente só desativa se for o valor exato 'false' (case-insensitive).
        if tls_reject_unauthorized is not None:
            self.tls_reject_unauthorized = bool(tls_reject_unauthorized)
        else:
            env_tls = os.environ.get('PBX_TLS_REJECT_UNAUTHORIZED', '').strip().lower()
            self.tls_reject_unauthorized = (env_tls != 'false')

        self.timeout = float(timeout)
        self._opener = None

    @property
    def configurado(self):
        return bool(self.base_url and self.token and self.key)

    def _obter_opener(self):
        if self._opener is None:
            ctx = ssl.create_default_context()
            if not self.tls_reject_unauthorized:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            https_handler = urlrequest.HTTPSHandler(context=ctx)
            self._opener = urlrequest.build_opener(BlockRedirectHandler(), https_handler)
        return self._opener

    def _requisicao(self, nome_acao, endpoint):
        """Executa GET no endpoint do PBX de forma segura e retorna os dados decodificados."""
        if not self.configurado:
            raise RuntimeError('Cliente PBX não configurado.')

        token_quoted = urlparse.quote(self.token, safe='')
        key_quoted = urlparse.quote(self.key, safe='')
        url_completa = f"{self.base_url}/api/{endpoint}/{token_quoted}/{key_quoted}"
        req = urlrequest.Request(
            url_completa,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'InfoMonitor-PBX-Client/1.0',
            },
            method='GET',
        )

        opener = self._obter_opener()
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                corpo = resp.read()
                dados = json.loads(corpo.decode('utf-8'))
        except urlerror.HTTPError as err:
            LOGGER.warning('[PBX] Falha HTTP na ação %s: código %d', nome_acao, err.code)
            raise RuntimeError(f'Falha na consulta PBX: HTTP {err.code}') from None
        except urlerror.URLError as err:
            LOGGER.warning('[PBX] Erro de rede/conexão na ação %s: %s', nome_acao, type(err.reason).__name__)
            raise RuntimeError('Falha de conexão com a central PBX') from None
        except Exception as err:
            LOGGER.warning('[PBX] Erro inesperado na ação %s: %s', nome_acao, type(err).__name__)
            raise RuntimeError('Resposta inválida da central PBX') from None

        if not isinstance(dados, dict) or dados.get('error') != 0:
            LOGGER.warning('[PBX] Resposta com erro ou formato inesperado na ação %s', nome_acao)
            raise RuntimeError('Central PBX reportou erro na resposta')

        return dados.get('data')

    def listar_dispositivos(self):
        """Consulta mapeamento de dispositivos (/api/listDevices)."""
        res = self._requisicao('listDevices', 'listDevices')
        if not isinstance(res, list):
            raise RuntimeError('Central PBX retornou dados inválidos para listDevices (não é lista)')
        return res

    def listar_dispositivos_registrados(self):
        """Consulta status SIP dos dispositivos (/api/listDevicesRegistered)."""
        res = self._requisicao('listDevicesRegistered', 'listDevicesRegistered')
        if not isinstance(res, list):
            raise RuntimeError('Central PBX retornou dados inválidos para listDevicesRegistered (não é lista)')
        return res

    def listar_chamadas_online(self):
        """Consulta chamadas em andamento (/api/onlineCalls)."""
        res = self._requisicao('onlineCalls', 'onlineCalls')
        if not isinstance(res, list):
            raise RuntimeError('Central PBX retornou dados inválidos para onlineCalls (não é lista)')
        return res


def carregar_mapa_analistas(caminho=None):
    """Carrega arquivo de mapeamento de analistas/ramais."""
    if not caminho:
        caminho = os.environ.get('PBX_MAPA_ARQUIVO', 'config/analysts_map.json')
    
    if not os.path.isabs(caminho):
        raiz = os.path.dirname(os.path.abspath(__file__))
        caminho = os.path.join(raiz, caminho)
        
    if not os.path.exists(caminho):
        return {}

    try:
        with open(caminho, 'r', encoding='utf-8') as f:
            conteudo = json.load(f)
        if isinstance(conteudo, dict):
            limpo = {}
            for k, v in conteudo.items():
                if isinstance(v, dict) and v.get('ramal'):
                    limpo[str(k).strip()] = {
                        'ramal': str(v['ramal']).strip(),
                        'departamento': str(v.get('departamento', '')).strip(),
                    }
                elif isinstance(v, (str, int)):
                    limpo[str(k).strip()] = {'ramal': str(v).strip(), 'departamento': ''}
            return limpo
    except Exception as err:
        LOGGER.warning('[PBX] Falha ao ler mapa de analistas: %s', type(err).__name__)
    return {}


def casar_analista(nome_agente, codigo_agente=None, extensao_agente=None, mapa_analistas=None):
    """Localiza o analista no mapa sem escolhas arbitrárias em caso de colisão.
    
    Retorna (ramal, departamento) ou (None, None).
    """
    if not mapa_analistas:
        return None, None

    nome_alvo = str(nome_agente or '').strip()
    nome_lower = nome_alvo.lower()

    ramal_por_nome = None
    dept_por_nome = None

    # 1. Correspondência exata por nome
    if nome_alvo in mapa_analistas:
        item = mapa_analistas[nome_alvo]
        ramal_por_nome, dept_por_nome = item['ramal'], item.get('departamento', '')
    else:
        # Correspondência insensível a maiúsculas/minúsculas
        matches_exatos = [
            (k, v) for k, v in mapa_analistas.items() if k.lower() == nome_lower
        ]
        if len(matches_exatos) == 1:
            item = matches_exatos[0][1]
            ramal_por_nome, dept_por_nome = item['ramal'], item.get('departamento', '')
        elif len(matches_exatos) > 1:
            return None, None
        else:
            # Correspondência parcial bidirecional (substring)
            matches_parciais = [
                (k, v) for k, v in mapa_analistas.items()
                if (k.lower() in nome_lower or nome_lower in k.lower()) and len(nome_lower) >= 3
            ]
            if len(matches_parciais) == 1:
                item = matches_parciais[0][1]
                ramal_por_nome, dept_por_nome = item['ramal'], item.get('departamento', '')
            elif len(matches_parciais) > 1:
                # Colisão de nomes parciais
                return None, None

    # Correspondência por 4 dígitos finais do código/extensão
    ramal_por_sufixo = None
    dept_por_sufixo = None
    for valor_id in (extensao_agente, codigo_agente):
        if not valor_id:
            continue
        digitos = re.sub(r'\D', '', str(valor_id))
        if len(digitos) >= 4:
            sufixo = digitos[-4:]
            matches_ramal = [
                (k, v) for k, v in mapa_analistas.items() if v.get('ramal') == sufixo
            ]
            if len(matches_ramal) == 1:
                item = matches_ramal[0][1]
                ramal_por_sufixo, dept_por_sufixo = item['ramal'], item.get('departamento', '')
                break
            elif len(matches_ramal) > 1:
                # Colisão de sufixo no mapa
                return None, None

    # Verificação de consistência: se ambos casaram e divergiram, conflito!
    if ramal_por_nome and ramal_por_sufixo and ramal_por_nome != ramal_por_sufixo:
        LOGGER.debug('[PBX] Conflito entre casamento de nome (%s) e sufixo (%s) para analista "%s"',
                     ramal_por_nome, ramal_por_sufixo, nome_alvo)
        return None, None

    ramal_final = ramal_por_nome or ramal_por_sufixo
    dept_final = dept_por_nome if ramal_por_nome else dept_por_sufixo
    return (ramal_final, dept_final) if ramal_final else (None, None)


def eh_identificador_analista(identificador, ramal, username=None):
    """Verifica se um identificador pertence ao próprio analista (ramal, username ou variação)."""
    if not identificador:
        return False
    ident = str(identificador).strip().lower()
    ramal_s = str(ramal or '').strip().lower()
    user_s = str(username or '').strip().lower()

    if ramal_s and (ident == ramal_s or ident == f"85{ramal_s}" or ident.endswith(ramal_s)):
        return True
    if user_s:
        if ident == user_s or ident == user_s.split('.')[0]:
            return True
        partes = user_s.split('.')
        nome_parte = partes[1] if len(partes) > 1 else user_s
        if len(nome_parte) > 2 and nome_parte in ident:
            return True
    return False


def processar_snapshot_pbx(dispositivos_lista, registrados_lista, chamadas_lista, mapa_analistas=None, agora=None):
    """Consolida os dados de dispositivos, registro SIP e chamadas ativas em um mapa por ramal."""
    agora = agora or datetime.now(FUSO)

    # 1. Mapa de username -> ramal a partir de listDevices
    username_to_ramal = {}
    ramal_to_username = {}
    for dev in dispositivos_lista:
        u = dev.get('username')
        linha = dev.get('linha_ip')
        if u and linha:
            u_str = str(u).strip()
            l_str = str(linha).strip()
            username_to_ramal[u_str] = l_str
            ramal_to_username[l_str] = u_str

    # 2. Mapa de chamadas ativas indexado por identificadores
    active_calls = {}
    for call in chamadas_lista:
        src = str(call.get('source') or '').strip()
        dst = str(call.get('destination') or '').strip()
        if src:
            active_calls.setdefault(src, []).append(call)
        if dst and dst != src:
            active_calls.setdefault(dst, []).append(call)

    # 3. Consolidação por ramal
    consolidado = {}

    for dev in registrados_lista:
        username = str(dev.get('device_username') or '').strip()
        ramal = username_to_ramal.get(username)
        if not ramal:
            match = re.search(r'85(\d{4})', username)
            if match:
                ramal = match.group(1)
        if not ramal:
            continue

        if ramal not in ramal_to_username and username:
            ramal_to_username[ramal] = username

        is_registered = dev.get('device_registered') is True
        
        # Procura chamadas associadas ao ramal ou ao username
        calls_para_ramal = (active_calls.get(ramal) or []) + (active_calls.get(username) or [])
        call_info = calls_para_ramal[0] if calls_para_ramal else None

        if not is_registered:
            consolidado[ramal] = {
                'status': 'Offline',
                'number': None,
                'direction': None,
                'seconds': 0,
            }
        elif call_info:
            src = str(call_info.get('source') or '').strip()
            dst = str(call_info.get('destination') or '').strip()
            caller_id = str(call_info.get('callerid') or call_info.get('cli') or call_info.get('from') or '').strip()
            status_text = str(call_info.get('status_text') or '').upper()

            # Status Ringing derivado apenas se explicitamente conhecido
            status_pbx = 'Ringing' if ('RING' in status_text) else 'Busy'

            src_analista = eh_identificador_analista(src, ramal, username)
            dst_analista = eh_identificador_analista(dst, ramal, username)

            # P1: Direção e número com equivalência de identificadores
            if src_analista and not dst_analista:
                direction = 'outbound'
                number = dst
            elif not src_analista and dst_analista:
                direction = 'inbound'
                number = src or caller_id
            elif src_analista and dst_analista:
                direction = 'inbound'
                if caller_id and not eh_identificador_analista(caller_id, ramal, username):
                    number = caller_id
                else:
                    number = 'Ligação Ativa'
            else:
                direction = 'outbound' if dst else 'inbound'
                number = dst or src or caller_id

            # Guarda final: nunca exibir identificador do próprio analista como número
            if eh_identificador_analista(number, ramal, username) or not number:
                number = 'Ligação Ativa'

            # Duração limitada entre 0 e 86400s (24h)
            seconds = 0
            starttime_raw = call_info.get('starttime')
            if starttime_raw:
                try:
                    dt_call = datetime.fromisoformat(str(starttime_raw))
                    if dt_call.tzinfo is None:
                        dt_call = dt_call.replace(tzinfo=FUSO)
                    diff = (agora - dt_call).total_seconds()
                    seconds = max(0, min(86400, int(diff)))
                except Exception:
                    seconds = 0

            consolidado[ramal] = {
                'status': status_pbx,
                'number': number or None,
                'direction': direction,
                'seconds': seconds,
            }
        else:
            consolidado[ramal] = {
                'status': 'Free',
                'number': None,
                'direction': None,
                'seconds': 0,
            }

    return consolidado


class ColetorPbx:
    """Coletor em segundo plano com thread daemon para ler dados da central PBX a cada ~3s."""
    def __init__(self, cliente=None, mapa_analistas=None):
        self.cliente = cliente or ClientePbx()
        self.mapa_analistas = mapa_analistas if mapa_analistas is not None else carregar_mapa_analistas()
        self._trava = threading.Lock()
        self._thread = None
        self._parar = threading.Event()
        self._acordar = threading.Event()
        
        self.snapshot = {}              # ramal -> { status, number, direction, seconds }
        self.coletado_em = None
        self.erro = None
        self._dispositivos_cache = []
        self._dispositivos_em = 0.0

    def garantir_rodando(self):
        if not self.cliente.configurado:
            return
        with self._trava:
            if self._thread and self._thread.is_alive():
                return
            self._parar.clear()
            self._thread = threading.Thread(target=self._laco, name='coletor-agentes-pbx', daemon=True)
            self._thread.start()

    def parar(self):
        self._parar.set()
        self._acordar.set()

    def _laco(self):
        while not self._parar.is_set():
            try:
                self.coletar()
            except Exception as erro:
                with self._trava:
                    self.erro = 'Coleta PBX indisponível no momento.'
                LOGGER.warning('[PBX] Falha no ciclo de coleta PBX: %s', type(erro).__name__)
            self._acordar.wait(INTERVALO_PBX)
            self._acordar.clear()

    def coletar(self):
        """Executa uma coleta imediata dos dados do PBX."""
        if not self.cliente.configurado:
            return

        agora = datetime.now(FUSO)

        # Atualiza lista de dispositivos a cada 5 minutos ou se vazia
        if time.monotonic() - self._dispositivos_em >= 300.0 or not self._dispositivos_cache:
            try:
                self._dispositivos_cache = self.cliente.listar_dispositivos()
                self._dispositivos_em = time.monotonic()
            except Exception as err:
                LOGGER.warning('[PBX] Falha ao listar dispositivos cadastrados: %s', type(err).__name__)

        registrados = self.cliente.listar_dispositivos_registrados()
        chamadas = self.cliente.listar_chamadas_online()

        snapshot_novo = processar_snapshot_pbx(
            self._dispositivos_cache,
            registrados,
            chamadas,
            mapa_analistas=self.mapa_analistas,
            agora=agora,
        )

        with self._trava:
            self.snapshot = snapshot_novo
            self.coletado_em = agora
            self.erro = None

    def obter_status_conexao(self):
        """Retorna o estado da conexão para o JSON da API /agentes/dados."""
        configured = self.cliente.configurado
        with self._trava:
            if not configured:
                return {'configured': False, 'available': False, 'error': None}
            
            if self.erro:
                return {'configured': True, 'available': False, 'error': self.erro}
            
            if self.coletado_em is None:
                return {'configured': True, 'available': False, 'error': None}
            
            idade = (datetime.now(FUSO) - self.coletado_em).total_seconds()
            available = idade <= VALIDADE_PBX
            return {
                'configured': True,
                'available': available,
                'error': None if available else 'Dados da central PBX desatualizados.',
            }

    def enriquecer_agentes(self, agentes_npx):
        """Aplica os campos pbx* e line em cada agente da lista NPX com unicidade estrita."""
        with self._trava:
            snapshot = dict(self.snapshot)

        # P6: Pré-cálculo de casamento por código único para detectar colisões
        # Se dois códigos NPX distintos casarem com o mesmo ramal -> nenhum recebe!
        candidatos_por_codigo = {}
        codigos_por_ramal = {}
        for a in agentes_npx:
            code = str(a.get('code'))
            if code not in candidatos_por_codigo:
                r, _ = casar_analista(
                    a.get('name'),
                    codigo_agente=code,
                    extensao_agente=a.get('extension'),
                    mapa_analistas=self.mapa_analistas,
                )
                candidatos_por_codigo[code] = r
                if r:
                    codigos_por_ramal.setdefault(r, set()).add(code)

        ramais_ambiguos = {r for r, codes in codigos_por_ramal.items() if len(codes) > 1}

        for a in agentes_npx:
            code = str(a.get('code'))
            ramal = candidatos_por_codigo.get(code)
            if ramal in ramais_ambiguos:
                ramal = None

            pbx_info = snapshot.get(ramal) if ramal else None
            
            if pbx_info:
                pbx_status = pbx_info.get('status') or 'Offline'
                pbx_number = pbx_info.get('number')
                pbx_direction = pbx_info.get('direction')
                pbx_seconds = pbx_info.get('seconds') or 0
            else:
                pbx_status = 'Offline' if ramal else None
                pbx_number = None
                pbx_direction = None
                pbx_seconds = 0 if ramal else None

            a['pbxExtension'] = ramal
            a['pbxStatus'] = pbx_status
            a['pbxNumber'] = pbx_number
            a['pbxDirection'] = pbx_direction
            a['pbxSeconds'] = pbx_seconds

            # Precedência estrita:
            # 1. NPX em pausa ou com status != 0 (status None não é ativo!)
            # 2. PBX registrada (Free, Busy, Ringing) -> 'pbx'
            # 3. Senão -> 'off'
            em_pausa = bool(a.get('paused'))
            status_npx = a.get('status')
            npx_ativo = status_npx is not None and status_npx != 0

            if em_pausa or npx_ativo:
                a['line'] = 'npx'
            elif pbx_status in ('Free', 'Busy', 'Ringing'):
                a['line'] = 'pbx'
            else:
                a['line'] = 'off'

        return agentes_npx

    def obter_agentes_pbx_only(self, codigos_npx_existentes, ramais_npx_casados=None):
        """Gera cards para analistas do mapa PBX que não possuem sessão na NPX e estão REGISTRADOS."""
        if not self.mapa_analistas:
            return []

        with self._trava:
            snapshot = dict(self.snapshot)

        ramais_npx_casados = ramais_npx_casados or set()
        codigos_set = set(str(c) for c in codigos_npx_existentes)
        cards_pbx = []

        for nome, info in self.mapa_analistas.items():
            ramal = info.get('ramal')
            if not ramal or ramal in ramais_npx_casados:
                continue

            codigo_pbx = f"pbx-{ramal}"
            if codigo_pbx in codigos_set:
                continue

            pbx_info = snapshot.get(ramal, {})
            pbx_status = pbx_info.get('status')

            # P5: PBX-only SÓ ADICIONA SE REGISTRADO (não inundar painel com todo mapa offline)
            if pbx_status not in ('Free', 'Busy', 'Ringing'):
                continue

            dept_traduzido = traduzir_departamento(info.get('departamento'))
            pbx_number = pbx_info.get('number')
            pbx_direction = pbx_info.get('direction')
            pbx_seconds = pbx_info.get('seconds') or 0

            cards_pbx.append({
                'code': codigo_pbx,
                'name': nome,
                'queue': dept_traduzido,
                'extension': ramal,
                'status': 0,
                'seconds': 0,
                'penalty': None,
                'paused': False,
                'pauseType': None,
                'pauseStart': None,
                'pauseMinutes': None,
                'pauseMaxMinutes': None,
                'callSrc': None,
                'callDst': None,
                'callClient': None,
                'erro': None,
                'line': 'pbx',
                'pbxExtension': ramal,
                'pbxStatus': pbx_status,
                'pbxNumber': pbx_number,
                'pbxDirection': pbx_direction,
                'pbxSeconds': pbx_seconds,
            })

        return cards_pbx
