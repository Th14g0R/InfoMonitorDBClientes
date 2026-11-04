# Plano de Implementação: Gestão Administrativa de Backups FTP

**Data:** 04/11/2026
**Status:** Concluído
**Propriedades / Responsabilidades:**
- **Rapadura (`Antigravity Andar1 · Rapadura`):** `bancos.py` (integração mínima), `templates/backups_ftp_admin.html`, `static/backups_ftp_admin.js`, `static/backups_ftp_admin.css`, `tests/test_backups_ftp_admin_web.py`, `.gitignore`, `.graphifyignore`, `.env.example`, `docs/esboco-backups-ftp-admin.md`, `docs/LEIAME-chave-backups-ftp.md`, este plano.
- **Thiago #2 (`Antigravity Thiago #2`):** `backups_ftp_admin.py`, `tests/test_backups_ftp_admin.py`.

---

## 1. Contrato Técnico Consolidado

- **Ponto de Integração:** Em `bancos.py`, importar e acionar `configurar_backups_ftp_admin(bancos_bp, conectar=get_db_connection, obter_status=obter_status_backups_ftp)`.
- **Rotas Administrativas (Prefixadas em `/admin/backups-ftp/...`):**
  - `GET /admin/backups-ftp/cliente/<alias>/rotinas`
  - `POST /admin/backups-ftp/rotina`
  - `PUT /admin/backups-ftp/rotina/<int:id>`
  - `DELETE /admin/backups-ftp/rotina/<int:id>`
  - `POST /admin/backups-ftp/rotina/<int:id>/revelar-senha`
  - `POST /admin/backups-ftp/rotina/<int:id>/copiar-senha`
  - `GET /admin/backups-ftp/rotina/<int:id>/listas`
  - `POST /admin/backups-ftp/rotina/<int:id>/upload-list`
  - `POST /admin/backups-ftp/rotina/<int:id>/lista/<int:versao_id>/ver-conteudo`
- **Controle de Acesso:** Estritamente `session.get('logged_in')` e `session.get('eh_master')`.
- **Headers Mandatórios:** `Cache-Control: no-store, private` e `Vary: Cookie`.
- **Proteção CSRF:** Métodos mutantes exigem `X-CSRF-Token` ou campo no formulário; requisições sem token retornam HTTP 400.

---

## 2. Checklist de Execução

### Fase 1: Proteções de Repositório e Documentação (Concluída)
- [x] Atualizar `.gitignore` com `uploads/`, `runtimeprivado/`, `*.lst`, `*.list`, `secrets/`, `dataprivada/`, `backups/`.
- [x] Atualizar `.graphifyignore` com exclusões equivalentes.
- [x] Adicionar placeholder `BACKUPS_FTP_KEY_PATH` em `.env.example` sem dados sensíveis.
- [x] Criar `docs/LEIAME-chave-backups-ftp.md`.
- [x] Atualizar `docs/esboco-backups-ftp-admin.md` refletindo o escopo aprovado em 04/11/2026.

### Fase 2: Integração no Backend `bancos.py` (Concluída)
- [x] Importar `configurar_backups_ftp_admin` em `bancos.py`.
- [x] Chamar `configurar_backups_ftp_admin(bancos_bp, conectar=get_db_connection, obter_status=obter_status_backups_ftp)`.
- [x] Na rota `GET /backups-ftp` (`exibir_backups_ftp`), se usuário for Master, emitir headers `Cache-Control: no-store, private` e `Vary: Cookie`, e disponibilizar indicador no contexto de renderização.

### Fase 3: Interface Web (Templates e Static) (Concluída)
- [x] Criar `templates/backups_ftp_admin.html` (modal/drawer seguro para gestão de rotinas e listas Cobian).
- [x] Integrar inclusão do modal e botão de ação nos cards de clientes em `HTML_BACKUPS_FTP` (visível apenas para Master).
- [x] Criar `static/backups_ftp_admin.js`:
  - Carregamento assíncrono sob demanda de rotinas por cliente.
  - Manipulação de senhas mascaradas sem valor no HTML inicial.
  - Ações Revelar e Copiar com auditoria, temporizador visual de 20s e remascaramento.
  - Upload e inspeção de arquivos `.list` / `.lst` com renderização segura via `.textContent` (proibido `.innerHTML`).
  - Tratamento de concorrência com `revision` (aviso amigável em caso de HTTP 409).
- [x] Criar `static/backups_ftp_admin.css` com estilos integrados ao design system existente (`ui.css`).

### Fase 4: Bateria de Testes Web de Integração (Concluída)
- [x] Criar `tests/test_backups_ftp_admin_web.py`:
  - Teste 1: Regressão pública de `GET /backups-ftp` (anônimo vê layout baseline, sem botões/dados privados).
  - Teste 2: Guardião de autenticação (anônimo redireciona para login 302; POST sem CSRF retorna 400).
  - Teste 3: Guardião de autorização (usuário logado não-master recebe HTTP 403 em todos os endpoints `/admin/backups-ftp/...`).
  - Teste 4: Integração Master completa (CRUD de rotina, concorrência otimista 409 ao divergir `revision`).
  - Teste 5: Ciclo de senhas (revelar e copiar geram auditoria no SQLite; resposta contém headers `no-store, private`).
  - Teste 6: Gestão de listas Cobian (upload de `.list` e `.lst`, rejeição de arquivo > 2 MiB com 413, versionamento até 10 versões, visualização de conteúdo auditada).
  - Teste 7: Fail-closed na ausência de chave (HTTP 503 sem quebrar o monitoramento público).
- [x] Executar bateria com `C:\Python314\python.exe` e confirmar 100% verde (9 testes em 0.76s).
- [x] Atualizar Graphify com `graphify update .` (`GRAPHIFY_BACKEND=none`).
- [x] Reportar entrega final ao Maestro1 via `maestri ask "Maestro1"`.
