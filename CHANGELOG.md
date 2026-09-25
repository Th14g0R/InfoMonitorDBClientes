# Histórico de versões

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
