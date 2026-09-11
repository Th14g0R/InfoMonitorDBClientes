import io
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from gestao_bancos import adicionar_banco, listar_pastas


class GestaoBancosTest(unittest.TestCase):
    def setUp(self):
        self.base = '/opt/infobrasil'
        self.pasta = self.base + '/cliente'
        self.entradas = {self.base: stat.S_IFDIR, self.pasta: stat.S_IFDIR}
        self.sftp = MagicMock()
        self.sftp.normalize.side_effect = lambda path: path
        def lstat(path):
            if path not in self.entradas:
                raise FileNotFoundError(path)
            return SimpleNamespace(st_mode=self.entradas[path])
        self.sftp.lstat.side_effect = lstat
        self.sftp.mkdir.side_effect = lambda path, mode: self.entradas.update({path: stat.S_IFDIR})
        self.destino = io.BytesIO()
        self.sftp.open.return_value.__enter__.return_value = self.destino

    def test_envia_para_pasta_existente_sem_recriar(self):
        caminho = adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta,
                                  'filial.fdb', io.BytesIO(b'banco de teste'))
        self.assertEqual(caminho, self.pasta + '/filial.fdb')
        self.assertEqual(self.destino.getvalue(), b'banco de teste')
        self.sftp.mkdir.assert_not_called()
        temporario, modo = self.sftp.open.call_args.args
        self.assertTrue(temporario.startswith(self.pasta + '/.filial.fdb.'))
        self.assertEqual(modo, 'wx')
        self.sftp.rename.assert_called_once_with(temporario, caminho)

    def test_falha_de_transferencia_nao_deixa_banco_parcial_no_destino(self):
        origem = MagicMock()
        origem.read.side_effect = OSError('conexão interrompida')
        with self.assertRaises(OSError):
            adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta, 'dados.fdb', origem)
        temporario = self.sftp.open.call_args.args[0]
        self.sftp.remove.assert_called_once_with(temporario)
        self.sftp.rename.assert_not_called()

    def test_arquivo_criado_durante_upload_nao_e_substituido(self):
        self.sftp.rename.side_effect = OSError('destino já existe')
        with self.assertRaises(OSError):
            adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta, 'dados.fdb', io.BytesIO(b'test'))
        temporario = self.sftp.open.call_args.args[0]
        self.sftp.remove.assert_called_once_with(temporario)

    def test_pasta_do_alias_existente_e_reutilizada(self):
        adicionar_banco(self.sftp, self.base, 'cliente', 'alias', '', 'dados.fdb', io.BytesIO(b'test'))
        self.sftp.mkdir.assert_not_called()

    def test_cria_apenas_pasta_nova(self):
        adicionar_banco(self.sftp, self.base, 'nova', 'alias', '', 'dados.fdb')
        self.sftp.mkdir.assert_called_once_with(self.base + '/nova', mode=0o775)

    def test_nao_sobrescreve_arquivo_existente(self):
        self.entradas[self.pasta + '/dados.fdb'] = stat.S_IFREG
        with self.assertRaises(ValueError):
            adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta,
                            'dados.fdb', io.BytesIO(b'nao gravar'))
        self.sftp.open.assert_not_called()

    def test_pode_cadastrar_alias_para_arquivo_existente_sem_upload(self):
        self.entradas[self.pasta + '/dados.fdb'] = stat.S_IFREG
        self.assertEqual(adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta,
                                         'dados.fdb'), self.pasta + '/dados.fdb')
        self.sftp.open.assert_not_called()

    def test_rejeita_escape_e_nome_de_arquivo_invalido(self):
        for pasta, nome in [('/tmp', 'dados.fdb'), (self.base + '/../etc', 'dados.fdb'),
                            (self.pasta, '../dados.fdb'), (self.pasta, 'dados.fbk')]:
            with self.subTest(pasta=pasta, nome=nome), self.assertRaises(ValueError):
                adicionar_banco(self.sftp, self.base, 'novo', 'existente', pasta, nome)
        self.sftp.open.assert_not_called()

    def test_rejeita_link_e_diretorio_resolvido_fora_da_base(self):
        self.entradas[self.pasta] = stat.S_IFLNK
        with self.assertRaises(ValueError):
            adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta, 'dados.fdb')
        self.entradas[self.pasta] = stat.S_IFDIR
        self.sftp.normalize.side_effect = lambda path: '/tmp' if path == self.pasta else path
        with self.assertRaises(ValueError):
            adicionar_banco(self.sftp, self.base, 'novo', 'existente', self.pasta, 'dados.fdb')

    def test_listagem_exclui_arquivos_e_links(self):
        self.sftp.listdir_attr.return_value = [
            SimpleNamespace(filename='cliente', st_mode=stat.S_IFDIR),
            SimpleNamespace(filename='dados.fdb', st_mode=stat.S_IFREG),
            SimpleNamespace(filename='link', st_mode=stat.S_IFLNK),
        ]
        resultado = listar_pastas(self.sftp, self.base, self.base)
        self.assertIsNone(resultado['pai'])
        self.assertEqual(resultado['pastas'], [dict(nome='cliente', caminho=self.pasta)])
