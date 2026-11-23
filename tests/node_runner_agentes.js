const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
let code = fs.readFileSync(jsPath, 'utf8');

// Cria ambiente sandbox simulando o DOM mínimo de agentes.html
const sandbox = {
  document: {
    getElementById: (id) => {
      if (id === 'cfg-agentes') return { textContent: '{"pode":true,"layout":{"cols":2,"c1":[],"c2":[],"ord":{"col":"status","dir":1}}}' };
      return {
        addEventListener: () => {},
        querySelectorAll: () => [],
        setAttribute: () => {},
        textContent: '',
        innerHTML: '',
        style: {}
      };
    },
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

// Instrumenta a IIFE para exportar as funções internas de interesse
code = code.replace(/const stDe = a => \{/, 'const __stDe = a => {');
code = code.replace(/function situacaoTexto\(a\) \{/, 'function __situacaoTexto(a) {');
code = code.replace(/function situacaoHtml\(a\) \{/, 'function __situacaoHtml(a) {');

code = code.replace(/\}\)\(\);?\s*$/, `
  globalThis.__exported = {
    stDe: typeof __stDe !== 'undefined' ? __stDe : (typeof stDe !== 'undefined' ? stDe : null),
    situacaoTexto: typeof __situacaoTexto !== 'undefined' ? __situacaoTexto : (typeof situacaoTexto !== 'undefined' ? situacaoTexto : null),
    situacaoHtml: typeof __situacaoHtml !== 'undefined' ? __situacaoHtml : (typeof situacaoHtml !== 'undefined' ? situacaoHtml : null),
    fmtMin: typeof fmtMin !== 'undefined' ? fmtMin : null,
    fmtSeg: typeof fmtSeg !== 'undefined' ? fmtSeg : null,
    fmtFone: typeof fmtFone !== 'undefined' ? fmtFone : null,
    ehLig: typeof ehLig !== 'undefined' ? ehLig : null,
    ehCham: typeof ehCham !== 'undefined' ? ehCham : null,
    ehPausa: typeof ehPausa !== 'undefined' ? ehPausa : null,
    ehLivre: typeof ehLivre !== 'undefined' ? ehLivre : null,
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

// 3. PBX Ringing + paused
const a3 = { line: 'pbx', pbxStatus: 'Ringing', paused: true, pauseType: 'Almoço' };
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
  vazio: fn.fmtFone(''),
  nulo: fn.fmtFone(null)
});

console.log(JSON.stringify(results));
