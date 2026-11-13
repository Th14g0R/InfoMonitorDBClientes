import json
import os
import subprocess
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
JS_PATH = os.path.join(ROOT_DIR, 'static', 'agentes.js')
CSS_PATH = os.path.join(ROOT_DIR, 'static', 'agentes.css')
RUNNER_PATH = os.path.join(ROOT_DIR, 'tests', 'node_runner_agentes.js')

class TestAgentesFrontendCores(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(JS_PATH), "static/agentes.js deve existir")
        self.assertTrue(os.path.exists(CSS_PATH), "static/agentes.css deve existir")
        self.assertTrue(os.path.exists(RUNNER_PATH), "tests/node_runner_agentes.js deve existir")

    def test_node_check_sintaxe_js(self):
        """Valida que static/agentes.js possui sintaxe JavaScript válida via node --check."""
        res = subprocess.run(['node', '--check', JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro de sintaxe em static/agentes.js:\n{res.stderr}")

    def test_css_possui_variaveis_e_classes_pbx(self):
        """Valida que static/agentes.css define os tokens e classes de PBX necessários."""
        with open(CSS_PATH, 'r', encoding='utf-8') as f:
            css = f.read()

        # Tokens esperados
        self.assertIn('--pbx-pausa', css, "CSS deve conter a variável --pbx-pausa")
        self.assertIn('--pbx-livre', css, "CSS deve conter a variável --pbx-livre")
        self.assertIn('--pbx-lig', css, "CSS deve conter a variável --pbx-lig")
        self.assertIn('--pbx-cham', css, "CSS deve conter a variável --pbx-cham")

        # Classe de linha
        self.assertIn('tr.l-pbx-pausa td', css, "CSS deve conter estilo para tr.l-pbx-pausa td")

    def test_matriz_stde_via_node(self):
        """Executa funções reais de agentes.js em Node.js para validar a matriz de precedência."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}

        # Validação 1: PBX Free + Pausa NPX
        r1 = by_id['pbx_free_paused']
        self.assertEqual(r1['st'][0], 'Em pausa', "Rótulo deve ser 'Em pausa'")
        self.assertEqual(r1['st'][1], 'var(--pbx-pausa)', "Cor deve ser 'var(--pbx-pausa)'")
        self.assertEqual(r1['st'][2], 0, "Ordem deve ser 0 (prioridade de pausa)")
        self.assertEqual(r1['st'][3], 'l-pbx-pausa', "Classe da linha deve ser 'l-pbx-pausa'")
        self.assertTrue(r1['ehPausa'], "ehPausa deve ser True")
        self.assertFalse(r1['ehLivre'], "ehLivre deve ser False")
        self.assertNotIn("Livre", r1['sitTxt'], "Situação não deve conter prefixo 'Livre'")
        self.assertIn("⏸", r1['sitTxt'], "Situação deve conter símbolo de pausa")
        self.assertNotIn("Livre", r1['sitHtml'], "Situação HTML não deve conter 'Livre'")

        # Validação 2: PBX Busy + Pausa NPX
        r2 = by_id['pbx_busy_paused']
        self.assertEqual(r2['st'][0], 'Em ligação', "Busy precede pausa: rótulo deve ser 'Em ligação'")
        self.assertEqual(r2['st'][1], 'var(--pbx-lig)', "Cor deve ser 'var(--pbx-lig)'")
        self.assertEqual(r2['st'][3], 'l-pbx-lig', "Classe deve ser 'l-pbx-lig'")
        self.assertTrue(r2['ehLig'], "ehLig deve ser True")
        self.assertFalse(r2['ehPausa'], "Busy precede pausa nos contadores de categoria única")

        # Validação 3: PBX Ringing + Pausa NPX
        r3 = by_id['pbx_ringing_paused']
        self.assertEqual(r3['st'][0], 'Recebendo', "Ringing precede pausa: rótulo deve ser 'Recebendo'")
        self.assertEqual(r3['st'][1], 'var(--pbx-cham)', "Cor deve ser 'var(--pbx-cham)'")
        self.assertEqual(r3['st'][3], 'l-pbx-cham', "Classe deve ser 'l-pbx-cham'")
        self.assertTrue(r3['ehCham'], "ehCham deve ser True")
        self.assertFalse(r3['ehPausa'], "Ringing precede pausa nos contadores de categoria única")

        # Validação 4: PBX Free + Sem Pausa
        r4 = by_id['pbx_free_sem_pausa']
        self.assertEqual(r4['st'][0], 'Livre')
        self.assertEqual(r4['st'][1], 'var(--pbx-livre)')
        self.assertEqual(r4['st'][3], 'l-pbx-livre')
        self.assertTrue(r4['ehLivre'])
        self.assertFalse(r4['ehPausa'])

        # Validação 5: NPX Pausa
        r5 = by_id['npx_pausa']
        self.assertEqual(r5['st'][0], 'Em pausa')
        self.assertEqual(r5['st'][1], 'var(--pausa)')
        self.assertEqual(r5['st'][3], 'l-pausa')
        self.assertTrue(r5['ehPausa'])
        self.assertFalse(r5['ehLivre'])

        # Validação 6: NPX Livre
        r6 = by_id['npx_livre']
        self.assertEqual(r6['st'][0], 'Livre')
        self.assertEqual(r6['st'][1], 'var(--livre)')
        self.assertEqual(r6['st'][3], 'l-livre')
        self.assertTrue(r6['ehLivre'])
        self.assertFalse(r6['ehPausa'])

        # Validação 7: Line off intacto
        r7 = by_id['line_off']
        self.assertEqual(r7['st'][0], 'Offline')
        self.assertEqual(r7['st'][1], 'var(--off)')
        self.assertEqual(r7['st'][3], 'l-off')

if __name__ == '__main__':
    unittest.main()
