# Plano de Integração PBX no Painel /agentes (Backend)

> **Contexto:** Demanda PBX para enriquecer o painel `/agentes` com dados em tempo real da central telefônica (dispositivos SIP registrados, ramais, ligações ativas, direção e duração), mantendo integridade com os dados da NPX.
> **Regra de Precedência:** NPX ativa ou em pausa vence sempre (`line='npx'`). PBX registrada atua como linha secundária (`line='pbx'`). Offline em ambos (`line='off'`).
> **Separação de Papéis:** Backend exclusivo nesta execução (Thiago); Frontend com Rapadura.

## 1. Objetivos e Contratos

### 1.1. Contrato da API `/agentes/dados` (JSON)
No topo da resposta:
```json
{
  "pbxConnection": {
    "configured": true,
    "available": true,
    "error": null
  }
}
```

Em cada objeto da lista `agentes`:
```json
{
  "code": "4422008",
  "name": "Thiago",
  "queue": "InfoBrasil_Suporte",
  "extension": "4422008",
  "status": 1,
  "seconds": 120,
  "penalty": 4,
  "paused": false,
  "pauseType": null,
  "pauseStart": null,
  "pauseMinutes": null,
  "pauseMaxMinutes": null,
  "callSrc": null,
  "callDst": null,
  "callClient": null,
  "erro": null,
  "line": "npx",
  "pbxExtension": "2008",
  "pbxStatus": "Free",
  "pbxNumber": null,
  "pbxDirection": null,
  "pbxSeconds": 0
}
```

### 1.2. Regra Estrita de Precedência (`line`)
1. Se `agent.get('paused') == True` ou (`agent.get('status') is not None` e `agent.get('status') != 0`):
   $\implies$ `line = 'npx'`
2. Senão, se `pbxStatus in ('Free', 'Busy', 'Ringing')`:
   $\implies$ `line = 'pbx'`
3. Senão:
   $\implies$ `line = 'off'`

### 1.3. Agentes PBX-Only e Regra do Grupo "Não logado"
- Analistas do mapa PBX cadastrados que não possuam sessão aberta na NPX.
- Somente adiciona se estiverem REGISTRADOS no PBX (`Free`, `Busy`, `Ringing`), evitando inundar o painel com ramais offline desnecessários. **NÃO criar agente offline sintético PBX-only**.
- **Grupo Único "Não logado":** Todo analista sem sessão NPX mas com PBX registrado vai SEMPRE para a fila sintética `PBX_Sem_Fila`, exibida com o nome `"Não logado"` (`NOME_PBX_SEM_FILA = 'Não logado'`), mesmo que o mapa possua `departamento` configurado. O departamento do mapa não direciona agentes PBX-only.
- Entram na lista de agentes com:
  - `code`: `f"pbx-{ramal}"`
  - `name`: Nome do analista no mapa
  - `queue`: `PBX_Sem_Fila` (exibida como `"Não logado"`)
  - `extension`: ramal (ex: `"2008"`)
  - `status`: `0`
  - `paused`: `False`
  - `line`: `'pbx'`
  - `pbxExtension`, `pbxStatus`, `pbxNumber`, `pbxDirection`, `pbxSeconds` preenchidos.
- A fila sintética `PBX_Sem_Fila` não é registrada permanentemente em `NOMES_PADRAO` e o grupo `"Não logado"` só existe dinamicamente quando houver agentes nessa situação, sumindo quando vazio.
- A fila sintética `PBX_Sem_Fila` é estritamente bloqueada como destino válido de transferências e mutações na NPX (`_validar_fila`).

### 1.4. Proteção de Ações Backend (Recusa para line=pbx)
- Qualquer mutação operacional via POST (`/admin/agentes/*`: tirar pausa, colocar pausa, remover fila, transferir, prioridade, deslogar) direcionada a um agente com `line == 'pbx'` (seja PBX-only ou NPX offline casado com PBX) é expressamente recusada com HTTP 400, impedindo mutações indevidas na NPX.

### 1.5. Privacidade e LGPD
- Se `podeGerenciar == False`:
  - `pbxNumber` é omitido/definido como `None` na serialização final de `/agentes/dados`, alinhado à proteção de `callSrc`, `callDst` e `callClient`.

### 1.6. Nova Regra Explícita (Thiago) — Filas NPX, Grupo "Não logado" e Ações NPX Offline
Esta regra substitui o detalhamento original do Requisito 7:
1. **Visibilidade nas Filas NPX:** O analista aparece nas filas NPX (`InfoBrasil_*`) **APENAS** se possuir sessão NPX aberta no momento (`coletor.retrato`).
2. **Precedência NPX Ativa/Pausa:** Mantém precedência estrita (`line = 'npx'`), preservando a fila NPX.
3. **NPX Status 0 / Offline e PBX Registrada:** Exibe dados PBX com `line = 'pbx'`, mas **mantém a(s) fila(s) NPX** do analista, sem movê-lo de fila. Ações via painel NPX são recusadas com HTTP 400 (`recusar_se_line_pbx`).
4. **NPX Offline e PBX Offline/Ausente:** Mantém todos os metadados da NPX intactos (`code`, `extension`, `queue`, `penalty`), status cinza com `line = 'off'`, e **permite ações NPX normalmente** (especialmente `colocar-pausa` via POST no painel fake).
5. **Agentes PBX-Only (Sem Sessão NPX e PBX Registrado):**
   - Vão **SEMPRE** para o grupo único exibido como `"Não logado"` (`FILA_PBX_SEM_FILA = 'PBX_Sem_Fila'`, `NOME_PBX_SEM_FILA = 'Não logado'`), mesmo quando o mapa possui `departamento` preenchido. O departamento não dirige o PBX-only.
   - O grupo `"Não logado"` só existe dinamicamente quando há agentes nessa condição; desaparece quando vazio.
   - Fila sintética `PBX_Sem_Fila` é rejeitada como destino em `_validar_fila`.
   - Ações operacionais para agentes PBX-only (`line = 'pbx'`) continuam recusadas (HTTP 400).
6. **Ausência Total:** Não criar agente offline sintético PBX-only. Agentes sem sessão NPX e ausentes/offline no PBX não aparecem no painel.
7. **Identidade via Mapa:** O mapa continua sendo a única fonte de identidade e ramal. Dispositivos SIP fora do mapa continuam estritamente ignorados. Novos analistas necessitam de entrada no mapa.

---

## 2. Componentes e Arquivos

1. `config/analysts_map.json`:
   - Mapeamento estático e configurável sem credenciais: `nome -> { ramal, departamento }`.
   - Suporte a tradução de departamentos para as filas `InfoBrasil_*`.
2. `pbx_monitor.py` (Novo):
   - `ClientePbx`: cliente REST em `urllib.request` para `/api/listDevices`, `/api/listDevicesRegistered`, `/api/onlineCalls`.
     - Verificação TLS configurável (padrão true; false se `PBX_TLS_REJECT_UNAUTHORIZED == 'false'`).
     - Bloqueio de redirecionamentos inseguros para não vazar credenciais.
     - Logs sanitizados sem credenciais, token, key, URLs completas ou corpos de requisição.
   - `ColetorPbx`:
     - Thread `daemon=True` paralela e desacoplada do coletor NPX com intervalo de ~3.0s e timeout estrito (2.5s).
     - Cache em memória com `threading.Lock`.
     - Resolução resiliente de ramais com desambiguação estrita (se colidir, não casa arbitrariamente).
3. `agentes_npx.py` (Modificado):
   - Integração com `ColetorPbx`.
   - Enriquecimento dos agentes com os campos `line`, `pbx*`.
   - Adição de agentes PBX-only.
   - Inclusão do nó `pbxConnection` em `/agentes/dados`.
   - Filtro de privacidade para `pbxNumber` quando `podeGerenciar == False`.
4. `.env.example` (Modificado):
   - Documentação de `PBX_BASE_URL`, `PBX_API_TOKEN`, `PBX_API_KEY`, `PBX_TLS_REJECT_UNAUTHORIZED`, `PBX_MAPA_ARQUIVO`.
5. Testes Unitários e Integração:
   - `tests/test_pbx_monitor.py` (Novo): testes unitários do cliente, coletor, TLS, redação de logs, mapa de analistas e desambiguação.
   - `tests/test_agentes_npx.py` (Modificado): testes de integração garantindo precedência (`line`), enriquecimento, privacidade, tolerância a falhas do PBX e contrato JSON.

---

## 3. Etapas de Execução e Commits

- **Etapa 1:**
  - Criar `config/analysts_map.json`.
  - Atualizar `.env.example`.
  - Implementar `pbx_monitor.py`.
  - Implementar `tests/test_pbx_monitor.py`.
  - Validar suíte focada de testes PBX.
  - Gravar Commit 1: `feat(pbx): implementar cliente, coletor e monitor pbx com desambiguação e segurança de tls`.
- **Etapa 2:**
  - Integrar `pbx_monitor` em `agentes_npx.py`.
  - Implementar precedência NPX > PBX > Off e privacidade de `pbxNumber`.
  - Adicionar testes de integração em `tests/test_agentes_npx.py`.
  - Validar suíte completa de testes (Python 3.14 sem dotenv).
  - Gravar Commit 2: `feat(agentes): integrar dados do pbx ao endpoint /agentes/dados com precedencia e privacidade`.
- **Etapa 3:**
  - Atualização do Grafo Graphify local (`graphify update .` com `GRAPHIFY_BACKEND=none`).
  - Notificar Maestro1 via `maestri ask Maestro1` com o resumo das evidências e contratos.
