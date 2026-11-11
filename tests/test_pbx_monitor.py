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
    carregar_mapa_analistas,
    casar_analista,
    processar_snapshot_pbx,
    traduzir_departamento,
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
    def test_configurado(self):
        c1 = ClientePbx(base_url='', token='', key='')
        self.assertFalse(c1.configurado)

        c2 = ClientePbx(base_url='http://192.168.243.2:8080', token='tok123', key='key456')
        self.assertTrue(c2.configurado)
        self.assertEqual(c2.base_url, 'http://192.168.243.2:8080')

    def test_redirecionamento_bloqueado(self):
        handler = BlockRedirectHandler()
        req = MagicMock()
        req.full_url = 'http://192.168.243.2:8080/api/onlineCalls/t/k'
        with self.assertRaises(urlerror.HTTPError) as ctx:
            handler.redirect_request(req, None, 302, 'Found', {}, 'http://outro.com')
        if ctx.exception.fp:
            ctx.exception.fp.close()

    def test_tls_reject_unauthorized_configuracao(self):
        with patch.dict(os.environ, {'PBX_TLS_REJECT_UNAUTHORIZED': 'false'}):
            c_false = ClientePbx(base_url='https://pbx.local', token='t', key='k')
            self.assertFalse(c_false.tls_reject_unauthorized)

        with patch.dict(os.environ, {'PBX_TLS_REJECT_UNAUTHORIZED': 'true'}):
            c_true = ClientePbx(base_url='https://pbx.local', token='t', key='k')
            self.assertTrue(c_true.tls_reject_unauthorized)

    @patch('pbx_monitor.urlrequest.build_opener')
    def test_metodos_get_sucesso(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener

        cliente = ClientePbx(base_url='http://pbx.test', token='t1', key='k1')

        # Teste listDevices
        mock_opener.open.return_value = MockHttpSuccess([{'username': '852008.Thiago', 'linha_ip': '2008'}])
        devs = cliente.listar_dispositivos()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]['linha_ip'], '2008')

        # Teste listDevicesRegistered
        mock_opener.open.return_value = MockHttpSuccess([{'device_username': '852008.Thiago', 'device_registered': True}])
        regs = cliente.listar_dispositivos_registrados()
        self.assertEqual(len(regs), 1)
        self.assertTrue(regs[0]['device_registered'])

        # Teste onlineCalls
        mock_opener.open.return_value = MockHttpSuccess([{'source': '85999998888', 'destination': '2008'}])
        calls = cliente.listar_chamadas_online()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['destination'], '2008')

    @patch('pbx_monitor.urlrequest.build_opener')
    def test_falhas_http_e_rede_sem_vazar_credenciais(self, mock_build_opener):
        mock_opener = MagicMock()
        mock_build_opener.return_value = mock_opener

        cliente = ClientePbx(base_url='http://pbx.test', token='TOKEN_SECRETO', key='KEY_SECRETA')

        # HTTP 500
        mock_opener.open.side_effect = urlerror.HTTPError('http://url', 500, 'Server Error', {}, None)
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
    def test_traducao_departamentos(self):
        self.assertEqual(traduzir_departamento('InfoBrasil_Suporte'), 'InfoBrasil_Suporte')
        self.assertEqual(traduzir_departamento('Suporte'), 'InfoBrasil_Suporte')
        self.assertEqual(traduzir_departamento('Infobrasil_finan'), 'Infobrasil_finan')
        self.assertEqual(traduzir_departamento('Financeiro'), 'Infobrasil_finan')
        self.assertEqual(traduzir_departamento('InfoBrasil_Comercial'), 'InfoBrasil_Comercial')
        self.assertEqual(traduzir_departamento('InfoBrasil_SPED_Fiscal'), 'InfoBrasil_SPED_Fiscal')
        self.assertEqual(traduzir_departamento('Desconhecido_X'), '')
        self.assertEqual(traduzir_departamento(None), '')

    def test_carregar_mapa_analistas(self):
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.json', encoding='utf-8') as f:
            f.write(json.dumps({
                "Davi Oliveira": {"ramal": "2002", "departamento": "InfoBrasil_Suporte"},
                "Thiago": {"ramal": "2008", "departamento": "InfoBrasil_Suporte"}
            }))
            temp_path = f.name

        try:
            mapa = carregar_mapa_analistas(temp_path)
            self.assertIn('Davi Oliveira', mapa)
            self.assertEqual(mapa['Davi Oliveira']['ramal'], '2002')
            self.assertEqual(mapa['Thiago']['ramal'], '2008')
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_casar_analista(self):
        mapa = {
            'Davi Oliveira': {'ramal': '2002', 'departamento': 'InfoBrasil_Suporte'},
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Vitor Dantas': {'ramal': '2020', 'departamento': 'InfoBrasil_Comercial'},
            'Josi Dantas': {'ramal': '2021', 'departamento': 'InfoBrasil_Comercial'},
        }

        # 1. Exato
        r, d = casar_analista('Thiago', mapa_analistas=mapa)
        self.assertEqual(r, '2008')
        self.assertEqual(d, 'InfoBrasil_Suporte')

        # 2. Case insensitive
        r, d = casar_analista('thiago', mapa_analistas=mapa)
        self.assertEqual(r, '2008')

        # 3. Parcial único (NPX vem só Davi, mapa tem Davi Oliveira)
        r, d = casar_analista('Davi', mapa_analistas=mapa)
        self.assertEqual(r, '2002')

        # 4. Colisão ambígua de substring (ex: "Dantas" casa com Vitor Dantas E Josi Dantas)
        r, d = casar_analista('Dantas', mapa_analistas=mapa)
        self.assertIsNone(r)
        self.assertIsNone(d)

        # 5. Casamento pelos 4 dígitos finais do código/extensão
        r, d = casar_analista('Outro Nome', codigo_agente='4422008', mapa_analistas=mapa)
        self.assertEqual(r, '2008')


class TestSnapshotPbx(unittest.TestCase):
    def test_processar_snapshot(self):
        dispositivos = [
            {'username': '852008.Thiago', 'linha_ip': '2008'},
            {'username': '852002.Davi', 'linha_ip': '2002'},
            {'username': '852011.Alisson', 'linha_ip': '2011'}
        ]
        registrados = [
            {'device_username': '852008.Thiago', 'device_registered': True},
            {'device_username': '852002.Davi', 'device_registered': True},
            {'device_username': '852011.Alisson', 'device_registered': False}
        ]
        chamadas = [
            {
                'source': '85988887777',
                'destination': '2008',
                'status_text': 'ANSWERED',
                'starttime': '2026-11-11T17:30:00-03:00'
            }
        ]
        mapa = {
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Davi': {'ramal': '2002', 'departamento': 'InfoBrasil_Suporte'},
            'Alisson': {'ramal': '2011', 'departamento': 'InfoBrasil_Suporte'}
        }

        agora = datetime.fromisoformat('2026-11-11T17:31:00-03:00')
        snapshot = processar_snapshot_pbx(dispositivos, registrados, chamadas, mapa_analistas=mapa, agora=agora)

        # 2008 em ligação inbound
        self.assertEqual(snapshot['2008']['status'], 'Busy')
        self.assertEqual(snapshot['2008']['direction'], 'inbound')
        self.assertEqual(snapshot['2008']['number'], '85988887777')
        self.assertEqual(snapshot['2008']['seconds'], 60)

        # 2002 registrado sem chamada -> Free
        self.assertEqual(snapshot['2002']['status'], 'Free')
        self.assertIsNone(snapshot['2002']['number'])

        # 2011 desregistrado -> Offline
        self.assertEqual(snapshot['2011']['status'], 'Offline')


class TestColetorPbxPrecedencia(unittest.TestCase):
    def test_precedencia_estrita_npx_vs_pbx(self):
        cliente_fake = MagicMock()
        cliente_fake.configurado = True
        coletor = ColetorPbx(cliente=cliente_fake, mapa_analistas={
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Davi': {'ramal': '2002', 'departamento': 'InfoBrasil_Suporte'},
            'Alisson': {'ramal': '2011', 'departamento': 'InfoBrasil_Suporte'}
        })
        coletor.snapshot = {
            '2008': {'status': 'Free', 'number': None, 'direction': None, 'seconds': 0},
            '2002': {'status': 'Busy', 'number': '85999998888', 'direction': 'inbound', 'seconds': 30},
            '2011': {'status': 'Offline', 'number': None, 'direction': None, 'seconds': 0}
        }

        # 1. Agente NPX em Pausa (status 0 ou -1) -> line='npx' SEMPRE VENCE
        agentes = [
            {'code': '101', 'name': 'Thiago', 'status': 0, 'paused': True, 'queue': 'InfoBrasil_Suporte'}
        ]
        res = coletor.enriquecer_agentes(agentes)
        self.assertEqual(res[0]['line'], 'npx')
        self.assertEqual(res[0]['pbxStatus'], 'Free')
        self.assertEqual(res[0]['pbxExtension'], '2008')

        # 2. Agente NPX Ativo (status=1 Livre) -> line='npx' VENCE
        agentes = [
            {'code': '101', 'name': 'Thiago', 'status': 1, 'paused': False, 'queue': 'InfoBrasil_Suporte'}
        ]
        res = coletor.enriquecer_agentes(agentes)
        self.assertEqual(res[0]['line'], 'npx')

        # 3. Agente NPX Offline (status=0, paused=False), mas PBX Busy -> line='pbx'
        agentes = [
            {'code': '102', 'name': 'Davi', 'status': 0, 'paused': False, 'queue': 'InfoBrasil_Suporte'}
        ]
        res = coletor.enriquecer_agentes(agentes)
        self.assertEqual(res[0]['line'], 'pbx')
        self.assertEqual(res[0]['pbxStatus'], 'Busy')
        self.assertEqual(res[0]['pbxNumber'], '85999998888')

        # 4. Agente NPX Offline (status=0, paused=False) e PBX Offline -> line='off'
        agentes = [
            {'code': '103', 'name': 'Alisson', 'status': 0, 'paused': False, 'queue': 'InfoBrasil_Suporte'}
        ]
        res = coletor.enriquecer_agentes(agentes)
        self.assertEqual(res[0]['line'], 'off')
        self.assertEqual(res[0]['pbxStatus'], 'Offline')

    def test_agentes_pbx_only(self):
        coletor = ColetorPbx(cliente=MagicMock(), mapa_analistas={
            'Thiago': {'ramal': '2008', 'departamento': 'InfoBrasil_Suporte'},
            'Carol': {'ramal': '2022', 'departamento': 'InfoBrasil_SPED_Fiscal'}
        })
        coletor.snapshot = {
            '2008': {'status': 'Free', 'number': None, 'direction': None, 'seconds': 0},
            '2022': {'status': 'Busy', 'number': '85987654321', 'direction': 'outbound', 'seconds': 45}
        }

        # Se Thiago já está na NPX, apenas Carol deve ser gerada como PBX-only
        pbx_only = coletor.obter_agentes_pbx_only(codigos_npx_existentes=['4422008'], ramais_npx_casados={'2008'})
        self.assertEqual(len(pbx_only), 1)
        self.assertEqual(pbx_only[0]['name'], 'Carol')
        self.assertEqual(pbx_only[0]['code'], 'pbx-2022')
        self.assertEqual(pbx_only[0]['queue'], 'InfoBrasil_SPED_Fiscal')
        self.assertEqual(pbx_only[0]['line'], 'pbx')
        self.assertEqual(pbx_only[0]['pbxStatus'], 'Busy')
        self.assertEqual(pbx_only[0]['pbxNumber'], '85987654321')


if __name__ == '__main__':
    unittest.main()
