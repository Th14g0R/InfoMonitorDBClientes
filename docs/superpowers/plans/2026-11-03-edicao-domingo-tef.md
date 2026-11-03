# Edição de lançamentos de domingo TEF

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Permitir editar data, funcionário e observação de um lançamento existente do suporte TEF de domingo.

**Architecture:** Manter Flask/SQLite e o padrão de formulário de edição da escala de sábado. Adicionar GET/POST por ID e botão Editar no cartão de domingo, preservando identidade do lançamento e registros de auditoria.

**Tech Stack:** Python, Flask, SQLite, Jinja, unittest.

**Spec:** Pedido do usuário nesta sessão: editar datas e lançamentos existentes para trocas na escala de domingo do suporte TEF.

## Global Constraints

- Permissões existentes de edição de horários e proteção CSRF.
- Sem mudança de schema, dependências ou regras do cadastro/exclusão.
- Data válida e domingo; funcionário ativo e participante do sobreaviso de domingo.
- Não sobrescrever outro lançamento na data de destino.
- Campos preenchidos com valores atuais; horário permanece Sobreaviso TEF.
- Registrar auditoria antes/depois; exibir mensagem clara de validação e manter dados diante de erro.

## Review Focus

- Acesso público e usuário sem permissão não podem editar nem carregar formulário protegido.
- Mudança para data ocupada preserva os dois registros.
- Datas inválidas e IDs de funcionário inválidos não alteram dados.
- Observação e nome escapados no HTML; POST protegido por CSRF.
- Registro inexistente e retorno ao período correto da escala.

## Abordagem

Formulário próprio preenchido segue o padrão existente de sábado. Edição inline exigiria JavaScript e controle adicional de estado; reaproveitar o cadastro não permite identificar claramente o registro cuja data mudou. A decisão de implementação é o formulário próprio, por manter o comportamento da aplicação.

### Tarefa 1: Testes de regressão da edição

**Files:** `tests/test_seguranca.py`

**Interfaces:** GET/POST `/horarios/editar_item_escala_domingo/<int:id>`; campos `data_domingo`, `funcionario_id`, `observacao`, `csrf_token`.

- [x] Criar testes para edição preenchida, salvamento no mesmo ID e auditoria; validar domingo, funcionário elegível, colisão, permissões, CSRF e registro inexistente.
- [x] Executar testes novos e confirmar falha pela ausência da rota.

### Tarefa 2: Formulário e atualização

**Files:** `horarios.py`

**Interfaces:** Endpoint `editar_item_escala_domingo(id)` GET/POST e link no cartão de domingo.

- [x] Implementar formulário preenchido com seleção de funcionário elegível, data e observação.
- [x] Implementar atualização por ID com validação, transação e auditoria antes/depois.
- [x] Redirecionar ao domingo editado, preservando a localização da seção.
- [x] Rodar testes da tarefa 1 e o fluxo existente de domingo.

### Tarefa 3: Revisão e validação

- [x] Claude revisa o diff sem editar.
- [x] Maestro valida testes e diff; executores atualizam Graphify com GRAPHIFY_BACKEND=none.
- [x] Entregar alteração revisada sem atualizar produção.
