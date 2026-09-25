# Histórico de versões

## 1.1.4 - build 20260925.5 - 2026-09-25

- atualiza código e prepara dependências sem parar ou reconfigurar o serviço Windows;
- pergunta ao final se o operador deseja ativar o runtime e reiniciar o serviço;
- permite adiar a ativação e concluí-la posteriormente pela opção Reiniciar;
- preserva o runtime anterior caso a ativação ou o health check falhem.

## 1.1.3 - build 20260925.4 - 2026-09-25

- registra e exibe o erro real ocorrido na etapa elevada do instalador Windows;
- confirma explicitamente o sucesso da aplicação elevada antes de encerrar;
- valida serviço e NSSM antes de baixar e preparar toda a atualização.

## 1.1.2 - build 20260925.3 - 2026-09-25

- separa no instalador Windows os fluxos de instalação nova e atualização existente;
- exige destino novo para instalar e aplicação existente para atualizar;
- mantém na atualização as verificações de Git, manifesto, versão, dependências, backup e rollback.

## 1.1.1 - build 20260925.2 - 2026-09-25

- corrige a seleção do diretório nos instaladores Windows, Linux e macOS;
- detecta a pasta registrada no serviço ou sugere o diretório do próprio instalador;
- permite atualizar um checkout Git local, criando backup e migrando-o para o manifesto gerenciado;
- mantém bloqueios para raízes de disco e diretórios protegidos do sistema.

## 1.1.0 - build 20260925.1 - 2026-09-25

- reorganiza as telas de administração, servidores e horários com temas claro/escuro e layout responsivo;
- corrige modais, cores, contraste e alinhamento dos botões administrativos;
- adiciona validação de permissões nos atalhos dos menus sem retirar o usuário da página;
- restaura a expansão dos detalhes de clientes e compacta títulos e colunas da lista de bancos;
- testa CNAMEs sob demanda, com cache, repetição automática e limite dedicado de 100 consultas;
- amplia as proteções de sessão, CSRF, auditoria e confirmação de ações administrativas;
- adiciona e atualiza testes de segurança e regressão das interfaces.

## 1.0.0 - 2026-09-23

- melhora o desempenho da coleta, do cache e das consultas históricas;
- adiciona tabela histórica rolável e gráficos de linha e área empilhada;
- adiciona paginação à API de histórico;
- executa produção em um único processo Waitress configurável;
- adiciona instalação e gestão de serviço para Windows, macOS e Linux;
- endurece confirmação de senha, SSH e transferências de banco.
