from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from flask import Flask
from certificados import MonitorCertificados, agora, validar_dominio, configurar_certificados, formatar_data


class CertificadosTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'teste.db')
        self.monitor = MonitorCertificados(self.conectar)

    def tearDown(self):
        self.temp.cleanup()

    def conectar(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def liberar(self):
        with closing(self.conectar()) as conn, conn:
            conn.execute('UPDATE certificados_rotina SET bloqueio_ate=NULL')

    def test_horario_fortaleza_com_mudanca_de_dia(self):
        self.assertEqual(formatar_data('2026-11-01T12:00:00+00:00'), '01/11/2026 09:00')
        self.assertEqual(formatar_data('2026-11-01T01:00:00+00:00'), '31/10/2026 22:00')
        self.assertEqual(formatar_data(None), '—')

    def test_validacao_ssrf(self):
        for dominio in ('localhost', '127.0.0.1', 'https://api.infobrasilsistemas.com.br',
                        'api.infobrasilsistemas.com.br:443', 'api.infobrasilsistemas.com.br/rota',
                        'api.infobrasilsistemas.com.br.evil.com', '-api.infobrasilsistemas.com.br'):
            with self.assertRaises(ValueError):
                validar_dominio(dominio)
        self.assertEqual(validar_dominio(' API.infobrasilsistemas.com.br '), 'api.infobrasilsistemas.com.br')
        from certificados import consultar_certificado
        with patch('certificados.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.1', 443))]), patch('certificados.socket.socket') as sock:
            with self.assertRaises(ValueError):
                consultar_certificado('api.infobrasilsistemas.com.br')
            sock.assert_not_called()

    def test_email_agrupado_deduplicado_e_renovacao(self):
        vencimento = (agora() + timedelta(days=4)).isoformat()
        with patch('certificados.consultar_certificado', return_value=(vencimento, 'Vencendo')), patch('certificados.enviar_email') as email:
            self.monitor.verificar(manual=True)
            self.assertEqual(len(email.call_args.args[0]), 8)
            self.liberar()
            self.monitor.verificar(manual=True)
            email.assert_called_once()
            with closing(self.conectar()) as conn, conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM certificados_emails').fetchone()[0], 1)
            self.liberar()
            novo = (agora() + timedelta(days=6)).isoformat()
            with patch('certificados.consultar_certificado', return_value=(novo, 'Vencendo')):
                self.monitor.verificar(manual=True)
            self.assertEqual(email.call_count, 2)

    def test_falha_email_tenta_novamente(self):
        vencimento = (agora() - timedelta(days=2)).isoformat()
        with patch('certificados.consultar_certificado', return_value=(vencimento, 'Expirado')), patch('certificados.enviar_email', side_effect=RuntimeError('teste')):
            self.monitor.verificar()
        with closing(self.conectar()) as conn, conn:
            self.assertIsNone(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0])
            self.assertIsNone(conn.execute('SELECT email_em FROM certificados LIMIT 1').fetchone()[0])
        self.liberar()
        with patch('certificados.consultar_certificado', return_value=(vencimento, 'Expirado')), patch('certificados.enviar_email') as email:
            self.monitor.verificar()
            email.assert_called_once()

    def test_rotina_semanal_e_falha_de_consulta(self):
        with patch('certificados.consultar_certificado', side_effect=OSError('DNS indisponível')):
            self.monitor.verificar()
        with closing(self.conectar()) as conn:
            self.assertIsNone(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0])
        self.liberar()
        with patch('certificados.consultar_certificado', return_value=((agora() + timedelta(days=90)).isoformat(), 'Válido')) as consultar:
            self.monitor.verificar()
            self.liberar()
            self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)

    def test_transicao_segunda_e_quinta_meia_noite_fortaleza(self):
        # Transição domingo 23:59 -> segunda 00:00 (Fortaleza UTC-3)
        with patch('certificados.consultar_certificado', return_value=('2030-01-01T00:00:00+00:00', 'Válido')) as consultar:
            # 2026-10-11 23:59 Fortaleza (UTC 2026-10-12T02:59:00) pertence ao bloco qui-dom (chave 2026-10-08)
            # 2026-10-12 00:00 Fortaleza (UTC 2026-10-12T03:00:00) pertence ao bloco seg-qua (chave 2026-10-12)
            for instante in ('2026-10-12T02:59:00+00:00', '2026-10-12T03:00:00+00:00'):
                self.liberar()
                with patch('certificados.agora', return_value=datetime.fromisoformat(instante)):
                    self.monitor.verificar()
            self.assertEqual(consultar.call_count, 16)
        with closing(self.conectar()) as conn:
            self.assertEqual(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0], '2026-10-12')

    def test_agendamento_segunda_e_quinta_mesma_semana(self):
        with patch('certificados.consultar_certificado', return_value=('2030-01-01T00:00:00+00:00', 'Válido')) as consultar:
            # 1. Execução de segunda-feira (12/10/2026 às 00:00 Fortaleza / 03:00 UTC)
            instante_seg = datetime.fromisoformat('2026-10-12T03:00:00+00:00')
            with patch('certificados.agora', return_value=instante_seg):
                res_seg = self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)
            self.assertIn('concluída', res_seg.lower())
            with closing(self.conectar()) as conn:
                self.assertEqual(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0], '2026-10-12')

            self.liberar()

            # 2. Execução de quinta-feira na MESMA semana (15/10/2026 às 00:00 Fortaleza / 03:00 UTC)
            instante_qui = datetime.fromisoformat('2026-10-15T03:00:00+00:00')
            with patch('certificados.agora', return_value=instante_qui):
                res_qui = self.monitor.verificar()
            # Deve executar novamente na mesma semana (8 consultas adicionais = 16)
            self.assertEqual(consultar.call_count, 16)
            self.assertIn('concluída', res_qui.lower())
            with closing(self.conectar()) as conn:
                self.assertEqual(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0], '2026-10-15')

    def test_evitar_duplicacao_seg_qua_e_qui_dom(self):
        with patch('certificados.consultar_certificado', return_value=('2030-01-01T00:00:00+00:00', 'Válido')) as consultar:
            # Segunda-feira 00h roda
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-12T03:00:00+00:00')):
                self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)

            self.liberar()

            # Terça-feira (mesmo bloco seg-qua): não deve rodar novamente
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-13T13:00:00+00:00')):
                res_ter = self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)
            self.assertIn('já concluída', res_ter.lower())

            self.liberar()

            # Quarta-feira (mesmo bloco seg-qua): não deve rodar novamente
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-14T20:00:00+00:00')):
                res_qua = self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)
            self.assertIn('já concluída', res_qua.lower())

            self.liberar()

            # Quinta-feira (novo bloco qui-dom): deve rodar
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-15T03:00:00+00:00')):
                self.monitor.verificar()
            self.assertEqual(consultar.call_count, 16)

            self.liberar()

            # Sexta-feira (mesmo bloco qui-dom): não deve rodar novamente
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-16T15:00:00+00:00')):
                res_sex = self.monitor.verificar()
            self.assertEqual(consultar.call_count, 16)
            self.assertIn('já concluída', res_sex.lower())

            self.liberar()

            # Domingo (mesmo bloco qui-dom): não deve rodar novamente
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-18T18:00:00+00:00')):
                res_dom = self.monitor.verificar()
            self.assertEqual(consultar.call_count, 16)
            self.assertIn('já concluída', res_dom.lower())

    def test_migracao_chave_antiga_semana(self):
        # Simula chave legada no banco (ex: domingo '2026-10-11' da rotina semanal anterior)
        with closing(self.conectar()) as conn, conn:
            conn.execute("UPDATE certificados_rotina SET semana='2026-10-11', bloqueio_ate=NULL WHERE id=1")

        with patch('certificados.consultar_certificado', return_value=('2030-01-01T00:00:00+00:00', 'Válido')) as consultar:
            # Na segunda-feira seguinte (12/10), a rotina não pode ser bloqueada pela chave antiga de domingo
            with patch('certificados.agora', return_value=datetime.fromisoformat('2026-10-12T03:00:00+00:00')):
                res = self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)
            self.assertIn('concluída', res.lower())
            with closing(self.conectar()) as conn:
                # Chave atualizada para o novo padrão do bloco seg-qua ('2026-10-12')
                self.assertEqual(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0], '2026-10-12')

    def test_migracao_mensal_e_agendamento_semanal(self):
        from flask import Blueprint
        with closing(self.conectar()) as conn, conn:
            conn.execute('DROP TABLE certificados_rotina')
            conn.execute('CREATE TABLE certificados_rotina(id INTEGER PRIMARY KEY, mes TEXT, bloqueio_ate TEXT)')
            conn.execute("INSERT INTO certificados_rotina VALUES(1, '2026-10', NULL)")
        scheduler = MagicMock()
        configurar_certificados(Blueprint('bancos', __name__), self.conectar, lambda _: False, scheduler)
        with closing(self.conectar()) as conn:
            self.assertIsNone(conn.execute('SELECT semana FROM certificados_rotina').fetchone()[0])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM certificados').fetchone()[0], 8)
        configuracao = scheduler.add_job.call_args.kwargs
        self.assertEqual((configuracao['day_of_week'], configuracao['hour'], configuracao['minute']), ('mon,thu', 0, 0))
        self.assertEqual(configuracao['timezone'], 'America/Fortaleza')

    def test_publico_e_permissao(self):
        from flask import Blueprint
        app = Flask(__name__, template_folder=str(Path.cwd() / 'templates'))
        app.secret_key = 'teste'
        app.jinja_env.globals['csrf_token'] = lambda: 'teste'
        bp = Blueprint('bancos', __name__)
        configurar_certificados(bp, self.conectar, lambda _: False, MagicMock())
        app.register_blueprint(bp)
        client = app.test_client()
        self.assertEqual(client.get('/certificados').status_code, 200)
        self.assertNotIn('Verificar agora', client.get('/certificados').text)
        self.assertEqual(client.post('/admin/certificados/adicionar', data={'dominio': 'teste.infobrasilsistemas.com.br'}).status_code, 403)
        self.assertEqual(client.post('/admin/certificados/verificar').status_code, 403)

    def test_ordenacao_numerica_datas_e_campos_ausentes(self):
        from flask import Blueprint
        app = Flask(__name__, template_folder=str(Path.cwd() / 'templates'))
        app.secret_key = 'teste'
        app.jinja_env.globals['csrf_token'] = lambda: 'teste'
        bp = Blueprint('bancos', __name__)
        configurar_certificados(bp, self.conectar, lambda _: False, MagicMock())
        app.register_blueprint(bp)
        with closing(self.conectar()) as conn, conn:
            conn.execute("DELETE FROM certificados")
            conn.executemany('INSERT INTO certificados(dominio, expira_em) VALUES (?,?)', [
                ('dez.infobrasilsistemas.com.br', (agora() + timedelta(days=10)).isoformat()),
                ('dois.infobrasilsistemas.com.br', (agora() + timedelta(days=2)).isoformat()),
                ('ausente.infobrasilsistemas.com.br', None)])
        client = app.test_client()
        for coluna in ('dias', 'expira_em'):
            for direcao, primeiro, segundo in (('asc', 'dois', 'dez'), ('desc', 'dez', 'dois')):
                html = client.get('/certificados', query_string={'ordem': coluna, 'direcao': direcao}).text
                self.assertLess(html.index('https://' + primeiro), html.index('https://' + segundo))
                self.assertLess(html.index('https://' + segundo), html.index('https://ausente'))
        self.assertEqual(client.get('/certificados?ordem=campo_invalido').status_code, 200)

    def test_email_corpo_e_tls(self):
        from certificados import enviar_email
        with patch.dict('os.environ', {'SMTP_USER': 'test@example.invalid', 'SMTP_PASS': 'test', 'SMTP_PORT': '587'}), patch('certificados.smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            enviar_email([{'dominio': 'api.infobrasilsistemas.com.br', 'expira_em': (agora() + timedelta(days=5)).isoformat()}])
            servidor = smtp.return_value.__enter__.return_value
            servidor.starttls.assert_called_once()
            msg = servidor.send_message.call_args.args[0]
            self.assertEqual(msg['To'], 'atendimento@nossatelecom.com.br')
            self.assertEqual(msg['Subject'], 'Certificado vencendo')
            self.assertIn('Suporte Infobrasil', msg.get_content())
            self.assertIn('Mensagem automática', msg.get_content())
            servidor.send_message.reset_mock()
            enviar_email([{'dominio': 'api.infobrasilsistemas.com.br', 'expira_em': '2026-11-01T12:00:00+00:00'}], teste=True)
            teste = servidor.send_message.call_args.args[0]
            self.assertEqual(teste['To'], 'suporte@infobrasilsistemas.com.br')
            self.assertIsNone(teste['Cc'])
            self.assertIsNone(teste['Bcc'])
            self.assertIn('Datas fictícias', teste.get_content())
            self.assertIn('01/11/2026 09:00', teste.get_content())

    def test_remover_subdominio_completo(self):
        from flask import Blueprint, url_for
        app = Flask(__name__, template_folder=str(Path.cwd() / 'templates'))
        app.secret_key = 'teste'
        app.jinja_env.globals['csrf_token'] = lambda: 'token-valido'
        # O menu comum vem do context processor de bancos_bp, que não é registrado neste app mínimo.
        app.jinja_env.globals['tem_permissao'] = lambda p: True
        bp = Blueprint('bancos', __name__)
        configurar_certificados(bp, self.conectar, lambda p: True, MagicMock())
        app.register_blueprint(bp)
        client = app.test_client()

        # url_for('certificados.remover') deve resolver para /certificados/remover
        with app.test_request_context():
            self.assertEqual(url_for('certificados.remover'), '/certificados/remover')
            self.assertEqual(url_for('bancos.remover'), '/certificados/remover')

        # 1. Sem GET para exclusão (deve retornar 405 Method Not Allowed)
        self.assertEqual(client.get('/certificados/remover').status_code, 405)

        # 2. Sem login: redireciona para tela de login
        with client.session_transaction() as sess:
            sess['_csrf_token'] = 'token-valido'
        resp_sem_login = client.post('/certificados/remover', data={
            'subdominio': 'info-api.infobrasilsistemas.com.br',
            'csrf_token': 'token-valido'
        })
        self.assertEqual(resp_sem_login.status_code, 302)
        self.assertIn('/admin/login', resp_sem_login.headers.get('Location', ''))

        # 3. Sem CSRF (ou CSRF inválido) para usuário logado: 400
        with client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['_csrf_token'] = 'token-valido'
        resp_sem_csrf = client.post('/certificados/remover', data={
            'subdominio': 'info-api.infobrasilsistemas.com.br'
        })
        self.assertEqual(resp_sem_csrf.status_code, 400)

        # 4. Subdomínio com formato inválido
        resp_formato = client.post('/certificados/remover', data={
            'subdominio': 'invalido-sem-sufixo',
            'csrf_token': 'token-valido'
        }, follow_redirects=True)
        self.assertEqual(resp_formato.status_code, 200)

        # 5. Subdomínio inexistente (formato válido, mas não cadastrado)
        resp_inexistente = client.post('/certificados/remover', data={
            'subdominio': 'fantasma.infobrasilsistemas.com.br',
            'csrf_token': 'token-valido'
        }, follow_redirects=True)
        self.assertEqual(resp_inexistente.status_code, 200)
        self.assertIn('não encontrado', resp_inexistente.text)

        # 6. Remoção ok: remove do banco, limpa semana da rotina e registra auditoria
        dominio_alvo = 'info-api.infobrasilsistemas.com.br'
        with closing(self.conectar()) as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM certificados WHERE dominio=?', (dominio_alvo,)).fetchone())

        with patch('certificados.registrar_acao_admin') as mock_audit:
            resp_ok = client.post('/certificados/remover', data={
                'subdominio': dominio_alvo,
                'csrf_token': 'token-valido'
            }, follow_redirects=True)
            self.assertEqual(resp_ok.status_code, 200)
            self.assertIn('removido com sucesso', resp_ok.text)
            mock_audit.assert_called_once()
            args_audit = mock_audit.call_args[0]
            self.assertEqual(args_audit[0], 'remover_subdominio_certificado')
            self.assertEqual(args_audit[1]['subdominio'], dominio_alvo)

        with closing(self.conectar()) as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM certificados WHERE dominio=?', (dominio_alvo,)).fetchone())

        # 7. Sem permissão (logged_in mas tem_permissao retorna False)
        app_sem_perm = Flask(__name__, template_folder=str(Path.cwd() / 'templates'))
        app_sem_perm.secret_key = 'teste'
        bp_sem_perm = Blueprint('bancos', __name__)
        configurar_certificados(bp_sem_perm, self.conectar, lambda p: False, MagicMock())
        app_sem_perm.register_blueprint(bp_sem_perm)
        client_sem_perm = app_sem_perm.test_client()
        with client_sem_perm.session_transaction() as sess:
            sess['logged_in'] = True
            sess['_csrf_token'] = 'token-valido'
        resp_403 = client_sem_perm.post('/certificados/remover', data={
            'subdominio': 'api-tray.infobrasilsistemas.com.br',
            'csrf_token': 'token-valido'
        })
        self.assertEqual(resp_403.status_code, 403)

    def test_limiar_alerta_email_7_dias_e_expirados(self):
        from certificados import ALERTA_DIAS
        self.assertEqual(ALERTA_DIAS, 7)

        # 1. Caso real: Verificacao em 01/11 com expiracao em 03/12/2026 (32 dias)
        instante_01_nov = datetime.fromisoformat('2026-11-01T03:00:00+00:00')
        expira_32_dias = datetime.fromisoformat('2026-12-03T12:00:00+00:00').isoformat()

        with patch('certificados.agora', return_value=instante_01_nov), \
             patch('certificados.consultar_certificado', return_value=(expira_32_dias, 'Válido')), \
             patch('certificados.enviar_email') as mock_email:
            self.monitor.verificar(manual=True)
            mock_email.assert_not_called()

        self.liberar()

        # 2. Certificado com 8 dias para expirar: NAO deve enviar e-mail
        expira_8_dias = (instante_01_nov + timedelta(days=8)).isoformat()
        with patch('certificados.agora', return_value=instante_01_nov), \
             patch('certificados.consultar_certificado', return_value=(expira_8_dias, 'Válido')), \
             patch('certificados.enviar_email') as mock_email:
            self.monitor.verificar(manual=True)
            mock_email.assert_not_called()

        self.liberar()

        # 3. Certificado com exatamente 7 dias para expirar: DEVE enviar e-mail
        expira_7_dias = (instante_01_nov + timedelta(days=7)).isoformat()
        with patch('certificados.agora', return_value=instante_01_nov), \
             patch('certificados.consultar_certificado', return_value=(expira_7_dias, 'Vencendo')), \
             patch('certificados.enviar_email') as mock_email:
            self.monitor.verificar(manual=True)
            mock_email.assert_called_once()
            self.assertEqual(len(mock_email.call_args[0][0]), 8)

        self.liberar()

        # 4. Certificado ja expirado (-1 dia): DEVE enviar e-mail
        expirado = (instante_01_nov - timedelta(days=1)).isoformat()
        with patch('certificados.agora', return_value=instante_01_nov), \
             patch('certificados.consultar_certificado', return_value=(expirado, 'Expirado')), \
             patch('certificados.enviar_email') as mock_email:
            self.monitor.verificar(manual=True)
            mock_email.assert_called_once()
            self.assertEqual(len(mock_email.call_args[0][0]), 8)

    def test_aviso_anterior_fora_da_janela_nao_impede_aviso_oportuno_7_dias(self):
        # 1. Simula estado resultante do aviso prematuro enviado em 01/11 para vencimento em 03/12/2026 (32 dias antes)
        expiracao_03_dez = '2026-12-03T12:00:00+00:00'
        envio_prematuro_01_nov = '2026-11-01T03:00:00+00:00'
        with closing(self.conectar()) as conn, conn:
            conn.execute("""UPDATE certificados SET expira_em=?, email_em=?, email_expiracao=?, email_status='Enviado'
                            WHERE dominio='info-api.infobrasilsistemas.com.br' """,
                         (expiracao_03_dez, envio_prematuro_01_nov, expiracao_03_dez))

        # 2. Em 28/11/2026 (5 dias para expirar, dentro da janela de 7 dias):
        instante_28_nov = datetime.fromisoformat('2026-11-28T03:00:00+00:00')
        with patch('certificados.agora', return_value=instante_28_nov), \
             patch('certificados.consultar_certificado', return_value=(expiracao_03_dez, 'Vencendo')), \
             patch('certificados.enviar_email') as mock_email:
            res = self.monitor.verificar(manual=True)
            # O aviso prematuro de 01/11 NAO pode suprimir o aviso correto de 28/11:
            mock_email.assert_called_once()
            dominios_avisados = [item['dominio'] for item in mock_email.call_args[0][0]]
            self.assertIn('info-api.infobrasilsistemas.com.br', dominios_avisados)

        self.liberar()

        # 3. Em 30/11/2026 (3 dias para expirar, ainda na mesma janela e para o mesmo vencimento):
        # O envio de 28/11 foi oportuno (dentro dos 7 dias), entao a deduplicacao deve funcionar e NAO reenviar
        instante_30_nov = datetime.fromisoformat('2026-11-30T03:00:00+00:00')
        with patch('certificados.agora', return_value=instante_30_nov), \
             patch('certificados.consultar_certificado', return_value=(expiracao_03_dez, 'Vencendo')), \
             patch('certificados.enviar_email') as mock_email:
            res_dedup = self.monitor.verificar(manual=True)
            mock_email.assert_not_called()


if __name__ == '__main__':
    unittest.main()
