import sqlite3
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

    def test_extrair_ips_netstat_ipv4_e_ipv6(self):
        saida_netstat = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    0.0.0.0:16032          0.0.0.0:0              LISTENING       4321
  TCP    192.168.243.2:16032    189.85.119.175:51234   ESTABLISHED     4321
  TCP    192.168.243.2:16032    177.136.250.10:49812   ESTABLISHED     4321
  TCP    192.168.243.2:16032    127.0.0.1:55000        ESTABLISHED     4321
  TCP    192.168.243.2:16032    201.20.10.5:54321      TIME_WAIT       0
  TCP    192.168.243.2:8888     192.168.243.50:50123   ESTABLISHED     9999
  TCP    [::]:16032             [2804:14d:5c::1]:60100 ESTABLISHED     4321
"""
        ips = cobian.extrair_ips_netstat(saida_netstat, porta=16032)
        # Deve extrair apenas os IPs remotos em ESTABLISHED na porta 16032, ignorando loopback e outras portas
        self.assertIn('189.85.119.175', ips)
        self.assertIn('177.136.250.10', ips)
        self.assertIn('2804:14d:5c::1', ips)
        self.assertNotIn('127.0.0.1', ips)
        self.assertNotIn('201.20.10.5', ips)  # TIME_WAIT
        self.assertNotIn('192.168.243.50', ips)  # Porta 8888, não 16032
        self.assertEqual(len(ips), 3)

    def test_cache_ttl_conexoes(self):
        with patch('cobian.listar_ips_conectados_cobian') as mock_listar:
            mock_listar.return_value = {'189.85.119.175'}

            # Primeira chamada: executa listar_ips_conectados_cobian
            ips1 = cobian.obter_ips_conectados_com_cache(forcar=False)
            self.assertEqual(ips1, {'189.85.119.175'})
            self.assertEqual(mock_listar.call_count, 1)

            # Segunda chamada dentro do TTL: deve retornar do cache sem chamar novamente
            ips2 = cobian.obter_ips_conectados_com_cache(forcar=False)
            self.assertEqual(ips2, {'189.85.119.175'})
            self.assertEqual(mock_listar.call_count, 1)

            # Chamada forçada: ignora cache e busca novamente
            mock_listar.return_value = {'189.85.119.175', '177.136.250.10'}
            ips3 = cobian.obter_ips_conectados_com_cache(forcar=True)
            self.assertEqual(ips3, {'189.85.119.175', '177.136.250.10'})
            self.assertEqual(mock_listar.call_count, 2)

    def test_salvar_e_obter_vinculo_cliente_ip(self):
        # Inserção válida
        cobian.salvar_vinculo_cliente_ip(self.conn, 'loja-central', '189.85.119.175', origem='manual')
        cobian.salvar_vinculo_cliente_ip(self.conn, 'loja-filial', '177.136.250.10', origem='manual')

        mapeamento = cobian.obter_mapeamento_clientes_ips(self.conn)
        self.assertIn('loja-central', mapeamento)
        self.assertEqual(mapeamento['loja-central']['ip'], '189.85.119.175')
        self.assertEqual(mapeamento['loja-filial']['ip'], '177.136.250.10')

        # Atualização de IP existente
        cobian.salvar_vinculo_cliente_ip(self.conn, 'loja-central', '189.85.119.200')
        mapeamento_atualizado = cobian.obter_mapeamento_clientes_ips(self.conn)
        self.assertEqual(mapeamento_atualizado['loja-central']['ip'], '189.85.119.200')

        # Remoção de vínculo
        cobian.remover_vinculo_cliente_ip(self.conn, 'loja-central')
        mapeamento_pos_remocao = cobian.obter_mapeamento_clientes_ips(self.conn)
        self.assertNotIn('loja-central', mapeamento_pos_remocao)

    def test_validacao_ip_invalido(self):
        with self.assertRaises(ValueError):
            cobian.salvar_vinculo_cliente_ip(self.conn, 'loja-teste', 'ip_invalido_123')

        with self.assertRaises(ValueError):
            cobian.salvar_vinculo_cliente_ip(self.conn, 'loja-teste', '999.999.999.999')

        with self.assertRaises(ValueError):
            cobian.salvar_vinculo_cliente_ip(self.conn, '', '192.168.1.1')

    def test_obter_status_conexoes_cobian_enriquecimento(self):
        cobian.salvar_vinculo_cliente_ip(self.conn, 'cliente-conectado', '189.85.119.175')
        cobian.salvar_vinculo_cliente_ip(self.conn, 'cliente-desconectado', '177.136.250.10')

        clientes = [
            {'alias': 'cliente-conectado', 'status': 'OK'},
            {'alias': 'cliente-desconectado', 'status': 'ATRASADO'},
            {'alias': 'cliente-sem-ip', 'status': 'OK'},
        ]

        # Simular que apenas 189.85.119.175 e um IP desconhecido 200.200.200.200 estão ativos
        with patch('cobian.obter_ips_conectados_com_cache') as mock_ips:
            mock_ips.return_value = {'189.85.119.175', '200.200.200.200'}

            res = cobian.obter_status_conexoes_cobian(clientes, conn=self.conn)

            c1 = next(c for c in clientes if c['alias'] == 'cliente-conectado')
            self.assertTrue(c1['online'])
            self.assertEqual(c1['ip'], '189.85.119.175')

            c2 = next(c for c in clientes if c['alias'] == 'cliente-desconectado')
            self.assertFalse(c2['online'])
            self.assertEqual(c2['ip'], '177.136.250.10')

            c3 = next(c for c in clientes if c['alias'] == 'cliente-sem-ip')
            self.assertFalse(c3['online'])
            self.assertIsNone(c3['ip'])

            self.assertEqual(res['total_online'], 1)
            self.assertEqual(res['total_offline'], 2)
            self.assertEqual(res['total_com_ip'], 2)
            self.assertEqual(res['ips_sem_vinculo'], ['200.200.200.200'])


if __name__ == '__main__':
    unittest.main()
