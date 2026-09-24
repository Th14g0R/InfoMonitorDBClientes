import io
import gc
import json
import re
import shlex
import sqlite3
import threading
from contextlib import closing, ExitStack
from datetime import date, timedelta
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

    def test_filtro_ausencias_e_aniversario(self):
        with closing(self.horarios.get_db()) as conn, conn:
            pessoa = conn.execute("INSERT INTO funcionarios (nome, data_nascimento) VALUES ('Filtro teste', '1987-12-17')").lastrowid
            conn.execute("INSERT INTO ausencias (funcionario_id, motivo, data_inicio, data_fim) VALUES (?, 'Motivo exato teste', '2001-01-01', '2001-01-31')", (pessoa,))
        resposta = self.client.get('/horarios')
        self.assertEqual(resposta.status_code, 200)
        self.assertIn('Aniversário ↕', resposta.text)
        self.assertIn('>17/12</td>', resposta.text)
        self.assertNotIn('>01/01/2001</td>', resposta.text)
        resposta = self.client.get('/horarios', query_string={'ausencia_motivo': 'Motivo exato teste'})
        self.assertIn('>01/01/2001</td>', resposta.text)
        resposta = self.client.get('/horarios?ausencia_inicio=2001-01-15&ausencia_fim=2001-01-20')
        self.assertIn('>01/01/2001</td>', resposta.text)
        self.assertEqual(self.client.get('/horarios?ausencia_inicio=invalid').status_code, 400)
        with closing(self.horarios.get_db()) as conn, conn:
            conn.execute('DELETE FROM ausencias WHERE funcionario_id = ?', (pessoa,))
            conn.execute('DELETE FROM funcionarios WHERE id = ?', (pessoa,))

    def test_nascimento_cadastro_edicao_e_cobertura(self):
        with closing(self.horarios.get_db()) as conn:
            cargo = conn.execute('SELECT id FROM cargos LIMIT 1').fetchone()[0]
            jornada = conn.execute("SELECT id FROM jornadas WHERE tipo = 'Semana' LIMIT 1").fetchone()[0]
        dados = dict(csrf_token=self.token, nome='Aniversariante teste', prioridade='P2',
                     cargo_id=cargo, jornada_id=jornada, equipe_sabado='Verde', tipo_sabado='nenhum',
                     data_nascimento='2000-09-17')
        for invalida in ('2000-02-30', '9999-01-01', '17/09/2000'):
            self.assertEqual(self.client.post('/horarios/salvar_funcionario',
                data={**dados, 'data_nascimento': invalida}).status_code, 400)
        self.assertEqual(self.client.post('/horarios/salvar_funcionario', data=dados).status_code, 302)
        with closing(self.horarios.get_db()) as conn:
            pessoa = conn.execute('SELECT id, data_nascimento FROM funcionarios WHERE nome = ?',
                                  (dados['nome'],)).fetchone()
        self.assertEqual(pessoa['data_nascimento'], '2000-09-17')
        for dia, esperado in (('2026-09-17', True), ('2026-09-18', False)):
            presentes = [at for slot in self.horarios.calcular_cobertura_diaria(dia)
                         for at in slot['atendentes'] if at['nome'] == dados['nome']]
            self.assertTrue(presentes)
            self.assertTrue(all(at['aniversariante'] == esperado for at in presentes))
        pagina = self.client.get('/horarios?data_cobertura=2026-09-17').get_data(as_text=True)
        self.assertIn('foto-cobertura aniversariante', pagina)
        self.assertIn('id="foto-ampliada"', pagina)
        self.assertIn('2000-09-17', self.client.get(
            f"/horarios/editar_funcionario/{pessoa['id']}").get_data(as_text=True))
        self.assertEqual(self.client.post('/horarios/salvar_funcionario',
            data={**dados, 'id': pessoa['id'], 'data_nascimento': ''}).status_code, 302)
        with closing(self.horarios.get_db()) as conn:
            self.assertIsNone(conn.execute('SELECT data_nascimento FROM funcionarios WHERE id = ?',
                                          (pessoa['id'],)).fetchone()[0])

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

    def test_confirmacao_de_acao_admin_mascara_senha(self):
        html = self.bancos.HTML_ADMIN
        self.assertIn('type="password" id="senhaConfirmacaoAcao"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertNotIn("prompt('Confirme sua senha", html)

    def test_historico_cria_indice_composto(self):
        caminho = str(Path(self.temp.name) / 'historico-indice.db')
        with patch.object(self.bancos, 'DB_HISTORICO', caminho):
            self.bancos.init_db_historico()
        with closing(sqlite3.connect(caminho)) as conn:
            indices = conn.execute("PRAGMA index_list('historico_servidores')").fetchall()
            nomes = {indice[1] for indice in indices}
            self.assertIn('idx_historico_servidor_data', nomes)
            colunas = conn.execute(
                "PRAGMA index_xinfo('idx_historico_servidor_data')"
            ).fetchall()
        self.assertEqual([(coluna[2], coluna[3]) for coluna in colunas[:2]], [
            ('servidor', 0), ('data', 1)
        ])

    def test_historico_tem_scroll_graficos_otimizados_e_chart_condicional(self):
        with closing(sqlite3.connect(self.bancos.DB_HISTORICO)) as conn, conn:
            conn.execute('DELETE FROM historico_servidores')
            conn.executemany(
                'INSERT INTO historico_servidores (data, servidor, total_bytes, qtd_bancos) VALUES (?, ?, ?, ?)',
                [('2026-09-21', 'DB01', 1024 ** 3, 1),
                 ('2026-09-22', 'DB01', 2 * 1024 ** 3, 2)],
            )
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True):
            pagina = self.client.get('/historico?servidores=DB01').get_data(as_text=True)
        self.assertIn('class="historico-scroll" role="region"', pagina)
        self.assertIn('chart.js@4.4.7/dist/chart.umd.min.js', pagina)
        self.assertIn('id="chart-historico-linha"', pagina)
        self.assertIn('id="chart-historico-area"', pagina)
        self.assertIn('role="img" aria-label="Evolução do consumo', pagina)
        self.assertIn('Seu navegador não suporta gráficos em canvas.', pagina)
        self.assertIn('animation: false', pagina)
        self.assertIn('normalized: true', pagina)
        self.assertIn('maxTicksLimit: 12', pagina)
        self.assertIn("stack: 'espaco-servidores'", pagina)

        with patch.dict(self.bancos.SERVIDORES, {}, clear=True):
            pagina_comum = self.client.get('/inativos').get_data(as_text=True)
        self.assertNotIn('chart.umd.min.js', pagina_comum)

    def test_historico_limita_somente_tabela_e_avisa_total(self):
        registros = [
            ((date(2025, 1, 1) + timedelta(days=i)).isoformat(),
             'DB01', (i + 1) * 1024 ** 3, 1)
            for i in range(1201)
        ]
        with closing(sqlite3.connect(self.bancos.DB_HISTORICO)) as conn, conn:
            conn.execute('DELETE FROM historico_servidores')
            conn.executemany(
                'INSERT OR REPLACE INTO historico_servidores (data, servidor, total_bytes, qtd_bancos) VALUES (?, ?, ?, ?)',
                registros,
            )
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True):
            pagina = self.client.get('/historico?servidores=DB01').get_data(as_text=True)
        self.assertIn('Exibindo 500 de 1201 registros.', pagina)
        self.assertIn('selecione um período menor', pagina)
        self.assertIn('Gráfico amostrado: 1000 de 1201 datas', pagina)
        match = re.search(r'const dadosHistorico = (\{.*\});', pagina)
        self.assertIsNotNone(match)
        grafico = json.loads(match.group(1))
        self.assertEqual(len(grafico['labels']), 1000)
        self.assertEqual(grafico['labels'][0], registros[0][0])
        self.assertEqual(grafico['labels'][-1], registros[-1][0])
        self.assertTrue(all(len(dataset['data']) == 1000 for dataset in grafico['datasets']))
        self.assertEqual(pagina.count('<td><strong>'), 500)

    def test_coleta_sem_servidores_e_limite_de_oito_threads(self):
        with patch.dict(self.bancos.SERVIDORES, {}, clear=True):
            self.assertEqual(self.bancos.buscar_em_todos_servidores(), ([], 0, []))
            self.assertEqual(self.bancos.obter_metricas_servidores(), [])

        servidores = {f'DB{i:02d}': {'ip': 'localhost'} for i in range(9)}
        executor_real = self.bancos.ThreadPoolExecutor
        with patch.dict(self.bancos.SERVIDORES, servidores, clear=True), \
                patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=([], 0, [])), \
                patch.object(self.bancos, 'ThreadPoolExecutor', wraps=executor_real) as executor:
            self.bancos.buscar_em_todos_servidores()
        executor.assert_called_once_with(max_workers=8)

        self.bancos.limpar_cache_global()
        with patch.dict(self.bancos.SERVIDORES, servidores, clear=True), \
                patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=([], 0, [])), \
                patch.object(self.bancos, 'calcular_runway_disco', return_value={}), \
                patch.object(self.bancos, 'ThreadPoolExecutor', wraps=executor_real) as executor:
            self.bancos.obter_metricas_servidores(salvar_db=False)
        executor.assert_called_once_with(max_workers=8)

    def test_varredura_de_orfaos_so_quando_solicitada_e_cache_expresso(self):
        arquivo_conf = MagicMock()
        arquivo_conf.__enter__.return_value.read.return_value = b'db = /opt/infobrasil/db.fdb\n'
        stat = MagicMock(st_size=1024, st_mtime=1)
        sftp = MagicMock()
        sftp.open.return_value = arquivo_conf
        sftp.stat.return_value = stat
        ssh = MagicMock()
        ssh.open_sftp.return_value = sftp
        ssh.exec_command.return_value = (MagicMock(), MagicMock(), MagicMock())
        ssh.exec_command.return_value[1].read.return_value = b''
        ssh.exec_command.return_value[1].channel.recv_exit_status.return_value = 0
        ssh.exec_command.return_value[2].read.return_value = b''
        info = {'ip': 'localhost', 'porta_fb': 3050}

        self.bancos.limpar_cache_global()
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.bancos.obter_dados_servidor_linux('DB01', info)
        ssh.exec_command.assert_not_called()
        self.assertFalse(self.bancos.CACHE_DADOS['bancos_por_servidor']['DB01']['orfaos_coletados'])

        ssh.reset_mock()
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.bancos.obter_dados_servidor_linux('DB01', info, varrer_orfaos=True)
        ssh.exec_command.assert_called_once()
        self.assertTrue(self.bancos.CACHE_DADOS['bancos_por_servidor']['DB01']['orfaos_coletados'])

        ssh.reset_mock()
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.bancos.obter_dados_servidor_linux('DB01', info, varrer_orfaos=True)
        ssh.exec_command.assert_not_called()

        # Uma atualização comum forçada não pode descartar a lista rica já coletada.
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.bancos.obter_dados_servidor_linux('DB01', info, forcar_atualizacao=True)
        cache = self.bancos.CACHE_DADOS['bancos_por_servidor']['DB01']
        self.assertTrue(cache['orfaos_coletados'])

    def test_cache_concorrente_descarta_coleta_anterior_a_atualizacao(self):
        iniciou = threading.Event()
        liberar = threading.Event()
        arquivo_conf = MagicMock()
        arquivo_conf.__enter__.return_value.read.return_value = b'db = /opt/infobrasil/db.fdb\n'
        sftp = MagicMock()
        sftp.stat.return_value = MagicMock(st_size=1024, st_mtime=1)
        def abrir(*_args, **_kwargs):
            iniciou.set()
            liberar.wait(2)
            return arquivo_conf
        sftp.open.side_effect = abrir
        ssh = MagicMock()
        ssh.open_sftp.return_value = sftp
        info = {'ip': 'localhost', 'porta_fb': 3050}

        self.bancos.limpar_cache_global()
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            thread = threading.Thread(
                target=self.bancos.obter_dados_servidor_linux,
                args=('DB01', info), daemon=True
            )
            thread.start()
            self.assertTrue(iniciou.wait(1))
            geracao_nova = self.bancos.limpar_cache_global()
            liberar.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.bancos.CACHE_DADOS['generation'], geracao_nova)
        self.assertNotIn('DB01', self.bancos.CACHE_DADOS['bancos_por_servidor'])

        # A primeira leitura após invalidar precisa abrir uma conexão e gravar a geração nova.
        sftp.open.side_effect = None
        sftp.open.return_value = arquivo_conf
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh) as conectar:
            dados, _, _ = self.bancos.obter_dados_servidor_linux('DB01', info)
        conectar.assert_called_once()
        self.assertEqual(dados[0]['alias'], 'db')
        self.assertIn('DB01', self.bancos.CACHE_DADOS['bancos_por_servidor'])

    def test_cache_de_orfaos_tem_frescor_independente_e_fast_path_sem_lock(self):
        self.bancos.limpar_cache_global()
        self.bancos.CACHE_DADOS['ttl_segundos'] = 100
        self.bancos.CACHE_DADOS['bancos_por_servidor']['DB01'] = {
            'dados': [{'alias': 'db', 'eh_inativo': False}], 'bytes': 10,
            'orfaos': [{'caminho': '/opt/infobrasil/orfao.fdb'}],
            'orfaos_coletados': True, 'timestamp': 200, 'orfaos_timestamp': 100,
        }
        with patch.object(self.bancos.time, 'time', return_value=250), \
                patch.object(self.bancos, '_lock_servidor', side_effect=AssertionError('lock desnecessario')):
            dados, total, orfaos = self.bancos.obter_dados_servidor_linux('DB01', {})
        self.assertEqual((dados[0]['alias'], total, orfaos), ('db', 10, []))
        with patch.object(self.bancos.time, 'time', return_value=250):
            self.assertIsNone(self.bancos._obter_resultado_cache_servidor(
                'DB01', None, False, False, True
            ))
        self.bancos.CACHE_DADOS['ttl_segundos'] = 600

    def test_falha_na_varredura_nao_cacheia_orfaos_nem_grava_snapshot_parcial(self):
        arquivo_conf = MagicMock()
        arquivo_conf.__enter__.return_value.read.return_value = b'db = /opt/infobrasil/db.fdb\n'
        sftp = MagicMock()
        sftp.open.return_value = arquivo_conf
        sftp.stat.return_value = MagicMock(st_size=100, st_mtime=1)
        stdout = MagicMock()
        stdout.read.return_value = b''
        stdout.channel.recv_exit_status.return_value = 1
        stderr = MagicMock()
        stderr.read.return_value = b'permission denied'
        ssh = MagicMock()
        ssh.open_sftp.return_value = sftp
        ssh.exec_command.return_value = (MagicMock(), stdout, stderr)
        info = {'ip': 'localhost', 'porta_fb': 3050}
        self.bancos.limpar_cache_global()
        with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            dados, _, orfaos = self.bancos.obter_dados_servidor_linux(
                'DB01', info, modo_busca_unificada=True, varrer_orfaos=True
            )
        self.assertIn('erro', dados[0])
        self.assertEqual(orfaos, [])
        self.assertNotIn('DB01', self.bancos.CACHE_DADOS['bancos_por_servidor'])

        for nome, saida, efeito_stat in (
            ('decode', b'\xff', None),
            ('banco_configurado', b'', [
                MagicMock(st_size=100, st_mtime=1),
                OSError('stat do banco falhou'),
            ]),
            ('stat', b'/opt/infobrasil/orfao.fdb\n', [
                MagicMock(st_size=100, st_mtime=1),
                MagicMock(st_size=100, st_mtime=1),
                OSError('stat falhou'),
            ]),
        ):
            with self.subTest(falha=nome):
                self.bancos.limpar_cache_global()
                stdout.read.return_value = saida
                stdout.channel.recv_exit_status.return_value = 0
                sftp.stat.side_effect = efeito_stat
                with patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
                    resultado, _, _ = self.bancos.obter_dados_servidor_linux(
                        'DB01', info, modo_busca_unificada=True, varrer_orfaos=True
                    )
                self.assertIn('erro', resultado[0])
                self.assertNotIn('DB01', self.bancos.CACHE_DADOS['bancos_por_servidor'])
        sftp.stat.side_effect = None

        caminho = str(Path(self.temp.name) / 'snapshot-incompleto.db')
        with patch.dict(self.bancos.SERVIDORES, {'DB01': info}, clear=True), \
                patch.object(self.bancos, 'DB_HISTORICO', caminho), \
                patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=(dados, 100, [])):
            self.bancos.init_db_historico()
            self.bancos.salvar_historico_diario()
        with closing(sqlite3.connect(caminho)) as conn:
            quantidade = conn.execute('SELECT COUNT(*) FROM historico_servidores').fetchone()[0]
        self.assertEqual(quantidade, 0)

    def test_snapshot_historico_faz_uma_coleta_ssh_por_servidor(self):
        bancos = [
            {'arquivo_existe': True, 'eh_inativo': False},
            {'arquivo_existe': True, 'eh_inativo': True},
        ]
        orfaos = [{'tamanho_bytes': 25}]
        info = {'ip': 'localhost', 'porta_fb': 3050}
        with patch.dict(self.bancos.SERVIDORES, {'DB01': info}, clear=True), \
                patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=(bancos, 100, orfaos)) as coletar:
            self.assertEqual(
                self.bancos.calcular_espaco_total_incluindo_sombra('DB01', info, True), 125
            )
        coletar.assert_called_once_with(
            'DB01', info, modo_busca_unificada=True,
            varrer_orfaos=True, forcar_atualizacao=True
        )

        caminho = str(Path(self.temp.name) / 'snapshot-historico.db')
        with patch.dict(self.bancos.SERVIDORES, {'DB01': info}, clear=True), \
                patch.object(self.bancos, 'DB_HISTORICO', caminho), \
                patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=(bancos, 100, orfaos)) as coletar:
            self.bancos.init_db_historico()
            self.bancos.salvar_historico_diario()
        coletar.assert_called_once()
        with closing(sqlite3.connect(caminho)) as conn:
            total, quantidade = conn.execute(
                'SELECT total_bytes, qtd_bancos FROM historico_servidores WHERE servidor = ?',
                ('DB01',)
            ).fetchone()
        self.assertEqual((total, quantidade), (125, 1))

    def test_alias_e_caminho_remotos_nao_entram_em_javascript_ou_innerhtml(self):
        html_publico = self.bancos.HTML_LAYOUT
        html_admin = self.bancos.HTML_ADMIN
        self.assertNotIn("toggleDetalhes('{{ banco.alias }}')", html_publico)
        self.assertNotIn("copiarString('{{ banco.cname_string }}'", html_publico)
        self.assertIn('onclick="toggleDetalhes(this)"', html_publico)
        self.assertIn('data-cname="{{ banco.cname_string }}"', html_publico)
        self.assertNotIn("confirm('Inativar o alias {{ b.alias }}?')", html_admin)
        self.assertNotIn('${originalAlias}', html_admin)
        self.assertNotIn('${originalCaminho}', html_admin)
        self.assertIn('textContent = originalAlias', html_admin)
        self.assertIn('textContent = originalCaminho', html_admin)

    def test_admin_sem_servidores_responde_controladamente(self):
        with patch.dict(self.bancos.SERVIDORES, {}, clear=True), \
                patch.object(self.bancos, 'conectar_ssh') as conectar:
            resposta = self.client.get('/admin')
        self.assertEqual(resposta.status_code, 503)
        self.assertIn(b'Nenhum servidor', resposta.data)
        conectar.assert_not_called()

    def test_pagina_orfaos_exibe_falha_sem_falso_sucesso(self):
        erro = {
            'servidor': 'DB01', 'erro': 'Erro SSH: varredura incompleta',
            'arquivo_existe': False, 'tamanho_bytes': 0,
        }
        with patch.object(self.bancos, 'buscar_em_todos_servidores', return_value=([erro], 0, [])), \
                patch.object(self.bancos, 'obter_metricas_servidores', return_value=[]):
            resposta = self.client.get('/orfaos')
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(b'N\xc3\xa3o foi poss\xc3\xadvel concluir a varredura', resposta.data)
        self.assertIn(b'DB01', resposta.data)
        self.assertIn(b'varredura incompleta', resposta.data)
        self.assertNotIn(b'Nenhum arquivo \xc3\xb3rf\xc3\xa3o', resposta.data)

    def test_cache_backups_respeita_geracao_e_timestamp_pos_io(self):
        iniciou = threading.Event()
        liberar = threading.Event()
        resposta_http = MagicMock()
        resposta_http.__enter__.return_value = resposta_http
        resposta_http.read.side_effect = lambda: (
            iniciou.set(), liberar.wait(2), b'ultima atualizacao: teste'
        )[-1]
        self.bancos.limpar_cache_global()
        with patch.object(self.bancos.urllib.request, 'urlopen', return_value=resposta_http), \
                patch.object(self.bancos, 'interpretar_status_backups', return_value={'clientes': []}):
            thread = threading.Thread(target=self.bancos.obter_status_backups_ftp, daemon=True)
            thread.start()
            self.assertTrue(iniciou.wait(1))
            self.bancos.limpar_cache_global()
            liberar.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.bancos.CACHE_DADOS['backups_ftp']['dados'])

        resposta_http.read.side_effect = None
        resposta_http.read.return_value = b'ok'
        with patch.object(self.bancos.urllib.request, 'urlopen', return_value=resposta_http), \
                patch.object(self.bancos, 'interpretar_status_backups', return_value={'clientes': []}), \
                patch.object(self.bancos.time, 'time', side_effect=[10, 20]):
            self.bancos.obter_status_backups_ftp()
        self.assertEqual(self.bancos.CACHE_DADOS['backups_ftp']['timestamp'], 20)

    def test_atualizacao_invalida_cache_uma_vez_sem_propagar_forcado(self):
        with patch.dict(self.bancos.SERVIDORES, {}, clear=True), \
                patch.object(self.bancos, 'limpar_cache_global', wraps=self.bancos.limpar_cache_global) as limpar, \
                patch.object(self.bancos, 'obter_metricas_servidores', return_value=[]) as metricas, \
                patch.object(self.bancos, 'buscar_em_todos_servidores', return_value=([], 0, [])) as buscar:
            resposta = self.client.get('/todos?atualizar=1')
        self.assertEqual(resposta.status_code, 200)
        limpar.assert_called_once_with()
        self.assertNotIn('forcar_atualizacao', metricas.call_args.kwargs)
        self.assertNotIn('forcar_atualizacao', buscar.call_args.kwargs)

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
                    patch.object(self.bancos, 'verificar_alias_existente_global', return_value=(None, None)), \
                    patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado',
                                 return_value={'ok': True, 'conteudo': '# configuracao', 'erro': None}), \
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

    def test_verificacao_global_de_alias_falha_fechada(self):
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}, 'DB02': {}}, clear=True), \
                patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado', side_effect=[
                    {'ok': True, 'conteudo': '# vazio', 'erro': None},
                    {'ok': False, 'conteudo': None, 'erro': 'sem conexao'},
                ]):
            self.assertEqual(
                self.bancos.verificar_alias_existente_global('novo'), (None, 'sem conexao')
            )

    def test_escrita_databases_conf_e_atomica_e_limpa_temporario_na_falha(self):
        sftp = MagicMock()
        sftp.normalize.return_value = '/srv/firebird/databases.conf.real'
        sftp.stat.return_value = MagicMock(st_mode=0o100640, st_uid=123, st_gid=456)
        def abrir(_caminho, modo):
            arquivo = MagicMock()
            arquivo.__enter__.return_value = arquivo
            if modo == 'rb':
                arquivo.read.return_value = b'conteudo atual\n'
            return arquivo
        sftp.open.side_effect = abrir
        ssh = MagicMock()
        ssh.open_sftp.return_value = sftp
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {'ip': 'localhost'}}, clear=True), \
                patch.object(self.bancos, 'conectar_ssh', return_value=ssh), \
                patch.object(self.bancos, 'limpar_cache_global'):
            self.assertTrue(self.bancos.salvar_databases_conf_remoto(
                'DB01', 'alias = /opt/infobrasil/alias/dados.fdb\n'
            ))
        chamada_temp = next(c for c in sftp.open.call_args_list if c.args[1] == 'x')
        caminho_temp = chamada_temp.args[0]
        self.assertEqual(self.bancos.posixpath.dirname(caminho_temp), '/srv/firebird')
        sftp.lstat.assert_called_once_with(self.bancos.CAMINHO_DATABASES_CONF)
        sftp.normalize.assert_called_once_with(self.bancos.CAMINHO_DATABASES_CONF)
        sftp.chown.assert_called_once_with(caminho_temp, 123, 456)
        sftp.chmod.assert_called_once_with(caminho_temp, 0o640)
        sftp.posix_rename.assert_called_once_with(caminho_temp, '/srv/firebird/databases.conf.real')

        sftp.reset_mock()
        sftp.normalize.return_value = '/srv/firebird/databases.conf.real'
        sftp.stat.return_value = MagicMock(st_mode=0o100640, st_uid=123, st_gid=456)
        sftp.open.side_effect = abrir
        sftp.posix_rename.side_effect = OSError('rename falhou')
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {'ip': 'localhost'}}, clear=True), \
                patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.assertFalse(self.bancos.salvar_databases_conf_remoto(
                'DB01', 'alias = /opt/infobrasil/alias/dados.fdb\n'
            ))
        sftp.remove.assert_called_once()

    def test_escrita_databases_conf_detecta_cas_e_falha_se_nao_preservar_owner(self):
        sftp = MagicMock()
        sftp.normalize.return_value = '/real/databases.conf'
        sftp.stat.return_value = MagicMock(st_mode=0o100600, st_uid=10, st_gid=20)
        leituras = iter((b'original', b'alterado por outro processo'))
        def abrir(_caminho, modo):
            arquivo = MagicMock()
            arquivo.__enter__.return_value = arquivo
            if modo == 'rb':
                arquivo.read.side_effect = lambda: next(leituras)
            return arquivo
        sftp.open.side_effect = abrir
        ssh = MagicMock()
        ssh.open_sftp.return_value = sftp
        hash_original = self.bancos.hashlib.sha256(b'original').hexdigest()
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {'ip': 'localhost'}}, clear=True), \
                patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.assertFalse(self.bancos.salvar_databases_conf_remoto(
                'DB01', 'novo', hash_original
            ))
        sftp.posix_rename.assert_not_called()
        sftp.remove.assert_called_once()

        sftp.reset_mock()
        sftp.normalize.return_value = '/real/databases.conf'
        sftp.stat.return_value = MagicMock(st_mode=0o100600, st_uid=10, st_gid=20)
        def abrir_estavel(_caminho, modo):
            arquivo = MagicMock()
            arquivo.__enter__.return_value = arquivo
            if modo == 'rb':
                arquivo.read.return_value = b'original'
            return arquivo
        sftp.open.side_effect = abrir_estavel
        sftp.chown.side_effect = OSError('chown negado')
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {'ip': 'localhost'}}, clear=True), \
                patch.object(self.bancos, 'conectar_ssh', return_value=ssh):
            self.assertFalse(self.bancos.salvar_databases_conf_remoto(
                'DB01', 'novo', hash_original
            ))
        sftp.posix_rename.assert_not_called()
        sftp.remove.assert_called_once()

    def test_admin_inativar_nao_anuncia_sucesso_em_falha_ou_alias_ausente(self):
        for leitura, alias, salvar in (
            ({'ok': False, 'conteudo': None, 'erro': 'falha'}, 'alias', True),
            ({'ok': True, 'conteudo': 'outro = /opt/infobrasil/outro/dados.fdb\n', 'erro': None}, 'alias', True),
            ({'ok': True, 'conteudo': 'alias = /opt/infobrasil/alias/dados.fdb\n', 'erro': None}, 'alias', False),
        ):
            with self.subTest(leitura=leitura, salvar=salvar), \
                    patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True), \
                    patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado', return_value=leitura), \
                    patch.object(self.bancos, 'salvar_databases_conf_remoto', return_value=salvar):
                resposta = self.client.post('/admin/inativar', data={
                    'csrf_token': self.token, 'servidor': 'DB01', 'alias': alias,
                    'senha_confirmacao': self.password,
                })
            self.assertIn('erro=', resposta.location)
            self.assertNotIn('sucesso=', resposta.location)

    def test_admin_salvar_raw_valida_leitura_conteudo_e_escrita(self):
        hash_atual = 'a' * 64
        casos = (
            ({'ok': False, 'conteudo': None, 'erro': 'falha'}, 'alias = /opt/x.fdb', True),
            ({'ok': True, 'conteudo': '# atual', 'erro': None, 'hash': hash_atual}, 'invalido\x00', True),
            ({'ok': True, 'conteudo': '# atual', 'erro': None, 'hash': hash_atual},
             'alias = /opt/infobrasil/alias/dados.fdb', False),
        )
        for leitura, conteudo, salvo in casos:
            with self.subTest(leitura=leitura, salvo=salvo), \
                    patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True), \
                    patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado', return_value=leitura), \
                    patch.object(self.bancos, 'salvar_databases_conf_remoto', return_value=salvo):
                resposta = self.client.post('/admin/salvar_raw', data={
                    'csrf_token': self.token, 'servidor': 'DB01', 'conteudo_raw': conteudo,
                    'senha_confirmacao': self.password, 'hash_original': hash_atual.upper(),
                })
            self.assertIn('erro=', resposta.location)
            self.assertNotIn('sucesso=', resposta.location)

        bloco_firebird = '''cliente = /opt/infobrasil/cliente/dados.fdb
{
    RemoteAccess = false
}
'''
        self.assertEqual(
            self.bancos.validar_conteudo_databases_conf(bloco_firebird), (True, None)
        )
        self.assertFalse(self.bancos.validar_conteudo_databases_conf(
            '# Não foi possível ler databases.conf em DB01.'
        )[0])

    def test_editor_raw_preserva_hash_do_get_e_rejeita_edicao_obsoleta(self):
        hash_get = '1' * 64
        leitura_get = {'ok': True, 'conteudo': '# versao H0', 'erro': None, 'hash': hash_get}
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {'rotulo': 'DB01'}}, clear=True), \
                patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado', return_value=leitura_get), \
                patch.object(self.bancos, 'obter_dados_servidor_linux', return_value=([], 0, [])):
            pagina = self.client.get('/admin?servidor=DB01&sucesso=ok').get_data(as_text=True)
        self.assertIn(f'name="hash_original" value="{hash_get}"', pagina)
        self.assertIn('# versao H0', pagina)

        leitura_post = {'ok': True, 'conteudo': '# versao H1', 'erro': None, 'hash': '2' * 64}
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True), \
                patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado', return_value=leitura_post), \
                patch.object(self.bancos, 'salvar_databases_conf_remoto') as salvar:
            resposta = self.client.post('/admin/salvar_raw', data={
                'csrf_token': self.token, 'servidor': 'DB01', 'conteudo_raw': '# minha edicao',
                'senha_confirmacao': self.password, 'hash_original': hash_get,
            })
        self.assertIn('Conflito', resposta.location)
        salvar.assert_not_called()

    def test_editor_raw_repassa_exatamente_hash_get_time_normalizado(self):
        hash_get_maiusculo = 'ABCDEF' * 10 + 'ABCD'
        self.assertEqual(len(hash_get_maiusculo), 64)
        leitura = {
            'ok': True, 'conteudo': '# atual', 'erro': None,
            'hash': hash_get_maiusculo.lower(),
        }
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True), \
                patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado', return_value=leitura), \
                patch.object(self.bancos, 'salvar_databases_conf_remoto', return_value=True) as salvar:
            resposta = self.client.post('/admin/salvar_raw', data={
                'csrf_token': self.token, 'servidor': 'DB01', 'conteudo_raw': '# novo',
                'senha_confirmacao': self.password, 'hash_original': hash_get_maiusculo,
            })
        self.assertIn('sucesso=', resposta.location)
        salvar.assert_called_once_with('DB01', '# novo', hash_get_maiusculo.lower())

        for invalido in ('', 'a' * 63, 'g' * 64, 'a' * 65):
            with self.subTest(hash=invalido), \
                    patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True), \
                    patch.object(self.bancos, 'ler_databases_conf_remoto_estruturado') as ler:
                resposta = self.client.post('/admin/salvar_raw', data={
                    'csrf_token': self.token, 'servidor': 'DB01', 'conteudo_raw': '# novo',
                    'senha_confirmacao': self.password, 'hash_original': invalido,
                })
            self.assertIn('erro=', resposta.location)
            ler.assert_not_called()

    def test_admin_adicionar_aborta_se_servidor_global_estiver_ilegivel(self):
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True), \
                patch.object(self.bancos, 'verificar_alias_existente_global',
                             return_value=(None, 'DB02 indisponivel')), \
                patch.object(self.bancos, 'conectar_ssh') as conectar:
            resposta = self.client.post('/admin/adicionar', data={
                'csrf_token': self.token, 'servidor': 'DB01', 'alias': 'novo',
                'senha_confirmacao': self.password,
            })
        self.assertIn('erro=', resposta.location)
        conectar.assert_not_called()

    def test_rota_de_perfil_legada_exige_senha_atual_e_politica(self):
        dados = {'csrf_token': self.token, 'nova_senha': 'curta',
                 'confirmar_nova_senha': 'curta', 'senha_atual': self.password}
        self.client.post('/admin/perfil', data=dados)
        with self.client.session_transaction() as sessao:
            self.assertTrue(any('mínimo' in mensagem for _, mensagem in sessao.get('_flashes', [])))
            sessao.pop('_flashes', None)
        dados.update(nova_senha='nova-senha-segura', confirmar_nova_senha='nova-senha-segura',
                     senha_atual='incorreta')
        self.client.post('/admin/perfil', data=dados)
        with self.client.session_transaction() as sessao:
            self.assertTrue(any('Senha atual incorreta' in mensagem for _, mensagem in sessao.get('_flashes', [])))

    def test_api_historico_filtra_paginar_e_rejeita_parametros_invalidos(self):
        with closing(sqlite3.connect(self.bancos.DB_HISTORICO)) as conn, conn:
            conn.execute('DELETE FROM historico_servidores')
            conn.executemany(
                'INSERT INTO historico_servidores (data, servidor, total_bytes, qtd_bancos) VALUES (?, ?, ?, ?)',
                [('2026-01-01', 'DB01', 1, 1), ('2026-01-02', 'DB01', 2, 2),
                 ('2026-01-02', 'DB02', 3, 3)],
            )
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}, 'DB02': {}}, clear=True):
            resposta = self.client.get('/api/historico?servidor=DB01&data_inicio=2026-01-01&limite=1')
            self.assertEqual(resposta.status_code, 200)
            self.assertIsInstance(resposta.get_json(), list)
            self.assertEqual(resposta.get_json()[0]['servidor'], 'DB01')
            self.assertEqual(resposta.headers['X-Total-Count'], '2')
            self.assertEqual(resposta.headers['X-Limit'], '1')
            self.assertIn('rel="next"', resposta.headers['Link'])
            self.assertEqual(self.client.get('/api/historico?servidor=desconhecido').status_code, 400)
            self.assertEqual(self.client.get('/api/historico?limite=1001').status_code, 400)
            self.assertEqual(self.client.get('/api/historico?offset=10000001').status_code, 400)
            self.assertEqual(self.client.get('/api/historico?offset=999999999999999999999999').status_code, 400)
            self.assertEqual(self.client.get('/api/historico?data_inicio=ontem').status_code, 400)

    def test_historico_rejeita_servidor_desconhecido_e_excesso(self):
        with patch.dict(self.bancos.SERVIDORES, {'DB01': {}}, clear=True):
            self.assertEqual(self.client.get('/historico?servidores=desconhecido').status_code, 400)
        servidores = {f'DB{i}': {} for i in range(51)}
        with patch.dict(self.bancos.SERVIDORES, servidores, clear=True):
            query = '&'.join(f'servidores=DB{i}' for i in range(51))
            self.assertEqual(self.client.get('/historico?' + query).status_code, 400)

    def test_secret_key_curta_e_bootstrap_fraco_sao_recusados(self):
        app = Flask(__name__)
        with patch.dict(os.environ, {'SECRET_KEY': 'curta'}), self.assertRaises(RuntimeError):
            self.security.aplicar_config_flask(app)
        with patch.dict(os.environ, {
            'ADMIN_MASTER_EMAIL': 'novo-master@example.invalid',
            'ADMIN_MASTER_PASSWORD': 'curta',
        }), self.assertRaises(RuntimeError):
            self.bancos.init_db_sistema()

    def test_bootstrap_fraco_nao_bloqueia_master_ja_existente(self):
        with closing(self.bancos.get_db_connection()) as conn:
            email = conn.execute('SELECT email FROM usuarios WHERE id = ?', (self.user_id,)).fetchone()[0]
        with patch.dict(os.environ, {
            'ADMIN_MASTER_EMAIL': email,
            'ADMIN_MASTER_PASSWORD': 'curta',
        }):
            self.bancos.init_db_sistema()

    def test_refresh_publico_repetido_e_coalescido(self):
        self.bancos.ULTIMO_REFRESH.clear()
        with patch.object(self.bancos.time, 'monotonic', side_effect=[100, 101, 120]), \
                self.app.test_request_context('/?atualizar=1'):
            self.assertTrue(self.bancos.solicitar_refresh('dados'))
            self.assertFalse(self.bancos.solicitar_refresh('dados'))
            self.assertTrue(self.bancos.solicitar_refresh('dados'))

        self.bancos.ULTIMO_REFRESH.clear()
        with patch.dict(self.bancos.SERVIDORES, {}, clear=True), \
                patch.object(self.bancos, 'limpar_cache_global') as limpar, \
                patch.object(self.bancos, 'obter_metricas_servidores', return_value=[]), \
                patch.object(self.bancos, 'buscar_em_todos_servidores', return_value=([], 0, [])), \
                patch.object(self.bancos, 'obter_status_backups_ftp', return_value={'erro': 'indisponível'}), \
                patch.object(self.bancos.time, 'monotonic', side_effect=[200, 201]):
            self.client.get('/todos?atualizar=1')
            self.client.get('/todos?atualizar=1')
        limpar.assert_called_once_with()

    def test_cache_retorna_copias_e_nao_vaza_detalhe_ssh(self):
        self.bancos.limpar_cache_global()
        self.bancos.CACHE_DADOS['bancos_por_servidor']['DB01'] = {
            'dados': [{'alias': 'db', 'eh_inativo': False}], 'bytes': 1,
            'orfaos': [], 'orfaos_coletados': False,
            'timestamp': self.bancos.time.time(), 'orfaos_timestamp': 0,
        }
        primeira, _, _ = self.bancos.obter_dados_servidor_linux('DB01', {})
        primeira[0]['alias'] = 'mutado'
        segunda, _, _ = self.bancos.obter_dados_servidor_linux('DB01', {})
        self.assertEqual(segunda[0]['alias'], 'db')

        self.bancos.CACHE_DADOS['bancos_por_servidor']['DB01']['dados'].append(
            {'alias': 'outro', 'eh_inativo': False}
        )
        deepcopy_real = self.bancos.copy.deepcopy
        with patch.object(self.bancos.copy, 'deepcopy', wraps=deepcopy_real) as copiar:
            filtrados, _, _ = self.bancos._obter_resultado_cache_servidor(
                'DB01', 'db', False, False, False
            )
        self.assertEqual([b['alias'] for b in filtrados], ['db'])
        listas_copiadas = [c.args[0] for c in copiar.call_args_list if isinstance(c.args[0], list)]
        self.assertTrue(listas_copiadas)
        self.assertTrue(all(len(lista) == 1 for lista in listas_copiadas))

        with patch.object(self.bancos, 'conectar_ssh', side_effect=OSError('segredo-remoto')):
            dados, _, _ = self.bancos.obter_dados_servidor_linux(
                'DB02', {'ip': '10.0.0.1', 'porta_fb': 3050}, forcar_atualizacao=True
            )
        self.assertNotIn('segredo-remoto', dados[0]['erro'])

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
