from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch, MagicMock

from flask import Flask
from certificados import MonitorCertificados, agora, validar_dominio, configurar_certificados


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
        vencimento = (agora() + timedelta(days=20)).isoformat()
        with patch('certificados.consultar_certificado', return_value=(vencimento, 'Vencendo')), patch('certificados.enviar_email') as email:
            self.monitor.verificar(manual=True)
            self.assertEqual(len(email.call_args.args[0]), 8)
            self.liberar()
            self.monitor.verificar(manual=True)
            email.assert_called_once()
            with closing(self.conectar()) as conn, conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM certificados_emails').fetchone()[0], 1)
            self.liberar()
            novo = (agora() + timedelta(days=25)).isoformat()
            with patch('certificados.consultar_certificado', return_value=(novo, 'Vencendo')):
                self.monitor.verificar(manual=True)
            self.assertEqual(email.call_count, 2)

    def test_falha_email_tenta_novamente(self):
        vencimento = (agora() - timedelta(days=2)).isoformat()
        with patch('certificados.consultar_certificado', return_value=(vencimento, 'Expirado')), patch('certificados.enviar_email', side_effect=RuntimeError('teste')):
            self.monitor.verificar()
        with closing(self.conectar()) as conn, conn:
            self.assertIsNone(conn.execute('SELECT mes FROM certificados_rotina').fetchone()[0])
            self.assertIsNone(conn.execute('SELECT email_em FROM certificados LIMIT 1').fetchone()[0])
        self.liberar()
        with patch('certificados.consultar_certificado', return_value=(vencimento, 'Expirado')), patch('certificados.enviar_email') as email:
            self.monitor.verificar()
            email.assert_called_once()

    def test_rotina_mensal_e_falha_de_consulta(self):
        with patch('certificados.consultar_certificado', side_effect=OSError('DNS indisponível')):
            self.monitor.verificar()
        with closing(self.conectar()) as conn:
            self.assertIsNone(conn.execute('SELECT mes FROM certificados_rotina').fetchone()[0])
        self.liberar()
        with patch('certificados.consultar_certificado', return_value=((agora() + timedelta(days=90)).isoformat(), 'Válido')) as consultar:
            self.monitor.verificar()
            self.liberar()
            self.monitor.verificar()
            self.assertEqual(consultar.call_count, 8)

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


if __name__ == '__main__':
    unittest.main()
