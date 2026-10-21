import unittest
from unittest import mock

import infobrasil_api


class UrlApiTest(unittest.TestCase):
    def test_rejeita_esquema_diferente_de_http(self):
        for url in ('file:///etc/passwd', 'ftp://host/x', 'gopher://x', 'javascript:alert(1)'):
            with mock.patch('urllib.request.urlopen') as urlopen:
                token, info, erro = infobrasil_api.autenticar_api(url=url, user='u', password='p')
            self.assertIsNone(token)
            self.assertIn('inválida', erro)
            urlopen.assert_not_called()

    def test_testar_conexao_nao_chama_rede_com_esquema_invalido(self):
        with mock.patch('urllib.request.urlopen') as urlopen:
            r = infobrasil_api.testar_conexao_api(url='file:///etc/passwd', user='u', password='p')
        self.assertFalse(r['sucesso'])
        urlopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
