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
  const COLS = [['name', 'Agente'], ['status', 'Status'], ['tempo', 'Tempo'], ['penalty', 'Prior.'], ['situacao', 'Situação']];
  const GRUPO = '<colgroup><col class="c-ag"><col class="c-st"><col class="c-tp"><col class="c-pr"><col class="c-si"><col class="c-ac"></colgroup>';

  let dados = [], filas = [], carregando = false, sel = null, arrastando = null, alvo = null, painelConfigurado = true;
  let layout = CFG.layout || {cols: 2, c1: [], c2: [], ord: {col: 'status', dir: 1}};
  let timerSalvar = null;

  // ---------- utilidades ----------
  const stDe = a => a.paused ? ST['-1'] : (ST[String(a.status)] || [a.erro || 'Desconhecido', 'var(--off)', 5, 'l-off']);
  const segs = a => a.paused ? (a.pauseMinutes != null ? a.pauseMinutes * 60 : (a.seconds || 0)) : (a.seconds || 0);
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
    const num = a.callSrc || a.callDst || a.callClient || '';
    return num ? {num, dir: a.callDst && !a.callSrc ? 'para' : 'de'} : null;
  }
  function situacaoTexto(a) {
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
    return `<tr class="${st[3]}${est ? ' estourada presa' : ''}" style="--c:${st[1]}">
      <td>${nome}<br><span class="mut">${esc(a.code)}${a.extension && a.extension != a.code ? ' · ramal ' + esc(a.extension) : ''}</span></td>
      <td><span class="pill" style="--c:${st[1]}">${esc(st[0])}</span></td>
      <td class="tempo">${a.seconds == null && !a.paused ? '–' : fmtSeg(segs(a))}</td>
      <td>${esc(a.penalty ?? '–')}</td>
      <td>${situacaoHtml(a)}</td><td>${btn}</td></tr>`;
  }
  function card(k, cab) {
    const todos = dados.filter(a => a.queue === k);
    const lista = ordenar(todos.filter(a => !soPausa() || a.paused));
    const n = f => todos.filter(f).length;
    const cont = `<div class="cont">
        <span style="color:var(--livre)">${n(a => !a.paused && a.status === 1)} livres</span>
        <span style="color:var(--cham)">${n(a => !a.paused && a.status === 5)} recebendo</span>
        <span style="color:var(--lig)">${n(a => !a.paused && a.status === 2)} em ligação</span>
        <span style="color:var(--pausa)">${n(a => a.paused)} em pausa</span></div>`;
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
    $('nLivre').textContent = conta(a => !a.paused && a.status === 1);
    $('nCham').textContent = conta(a => !a.paused && a.status === 5);
    $('nLig').textContent = conta(a => !a.paused && a.status === 2);
    $('nPausa').textContent = conta(a => a.paused);
    $('nPresa').textContent = conta(estourada);
    const o = layout.ord || {};
    const cab = COLS.map(([c, t]) => `<th ${PODE ? `data-col="${c}"` : ''} title="${c === 'penalty' ? 'Prioridade (1 = atende primeiro)' : t}" class="${o.col === c ? 'ativo' : ''}">${t}<span class="seta">${o.col === c ? (o.dir > 0 ? '▲' : '▼') : '↕'}</span></th>`).join('') + '<th></th>';
    const {c1, c2} = arrumacao();
    const coluna = (n, ks) => `<div class="col" data-coluna="${n}">${ks.map(k => card(k, cab)).join('') ||
      (PODE ? '<div class="col-vazia">Arraste um grupo para cá</div>' : '')}</div>`;
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
    $('dNome').textContent = a.name;
    $('dInfo').innerHTML = `Código ${esc(a.code)} · ramal ${esc(a.extension || a.code)} · fila <b>${esc(fila(a.queue).nome)}</b><br>
      <span class="pill" style="--c:${st[1]}">${esc(st[0])}</span>${c && !a.paused ? ` 📞 ${c.dir} <b>${esc(fmtFone(c.num))}</b>` : ''}`;
    $('aPausa').style.display = '';
    if (a.paused) {
      $('bTirar').style.display = '';
      $('bPausar').style.display = 'none';
      $('dPausa').textContent = `${a.pauseType || 'Pausa'} há ${fmtSeg(segs(a))}`;
    } else {
      $('bTirar').style.display = 'none';
      $('bPausar').style.display = '';
      $('bPausar').disabled = !painelConfigurado;
      $('dPausa').textContent = 'Agente ativo / fora de pausa';
    }
    if (inicial) {
      const destinos = filas.filter(f => f.key !== a.queue);
      $('dFila').innerHTML = destinos.length
        ? destinos.map(f => `<option value="${esc(f.key)}">${esc(f.nome)}</option>`).join('')
        : '<option value="" disabled>Nenhuma outra fila disponível</option>';
      $('bTransf').disabled = !painelConfigurado || !destinos.length;
      $('bPen').disabled = !painelConfigurado;
      $('bRemoverFila').disabled = !painelConfigurado;
      const penAtual = Number(a.penalty);
      $('dPen').value = Number.isInteger(penAtual) && penAtual >= 1 && penAtual <= 10 ? penAtual : 1;
      if (!painelConfigurado) {
        $('dlgMsg').textContent = 'Ações do painel aguardando configuração de credenciais no servidor.';
        $('dlgMsg').style.color = 'var(--mut)';
      }
    }
  }
  if (PODE) {
    $('bTirar').onclick = e => tirar(sel.agente.code, sel.agente.name, $('dlgMsg'), e.currentTarget);
    $('bPausar').onclick = e => colocarPausa(sel.agente.code, sel.agente.name, $('dlgMsg'), e.currentTarget);
    $('bTransf').onclick = e => acao(e.currentTarget, async (alvoMsg) => {
      const a = sel.agente, para = $('dFila').value; if (!para) return;
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
      e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', arrastando);
      try { e.dataTransfer.setDragImage(s, 20, 20); } catch (_) { /* navegador sem suporte */ }
    });
    box.addEventListener('dragover', e => {
      if (!arrastando) return;
      const col = e.target.closest('.col'); if (!col) return;
      e.preventDefault(); limparAlvos();
      const s = e.target.closest('section.fila');
      if (s && s.dataset.fila !== arrastando) {
        const r = s.getBoundingClientRect(), antes = e.clientY < r.top + r.height / 2;
        s.classList.add(antes ? 'alvo-antes' : 'alvo-depois');
        const prox = antes ? s : s.nextElementSibling;
        alvo = {coluna: col.dataset.coluna, antesDe: prox && prox.dataset ? prox.dataset.fila || null : null};
      } else if (!s) {
        col.classList.add('alvo-fim'); alvo = {coluna: col.dataset.coluna, antesDe: null};
      }
    });
    box.addEventListener('drop', e => {
      if (!arrastando || !alvo) return; e.preventDefault();
      const {c1, c2} = arrumacao();
      let n1 = c1.filter(k => k !== arrastando), n2 = c2.filter(k => k !== arrastando);
      if (!duasColunas()) {   // uma coluna: reordena a sequência e mantém cada grupo na sua coluna
        const seq = n1.concat(n2), i = alvo.antesDe ? seq.indexOf(alvo.antesDe) : seq.length;
        seq.splice(i < 0 ? seq.length : i, 0, arrastando);
        n1 = seq.filter(k => c1.includes(k)); n2 = seq.filter(k => !c1.includes(k));
      } else {
        const lista = alvo.coluna === '1' ? n1 : n2, i = alvo.antesDe ? lista.indexOf(alvo.antesDe) : lista.length;
        lista.splice(i < 0 ? lista.length : i, 0, arrastando);
      }
      layout = {...layout, c1: n1, c2: n2}; salvarLayout();
    });
    box.addEventListener('dragend', () => { arrastando = null; alvo = null; limparAlvos(); render(); });
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
  // tela cheia (botão ou F11): esconde menu e controles
  function checaTelaCheia() {
    const cheia = !!document.fullscreenElement || matchMedia('(display-mode: fullscreen)').matches ||
                  (innerHeight >= screen.height - 2 && innerWidth >= screen.width - 2);
    root.classList.toggle('tela-cheia', cheia);
  }
  $('cheia').addEventListener('click', () => { if (root.requestFullscreen) root.requestFullscreen().catch(() => {}); });
  document.addEventListener('fullscreenchange', checaTelaCheia);
  let largAnterior = innerWidth;
  addEventListener('resize', () => { checaTelaCheia(); if ((largAnterior > 900) !== (innerWidth > 900)) render(); largAnterior = innerWidth; });
  checaTelaCheia();
  aplicarControles();

  carregar();
  setInterval(carregar, 5000);
})();
