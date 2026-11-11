/* Painel de Agentes (NPX). Leitura para todos; ações e layout só para quem tem perm_agentes. */
(function () {
  'use strict';
  const CFG = JSON.parse(document.getElementById('cfg-agentes').textContent);
  const PODE = !!CFG.pode;
  const root = document.documentElement;
  const AG = document.getElementById('ag');
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const ler = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const guardar = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* sem armazenamento */ } };

  // [rótulo, cor, ordem, classe da linha]
  const ST = {
    '-1': ['Em pausa', 'var(--pausa)', 0, 'l-pausa'], '5': ['Recebendo', 'var(--cham)', 1, 'l-cham'],
    '2': ['Em ligação', 'var(--lig)', 2, 'l-lig'], '1': ['Livre', 'var(--livre)', 3, 'l-livre'], '0': ['Offline', 'var(--off)', 4, 'l-off'],
  };
  const ST_PBX = {
    'Ringing': ['Recebendo', 'var(--pbx-cham)', 1, 'l-pbx-cham'],
    'Busy': ['Em ligação', 'var(--pbx-lig)', 2, 'l-pbx-lig'],
    'Free': ['Livre', 'var(--pbx-livre)', 3, 'l-pbx-livre'],
    'Offline': ['Offline', 'var(--off)', 4, 'l-pbx-off'],
  };
  const COLS = [['name', 'Agente'], ['status', 'Status'], ['tempo', 'Tempo'], ['penalty', 'Prior.'], ['situacao', 'Situação']];
  const GRUPO = '<colgroup><col class="c-ag"><col class="c-st"><col class="c-tp"><col class="c-pr"><col class="c-si"><col class="c-ac"></colgroup>';

  let dados = [], filas = [], carregando = false, sel = null, arrastando = null, alvo = null, painelConfigurado = true;
  let layout = CFG.layout || {cols: 2, c1: [], c2: [], ord: {col: 'status', dir: 1}};
  let timerSalvar = null;

  // ---------- utilidades ----------
  const stDe = a => {
    if (a.line === 'pbx') {
      return ST_PBX[String(a.pbxStatus || '')] || [a.pbxStatus || a.erro || 'Desconhecido', 'var(--off)', 5, 'l-pbx-off'];
    }
    if (a.line === 'off') {
      return ST['0'];
    }
    return a.paused ? ST['-1'] : (ST[String(a.status)] || [a.erro || 'Desconhecido', 'var(--off)', 5, 'l-off']);
  };
  const segs = a => a.line === 'pbx' ? (a.pbxSeconds || 0) : (a.paused ? (a.pauseMinutes != null ? a.pauseMinutes * 60 : (a.seconds || 0)) : (a.seconds || 0));
  const ehLivre = a => a.line === 'pbx' ? (a.pbxStatus === 'Free') : (!a.paused && a.status === 1);
  const ehCham = a => a.line === 'pbx' ? (a.pbxStatus === 'Ringing') : (!a.paused && a.status === 5);
  const ehLig = a => a.line === 'pbx' ? (a.pbxStatus === 'Busy') : (!a.paused && a.status === 2);
  const ehPausa = a => (!a.line || a.line === 'npx') && !!a.paused;

  function fmtSeg(t) {
    t = Math.round(t || 0); const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
    return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s).padStart(2, '0')}s`;
  }
  function fmtHora(iso) { if (!iso) return ''; const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'}); }
  function fmtFone(n) {
    const d = String(n || '').replace(/\D/g, '');
    if (d.length === 11) return `(${d.slice(0, 2)}) ${d.slice(2, 7)}-${d.slice(7)}`;
    if (d.length === 10) return `(${d.slice(0, 2)}) ${d.slice(2, 6)}-${d.slice(6)}`;
    if (d.length === 9) return `${d.slice(0, 5)}-${d.slice(5)}`;
    if (d.length === 8) return `${d.slice(0, 4)}-${d.slice(4)}`;
    return n || '';
  }
  const fila = k => filas.find(f => f.key === k) || {key: k, nome: k, temId: false};
  const maxValido = m => m != null && Number.isFinite(Number(m)) && Number(m) >= 0;
  const estourada = a => {
    if (!a.paused || !maxValido(a.pauseMaxMinutes) || a.pauseMinutes == null) return false;
    const min = Number(a.pauseMinutes);
    return Number.isFinite(min) && min > Number(a.pauseMaxMinutes);
  };
  const duasColunas = () => Number(layout.cols) === 2 && innerWidth > 900;
  const soPausa = () => PODE && $('soPausa') && $('soPausa').checked;

  function chamada(a) {
    if (a.line === 'pbx') {
      const num = a.pbxNumber || '';
      const dir = a.pbxDirection === 'outbound' ? 'para' : 'de';
      return (num || a.pbxStatus === 'Busy' || a.pbxStatus === 'Ringing') ? {num, dir} : null;
    }
    const num = a.callSrc || a.callDst || a.callClient || '';
    return num ? {num, dir: a.callDst && !a.callSrc ? 'para' : 'de'} : null;
  }
  function situacaoTexto(a) {
    if (a.line === 'pbx') {
      const dur = a.pbxSeconds ? ` (${fmtSeg(a.pbxSeconds)})` : '';
      if (a.pbxStatus === 'Busy' || a.pbxStatus === 'Ringing') {
        const num = a.pbxNumber ? fmtFone(a.pbxNumber) : (a.pbxStatus === 'Ringing' ? 'Chamando' : 'Em ligação');
        return `${num}${dur}`;
      }
      if (a.pbxStatus === 'Free') return 'Livre';
      return a.pbxStatus || '';
    }
    if (a.paused) {
      const tipo = a.pauseType || 'Pausa';
      if (maxValido(a.pauseMaxMinutes) && a.pauseMinutes != null && Number.isFinite(Number(a.pauseMinutes))) {
        return `${tipo} · ${Math.round(a.pauseMinutes)}m de ${Math.round(a.pauseMaxMinutes)}m`;
      }
      return `${tipo}${a.pauseStart ? ` desde ${fmtHora(a.pauseStart)}` : ''}`;
    }
    const c = chamada(a); return c ? c.num : '';
  }
  function situacaoHtml(a) {
    if (a.line === 'pbx') {
      const dur = a.pbxSeconds != null && a.pbxSeconds > 0 ? ` <span class="mut">(${fmtSeg(a.pbxSeconds)})</span>` : '';
      if (a.pbxStatus === 'Busy' || a.pbxStatus === 'Ringing') {
        const c = chamada(a);
        if (c && c.num) {
          return `📞 <span class="mut">${c.dir}</span> <span class="fone">${esc(fmtFone(c.num))}</span>${dur}`;
        }
        return `<span class="mut">${a.pbxStatus === 'Ringing' ? 'Chamando' : 'Em ligação'}</span>${dur}`;
      }
      if (a.pbxStatus === 'Free') {
        return '<span class="mut">Livre</span>';
      }
      return '<span class="mut">—</span>';
    }
    if (a.paused) {
      const tipo = esc(a.pauseType || 'Pausa');
      if (maxValido(a.pauseMaxMinutes) && a.pauseMinutes != null && Number.isFinite(Number(a.pauseMinutes))) {
        return `${tipo} <span class="mut">· ${Math.round(a.pauseMinutes)}m de ${Math.round(a.pauseMaxMinutes)}m</span>`;
      }
      return `${tipo}${a.pauseStart ? ` <span class="mut">desde ${fmtHora(a.pauseStart)}</span>` : ''}`;
    }
    if (a.status === 2 || a.status === 5) {
      const c = chamada(a);
      if (!c) return `<span class="mut">${a.status === 5 ? 'Chamando' : 'Em ligação'}</span>`;
      return `📞 <span class="mut">${c.dir}</span> <span class="fone">${esc(fmtFone(c.num))}</span>`;
    }
    return '<span class="mut">—</span>';
  }
  function chave(a, col) {
    switch (col) {
      case 'name': return String(a.name).toLowerCase();
      case 'status': return stDe(a)[2] * 1e7 - segs(a);
      case 'tempo': return segs(a);
      case 'penalty': return a.penalty == null ? 999 : Number(a.penalty);
      case 'situacao': return situacaoTexto(a).toLowerCase() || '~';
    }
    return 0;
  }
  function ordenar(lista) {
    const o = layout.ord || {col: 'status', dir: 1};
    return lista.slice().sort((x, y) => {
      const a = chave(x, o.col), b = chave(y, o.col);
      return ((a < b ? -1 : a > b ? 1 : 0) * o.dir) || String(x.name).localeCompare(y.name);
    });
  }
  // distribui as filas nas colunas: o que foi arrumado fica; filas novas vão para a coluna 2
  function arrumacao() {
    const todas = filas.map(f => f.key);
    dados.forEach(a => { if (a.queue && !todas.includes(a.queue)) todas.push(a.queue); });
    const c1 = (layout.c1 || []).filter(k => todas.includes(k));
    const c2 = (layout.c2 || []).filter(k => todas.includes(k) && !c1.includes(k));
    todas.forEach(k => { if (!c1.includes(k) && !c2.includes(k)) c2.push(k); });
    return {c1, c2};
  }

  // ---------- desenho ----------
  function linha(a) {
    const st = stDe(a), est = estourada(a);
    const nome = PODE ? `<button type="button" class="nome" data-abrir="${esc(a.code)}" data-fila="${esc(a.queue)}">${esc(a.name)}</button>`
                      : `<span class="nome">${esc(a.name)}</span>`;
    const btn = PODE && a.paused ? `<button type="button" class="${est ? 'btn-pausa' : ''}" data-tirar="${esc(a.code)}" data-nome="${esc(a.name)}">Tirar pausa</button>` : '';
    const tagLinha = a.line === 'pbx' ? '<span class="tag-linha tag-pbx">PBX</span>'
                   : (a.line === 'npx' || (!a.line && a.status != null) ? '<span class="tag-linha tag-npx">NPX</span>' : '');

    const sub = a.line === 'pbx'
      ? `${esc(a.code)}${a.pbxExtension ? ' · ramal PBX ' + esc(a.pbxExtension) : (a.extension && a.extension != a.code ? ' · ramal ' + esc(a.extension) : '')}`
      : `${esc(a.code)}${a.extension && a.extension != a.code ? ' · ramal ' + esc(a.extension) : ''}`;

    const tempoTxt = a.line === 'pbx'
      ? (a.pbxSeconds == null ? '–' : fmtSeg(a.pbxSeconds))
      : (a.seconds == null && !a.paused ? '–' : fmtSeg(segs(a)));

    return `<tr class="${st[3]}${est ? ' estourada presa' : ''}" style="--c:${st[1]}">
      <td>${nome}<br><span class="mut">${sub}</span></td>
      <td><span class="pill" style="--c:${st[1]}">${esc(st[0])}</span>${tagLinha}</td>
      <td class="tempo">${tempoTxt}</td>
      <td>${esc(a.penalty ?? '–')}</td>
      <td>${situacaoHtml(a)}</td><td>${btn}</td></tr>`;
  }
  function card(k, cab) {
    const todos = dados.filter(a => a.queue === k);
    const lista = ordenar(todos.filter(a => !soPausa() || a.paused));
    const n = f => todos.filter(f).length;
    const cont = `<div class="cont">
        <span style="color:var(--livre)">${n(ehLivre)} livres</span>
        <span style="color:var(--cham)">${n(ehCham)} recebendo</span>
        <span style="color:var(--lig)">${n(ehLig)} em ligação</span>
        <span style="color:var(--pausa)">${n(ehPausa)} em pausa</span></div>`;
    const corpo = lista.length
      ? `<div class="wrap"><table>${GRUPO}<thead><tr>${cab}</tr></thead><tbody>${lista.map(linha).join('')}</tbody></table></div>`
      : `<div class="vazio mut">${todos.length ? 'Ninguém em pausa nesta fila.' : 'Nenhum agente logado.'}</div>`;
    const alca = PODE ? '<span class="alca" draggable="true" title="Arraste para mudar a posição">⠿</span>' : '';
    return `<section class="fila" data-fila="${esc(k)}"><div class="fila-h">${alca}<h2>${esc(fila(k).nome)}</h2>
      <span class="mut">${todos.length} agente(s)</span>${cont}</div>${corpo}</section>`;
  }
  function render() {
    if (arrastando) return;   // não redesenha no meio de um arraste
    const u = [...new Map(dados.map(a => [a.code, a])).values()];
    const conta = f => u.filter(f).length;
    $('nLog').textContent = u.length;
    $('nLivre').textContent = conta(ehLivre);
    $('nCham').textContent = conta(ehCham);
    $('nLig').textContent = conta(ehLig);
    $('nPausa').textContent = conta(ehPausa);
    $('nPresa').textContent = conta(estourada);
    const o = layout.ord || {};
    const cab = COLS.map(([c, t]) => `<th ${PODE ? `data-col="${c}"` : ''} title="${c === 'penalty' ? 'Prioridade (1 = atende primeiro)' : t}" class="${o.col === c ? 'ativo' : ''}">${t}<span class="seta">${o.col === c ? (o.dir > 0 ? '▲' : '▼') : '↕'}</span></th>`).join('') + '<th></th>';
    const {c1, c2} = arrumacao();
    const dropFim = PODE ? '<div class="drop-fim" title="Solte aqui para mover para o fim da coluna"></div>' : '';
    const coluna = (n, ks) => `<div class="col" data-coluna="${n}">${ks.map(k => card(k, cab)).join('') ||
      (PODE ? '<div class="col-vazia">Arraste um grupo para cá</div>' : '')}${ks.length && PODE ? dropFim : ''}</div>`;
    const box = $('filas');
    AG.classList.toggle('duas-colunas', duasColunas());
    if (duasColunas()) { box.classList.add('duas'); box.innerHTML = coluna(1, c1) + coluna(2, c2); }
    else { box.classList.remove('duas'); box.innerHTML = coluna(1, c1.concat(c2)); }
  }
  function marcar(seg, v) { const s = $(seg); if (s) s.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === String(v)))); }

  // ---------- dados ----------
  let primeiraCarga = true;
  async function carregar() {
    if (carregando) return; carregando = true;
    try {
      const r = await fetch('/agentes/dados', {cache: 'no-store', headers: {Accept: 'application/json'}});
      const j = await r.json();
      if (!r.ok) throw new Error(j.erro || r.status);
      dados = j.agentes || []; filas = j.filas || [];
      painelConfigurado = j.painelConfigurado !== false;
      const pbxConn = j.pbxConnection || null;
      const avisoPbx = $('avisoPbx');
      if (avisoPbx) {
        if (pbxConn && pbxConn.configured && !pbxConn.available) {
          avisoPbx.style.display = '';
          const detalhe = pbxConn.error ? `: ${esc(pbxConn.error)}` : '';
          avisoPbx.textContent = `ℹ Telefonia PBX indisponível no momento${detalhe}`;
        } else {
          avisoPbx.style.display = 'none';
          avisoPbx.textContent = '';
        }
      }
      // quem só visualiza segue sempre o layout do administrador; quem edita só recebe na abertura
      if (!PODE || primeiraCarga) { layout = j.layout || layout; aplicarControles(); }
      primeiraCarga = false;
      $('hora').textContent = 'Atualizado às ' + new Date(j.atualizado || Date.now()).toLocaleTimeString('pt-BR');
      $('msg').textContent = j.erroNpx ? '⚠ Última consulta à NPX falhou: ' + j.erroNpx : '';
      render();
      if (sel && $('dlg') && $('dlg').open) atualizarDialogo();
    } catch (e) {
      $('msg').textContent = 'Erro ao consultar a NPX: ' + e.message;
    } finally { carregando = false; }
  }
  function aplicarControles() {
    marcar('segCols', layout.cols);
  }
  function salvarLayout() {
    if (!PODE) return;
    clearTimeout(timerSalvar);
    timerSalvar = setTimeout(async () => {
      try {
        const r = await csrfFetch('/admin/agentes/layout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(layout)});
        const j = await r.json();
        if (!j.ok) $('msg').textContent = '✖ Layout não salvo: ' + (j.mensagem || j.erro || r.status);
      } catch (e) { $('msg').textContent = '✖ Layout não salvo: ' + e.message; }
    }, 700);
  }

  // ---------- ações ----------
  async function post(url, corpo) {
    const r = await csrfFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corpo || {})});
    let j; try { j = await r.json(); } catch (e) { j = {ok: false, mensagem: 'HTTP ' + r.status}; }
    if (r.status === 401) j = {ok: false, mensagem: 'Sua sessão expirou. Entre novamente.'};
    return j;
  }
  function resultado(j, okTxt, el) {
    el.textContent = j.ok ? '✔ ' + okTxt : '✖ ' + (j.mensagem || j.erro || 'Falhou.');
    el.style.color = j.ok ? 'var(--livre)' : 'var(--lig)';
  }
  async function acao(btn, fn, el) {
    const alvoMsg = el || $('dlgMsg') || $('msg');
    if (btn) btn.disabled = true;
    try { await fn(alvoMsg); } catch (e) { if (alvoMsg) alvoMsg.textContent = 'Erro: ' + e.message; }
    finally { if (btn) btn.disabled = false; setTimeout(carregar, 1200); }
  }
  function tirar(code, nome, el, btn) {
    if (!confirm(`Tirar ${nome} (${code}) da pausa?`)) return;
    acao(btn, async (alvoMsg) => {
      resultado(await post(`/admin/agentes/${encodeURIComponent(code)}/tirar-pausa`), `${nome} saiu da pausa.`, alvoMsg);
    }, el);
  }
  function colocarPausa(code, nome, el, btn) {
    if (!confirm(`Colocar ${nome} (${code}) em pausa?`)) return;
    acao(btn, async (alvoMsg) => {
      resultado(await post(`/admin/agentes/${encodeURIComponent(code)}/colocar-pausa`), `${nome} foi colocado em pausa.`, alvoMsg);
    }, el);
  }
  function abrir(code, f) { sel = {code, fila: f}; $('dlgMsg').textContent = ''; atualizarDialogo(true); $('dlg').showModal(); }
  function atualizarDialogo(inicial) {
    const a = dados.find(x => x.code === sel.code && x.queue === sel.fila) || dados.find(x => x.code === sel.code);
    if (!a) { $('dInfo').textContent = 'Agente não está mais logado.'; return; }
    sel.agente = a;
    const st = stDe(a), c = chamada(a);
    const ehPbx = a.line === 'pbx';
    const tagLinha = ehPbx ? ' <span class="tag-linha tag-pbx">PBX</span>'
                           : (a.line === 'npx' || (!a.line && a.status != null) ? ' <span class="tag-linha tag-npx">NPX</span>' : '');

    const subRamal = ehPbx
      ? `Código ${esc(a.code)}${a.pbxExtension ? ' · ramal PBX ' + esc(a.pbxExtension) : (a.extension && a.extension != a.code ? ' · ramal ' + esc(a.extension) : '')} · fila <b>${esc(fila(a.queue).nome)}</b>`
      : `Código ${esc(a.code)} · ramal ${esc(a.extension || a.code)} · fila <b>${esc(fila(a.queue).nome)}</b>`;

    let callTxt = '';
    if (ehPbx) {
      if (c && c.num) {
        callTxt = ` 📞 ${c.dir} <b>${esc(fmtFone(c.num))}</b>${a.pbxSeconds ? ` (${fmtSeg(a.pbxSeconds)})` : ''}`;
      }
    } else {
      if (c && !a.paused && c.num) {
        callTxt = ` 📞 ${c.dir} <b>${esc(fmtFone(c.num))}</b>`;
      }
    }

    $('dNome').innerHTML = `${esc(a.name)}${tagLinha}`;
    $('dInfo').innerHTML = `${subRamal}<br>
      <span class="pill" style="--c:${st[1]}">${esc(st[0])}</span>${callTxt}`;
    $('aPausa').style.display = '';

    if (ehPbx) {
      $('bTirar').style.display = 'none';
      $('bPausar').style.display = '';
      $('bPausar').disabled = true;
      $('bPausar').title = 'Ações de pausa são exclusivas da integração NPX.';
      $('dPausa').textContent = 'Agente em linha PBX (pausa não disponível)';
    } else if (a.paused) {
      $('bTirar').style.display = '';
      $('bPausar').style.display = 'none';
      $('bTirar').title = '';
      $('dPausa').textContent = `${a.pauseType || 'Pausa'} há ${fmtSeg(segs(a))}`;
    } else {
      $('bTirar').style.display = 'none';
      $('bPausar').style.display = '';
      $('bPausar').disabled = !painelConfigurado;
      $('bPausar').title = '';
      $('dPausa').textContent = 'Agente ativo / fora de pausa';
    }

    const destinos = filas.filter(f => f.key !== a.queue && f.key !== 'PBX_Sem_Fila');
    if (inicial) {
      $('dFila').innerHTML = destinos.length
        ? destinos.map(f => `<option value="${esc(f.key)}">${esc(f.nome)}</option>`).join('')
        : '<option value="" disabled>Nenhuma outra fila disponível</option>';
      const penAtual = Number(a.penalty);
      $('dPen').value = Number.isInteger(penAtual) && penAtual >= 1 && penAtual <= 10 ? penAtual : 1;
    }

    // Recalcula elegibilidade das ações a cada atualização
    if (ehPbx) {
      $('bTransf').disabled = true;
      $('bTransf').title = 'Transferência de fila é exclusiva para agentes NPX.';
      $('bPen').disabled = true;
      $('bPen').title = 'Prioridade de fila é exclusiva para agentes NPX.';
      $('bRemoverFila').disabled = true;
      $('bRemoverFila').title = 'Remoção de fila é exclusiva para agentes NPX.';
      $('bDeslogar').disabled = true;
      $('bDeslogar').title = 'Deslogar agente é exclusivo para agentes NPX.';
      $('dlgMsg').textContent = 'ℹ Agente conectado via PBX. Ações de fila e pausa são gerenciadas pelo NPX.';
      $('dlgMsg').style.color = 'var(--mut)';
    } else {
      $('bTransf').disabled = !painelConfigurado || !destinos.length;
      $('bTransf').title = '';
      $('bPen').disabled = !painelConfigurado;
      $('bPen').title = '';
      $('bRemoverFila').disabled = !painelConfigurado;
      $('bRemoverFila').title = '';
      $('bDeslogar').disabled = false;
      $('bDeslogar').title = '';
      if (!painelConfigurado) {
        $('dlgMsg').textContent = 'Ações do painel aguardando configuração de credenciais no servidor.';
        $('dlgMsg').style.color = 'var(--mut)';
      } else if ($('dlgMsg').textContent.startsWith('ℹ Agente conectado via PBX')) {
        $('dlgMsg').textContent = '';
      }
    }
  }
  if (PODE) {
    $('bTirar').onclick = e => tirar(sel.agente.code, sel.agente.name, $('dlgMsg'), e.currentTarget);
    $('bPausar').onclick = e => colocarPausa(sel.agente.code, sel.agente.name, $('dlgMsg'), e.currentTarget);
    $('bTransf').onclick = e => acao(e.currentTarget, async (alvoMsg) => {
      const a = sel.agente, para = $('dFila').value; if (!para || para === 'PBX_Sem_Fila') return;
      if (!confirm(`Transferir ${a.name} de ${fila(a.queue).nome} para ${fila(para).nome}?`)) return;
      const j = await post('/admin/agentes/transferir', {code: a.code, de: a.queue, para: para, extension: a.extension});
      resultado(j, `${a.name} agora está em ${fila(para).nome}.`, alvoMsg);
      if (j.ok) sel.fila = para;
    });
    $('bPen').onclick = e => acao(e.currentTarget, async (alvoMsg) => {
      const a = sel.agente, pen = Number($('dPen').value);
      if (!Number.isInteger(pen) || pen < 1 || pen > 10) {
        alvoMsg.textContent = 'Prioridade deve ser um número inteiro entre 1 e 10.';
        alvoMsg.style.color = 'var(--lig)';
        return;
      }
      if (!confirm(`Alterar prioridade de ${a.name} para ${pen}?`)) return;
      const j = await post('/admin/agentes/prioridade', {code: a.code, fila: a.queue, penalty: pen});
      resultado(j, `Prioridade de ${a.name} agora é ${pen}.`, alvoMsg);
    });
    $('bRemoverFila').onclick = e => acao(e.currentTarget, async (alvoMsg) => {
      const a = sel.agente;
      if (!confirm(`ATENÇÃO: Remover ${a.name} (${a.code}) da fila ${fila(a.queue).nome}? Ele deixará de receber chamadas desta fila.`)) return;
      const j = await post(`/admin/agentes/${encodeURIComponent(a.code)}/remover-fila`, {fila: a.queue});
      resultado(j, `${a.name} foi removido da fila ${fila(a.queue).nome}.`, alvoMsg);
    });
    $('bDeslogar').onclick = e => acao(e.currentTarget, async (alvoMsg) => {
      const a = sel.agente;
      if (!confirm(`Deslogar ${a.name} (${a.code})? Ele sai das filas e para de receber ligações.`)) return;
      resultado(await post(`/admin/agentes/${encodeURIComponent(a.code)}/deslogar`), `${a.name} foi deslogado.`, alvoMsg);
    });
    $('bFechar').onclick = () => $('dlg').close();
    $('dlg').addEventListener('close', () => { sel = null; });
  }

  // ---------- arrastar grupos (só quem gerencia) ----------
  const box = $('filas');
  if (PODE) {
    const limparAlvos = () => box.querySelectorAll('.alvo-antes,.alvo-depois,.alvo-fim').forEach(x => x.classList.remove('alvo-antes', 'alvo-depois', 'alvo-fim'));
    box.addEventListener('dragstart', e => {
      const s = e.target.closest('.alca') && e.target.closest('section.fila'); if (!s) return;
      arrastando = s.dataset.fila; s.classList.add('arrastando');
      AG.classList.add('arrastando');
      e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', arrastando);
      try { e.dataTransfer.setDragImage(s, 20, 20); } catch (_) { /* navegador sem suporte */ }
    });
    box.addEventListener('dragover', e => {
      if (!arrastando) return;
      const col = e.target.closest('.col'); if (!col) return;
      e.preventDefault(); limparAlvos();
      const s = e.target.closest('section.fila');

      // sobre o próprio grupo que está sendo arrastado: limpa alvo para não reter posição anterior
      if (s && s.dataset.fila === arrastando) {
        alvo = null;
        return;
      }

      if (s) {
        const r = s.getBoundingClientRect(), antes = e.clientY < r.top + r.height / 2;
        s.classList.add(antes ? 'alvo-antes' : 'alvo-depois');
        let prox = s;
        if (!antes) {
          prox = s.nextElementSibling;
          while (prox && prox.dataset && prox.dataset.fila === arrastando) {
            prox = prox.nextElementSibling;
          }
        }
        alvo = {coluna: col.dataset.coluna, antesDe: prox && prox.dataset ? prox.dataset.fila || null : null};
        return;
      }

      // cursor em gap entre cards ou na dropzone final
      const cards = [...col.querySelectorAll('section.fila:not(.arrastando)')];
      if (!cards.length) {
        col.classList.add('alvo-fim'); alvo = {coluna: col.dataset.coluna, antesDe: null};
        return;
      }
      const ultimoCard = cards[cards.length - 1], rUltimo = ultimoCard.getBoundingClientRect();
      if (e.clientY >= rUltimo.bottom) {
        col.classList.add('alvo-fim'); alvo = {coluna: col.dataset.coluna, antesDe: null};
        return;
      }
      const primeiroCard = cards[0], rPrimeiro = primeiroCard.getBoundingClientRect();
      if (e.clientY <= rPrimeiro.top) {
        primeiroCard.classList.add('alvo-antes');
        alvo = {coluna: col.dataset.coluna, antesDe: primeiroCard.dataset.fila};
        return;
      }
      // no gap entre cards: seleciona o vizinho mais próximo por clientY
      let maisProx = cards[0], menorDist = Math.abs(e.clientY - (rPrimeiro.top + rPrimeiro.height / 2));
      for (let i = 1; i < cards.length; i++) {
        const rc = cards[i].getBoundingClientRect(), d = Math.abs(e.clientY - (rc.top + rc.height / 2));
        if (d < menorDist) { menorDist = d; maisProx = cards[i]; }
      }
      const rm = maisProx.getBoundingClientRect(), antes = e.clientY < rm.top + rm.height / 2;
      maisProx.classList.add(antes ? 'alvo-antes' : 'alvo-depois');
      let prox = maisProx;
      if (!antes) {
        prox = maisProx.nextElementSibling;
        while (prox && prox.dataset && prox.dataset.fila === arrastando) {
          prox = prox.nextElementSibling;
        }
      }
      alvo = {coluna: col.dataset.coluna, antesDe: prox && prox.dataset ? prox.dataset.fila || null : null};
    });
    box.addEventListener('drop', e => {
      if (!arrastando || !alvo) return; e.preventDefault();
      const {c1, c2} = arrumacao();
      let n1 = c1.filter(k => k !== arrastando), n2 = c2.filter(k => k !== arrastando);
      if (!duasColunas()) {   // uma coluna: reordena a sequência visual mantendo separação c1/c2
        const seq = n1.concat(n2), i = alvo.antesDe ? seq.indexOf(alvo.antesDe) : seq.length;
        seq.splice(i < 0 ? seq.length : i, 0, arrastando);
        if (alvo.antesDe === null && n2.length > 0) {
          // ao soltar no drop-fim da coluna única, posiciona no fim de c2 para ser o último visualmente
          n1 = seq.filter(k => c1.includes(k) && k !== arrastando);
          n2 = seq.filter(k => c2.includes(k) || k === arrastando);
        } else {
          n1 = seq.filter(k => c1.includes(k)); n2 = seq.filter(k => !c1.includes(k));
        }
      } else {
        const lista = alvo.coluna === '1' ? n1 : n2, i = alvo.antesDe ? lista.indexOf(alvo.antesDe) : lista.length;
        lista.splice(i < 0 ? lista.length : i, 0, arrastando);
      }
      layout = {...layout, c1: n1, c2: n2}; salvarLayout();
    });
    box.addEventListener('dragend', () => { arrastando = null; alvo = null; AG.classList.remove('arrastando'); limparAlvos(); render(); });
  }

  // ---------- controles ----------
  box.addEventListener('click', e => {
    if (!PODE) return;
    const th = e.target.closest('th[data-col]');
    if (th) {
      const c = th.dataset.col, o = layout.ord || {};
      layout = {...layout, ord: {col: c, dir: o.col === c ? -o.dir : 1}}; render(); salvarLayout(); return;
    }
    const t = e.target.closest('button[data-tirar]'); if (t) { tirar(t.dataset.tirar, t.dataset.nome, $('msg'), t); return; }
    const n = e.target.closest('button[data-abrir]'); if (n) abrir(n.dataset.abrir, n.dataset.fila);
  });
  if (PODE) {
    $('segCols').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; layout = {...layout, cols: Number(b.dataset.v)}; marcar('segCols', layout.cols); render(); salvarLayout(); });
    $('soPausa').checked = ler('agentes-soPausa') === '1';
    $('soPausa').addEventListener('change', () => { guardar('agentes-soPausa', $('soPausa').checked ? '1' : '0'); render(); });
  }
  // tela cheia (F11 ou fullscreenchange): esconde menu e controles
  function checaTelaCheia() {
    const cheia = !!document.fullscreenElement || matchMedia('(display-mode: fullscreen)').matches ||
                  (innerHeight >= screen.height - 2 && innerWidth >= screen.width - 2);
    root.classList.toggle('tela-cheia', cheia);
  }
  document.addEventListener('fullscreenchange', checaTelaCheia);
  let largAnterior = innerWidth;
  addEventListener('resize', () => { checaTelaCheia(); if ((largAnterior > 900) !== (innerWidth > 900)) render(); largAnterior = innerWidth; });
  checaTelaCheia();
  aplicarControles();
  if (typeof window !== 'undefined') {
    window._testAgentes = { carregar, atualizarDialogo, getDados: () => dados };
  }

  carregar();
  setInterval(carregar, 5000);
})();
