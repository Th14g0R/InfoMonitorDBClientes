import unittest

from backups import interpretar_status_backups


class BackupsTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
