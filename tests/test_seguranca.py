import io
import gc
import shlex
from contextlib import closing, ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, render_template_string
from werkzeug.security import generate_password_hash


class SegurancaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_dir = Path.cwd()
        cls.temp = tempfile.TemporaryDirectory(dir=cls.original_dir, prefix='.test-security-')
        cls.env = patch.dict(os.environ, {
            'SECRET_KEY': 'test-only-' + 'a' * 64,
            'SERVIDORES_CONFIG': '{}', 'ADMIN_MASTER_EMAIL': '',
            'ADMIN_MASTER_PASSWORD': '', 'SESSION_COOKIE_SECURE': '0',
            'PUBLIC_HORARIOS': '0',
        })
        cls.env.start()
        os.chdir(cls.temp.name)
        # Import blueprints directly: do not load the production .env or start jobs.
        with patch('apscheduler.schedulers.background.BackgroundScheduler.start'):
            import bancos
            import horarios
        import seguranca
        cls.bancos, cls.horarios, cls.security = bancos, horarios, seguranca
        cls.app = Flask(__name__)
        seguranca.aplicar_config_flask(cls.app)
        cls.app.config['TESTING'] = True
        cls.app.register_blueprint(bancos.bancos_bp)
        cls.app.register_blueprint(horarios.horarios_bp)
        cls.password = 'test-only-password'
        with closing(bancos.get_db_connection()) as conn, conn:
            cls.user_id = conn.execute(
                'INSERT INTO usuarios (nome, email, senha_hash, eh_master, ativo) VALUES (?, ?, ?, 1, 1)',
                ('Teste', 'test@example.invalid', generate_password_hash(cls.password)),
            ).lastrowid

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.original_dir)
        cls.env.stop()
        gc.collect()
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        response = self.client.get('/admin/login')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="csrf_token"', response.data)
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
            session.update(logged_in=True, user_id=self.user_id, eh_master=True)

    def test_csrf_missing_invalid_and_foreign_session(self):
        for token in (None, 'invalid', 'á', 'b' * 64):
            with self.subTest(token=token):
                data = {} if token is None else {'csrf_token': token}
                self.assertEqual(self.client.post('/admin/logout', data=data).status_code, 400)
        other = self.app.test_client()
        self.assertEqual(other.post('/admin/logout', data={'csrf_token': self.token}).status_code, 400)

    def test_csrf_form_and_json(self):
        response = self.client.post('/horarios/novo_cargo', data={
            'csrf_token': self.token, 'nome_cargo': 'Teste CSRF',
        })
        self.assertEqual(response.status_code, 302)
        with closing(self.bancos.get_db_connection()) as conn, conn:
            pessoa = conn.execute("INSERT INTO funcionarios (nome, ativo) VALUES ('Teste CSRF', 1)").lastrowid
            escala = conn.execute("INSERT INTO escala_sabado (data_sabado, cor_equipe, funcionario_id, horario) VALUES ('2026-09-19', 'Verde', ?, '09:00 - 13:00')", (pessoa,)).lastrowid
        response = self.client.post('/horarios/atualizar_horario_escala',
                                    json={'id': escala, 'horario': '08:00 - 12:00'},
                                    headers={'X-CSRF-Token': self.token})
        self.assertEqual(response.status_code, 200)

    def test_mutations_reject_get(self):
        paths = ['/admin/logout', '/admin/usuarios/toggle/1', '/admin/usuarios/reset-senha/1',
                 '/horarios/logout', '/horarios/excluir_dia_inteiro/2030-01-01']
        paths += ['/horarios/excluir_' + name + '/1' for name in
                  ('ausencia', 'cargo', 'jornada', 'funcionario', 'escala_sabado')]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 405)

    def test_authorization_still_required_with_csrf(self):
        with self.client.session_transaction() as session:
            session.pop('logged_in')
        response = self.client.post('/horarios/excluir_cargo/1', data={'csrf_token': self.token})
        self.assertIn(response.status_code, (302, 403))

    def test_templates_compile_and_include_csrf(self):
        with self.app.test_request_context():
            for module in (self.bancos, self.horarios):
                for name, value in vars(module).items():
                    if name.startswith('HTML_') and isinstance(value, str):
                        with self.subTest(template=name):
                            self.app.jinja_env.from_string(value)
                            if '</head>' in value:
                                self.assertIn('name="csrf-token"', value)
            html = render_template_string(self.bancos.HTML_LOGIN)
            self.assertNotIn('{{ csrf_token()', html)

    def test_backup_atrasado_aparece_no_filtro_e_totalizador(self):
        from backups import interpretar_status_backups
        status = interpretar_status_backups(
            '<font color="red">O cliente loja-teste possui o arquivo criado a 2 dias '
            '- status [ERRO] 2026-09-08 22:00:00.</font>')
        with patch.object(self.bancos, 'obter_status_backups_ftp', return_value=status):
            response = self.client.get('/backups-ftp?filtro=atrasado')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'card-cliente status-ATRASADO', response.data)
            self.assertIn(b'loja-teste', response.data)
            self.assertIn(b'<div class="valor">1</div>', response.data)
            response = self.client.get('/backups-ftp?filtro=sem_arquivo')
            self.assertNotIn(b'loja-teste', response.data)

    def test_paths(self):
        base = '/opt/infobrasil'
        self.assertEqual(self.security.validar_caminho_banco(base + '/cliente/dados.fdb', base),
                         base + '/cliente/dados.fdb')
        for path in (None, '/tmp/dados.fdb', base + '2/dados.fdb', base + '/../dados.fdb',
                     base + '/x/../../dados.fdb', base + '/dados.txt', base + '/x\ndados.fdb'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.security.validar_caminho_banco(path, base)

    def test_bootstrap_does_not_promote_or_reactivate_existing_account(self):
        with closing(self.bancos.get_db_connection()) as conn, conn:
            conn.execute('INSERT INTO usuarios (nome, email, senha_hash, eh_master, ativo) VALUES (?, ?, ?, 0, 0)',
                         ('Existente', 'existing@example.invalid', 'unused'))
        with patch.dict(os.environ, {'ADMIN_MASTER_EMAIL': 'existing@example.invalid',
                                     'ADMIN_MASTER_PASSWORD': self.password}):
            self.bancos.init_db_sistema()
        with closing(self.bancos.get_db_connection()) as conn:
            user = conn.execute('SELECT eh_master, ativo FROM usuarios WHERE email = ?',
                                ('existing@example.invalid',)).fetchone()
        self.assertEqual(tuple(user), (0, 0))

    def test_upload_quotes_registered_path(self):
        destination = '/opt/infobrasil/$(touch unwanted)/dados.fdb'
        ssh = MagicMock()
        ssh.open_sftp.return_value.stat.side_effect = FileNotFoundError
        with patch.dict(self.bancos.SERVIDORES, {'test': {'ip': 'localhost'}}), \
                patch.object(self.bancos, 'caminho_do_alias_no_conf', return_value=destination), \
                patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            response = self.client.post('/admin/upload_banco', data={
                'csrf_token': self.token, 'servidor': 'test', 'alias': 'x',
                'senha_confirmacao': self.password,
                'arquivo_fdb': (io.BytesIO(b'test'), 'dados.fdb'),
            })
        self.assertEqual(response.status_code, 302)
        command = ssh.exec_command.call_args.args[0]
        self.assertEqual(shlex.split(command), [
            'mkdir', '-p', '--', '/opt/infobrasil/$(touch unwanted)',
            '&&', 'chmod', '775', '--', '/opt/infobrasil/$(touch unwanted)',
        ])

    def test_upload_rejects_changed_destination_before_ssh(self):
        with patch.dict(self.bancos.SERVIDORES, {'test': {}}), \
                patch.object(self.bancos, 'caminho_do_alias_no_conf', return_value='/opt/infobrasil/x/dados.fdb'), \
                patch.object(self.bancos, 'conectar_ssh') as ssh:
            # A configured server must be nonempty to pass the route's lookup.
            self.bancos.SERVIDORES['test'] = {'ip': 'localhost'}
            response = self.client.post('/admin/upload_banco', data={
                'csrf_token': self.token, 'servidor': 'test', 'alias': 'x',
                'senha_confirmacao': self.password,
                'caminho': '/opt/infobrasil/$(touch unwanted)/dados.fdb',
                'arquivo_fdb': (io.BytesIO(b'test'), 'dados.fdb'),
            })
            self.assertEqual(response.status_code, 302)
            self.assertIn('inv', response.location)
            ssh.assert_not_called()

    def test_ssh_rejects_unknown_hosts_and_invalid_file(self):
        import paramiko
        with patch.object(self.security, 'KNOWN_HOSTS_PATH', str(Path(self.temp.name) / 'missing')):
            client = self.security.criar_cliente_ssh()
            self.assertIsInstance(client._policy, paramiko.RejectPolicy)
            key = MagicMock()
            key.get_fingerprint.return_value = b'test fingerprint'
            with self.assertRaises(paramiko.SSHException):
                client._policy.missing_host_key(MagicMock(), 'untrusted.invalid', key)
            client.close()
        with patch('paramiko.SSHClient') as factory, patch('os.path.isfile', return_value=True):
            factory.return_value.load_host_keys.side_effect = ValueError('invalid key file')
            with self.assertRaises(ValueError):
                self.security.criar_cliente_ssh()

    def test_forwarded_header_does_not_bypass_rate_limit_identity(self):
        with self.app.test_request_context(headers={'X-Forwarded-For': '203.0.113.9'},
                                           environ_base={'REMOTE_ADDR': '127.0.0.1'}):
            self.assertEqual(self.security.ip_cliente(), '127.0.0.1')

    def test_listagem_de_pastas_exige_permissao(self):
        with self.client.session_transaction() as session:
            session.pop('logged_in')
        with patch.object(self.bancos, 'conectar_ssh') as ssh:
            response = self.client.get('/admin/pastas-destino?servidor=DB01')
            self.assertEqual(response.status_code, 302)
            ssh.assert_not_called()

    def test_alias_com_pasta_selecionada_e_falha_de_configuracao(self):
        for salvo in (True, False):
            with self.subTest(salvo=salvo), \
                    patch.dict(self.bancos.SERVIDORES, {'test': {'ip': 'localhost'}}), \
                    patch.object(self.bancos, 'verificar_alias_existente_global', return_value=None), \
                    patch.object(self.bancos, 'ler_databases_conf_remoto', return_value='# configuracao'), \
                    patch.object(self.bancos, 'conectar_ssh'), \
                    patch.object(self.bancos, 'adicionar_banco', return_value='/opt/infobrasil/cliente/filial.fdb') as adicionar, \
                    patch.object(self.bancos, 'salvar_databases_conf_remoto', return_value=salvo) as salvar:
                response = self.client.post('/admin/adicionar', data={
                    'csrf_token': self.token, 'servidor': 'test', 'alias': 'filial',
                    'senha_confirmacao': self.password, 'modo_destino': 'existente',
                    'pasta_destino': '/opt/infobrasil/cliente', 'nome_arquivo': 'filial.fdb',
                })
                self.assertEqual(response.status_code, 302)
                self.assertIn('sucesso=' if salvo else 'erro=', response.location)
                self.assertEqual(adicionar.call_args.args[3:6], ('existente', '/opt/infobrasil/cliente', 'filial.fdb'))
                self.assertIn('filial = /opt/infobrasil/cliente/filial.fdb', salvar.call_args.args[1])

    def test_email_cname_destinatario_e_nome_sem_master(self):
        from email import message_from_string
        with patch.object(self.bancos.smtplib, 'SMTP') as smtp, \
                patch.object(self.bancos, 'SMTP_USER', 'test@example.invalid'):
            ok, _ = self.bancos.enviar_solicitacao_cname('Loja teste', 'loja', 'Nome Sobrenome (Master)')
        self.assertTrue(ok)
        argumentos = smtp.return_value.sendmail.call_args.args
        self.assertIn('atendimento@nossatelecom.com.br', argumentos[1])
        mensagem = message_from_string(argumentos[2])
        corpo = mensagem.get_payload(0).get_payload(decode=True).decode('utf-8')
        self.assertIn('Nome Sobrenome', corpo)
        self.assertNotIn('Master', corpo)

    def test_horarios_publicos_somente_leitura(self):
        client = self.app.test_client()
        response = client.get('/horarios')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'action="/horarios/salvar_funcionario"', response.data)
        self.assertNotIn(b'action="/horarios/registrar_troca"', response.data)
        self.assertNotIn(b'action="/horarios/gerar_sugestao_sabado"', response.data)
        self.assertEqual(client.get('/horarios/editar_funcionario/1').status_code, 302)
        with client.session_transaction() as session:
            token = session['_csrf_token']
        for path in ('salvar_funcionario', 'registrar_troca', 'gerar_sugestao_sabado'):
            self.assertEqual(client.post('/horarios/' + path, data={'csrf_token': token}).status_code, 302)

    def test_cadastro_e_edicao_de_estagio_e_sabado_nenhum(self):
        with closing(self.bancos.get_db_connection()) as conn:
            cargo = conn.execute("SELECT id FROM cargos WHERE nome = 'Suporte Técnico'").fetchone()[0]
            jornada = conn.execute("SELECT id FROM jornadas WHERE tipo = 'Semana' AND manha_inicio = '08:00' AND manha_fim = '12:00' AND tarde_inicio = ''").fetchone()[0]
        dados = dict(csrf_token=self.token, nome='Estagiário Teste', prioridade='P1', cargo_id=cargo,
                     jornada_id=jornada, equipe_sabado='Estágio', tipo_sabado='nenhum', ativo='1')
        self.assertEqual(self.client.post('/horarios/salvar_funcionario', data=dados).status_code, 302)
        with closing(self.bancos.get_db_connection()) as conn:
            pessoa = conn.execute('SELECT * FROM funcionarios WHERE nome = ?', (dados['nome'],)).fetchone()
        self.assertEqual(pessoa['nao_trabalha_sabado'], 1)
        self.assertEqual(pessoa['equipe_sabado'], 'Estágio')
        pagina = self.client.get(f"/horarios/editar_funcionario/{pessoa['id']}")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn('value="Estágio" selected'.encode(), pagina.data)
        self.assertIn(b'value="nenhum" selected', pagina.data)
        dados.update(id=pessoa['id'], equipe_sabado='Sem Equipe')
        self.assertEqual(self.client.post('/horarios/salvar_funcionario', data=dados).status_code, 302)
        with closing(self.bancos.get_db_connection()) as conn:
            self.assertEqual(conn.execute('SELECT equipe_sabado FROM funcionarios WHERE id = ?', (pessoa['id'],)).fetchone()[0], 'Sem Equipe')
        resposta = self.client.post('/horarios/adicionar_item_escala', data=dict(
            csrf_token=self.token, data_sabado='2026-09-19', funcionario_id=pessoa['id'], horario='08:00 - 12:00'))
        self.assertEqual(resposta.status_code, 400)
        cobertura = self.horarios.calcular_cobertura_diaria('2026-09-14')
        self.assertIn(dados['nome'], [a['nome'] for a in next(c for c in cobertura if c['faixa'].startswith('08:00'))['atendentes']])
        self.assertNotIn(dados['nome'], [a['nome'] for a in next(c for c in cobertura if c['faixa'].startswith('12:00'))['atendentes']])

    def test_troca_atualiza_cobertura_e_historico_da_data(self):
        with closing(self.bancos.get_db_connection()) as conn, conn:
            jornada_a = conn.execute("INSERT INTO jornadas (descricao, tipo, manha_inicio, manha_fim) VALUES ('Manhã teste', 'Semana', '08:00', '12:00')").lastrowid
            jornada_b = conn.execute("INSERT INTO jornadas (descricao, tipo, manha_inicio, manha_fim) VALUES ('Tarde teste', 'Semana', '13:00', '17:00')").lastrowid
            a = conn.execute("INSERT INTO funcionarios (nome, jornada_id, ativo) VALUES ('Pessoa Troca A', ?, 1)", (jornada_a,)).lastrowid
            b = conn.execute("INSERT INTO funcionarios (nome, jornada_id, ativo) VALUES ('Pessoa Troca B', ?, 1)", (jornada_b,)).lastrowid
        response = self.client.post('/horarios/registrar_troca', data=dict(
            csrf_token=self.token, data_sabado='2026-09-14', substituido_id=a, substituto_id=b, motivo='Troca de horário teste'))
        self.assertEqual(response.status_code, 302)
        cobertura = self.horarios.calcular_cobertura_diaria('2026-09-14')
        nomes = [a['nome'] for a in next(c for c in cobertura if c['faixa'].startswith('08:00'))['atendentes']]
        self.assertIn('Pessoa Troca B', nomes)
        self.assertNotIn('Pessoa Troca A', nomes)
        pagina = self.client.get('/horarios?data_trocas=2026-09-14&data_cobertura=2026-09-14')
        self.assertEqual(pagina.status_code, 200)
        self.assertIn('Troca de horário teste'.encode(), pagina.data)
        with closing(self.bancos.get_db_connection()) as conn:
            self.assertEqual(conn.execute('SELECT jornada_id FROM funcionarios WHERE id = ?', (a,)).fetchone()[0], jornada_a)

    def test_nenhum_nao_entra_na_geracao_de_sabado(self):
        with closing(self.bancos.get_db_connection()) as conn, conn:
            pessoa = conn.execute("INSERT INTO funcionarios (nome, equipe_sabado, ativo, nao_trabalha_sabado) VALUES ('Não escalar teste', 'Amarela', 1, 1)").lastrowid
        response = self.client.post('/horarios/gerar_sugestao_sabado', data=dict(csrf_token=self.token, data_sabado='2026-10-03'))
        self.assertEqual(response.status_code, 302)
        with closing(self.bancos.get_db_connection()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM escala_sabado WHERE funcionario_id = ?', (pessoa,)).fetchone()[0], 0)

    def test_geracao_preserva_escala_com_troca_aplicada(self):
        with closing(self.bancos.get_db_connection()) as conn, conn:
            pessoa = conn.execute("INSERT INTO funcionarios (nome, ativo) VALUES ('Preservar escala', 1)").lastrowid
            escala = conn.execute("INSERT INTO escala_sabado (data_sabado, cor_equipe, funcionario_id, horario) VALUES ('2026-10-10', 'Verde', ?, '14:00 - 18:00')", (pessoa,)).lastrowid
            conn.execute("INSERT INTO trocas_sabado (data_sabado, aplicada, detalhes) VALUES ('2026-10-10', 1, 'Ajuste anterior')")
        response = self.client.post('/horarios/gerar_sugestao_sabado', data=dict(csrf_token=self.token, data_sabado='2026-10-10'))
        self.assertEqual(response.status_code, 302)
        with closing(self.bancos.get_db_connection()) as conn:
            self.assertEqual(conn.execute('SELECT horario FROM escala_sabado WHERE id = ?', (escala,)).fetchone()[0], '14:00 - 18:00')

    def test_paginas_de_consulta_publicas_sem_edicao(self):
        from html.parser import HTMLParser
        class Controles(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))
        banco = dict(alias='cliente-teste', servidor='DB01', porta='3050', arquivo_existe=True,
                     tamanho_bytes=1024, tamanho_str='1 KB', cname_string='dbteste.example.invalid:3050/cliente',
                     status_inatividade={'status': 'normal'}, timestamp=1, data_criacao='01/09/2026', lojas=[])
        status = dict(erro=None, clientes=[], total_ok=0, total_erro=0, total_atrasado=0, ultima_atualizacao=None)
        client = self.app.test_client()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(self.bancos.SERVIDORES, {'DB01': dict(rotulo='DB01', senha='segredo-nao-publicar')}))
            stack.enter_context(patch.object(self.bancos, 'obter_metricas_servidores', return_value=[]))
            stack.enter_context(patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=([banco], 1024, [])))
            stack.enter_context(patch.object(self.bancos, 'buscar_em_todos_servidores', return_value=([banco], 1024, [])))
            stack.enter_context(patch.object(self.bancos, 'obter_status_backups_ftp', return_value=status))
            for path in ('/', '/servidor/DB01', '/todos', '/todos?filtro=sem_lojas', '/inativos',
                         '/orfaos', '/historico', '/backups-ftp', '/api/historico'):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(b'segredo-nao-publicar', response.data)
                    self.assertNotIn(b'<summary class="btn">Gerenciar</summary>', response.data)
                    controles = Controles()
                    controles.feed(response.get_data(as_text=True))
                    self.assertFalse(any(tag == 'form' and attrs.get('method', '').upper() == 'POST'
                                         for tag, attrs in controles.tags))
                    self.assertFalse(any('editarCname' in attrs.get('onclick', '')
                                         for _, attrs in controles.tags))
            response = client.get('/')
            self.assertIn(b'data-assinatura=', response.data)
            self.assertIn(b'Clientes sem lojas cadastradas', response.data)

    def test_administracao_protegida_mesmo_com_token_csrf_publico(self):
        client = self.app.test_client()
        client.get('/horarios')
        with client.session_transaction() as session:
            token = session['_csrf_token']
        for path in ('/admin', '/admin/importar-lojas', '/admin/importar-lojas/modelo', '/admin/pastas-destino'):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/admin/login', response.location)
        for path in ('/admin/cliente-loja/salvar', '/admin/cliente-loja/excluir', '/admin/cname/salvar',
                     '/admin/usuarios/permissoes', '/admin/solicitar-cname'):
            with self.subTest(path=path):
                self.assertEqual(client.post(path, json={}, headers={'X-CSRF-Token': token}).status_code, 401)
        self.assertEqual(client.get('/api/lojas/buscar-alias?fantasia=teste').status_code, 401)

    def test_cname_publico_exige_assinatura_e_nao_abre_conexao_tcp(self):
        client = self.app.test_client()
        host = 'dbteste.example.invalid:3050/cliente'
        with self.app.app_context():
            assinatura = self.bancos.assinatura_cname(host)
        with patch.object(self.bancos.socket, 'gethostbyname', return_value='192.0.2.1') as dns, \
                patch.object(self.bancos.socket, 'create_connection') as tcp:
            for invalida in ('', 'inválida', assinatura + 'x'):
                self.assertEqual(client.get('/api/cname/testar', query_string=dict(host=host, assinatura=invalida)).status_code, 400)
            dns.assert_not_called()
            response = client.get('/api/cname/testar', query_string=dict(host=host, assinatura=assinatura))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {'ativo': True})
            tcp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
