import json
import base64

def get_seconds_from_timestamp(ts: str) -> float:
    """Converte 'MM:SS' ou 'HH:MM:SS' para segundos numéricos."""
    try:
        parts = ts.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        pass
    return 0.0

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <style>
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }

    body {
      background: #f8fafc;
      color: #1e293b;
      padding: 10px;
    }

    /* Container Fixo do Player e Legenda */
    .sticky-header {
      position: sticky;
      top: 0;
      z-index: 100;
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      padding: 14px 18px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
      margin-bottom: 14px;
    }

    .controls-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
    }

    audio {
      flex: 1;
      min-width: 270px;
      height: 40px;
      outline: none;
    }

    /* Grupo de Controle de Velocidade */
    .speed-wrapper {
      display: flex;
      align-items: center;
      gap: 6px;
      background: #f1f5f9;
      padding: 4px 10px;
      border-radius: 8px;
      border: 1px solid #cbd5e1;
    }

    .speed-label {
      font-size: 12px;
      font-weight: 600;
      color: #475569;
      white-space: nowrap;
    }

    .speed-badge {
      background: #0284c7;
      color: #ffffff;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
      margin-right: 2px;
    }

    .speed-btn-group {
      display: flex;
      align-items: center;
      gap: 2px;
    }

    .speed-btn {
      background: transparent;
      border: 1px solid transparent;
      padding: 4px 7px;
      font-size: 11.5px;
      font-weight: 600;
      color: #334155;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .speed-btn:hover {
      background: #e2e8f0;
      color: #0f172a;
    }

    .speed-btn.active {
      background: #2563eb !important;
      border-color: #1d4ed8 !important;
      color: #ffffff !important;
      font-weight: 700 !important;
      box-shadow: 0 2px 4px rgba(37, 99, 235, 0.35) !important;
    }

    /* Botão de Cortar Silêncio / Pausas */
    .skip-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #f8fafc;
      border: 1.5px solid #cbd5e1;
      color: #334155;
      padding: 6px 14px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      user-select: none;
    }

    .skip-btn:hover {
      background: #e2e8f0;
      border-color: #94a3b8;
    }

    .skip-btn.active {
      background: #15803d !important;
      border-color: #166534 !important;
      color: #ffffff !important;
      box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.25), 0 2px 6px rgba(22, 163, 74, 0.35) !important;
    }

    /* Navegação Rápida entre Falas */
    .nav-btn-group {
      display: flex;
      gap: 4px;
    }

    .nav-btn {
      background: #f1f5f9;
      border: 1px solid #cbd5e1;
      padding: 5px 8px;
      border-radius: 6px;
      font-size: 11.5px;
      font-weight: 600;
      color: #334155;
      cursor: pointer;
      transition: all 0.15s;
    }

    .nav-btn:hover {
      background: #e2e8f0;
      border-color: #94a3b8;
    }

    .autoscroll-toggle {
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 12.5px;
      color: #475569;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }

    /* Caixa da Legenda em Tempo Real */
    .subtitle-box {
      background: linear-gradient(135deg, #1e293b, #0f172a);
      color: #f8fafc;
      border-radius: 10px;
      padding: 12px 16px;
      display: flex;
      align-items: flex-start;
      gap: 12px;
      min-height: 58px;
      box-shadow: inset 0 1px 3px rgba(0,0,0,0.25);
    }

    .subtitle-badge {
      background: #2563eb;
      color: #ffffff;
      font-size: 12px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 6px;
      white-space: nowrap;
      margin-top: 2px;
    }

    .subtitle-badge.cliente {
      background: #dc2626;
    }

    .subtitle-badge.skip {
      background: #16a34a;
      animation: pulse 1s infinite alternate;
    }

    @keyframes pulse {
      from { opacity: 0.8; }
      to { opacity: 1; }
    }

    .subtitle-text {
      font-size: 14.5px;
      line-height: 1.45;
      color: #f8fafc;
      flex: 1;
    }

    .subtitle-text.idle {
      color: #94a3b8;
      font-style: italic;
    }

    /* Barra de Busca rápida interna */
    .search-row {
      display: flex;
      gap: 10px;
      margin-bottom: 12px;
    }

    .search-input {
      flex: 1;
      padding: 8px 14px;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      font-size: 13.5px;
      outline: none;
      transition: border-color 0.2s;
    }

    .search-input:focus {
      border-color: #2563eb;
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
    }

    .search-counter {
      align-self: center;
      font-size: 12.5px;
      color: #64748b;
      font-weight: 500;
    }

    /* Lista de Falas */
    .transcript-container {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding-bottom: 40px;
    }

    .speech-card {
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-left: 4px solid #94a3b8;
      border-radius: 8px;
      padding: 10px 14px;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .speech-card:hover {
      background: #f8fafc;
      transform: translateX(2px);
      box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    }

    .speech-card.atendente {
      border-left-color: #2563eb;
    }

    .speech-card.cliente {
      border-left-color: #dc2626;
    }

    /* Fala Ativa (Tocando agora) */
    .speech-card.active {
      background: #eff6ff !important;
      border-left-color: #1d4ed8 !important;
      border-left-width: 6px !important;
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.3), 0 4px 12px rgba(37, 99, 235, 0.12) !important;
      transform: scale(1.008);
    }

    .speech-card.active.cliente {
      background: #fef2f2 !important;
      border-left-color: #b91c1c !important;
      box-shadow: 0 0 0 2px rgba(220, 38, 38, 0.3), 0 4px 12px rgba(220, 38, 38, 0.12) !important;
    }

    .speech-header {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: #64748b;
    }

    .timestamp-tag {
      background: #f1f5f9;
      padding: 2px 6px;
      border-radius: 4px;
      font-family: monospace;
      font-weight: 700;
      color: #334155;
    }

    .speaker-name {
      font-weight: 700;
      font-size: 12.5px;
    }

    .speaker-name.atendente {
      color: #1d4ed8;
    }

    .speaker-name.cliente {
      color: #b91c1c;
    }

    .speech-body {
      font-size: 14px;
      line-height: 1.5;
      color: #1e293b;
    }

    mark {
      background-color: #fef08a;
      color: #000;
      padding: 1px 4px;
      border-radius: 3px;
      font-weight: 600;
    }

    .hidden {
      display: none !important;
    }
  </style>
</head>
<body>

  <!-- Barra Fixa com Player e Legenda -->
  <div class="sticky-header">
    <div class="controls-row">
      <audio id="audio-player" controls preload="metadata">
        <source src="__AUDIO_DATA_URI__" type="__MIME_TYPE__">
        Seu navegador não suporta reprodução de áudio.
      </audio>

      <!-- Seletor de Velocidade com indicação clara -->
      <div class="speed-wrapper" title="Velocidade de reprodução do áudio">
        <span class="speed-label">⚡ Velocidade:</span>
        <span id="speed-display" class="speed-badge">1.0x</span>
        <div class="speed-btn-group">
          <button type="button" class="speed-btn" data-speed="0.75" onclick="setSpeed(0.75)">0.75x</button>
          <button type="button" class="speed-btn active" data-speed="1.0" onclick="setSpeed(1.0)">1.0x</button>
          <button type="button" class="speed-btn" data-speed="1.25" onclick="setSpeed(1.25)">1.25x</button>
          <button type="button" class="speed-btn" data-speed="1.5" onclick="setSpeed(1.5)">1.5x</button>
          <button type="button" class="speed-btn" data-speed="1.75" onclick="setSpeed(1.75)">1.75x</button>
          <button type="button" class="speed-btn" data-speed="2.0" onclick="setSpeed(2.0)">2.0x</button>
        </div>
      </div>

      <!-- Botão para Cortar Silêncio / Sem Conversa -->
      <button type="button" id="skip-btn" class="skip-btn" onclick="toggleSkipSilence()" title="Corta silêncios, esperas telefônicas ou períodos sem diálogo para economizar tempo">
        <span>✂️</span>
        <span id="skip-label">Cortar Silêncios: <b>Desligado</b></span>
      </button>

      <!-- Navegação Entre Falas -->
      <div class="nav-btn-group" title="Navegar diretamente entre as falas transcritas">
        <button type="button" class="nav-btn" onclick="prevSpeech()" title="Voltar para a fala anterior">⏮️ Anterior</button>
        <button type="button" class="nav-btn" onclick="nextSpeech()" title="Pular para a próxima fala">⏭️ Próxima</button>
      </div>

      <label class="autoscroll-toggle" title="Rolar a lista de falas automaticamente acompanhando o áudio">
        <input type="checkbox" id="autoscroll-check" checked>
        Rolar com áudio
      </label>
    </div>

    <!-- Caixa da Legenda em Tempo Real -->
    <div class="subtitle-box">
      <div id="sub-badge" class="subtitle-badge">⏱️ 00:00</div>
      <div id="sub-text" class="subtitle-text idle">
        ▶️ Dê play no áudio ou clique em qualquer fala abaixo para ouvir e acompanhar a legenda em tempo real...
      </div>
    </div>
  </div>

  <!-- Campo de Busca rápida nas falas -->
  <div class="search-row">
    <input type="text" id="filter-input" class="search-input" placeholder="🔍 Filtrar nesta gravação (ex: cancelamento, banco, protocolo, valor)...">
    <span id="filter-counter" class="search-counter"></span>
  </div>

  <!-- Lista de Falas -->
  <div id="transcript-list" class="transcript-container"></div>

  <script>
    const dialogo = __DIALOGO_JSON__;
    const player = document.getElementById('audio-player');
    const subBadge = document.getElementById('sub-badge');
    const subText = document.getElementById('sub-text');
    const container = document.getElementById('transcript-list');
    const filterInput = document.getElementById('filter-input');
    const filterCounter = document.getElementById('filter-counter');
    const autoscrollCheck = document.getElementById('autoscroll-check');
    const skipBtn = document.getElementById('skip-btn');
    const skipLabel = document.getElementById('skip-label');
    const speedDisplay = document.getElementById('speed-display');

    let currentIndex = -1;
    let skipSilenceEnabled = false;

    // Calcula a duração estimada de cada fala com base na quantidade de palavras
    // Média de fala: ~2.4 palavras por segundo (mínimo 2s, máximo 30s)
    dialogo.forEach((item, index) => {
      const words = (item.texto || '').trim().split(/\\s+/).length;
      const durEstimada = Math.max(2.0, Math.min(30.0, (words / 2.4) + 0.8));
      item.duracao = durEstimada;
      item.fimEstimado = item.seconds + durEstimada;
    });

    // -------------------------------------------------------------
    // CONTROLE DE VELOCIDADE COM SINCRONIZAÇÃO E MARCAÇÃO ATIVA
    // -------------------------------------------------------------
    function updateSpeedUI(rate) {
      const currentRate = parseFloat(rate);
      
      // Atualiza texto do selo de velocidade
      if (speedDisplay) {
        speedDisplay.textContent = currentRate.toFixed(currentRate % 1 === 0 ? 1 : 2) + 'x';
      }

      // Marca o botão ativo correto
      let matched = false;
      document.querySelectorAll('.speed-btn').forEach(btn => {
        const btnSpeed = parseFloat(btn.getAttribute('data-speed'));
        if (Math.abs(btnSpeed - currentRate) < 0.06) {
          btn.classList.add('active');
          matched = true;
        } else {
          btn.classList.remove('active');
        }
      });

      // Se foi uma velocidade customizada selecionada no menu nativo (ex: 0.5x)
      if (!matched) {
        document.querySelectorAll('.speed-btn').forEach(btn => btn.classList.remove('active'));
      }

      // Salva no localStorage para persistir na sessão do atendente
      try {
        localStorage.setItem('infobrasil_playback_speed', currentRate.toString());
      } catch(e) {}
    }

    function setSpeed(rate) {
      const targetRate = parseFloat(rate);
      player.playbackRate = targetRate;
      updateSpeedUI(targetRate);
    }

    // Ouve alterações nativas de velocidade (inclusive se alteradas pelo menu de 3 pontinhos do navegador)
    player.addEventListener('ratechange', () => {
      updateSpeedUI(player.playbackRate);
    });

    // Restaura velocidade salva se houver
    try {
      const savedSpeed = localStorage.getItem('infobrasil_playback_speed');
      if (savedSpeed) {
        const parsed = parseFloat(savedSpeed);
        if (!isNaN(parsed) && parsed >= 0.5 && parsed <= 3.0) {
          player.playbackRate = parsed;
          updateSpeedUI(parsed);
        }
      }
    } catch(e) {}

    // -------------------------------------------------------------
    // CONTROLE DE PULAR / CORTAR SILÊNCIO & ESPERA
    // -------------------------------------------------------------
    function toggleSkipSilence() {
      skipSilenceEnabled = !skipSilenceEnabled;
      updateSkipSilenceUI();
      
      // Se acabou de ligar e está em momento de silêncio, já avança imediatamente
      if (skipSilenceEnabled && !player.paused) {
        checkAndSkipSilence(player.currentTime);
      }

      try {
        localStorage.setItem('infobrasil_skip_silence', skipSilenceEnabled ? '1' : '0');
      } catch(e) {}
    }

    function updateSkipSilenceUI() {
      if (skipSilenceEnabled) {
        skipBtn.classList.add('active');
        skipLabel.innerHTML = 'Cortar Silêncios: <b>ATIVADO (Pula Esperas)</b>';
      } else {
        skipBtn.classList.remove('active');
        skipLabel.innerHTML = 'Cortar Silêncios: <b>Desligado</b>';
      }
    }

    // Restaura estado de corte de silêncio se salvo
    try {
      const savedSkip = localStorage.getItem('infobrasil_skip_silence');
      if (savedSkip === '1') {
        skipSilenceEnabled = true;
        updateSkipSilenceUI();
      }
    } catch(e) {}

    function checkAndSkipSilence(ct) {
      if (!skipSilenceEnabled || player.paused || dialogo.length === 0) return;

      // 1. Silêncio antes da primeira fala (ex: ringtone inicial de espera)
      if (ct < dialogo[0].seconds - 1.2) {
        const segPulo = Math.round(dialogo[0].seconds - ct);
        subBadge.textContent = `✂️ Pulo de Espera (-${segPulo}s)`;
        subBadge.className = 'subtitle-badge skip';
        subText.textContent = `Pulando ${segPulo}s de espera inicial direto para o início da ligação: [${dialogo[0].timestamp}] ${dialogo[0].locutor}...`;
        player.currentTime = Math.max(0, dialogo[0].seconds - 0.2);
        return;
      }

      // 2. Silêncio entre falas durante o atendimento
      for (let i = 0; i < dialogo.length; i++) {
        const currentItem = dialogo[i];
        const nextItem = (i + 1 < dialogo.length) ? dialogo[i + 1] : null;

        if (nextItem) {
          // Se estamos após o fim estimado da fala atual e a próxima fala está a mais de 1.8 segundos
          if (ct >= currentItem.fimEstimado && ct < nextItem.seconds && (nextItem.seconds - ct) > 1.8) {
            const segundosSaltados = Math.round(nextItem.seconds - ct);
            subBadge.textContent = `✂️ Silêncio Cortado (-${segundosSaltados}s)`;
            subBadge.className = 'subtitle-badge skip';
            subText.textContent = `Pulando ${segundosSaltados}s de silêncio/espera direto para: [${nextItem.timestamp}] ${nextItem.locutor}...`;
            
            player.currentTime = Math.max(0, nextItem.seconds - 0.2);
            return;
          }
        }
      }
    }

    // -------------------------------------------------------------
    // NAVEGAÇÃO ENTRE FALAS (ANTERIOR / PRÓXIMA)
    // -------------------------------------------------------------
    function prevSpeech() {
      if (currentIndex > 0) {
        jumpTo(dialogo[currentIndex - 1].seconds, currentIndex - 1);
      } else if (dialogo.length > 0) {
        jumpTo(dialogo[0].seconds, 0);
      }
    }

    function nextSpeech() {
      if (currentIndex < dialogo.length - 1) {
        jumpTo(dialogo[currentIndex + 1].seconds, currentIndex + 1);
      }
    }

    // -------------------------------------------------------------
    // RENDERIZAÇÃO DA TRANSCRIÇÃO E CARDS
    // -------------------------------------------------------------
    function renderTranscript(filterQuery = '') {
      container.innerHTML = '';
      let visibleCount = 0;
      const query = filterQuery.trim().toLowerCase();

      dialogo.forEach((item, index) => {
        const isAtendente = item.papel.toLowerCase().includes('atendente') || item.locutor.toLowerCase().includes('atendente');
        const roleClass = isAtendente ? 'atendente' : 'cliente';
        const roleIcon = isAtendente ? '🎧' : '👤';

        let textHtml = item.texto;
        let matches = true;

        if (query) {
          if (item.texto.toLowerCase().includes(query) || item.locutor.toLowerCase().includes(query)) {
            const escapedQuery = query.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
            const regex = new RegExp('(' + escapedQuery + ')', 'gi');
            textHtml = item.texto.replace(regex, '<mark>$1</mark>');
          } else {
            matches = false;
          }
        }

        const card = document.createElement('div');
        card.className = 'speech-card ' + roleClass + (matches ? '' : ' hidden');
        card.id = 'card-' + index;
        card.onclick = () => {
          jumpTo(item.seconds, index);
        };

        card.innerHTML = `
          <div class="speech-header">
            <span class="timestamp-tag">${item.timestamp}</span>
            <span class="speaker-name ${roleClass}">${roleIcon} ${item.locutor}</span>
          </div>
          <div class="speech-body">${textHtml}</div>
        `;

        container.appendChild(card);
        if (matches) visibleCount++;
      });

      if (query) {
        filterCounter.textContent = visibleCount + ' fala(s) encontrada(s)';
      } else {
        filterCounter.textContent = dialogo.length + ' falas totais';
      }
    }

    // Salto para o tempo exato ao clicar
    function jumpTo(seconds, index) {
      player.currentTime = Math.max(0, seconds - 0.1);
      player.play();
      setActiveIndex(index, true);
    }

    // Define a fala ativa e atualiza legenda
    function setActiveIndex(idx, forceScroll = false) {
      if (idx === currentIndex && !forceScroll) return;

      // Remove ativa anterior
      if (currentIndex >= 0) {
        const prevCard = document.getElementById('card-' + currentIndex);
        if (prevCard) prevCard.classList.remove('active');
      }

      currentIndex = idx;

      if (idx >= 0 && idx < dialogo.length) {
        const item = dialogo[idx];
        const isAtendente = item.papel.toLowerCase().includes('atendente') || item.locutor.toLowerCase().includes('atendente');

        // Atualiza a legenda fixa
        subBadge.textContent = (isAtendente ? '🎧 ' : '👤 ') + item.timestamp + ' | ' + item.locutor;
        subBadge.className = 'subtitle-badge ' + (isAtendente ? 'atendente' : 'cliente');
        subText.textContent = item.texto;
        subText.className = 'subtitle-text';

        // Atualiza o card na lista
        const activeCard = document.getElementById('card-' + idx);
        if (activeCard) {
          activeCard.classList.add('active');
          if (autoscrollCheck.checked || forceScroll) {
            activeCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
        }
      }
    }

    // Monitora o áudio em tempo real com suporte a Pular Silêncios
    player.addEventListener('timeupdate', () => {
      const ct = player.currentTime;
      let activeIdx = -1;

      // 1. Identifica em qual fala estamos
      for (let i = 0; i < dialogo.length; i++) {
        const nextTime = (i + 1 < dialogo.length) ? dialogo[i + 1].seconds : 999999;
        if (ct >= dialogo[i].seconds && ct < nextTime) {
          activeIdx = i;
          break;
        }
      }

      if (activeIdx !== -1) {
        setActiveIndex(activeIdx);
      }

      // 2. Executa corte de silêncio se habilitado
      if (skipSilenceEnabled && !player.paused) {
        checkAndSkipSilence(ct);
      }
    });

    // Filtro instantâneo no input
    filterInput.addEventListener('input', (e) => {
      renderTranscript(e.target.value);
    });

    // Inicialização
    renderTranscript();
  </script>
</body>
</html>
"""

def build_interactive_player_html(dialogo: list, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Gera o HTML/CSS/JS do Player com Legenda Sincronizada em Tempo Real."""
    b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    audio_data_uri = f"data:{mime_type};base64,{b64_audio}"
    
    dialogo_com_segundos = []
    for item in dialogo:
        sec = get_seconds_from_timestamp(item.get("timestamp", "00:00"))
        dialogo_com_segundos.append({
            "timestamp": item.get("timestamp", "00:00"),
            "seconds": sec,
            "locutor": item.get("locutor", "Interlocutor"),
            "papel": item.get("papel", "Outro"),
            "texto": item.get("texto", "")
        })
    
    dialogo_json = json.dumps(dialogo_com_segundos, ensure_ascii=False)
    
    return (
        HTML_TEMPLATE
        .replace("__AUDIO_DATA_URI__", audio_data_uri)
        .replace("__MIME_TYPE__", mime_type)
        .replace("__DIALOGO_JSON__", dialogo_json)
    )
