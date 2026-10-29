# Plano de Implementação: Exportação de Ausências para Excel (/horarios/exportar_ausencias)

## 1. Objetivo
Implementar a funcionalidade de exportação em planilha Excel (.xlsx) das ausências cadastradas em `/horarios`, expandidas dia a dia, com suporte aos filtros de tela (`ausencia_inicio`, `ausencia_fim`, `ausencia_motivo`), respeitando permissões (`perm_horarios` ou `eh_master`), com prevenção contra injeção de fórmulas e layout profissional.

## 2. Regras de Negócio e Comportamento

### 2.1 Rota e Parâmetros
- **Rota**: `GET /horarios/exportar_ausencias`
- **Filtros (Query params)**:
  - `ausencia_inicio` (ISO `YYYY-MM-DD`, opcional)
  - `ausencia_fim` (ISO `YYYY-MM-DD`, opcional)
  - `ausencia_motivo` (texto, opcional)
- **Validação**:
  - `validar_data(valor)` para datas informadas.
  - Se `ausencia_inicio` e `ausencia_fim` informados: `ausencia_inicio <= ausencia_fim`.
  - Retorna `HTTP 400` ("Informe um período válido.") se inválido.

### 2.2 Permissão e Segurança
- Requer login ativo (`@login_required`).
- Requer permissão `pode_editar_horarios()` (`eh_master` ou `perm_horarios` ativo no banco); retorna `HTTP 403` caso não autorizado.
- Prevenção contra formula injection: strings que comecem com `=`, `+`, `-`, `@` são escapadas/prefixadas para evitar execução indevida no Excel.

### 2.3 Filtros vs. Sem Filtros
- **Sem filtros (`not filtro_ausencias`)**:
  - Mantém o comportamento da tela: ausências vigentes e futuras (`data_fim >= hoje`).
  - Não inventa mês corrente.
  - Recorta o início da expansão em `hoje` (`max(data_inicio, hoje)`).
- **Com filtros (`filtro_ausencias == True`)**:
  - `limite_inicio = ausencia_inicio or '0001-01-01'`
  - `limite_fim = ausencia_fim or '9999-12-31'`
  - Busca ausências que interceptam o período: `data_fim >= limite_inicio AND data_inicio <= limite_fim AND (? = '' OR motivo = ?)`.
  - Expansão inclusiva recortada estritamente nos limites filtrados:
    - `dia_inicio = max(data_inicio, limite_inicio)`
    - `dia_fim = min(data_fim, limite_fim)`
  - Se `ausencia_inicio` não foi informada mas há outros filtros, não recorta em `hoje`.

### 2.4 Expansão Dia a Dia e Formato das Colunas
- Cada ausência com intervalo de $N$ dias gera $N$ linhas na planilha (recortadas pelo limite).
- Preserva todas as ausências distintas sem perder motivos sobrepostos.
- Colunas:
  1. **Data**: Objeto `datetime.date` com `number_format = 'dd/mm/yyyy'` (alinhamento centro).
  2. **Dia da semana**: Texto em português (`Segunda-feira`, `Terça-feira`, etc.) (alinhamento centro).
  3. **Funcionário**: Nome do técnico (alinhamento esquerda).
  4. **Motivo**: Motivo cadastrado (alinhamento esquerda).
  5. **Horário**: Se `hora_inicio` e `hora_fim` presentes: `f"{hora_inicio} - {hora_fim}"`; senão `Dia Inteiro` (alinhamento centro).
- **Ordenação**: `Data` crescente, depois `Funcionário` (nome) crescente.

### 2.5 Estilo e Formatação da Planilha
- Cabeçalho estilizado: fundo azul, texto branco em negrito.
- Congelamento da primeira linha (`freeze_panes = 'A2'`).
- Filtro automático ativo (`auto_filter.ref`).
- Ajuste automático de largura das colunas com limites mínimos e de respiro.
- Caso não haja registros: retorna a planilha com cabeçalho formatado e zero linhas de dados.

### 2.6 Interface (HTML)
- No card `#secao-ausencias`, dentro do formulário de filtros:
  - Adicionar botão "📥 Exportar Ausências (Excel)" visível apenas para usuários autorizados (`{% if pode_editar_horarios() %}`).
  - O link preserva os parâmetros ativos: `ausencia_inicio`, `ausencia_fim`, `ausencia_motivo`.

## 3. Arquivos Envolvidos
- `horarios.py`:
  - Função auxiliar `gerar_planilha_ausencias(conn, ausencia_inicio=None, ausencia_fim=None, ausencia_motivo=None)`
  - Rota `exportar_ausencias()` com `@horarios_bp.route('/horarios/exportar_ausencias')`
  - Bloco `#secao-ausencias` do template HTML embutido.
