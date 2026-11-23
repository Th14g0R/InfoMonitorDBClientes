const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
let code = fs.readFileSync(jsPath, 'utf8');

// Cria elementos DOM simulados com suporte a propriedades e estilos
function createElementMock(tag = 'div') {
  return {
    tagName: tag.toUpperCase(),
    textContent: '',
    innerHTML: '',
    value: '',
    disabled: false,
    title: '',
    style: { display: '', color: '' },
    setAttribute: function(k, v) { this[k] = v; },
    getAttribute: function(k) { return this[k]; },
    addEventListener: () => {},
    querySelectorAll: () => []
  };
}

const elements = {
  'cfg-agentes': { textContent: '{"pode":true,"layout":{"cols":2,"c1":[],"c2":[],"ord":{"col":"status","dir":1}}}' },
  'dNome': createElementMock('h3'),
  'dInfo': createElementMock('div'),
  'aPausa': createElementMock('div'),
  'dPausa': createElementMock('span'),
  'bTirar': createElementMock('button'),
  'bPausar': createElementMock('button'),
  'dFila': createElementMock('select'),
  'dPen': createElementMock('input'),
  'bTransf': createElementMock('button'),
  'bPen': createElementMock('button'),
  'bRemoverFila': createElementMock('button'),
  'bDeslogar': createElementMock('button'),
  'dlgMsg': createElementMock('div'),
  'dlg': {
    open: true,
    showModal: function() { this.open = true; },
    close: function() { this.open = false; },
    addEventListener: () => {}
  },
  'msg': createElementMock('div'),
  'avisoPbx': createElementMock('div'),
  'filas': createElementMock('div'),
  'nLog': createElementMock('span'),
  'nLivre': createElementMock('span'),
  'nCham': createElementMock('span'),
  'nLig': createElementMock('span'),
  'nPausa': createElementMock('span'),
  'nPresa': createElementMock('span'),
  'hora': createElementMock('span')
};

const sandbox = {
  document: {
    getElementById: (id) => elements[id] || createElementMock(),
    documentElement: { classList: { add: () => {}, remove: () => {}, contains: () => false, toggle: () => {} } },
    querySelectorAll: () => [],
    addEventListener: () => {}
  },
  window: { addEventListener: () => {} },
  addEventListener: () => {},
  matchMedia: () => ({ matches: false }),
  screen: { width: 1920, height: 1080 },
  innerWidth: 1024,
  innerHeight: 768,
  localStorage: { getItem: () => null, setItem: () => {} },
  setTimeout: () => {},
  setInterval: () => {},
  console: console
};

// Instrumenta o final da IIFE para expor controles de injeção de estado de teste
code = code.replace(/\}\)\(\);?\s*$/, `
  if (typeof window !== 'undefined') {
    window._testAgentes = window._testAgentes || {};
    window._testAgentes.setDados = (d) => { dados = d; };
    window._testAgentes.setFilas = (f) => { filas = f; };
    window._testAgentes.setSel = (s) => { sel = s; };
    window._testAgentes.setPainelConfigurado = (p) => { painelConfigurado = p; };
  }
})();
`);

vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const api = sandbox.window._testAgentes;
api.getElements = () => elements;

const filasPadrao = [
  { key: 'Fila_Suporte', nome: 'Suporte N1' },
  { key: 'Fila_Comercial', nome: 'Comercial' },
  { key: 'PBX_Sem_Fila', nome: 'Não logado' }
];

function executarCenario(id, agente, opcoes = {}) {
  const painelConfig = opcoes.painelConfigurado !== false;
  const listaFilas = opcoes.filas || filasPadrao;

  api.setPainelConfigurado(painelConfig);
  api.setFilas(listaFilas);
  api.setDados([agente]);
  api.setSel({ code: agente.code, fila: agente.queue, agente: null });

  // Limpa estados dos botões antes da invocação
  const el = api.getElements();
  el.bTransf.disabled = false; el.bTransf.title = '';
  el.bPen.disabled = false; el.bPen.title = '';
  el.bRemoverFila.disabled = false; el.bRemoverFila.title = '';
  el.bDeslogar.disabled = false; el.bDeslogar.title = '';
  el.bPausar.disabled = false; el.bPausar.title = ''; el.bPausar.style.display = '';
  el.bTirar.disabled = false; el.bTirar.title = ''; el.bTirar.style.display = '';
  el.dlgMsg.textContent = '';
  el.dPausa.textContent = '';

  api.atualizarDialogo(true);

  return {
    id,
    dInfo: el.dInfo.innerHTML,
    bTransf: { disabled: el.bTransf.disabled, title: el.bTransf.title },
    bPen: { disabled: el.bPen.disabled, title: el.bPen.title },
    bRemoverFila: { disabled: el.bRemoverFila.disabled, title: el.bRemoverFila.title },
    bDeslogar: { disabled: el.bDeslogar.disabled, title: el.bDeslogar.title },
    bPausar: { disabled: el.bPausar.disabled, title: el.bPausar.title, display: el.bPausar.style.display },
    bTirar: { disabled: el.bTirar.disabled, title: el.bTirar.title, display: el.bTirar.style.display },
    dlgMsg: el.dlgMsg.textContent,
    dPausa: el.dPausa.textContent
  };
}

const resultados = [];

// 1. Caso do Screenshot: PBX Free + voz NPX 0 + npxLoggedIn: true (não pausado)
resultados.push(executarCenario('pbx_free_voz0_loggedin_true', {
  code: '1001', name: 'Analista Alfa', queue: 'Fila_Suporte', extension: '1001',
  line: 'pbx', pbxStatus: 'Free', pbxExtension: '1001', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

// 2. PBX Busy + npxLoggedIn: true
resultados.push(executarCenario('pbx_busy_loggedin_true', {
  code: '1002', name: 'Analista Beta', queue: 'Fila_Suporte', extension: '1002',
  line: 'pbx', pbxStatus: 'Busy', pbxNumber: '85988887777', pbxSeconds: 45, status: 0,
  paused: false, npxLoggedIn: true, penalty: 2
}));

// 3. PBX Ringing + npxLoggedIn: true
resultados.push(executarCenario('pbx_ringing_loggedin_true', {
  code: '1003', name: 'Analista Gama', queue: 'Fila_Suporte', extension: '1003',
  line: 'pbx', pbxStatus: 'Ringing', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

// 4. PBX Free + Pausado na NPX + npxLoggedIn: true
resultados.push(executarCenario('pbx_free_pausado_loggedin_true', {
  code: '1004', name: 'Analista Delta', queue: 'Fila_Suporte', extension: '1004',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: true, pauseType: 'Almoço', pauseMinutes: 15, npxLoggedIn: true, penalty: 1
}));

// 5. PBX Free com npxLoggedIn: false (não pausado: recusa/bloqueio mantido)
resultados.push(executarCenario('pbx_free_loggedin_false', {
  code: '1005', name: 'Analista Epsilon', queue: 'Fila_Suporte', extension: '1005',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: false, npxLoggedIn: false, penalty: 1
}));

// 6. Cenário exato do usuário: {code:4422008, queue:Suporte, line:pbx, pbxStatus:Free, status:0, paused:true, npxLoggedIn:false}
resultados.push(executarCenario('cenario_exato_4422008_paused_false', {
  code: '4422008', name: 'Analista 4422008', queue: 'Fila_Suporte', extension: '4422008',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: true, pauseType: 'Almoço', pauseMinutes: 30, npxLoggedIn: false, penalty: 1
}));

// 7. PBX Free com npxLoggedIn: undefined / ausente + paused: true
resultados.push(executarCenario('pbx_free_pausado_loggedin_undefined', {
  code: '1007', name: 'Analista Zeta Pausado', queue: 'Fila_Suporte', extension: '1007',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: true, pauseType: 'Lanche', pauseMinutes: 10, penalty: 1
}));

// 8. PBX Free com npxLoggedIn: "true" (string truthy - consumo booleano estrito deve recusar)
resultados.push(executarCenario('pbx_free_pausado_loggedin_string_truthy', {
  code: '1008', name: 'Analista String Truthy', queue: 'Fila_Suporte', extension: '1008',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: true, pauseType: 'Almoço', pauseMinutes: 15, npxLoggedIn: 'true', penalty: 1
}));

// 9. Linha NPX com npxLoggedIn: false (bloqueio por falta de sessão)
resultados.push(executarCenario('npx_linha_loggedin_false', {
  code: '1009', name: 'Analista NPX Sem Sessão', queue: 'Fila_Suporte', extension: '1009',
  line: 'npx', status: 0,
  paused: false, npxLoggedIn: false, penalty: 1
}));

// 10. Linha NPX com npxLoggedIn: undefined (bloqueio por falta de sessão)
resultados.push(executarCenario('npx_linha_loggedin_undefined', {
  code: '1010', name: 'Analista NPX Sem Flag', queue: 'Fila_Suporte', extension: '1010',
  line: 'npx', status: 0,
  paused: false, penalty: 1
}));

// 11. Linha OFF com npxLoggedIn: false (bloqueio por falta de sessão)
resultados.push(executarCenario('off_linha_loggedin_false', {
  code: '1011', name: 'Analista OFF Sem Sessão', queue: 'Fila_Suporte', extension: '1011',
  line: 'off', status: 0,
  paused: false, npxLoggedIn: false, penalty: 1
}));

// 12. Linha OFF com npxLoggedIn: true (sessão aberta mas status off/voz 0: liberado)
resultados.push(executarCenario('off_linha_loggedin_true', {
  code: '1012', name: 'Analista OFF Com Sessão', queue: 'Fila_Suporte', extension: '1012',
  line: 'off', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

// 13. PBX-Only (Fila PBX_Sem_Fila, código pbx-1099, npxLoggedIn: false)
resultados.push(executarCenario('pbx_only_sem_fila', {
  code: 'pbx-1099', name: 'Analista Ramal Só', queue: 'PBX_Sem_Fila', extension: '1099',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: false, npxLoggedIn: false, penalty: null
}));

// 14. PBX Free com npxLoggedIn: true, mas painelConfigurado: false
resultados.push(executarCenario('pbx_free_loggedin_true_sem_painel', {
  code: '1001', name: 'Analista Alfa', queue: 'Fila_Suporte', extension: '1001',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}, { painelConfigurado: false }));

// 15. PBX Free com npxLoggedIn: true, mas sem outras filas para transferir
resultados.push(executarCenario('pbx_free_loggedin_true_sem_destinos', {
  code: '1001', name: 'Analista Alfa', queue: 'Fila_Suporte', extension: '1001',
  line: 'pbx', pbxStatus: 'Free', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}, { filas: [{ key: 'Fila_Suporte', nome: 'Suporte N1' }, { key: 'PBX_Sem_Fila', nome: 'Não logado' }] }));

// 16. Agente NPX normal (line: 'npx', npxLoggedIn: true)
resultados.push(executarCenario('npx_normal_loggedin_true', {
  code: '1016', name: 'Analista NPX Puro', queue: 'Fila_Suporte', extension: '1016',
  line: 'npx', status: 1,
  paused: false, npxLoggedIn: true, penalty: 1
}));

// =========================================================================
// Testes de Atualização Real (NÃO resetando DOM) e Abertura de Outro Agente
// =========================================================================

const el = api.getElements();

// Setup inicial com agente alvo PBX + npxLoggedIn: true
const agenteAlvo = {
  code: '1001', name: 'Analista Alfa', queue: 'Fila_Suporte', extension: '1001',
  line: 'pbx', pbxStatus: 'Free', pbxExtension: '1001', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
};
api.setPainelConfigurado(true);
api.setFilas(filasPadrao);
api.setDados([agenteAlvo]);
api.setSel({ code: '1001', fila: 'Fila_Suporte', agente: null });
el.dlgMsg.textContent = '';
api.atualizarDialogo(true);

// 17. Atualização não-inicial preserva feedback ✔ x no caso alvo
el.dlgMsg.textContent = '✔ Analista saiu da pausa.';
el.dlgMsg.style.color = 'var(--livre)';
api.atualizarDialogo(false);
resultados.push({
  id: 'atualizacao_nao_inicial_preserva_sucesso',
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled
});

// 18. Atualização não-inicial preserva feedback ✖ recusa no caso alvo
el.dlgMsg.textContent = '✖ Não foi possível transferir: fila de destino cheia.';
el.dlgMsg.style.color = 'var(--lig)';
api.atualizarDialogo(false);
resultados.push({
  id: 'atualizacao_nao_inicial_preserva_recusa',
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled
});

// 19. Quando flag muda para false (ex: deslogar), controles bloqueiam mas feedback ✔ x permanece visível
el.dlgMsg.textContent = '✔ Analista Alfa deslogado com sucesso.';
el.dlgMsg.style.color = 'var(--livre)';
const agenteSemSessao = { ...agenteAlvo, npxLoggedIn: false };
api.setDados([agenteSemSessao]);
api.atualizarDialogo(false);
resultados.push({
  id: 'atualizacao_flag_muda_false_preserva_feedback',
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bPenDisabled: el.bPen.disabled,
  bRemoverFilaDisabled: el.bRemoverFila.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled,
  bPausarDisabled: el.bPausar.disabled,
  bTirarDisabled: el.bTirar.disabled
});

// 20. Quando cai em PBX_Sem_Fila, controles bloqueiam mas feedback permanece visível
el.dlgMsg.textContent = '✔ Analista Alfa removido da fila.';
el.dlgMsg.style.color = 'var(--livre)';
const agenteSemFila = { ...agenteAlvo, queue: 'PBX_Sem_Fila', npxLoggedIn: false };
api.setDados([agenteSemFila]);
api.setSel({ code: '1001', fila: 'PBX_Sem_Fila', agente: null });
api.atualizarDialogo(false);
resultados.push({
  id: 'atualizacao_cai_pbx_sem_fila_preserva_feedback',
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled,
  bPausarDisabled: el.bPausar.disabled,
  bTirarDisabled: el.bTirar.disabled
});

// 21. Fluxo abrir() para outro agente limpa feedback anterior e exibe info do novo agente
const outroAgente = {
  code: '1002', name: 'Analista Beta', queue: 'Fila_Suporte', extension: '1002',
  line: 'pbx', pbxStatus: 'Free', pbxExtension: '1002', status: 0,
  paused: false, npxLoggedIn: true, penalty: 2
};
api.setDados([agenteSemFila, outroAgente]);
api.abrir('1002', 'Fila_Suporte');
resultados.push({
  id: 'abrir_outro_agente_limpa_feedback_e_exibe_info',
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled,
  bPausarDisabled: el.bPausar.disabled
});

// =========================================================================
// Fix pós-logout (ramo !a): agente sumiu do snapshot (ex: restou apenas PBX-only)
// =========================================================================

// 22. Original flagtrue habilitado, setDados com apenas PBX-only de outro code (ou []),
// atualizarDialogo(false) -> todos os 6 controles disabled e feedback de sucesso ✔ preservado
const agente4422008 = {
  code: '4422008', name: 'Analista 4422008', queue: 'Fila_Suporte', extension: '2008',
  line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
};
api.setPainelConfigurado(true);
api.setFilas(filasPadrao);
api.setDados([agente4422008]);
api.setSel({ code: '4422008', fila: 'Fila_Suporte', agente: null });
el.dlgMsg.textContent = '';
api.atualizarDialogo(true);

// Simula clique em "Deslogar agente" com feedback de sucesso
el.dlgMsg.textContent = '✔ Analista 4422008 deslogado com sucesso.';
el.dlgMsg.style.color = 'var(--livre)';

// O refetch da NPX retorna sem o agente 4422008 (sobrando apenas PBX-only de outro code, ex: pbx-2008)
const pbxOnlyOutro = {
  code: 'pbx-2008', name: 'Ramal 2008 Só', queue: 'PBX_Sem_Fila', extension: '2008',
  line: 'pbx', pbxStatus: 'Free', status: 0, paused: false, npxLoggedIn: false
};
api.setDados([pbxOnlyOutro]);

// Refetch pós-ação sem reset de DOM (inicial=false)
api.atualizarDialogo(false);

resultados.push({
  id: 'pos_logout_ramo_sem_agente_preserva_sucesso_bloqueia_seis',
  dInfo: el.dInfo.textContent,
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bPenDisabled: el.bPen.disabled,
  bRemoverFilaDisabled: el.bRemoverFila.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled,
  bPausarDisabled: el.bPausar.disabled,
  bTirarDisabled: el.bTirar.disabled
});

// 23. Ramo !a com feedback de recusa e dados vazios []
api.setDados([agente4422008]);
api.setSel({ code: '4422008', fila: 'Fila_Suporte', agente: null });
api.atualizarDialogo(true);
el.dlgMsg.textContent = '✖ Sessão finalizada pelo servidor.';
el.dlgMsg.style.color = 'var(--lig)';
api.setDados([]);
api.atualizarDialogo(false);

resultados.push({
  id: 'pos_logout_dados_vazio_preserva_recusa_bloqueia_seis',
  dInfo: el.dInfo.textContent,
  dlgMsg: el.dlgMsg.textContent,
  bTransfDisabled: el.bTransf.disabled,
  bPenDisabled: el.bPen.disabled,
  bRemoverFilaDisabled: el.bRemoverFila.disabled,
  bDeslogarDisabled: el.bDeslogar.disabled,
  bPausarDisabled: el.bPausar.disabled,
  bTirarDisabled: el.bTirar.disabled
});

// 24. Validação de números PBX formatados no modal (dInfo)
resultados.push(executarCenario('modal_pbx_call_558999180044', {
  code: '2001', name: 'Analista 1', queue: 'Fila_Suporte', extension: '2001',
  line: 'pbx', pbxStatus: 'Busy', pbxNumber: '558999180044', pbxDirection: 'inbound', pbxSeconds: 45, status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

resultados.push(executarCenario('modal_pbx_call_5588981669885', {
  code: '2002', name: 'Analista 2', queue: 'Fila_Suporte', extension: '2002',
  line: 'pbx', pbxStatus: 'Busy', pbxNumber: '5588981669885', pbxDirection: 'outbound', pbxSeconds: 70, status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

resultados.push(executarCenario('modal_pbx_call_ramal_2008', {
  code: '2003', name: 'Analista 3', queue: 'Fila_Suporte', extension: '2003',
  line: 'pbx', pbxStatus: 'Ringing', pbxNumber: '2008', pbxDirection: 'inbound', pbxSeconds: 15, status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

resultados.push(executarCenario('modal_pbx_call_internacional', {
  code: '2004', name: 'Analista 4', queue: 'Fila_Suporte', extension: '2004',
  line: 'pbx', pbxStatus: 'Busy', pbxNumber: '+14155552671', pbxDirection: 'inbound', pbxSeconds: 120, status: 0,
  paused: false, npxLoggedIn: true, penalty: 1
}));

console.log(JSON.stringify(resultados));
