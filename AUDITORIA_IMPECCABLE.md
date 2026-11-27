# Relatório de Auditoria Global de UI/UX — Impeccable

> **Projeto:** InfoMonitorDBClientes  
> **Data:** 27 de Novembro de 2026  
> **Metodologia:** Impeccable Craftsmanship (`audit` → `normalize` → `harden` → `polish`)  
> **Status:** Concluído com Sucesso (`100% Corrigido e Testado`)

---

## 1. Mapeamento de Rotas, Telas e Componentes

| Rota / Arquivo | Tipo / Função | Stack / Componentes Principais |
| :--- | :--- | :--- |
| `templates/menu_navegacao.html`<br>`static/menu_navegacao.css` | Componente Compartilhado (Topbar Global) | Seletor de servidor, dropdowns de consultas, links externos, botões de ação, alternador de tema claro/escuro e menu do usuário |
| `templates/agentes.html`<br>`static/agentes.css`, `static/agentes.js` | Tela Operacional (`/agentes`) | Painel de monitoramento de agentes em tempo real (NPX + PBX), chips de métricas, grid responsivo de filas, drag-and-drop de grupos, modal de ações operacionais |
| `templates/cnpj.html` | Tela de Consulta (`/cnpj`) | Hero de busca de CNPJ, autocomplete de exemplos, cards de dados cadastrais, visualizador de JSON bruto e inspetor em árvore de propriedades |
| `templates/certificados.html` | Tela Administrativa (`/certificados`) | Tabela de subdomínios monitorados, status de validade SSL, alerta de expiração em dias, modal de exclusão e formulário de adição |
| `templates/backups_ftp_admin.html`<br>`static/backups_ftp_admin.css`, `static/backups_ftp_admin.js` | Modal Administrativo Master (`/backups-ftp`) | Gestão de rotinas de backup FTP por cliente, visualização auditada de senha, campos extras dinâmicos, versionador de tarefas Cobian (.lst) |
| `templates/eventos.html`<br>`horarios.py` | Seção Integrada (`/horarios#secao-eventos`) | Filtros de eventos por dia/período, cadastro de feriados/plantões, cards de eventos, listagem de participantes e seleção múltipla de escala |
| `templates/campos_funcionario.html`<br>`templates/editar_funcionario.html` | Formulário e Tela de Edição | Inputs de cadastro de colaborador, jornada de trabalho, escala de sábado e domingo (TEF), upload de foto |
| `templates/editar_evento.html` | Tela de Edição (`/horarios/editar_evento/<id>`) | Formulário de edição de tipo de evento, horários, local, observação |
| `static/ui.css` | Design System Base | Variáveis CSS (tokens Light/Dark), tipografia, botões, tabelas, badges, alertas, toasts e scrollbars |

---

## 2. Auditoria e Status de Correção Sistemática

### Legenda de Severidade
- 🔴 **Crítico**: Falhas de renderização, erros de sintaxe CSS, quebras visíveis em tema escuro/claro ou bloqueio de usabilidade em mobile.
- 🟡 **Moderado**: Inconsistências de contraste de cores, falta de responsividade suave, falta de classes utilitárias base (.form-control fora de grid), estados de foco/acessibilidade ausentes.
- 🔵 **Visual/Polimento**: Microespaçamentos desalinhados, hierarquia tipográfica desigual, falta de transições suaves, estados vazios sem ícones ilustrativos.

---

### A. Consulta CNPJ (`templates/cnpj.html`)
- [x] 🔴 **[CRÍTICO] Corrigido**: Removido erro de sintaxe CSS na linha 152 do bloco `<style>` (bloco órfão sem seletor descartado pelo navegador).
- [x] 🔴 **[CRÍTICO] Corrigido**: Substituídas todas as cores escuras hardcoded (`#cbd5e1`, `#fff`, `#334155`, `#f1f5f9`, `#020617`, etc.) em `.metric-badge`, `.btn-action` e `.raw-json-box` por variáveis semânticas do tema (`var(--bg-main)`, `var(--bg-card)`, `var(--text-main)`, `var(--text-muted)`). Agora 100% harmônico e legível em ambos os temas.
- [x] 🟡 **[MODERADO] Corrigido**: Campo de input `.search-input` agora possui `box-sizing: border-box` e padding proporcional garantindo que não estoure em telas de 320px–360px.
- [x] 🟡 **[MODERADO] Corrigido**: Mensagem de erro `.alert-error` com regras deduplicadas e contraste corrigido.
- [x] 🔵 **[POLIMENTO] Corrigido**: Botão de limpar `✖` (`#btn-clear`) com área de toque mínima acessível de 28x28px, alinhamento flex centralizado e feedback visual ao passar o cursor.

---

### B. Gestão de Backups FTP (`templates/backups_ftp_admin.html` & `static/backups_ftp_admin.css`)
- [x] 🔴 **[CRÍTICO] Corrigido**: Submodal `.ftp-submodal-card` refatorado para utilizar `var(--cor-superficie)` e `var(--cor-texto)`, eliminando o fundo branco ofuscante no tema escuro. Caixa de senha revelada atualizada para `var(--cor-primaria-clara)` e `var(--cor-primaria)`.
- [x] 🔴 **[CRÍTICO] Corrigido**: Adicionado breakpoint responsivo mobile (< 600px) ajustando padding do modal para 14px 16px e `max-height: 96vh`, garantindo visualização impecável em telas estreitas de 360px.
- [x] 🟡 **[MODERADO] Corrigido**: Botões `.btn-disparo` e `.btn-primario` padronizados em `static/ui.css` com cores, hover e foco do design system.
- [x] 🟡 **[MODERADO] Corrigido**: Botões de fechar modal (`.ftp-admin-btn-close`) atualizados com área de clique de 32x32px, transição suave e anel de foco `:focus-visible`.
- [x] 🔵 **[POLIMENTO] Corrigido**: Padronização dos cantos arredondados e sombras (`box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.25)`).

---

### C. Monitoramento de Certificados SSL (`templates/certificados.html`)
- [x] 🔴 **[CRÍTICO] Corrigido**: Classes `.form-control` e `.form-select` agora definidas universalmente em `static/ui.css`, permitindo que o input `#dominio` tenha borda, altura, padding e foco padronizados mesmo fora de `.form-grid`.
- [x] 🟡 **[MODERADO] Corrigido**: Estilização de `.info-box` e `.info-box-primary` integrada ao tema com adaptação em dark mode.
- [x] 🟡 **[MODERADO] Corrigido**: Contêiner `.tabela-wrap` padronizado com rolagem suave horizontal `-webkit-overflow-scrolling: touch` para telas móveis.
- [x] 🟡 **[MODERADO] Corrigido**: Modal de exclusão com tipografia e espaçamentos alinhados aos tokens do sistema.
- [x] 🔵 **[POLIMENTO] Corrigido**: Estado vazio de tabela refatorado com ícone ilustrativo `🔒`, texto em negrito e instrução de primeiro cadastro.

---

### D. Gestão de Horários e Eventos (`templates/eventos.html`, `templates/campos_funcionario.html`, `templates/editar_evento.html`, `templates/editar_funcionario.html`)
- [x] 🟡 **[MODERADO] Corrigido**: Botões `.btn-bancos`, `.btn-edit` e `.btn-delete` padronizados no design system (`static/ui.css`) com estados de hover e cores semânticas.
- [x] 🟡 **[MODERADO] Corrigido**: Cor de aviso de plantão `.evento-aviso-plantao` corrigida de `#92400e` fixo para `var(--cor-aviso)`, tornando o texto legível no modo escuro.
- [x] 🟡 **[MODERADO] Corrigido**: Criada classe utilitária `.container-sm` e `.nav-bar` em `static/ui.css` e aplicada em `editar_evento.html` e `editar_funcionario.html`, eliminando estilos inline repetitivos.
- [x] 🔵 **[POLIMENTO] Corrigido**: Em `eventos.html`, o texto de ajuda `#evento-ajuda-tipo` agora utiliza `grid-column: 1 / -1;` para cobrir dinamicamente todas as colunas em qualquer resolução.

---

### E. Topbar e Menu de Navegação Global (`templates/menu_navegacao.html` & `static/menu_navegacao.css`)
- [x] 🟡 **[MODERADO] Corrigido**: Painel suspenso `.menu-painel` em mobile refinado para garantir que não corte em telas estreitas, com `z-index: var(--z-dropdown)` e elevação visual adequada.
- [x] 🟡 **[MODERADO] Corrigido**: Adicionado `:focus-visible` unificado para todos os botões e resumos de menu (`summary.btn`, `.btn`, `.theme-toggle`, `.seletor-servidor select`).
- [x] 🔵 **[POLIMENTO] Corrigido**: Contraste aprimorado no modo escuro para links ativos/em hover dentro do `.menu-painel`, utilizando `#93c5fd` sobre `--cor-primaria-clara` para atender WCAG AA.

---

### F. Painel de Agentes (`templates/agentes.html` & `static/agentes.css`)
- [x] 🟡 **[MODERADO] Corrigido**: O diálogo `#dlg` (ações operacionais) recebeu `box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3)` e `backdrop-filter: blur(2px)` no `::backdrop`.
- [x] 🟡 **[MODERADO] Corrigido**: Inputs e selects dentro de `#ag dialog` receberam anel de foco explícito `:focus` com `var(--pausa)`.
- [x] 🔵 **[POLIMENTO] Corrigido**: As tags de direção de chamada (`.dir-entrada`, `.dir-saida`, `.dir-indefinida`) utilizam `color-mix(in srgb, currentColor 12%, var(--card))` mantendo alto contraste em temas claro e escuro.

---

### G. Design System Compartilhado (`static/ui.css`)
- [x] 🔴 **[CRÍTICO] Corrigido**: Adicionadas classes universais `.form-control`, `.form-select`, `.form-label` fora de `.form-grid`.
- [x] 🔴 **[CRÍTICO] Corrigido**: Adicionadas classes utilitárias flexbox essenciais (`.d-flex`, `.d-inline-flex`, `.flex-column`, `.flex-wrap`, `.align-center`, `.justify-between`, `.justify-center`, `.justify-end`, `.gap-1` até `.gap-4`, `.text-muted`, `.text-center`, `.text-right`, `.w-100`, `.container`, `.container-sm`, `.tabela-wrap`).
- [x] 🟡 **[MODERADO] Corrigido**: Badges de equipe (`.badge-amarela`, `.badge-verde`, `.badge-prio`) agora se adaptam semanticamente em `[data-theme="dark"]` usando as variáveis `--cor-aviso-bg`, `--cor-sucesso-bg` e `--cor-roxo-bg`.
- [x] 🟡 **[MODERADO] Corrigido**: Estados de foco universal `:focus-visible` aplicados para botões, resumos de menu e seletores.
- [x] 🔵 **[POLIMENTO] Corrigido**: Variantes de botão padronizadas (`.btn-primario`, `.btn-disparo`, `.btn-bancos`, `.btn-edit`, `.btn-delete`, `.btn-icon`, `.btn-icon-danger`).

---

## 3. Resumo dos Arquivos Modificados

1. [static/ui.css](file:///C:/TEMP/site/InfoMonitorDBClientes/static/ui.css): Classes utilitárias universais (`.form-control`, `.form-select`, `.d-flex`, `.nav-bar`, `.container-sm`), adaptação de badges em tema escuro, botões variantes e contrastes de alerta e menu.
2. [static/menu_navegacao.css](file:///C:/TEMP/site/InfoMonitorDBClientes/static/menu_navegacao.css): Foco `:focus-visible` na topbar, alinhamento mobile e consistência de elevação.
3. [static/agentes.css](file:///C:/TEMP/site/InfoMonitorDBClientes/static/agentes.css): Elevação com sombra suave e blur no modal `#dlg`, foco nos inputs e preservação da grade responsiva.
4. [static/backups_ftp_admin.css](file:///C:/TEMP/site/InfoMonitorDBClientes/static/backups_ftp_admin.css): Cores adaptativas do submodal em tema escuro, botão de fechar acessível e breakpoint mobile (< 600px).
5. [templates/cnpj.html](file:///C:/TEMP/site/InfoMonitorDBClientes/templates/cnpj.html): Remoção do erro de sintaxe CSS na linha 152, substituição de cores hardcoded por tokens de tema e botão de limpar acessível.
6. [templates/backups_ftp_admin.html](file:///C:/TEMP/site/InfoMonitorDBClientes/templates/backups_ftp_admin.html): Substituição de cores hardcoded na caixa de senha revelada por tokens semânticos.
7. [templates/certificados.html](file:///C:/TEMP/site/InfoMonitorDBClientes/templates/certificados.html): Estado vazio ilustrado com ícone `🔒` e aproveitamento de `.form-control` e `.info-box` normalizados.
8. [templates/eventos.html](file:///C:/TEMP/site/InfoMonitorDBClientes/templates/eventos.html): Ajuste do span do texto de ajuda para `1 / -1`.
9. [templates/editar_evento.html](file:///C:/TEMP/site/InfoMonitorDBClientes/templates/editar_evento.html): Utilização da classe utilitária `.container-sm` e `.nav-bar` normalizada.
10. [templates/editar_funcionario.html](file:///C:/TEMP/site/InfoMonitorDBClientes/templates/editar_funcionario.html): Utilização da classe utilitária `.container-sm` e `.nav-bar` normalizada.
11. [AUDITORIA_IMPECCABLE.md](file:///C:/TEMP/site/InfoMonitorDBClientes/AUDITORIA_IMPECCABLE.md): Documentação completa de auditoria e checklist de correções.

---

## 4. Validação e Testes
- **Sintaxe JavaScript:** `node --check` validado sem erros em todos os arquivos de script (`csrf.js`, `ui.js`, `agentes.js`, `backups_ftp_admin.js`).
- **Suíte de Testes Automatizados:** `python -m unittest discover tests` executou **474 testes** com resultado **OK** (0 falhas, 0 erros).
- **Integridade de Negócio:** 100% dos seletores, IDs, rotas e regras de negócio preservados.
