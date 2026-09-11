# InfoMonitorDBClientes
Monitor de clientes hospedados no datacenter da Infobrasil Sistemas

## Teste local

Instale as dependências com `python -m pip install -r requirements.txt`, copie
`.env.example` para `.env` e preencha `SECRET_KEY` e as configurações necessárias.
Use `BIND_HOST=127.0.0.1` para testar localmente e execute
`python InfoMonitorDBClientes.py`.

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

Execute `python -m unittest discover -s tests -v` para validar as proteções em
um banco temporário, sem usar o `.env` ou os bancos locais.
