# -*- coding: utf-8 -*-
"""
test_backups_ftp_admin_web.py
Suíte de testes de integração Web para o Módulo Administrativo de Backups FTP e Listas Cobian.

Isolamento absoluto:
- NÃO importa bancos no top-level nem define SECRET_KEY global na descoberta de testes.
- Carrega bancos.py dinamicamente via importlib.util.spec_from_file_location com o nome
  único '_bancos_ftp_web_test', sob patch.dict de DATA_DIR temporário e sem iniciar jobs.
- sys.modules['bancos'] global NÃO é tocado, garantindo execução perfeita no discover da suíte completa.
- Limpeza rigorosa no tearDownClass removendo apenas o alias '_bancos_ftp_web_test'.
"""

import base64
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from flask import Flask


class BackupsFtpAdminWebIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Diretório temporário da classe para DATA_DIR e chaves isoladas
        cls.tmp_class_dir = tempfile.TemporaryDirectory()
        cls.data_dir = os.path.join(cls.tmp_class_dir.name, 'data')
        cls.key_dir = os.path.join(cls.tmp_class_dir.name, 'keys')
        os.makedirs(cls.data_dir, exist_ok=True)
        os.makedirs(cls.key_dir, exist_ok=True)

        cls.key_path = os.path.join(cls.key_dir, 'backups_ftp.key')
        chave_bytes = os.urandom(32)
        chave_b64 = base64.urlsafe_b64encode(chave_bytes).decode('ascii')
        with open(cls.key_path, 'w', encoding='ascii') as f:
            f.write(chave_b64)

        # 2. Garante raiz do projeto no sys.path para imports internos
        cls.raiz_projeto = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if cls.raiz_projeto not in sys.path:
            sys.path.insert(0, cls.raiz_projeto)

        # 3. Patch das variáveis de ambiente durante o import isolado
        cls.env_patcher = patch.dict(os.environ, {
            'DATA_DIR': cls.data_dir,
            'BACKUPS_FTP_KEY_PATH': cls.key_path,
            'SECRET_KEY': 'test-secret-key-com-mais-de-32-caracteres-de-entropia-12345',
            'SERVIDORES_CONFIG': '{}',
            'ADMIN_MASTER_EMAIL': '',
            'ADMIN_MASTER_PASSWORD': '',
            'SESSION_COOKIE_SECURE': '0'
        })
        cls.env_patcher.start()

        # 4. Importa bancos.py sob o namespace único '_bancos_ftp_web_test'
        caminho_bancos = os.path.join(cls.raiz_projeto, 'bancos.py')
        with patch('apscheduler.schedulers.background.BackgroundScheduler.start'):
            spec = importlib.util.spec_from_file_location('_bancos_ftp_web_test', caminho_bancos)
            cls.bancos_mod = importlib.util.module_from_spec(spec)
            sys.modules['_bancos_ftp_web_test'] = cls.bancos_mod
            spec.loader.exec_module(cls.bancos_mod)

        # 5. Importa seguranca e backups_ftp_admin
        import seguranca
        cls.seguranca_mod = seguranca
        from backups_ftp_admin import configurar_backups_ftp_admin, cifrar_dado
        cls.configurar_backups_ftp_admin = configurar_backups_ftp_admin
        cls.cifrar_dado = cifrar_dado

        # 6. Inicializa o banco de dados do sistema no DATA_DIR temporário
        cls.bancos_mod.init_db_sistema()

    @classmethod
    def tearDownClass(cls):
        # Remove estritamente o alias isolado de sys.modules
        sys.modules.pop('_bancos_ftp_web_test', None)
        try:
            cls.env_patcher.stop()
        except Exception:
            pass
        try:
            cls.tmp_class_dir.cleanup()
        except Exception:
            pass

    def setUp(self):
        # Status mockado para evitar consultas HTTP de rede reais ao servidor de backups
        self.status_mock = {
            'clientes': [
                {
                    'alias': 'CLIENTE-ALFA',
                    'status': 'OK',
                    'idade': 'a menos de 1 dia',
                    'timestamp': '2026-11-04 10:00:00',
                    'caminho': '/backups/alfa.bkp'
                },
                {
                    'alias': 'CLIENTE-BETA',
                    'status': 'ATRASADO',
                    'idade': 'a mais de 2 dias',
                    'timestamp': '2026-11-02 08:30:00',
                    'caminho': '/backups/beta.bkp'
                }
            ],
            'total_ok': 1,
            'total_atrasado': 1,
            'total_erro': 0,
            'ultima_atualizacao': '04/11/2026 10:30'
        }

        self.patcher_status = patch.object(self.bancos_mod, 'obter_status_backups_ftp', return_value=self.status_mock)
        self.patcher_status.start()
        self.patcher_status_priv = patch.object(self.bancos_mod, '_obter_status_backups_ftp', return_value=self.status_mock)
        self.patcher_status_priv.start()

        # Restaura a chave válida de criptografia no ambiente
        os.environ['BACKUPS_FTP_KEY_PATH'] = self.key_path

        # Limpa e reinsere registros necessários no banco SQLite isolado
        conn = self.bancos_mod.get_db_connection()
        conn.execute("DELETE FROM backups_ftp_auditoria")
        conn.execute("DELETE FROM backups_ftp_listas_versoes")
        conn.execute("DELETE FROM backups_ftp_rotinas")
        conn.execute("INSERT OR REPLACE INTO usuarios (id, nome, email, senha_hash, eh_master, ativo) VALUES (1, 'Admin Master', 'admin@teste.com', 'hash123', 1, 1)")
        conn.execute("INSERT OR REPLACE INTO usuarios (id, nome, email, senha_hash, eh_master, ativo) VALUES (2, 'Operador Comum', 'comum@teste.com', 'hash123', 0, 1)")
        conn.commit()
        conn.close()

        # Inicializa App Flask de teste com o blueprint isolado
        self.app = Flask(__name__)
        self.seguranca_mod.aplicar_config_flask(self.app)
        self.app.register_blueprint(self.bancos_mod.bancos_bp)

        self.client = self.app.test_client()

    def tearDown(self):
        self.patcher_status_priv.stop()
        self.patcher_status.stop()

    # -------------------------------------------------------------------------
    # Helpers de Sessão e Autenticação
    # -------------------------------------------------------------------------

    def _login_master(self, client=None, user_id=1, user_nome='Admin Master'):
        cl = client or self.client
        with cl.session_transaction() as sess:
            sess['logged_in'] = True
            sess['eh_master'] = True
            sess['user_id'] = user_id
            sess['user_nome'] = user_nome
            sess['_csrf_token'] = 'csrf-token-master-123'

    def _login_comum(self, client=None, user_id=2, user_nome='Operador Comum'):
        cl = client or self.client
        with cl.session_transaction() as sess:
            sess['logged_in'] = True
            sess['eh_master'] = False
            sess['user_id'] = user_id
            sess['user_nome'] = user_nome
            sess['_csrf_token'] = 'csrf-token-comum-456'

    def _obter_csrf(self, client=None):
        cl = client or self.client
        with cl.session_transaction() as sess:
            return sess.get('_csrf_token', '')

    # -------------------------------------------------------------------------
    # Cenário 1: Regressão Pública e Visão Master
    # -------------------------------------------------------------------------

    def test_regressao_publica_get_backups_ftp_anonimo(self):
        """Visitante anônimo deve ver o layout público sem botões nem assets administrativos."""
        resp = self.client.get('/backups-ftp')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn('CLIENTE-ALFA', html)
        self.assertIn('CLIENTE-BETA', html)

        self.assertNotIn('btn-gerenciar-ftp', html)
        self.assertNotIn('modal-backups-ftp-admin', html)
        self.assertNotIn('backups_ftp_admin.js', html)
        self.assertNotIn('backups_ftp_admin.css', html)

        cache_control = resp.headers.get('Cache-Control', '')
        self.assertNotIn('no-store', cache_control)

    def test_regressao_publica_get_backups_ftp_operador_comum(self):
        """Operador autenticado não-master também não deve receber controles administrativos."""
        self._login_comum()
        resp = self.client.get('/backups-ftp')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn('CLIENTE-ALFA', html)
        self.assertNotIn('btn-gerenciar-ftp', html)
        self.assertNotIn('modal-backups-ftp-admin', html)
        self.assertNotIn('backups_ftp_admin.js', html)

        cache_control = resp.headers.get('Cache-Control', '')
        self.assertNotIn('no-store', cache_control)

    def test_visao_master_get_backups_ftp(self):
        """Usuário Master deve receber o botão de gerenciar, modais, scripts e cabeçalhos no-store."""
        self._login_master()
        resp = self.client.get('/backups-ftp')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn('no-store', resp.headers.get('Cache-Control', ''))
        self.assertIn('private', resp.headers.get('Cache-Control', ''))
        self.assertEqual(resp.headers.get('Vary', ''), 'Cookie')

        self.assertIn('btn-gerenciar-ftp', html)
        self.assertIn('data-alias="CLIENTE-ALFA"', html)
        self.assertIn('Painel de Rotinas Privadas', html)
        self.assertIn('modal-backups-ftp-admin', html)
        self.assertIn('backups_ftp_admin.js', html)
        self.assertIn('backups_ftp_admin.css', html)
        self.assertIn('name="csrf-token"', html)

        # Controles do Gerador Cobian (Marco 3)
        self.assertIn('btn-abrir-gerador-list', html)
        self.assertIn('modal-sub-gerar-lista-cobian', html)
        self.assertIn('ftp-gerador-preset', html)
        self.assertIn('ftp-check-reaproveitar-hash', html)
        self.assertIn('ftp-gerador-comentario-versao', html)
        self.assertIn('btn-salvar-versao-gerada', html)
        self.assertIn('btn-baixar-lista-gerada', html)

    # -------------------------------------------------------------------------
    # Cenário 2: Novo Endpoint GET /admin/backups-ftp/clientes
    # -------------------------------------------------------------------------

    def test_endpoint_get_clientes_uniao_feed_e_banco_privado(self):
        """GET /admin/backups-ftp/clientes deve unir aliases do feed com os cadastrados no banco privado."""
        self._login_master()
        csrf = self._obter_csrf()

        # Cadastra cliente que não existe no feed (cliente fora do feed)
        resp_cria = self.client.post(
            '/admin/backups-ftp/rotina',
            json={'cliente_alias': 'CLIENTE-PRIVADO-FORA-DO-FEED', 'nome_rotina': 'Cobian Especial'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_cria.status_code, 201)

        resp_clientes = self.client.get('/admin/backups-ftp/clientes')
        self.assertEqual(resp_clientes.status_code, 200)
        data = resp_clientes.get_json()
        self.assertTrue(data['sucesso'])
        lista = data['clientes']

        self.assertIn('CLIENTE-ALFA', lista)
        self.assertIn('CLIENTE-BETA', lista)
        self.assertIn('CLIENTE-PRIVADO-FORA-DO-FEED', lista)

        self._login_comum()
        resp_comum = self.client.get('/admin/backups-ftp/clientes')
        self.assertEqual(resp_comum.status_code, 403)

    # -------------------------------------------------------------------------
    # Cenário 3: Guardião de Acesso (302/400/403)
    # -------------------------------------------------------------------------

    def test_bloqueio_endpoints_admin_visitante_anonimo(self):
        """Endpoints sob /admin/backups-ftp/... exigem login e CSRF para requisições anônimas."""
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        self.assertEqual(resp_get.status_code, 302)
        self.assertIn('/admin/login', resp_get.headers.get('Location', ''))

        resp_post_sem_csrf = self.client.post('/admin/backups-ftp/rotina', json={'cliente_alias': 'CLIENTE-ALFA'})
        self.assertEqual(resp_post_sem_csrf.status_code, 400)

        with self.client.session_transaction() as sess:
            sess['_csrf_token'] = 'token-anonimo'

        # POST via Form data com CSRF válido redireciona para login (302)
        resp_post_com_csrf = self.client.post(
            '/admin/backups-ftp/rotina',
            data={'cliente_alias': 'CLIENTE-ALFA', 'csrf_token': 'token-anonimo'}
        )
        self.assertEqual(resp_post_com_csrf.status_code, 302)
        self.assertIn('/admin/login', resp_post_com_csrf.headers.get('Location', ''))

        # POST via JSON com CSRF válido também redireciona para login (302 com next=/backups-ftp)
        resp_post_json_com_csrf = self.client.post(
            '/admin/backups-ftp/rotina',
            json={'cliente_alias': 'CLIENTE-ALFA'},
            headers={'X-CSRF-Token': 'token-anonimo'}
        )
        self.assertEqual(resp_post_json_com_csrf.status_code, 302)
        self.assertIn('/admin/login', resp_post_json_com_csrf.headers.get('Location', ''))
        self.assertIn('next=/backups-ftp', resp_post_json_com_csrf.headers.get('Location', ''))
        self.assertIn('no-store', resp_post_json_com_csrf.headers.get('Cache-Control', ''))
        self.assertEqual(resp_post_json_com_csrf.headers.get('Vary', ''), 'Cookie')

        # Novas rotas de geração/download Cobian também devem redirecionar anônimo
        resp_anon_gerar = self.client.post(
            '/admin/backups-ftp/rotina/1/gerar-lista',
            json={'preset': 'fbk', 'host': 'ims.iprojectti.com.br', 'porta': '8821', 'usuario': 'u'},
            headers={'X-CSRF-Token': 'token-anonimo'}
        )
        self.assertEqual(resp_anon_gerar.status_code, 302)
        self.assertIn('/admin/login', resp_anon_gerar.headers.get('Location', ''))

        resp_anon_down = self.client.post(
            '/admin/backups-ftp/rotina/1/download-lista-gerada',
            json={'preset': 'fbk', 'host': 'ims.iprojectti.com.br', 'porta': '8821', 'usuario': 'u'},
            headers={'X-CSRF-Token': 'token-anonimo'}
        )
        self.assertEqual(resp_anon_down.status_code, 302)
        self.assertIn('/admin/login', resp_anon_down.headers.get('Location', ''))

    def test_bloqueio_endpoints_admin_operador_nao_master(self):
        """Operador autenticado mas sem eh_master deve receber 403 Forbidden em todos os endpoints."""
        self._login_comum()
        csrf = self._obter_csrf()

        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        self.assertEqual(resp_get.status_code, 403)

        resp_post = self.client.post(
            '/admin/backups-ftp/rotina',
            json={'cliente_alias': 'CLIENTE-ALFA', 'nome_rotina': 'Tentativa'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_post.status_code, 403)

        resp_del = self.client.delete('/admin/backups-ftp/rotina/1', headers={'X-CSRF-Token': csrf})
        self.assertEqual(resp_del.status_code, 403)

        resp_rev = self.client.post('/admin/backups-ftp/rotina/1/revelar-senha', headers={'X-CSRF-Token': csrf})
        self.assertEqual(resp_rev.status_code, 403)

        resp_rev_extra = self.client.post(
            '/admin/backups-ftp/rotina/1/revelar-extra',
            json={'chave': 'teste'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_rev_extra.status_code, 403)

        resp_comum_gerar = self.client.post(
            '/admin/backups-ftp/rotina/1/gerar-lista',
            json={'preset': 'fbk', 'host': 'ims.iprojectti.com.br', 'porta': '8821', 'usuario': 'u'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_comum_gerar.status_code, 403)

        resp_comum_down = self.client.post(
            '/admin/backups-ftp/rotina/1/download-lista-gerada',
            json={'preset': 'fbk', 'host': 'ims.iprojectti.com.br', 'porta': '8821', 'usuario': 'u'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_comum_down.status_code, 403)

    # -------------------------------------------------------------------------
    # Cenário 4: Ciclo CRUD de Rotinas, Campo lancamento_em e Concorrência
    # -------------------------------------------------------------------------

    def test_ciclo_completo_rotinas_e_controle_de_concorrencia(self):
        """Testa criação, listagem mascarada, atualização com revision e concorrência (409)."""
        self._login_master()
        csrf = self._obter_csrf()

        # 1. Criação de nova rotina via POST incluindo lancamento_em
        payload_nova = {
            'cliente_alias': 'CLIENTE-ALFA',
            'nome_rotina': 'Backup Principal Cobian',
            'servidor': 'ftp.clientealfa.com.br',
            'porta': '2121',
            'usuario': 'usr_backup',
            'senha': 'SenhaForte123!@#',
            'responsavel': 'Carlos TI',
            'lancamento_em': '2026-11-04',
            'notas': 'Rotina noturna às 23h',
            'dados_extras': {'pasta_remota': '/backups/cobian', 'timeout': '60'},
            'extras_sensiveis': {'pasta_remota': True, 'timeout': False},
            'status': 1
        }
        resp_cria = self.client.post(
            '/admin/backups-ftp/rotina',
            json=payload_nova,
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_cria.status_code, 201)
        data_cria = resp_cria.get_json()
        self.assertTrue(data_cria['sucesso'])
        rotina_id = data_cria['rotina']['id']
        self.assertEqual(data_cria['rotina']['revision'], 1)
        self.assertEqual(data_cria['rotina']['lancamento_em'], '2026-11-04')
        self.assertTrue(data_cria['rotina']['senha_configurada'])

        # 2. Listagem de rotinas do cliente: confirma mascaramento e ausência de senha plana
        resp_list = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        self.assertEqual(resp_list.status_code, 200)
        data_list = resp_list.get_json()
        rotinas = data_list['rotinas']
        self.assertEqual(len(rotinas), 1)
        r = rotinas[0]
        self.assertEqual(r['id'], rotina_id)
        self.assertEqual(r['nome_rotina'], 'Backup Principal Cobian')
        self.assertEqual(r['lancamento_em'], '2026-11-04')
        self.assertTrue(r['senha_configurada'])
        self.assertNotIn('senha', r)
        self.assertIn('pasta_remota', r['dados_extras'])
        self.assertIsNone(r['dados_extras']['pasta_remota'])
        self.assertEqual(r['dados_extras']['timeout'], '60')
        self.assertTrue(r['extras_sensiveis']['pasta_remota'])
        self.assertFalse(r['extras_sensiveis']['timeout'])

        # 3. Atualização com revision válida (1 -> 2) e preservação de extra via null
        payload_update = {
            'expected_revision': 1,
            'nome_rotina': 'Backup Principal Cobian Atualizado',
            'servidor': 'ftp.clientealfa.com.br',
            'porta': '2121',
            'usuario': 'usr_backup',
            'responsavel': 'Carlos TI',
            'lancamento_em': '2026-11-05',
            'notas': 'Rotina alterada para as 01h',
            'dados_extras': {'pasta_remota': None, 'novo_extra': 'valor_novo'},
            'extras_sensiveis': {'pasta_remota': True, 'novo_extra': False}
        }
        resp_up = self.client.put(
            f'/admin/backups-ftp/rotina/{rotina_id}',
            json=payload_update,
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_up.status_code, 200)
        data_up = resp_up.get_json()
        self.assertEqual(data_up['rotina']['revision'], 2)
        self.assertEqual(data_up['rotina']['lancamento_em'], '2026-11-05')

        # 4. Concorrência: tentativa de atualização com revision antiga (1) retorna 409
        resp_conflito = self.client.put(
            f'/admin/backups-ftp/rotina/{rotina_id}',
            json={'expected_revision': 1, 'nome_rotina': 'Tentativa Concorrente'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_conflito.status_code, 409)
        self.assertIn('modificado por outro operador', resp_conflito.get_json()['erro'])

        # 5. Remoção explícita de campo extra via remover_extras
        resp_rem_extra = self.client.put(
            f'/admin/backups-ftp/rotina/{rotina_id}',
            json={'expected_revision': 2, 'remover_extras': ['novo_extra']},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_rem_extra.status_code, 200)
        self.assertNotIn('novo_extra', resp_rem_extra.get_json()['rotina']['extras_configurados'])

        # 6. Exclusão da rotina
        resp_del = self.client.delete(f'/admin/backups-ftp/rotina/{rotina_id}', headers={'X-CSRF-Token': csrf})
        self.assertEqual(resp_del.status_code, 200)

        conn = self.bancos_mod.get_db_connection()
        try:
            rem = conn.execute("SELECT COUNT(*) FROM backups_ftp_rotinas WHERE id = ?", (rotina_id,)).fetchone()[0]
            self.assertEqual(rem, 0)
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Cenário 5: Revelação e Cópia de Senhas Auditadas
    # -------------------------------------------------------------------------

    def test_revelar_e_copiar_senha_com_auditoria_no_sqlite(self):
        """Revelar e Copiar senha devem descriptografar e gravar auditoria comprovada no SQLite."""
        self._login_master()
        csrf = self._obter_csrf()

        resp_cria = self.client.post(
            '/admin/backups-ftp/rotina',
            json={
                'cliente_alias': 'CLIENTE-ALFA',
                'nome_rotina': 'FTP Segredo',
                'usuario': 'sec_user',
                'senha': 'SenhaConfidencial#2026'
            },
            headers={'X-CSRF-Token': csrf}
        )
        rotina_id = resp_cria.get_json()['rotina']['id']

        # Revelar Senha
        resp_rev = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/revelar-senha',
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_rev.status_code, 200)
        data_rev = resp_rev.get_json()
        self.assertTrue(data_rev['sucesso'])
        self.assertEqual(data_rev['senha'], 'SenhaConfidencial#2026')

        # Copiar Senha
        resp_cop = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/copiar-senha',
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_cop.status_code, 200)
        data_cop = resp_cop.get_json()
        self.assertTrue(data_cop['sucesso'])
        self.assertEqual(data_cop['senha'], 'SenhaConfidencial#2026')

        # Comprova auditoria no SQLite
        conn = self.bancos_mod.get_db_connection()
        try:
            cursor = conn.cursor()
            audits_rev = cursor.execute("""
                SELECT * FROM backups_ftp_auditoria
                WHERE rotina_id = ? AND acao = 'revelar_senha'
            """, (rotina_id,)).fetchall()
            self.assertEqual(len(audits_rev), 1)
            self.assertEqual(audits_rev[0]['campo_ou_segredo'], 'senha')
            self.assertEqual(audits_rev[0]['resultado'], 'sucesso')

            audits_cop = cursor.execute("""
                SELECT * FROM backups_ftp_auditoria
                WHERE rotina_id = ? AND acao = 'copiar_senha'
            """, (rotina_id,)).fetchall()
            self.assertEqual(len(audits_cop), 1)
            self.assertEqual(audits_cop[0]['resultado'], 'sucesso')
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Cenário 6: Revelação e Cópia de Campos Extras Auditadas
    # -------------------------------------------------------------------------

    def test_revelar_e_copiar_campos_extras_com_auditoria_no_sqlite(self):
        """Revelar e Copiar campos extras devem descriptografar e gravar auditoria comprovada no SQLite."""
        self._login_master()
        csrf = self._obter_csrf()

        resp_cria = self.client.post(
            '/admin/backups-ftp/rotina',
            json={
                'cliente_alias': 'CLIENTE-ALFA',
                'nome_rotina': 'FTP Extras',
                'dados_extras': {'token_api': 'SuperTokenPrivado#2026'}
            },
            headers={'X-CSRF-Token': csrf}
        )
        rotina_id = resp_cria.get_json()['rotina']['id']

        # Revelar Extra
        resp_rev_extra = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra',
            json={'chave': 'token_api'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_rev_extra.status_code, 200)
        data_rev = resp_rev_extra.get_json()
        self.assertTrue(data_rev['sucesso'])
        self.assertEqual(data_rev['chave'], 'token_api')
        self.assertEqual(data_rev['valor'], 'SuperTokenPrivado#2026')

        # Copiar Extra
        resp_cop_extra = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/copiar-extra',
            json={'chave': 'token_api'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_cop_extra.status_code, 200)
        data_cop = resp_cop_extra.get_json()
        self.assertTrue(data_cop['sucesso'])
        self.assertEqual(data_cop['valor'], 'SuperTokenPrivado#2026')

        # Comprova auditoria genérica (sem expor a chave confidencial no campo_ou_segredo)
        conn = self.bancos_mod.get_db_connection()
        try:
            cursor = conn.cursor()
            audits_rev = cursor.execute("""
                SELECT * FROM backups_ftp_auditoria
                WHERE rotina_id = ? AND acao = 'revelar_extra'
            """, (rotina_id,)).fetchall()
            self.assertEqual(len(audits_rev), 1)
            self.assertEqual(audits_rev[0]['campo_ou_segredo'], 'campo_extra')

            audits_cop = cursor.execute("""
                SELECT * FROM backups_ftp_auditoria
                WHERE rotina_id = ? AND acao = 'copiar_extra'
            """, (rotina_id,)).fetchall()
            self.assertEqual(len(audits_cop), 1)
            self.assertEqual(audits_cop[0]['campo_ou_segredo'], 'campo_extra')
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Cenário 7: Upload e Inspeção de Listas Cobian (.list / .lst)
    # -------------------------------------------------------------------------

    def test_upload_e_inspecao_lista_cobian(self):
        """Upload de arquivo Cobian válido com BOM, limite de tamanho e inspeção auditada."""
        self._login_master()
        csrf = self._obter_csrf()

        resp_cria = self.client.post(
            '/admin/backups-ftp/rotina',
            json={'cliente_alias': 'CLIENTE-ALFA', 'nome_rotina': 'Cobian Teste'},
            headers={'X-CSRF-Token': csrf}
        )
        rotina_id = resp_cria.get_json()['rotina']['id']

        conteudo_lista = (
            "{\n"
            "// Tarefa Cobian Backup\n"
            "Name=Backup Base Principal\n"
            "Source=C:\\Dados\n"
            "Destination=ftp://usr:pass@servidor/bkp\n"
            "}\n"
        )
        bytes_lista = b'\xef\xbb\xbf' + conteudo_lista.encode('utf-8')

        # 1. Upload via POST com FormData
        data_form = {
            'arquivo': (BytesIO(bytes_lista), 'tarefas.lst'),
            'comentario': 'Versão inicial de teste',
            'csrf_token': csrf
        }
        resp_upload = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
            data=data_form,
            content_type='multipart/form-data',
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_upload.status_code, 201)
        data_up = resp_upload.get_json()
        versao_id = data_up['versao']['id']
        self.assertEqual(data_up['versao']['versao_numero'], 1)
        self.assertEqual(data_up['versao']['encoding_detectado'], 'UTF-8')

        # 2. Listar versões
        resp_listas = self.client.get(f'/admin/backups-ftp/rotina/{rotina_id}/listas')
        self.assertEqual(resp_listas.status_code, 200)
        versoes = resp_listas.get_json()['versoes']
        self.assertEqual(len(versoes), 1)
        self.assertEqual(versoes[0]['id'], versao_id)

        # 3. Inspecionar conteúdo decifrado (POST auditado)
        resp_conteudo = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/lista/{versao_id}/ver-conteudo',
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_conteudo.status_code, 200)
        data_cont = resp_conteudo.get_json()
        self.assertTrue(data_cont['sucesso'])
        self.assertEqual(data_cont['conteudo'], conteudo_lista)

        conn = self.bancos_mod.get_db_connection()
        try:
            audits_list = conn.execute("""
                SELECT * FROM backups_ftp_auditoria
                WHERE rotina_id = ? AND acao = 'inspecionar_list'
            """, (rotina_id,)).fetchall()
            self.assertEqual(len(audits_list), 1)
            self.assertEqual(audits_list[0]['campo_ou_segredo'], 'conteudo_lista')
        finally:
            conn.close()

        # 4. Rejeição de arquivo > 2 MiB (HTTP 413)
        bytes_gigante = b'X' * (2 * 1024 * 1024 + 10)
        data_gigante = {
            'arquivo': (BytesIO(bytes_gigante), 'tarefas_pesadas.list'),
            'csrf_token': csrf
        }
        resp_413 = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
            data=data_gigante,
            content_type='multipart/form-data',
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_413.status_code, 413)

    # -------------------------------------------------------------------------
    # Cenário 8: Fail-Closed em Ausência de Chave de Criptografia
    # -------------------------------------------------------------------------

    def test_fail_closed_sem_chave_de_criptografia(self):
        """Se a chave não existir, endpoints administrativos retornam 503 sem afetar GET /backups-ftp público."""
        os.environ['BACKUPS_FTP_KEY_PATH'] = os.path.join(self.tmp_class_dir.name, 'chave_inexistente.key')

        self._login_master()
        csrf = self._obter_csrf()

        resp_admin = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        self.assertEqual(resp_admin.status_code, 503)
        self.assertIn('indisponível', resp_admin.get_json().get('erro', '').lower())

        resp_publica = self.client.get('/backups-ftp')
        self.assertEqual(resp_publica.status_code, 200)
        self.assertIn('CLIENTE-ALFA', resp_publica.get_data(as_text=True))

    # -------------------------------------------------------------------------
    # Cenário 9: CSRF Obrigatório para Master em Todas as 9 Rotas Mutantes
    # -------------------------------------------------------------------------

    def test_csrf_obrigatorio_master_todas_rotas_mutantes(self):
        """
        Master logado deve receber 400 em todas as 9 rotas mutantes quando o CSRF for ausente ou inválido.
        Comprova ausência total de mutação no SQLite e ausência de novos registros de auditoria.
        """
        self._login_master()
        csrf_valido = self._obter_csrf()

        # 1. Criação de rotina base e versão de lista base com token CSRF válido
        payload_base = {
            'cliente_alias': 'CLIENTE-ALFA',
            'nome_rotina': 'Rotina Base Intacta',
            'servidor': 'ftp.alfa.com',
            'porta': '21',
            'usuario': 'user_base',
            'senha': 'SenhaBaseValida123!',
            'dados_extras': {'pasta_remota': '/base', 'token_auth': 'segredo123'},
            'status': 1
        }
        resp_cria_base = self.client.post(
            '/admin/backups-ftp/rotina',
            json=payload_base,
            headers={'X-CSRF-Token': csrf_valido}
        )
        self.assertEqual(resp_cria_base.status_code, 201)
        rotina_id = resp_cria_base.get_json()['rotina']['id']

        bytes_lista = b'\xef\xbb\xbf{\nName=BaseTask\n}\n'
        resp_upload_base = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
            data={
                'arquivo': (BytesIO(bytes_lista), 'base.lst'),
                'csrf_token': csrf_valido
            },
            content_type='multipart/form-data',
            headers={'X-CSRF-Token': csrf_valido}
        )
        self.assertEqual(resp_upload_base.status_code, 201)
        versao_id = resp_upload_base.get_json()['versao']['id']

        # 2. Registra o estado inicial das tabelas no SQLite
        conn = self.bancos_mod.get_db_connection()
        try:
            total_rotinas_ini = conn.execute("SELECT COUNT(*) FROM backups_ftp_rotinas").fetchone()[0]
            total_versoes_ini = conn.execute("SELECT COUNT(*) FROM backups_ftp_listas_versoes").fetchone()[0]
            total_auditorias_ini = conn.execute("SELECT COUNT(*) FROM backups_ftp_auditoria").fetchone()[0]
            rotina_antes = conn.execute("SELECT revision, atualizado_em FROM backups_ftp_rotinas WHERE id = ?", (rotina_id,)).fetchone()
            rev_ini = rotina_antes['revision']
        finally:
            conn.close()

        # 3. Definição das 9 rotas mutantes com seus respectivos métodos, paths e geradores de chamada
        rotas_mutantes = [
            # 1: Criar rotina
            (
                '1_criar_rotina',
                'POST',
                '/admin/backups-ftp/rotina',
                lambda: {'json': {'cliente_alias': 'CLIENTE-ALFA', 'nome_rotina': 'Mutacao Fraudulenta'}}
            ),
            # 2: Atualizar rotina (PUT)
            (
                '2_atualizar_rotina',
                'PUT',
                f'/admin/backups-ftp/rotina/{rotina_id}',
                lambda: {'json': {'expected_revision': rev_ini, 'nome_rotina': 'Nome Alterado Fraude'}}
            ),
            # 3: Excluir rotina (DELETE)
            (
                '3_excluir_rotina',
                'DELETE',
                f'/admin/backups-ftp/rotina/{rotina_id}',
                lambda: {}
            ),
            # 4: Upload lista Cobian
            (
                '4_upload_lista',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                lambda: {
                    'data': {'arquivo': (BytesIO(b'\xef\xbb\xbf{\nName=TentativaInvalida\n}\n'), 'fraude.lst')},
                    'content_type': 'multipart/form-data'
                }
            ),
            # 5: Revelar senha
            (
                '5_revelar_senha',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/revelar-senha',
                lambda: {}
            ),
            # 6: Copiar senha
            (
                '6_copiar_senha',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/copiar-senha',
                lambda: {}
            ),
            # 7: Revelar extra
            (
                '7_revelar_extra',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra',
                lambda: {'json': {'chave': 'pasta_remota'}}
            ),
            # 8: Copiar extra
            (
                '8_copiar_extra',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/copiar-extra',
                lambda: {'json': {'chave': 'pasta_remota'}}
            ),
            # 9: Ver conteúdo lista
            (
                '9_ver_conteudo_lista',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/lista/{versao_id}/ver-conteudo',
                lambda: {}
            ),
            # 10: Gerar lista Cobian (.lst)
            (
                '10_gerar_lista_cobian',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/gerar-lista',
                lambda: {'json': {'preset': 'fbk', 'host': 'ims.iprojectti.com.br', 'porta': '8821', 'usuario': 'user_base'}}
            ),
            # 11: Download direto de lista gerada (.lst)
            (
                '11_download_lista_gerada',
                'POST',
                f'/admin/backups-ftp/rotina/{rotina_id}/download-lista-gerada',
                lambda: {'json': {'preset': 'fbk', 'host': 'ims.iprojectti.com.br', 'porta': '8821', 'usuario': 'user_base'}}
            )
        ]

        # 4. Execução dos testes em cada rota para token ausente e token inválido
        for nome_rota, metodo, url, gerador_kwargs in rotas_mutantes:
            for cenario_token in ('token_ausente', 'token_invalido'):
                with self.subTest(rota=nome_rota, cenario=cenario_token):
                    # Limpa o rate limit store antes de cada tentativa para evitar HTTP 429
                    self.seguranca_mod._rate_limit_store.clear()

                    headers = {}
                    if cenario_token == 'token_invalido':
                        headers['X-CSRF-Token'] = 'token-falso-e-invalido-999'

                    chamada_kwargs = gerador_kwargs()
                    chamada_kwargs['headers'] = headers

                    if metodo == 'POST':
                        resp = self.client.post(url, **chamada_kwargs)
                    elif metodo == 'PUT':
                        resp = self.client.put(url, **chamada_kwargs)
                    elif metodo == 'DELETE':
                        resp = self.client.delete(url, **chamada_kwargs)
                    else:
                        self.fail(f"Método não suportado: {metodo}")

                    self.assertEqual(
                        resp.status_code, 400,
                        f"Esperado 400 para {nome_rota} no cenário {cenario_token}, obtido {resp.status_code}"
                    )
                    data_resp = resp.get_json()
                    self.assertIsNotNone(data_resp)
                    self.assertIn('erro', data_resp)

        # 5. Validação rigorosa de ausência de mutação e ausência de novas auditorias no SQLite
        conn = self.bancos_mod.get_db_connection()
        try:
            total_rotinas_fim = conn.execute("SELECT COUNT(*) FROM backups_ftp_rotinas").fetchone()[0]
            total_versoes_fim = conn.execute("SELECT COUNT(*) FROM backups_ftp_listas_versoes").fetchone()[0]
            total_auditorias_fim = conn.execute("SELECT COUNT(*) FROM backups_ftp_auditoria").fetchone()[0]
            rotina_depois = conn.execute("SELECT revision, nome_rotina_cifrado FROM backups_ftp_rotinas WHERE id = ?", (rotina_id,)).fetchone()

            # Nenhuma rotina adicionada ou excluída
            self.assertEqual(total_rotinas_fim, total_rotinas_ini)
            # Nenhuma versão adicionada ou excluída
            self.assertEqual(total_versoes_fim, total_versoes_ini)
            # NENHUM registro novo de auditoria gravado (bloqueio CSRF ocorre antes da auditoria)
            self.assertEqual(total_auditorias_fim, total_auditorias_ini)
            # A rotina existente não teve sua revision incrementada
            self.assertEqual(rotina_depois['revision'], rev_ini)
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Cenário 10: Checkbox Confidencial por Campo Extra (Toggles e Legado)
    # -------------------------------------------------------------------------

    def test_checkbox_confidencial_extras_toggles_e_legado(self):
        """
        Valida ciclo de vida do checkbox Confidencial em extras:
        1. Criação com campos confidenciais (mascarados como None) e não-confidenciais (texto claro).
        2. Toggle Confidencial -> Não-Confidencial com envio de null preserva o valor no banco e passa a exibi-lo em claro.
        3. Registro de auditoria com acao='alterar_sensibilidade_extra'.
        4. Toggle Não-Confidencial -> Confidencial com envio de null preserva o valor e reativa máscara / revelação auditada.
        5. Formato legado sem flag de sensibilidade interpretado estritamente como sensivel=True.
        """
        self._login_master()
        csrf = self._obter_csrf()

        # 1. Criação com 1 campo confidencial e 1 não-confidencial
        payload_cria = {
            'cliente_alias': 'CLIENTE-ALFA',
            'nome_rotina': 'Rotina Extras Hibridos',
            'dados_extras': {
                'token_secreto': 'SenhaSuperPrivada#99',
                'pasta_destino': '/backups/publico'
            },
            'extras_sensiveis': {
                'token_secreto': True,
                'pasta_destino': False
            }
        }
        resp_cria = self.client.post('/admin/backups-ftp/rotina', json=payload_cria, headers={'X-CSRF-Token': csrf})
        self.assertEqual(resp_cria.status_code, 201)
        data_cria = resp_cria.get_json()['rotina']
        rotina_id = data_cria['id']
        rev = data_cria['revision']

        # Listagem inicial: confidencial vem None, não-confidencial vem valor claro
        resp_list1 = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        rot1 = [r for r in resp_list1.get_json()['rotinas'] if r['id'] == rotina_id][0]
        self.assertIsNone(rot1['dados_extras']['token_secreto'])
        self.assertEqual(rot1['dados_extras']['pasta_destino'], '/backups/publico')
        self.assertTrue(rot1['extras_sensiveis']['token_secreto'])
        self.assertFalse(rot1['extras_sensiveis']['pasta_destino'])

        # 2. Toggle Confidencial -> Não-Confidencial:
        # Envia null para token_secreto (preservando o valor já salvo) e extras_sensiveis=False
        payload_toggle1 = {
            'expected_revision': rev,
            'nome_rotina': 'Rotina Extras Hibridos',
            'dados_extras': {
                'token_secreto': None,
                'pasta_destino': '/backups/publico'
            },
            'extras_sensiveis': {
                'token_secreto': False,
                'pasta_destino': False
            }
        }
        resp_toggle1 = self.client.put(f'/admin/backups-ftp/rotina/{rotina_id}', json=payload_toggle1, headers={'X-CSRF-Token': csrf})
        self.assertEqual(resp_toggle1.status_code, 200)
        rot_up1 = resp_toggle1.get_json()['rotina']
        rev = rot_up1['revision']

        # Agora token_secreto é retornado em texto claro com o valor original preservado!
        self.assertEqual(rot_up1['dados_extras']['token_secreto'], 'SenhaSuperPrivada#99')
        self.assertFalse(rot_up1['extras_sensiveis']['token_secreto'])

        # Comprova auditoria de alteração de sensibilidade no SQLite
        conn = self.bancos_mod.get_db_connection()
        try:
            audit_sens = conn.execute("""
                SELECT * FROM backups_ftp_auditoria
                WHERE rotina_id = ? AND acao = 'alterar_sensibilidade_extra'
            """, (rotina_id,)).fetchall()
            self.assertGreaterEqual(len(audit_sens), 1)
            self.assertEqual(audit_sens[0]['campo_ou_segredo'], 'campo_extra')
        finally:
            conn.close()

        # 3. Toggle Não-Confidencial -> Confidencial:
        # pasta_destino passa a ser confidencial, enviando null para preservar valor
        payload_toggle2 = {
            'expected_revision': rev,
            'nome_rotina': 'Rotina Extras Hibridos',
            'dados_extras': {
                'token_secreto': 'SenhaSuperPrivada#99',
                'pasta_destino': None
            },
            'extras_sensiveis': {
                'token_secreto': False,
                'pasta_destino': True
            }
        }
        resp_toggle2 = self.client.put(f'/admin/backups-ftp/rotina/{rotina_id}', json=payload_toggle2, headers={'X-CSRF-Token': csrf})
        self.assertEqual(resp_toggle2.status_code, 200)
        rot_up2 = resp_toggle2.get_json()['rotina']
        self.assertIsNone(rot_up2['dados_extras']['pasta_destino'])
        self.assertTrue(rot_up2['extras_sensiveis']['pasta_destino'])

        # Revelação auditada recupera o valor preservado
        resp_rev_pasta = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra',
            json={'chave': 'pasta_destino'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_rev_pasta.status_code, 200)
        self.assertEqual(resp_rev_pasta.get_json()['valor'], '/backups/publico')

        # 4. Compatibilidade com Formato Legado (JSON simples sem estrutura de sensibilidade)
        from backups_ftp_admin import cifrar_dado
        conn = self.bancos_mod.get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO backups_ftp_rotinas (
                    cliente_alias, status, revision, criado_em, atualizado_em, atualizado_por
                ) VALUES (?, 1, 1, '2026-11-05T10:00:00Z', '2026-11-05T10:00:00Z', 'master')
            """, ('CLIENTE-ALFA',))
            legada_id = cursor.lastrowid
            cursor.execute("""
                UPDATE backups_ftp_rotinas SET
                    nome_rotina_cifrado = ?,
                    dados_extras_json_cifrado = ?
                WHERE id = ?
            """, (
                cifrar_dado('Rotina Legada Puro', f"rotina:{legada_id}:nome"),
                cifrar_dado(json.dumps({'segredo_legado': 'ValorAntigo123'}), f"rotina:{legada_id}:extras"),
                legada_id
            ))
            conn.commit()
        finally:
            conn.close()

        resp_leg = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        rot_leg = [r for r in resp_leg.get_json()['rotinas'] if r['id'] == legada_id][0]
        # Campo legado é considerado sensível por default de segurança
        self.assertIn('segredo_legado', rot_leg['extras_configurados'])
        self.assertTrue(rot_leg['extras_sensiveis']['segredo_legado'])
        self.assertIsNone(rot_leg['dados_extras']['segredo_legado'])

        # Revelação do campo legado funciona perfeitamente
        resp_rev_leg = self.client.post(
            f'/admin/backups-ftp/rotina/{legada_id}/revelar-extra',
            json={'chave': 'segredo_legado'},
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_rev_leg.status_code, 200)
        self.assertEqual(resp_rev_leg.get_json()['valor'], 'ValorAntigo123')

    # -------------------------------------------------------------------------
    # Cenário 11: E2E Gerador Cobian (Salvar Versão e Download)
    # -------------------------------------------------------------------------

    def test_e2e_gerador_cobian_salvar_e_download(self):
        """Fluxo E2E: criar rotina, gerar lista via preset FBK, inspecionar conteúdo e baixar .lst."""
        self._login_master()
        csrf = self._obter_csrf()

        # 1. Cria rotina
        resp_cria = self.client.post(
            '/admin/backups-ftp/rotina',
            json={
                'cliente_alias': 'CLIENTE-E2E-COBIAN',
                'nome_rotina': 'Rotina E2E Cobian',
                'servidor': 'ims.iprojectti.com.br',
                'porta': '8821',
                'usuario': 'cli-e2e',
            },
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_cria.status_code, 201)
        rotina_id = resp_cria.get_json()['rotina']['id']

        # 2. Gera e salva como nova versão
        payload_gerador = {
            'comentario_versao': 'Versão E2E Automática',
            'preset': 'fbk',
            'host': 'ims.iprojectti.com.br',
            'porta': '8821',
            'usuario': 'cli-e2e',
            'diretorio_inicial': '/',
        }
        resp_gera = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/gerar-lista',
            json=payload_gerador,
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_gera.status_code, 201)
        versao_obj = resp_gera.get_json()['versao']
        self.assertEqual(versao_obj['versao_numero'], 1)
        self.assertEqual(versao_obj['encoding_detectado'], 'UTF-16LE')

        # 3. Inspeciona a versão gerada via endpoint oficial
        resp_insp = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/lista/{versao_obj["id"]}/ver-conteudo',
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_insp.status_code, 200)
        conteudo = resp_insp.get_json()['conteudo']
        self.assertIn('Name=IMS-CLIENTE-E2E-COBIAN-13h', conteudo)
        self.assertIn('Name=IMS-CLIENTE-E2E-COBIAN-23h', conteudo)
        self.assertIn('FTP Host=ims.iprojectti.com.br', conteudo)

        # 4. Download direto da lista gerada
        resp_down = self.client.post(
            f'/admin/backups-ftp/rotina/{rotina_id}/download-lista-gerada',
            json=payload_gerador,
            headers={'X-CSRF-Token': csrf}
        )
        self.assertEqual(resp_down.status_code, 200)
        self.assertIn('CLIENTE-E2E-COBIAN-cobian.lst', resp_down.headers.get('Content-Disposition', ''))
        raw_bytes = resp_down.get_data()
        self.assertTrue(raw_bytes.startswith(b'\xff\xfe'))
        txt_down = raw_bytes[2:].decode('utf-16le')
        self.assertIn('Name=IMS-CLIENTE-E2E-COBIAN-13h', txt_down)


if __name__ == '__main__':
    unittest.main()
