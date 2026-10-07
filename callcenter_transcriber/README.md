# 🎧 Infobrasil Sistemas — Transcrição & Auditoria de Call Center

Ferramenta visual desenvolvida com **Streamlit**, **ReportLab** e a API oficial **Google GenAI (`google-genai`)** para auditoria de chamadas telefônicas de suporte técnico nos formatos compactados **.ogg**, **.mp3**, **.m4a** e **.aac** (com limite de segurança de até **64 MB** e até **2 horas** de áudio por ligação; formatos sem compactação como `.wav` são rejeitados para economia de banda e recursos).

---

## ✨ Principais Funcionalidades

1. **Autenticação Individual por Atendente**:
   - Cada atendente insere sua própria Chave de API do Google Gemini no menu lateral antes de processar.
   - Chave protegida (campo de senha) e lembrada durante a sessão.

2. **Player com Legenda Dinâmica e Sincronizada em Tempo Real**:
   - **Legenda Fixa no Topo:** Acompanhe a fala que está sendo dita no exato segundo da reprodução.
   - **Realce Dinâmico:** Cada fala ganha destaque luminoso e o texto rola automaticamente (*auto-scroll*).
   - **Clique para Pular:** Clique em qualquer fala ou timestamp no texto para saltar o áudio instantaneamente para aquele segundo.
   - **Seletor de Velocidade com Indicação Ativa:** Marcador visual destacado da velocidade atual (0.75x, 1.0x, 1.25x, 1.5x, 1.75x, 2.0x), sincronizado inclusive com os controles nativos do navegador e salvo em cache local.

3. **✂️ Botão de Cortar Silêncios / Esperas Telefônicas**:
   - Pula automaticamente pausas, músicas de espera ou períodos sem diálogo maiores que 1.8s, economizando tempo valioso do analista ao ouvir chamadas longas.
   - Botões de navegação rápida `⏮️ Anterior` e `⏭️ Próxima` para saltar de fala em fala.

4. **Diferenciação Rigorosa de Vozes (Sem misturar interlocutores)**:
   - **Atendente:** Quem atende em nome da **Infobrasil Sistemas**.
   - **Cliente:** Pessoa externa que relata a dúvida ou problema.
   - A IA utiliza o padrão de quem com certeza é cliente para diferenciar a voz do atendente com máxima clareza.
   - Sem misturar nomes ou inventar parênteses confusos.

5. **Empresa do Cliente (Nome Fantasia e Razão Social)**:
   - Destaque claro para a **Empresa do Cliente** (quem solicitou o chamado).
   - Separação entre **Nome Fantasia** e **Razão Social** (quando mencionada).

6. **Metadados Completos de Auditoria**:
   - **Situação da Demanda:** *Resolvido no atendimento*, *Pendente / Aguardando retorno*, *Em andamento* ou *Encaminhado*.
   - **Prazo Informado:** Prazos acordados ou prometidos pelo suporte (ex: *em 2 horas*, *até o fim do dia*).
   - **Protocolo Principal & Outros Protocolos:** Detecção de novos protocolos e números de chamados anteriores citados.
   - **Sentimento do Cliente:** Classificação (Positivo, Neutro ou Insatisfeito).
   - **Resumo Executivo:** Síntese em poucas frases para rápida leitura gerencial.

7. **Exportação & Documentação Formal**:
   - 📕 **Download de Relatório Oficial em PDF (.pdf):** Documento formatado pronto para impressão e arquivo, com cabeçalho institucional da Infobrasil, tabela de metadados, resumo e diálogo completo com paginação automática.
   - 📄 **Download em Texto (.txt)**.
   - 📊 **Download em JSON (.json)**.

---

## 🚀 Como Executar

### 1. Iniciar com 1 Clique (Windows)
Dê um duplo clique no arquivo:
`iniciar.bat`

### 2. Ou pelo Terminal
```bash
streamlit run app.py
```
Acesse no navegador: `http://localhost:8501`.

---

## 🔗 Integração com o InfoMonitorDBClientes
O transcritor é integrado diretamente ao sistema principal de monitoramento:
- Botão `🎧 Transcritor Call Center` na barra de navegação superior e no menu suspenso de **Gestão** em `bancos.py`.
- Botão `🎧 Transcritor Call Center` no cabeçalho do módulo `horarios.py`.
