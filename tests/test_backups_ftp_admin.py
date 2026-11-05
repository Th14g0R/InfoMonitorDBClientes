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
            'extras_sensiveis': {'tipo_conexao': False, 'token_api': True},
            'status': 1
        }

        resp_post = self.client.post('/admin/backups-ftp/rotina', json=payload)
        self.assertEqual(resp_post.status_code, 201)
        data_post = resp_post.get_json()
        self.assertTrue(data_post['sucesso'])
        rotina_id = data_post['rotina']['id']
        self.assertEqual(data_post['rotina']['lancamento_em'], '2026-11-04')

        # Máscara de extras: valor confidencial é mascarado como None, não-confidencial retorna valor real
        self.assertEqual(data_post['rotina']['dados_extras'], {'tipo_conexao': 'Passivo', 'token_api': None})
        self.assertEqual(data_post['rotina']['extras_sensiveis'], {'tipo_conexao': False, 'token_api': True})
        self.assertEqual(sorted(data_post['rotina']['extras_configurados']), ['tipo_conexao', 'token_api'])
        self.assertNotIn('tok_segredo_xyz', json.dumps(data_post))

        # Consulta listagem: valor confidencial permanece protegido como None
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-ALFA/rotinas')
        self.assertEqual(resp_get.status_code, 200)
        data_get = resp_get.get_json()
        r = data_get['rotinas'][0]
        self.assertEqual(r['lancamento_em'], '2026-11-04')
        self.assertEqual(r['dados_extras'], {'tipo_conexao': 'Passivo', 'token_api': None})
        self.assertEqual(r['extras_sensiveis'], {'tipo_conexao': False, 'token_api': True})
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

    # -------------------------------------------------------------
    # 7. Checkbox Confidencial, Legado e Toggle em Ambos Sentidos
    # -------------------------------------------------------------
    def test_extras_confidencial_novo_campo_default_nao_sensivel(self):
        """Novo campo de dados_extras tem default sensivel=False caso extras_sensiveis não seja enviado."""
        self._login_master()

        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-NOVOS-EXTRAS',
            'nome_rotina': 'FTP Teste Novos Campos',
            'dados_extras': {'porta_ssh': '2222', 'param_aberto': 'valor_visivel'}
        })
        self.assertEqual(resp_post.status_code, 201)
        r = resp_post.get_json()['rotina']

        # Default é sensivel=False para ambos os campos
        self.assertEqual(r['extras_sensiveis'], {'porta_ssh': False, 'param_aberto': False})
        self.assertEqual(r['dados_extras'], {'porta_ssh': '2222', 'param_aberto': 'valor_visivel'})
        self.assertEqual(sorted(r['extras_configurados']), ['param_aberto', 'porta_ssh'])

        # Na consulta GET, os valores não-sensíveis permanecem disponíveis em texto claro
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-NOVOS-EXTRAS/rotinas')
        self.assertEqual(resp_get.status_code, 200)
        r_get = resp_get.get_json()['rotinas'][0]
        self.assertEqual(r_get['dados_extras'], {'porta_ssh': '2222', 'param_aberto': 'valor_visivel'})
        self.assertEqual(r_get['extras_sensiveis'], {'porta_ssh': False, 'param_aberto': False})

    def test_extras_retrocompatibilidade_legado_sem_atributo_sempre_sensivel(self):
        """Extras em formato legado ({chave: valor}) no banco são interpretados como sensivel=True."""
        from backups_ftp_admin import cifrar_dado

        self._login_master()

        # Inserção direta simulando registro legado gravado no banco antes da feature de confidencialidade
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO backups_ftp_rotinas (
                cliente_alias, status, revision, criado_em, atualizado_em, atualizado_por
            ) VALUES ('CLIENTE-LEGADO', 1, 1, '2026-11-04T10:00:00Z', '2026-11-04T10:00:00Z', 'master')
        """)
        rid = cursor.lastrowid

        payload_legado = {'token_legado': 'segredo_super_antigo', 'proxy_auth': 'usr:pwd'}
        cifrado_legado = cifrar_dado(json.dumps(payload_legado), f"rotina:{rid}:extras")
        cursor.execute("UPDATE backups_ftp_rotinas SET dados_extras_json_cifrado = ? WHERE id = ?", (cifrado_legado, rid))
        self.conn.commit()

        # Consulta listagem: chaves legadas devem ser interpretadas como sensivel=True e mascaradas com None
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-LEGADO/rotinas')
        self.assertEqual(resp_get.status_code, 200)
        r = resp_get.get_json()['rotinas'][0]
        self.assertEqual(r['extras_sensiveis'], {'token_legado': True, 'proxy_auth': True})
        self.assertEqual(r['dados_extras'], {'token_legado': None, 'proxy_auth': None})
        self.assertEqual(sorted(r['extras_configurados']), ['proxy_auth', 'token_legado'])

        # Revelação do campo legado funciona perfeitamente
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rid}/revelar-extra', json={'chave': 'token_legado'})
        self.assertEqual(resp_rev.status_code, 200)
        self.assertEqual(resp_rev.get_json()['valor'], 'segredo_super_antigo')

    def test_extras_toggle_sensibilidade_ambos_sentidos_preserva_valor_e_audita(self):
        """
        Alternar sensibilidade (True <-> False) preserva o valor atual mesmo com input None/mascarado
        e gera registro na auditoria (alterar_sensibilidade_extra) sem vazar a chave ou valor.
        """
        self._login_master()

        # 1. Cria rotina com campo confidencial (sensivel=True) e campo não-confidencial (sensivel=False)
        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-TOGGLE',
            'nome_rotina': 'Rotina Toggle',
            'dados_extras': {'token_api': 'meu_token_secreto', 'host_destino': 'backup.lan'},
            'extras_sensiveis': {'token_api': True, 'host_destino': False}
        })
        self.assertEqual(resp_post.status_code, 201)
        rid = resp_post.get_json()['rotina']['id']

        # -------------------------------------------------------------------
        # Sentido A: Confidencial -> Não-Confidencial (True -> False)
        # O operador envia token_api mascarado como None, mas com sensivel=False
        # -------------------------------------------------------------------
        resp_put_a = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 1,
            'dados_extras': {'token_api': None},
            'extras_sensiveis': {'token_api': False}
        })
        self.assertEqual(resp_put_a.status_code, 200)
        r_put_a = resp_put_a.get_json()['rotina']

        # Valor original 'meu_token_secreto' deve ser preservado e agora exibido em texto claro
        self.assertEqual(r_put_a['extras_sensiveis']['token_api'], False)
        self.assertEqual(r_put_a['dados_extras']['token_api'], 'meu_token_secreto')

        # Auditoria deve conter evento 'alterar_sensibilidade_extra'
        cursor = self.conn.cursor()
        auditorias_a = cursor.execute("""
            SELECT campo_ou_segredo, acao, resultado FROM backups_ftp_auditoria
            WHERE rotina_id = ? AND acao = 'alterar_sensibilidade_extra'
        """, (rid,)).fetchall()
        self.assertEqual(len(auditorias_a), 1)
        self.assertEqual(auditorias_a[0]['campo_ou_segredo'], 'campo_extra')
        self.assertEqual(auditorias_a[0]['resultado'], 'sucesso')

        # Garante que nenhum segredo ou nome de chave foi gravado na tabela de auditoria
        todas_audits = cursor.execute("SELECT * FROM backups_ftp_auditoria WHERE rotina_id = ?", (rid,)).fetchall()
        for audit in todas_audits:
            for val in audit:
                self.assertNotIn('token_api', str(val))
                self.assertNotIn('meu_token_secreto', str(val))

        # -------------------------------------------------------------------
        # Sentido B: Não-Confidencial -> Confidencial (False -> True)
        # O operador altera host_destino de False para True com input None
        # -------------------------------------------------------------------
        resp_put_b = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 2,
            'dados_extras': {'host_destino': None},
            'extras_sensiveis': {'host_destino': True}
        })
        self.assertEqual(resp_put_b.status_code, 200)
        r_put_b = resp_put_b.get_json()['rotina']

        # Agora host_destino é confidencial: mascarado com None
        self.assertEqual(r_put_b['extras_sensiveis']['host_destino'], True)
        self.assertEqual(r_put_b['dados_extras']['host_destino'], None)

        # Revelar extra comprova que o valor 'backup.lan' foi rigorosamente preservado
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rid}/revelar-extra', json={'chave': 'host_destino'})
        self.assertEqual(resp_rev.status_code, 200)
        self.assertEqual(resp_rev.get_json()['valor'], 'backup.lan')

        # Segunda auditoria de alteração de sensibilidade registrada
        auditorias_b = cursor.execute("""
            SELECT COUNT(*) FROM backups_ftp_auditoria
            WHERE rotina_id = ? AND acao = 'alterar_sensibilidade_extra'
        """, (rid,)).fetchone()[0]
        self.assertEqual(auditorias_b, 2)

    def test_extras_armazenamento_cifrado_no_sqlite(self):
        """Garante que a coluna dados_extras_json_cifrado não armazena valores em claro."""
        self._login_master()

        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-CIPHER-CHECK',
            'nome_rotina': 'Rotina Cifra',
            'dados_extras': {'chave_pub': 'valor_pub_123', 'chave_priv': 'super_secreto_456'},
            'extras_sensiveis': {'chave_pub': False, 'chave_priv': True}
        })
        self.assertEqual(resp_post.status_code, 201)
        rid = resp_post.get_json()['rotina']['id']

        cursor = self.conn.cursor()
        raw_db = cursor.execute("SELECT dados_extras_json_cifrado FROM backups_ftp_rotinas WHERE id = ?", (rid,)).fetchone()[0]

        # Verifica envelope AEAD
        env = json.loads(raw_db)
        self.assertEqual(env.get('v'), 'v1')
        self.assertEqual(env.get('kid'), 'v1')
        self.assertIn('n', env)
        self.assertIn('c', env)

        # Não pode conter nem a chave sensível nem o valor sensível em texto claro
        self.assertNotIn('super_secreto_456', raw_db)
        self.assertNotIn('valor_pub_123', raw_db)

    def test_extras_validacao_boolean_estrito_retorna_400(self):
        """extras_sensiveis exige boolean estrito (true/false); tipos como str, int ou lista retornam 400."""
        self._login_master()

        # POST com string "true" em vez de boolean nativo -> 400
        resp_post_str = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-BOOL-CHECK',
            'nome_rotina': 'FTP Bool Test',
            'dados_extras': {'token': '123'},
            'extras_sensiveis': {'token': 'true'}
        })
        self.assertEqual(resp_post_str.status_code, 400)
        self.assertIn('booleano estrito', resp_post_str.get_json().get('erro', ''))

        # POST com int 1 em vez de boolean nativo -> 400
        resp_post_int = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-BOOL-CHECK',
            'nome_rotina': 'FTP Bool Test',
            'dados_extras': {'token': '123'},
            'extras_sensiveis': {'token': 1}
        })
        self.assertEqual(resp_post_int.status_code, 400)

        # POST com boolean válido -> 201
        resp_post_ok = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-BOOL-CHECK',
            'nome_rotina': 'FTP Bool Test',
            'dados_extras': {'token': '123'},
            'extras_sensiveis': {'token': True}
        })
        self.assertEqual(resp_post_ok.status_code, 201)
        rid = resp_post_ok.get_json()['rotina']['id']

        # PUT com string "false" -> 400
        resp_put_str = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 1,
            'extras_sensiveis': {'token': 'false'}
        })
        self.assertEqual(resp_put_str.status_code, 400)

        # PUT com null em extras_sensiveis -> mantém o boolean anterior (200)
        resp_put_null = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 1,
            'extras_sensiveis': {'token': None}
        })
        self.assertEqual(resp_put_null.status_code, 200)
        self.assertEqual(resp_put_null.get_json()['rotina']['extras_sensiveis']['token'], True)

    def test_extras_edicao_para_vazio_em_nao_confidencial_e_preservacao_em_confidencial(self):
        """
        null preserva sempre;
        vazio ("") preserva para confidencial carregado mascarado;
        nao-confidencial aceita string vazia como edicao direta.
        """
        self._login_master()

        # Cria com campo não-confidencial e campo confidencial
        resp_post = self.client.post('/admin/backups-ftp/rotina', json={
            'cliente_alias': 'CLIENTE-VAZIO',
            'nome_rotina': 'FTP Teste Vazio',
            'dados_extras': {'param_pub': 'texto_inicial', 'param_sec': 'segredo_inicial'},
            'extras_sensiveis': {'param_pub': False, 'param_sec': True}
        })
        self.assertEqual(resp_post.status_code, 201)
        rid = resp_post.get_json()['rotina']['id']

        # 1. Campo não-confidencial: enviar "" edita diretamente para vazio
        resp_put_1 = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 1,
            'dados_extras': {'param_pub': ''}
        })
        self.assertEqual(resp_put_1.status_code, 200)
        r1 = resp_put_1.get_json()['rotina']
        self.assertEqual(r1['dados_extras']['param_pub'], '')
        self.assertEqual(r1['extras_sensiveis']['param_pub'], False)

        # 2. Campo confidencial: enviar "" preserva o segredo gravado anteriormente (mascarado)
        resp_put_2 = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 2,
            'dados_extras': {'param_sec': ''}
        })
        self.assertEqual(resp_put_2.status_code, 200)
        # Confirma via revelar-extra que o segredo não foi limpo acidentalmente
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rid}/revelar-extra', json={'chave': 'param_sec'})
        self.assertEqual(resp_rev.status_code, 200)
        self.assertEqual(resp_rev.get_json()['valor'], 'segredo_inicial')

        # 3. Enviar null para campo não-confidencial preserva o valor atual (que agora é "")
        resp_put_3 = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 3,
            'dados_extras': {'param_pub': None}
        })
        self.assertEqual(resp_put_3.status_code, 200)
        self.assertEqual(resp_put_3.get_json()['rotina']['dados_extras']['param_pub'], '')

        # 4. Atualiza param_pub para novo texto, e no PUT seguinte envia null -> preserva novo texto
        resp_put_4 = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 4,
            'dados_extras': {'param_pub': 'novo_valor_pub'}
        })
        self.assertEqual(resp_put_4.status_code, 200)
        self.assertEqual(resp_put_4.get_json()['rotina']['dados_extras']['param_pub'], 'novo_valor_pub')

        resp_put_5 = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 5,
            'dados_extras': {'param_pub': None}
        })
        self.assertEqual(resp_put_5.status_code, 200)
        self.assertEqual(resp_put_5.get_json()['rotina']['dados_extras']['param_pub'], 'novo_valor_pub')

    def test_extras_json_cifrado_corrompido_retorna_503_sem_apagar_dados(self):
        """JSON interno cifrado malformado ou corrompido retorna 503 sem apagar ou mutar os dados."""
        from backups_ftp_admin import cifrar_dado

        self._login_master()

        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO backups_ftp_rotinas (
                cliente_alias, status, revision, criado_em, atualizado_em, atualizado_por
            ) VALUES ('CLIENTE-CORRUPTO', 1, 1, '2026-11-04T10:00:00Z', '2026-11-04T10:00:00Z', 'master')
        """)
        rid = cursor.lastrowid

        # Plaintext com JSON inválido (não parseável)
        cifrado_corrompido = cifrar_dado("{json_invalido_sem_fechar", f"rotina:{rid}:extras")
        cursor.execute("UPDATE backups_ftp_rotinas SET dados_extras_json_cifrado = ? WHERE id = ?", (cifrado_corrompido, rid))
        self.conn.commit()

        # 1. GET rotinas deve retornar 503 com erro seguro
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-CORRUPTO/rotinas')
        self.assertEqual(resp_get.status_code, 503)

        # 2. PUT não pode prosseguir nem mutar/apagar os dados
        resp_put = self.client.put(f'/admin/backups-ftp/rotina/{rid}', json={
            'expected_revision': 1,
            'nome_rotina': 'Tentativa Mutacao'
        })
        self.assertEqual(resp_put.status_code, 503)

        # 3. revelar-extra deve retornar 503
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rid}/revelar-extra', json={'chave': 'qualquer'})
        self.assertEqual(resp_rev.status_code, 503)

    def test_extras_legado_dict_arbitrario_preserva_serializado_e_sensivel_true(self):
        """Dict legado arbitrário é preservado serializado e marcado como sensivel=True para não expor legado."""
        from backups_ftp_admin import cifrar_dado

        self._login_master()

        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO backups_ftp_rotinas (
                cliente_alias, status, revision, criado_em, atualizado_em, atualizado_por
            ) VALUES ('CLIENTE-DICT-LEGADO', 1, 1, '2026-11-04T10:00:00Z', '2026-11-04T10:00:00Z', 'master')
        """)
        rid = cursor.lastrowid

        payload_legado_dict = {'config_complexa': {'host': 'srv01', 'porta': 8080}}
        cifrado_legado = cifrar_dado(json.dumps(payload_legado_dict), f"rotina:{rid}:extras")
        cursor.execute("UPDATE backups_ftp_rotinas SET dados_extras_json_cifrado = ? WHERE id = ?", (cifrado_legado, rid))
        self.conn.commit()

        # Listagem: sensivel=True e mascarado como None
        resp_get = self.client.get('/admin/backups-ftp/cliente/CLIENTE-DICT-LEGADO/rotinas')
        self.assertEqual(resp_get.status_code, 200)
        r = resp_get.get_json()['rotinas'][0]
        self.assertEqual(r['extras_sensiveis']['config_complexa'], True)
        self.assertIsNone(r['dados_extras']['config_complexa'])

        # Revelar: retorna o dict legado serializado como JSON
        resp_rev = self.client.post(f'/admin/backups-ftp/rotina/{rid}/revelar-extra', json={'chave': 'config_complexa'})
        self.assertEqual(resp_rev.status_code, 200)
        valor_decifrado = resp_rev.get_json()['valor']
        self.assertEqual(json.loads(valor_decifrado), {'host': 'srv01', 'porta': 8080})


if __name__ == '__main__':
    unittest.main()
