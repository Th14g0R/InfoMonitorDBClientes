import unittest
from InfoMonitorDBClientes import app


class CnpjRouteTest(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_cnpj_route_status_and_content(self):
        resp = self.client.get('/cnpj')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Consulta CNPJ Pro', resp.data)
        self.assertIn(b'publica.cnpj.ws', resp.data)
        self.assertIn(b'React', resp.data)

    def test_consulta_cnpj_alias_route(self):
        resp = self.client.get('/consulta-cnpj')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Consulta CNPJ Pro', resp.data)
