import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from http import cookiejar
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error as urlerror

from flask import Blueprint, Flask

import agentes_npx
import seguranca
from agentes_npx import (
    ClienteNpx,
    ClientePainelNpx,
    ColetorAgentes,
    LayoutAgentes,
    SameOriginRedirectHandler,
    alterar_prioridade,
    configurar_agentes,
    nomes_filas,
    transferir,
    validar_layout,
)

RAIZ = Path(__file__).resolve().parent.parent


class MockResponse:
    def __init__(self, body, status=200, headers=None, url='https://app.npx.com.br/rates'):
        self.body = body.encode('utf-8') if isinstance(body, str) else body
        self.status = status
        self.headers = headers or {}
        self.url = url

    def read(self, *args):
        return self.body

    def geturl(self):
        return self.url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ClienteFalso(ClienteNpx):
    """Responde como a API Beatrix da NPX a partir de um dicionário, registrando os POSTs."""
    def __init__(self, respostas=None, posts=None):
        super().__init__(base='https://npx.invalid/api', token='teste')
        self.respostas = respostas or {}
        self.respostas_post = posts or {}
        self.enviados = []

    def get(self, caminho):
        for prefixo, valor in self.respostas.items():
            if caminho.startswith(prefixo):
                return valor
        raise RuntimeError('sem resposta para ' + caminho)

    def post(self, caminho, corpo=None):
        self.enviados.append((caminho, corpo or {}))
        for trecho, valor in self.respostas_post.items():
            if trecho in caminho:
                return valor
        return {'ok': True, 'http': 200, 'mensagem': 'Sucesso'}


class ClientePainelFalso:
    """Mock do ClientePainelNpx registrando chamadas de executar()."""
    def __init__(self, configurado=True, respostas=None):
        self._configurado = configurado
        self.respostas = respostas or {}
        self.executados = []

    @property
    def configurado(self):
        return self._configurado

    def executar(self, acao, parametros):
        self.executados.append((acao, dict(parametros)))
        if not self._configurado:
            return {'ok': False, 'http': 503, 'mensagem': 'Painel NPX não configurado no servidor.'}
        for chave, resp in self.respostas.items():
            if chave == acao or chave in str(parametros):
                return resp
        return {'ok': True, 'http': 200, 'mensagem': f'Ação {acao} executada com sucesso.'}


def status(nome, st=1, paused=0, duration=60, penalty=2, src=''):
    return {'name': nome, 'status': st, 'paused': paused, 'duration': duration, 'penalty': penalty,
            'extension': None, 'src': src, 'dst': '', 'current_call': {'client_name': src}}


REG = '/call_center_reports/report_register_points_details'


class ColetorTest(unittest.TestCase):
    def test_sessoes_abertas_de_dias_anteriores_e_encerradas_hoje(self):
        historico = [
            {'code': '4422000', 'agent_name': 'Ana', 'queue': 'InfoBrasil_Comercial', 'logon_date': '2026-10-28T11:23:15-03:00', 'logoff_date': None},
            {'code': '4422005', 'agent_name': 'Saiu', 'queue': 'InfoBrasil_Suporte', 'logon_date': '2026-11-06T07:00:00-03:00', 'logoff_date': None},
            {'code': '4422009', 'agent_name': 'Antigo', 'queue': 'InfoBrasil_Suporte', 'logon_date': '2026-10-01T07:00:00-03:00', 'logoff_date': '2026-10-01T18:00:00-03:00'},
        ]
        hoje = [
            {'code': '4422005', 'agent_name': 'Saiu', 'queue': 'InfoBrasil_Suporte', 'logon_date': '2026-11-06T07:00:00-03:00', 'logoff_date': '2026-11-06T08:00:00-03:00'},
            {'code': '4422001', 'agent_name': 'Darkison', 'queue': 'InfoBrasil_Suporte', 'logon_date': '2026-11-06T07:57:08-03:00', 'logoff_date': None},
        ]
        cliente = ClienteFalso({
            REG + '?date_started_at': {'report_data': historico},
            REG + '?per_page': {'report_data': hoje},
            '/call_center_reports/report_pause_details': {'report_data': [
                {'code': '4422001', 'agent_name': 'Darkison', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
                 'pause_start': '2026-11-06T08:00:00-03:00', 'pause_finish': None}]},
            '/pabx/4422000/agent_status': status('Ana'),
            '/pabx/4422001/agent_status': status('Darkison', st=-1, paused=1),
        })
        lista = ColetorAgentes(cliente).coletar()
        por_codigo = {a['code']: a for a in lista}
        self.assertEqual(set(por_codigo), {'4422000', '4422001'})
        self.assertEqual(por_codigo['4422000']['queue'], 'InfoBrasil_Comercial')
        self.assertTrue(por_codigo['4422001']['paused'])
        self.assertEqual(por_codigo['4422001']['pauseType'], 'Almoço')
        self.assertIsNotNone(por_codigo['4422001']['pauseMinutes'])

    def test_status_indisponivel_nao_derruba_a_coleta(self):
        cliente = ClienteFalso({
            REG + '?date_started_at': {'report_data': []},
            REG + '?per_page': {'report_data': [
                {'code': '4422001', 'agent_name': 'D', 'queue': 'InfoBrasil_Suporte', 'logon_date': '2026-11-06T07:00:00-03:00', 'logoff_date': None}]},
            '/call_center_reports/report_pause_details': {'report_data': []},
        })
        lista = ColetorAgentes(cliente).coletar()
        self.assertEqual(lista[0]['erro'], 'status indisponível')
        self.assertIsNone(lista[0]['status'])

    def test_post_interpreta_erro_com_http_200(self):
        cliente = ClienteNpx(base='https://npx.invalid/api', token='t')
        with patch.object(ClienteNpx, '_req', return_value=(200, '{"status":0,"error":"undefined method"}')):
            self.assertFalse(cliente.post('/x')['ok'])
        with patch.object(ClienteNpx, '_req', return_value=(200, '{"message":"Sucesso","status":1}')):
            self.assertTrue(cliente.post('/x')['ok'])

    def test_forcar_releitura_acorda_coletor(self):
        cliente = ClienteFalso()
        coletor = ColetorAgentes(cliente)
        coletor.forcar_releitura()
        self.assertTrue(coletor._forcar.is_set())
        self.assertTrue(coletor._acordar.is_set())

    def test_coletor_erro_publico_generico_sem_vazar_exception(self):
        cliente = ClienteFalso()
        coletor = ColetorAgentes(cliente)
        with patch.object(coletor, 'coletar', side_effect=RuntimeError('Sensivel: token_privado_xyz')):
            try:
                coletor.coletar()
            except Exception:
                coletor.erro = 'Coleta de agentes indisponível no momento.'
        self.assertEqual(coletor.erro, 'Coleta de agentes indisponível no momento.')
        self.assertNotIn('token_privado_xyz', coletor.erro)

    def test_normalizar_max_minutes_valores_validos_e_invalidos(self):
        # 12.7 não truncado (float válido)
        self.assertEqual(agentes_npx._normalizar_max_minutes(12.7), 12.7)
        self.assertEqual(agentes_npx._normalizar_max_minutes('12.7'), 12.7)
        # Inteiros válidos
        self.assertEqual(agentes_npx._normalizar_max_minutes(0), 0)
        self.assertEqual(agentes_npx._normalizar_max_minutes('0'), 0)
        self.assertEqual(agentes_npx._normalizar_max_minutes(60), 60)
        self.assertEqual(agentes_npx._normalizar_max_minutes('45'), 45)
        # Inválidos retornam None
        for inv in (None, True, False, -1, -12.7, '-10', 'abc', float('inf'), float('-inf'), float('nan')):
            self.assertIsNone(agentes_npx._normalizar_max_minutes(inv), f'Falhou para {inv}')

    def test_hist60_mais_hoje_null_resulta_em_none(self):
        sessoes = [{'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte',
                    'logon_date': '2026-11-06T08:00:00-03:00', 'logoff_date': None}]
        hist_pausas = [
            {'pause': 'Almoço', 'max_minutes': 60, 'pause_start': '2026-10-25T12:00:00-03:00'},
        ]
        hoje_pausas = [
            # Pausa anterior encerrada hoje com max_minutes nulo
            {'code': '4422002', 'agent_name': 'A2', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
             'pause_start': '2026-11-06T09:00:00-03:00', 'pause_finish': '2026-11-06T10:00:00-03:00', 'max_minutes': None},
            # Pausa aberta atual do agente sem o campo max_minutes (campo ausente obriga consulta ao cache)
            {'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
             'pause_start': '2026-11-06T12:00:00-03:00', 'pause_finish': None},
        ]
        cliente = ClienteFalso({
            REG: {'report_data': sessoes},
            '/call_center_reports/report_pause_details?date_started_at': {'report_data': hist_pausas},
            '/call_center_reports/report_pause_details?per_page': {'report_data': hoje_pausas},
            '/pabx/4422001/agent_status': status('A1', paused=1),
        })
        coletor = ColetorAgentes(cliente)
        lista = coletor.coletar()
        self.assertEqual(lista[0]['pauseType'], 'Almoço')
        self.assertIn('Almoço', coletor._limites_cache)
        self.assertIsNone(coletor._limites_cache['Almoço'])
        self.assertIsNone(lista[0]['pauseMaxMinutes'])

    def test_hoje_dois_mesmo_tipo_mais_recente_null_vence(self):
        sessoes = [{'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte',
                    'logon_date': '2026-11-06T08:00:00-03:00', 'logoff_date': None}]
        # Fornecidos fora de ordem cronológica: mais recente (null) primeiro, mais antigo (60) depois
        hoje_pausas = [
            {'code': '4422002', 'agent_name': 'A2', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
             'pause_start': '2026-11-06T11:00:00-03:00', 'pause_finish': '2026-11-06T12:00:00-03:00', 'max_minutes': None},
            {'code': '4422003', 'agent_name': 'A3', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
             'pause_start': '2026-11-06T08:00:00-03:00', 'pause_finish': '2026-11-06T09:00:00-03:00', 'max_minutes': 60},
            # Pausa aberta do agente sem o campo max_minutes (obriga consulta ao cache)
            {'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
             'pause_start': '2026-11-06T12:30:00-03:00', 'pause_finish': None},
        ]
        cliente = ClienteFalso({
            REG: {'report_data': sessoes},
            '/call_center_reports/report_pause_details?date_started_at': {'report_data': []},
            '/call_center_reports/report_pause_details?per_page': {'report_data': hoje_pausas},
            '/pabx/4422001/agent_status': status('A1', paused=1),
        })
        coletor = ColetorAgentes(cliente)
        lista = coletor.coletar()
        por_cod = {a['code']: a for a in lista}
        self.assertEqual(por_cod['4422001']['pauseType'], 'Almoço')
        self.assertIn('Almoço', coletor._limites_cache)
        self.assertIsNone(coletor._limites_cache['Almoço'])
        self.assertIsNone(por_cod['4422001']['pauseMaxMinutes'])

    def test_historico_fora_de_ordem_preserva_null_mais_recente(self):
        sessoes = [{'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte',
                    'logon_date': '2026-11-06T08:00:00-03:00', 'logoff_date': None}]
        # Fornecidos fora de ordem cronológica: mais recente (null) primeiro, mais antigo (60) depois
        hist_pausas = [
            {'pause': 'Almoço', 'max_minutes': None, 'pause_start': '2026-10-25T12:00:00-03:00'},
            {'pause': 'Almoço', 'max_minutes': 60, 'pause_start': '2026-10-20T12:00:00-03:00'},
        ]
        hoje_pausas = [
            # Pausa aberta com campo max_minutes ausente (obriga consulta ao cache)
            {'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
             'pause_start': '2026-11-06T12:00:00-03:00', 'pause_finish': None},
        ]
        cliente = ClienteFalso({
            REG: {'report_data': sessoes},
            '/call_center_reports/report_pause_details?date_started_at': {'report_data': hist_pausas},
            '/call_center_reports/report_pause_details?per_page': {'report_data': hoje_pausas},
            '/pabx/4422001/agent_status': status('A1', paused=1),
        })
        coletor = ColetorAgentes(cliente)
        lista = coletor.coletar()
        por_cod = {a['code']: a for a in lista}
        self.assertEqual(por_cod['4422001']['pauseType'], 'Almoço')
        self.assertIn('Almoço', coletor._limites_hist)
        self.assertIsNone(coletor._limites_hist['Almoço'])
        self.assertIn('Almoço', coletor._limites_cache)
        self.assertIsNone(coletor._limites_cache['Almoço'])
        self.assertIsNone(por_cod['4422001']['pauseMaxMinutes'])

    def test_aberta_null_com_cache_positivo_resulta_em_none(self):
        sessoes = [{'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte',
                    'logon_date': '2026-11-06T08:00:00-03:00', 'logoff_date': None}]
        cliente = ClienteFalso({
            REG: {'report_data': sessoes},
            '/call_center_reports/report_pause_details?date_started_at': {'report_data': [{'pause': 'Almoço', 'max_minutes': 60}]},
            '/call_center_reports/report_pause_details?per_page': {'report_data': [
                {'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte', 'pause': 'Almoço',
                 'pause_start': '2026-11-06T12:00:00-03:00', 'pause_finish': None, 'max_minutes': None}
            ]},
            '/pabx/4422001/agent_status': status('A1', paused=1),
        })
        coletor = ColetorAgentes(cliente)
        lista = coletor.coletar()
        self.assertEqual(lista[0]['pauseType'], 'Almoço')
        self.assertIsNone(lista[0]['pauseMaxMinutes'])

    def test_aberta_campo_ausente_coberta_pelo_cache_historico_60(self):
        sessoes = [{'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte',
                    'logon_date': '2026-11-06T08:00:00-03:00', 'logoff_date': None}]
        aberta_sem_campo = {
            'code': '4422001', 'agent_name': 'A1', 'queue': 'InfoBrasil_Suporte',
            'pause': 'Almoço', 'pause_start': '2026-11-06T12:00:00-03:00', 'pause_finish': None
        }
        self.assertNotIn('max_minutes', aberta_sem_campo)
        cliente = ClienteFalso({
            REG: {'report_data': sessoes},
            '/call_center_reports/report_pause_details?date_started_at': {'report_data': [{'pause': 'Almoço', 'max_minutes': 60}]},
            '/call_center_reports/report_pause_details?per_page': {'report_data': [aberta_sem_campo]},
            '/pabx/4422001/agent_status': status('A1', paused=1),
        })
        coletor = ColetorAgentes(cliente)
        lista = coletor.coletar()
        self.assertEqual(lista[0]['pauseType'], 'Almoço')
        self.assertEqual(lista[0]['pauseMaxMinutes'], 60)

    def test_falha_30_dias_marca_timestamp_e_aguarda_60s(self):
        chamadas_30d = 0
        def get_hook(caminho):
            nonlocal chamadas_30d
            if 'date_started_at' in caminho and 'report_pause_details' in caminho:
                chamadas_30d += 1
                raise RuntimeError('Erro de rede 30 dias')
            return {'report_data': []}

        cliente = MagicMock()
        cliente.configurado = True
        cliente.get.side_effect = get_hook

        coletor = ColetorAgentes(cliente)
        coletor._limites_hist = {'Almoço': 60}
        coletor.coletar()

        self.assertEqual(chamadas_30d, 1)
        self.assertEqual(coletor._limites_hist, {'Almoço': 60})
        t1 = coletor._limites_hist_em
        self.assertGreater(t1, 0)

        # Segunda coleta dentro de 60s não repete a chamada com falha
        coletor.coletar()
        self.assertEqual(chamadas_30d, 1)

    def test_forcar_releitura_nao_refaz_30_dias_de_limites(self):
        chamadas_30d = 0
        def get_hook(caminho):
            nonlocal chamadas_30d
            if 'date_started_at' in caminho and 'report_pause_details' in caminho:
                chamadas_30d += 1
            return {'report_data': [{'pause': 'Almoço', 'max_minutes': 60}]}

        cliente = MagicMock()
        cliente.configurado = True
        cliente.get.side_effect = get_hook

        coletor = ColetorAgentes(cliente)
        coletor.coletar()
        self.assertEqual(chamadas_30d, 1)

        # forcar_releitura() atualiza estado de hoje, mas preserva cache de 60s
        coletor.forcar_releitura()
        coletor.coletar()
        self.assertEqual(chamadas_30d, 1)


class ClientePainelNpxTest(unittest.TestCase):
    def test_painel_url_requer_https_sem_credenciais_e_default_exato(self):
        painel_padrao = ClientePainelNpx(base='', email='u@x.com', senha='123')
        self.assertEqual(painel_padrao.base, 'https://app.npx.com.br')
        self.assertTrue(painel_padrao.configurado)

        painel_http = ClientePainelNpx(base='http://app.npx.com.br', email='u@x.com', senha='123')
        self.assertFalse(painel_http.configurado)

        painel_creds = ClientePainelNpx(base='https://user:pass@app.npx.com.br', email='u@x.com', senha='123')
        self.assertFalse(painel_creds.configurado)

        painel_query = ClientePainelNpx(base='https://app.npx.com.br?foo=bar', email='u@x.com', senha='123')
        self.assertFalse(painel_query.configurado)

        self.assertFalse(ClientePainelNpx(base='https://app.npx.com.br', email='', senha='123').configurado)
        self.assertFalse(ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='').configurado)

    def test_autenticacao_devise_extrai_token_e_grava_cookies(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='user@teste.com', senha='secretpassword')
        html_login = '<html><form action="/users/sign_in"><input type="hidden" name="authenticity_token" value="csrf_abc_123"></form></html>'

        respostas = [
            MockResponse(html_login, status=200, url='https://app.npx.com.br/users/sign_in'),
            MockResponse('<html>Dashboard</html>', status=200, url='https://app.npx.com.br/rates/monitor'),
        ]

        with patch.object(painel, '_criar_opener') as mock_criar:
            mock_opener = MagicMock()
            mock_opener.open.side_effect = lambda req, timeout=None: respostas.pop(0)
            mock_criar.return_value = mock_opener

            self.assertTrue(painel.autenticar())
            self.assertTrue(painel._autenticado)
            self.assertEqual(painel._token_csrf, 'csrf_abc_123')
            self.assertEqual(mock_opener.open.call_count, 2)
            req_post = mock_opener.open.call_args_list[1][0][0]
            corpo_post = req_post.data.decode('utf-8')
            self.assertIn('authenticity_token=csrf_abc_123', corpo_post)
            self.assertIn('user%5Bemail%5D=user%40teste.com', corpo_post)

    def test_autenticacao_redirect_fora_da_origem_bloqueado(self):
        handler = SameOriginRedirectHandler('https://app.npx.com.br')
        from urllib.request import Request
        req = Request('https://app.npx.com.br/users/sign_in')
        with self.assertRaises(urlerror.HTTPError) as ctx:
            handler.redirect_request(req, None, 302, 'Found', {}, 'https://malicious.com/steal')
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

        with self.assertRaises(urlerror.HTTPError) as ctx_creds:
            handler.redirect_request(req, None, 302, 'Found', {}, 'https://user:pass@app.npx.com.br/safe')
        self.assertEqual(ctx_creds.exception.code, 403)
        ctx_creds.exception.close()

        with self.assertRaises(urlerror.HTTPError) as ctx_http:
            handler.redirect_request(req, None, 302, 'Found', {}, 'http://app.npx.com.br/insecure')
        self.assertEqual(ctx_http.exception.code, 403)
        ctx_http.exception.close()

    def test_login_errado_e_cooldown(self):
        relogio_simulado = 1000.0
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='err')
        painel._clock = lambda: relogio_simulado

        html_login = '<input name="authenticity_token" value="tk1">'
        respostas = [
            MockResponse(html_login, status=200, url='https://app.npx.com.br/users/sign_in'),
            MockResponse('<form class="new_user">Invalid</form>', status=200, url='https://app.npx.com.br/users/sign_in'),
        ]
        with patch.object(painel, '_criar_opener') as mock_criar:
            mock_opener = MagicMock()
            mock_opener.open.side_effect = lambda req, timeout=None: respostas.pop(0)
            mock_criar.return_value = mock_opener

            res1 = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertFalse(res1['ok'])
            self.assertEqual(res1['http'], 502)
            self.assertFalse(painel._autenticado)
            self.assertEqual(painel._login_falha_em, 1000.0)

            relogio_simulado = 1010.0
            chamadas_antes = mock_opener.open.call_count
            res2 = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertFalse(res2['ok'])
            self.assertEqual(res2['http'], 502)
            self.assertIn('temporariamente suspensa', res2['mensagem'])
            self.assertEqual(mock_opener.open.call_count, chamadas_antes)

            relogio_simulado = 1065.0
            respostas.append(MockResponse(html_login, status=200, url='https://app.npx.com.br/users/sign_in'))
            respostas.append(MockResponse('<form class="new_user">Invalid</form>', status=200, url='https://app.npx.com.br/users/sign_in'))
            res3 = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertFalse(res3['ok'])
            self.assertGreater(mock_opener.open.call_count, chamadas_antes)

    def test_urlerror_e_excecoes_de_rede_retornam_resultado_seguro_502(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        painel._opener = MagicMock()
        painel._opener.open.side_effect = urlerror.URLError('Connection refused host:123')

        res = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
        self.assertFalse(res['ok'])
        self.assertEqual(res['http'], 502)
        self.assertEqual(res['mensagem'], 'Falha na comunicação com o painel NPX.')

    def test_concorrencia_duas_threads_uma_reautenticacao(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        # Inicia pré-autenticado com sessão antiga expirada
        painel._autenticado = True

        html_login = '<input name="authenticity_token" value="tk_conc">'
        chamadas_login_get = 0
        chamadas_login_post = 0
        geracao_sessao = 0
        lock_cont = threading.Lock()

        def fake_open(req, timeout=None):
            nonlocal chamadas_login_get, chamadas_login_post, geracao_sessao
            url = req.full_url
            if '/users/sign_in' in url:
                with lock_cont:
                    if req.data:
                        chamadas_login_post += 1
                        time.sleep(0.02)
                        geracao_sessao += 1
                        return MockResponse('<html>OK</html>', status=200, url='https://app.npx.com.br/rates/monitor')
                    chamadas_login_get += 1
                    time.sleep(0.02)
                    return MockResponse(html_login, status=200, url='https://app.npx.com.br/users/sign_in')

            with lock_cont:
                g = geracao_sessao
            if g == 0:
                # Sessão antiga expirada responde HTTP 401
                raise urlerror.HTTPError(url, 401, 'Unauthorized', {}, None)
            return MockResponse('{"status": 1, "message": "OK"}', status=200)

        mock_opener = MagicMock()
        mock_opener.open.side_effect = fake_open
        painel._opener = mock_opener

        resultados = []
        lock_res = threading.Lock()

        def worker(code, queue):
            r = painel.executar('move_agent', {'code': code, 'queue': queue})
            with lock_res:
                resultados.append(r)

        with patch.object(painel, '_criar_opener', return_value=mock_opener):
            t1 = threading.Thread(target=worker, args=('4422001', 'InfoBrasil_Suporte'))
            t2 = threading.Thread(target=worker, args=('4422002', 'InfoBrasil_Comercial'))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(len(resultados), 2)
            self.assertTrue(resultados[0]['ok'])
            self.assertTrue(resultados[1]['ok'])
            # Exatamente uma reautenticação com 1 GET e 1 POST
            self.assertEqual(chamadas_login_get, 1)
            self.assertEqual(chamadas_login_post, 1)
            self.assertEqual(chamadas_login_get + chamadas_login_post, 2)
            self.assertTrue(painel._autenticado)

    def test_executar_acao_move_agent(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        painel._opener = MagicMock()
        painel._opener.open.return_value = MockResponse('{"status": 1, "message": "Agente transferido"}', status=200)

        res = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Comercial'})
        self.assertTrue(res['ok'])
        self.assertEqual(res['mensagem'], 'Agente transferido')
        req = painel._opener.open.call_args[0][0]
        self.assertIn('/rates/move_agent?code=4422001&queue=InfoBrasil_Comercial', req.full_url)

    def test_executar_acao_update_penalty_valida_int_estrito_1_a_10(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        painel._opener = MagicMock()
        painel._opener.open.return_value = MockResponse('{"status": 1, "message": "Prioridade alterada"}', status=200)

        r1 = painel.executar('update_penalty', {'code': '4422001', 'queue': 'InfoBrasil_Suporte', 'penalty': 1})
        self.assertTrue(r1['ok'])
        r10 = painel.executar('update_penalty', {'code': '4422001', 'queue': 'InfoBrasil_Suporte', 'penalty': 10})
        self.assertTrue(r10['ok'])

        for ruim in (0, 11, -1, True, False, 5.5, '5', float('inf')):
            with self.assertRaises(ValueError):
                painel.executar('update_penalty', {'code': '4422001', 'queue': 'InfoBrasil_Suporte', 'penalty': ruim})

    def test_executar_acao_invert_pause_status(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        painel._opener = MagicMock()
        painel._opener.open.return_value = MockResponse('{"status": 1, "message": "Pausa invertida"}', status=200)

        r_pausar = painel.executar('invert_pause_status', {'code': '4422001', 'pause': '1'})
        self.assertTrue(r_pausar['ok'])
        req = painel._opener.open.call_args[0][0]
        self.assertIn('/rates/invert_pause_status?code=4422001&pause=1', req.full_url)

        r_despausar = painel.executar('invert_pause_status', {'code': '4422001', 'pause': '0'})
        self.assertTrue(r_despausar['ok'])
        req2 = painel._opener.open.call_args[0][0]
        self.assertIn('/rates/invert_pause_status?code=4422001&pause=0', req2.full_url)

        for ruim in (True, False, 2, 'pause', None):
            with self.assertRaises(ValueError):
                painel.executar('invert_pause_status', {'code': '4422001', 'pause': ruim})

    def test_executar_acao_remove_agent(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        painel._opener = MagicMock()
        painel._opener.open.return_value = MockResponse('{"status": 1, "message": "Agente removido"}', status=200)

        res = painel.executar('remove_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
        self.assertTrue(res['ok'])
        req = painel._opener.open.call_args[0][0]
        # A spec externa do NPX rates/remove_agent usa SOMENTE code na query string
        self.assertEqual(req.full_url, 'https://app.npx.com.br/rates/remove_agent?code=4422001')

    def test_executar_relogin_e_retry_quando_sessao_expirada(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        mock_opener = MagicMock()

        respostas = [
            MockResponse('<html>Sessão expirada</html>', status=401, url='https://app.npx.com.br/rates/move_agent'),
            MockResponse('<input name="authenticity_token" value="tk2">', status=200, url='https://app.npx.com.br/users/sign_in'),
            MockResponse('<html>Dashboard</html>', status=200, url='https://app.npx.com.br/rates/monitor'),
            MockResponse('{"status": 1, "message": "Sucesso após retry"}', status=200, url='https://app.npx.com.br/rates/move_agent'),
        ]
        mock_opener.open.side_effect = lambda req, timeout=None: respostas.pop(0)
        painel._opener = mock_opener

        with patch.object(painel, '_criar_opener', return_value=mock_opener):
            res = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertTrue(res['ok'])
            self.assertEqual(res['mensagem'], 'Sucesso após retry')

    def test_executar_nao_repete_mais_de_uma_vez_se_falhar(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        mock_opener = MagicMock()

        respostas = [
            MockResponse('<html>401</html>', status=401),
            MockResponse('<input name="authenticity_token" value="tk2">', status=200),
            MockResponse('<html>Dashboard</html>', status=200),
            MockResponse('<html>Erro persistente</html>', status=500),
        ]
        mock_opener.open.side_effect = lambda req, timeout=None: respostas.pop(0)
        painel._opener = mock_opener

        with patch.object(painel, '_criar_opener', return_value=mock_opener):
            res = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertFalse(res['ok'])
            self.assertEqual(res['mensagem'], 'Resposta inválida do painel NPX.')
            self.assertEqual(res['http'], 502)

    def test_sanitizacao_remove_tags_colapsa_espacos_max200_e_redige_segredos(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='admin@empresa.com', senha='super_secret_password')
        painel._autenticado = True
        painel._token_csrf = 'csrf_token_muito_longo_12345'
        painel._opener = MagicMock()

        # Cookie longo no CookieJar e cookie curto 'ok'
        c_longo = cookiejar.Cookie(version=0, name='_session', value='cookie_longo_super_secreto', port=None, port_specified=False,
                                   domain='app.npx.com.br', domain_specified=True, domain_initial_dot=False,
                                   path='/', path_specified=True, secure=True, expires=None, discard=False,
                                   comment=None, comment_url=None, rest={}, rfc2109=False)
        c_curto = cookiejar.Cookie(version=0, name='pt', value='ok', port=None, port_specified=False,
                                   domain='app.npx.com.br', domain_specified=True, domain_initial_dot=False,
                                   path='/', path_specified=True, secure=True, expires=None, discard=False,
                                   comment=None, comment_url=None, rest={}, rfc2109=False)
        painel._jar.set_cookie(c_longo)
        painel._jar.set_cookie(c_curto)

        texto_remoto = (
            '  <div>status ok: Erro <span class="badge">grave</span>:  admin@empresa.com '
            'e senha super_secret_password com token csrf_token_muito_longo_12345 '
            'e cookie: cookie_longo_super_secreto com sucesso!  </div> '
            + 'x' * 300
        )
        painel._opener.open.return_value = MockResponse(json.dumps({'error': texto_remoto}), status=400)

        with patch.dict(os.environ, {'NPX_API_TOKEN': 'secret_api_token_456'}):
            res = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertFalse(res['ok'])
            msg = res['mensagem']

            # Tags HTML removidas
            self.assertNotIn('<div>', msg)
            self.assertNotIn('</span>', msg)
            # Múltiplos espaços colapsados
            self.assertNotIn('  ', msg)
            # Tamanho máximo 200 caracteres
            self.assertLessEqual(len(msg), 200)
            # Segredos redigidos
            self.assertNotIn('admin@empresa.com', msg)
            self.assertNotIn('super_secret_password', msg)
            self.assertNotIn('csrf_token_muito_longo_12345', msg)
            self.assertNotIn('cookie_longo_super_secreto', msg)
            self.assertIn('***', msg)
            # Palavras legítimas preservadas no trecho inicial mesmo com cookie curto 'ok'
            self.assertIn('status ok', msg)
            self.assertIn('sucesso', msg)

    def test_cliente_npx_post_captura_value_error_com_log_sanitizado(self):
        cliente = ClienteNpx(base='https://app.npxtech.com.br/api', token='token_secreto_npx_123')
        with patch.object(cliente, '_req', side_effect=ValueError('Erro com token_secreto_npx_123')):
            with self.assertLogs('agentes_npx', level='WARNING') as cm:
                res = cliente.post('/v2/agents/4422001/unpause')
                self.assertFalse(res['ok'])
                self.assertEqual(res['http'], 502)
                self.assertIn('Falha na comunicação com a API NPX.', res['mensagem'])
                log_completo = '\n'.join(cm.output)
                self.assertIn('ValueError', log_completo)
                self.assertNotIn('token_secreto_npx_123', log_completo)

    def test_logs_usam_apenas_tipo_de_erro_sem_vazar_credenciais(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='secreto@empresa.com', senha='senha_ultrasecreta')
        painel._autenticado = True
        painel._opener = MagicMock()
        # Exceção contendo senha e token na mensagem
        excecao_sensivel = urlerror.URLError('Falha com senha_ultrasecreta e token_privado_999')
        painel._opener.open.side_effect = excecao_sensivel

        with self.assertLogs('agentes_npx', level='WARNING') as cm:
            res = painel.executar('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Suporte'})
            self.assertFalse(res['ok'])
            self.assertEqual(res['http'], 502)

        # Nenhum log contém a senha ou token em claro
        registro = ' '.join(cm.output)
        self.assertNotIn('senha_ultrasecreta', registro)
        self.assertNotIn('secreto@empresa.com', registro)
        self.assertNotIn('token_privado_999', registro)
        # Contém o nome da classe do erro
        self.assertIn('URLError', registro)

    def test_cliente_npx_post_captura_urlerror_e_nao_html(self):
        cliente = ClienteNpx(base='https://npx.invalid/api', token='tok_12345678')

        with patch.object(cliente, '_req', side_effect=urlerror.URLError('Falha DNS')):
            r_err = cliente.post('/v2/agents/4422001/unpause')
            self.assertFalse(r_err['ok'])
            self.assertEqual(r_err['http'], 502)
            self.assertEqual(r_err['mensagem'], 'Falha na comunicação com a API NPX.')

        with patch.object(cliente, '_req', return_value=(502, '<html><head><title>502 Bad Gateway</title></head><body>Bad Gateway</body></html>')):
            r_html = cliente.post('/v2/agents/4422001/unpause')
            self.assertFalse(r_html['ok'])
            self.assertEqual(r_html['http'], 502)
            self.assertEqual(r_html['mensagem'], 'Falha na operação na API NPX.')
            self.assertNotIn('<html>', r_html['mensagem'])

        # Captura Exception genérica (ex: ValueError) e retorna 502
        with patch.object(cliente, '_req', side_effect=ValueError('Payload corrompido')):
            r_val = cliente.post('/v2/agents/4422001/unpause')
            self.assertFalse(r_val['ok'])
            self.assertEqual(r_val['http'], 502)
            self.assertEqual(r_val['mensagem'], 'Falha na comunicação com a API NPX.')


class AcoesTest(unittest.TestCase):
    def test_transferir_valida_fila_e_chama_painel(self):
        painel = ClientePainelFalso()
        res = transferir(painel, {'code': '4422001', 'para': 'InfoBrasil_Comercial'})
        self.assertTrue(res['ok'])
        self.assertEqual(painel.executados, [('move_agent', {'code': '4422001', 'queue': 'InfoBrasil_Comercial'})])

        for fila_invalida in ('', '   ', 'fila com espacos', 'FilaDesconhecida', '<script>'):
            with self.assertRaises(ValueError):
                transferir(painel, {'code': '4422001', 'para': fila_invalida})

    def test_transferir_e_prioridade_aceitam_fila_conhecida_apenas_no_snapshot(self):
        painel = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel._autenticado = True
        mock_opener = MagicMock()
        mock_opener.open.return_value = MockResponse('{"status": 1, "message": "OK"}', status=200)
        painel._opener = mock_opener

        coletor = MagicMock()
        coletor.retrato = [{'code': '4422009', 'queue': 'FilaEspecial_Temporaria'}]

        # Fila que não está no nomes_filas(), mas existe no snapshot do coletor:
        # ClientePainelNpx REAL com opener mock deve aceitar a fila especial sem erro
        res1 = transferir(painel, {'code': '4422001', 'para': 'FilaEspecial_Temporaria'}, coletor=coletor)
        self.assertTrue(res1['ok'])
        req1 = painel._opener.open.call_args[0][0]
        self.assertEqual(req1.full_url, 'https://app.npx.com.br/rates/move_agent?code=4422001&queue=FilaEspecial_Temporaria')

        res2 = alterar_prioridade(painel, {'code': '4422001', 'fila': 'FilaEspecial_Temporaria', 'penalty': 4}, coletor=coletor)
        self.assertTrue(res2['ok'])
        req2 = painel._opener.open.call_args[0][0]
        self.assertEqual(req2.full_url, 'https://app.npx.com.br/rates/update_penalty?code=4422001&penalty=4&queue=FilaEspecial_Temporaria')

    def test_alterar_prioridade_valida_int_estrito_1_a_10_e_chama_painel(self):
        painel = ClientePainelFalso()
        res = alterar_prioridade(painel, {'code': '4422001', 'fila': 'InfoBrasil_Suporte', 'penalty': 5})
        self.assertTrue(res['ok'])
        self.assertEqual(painel.executados, [('update_penalty', {'code': '4422001', 'queue': 'InfoBrasil_Suporte', 'penalty': 5})])

        for pen_invalido in (0, 11, -1, True, False, 5.5, '5', float('inf')):
            with self.assertRaises(ValueError):
                alterar_prioridade(painel, {'code': '4422001', 'fila': 'InfoBrasil_Suporte', 'penalty': pen_invalido})

    def test_transferir_e_prioridade_retornam_503_quando_painel_desconfigurado(self):
        painel_desconfigurado = ClientePainelFalso(configurado=False)
        r1 = transferir(painel_desconfigurado, {'code': '4422001', 'para': 'InfoBrasil_Comercial'})
        self.assertFalse(r1['ok'])
        self.assertEqual(r1['http'], 503)

        r2 = alterar_prioridade(painel_desconfigurado, {'code': '4422001', 'fila': 'InfoBrasil_Suporte', 'penalty': 3})
        self.assertFalse(r2['ok'])
        self.assertEqual(r2['http'], 503)

    def test_validar_layout(self):
        self.assertEqual(validar_layout({'cols': 2, 'c1': ['A', 'A'], 'c2': ['A', 'B'], 'ord': {'col': 'tempo', 'dir': -1}, 'limite': 5}),
                         {'cols': 2, 'c1': ['A'], 'c2': ['B'], 'ord': {'col': 'tempo', 'dir': -1}})
        for ruim in ({'cols': 3}, {'c1': ['<script>']}, {'ord': {'col': 'senha'}}, 'x'):
            with self.assertRaises((ValueError, TypeError)):
                validar_layout(ruim)

    def test_validar_layout_rejeita_ord_invalido_e_infinity(self):
        for ruim in ({'ord': 'x'}, {'ord': 123}, {'cols': float('inf')},
                     {'ord': {'col': 'status', 'dir': float('inf')}}):
            with self.assertRaises((ValueError, TypeError, OverflowError)):
                validar_layout(ruim)

    def test_validar_layout_aceita_e_ignora_campo_antigo_limite_qualquer_tipo(self):
        # B8: validar_layout aceita e ignora silenciosamente o campo antigo limite com qualquer tipo
        for lim in (0, 10, -5, float('inf'), 'invalido', None, {'chave': 'valor'}, [1, 2]):
            res = validar_layout({'cols': 2, 'c1': ['A'], 'c2': [], 'limite': lim})
            self.assertEqual(res, {'cols': 2, 'c1': ['A'], 'c2': [], 'ord': {'col': 'status', 'dir': 1}})
            self.assertNotIn('limite', res)

    def test_remocao_completa_ids_filas_e_npx_filas_ids(self):
        self.assertFalse(hasattr(agentes_npx, 'ids_filas'), 'ids_filas não deve existir em agentes_npx')
        self.assertFalse(hasattr(agentes_npx, '_id_fila'), '_id_fila não deve existir em agentes_npx')
        fonte = (RAIZ / 'agentes_npx.py').read_text(encoding='utf-8')
        self.assertNotIn('NPX_FILAS_IDS', fonte)


class RotasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'teste.db')
        self.env = patch.dict(os.environ, {'SECRET_KEY': 'test-only-' + 'a' * 64})
        self.env.start()
        self.permitido = False

        with seguranca._rate_limit_lock:
            seguranca._rate_limit_store.clear()

        self.app = Flask(__name__, template_folder=str(RAIZ / 'templates'), static_folder=str(RAIZ / 'static'))
        seguranca.aplicar_config_flask(self.app)
        self.app.config['TESTING'] = True
        bp = Blueprint('bancos', __name__)
        self.cliente = ClienteFalso()
        self.cliente_painel = ClientePainelFalso(configurado=True)
        configurar_agentes(bp, self.conectar, lambda perm: self.permitido and perm == 'perm_agentes',
                           cliente=self.cliente, cliente_painel=self.cliente_painel)
        self.coletor = bp.coletor_agentes
        self.app.register_blueprint(bp)

        @self.app.context_processor
        def menu():
            return {'servidores': {}, 'tem_permissao': lambda p: self.permitido, 'servidor_atual': '', 'modo_todos': False,
                    'modo_inativos': False, 'modo_orfaos': False, 'modo_historico': False, 'filtro_status': None,
                    'busca_termo': '', 'ordem_atual': '', 'atualizar_url': ''}
        self.coletor.garantir_rodando = lambda: None
        self.coletor.retrato = [{'code': '4422001', 'name': 'Darkison', 'queue': 'InfoBrasil_Suporte', 'status': -1, 'paused': True}]
        self.coletor.coletado_em = datetime.now(agentes_npx.FUSO)
        self.http = self.app.test_client()
        self.http.get('/agentes')
        with self.http.session_transaction() as sessao:
            self.token = sessao['_csrf_token']

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()
        with seguranca._rate_limit_lock:
            seguranca._rate_limit_store.clear()

    def conectar(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_visitante_ve_somente_leitura(self):
        html = self.http.get('/agentes').text
        self.assertIn('class="leitura"', html)
        self.assertNotIn('id="dlg"', html)
        self.assertNotIn('id="segCols"', html)
        dados = self.http.get('/agentes/dados').get_json()
        self.assertEqual(dados['agentes'][0]['code'], '4422001')
        self.assertFalse(dados['podeGerenciar'])
        self.assertTrue(dados['painelConfigurado'])
        self.assertIn('InfoBrasil_Suporte', [f['key'] for f in dados['filas']])
        self.assertIn('InfoBrasil_Transbordo', [f['key'] for f in dados['filas']])

    def test_quem_gerencia_ve_acoes(self):
        self.permitido = True
        html = self.http.get('/agentes').text
        self.assertIn('class="pode"', html)
        self.assertIn('id="dlg"', html)

    def test_todas_as_7_rotas_post_exigem_permissao_e_csrf(self):
        rotas = [
            ('/admin/agentes/layout', {'cols': 2, 'c1': ['InfoBrasil_Suporte'], 'c2': []}),
            ('/admin/agentes/4422001/tirar-pausa', {}),
            ('/admin/agentes/4422001/colocar-pausa', {}),
            ('/admin/agentes/4422001/deslogar', {}),
            ('/admin/agentes/4422001/remover-fila', {'fila': 'InfoBrasil_Suporte'}),
            ('/admin/agentes/transferir', {'code': '4422001', 'para': 'InfoBrasil_Comercial'}),
            ('/admin/agentes/prioridade', {'code': '4422001', 'fila': 'InfoBrasil_Suporte', 'penalty': 3}),
        ]
        cabecalho = {'X-CSRF-Token': self.token}

        # 1. Sem permissão -> todas retornam 403
        self.permitido = False
        for url, payload in rotas:
            with seguranca._rate_limit_lock:
                seguranca._rate_limit_store.clear()
            r = self.http.post(url, json=payload, headers=cabecalho)
            self.assertEqual(r.status_code, 403, f'Rota {url} deveria exigir permissão (403)')

        # 2. Com permissão mas sem CSRF -> todas retornam 400
        self.permitido = True
        for url, payload in rotas:
            with seguranca._rate_limit_lock:
                seguranca._rate_limit_store.clear()
            r = self.http.post(url, json=payload)
            self.assertEqual(r.status_code, 400, f'Rota {url} deveria exigir CSRF (400)')

        # 3. Com permissão e CSRF -> todas têm sucesso (200)
        for url, payload in rotas:
            with seguranca._rate_limit_lock:
                seguranca._rate_limit_store.clear()
            r = self.http.post(url, json=payload, headers=cabecalho)
            self.assertEqual(r.status_code, 200, f'Rota {url} falhou com CSRF válido: {r.get_json()}')
            self.assertTrue(r.get_json()['ok'], f'Rota {url} ok!=True: {r.get_json()}')

    def test_validacao_codigo_antes_de_configuracao(self):
        cabecalho = {'X-CSRF-Token': self.token}
        self.permitido = True
        self.cliente_painel._configurado = False

        for cod_invalido in ('abc', '999999999999999', '123_45', '-10'):
            r1 = self.http.post(f'/admin/agentes/{cod_invalido}/colocar-pausa', json={}, headers=cabecalho)
            self.assertEqual(r1.status_code, 400)
            r2 = self.http.post('/admin/agentes/transferir', json={'code': cod_invalido, 'para': 'InfoBrasil_Suporte'}, headers=cabecalho)
            self.assertEqual(r2.status_code, 400)
        r3 = self.http.post('/admin/agentes/transferir', json={'code': '', 'para': 'InfoBrasil_Suporte'}, headers=cabecalho)
        self.assertEqual(r3.status_code, 400)

    def test_mapeamento_status_401_403_upstream_para_local_502(self):
        cabecalho = {'X-CSRF-Token': self.token}
        self.permitido = True
        self.cliente_painel.respostas['invert_pause_status'] = {'ok': False, 'http': 401, 'mensagem': 'Não autorizado'}
        r = self.http.post('/admin/agentes/4422001/colocar-pausa', json={}, headers=cabecalho)
        self.assertEqual(r.status_code, 502)

    def test_tirar_pausa_usa_painel_quando_configurado_ou_fallback_api(self):
        cabecalho = {'X-CSRF-Token': self.token}
        self.permitido = True

        r1 = self.http.post('/admin/agentes/4422001/tirar-pausa', json={}, headers=cabecalho)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(self.cliente_painel.executados[-1], ('invert_pause_status', {'code': '4422001', 'pause': '0'}))

        self.cliente_painel._configurado = False
        r2 = self.http.post('/admin/agentes/4422001/tirar-pausa', json={}, headers=cabecalho)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self.cliente.enviados[-1][0], '/v2/agents/4422001/unpause')

    def test_colocar_pausa_remover_fila_transferir_prioridade_retornam_503_sem_painel(self):
        cabecalho = {'X-CSRF-Token': self.token}
        self.permitido = True
        self.cliente_painel._configurado = False

        r_pausa = self.http.post('/admin/agentes/4422001/colocar-pausa', json={}, headers=cabecalho)
        self.assertEqual(r_pausa.status_code, 503)

        r_remover = self.http.post('/admin/agentes/4422001/remover-fila', json={'fila': 'InfoBrasil_Suporte'}, headers=cabecalho)
        self.assertEqual(r_remover.status_code, 503)

        r_transf = self.http.post('/admin/agentes/transferir', json={'code': '4422001', 'para': 'InfoBrasil_Comercial'}, headers=cabecalho)
        self.assertEqual(r_transf.status_code, 503)

        r_prio = self.http.post('/admin/agentes/prioridade', json={'code': '4422001', 'fila': 'InfoBrasil_Suporte', 'penalty': 5}, headers=cabecalho)
        self.assertEqual(r_prio.status_code, 503)

    def test_auditoria_apenas_acao_e_codigo_sem_texto_remoto(self):
        self.permitido = True
        cabecalho = {'X-CSRF-Token': self.token}
        with patch('agentes_npx.registrar_acao_admin') as audit:
            resp = self.http.post('/admin/agentes/4422001/colocar-pausa', json={}, headers=cabecalho)
            self.assertEqual(resp.status_code, 200)
            audit.assert_called_once_with('agentes_colocar_pausa', {'agente': '4422001', 'ok': True})

    def test_layout_salvo_pelo_admin_vale_para_todos(self):
        cabecalho = {'X-CSRF-Token': self.token}
        novo = {'cols': 1, 'c1': ['InfoBrasil_Comercial'], 'c2': ['InfoBrasil_Suporte'], 'ord': {'col': 'name', 'dir': 1}, 'limite': 7}
        self.assertEqual(self.http.post('/admin/agentes/layout', json=novo, headers=cabecalho).status_code, 403)
        self.permitido = True
        self.assertEqual(self.http.post('/admin/agentes/layout', json={**novo, 'cols': 9}, headers=cabecalho).status_code, 400)
        self.assertTrue(self.http.post('/admin/agentes/layout', json=novo, headers=cabecalho).get_json()['ok'])
        self.permitido = False
        esperado = {'cols': 1, 'c1': ['InfoBrasil_Comercial'], 'c2': ['InfoBrasil_Suporte'], 'ord': {'col': 'name', 'dir': 1}}
        self.assertEqual(self.http.get('/agentes/dados').get_json()['layout'], esperado)

    def test_sem_token_avisa_na_pagina(self):
        self.cliente.token = ''
        self.assertIn('NPX_API_TOKEN', self.http.get('/agentes').text)
        self.assertEqual(self.http.get('/agentes/dados').status_code, 503)

    def test_visitante_nao_ve_dados_sensiveis_de_chamada(self):
        self.coletor.retrato = [{'code': '4422001', 'name': 'Darkison', 'queue': 'InfoBrasil_Suporte',
                                 'status': 2, 'paused': False, 'callSrc': '85999990001',
                                 'callDst': '4422001', 'callClient': '85999990001'}]
        self.permitido = False
        dados_pub = self.http.get('/agentes/dados').get_json()
        agente_pub = dados_pub['agentes'][0]
        self.assertFalse(dados_pub['podeGerenciar'])
        self.assertNotIn('callSrc', agente_pub)
        self.assertNotIn('callDst', agente_pub)
        self.assertNotIn('callClient', agente_pub)
        self.assertIn('callSrc', self.coletor.retrato[0])

        self.permitido = True
        dados_auth = self.http.get('/agentes/dados').get_json()
        agente_auth = dados_auth['agentes'][0]
        self.assertTrue(dados_auth['podeGerenciar'])
        self.assertEqual(agente_auth.get('callSrc'), '85999990001')
        self.assertEqual(agente_auth.get('callDst'), '4422001')
        self.assertEqual(agente_auth.get('callClient'), '85999990001')

    def test_salvar_layout_rejeita_ord_invalido_e_infinity_com_400(self):
        self.permitido = True
        cabecalho = {'X-CSRF-Token': self.token}
        self.assertEqual(self.http.post('/admin/agentes/layout', json={'ord': 'invalido'}, headers=cabecalho).status_code, 400)
        self.assertEqual(self.http.post('/admin/agentes/layout', json={'cols': float('inf')}, headers=cabecalho).status_code, 400)
        # B8: limite antigo com infinity é aceito e ignorado com HTTP 200
        res = self.http.post('/admin/agentes/layout', json={'cols': 1, 'limite': float('inf')}, headers=cabecalho)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('limite', res.get_json()['layout'])

    def test_salvar_layout_registra_auditoria_sem_payload_sensivel(self):
        self.permitido = True
        cabecalho = {'X-CSRF-Token': self.token}
        novo = {'cols': 2, 'c1': ['InfoBrasil_Suporte'], 'c2': [], 'ord': {'col': 'name', 'dir': 1}, 'limite': 15}
        with patch('agentes_npx.registrar_acao_admin') as audit:
            resp = self.http.post('/admin/agentes/layout', json=novo, headers=cabecalho)
            self.assertEqual(resp.status_code, 200)
            audit.assert_called_once_with('agentes_layout', {'cols': 2})

    def test_prioridade_valida_penalty_int_estrito_1_a_10_com_400(self):
        self.permitido = True
        cabecalho = {'X-CSRF-Token': self.token}
        for ruim in (0, 11, -1, 5.5, True, False, '5'):
            r = self.http.post('/admin/agentes/prioridade',
                               json={'code': '4422001', 'fila': 'InfoBrasil_Suporte', 'penalty': ruim},
                               headers=cabecalho)
            self.assertEqual(r.status_code, 400)

    def test_remover_fila_com_snapshot_e_cliente_painel_real(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = str(Path(temp.name) / 'teste.db')

        def conectar():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        app = Flask(__name__)
        seguranca.aplicar_config_flask(app)
        app.config['TESTING'] = True
        bp = Blueprint('bancos_real', __name__)

        painel_real = ClientePainelNpx(base='https://app.npx.com.br', email='u@x.com', senha='s')
        painel_real._autenticado = True
        mock_opener = MagicMock()
        mock_opener.open.return_value = MockResponse('{"status": 1, "message": "Agente removido"}', status=200)
        painel_real._opener = mock_opener

        cliente = ClienteFalso()
        configurar_agentes(bp, conectar, lambda perm: True, cliente=cliente, cliente_painel=painel_real)
        coletor = bp.coletor_agentes
        coletor.garantir_rodando = lambda: None
        coletor.retrato = [{'code': '4422001', 'queue': 'FilaEspecial_Temporaria'}]
        app.register_blueprint(bp)

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['user_nome'] = 'Master'
                sess['_csrf_token'] = 'token_csrf_123'

            with seguranca._rate_limit_lock:
                seguranca._rate_limit_store.clear()

            resp = client.post('/admin/agentes/4422001/remover-fila',
                               json={'fila': 'FilaEspecial_Temporaria'},
                               headers={'X-CSRF-Token': 'token_csrf_123'})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()['ok'])

            # Valida URL exata chamada no open: remove_agent recebe SOMENTE code, sem parâmetro queue
            req = mock_opener.open.call_args[0][0]
            self.assertEqual(req.full_url, 'https://app.npx.com.br/rates/remove_agent?code=4422001')


class IntegracaoBancosTest(unittest.TestCase):
    def test_rotas_publicas_e_permissao_registradas(self):
        fonte = (RAIZ / 'bancos.py').read_text(encoding='utf-8')
        self.assertIn("'bancos.agentes', 'bancos.agentes_dados'", fonte)
        self.assertIn("'perm_agentes':", fonte)
        self.assertIn('configurar_agentes(bancos_bp, get_db_connection, tem_permissao)', fonte)
        self.assertIn('href="/agentes"', (RAIZ / 'templates' / 'menu_navegacao.html').read_text(encoding='utf-8'))


class RotasPbxIntegracaoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'teste.db')
        self.env = patch.dict(os.environ, {'SECRET_KEY': 'test-only-' + 'b' * 64})
        self.env.start()
        self.permitido = False

        with seguranca._rate_limit_lock:
            seguranca._rate_limit_store.clear()

        self.app = Flask(__name__, template_folder=str(RAIZ / 'templates'), static_folder=str(RAIZ / 'static'))
        seguranca.aplicar_config_flask(self.app)
        self.app.config['TESTING'] = True
        self.bp = Blueprint('bancos_pbx', __name__)
        self.cliente_npx = ClienteFalso()
        self.cliente_painel = ClientePainelFalso(configurado=True)

        # Mock de ColetorPbx
        self.coletor_pbx = MagicMock()
        self.coletor_pbx.garantir_rodando = MagicMock()
        self.coletor_pbx.mapa_analistas = {
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Carol': {'ramal': '2022', 'departamento': 'InfoBrasil_SPED_Fiscal'},
        }

        # Snapshot padrão do PBX
        def enriquecer_mock(agentes):
            for a in agentes:
                if a.get('name') == 'Thiago' or a.get('code') == '4422008':
                    a['pbxExtension'] = '2008'
                    a['pbxStatus'] = 'Busy'
                    a['pbxNumber'] = '85999998888'
                    a['pbxDirection'] = 'inbound'
                    a['pbxSeconds'] = 45
                else:
                    a['pbxExtension'] = None
                    a['pbxStatus'] = 'Offline'
                    a['pbxNumber'] = None
                    a['pbxDirection'] = None
                    a['pbxSeconds'] = None
                
                # Precedência estrita
                if a.get('paused') or (a.get('status') is not None and a.get('status') != 0):
                    a['line'] = 'npx'
                elif a.get('pbxStatus') in ('Free', 'Busy', 'Ringing'):
                    a['line'] = 'pbx'
                else:
                    a['line'] = 'off'
            return agentes

        self.coletor_pbx.enriquecer_agentes = MagicMock(side_effect=enriquecer_mock)
        self.coletor_pbx.obter_status_conexao = MagicMock(return_value={
            'configured': True, 'available': True, 'error': None
        })
        self.coletor_pbx.obter_agentes_pbx_only = MagicMock(return_value=[
            {
                'code': 'pbx-2022',
                'name': 'Carol',
                'queue': 'InfoBrasil_SPED_Fiscal',
                'extension': '2022',
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
                'pbxExtension': '2022',
                'pbxStatus': 'Busy',
                'pbxNumber': '85988887777',
                'pbxDirection': 'outbound',
                'pbxSeconds': 20,
            }
        ])

        configurar_agentes(
            self.bp,
            self.conectar,
            lambda perm: self.permitido and perm == 'perm_agentes',
            cliente=self.cliente_npx,
            cliente_painel=self.cliente_painel,
            coletor_pbx=self.coletor_pbx,
        )
        self.coletor_npx = self.bp.coletor_agentes
        self.coletor_npx.garantir_rodando = lambda: None
        self.coletor_npx.retrato = [
            {'code': '4422008', 'name': 'Thiago', 'queue': 'InfoBrasil_Suporte', 'status': 1, 'paused': False}
        ]
        self.coletor_npx.coletado_em = datetime.now(agentes_npx.FUSO)
        self.app.register_blueprint(self.bp)
        self.http = self.app.test_client()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()
        with seguranca._rate_limit_lock:
            seguranca._rate_limit_store.clear()

    def conectar(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_contrato_dados_com_pbx_e_privacidade_visitante(self):
        self.permitido = False
        resp = self.http.get('/agentes/dados')
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()

        # Verifica nó pbxConnection
        self.assertIn('pbxConnection', dados)
        self.assertEqual(dados['pbxConnection'], {'configured': True, 'available': True, 'error': None})

        # Verifica agentes retornados (Thiago da NPX e Carol PBX-only)
        agentes = dados['agentes']
        self.assertEqual(len(agentes), 2)

        thiago = next(a for a in agentes if a['name'] == 'Thiago')
        self.assertEqual(thiago['line'], 'npx')
        self.assertEqual(thiago['pbxExtension'], '2008')
        self.assertEqual(thiago['pbxStatus'], 'Busy')
        self.assertEqual(thiago['pbxSeconds'], 45)
        self.assertEqual(thiago['pbxDirection'], 'inbound')
        # Privacidade: pbxNumber NUNCA deve ser enviado a visitante anônimo
        self.assertNotIn('pbxNumber', thiago)

        carol = next(a for a in agentes if a['name'] == 'Carol')
        self.assertEqual(carol['code'], 'pbx-2022')
        self.assertEqual(carol['line'], 'pbx')
        self.assertEqual(carol['queue'], 'InfoBrasil_SPED_Fiscal')
        self.assertEqual(carol['pbxStatus'], 'Busy')
        self.assertNotIn('pbxNumber', carol)

    def test_contrato_dados_com_pbx_administrador_ve_pbx_number(self):
        self.permitido = True
        resp = self.http.get('/agentes/dados')
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()

        self.assertTrue(dados['podeGerenciar'])
        thiago = next(a for a in dados['agentes'] if a['name'] == 'Thiago')
        # Administrador tem direito de visualizar o número da chamada PBX
        self.assertEqual(thiago['pbxNumber'], '85999998888')

        carol = next(a for a in dados['agentes'] if a['name'] == 'Carol')
        self.assertEqual(carol['pbxNumber'], '85988887777')

    def test_resiliencia_e_tolerancia_a_falha_do_pbx(self):
        # Central PBX com erro / indisponível
        self.coletor_pbx.obter_status_conexao.return_value = {
            'configured': True, 'available': False, 'error': 'Central PBX indisponível'
        }
        resp = self.http.get('/agentes/dados')
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()
        self.assertFalse(dados['pbxConnection']['available'])
        self.assertEqual(dados['pbxConnection']['error'], 'Central PBX indisponível')
        # Agentes NPX continuam respondendo sem qualquer interrupção
        self.assertEqual(len(dados['agentes']), 2)


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
