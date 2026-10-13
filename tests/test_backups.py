import datetime
import unittest
from unittest.mock import MagicMock, patch

from backups import (
    analisar_status_para_alerta,
    enviar_email_alerta_backups,
    extrair_dias_atraso,
    gerar_relatorio_email_backups,
    interpretar_status_backups,
)


class BackupsTest(unittest.TestCase):
    def test_formato_real_do_servidor_inclui_erros_como_atrasados(self):
        resultado = interpretar_status_backups('''
O cliente casabonita-1887 possui o arquivo criado a menos de 1 dia - status [OK] 2026-09-13 22:04:48.
O cliente ceramicajscunha-1744 esta a mais de 1 dia sem bkp - status ERRO 2026-09-11 16:43:18.
O cliente lanehomecenter-1526 está a mais de 1 dia sem bkp - status ERRO 2026-09-12 12:10:32.
Total de clientes OK --> [ 1 ]
Total de clientes ERRO --> [ 2 ]
''')
        self.assertEqual(len(resultado['clientes']), 3)
        self.assertEqual(
            (resultado['total_ok'], resultado['total_atrasado'], resultado['total_erro']),
            (1, 2, 0))
        self.assertEqual(resultado['clientes'][1], {
            'alias': 'ceramicajscunha-1744', 'status': 'ATRASADO',
            'idade': 'a mais de 1 dia', 'timestamp': '2026-09-11 16:43:18',
            'caminho': None})

    def test_atrasado_com_erro_e_formatacao_html(self):
        resultado = interpretar_status_backups(
            '<pre><font color="red">O cliente loja.atrasada possui o arquivo criado a '
            'mais de 1 dia - status [ERRO] 2026-09-08 22:00:00.</font></pre>'
            'Total de clientes OK --> [ 0 ]\nTotal de clientes ERRO --> [ 1 ]')
        self.assertEqual(resultado['total_atrasado'], 1)
        self.assertEqual(resultado['total_erro'], 0)
        cliente = resultado['clientes'][0]
        self.assertEqual(cliente['alias'], 'loja.atrasada')
        self.assertEqual(cliente['timestamp'], '2026-09-08 22:00:00')
        self.assertEqual(cliente['status'], 'ATRASADO')

    def test_totais_correspondem_aos_cards(self):
        resultado = interpretar_status_backups('''
O cliente recente possui o arquivo criado a menos de 1 dia - status [OK] hoje.
O cliente antigo possui o arquivo criado a 2 dias - status [OK] ontem.
O cliente atrasado possui o arquivo criado a 1 dia - status [ERRO] ontem.
O cliente falhou possui o arquivo criado a menos de 1 dia - status [ERRO] hoje.
O caminho /backups/ausente NÃO possui arquivo bkp compactado.
Total de clientes OK --> [ 2 ]
Total de clientes ERRO --> [ 3 ]
''')
        self.assertEqual((resultado['total_ok'], resultado['total_atrasado'], resultado['total_erro']), (1, 2, 2))
        self.assertEqual(len(resultado['clientes']), 5)

    def test_br_entidades_e_status_explicito(self):
        resultado = interpretar_status_backups(
            'O cliente um possui o arquivo criado há 36 horas - status [ATRASADO] ontem.<br>'
            'O cliente dois possui o arquivo criado a menos de 1 dia - status [OK] hoje.<br>'
            'O caminho /backups/tres N&Atilde;O possui arquivo bkp compactado.<br>'
            'Horario de ultima atualização dos arquivos<br>2026-09-11 12:00:00')
        self.assertEqual(len(resultado['clientes']), 3)
        self.assertEqual(resultado['total_atrasado'], 1)
        self.assertEqual(resultado['ultima_atualizacao'], '2026-09-11 12:00:00')

    def test_idade_desconhecida_nao_vira_atraso(self):
        resultado = interpretar_status_backups(
            'O cliente falhou possui o arquivo criado a idade desconhecida - status [ERRO] .')
        self.assertEqual(resultado['total_erro'], 1)
        self.assertEqual(resultado['total_atrasado'], 0)

    def test_extrair_dias_atraso_formatos_diversos(self):
        ref = datetime.date(2026, 10, 13)
        c_ok = {'alias': 'c1', 'status': 'OK', 'idade': 'menos de 1 dia', 'timestamp': '2026-10-12 22:00:00'}
        c_ontem = {'alias': 'c2', 'status': 'ATRASADO', 'idade': '1 dia', 'timestamp': 'ontem'}
        c_data_3 = {'alias': 'c3', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-10 12:00:00'}
        c_data_4 = {'alias': 'c4', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-09 16:43:16'}
        c_sem_arq = {'alias': 'c5', 'status': 'SEM_ARQUIVO', 'idade': None, 'timestamp': None}

        self.assertEqual(extrair_dias_atraso(c_ok, ref), 0)
        self.assertEqual(extrair_dias_atraso(c_ontem, ref), 1)
        self.assertEqual(extrair_dias_atraso(c_data_3, ref), 3)
        self.assertEqual(extrair_dias_atraso(c_data_4, ref), 4)
        self.assertEqual(extrair_dias_atraso(c_sem_arq, ref), 999)

    def test_analisar_status_para_alerta_ordenacao_e_gatilho(self):
        ref = datetime.date(2026, 10, 13)
        clientes = [
            {'alias': 'zebra-ok', 'status': 'OK', 'idade': 'menos de 1 dia', 'timestamp': '2026-10-12 20:00:00'},
            {'alias': 'alfa-ok', 'status': 'OK', 'idade': 'menos de 1 dia', 'timestamp': '2026-10-12 21:00:00'},
            {'alias': 'beta-1dia', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-12 05:00:00'},
            {'alias': 'gama-4dias', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-09 10:00:00'},
            {'alias': 'delta-3dias', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-10 11:00:00'},
        ]
        analise = analisar_status_para_alerta(clientes, data_referencia=ref, dias_corte=3)
        self.assertTrue(analise['tem_alerta'])
        self.assertEqual(analise['total_critico'], 2)  # gama-4dias e delta-3dias
        self.assertEqual(analise['total_ok'], 2)
        self.assertEqual(analise['total_atrasado'], 3)

        # Verifica ordenação de todos_ordenados: atrasados primeiro por dias desc (gama, delta, beta), depois OKs alfabéticos (alfa, zebra)
        aliases_ordenados = [c['alias'] for c in analise['todos_ordenados']]
        self.assertEqual(aliases_ordenados, ['gama-4dias', 'delta-3dias', 'beta-1dia', 'alfa-ok', 'zebra-ok'])

        # Verifica que os mais atrasados (>= 3 dias) estão marcados como críticos
        criticos = [c['alias'] for c in analise['clientes_criticos']]
        self.assertEqual(criticos, ['gama-4dias', 'delta-3dias'])

    def test_analisar_status_sem_alerta_se_menor_que_3_dias(self):
        ref = datetime.date(2026, 10, 13)
        clientes = [
            {'alias': 'cliente-ok', 'status': 'OK', 'idade': 'menos de 1 dia', 'timestamp': '2026-10-12 20:00:00'},
            {'alias': 'cliente-2dias', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-11 10:00:00'},
        ]
        analise = analisar_status_para_alerta(clientes, data_referencia=ref, dias_corte=3)
        self.assertFalse(analise['tem_alerta'])
        self.assertEqual(analise['total_critico'], 0)

    def test_gerar_relatorio_email_destaca_mais_atrasados(self):
        ref = datetime.date(2026, 10, 13)
        clientes = [
            {'alias': 'loja-ok', 'status': 'OK', 'idade': 'menos de 1 dia', 'timestamp': '2026-10-12 20:00:00'},
            {'alias': 'loja-critica', 'status': 'ATRASADO', 'idade': 'a mais de 1 dia', 'timestamp': '2026-10-09 10:00:00'},
        ]
        analise = analisar_status_para_alerta(clientes, data_referencia=ref)
        corpo_html, corpo_texto = gerar_relatorio_email_backups(analise, ultima_atualizacao='2026-10-13 07:00:00', data_envio='13/10/2026 09:00')

        self.assertIn('Alerta de Backup em Atraso', corpo_html)
        self.assertIn('loja-ok', corpo_html)
        self.assertIn('loja-critica', corpo_html)
        # O cliente crítico com 4 dias deve ter grifo/destaque em amarelo com badge crítico
        self.assertIn('CRÍTICO', corpo_html)
        self.assertIn('background-color: #fef3c7', corpo_html)
        self.assertIn('GRIFADO', corpo_texto)

        # 1. Coluna IDADE INFORMADA removida
        self.assertNotIn('Idade Informada', corpo_html)
        self.assertNotIn('IDADE INFORMADA', corpo_html)

        # 2. Mesmos títulos nas duas tabelas
        self.assertEqual(corpo_html.count('>Cliente (Alias)<'), 2)
        self.assertEqual(corpo_html.count('>Situação<'), 2)
        self.assertEqual(corpo_html.count('>Dias de Atraso<'), 2)
        self.assertEqual(corpo_html.count('>Último Backup<'), 2)

        # 3. Clientes atrasados antes dos em dia no HTML e no texto
        pos_atrasados_html = corpo_html.find('Clientes com Backup em Atraso')
        pos_ok_html = corpo_html.find('Clientes com Backup em Dia')
        self.assertTrue(pos_atrasados_html < pos_ok_html)

        pos_atrasados_txt = corpo_texto.find('1. CLIENTES COM BACKUP EM ATRASO')
        pos_ok_txt = corpo_texto.find('2. CLIENTES COM BACKUP EM DIA')
        self.assertTrue(pos_atrasados_txt < pos_ok_txt)

        # 4. Sem ícone de alerta no alias dentro de mark para evitar quebra de linha
        self.assertNotIn('⚠️ loja-critica', corpo_html)
        self.assertIn('>loja-critica</mark>', corpo_html)

    def test_enviar_email_alerta_backups_chama_smtp_com_destinatario_correto(self):
        with patch('backups.smtplib.SMTP') as mock_smtp:
            servidor = MagicMock()
            mock_smtp.return_value = servidor

            sucesso, msg = enviar_email_alerta_backups(
                destinatario='suporte@infobrasilsistemas.com.br',
                assunto='Alerta de backup em atraso',
                corpo_html='<p>Teste</p>',
                corpo_texto='Teste',
                servidor_smtp='smtp.teste.com',
                porta_smtp=587,
                usuario_smtp='suporte@infobrasilsistemas.com.br',
                senha_smtp='senha-teste'
            )
            self.assertTrue(sucesso)
            servidor.login.assert_called_once_with('suporte@infobrasilsistemas.com.br', 'senha-teste')
            servidor.sendmail.assert_called_once()
            args = servidor.sendmail.call_args[0]
            self.assertEqual(args[0], 'suporte@infobrasilsistemas.com.br')
            self.assertEqual(args[1], ['suporte@infobrasilsistemas.com.br'])
            self.assertIn('Subject: Alerta de backup em atraso', args[2])


if __name__ == '__main__':
    unittest.main()

