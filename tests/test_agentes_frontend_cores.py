import json
import os
import subprocess
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
JS_PATH = os.path.join(ROOT_DIR, 'static', 'agentes.js')
CSS_PATH = os.path.join(ROOT_DIR, 'static', 'agentes.css')
HTML_PATH = os.path.join(ROOT_DIR, 'templates', 'agentes.html')
RUNNER_PATH = os.path.join(ROOT_DIR, 'tests', 'node_runner_agentes.js')

class TestAgentesFrontendCores(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(JS_PATH), "static/agentes.js deve existir")
        self.assertTrue(os.path.exists(CSS_PATH), "static/agentes.css deve existir")
        self.assertTrue(os.path.exists(HTML_PATH), "templates/agentes.html deve existir")
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

    def test_mascara_telefonia_pbx_e_fmtfone(self):
        """Valida máscara de números PBX: remoção de 55 somente em comprimentos 12/13, preservando DDD 55, ramal e internacional."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}

        # 1. Validação em Situação com número 558999180044 (12 dígitos, fixo/móvel com DDI 55)
        c1 = by_id['chamada_pbx_558999180044']
        self.assertEqual(c1['sitTxt'], "(89) 9918-0044 (0m 45s)")
        self.assertIn('<span class="fone">(89) 9918-0044</span>', c1['sitHtml'])
        self.assertNotIn("558999180044", c1['sitTxt'])

        # 2. Validação em Situação com número 5588981669885 (13 dígitos, celular com DDI 55)
        c2 = by_id['chamada_pbx_5588981669885']
        self.assertEqual(c2['sitTxt'], "(88) 98166-9885 (1m 10s)")
        self.assertIn('<span class="fone">(88) 98166-9885</span>', c2['sitHtml'])
        self.assertNotIn("5588981669885", c2['sitTxt'])

        # 3. Validação em Situação com ramal 2008 (4 dígitos)
        c3 = by_id['chamada_pbx_ramal_2008']
        self.assertEqual(c3['sitTxt'], "2008 (0m 15s)")
        self.assertIn('<span class="fone">2008</span>', c3['sitHtml'])

        # 4. Validação em Situação com internacional +14155552671
        c4 = by_id['chamada_pbx_internacional_mais']
        self.assertEqual(c4['sitTxt'], "+14155552671 (2m 00s)")
        self.assertIn('<span class="fone">+14155552671</span>', c4['sitHtml'])

        # 5. Validações unitárias completas da função fmtFone
        f = by_id['fone_tests']
        # Números da imagem (12 e 13 dígitos) com e sem prefixo '+'
        self.assertEqual(f['imgFixo'], "(89) 9918-0044")
        self.assertEqual(f['imgCel'], "(88) 98166-9885")
        self.assertEqual(f['imgFixoMais'], "(89) 9918-0044")
        self.assertEqual(f['imgCelMais'], "(88) 98166-9885")

        # Manutenção de DDD 55 local (comprimentos 10 e 11 não removem 55)
        self.assertEqual(f['ddd55Cel'], "(55) 98166-9885")
        self.assertEqual(f['ddd55Fixo'], "(55) 3222-1234")

        # DDI 55 + DDD 55 (12 e 13 dígitos removem DDI e preservam DDD)
        self.assertEqual(f['ddi55Ddd55Cel'], "(55) 98166-9885")
        self.assertEqual(f['ddi55Ddd55Fixo'], "(55) 3222-1234")

        # Ramais (comprimento curto mantido intacto)
        self.assertEqual(f['ramal4'], "2008")
        self.assertEqual(f['ramal3'], "101")

        # Telefonia internacional com '+' explícito preservado
        self.assertEqual(f['interMaisUs'], "+14155552671")
        self.assertEqual(f['interMaisPt'], "+351912345678")

        # Evita inferir DDI ambíguo sem '+' explícito
        self.assertEqual(f['interSemMais'], "141555526710")

        # Padrão NPX 10 e 11 dígitos, local 8 e 9 dígitos, vazios
        self.assertEqual(f['stdCel'], "(85) 99999-9999")
        self.assertEqual(f['stdFixo'], "(85) 3222-1100")
        self.assertEqual(f['local9'], "99999-8888")
        self.assertEqual(f['local8'], "3333-4444")
        self.assertEqual(f['vazio'], "")
        self.assertEqual(f['nulo'], "")

    def test_matriz_logado_npx_contrato_sessao(self):
        """Valida que logadoNpx cumpre estritamente o contrato booleano npxLoggedIn e casos sem flag (sem inferir MicroSIP)."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}
        m = by_id['logado_npx_matrix']

        # 1. Contrato npxLoggedIn booleano true confirma sessão
        self.assertTrue(m['pbx_loggedin_true'], "PBX com npxLoggedIn: true deve ter sessão confirmada (inclusive se line pbx)")
        self.assertTrue(m['npx_loggedin_true'], "NPX com npxLoggedIn: true deve ter sessão confirmada")
        self.assertTrue(m['off_loggedin_true'], "OFF com npxLoggedIn: true deve ter sessão confirmada")

        # 2. Contrato npxLoggedIn booleano false e string truthy recusam sessão
        self.assertFalse(m['pbx_loggedin_false'], "PBX com npxLoggedIn: false deve ser recusado")
        self.assertFalse(m['npx_loggedin_false'], "NPX com npxLoggedIn: false deve ser recusado")
        self.assertFalse(m['string_truthy'], "String 'true' não deve ser aceita como sessão confirmada")

        # 3. Casos sem flag: nunca inferir sessão pelo MicroSIP
        self.assertFalse(m['sem_flag_pbx_free'], "PBX Free sem flag não deve inferir login NPX")
        self.assertFalse(m['sem_flag_pbx_busy'], "PBX Busy sem flag não deve inferir login NPX")
        self.assertFalse(m['sem_flag_pbx_sem_fila'], "PBX_Sem_Fila sem flag não deve inferir login NPX")
        self.assertFalse(m['sem_flag_pbx_code'], "Código prefixado pbx- sem flag não deve inferir login NPX")
        self.assertFalse(m['sem_flag_off'], "Linha off sem flag não tem sessão")

        # 4. Casos sem flag: seguir dados reais de sessão na NPX
        for estado in ('livre', 'lig', 'cham', 'pausa'):
            with self.subTest(estado=estado):
                self.assertFalse(m[f'sem_flag_npx_{estado}'])
        self.assertFalse(m['sem_flag_npx_deslogado'], "NPX com status=0 e paused=false está deslogado")

    def test_contadores_topo_e_card_separado_pbx(self):
        """Valida que contadores do topo consideram exclusivamente agentes com sessão NPX e que PBX sem fila possui card separado."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}

        # 1. Caso da imagem: 1 analista PBX-only sem sessão na fila NPX
        # Topo deve mostrar 0 logados e 0 livres NPX, 1 só PBX. Card separado PBX_Sem_Fila deve estar visível no grupo.
        img = by_id['topo_cenario_imagem_pbx_sem_fila']
        self.assertEqual(img['nLog'], 0, "Topo deve exibir 0 logados NPX no caso da imagem")
        self.assertEqual(img['nLivre'], 0, "Topo deve exibir 0 livres NPX no caso da imagem")
        self.assertEqual(img['nCham'], 0)
        self.assertEqual(img['nLig'], 0)
        self.assertEqual(img['nPausa'], 0)
        self.assertEqual(img['nPresa'], 0)
        self.assertEqual(img['nPbx'], 1, "Topo deve exibir 1 no chip 'só PBX'")
        self.assertTrue(img['temCardPbx'], "Card separado PBX_Sem_Fila deve estar visível no grupo")

        # 2. PBX com logon NPX confirmado (inclusive se line pbx)
        # Topo deve contabilizar o agente como logado e livre NPX
        pbx_npx = by_id['topo_cenario_pbx_com_npx_logon']
        self.assertEqual(pbx_npx['nLog'], 1, "Topo deve contabilizar 1 logado quando line pbx tem sessão confirmada")
        self.assertEqual(pbx_npx['nLivre'], 1, "Topo deve contabilizar 1 livre quando PBX Free tem sessão confirmada")
        self.assertEqual(pbx_npx['nPbx'], 0, "Não deve constar em 'só PBX' pois está logado na NPX")
        self.assertFalse(pbx_npx['temCardPbx'], "Card PBX_Sem_Fila não deve ser exibido quando não há agentes sem fila")

        # 3. Deduplicação multi-fila: mesmo código em 2 filas NPX conta apenas 1 vez no topo
        dedup = by_id['topo_cenario_deduplicacao_multi_fila']
        self.assertEqual(dedup['nLog'], 1, "Deduplicação por código deve contar 1 agente no topo")
        self.assertEqual(dedup['nLivre'], 1)

        # 4. Caso sem flag: PBX-only não infere login
        sem_flag_pbx = by_id['topo_cenario_sem_flag_pbx_only']
        self.assertEqual(sem_flag_pbx['nLog'], 0)
        self.assertEqual(sem_flag_pbx['nLivre'], 0)
        self.assertEqual(sem_flag_pbx['nPbx'], 1)
        self.assertTrue(sem_flag_pbx['temCardPbx'])

        # 5. Caso sem flag: NPX livre infere sessão
        sem_flag_npx = by_id['topo_cenario_sem_flag_npx_livre']
        self.assertEqual(sem_flag_npx['nLog'], 0)
        self.assertEqual(sem_flag_npx['nLivre'], 0)
        self.assertEqual(sem_flag_npx['nPbx'], 0)

        # 6. Cenário misto: 1 NPX livre, 1 PBX+NPX em ligação, 1 PBX-only sem sessão
        misto = by_id['topo_cenario_misto']
        self.assertEqual(misto['nLog'], 2, "2 agentes logados em fila NPX")
        self.assertEqual(misto['nLivre'], 1, "1 livre na NPX")
        self.assertEqual(misto['nLig'], 1, "1 em ligação PBX com sessão NPX")
        self.assertEqual(misto['nPbx'], 1, "1 agente só PBX")
        self.assertTrue(misto['temCardPbx'], "Card PBX_Sem_Fila visível no grupo para o agente PBX-only")

    def test_alerta_visual_retorne_ramal_npx_pbxidlealert(self):
        """Valida alerta visual acessível na linha do agente (badge/texto 'Retorne ao ramal NPX') condicionado a pbxIdleAlert: true."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}
        alerta = by_id['alerta_pbx_idle_tests']

        # 1. pbxIdleAlert: true deve renderizar a classe 'alerta-pbx' e o texto acessível
        self.assertIn('alerta-pbx', alerta['htmlTrue'], "Linha do agente deve conter a classe alerta-pbx quando pbxIdleAlert=true")
        celulas = alerta['htmlTrue'].split('<td')
        self.assertNotIn('Retorne ao ramal NPX', celulas[1])
        self.assertIn('<span class="alerta-pbx-texto">Retorne ao ramal NPX</span>', celulas[5])

        # 2. pbxIdleAlert: false não deve renderizar o alerta
        self.assertNotIn('alerta-pbx', alerta['htmlFalse'], "Linha não deve conter alerta-pbx quando pbxIdleAlert=false")
        self.assertNotIn('Retorne ao ramal NPX', alerta['htmlFalse'])

        # 3. pbxIdleAlert: undefined (ausente) não deve renderizar o alerta
        self.assertNotIn('alerta-pbx', alerta['htmlUndefined'], "Linha não deve conter alerta-pbx quando flag ausente")
        self.assertNotIn('Retorne ao ramal NPX', alerta['htmlUndefined'])

        # 4. pbxIdleAlert: "true" (string) não deve renderizar o alerta (booleano estrito)
        self.assertNotIn('alerta-pbx', alerta['htmlString'], "Linha não deve aceitar string truthy")
        self.assertNotIn('Retorne ao ramal NPX', alerta['htmlString'])

        # 5. Validação no CSS: regras de destaque acessível e respeito a prefers-reduced-motion
        with open(CSS_PATH, 'r', encoding='utf-8') as f:
            css = f.read()

        self.assertIn('tr.alerta-pbx td', css, "CSS deve conter estilo para tr.alerta-pbx td")
        self.assertIn('.alerta-pbx-texto', css, "CSS deve conter estilo para .alerta-pbx-texto")
        self.assertIn('prefers-reduced-motion: no-preference', css, "Animação de pulso deve ser restrita a no-preference")
        self.assertIn('@keyframes ag-alerta-pbx', css, "CSS deve definir o keyframe ag-alerta-pbx")

    def test_responsividade_mobile_393px_e_rotulo_excedidos(self):
        """Valida que o chip de pausa estourada foi alterado para 'Excedidos' e que o CSS contém as regras responsivas para 393px/mobile."""
        # 1. Rótulo 'Excedidos' no HTML
        with open(HTML_PATH, 'r', encoding='utf-8') as f:
            html = f.read()

        self.assertIn('<b id="nPresa">–</b>Excedidos</div>', html, "Chip deve exibir rótulo 'Excedidos'")
        self.assertNotIn('<b id="nPresa">–</b>pausa estourada</div>', html, "Rótulo antigo 'pausa estourada' deve ser removido")

        # 2. Regras de responsividade no CSS para mobile / 393px
        with open(CSS_PATH, 'r', encoding='utf-8') as f:
            css = f.read()

        # Media query mobile
        self.assertIn('@media (max-width: 660px)', css)

        # Chips simétricos e bem distribuídos (grid 8 colunas com span 2 e grid-auto-rows 1fr)
        self.assertIn('repeat(8, minmax(0, 1fr))', css, "Grid deve definir colunas simétricas para os chips")
        self.assertIn('grid-auto-rows: 1fr', css, "Chips devem ter alturas iguais")
        self.assertIn('grid-column: span 2', css, "Chips devem ter larguras proporcionais iguais")

        # Espaçamento de grupos
        self.assertIn('.fila-h', css)
        self.assertIn('.cont', css)

        # Tabela não-mutilante para situação e ações (ocupando linha inteira em mobile)
        self.assertIn('td:nth-child(5)', css, "Situação deve ter regra específica no mobile")
        self.assertIn('grid-column: 1 / -1', css, "Situação e ações devem ocupar largura total para evitar corte")
        self.assertIn('overflow-wrap: anywhere', css, "Quebra de palavra não-mutilante no mobile")
        self.assertIn('min-height: 32px', css, "Botões de ação devem ter área de toque confortável")

if __name__ == '__main__':
    unittest.main()
