# Recuperação automática de áudio — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Avisar sobre OGG inválido, tentar normalizar uma cópia local e transcrever automaticamente a saída validada, sem alegar recuperação integral.
**Architecture:** Conversor isolado com FFmpeg homologado, sem shell e sem downloads em runtime. Integração no helper Gemini e UI Streamlit; áudio íntegro mantém o fluxo atual. Reprodução e timestamps devem usar a mesma cópia efetivamente transcrita quando houver normalização.
**Tech Stack:** Python 3.14, Streamlit 1.65.0, google-genai 2.28.0, FFmpeg local, unittest.
**Spec:** `.maestri/roles/dd265fa7-8820-4979-9a29-54751165734d/transcritor-400/proposta-recuperacao.md` (aprovada pelo usuário).

**Ruling de implementação:** usar FLAC nativo (`audio/flac`) para a cópia final, com limite de 64 MiB, em vez de manter WAV PCM grande na sessão e no player base64. Formato foi deixado à homologação no desenho aprovado; ambos os executores alinharam FLAC. Saída excessiva é recusada, nunca truncada e aceita. Os pontos abaixo que mencionam WAV descrevem a opção inicial; aplicar validação equivalente de FLAC/frames/decodificação real no formato escolhido.

**Ruling de decodificação após experimento:** FFmpeg direto sobre o original ficou em 9,376 s / 9,344 s (duas variantes), sem superar o prefixo íntegro de 9,376 s. Rejeitar essas saídas, sem upload. O experimento anterior de 99,136 s usou reescrita de cabeçalhos e foi descartado; nenhum rewrite de serial/seq/CRC entra no produto. VLC já instalado localmente decodificou 195,360 s sem alterar o original. Usar VLC como alternativa de decodificação local e FFmpeg como encoder/validador FLAC preserva o comportamento aprovado, sem reconstrução forense. Não autoriza instalação ou configuração no servidor de produção.

**Task 1 — complemento VLC:** resolver binário absoluto configurado pelo administrador (sem PATH/registro/cwd), nova instância dummy sem configuração/perfil/metadata em rede, ambiente mínimo isolado no tempdir e arquivo de entrada fixo via file MRL/access file/demux ogg. Controlar timeout, processo e limite de saída. WAV deve passar parser PCM e tamanho real; exit code 0 sozinho não basta. FLAC final deve passar round-trip de PCM, contagem de amostras, MD5 STREAMINFO quando informado, áudio não nulo, prefixo e tetos. Dependência ausente ou saída insuficiente falham de forma segura. Testar exit 0 sem WAV, cabeçalho inválido, timeout, argumentos e ambiente. Aviso persistente de recuperação parcial com duração efetivamente decodificada pelo VLC, sem percentual de completude.

## Global Constraints
- Branch `fix/transcritor-400`; preservar trabalho existente e pasta untracked preexistente.
- Sem push, merge ou alteração em produção. Instalar ferramenta somente no ambiente local de teste; documentar implantação futura.
- Original e áudios recuperados fora do Git; sem transcrição, chave ou stderr bruto em logs.
- Conversão somente para OGG que falhou na validação; manter os limites originais de upload (64 MB, formatos compactados).
- Sem threshold arbitrário de 70% baseado no Mutagen, sem promessa de reconstruir pacotes perdidos.

## Review Focus
- Recuperação não pode parar silenciosamente no primeiro trecho de ~10 s: evidência real com arquivo fornecido e verificação da saída completa decodificável.
- Decoder que sai 0 mas produz vazio, saída truncada, duração acima de 2 h, timeout e saída inválida devem impedir upload.
- Cópia normalizada e original nunca confundidos na reprodução/legenda, cache ou tentativas posteriores.
- FFmpeg ausente deve produzir erro claro, sem instalação/download automático.
- Segredos e caminhos técnicos não devem aparecer nas mensagens nem em logs.

## Task 1 — Conversor local e prova mínima (Executor Thiago #2)
**Files:** criar `callcenter_transcriber/audio_normalization.py` e `tests/test_audio_normalization.py`; dependência/runbook definidos com Rapadura.
**Interface:** definir e comunicar função/context manager que consome bytes/nome, produz cópia WAV PCM mono 16 kHz, duração e avisos, garantindo limpeza de temporários em todos os caminhos. Nunca modifica input. Não retorna caminhos eliminados antes do consumidor usar.
- [ ] Homologar FFmpeg geral local por distribuição confiável versionada; registrar versão/proveniência. Não usar o binário limitado do Playwright.
- [ ] Escrever e executar testes vermelhos para normalização, subprocess sem shell, timeout, ausência de ferramenta, falha/saída vazia, limites e cleanup.
- [ ] Implementar mínimo: execução com argumentos fixos (input temporário gerado), prazo 120 s, WAV PCM mono 16 kHz; limitar saída a 2 h + 1 s para detectar e recusar excesso, não cortar e aceitar. Saída limitada a 256 MiB e verificada com parser WAV.
- [ ] Validar duração real por frames/rate, canais e PCM, integralidade do payload e decodificação da saída. Preservar avisos de perda potencial; não inferir duração completa do original danificado.
- [ ] Testar em cópia do áudio real fornecido, sem API inicialmente; registrar apenas bytes, duração e métricas técnicas. Comparar além do trecho inicial; relatar limites da prova.
- [ ] Reportar interface e freeze para integração/revisão. Sem commit nesta etapa.

## Task 2 — UI e fluxo Gemini (Executor Rapadura, após contrato Task 1)
**Files:** `callcenter_transcriber/gemini_transcriber.py`, `callcenter_transcriber/app.py`, `callcenter_transcriber/ogg_validation.py`, `callcenter_transcriber/pdf_generator.py`, `tests/test_callcenter_transcriber.py`, `tests/test_ogg_validation.py`, `callcenter_transcriber/requirements.txt`, `docs/transcritor-400.md`. Cache version atualizada para `NORMALIZER_VERSION = "v3_flac_vlc_3024"`.
- [x] Escrever testes vermelhos: íntegro sem conversão; OGG inválido recuperável segue; falha sem upload; aviso persistente; player usa áudio transcrito; original permanece inalterado.
- [x] Exportar helper canônico `obter_prefixo_valido_pre_defeito(dados)` com fail-closed para `None` e suporte a Vorbis/Opus.
- [x] Separar inválido recuperável de upload bloqueado por extensão/tamanho. Manter cópia e estado de recuperação por identidade/conteúdo com invalidação de cache (C3) via `sincronizar_upload_session`.
- [x] No clique normal de transcrever, avisar, recuperar, validar saída e só depois chamar Gemini com MIME audio/flac. Se falhar ou não superar prefixo pré-defeito, mensagem segura para reexportar; sem chamada externa nem upload.
- [x] Mostrar duração da cópia e aviso de possíveis cortes; disponibilizar reprodução da mesma cópia usada para os timestamps. Não anunciar integralidade garantida.
- [x] Integrar dependência pinada e roteiro de implantação NSSM (incluindo `vlc_decoder.py`, `audio_normalization.py`, `pdf_generator.py` no `$arquivos` e rollback estrito sem wildcards); sem instalar no servidor.
- [x] Rodar focused/UI no SDK 2.28.0 e Streamlit 1.65.0; pedir revisão do Revisor.

## Task 3 — Fechamento (Maestro e Revisor)
- [ ] Revisão do diff: fluxo aprovado, segurança subprocess/segredos, limites, cleanup, integridade, player e cache.
- [ ] Suite completa com dependências reais e focused no ambiente homologado. Prova real recuperada + chamada Gemini controlada autorizada pelo teste solicitado, sem imprimir fala; indicar se resposta cobre apenas trecho ou fluxo útil completo.
- [ ] Reiniciar somente Streamlit local 127.0.0.1:8502, confirmar health/UI; atualizar grafo local com GRAPHIFY_BACKEND=none e estado do Maestro.
- [ ] Mostrar resultado e link ao usuário; parar antes de push/merge/produção.

## Task 4 — Metadados resilientes após 503 (ajuste aprovado pelo usuário)
**Aprovação:** usuário aceitou a recomendação de tentativas limitadas apenas nos metadados e mensagem simples se a indisponibilidade persistir. Recuperação/transcrição concluídas não devem ser repetidas.
**Files:** `callcenter_transcriber/gemini_transcriber.py`, `tests/test_callcenter_transcriber.py`, `docs/transcritor-400.md`.
- [x] Testes: metadados 503→sucesso; três 503; erro 400/403/429 sem retry; JSON inválido sem retry; sucesso inicial; aviso sem erro bruto/segredos.
- [x] Implementar no máximo **3 chamadas** da etapa de metadados, esperas **2 s e 4 s** entre falhas 503/UNAVAILABLE. Classificar por código/status estruturado, nunca substring. Não trocar de modelo nem repetir upload, normalização ou transcrição.
- [x] Restringir retries internos do SDK **somente nessa chamada** com `GenerateContentConfig.http_options=HttpOptions(retry_options=HttpRetryOptions(attempts=1))`, disponível no SDK 2.28.0. Preservar schema, prompt e demais configurações.
- [x] Se esgotar: retornar transcrição/dados padrão e aviso fixo: “Transcrição concluída; dados complementares temporariamente indisponíveis.” Para falha não temporária, aviso simples sem corpo de exceção. Preservar o aviso independente de recuperação parcial e eventual aviso de truncamento.
- [x] Asserções de contagem: uma normalização, um upload, uma transcrição; até três metadados. Sleeps mockados, sem API/rede/PII nos testes.
- [x] Revisão focada do Claude; **86 focused e 359 na suíte completa OK**. Servidor local 8502 atualizado (PID 18540), sem push, merge ou produção. Estado do Maestro atualizado.
