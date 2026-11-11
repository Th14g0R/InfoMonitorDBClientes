import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib import error as urlerror

import pbx_monitor
from pbx_monitor import (
    BlockRedirectHandler,
    ClientePbx,
    ColetorPbx,
    FILA_PBX_SEM_FILA,
    carregar_mapa_analistas,
    casar_analista,
    processar_snapshot_pbx,
    traduzir_departamento,
    validar_pbx_base_url,
)


class MockHttpSuccess:
    def __init__(self, data):
        self.data = json.dumps({'error': 0, 'data': data}).encode('utf-8')

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestClientePbx(unittest.TestCase):
    def test_validar_pbx_base_url_seguranca(self):
        # Somente HTTPS válido
        self.assertEqual(validar_pbx_base_url('https://pbx.infobrasil.com.br'), 'https://pbx.infobrasil.com.br')
        self.assertEqual(validar_pbx_base_url('https://pbx.infobrasil.com.br/'), 'https://pbx.infobrasil.com.br')
        self.assertEqual(validar_pbx_base_url('https://pbx.infobrasil.com.br:8443/central'), 'https://pbx.infobrasil.com.br:8443/central')

        # HTTP não é aceito
        self.assertEqual(validar_pbx_base_url('http://pbx.infobrasil.com.br'), '')
        self.assertEqual(validar_pbx_base_url('http://192.168.243.2:8080'), '')

        # Userinfo rejeitado
        self.assertEqual(validar_pbx_base_url('https://admin:senha@pbx.com.br'), '')

        # Query ou fragment rejeitados
        self.assertEqual(validar_pbx_base_url('https://pbx.com.br/api?param=1'), '')
        self.assertEqual(validar_pbx_base_url('https://pbx.com.br/api#topo'), '')

    def test_configurado(self):
        with patch.dict(os.environ, {'PBX_BASE_URL': '', 'PBX_API_TOKEN': '', 'PBX_API_KEY': ''}):
            c1 = ClientePbx(base_url='', token='', key='')
            self.assertFalse(c1.configurado)

            c_env = ClientePbx()
            self.assertFalse(c_env.configurado)

        c2 = ClientePbx(base_url='https://pbx.infobrasil.com.br', token='tok123', key='key456')
        self.assertTrue(c2.configurado)
        self.assertEqual(c2.base_url, 'https://pbx.infobrasil.com.br')

    def test_redirecionamento_bloqueado(self):
        handler = BlockRedirectHandler()
        req = MagicMock()
        req.full_url = 'https://pbx.infobrasil.com.br/api/onlineCalls/t/k'
        with self.assertRaises(urlerror.HTTPError) as ctx:
            handler.redirect_request(req, None, 302, 'Found', {}, 'https://outro.com')
        if ctx.exception.fp:
            ctx.exception.fp.close()

    def test_tls_reject_unauthorized_configuracao(self):
        # 1. P9: Construtor explícito vence a variável de ambiente
        with patch.dict(os.environ, {'PBX_TLS_REJECT_UNAUTHORIZED': 'true'}):
            c_exp_false = ClientePbx(base_url='https://pbx.local', token='t', key='k', tls_reject_unauthorized=False)
            self.assertFalse(c_exp_false.tls_reject_unauthorized)

        with patch.dict(os.environ, {'PBX_TLS_REJECT_UNAUTHORIZED': 'false'}):
            c_exp_true = ClientePbx(base_url='https://pbx.local', token='t', key='k', tls_reject_unauthorized=True)
            self.assertTrue(c_exp_true.tls_reject_unauthorized)

        # 2. Somente valor exato 'false' desativa TLS via env
        with patch.dict(os.environ, {'PBX_TLS_REJECT_UNAUTHORIZED': 'false'}):
            c_env_false = ClientePbx(base_url='https://pbx.local', token='t', key='k')
            self.assertFalse(c_env_false.tls_reject_unauthorized)

        with patch.dict(os.environ, {'PBX_TLS_REJECT_UNAUTHORIZED': '0'}):
            c_env_outro = ClientePbx(base_url='https://pbx.local', token='t', key='k')
            self.assertTrue(c_env_outro.tls_reject_unauthorized)

    @patch('pbx_monitor.urlrequest.build_opener')
    def test_quote_token_key_safe_vazio(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener
        mock_opener.open.return_value = MockHttpSuccess([])

        # Token com caracteres especiais: @, /, ?
        cliente = ClientePbx(base_url='https://pbx.test', token='tok/en@1', key='ke y+2')
        cliente.listar_dispositivos()

        req = mock_opener.open.call_args[0][0]
        # quote com safe='' deve converter / para %2F e @ para %40
        self.assertIn('tok%2Fen%401', req.full_url)
        self.assertIn('ke%20y%2B2', req.full_url)

    @patch('pbx_monitor.urlrequest.build_opener')
    def test_p2_data_nao_lista_deve_falhar_ciclo(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener

        cliente = ClientePbx(base_url='https://pbx.test', token='t1', key='k1')

        # Se data vier como dicionário em vez de lista
        mock_opener.open.return_value = MockHttpSuccess({'invalido': True})
        with self.assertRaises(RuntimeError) as ctx:
            cliente.listar_dispositivos_registrados()
        self.assertIn('não é lista', str(ctx.exception))

        # Se data vier como None
        mock_opener.open.return_value = MockHttpSuccess(None)
        with self.assertRaises(RuntimeError) as ctx:
            cliente.listar_chamadas_online()
        self.assertIn('não é lista', str(ctx.exception))

    @patch('pbx_monitor.urlrequest.build_opener')
    def test_falhas_http_e_rede_sem_vazar_credenciais(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener

        cliente = ClientePbx(base_url='https://pbx.test', token='TOKEN_SECRETO', key='KEY_SECRETA')

        # HTTP 500
        mock_opener.open.side_effect = urlerror.HTTPError('https://url', 500, 'Server Error', {}, None)
        with self.assertRaises(RuntimeError) as ctx:
            cliente.listar_chamadas_online()
        self.assertIn('HTTP 500', str(ctx.exception))
        self.assertNotIn('TOKEN_SECRETO', str(ctx.exception))

        # URLError (timeout/offline)
        mock_opener.open.side_effect = urlerror.URLError('Conexao recusada')
        with self.assertRaises(RuntimeError) as ctx:
            cliente.listar_chamadas_online()
        self.assertIn('Falha de conexão', str(ctx.exception))
        self.assertNotIn('TOKEN_SECRETO', str(ctx.exception))


class TestMapaAnalistasETraducao(unittest.TestCase):
    def test_traducao_departamentos_e_fila_sintetica(self):
        self.assertEqual(traduzir_departamento('InfoBrasil_Suporte'), 'InfoBrasil_Suporte')
        self.assertEqual(traduzir_departamento('Suporte'), 'InfoBrasil_Suporte')
        self.assertEqual(traduzir_departamento('Infobrasil_finan'), 'Infobrasil_finan')
        self.assertEqual(traduzir_departamento('Financeiro'), 'Infobrasil_finan')
        self.assertEqual(traduzir_departamento('InfoBrasil_Comercial'), 'InfoBrasil_Comercial')
        self.assertEqual(traduzir_departamento('InfoBrasil_SPED_Fiscal'), 'InfoBrasil_SPED_Fiscal')
        # P5: Desconhecido vira FILA_PBX_SEM_FILA ('PBX_Sem_Fila'), nunca vazio
        self.assertEqual(traduzir_departamento('Desconhecido_X'), FILA_PBX_SEM_FILA)
        self.assertEqual(traduzir_departamento(None), FILA_PBX_SEM_FILA)

    def test_casar_analista_desambiguacao_estrita(self):
        mapa = {
            'Davi Oliveira': {'ramal': '2002', 'departamento': 'InfoBrasil_Suporte'},
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Vitor Dantas': {'ramal': '2020', 'departamento': 'InfoBrasil_Comercial'},
            'Josi Dantas': {'ramal': '2021', 'departamento': 'InfoBrasil_Comercial'},
        }

        # 1. Exato
        r, d = casar_analista('Thiago', mapa_analistas=mapa)
        self.assertEqual(r, '2008')

        # 2. Case insensitive
        r, d = casar_analista('thiago', mapa_analistas=mapa)
        self.assertEqual(r, '2008')

        # 3. Parcial único (NPX vem Davi, mapa tem Davi Oliveira)
        r, d = casar_analista('Davi', mapa_analistas=mapa)
        self.assertEqual(r, '2002')

        # 4. Colisão ambígua parcial (Dantas casa com Vitor e Josi) -> retorna None!
        r, d = casar_analista('Dantas', mapa_analistas=mapa)
        self.assertIsNone(r)
        self.assertIsNone(d)

        # 5. Sufixo de 4 dígitos casa
        r, d = casar_analista('Outro Nome', codigo_agente='4422008', mapa_analistas=mapa)
        self.assertEqual(r, '2008')

        # 6. Conflito entre nome e sufixo: nome aponta 2008 mas sufixo aponta 2002 -> NÃO casa!
        r, d = casar_analista('Thiago', codigo_agente='4422002', mapa_analistas=mapa)
        self.assertIsNone(r)
        self.assertIsNone(d)


class TestSnapshotPbxP1EDuracao(unittest.TestCase):
    def test_p1_direction_number_quando_destination_username(self):
        dispositivos = [
            {'username': '852008.Thiago', 'linha_ip': '2008'}
        ]
        registrados = [
            {'device_username': '852008.Thiago', 'device_registered': True}
        ]
        # P1: Cliente liga para o username do analista (852008.Thiago)
        chamadas_inbound = [
            {
                'source': '85999998888',
                'destination': '852008.Thiago',
                'status_text': 'ANSWERED',
                'starttime': '2026-11-11T17:30:00-03:00'
            }
        ]
        agora = datetime.fromisoformat('2026-11-11T17:30:30-03:00')
        snap = processar_snapshot_pbx(dispositivos, registrados, chamadas_inbound, agora=agora)

        # Deve ser INBOUND e número do cliente, NUNCA '852008.Thiago'
        self.assertEqual(snap['2008']['status'], 'Busy')
        self.assertEqual(snap['2008']['direction'], 'inbound')
        self.assertEqual(snap['2008']['number'], '85999998888')
        self.assertEqual(snap['2008']['seconds'], 30)

        # Chamada outbound: analista liga para cliente
        chamadas_outbound = [
            {
                'source': '852008.Thiago',
                'destination': '85999998888',
                'status_text': 'ANSWERED',
                'starttime': '2026-11-11T17:30:00-03:00'
            }
        ]
        snap_out = processar_snapshot_pbx(dispositivos, registrados, chamadas_outbound, agora=agora)
        self.assertEqual(snap_out['2008']['direction'], 'outbound')
        self.assertEqual(snap_out['2008']['number'], '85999998888')

        # Se number por algum motivo fosse o próprio analista -> mascara para 'Ligação Ativa'
        chamadas_loop = [
            {
                'source': '852008.Thiago',
                'destination': '852008.Thiago',
                'status_text': 'ANSWERED',
                'starttime': '2026-11-11T17:30:00-03:00'
            }
        ]
        snap_loop = processar_snapshot_pbx(dispositivos, registrados, chamadas_loop, agora=agora)
        self.assertEqual(snap_loop['2008']['number'], 'Ligação Ativa')

    def test_duracao_teto_86400(self):
        dispositivos = [{'username': '852008.Thiago', 'linha_ip': '2008'}]
        registrados = [{'device_username': '852008.Thiago', 'device_registered': True}]
        # Chamada de 3 dias atrás
        chamadas = [{
            'source': '85999998888', 'destination': '2008',
            'status_text': 'ANSWERED', 'starttime': '2026-11-08T17:30:00-03:00'
        }]
        agora = datetime.fromisoformat('2026-11-11T17:30:00-03:00')
        snap = processar_snapshot_pbx(dispositivos, registrados, chamadas, agora=agora)
        self.assertEqual(snap['2008']['seconds'], 86400)


class TestColetorPbxP5EP6(unittest.TestCase):
    def test_p6_unicidade_dois_codes_mesmo_ramal_nenhum_recebe(self):
        coletor = ColetorPbx(cliente=MagicMock(), mapa_analistas={
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'}
        })
        coletor.snapshot = {
            '2008': {'status': 'Free', 'number': None, 'direction': None, 'seconds': 0}
        }

        # Dois agentes distintos disputam o mesmo ramal 2008
        agentes = [
            {'code': '101', 'name': 'Thiago', 'status': 0, 'paused': False},
            {'code': '102', 'name': 'Thiago Junior', 'status': 0, 'paused': False}
        ]
        res = coletor.enriquecer_agentes(agentes)
        # P6: Nenhum recebe!
        self.assertIsNone(res[0]['pbxExtension'])
        self.assertIsNone(res[1]['pbxExtension'])
        self.assertEqual(res[0]['line'], 'off')
        self.assertEqual(res[1]['line'], 'off')

    def test_p5_pbx_only_somente_registrados(self):
        coletor = ColetorPbx(cliente=MagicMock(), mapa_analistas={
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Carol': {'ramal': '2022', 'departamento': 'InfoBrasil_SPED_Fiscal'},
            'Alisson': {'ramal': '2011', 'departamento': 'InfoBrasil_Suporte'}
        })
        # Carol está Busy (registrada), Alisson está Offline
        coletor.snapshot = {
            '2008': {'status': 'Free', 'number': None, 'direction': None, 'seconds': 0},
            '2022': {'status': 'Busy', 'number': '85987654321', 'direction': 'outbound', 'seconds': 45},
            '2011': {'status': 'Offline', 'number': None, 'direction': None, 'seconds': 0}
        }

        # Thiago já está na NPX
        pbx_only = coletor.obter_agentes_pbx_only(codigos_npx_existentes=['4422008'], ramais_npx_casados={'2008'})
        
        # P5: Somente Carol deve entrar! Alisson (offline) NÃO deve entrar!
        self.assertEqual(len(pbx_only), 1)
        self.assertEqual(pbx_only[0]['name'], 'Carol')
        self.assertEqual(pbx_only[0]['code'], 'pbx-2022')
        self.assertEqual(pbx_only[0]['line'], 'pbx')


if __name__ == '__main__':
    unittest.main()
