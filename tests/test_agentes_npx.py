import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask

import agentes_npx
from agentes_npx import ClienteNpx, ColetorAgentes, LayoutAgentes, configurar_agentes, transferir, validar_layout

RAIZ = Path(__file__).resolve().parent.parent


class ClienteFalso(ClienteNpx):
    """Responde como a API da NPX a partir de um dicionário, registrando os POSTs."""
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
            # mesma sessão do histórico, agora com logoff: não pode aparecer
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


class AcoesTest(unittest.TestCase):
    def test_transferir_exige_id_das_duas_filas_antes_de_mexer(self):
        cliente = ClienteFalso()
        with patch.dict(os.environ, {'NPX_FILAS_IDS': 'InfoBrasil_Suporte=10'}):
            with self.assertRaises(ValueError):
                transferir(cliente, {'code': '4422001', 'de': 'InfoBrasil_Suporte', 'para': 'InfoBrasil_Comercial'})
        self.assertEqual(cliente.enviados, [])

    def test_transferir_inclui_na_nova_antes_de_remover_da_antiga(self):
        cliente = ClienteFalso()
        with patch.dict(os.environ, {'NPX_FILAS_IDS': 'InfoBrasil_Suporte=10;InfoBrasil_Comercial=11'}):
            r = transferir(cliente, {'code': '4422001', 'de': 'InfoBrasil_Suporte', 'para': 'InfoBrasil_Comercial', 'penalty': 2})
        self.assertTrue(r['ok'])
        self.assertEqual([c for c, _ in cliente.enviados], ['/v2/agents/4422001/add_to_queue', '/v2/agents/4422001/remove_to_queue'])
        self.assertEqual(cliente.enviados[0][1]['queue_id'], '11')

    def test_transferir_nao_remove_se_inclusao_falhar(self):
        cliente = ClienteFalso(posts={'add_to_queue': {'ok': False, 'http': 200, 'mensagem': 'erro'}})
        with patch.dict(os.environ, {'NPX_FILAS_IDS': 'InfoBrasil_Suporte=10;InfoBrasil_Comercial=11'}):
            r = transferir(cliente, {'code': '4422001', 'de': 'InfoBrasil_Suporte', 'para': 'InfoBrasil_Comercial'})
        self.assertFalse(r['ok'])
        self.assertEqual(len(cliente.enviados), 1)

    def test_validar_layout(self):
        self.assertEqual(validar_layout({'cols': 2, 'c1': ['A', 'A'], 'c2': ['A', 'B'], 'ord': {'col': 'tempo', 'dir': -1}, 'limite': 5}),
                         {'cols': 2, 'c1': ['A'], 'c2': ['B'], 'ord': {'col': 'tempo', 'dir': -1}, 'limite': 5})
        for ruim in ({'cols': 3}, {'c1': ['<script>']}, {'ord': {'col': 'senha'}}, {'limite': 0}, 'x'):
            with self.assertRaises((ValueError, TypeError)):
                validar_layout(ruim)


class RotasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'teste.db')
        self.env = patch.dict(os.environ, {'SECRET_KEY': 'test-only-' + 'a' * 64})
        self.env.start()
        self.permitido = False
        import seguranca
        self.app = Flask(__name__, template_folder=str(RAIZ / 'templates'), static_folder=str(RAIZ / 'static'))
        seguranca.aplicar_config_flask(self.app)
        self.app.config['TESTING'] = True
        bp = Blueprint('bancos', __name__)
        self.cliente = ClienteFalso()
        configurar_agentes(bp, self.conectar, lambda perm: self.permitido and perm == 'perm_agentes', cliente=self.cliente)
        self.coletor = bp.coletor_agentes
        self.app.register_blueprint(bp)

        @self.app.context_processor
        def menu():
            return {'servidores': {}, 'tem_permissao': lambda p: self.permitido, 'servidor_atual': '', 'modo_todos': False,
                    'modo_inativos': False, 'modo_orfaos': False, 'modo_historico': False, 'filtro_status': None,
                    'busca_termo': '', 'ordem_atual': '', 'atualizar_url': ''}
        self.coletor.garantir_rodando = lambda: None
        self.coletor.retrato = [{'code': '4422001', 'name': 'Darkison', 'queue': 'InfoBrasil_Suporte', 'status': -1, 'paused': True}]
        from datetime import datetime
        self.coletor.coletado_em = datetime.now(agentes_npx.FUSO)
        self.http = self.app.test_client()
        self.http.get('/agentes')
        with self.http.session_transaction() as sessao:
            self.token = sessao['_csrf_token']

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

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
        self.assertIn('InfoBrasil_Suporte', [f['key'] for f in dados['filas']])

    def test_quem_gerencia_ve_acoes(self):
        self.permitido = True
        html = self.http.get('/agentes').text
        self.assertIn('class="pode"', html)
        self.assertIn('id="dlg"', html)

    def test_acoes_exigem_permissao_e_csrf(self):
        cabecalho = {'X-CSRF-Token': self.token}
        self.assertEqual(self.http.post('/admin/agentes/4422001/tirar-pausa', json={}, headers=cabecalho).status_code, 403)
        self.permitido = True
        self.assertEqual(self.http.post('/admin/agentes/4422001/tirar-pausa', json={}).status_code, 400)   # sem CSRF
        r = self.http.post('/admin/agentes/4422001/tirar-pausa', json={}, headers=cabecalho)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(self.cliente.enviados[-1][0], '/v2/agents/4422001/unpause')
        self.assertEqual(self.http.post('/admin/agentes/abc/deslogar', json={}, headers=cabecalho).status_code, 400)
        self.assertEqual(len(self.cliente.enviados), 1)

    def test_layout_salvo_pelo_admin_vale_para_todos(self):
        cabecalho = {'X-CSRF-Token': self.token}
        novo = {'cols': 1, 'c1': ['InfoBrasil_Comercial'], 'c2': ['InfoBrasil_Suporte'], 'ord': {'col': 'name', 'dir': 1}, 'limite': 7}
        self.assertEqual(self.http.post('/admin/agentes/layout', json=novo, headers=cabecalho).status_code, 403)
        self.permitido = True
        self.assertEqual(self.http.post('/admin/agentes/layout', json={**novo, 'cols': 9}, headers=cabecalho).status_code, 400)
        self.assertTrue(self.http.post('/admin/agentes/layout', json=novo, headers=cabecalho).get_json()['ok'])
        self.permitido = False
        self.assertEqual(self.http.get('/agentes/dados').get_json()['layout'], novo)

    def test_sem_token_avisa_na_pagina(self):
        self.cliente.token = ''
        self.assertIn('NPX_API_TOKEN', self.http.get('/agentes').text)
        self.assertEqual(self.http.get('/agentes/dados').status_code, 503)


class IntegracaoBancosTest(unittest.TestCase):
    def test_rotas_publicas_e_permissao_registradas(self):
        fonte = (RAIZ / 'bancos.py').read_text(encoding='utf-8')
        self.assertIn("'bancos.agentes', 'bancos.agentes_dados'", fonte)
        self.assertIn("'perm_agentes':", fonte)
        self.assertIn('configurar_agentes(bancos_bp, get_db_connection, tem_permissao)', fonte)
        self.assertIn('href="/agentes"', (RAIZ / 'templates' / 'menu_navegacao.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
