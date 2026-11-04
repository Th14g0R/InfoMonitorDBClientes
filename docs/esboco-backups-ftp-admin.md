# Esboço Arquitetural: Gestão Administrativa de Backups FTP

**Documento:** `docs/esboco-backups-ftp-admin.md`
**Data:** 04/11/2026 (Escopo Final Aprovado)
**Status:** APROVADO pelo Usuário e Maestro em 04/11/2026 (Sem CSV e Sem Novas Permissões / Sem `ALTER TABLE usuarios`)
**Referência Visual (Mockup Estático):** [docs/esboco-backups-ftp-admin.html](esboco-backups-ftp-admin.html)

---

## 1. Fatos do Código Atual e Diretrizes Baseline

- **Rota Pública de Monitoramento:** `GET /backups-ftp` em [bancos.py#L6857](bancos.py#L6857) (`exibir_backups_ftp`). Leitura aberta sem login, servindo dados em memória de `CACHE_DADOS['backups_ftp']` gerados por `obter_status_backups_ftp()` em [bancos.py#L622](bancos.py#L622).
- **Preservação Integral da Visão Pública:** Visitantes anônimos e usuários não-master continuam com a experiência, filtros, caminhos (`c.caminho` em [bancos.py#L6775](bancos.py#L6775)) e totais rigorosamente idênticos aos atuais.
- **Ação de Alerta Diário Intacta:** `POST /admin/backups-ftp/disparar-alerta` em [bancos.py#L6911](bancos.py#L6911) permanece inalterada sob a regra existente `pode_disparar` em [bancos.py#L6894](bancos.py#L6894) (`eh_master` ou `perm_servidores`).
- **Segurança CSRF Global:** Middleware em [seguranca.py#L480](seguranca.py#L480) valida CSRF para `POST`, `PUT`, `PATCH`, `DELETE`. Todas as novas rotas administrativas utilizam o prefixo `/admin/backups-ftp/...` (fora de `/api/`).
- **Escopo Aprovado (04/11/2026):**
  - **SEM CSV:** Sem staging, parser ou rotina de importação de CSV nesta fase.
  - **SEM novas permissões:** Sem `ALTER TABLE usuarios` e sem criação de `perm_backups_ftp*`.
  - **MASTER SOMENTE:** Todo acesso administrativo exige estritamente `session.get('logged_in')` e `session.get('eh_master')`.

---

## 2. Interface e Painel Admin Sob Demanda

- **Experiência Pública:** Visitantes anônimos e operadores não-master não veem botões administrativos, credenciais ou marcações privadas. O cache público jamais armazena dados administrativos.
- **Experiência Master:** No `GET /backups-ftp`, os cards recebem um botão sutil de gerenciamento por cliente (`⚙ Gerenciar Rotinas`).
- **Painel Administrativo Sob Demanda:** Os dados confidenciais (rotinas, credenciais mascaradas e listas Cobian) são consultados e manipulados via chamadas AJAX/Fetch sob demanda nas rotas `/admin/backups-ftp/...`.
- **Cabeçalhos Mandatórios:**
  ```http
  Cache-Control: no-store, private
  Vary: Cookie
  ```
- **Higiene de DOM e Prevenção de XSS:** Toda renderização de valores recebidos é executada estritamente via `.textContent` (proibido o uso de `.innerHTML` com dados dinâmicos).

---

## 3. Modelo de Dados (Módulo `backups_ftp_admin.py`)

A inicialização das tabelas ocorre de forma autocontida via `configurar_backups_ftp_admin(bp, conectar, obter_status)`, sem migrações sobre a tabela `usuarios`:

1. **`backups_ftp_rotinas` (Processos e Credenciais por Cliente):**
   - `id`: Inteiro Chave Primária.
   - `cliente_alias`: Texto indexado (chave de ligação exata com o monitor remoto).
   - `status`: Inteiro (1 = ativo, 0 = inativo).
   - `revision`: Inteiro para concorrência otimista (prevenção de conflito 409).
   - `servidor_cifrado`, `porta_cifrada`, `usuario_cifrado`, `senha_cifrada`: Cifrados com AES-256-GCM (hipóteses da planilha).
   - `nome_rotina_cifrado`, `responsavel_cifrado`, `notas_cifradas`: Cifrados com AES-256-GCM.
   - `dados_extras_json_cifrado`: Payload JSON flexível criptografado para pares chave/valor privados.
   - `criado_em`, `atualizado_em`: Timestamp UTC.
   - `atualizado_por`: Identificador do operador Master.

2. **`backups_ftp_listas_versoes` (Versionamento de Listas Cobian `.list` / `.lst`):**
   - `id`: Inteiro Chave Primária.
   - `rotina_id`: Chave Estrangeira em `backups_ftp_rotinas(id)` (ON DELETE CASCADE).
   - `versao_numero`: Inteiro incremental por rotina (retém as 10 mais recentes).
   - `tamanho_bytes`: Inteiro com tamanho exato (limite estrito de 2 MiB / HTTP 413).
   - `encoding_detectado`: Texto (`UTF-16LE`, `UTF-8`).
   - `checksum_sha256`: Hash hexadecimal para validação de integridade.
   - `nome_original_cifrado`, `comentario_cifrado`, `conteudo_cifrado`: Cifrados em repouso.
   - `enviado_por`, `enviado_em`: Metadados do envio.

3. **`backups_ftp_auditoria` (Trilha de Auditoria Síncrona Fail-Closed):**
   - `id`: Inteiro Chave Primária.
   - `usuario_id`, `usuario_nome`, `cliente_alias`: Identificadores do operador e cliente.
   - `rotina_id`, `versao_id`: Referências dos registros envolvidos.
   - `acao`: `revelar_senha`, `copiar_senha`, `ver_conteudo_list`, `upload_list`, `criar_rotina`, `atualizar_rotina`, `excluir_rotina`.
   - `ip_origem`, `horario_utc`: Metadados da conexão.
   - *Regra Fail-Closed:* A auditoria é commitada no SQLite **antes** de qualquer descriptografia de segredo.

---

## 4. Gestão da Chave Mestra e Ciclo de Vida de Segredos

- **Chave Externa:** O `.env` armazena apenas `BACKUPS_FTP_KEY_PATH` apontando para arquivo externo protegido por ACL no sistema operacional.
- **Formato:** 44 caracteres Base64 URL-safe (32 bytes de entropia).
- **Sem Chave (HTTP 503):** Na ausência da chave, rotas de segredos retornam 503 sem travar o monitor público.
- **Interface e Senhas:** Senhas nunca são enviadas no HTML/JSON inicial (`senha_configurada: bool`). Revelar e Copiar exigem `POST` com CSRF, são auditados e utilizam temporizador de 20s para re-mascaramento.
- **Listas Cobian:** Suporte transparente a extensões `.lst` e `.list` (sinônimos), limite de 2 MiB, sem download bruto ou execução remota.

---

## 5. Cronograma e Fases de Implementação

1. **Fase 1 — Isolamento e Proteções de Repositório (Concluída):** Enriquecimento de `.gitignore` e `.graphifyignore`, salvaguarda de `.env.example` e documentação `docs/LEIAME-chave-backups-ftp.md`.
2. **Fase 2 — Backend Administrativo e Testes de Domínio (Thiago):** `backups_ftp_admin.py` com rotinas AES-GCM, concorrência otimista e auditoria fail-closed.
3. **Fase 3 — Integração e Interface Web (Rapadura):** `bancos.py` (chamada mínima de configuração), `templates/backups_ftp_admin.html`, `static/backups_ftp_admin.js` e `.css`.
4. **Fase 4 — Bateria de Testes Web de Integração (Rapadura):** `tests/test_backups_ftp_admin_web.py` cobrindo regressão pública, guard Master, CSRF, fail-closed e proteção de segredos.
