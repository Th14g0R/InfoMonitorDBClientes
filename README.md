# InfoMonitorDBClientes
Monitor de clientes hospedados no datacenter da Infobrasil Sistemas

## Teste local

Instale as dependências com `python -m pip install -r requirements.lock`, copie
`.env.example` para `.env` e preencha `SECRET_KEY` e as configurações necessárias.
Use `BIND_HOST=127.0.0.1` para testar localmente e execute
`python InfoMonitorDBClientes.py`.

`SECRET_KEY` é obrigatória, deve ser aleatória e ter pelo menos 32 caracteres.
Gere uma com `python -c "import secrets; print(secrets.token_hex(32))"`. Nunca
publique `.env`, bancos, `known_hosts`, fotos ou logs no Git.

O administrador inicial é criado somente com `ADMIN_MASTER_EMAIL` e
`ADMIN_MASTER_PASSWORD`, se esse usuário ainda não existir. Remova a senha de
bootstrap do `.env` após a criação. Contas existentes não são promovidas nem
reativadas automaticamente; permissões já gravadas não são alteradas por esta correção.

As conexões SSH exigem uma chave de servidor previamente confiável. Confira a
impressão digital com o administrador do servidor por um canal independente e
adicione a chave validada ao arquivo OpenSSH indicado por `SSH_KNOWN_HOSTS`
(por padrão, `known_hosts` na pasta do projeto). Chaves desconhecidas, alteradas
ou arquivos inválidos causam falha na conexão. Não confie em uma chave apenas
por ter sido obtida pela rede; revise também as entradas aceitas anteriormente.

Recarregue as páginas abertas após atualizar o código. Formulários e chamadas
JavaScript enviam um token CSRF da sessão; clientes externos precisam enviar
`X-CSRF-Token` ou o campo `csrf_token` junto ao cookie da mesma sessão.
Exclusões, alteração de status, envio de redefinição e logout exigem POST.
No envio para um alias já cadastrado, o destino é o caminho `.fdb` desse alias.

Na gestão de bancos, ao adicionar um alias, escolha **Usar pasta com o nome do
alias** ou **Selecionar uma pasta existente**. Na segunda opção, selecione a
pasta na lista e use **Ver subpastas** para navegar. O caminho final é mostrado
antes do envio; o DB05 usa `/opt/infobrasil/3.0` como base.

O campo **Nome do banco no destino** permite adicionar outro arquivo `.fdb` à
mesma pasta. Arquivos existentes não são substituídos. Para cadastrar um alias
para um banco que já está no servidor, selecione a pasta e o nome correspondente
e deixe o campo de envio vazio. Transferências novas usam um arquivo temporário,
renomeado somente após o envio completo. Se a conexão cair, o temporário pode
permanecer como `.nome.fdb.<identificador>.upload`, sem substituir o banco.

Restaurações que substituem um destino dependem da extensão SFTP
`posix-rename@openssh.com`; confirme que o servidor SSH a oferece. Sem essa
operação atômica, o sistema recusa finalizar a substituição em vez de expor um
banco parcialmente enviado.

Execute `python -m unittest discover -s tests -v` para validar as proteções em
um banco temporário, sem usar o `.env` ou os bancos locais.

## Visualização pública e gestão

As páginas de servidores, todos os bancos, aliases inativos, arquivos órfãos,
histórico de disco, backups FTP e horários permitem consulta sem login.
Busca, filtros, detalhes e cópia de CNAME continuam disponíveis aos visitantes.
O menu **Gerenciar** e os controles de alteração aparecem somente para usuários
autenticados com a permissão correspondente. As rotas administrativas também
validam a sessão no servidor; ocultar os botões não é a única proteção.

## Horários e cobertura

`/horarios` é público para consulta. Cadastro, edição, escalas, ausências e trocas
continuam exigindo sessão ativa com permissão de horários ou acesso Master.
O antigo parâmetro `PUBLIC_HORARIOS` não é mais utilizado.

Cadastro e edição oferecem as equipes Amarela, Verde, Estágio e Sem Equipe,
jornada da semana de 08:00 às 12:00 e participação aos sábados **Nenhum**.
Essa opção impede novas inclusões em escalas, inclusive manuais; escalas já
registradas são preservadas e devem ser revistas se a disponibilidade mudar.

Em **Trocar horários em uma data**, as jornadas dos dois funcionários são
trocadas somente no dia informado. Aos sábados, são usados os turnos da escala;
quem estiver de folga assume o turno do titular. A cobertura considera as trocas,
os ajustes da escala e as ausências. Domingo não utiliza a jornada da semana.
O histórico informa os horários anteriores e posteriores, com filtro próprio de
data. Registros antigos são mantidos como histórico, sem reaplicação automática.
Sábados com trocas ou ajustes registrados não podem ser regenerados automaticamente.

Ao iniciar esta versão sobre um banco anterior, o sistema cria uma cópia SQLite
em `backups/sistema-antes-ajustes-horarios-<data-hora>.db`, junto ao banco, antes
da migração de horários. O processo adiciona campos e tabelas e preserva os dados.
Atualize somente o código em produção; não copie o `sistema.db` de testes sobre
o banco de produção. Essa cópia de migração não substitui backups periódicos.

## Produção

Use `python production.py`, nunca o servidor de desenvolvimento do Flask. O
runner valida `BIND_HOST`, `BIND_PORT` (1–65535) e `WEB_THREADS` (1–64) e inicia
Waitress. A configuração recomendada é `WEB_THREADS=4`; aumente somente depois
de medir carga e memória.

Mantenha **um único processo**. O APScheduler é criado dentro da aplicação e
múltiplos processos executariam a mesma coleta agendada mais de uma vez. Waitress
oferece concorrência por threads dentro desse processo. Para acesso externo,
termine HTTPS em um proxy reverso e restrinja a porta do Waitress à rede adequada.

O endpoint `GET /api/historico` aceita `servidor`/`servidores`, `data_inicio`,
`data_fim`, `limite` e `offset`. `limite` usa 500 por padrão (máximo 1000) e
`offset` usa 0 (máximo 10.000.000). A resposta informa `X-Total-Count`, `X-Limit`
e `X-Offset`; quando aplicável, `Link` contém `rel="prev"` e `rel="next"`.

## Instaladores e dados preservados

Os lançadores são scripts legíveis, não binários. Eles criam `.venv`, exigem
`requirements.lock`, verificam dependências e geram uma `SECRET_KEY` na primeira
instalação e registram um único processo `production.py`. Permissão administrativa
é solicitada apenas ao registrar ou controlar o serviço.

O lock fixa versões, mas ainda não contém hashes para todas as rodas de Windows,
macOS e Linux. Assim, ele melhora reprodutibilidade, porém não comprova a
integridade criptográfica dos artefatos baixados; use índice PyPI confiável e TLS.

Atualizações preservam `.env`, `*.db`, `known_hosts`, `static/fotos`, `backups` e
`logs`. O backup automático contém **somente código versionado**, portanto não é
backup dos dados operacionais. Mantenha cópias protegidas e independentes dos
bancos e da configuração. A remoção pelo assistente remove somente o serviço.
Uma atualização baixa e valida código/dependências antes da parada, registra um
manifesto do código instalado, remove somente arquivos que pertenciam ao manifesto
anterior e executa health check. Em falha, tenta restaurar o código e o estado
anterior do serviço; ainda assim, mantenha backup externo dos dados.

`DATA_DIR` separa SQLite, fotos e logs do código (novas instalações usam `data`).
Ao migrar instalação antiga, os instaladores **copiam** bancos e fotos para esse
diretório sem apagar os originais; remova a cópia antiga somente depois de validar
e fazer backup. O runtime candidato é montado em `.venv.next`, testado e então
trocado atomicamente, mantendo `.venv.previous` até o health check passar.

### Windows

Pré-requisitos: Python 3 de 64 bits, Git for Windows e NSSM obtido do site
oficial. O repositório não distribui `nssm.exe`; instale-o exatamente em
`C:\Program Files\nssm\win64\nssm.exe`. Antes de executar, defina `NSSM_SHA256`
com o SHA-256 conferido do binário obtido; cópias no `PATH` ou na pasta do projeto
não são executadas. Execute `INSTALAR_OU_ATUALIZAR.bat` e escolha instalar/atualizar,
configurar, consultar status, reiniciar, parar ou remover. O assistente mostra a
pasta de onde está sendo executado e sugere, nesta ordem, o diretório já registrado
no serviço, o diretório local do instalador ou `C:\InfoMonitorDBClientes`. Um checkout
Git local pode ser atualizado no próprio lugar; na primeira atualização ele é
convertido para o manifesto gerenciado pelo instalador. O NSSM grava a saída e os
erros na subpasta `logs` do `DATA_DIR` configurado.

O serviço roda como `NT AUTHORITY\LocalService`, não como SYSTEM. O instalador
restringe as ACLs da instalação aos administradores, SYSTEM, usuário instalador e
LocalService; essa última identidade recebe escrita porque os SQLite e seus
journals ficam na pasta da aplicação. Não use a pasta Windows, Program Files,
ProgramData, a raiz de um disco ou uma pasta não vazia que não seja uma instalação
existente nem um checkout Git válido do projeto.

Também é possível chamar, em PowerShell:

```powershell
.\INSTALAR_OU_ATUALIZAR.bat -Action install -Target C:\InfoMonitorDBClientes
.\INSTALAR_OU_ATUALIZAR.bat -Action configure -Target C:\InfoMonitorDBClientes
.\INSTALAR_OU_ATUALIZAR.bat -Action status -Target C:\InfoMonitorDBClientes
.\INSTALAR_OU_ATUALIZAR.bat -Action restart -Target C:\InfoMonitorDBClientes
.\INSTALAR_OU_ATUALIZAR.bat -Action stop -Target C:\InfoMonitorDBClientes
```

### macOS

Com Python 3, Git, `curl` e as Command Line Tools instalados, dê permissão uma vez e abra
o assistente:

```bash
chmod +x INSTALAR_OU_ATUALIZAR.command scripts/servico_unix.sh
./INSTALAR_OU_ATUALIZAR.command
```

Ele registra `/Library/LaunchDaemons/com.infobrasil.infomonitor.plist`, com
`WorkingDirectory` e caminhos absolutos. O menu informa o diretório local que será
instalado ou atualizado. Comandos diretos:

```bash
./INSTALAR_OU_ATUALIZAR.command update
./INSTALAR_OU_ATUALIZAR.command configure
./INSTALAR_OU_ATUALIZAR.command status
./INSTALAR_OU_ATUALIZAR.command restart
./INSTALAR_OU_ATUALIZAR.command stop
./INSTALAR_OU_ATUALIZAR.command logs
./INSTALAR_OU_ATUALIZAR.command uninstall
```

Os logs ficam em `logs/servico-saida.log` e `logs/servico-erro.log`. Para inspeção
nativa, use `sudo launchctl print system/com.infobrasil.infomonitor`.

### Linux (systemd)

Com Python 3 (incluindo o módulo `venv`), Git e `curl` instalados:

```bash
chmod +x INSTALAR_OU_ATUALIZAR.sh scripts/servico_unix.sh
./INSTALAR_OU_ATUALIZAR.sh
```

O assistente registra `/etc/systemd/system/infomonitor.service` com `Type=simple`,
usuário atual, `WorkingDirectory` absoluto, `Restart=on-failure`, `PrivateTmp`,
`NoNewPrivileges` e proteção somente-leitura do sistema, liberando escrita apenas
na pasta da aplicação. O menu informa o diretório local que será instalado ou
atualizado. Comandos:

```bash
./INSTALAR_OU_ATUALIZAR.sh update
./INSTALAR_OU_ATUALIZAR.sh configure
./INSTALAR_OU_ATUALIZAR.sh status
./INSTALAR_OU_ATUALIZAR.sh restart
./INSTALAR_OU_ATUALIZAR.sh stop
./INSTALAR_OU_ATUALIZAR.sh logs
./INSTALAR_OU_ATUALIZAR.sh uninstall
```

Alternativamente: `systemctl status infomonitor` e
`journalctl -u infomonitor -f`. Se o projeto estiver sob `/home`, confirme que o
sistema de arquivos está disponível antes do serviço iniciar.

## Atualização manual e versão

O número da versão está em `VERSION` e as mudanças em `CHANGELOG.md`. Antes de
atualizar, faça backup dos dados, pare o serviço, execute `git pull --ff-only`,
instale `requirements.lock` dentro da `.venv` e reinicie. Não copie um
`sistema.db` local sobre o servidor.
