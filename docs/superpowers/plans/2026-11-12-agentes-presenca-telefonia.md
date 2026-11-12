# Plano — Presença de Telefonia e Convivência Fone/Fila (`fix/agentes-presenca-telefonia`)

> **Contexto:** Demanda pós-produção (Thiago).
> **Problema 1:** Bruno em pausa NPX ("Trein. Remoto") com MicroSIP registrado/em ligação no PBX deve exibir linha/ramal/chamada PBX sem perder o estado de pausa da NPX e permitindo a ação "Tirar pausa" da NPX.
> **Problema 2:** Investigação de fonte autoritativa de registro SIP/MicroSIP na NPX para analistas sem sessão de fila aparecerem no grupo "Não logado" com tag `[NPX]`.

---

## 1. Investigação da Fonte Autoritativa SIP NPX (Relatório de Lacuna Concreta)

### 1.1. Inspeção no Infoboard (`\\192.168.243.2\c$\Tomcat 9.0\webapps\Infoboard`)
- `npx-monitor-state.js`:
  Registra textualmente: `// Contrato observado no monitor oficial: 0 desligado, 1 livre, 2 ocupado, 5 chamando, -1 pausa. Trata-se de estado operacional, nao de SIP REGISTER.`
- `public/agent-state.js`:
  Função `getNpxSipRegistration(agent, { explicitOnly = false })`: verifica campos hipotéticos (`npxSipRegistered`, `sip_registered`, etc.), mas registra expressamente no comentário: `// A API de filas nao confirmou nem negou o registro SIP.`
- `server.js`:
  Coleta somente `/rates/details_by_queue.json`, `/rates/wait_calls.json` e `/rates/totals_filter_by_queues.json`. O PBX é o único serviço com endpoint real de registro SIP (`/api/listDevicesRegistered`).

### 1.2. Inspeção no Código Atual e na API Real da NPX
- A API da NPX (`https://app.npxtech.com.br/api`) expõe relatórios (`report_register_points_details`, `report_pause_details`) e `/pabx/{codigo}/agent_status`.
- `/pabx/{codigo}/agent_status` responde `{status: 0, message: "Agente não está em nenhuma fila"}` quando o analista não está em fila. A instrução proíbe inferir SIP online a partir do status 0 ou histórico de login.
- O painel web autenticado (`https://app.npx.com.br`) não possui rotas conhecidas para listar registros brutos do Asterisk/SIP softphone.
- O usuário informou que NÃO possui links de documentação técnica ou tela NPX que exponham registros SIP de softphones desconectados de filas.
- **Conclusão / Lacuna Concreta:** NÃO IDENTIFICAMOS fonte SIP nos endpoints e código consultados (Infoboard UNC, rotas conhecidas da API NPX e Rails panel). Não afirmamos que inexiste na API oficial toda (fato não provado), mas nos endpoints e documentos acessíveis não há fonte autoritativa disponível. Sem fonte autoritativa, NÃO criamos registro SIP fake ou por heurística nem adicionamos stubs ou métodos mortos especulativos. O terceiro caso fica **PENDENTE** de fonte autoritativa, e os 2 ajustes confirmados são concluídos com testes rigorosos.

---

## 2. Separação de Linha Telefônica e Estado de Fila (Caso Bruno)

### 2.1. Causa Raiz Reproduzida
Em `pbx_monitor.py`:
A condição anterior forçava `line = 'npx'` quando `em_pausa` estava ativo, sobrepondo o estado do PBX mesmo quando o analista estava com chamada ativa ou ramal livre no PBX. Como o frontend chaveia a exibição de chamadas, ramal e selo por `a.line`, os dados da ligação e do ramal PBX eram ocultados.
Além disso, `agentes_npx.py:recusar_se_line_pbx` bloqueava qualquer ação via painel (inclusive `tirar_pausa`) se `line == 'pbx'`.

### 2.2. Solução Arquitetural
1. **Precedência da Fonte de Telefonia (`pbx_monitor.py`)**:
   Ordem estrita mantendo a primazia de NPX ativa sem pausa:
   - `if npx_ativo and not em_pausa:` -> `line = 'npx'` (NPX ativa sem pausa preservada como linha primária).
   - `elif pbx_status in ('Free', 'Busy', 'Ringing'):` -> `line = 'pbx'` (analista em pausa na NPX ou com status 0 que esteja registrado no PBX exibe fone/ramal/chamadas do PBX).
   - `elif em_pausa or npx_ativo:` -> `line = 'npx'` (analista em pausa na NPX com PBX offline/ausente mantém linha 'npx').
   - `else:` -> `line = 'off'`.
   - **Preservação de Fila e Pausa**: Os campos `paused`, `pauseType`, `pauseStart`, `pauseMinutes`, `pauseMaxMinutes`, `queue`, `status`, `penalty`, `extension` da NPX continuam 100% preenchidos no objeto do analista.

2. **Exceção de Ações (`agentes_npx.py`)**:
   - Ordem estrita na rota `tirar_pausa`:
     1. Validação de formato de código via `_codigo(codigo)` (rejeita `pbx-*` e caracteres inválidos com HTTP 400).
     2. Verificação de autorização via `pode()` (retorna HTTP 403 se anônimo/sem permissão).
     3. Proteção CSRF obrigatória.
     4. `recusar_se_line_pbx(codigo, acao='tirar_pausa')`:
        - Rejeita códigos `pbx-*` com HTTP 400 (`'Ação indisponível para agentes operando exclusivamente via PBX.'`).
        - Se no **próprio retrato do servidor** (`coletor.retrato`) o analista estiver com `line == 'pbx'`:
          - Se `acao == 'tirar_pausa'` E o analista no servidor tiver `paused == True`: **PERMITIDO** (retorna `None`), delegando ao painel NPX. Ignora qualquer parâmetro `paused` enviado pelo cliente.
          - Caso contrário: **BLOQUEADO** com HTTP 400 (`'Ação indisponível para agentes operando via PBX ou offline na NPX.'`).
     5. Demais ações (`colocar_pausa`, `remover_fila`, `deslogar`, `transferir`, `alterar_prioridade`): continuam **BLOQUEADAS** com HTTP 400 para analistas com `line == 'pbx'`.

3. **Grupo "Não logado" (`PBX_Sem_Fila`)**:
   - Mantém analistas sem sessão NPX que estejam registrados no PBX agrupados sob `"Não logado"`.
   - Sem adicionar stubs mortos ou heurísticas para SIP NPX sem fila até haver endpoint autoritativo.

---

## 3. Matriz de Testes

| Estado NPX | Estado PBX | Linha Esperada (`line`) | Observações |
|---|---|---|---|
| Ativo (status=1) && !paused | Busy / Ringing | `'npx'` | NPX ativa sem pausa primária mantida |
| Ativo (status=1) && !paused | Free | `'npx'` | NPX ativa sem pausa primária mantida |
| Em pausa (paused=True) | Busy / Ringing | `'pbx'` | Exibe fone/ramal/chamada PBX + campos de pausa NPX preservados |
| Em pausa (paused=True) | Free | `'pbx'` | Exibe fone/ramal PBX + campos de pausa NPX preservados |
| Em pausa (paused=True) | Offline / Ausente | `'npx'` | Exibe pausa NPX normalmente |
| Offline (status=0) && !paused | Free | `'pbx'` | Exibe presença PBX |
| Offline (status=0) && !paused | Offline / Ausente | `'off'` | Linha off cinza |
| Sem sessão NPX (PBX-only) | Free / Busy | `'pbx'` | Grupo "Não logado" (`PBX_Sem_Fila`) |

---

## 4. Etapas de Execução e Commits

- **Etapa 1:** Atualizar plano com a formulação correta da lacuna e matriz de precedência.
- **Etapa 2:** Ajustar ordem de precedência em `pbx_monitor.py` e validação estrita no servidor em `agentes_npx.py`.
- **Etapa 3:** Escrever testes cobrindo a matriz completa e as regras de segurança/recusa de ações.
- **Etapa 4:** Rodar suíte completa de testes (`.venv/Scripts/python.exe -m unittest discover -s tests` com `PYTHON_DOTENV_DISABLED=1`).
- **Etapa 5:** Gravar commit local na branch `fix/agentes-presenca-telefonia`.
- **Etapa 6:** Reportar ao Maestro1 com contrato, lacuna concreta e evidências.
