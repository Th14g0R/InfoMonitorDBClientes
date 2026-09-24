# Plano 01 — desempenho, histórico, implantação e release

## Fase 0 — documentação e estado inicial

- Preservar as alterações existentes em `bancos.py` e `tests/test_seguranca.py`.
- Confirmar sincronização de `main` com `origin/main` e buscar tags antes de escolher a versão.
- Referências permitidas:
  - Chart.js: documentação oficial de performance e gráficos de linha/área empilhada.
  - Flask: documentação oficial de implantação; não usar o servidor de desenvolvimento em produção.
  - Waitress: runner/API oficial; um processo com pool de threads.
  - Windows: `Start-Process -Verb RunAs` e o padrão NSSM já existente no projeto.
  - macOS: `launchd.plist(5)` e `launchctl(1)` instalados no sistema.
  - Linux: unidades `systemd.service` com `Type=simple`, `WorkingDirectory` e `Restart=on-failure`.

## Fase 1 — desempenho e página de histórico

### Implementar

- Adicionar índice SQLite `(servidor, data DESC)` para consultas do histórico.
- Colocar a tabela histórica em um contêiner rolável, acessível e com cabeçalho fixo.
- Carregar Chart.js somente no modo histórico, com versão fixa, minificado e `defer`.
- Otimizar o gráfico atual: sem animação, pontos invisíveis, linha reta, normalização e redução de rótulos.
- Adicionar um segundo gráfico de área empilhada por servidor usando os mesmos dados já consultados.
- Tratar zero servidores e limitar o pool de coleta remota a no máximo oito threads.
- Evitar varredura de órfãos quando ela não foi solicitada e evitar consultas SSH forçadas duplicadas após invalidação de cache, preservando a semântica atual por meio de estado explícito de cache.
- Adicionar testes para índice, tabela rolável, carregamento condicional, gráfico e casos de cache/zero servidores.

### Verificação

- `python -m unittest discover -s tests -v`
- `python -m compileall -q ...`
- `git diff --check`
- Smoke HTTP de `/`, `/historico`, `/api/historico` e recursos estáticos.
- Confirmar que nenhuma rota pública mudou de resultado funcional.

### Guardas

- Não ativar decimation do Chart.js no eixo categórico atual.
- Não criar múltiplos processos WSGI enquanto o APScheduler iniciar no import.
- Não impor recorte temporal silencioso aos dados históricos.

## Fase 2 — execução de produção e instalação multiplataforma

### Implementar

- Adicionar Waitress e um runner Python único que valide `BIND_HOST`, `BIND_PORT` e `WEB_THREADS`.
- Gerar lock de dependências testado e fazer instaladores preferirem esse lock.
- Windows: executar o runner de produção no NSSM, remover instalação duplicada no Python global e impedir que backups automáticos incluam segredos/bancos sem proteção.
- macOS: adicionar lançador `.command` e instalador auditável para `launchd`, solicitando `sudo` somente ao registrar/reiniciar o serviço.
- Linux: adicionar instalador auditável para `systemd`, com usuário de serviço, diretório de trabalho, reinício automático e permissões restritas.
- Documentar instalação, atualização, status, logs, reinício e remoção nos três sistemas.
- Adicionar `VERSION`, `CHANGELOG.md` e exemplo de `WEB_THREADS`.

### Verificação

- Testes Python completos e smoke com Waitress.
- `bash -n` nos scripts Unix, `plutil -lint` no plist gerado e análise sintática PowerShell quando disponível.
- Confirmar caminhos absolutos, `WorkingDirectory` e execução em processo único.
- Confirmar que `.env`, `*.db`, `known_hosts`, fotos e logs não entram no Git.

### Guardas

- Não commitar binários NSSM nem executáveis opacos.
- Não usar Gunicorn no Windows.
- Não instalar pacotes no Python global.
- Não copiar nem sobrescrever dados locais durante atualização.

## Fase 3 — revisão, versão e publicação

- Revisão independente de segurança, desempenho, portabilidade e qualidade.
- Repetir toda a suíte, compilação, smoke HTTP/Waitress e verificações de diff.
- Inspecionar staging explicitamente; nunca usar `git add .` sem lista revisada.
- Criar commits curtos e objetivos.
- Se não houver tag remota anterior, criar a primeira release formal `v1.0.0`; caso exista, calcular o próximo SemVer compatível.
- Publicar branch e tag de forma atômica somente se `main` continuar integrável com `origin/main`.
