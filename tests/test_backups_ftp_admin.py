import base64
import json
import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from flask import Flask, Blueprint, session


class BackupsFtpAdminTest(unittest.TestCase):
    def setUp(self):
        # Cria diretório temporário FORA do repositório para o banco e para a chave
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, 'test_database.db')
        self.key_path = os.path.join(self.tmp_dir.name, 'secret_key.key')
        self.conn = None

        # Gera chave válida de 32 bytes em Base64 URL-safe (44 caracteres)
        chave_32bytes = os.urandom(32)
        chave_b64 = base64.urlsafe_b64encode(chave_32bytes).decode('ascii')
        with open(self.key_path, 'w', encoding='utf-8') as f:
            f.write(chave_b64)

        os.environ['BACKUPS_FTP_KEY_PATH'] = self.key_path

        # Inicializa banco SQLite
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Inicializa App Flask de teste
        self.app = Flask(__name__)
        self.app.secret_key = 'test-secret-key-12345'
        self.bp = Blueprint('backups_admin_bp', __name__)

        from backups_ftp_admin import configurar_backups_ftp_admin
        self.configurar_backups_ftp_admin = configurar_backups_ftp_admin

        def _conectar():
            return sqlite3.connect(self.db_path, check_same_thread=False)

        def _obter_status():
            return {'clientes': [{'alias': 'CLIENTE-TESTE', 'status': 'OK'}]}

        self.conectar = _conectar
        self.obter_status = _obter_status

        self.configurar_backups_ftp_admin(self.bp, conectar=_conectar, obter_status=_obter_status)
        self.app.register_blueprint(self.bp)

        self.client = self.app.test_client()

    def tearDown(self):
        if getattr(self, 'conn', None):
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        os.environ.pop('BACKUPS_FTP_KEY_PATH', None)
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _login_master(self, client=None, user_id=1, user_nome='admin_master'):
        cl = client or self.client
        with cl.session_transaction() as sess:
            sess['logged_in'] = True
            sess['eh_master'] = True
            sess['user_id'] = user_id
            sess['user_nome'] = user_nome
            sess['_csrf_token'] = 'csrf-valido-123'

    def _login_comum(self, client=None, user_id=2, user_nome='usuario_comum'):
        cl = client or self.client
        with cl.session_transaction() as sess:
            sess['logged_in'] = True
            sess['eh_master'] = False
            sess['user_id'] = user_id
            sess['user_nome'] = user_nome
            sess['_csrf_token'] = 'csrf-valido-123'

    # -------------------------------------------------------------
    # 1. Configuração e Inicialização de Tabelas
    # -------------------------------------------------------------
    def test_inicializacao_tabelas_e_idempotencia(self):
        """Verifica se as 3 tabelas e índices são criados corretamente e de forma idempotente."""
        cursor = self.conn.cursor()
        tabelas = [row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn('backups_ftp_rotinas', tabelas)
        self.assertIn('backups_ftp_listas_versoes', tabelas)
        self.assertIn('backups_ftp_auditoria', tabelas)

        # Chamada repetida deve ser idempotente
        self.configurar_backups_ftp_admin(self.bp, conectar=self.conectar, obter_status=self.obter_status)

    # -------------------------------------------------------------
    # 2. Criptografia AEAD, Key ID e Rejeição de Chave Dentro do Repo
    # -------------------------------------------------------------
    def test_crypto_aead_envelope_key_id_e_tamper(self):
        """Testa cifragem AEAD com envelope contendo key_id ('v1') e detecção de adulteração."""
        from backups_ftp_admin import cifrar_dado, decifrar_dado

        texto_original = "MinhaSenhaSuperSecreta@2026"
        contexto_aad = "rotina:1:senha"

        cifrado = cifrar_dado(texto_original, contexto_aad)
        self.assertIsInstance(cifrado, str)
        self.assertNotIn(texto_original, cifrado)

        # Envelope JSON contém 'kid': 'v1'
        env = json.loads(cifrado)
        self.assertEqual(env.get('kid'), 'v1')
        self.assertEqual(env.get('v'), 'v1')
        self.assertIn('n', env)
        self.assertIn('c', env)

        # Cifragem probabilística (nonces distintos)
        cifrado2 = cifrar_dado(texto_original, contexto_aad)
        self.assertNotEqual(cifrado, cifrado2)

        # Decifragem com contexto idêntico
        decifrado = decifrar_dado(cifrado, contexto_aad)
        self.assertEqual(decifrado, texto_original)

        # Decifragem com contexto adulterado deve falhar
        with self.assertRaises(Exception):
            decifrar_dado(cifrado, "rotina:2:senha")

        # Decifragem com ciphertext adulterado deve falhar
        env['c'] = env['c'][:-4] + "AAAA"
        with self.assertRaises(Exception):
            decifrar_dado(json.dumps(env), contexto_aad)

    def test_crypto_chave_dentro_do_repo_rejeitada(self):
        """Chave localizada dentro do repositório Git deve ser estritamente rejeitada por segurança."""
        repo_root = Path(__file__).resolve().parent.parent
        chave_dentro_repo = repo_root / "chave_fake_teste.key"
        try:
            with open(chave_dentro_repo, 'w', encoding='utf-8') as f:
                f.write(base64.urlsafe_b64encode(os.urandom(32)).decode('ascii'))

            os.environ['BACKUPS_FTP_KEY_PATH'] = str(chave_dentro_repo)
            self._login_master()

            resp = self.client.get('/admin/backups-ftp/cliente/CLIENTE-TESTE/rotinas')
            self.assertEqual(resp.status_code, 503)
            self.assertIn('erro', resp.get_json())
        finally:
            if chave_dentro_repo.exists():
                chave_dentro_repo.unlink()

    def test_crypto_chave_ausente_ou_invalida_retorna_503(self):
        """Se BACKUPS_FTP_KEY_PATH for inválido, rotas administrativas com segredos retornam 503."""
        os.environ['BACKUPS_FTP_KEY_PATH'] = os.path.join(self.tmp_dir.name, 'arquivo_inexistente.key')
        self._login_master()

        resp = self.client.get('/admin/backups-ftp/cliente/CLIENTE-TESTE/rotinas')
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertIn('erro', data)

    # -------------------------------------------------------------
    # 3. Guardião de Permissões e Cabeçalhos de Resposta
    # -------------------------------------------------------------
    def test_seguranca_anonimo_e_nao_master(self):
        """Usuário anônimo recebe 302 para login; usuário não-master recebe 403."""
        resp_anon = self.client.get('/admin/backups-ftp/cliente/CLIENTE-TESTE/rotinas')
        self.assertEqual(resp_anon.status_code, 302)
        self.assertIn('/admin/login', resp_anon.headers.get('Location', ''))

        self._login_comum()
        resp_comum = self.client.get('/admin/backups-ftp/cliente/CLIENTE-TESTE/rotinas')
        self.assertEqual(resp_comum.status_code, 403)
        self.assertIn('Requer perfil Master', resp_comum.get_json().get('erro', ''))

    def test_headers_no_store_e_vary_cookie(self):
        """Todas as respostas administrativas devem ter Cache-Control: no-store, private e Vary: Cookie."""
        self._login_master()
        resp = self.client.get('/admin/backups-ftp/cliente/CLIENTE-TESTE/rotinas')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-store, private')
        self.assertEqual(resp.headers.get('Vary'), 'Cookie')

    # -------------------------------------------------------------
    # 4. CRUD de Rotinas, Máscara de Extras e Campo lancamento_em
    # -------------------------------------------------------------
    def test_crud_rotinas_criacao_com_lancamento_em_e_mascara_de_extras(self):
        """Cria rotina com lancamento_em e garante que dados_extras retorna mascarado como {k: None}."""
        self._login_master()

        payload = {
            'cliente_alias': 'CLIENTE-ALFA',
            'nome_rotina': 'FTP Principal',
            'servidor': 'srv-ftp01.local',
            'porta': '21',
            'usuario': 'bkp_matriz',
            'senha': 'SenhaUltraSecreta123',
            'responsavel': 'Equipe Linux',
            'lancamento_em': '2026-11-04',
            'notas': 'Rotina diária madrugada',
            'dados_extras': {'tipo_conexao': 'Passivo', 'token_api': 'tok_segredo_xyz'},
            'status': 1
        }

        resp_post = self.client.post('/admin/backups-ftp/rotina', json=payload)
        self.assertEqual(resp_post.status_code, 201)
        data_post = resp_post.get_json()
        self.assertTrue(data_post['sucesso'])
        rotina_id = data_post['rotina']['id']
        self.assertEqual(data_post['rotina']['lancamento_em'], '2026-11-04')

        # Máscara de extras: valores nunca são ecoados
        self.assertEqual(data_post['rotina']['dados_extras'], {'tipo_conexao': None, 'token_api': None})
        self.assertEqual(sorted(data_post['rotina']['extras_configurados']), ['tipo_conexao', 'token_api'])
        self.assertNotIn('tok_segredo_xyz', json.dumps(data_post))

        # Consulta listagem: valores reais também nunca são entregues
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        self.assertEqual(resp_get.status_code, 200)
        data_get = resp_get.get_json()
        r = data_get['rotinas'][0]
        self.assertEqual(r['lancamento_em'], '2026-11-04')
        self.assertEqual(r['dados_extras'], {'tipo_conexao': None, 'token_api': None})
        self.assertEqual(sorted(r['extras_configurados']), ['tipo_conexao', 'token_api'])
        self.assertNotIn('tok_segredo_xyz', json.dumps(data_get))

    def test_crud_rotinas_atualizacao_preservando_senha_espacos_e_extras(self):
        """PUT preserva espaços exatos na senha, preserva extras com valor null e remove via remover_extras."""
        self._login_master()

        # Cria rotina inicial
        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-BETA',
            'nome_rotina': 'Backup SQL',
            'senha': '  Senha Com Espacos  ',
            'dados_extras': {'chave_a': 'valor_a', 'chave_b': 'valor_b'},
            'status': 1
        })
        rotina_id = resp_post.get_json()['rotina']['id']

        # Atualização enviando senha vazia (deve preservar senha anterior COM espaços)
        # e atualizando extras preservando chave_a (null) e removendo chave_b
        resp_put = self.client.put(f'/admin/backups-ftp/rotina/{rotina_id}', json={
            'expected_revision': 1,
            'nome_rotina': 'Backup SQL Alterado',
            'senha': '',
            'dados_extras': {'chave_a': None, 'chave_c': 'novo_valor_c'},
            'remover_extras': ['chave_b'],
            'status': 1
        })
        self.assertEqual(resp_put.status_code, 200)
        r_atualizada = resp_put.get_json()['rotina']
        self.assertEqual(r_atualizada['revision'], 2)
        self.assertEqual(sorted(r_atualizada['extras_configurados']), ['chave_a', 'chave_c'])

        # Revela senha para garantir que espaços NÃO foram removidos com strip
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-senha')
        self.assertEqual(resp_rev.status_code, 200)
        self.assertEqual(resp_rev.get_json()['senha'], '  Senha Com Espacos  ')

        # Revela extra chave_a para garantir que valor original foi mantido
        resp_ext_a = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra', json={'chave': 'chave_a'})
        self.assertEqual(resp_ext_a.status_code, 200)
        self.assertEqual(resp_ext_a.get_json()['valor'], 'valor_a')

        # Revela extra chave_c
        resp_ext_c = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra', json={'chave': 'chave_c'})
        self.assertEqual(resp_ext_c.status_code, 200)
        self.assertEqual(resp_ext_c.get_json()['valor'], 'novo_valor_c')

        # Chave_b removida deve retornar 404
        resp_ext_b = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra', json={'chave': 'chave_b'})
        self.assertEqual(resp_ext_b.status_code, 404)

        # Concorrência otimista com revision desatualizada
        resp_conflito = self.client.put(f'/admin/backups-ftp/rotina/{rotina_id}', json={
            'expected_revision': 1,
            'nome_rotina': 'Conflito'
        })
        self.assertEqual(resp_conflito.status_code, 409)

    def test_crud_rotinas_remocao_explicita_senha_e_exclusao_rotina(self):
        """Testa remoção de senha com remover_senha=True e exclusão física da rotina."""
        self._login_master()

        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-GAMA',
            'nome_rotina': 'Rotina Temp',
            'senha': 'SenhaPraRemover',
            'status': 1
        })
        rotina_id = resp_post.get_json()['rotina']['id']

        resp_put = self.client.put(f'/admin/backups-ftp/rotina/{rotina_id}', json={
            'expected_revision': 1,
            'remover_senha': True,
            'status': 1
        })
        self.assertEqual(resp_put.status_code, 200)
        self.assertFalse(resp_put.get_json()['rotina']['senha_configurada'])

        resp_del = self.client.delete(f'/admin/backups-ftp/rotina/{rotina_id}')
        self.assertEqual(resp_del.status_code, 200)

        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-GAMA/rotinas')
        self.assertEqual(len(resp_get.get_json()['rotinas']), 0)

    # -------------------------------------------------------------
    # 5. Revelar e Copiar Senha / Extra (Auditoria Fail-Closed Segura)
    # -------------------------------------------------------------
    def test_revelar_e_copiar_senha_auditoria_fail_closed(self):
        """Auditoria registra inserção síncrona com COMMIT. Se o commit falhar, o segredo NÃO é revelado."""
        self._login_master(user_id=10, user_nome='operador_dez')

        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-DELTA',
            'nome_rotina': 'Rotina Segredos',
            'senha': 'SegredoTopSecretissimo',
            'status': 1
        })
        rotina_id = resp_post.get_json()['rotina']['id']

        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-senha')
        self.assertEqual(resp_rev.status_code, 200)
        self.assertEqual(resp_rev.get_json()['senha'], 'SegredoTopSecretissimo')

        cursor = self.conn.cursor()
        auditorias = cursor.execute(
            "SELECT usuario_id, acao, cliente_alias, rotina_id, resultado FROM backups_ftp_auditoria WHERE rotina_id=? AND acao='revelar_senha'",
            (rotina_id,)
        ).fetchall()
        self.assertEqual(len(auditorias), 1)
        self.assertEqual(auditorias[0]['usuario_id'], '10')
        self.assertEqual(auditorias[0]['resultado'], 'sucesso')

        # Copiar normal
        resp_cop = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/copiar-senha')
        self.assertEqual(resp_cop.status_code, 200)
        self.assertEqual(resp_cop.get_json()['senha'], 'SegredoTopSecretissimo')

        # Simulação Fail-Closed
        from backups_ftp_admin import _gravar_auditoria_commit
        with patch('backups_ftp_admin._gravar_auditoria_commit', side_effect=sqlite3.OperationalError('disk I/O error')):
            resp_falha = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-senha')
            self.assertEqual(resp_falha.status_code, 500)
            data_falha = resp_falha.get_json()
            self.assertNotIn('SegredoTopSecretissimo', json.dumps(data_falha))

    def test_revelar_e_copiar_extra_auditoria_fail_closed_sem_vazar_chave_no_log(self):
        """Revelar e copiar campos extras grava estritamente 'campo_extra' no log sem vazar o nome da chave."""
        self._login_master(user_id=25, user_nome='operador_vinte_cinco')

        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-EXTRAS-LOG',
            'nome_rotina': 'Rotina Extra Test',
            'dados_extras': {'token_gateway_secreto': 'meu_token_privado_999'}
        })
        rotina_id = resp_post.get_json()['rotina']['id']

        # Revela campo extra
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/revelar-extra', json={'chave': 'token_gateway_secreto'})
        self.assertEqual(resp_rev.status_code, 200)
        self.assertEqual(resp_rev.get_json()['valor'], 'meu_token_privado_999')

        # Copia campo extra
        resp_cop = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/copiar-extra', json={'chave': 'token_gateway_secreto'})
        self.assertEqual(resp_cop.status_code, 200)
        self.assertEqual(resp_cop.get_json()['valor'], 'meu_token_privado_999')

        # Verifica que o log de auditoria registrou 'campo_extra', e NUNCA 'token_gateway_secreto'
        cursor = self.conn.cursor()
        logs_audit = cursor.execute(
            "SELECT campo_ou_segredo, acao FROM backups_ftp_auditoria WHERE rotina_id=? AND acao IN ('revelar_extra', 'copiar_extra')",
            (rotina_id,)
        ).fetchall()
        self.assertEqual(len(logs_audit), 2)
        for log in logs_audit:
            self.assertEqual(log['campo_ou_segredo'], 'campo_extra')

    # -------------------------------------------------------------
    # 6. Upload e Versionamento de Listas Cobian (.list / .lst)
    # -------------------------------------------------------------
    def test_upload_list_extensoes_sinonimas_e_bom_obrigatorio(self):
        """Aceita .list e .lst se contiverem BOM (UTF-16LE ou UTF-8); rejeita sem BOM ou com extensão errada."""
        self._login_master()
        resp_rot = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-LISTAS-BOM',
            'nome_rotina': 'Rotina Cobian BOM'
        })
        rotina_id = resp_rot.get_json()['rotina']['id']

        # Sintético com UTF-16LE BOM (\xff\xfe) e estrutura de tarefas Cobian
        conteudo_valido_utf16le = b'\xff\xfe' + "{\n  // Cobian Task\n  TaskName=DiarioMatriz\n  Source=/dados\n}".encode('utf-16le')

        # Envia como .lst
        resp_lst = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                    data={'arquivo': (BytesIO(conteudo_valido_utf16le), 'tarefas.lst')},
                                    content_type='multipart/form-data')
        self.assertEqual(resp_lst.status_code, 201)
        self.assertEqual(resp_lst.get_json()['versao']['encoding_detectado'], 'UTF-16LE')

        # Envia como .list
        resp_list = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                     data={'arquivo': (BytesIO(conteudo_valido_utf16le), 'tarefas.list')},
                                     content_type='multipart/form-data')
        self.assertEqual(resp_list.status_code, 201)

        # Arquivo sem BOM deve ser rejeitado
        conteudo_sem_bom = "{\n  TaskName=SemBOM\n}".encode('utf-8')
        resp_sem_bom = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                        data={'arquivo': (BytesIO(conteudo_sem_bom), 'tarefas.list')},
                                        content_type='multipart/form-data')
        self.assertEqual(resp_sem_bom.status_code, 400)
        self.assertIn('formato exige codificação com BOM', resp_sem_bom.get_json().get('erro', ''))

    def test_upload_list_limite_2mib_file_bytes_retorna_413(self):
        """Arquivo com tamanho de payload superior a 2 MiB deve retornar HTTP 413 via streaming."""
        self._login_master()
        resp_rot = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-2MIB',
            'nome_rotina': 'Rotina 2MiB'
        })
        rotina_id = resp_rot.get_json()['rotina']['id']

        tam_excessivo = (2 * 1024 * 1024) + 100
        payload_grande = b'\xff\xfe' + ('{\n  TaskName=' + ('X' * (tam_excessivo // 2)) + '\n}').encode('utf-16le')

        resp = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                data={'arquivo': (BytesIO(payload_grande), 'tarefas_gigante.list')},
                                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 413)

    def test_upload_list_validacao_gramatica_cobian(self):
        """Rejeita arquivos que possuem apenas chaves isoladas sem gramática de tarefas Cobian."""
        self._login_master()
        resp_rot = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-GRAMATICA',
            'nome_rotina': 'Rotina Gramatica'
        })
        rotina_id = resp_rot.get_json()['rotina']['id']

        # Possui apenas { e }, mas nenhuma linha de propriedade ou comentário de tarefa
        conteudo_vazio = b'\xff\xfe' + "{\n}".encode('utf-16le')
        resp = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                data={'arquivo': (BytesIO(conteudo_vazio), 'tarefas.list')},
                                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('estrutura sintática válida', resp.get_json().get('erro', ''))

    def test_upload_list_retencao_10_versoes_e_unique_constraint(self):
        """Retém no máximo 10 versões por rotina e respeita constraint UNIQUE(rotina_id, versao_numero)."""
        self._login_master()
        resp_rot = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-RETENCAO-10',
            'nome_rotina': 'Rotina 10 Versoes'
        })
        rotina_id = resp_rot.get_json()['rotina']['id']

        conteudo = b'\xff\xfe' + "{\n  // Task Cobian\n  TaskName=VersaoTeste\n}".encode('utf-16le')

        for i in range(1, 13):
            resp = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                    data={'arquivo': (BytesIO(conteudo), f'tarefas_v{i}.list')},
                                    content_type='multipart/form-data')
            self.assertEqual(resp.status_code, 201)

        resp_get = self.client.get(f'/admin/backups-ftp/rotina/{rotina_id}/listas')
        versoes = resp_get.get_json()['versoes']
        self.assertEqual(len(versoes), 10)
        self.assertEqual([v['versao_numero'] for v in versoes], list(range(3, 13)))

    def test_upload_list_rejeita_caracteres_controle_no_nome(self):
        """Nome de arquivo contendo quebras de linha ou caracteres de controle é rejeitado."""
        self._login_master()
        resp_rot = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-NAME-CTRL',
            'nome_rotina': 'Rotina Nome Ctrl'
        })
        rotina_id = resp_rot.get_json()['rotina']['id']

        conteudo = b'\xff\xfe' + "{\n  TaskName=NomeInvalido\n}".encode('utf-16le')
        resp = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                data={'arquivo': (BytesIO(conteudo), 'tarefas\nperigosa.list')},
                                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)

    def test_ver_conteudo_list_auditoria_fail_closed(self):
        """Inspecionar conteúdo de lista é auditado de forma fail-closed e descriptografado."""
        self._login_master()
        resp_rot = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-INSPECAO',
            'nome_rotina': 'Rotina Inspecao'
        })
        rotina_id = resp_rot.get_json()['rotina']['id']

        texto = "{\n  // Conteudo de teste\n  Source=/var/log\n}"
        conteudo_bytes = b'\xff\xfe' + texto.encode('utf-16le')

        resp_up = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/upload-list',
                                   data={'arquivo': (BytesIO(conteudo_bytes), 'tarefas.list')},
                                   content_type='multipart/form-data')
        versao_id = resp_up.get_json()['versao']['id']

        resp_ver = self.client.post(f'/admin/backups-ftp/rotina/{rotina_id}/lista/{versao_id}/ver-conteudo')
        self.assertEqual(resp_ver.status_code, 200)
        self.assertEqual(resp_ver.get_json()['conteudo'], texto)

    # -------------------------------------------------------------
    # 7. Validação de Schemas e Erros Seguros (400 vs 500)
    # -------------------------------------------------------------
    def test_validacao_schema_json_invalido_retorna_400(self):
        """Payloads com formato inválido (não dict, revision ausente, status não int) retornam 400."""
        self._login_master()

        # POST com payload lista em vez de dict
        resp1 = self.client.post('/admin/backups-ftp/rotina', json=["isso_nao_e_dict"])
        self.assertEqual(resp1.status_code, 400)

        # POST com lancamento_em em formato inválido
        resp2 = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-SCHEMA',
            'lancamento_em': '04/11/2026'  # Formato errado, deve ser YYYY-MM-DD
        })
        self.assertEqual(resp2.status_code, 400)

        # PUT sem expected_revision
        resp3 = self.client.put('/admin/backups-ftp/rotina/1', json={'nome_rotina': 'SemRevision'})
        self.assertEqual(resp3.status_code, 400)

    def test_auditoria_identificacao_usuario_user_id_e_ignora_ip_spoofing(self):
        """Auditoria registra user_id e utiliza request.remote_addr ignorando X-Forwarded-For forjado."""
        self._login_master(user_id=88, user_nome='operador_88')
        headers = {'X-Forwarded-For': '198.51.100.25, 10.0.0.1'}  # IP forjado no header

        resp = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-IP-TEST',
            'nome_rotina': 'Rotina IP'
        }, headers=headers)
        self.assertEqual(resp.status_code, 201)
        rotina_id = resp.get_json()['rotina']['id']

        cursor = self.conn.cursor()
        audit = cursor.execute("SELECT usuario_id, ip_origem, acao FROM backups_ftp_auditoria WHERE rotina_id=?",
                               (rotina_id,)).fetchone()
        self.assertEqual(audit['usuario_id'], '88')
        # Deve registrar 127.0.0.1 (remote_addr do test client), e NÃO o IP forjado
        self.assertEqual(audit['ip_origem'], '127.0.0.1')
        self.assertEqual(audit['acao'], 'criar_rotina')

    def test_mutacoes_auditoria_na_mesma_transacao_rollback(self):
        """Se ocorrer falha durante a mutação da rotina, a transação sofre rollback e retorna 500 com headers no-store."""
        self._login_master()

        from backups_ftp_admin import _gravar_auditoria_na_transacao
        with patch('backups_ftp_admin._gravar_auditoria_na_transacao', side_effect=sqlite3.OperationalError('simulated error')):
            resp = self.client.post('/admin/backups-ftp/rotina', json={
                'cliente_alias': 'CLIENTE-ROLLBACK',
                'nome_rotina': 'Rotina Rollback'
            })
            self.assertEqual(resp.status_code, 500)
            self.assertEqual(resp.headers.get('Cache-Control'), 'no-store, private')
            self.assertEqual(resp.headers.get('Vary'), 'Cookie')
            self.assertIn('erro', resp.get_json())

        # Verifica que nada foi gravado no banco após o rollback
        cursor = self.conn.cursor()
        rotinas = cursor.execute("SELECT COUNT(*) FROM backups_ftp_rotinas WHERE cliente_alias='CLIENTE-ROLLBACK'").fetchone()[0]
        self.assertEqual(rotinas, 0)
        auditorias = cursor.execute("SELECT COUNT(*) FROM backups_ftp_auditoria WHERE cliente_alias='CLIENTE-ROLLBACK'").fetchone()[0]
        self.assertEqual(auditorias, 0)

    def test_crypto_envelope_v_e_kid_incompativeis_rejeitados(self):
        """Envelope com versão v diferente de v1 ou kid incompatível é rejeitado na decifragem."""
        from backups_ftp_admin import cifrar_dado, decifrar_dado

        cifrado = cifrar_dado("SenhaTeste", "contexto")
        env = json.loads(cifrado)

        # Altera kid
        env_bad_kid = dict(env)
        env_bad_kid['kid'] = 'v2_invalido'
        with self.assertRaises(ValueError):
            decifrar_dado(json.dumps(env_bad_kid), "contexto")

        # Altera versão v
        env_bad_v = dict(env)
        env_bad_v['v'] = 'v9'
        with self.assertRaises(ValueError):
            decifrar_dado(json.dumps(env_bad_v), "contexto")

    def test_listar_todos_clientes_uniao_feed_e_banco_com_resiliencia(self):
        """GET /admin/backups-ftp/clientes retorna união ordenada dos aliases do feed e do banco, com resiliência a falhas no feed."""
        self._login_master()

        # Cadastra cliente que está fora do feed
        self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-OFFLINE-ADM',
            'nome_rotina': 'Rotina Offline'
        })

        # Consulta normal: deve conter CLIENTE-TESTE (do feed) e CLIENTE-OFFLINE-ADM (do banco)
        resp = self.client.get('/admin/backups-ftp/clientes')
        self.assertEqual(resp.status_code, 200)
        clientes = resp.get_json()['clientes']
        self.assertIn('CLIENTE-TESTE', clientes)
        self.assertIn('CLIENTE-OFFLINE-ADM', clientes)
        self.assertEqual(clientes, sorted(clientes))

        # Simulação de erro no feed de status: não deve quebrar os clientes cadastrados no banco
        with patch.object(self, 'obter_status', side_effect=Exception('falha feed')):
            resp_resiliente = self.client.get('/admin/backups-ftp/clientes')
            self.assertEqual(resp_resiliente.status_code, 200)
            clientes_res = resp_resiliente.get_json()['clientes']
            self.assertIn('CLIENTE-OFFLINE-ADM', clientes_res)

    def test_validacao_lancamento_em_data_real_e_tipo_alias(self):
        """Valida que lancamento_em rejeita datas de calendário inexistentes e alias exige string não-vazia."""
        self._login_master()

        # Data inexistente 30 de fevereiro
        resp_feb30 = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-DATA',
            'lancamento_em': '2026-02-30'
        })
        self.assertEqual(resp_feb30.status_code, 400)
        self.assertIn('Data de lançamento inválida', resp_feb30.get_json().get('erro', ''))

        # Mês 13
        resp_mes13 = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-DATA',
            'lancamento_em': '2026-13-01'
        })
        self.assertEqual(resp_mes13.status_code, 400)

        # Alias inválido (inteiro ou vazio)
        resp_bad_alias = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 12345
        })
        self.assertEqual(resp_bad_alias.status_code, 400)


if __name__ == '__main__':
    unittest.main()
