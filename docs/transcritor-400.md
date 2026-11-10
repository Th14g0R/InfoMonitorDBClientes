# Diagnóstico Técnico e Roteiro de Implantação: Transcritor Call Center (Investigação do Erro 400 INVALID_ARGUMENT)

**Data da Análise:** 10/11/2026
**Serviço Afetado:** Transcritor de Call Center (`callcenter_transcriber`), interface Streamlit em `http://192.168.243.2:8501/`
**Servidor de Produção:** `SRV-ADS002` (IP `192.168.243.2`), serviço Windows NSSM `InfoTranscritorCallCenter`
**Estado:** Documento de diagnóstico e roteiro operacional. **Nenhuma alteração em código de produto ou execução em servidor de produção sem aprovação prévia.**

---

## 1. Descrição do Problema e Sintomas Registrados

Ao submeter a gravação de chamada telefônica em formato compactado `.ogg` (`audio-teste.ogg`, contêiner OGG/Vorbis, 653.553 bytes), o Streamlit interrompe a execução com a mensagem genérica:

```text
Erro durante o processamento: 400 INVALID_ARGUMENT.
{'error': {'code': 400, 'message': 'Request contains an invalid argument.', 'status': 'INVALID_ARGUMENT'}}
```

### Pontos de Atenção na Investigação:
1. **Validação da Chave API:** O indicativo visual na interface ("Chave API pronta para esta sessão") reflete apenas que o campo de senha continha uma string não-vazia (`if api_key:`). **Não há validação prévia de conectividade ou autorização da chave contra os endpoints da Google**.
2. **Indeterminação da Etapa da Falha:** A tela do Streamlit encapsula todo o fluxo de processamento sob um único bloco `try/except Exception`. Dessa forma, o erro 400 pode ter ocorrido em qualquer uma das etapas:
   - No upload do arquivo via Google File API (`client.files.upload`);
   - No loop de verificação de estado (`client.files.get`);
   - Na primeira inferência de transcrição (`client.models.generate_content`);
   A segunda inferência de metadados (`resp_meta`) tinha tratamento próprio que ocultava suas falhas; ela não explica o erro exibido no fluxo original.
3. **Necessidade de Evidência Empírica:** Nenhuma hipótese deve ser tratada como causa confirmada sem teste isolado reproduzível com a chave API real.

---

## 2. Análise Técnica Baseada em Contratos Oficiais Google GenAI

### 2.1. Formatos de Áudio e MIME Types Oficiais

Fontes oficiais diretas:
- **Áudio no Gemini:** [https://ai.google.dev/gemini-api/docs/audio](https://ai.google.dev/gemini-api/docs/audio)
- **Google File API:** [https://ai.google.dev/gemini-api/docs/files](https://ai.google.dev/gemini-api/docs/files)
- **SDK Oficial Python (`google-genai`):** [https://googleapis.github.io/python-genai/](https://googleapis.github.io/python-genai/)

A aplicação restringe o envio estritamente aos quatro formatos compactados permitidos (`FORMATOS_PERMITIDOS = [".mp3", ".ogg", ".m4a", ".aac"]`):

| Extensão | Codec Típico | MIME Type Oficial Aceito | Observações Contratuais |
|---|---|---|---|
| `.ogg` | Vorbis / Opus | `audio/ogg` | **Obrigatório passar `audio/ogg`.** MIME types como `application/ogg` ou `video/ogg` não são aceitos para a modalidade de áudio. |
| `.mp3` | MPEG-1 Audio Layer III | `audio/mp3` (ou `audio/mpeg`) | Ambos são aceitos na documentação oficial. |
| `.m4a` | AAC em contêiner MP4 | `audio/mp4` ou `audio/m4a` | Ambos constam na especificação do SDK. Mapear deterministamente para `audio/mp4` ou `audio/m4a`. |
| `.aac` | AAC (ADTS bitstream) | `audio/aac` | Padrão oficial para streams elementares AAC com cabeçalhos ADTS. |

> [!CRITICAL]
> **Extensões Desconhecidas:** NUNCA adotar um MIME type padrão (como `audio/ogg`) para extensões desconhecidas ou não mapeadas. Se o arquivo não pertencer estritamente a `FORMATOS_PERMITIDOS`, a requisição deve ser rejeitada imediatamente com erro claro antes de qualquer chamada externa.

### 2.2. Conclusão Empírica Decisiva (Teste C2 e Isolamento da Falha)

O teste de controle (C2) foi executado contra a API real em 10/11/2026:
1. **Flash Suporta OGG Vorbis 8kHz Mono:** O recorte/reparo local de controle `audio-controle-vorbis8k.ogg` (32.063 bytes, primeiras 9 páginas da gravação com flag EOS e CRC válido, fora do Git) **foi processado e transcrito com sucesso pelo modelo `gemini-3.5-flash-lite`**.
2. **Defeito confirmado e limite do experimento:** O original tem CRC inválido na página #9 (offset 32.063) e quebra do encadeamento no próximo offset esperado, 36.264. A mesma origem com estrutura válida passa no Flash; o original com a página inválida falha. O controle é um recorte curto, diferindo também na duração: não demonstra recuperação integral da chamada nem permite afirmar o comportamento interno do decodificador da Google.
3. **Manutenção dos Modelos Flash:** Fica dispensada a migração para modelos especializados como `gemini-3.5-transcribe` ou criação de novos seletores complexos. O pipeline mantém os modelos Flash padrão (`gemini-3.5-flash-lite`, `gemini-3.8-flash`).
4. **Higiene de Dados e Segredos:** O arquivo de controle é um recorte/reparo do áudio real (contendo voz/PII que deve ser ignorada em testes e nunca versionada em git). Nenhum áudio ou chave foi adicionado ao repositório git.

### 2.3. Correção proposta

A solução adotada consolida uma defesa em múltiplas camadas no processamento de gravações:

1. **Pré-Validação Rápida de Contêiner (`callcenter_transcriber/ogg_validation.py`):**
   - Antes de iniciar o upload via File API, o áudio `.ogg` passa pela função pura `validar_ogg(audio_bytes)`.
   - Rejeita imediatamente arquivos com corrupção de CRC, quebra de encadeamento, truncamento, gaps ou multiplexação concorrente de seriais, apresentando mensagem clara de erro ao usuário na interface sem onerar a cota de API.
   - Suporta contêineres encadeados sequenciais (Chaining) somando a duração através dos múltiplos fluxos lógicos.
   - Taxa de transferência medida: ~1.150 MB/s (valida 20 MB em menos de 17 ms).
2. **MIME Type Determinístico (Defesa em Profundidade):**
   - O upload no File API passa explicitamente `config={'mime_type': 'audio/ogg'}` (ou o MIME correspondente da extensão permitida), evitando que o SDK recorra a resoluções ambíguas do sistema operacional.
3. **Guard de Timeout no Estado `ACTIVE`:**
   - O loop de polling valida que o arquivo atingiu `ACTIVE` e aborta com `TimeoutError` antes de chamar `generate_content`.
4. **Tratamento Resiliente de Erros e Metadados:**
   - Falhas na extração de metadados em JSON (Etapa 2) preservam o diálogo transcrito (Etapa 1) com alerta em tela.
   - Fallback de modelos restrito estritamente a indisponibilidade (503) ou modelo não encontrado (404), sem mascarar erros de arquivo.
5. **Recuperação Automática e Transparente de Contêiner Danificado (`audio_normalization.py` + `vlc_decoder.py`):**
   - Gravações com contêiner OGG danificado (gaps de CRC, quebra de encadeamento por centrais telefônicas/PBX) que possuam cabeçalho inicial íntegro não bloqueiam mais o botão de transcrição.
   - O sistema gera uma cópia temporária normalizada em formato FLAC lossless (`audio/flac`) utilizando decodificador homologado sem acesso à rede, sem uso de shell e sem qualquer reescrita manual de CRC/bytes.
   - **Histórico e Prova Técnica:** Em experimentos preliminares, uma reescrita sintética de cabeçalhos/CRC alcançou ~99s, mas foi **completamente descartada** por alterar a estrutura e poder mascarar defeitos; **não faz parte da entrega nem entra em produção**. Ao rodar o FFmpeg puro diretamente sobre os bytes originais avariados, a decodificação estancou aos 9,376s (o prefixo íntegro termina na página #8, antes da página #9 inválida).
   - **Decisão Arquitetural (Fallback VLC 3.0.24):** Para contêineres com descontinuidade de fluxo gerados por PBX, o sistema utiliza o decodificador local homologado **VLC Media Player 3.0.24** (Win64 Portable Executable / Installer oficial da VideoLAN, `https://www.videolan.org/vlc/`), combinado ao FFmpeg para codificação em FLAC e prova de integridade da cópia. No áudio real do incidente, este fluxo decodificou **195,36s**, sem comprovar recuperação integral da gravação original.
   - **Parâmetros de Segurança do VLC:** Execução headless e isolada com flags reais homologadas (`-I dummy`, `--dummy-quiet`, `--ignore-config`, `--no-one-instance`, `--no-media-library`, `--no-metadata-network-access`, `--no-sub-autodetect-file`, `--no-video`, `--no-spu`, `--no-osd`, `--no-stats`, `--no-interact`, `--play-and-exit`, `--stop-time 7201`, `--no-repeat`, `--no-loop`, `--no-auto-preparse`, `--demux ogg`, file MRL e `sout` transcode PCM `s16l` mono), sem uso de shell, sem acesso à rede, `stderr` descartado via `DEVNULL`, ambiente sanitizado mínimo (remoção de variáveis `VLC_*` e padrões de credenciais; redirecionamento de `APPDATA`, `LOCALAPPDATA`, `USERPROFILE` e `TEMP` para diretório temporário isolado; preservação estrita de variáveis essenciais do sistema), timeout com encerramento forçado (`kill` + `wait` antes de qualquer cleanup) e controle de concorrência por semáforo mono-processo.
   - **Configuração do Administrador (`TRANSCRITOR_VLC_EXE`):** O caminho do binário deve ser configurado via caminho absoluto estrito na variável de ambiente `TRANSCRITOR_VLC_EXE` (ex: `C:\Program Files\VideoLAN\VLC\vlc.exe`). O sistema **nunca busca no `%PATH%`**, nem em caminhos relativos ou chaves de registro do Windows, prevenindo injeções de binários.
   - **Guarda de Falha Fechada:** Caso o decodificador não ultrapasse o prefixo íntegro inicial pré-defeito (ou caso a dependência opcional do VLC não esteja instalada no ambiente), o sistema opera em **falha fechada**: aborta imediatamente sem efetuar upload no Gemini, emitindo aviso orientativo de que não foi possível recuperar a gravação além do trecho inicial.
   - **Cache Invalidation Version:** A chave de cache foi atualizada para `NORMALIZER_VERSION = "v3_flac_vlc_3024"`, invalidando artefatos de tentativas anteriores gerados por encoders legados ou falhas fechadas do FFmpeg puro.
   - A UI exibe aviso persistente de **Recuperação Parcial** destacando a duração decodificada e alertando para possíveis cortes caso a chamada original fosse mais longa.
   - O aviso de integridade é gravado de forma indelével nas exportações oficiais (PDF, TXT, JSON).
   - O player interativo HTML5 reproduz os mesmos bytes FLAC enviados à API Gemini. Os timestamps gerados pelo modelo são aproximados e se referem a essa cópia, não à linha de tempo original do PBX. FLAC reduz o volume em memória em relação ao WAV; a saída continua limitada a 64 MiB.

### 2.4. Modelos e Verificação de Disponibilidade (`client.models.list`)

- **Descoberta Dinâmica:** Em vez de assumir a validade estática de modelos selecionados na interface, a lista de modelos suportados para geração de conteúdo multimídia deve ser confirmada contra a chave real por meio de `client.models.list()`.
- **Diferenciação de Erros de Modelo vs Payload:**
  - Erro de modelo inexistente, não suportado para o método ou não habilitado para a chave normalmente retorna **`404 NOT_FOUND`** (ou mensagem indicando que o recurso do modelo não existe na versão da API).
  - Erro `400 INVALID_ARGUMENT` decorre tipicamente de payload malformado, arquivo em estado não ativo ou incompatibilidade de modalidade de mídia.

### 2.5. Whitelist Estrita de Fallback de Modelos

Caso seja implementado fallback entre modelos na transcrição:
- **Permitido tentar próximo modelo APENAS para:**
  - Erros de capacidade e indisponibilidade: HTTP `503`, `UNAVAILABLE`, `"high demand"`.
  - Erros específicos de modelo: HTTP `404` (`NOT_FOUND`), mensagens explícitas de modelo inexistente ou não suportado para a operação.
- **Estritamente proibido disparar fallback para:**
  - `400 INVALID_ARGUMENT` genérico;
  - Erros de autenticação ou chave (`API_KEY_INVALID`, 401, 403);
  - Erros de MIME type ou arquivo não processável;
  - Erros de validação de schema JSON (`response_schema`).

*Justificativa:* Erros de payload ou credencial falharão igualmente em qualquer modelo subsequente, mascarando a raiz do problema e gerando tráfego espúrio.

### 2.6. Tratamento Obrigatório de Timeout no Polling (`ACTIVE`)

No loop de polling:
```python
for _ in range(30):
    if hasattr(gemini_file, "state"):
        state_name = getattr(gemini_file.state, "name", str(gemini_file.state))
        if state_name == "ACTIVE":
            break
        elif state_name in ("FAILED", "ERROR"):
            raise RuntimeError(f"O processamento do áudio falhou no Gemini (estado: {state_name}).")
    time.sleep(1)
    gemini_file = client.files.get(name=gemini_file.name)
# Reavaliar também o resultado da última consulta.
state_name = getattr(gemini_file.state, "name", str(gemini_file.state))
if state_name != "ACTIVE":
    raise TimeoutError("O arquivo de áudio não ficou pronto no prazo de processamento.")
```
Se os 30 segundos expirarem e o arquivo ainda estiver em `PROCESSING`:
- O código **deve bloquear** a chamada de `generate_content`.
- Deve lançar `TimeoutError` explícito:
  `"O arquivo de áudio não atingiu o estado ACTIVE após 30 segundos (estado atual: {state_name})."`

### 2.7. Isolamento e Resiliência na Extração de Metadados

- Se a Etapa 2 (`resp_meta`, extração de JSON) falhar por timeout, formatação ou restrição de tokens:
  - O diálogo já transcrito na Etapa 1 **não deve ser descartado**;
  - A interface deve exibir a transcrição completa, preencher metadados padrão e emitir alerta visual (`st.warning`).
- **Resiliência a Indisponibilidade Transitória (503 / UNAVAILABLE):**
  - Para falhas 503/UNAVAILABLE estritamente na extração de metadados, o pipeline executa até **3 tentativas** com esperas de **2s e 4s** entre as falhas, sem trocar de modelo.
  - **Isolamento Estrito de Escopo:** O retry aplica-se **exclusivamente à chamada de metadados** (`resp_meta`). A normalização do áudio, o upload no File API e a transcrição já finalizada na Etapa 1 **nunca são repetidos**.
  - Erros não-temporários (400, 403, 429) ou respostas com payload/JSON inválido **não realizam retry**.
  - Caso a indisponibilidade persista após as 3 tentativas, o sistema preenche os metadados com valores padrão e exibe aviso amigável: *"Transcrição concluída; dados complementares temporariamente indisponíveis."*, sem expor erros brutos ou exceções ao operador.

---

## 3. Roteiro e Matriz Mínima de Testes

| ID | Objetivo do Teste | Tipo | Critério de Aceite |
|---|---|---|---|
| **M1** | Mapeamento estrito de MIME types | Offline (Unitário) | `.ogg` -> `audio/ogg`, `.mp3` -> `audio/mp3`, `.m4a` -> `audio/mp4`, `.aac` -> `audio/aac`. Extensão desconhecida -> rejeição. |
| **M2** | Upload com `config={'mime_type': ...}` | Offline (Mock) | Garantir que o parâmetro `config` é enviado com o MIME determinístico na chamada `files.upload`. |
| **M3** | Bloqueio de inferência em timeout do estado `ACTIVE` | Offline (Mock) | Se `state != "ACTIVE"` após o loop, `client.models.generate_content` **não é chamado** e lança `TimeoutError`. |
| **M4** | Whitelist de fallback | Offline (Mock) | `404 NOT_FOUND` aciona modelo alternativo; `400 INVALID_ARGUMENT` e `API_KEY_INVALID` encerram sem tentar próximo modelo. |
| **M5** | Resiliência da transcrição em falha de metadados | Offline (Mock) | Falha na Etapa 2 mantém o diálogo da Etapa 1 e exibe aviso em tela. |
| **M6** | Reprodução e validação do áudio real | Online (Chave real) | Executar teste mínimo isolado com `audio-teste.ogg`, identificando a etapa exata e comprovando a eficácia da correção antes do deploy. |

---

## 4. Política de Privacidade e Higiene de Segredos

- NUNCA commitar, versionar ou logar chaves de API (`AIzaSy...`).
- A chave deve permanecer estritamente em memória de sessão (`st.session_state`) ou em arquivo `.env` local explicitamente listado no `.gitignore`.
- Arquivos temporários gerados (`NamedTemporaryFile`) e arquivos remotos no Google File API devem ser excluídos no bloco `finally`.

---

## 5. Roteiro de Implantação e Operação via NSSM (SRV-ADS002)

O transcritor opera em produção gerenciado pelo NSSM sob o nome `InfoTranscritorCallCenter`.
**Atenção: Não executar nenhum comando no servidor sem aprovação prévia do Thiago.**

### 5.1. Dados do Ambiente em `SRV-ADS002` (192.168.243.2)
- **Diretório da Aplicação:** `C:\Tomcat 9.0\webapps\InfoMonitorDBClientes\callcenter_transcriber`
- **Python do Serviço:** `C:\Users\thiago.rezende\AppData\Local\Programs\Python\Python314\python.exe` (Python compartilhado do usuário)
- **Serviço NSSM:** `InfoTranscritorCallCenter`

### 5.2. Procedimento de Implantação Passo a Passo

#### Passo 1: Registrar dependências pré-existentes (Pre-install freeze)
Como o Python utilizado é compartilhado no ambiente do usuário, registre as versões atuais antes de qualquer intervenção:
```powershell
$py = "C:\Users\thiago.rezende\AppData\Local\Programs\Python\Python314\python.exe"
& $py -m pip list --format=freeze > "C:\Backups\pre_deploy_callcenter_freeze.txt"
```

#### Passo 2: Criar backup seguro FORA da webroot
O backup deve ser armazenado fora da raiz do Tomcat (`webapps`), com permissões restritas (contém `.env` com configurações locais):
```powershell
$dataHora = Get-Date -Format "yyyyMMdd_HHmmss"
$origem = "C:\Tomcat 9.0\webapps\InfoMonitorDBClientes\callcenter_transcriber"
$dirBackup = "C:\Backups\callcenter_transcriber_bkp_$dataHora"

New-Item -ItemType Directory -Path $dirBackup -Force
Copy-Item -Path "$origem\*" -Destination $dirBackup -Recurse
Write-Host "Backup seguro armazenado em: $dirBackup"
```

Antes de parar o serviço, validar o pacote de staging:
```powershell
# Diretório de staging contendo os arquivos da branch homologada.
# Preparar e validar esta origem ANTES de parar o serviço no passo 3.
$src = 'C:\TEMP\release-transcritor\callcenter_transcriber'
$arquivos = @(
    'app.py',
    'requirements.txt',
    'ogg_validation.py',
    'gemini_transcriber.py',
    'audio_normalization.py',
    'vlc_decoder.py',
    'pdf_generator.py'
)
foreach ($arquivo in $arquivos) {
    if (-not (Test-Path -LiteralPath (Join-Path $src $arquivo) -PathType Leaf)) {
        throw "Arquivo da entrega ausente: $arquivo"
    }
}
```

#### Passo 3: Parar o serviço NSSM
```powershell
nssm stop InfoTranscritorCallCenter
nssm status InfoTranscritorCallCenter  # Deve responder SERVICE_STOPPED
```

#### Passo 4: Atualizar estritamente os arquivos da entrega
```powershell
foreach ($arquivo in $arquivos) {
    Copy-Item -LiteralPath (Join-Path $src $arquivo) -Destination (Join-Path $origem $arquivo) -Force
}
```

#### Passo 5: Instalar dependências fixadas exatamente conforme especificado
Usar os requisitos homologados, incluindo Mutagen. Gemini e Streamlit permanecem nas versões exatas solicitadas. Conferir o plano do pip contra o inventário prévio antes de autorizar mudanças no Python compartilhado:
```powershell
& $py -m pip install --dry-run -r (Join-Path $origem 'requirements.txt')
& $py -m pip install -r (Join-Path $origem 'requirements.txt')
```

#### Passo 6: Iniciar o serviço NSSM
```powershell
nssm start InfoTranscritorCallCenter
nssm status InfoTranscritorCallCenter  # Deve responder SERVICE_RUNNING
```

#### Passo 7: Validação Pós-Implantação
1. Verificar conectividade HTTP: `http://192.168.243.2:8501/`.
2. Validar uma gravação íntegra com a chave do operador e confirmar transcrição e metadados. O áudio original do incidente deve ser recusado antes do upload com orientação de nova exportação; não esperar sua transcrição automática.
3. Inspecionar logs de erro do NSSM/Streamlit caso ocorra qualquer intercorrência.

### 5.3. Plano de Rollback Estrito e Não-Destrutivo

> [!CAUTION]
> **NUNCA utilizar `Remove-Item * -Recurse` no diretório de produção.** Isso destruiria o `.env` de produção, logs e arquivos de configuração locais.

Em caso de necessidade de reversão, restaurar **apenas os arquivos que foram objeto da entrega**:
```powershell
nssm stop InfoTranscritorCallCenter

# Restaura pontualmente os arquivos modificados da entrega a partir do backup
Copy-Item -Path "$dirBackup\app.py" -Destination "$origem\app.py" -Force
Copy-Item -Path "$dirBackup\requirements.txt" -Destination "$origem\requirements.txt" -Force
Copy-Item -Path "$dirBackup\pdf_generator.py" -Destination "$origem\pdf_generator.py" -Force

# Remove novos módulos auxiliares que foram adicionados nesta entrega (sem wildcard):
Remove-Item -Path "$origem\ogg_validation.py" -ErrorAction SilentlyContinue
Remove-Item -Path "$origem\gemini_transcriber.py" -ErrorAction SilentlyContinue
Remove-Item -Path "$origem\audio_normalization.py" -ErrorAction SilentlyContinue
Remove-Item -Path "$origem\vlc_decoder.py" -ErrorAction SilentlyContinue

# Se houver mudanças de pacotes, reinstalar apenas os afetados nas versões
# exatas do inventário anterior do passo 1: pip install 'pacote==versao_anterior'.
# O requirements antigo com '>=' não restaura versões anteriores.

nssm start InfoTranscritorCallCenter
nssm status InfoTranscritorCallCenter
```
Verificar que a versão anterior está em execução sem efeitos colaterais nos arquivos locais.
