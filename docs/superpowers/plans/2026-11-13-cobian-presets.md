# Gerador de Listas Cobian 11 com Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implementar gerador assistido de arquivos `.lst` para Cobian Backup 11 baseado em presets (FBK 13h/23h, FDB 23h e Personalizado) com herança de dados FTP da rotina, permitindo download e salvamento auditado como versão no monitor.

**Architecture:** Módulo puro de serialização sintática (`cobian_generator.py`) desacoplado de IO para formatar arquivos compatíveis com Cobian 11 (UTF-16LE BOM, CRLF, envelope `{11// ... //11}`, formato compacto `Chave=Valor`, GUIDs e Date/Time em float IEEE 754 de 64 bits), integrado a rotas administrativas em `backups_ftp_admin.py` que seguem o padrão transacional existente (`BEGIN IMMEDIATE`, `MAX+1`, retenção a 10 versões, AAD `list:{id}:{v}:*`, HTTP 201 e auditoria fail-closed), com interface web em `templates/backups_ftp_admin.html` e `static/backups_ftp_admin.js`.

**Tech Stack:** Python 3.14 / Flask, SQLite3 com criptografia AEAD (AES-256-GCM), JavaScript vanilla (ES6+), HTML5/CSS, `unittest`.

**Spec:** [`docs/superpowers/specs/2026-11-13-cobian-presets-design.md`](file:///C:/TEMP/site/InfoMonitorDBClientes/docs/superpowers/specs/2026-11-13-cobian-presets-design.md)

---

## Global Constraints

- **Encoding & Envelope:** Saída em `UTF-16LE` com BOM `\xFF\xFE`, quebras de linha estritas em CRLF (`\r\n`), cabeçalho `{11//\r\n` e rodapé `//11}\r\n`.
- **Gramática Compacta:** Formato nativo `Chave=Valor`, sem espaços ao redor do `=`, conforme as amostras reais.
- **Ressalva de Compatibilidade:** A validação sintática existente (`_validar_e_processar_lista()`) realiza checagem estrutural básica e não substitui a importação funcional no executável Cobian Backup 11 em ambiente Windows.
- **Credenciais & Tripla Correspondência no Reaproveitamento:** Não inventar algoritmo de cifra. Exibir aviso visível de que a senha deve ser digitada no Cobian. O reaproveitamento de hash de senha é estritamente opcional (*opt-in*), exige correspondência exata de `host` + `porta` + `usuario` com a versão de referência, consulta via `WHERE id = :versao_id AND rotina_id = :rotina_id`, e recusa destinos múltiplos ou hash vazio.
- **Padrão Transacional e AAD Existente:** Rotas de geração usam `BEGIN IMMEDIATE`, `MAX(versao_numero) + 1`, cifragem AAD `list:{id}:{v}:nome`, `list:{id}:{v}:comentario` e `list:{id}:{v}:conteudo`, retenção constante de 10 versões, auditoria transacional e retorno HTTP **201** com objeto de versão.
- **Separação de Comentários:** O payload separa `comentario_versao` (para a tabela do monitor) de `comentario` da tarefa (para a chave `Comment = ...` do `.lst`).
- **Headers de Resposta:** Todas as respostas utilizam o helper existente `_resposta_com_headers` (`no-store`, `private`, `Vary: Cookie`).
- **Auditoria Fail-Closed no Download:** A rota de download gera e grava auditoria commitada com o hash SHA-256 do arquivo antes de entregar os bytes.
- **Sem Mutação Operacional:** Não executar eventos locais e não alterar o serviço Firebird no monitor; perfil FDB apenas emite declaração de texto dos comandos.
- **Segurança de Acesso:** Usuário anônimo redireciona para login (302/redirect), usuário logado sem perfil Master recebe HTTP 403, e validação de token CSRF obrigatória nas novas rotas.

---

## Review Focus

1. **Injeção de CRLF:** Entrada com `\r` ou `\n` em nomes de tarefa, caminhos ou comentários deve retornar HTTP 400.
2. **Reaproveitamento com Divergência de Porta ou Usuário:** Tentativa de reaproveitar hash de versão anterior onde a porta difere (ex: 8821 no gerador vs 21 na lista anterior) ou o usuário difere deve ser recusada com HTTP 400.
3. **Reaproveitamento com Hash Vazio ou Versão de Outra Rotina:** Consulta com `versao_id` que não pertence à `rotina_id` ou cuja lista anterior tenha `FTP Password=` vazio deve retornar erro claro e não reaproveitar.
4. **Igualdade Exata de Date/Time com Amostras Reais:** Os horários 13:00 e 23:00 devem gerar strings binárias de 64 bits matematicamente idênticas às extraídas das amostras de produção (`hiperbompreco-2225.lst`).
5. **Controle de Acesso e CSRF:** Requisição anônima deve ser redirecionada para login (302), requisição autenticada sem perfil Master deve receber HTTP 403, e requisição sem token CSRF válido deve falhar com HTTP 400.

---

### Task 1: Módulo Gerador Cobian Core (`cobian_generator.py`)

**Files:**
- Create: `cobian_generator.py`
- Test: `tests/test_cobian_generator.py`

**Interfaces:**
- Produces:
  - `codificar_horario_cobian(horario_hhmm: str, dias_base: float = 46339.0) -> str`: Retorna string binária de 64 caracteres do float IEEE 754 de 64 bits para o horário informado.
  - `sanitizar_campo_cobian(valor: str, max_len: int = 260) -> str`: Valida ausência de CRLF e caracteres de controle.
  - `montar_tarefa_cobian(params: dict, guid: str | None = None) -> str`: Gera bloco de tarefa no formato compacto `Chave=Valor`.
  - `gerar_arquivo_cobian_lst(dados_lista: dict) -> bytes`: Empacota envelope `{11//\r\n ... //11}\r\n` codificado em UTF-16LE com BOM `\xFF\xFE`.

- [x] **Step 1: Escrever teste unitário com falha para serialização, gramática `Chave=Valor` e equivalência dos horários Date/Time com amostras reais, sem reproduzir jitter de milissegundos**
- [x] **Step 2: Executar teste para verificar falha de importação**
- [x] **Step 3: Implementar `cobian_generator.py`**
- [x] **Step 4: Executar teste para verificar aprovação**
- [x] **Step 5: Adicionar teste com fixture completa sanitizada e validação estrutural básica**

---

### Task 2: Presets e Reaproveitamento com Tripla Correspondência

**Files:**
- Modify: `cobian_generator.py`
- Test: `tests/test_cobian_generator.py`

**Interfaces:**
- Produces:
  - `obter_preset_fbk(alias: str, caminho_fbk: str | None = None, caminho_exe: str | None = None, pausa: int = 1800) -> list[dict]`
  - `obter_preset_fdb(alias: str, caminho_fdb: str | None = None, servico: str = 'FirebirdServerDefaultInstance', pausa: int = 60) -> list[dict]`
  - `extrair_hash_senha_compativel(conteudo_lst: str, host: str, porta: str, usuario: str) -> str`: Extrai o `FTP Password` se e somente se `host`, `porta` e `usuario` coincidirem perfeitamente em todas as tarefas com destino FTP. Lança `ValueError` se houver divergência, destinos múltiplos conflitantes ou hash vazio/inválido.

- [x] **Step 1: Escrever testes unitários para presets e tripla correspondência de credenciais**
- [x] **Step 2: Executar testes para verificar falha**
- [x] **Step 3: Implementar presets e função `extrair_hash_senha_compativel` em `cobian_generator.py`**
- [x] **Step 4: Executar testes para verificar aprovação**

---

### Task 3: Endpoints Administrativos Backend em `backups_ftp_admin.py`

**Files:**
- Modify: `backups_ftp_admin.py`
- Test: `tests/test_backups_ftp_admin.py`

**Interfaces:**
- Consumes:
  - `cobian_generator.gerar_arquivo_cobian_lst`
  - `cobian_generator.extrair_hash_senha_compativel`
  - `cobian_generator.validar_porta_cobian`
  - `cobian_generator.validar_host_cobian`
  - `_validar_e_processar_lista`
  - `cifrar_dado`
  - `decifrar_dado`
  - `_resposta_com_headers`
- Produces:
  - Helper unificado `_preparar_dados_gerador_cobian(rotina, payload, cursor)` eliminando duplicação de validações.
  - `POST /admin/backups-ftp/rotina/<int:id>/gerar-lista`: Retorna HTTP 201 com objeto de versão e auditoria com metadados.
  - `POST /admin/backups-ftp/rotina/<int:id>/download-lista-gerada`: Retorna binário para download com auditoria fail-closed.

- [x] **Step 1: Escrever testes de integração para as rotas e controle de acesso**
- [x] **Step 2: Executar testes para verificar falha (404)**
- [x] **Step 3: Implementar endpoints em `backups_ftp_admin.py`**
- [x] **Step 4: Executar testes para verificar aprovação**

---

### Task 4: Interface do Usuário (Frontend)

**Files:**
- Modify: `templates/backups_ftp_admin.html`
- Modify: `static/backups_ftp_admin.js`
- Test: `tests/test_backups_ftp_admin_web.py`

**Interfaces:**
- Produces:
  - Botão `#btn-abrir-gerador-list` na Visão 3 de Listas Cobian.
  - Submodal `#modal-sub-gerar-lista-cobian`:
    - Campo separado `#ftp-gerador-comentario-versao` (comentário da versão).
    - Seletor de presets `#ftp-gerador-preset` (`fbk`, `fdb`, `custom`).
    - Alerta visível de senha do Cobian 11.
    - Checkbox `#ftp-check-reaproveitar-hash` (desabilitado se não houver versões anteriores disponíveis).
    - Tarefas com inputs individuais (incluindo `pos_evento_tipo`, `pos_evento_caminho` e `comentario` da tarefa).
    - Gerenciamento de foco: foco inicial em `#ftp-gerador-preset` e restauração ao botão de abertura no fechamento.
    - Download direto com verificação de redirecionamento de sessão expirada.
    - Botões `#btn-salvar-versao-gerada` e `#btn-baixar-lista-gerada`.

- [x] **Step 1: Escrever teste de integração web verificando o markup dos elementos e separação dos campos de comentário**
- [x] **Step 2: Executar teste para verificar falha**
- [x] **Step 3: Implementar markup e lógica JavaScript**
- [x] **Step 4: Executar testes de integração web para verificar aprovação**

---

### Task 5: Validação Focada de Testes e Entrega

**Files:**
- Test: Suítes de testes de unidade e integração do escopo

- [x] **Step 1: Executar suítes focadas do escopo com dotenv desativado**
  - `tests/test_cobian_generator.py` (11 testes OK)
  - `tests/test_backups_ftp_admin.py` (37 testes OK)
  - `tests/test_backups_ftp_admin_web.py` (14 testes OK)
  - Total: 62 testes focados passando com 0 falhas e 0 erros. A suíte completa global será executada pelo Maestro1 para fechamento.

- [x] **Step 2: Verificar atualização do grafo Graphify local no Andar 1**
  - Conforme instrução expressa, não rodar update de grafo enquanto agente parceiro (Rapadura) estiver em execução paralela no andar.

- [x] **Step 3: Verificar status do Git e escopo de arquivos**
  - Arquivos de posse exclusiva respeitados (`cobian_generator.py`, `backups_ftp_admin.py`, `static/backups_ftp_admin.js`, `templates/backups_ftp_admin.html`, testes e documentação). Nenhuma alteração realizada em `agentes.js`, `agentes.css` ou `templates/agentes.html`.
