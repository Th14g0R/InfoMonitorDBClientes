"""Painel de Agentes do call center (NPX/Beatrix).

Consulta pública somente leitura em /agentes; quem tem a permissão ``perm_agentes``
(ou é Master) organiza o layout e executa ações: tirar pausa, colocar em pausa,
transferir de fila, mudar prioridade, remover de fila e deslogar.

A API da NPX responde uma consulta por vez (~0,4 s cada), então a coleta roda
continuamente numa thread e as páginas leem sempre o último retrato em memória.
As ações administrativas operam via sessão web Devise (ClientePainelNpx), mantida
estritamente em memória com re-autenticação automática e proteção contra vazamento
de credenciais.
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http import cookiejar
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

from flask import jsonify, render_template, request, session

from seguranca import registrar_acao_admin

LOGGER = logging.getLogger(__name__)
FUSO = ZoneInfo('America/Fortaleza')
PERMISSAO = 'perm_agentes'
RE_CODIGO = re.compile(r'^\d{1,12}$')
RE_FILA = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
NOMES_PADRAO = {
    'InfoBrasil_Suporte': 'Suporte',
    'InfoBrasil_Comercial': 'Comercial',
    'Infobrasil_finan': 'Financeiro',
    'InfoBrasil_SPED_Fiscal': 'SPED Fiscal',
    'InfoBrasil_Ponto': 'Ponto',
    'InfoBrasil_Transbordo': 'Transbordo',
}
COLUNAS_ORDEM = {'name', 'status', 'tempo', 'penalty', 'situacao'}
INTERVALO_COLETA = 1.5      # pausa entre coletas (s)
VALIDADE_HOJE = 15          # relatórios do dia (s)
VALIDADE_HISTORICO = 60     # sessões abertas de dias anteriores (s)


def _lista_env(nome):
    """Lê ``FILA=valor;FILA2=valor`` (aceita também vírgula) de uma variável do .env."""
    itens = {}
    for parte in re.split(r'[;,]', os.getenv(nome, '')):
        if '=' in parte:
            chave, valor = parte.split('=', 1)
            if chave.strip() and valor.strip():
                itens[chave.strip()] = valor.strip()
    return itens


def nomes_filas():
    return {**NOMES_PADRAO, **_lista_env('NPX_FILAS_NOMES')}


def _validar_fila(fila, coletor=None):
    f = str(fila or '').strip()
    if not f or not RE_FILA.match(f):
        raise ValueError('Fila inválida.')
    validas = set(nomes_filas().keys())
    if coletor and coletor.retrato:
        validas.update(a.get('queue') for a in coletor.retrato if a.get('queue'))
    if f not in validas:
        raise ValueError(f'Fila "{f}" não reconhecida.')
    return f


def _iso_para_datetime(valor):
    try:
        return datetime.fromisoformat(str(valor))
    except (TypeError, ValueError):
        return None


class ClienteNpx:
    """Cliente para a API Beatrix (pabx, relatórios e deslogar)."""
    def __init__(self, base=None, token=None, timeout=15):
        self.base = (base or os.getenv('NPX_API_URL') or 'https://app.npxtech.com.br/api').rstrip('/')
        self.token = token if token is not None else os.getenv('NPX_API_TOKEN', '').strip()
        self.timeout = timeout

    @property
    def configurado(self):
        return bool(self.token)

    def _req(self, metodo, caminho, corpo=None):
        dados = None if corpo is None else json.dumps(corpo).encode()
        req = urlrequest.Request(self.base + caminho, data=dados, method=metodo, headers={
            'Access-token': self.token, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode('utf-8', 'replace')
        except urlerror.HTTPError as erro:
            return erro.code, erro.read().decode('utf-8', 'replace')

    def get(self, caminho):
        status, texto = self._req('GET', caminho)
        if status >= 400:
            raise RuntimeError(f'NPX respondeu HTTP {status}')
        return json.loads(texto)

    def post(self, caminho, corpo=None):
        """A NPX responde HTTP 200 mesmo com erro: sucesso = status 1 e sem campo ``error``."""
        try:
            status, texto = self._req('POST', caminho, corpo or {})
        except Exception as erro:
            LOGGER.warning('Falha de rede no POST da API NPX (%s): %s', caminho, type(erro).__name__)
            return {'ok': False, 'http': 502, 'mensagem': 'Falha na comunicação com a API NPX.'}

        try:
            obj = json.loads(texto)
        except (ValueError, TypeError):
            obj = None

        ok = 200 <= status < 300
        if ok and isinstance(obj, dict):
            if obj.get('error'):
                ok = False
            elif 'status' in obj and str(obj['status']) != '1':
                ok = False

        raw_msg = (obj.get('message') or obj.get('error')) if isinstance(obj, dict) else None
        if isinstance(raw_msg, str):
            s = re.sub(r'<[^>]+>', ' ', raw_msg)
            s = re.sub(r'\s+', ' ', s).strip()
            if self.token and len(self.token) >= 8:
                s = s.replace(self.token, '***')
            s = re.sub(r'(?i)(?:bearer\s+|token[:=]\s*)[A-Za-z0-9_\-\.]{8,}', 'token: ***', s)
            mensagem = s[:200]
        else:
            mensagem = 'Ação realizada com sucesso.' if ok else 'Falha na operação na API NPX.'

        http_retorno = status if ok else (502 if status in (401, 403) else (status if status >= 400 else 502))
        return {'ok': ok, 'http': http_retorno, 'mensagem': mensagem}


class CSRFParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        if self.token:
            return
        d = dict(attrs)
        if tag == 'input' and d.get('name') == 'authenticity_token':
            self.token = d.get('value')
        elif tag == 'meta' and d.get('name') == 'csrf-token':
            self.token = d.get('content')


class SameOriginRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Permite redirecionamentos apenas dentro da mesma origem HTTPS configurada."""
    def __init__(self, allowed_origin):
        super().__init__()
        self.allowed_origin = allowed_origin.rstrip('/')

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse.urljoin(req.full_url, newurl)
        p = urlparse.urlparse(target)
        origin = f'{p.scheme}://{p.netloc}'
        if origin != self.allowed_origin or p.username or p.password or p.scheme != 'https':
            raise urlerror.HTTPError(req.full_url, 403, 'Redirecionamento fora da origem HTTPS bloqueado.', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


class ClientePainelNpx:
    """Cliente para automação de ações operacionais no painel web da NPX (Rails/Devise).

    Mantém sessão e cookies exclusivamente em memória (CookieJar).
    Nunca persiste credenciais nem repassa segredos em logs ou respostas.
    """
    ACOES = {
        'update_penalty': '/rates/update_penalty',
        'move_agent': '/rates/move_agent',
        'invert_pause_status': '/rates/invert_pause_status',
        'remove_agent': '/rates/remove_agent',
    }
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    COOLDOWN_LOGIN = 60.0

    def __init__(self, base=None, email=None, senha=None, timeout=15):
        self.base = (base or os.getenv('NPX_PAINEL_URL') or 'https://app.npx.com.br').rstrip('/')
        self.email = email if email is not None else os.getenv('NPX_PAINEL_EMAIL', '').strip()
        self.senha = senha if senha is not None else os.getenv('NPX_PAINEL_SENHA', '').strip()
        self.timeout = timeout
        self._clock = time.monotonic
        self._login_falha_em = -999.0
        self._trava = threading.RLock()
        self._jar = cookiejar.CookieJar()
        self._opener = None
        self._autenticado = False
        self._token_csrf = None

    def _validar_base_url(self):
        if not self.base:
            return False
        p = urlparse.urlparse(self.base)
        if p.scheme != 'https' or not p.netloc:
            return False
        if p.username or p.password or p.query or p.fragment:
            return False
        return True

    @property
    def configurado(self):
        return bool(self._validar_base_url() and self.email and self.senha)

    def _em_cooldown(self):
        return (self._clock() - self._login_falha_em) < self.COOLDOWN_LOGIN

    def _obter_origem(self):
        p = urlparse.urlparse(self.base)
        return f'{p.scheme}://{p.netloc}'

    def _criar_opener(self):
        origem = self._obter_origem()
        redirect_handler = SameOriginRedirectHandler(origem)
        cookie_processor = urlrequest.HTTPCookieProcessor(self._jar)
        return urlrequest.build_opener(cookie_processor, redirect_handler)

    def _sanitizar_mensagem(self, texto):
        if not isinstance(texto, str):
            return None
        s = re.sub(r'<[^>]+>', ' ', texto)
        s = re.sub(r'\s+', ' ', s).strip()
        if not s:
            return ''

        segredos = set()
        if self.senha and len(self.senha) >= 3:
            segredos.add(self.senha)
        if self.email and len(self.email) >= 3:
            segredos.add(self.email)
        token_api = os.getenv('NPX_API_TOKEN', '').strip()
        if token_api and len(token_api) >= 8:
            segredos.add(token_api)
        if self._token_csrf and len(self._token_csrf) >= 8:
            segredos.add(self._token_csrf)
        # Valores de CookieJar: somente cookies com len >= 8 para não corromper texto legítimo
        for c in self._jar:
            if c.value and len(c.value) >= 8:
                segredos.add(c.value)

        for segredo in sorted(segredos, key=len, reverse=True):
            s = s.replace(segredo, '***')

        s = re.sub(r'(?i)(?:bearer\s+|token[:=]\s*)[A-Za-z0-9_\-\.]{8,}', 'token: ***', s)
        s = re.sub(r'(?i)(?:cookie[:=]\s*)[^;\s]{8,}', 'cookie: ***', s)
        s = re.sub(r'([?&](?:authenticity_token|password|senha|token)=)[^&]+', r'\1***', s, flags=re.IGNORECASE)
        s = re.sub(r'(?:_session|_callcenter_session|remember_user_token)=[^;\s]+', '***', s, flags=re.IGNORECASE)

        s = re.sub(r'\s+', ' ', s).strip()
        return s[:200]

    def autenticar(self, forcar=False):
        if not self.configurado:
            raise RuntimeError('Credenciais do painel NPX não configuradas ou URL inválida.')
        with self._trava:
            if self._autenticado and not forcar:
                return True
            if self._em_cooldown():
                raise RuntimeError('Login no painel NPX temporariamente bloqueado após falha recente.')

            self._autenticado = False
            self._jar.clear()
            self._opener = self._criar_opener()
            self._token_csrf = None
            login_url = self.base + '/users/sign_in'

            # 1. GET para extrair authenticity_token
            req_get = urlrequest.Request(login_url, headers={
                'User-Agent': self.USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            })
            try:
                with self._opener.open(req_get, timeout=self.timeout) as resp:
                    corpo_html = resp.read().decode('utf-8', 'replace')
            except Exception as erro:
                self._login_falha_em = self._clock()
                LOGGER.warning('Falha de conexão com login do painel NPX: %s', type(erro).__name__)
                raise RuntimeError('Falha de conexão com a página de login do painel NPX.')

            parser = CSRFParser()
            parser.feed(corpo_html)
            token = parser.token
            if not token:
                self._login_falha_em = self._clock()
                LOGGER.warning('Token CSRF não localizado na página de login do painel NPX.')
                raise RuntimeError('Token de autenticidade não encontrado no login do painel NPX.')
            self._token_csrf = token

            # 2. POST de login
            dados_post = urlparse.urlencode({
                'authenticity_token': token,
                'user[email]': self.email,
                'user[password]': self.senha,
                'commit': 'Entrar',
            }).encode('utf-8')

            req_post = urlrequest.Request(login_url, data=dados_post, headers={
                'User-Agent': self.USER_AGENT,
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            })
            try:
                with self._opener.open(req_post, timeout=self.timeout) as resp:
                    final_url = resp.geturl()
                    final_body = resp.read().decode('utf-8', 'replace')
            except urlerror.HTTPError as erro:
                self._login_falha_em = self._clock()
                LOGGER.warning('HTTP %s no login do painel NPX: %s', erro.code, type(erro).__name__)
                raise RuntimeError('Credenciais inválidas ou erro no login do painel NPX.')
            except Exception as erro:
                self._login_falha_em = self._clock()
                LOGGER.warning('Falha na comunicação de autenticação com o painel NPX: %s', type(erro).__name__)
                raise RuntimeError('Falha na comunicação de autenticação com o painel NPX.')

            if '/users/sign_in' in final_url or 'new_user' in final_body:
                self._login_falha_em = self._clock()
                self._jar.clear()
                self._token_csrf = None
                LOGGER.warning('Credenciais do painel NPX inválidas ou sessão não confirmada.')
                raise RuntimeError('Credenciais do painel NPX inválidas ou sessão não confirmada.')

            self._autenticado = True
            LOGGER.info('Sessão do painel NPX autenticada com sucesso.')
            return True

    def _requisicao_acao(self, endpoint, query_params):
        if not self._autenticado or not self._opener:
            self.autenticar()
        qs = urlparse.urlencode(query_params)
        url = f'{self.base}{endpoint}?{qs}'
        req = urlrequest.Request(url, headers={
            'User-Agent': self.USER_AGENT,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
        })
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                texto = resp.read().decode('utf-8', 'replace')
                url_final = resp.geturl()
        except urlerror.HTTPError as erro:
            status = erro.code
            texto = erro.read().decode('utf-8', 'replace')
            url_final = erro.url
            try:
                erro.close()
            except Exception:
                pass
        return status, texto, url_final

    def executar(self, acao, parametros):
        if not self.configurado:
            return {'ok': False, 'http': 503, 'mensagem': 'Painel NPX não configurado no servidor.'}
        if acao not in self.ACOES:
            return {'ok': False, 'http': 400, 'mensagem': f'Ação "{acao}" inválida.'}

        # Validação estrita de parâmetros ANTES de qualquer requisição
        params_envio = {}
        codigo = _codigo(parametros.get('code'))
        params_envio['code'] = codigo

        if acao == 'update_penalty':
            pen = parametros.get('penalty')
            if isinstance(pen, bool) or not isinstance(pen, int) or not (1 <= pen <= 10):
                raise ValueError('O valor da prioridade deve ser um número inteiro entre 1 e 10.')
            queue = str(parametros.get('queue') or '').strip()
            if not RE_FILA.match(queue):
                raise ValueError('Fila inválida.')
            params_envio['penalty'] = str(pen)
            params_envio['queue'] = queue

        elif acao == 'move_agent':
            queue = str(parametros.get('queue') or '').strip()
            if not RE_FILA.match(queue):
                raise ValueError('Fila inválida.')
            params_envio['queue'] = queue

        elif acao == 'invert_pause_status':
            pause_raw = parametros.get('pause')
            if pause_raw in ('0', 0) and not isinstance(pause_raw, bool):
                params_envio['pause'] = '0'
            elif pause_raw in ('1', 1) and not isinstance(pause_raw, bool):
                params_envio['pause'] = '1'
            else:
                raise ValueError('Status de pausa inválido (esperado 0 ou 1).')

        elif acao == 'remove_agent':
            queue = str(parametros.get('queue') or '').strip()
            if queue and not RE_FILA.match(queue):
                raise ValueError('Fila inválida.')
            # A spec externa do NPX rates/remove_agent usa SOMENTE code na query string

        endpoint = self.ACOES[acao]

        with self._trava:
            if self._em_cooldown():
                return {'ok': False, 'http': 502,
                        'mensagem': 'Comunicação com o painel NPX temporariamente suspensa após falha recente de autenticação.'}

            try:
                status, texto, url_final = self._requisicao_acao(endpoint, params_envio)
            except Exception as erro:
                LOGGER.warning('Falha de rede na ação %s do painel NPX: %s', acao, type(erro).__name__)
                return {'ok': False, 'http': 502, 'mensagem': 'Falha na comunicação com o painel NPX.'}

            def eh_sessao_expirada(st, corpo, u):
                if st in (401, 403):
                    return True
                if '/users/sign_in' in (u or ''):
                    return True
                if 'new_user' in corpo or 'users/sign_in' in corpo:
                    return True
                try:
                    json.loads(corpo)
                    return False
                except (ValueError, TypeError):
                    return True

            if eh_sessao_expirada(status, texto, url_final):
                LOGGER.info('Sessão expirada ou resposta não-JSON em %s. Re-autenticando e repetindo 1 vez...', acao)
                try:
                    self.autenticar(forcar=True)
                    status, texto, url_final = self._requisicao_acao(endpoint, params_envio)
                except Exception:
                    LOGGER.warning('Falha na re-autenticação do painel NPX.')
                    return {'ok': False, 'http': 502,
                            'mensagem': 'Sessão do painel NPX expirada e não foi possível re-autenticar.'}

            # Avaliação do resultado após retry (ou primeira execução válida)
            try:
                obj = json.loads(texto)
            except (ValueError, TypeError):
                return {'ok': False, 'http': 502, 'mensagem': 'Resposta inválida do painel NPX.'}

            ok = (200 <= status < 300)
            if isinstance(obj, dict):
                if obj.get('error'):
                    ok = False
                elif 'status' in obj and str(obj['status']) not in ('1', 'true', 'ok'):
                    ok = False
                raw_msg = obj.get('message') or obj.get('error') or obj.get('msg')
            else:
                raw_msg = None

            msg_sanitizada = self._sanitizar_mensagem(raw_msg)
            if msg_sanitizada:
                msg_final = msg_sanitizada
            else:
                msg_final = 'Ação realizada com sucesso.' if ok else 'Falha na operação no painel NPX.'

            http_retorno = status if ok else (502 if status in (401, 403) else (status if status >= 400 else 502))
            return {'ok': ok, 'http': http_retorno, 'mensagem': msg_final}


class ColetorAgentes:
    def __init__(self, cliente):
        self.cliente = cliente
        self._trava = threading.Lock()
        self._thread = None
        self._parar = threading.Event()
        self._forcar = threading.Event()
        self._acordar = threading.Event()
        self.retrato = None          # lista de agentes
        self.coletado_em = None
        self.erro = None
        self._historico, self._historico_em = [], 0.0
        self._hoje, self._pausas, self._hoje_em = [], [], 0.0

    # ---- ciclo de vida ----
    def garantir_rodando(self):
        if not self.cliente.configurado:
            return
        with self._trava:
            if self._thread and self._thread.is_alive():
                return
            self._parar.clear()
            self._thread = threading.Thread(target=self._laco, name='coletor-agentes-npx', daemon=True)
            self._thread.start()

    def parar(self):
        self._parar.set()
        self._acordar.set()

    def forcar_releitura(self):
        self._forcar.set()
        self._acordar.set()

    def _laco(self):
        while not self._parar.is_set():
            try:
                self.coletar()
            except Exception as erro:
                self.erro = 'Coleta de agentes indisponível no momento.'
                LOGGER.warning('Falha na coleta de agentes da NPX: %s', type(erro).__name__)
            self._acordar.wait(INTERVALO_COLETA)
            self._acordar.clear()

    # ---- coleta ----
    def _relatorio(self, caminho):
        return list((self.cliente.get(caminho) or {}).get('report_data') or [])

    def _sessoes_abertas(self, agora):
        if self._forcar.is_set():
            self._forcar.clear()
            self._hoje_em = 0.0
        if time.monotonic() - self._historico_em >= VALIDADE_HISTORICO:
            # agente pode estar logado há dias: varre 30 dias numa única página
            de = (agora - timedelta(days=30)).strftime('%Y-%m-%d')
            ate = agora.strftime('%Y-%m-%d')
            self._historico = self._relatorio(
                f'/call_center_reports/report_register_points_details?date_started_at={de}&date_ended_at={ate}&per_page=1000')
            self._historico_em = time.monotonic()
        if time.monotonic() - self._hoje_em >= VALIDADE_HOJE:
            self._hoje = self._relatorio('/call_center_reports/report_register_points_details?per_page=1000')
            self._pausas = self._relatorio('/call_center_reports/report_pause_details?per_page=1000')
            self._hoje_em = time.monotonic()
        sessoes = {}
        for linha in self._historico + self._hoje:          # a de hoje prevalece para a mesma sessão
            sessoes[(str(linha.get('code')), linha.get('queue'), linha.get('logon_date'))] = linha
        ultima = {}
        for linha in sessoes.values():                      # vale a sessão mais recente de cada agente+fila
            chave = (str(linha.get('code')), linha.get('queue'))
            if chave not in ultima or str(linha.get('logon_date')) > str(ultima[chave].get('logon_date')):
                ultima[chave] = linha
        return sorted((l for l in ultima.values() if not l.get('logoff_date')), key=lambda l: str(l.get('logon_date')))

    def coletar(self):
        agora = datetime.now(FUSO)
        agentes = {}
        for linha in self._sessoes_abertas(agora):
            codigo = str(linha.get('code'))
            if not RE_CODIGO.match(codigo):
                continue
            item = agentes.setdefault(codigo, {'name': linha.get('agent_name'), 'filas': []})
            if linha.get('queue') and linha['queue'] not in item['filas']:
                item['filas'].append(linha['queue'])
        pausas_abertas = {}
        for pausa in self._pausas:
            if pausa.get('pause_finish'):
                continue
            codigo = str(pausa.get('code'))
            pausas_abertas[codigo] = pausa
            item = agentes.setdefault(codigo, {'name': pausa.get('agent_name'), 'filas': []})
            if pausa.get('queue') and pausa['queue'] not in item['filas']:
                item['filas'].append(pausa['queue'])

        def status_de(codigo):
            try:
                return codigo, self.cliente.get(f'/pabx/{codigo}/agent_status')
            except Exception:
                return codigo, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            estados = dict(pool.map(status_de, list(agentes)))

        lista = []
        for codigo, info in agentes.items():
            st = estados.get(codigo) or None
            aberta = pausas_abertas.get(codigo)
            em_pausa = str(st.get('paused')) == '1' if st else bool(aberta)
            inicio = tipo = minutos = None
            if em_pausa:
                if aberta:
                    inicio, tipo = aberta.get('pause_start'), aberta.get('pause')
                    dt = _iso_para_datetime(inicio)
                    minutos = round((agora - dt).total_seconds() / 60, 1) if dt else None
                elif st:
                    minutos = round(float(st.get('duration') or 0) / 60, 1)
            chamada = (st or {}).get('current_call') or {}
            for fila in info['filas'] or ['']:
                lista.append({
                    'code': codigo,
                    'name': (st or {}).get('name') or info['name'],
                    'queue': fila,
                    'extension': (st or {}).get('extension') or codigo,
                    'status': int(st['status']) if st and str(st.get('status', '')).lstrip('-').isdigit() else None,
                    'seconds': int(st.get('duration') or 0) if st else None,
                    'penalty': st.get('penalty') if st else None,
                    'paused': em_pausa,
                    'pauseType': tipo,
                    'pauseStart': inicio,
                    'pauseMinutes': minutos,
                    'callSrc': (st or {}).get('src') or None,
                    'callDst': (st or {}).get('dst') or None,
                    'callClient': chamada.get('client_name') or None,
                    'erro': None if st else 'status indisponível',
                })
        self.retrato, self.coletado_em, self.erro = lista, agora, None
        return lista

    def dados(self, espera=12):
        """Último retrato; na primeira chamada espera a coleta inicial terminar."""
        self.garantir_rodando()
        limite = time.monotonic() + espera
        while self.retrato is None and self.erro is None and time.monotonic() < limite and self.cliente.configurado:
            time.sleep(0.2)
        return self.retrato, self.coletado_em, self.erro


# ---- layout definido pelo administrador ----
LAYOUT_PADRAO = {'cols': 2, 'c1': ['InfoBrasil_Suporte'], 'c2': [], 'ord': {'col': 'status', 'dir': 1}, 'limite': 10}


def validar_layout(dados):
    if not isinstance(dados, dict):
        raise ValueError('Layout inválido.')
    try:
        cols = int(dados.get('cols', 1))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Número de colunas inválido.')
    if cols not in (1, 2):
        raise ValueError('Número de colunas inválido.')
    colunas = {}
    for chave in ('c1', 'c2'):
        valor = dados.get(chave) or []
        if not isinstance(valor, list) or len(valor) > 50 or not all(isinstance(v, str) and RE_FILA.match(v) for v in valor):
            raise ValueError('Lista de filas inválida.')
        colunas[chave] = list(dict.fromkeys(valor))
    colunas['c2'] = [v for v in colunas['c2'] if v not in colunas['c1']]
    ord_ = dados.get('ord')
    if ord_ is None:
        ord_ = {}
    elif not isinstance(ord_, dict):
        raise ValueError('Ordenação inválida.')
    col = ord_.get('col', 'status')
    try:
        direcao = int(ord_.get('dir', 1))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Ordenação inválida.')
    if col not in COLUNAS_ORDEM or direcao not in (1, -1):
        raise ValueError('Ordenação inválida.')
    try:
        limite = int(dados.get('limite', 10))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Limite de pausa inválido.')
    if not 1 <= limite <= 600:
        raise ValueError('Limite de pausa inválido.')
    return {'cols': cols, **colunas, 'ord': {'col': col, 'dir': direcao}, 'limite': limite}


class LayoutAgentes:
    def __init__(self, conectar):
        self.conectar = conectar
        with closing(conectar()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS agentes_layout (
                id INTEGER PRIMARY KEY CHECK(id=1), dados TEXT NOT NULL,
                atualizado_em TEXT, atualizado_por TEXT)''')

    def obter(self):
        with closing(self.conectar()) as conn:
            linha = conn.execute('SELECT dados FROM agentes_layout WHERE id=1').fetchone()
        if not linha:
            return dict(LAYOUT_PADRAO)
        try:
            return validar_layout(json.loads(linha['dados']))
        except (ValueError, TypeError):
            return dict(LAYOUT_PADRAO)

    def salvar(self, dados, usuario):
        layout = validar_layout(dados)
        with closing(self.conectar()) as conn, conn:
            conn.execute('''INSERT INTO agentes_layout(id, dados, atualizado_em, atualizado_por) VALUES(1, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET dados=excluded.dados, atualizado_em=excluded.atualizado_em,
                            atualizado_por=excluded.atualizado_por''',
                         (json.dumps(layout), datetime.now(FUSO).isoformat(), usuario))
        return layout


# ---- ações ----
def _codigo(valor):
    codigo = str(valor or '').strip()
    if not RE_CODIGO.match(codigo):
        raise ValueError('Código de agente inválido.')
    return codigo


def transferir(cliente_painel, dados, coletor=None):
    codigo = _codigo(dados.get('code'))
    para = _validar_fila(dados.get('para'), coletor)
    if not cliente_painel or not cliente_painel.configurado:
        return {'ok': False, 'http': 503, 'mensagem': 'Painel NPX não configurado no servidor.'}
    return cliente_painel.executar('move_agent', {'code': codigo, 'queue': para})


def alterar_prioridade(cliente_painel, dados, coletor=None):
    codigo = _codigo(dados.get('code'))
    fila = _validar_fila(dados.get('fila'), coletor)
    raw_pen = dados.get('penalty')
    if isinstance(raw_pen, bool) or not isinstance(raw_pen, int) or not (1 <= raw_pen <= 10):
        raise ValueError('O valor da prioridade deve ser um número inteiro entre 1 e 10.')
    if not cliente_painel or not cliente_painel.configurado:
        return {'ok': False, 'http': 503, 'mensagem': 'Painel NPX não configurado no servidor.'}
    return cliente_painel.executar('update_penalty', {'code': codigo, 'queue': fila, 'penalty': raw_pen})


def configurar_agentes(bp, conectar, tem_permissao, cliente=None, cliente_painel=None):
    cliente = cliente or ClienteNpx()
    cliente_painel = cliente_painel or ClientePainelNpx()
    coletor = ColetorAgentes(cliente)
    layout = LayoutAgentes(conectar)
    bp.coletor_agentes = coletor
    bp.cliente_npx = cliente
    bp.cliente_painel_npx = cliente_painel

    def pode():
        return bool(tem_permissao(PERMISSAO))

    def negar():
        return jsonify(ok=False, mensagem='Sem permissão para gerenciar agentes.'), 403

    def usuario():
        return session.get('user_nome') or str(session.get('user_id') or '?')

    def responder_acao(acao, codigo, resultado):
        ok = bool(resultado.get('ok'))
        registrar_acao_admin(f'agentes_{acao}', {'agente': codigo, 'ok': ok})
        coletor.forcar_releitura()
        status_http = resultado.get('http', 200)
        if not ok:
            if status_http in (401, 403):
                status_http = 502
            elif status_http == 503:
                status_http = 503
            elif status_http < 400:
                status_http = 502
        else:
            status_http = 200
        return jsonify(ok=ok, mensagem=resultado.get('mensagem')), status_http

    @bp.route('/agentes', endpoint='agentes')
    def pagina_agentes():
        return render_template('agentes.html', pode_gerenciar=pode(), configurado=cliente.configurado,
                               layout_inicial=layout.obter())

    @bp.route('/agentes/dados', endpoint='agentes_dados')
    def dados_agentes():
        if not cliente.configurado:
            return jsonify(erro='NPX_API_TOKEN não configurado no .env do servidor.'), 503
        lista, em, erro = coletor.dados()
        if lista is None:
            return jsonify(erro='Sem resposta da NPX.'), 502
        nomes = nomes_filas()
        chaves = list(dict.fromkeys(list(nomes) + [a['queue'] for a in lista if a['queue']]))
        pode_gerenciar = pode()
        if pode_gerenciar:
            agentes_retorno = lista
        else:
            campos_privados = {'callSrc', 'callDst', 'callClient'}
            agentes_retorno = [{k: v for k, v in a.items() if k not in campos_privados} for a in lista]
        resposta = jsonify(
            atualizado=em.isoformat(), erroNpx=erro, layout=layout.obter(), podeGerenciar=pode_gerenciar,
            painelConfigurado=bool(cliente_painel and cliente_painel.configurado),
            filas=[{'key': k, 'nome': nomes.get(k, k.replace('_', ' '))} for k in chaves],
            agentes=agentes_retorno)
        resposta.headers['Cache-Control'] = 'no-store'
        return resposta

    @bp.route('/admin/agentes/layout', methods=['POST'], endpoint='agentes_layout')
    def salvar_layout():
        if not pode():
            return negar()
        try:
            novo = layout.salvar(request.get_json(silent=True), usuario())
        except (ValueError, TypeError, OverflowError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        registrar_acao_admin('agentes_layout', {'cols': novo.get('cols'), 'limite': novo.get('limite')})
        return jsonify(ok=True, layout=novo)

    @bp.route('/admin/agentes/<codigo>/tirar-pausa', methods=['POST'], endpoint='agentes_tirar_pausa')
    def tirar_pausa(codigo):
        try:
            codigo = _codigo(codigo)
        except ValueError as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not pode():
            return negar()
        if cliente_painel and cliente_painel.configurado:
            res = cliente_painel.executar('invert_pause_status', {'code': codigo, 'pause': '0'})
        else:
            res = cliente.post(f'/v2/agents/{codigo}/unpause')
        return responder_acao('tirar_pausa', codigo, res)

    @bp.route('/admin/agentes/<codigo>/colocar-pausa', methods=['POST'], endpoint='agentes_colocar_pausa')
    def colocar_pausa(codigo):
        try:
            codigo = _codigo(codigo)
        except ValueError as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not pode():
            return negar()
        if not (cliente_painel and cliente_painel.configurado):
            return jsonify(ok=False, mensagem='Painel NPX não configurado no servidor.'), 503
        res = cliente_painel.executar('invert_pause_status', {'code': codigo, 'pause': '1'})
        return responder_acao('colocar_pausa', codigo, res)

    @bp.route('/admin/agentes/<codigo>/remover-fila', methods=['POST'], endpoint='agentes_remover_fila')
    def remover_fila(codigo):
        try:
            codigo = _codigo(codigo)
        except ValueError as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not pode():
            return negar()
        dados = request.get_json(silent=True) or {}
        try:
            fila = _validar_fila(dados.get('fila'), coletor)
        except (ValueError, TypeError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not (cliente_painel and cliente_painel.configurado):
            return jsonify(ok=False, mensagem='Painel NPX não configurado no servidor.'), 503
        try:
            res = cliente_painel.executar('remove_agent', {'code': codigo, 'queue': fila})
        except (ValueError, TypeError, OverflowError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        return responder_acao('remover_fila', codigo, res)

    @bp.route('/admin/agentes/<codigo>/deslogar', methods=['POST'], endpoint='agentes_deslogar')
    def deslogar(codigo):
        try:
            codigo = _codigo(codigo)
        except ValueError as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not pode():
            return negar()
        return responder_acao('deslogar', codigo, cliente.post(f'/callcenter/{codigo}/logoff'))

    @bp.route('/admin/agentes/transferir', methods=['POST'], endpoint='agentes_transferir')
    def rota_transferir():
        dados = request.get_json(silent=True) or {}
        try:
            codigo = _codigo(dados.get('code'))
        except (ValueError, TypeError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not pode():
            return negar()
        try:
            res = transferir(cliente_painel, dados, coletor)
            return responder_acao('transferir', codigo, res)
        except (ValueError, TypeError, OverflowError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400

    @bp.route('/admin/agentes/prioridade', methods=['POST'], endpoint='agentes_prioridade')
    def rota_prioridade():
        dados = request.get_json(silent=True) or {}
        try:
            codigo = _codigo(dados.get('code'))
        except (ValueError, TypeError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        if not pode():
            return negar()
        try:
            res = alterar_prioridade(cliente_painel, dados, coletor)
            return responder_acao('prioridade', codigo, res)
        except (ValueError, TypeError, OverflowError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400

    return coletor
