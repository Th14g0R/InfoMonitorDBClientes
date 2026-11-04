# LEIAME: Chave Mestra de Criptografia dos Backups FTP

**Responsável:** Thiago
**Data:** 04/11/2026
**Finalidade:** Criptografia autenticada em repouso (AES-256-GCM) de senhas, campos extras privados e listas Cobian (`.list` / `.lst`) do painel administrativo de Backups FTP.

---

### 1. Especificação da Chave
- **Algoritmo:** AES-256-GCM (32 bytes de entropia criptográfica segura).
- **Codificação:** String ASCII de **44 caracteres Base64 URL-safe** (sem espaços ou quebras de linha).
- **Localização:** Armazenada estritamente em arquivo no sistema operacional em `C:\ProgramData\InfoMonitorKeys\backups_ftp.key`, **fora** do repositório Git e **fora** do banco SQLite.

---

### 2. Procedimento de Preparação e Geração Segura
> [!IMPORTANT]
> Execute estes passos no servidor de produção **a partir da pasta raiz da aplicação**, após o *git pull* e antes de reiniciar o serviço.

#### Passo A: Identificar a Conta de Serviço da Aplicação
Identifique o usuário configurado para executar o serviço no Windows (campo `StartName`):
```powershell
Get-CimInstance Win32_Service -Filter "Name LIKE '%InfoMonitor%'" | Select-Object Name, StartName
```

#### Passo B: Criar o Diretório e Restringir ACLs ANTES de Gerar a Chave
Crie a pasta de chaves e bloqueie o acesso herdado imediatamente. Para evitar problemas de idioma no Windows (pt-br vs en-us), utilize os SIDs fixos:
- `*S-1-5-32-544`: Grupo de Administradores
- `*S-1-5-18`: Conta LocalSystem

Substitua `NOME_CONTA_SERVICO` pelo `StartName` identificado e `LOGIN_COMPLETO_THIAGO` pelo login Windows completo de Thiago (confira com `whoami`). Se o serviço usar `LocalSystem`, a linha do SID `*S-1-5-18` já o contempla; omita a linha de conta personalizada. Execute em PowerShell elevado e interrompa o procedimento se algum comando de ACL falhar.

```powershell
# 1. Criar a pasta de chaves no ProgramData
New-Item -ItemType Directory -Force -Path "C:\ProgramData\InfoMonitorKeys" | Out-Null

# 2. Restringir a pasta ANTES da chave existir (substitua os dois nomes abaixo)
icacls "C:\ProgramData\InfoMonitorKeys" /inheritance:r
icacls "C:\ProgramData\InfoMonitorKeys" /grant:r "*S-1-5-32-544:(OI)(CI)F"
icacls "C:\ProgramData\InfoMonitorKeys" /grant:r "*S-1-5-18:(OI)(CI)F"
icacls "C:\ProgramData\InfoMonitorKeys" /grant:r "NOME_CONTA_SERVICO:(OI)(CI)R"
icacls "C:\ProgramData\InfoMonitorKeys" /grant:r "LOGIN_COMPLETO_THIAGO:(OI)(CI)F"
```

Confira a ACL resultante com `icacls "C:\ProgramData\InfoMonitorKeys"`; não mantenha concessões explícitas para outros usuários. Referência de sintaxe: [icacls — Microsoft Learn](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls).

#### Passo C: Gerar a Chave com Modo Exclusivo `'x'`
A partir da pasta raiz da aplicação, utilize o executável Python do ambiente virtual local (`.\.venv\Scripts\python.exe`) com modo exclusivo `'x'` (impede sobrescrever chave pré-existente e perder dados):

```powershell
# Executar a partir da pasta raiz da aplicação:
& ".\.venv\Scripts\python.exe" -c "import secrets, base64; p = r'C:\ProgramData\InfoMonitorKeys\backups_ftp.key'; open(p, 'x', encoding='ascii').write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii'))"
```

> [!CAUTION]
> O modo exclusivo `'x'` gera `FileExistsError` caso o arquivo já exista, prevenindo a perda irreversível de dados criptografados com uma chave anterior.

---

### 3. Configuração no `.env`
O arquivo `.env` da aplicação deve conter **apenas o caminho físico** para a chave, nunca o segredo em si:

```env
BACKUPS_FTP_KEY_PATH=C:\ProgramData\InfoMonitorKeys\backups_ftp.key
```

---

### 4. Custódia e Contingência
- **Cópia de Recuperação Obrigatória:** Uma cópia dos 44 caracteres deve ser guardada imediatamente no cofre corporativo de senhas da equipe, fora do servidor.
- **Perda Irreversível:** Sem a chave exata, qualquer senha ou lista Cobian gravada no banco torna-se permanentemente irrecuperável.
- **Substituição:** Nunca troque o arquivo da chave sem antes executar um procedimento de recifragem dos dados cadastrados.
- **Backups do Sistema:** Nunca inclua o arquivo `.key` em rotinas de backup ou arquivos ZIP do repositório/banco.
