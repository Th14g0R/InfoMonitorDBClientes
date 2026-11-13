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

console.log(JSON.stringify(results));
