# Histórico de versões

## 1.2.1 - 2026-10-21

- rede: `BIND_HOST` ausente no `.env` mantém 127.0.0.1, mas agora emite aviso em destaque na inicialização; documentado `BIND_HOST=0.0.0.0` para rede interna (README e `.env.example`);
- /orfaos: timeouts configuráveis (`TIMEOUT_FIND_ORFAOS`, padrão 30s; `TIMEOUT_CONEXAO_SSH`, padrão 10s) e causa detalhada da falha SSH registrada no log do servidor, com credenciais mascaradas;
- certificados: remoção de subdomínio em /certificados (POST `/certificados/remover`, somente logado, CSRF, auditoria) com diálogo de confirmação;
- visual: indicador de conexão CNAME desenhado em CSS, sem depender de fonte de emoji (corrige retângulo vazio no Windows);
- backups: histórico de alertas com data e hora separadas por hífen, sem coluna Detalhes, envio manual indicado na situação, dias dispensados nos últimos 30 dias, horário lido do agendador (9h) e rolagem com cabeçalho fixo;
- pendências conhecidas: causa de /orfaos a confirmar pelo log em produção (`find` com status 1 ainda interrompe o servidor); linhas "Dispensado" são inferidas pela ausência de registro; altura da rolagem aproximada (cerca de 11 linhas);
- testes: 143 testes passando.

## 1.2.0 - 2026-10-21

- seguranca: redirecionamentos validados (mesmo host, caminhos relativos), whitelist de servidor, nan-injection, sanitizacao de excecoes e `BIND_HOST` padrao 127.0.0.1;
- seguranca: URL da API InfoBrasil aceita apenas http/https (bloqueia file:// e outros esquemas) com testes;
- visual: tokens de cor/tipografia unificados em `static/ui.css`, templates padronizados (cnpj, certificados, eventos, funcionarios) e correcoes de acessibilidade (axe: 7 violacoes para 0);
- testes: 134 testes passando.

## 1.1.6 - build 20260925.7 - 2026-09-25

- restaura a edição e inativação de aliases em servidores sem permissão para criar arquivos temporários;
- mantém a troca atômica como primeira opção e usa escrita direta verificada como compatibilidade;
- verifica o hash após a escrita e restaura o conteúdo anterior automaticamente em caso de falha.

## 1.1.5 - build 20260925.6 - 2026-09-25

- reinicia o serviço Windows existente sem depender do NSSM e sem reconfigurá-lo;
- limita a ativação pendente a parar, trocar o runtime validado e iniciar o mesmo serviço;
- restaura o runtime anterior e o serviço em execução se o health check falhar.

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
