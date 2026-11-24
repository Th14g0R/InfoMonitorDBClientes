const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
let code = fs.readFileSync(jsPath, 'utf8');

// Cria ambiente sandbox simulando o DOM mínimo de agentes.html
function mockEl(props = {}) {
  return {
    textContent: '',
    innerHTML: '',
    value: '',
    disabled: false,
    checked: false,
    title: '',
    style: {},
    classList: { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
    setAttribute: function(k, v) { this[k] = v; },
    getAttribute: function(k) { return this[k]; },
    addEventListener: () => {},
    querySelectorAll: () => [],
    ...props
  };
}

const elements = {
  'ag': mockEl(),
  'cfg-agentes': mockEl({ textContent: '{"pode":true,"layout":{"cols":2,"c1":[],"c2":[],"ord":{"col":"status","dir":1}}}' }),
  'nLog': mockEl(),
  'nPbx': mockEl(),
  'nLivre': mockEl(),
  'nFila': mockEl({ textContent: '–' }),
  'nCham': mockEl(),
  'nLig': mockEl(),
  'nPausa': mockEl(),
  'nPresa': mockEl(),
  'filas': mockEl(),
  'msg': mockEl(),
  'avisoPbx': mockEl(),
  'hora': mockEl(),
  'soPausa': mockEl({ checked: false })
};

const sandbox = {
  document: {
    getElementById: (id) => elements[id] || mockEl(),
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

code = code.replace(/\}\)\(\);?\s*$/, `
  globalThis.__exported = {
    stDe: typeof stDe !== 'undefined' ? stDe : null,
    situacaoTexto: typeof situacaoTexto !== 'undefined' ? situacaoTexto : null,
    situacaoHtml: typeof situacaoHtml !== 'undefined' ? situacaoHtml : null,
    fmtMin: typeof fmtMin !== 'undefined' ? fmtMin : null,
    fmtSeg: typeof fmtSeg !== 'undefined' ? fmtSeg : null,
    fmtFone: typeof fmtFone !== 'undefined' ? fmtFone : null,
    ehLig: typeof ehLig !== 'undefined' ? ehLig : null,
    ehToque: typeof ehToque !== 'undefined' ? ehToque : null,
    ehCham: typeof ehCham !== 'undefined' ? ehCham : null,
    ehRecebendo: typeof ehRecebendo !== 'undefined' ? ehRecebendo : null,
    ehDiscendo: typeof ehDiscendo !== 'undefined' ? ehDiscendo : null,
    ehChamDesc: typeof ehChamDesc !== 'undefined' ? ehChamDesc : null,
    ehPausa: typeof ehPausa !== 'undefined' ? ehPausa : null,
    ehLivre: typeof ehLivre !== 'undefined' ? ehLivre : null,
    logadoNpx: typeof logadoNpx !== 'undefined' ? logadoNpx : null,
    chamada: typeof chamada !== 'undefined' ? chamada : null,
    linha: typeof linha !== 'undefined' ? linha : null,
    card: typeof card !== 'undefined' ? card : null,
    render: typeof render !== 'undefined' ? render : null,
    setDados: d => { dados = d; },
    setFilas: f => { filas = f; },
    setWaitingCalls: w => { waitingCalls = w; },
    getWaitingCalls: () => waitingCalls,
    ST_PBX: typeof ST_PBX !== 'undefined' ? ST_PBX : null
  };
})();
`);

vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const fn = sandbox.__exported;

const results = [];

// 1. PBX Free + paused
const a1 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Almoço', pauseMinutes: 10, pauseMaxMinutes: 30 };
results.push({ id: 'pbx_free_paused', st: fn.stDe(a1), ehPausa: fn.ehPausa(a1), ehLivre: fn.ehLivre(a1), sitTxt: fn.situacaoTexto(a1), sitHtml: fn.situacaoHtml(a1) });

// 2. PBX Busy + paused
const a2 = { line: 'pbx', pbxStatus: 'Busy', pbxNumber: '85999999999', pbxDirection: 'inbound', paused: true, pauseType: 'Almoço' };
results.push({ id: 'pbx_busy_paused', st: fn.stDe(a2), ehLig: fn.ehLig(a2), ehPausa: fn.ehPausa(a2) });

// 3. PBX Ringing + paused (inbound explícito para precedência Ringing com Recebendo)
const a3 = { line: 'pbx', pbxStatus: 'Ringing', pbxDirection: 'inbound', paused: true, pauseType: 'Almoço' };
results.push({ id: 'pbx_ringing_paused', st: fn.stDe(a3), ehCham: fn.ehCham(a3), ehPausa: fn.ehPausa(a3) });

// 4. PBX Free + sem pausa
const a4 = { line: 'pbx', pbxStatus: 'Free', paused: false };
results.push({ id: 'pbx_free_sem_pausa', st: fn.stDe(a4), ehLivre: fn.ehLivre(a4), ehPausa: fn.ehPausa(a4), sitTxt: fn.situacaoTexto(a4) });

// 5. NPX ativo + pausa
const a5 = { line: 'npx', status: 1, paused: true, pauseType: 'Lanche' };
results.push({ id: 'npx_pausa', st: fn.stDe(a5), ehPausa: fn.ehPausa(a5), ehLivre: fn.ehLivre(a5) });

// 6. NPX ativo + sem pausa
const a6 = { line: 'npx', status: 1, paused: false };
results.push({ id: 'npx_livre', st: fn.stDe(a6), ehLivre: fn.ehLivre(a6), ehPausa: fn.ehPausa(a6) });

// 7. Line off intacto
const a7 = { line: 'off', status: 0, paused: false };
results.push({ id: 'line_off', st: fn.stDe(a7) });

// 8. Fronteiras de duracao decorrida em Pausa (PBX e NPX)
const p101 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 101, pauseMaxMinutes: 60 };
results.push({ id: 'pause_101m', sitTxt: fn.situacaoTexto(p101), sitHtml: fn.situacaoHtml(p101) });

const p60 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 60, pauseMaxMinutes: 60 };
results.push({ id: 'pause_60m', sitTxt: fn.situacaoTexto(p60), sitHtml: fn.situacaoHtml(p60) });

const p61 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 61, pauseMaxMinutes: 60 };
results.push({ id: 'pause_61m', sitTxt: fn.situacaoTexto(p61), sitHtml: fn.situacaoHtml(p61) });

const p120 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 120, pauseMaxMinutes: 60 };
results.push({ id: 'pause_120m', sitTxt: fn.situacaoTexto(p120), sitHtml: fn.situacaoHtml(p120) });

const p59 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 59, pauseMaxMinutes: 60 };
results.push({ id: 'pause_59m', sitTxt: fn.situacaoTexto(p59), sitHtml: fn.situacaoHtml(p59) });

const p12 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 12, pauseMaxMinutes: 60 };
results.push({ id: 'pause_12m', sitTxt: fn.situacaoTexto(p12), sitHtml: fn.situacaoHtml(p12) });

const p0 = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 0, pauseMaxMinutes: 60 };
results.push({ id: 'pause_0m', sitTxt: fn.situacaoTexto(p0), sitHtml: fn.situacaoHtml(p0) });

const npx101 = { line: 'npx', status: 1, paused: true, pauseType: 'Almoço', pauseMinutes: 101, pauseMaxMinutes: 60 };
results.push({ id: 'npx_pause_101m', sitTxt: fn.situacaoTexto(npx101), sitHtml: fn.situacaoHtml(npx101) });

const npx59 = { line: 'npx', status: 1, paused: true, pauseType: 'Lanche', pauseMinutes: 59, pauseMaxMinutes: 60 };
results.push({ id: 'npx_pause_59m', sitTxt: fn.situacaoTexto(npx59), sitHtml: fn.situacaoHtml(npx59) });

// 9. Durações >= 24h (formato Nd HHh MMm)
const p24h = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 1440, pauseMaxMinutes: 60 };
results.push({ id: 'pause_24h', sitTxt: fn.situacaoTexto(p24h), sitHtml: fn.situacaoHtml(p24h) });

const p25h5m = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 1505, pauseMaxMinutes: 60 };
results.push({ id: 'pause_25h_5m', sitTxt: fn.situacaoTexto(p25h5m), sitHtml: fn.situacaoHtml(p25h5m) });

const p48h12m = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 2892, pauseMaxMinutes: 60 };
results.push({ id: 'pause_48h_12m', sitTxt: fn.situacaoTexto(p48h12m), sitHtml: fn.situacaoHtml(p48h12m) });

const npx24h = { line: 'npx', status: 1, paused: true, pauseType: 'Almoço', pauseMinutes: 1440, pauseMaxMinutes: 60 };
results.push({ id: 'npx_pause_24h', sitTxt: fn.situacaoTexto(npx24h), sitHtml: fn.situacaoHtml(npx24h) });

// 10. Duração válida mesmo SEM pauseMaxMinutes
const pSemMax = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 101, pauseMaxMinutes: null };
results.push({ id: 'pause_sem_max_minutos', sitTxt: fn.situacaoTexto(pSemMax), sitHtml: fn.situacaoHtml(pSemMax) });

const pSemMax24h = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: 1440, pauseMaxMinutes: undefined };
results.push({ id: 'pause_sem_max_minutos_24h', sitTxt: fn.situacaoTexto(pSemMax24h), sitHtml: fn.situacaoHtml(pSemMax24h) });

// 11. Fallback para pauseStart quando pauseMinutes ausente/nulo
const pFallback = { line: 'pbx', pbxStatus: 'Free', paused: true, pauseType: 'Pausa', pauseMinutes: null, pauseStart: '2026-11-23T14:30:00' };
results.push({ id: 'pause_fallback_start', sitTxt: fn.situacaoTexto(pFallback), sitHtml: fn.situacaoHtml(pFallback) });

const npxFallback = { line: 'npx', status: 1, paused: true, pauseType: 'Lanche', pauseMinutes: null, pauseStart: '2026-11-23T14:30:00' };
results.push({ id: 'npx_pause_fallback_start', sitTxt: fn.situacaoTexto(npxFallback), sitHtml: fn.situacaoHtml(npxFallback) });

results.push({
  id: 'fmt_tests',
  fmt101: fn.fmtMin(101),
  fmt60: fn.fmtMin(60),
  fmt61: fn.fmtMin(61),
  fmt120: fn.fmtMin(120),
  fmt59: fn.fmtMin(59),
  fmt12: fn.fmtMin(12),
  fmt0: fn.fmtMin(0),
  fmtNull: fn.fmtMin(null),
  fmtUndefined: fn.fmtMin(undefined),
  fmtMin24h: fn.fmtMin(1440),
  fmtMin25h5m: fn.fmtMin(1505),
  fmtSeg24h: fn.fmtSeg(86400),
  fmtSeg25h1m2s: fn.fmtSeg(86400 + 3600 + 62),
  fmtSeg2d15h45m: fn.fmtSeg(2 * 86400 + 15 * 3600 + 45 * 60),
  fmtSeg1h41m: fn.fmtSeg(6060),
  fmtSeg1h0m: fn.fmtSeg(3600),
  fmtSeg59s: fn.fmtSeg(59),
  fmtSeg0s: fn.fmtSeg(0)
});

// 12. Telefonia PBX e Máscaras de Telefone (fmtFone)
const c1 = { line: 'pbx', pbxStatus: 'Busy', pbxNumber: '558999180044', pbxDirection: 'inbound', pbxSeconds: 45 };
results.push({ id: 'chamada_pbx_558999180044', sitTxt: fn.situacaoTexto(c1), sitHtml: fn.situacaoHtml(c1) });

const c2 = { line: 'pbx', pbxStatus: 'Busy', pbxNumber: '5588981669885', pbxDirection: 'outbound', pbxSeconds: 70 };
results.push({ id: 'chamada_pbx_5588981669885', sitTxt: fn.situacaoTexto(c2), sitHtml: fn.situacaoHtml(c2) });

const c3 = { line: 'pbx', pbxStatus: 'Ringing', pbxNumber: '2008', pbxDirection: 'inbound', pbxSeconds: 15 };
results.push({ id: 'chamada_pbx_ramal_2008', sitTxt: fn.situacaoTexto(c3), sitHtml: fn.situacaoHtml(c3) });

const c4 = { line: 'pbx', pbxStatus: 'Busy', pbxNumber: '+14155552671', pbxDirection: 'inbound', pbxSeconds: 120 };
results.push({ id: 'chamada_pbx_internacional_mais', sitTxt: fn.situacaoTexto(c4), sitHtml: fn.situacaoHtml(c4) });

results.push({
  id: 'fone_tests',
  imgFixo: fn.fmtFone('558999180044'),
  imgCel: fn.fmtFone('5588981669885'),
  imgFixoMais: fn.fmtFone('+558999180044'),
  imgCelMais: fn.fmtFone('+5588981669885'),
  ddd55Cel: fn.fmtFone('55981669885'),
  ddd55Fixo: fn.fmtFone('5532221234'),
  ddi55Ddd55Cel: fn.fmtFone('5555981669885'),
  ddi55Ddd55Fixo: fn.fmtFone('555532221234'),
  ramal4: fn.fmtFone('2008'),
  ramal3: fn.fmtFone('101'),
  interMaisUs: fn.fmtFone('+14155552671'),
  interMaisPt: fn.fmtFone('+351912345678'),
  interSemMais: fn.fmtFone('141555526710'),
  stdCel: fn.fmtFone('85999999999'),
  stdFixo: fn.fmtFone('8532221100'),
  local9: fn.fmtFone('999998888'),
  local8: fn.fmtFone('33334444'),
  imgZeroCel: fn.fmtFone('088999263130'),
  imgZeroCelMais: fn.fmtFone('+088999263130'),
  zeroFixo: fn.fmtFone('08832221234'),
  vazio: fn.fmtFone(''),
  nulo: fn.fmtFone(null)
});

// 13. Matriz de Elegibilidade logadoNpx
results.push({
  id: 'logado_npx_matrix',
  pbx_loggedin_true: fn.logadoNpx({ line: 'pbx', pbxStatus: 'Free', npxLoggedIn: true }),
  npx_loggedin_true: fn.logadoNpx({ line: 'npx', status: 1, npxLoggedIn: true }),
  off_loggedin_true: fn.logadoNpx({ line: 'off', status: 0, npxLoggedIn: true }),
  pbx_loggedin_false: fn.logadoNpx({ line: 'pbx', pbxStatus: 'Free', npxLoggedIn: false }),
  npx_loggedin_false: fn.logadoNpx({ line: 'npx', status: 1, npxLoggedIn: false }),
  string_truthy: fn.logadoNpx({ line: 'pbx', pbxStatus: 'Free', npxLoggedIn: 'true' }),
  sem_flag_pbx_free: fn.logadoNpx({ line: 'pbx', pbxStatus: 'Free' }),
  sem_flag_pbx_busy: fn.logadoNpx({ line: 'pbx', pbxStatus: 'Busy', pbxNumber: '85999999999' }),
  sem_flag_pbx_sem_fila: fn.logadoNpx({ queue: 'PBX_Sem_Fila', line: 'pbx' }),
  sem_flag_pbx_code: fn.logadoNpx({ code: 'pbx-2008', line: 'pbx' }),
  sem_flag_off: fn.logadoNpx({ line: 'off', status: 0 }),
  sem_flag_npx_livre: fn.logadoNpx({ line: 'npx', status: 1 }),
  sem_flag_npx_lig: fn.logadoNpx({ line: 'npx', status: 2 }),
  sem_flag_npx_cham: fn.logadoNpx({ line: 'npx', status: 5 }),
  sem_flag_npx_pausa: fn.logadoNpx({ line: 'npx', status: 0, paused: true }),
  sem_flag_npx_deslogado: fn.logadoNpx({ line: 'npx', status: 0, paused: false })
});

// 14. Simulação de render() para Contadores do Topo e Cards PBX
function simularRender(dadosEntrada, filasEntrada, waitingCallsEntrada) {
  if (typeof fn.setWaitingCalls === 'function') {
    fn.setWaitingCalls(waitingCallsEntrada !== undefined ? waitingCallsEntrada : null);
  }
  fn.setDados(dadosEntrada);
  fn.setFilas(filasEntrada || [
    { key: 'Fila_Suporte', nome: 'Suporte N1' },
    { key: 'PBX_Sem_Fila', nome: 'Não logado' }
  ]);
  fn.render();
  const elFila = elements['nFila'] || elements['nCham'];
  return {
    nLog: Number(elements['nLog'].textContent),
    nPbx: Number(elements['nPbx'].textContent),
    nLivre: Number(elements['nLivre'].textContent),
    nFila: elFila ? (String(elFila.textContent) === '–' ? null : Number(elFila.textContent)) : null,
    nFilaTxt: elFila ? String(elFila.textContent) : '–',
    nCham: Number(elements['nCham'].textContent),
    nLig: Number(elements['nLig'].textContent),
    nPausa: Number(elements['nPausa'].textContent),
    nPresa: Number(elements['nPresa'].textContent),
    temCardPbx: elements['filas'].innerHTML.includes('data-fila="PBX_Sem_Fila"')
  };
}

// 14.1 Cenário da Imagem: 1 analista PBX-only sem sessão na fila NPX
results.push({
  id: 'topo_cenario_imagem_pbx_sem_fila',
  ...simularRender([
    { code: 'pbx-2008', name: 'Analista 2008', queue: 'PBX_Sem_Fila', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', npxLoggedIn: false }
  ])
});

// 14.2 Cenário PBX com sessão NPX confirmada (inclusive se line pbx)
results.push({
  id: 'topo_cenario_pbx_com_npx_logon',
  ...simularRender([
    { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', npxLoggedIn: true }
  ])
});

// 14.3 Cenário Deduplicação Multi-fila (mesmo código de agente em 2 filas NPX)
results.push({
  id: 'topo_cenario_deduplicacao_multi_fila',
  ...simularRender([
    { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Free', npxLoggedIn: true },
    { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Comercial', line: 'pbx', pbxStatus: 'Free', npxLoggedIn: true }
  ], [
    { key: 'Fila_Suporte', nome: 'Suporte N1' },
    { key: 'Fila_Comercial', nome: 'Comercial' },
    { key: 'PBX_Sem_Fila', nome: 'Não logado' }
  ])
});

// 14.4 Cenário Sem Flag: PBX Free não infere sessão MicroSIP
results.push({
  id: 'topo_cenario_sem_flag_pbx_only',
  ...simularRender([
    { code: 'pbx-2008', name: 'Analista 2008', queue: 'PBX_Sem_Fila', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008' }
  ])
});

// 14.5 Cenário Sem Flag: NPX livre infere dados de sessão da NPX
results.push({
  id: 'topo_cenario_sem_flag_npx_livre',
  ...simularRender([
    { code: '1001', name: 'Analista NPX', queue: 'Fila_Suporte', line: 'npx', status: 1 }
  ])
});

// 14.6 Cenário Misto: 1 NPX livre, 1 PBX+NPX em ligação, 1 PBX-only sem sessão
results.push({
  id: 'topo_cenario_misto',
  ...simularRender([
    { code: '1001', name: 'Analista 1', queue: 'Fila_Suporte', line: 'npx', status: 1, npxLoggedIn: true },
    { code: '4422008', name: 'Analista 2', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Busy', pbxNumber: '85988887777', pbxSeconds: 30, npxLoggedIn: true },
    { code: 'pbx-3001', name: 'Analista 3', queue: 'PBX_Sem_Fila', line: 'pbx', pbxStatus: 'Free', pbxExtension: '3001', npxLoggedIn: false }
  ])
});

// 15. Alerta Visual de Retorno ao Ramal NPX (pbxIdleAlert)
const agAlertaTrue = { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', npxLoggedIn: true, pbxIdleAlert: true };
const agAlertaFalse = { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', npxLoggedIn: true, pbxIdleAlert: false };
const agAlertaUndefined = { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', npxLoggedIn: true };
const agAlertaString = { code: '4422008', name: 'Analista Alfa', queue: 'Fila_Suporte', line: 'pbx', pbxStatus: 'Free', pbxExtension: '2008', npxLoggedIn: true, pbxIdleAlert: 'true' };

results.push({
  id: 'alerta_pbx_idle_tests',
  htmlTrue: fn.linha(agAlertaTrue),
  htmlFalse: fn.linha(agAlertaFalse),
  htmlUndefined: fn.linha(agAlertaUndefined),
  htmlString: fn.linha(agAlertaString)
});

// 16. Direção de Chamadas NPX/PBX com sinal visual + texto e mascaramento 088...
const chNpxSaida = { code: '4422008', extension: '2008', line: 'npx', status: 2, callSrc: '2008', callDst: '088999263130' };
const chNpxEntrada = { code: '4422008', extension: '2008', line: 'npx', status: 2, callSrc: '088999263130', callDst: '2008' };
const chPbxSaida = { line: 'pbx', pbxStatus: 'Busy', pbxNumber: '088999263130', pbxDirection: 'outbound' };
const chPbxEntrada = { line: 'pbx', pbxStatus: 'Busy', pbxNumber: '088999263130', pbxDirection: 'inbound' };

results.push({
  id: 'chamadas_direcao_tests',
  npxSaida: {
    chamada: fn.chamada(chNpxSaida),
    sitHtml: fn.situacaoHtml(chNpxSaida)
  },
  npxEntrada: {
    chamada: fn.chamada(chNpxEntrada),
    sitHtml: fn.situacaoHtml(chNpxEntrada)
  },
  pbxSaida: {
    chamada: fn.chamada(chPbxSaida),
    sitHtml: fn.situacaoHtml(chPbxSaida)
  },
  pbxEntrada: {
    chamada: fn.chamada(chPbxEntrada),
    sitHtml: fn.situacaoHtml(chPbxEntrada)
  }
});

// 17. Diferenciação Recebendo vs Discando vs Chamando durante toque (PBX e NPX)
const pbxToqueOut = { line: 'pbx', pbxStatus: 'Ringing', pbxNumber: '85999999999', pbxDirection: 'outbound' };
const pbxToqueIn = { line: 'pbx', pbxStatus: 'Ringing', pbxNumber: '85999999999', pbxDirection: 'inbound' };
const pbxToqueSemDir = { line: 'pbx', pbxStatus: 'Ringing', pbxNumber: '85999999999' };

const npxToqueOut = { code: '4422008', extension: '2008', line: 'npx', status: 5, callSrc: '2008', callDst: '088999263130' };
const npxToqueIn = { code: '4422008', extension: '2008', line: 'npx', status: 5, callSrc: '088999263130', callDst: '2008' };
const npxToqueOutInferida = { code: '4422008', line: 'npx', status: 5, callSrc: '101', callDst: '088999263130' };
const npxToqueInInferida = { code: '4422008', line: 'npx', status: 5, callSrc: '088999263130', callDst: '101' };
const npxToqueSemDir = { code: '4422008', line: 'npx', status: 5 };

results.push({
  id: 'toque_direcao_detalhada_tests',
  pbxOut: {
    st: fn.stDe(pbxToqueOut),
    ehRecebendo: fn.ehRecebendo(pbxToqueOut),
    ehDiscendo: fn.ehDiscendo(pbxToqueOut),
    ehChamDesc: fn.ehChamDesc(pbxToqueOut),
    sitHtml: fn.situacaoHtml(pbxToqueOut)
  },
  pbxIn: {
    st: fn.stDe(pbxToqueIn),
    ehRecebendo: fn.ehRecebendo(pbxToqueIn),
    ehDiscendo: fn.ehDiscendo(pbxToqueIn),
    ehChamDesc: fn.ehChamDesc(pbxToqueIn),
    sitHtml: fn.situacaoHtml(pbxToqueIn)
  },
  pbxSemDir: {
    st: fn.stDe(pbxToqueSemDir),
    ehRecebendo: fn.ehRecebendo(pbxToqueSemDir),
    ehDiscendo: fn.ehDiscendo(pbxToqueSemDir),
    ehChamDesc: fn.ehChamDesc(pbxToqueSemDir),
    sitHtml: fn.situacaoHtml(pbxToqueSemDir)
  },
  npxOut: {
    st: fn.stDe(npxToqueOut),
    ehRecebendo: fn.ehRecebendo(npxToqueOut),
    ehDiscendo: fn.ehDiscendo(npxToqueOut),
    ehChamDesc: fn.ehChamDesc(npxToqueOut),
    sitHtml: fn.situacaoHtml(npxToqueOut)
  },
  npxIn: {
    st: fn.stDe(npxToqueIn),
    ehRecebendo: fn.ehRecebendo(npxToqueIn),
    ehDiscendo: fn.ehDiscendo(npxToqueIn),
    ehChamDesc: fn.ehChamDesc(npxToqueIn),
    sitHtml: fn.situacaoHtml(npxToqueIn)
  },
  npxOutInferida: {
    st: fn.stDe(npxToqueOutInferida),
    ehRecebendo: fn.ehRecebendo(npxToqueOutInferida),
    ehDiscendo: fn.ehDiscendo(npxToqueOutInferida),
    ehChamDesc: fn.ehChamDesc(npxToqueOutInferida),
    sitHtml: fn.situacaoHtml(npxToqueOutInferida)
  },
  npxInInferida: {
    st: fn.stDe(npxToqueInInferida),
    ehRecebendo: fn.ehRecebendo(npxToqueInInferida),
    ehDiscendo: fn.ehDiscendo(npxToqueInInferida),
    ehChamDesc: fn.ehChamDesc(npxToqueInInferida),
    sitHtml: fn.situacaoHtml(npxToqueInInferida)
  },
  npxSemDir: {
    st: fn.stDe(npxToqueSemDir),
    ehRecebendo: fn.ehRecebendo(npxToqueSemDir),
    ehDiscendo: fn.ehDiscendo(npxToqueSemDir),
    ehChamDesc: fn.ehChamDesc(npxToqueSemDir),
    sitHtml: fn.situacaoHtml(npxToqueSemDir)
  }
});

// 18. Contadores de Grupo no card() separando direções
const grupoAgentes = [
  { code: 'ag1', queue: 'Fila_Teste', line: 'npx', status: 1 }, // livre
  { code: 'ag2', queue: 'Fila_Teste', line: 'npx', status: 2 }, // ligação
  { code: 'ag3', queue: 'Fila_Teste', line: 'npx', status: 0, paused: true, pauseType: 'Lanche' }, // pausa
  { code: 'ag4', queue: 'Fila_Teste', line: 'npx', status: 5, callSrc: '088999263130', callDst: 'ag4', extension: 'ag4' }, // toque recebendo
  { code: 'ag5', queue: 'Fila_Teste', line: 'npx', status: 5, callSrc: 'ag5', callDst: '088999263130', extension: 'ag5' }, // toque discando
  { code: 'ag6', queue: 'Fila_Teste', line: 'npx', status: 5 } // toque sem direção
];
fn.setDados(grupoAgentes);
fn.setFilas([{ key: 'Fila_Teste', nome: 'Fila de Teste' }]);
const cardHtml = fn.card('Fila_Teste', '<th>Agente</th>');

results.push({
  id: 'card_grupo_contadores_tests',
  cardHtml
});

// 19. Card do Topo Fila alimentado por waitingCalls
results.push({
  id: 'waiting_calls_topo_tests',
  com5: simularRender([], [{ key: 'Fila_Suporte', nome: 'Suporte N1' }], 5),
  com0: simularRender([], [{ key: 'Fila_Suporte', nome: 'Suporte N1' }], 0),
  comNull: simularRender([], [{ key: 'Fila_Suporte', nome: 'Suporte N1' }], null),
  comUndefined: simularRender([], [{ key: 'Fila_Suporte', nome: 'Suporte N1' }], undefined)
});

console.log(JSON.stringify(results));
