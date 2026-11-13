# Especificação Técnica — Gerador de Listas Cobian 11 com Presets (`.lst`)

> **Documento:** `docs/superpowers/specs/2026-11-13-cobian-presets-design.md`
> **Status:** Revisado conforme alinhamento de segurança e arquitetura (Sem código de produto)
> **Base de Referência:** [`docs/relatorio_investigacao_cobian_lst.md`](file:///C:/TEMP/site/InfoMonitorDBClientes/docs/relatorio_investigacao_cobian_lst.md)
> **Alvo de Estrutura:** Cobian Backup 11 (Gravity)

---

## 1. Visão Geral e Propósito

Atualmente, o painel administrativo de Backups FTP (`/admin/backups-ftp`) permite gerenciar credenciais de conexão e fazer upload/inspeção de arquivos `.list` e `.lst` pré-existentes. No entanto, não há no sistema uma ferramenta que gere esses arquivos de forma assistida.

Esta especificação define o design do **Gerador de Listas Cobian 11 baseado em Presets**. O objetivo é permitir que o operador administrativo gere arquivos `.lst` estruturados para o Cobian Backup 11 diretamente pelo painel web, herdando os dados da rotina FTP selecionada, com templates rápidos (presets) e capacidade de ajuste fino por tarefa, sem risco de injeção ou vazamento de segredos.

---

## 2. Padrão Estrutural e Gramática Cobian 11 (Gravity)

Todo arquivo gerado deve seguir as características estruturais identificadas nas amostras de produção:

1. **Codificação e BOM:**
   - Encoding: `UTF-16LE` (Little-Endian).
   - BOM Obrigatório: `\xFF\xFE` nos primeiros 2 bytes.
   - Separador de Linhas: Estritamente CRLF (`\r\n`).
2. **Envelope Cobian 11 por Tarefa:**
   - No Cobian 11 (Gravity), cada tarefa possui seu próprio envelope `{11//\r\n` ... `//11}\r\n`, mantendo a delimitação de cada tarefa individual.
3. **Delimitação de Tarefas e Formato das Propriedades:**
   - Cada tarefa inicia com a chave `Id={GUID}` onde o GUID é gerado dinamicamente (UUID v4 em maiúsculas entre chaves).
   - Propriedades formatadas no padrão nativo do Cobian 11, sem espaços ao redor do sinal de igual: `Chave=Valor` (ex: `Name=...`, `Source="0,..."`).
4. **Codificação de Data e Hora (`Date/Time`):**
   - No Cobian 11, o agendamento horário diário é armazenado como uma string binária de 64 caracteres (`0`s e `1`s).
   - Essa string representa o valor binário exato de um ponto flutuante IEEE 754 de 64 bits em big-endian (`double`), correspondente ao tipo `TDateTime` do Delphi:
     $$\text{TDateTime} = \text{Dias desde } 30/12/1899 + \frac{\text{hora} \times 3600 + \text{minuto} \times 60}{86400}$$
   - O gerador converte o horário `HH:MM` escolhido para essa representação binária de 64 bits sem uso de constantes mágicas, suportando injeção de `base_date` (default `datetime.date.today()`) para comparações determinísticas em testes.
5. **Formatação de Destino FTP (`Destination`) e Contagem Exata de Aspas:**
   - O destino remoto possui o prefixo `2,` (indicando armazenamento FTP), seguido por parâmetros delimitados por aspas quadruplicadas (`""""`):
     `"2,""""""FTP Host=<host>"""",""""FTP Port=<porta>"""",""""FTP User name=<usuario>"""",""""FTP Password=<hash_ou_vazio>"""",""""FTP Initial directory=<dir>"""",..."`
   - No subbloco de proxy, o encerramento de `Proxy use IPv6=false` contém exatamente 12 aspas (`""""""""""""`), seguido de vírgula e 4 aspas (`""""`) antes de `FTP SSL=`.
   - **Proxy password intencionalmente vazio:** O campo `Proxy password=` permanece vazio (`Proxy password=`) por padrão seguro, uma vez que o proxy está desativado (`Proxy type=0`). Nenhuma senha codificada de proxy de produção é reutilizada.
   - Parâmetros fixos de compatibilidade: `FTP Passive=true`, `FTP Passive host=true`, `FTP Use MLIS=true`, `FTP Use HOST=true`, `FTP SSL=` (desativado/zerado), `FTP Proxy=` (desativado/zerado), timeouts padrão (Connect=20000, Read=60000, Listen=10000, KeepAlive=true).
6. **Ruling de Campos de Senha Auxiliares (Segurança e Sanitização):**
   - **`Passphrase=` e `Run as Password=`:** Permanecem intencionalmente VAZIOS (`Passphrase=`, `Run as Password=`) em consonância com as flags desativadas `Encryption=0` (sem criptografia de arquivo) e `Impersonate=false` (sem impersonação de usuário Windows), exatamente igual a `Proxy password=`. Nunca copiar valores codificados reais desses campos de amostras de produção.
   - **Validação Operacional Pendente:** A aceitação desses campos vazios em produção depende da validação de importação no executável oficial do Cobian Backup 11 em ambiente Windows.

> [!WARNING]
> **Ressalva de Compatibilidade:** A validação sintática existente no sistema (`_validar_e_processar_lista()`) realiza apenas checagem estrutural básica (BOM, delimitadores `{ ... }` e presença de propriedades) e **NÃO constitui prova absoluta de compatibilidade**. O teste definitivo de aceitação operacional requer a importação do arquivo no software executável Cobian Backup 11 em ambiente Windows.

---

## 3. Catálogo de Presets

O formulário deve oferecer três modos de geração:

### 3.1. Preset 1: Padrão InfoBackup (FBK) — Recomendado
- **Cenário:** Clientes que utilizam o utilitário `infoBackup.exe` para gerar cópias `.fbk` periódicas.
- **Número de Tarefas:** 2 tarefas diárias.
  - **Tarefa 1 (Almoço):** Nome `IMS-{alias}-13h`, Horário `13:00`.
  - **Tarefa 2 (Noite):** Nome `IMS-{alias}-23h`, Horário `23:00`.
- **Origem (`Source`):** `"0,C:\Infobrasil\infoBackup\backup\{alias}\backup.fbk"`.
- **Pré-evento (`Pre backup events`):** `"EXECUTE,C:\Infobrasil\infoBackup\infoBackup.exe,","PAUSE,1800,"` (executa o dump e aguarda até 30 minutos).
- **Pós-evento (`Post backup events`):** Vazio.

### 3.2. Preset 2: Banco Direto (FDB)
- **Cenário:** Clientes que realizam a cópia a frio do arquivo de banco Firebird ativo.
- **Número de Tarefas:** 1 tarefa diária.
  - **Tarefa 1 (Madrugada/Noite):** Nome `FDB-{alias}-23h`, Horário `23:00`.
- **Origem (`Source`):** `"0,C:\Infobrasil\Dados\DADOS.FDB"`.
- **Pré-evento (`Pre backup events`):** `"STOP_SERVICE,FirebirdServerDefaultInstance,","PAUSE,60,"` (solicita parada do serviço Firebird e aguarda 60s).
- **Pós-evento (`Post backup events`):** `"START_SERVICE,FirebirdServerDefaultInstance,"` (reinicia o serviço imediatamente após a conclusão da cópia).
- **Segurança de Execução:** O Monitor **não executa comandos locais nem altera serviços Firebird**; ele apenas emite a declaração textual dessas instruções para execução pelo Cobian no cliente. O formulário exibe um aviso visível antes da geração alertando sobre a parada temporária do banco.

### 3.3. Preset 3: Personalizado
- Permite definir $N$ tarefas (adicionar/remover linhas até o limite de 10).
- Inicializa com 1 tarefa em branco ou com template configurável.
- Cada tarefa pode ter seu nome, horário, origem e eventos customizados.

---

## 4. Campos Editáveis, Comentários e Opções Avançadas

### 4.1. Dados Globais da Conexão FTP (Herança da Rotina)
- **Host FTP:** Pré-preenchido com `servidor` da rotina (editável).
- **Porta FTP:** Pré-preenchido com `porta` da rotina (padrão `8821` se vazio, editável).
- **Usuário FTP:** Pré-preenchido com `usuario` da rotina (editável).
- **Diretório Inicial:** Padrão `/` (editável).
- **Alias do Cliente:** Herdado de `cliente_alias` para compor nomes e comentários padrão.

### 4.2. Separação de Comentários
Para evitar ambiguidade entre os dados da versão e os dados do arquivo gerado:
- **`comentario_versao`:** Comentário descritivo da versão no histórico do Monitor (armazenado na coluna `comentario_cifrado` de `backups_ftp_listas_versoes`).
- **`comentario` da Tarefa:** Texto de observação individual de cada tarefa inserido na diretiva `Comment = ...` dentro do arquivo `.lst`.

### 4.3. Campos Editáveis por Tarefa
1. **Nome da Tarefa:** String alfa-numérica sanitizada.
2. **Ativo (`Enabled`):** `true` ou `false` (checkbox).
3. **Caminho de Origem:** Caminho Windows absoluto do arquivo/pasta (ex: `C:\Infobrasil\...`).
4. **Horário:** Seletor de hora e minuto (`HH:MM`).
5. **Pré-evento Executável:** Caminho do utilitário (ex: `C:\Infobrasil\infoBackup\infoBackup.exe`).
6. **Tempo de Espera (Pausa):** Inteiro em segundos (padrão 1800 para FBK, 60 para FDB).
7. **Serviço Windows (Pré/Pós):** Nome do serviço a parar/iniciar (padrão `FirebirdServerDefaultInstance`).
8. **Comentário da Tarefa:** Identificação interna da tarefa no `.lst`.

### 4.4. Opções Avançadas Comuns (Fixas no Gerador)
Mantidas fixas no código do gerador com os valores observados nas amostras:
- `Group = IMS`
- `Include subdirectories = true`
- `Create separated backups = false`
- `Use attributes = true`
- `Use VSC = true` (VSS)
- `Backup type = 0` (Full)
- `Priority = 0` (Normal)
- `Compression = 1` (Zip)
- `Clear archive attribute = true`
- `Delete empty directories = false`

---

## 5. Política de Credenciais e Tratamento da Senha FTP

> [!IMPORTANT]
> **Sem Algoritmo Inventado e Sem Senha Vazia Silenciosa:**
> O Cobian 11 Gravity cifra senhas com um mecanismo proprietário em Delphi (`cbLegED.dll`). Não inventaremos algoritmos de criptografia especulativos.

1. **Aviso Visível Obrigatório na Interface:**
   - O modal de geração exibe um alerta em destaque:
     `"ℹ A senha do FTP não é gravada em texto plano no Cobian 11. Após importar a lista no cliente, abra as propriedades da tarefa no Cobian e digite a senha da rotina para que o Cobian gere a senha codificada localmente (ou marque abaixo para reaproveitar a senha codificada de uma versão anterior compatível)."`
2. **Regras Estritas de Reaproveitamento de Hash (Opt-in Explícito):**
   - O operador pode optar por reaproveitar a senha codificada de uma versão anterior da lista (`reaproveitar_hash_versao_id`).
   - O hash da senha no Cobian deve validar estritamente como Base64 (`^[A-Za-z0-9+/]+={0,2}$`).
   - **Validações Obrigatórias de Segurança:**
     1. **Isolamento de Rotina:** A consulta ao banco deve obrigatoriamente validar que a versão pertence à rotina em edição:
        ```sql
        SELECT conteudo_cifrado FROM backups_ftp_listas_versoes
        WHERE id = :versao_id AND rotina_id = :rotina_id
        ```
     2. **Tripla Correspondência de Destino:** O `FTP Host`, a `FTP Port` e o `FTP User name` configurados no gerador devem ser **estritamente idênticos** aos encontrados no bloco `Destination` da lista de referência. Se qualquer um divergir (ex: porta 8821 vs 21, ou host alterado), o reaproveitamento é **recusado com HTTP 400**.
     3. **Destinos Múltiplos ou Divergentes:** Se a lista anterior contiver tarefas com destinos apontando para servidores diferentes ou senhas distintas, o reaproveitamento é **recusado com HTTP 400**.
     4. **Hash Vazio:** Se a versão de referência possuir `FTP Password=` vazio ou ausente, a opção é rejeitada com mensagem informando que a versão de referência não possui senha codificada gravada.
     5. **Proibição Cruzada:** É terminantemente proibido referenciar versões de outras rotinas ou clientes.
3. **Comportamento Padrão (Sem Reaproveitamento):**
   - O campo no arquivo gerado é emitido como `FTP Password=` (vazio), deixando a senha para ser preenchida na interface do Cobian no cliente (sujeito à validação de importação real).

---

## 6. Sanitização, Limites e Proteção contra Injeção

1. **Injeção de Linhas (CR/LF) e Controles Estendidos:**
   - Todos os campos textuais recebidos (nomes, caminhos, hosts, usuários, comentários) são rigorosamente validados contra `\r`, `\n` e bytes de controle (`\x00-\x1f`), além de `\x7f` (DEL), `\x85` (NEL), `\u2028` (LS) e `\u2029` (PS). Se encontrados, a requisição é rejeitada com HTTP 400.
2. **Injeção de Aspas e Vírgulas:**
   - Aspas duplas (`"`) são terminantemente proibidas em todos os campos textuais.
   - Vírgulas (`,`) são rejeitadas em campos embutidos em estruturas CSV (eventos, serviço, host, porta, usuário, diretório, origem).
3. **Política de Destino Global (Sem Overrides):**
   - O destino FTP (`host`, `porta`, `usuario`, `diretorio_inicial`, `hash_senha`) é configurado estritamente em nível global para o arquivo gerado.
   - Tarefas individuais **não podem conter nem sobrescrever** propriedades de destino; o envio dessas propriedades dentro do dicionário de uma tarefa resulta em HTTP 400.
4. **Limites de Tamanho e Volume:**
   - Nomes de tarefa: máx. 80 caracteres.
   - Caminhos de origem: máx. 260 caracteres (MAX_PATH Windows).
   - Comentários: máx. 120 caracteres.
   - Quantidade de tarefas por lista: mínimo 1, máximo 10 tarefas.
5. **Validação de Horários e Portas:**
   - Horários: formato estrito `^([01]\d|2[0-3]):[0-5]\d$`.
   - Portas: inteiros estritos na faixa `1..65535` (`validar_porta_cobian`).

---

## 7. Endpoints da API Backend

As rotas são integradas ao blueprint em `backups_ftp_admin.py` sem sobrescrever as rotas existentes (`/upload-list`, `/lista/<v>`, etc.). Ambas utilizam o helper privado unificado `_preparar_dados_gerador_cobian(rotina, payload, cursor)` e respondem via `_resposta_com_headers(payload, status)` com cabeçalhos restritivos `Cache-Control: no-store, private` e `Vary: Cookie`.

### 7.1. `POST /admin/backups-ftp/rotina/<int:id>/gerar-lista`
Gera a lista `.lst`, valida o conteúdo gerado com `_validar_e_processar_lista()`, grava no banco como nova versão criptografada com AEAD e registra auditoria transacional com metadados.
- **Padrão Transacional e AAD Existente:**
  - `cursor.execute("BEGIN IMMEDIATE")`
  - `max_v = cursor.execute("SELECT MAX(versao_numero) FROM backups_ftp_listas_versoes WHERE rotina_id = ?", (id,)).fetchone()[0]`
  - `proxima_versao = (max_v or 0) + 1`
  - Cifragem AAD idêntica ao upload:
    - `nome_cifrado = cifrar_dado(nome_arquivo, f"list:{id}:{proxima_versao}:nome")`
    - `comentario_cifrado = cifrar_dado(comentario_versao, f"list:{id}:{proxima_versao}:comentario")`
    - `conteudo_cifrado = cifrar_dado(texto_gerado, f"list:{id}:{proxima_versao}:conteudo")`
  - Retenção atômica: expurga excedentes a `MAX_VERSOES_RETENCAO` (10 versões).
  - Auditoria com metadados: `resultado = f"sucesso;reaproveitado={1 if reaproveitado else 0};ref={ref_id or ''}"`.

### 7.2. `POST /admin/backups-ftp/rotina/<int:id>/download-lista-gerada`
Gera o conteúdo da lista `.lst` em memória a partir dos mesmos parâmetros, valida estruturalmente e devolve diretamente o binário para download com auditoria fail-closed.
- **Auditoria Fail-Closed Obrigatória:**
  - Registra previamente em `backups_ftp_auditoria` via `_gravar_auditoria_commit` com `resultado = f"sucesso:{checksum_sha256};reaproveitado={1 if reaproveitado else 0};ref={ref_id or ''}"`.
  - Se a auditoria falhar, a requisição aborta com HTTP 500 sem entregar o arquivo.
- **Headers de Resposta:**
  - `Content-Type: text/plain; charset=utf-16le`
  - `Content-Disposition: attachment; filename="{secure_filename(alias)}-cobian.lst"`
  - `Cache-Control: no-store, private`
  - `Vary: Cookie`

---

## 8. Arquitetura de Testes

1. **Testes Unitários do Gerador (`tests/test_cobian_generator.py`):**
   - Formatação nativa compacta: `Chave=Valor`, sem espaços ao redor do sinal de igual.
   - Comparação exata de `Date/Time` para 13h e 23h contra amostras reais de produção.
   - Fixture oficial sanitizada versionada no repositório: `tests/fixtures/amostra_cobian_sanitizada.lst`.
   - Rejeição de injeção de CRLF, aspas duplas, vírgulas em campos CSV e caracteres de controle estendidos (DEL, NEL, LS, PS).
   - Validação de tipos estritos (proibição de coerção silenciosa) e validação de porta na faixa 1..65535.
   - Passagem pela função oficial `_validar_e_processar_lista()`.
2. **Testes de Integração Backend e Segurança (`tests/test_backups_ftp_admin.py`):**
   - Geração e gravação com HTTP 201 e retorno do objeto de versão.
   - Reaproveitamento de hash: sucesso quando `host` + `porta` + `usuario` coincidem; recusa (400) se porta ou usuário divergirem, se o hash for vazio ou se pertencer a outra rotina (`WHERE id = ? AND rotina_id = ?`).
   - Auditoria com metadados verificada tanto em `gerar-lista` quanto em `download-lista-gerada`.
   - Suíte negativa para payloads inválidos, tentativas de override de destino por tarefa e portas inválidas.
   - Auditoria fail-closed gravada no download com hash SHA-256.
   - Teste de controle de acesso: usuário anônimo redireciona para login (302/redirect), usuário autenticado sem perfil Master recebe HTTP 403, e validação de token CSRF obrigatória nas novas rotas.
