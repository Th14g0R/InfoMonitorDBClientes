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

    def test_formatacao_duracao_decorrida_situacao_pausa(self):
        """Valida remoção de 'de Xm', exibição sem pauseMaxMinutes, fallback de início e formato >= 24h (Nd HHh MMm)."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}

        # 1. Caso 101m: exibe '1h 41m' sem 'de 60m'
        p101 = by_id['pause_101m']
        self.assertEqual(p101['sitTxt'], "⏸ Pausa · 1h 41m")
        self.assertEqual(p101['sitHtml'], "⏸ Pausa <span class=\"mut\">· 1h 41m</span>")
        self.assertNotIn("de 60m", p101['sitTxt'])

        # 2. Fronteira exata de 1 hora: 60m -> 1h 00m
        p60 = by_id['pause_60m']
        self.assertEqual(p60['sitTxt'], "⏸ Pausa · 1h 00m")
        self.assertEqual(p60['sitHtml'], "⏸ Pausa <span class=\"mut\">· 1h 00m</span>")

        # 3. Fronteira 61m -> 1h 01m
        p61 = by_id['pause_61m']
        self.assertEqual(p61['sitTxt'], "⏸ Pausa · 1h 01m")

        # 4. Fronteira de 2 horas: 120m -> 2h 00m
        p120 = by_id['pause_120m']
        self.assertEqual(p120['sitTxt'], "⏸ Pausa · 2h 00m")

        # 5. Formato curto abaixo de 1 hora preservado: 59m -> 59m
        p59 = by_id['pause_59m']
        self.assertEqual(p59['sitTxt'], "⏸ Pausa · 59m")
        self.assertEqual(p59['sitHtml'], "⏸ Pausa <span class=\"mut\">· 59m</span>")

        # 6. Formato curto: 12m -> 12m
        p12 = by_id['pause_12m']
        self.assertEqual(p12['sitTxt'], "⏸ Pausa · 12m")

        # 7. Formato curto no limite inferior: 0m -> 0m
        p0 = by_id['pause_0m']
        self.assertEqual(p0['sitTxt'], "⏸ Pausa · 0m")

        # 8. Linha NPX (fora PBX) com > 1h
        npx101 = by_id['npx_pause_101m']
        self.assertEqual(npx101['sitTxt'], "Almoço · 1h 41m")
        self.assertEqual(npx101['sitHtml'], "Almoço <span class=\"mut\">· 1h 41m</span>")

        # 9. Linha NPX (fora PBX) com < 1h
        npx59 = by_id['npx_pause_59m']
        self.assertEqual(npx59['sitTxt'], "Lanche · 59m")
        self.assertEqual(npx59['sitHtml'], "Lanche <span class=\"mut\">· 59m</span>")

        # 10. Caso existente a1 (10m sem de 30m)
        r1 = by_id['pbx_free_paused']
        self.assertEqual(r1['sitTxt'], "⏸ Almoço · 10m")
        self.assertEqual(r1['sitHtml'], "⏸ Almoço <span class=\"mut\">· 10m</span>")

        # 11. Durações a partir de 24h: formato Nd HHh MMm
        p24h = by_id['pause_24h']
        self.assertEqual(p24h['sitTxt'], "⏸ Pausa · 1d 00h 00m")
        self.assertEqual(p24h['sitHtml'], "⏸ Pausa <span class=\"mut\">· 1d 00h 00m</span>")

        p25h5m = by_id['pause_25h_5m']
        self.assertEqual(p25h5m['sitTxt'], "⏸ Pausa · 1d 01h 05m")

        p48h12m = by_id['pause_48h_12m']
        self.assertEqual(p48h12m['sitTxt'], "⏸ Pausa · 2d 00h 12m")

        npx24h = by_id['npx_pause_24h']
        self.assertEqual(npx24h['sitTxt'], "Almoço · 1d 00h 00m")
        self.assertEqual(npx24h['sitHtml'], "Almoço <span class=\"mut\">· 1d 00h 00m</span>")

        # 12. Duração válida exibida mesmo SEM pauseMaxMinutes
        pSemMax = by_id['pause_sem_max_minutos']
        self.assertEqual(pSemMax['sitTxt'], "⏸ Pausa · 1h 41m")
        self.assertEqual(pSemMax['sitHtml'], "⏸ Pausa <span class=\"mut\">· 1h 41m</span>")

        pSemMax24h = by_id['pause_sem_max_minutos_24h']
        self.assertEqual(pSemMax24h['sitTxt'], "⏸ Pausa · 1d 00h 00m")

        # 13. Fallback de início preservado quando pauseMinutes ausente/nulo
        pFallback = by_id['pause_fallback_start']
        self.assertEqual(pFallback['sitTxt'], "⏸ Pausa desde 14:30")
        self.assertEqual(pFallback['sitHtml'], "⏸ Pausa <span class=\"mut\">desde 14:30</span>")

        npxFallback = by_id['npx_pause_fallback_start']
        self.assertEqual(npxFallback['sitTxt'], "Lanche desde 14:30")
        self.assertEqual(npxFallback['sitHtml'], "Lanche <span class=\"mut\">desde 14:30</span>")

        # 14. Validações diretas das funções fmtMin e fmtSeg (Tempo e modal)
        fmt = by_id['fmt_tests']
        self.assertEqual(fmt['fmt101'], "1h 41m")
        self.assertEqual(fmt['fmt60'], "1h 00m")
        self.assertEqual(fmt['fmt61'], "1h 01m")
        self.assertEqual(fmt['fmt120'], "2h 00m")
        self.assertEqual(fmt['fmt59'], "59m")
        self.assertEqual(fmt['fmt12'], "12m")
        self.assertEqual(fmt['fmt0'], "0m")
        self.assertEqual(fmt['fmtNull'], "0m")
        self.assertEqual(fmt['fmtUndefined'], "0m")
        self.assertEqual(fmt['fmtMin24h'], "1d 00h 00m")
        self.assertEqual(fmt['fmtMin25h5m'], "1d 01h 05m")

        # Helper fmtSeg (Tempo e modal): >= 24h usa Nd HHh MMm
        self.assertEqual(fmt['fmtSeg24h'], "1d 00h 00m")
        self.assertEqual(fmt['fmtSeg25h1m2s'], "1d 01h 01m")
        self.assertEqual(fmt['fmtSeg2d15h45m'], "2d 15h 45m")
        self.assertEqual(fmt['fmtSeg1h41m'], "1h 41m")
        self.assertEqual(fmt['fmtSeg1h0m'], "1h 00m")
        self.assertEqual(fmt['fmtSeg59s'], "0m 59s")
        self.assertEqual(fmt['fmtSeg0s'], "0m 00s")

if __name__ == '__main__':
    unittest.main()
