# Painel de Agentes — segunda rodada

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execução delegada aos Executores Antigravity do andar, com revisão do Claude e integração pelo Maestro1.

**Goal:** operar agentes pelo painel NPX, destacar pausas pelo limite real, corrigir arraste e retirar o botão de tela cheia.

**Architecture:** o coletor mantém a API existente para leitura. Um cliente Devise no backend guarda cookies somente em memória e executa as ações internas do painel. O navegador continua chamando POSTs locais protegidos por permissão, CSRF e auditoria. Limites de pausa vêm exclusivamente de fontes NPX verificadas.

**Tech Stack:** Flask, urllib/http.cookiejar, SQLite, JavaScript/CSS existentes, unittest da .venv e Playwright.

**Spec:** pedido do usuário nesta sessão em 06/11/2026; contexto em `.maestri/roles/dd265fa7-8820-4979-9a29-54751165734d/estado-painel-agentes.md`.

## Restrições globais
- Branch `feat/painel-agentes`, quatro commits locais, um por item. Nenhum push, merge ou implantação nesta rodada.
- Não versionar `.env`, cookies, tokens, senhas, credenciais temporárias ou dados reais.
- Graphify somente local, `GRAPHIFY_BACKEND=none`, grafo `C:/TEMP/site/InfoMonitorDBClientes/graphify-out/graph.json`.
- Thiago: backend e testes Python. Rapadura: template/JS/CSS e Playwright. Não editar arquivos do outro executor.
- Maestro1 coordena os commits somente quando os dois lados do item estiverem prontos; arquivos compartilhados mudam sequencialmente entre itens.
- Testes reais de ações somente em `4422008`, com aviso prévio ao Thiago. Não testar remoção da fila sem autorização dada na hora. Nenhuma alteração em outros agentes.

## Foco de revisão
- Credenciais/cookies/token refletidos por erro remoto, redirect, representação de exceção ou auditoria não podem chegar ao navegador/log.
- Expiração de sessão deve provocar somente um novo login e uma repetição; credenciais inválidas não podem causar loop.
- Todo POST local, inclusive colocar pausa/remover fila, exige permissão e CSRF antes de chamar NPX.
- Layout antigo com `limite` deve continuar sendo lido; máximo nulo ou não encontrado nunca usa limite inventado.
- Arraste antes/depois, coluna vazia e arraste inverso precisam persistir após reload; leitura não pode arrastar.

## Item 1 — ações com sessão do painel (commit 1)
**Files:** `agentes_npx.py`, `tests/test_agentes_npx.py`, `.env.example`, `templates/agentes.html`, `static/agentes.js`; documentação correlata se necessária.

**Interfaces previstas:** `ClientePainelNpx.configurado`, `executar(acao, parametros) -> {ok, http, mensagem}` e injeção opcional `cliente_painel` em `configurar_agentes`. Confirmar nomes com os dois executores antes de escrever. Somente booleanos de configuração são públicos.

- [x] Testes primeiro: login Devise com token e cookiejar, expiração redirect/HTML repetida uma vez, URLs/query corretas, erro HTTP, prioridade 1–10, credencial refletida suprimida, permissão/CSRF/auditoria antes de rede, fallback unpause da API.
- [x] Implementar GET sign_in e POST Devise como `authenticate()` do Infoboard, cookies em memória, sessão serializada quando necessário; nenhuma persistência ou log dos segredos.
- [x] Usar `update_penalty`, `move_agent`, `invert_pause_status` e `remove_agent` no backend; preservar deslogar da API. Remover IDs internos e rollback. Incluir Transbordo.
- [x] Diálogo com colocar/tirar pausa mutuamente exclusivos, remover fila, confirmação em toda ação, prioridade 1–10 e destinos por nome sem bloqueio por IDs. Mostrar mensagem segura da NPX e forçar coleta após ação.
- [x] `.env.example`: NPX_PAINEL_URL padrão https://app.npx.com.br, NPX_PAINEL_EMAIL e NPX_PAINEL_SENHA vazios; retirar NPX_FILAS_IDS.
- [x] Rodar testes focados, revisar segurança e gravar commit 1.

## Item 2 — limite real por tipo de pausa (commit 2)
**Files:** `agentes_npx.py`, `tests/test_agentes_npx.py`, `templates/agentes.html`, `static/agentes.js`, `static/agentes.css`.

**Interface:** campo `pauseMaxMinutes` no retrato, máximo numérico finito maior ou igual a zero ou `null`; fonte por tipo no relatório de pausas de hoje e histórico de 30 dias, `per_page=1000`. `null` significa sem máximo conhecido; não inferir máximo do nome da pausa. Se todos os valores forem nulos, procurar cadastro web; sem fonte, avisar usuário e manter máximo desconhecido.

- [x] Testar cache de tipo com histórico, pausa atual ausente do relatório, máximo nulo/desconhecido e compatibilidade de layout antigo.
- [x] Montar cache de máximos sem inventar valores e conservar coleta com falhas parciais.
- [x] Remover limite global/campo/selo PRESA; aceitar e ignorar `layout.limite` antigo. Atualizar contador para “pausa estourada”.
- [x] Linha e selo de pausa em azul suave quando tempo > máximo do tipo; `prefers-reduced-motion` com destaque fixo. Situação com tempo/máximo, ou tipo + hora inicial quando não houver máximo.
- [x] Testes Python e Playwright sintético verificam ultrapassagem, nulidade e movimento reduzido; revisar e gravar commit 2.

## Item 3 — arraste entre colunas (commit 3)
**Files:** `static/agentes.js`, `static/agentes.css`; fixture/teste Playwright local ignorado.

- [x] Reproduzir com Playwright card da direita para baixo do card da esquerda e confirmar falha antes da correção.
- [x] Usar colunas esticadas e área final de soltura visível durante drag; suportar antes/depois e coluna vazia.
- [x] Testar drag nos dois sentidos, esvaziar/repovoar, reload e layout persistido, além de leitura sem drag. Revisar e gravar commit 3.

## Item 4 — remover botão de tela cheia (commit 4)
**Files:** `templates/agentes.html`, `static/agentes.js` e referências de cache.

- [x] Remover botão e listener dedicado; manter `fullscreenchange`, detecção F11 e ocultação de menu/controles/alças.
- [x] Conferir ausência de acesso a elemento nulo e comportamento de tela cheia/leitura no navegador. Incrementar versões dos assets alterados para evitar cache incompatível. Revisar e gravar commit 4.

## Validação e entrega
- [x] Suíte inteira `.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"` com dependências reais e dotenv desativado nos testes; node --check no JS.
- [x] Revisor de Andar aprova segurança e diff final. Capturas Playwright persistentes em pasta local ignorada: admin/leitura, claro/escuro, pausa estourada e arraste direita→esquerda. Capturas finais devem usar a aplicação Flask e templates renderizados com dados sintéticos, sem substituição textual que deixe Jinja literal no HTML; testes unitários de JS podem interceptar rede.
- [ ] Avisar Thiago antes do teste real e conferir agente 4422008 antes/depois por API agent_status e details_by_queue. Prioridade 4→4, mover Comercial→Suporte, colocar/tirar pausa, logoff; remoção não executada. Se pré-condição ou acesso faltar, registrar impedimento sem atuar em outro agente.

  **Pendente por condição externa:** o aviso foi feito e duas prévias somente GET encontraram `4422008` offline, com prioridade 4 e ausente das filas consultadas. Nenhuma ação real foi disparada. O usuário foi solicitado a logar o agente no Suporte. Retomar somente após nova prévia com as condições satisfeitas; não testar remoção da fila sem autorização na hora.
- [x] Atualizar estado com quatro commits, testes, evidências, fontes de limites e pendências. Parar sem push ou merge.
