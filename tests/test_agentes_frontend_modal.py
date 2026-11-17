import json
import os
import subprocess
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
JS_PATH = os.path.join(ROOT_DIR, 'static', 'agentes.js')
RUNNER_PATH = os.path.join(ROOT_DIR, 'tests', 'node_runner_agentes_modal.js')


class TestAgentesFrontendModal(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.exists(JS_PATH), "static/agentes.js deve existir")
        self.assertTrue(os.path.exists(RUNNER_PATH), "tests/node_runner_agentes_modal.js deve existir")

    def test_sintaxe_js_valida(self):
        """Valida que static/agentes.js possui sintaxe JavaScript estritamente válida."""
        res = subprocess.run(['node', '--check', JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro de sintaxe em static/agentes.js:\n{res.stderr}")

    def test_modal_elegibilidade_acoes_npx_com_pbx(self):
        """Valida elegibilidade dos botões do modal sob a matriz completa de sessões e linhas."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}

        # 1. Caso do Screenshot: PBX Free + voz NPX 0 + npxLoggedIn: true (não pausado)
        c1 = by_id['pbx_free_voz0_loggedin_true']
        self.assertFalse(c1['bTransf']['disabled'], "bTransf deve estar habilitado")
        self.assertFalse(c1['bPen']['disabled'], "bPen deve estar habilitado")
        self.assertFalse(c1['bRemoverFila']['disabled'], "bRemoverFila deve estar habilitado")
        self.assertFalse(c1['bDeslogar']['disabled'], "bDeslogar deve estar habilitado")
        self.assertFalse(c1['bPausar']['disabled'], "bPausar deve estar habilitado")
        self.assertEqual(c1['bPausar']['display'], "", "bPausar deve estar visível")
        self.assertEqual(c1['bTirar']['display'], "none", "bTirar deve estar oculto quando não pausado")
        self.assertIn("Telefonia ativa via PBX com sessão confirmada na NPX", c1['dlgMsg'])
        self.assertIn("sessão NPX ativa / fora de pausa", c1['dPausa'])

        # 2. PBX Busy + npxLoggedIn: true
        c2 = by_id['pbx_busy_loggedin_true']
        self.assertFalse(c2['bTransf']['disabled'])
        self.assertFalse(c2['bPen']['disabled'])
        self.assertFalse(c2['bRemoverFila']['disabled'])
        self.assertFalse(c2['bDeslogar']['disabled'])
        self.assertFalse(c2['bPausar']['disabled'])

        # 3. PBX Ringing + npxLoggedIn: true
        c3 = by_id['pbx_ringing_loggedin_true']
        self.assertFalse(c3['bTransf']['disabled'])
        self.assertFalse(c3['bPen']['disabled'])
        self.assertFalse(c3['bRemoverFila']['disabled'])
        self.assertFalse(c3['bDeslogar']['disabled'])
        self.assertFalse(c3['bPausar']['disabled'])

        # 4. PBX Free + Pausado na NPX + npxLoggedIn: true
        c4 = by_id['pbx_free_pausado_loggedin_true']
        self.assertFalse(c4['bTirar']['disabled'], "bTirar deve estar habilitado quando npxLoggedIn=true e paused=true")
        self.assertEqual(c4['bTirar']['display'], "", "bTirar deve estar visível")
        self.assertEqual(c4['bPausar']['display'], "none", "bPausar deve estar oculto quando paused=true")
        self.assertFalse(c4['bTransf']['disabled'])
        self.assertFalse(c4['bPen']['disabled'])
        self.assertFalse(c4['bRemoverFila']['disabled'])
        self.assertFalse(c4['bDeslogar']['disabled'])

        # 5. PBX Free com npxLoggedIn: false (não pausado: bloqueio total)
        c5 = by_id['pbx_free_loggedin_false']
        self.assertTrue(c5['bTransf']['disabled'])
        self.assertTrue(c5['bPen']['disabled'])
        self.assertTrue(c5['bRemoverFila']['disabled'])
        self.assertTrue(c5['bDeslogar']['disabled'])
        self.assertTrue(c5['bPausar']['disabled'])
        self.assertEqual(c5['bPausar']['display'], "")
        self.assertEqual(c5['bTirar']['display'], "none")
        self.assertIn("sem sessão ativa confirmada", c5['dlgMsg'])

        # 6. Cenário exato do usuário: {code:4422008, queue:Suporte, line:pbx, pbxStatus:Free, status:0, paused:true, npxLoggedIn:false}
        c6 = by_id['cenario_exato_4422008_paused_false']
        self.assertTrue(c6['bTirar']['disabled'], "bTirar deve estar bloqueado (disabled=true) quando npxLoggedIn=false")
        self.assertEqual(c6['bTirar']['display'], "", "bTirar deve estar visível por estado mas desabilitado")
        self.assertEqual(c6['bPausar']['display'], "none", "bPausar deve estar oculto")
        self.assertTrue(c6['bTransf']['disabled'], "bTransf deve estar bloqueado")
        self.assertTrue(c6['bPen']['disabled'], "bPen deve estar bloqueado")
        self.assertTrue(c6['bRemoverFila']['disabled'], "bRemoverFila deve estar bloqueado")
        self.assertTrue(c6['bDeslogar']['disabled'], "bDeslogar deve estar bloqueado")
        self.assertNotIn("Apenas retirar pausa", c6['dlgMsg'], "Mensagem antiga de exceção não pode existir")
        self.assertIn("sem sessão ativa confirmada na fila NPX", c6['dlgMsg'])
        self.assertIn("sem sessão ativa na NPX", c6['dPausa'])

        # 7. PBX Free com npxLoggedIn: undefined / ausente + paused: true
        c7 = by_id['pbx_free_pausado_loggedin_undefined']
        self.assertTrue(c7['bTirar']['disabled'], "bTirar deve estar bloqueado quando npxLoggedIn é ausente")
        self.assertTrue(c7['bTransf']['disabled'])
        self.assertTrue(c7['bPen']['disabled'])
        self.assertTrue(c7['bRemoverFila']['disabled'])
        self.assertTrue(c7['bDeslogar']['disabled'])

        # 8. PBX Free com npxLoggedIn: "true" (string truthy - consumo booleano estrito)
        c8 = by_id['pbx_free_pausado_loggedin_string_truthy']
        self.assertTrue(c8['bTirar']['disabled'], "bTirar deve estar bloqueado para string 'true' (só boolean true libera)")
        self.assertTrue(c8['bTransf']['disabled'])
        self.assertTrue(c8['bPen']['disabled'])
        self.assertTrue(c8['bRemoverFila']['disabled'])
        self.assertTrue(c8['bDeslogar']['disabled'])
        self.assertIn("sem sessão ativa confirmada", c8['dlgMsg'])

        # 9. Linha NPX com npxLoggedIn: false (fail-closed independente da linha)
        c9 = by_id['npx_linha_loggedin_false']
        self.assertTrue(c9['bTransf']['disabled'], "bTransf deve estar bloqueado para line=npx sem flag true")
        self.assertTrue(c9['bPen']['disabled'])
        self.assertTrue(c9['bRemoverFila']['disabled'])
        self.assertTrue(c9['bDeslogar']['disabled'])
        self.assertTrue(c9['bPausar']['disabled'])
        self.assertIn("sem sessão ativa confirmada na fila NPX", c9['dlgMsg'])

        # 10. Linha NPX com npxLoggedIn: undefined (fail-closed independente da linha)
        c10 = by_id['npx_linha_loggedin_undefined']
        self.assertTrue(c10['bTransf']['disabled'], "bTransf deve estar bloqueado para line=npx sem flag")
        self.assertTrue(c10['bPen']['disabled'])
        self.assertTrue(c10['bRemoverFila']['disabled'])
        self.assertTrue(c10['bDeslogar']['disabled'])
        self.assertTrue(c10['bPausar']['disabled'])

        # 11. Linha OFF com npxLoggedIn: false
        c11 = by_id['off_linha_loggedin_false']
        self.assertTrue(c11['bTransf']['disabled'], "bTransf deve estar bloqueado para line=off sem sessão")
        self.assertTrue(c11['bPen']['disabled'])
        self.assertTrue(c11['bRemoverFila']['disabled'])
        self.assertTrue(c11['bDeslogar']['disabled'])
        self.assertTrue(c11['bPausar']['disabled'])

        # 12. Linha OFF com npxLoggedIn: true
        c12 = by_id['off_linha_loggedin_true']
        self.assertFalse(c12['bTransf']['disabled'], "bTransf deve estar liberado para line=off com sessão confirmada")
        self.assertFalse(c12['bPen']['disabled'])
        self.assertFalse(c12['bRemoverFila']['disabled'])
        self.assertFalse(c12['bDeslogar']['disabled'])
        self.assertFalse(c12['bPausar']['disabled'])

        # 13. PBX-Only (Fila PBX_Sem_Fila, código pbx-1099)
        c13 = by_id['pbx_only_sem_fila']
        self.assertTrue(c13['bTransf']['disabled'], "bTransf deve estar bloqueado para PBX-only")
        self.assertTrue(c13['bPen']['disabled'])
        self.assertTrue(c13['bRemoverFila']['disabled'])
        self.assertTrue(c13['bDeslogar']['disabled'])
        self.assertTrue(c13['bPausar']['disabled'])
        self.assertTrue(c13['bTirar']['disabled'])
        self.assertIn("sem sessão ativa em fila", c13['dlgMsg'])

        # 14. PBX Free com npxLoggedIn: true, mas painelConfigurado: false
        c14 = by_id['pbx_free_loggedin_true_sem_painel']
        self.assertTrue(c14['bTransf']['disabled'], "bTransf deve respeitar painelConfigurado=False")
        self.assertTrue(c14['bPen']['disabled'], "bPen deve respeitar painelConfigurado=False")
        self.assertTrue(c14['bRemoverFila']['disabled'], "bRemoverFila deve respeitar painelConfigurado=False")
        self.assertTrue(c14['bPausar']['disabled'], "bPausar deve respeitar painelConfigurado=False")
        self.assertFalse(c14['bDeslogar']['disabled'], "bDeslogar independe do painel de controle")
        self.assertIn("aguardando configuração de credenciais", c14['dlgMsg'])

        # 15. PBX Free com npxLoggedIn: true, mas sem outras filas
        c15 = by_id['pbx_free_loggedin_true_sem_destinos']
        self.assertTrue(c15['bTransf']['disabled'], "bTransf deve estar desabilitado se não houver outras filas")
        self.assertFalse(c15['bPen']['disabled'])
        self.assertFalse(c15['bRemoverFila']['disabled'])
        self.assertFalse(c15['bDeslogar']['disabled'])
        self.assertFalse(c15['bPausar']['disabled'])

        # 16. Agente NPX normal com npxLoggedIn: true
        c16 = by_id['npx_normal_loggedin_true']
        self.assertFalse(c16['bTransf']['disabled'])
        self.assertFalse(c16['bPen']['disabled'])
        self.assertFalse(c16['bRemoverFila']['disabled'])
        self.assertFalse(c16['bDeslogar']['disabled'])
        self.assertFalse(c16['bPausar']['disabled'])

    def test_preservacao_feedback_e_abrir_outro_agente(self):
        """Valida preservação de ✔/✖ em atualizarDialogo(false) e limpeza no fluxo abrir()."""
        res = subprocess.run(['node', RUNNER_PATH, JS_PATH], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f"Erro ao executar runner Node.js:\n{res.stderr}")

        data = json.loads(res.stdout)
        by_id = {r['id']: r for r in data}

        # 1. Preservação de feedback de sucesso ✔ x no caso alvo durante refetch
        u1 = by_id['atualizacao_nao_inicial_preserva_sucesso']
        self.assertEqual(u1['dlgMsg'], "✔ Analista saiu da pausa.", "Feedback de sucesso não pode ser sobrescrito")
        self.assertFalse(u1['bTransfDisabled'])
        self.assertFalse(u1['bDeslogarDisabled'])

        # 2. Preservação de feedback de recusa ✖ x no caso alvo durante refetch
        u2 = by_id['atualizacao_nao_inicial_preserva_recusa']
        self.assertEqual(u2['dlgMsg'], "✖ Não foi possível transferir: fila de destino cheia.", "Feedback de recusa não pode ser sobrescrito")
        self.assertFalse(u2['bTransfDisabled'])
        self.assertFalse(u2['bDeslogarDisabled'])

        # 3. Quando flag muda para false após ação (ex: deslogar), controles bloqueiam mas feedback fica visível
        u3 = by_id['atualizacao_flag_muda_false_preserva_feedback']
        self.assertEqual(u3['dlgMsg'], "✔ Analista Alfa deslogado com sucesso.", "Feedback de logout deve permanecer visível")
        self.assertTrue(u3['bTransfDisabled'], "Controles devem bloquear quando flag muda para false")
        self.assertTrue(u3['bPenDisabled'])
        self.assertTrue(u3['bRemoverFilaDisabled'])
        self.assertTrue(u3['bDeslogarDisabled'])
        self.assertTrue(u3['bPausarDisabled'])
        self.assertTrue(u3['bTirarDisabled'])

        # 4. Quando agente cai em PBX_Sem_Fila, controles bloqueiam mas feedback fica visível
        u4 = by_id['atualizacao_cai_pbx_sem_fila_preserva_feedback']
        self.assertEqual(u4['dlgMsg'], "✔ Analista Alfa removido da fila.", "Feedback deve permanecer visível ao cair em PBX_Sem_Fila")
        self.assertTrue(u4['bTransfDisabled'])
        self.assertTrue(u4['bDeslogarDisabled'])
        self.assertTrue(u4['bPausarDisabled'])
        self.assertTrue(u4['bTirarDisabled'])

        # 5. Fluxo abrir() para outro agente limpa feedback anterior e exibe info do novo agente
        u5 = by_id['abrir_outro_agente_limpa_feedback_e_exibe_info']
        self.assertNotIn("✔", u5['dlgMsg'], "Feedback anterior deve ser limpo ao abrir outro agente")
        self.assertIn("Telefonia ativa via PBX com sessão confirmada na NPX", u5['dlgMsg'])
        self.assertFalse(u5['bTransfDisabled'])
        self.assertFalse(u5['bDeslogarDisabled'])

        # 6. Fix pós-logout (ramo !a): agente sumiu do snapshot (ex: sobrou apenas PBX-only de outro code)
        # Todos os 6 controles devem ficar desabilitados, dInfo atualizado e feedback ✔ preservado
        u6 = by_id['pos_logout_ramo_sem_agente_preserva_sucesso_bloqueia_seis']
        self.assertEqual(u6['dInfo'], "Agente não está mais logado.")
        self.assertEqual(u6['dlgMsg'], "✔ Analista 4422008 deslogado com sucesso.", "Feedback de sucesso deve ser preservado")
        self.assertTrue(u6['bTransfDisabled'], "bTransf deve estar bloqueado quando !a")
        self.assertTrue(u6['bPenDisabled'], "bPen deve estar bloqueado quando !a")
        self.assertTrue(u6['bRemoverFilaDisabled'], "bRemoverFila deve estar bloqueado quando !a")
        self.assertTrue(u6['bDeslogarDisabled'], "bDeslogar deve estar bloqueado quando !a")
        self.assertTrue(u6['bPausarDisabled'], "bPausar deve estar bloqueado quando !a")
        self.assertTrue(u6['bTirarDisabled'], "bTirar deve estar bloqueado quando !a")

        # 7. Ramo !a com dados vazios e feedback de recusa ✖ preservado
        u7 = by_id['pos_logout_dados_vazio_preserva_recusa_bloqueia_seis']
        self.assertEqual(u7['dInfo'], "Agente não está mais logado.")
        self.assertEqual(u7['dlgMsg'], "✖ Sessão finalizada pelo servidor.", "Feedback de recusa deve ser preservado")
        self.assertTrue(u7['bTransfDisabled'])
        self.assertTrue(u7['bPenDisabled'])
        self.assertTrue(u7['bRemoverFilaDisabled'])
        self.assertTrue(u7['bDeslogarDisabled'])
        self.assertTrue(u7['bPausarDisabled'])
        self.assertTrue(u7['bTirarDisabled'])


if __name__ == '__main__':
    unittest.main()
