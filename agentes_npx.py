"""Painel de Agentes do call center (NPX/Beatrix).

Consulta pública somente leitura em /agentes; quem tem a permissão ``perm_agentes``
(ou é Master) organiza o layout e executa ações: tirar pausa, transferir de fila,
mudar prioridade e deslogar.

A API da NPX responde uma consulta por vez (~0,4 s cada), então a coleta roda
continuamente numa thread e as páginas leem sempre o último retrato em memória.
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
from urllib import error as urlerror
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


def ids_filas():
    """ID interno de cada fila na NPX, exigido por add_to_queue/remove_to_queue."""
    return _lista_env('NPX_FILAS_IDS')


def _iso_para_datetime(valor):
    try:
        return datetime.fromisoformat(str(valor))
    except (TypeError, ValueError):
        return None


class ClienteNpx:
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
        status, texto = self._req('POST', caminho, corpo or {})
        try:
            obj = json.loads(texto)
        except ValueError:
            obj = None
        ok = 200 <= status < 300
        if ok and isinstance(obj, dict):
            if obj.get('error'):
                ok = False
            elif 'status' in obj and str(obj['status']) != '1':
                ok = False
        mensagem = (obj.get('message') or obj.get('error')) if isinstance(obj, dict) else None
        return {'ok': ok, 'http': status, 'mensagem': mensagem or texto[:300]}


class ColetorAgentes:
    def __init__(self, cliente):
        self.cliente = cliente
        self._trava = threading.Lock()
        self._thread = None
        self._parar = threading.Event()
        self._forcar = threading.Event()
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

    def forcar_releitura(self):
        self._forcar.set()

    def _laco(self):
        while not self._parar.is_set():
            try:
                self.coletar()
            except Exception as erro:   # mantém o último retrato válido e tenta de novo
                self.erro = str(erro)
                LOGGER.warning('Coleta NPX falhou: %s', erro)
            self._parar.wait(INTERVALO_COLETA)

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


def _id_fila(nome):
    ident = ids_filas().get(nome)
    if not ident:
        raise ValueError(f'A fila "{nomes_filas().get(nome, nome)}" ainda não tem o ID da NPX configurado (NPX_FILAS_IDS).')
    return ident


def transferir(cliente, dados):
    codigo, de, para = _codigo(dados.get('code')), str(dados.get('de') or ''), str(dados.get('para') or '')
    if de == para or not RE_FILA.match(de) or not RE_FILA.match(para):
        raise ValueError('Filas inválidas.')
    ramal = _codigo(dados.get('extension') or codigo)
    raw_pen = dados.get('penalty')
    if raw_pen in (None, ''):
        prioridade = 1
    else:
        try:
            prioridade = int(raw_pen)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('Prioridade deve ficar entre 0 e 99.')
        if not 0 <= prioridade <= 99:
            raise ValueError('Prioridade deve ficar entre 0 e 99.')
    id_de, id_para = _id_fila(de), _id_fila(para)      # valida as duas antes de mexer
    # inclui na nova antes de tirar da antiga: o agente nunca fica sem fila
    incluir = cliente.post(f'/v2/agents/{codigo}/add_to_queue', {'queue_id': id_para, 'extension': ramal, 'penalty': str(prioridade)})
    if not incluir['ok']:
        return {'ok': False, 'mensagem': f'Não incluiu na nova fila: {incluir["mensagem"]}'}
    remover = cliente.post(f'/v2/agents/{codigo}/remove_to_queue', {'queue_id': id_de})
    if not remover['ok']:
        return {'ok': False, 'mensagem': f'Incluído na nova fila, mas não saiu da anterior: {remover["mensagem"]}'}
    return {'ok': True, 'mensagem': 'Transferido.'}


def alterar_prioridade(cliente, dados):
    codigo, fila = _codigo(dados.get('code')), str(dados.get('fila') or '')
    if not RE_FILA.match(fila):
        raise ValueError('Fila inválida.')
    raw_pen = dados.get('penalty')
    try:
        prioridade = int(raw_pen)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Prioridade deve ficar entre 0 e 99.')
    if not 0 <= prioridade <= 99:
        raise ValueError('Prioridade deve ficar entre 0 e 99.')
    raw_pen_atual = dados.get('penaltyAtual')
    if raw_pen_atual in (None, ''):
        pen_atual = 1
    else:
        try:
            pen_atual = int(raw_pen_atual)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('Prioridade atual deve ficar entre 0 e 99.')
        if not 0 <= pen_atual <= 99:
            raise ValueError('Prioridade atual deve ficar entre 0 e 99.')
    ramal = _codigo(dados.get('extension') or codigo)
    id_fila = _id_fila(fila)
    corpo = {'queue_id': id_fila, 'extension': ramal, 'penalty': str(prioridade)}
    primeira = cliente.post(f'/v2/agents/{codigo}/add_to_queue', corpo)
    if primeira['ok']:
        return {'ok': True, 'mensagem': 'Prioridade alterada.'}
    # se a NPX não atualiza quem já está na fila: remove e inclui de novo, com volta em caso de falha
    if not cliente.post(f'/v2/agents/{codigo}/remove_to_queue', {'queue_id': id_fila})['ok']:
        return {'ok': False, 'mensagem': primeira['mensagem']}
    segunda = cliente.post(f'/v2/agents/{codigo}/add_to_queue', corpo)
    if segunda['ok']:
        return {'ok': True, 'mensagem': 'Prioridade alterada.'}
    volta = cliente.post(f'/v2/agents/{codigo}/add_to_queue', {**corpo, 'penalty': str(pen_atual)})
    aviso = 'o agente voltou para a fila' if volta['ok'] else 'ATENÇÃO: o agente ficou fora da fila'
    return {'ok': False, 'mensagem': f'Não aplicou ({segunda["mensagem"]}); {aviso}.'}


def configurar_agentes(bp, conectar, tem_permissao, cliente=None):
    cliente = cliente or ClienteNpx()
    coletor = ColetorAgentes(cliente)
    layout = LayoutAgentes(conectar)
    bp.coletor_agentes = coletor

    def pode():
        return bool(tem_permissao(PERMISSAO))

    def negar():
        return jsonify(ok=False, mensagem='Sem permissão para gerenciar agentes.'), 403

    def usuario():
        return session.get('user_nome') or str(session.get('user_id') or '?')

    def responder_acao(acao, codigo, resultado):
        registrar_acao_admin(f'agentes_{acao}', {'agente': codigo, 'ok': resultado.get('ok'),
                                                'mensagem': str(resultado.get('mensagem'))[:200]})
        coletor.forcar_releitura()
        return jsonify(ok=bool(resultado.get('ok')), mensagem=resultado.get('mensagem'))

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
            return jsonify(erro=erro or 'Sem resposta da NPX.'), 502
        nomes, ids = nomes_filas(), ids_filas()
        chaves = list(dict.fromkeys(list(nomes) + [a['queue'] for a in lista if a['queue']]))
        pode_gerenciar = pode()
        if pode_gerenciar:
            agentes_retorno = lista
        else:
            campos_privados = {'callSrc', 'callDst', 'callClient'}
            agentes_retorno = [{k: v for k, v in a.items() if k not in campos_privados} for a in lista]
        resposta = jsonify(
            atualizado=em.isoformat(), erroNpx=erro, layout=layout.obter(), podeGerenciar=pode_gerenciar,
            filas=[{'key': k, 'nome': nomes.get(k, k.replace('_', ' ')), 'temId': k in ids} for k in chaves],
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
        if not pode():
            return negar()
        try:
            codigo = _codigo(codigo)
        except ValueError as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        return responder_acao('tirar_pausa', codigo, cliente.post(f'/v2/agents/{codigo}/unpause'))

    @bp.route('/admin/agentes/<codigo>/deslogar', methods=['POST'], endpoint='agentes_deslogar')
    def deslogar(codigo):
        if not pode():
            return negar()
        try:
            codigo = _codigo(codigo)
        except ValueError as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400
        return responder_acao('deslogar', codigo, cliente.post(f'/callcenter/{codigo}/logoff'))

    @bp.route('/admin/agentes/transferir', methods=['POST'], endpoint='agentes_transferir')
    def rota_transferir():
        if not pode():
            return negar()
        dados = request.get_json(silent=True) or {}
        try:
            return responder_acao('transferir', dados.get('code'), transferir(cliente, dados))
        except (ValueError, TypeError, OverflowError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400

    @bp.route('/admin/agentes/prioridade', methods=['POST'], endpoint='agentes_prioridade')
    def rota_prioridade():
        if not pode():
            return negar()
        dados = request.get_json(silent=True) or {}
        try:
            return responder_acao('prioridade', dados.get('code'), alterar_prioridade(cliente, dados))
        except (ValueError, TypeError, OverflowError) as erro:
            return jsonify(ok=False, mensagem=str(erro)), 400

    return coletor
