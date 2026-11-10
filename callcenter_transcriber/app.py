import json
import os
import re
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
try:
    from dotenv import load_dotenv
    _current_dir = Path(__file__).resolve().parent
    load_dotenv(_current_dir / ".env")
    load_dotenv(_current_dir.parent / ".env")
except ImportError:
    pass

try:
    from callcenter_transcriber import pdf_generator, player_component
except ImportError:
    import pdf_generator
    import player_component

try:
    from callcenter_transcriber.gemini_transcriber import (
        FalaItem,
        MetadadosChamada,
        MODELOS_DISPONIVEIS,
        inspecionar_audio,
        obter_mime_audio,
        processar_audio,
        sincronizar_upload_session,
    )
except ImportError:
    try:
        from gemini_transcriber import (
            FalaItem,
            MetadadosChamada,
            MODELOS_DISPONIVEIS,
            inspecionar_audio,
            obter_mime_audio,
            processar_audio,
            sincronizar_upload_session,
        )
    except ImportError as e_sdk:
        st.error(
            "⚠️ **Dependência do sistema ausente:** Não foi possível inicializar o módulo do transcritor. "
            f"Verifique se o pacote `google-genai` está instalado no ambiente Python. Detalhes: {e_sdk}"
        )
        st.stop()

# ---------------------------------------------------------
# Configuração da Página
# ---------------------------------------------------------
st.set_page_config(
    page_title="Infobrasil — Transcrição & Auditoria de Call Center",
    page_icon="🎧",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ---------------------------------------------------------
# Barra Lateral - Configurações e Chave de API
# ---------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Painel do Atendente")
    
    st.markdown("### 🔑 Chave de API Google Gemini")
    st.markdown(
        "Cada atendente deve digitar sua própria chave API para transcrever.\n\n"
        "👉 Obtenha sua chave gratuitamente em:\n"
        "[API keys | Google AI Studio](https://aistudio.google.com/api-keys)"
    )
    
    chave_salva = st.session_state.get("api_key", "").strip()
    api_key = st.text_input(
        "Chave API do Gemini:",
        value=chave_salva,
        type="password",
        placeholder="Cole sua chave AIzaSy... aqui",
        help="Chave de API pessoal do Google Gemini. Obtenha gratuitamente em: https://aistudio.google.com/api-keys"
    )
    if api_key:
        st.session_state["api_key"] = api_key.strip()
        st.success("✅ Chave API pronta para esta sessão.")
    else:
        st.warning("⚠️ Chave não informada. Digite sua chave acima para liberar a transcrição.")
    
    st.markdown("---")
    
    modelo_selecionado = st.selectbox(
        "Modelo de IA:",
        options=MODELOS_DISPONIVEIS,
        index=0,
        help="gemini-3.5-flash-lite é ultra-rápido, estável e ideal para áudios longos de telefonia."
    )
    
    st.markdown("---")
    st.markdown(
        """
        ### 💡 Diretrizes de Auditoria:
        - **Atendente:** Quem atende pela **Infobrasil Sistemas**.
        - **Cliente:** Pessoa externa que relata a dúvida ou problema.
        - **Empresa do Cliente:** Identificação por Nome Fantasia e Razão Social.
        - **Legenda & Player:** Seleção de velocidade e botão de **Cortar Silêncios / Esperas**.
        - **Exportação:** Relatório oficial em PDF formatado para documentação.
        """
    )
    
    st.markdown("---")
    st.markdown("[🖥️ **Acessar Monitor banco de dados**](http://192.168.243.2:8888/)")
    
    if "resultado" in st.session_state:
        if st.button("🗑️ Limpar Transcrição Atual", use_container_width=True):
            del st.session_state["resultado"]
            if "audio_bytes" in st.session_state:
                del st.session_state["audio_bytes"]
            if "meta_aviso" in st.session_state:
                del st.session_state["meta_aviso"]
            if "aviso_recuperacao" in st.session_state:
                del st.session_state["aviso_recuperacao"]
            # Limpa cache de cópia normalizada mantendo apenas 1 cópia ativa na sessão
            for k in list(st.session_state.keys()):
                if k.startswith("norm_"):
                    del st.session_state[k]
            st.rerun()

# ---------------------------------------------------------
# Cabeçalho Principal
# ---------------------------------------------------------
st.title("🎧 Infobrasil Sistemas — Auditoria & Transcrição de Chamadas")
st.markdown("Identificação inteligente de interlocutores, empresa do cliente, prazos e **reprodução com legenda sincronizada em tempo real**.")

# ---------------------------------------------------------
# Upload de Áudio
# ---------------------------------------------------------
col_upload, col_info = st.columns([1.3, 1.1])

with col_upload:
    uploaded_file = st.file_uploader(
        "Selecione o arquivo de gravação de ligação (formatos compactados):",
        type=["mp3", "ogg", "m4a", "aac"],
        help="Formatos aceitos: .ogg, .mp3, .m4a, .aac. Limites: máximo 64 MB e até 2 horas de áudio. Arquivos sem compactação (.wav) não são permitidos."
    )
    # C3: Invalida resultados, player anterior e caches norm_* se o arquivo mudou ou foi removido
    sincronizar_upload_session(uploaded_file, st.session_state)

with col_info:
    if uploaded_file is not None:
        info_audio = inspecionar_audio(uploaded_file, st.session_state)
        if not info_audio["valido"]:
            st.error("⚠️ **Arquivo não atende aos requisitos:**")
            for err in info_audio["erros"]:
                st.markdown(f"- ❌ {err}")
        else:
            st.markdown("**Resumo do Arquivo:**")
            dur_txt = f"\n\n⏱️ **Duração:** `{info_audio['duracao_formatada']}` *(Limite: 2h)*" if info_audio["duracao_segundos"] > 0 else ""
            aviso_txt = ""
            if info_audio.get("avisos"):
                aviso_txt = "\n\n⚠️ **Avisos:** " + "; ".join(info_audio["avisos"])
            st.success(
                f"📁 **Nome:** `{info_audio['nome']}`\n\n"
                f"⚖️ **Tamanho:** `{info_audio['tamanho_mb']:.2f} MB` *(Limite: 64 MB)*"
                f"{dur_txt}\n\n"
                f"✨ **Formato compactado aceito:** `{info_audio['ext']}`"
                f"{aviso_txt}"
            )

# ---------------------------------------------------------
# Botão de Execução da Transcrição
# ---------------------------------------------------------
if uploaded_file is not None:
    info_audio = inspecionar_audio(uploaded_file, st.session_state)
    chave_atual = (st.session_state.get("api_key") or os.getenv("GEMINI_API_KEY") or "").strip()
    
    if not info_audio["valido"]:
        st.error("🚫 **Envio bloqueado:** O arquivo não atende aos limites do sistema (contêiner corrompido, formato sem compactação, tamanho > 64 MB ou duração > 2 horas).")
        st.button(
            "🚫 Transcrição Bloqueada",
            disabled=True,
            use_container_width=True,
            help="Corrija os erros apontados no resumo do arquivo para prosseguir."
        )
    elif not chave_atual:
        st.warning("🔑 **Atenção Atendente:** Para iniciar a transcrição, digite ou cole sua **Chave API do Gemini** no menu lateral à esquerda (obtenha gratuitamente em: [API keys | Google AI Studio](https://aistudio.google.com/api-keys)).")
        st.button(
            "🚀 Transcrever e Auditar Ligação",
            disabled=True,
            use_container_width=True,
            help="Insira sua Chave API no menu lateral esquerdo para habilitar este botão."
        )
    else:
        if st.button("🚀 Transcrever e Auditar Ligação", type="primary", use_container_width=True):
            status_box = st.empty()
            with st.spinner("🎧 Processando gravação com IA..."):
                try:
                    resultado, modelo_usado, meta_aviso = processar_audio(
                        uploaded_file,
                        chave_atual,
                        modelo_selecionado,
                        status_box,
                        requer_recuperacao=info_audio.get("requer_recuperacao", False),
                        duracao_prefixo_valido=info_audio.get("duracao_prefixo_valido", 0.0),
                        session_cache=st.session_state
                    )
                    st.session_state["resultado"] = resultado
                    st.session_state["ultimo_arquivo"] = uploaded_file.name
                    # Utiliza os mesmos bytes e MIME que foram efetivamente transcritos (FLAC normalizado ou original)
                    st.session_state["audio_bytes"] = resultado.get("audio_bytes") or uploaded_file.getvalue()
                    st.session_state["mime_type"] = resultado.get("mime_type") or obter_mime_audio(uploaded_file.name)
                    st.session_state["modelo_usado"] = modelo_usado
                    if resultado.get("aviso_recuperacao"):
                        st.session_state["aviso_recuperacao"] = resultado["aviso_recuperacao"]
                    elif "aviso_recuperacao" in st.session_state:
                        del st.session_state["aviso_recuperacao"]

                    if meta_aviso:
                        st.session_state["meta_aviso"] = meta_aviso
                    elif "meta_aviso" in st.session_state:
                        del st.session_state["meta_aviso"]
                    status_box.empty()
                    
                    if modelo_usado != modelo_selecionado:
                        st.warning(f"O modelo `{modelo_selecionado}` estava momentaneamente indisponível. Alternamos automaticamente para `{modelo_usado}` e o atendimento foi concluído!")
                    else:
                        st.success(f"Transcrição e auditoria concluídas com sucesso usando `{modelo_usado}`!")
                    st.rerun()
                except Exception as e:
                    status_box.empty()
                    st.error(f"Erro durante o processamento: {e}")

# ---------------------------------------------------------
# Exibição dos Resultados & Auditoria
# ---------------------------------------------------------
if "resultado" in st.session_state:
    data = st.session_state["resultado"]
    st.divider()

    if st.session_state.get("aviso_recuperacao"):
        st.warning(f"⚠️ **Recuperação Parcial:** {st.session_state['aviso_recuperacao']}")
    
    if st.session_state.get("meta_aviso"):
        st.warning(f"⚠️ **Aviso no processamento:** {st.session_state['meta_aviso']}")

    # 1. Métricas Principais
    st.subheader("📋 Auditoria do Atendimento")
    
    # Linha 1 de Cards
    c1, c2, c3, c4 = st.columns(4)
    emp_fantasia = data.get("empresa_cliente_fantasia", "Não identificada")
    emp_razao = data.get("empresa_cliente_razao_social", "Não citada")
    c1.metric("🏢 Empresa do Cliente", emp_fantasia)
    if emp_razao and emp_razao != "Não citada":
        c1.caption(f"Razão Social: `{emp_razao}`")
    
    c2.metric("🎧 Atendente (Infobrasil)", data.get("atendente", "Não identificado"))
    c3.metric("👤 Cliente (Solicitante)", data.get("cliente", "Não identificado"))
    
    proto_princ = data.get("protocolo_principal", "Não citado")
    c4.metric("📄 Protocolo Principal", proto_princ)
    outros_p = data.get("outros_protocolos", [])
    if outros_p:
        c4.caption(f"Outros citados: `{', '.join(outros_p)}`")

    # Linha 2 de Cards
    s1, s2, s3, s4 = st.columns(4)
    
    # Status da Resolução
    status_res = data.get("status_resolucao", "Não especificado")
    cor_res = "🟢" if "Resolvido" in status_res else ("🟡" if "Pendente" in status_res or "andamento" in status_res else "🔵")
    s1.markdown(f"**Situação da Demanda:**<br>{cor_res} `{status_res}`", unsafe_allow_html=True)
    
    # Prazo Informado
    prazo = data.get("prazo_informado", "Não informado")
    s2.markdown(f"**Prazo / Previsão:**<br>⏱️ `{prazo}`", unsafe_allow_html=True)
    
    # Motivo do Contato
    s3.markdown(f"**Motivo Principal:**<br>🎯 {data.get('motivo_contato', 'Suporte técnico')}", unsafe_allow_html=True)
    
    # Sentimento Geral
    sentimento = data.get("sentimento_geral", "Neutro")
    cor_sent = "🟢" if "Positivo" in sentimento or "Satisfeito" in sentimento else ("🔴" if "Insatisfeito" in sentimento or "Reclamação" in sentimento else "🟡")
    s4.markdown(f"**Sentimento do Cliente:**<br>{cor_sent} {sentimento}", unsafe_allow_html=True)

    # Caixa de Resumo Executivo
    st.info(f"**📝 Resumo Executivo:**\n\n{data.get('resumo_atendimento', 'Sem resumo disponível.')}")

    # -----------------------------------------------------
    # Abas: Player com Legenda Ao Vivo VS Busca Textual
    # -----------------------------------------------------
    st.divider()
    
    tab_player, tab_busca = st.tabs([
        "🎧 Player com Legenda em Tempo Real (Sincronizado)",
        "🔍 Busca Textual Rápida (Estilo Ctrl + F)"
    ])
    
    # ABA 1: PLAYER COM LEGENDA AO VIVO
    with tab_player:
        st.markdown("Ouça a gravação com **legenda dinâmica no topo**. Conforme o áudio toca, a fala atual é realçada e a tela rola automaticamente. **Dica:** Clique em qualquer fala para pular o áudio para o segundo exato!")
        if data.get("recuperacao_parcial") or data.get("audio_recuperado"):
            dur_rec = data.get("duracao_recuperada")
            dur_msg = f" ({dur_rec:.1f}s decodificados)" if dur_rec else ""
            st.caption(f"ℹ️ **Áudio reproduzido:** cópia FLAC lossless recuperada{dur_msg}. Trechos adicionais corrompidos podem não ter sido recuperados.")
        
        audio_bytes = st.session_state.get("audio_bytes")
        mime_type = st.session_state.get("mime_type", "audio/ogg")
        dialogo = data.get("dialogo", [])
        
        if audio_bytes and dialogo:
            player_html = player_component.build_interactive_player_html(dialogo, audio_bytes, mime_type)
            components.html(player_html, height=650, scrolling=True)
        else:
            st.warning("Áudio ou diálogo não disponível para exibição sincronizada.")

    # ABA 2: BUSCA TEXTUAL (CTRL + F)
    with tab_busca:
        b_col1, b_col2 = st.columns([3, 1])
        with b_col1:
            termo_busca = st.text_input(
                "Digite uma palavra, protocolo ou termo para localizar:",
                placeholder="Ex: cancelamento, banco, protocolo, valor, reinicie...",
                value=""
            ).strip()
        with b_col2:
            filtro_papel = st.selectbox(
                "Filtrar por papel:",
                options=["Todos", "Atendente", "Cliente"],
                index=0
            )

        falas_filtradas = []
        total_ocorrencias = 0

        for item in dialogo:
            papel = item.get("papel", "")
            texto = item.get("texto", "")

            if filtro_papel != "Todos" and filtro_papel.lower() not in papel.lower():
                continue

            if termo_busca:
                padrao = re.compile(re.escape(termo_busca), re.IGNORECASE)
                matches = list(padrao.finditer(texto))
                if matches:
                    total_ocorrencias += len(matches)
                    falas_filtradas.append((item, True))
            else:
                falas_filtradas.append((item, False))

        if termo_busca:
            if total_ocorrencias > 0:
                st.success(f"Encontrada(s) **{total_ocorrencias} ocorrência(s)** do termo **'{termo_busca}'** em **{len(falas_filtradas)} fala(s)**:")
            else:
                st.warning(f"Nenhuma ocorrência encontrada para o termo **'{termo_busca}'**.")
        else:
            st.caption(f"Exibindo todas as {len(falas_filtradas)} falas registradas:")

        for item, destacado in falas_filtradas:
            locutor = item.get("locutor", "Interlocutor")
            tempo = item.get("timestamp", "00:00")
            texto = item.get("texto", "")
            papel = item.get("papel", "")

            if termo_busca and destacado:
                padrao = re.compile(f"({re.escape(termo_busca)})", re.IGNORECASE)
                texto_exibicao = padrao.sub(
                    r"<mark style='background-color: #ffd43b; color: #000; padding: 2px 4px; border-radius: 3px; font-weight: bold;'>\1</mark>",
                    texto
                )
            else:
                texto_exibicao = texto

            is_atendente = "Atendente" in papel or "Atendente" in locutor
            avatar = "🎧" if is_atendente else "👤"

            with st.chat_message(name="assistant" if is_atendente else "user", avatar=avatar):
                st.markdown(f"**`{tempo}` | {locutor}:**<br>{texto_exibicao}", unsafe_allow_html=True)

    # -----------------------------------------------------
    # Área de Exportação & Relatório em PDF
    # -----------------------------------------------------
    st.divider()
    st.subheader("💾 Exportação & Documentação Oficial")
    
    nome_audio = st.session_state.get('ultimo_arquivo', 'chamada')
    
    # 1. Gerar PDF oficial com ReportLab
    try:
        pdf_bytes = pdf_generator.gerar_relatorio_pdf(data, nome_audio)
    except Exception as e:
        pdf_bytes = None
        st.error(f"Erro ao compilar relatório PDF: {e}")

    # 2. Formatar texto plano
    linhas_txt = [
        f"=== INFOBRASIL SISTEMAS — AUDITORIA DE ATENDIMENTO: {nome_audio} ===",
    ]
    if data.get("aviso_recuperacao"):
        linhas_txt.append(f"⚠️ AVISO DE INTEGRIDADE: {data['aviso_recuperacao']}")

    linhas_txt.extend([
        f"Empresa do Cliente (Fantasia): {data.get('empresa_cliente_fantasia', 'Não identificada')}",
        f"Razão Social do Cliente: {data.get('empresa_cliente_razao_social', 'Não citada')}",
        f"Atendente (Infobrasil): {data.get('atendente', 'Não identificado')}",
        f"Cliente (Solicitante): {data.get('cliente', 'Não identificado')}",
        f"Protocolo Principal: {data.get('protocolo_principal', 'Não citado')}",
        f"Outros Protocolos: {', '.join(data.get('outros_protocolos', [])) or 'Nenhum'}",
        f"Status da Resolução: {data.get('status_resolucao', 'Não especificado')}",
        f"Prazo Informado: {data.get('prazo_informado', 'Não informado')}",
        f"Motivo do Contato: {data.get('motivo_contato', '')}",
        f"Sentimento Geral: {data.get('sentimento_geral', '')}",
        f"Resumo Executivo: {data.get('resumo_atendimento', '')}",
        "",
        "--- DIÁLOGO COMPLETO ---"
    ])
    for f in dialogo:
        linhas_txt.append(f"[{f.get('timestamp', '00:00')}] {f.get('locutor', 'Locutor')}: {f.get('texto', '')}")
    conteudo_txt = "\n".join(linhas_txt)

    exp_c1, exp_c2, exp_c3 = st.columns(3)
    
    with exp_c1:
        if pdf_bytes:
            st.download_button(
                label="📕 Baixar Relatório em PDF (.pdf)",
                data=pdf_bytes,
                file_name=f"relatorio_auditoria_{Path(nome_audio).stem}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
    with exp_c2:
        st.download_button(
            label="📄 Baixar Transcrição (.txt)",
            data=conteudo_txt,
            file_name=f"transcricao_{Path(nome_audio).stem}.txt",
            mime="text/plain",
            use_container_width=True
        )
    with exp_c3:
        dados_export_json = {k: v for k, v in data.items() if k != "audio_bytes"}
        st.download_button(
            label="📊 Baixar Dados Estruturados (.json)",
            data=json.dumps(dados_export_json, indent=2, ensure_ascii=False),
            file_name=f"auditoria_{Path(nome_audio).stem}.json",
            mime="application/json",
            use_container_width=True
        )
