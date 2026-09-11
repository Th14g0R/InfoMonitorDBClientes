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
