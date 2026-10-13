import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import cobian


class CobianConexoesTest(unittest.TestCase):
    def setUp(self):
        cobian.limpar_cache_conexoes_cobian()
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        cobian.init_db_cobian(self.conn)

    def tearDown(self):
        self.conn.close()
        cobian.limpar_cache_conexoes_cobian()

    def test_corresponder_cliente_exato_e_normalizado(self):
        remotos = [
            {'cliente': 'casabonita-1887', 'ip': '189.85.119.175', 'tempo_conexao': '01:23:45', 'id_cliente': '1887'},
            {'cliente': 'COSTELA-2259', 'ip': '177.136.250.10', 'tempo_conexao': '00:45:10', 'id_cliente': '2259'},
            {'cliente': 'lojaspadrecicero', 'ip': '187.19.20.21', 'tempo_conexao': '02:00:00', 'id_cliente': '233'},
        ]

        # 1. Correspondência exata
        match1 = cobian.corresponder_cliente('casabonita-1887', remotos)
        self.assertIsNotNone(match1)
        self.assertEqual(match1['ip'], '189.85.119.175')

        # 2. Case-insensitive / maiúsculas
        match2 = cobian.corresponder_cliente('costela-2259', remotos)
        self.assertIsNotNone(match2)
        self.assertEqual(match2['ip'], '177.136.250.10')

        # 3. Correspondência por ID / sufixo
        match3 = cobian.corresponder_cliente('lojaspadrecicero-233', remotos)
        self.assertIsNotNone(match3)
        self.assertEqual(match3['ip'], '187.19.20.21')

        # 4. Cliente inexistente
        match4 = cobian.corresponder_cliente('cliente-inexistente-999', remotos)
        self.assertIsNone(match4)

    def test_cache_ttl_conexoes_remotas(self):
        dados_mock = ([{'cliente': 'loja-1', 'ip': '189.85.119.175'}], {'189.85.119.175'}, True)

        with patch('cobian.extrair_clientes_janela_cobian', return_value=[{'cliente': 'loja-1', 'ip': '189.85.119.175'}]) as mock_ext:
            with patch('cobian.listar_ips_conectados_cobian', return_value={'189.85.119.175'}):
                # Primeira chamada
                rem1, ips1, jan1 = cobian.obter_clientes_remotos_cobian(forcar=False)
                self.assertEqual(len(rem1), 1)
                self.assertEqual(mock_ext.call_count, 1)

                # Segunda chamada dentro do TTL (não chama extrair_clientes_janela_cobian novamente)
                rem2, ips2, jan2 = cobian.obter_clientes_remotos_cobian(forcar=False)
                self.assertEqual(len(rem2), 1)
                self.assertEqual(mock_ext.call_count, 1)

                # Chamada com forcar=True (invalida cache e recarrega)
                rem3, ips3, jan3 = cobian.obter_clientes_remotos_cobian(forcar=True)
                self.assertEqual(len(rem3), 1)
                self.assertEqual(mock_ext.call_count, 2)

    def test_extrair_clientes_janela_cobian_sem_janela_retorna_vazio(self):
        # Quando a janela do cbRemoteManager não estiver aberta, a busca retorna [] em milissegundos
        t0 = time.time()
        res = cobian.extrair_clientes_janela_cobian(timeout_busca=0.1)
        t1 = time.time()
        self.assertIsInstance(res, list)
        self.assertLess(t1 - t0, 1.0)

    def test_extrair_conexoes_logs_cobian(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, 'log 2026-10-13.txt')
            # Escrever arquivo de log em formato UTF-16LE como o Cobian 11 produz
            conteudo = (
                "2026-10-13 09:15:00 Bem-vindo ao Cobian Backup 11 Gravity!\n"
                "2026-10-13 09:20:00 Cliente casabonita-1887 conectado de 189.85.119.175\n"
                "2026-10-13 09:22:00 Cliente costela-2259 conectado de 177.136.250.10\n"
            )
            with open(log_path, 'w', encoding='utf-16-le') as f:
                f.write(conteudo)

            with patch('cobian.CAMINHOS_LOGS_COBIAN', [tmpdir]):
                conexoes = cobian.extrair_conexoes_logs_cobian()
                self.assertGreaterEqual(len(conexoes), 2)
                ips = [c['ip'] for c in conexoes]
                self.assertIn('189.85.119.175', ips)
                self.assertIn('177.136.250.10', ips)

    def test_obter_status_conexoes_cobian_enriquecimento_e_historico(self):
        # Pré-gravar um histórico no SQLite para um cliente desconectado
        self.conn.execute('''
            INSERT INTO cobian_clientes_ips (alias, ip, tempo_conexao, ultimo_visto, origem)
            VALUES ('cliente-desconectado', '177.136.250.10', '00:15:00', '2026-10-12 18:00:00', 'cbRemoteManager')
        ''')
        self.conn.commit()

        clientes = [
            {'alias': 'casabonita-1887', 'status': 'OK'},
            {'alias': 'cliente-desconectado', 'status': 'ATRASADO'},
            {'alias': 'cliente-nunca-visto', 'status': 'OK'},
        ]

        remotos_mock = [
            {'cliente': 'casabonita-1887', 'ip': '189.85.119.175', 'tempo_conexao': '01:23:45', 'id_cliente': '1887'}
        ]

        with patch('cobian.obter_clientes_remotos_cobian', return_value=(remotos_mock, {'189.85.119.175'}, True)):
            res = cobian.obter_status_conexoes_cobian(clientes, conn=self.conn)

            # Cliente ativo na janela
            c1 = next(c for c in clientes if c['alias'] == 'casabonita-1887')
            self.assertTrue(c1['online'])
            self.assertEqual(c1['ip'], '189.85.119.175')
            self.assertEqual(c1['tempo_conexao'], '01:23:45')

            # Cliente desconectado, mas presente no histórico
            c2 = next(c for c in clientes if c['alias'] == 'cliente-desconectado')
            self.assertFalse(c2['online'])
            self.assertEqual(c2['ip'], '177.136.250.10')

            # Cliente nunca visto
            c3 = next(c for c in clientes if c['alias'] == 'cliente-nunca-visto')
            self.assertFalse(c3['online'])
            self.assertIsNone(c3['ip'])

            self.assertEqual(res['total_online'], 1)
            self.assertEqual(res['total_offline'], 2)
            self.assertEqual(res['total_com_ip'], 2)

            # Verificar que casabonita-1887 foi salvo automaticamente no histórico SQLite
            row = self.conn.execute("SELECT ip FROM cobian_clientes_ips WHERE alias = 'casabonita-1887'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['ip'], '189.85.119.175')


if __name__ == '__main__':
    unittest.main()
