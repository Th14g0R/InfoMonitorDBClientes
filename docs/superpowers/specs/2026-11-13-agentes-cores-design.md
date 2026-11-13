# Especificação de Design — Cores e Precedência de Status NPX/PBX no Painel de Agentes

> **Documento:** `docs/superpowers/specs/2026-11-13-agentes-cores-design.md`
> **Data:** 13/11/2026
> **Status:** Proposta para revisão do usuário
> **Escopo:** Frontend do Painel `/agentes` (`static/agentes.js`, `static/agentes.css`) e testes de regressão/visuais.
> **Relacionado:** `docs/superpowers/plans/2026-11-13-agentes-cores.md`

---

## 1. Contexto e Motivação

No painel operacional `/agentes`, analistas podem operar via telefonia integrada da central NPX ou via ramal telefônico alternativo PBX (MicroSIP).

### 1.1. Problema Identificado
Quando um analista está com sessão aberta e em **pausa operacional na NPX** (ex.: "Trein. Remoto" ou "Almoço"), mas possui seu softphone PBX registrado e sem chamadas em curso (`pbxStatus: 'Free'`), o backend atribui `line: 'pbx'` para apontar a telefonia ativa.

No frontend (`static/agentes.js:32-40`), a função de resolução de status visual `stDe(a)` avaliava:
```javascript
if (a.line === 'pbx') {
  return ST_PBX[String(a.pbxStatus || '')] || ...;
}
```
Como `a.pbxStatus` é `'Free'`, a função retornava `ST_PBX['Free']` (`['Livre', 'var(--pbx-livre)', 3, 'l-pbx-livre']`), ignorando completamente o campo `a.paused`.

### 1.2. Sintomas e Impacto no Painel de Parede (Wallboard)
1. **Pill de Status Incorreta**: A pill principal exibia o texto **"Livre"** em verde brilhante.
2. **Tingimento da Linha da Tabela**: A classe de linha `l-pbx-livre` aplicava um fundo esverdeado na linha inteira e borda esquerda verde.
3. **Contradição com a Coluna Situação**: A coluna "Situação" exibia `Livre · ⏸ Trein. Remoto`, confundindo supervisores.
4. **Divergência com os Contadores**: Os contadores de resumo do topo e do cabeçalho da fila (`ehPausa` e `ehLivre`) já contabilizavam o operador como "Em pausa" e não como "Livre", gerando discrepância numérica direta com o que os olhos viam na tabela.
5. **Dificuldade de Diferenciação à Distância**: No monitor de parede (wallboard, visão a 2–5 metros), as cores PBX atuais eram apenas variações muito próximas das cores NPX (ou coincidentes entre temas claro e escuro), dificultando identificar qual sistema telefônico estava ativo em cada operador.

---

## 2. Objetivos e Não-Objetivos

### Objetivos
- **Harmonia Cromática por Famílias**: Manter estritamente as 4 famílias semânticas fundamentais:
  - **Verde**: Livre / Disponível.
  - **Azul**: Pausa.
  - **Vermelho**: Em ligação.
  - **Laranja**: Recebendo / Chamando.
- **Tons Distinguíveis por Linha (NPX vs PBX)**:
  - **NPX**: Tons canônicos preservados (Azul Royal na pausa, Verde Esmeralda no livre, Vermelho clássico na ligação, Laranja clássico no chamando).
  - **PBX**: Tons próprios diferenciados nos temas claro e escuro. Para pausa PBX, adotar **Azul-Ciano** (Sky/Cyan), contrastando nitidamente com o **Azul Royal** da NPX (e descartando a hipótese de roxo).
- **Precedência Estrita de Status (`stDe`)**:
  - `Busy` / `Ringing` no PBX precede a pausa NPX (uma ligação física em curso tem prioridade de atenção no monitor).
  - `PBX Free + paused` exibe SEMPRE status **"Em pausa"** com tom PBX azul-ciano.
  - Alinhamento total entre pill, linha da tabela (`tr`), modal de detalhes, coluna de situação, ordenação por status e contadores de topo/grupo.
- **Badge e Ações Preservadas**: Manter badge textual `PBX` adjacente à pill e ação "Tirar pausa" perfeitamente acessível para operadores pausados com linha PBX.

### Não-Objetivos
- NÃO alterar contratos de payload da API `/agentes/dados` nem lógica no backend Python.
- NÃO implementar ações de telefonia direta no PBX (desligar, transferir chamada física).
- NÃO alterar a decisão de 12/11/2026: investigação de fonte SIP NPX sem fila permanece encerrada e fora de escopo.
- NÃO realizar mutações em produção (porta 8888) nem git push/merge.

---

## 3. Matriz de Paleta de Cores e Estilos CSS

### 3.1. Especificação de Cores por Família e Linha

| Família | Linha | Estado Visual | Token CSS | Tema Claro | Tema Escuro | Descrição Perceptiva |
|---|---|---|---|---|---|---|
| **Verde** | **NPX** | Livre | `--livre` | `#16a34a` (Emerald 600) | `#22c55e` (Emerald 500) | Verde esmeralda canônico |
| **Verde** | **PBX** | Livre | `--pbx-livre` | `#0d9488` (Teal 600) | `#2dd4bf` (Teal 400) | Verde-azulado / Menta frio |
| **Azul** | **NPX** | Em pausa | `--pausa` | `#2563eb` (Blue 600) | `#3b82f6` (Blue 500) | Azul Royal clássico |
| **Azul** | **PBX** | Em pausa | `--pbx-pausa` | `#0284c7` (Sky/Cyan 600) | `#38bdf8` (Sky/Cyan 400) | **Azul-Ciano luminoso** |
| **Vermelho** | **NPX** | Em ligação | `--lig` | `#dc2626` (Red 600) | `#ef4444` (Red 500) | Vermelho puro canônico |
| **Vermelho** | **PBX** | Em ligação | `--pbx-lig` | `#e11d48` (Rose 600) | `#fb7185` (Rose 400) | Vermelho carmim / framboesa |
| **Laranja** | **NPX** | Recebendo | `--cham` | `#ea580c` (Orange 600) | `#f97316` (Orange 500) | Laranja vivo canônico |
| **Laranja** | **PBX** | Recebendo | `--pbx-cham` | `#d97706` (Amber 600) | `#fbbf24` (Amber 400) | Âmbar / Dourado quente |
| **Neutro** | Ambos | Offline | `--off` | `#9aa5b1` | `#6b7785` | Cinza neutro |

### 3.2. Estilos de Linha e Classes da Tabela
- Nova classe de linha: `tr.l-pbx-pausa td`
  ```css
  #ag tr.l-pbx-pausa td {
    background: color-mix(in srgb, var(--pbx-pausa) var(--tint), transparent);
  }
  ```
- O indicador lateral esquerdo (`#ag tr td:first-child`) herda automaticamente `--c: var(--pbx-pausa)`.
- A pill de status renderiza:
  ```html
  <div class="st-cell">
    <span class="pill" style="--c: var(--pbx-pausa)">
      <span class="pill-txt">Em pausa</span>
    </span>
    <span class="tag-linha tag-pbx">PBX</span>
  </div>
  ```

---

## 4. Lógica de Precedência e Comportamento dos Componentes

### 4.1. Resolução de Status em `stDe(a)`
A função `stDe(a)` retorna uma tupla de 4 elementos: `[rótulo, cor, ordem, classe_linha]`.

```javascript
const stDe = a => {
  if (a.line === 'pbx') {
    // 1º: Chamada em curso ou tocando no PBX tem prioridade visual máxima
    if (a.pbxStatus === 'Busy') return ST_PBX['Busy'];
    if (a.pbxStatus === 'Ringing') return ST_PBX['Ringing'];
    // 2º: Analista em pausa operacional na NPX com PBX livre/ocioso
    if (a.paused) return ['Em pausa', 'var(--pbx-pausa)', 0, 'l-pbx-pausa'];
    // 3º: Demais status PBX (Free -> Livre, Offline -> Offline)
    return ST_PBX[String(a.pbxStatus || '')] || [a.pbxStatus || a.erro || 'Desconhecido', 'var(--off)', 5, 'l-pbx-off'];
  }
  if (a.line === 'off') {
    return ST['0']; // Ramo line off permanece intacto (fora de escopo)
  }
  // Linha NPX padrão
  return a.paused ? ST['-1'] : (ST[String(a.status)] || [a.erro || 'Desconhecido', 'var(--off)', 5, 'l-off']);
};
```

### 4.2. Dicionários de Apoio
Atualização em `ST_PBX`:
```javascript
const ST_PBX = {
  'Ringing': ['Recebendo', 'var(--pbx-cham)', 1, 'l-pbx-cham'],
  'Busy': ['Em ligação', 'var(--pbx-lig)', 2, 'l-pbx-lig'],
  'Free': ['Livre', 'var(--pbx-livre)', 3, 'l-pbx-livre'],
  'Offline': ['Offline', 'var(--off)', 4, 'l-pbx-off'],
};
```

### 4.3. Coluna "Situação" (`situacaoHtml` e `situacaoTexto`)
- **Cenário PBX Busy + Pausa NPX**: Exibe chamada física ativa + sufixo de pausa:
  - HTML: `📞 <span class="mut">de</span> <span class="fone">(85) 99999-9999</span> (02m 15s) <span class="mut" title="Em pausa na NPX">· ⏸ Trein. Remoto · 15m de 60m</span>`
- **Cenário PBX Free + Pausa NPX**: O analista NÃO está livre; a telefonia está em espera e a presença operacional é pausa:
  - NÃO prefixar `"Livre · "`.
  - HTML: `⏸ Trein. Remoto <span class="mut">· 15m de 60m</span>` (ou `desde 14:30`).
  - Texto ordenação: `⏸ trein. remoto · 15m de 60m`.
- **Cenário PBX Free sem pausa**: Mantém `Livre`.

### 4.4. Modal de Detalhes (`atualizarDialogo`)
- Como `atualizarDialogo` consome diretamente `stDe(a)`, o cabeçalho do diálogo passa a exibir a pill `"Em pausa"` com estilo `var(--pbx-pausa)`.
- Subtítulo exibe `Código 4422008 · 2008 · fila Suporte`.
- Bloco de pausa (`$('dPausa')`) exibe `${pauseType} há ${tempo} (NPX)`.
- Botão "Tirar pausa" (`$('bTirar')`) permanece **visível e habilitado**, preservando a ação permitida no backend.

### 4.5. Ordenação e Contadores
- **Ordenação por Status (`chave(a, 'status')`)**:
  - `stDe(a)[2]` define o peso: `0` para Pausa (tanto NPX quanto PBX).
  - Analistas em pausa PBX agrupam-se naturalmente junto com analistas em pausa NPX no topo da ordenação de status.
- **Contadores de Topo e Grupo**:
  - `ehPausa(a)`: `!!a.paused && !ehLig(a) && !ehCham(a)` $\rightarrow$ contabiliza `PBX Free + paused` no card `#nPausa`.
  - `ehLivre(a)`: `a.line === 'pbx' ? (!a.paused && a.pbxStatus === 'Free') : (!a.paused && a.status === 1)` $\rightarrow$ NÃO contabiliza analistas pausados no `#nLivre`.
  - Os contadores de resumo e cabeçalhos alinham-se à exibição visual das pills da tabela.

---

## 5. Matriz de Estados e Comportamento Visual

| Linha (`line`) | Status NPX | Status PBX | Pausado (`paused`) | Pill Status | Cor Token | Classe Linha | Situação |
|---|---|---|---|---|---|---|---|
| `npx` | 1 (Livre) | - | `false` | Livre | `--livre` | `l-livre` | — |
| `npx` | -1 (Pausa) | - | `true` | Em pausa | `--pausa` | `l-pausa` | `⏸ Almoço...` |
| `npx` | 2 (Ligação) | - | `false` | Em ligação | `--lig` | `l-lig` | `📞 (85) ...` |
| `npx` | 5 (Chamando) | - | `false` | Recebendo | `--cham` | `l-cham` | `Chamando` |
| `pbx` | 0 | Busy | `false` | Em ligação | `--pbx-lig` | `l-pbx-lig` | `📞 (85) ...` |
| `pbx` | 0 | Busy | `true` | Em ligação | `--pbx-lig` | `l-pbx-lig` | `📞 ... · ⏸ Pausa` |
| `pbx` | 0 | Ringing | `false` | Recebendo | `--pbx-cham` | `l-pbx-cham` | `Chamando` |
| `pbx` | 0 | Ringing | `true` | Recebendo | `--pbx-cham` | `l-pbx-cham` | `Chamando · ⏸ Pausa` |
| `pbx` | 0 | Free | `false` | Livre | `--pbx-livre` | `l-pbx-livre` | `Livre` |
| **`pbx`** | **0** | **Free** | **`true`** | **Em pausa** | **`--pbx-pausa`** | **`l-pbx-pausa`** | **`⏸ Pausa...`** |
| `pbx` | 0 | Offline | `false` | Offline | `--off` | `l-pbx-off` | `—` |
| `off` | 0 | Offline | `false` | Offline | `--off` | `l-off` | `—` |

---

## 6. Acessibilidade e Contraste (WCAG AA)

O contraste efetivo deve ser verificado calculando a razão entre o texto real e o fundo real renderizado nos navegadores, sem assumir previamente garantias teóricas:
- **Texto normal de pills e badges**: contraste mínimo medido de **4.5:1** nos temas claro e escuro. Usar a razão de **3:1** apenas para elementos gráficos de interface (bordas/pontos indicadores) ou texto grande conforme os critérios WCAG; negrito por si só não reduz a exigência de 4.5:1.
- **Ajuste de contraste**: se a medição no fundo real (`color-mix(in srgb, var(--c) 15%, var(--card))`) não atingir 4.5:1 nos temas claro ou escuro, ajustar a cor do texto da pill mantendo a identidade visual das famílias NPX e PBX.
- **Fundo das linhas da tabela**: uso de `color-mix(in srgb, var(--cor) var(--tint), transparent)` com `--tint: 9%` (claro) e `14%` (escuro) para manter o contraste do texto da linha.
- `--prefers-reduced-motion`: respeitado para todas as animações (pulso e piscar).
