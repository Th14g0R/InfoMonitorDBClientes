# Relatório de Investigação — Gerador de Listas `.lst` Cobian Backup 11 (Gravity)

> **Status:** Investigação Técnica Concluída (Sem alteração de código)
> **Objetivo:** Analisar a anatomia dos arquivos de tarefas `.lst` do Cobian Backup 11, mapear a integração existente em `backups_ftp_admin`, propor modelo de formulário editável e definir validações de compatibilidade.
> **Segurança:** Nenhuma credencial ou senha real foi exposta neste documento ou nos logs.

---

## 1. Anatomia e Comparação dos 3 Arquivos de Amostra

Foram inspecionados os arquivos:
1. `C:/infobrasil/infoBackup/util/hiperbompreco-2225.lst` (14.918 bytes)
2. `C:/infobrasil/infoBackup/util/custodioeletro-1342.lst` (14.938 bytes)
3. `C:/infobrasil/infoBackup/util/supernossafamilia-302.lst` (22.450 bytes)

### 1.1. Formato, Encoding e Delimitação Global

- **Encoding Obrigatório:** `UTF-16LE` com Byte Order Mark (BOM) inicial de 2 bytes: `\xFF\xFE`.
- **Quebras de Linha:** Padrão Windows CRLF (`\r\n`).
- **Envoltório Global (Envelope):**
  - **Primeira linha (Cabeçalho):** `{11//` (indica versão 11 do Cobian Backup).
  - **Última linha (Rodapé):** `//11}`.
- **Estrutura Interna:** Formato de dicionário plano linha a linha `Chave=Valor` sem espaços ao redor do sinal de igual (padrão nativo do Cobian Backup 11). Não há blocos aninhados com chaves adicionais para as tarefas; cada tarefa começa com a diretiva `Id={GUID}`. Os campos `Proxy password=`, `Passphrase=` e `Run as Password=` permanecem intencionalmente vazios por padrão seguro (com `Proxy type=0`, `Encryption=0` e `Impersonate=false`), nunca copiando dados codificados de produção; sua aceitação operacional depende de validação na importação real do Cobian.

---

### 1.2. Mapeamento de Tarefas por Arquivo

| Arquivo | Qtd Tarefas | Tarefas Identificadas | Tipo de Origem | Horários Decodificados | Pré/Pós Eventos |
|---|---|---|---|---|---|
| **hiperbompreco-2225** | 2 | `IMS-hiperbompreco-2225-13h`<br>`IMS-hiperbompreco-2225-23h` | `backup.fbk` (dump FBK) | 13:00 e 23:00 | **Pré:** `EXECUTE,infoBackup.exe` + `PAUSE,1800,`<br>**Pós:** Vazio |
| **custodioeletro-1342** | 2 | `IMS-custodioeletro-1342-12h`<br>`IMS-custodioeletro-1342-18h` | `backup.fbk` (em `infoBackup3`) | 12:00 e 18:00 | **Pré:** `EXECUTE,infoBackup3\infoBackup.exe` + `PAUSE,180,`<br>**Pós:** Vazio |
| **supernossafamilia-302** | 3 | `IMS-supernossafamilia-302-23h` *(desat)*<br>`FDB-supernossafamilia-302-23h` *(ativa)*<br>`IMS-supernossafamilia-302-13h` *(desat)* | `DADOS.FDB` (banco bruto)<br>e `backup.fbk` | 23:00 e 13:00 | **FDB Pré:** `STOP_SERVICE,FirebirdServerDefaultInstance` + `PAUSE,60,`<br>**FDB Pós:** `START_SERVICE,FirebirdServerDefaultInstance` |

---

### 1.3. Campos Comuns vs. Variáveis

#### A. Campos Comuns e Fixos (Idênticos em 100% das tarefas padrão)
- `Group = IMS`: Todas as tarefas são agrupadas no grupo visual "IMS" do Cobian.
- `Enabled = true` (pode ser `false` para tarefas desativadas temporariamente).
- `Include subdirectories = true`
- `Create separated backups = false`
- `Use attributes = true`
- `Use VSC = true`: Ativação de Volume Shadow Copy (VSS do Windows).
- `Backup type = 0`: Modo Completo (Full).
- `Priority = 0`: Prioridade Normal do processo.
- `Schedule type = 1`: Agendamento Diário.
- `Select days of the week = false`
- `Order of Day of the week = 1`, `Day of the week = 1`, `Day of the month = 1`, `Month = 1`
- `Timer = 180`, `Timer from = ...`, `Timer to = ...`
- `Full copies = 0`, `Differental copies = 0`, `One full every = 0`
- `Use fixed day = false`, `Fixed day = 1`
- `Compression = 1`: Compressão ZIP padrão (0 = Sem compressão, 1 = Zip, 2 = 7z).
- `Compress individually = false`
- `Split = 0`, `Custom size = 4300000000`
- `Encryption = 0`, `Passphrase = `
- `Exclusions = `, `Inclusions = `
- `Abort if pre-event fails = false`, `Abort if post-event fails = false`
- `Mirror = false`, `Absolute paths = false`, `Always create top directory = false`
- `Clear archive attribute = true`, `Include backup type = true`, `Delete empty directories = false`
- `Impersonate = false`, `Abort if impersonation fails = false`, `Run as User name = `, `Run as Domain = .`, `Run as Password = `

#### B. Campos Variáveis por Tarefa/Cliente
1. **`Id`**: GUID v4 único gerado entre chaves, ex: `{1F41403F-426E-4FBE-8D00-9AFC2C94EEB3}`.
2. **`Name`**: Nome da rotina no Cobian:
   - Formato padronizado: `<TIPO>-<cliente_alias>-<horario>h` (ex: `IMS-hiperbompreco-2225-13h` ou `FDB-supernossafamilia-302-23h`).
3. **`Comment`**: Preenchido com o alias/identificador do cliente (ex: `hiperbompreco-2225`).
4. **`Source`**: Caminho entre aspas precedido do código de tipo `0,` (arquivo único):
   - Exemplo FBK: `"0,C:\Infobrasil\infoBackup\backup\hiperbompreco-2225\backup.fbk"`
   - Exemplo FDB: `"0,C:\Infobrasil\Dados\DADOS.FDB"`
5. **`Date/Time` (Horário de Execução)**:
   - **Formato Técnico:** String binária de 64 caracteres (`0`s e `1`s).
   - **Decodificação:** Ponto flutuante IEEE 754 de 64 bits em big-endian (`double`), representando o `TDateTime` do Delphi (número de dias e fração do dia desde `30/12/1899`).
   - A fração do dia reflete perfeitamente a hora (`13:00:00` = `0.541666...`, `23:00:00` = `0.958333...`).
6. **`Pre backup events` / `Post backup events`**:
   - Para rotinas `IMS` (.fbk): Execução do dump prévio com pausa para conclusão:
     `"EXECUTE,C:\Infobrasil\infoBackup\infoBackup.exe,","PAUSE,1800,"`
   - Para rotinas `FDB` (.fdb): Parada e reinício do serviço Firebird para evitar arquivo travado:
     - Pré: `"STOP_SERVICE,FirebirdServerDefaultInstance,","PAUSE,60,"`
     - Pós: `"START_SERVICE,FirebirdServerDefaultInstance,"`
7. **`Destination`**:
   - String composta delimitada por `"2,""""""...""""` indicando destino FTP (`2,`).
   - Parâmetros embutidos:
     - `FTP Host`: ex: `ims.iprojectti.com.br`
     - `FTP Port`: ex: `8821`
     - `FTP User name`: ex: `hiperbompreco-2225`
     - `FTP Password`: Senha ofuscada/cifrada pelo Cobian (string base64 de 200 caracteres, decodificada em 74 caracteres UTF-16LE).
     - `FTP Initial directory`: `/`
     - `FTP Passive=true`, `FTP Passive host=true`
     - Sub-blocos `FTP SSL=` e `FTP Proxy=` zerados/desativados por padrão.

---

## 2. Diagnóstico da Integração Atual em `InfoMonitorDBClientes`

### 2.1. O que já existe
1. **Armazenamento de Rotinas (`backups_ftp_rotinas`):**
   - O sistema gerencia e criptografa com AES-256-GCM (chave AEAD) todos os dados de conexão do cliente: `servidor`, `porta`, `usuario`, `senha`, `responsavel`, `lancamento_em`, `notas`, `dados_extras`.
2. **Versionamento de Listas Cobian (`backups_ftp_listas_versoes`):**
   - Suporte para upload de arquivos `.list` e `.lst` com limite de 2 MiB;
   - Validação estrutural básica em `_validar_e_processar_lista()` (BOM obrigatório, quebras de linha sanitizadas, delimitadores `{` e `}`);
   - Retenção atômica de até 10 versões por rotina;
   - Visualização sanitizada somente-leitura em submodal (`ftp-pre-conteudo-list`).

### 2.2. A lacuna identificada
- Atualmente, a única forma de registrar uma lista Cobian no sistema é via **Upload manual** de um arquivo já gerado externamente.
- Não há interface nem gerador backend para emitir o arquivo `.lst` a partir dos dados do cliente.

---

## 3. Proposta de Arquitetura: Formulário Editável e Gerador `.lst`

### 3.1. Posicionamento na Interface do Usuário (UI)
No painel de gerenciamento de listas Cobian (`templates/backups_ftp_admin.html`), adicionar uma seção/botão:
`[⚡ Gerar Lista Cobian .lst]`

Ao clicar, abre-se um sub-modal com:
1. **Pré-carregamento Automático**:
   - Host FTP, Porta e Usuário herdados diretamente da rotina de FTP selecionada;
   - Alias do cliente preenchido no comentário e nomes das tarefas.
2. **Seleção de Perfil de Rotina (Presets)**:
   - **Perfil 1: Padrão InfoBackup (FBK) — Recomendado**:
     - 2 tarefas diárias: **13h** e **23h**;
     - Origem: `C:\Infobrasil\infoBackup\backup\{alias}\backup.fbk`;
     - Pré-evento: `EXECUTE,C:\Infobrasil\infoBackup\infoBackup.exe,` com pausa de `1800s` (30 min).
   - **Perfil 2: Banco Direto (FDB)**:
     - 1 tarefa diária: **23h**;
     - Origem: `C:\Infobrasil\Dados\DADOS.FDB`;
     - Pré-evento: `STOP_SERVICE,FirebirdServerDefaultInstance,` com pausa de `60s`;
     - Pós-evento: `START_SERVICE,FirebirdServerDefaultInstance,`.
   - **Perfil 3: Customizado / Multi-Horário**:
     - Permite ao operador selecionar os horários desejados (ex: marcar checkboxes para `12h`, `13h`, `18h`, `23h` ou adicionar novos).
3. **Opções de Saída**:
   - Botão **"Salvar como Nova Versão no Monitor"** (cria versão direto no histórico de listas da rotina);
   - Botão **"Baixar Arquivo .lst"** (download do arquivo pronto para envio ao cliente).

---

### 3.2. Validações de Compatibilidade com Cobian 11 (Gravity)

1. **BOM e Encoding:** Gerar estritamente `UTF-16LE` com cabeçalho `\xFF\xFE` e quebras CRLF (`\r\n`).
2. **Cabeçalho e Rodapé de Versão:** Primeira linha `{11//\r\n` e última linha `//11}\r\n`.
3. **Codificação Matemática do Horário (`Date/Time`):**
   - Função de conversão de hora/minuto para float de dias Delphi `TDateTime` e serialização para string binária de 64 bits.
4. **Formatação Estrita do Bloco `Destination`:**
   - Respeitar rigorosamente a ordem e escape de aspas quadruplicadas (`""""`) dos parâmetros de FTP.

---

## 4. Limitação Técnica Concreta e Alternativas Seguras para Senhas

### 4.1. Limitação
O Cobian 11 Gravity utiliza um mecanismo interno de ofuscação/cifragem proprietário em Delphi (`cbLegED.dll` / `cbEngine.dll`), salvando senhas como strings de 74 caracteres UTF-16LE codificadas em Base64 (200 caracteres de texto).

### 4.2. Alternativas Seguras para o Produto
1. **Alternativa A (Recomendada e Segura):**
   - O gerador emite a lista `.lst` com todos os parâmetros preenchidos e o campo `FTP Password=` vazio (ou com marcador).
   - Ao importar o `.lst` no Cobian 11 do cliente, o técnico abre as propriedades de uma tarefa, digita a senha da rotina FTP (disponível no monitor) e salva. O Cobian gera seu próprio hash localmente.
2. **Alternativa B (Herança de Hash de Lista Anterior):**
   - Caso a rotina do cliente já possua uma versão de lista Cobian enviada anteriormente, o gerador pode herdar o campo `FTP Password=...` já codificado e reaproveitá-lo intacto na nova lista gerada, sem precisar conhecer ou decifrar o algoritmo.
