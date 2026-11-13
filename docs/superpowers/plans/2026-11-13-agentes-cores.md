# Plano de Implementação — Cores e Precedência de Status NPX/PBX no Painel /agentes

> **Documento:** `docs/superpowers/plans/2026-11-13-agentes-cores.md`
> **Data:** 13/11/2026
> **Status:** Pronto para Execução (após aprovação do usuário)
> **Spec de Referência:** `docs/superpowers/specs/2026-11-13-agentes-cores-design.md`
> **Executor Designado:** Rapadura (Frontend & QA / Andar 1)
> **Revisão:** Maestro1 / Claude

---

## 1. Visão Geral e Arquitetura

### 1.1. Objetivo
Ajustar a exibição visual de status e cores no painel `/agentes` para que operadores com linha PBX ativa e pausa NPX exibam com clareza o estado **"Em pausa"** (em tom **azul-ciano**), eliminando a exibição incorreta da pill verde "Livre". Preservar as famílias cromáticas (verde=livre, azul=pausa, vermelho=ligação, laranja=recebendo) com tons distintos e contrastantes entre NPX e PBX nos temas claro e escuro, mantendo coerência estrita entre pills, linhas da tabela, coluna situação, ordenação, contadores e modal de detalhes.

### 1.2. Arquitetura da Solução
A alteração é **estritamente restrita ao frontend** (`static/agentes.css` e `static/agentes.js`) e aos testes automatizados de validação:
- **CSS (`static/agentes.css`)**:
  - Introdução do token `--pbx-pausa` nos temas claro (`#0284c7`) e escuro (`#38bdf8`).
  - Ajuste fino dos tokens PBX existentes (`--pbx-livre`, `--pbx-lig`, `--pbx-cham`) para garantir distinção imediata à distância em relação aos tokens NPX canônicos.
  - Criação da regra de tingimento de linha `#ag tr.l-pbx-pausa td`.
- **JavaScript (`static/agentes.js`)**:
  - Reordenação da lógica em `stDe(a)`: ligações físicas (`Busy`/`Ringing`) mantêm precedência sobre a pausa NPX; pausa NPX (`a.paused`) assume precedência sobre ramal livre (`Free`).
  - Ajuste em `situacaoHtml` e `situacaoTexto` para não prefixar `"Livre · "` quando o operador estiver pausado.
  - Garantia de que a ordenação (`chave`) e o modal (`atualizarDialogo`) reflitam a nova tupla de status.
- **Backend**:
  - **Inalterado**. Os contratos da API `/agentes/dados`, regras de autorização, bloqueios de mutações no PBX e exceção de "Tirar pausa" permanecem intactos.

---

## 2. Restrições Globais

1. **Sem Alteração de Backend**: Nenhuma modificação em `agentes_npx.py`, `pbx_monitor.py` ou endpoints da API.
2. **Sem Fonte Fictícia de Presença**: O caso de softphone SIP NPX sem fila de atendimento permanece **encerrado e fora de escopo** conforme deliberado em 12/11/2026.
3. **Sem Ações Reais de Telefonia**: Testes e validações operam exclusivamente com stubs/dados sintéticos locais; nenhum comando real de telefonia ou POST externo é emitido.
4. **Isolamento de Ambiente**: Não alterar o servidor em produção (`192.168.243.2:8888`); manter testes no ambiente local de QA (porta 8889).
5. **Sem Gitops Automático**: Não realizar commits, push ou merge sem comando expresso do usuário.

---

## 3. Plano Passo a Passo de Execução

### Etapa 1 — Atualização de Variáveis CSS e Estilos de Linha (`static/agentes.css`)

**Objetivo:** Adicionar `--pbx-pausa`, refinar paleta PBX e criar classe de linha `l-pbx-pausa`.

**Arquivos:**
- `static/agentes.css`

**Ações detalhadas:**
1. No seletor `#ag` (tema claro), declarar os tokens:
   ```css
   --livre: #16a34a; --lig: #dc2626; --pausa: #2563eb; --cham: #ea580c; --off: #9aa5b1; --tint: 9%; --tint2: 18%;
   --pbx-livre: #0d9488; --pbx-lig: #e11d48; --pbx-cham: #d97706; --pbx-pausa: #0284c7;
   ```
2. No seletor `[data-theme="dark"] #ag` (tema escuro), declarar os tokens:
   ```css
   --livre: #22c55e; --lig: #ef4444; --pausa: #3b82f6; --cham: #f97316; --off: #6b7785;
   --pbx-livre: #2dd4bf; --pbx-lig: #fb7185; --pbx-cham: #fbbf24; --pbx-pausa: #38bdf8;
   ```
3. Na seção de linhas de tabela, incluir a nova classe:
   ```css
   #ag tr.l-pbx-livre td { background: color-mix(in srgb, var(--pbx-livre) var(--tint), transparent); }
   #ag tr.l-pbx-pausa td { background: color-mix(in srgb, var(--pbx-pausa) var(--tint), transparent); }
   #ag tr.l-pbx-lig td   { background: color-mix(in srgb, var(--pbx-lig)   var(--tint), transparent); }
   #ag tr.l-pbx-cham td  { background: color-mix(in srgb, var(--pbx-cham)  var(--tint2), transparent); }
   ```

**Critério de Verificação da Etapa 1:**
- Sintaxe CSS válida sem quebras de escopo de `#ag`.

---

### Etapa 2 — Refatoração da Resolução de Status e Situação (`static/agentes.js`)

**Objetivo:** Ajustar `stDe(a)`, `situacaoHtml` e `situacaoTexto` para honrar a precedência definida na spec.

**Arquivos:**
- `static/agentes.js`

**Ações detalhadas:**
1. Ajustar `stDe(a)`:
   ```javascript
   const stDe = a => {
     if (a.line === 'pbx') {
       if (a.pbxStatus === 'Busy') return ST_PBX['Busy'];
       if (a.pbxStatus === 'Ringing') return ST_PBX['Ringing'];
       if (a.paused) return ['Em pausa', 'var(--pbx-pausa)', 0, 'l-pbx-pausa'];
       return ST_PBX[String(a.pbxStatus || '')] || [a.pbxStatus || a.erro || 'Desconhecido', 'var(--off)', 5, 'l-pbx-off'];
     }
     if (a.line === 'off') {
       return ST['0']; // Ramo line off permanece intacto (fora de escopo)
     }
     return a.paused ? ST['-1'] : (ST[String(a.status)] || [a.erro || 'Desconhecido', 'var(--off)', 5, 'l-off']);
   };
   ```
2. Ajustar `situacaoTexto(a)`:
   - Se `a.line === 'pbx'`: quando `a.paused` for verdadeiro e `a.pbxStatus === 'Free'`, inicializar `res = ''` (em vez de `'Livre'`), gerando apenas `⏸ ${pausaInfo}`.
3. Ajustar `situacaoHtml(a)`:
   - Se `a.line === 'pbx'`: quando `a.paused` for verdadeiro e `a.pbxStatus === 'Free'`, omitir o prefixo `<span class="mut">Livre</span>`, retornando diretamente `⏸ ${pausaTxt}`.
4. Validar sintaxe via Node.js:
   ```bash
   node --check static/agentes.js
   ```

**Critério de Verificação da Etapa 2:**
- `node --check` executa sem erros de sintaxe.
- Agente com `line: 'pbx'`, `pbxStatus: 'Free'` e `paused: true` resolve para rótulo `'Em pausa'`, estilo `'var(--pbx-pausa)'`, ordem `0` e classe `'l-pbx-pausa'`.

---

### Etapa 3 — Testes de Unidade e Matriz de Estados Frontend

**Objetivo:** Criar/atualizar bateria de testes automatizados garantindo a cobertura de todas as permutações da matriz de status.

**Arquivos:**
- `tests/test_agentes_frontend_cores.py` (ou asserções em teste de integração/Playwright)

**Casos de Teste Obrigatórios na Matriz:**
1. `line: 'pbx'`, `pbxStatus: 'Free'`, `paused: true` $\rightarrow$ Pill "Em pausa", cor `--pbx-pausa`, classe `l-pbx-pausa`, botão "Tirar pausa" habilitado.
2. `line: 'pbx'`, `pbxStatus: 'Busy'`, `paused: true` $\rightarrow$ Pill "Em ligação", cor `--pbx-lig`, classe `l-pbx-lig`, chamada visível + sufixo de pausa, botão "Tirar pausa" habilitado.
3. `line: 'pbx'`, `pbxStatus: 'Ringing'`, `paused: true` $\rightarrow$ Pill "Recebendo", cor `--pbx-cham`, classe `l-pbx-cham`, chamada visível + sufixo de pausa, botão "Tirar pausa" habilitado.
4. `line: 'pbx'`, `pbxStatus: 'Free'`, `paused: false` $\rightarrow$ Pill "Livre", cor `--pbx-livre`, classe `l-pbx-livre`, sem botão "Tirar pausa".
5. `line: 'npx'`, `status: 1`, `paused: true` $\rightarrow$ Pill "Em pausa", cor `--pausa` (Azul Royal), classe `l-pausa`.
6. `line: 'npx'`, `status: 1`, `paused: false` $\rightarrow$ Pill "Livre", cor `--livre` (Verde Esmeralda), classe `l-livre`.
7. Contadores de resumo (`#nPausa`, `#nLivre`, `#nLig`, `#nCham`): soma exata das categorias exclusivas, sem duplicidade de contagem.

**Comando de Execução:**
```bash
.venv/Scripts/python.exe -m unittest discover -s tests
```

---

### Etapa 4 — Validação Visual e Contraste com Playwright (Wallboard)

**Objetivo:** Verificar visualmente a interface renderizada em navegador real nos modos claro e escuro, medindo o contraste real de texto sobre fundo.

**Cenários de Teste Playwright:**
1. Subir servidor de QA na porta 8889 com snapshot sintético contendo analistas em todas as condições da matriz.
2. Carregar a página `/agentes` no tema claro:
   - Capturar screenshot comparativo.
   - Medir computação real de contraste (WCAG AA): calcular razão entre cor de texto efetiva e fundo efetivo de pills e badges (`color-mix(in srgb, var(--c) 15%, var(--card))`), exigindo >= 4.5:1 para texto normal (3:1 apenas para elementos gráficos como ponto indicador).
   - Se alguma combinação não atingir 4.5:1, ajustar a cor do texto da pill preservando a família cromática.
3. Alternar para o tema escuro (`data-theme="dark"`):
   - Capturar screenshot comparativo.
   - Medir computação real de contraste (WCAG AA >= 4.5:1 para texto normal) nas classes `l-pbx-pausa`, `l-pbx-livre`, `l-pbx-lig` e `l-pbx-cham`.
4. Abrir modal de detalhes para analista PBX pausado:
   - Confirmar pill "Em pausa" no cabeçalho do diálogo.
   - Confirmar botão "Tirar pausa" presente e interativo.

---

### Etapa 5 — Conclusão, Evidências e Comunicação

**Objetivo:** Consolidar artefatos, atualizar documentação e reportar ao Maestro1.

1. Registrar logs e evidências no diretório de QA.
2. Confirmar que o grafo do projeto está sincronizado:
   ```bash
   graphify update .
   ```
3. Reportar ao Maestro1 via `maestri ask Maestro1` informando os caminhos dos arquivos criados, ausência de alterações de código nesta fase de planejamento, e confirmação de prontidão para implementação.

---

## 4. Matriz de Rastreabilidade

| Requisito do Usuário | Seção da Spec | Etapa do Plano | Arquivo Afetado |
|---|---|---|---|
| Manter famílias de cores (verde, azul, vermelho, laranja) | Spec §3.1 | Etapa 1 | `static/agentes.css` |
| Tons diferentes NPX vs PBX (Azul-ciano PBX vs Royal NPX) | Spec §3.1 | Etapa 1 | `static/agentes.css` |
| `stDe` prioriza Busy/Ringing sobre pausa | Spec §4.1 | Etapa 2 | `static/agentes.js` |
| PBX Free + paused exibe sempre "Em pausa" | Spec §4.1 | Etapa 2 | `static/agentes.js` |
| Coerência em pill, linha, modal, situação, ordenação e contadores | Spec §4.3 a §4.5 | Etapa 2 | `static/agentes.js` |
| Manter badge PBX e despausa disponível | Spec §4.4 | Etapa 2 | `static/agentes.js` |
| Não alterar backend/API nem expandir NPX fora fila | Spec §2 | Seção 2 | - (Sem alteração) |
| Testes de matriz e browser (claro/escuro) sem telefonia real | Spec §5 e §6 | Etapas 3 e 4 | `tests/` / Playwright |
