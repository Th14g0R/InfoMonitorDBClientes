import os
import re
import json
import tempfile
from pathlib import Path
from typing import List, Optional

import streamlit as st
import streamlit.components.v1 as components
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import pdf_generator
import player_component

# Tenta carregar variáveis do .env na pasta do script ou na pasta raiz
_current_dir = Path(__file__).resolve().parent
load_dotenv(_current_dir / ".env")
load_dotenv(_current_dir.parent / ".env")

try:
    from google import genai
    from google.genai import types
except ImportError:
    st.error("Biblioteca 'google-genai' não encontrada. Instale com: pip install google-genai")
    st.stop()

# ---------------------------------------------------------
# Estruturas de Dados Pydantic
# ---------------------------------------------------------
class FalaItem(BaseModel):
    timestamp: str = Field(description="Momento aproximado da fala no formato MM:SS (ex: '00:08')")
    locutor: str = Field(description="Nome e papel do interlocutor (ex: 'Atendente (Carlos)' ou 'Cliente (Juliana)')")
    papel: str = Field(description="Papel do participante: 'Atendente', 'Cliente' ou 'Outro'")
    texto: str = Field(description="Transcrição literal e fidedigna do que foi falado")

class MetadadosChamada(BaseModel):
    empresa_cliente_fantasia: str = Field(
        default="Não identificada",
        description="Nome fantasia ou marca da empresa do CLIENTE (ex: 'Supermercado Central', 'Farmácia Vida'). ATENÇÃO: NÃO colocar Infobrasil, pois Infobrasil é a nossa empresa que atende."
    )
    empresa_cliente_razao_social: Optional[str] = Field(
        default="Não citada",
        description="Razão social da empresa do CLIENTE se citada (ex: 'Comercial de Alimentos Silva Ltda'), senão 'Não citada'."
    )
    atendente: str = Field(
        default="Não identificado",
        description="Nome do atendente/técnico da Infobrasil que atendeu o chamado."
    )
    cliente: str = Field(
        default="Não identificado",
        description="Nome do cliente/usuário solicitante que conversou com o atendente."
    )
    protocolo_principal: Optional[str] = Field(
        default="Não citado",
        description="Número do protocolo principal gerado ou citado nesta chamada."
    )
    outros_protocolos: List[str] = Field(
        default_factory=list,
        description="Outros números de protocolos, tickets ou chamados anteriores citados na conversa (ex: ['88412', '91002'])."
    )
    status_resolucao: str = Field(
        default="Não especificado",
        description="Situação do chamado: 'Resolvido no atendimento', 'Pendente / Aguardando retorno', 'Em andamento' ou 'Encaminhado para outro setor/nível'."
    )
    prazo_informado: Optional[str] = Field(
        default="Não informado",
        description="Prazo ou previsão que o atendente acordou com o cliente (ex: 'Em até 2 horas', 'Até o fim da tarde', 'Próximo dia útil' ou 'Não informado')."
    )
    motivo_contato: str = Field(
        default="Suporte técnico",
        description="Motivo central do chamado (ex: 'Banco de dados travado', 'Erro na emissão de NF-e', 'Lentidão no PDV')."
    )
    resumo_atendimento: str = Field(
        default="",
        description="Resumo sucinto em 2 ou 3 frases sobre o atendimento e o desfecho."
    )
    sentimento_geral: str = Field(
        default="Neutro",
        description="Sentimento geral do cliente: 'Positivo / Satisfeito', 'Neutro' ou 'Insatisfeito / Reclamação'."
    )

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
    st.caption("Cada atendente deve digitar sua própria chave API para transcrever.")
    
    chave_salva = st.session_state.get("api_key", "").strip()
    api_key = st.text_input(
        "Chave API do Gemini:",
        value=chave_salva,
        type="password",
        placeholder="Cole sua chave AIzaSy... aqui",
        help="Chave de API pessoal do Google Gemini. Obtenha gratuitamente em: https://aistudio.google.com/app/apikey"
    )
    if api_key:
        st.session_state["api_key"] = api_key.strip()
        st.success("✅ Chave API pronta para esta sessão.")
    else:
        st.warning("⚠️ Chave não informada. Digite sua chave acima para liberar a transcrição.")
    
    st.markdown("---")
    
    modelo_selecionado = st.selectbox(
        "Modelo de IA:",
        options=["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-flash-lite-latest"],
        index=0,
        help="gemini-3.5-flash-lite é ultra-rápido, estável e ideal para áudios longos sem fila."
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
    st.markdown("[🖥️ **Acessar Monitor de Bancos (InfoMonitor)**](http://localhost:5000)")
    
    if "resultado" in st.session_state:
        if st.button("🗑️ Limpar Transcrição Atual", use_container_width=True):
            del st.session_state["resultado"]
            if "audio_bytes" in st.session_state:
                del st.session_state["audio_bytes"]
            st.rerun()

# ---------------------------------------------------------
# Cabeçalho Principal
# ---------------------------------------------------------
st.title("🎧 Infobrasil Sistemas — Auditoria & Transcrição de Chamadas")
st.markdown("Identificação inteligente de interlocutores, empresa do cliente, prazos e **reprodução com legenda sincronizada em tempo real**.")

# ---------------------------------------------------------
# Upload de Áudio
# ---------------------------------------------------------
col_upload, col_info = st.columns([1.4, 1])

with col_upload:
    uploaded_file = st.file_uploader(
        "Selecione o arquivo de gravação de ligação:",
        type=["mp3", "ogg", "wav", "m4a", "aac"],
        help="Formatos aceitos: .ogg, .mp3, .wav, .m4a. Suporta gravações telefônicas PBX de qualquer duração."
    )

with col_info:
    if uploaded_file is not None:
        tam_mb = uploaded_file.size / (1024 * 1024)
        st.markdown("**Resumo do Arquivo:**")
        st.info(f"📁 **Nome:** `{uploaded_file.name}`\n\n⚖️ **Tamanho:** `{tam_mb:.2f} MB`\n\n✨ Pronto para envio via Google File API segura.")

# ---------------------------------------------------------
# Função de Processamento com a API Gemini
# ---------------------------------------------------------
def processar_audio(arquivo, chave_api: str, modelo_desejado: str, status_placeholder=None) -> tuple:
    import time
    client = genai.Client(api_key=chave_api)
    
    ext = Path(arquivo.name).suffix.lower() or ".mp3"
    
    arquivo.seek(0)
    audio_bytes = arquivo.read()
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
        tmp_file.write(audio_bytes)
        tmp_path = tmp_file.name

    gemini_file = None
    try:
        if status_placeholder:
            status_placeholder.info("📤 Carregando áudio no serviço seguro do Google File API...")
        
        gemini_file = client.files.upload(file=tmp_path)
        
        # Aguarda o arquivo ficar ativo
        for _ in range(30):
            if hasattr(gemini_file, "state"):
                state_name = getattr(gemini_file.state, "name", str(gemini_file.state))
                if state_name == "ACTIVE":
                    break
                elif state_name in ("FAILED", "ERROR"):
                    raise RuntimeError(f"O processamento do áudio falhou no Gemini (estado: {state_name}).")
            time.sleep(1)
            gemini_file = client.files.get(name=gemini_file.name)
        
        # Etapa 1: Transcrição Direta com Regras Estritas de Diferenciação de Vozes
        prompt_transcricao = (
            "Você é um auditor sênior de qualidade e transcrição de call center de TI e telecomunicações.\n"
            "Esta gravação é uma chamada de suporte técnico da empresa 'Infobrasil Sistemas' (ou apenas 'Infobrasil').\n\n"
            "REGRAS CRÍTICAS DE IDENTIFICAÇÃO DOS PARTICIPANTES:\n"
            "1. A INFOBRASIL SOMOS NÓS (a empresa que presta o atendimento de suporte). O 'Atendente' é SEMPRE quem atende pela Infobrasil.\n"
            "2. O 'Cliente' é a pessoa externa que liga ou recebe o contato solicitando suporte para a sua respectiva empresa (loja, farmácia, supermercado, etc.).\n"
            "3. DIFERENCIAÇÃO CLARA DE VOZES: Use a voz e o contexto de quem com certeza é o cliente (quem relata o problema/dúvida) para diferenciar com precisão a voz do atendente (quem atende com saudação institucional e busca resolver o chamado).\n"
            "4. PADRÃO ESTRITO DE ROTULAÇÃO (NÃO MISTURE NOMES):\n"
            "   - Se souber o nome do atendente: 'Atendente (Nome)'\n"
            "   - Se souber o nome do cliente: 'Cliente (Nome)'\n"
            "   - NUNCA coloque dois nomes juntos ou entre parênteses como '(NomeAtendente / NomeCliente)'.\n"
            "   - Se houver qualquer dúvida sobre o nome, rotule estritamente como apenas 'Atendente' ou 'Cliente', sem inventar parênteses.\n"
            "5. FORMATO OBRIGATÓRIO POR LINHA:\n"
            "   Cada fala DEVE começar em uma linha separada no formato:\n"
            "   [MM:SS] Atendente (Nome): Fala dita\n"
            "   [MM:SS] Cliente (Nome): Fala dita\n"
            "   NUNCA junte falas de interlocutores diferentes no mesmo parágrafo. Se o interlocutor mudar, crie uma nova linha com o novo timestamp.\n"
            "6. Transcreva com fidelidade absoluta todo o diálogo até o final da ligação."
        )
        
        candidatos = [modelo_desejado]
        for alt in ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.8-flash"]:
            if alt not in candidatos:
                candidatos.append(alt)
        
        texto_transcrito = ""
        modelo_utilizado = modelo_desejado
        
        for idx, mod in enumerate(candidatos):
            try:
                if status_placeholder:
                    status_placeholder.info(f"🎧 Transcrevendo áudio com **{mod}**... (gravações de ~35 min levam cerca de 25 a 45s)")
                
                resp_trans = client.models.generate_content(
                    model=mod,
                    contents=[gemini_file, prompt_transcricao]
                )
                texto_transcrito = resp_trans.text.strip()
                modelo_utilizado = mod
                break
            except Exception as e:
                err_str = str(e)
                if any(code in err_str for code in ["503", "UNAVAILABLE", "high demand"]):
                    if idx < len(candidatos) - 1:
                        time.sleep(2)
                        continue
                raise e
        
        if not texto_transcrito:
            raise RuntimeError("Não foi possível gerar a transcrição do áudio.")
        
        # Etapa 2: Parsing das falas em lista estruturada
        padrao = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:]+):\s*(.*)")
        dialogo = []
        for linha in texto_transcrito.split("\n"):
            linha_limpa = linha.strip()
            if not linha_limpa:
                continue
            m = padrao.match(linha_limpa)
            if m:
                tempo, locutor, fala = m.groups()
                loc_clean = locutor.strip()
                papel = "Atendente" if "atendente" in loc_clean.lower() else ("Cliente" if "cliente" in loc_clean.lower() else "Outro")
                dialogo.append({
                    "timestamp": tempo,
                    "locutor": loc_clean,
                    "papel": papel,
                    "texto": fala.strip()
                })
            else:
                if dialogo:
                    dialogo[-1]["texto"] += " " + linha_limpa
                else:
                    dialogo.append({
                        "timestamp": "00:00",
                        "locutor": "Áudio",
                        "papel": "Outro",
                        "texto": linha_limpa
                    })
        
        # Etapa 3: Extração de Metadados com foco na Empresa do Cliente e Status de Resolução
        if status_placeholder:
            status_placeholder.info("⚡ Identificando dados do cliente, empresa, protocolos, prazos e desfecho...")
            
        prompt_meta = (
            "Você é um analista de qualidade da INFOBRASIL SISTEMAS.\n"
            "Analise a transcrição deste atendimento e extraia com precisão os seguintes dados em JSON:\n\n"
            "1. empresa_cliente_fantasia: Nome fantasia ou marca da empresa do CLIENTE (atenção: NÃO coloque Infobrasil, pois Infobrasil é a nossa empresa de suporte). Ex: 'Supermercado Central', 'Drogaria Silva', 'Padaria Central'. Se não citada, coloque 'Não identificada'.\n"
            "2. empresa_cliente_razao_social: Razão social da empresa do CLIENTE se mencionada (ex: 'Comercial de Alimentos Ltda'). Se não citada, coloque 'Não citada'.\n"
            "3. atendente: Nome do atendente/técnico da Infobrasil que prestou o suporte.\n"
            "4. cliente: Nome da pessoa (cliente) que solicitou o atendimento.\n"
            "5. protocolo_principal: Número de protocolo gerado ou principal citado neste atendimento (ou 'Não citado').\n"
            "6. outros_protocolos: Lista com quaisquer outros números de chamados, tickets ou protocolos anteriores citados durante a conversa (ex: ['88412']). Se nenhum outro for citado, lista vazia.\n"
            "7. status_resolucao: 'Resolvido no atendimento', 'Pendente / Aguardando retorno', 'Em andamento' ou 'Encaminhado para outro setor/nível'.\n"
            "8. prazo_informado: Prazo ou previsão que o atendente prometeu/acordou com o cliente (ex: 'Em até 2 horas', 'Até o fim da tarde', 'Próximo dia útil' ou 'Não informado').\n"
            "9. motivo_contato: Descrição concisa do problema/dúvida central (ex: 'Banco de dados travado', 'Erro na emissão de NF-e', 'Lentidão no PDV').\n"
            "10. resumo_atendimento: Resumo executivo em 2 ou 3 frases sobre o atendimento e o desfecho.\n"
            "11. sentimento_geral: Classificação do sentimento do cliente: 'Positivo / Satisfeito', 'Neutro' ou 'Insatisfeito / Reclamação'.\n\n"
            f"Transcrição da chamada:\n{texto_transcrito[:16000]}"
        )
        
        try:
            resp_meta = client.models.generate_content(
                model=modelo_utilizado,
                contents=[prompt_meta],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=MetadadosChamada,
                    temperature=0.1
                )
            )
            meta_dict = json.loads(resp_meta.text)
        except Exception:
            meta_dict = {
                "empresa_cliente_fantasia": "Não identificada",
                "empresa_cliente_razao_social": "Não citada",
                "atendente": "Identificado no diálogo",
                "cliente": "Identificado no diálogo",
                "protocolo_principal": "Não citado",
                "outros_protocolos": [],
                "status_resolucao": "Não especificado",
                "prazo_informado": "Não informado",
                "motivo_contato": "Atendimento telefônico",
                "resumo_atendimento": "Transcrição realizada com sucesso.",
                "sentimento_geral": "Neutro"
            }
            
        resultado_final = {
            "empresa_cliente_fantasia": meta_dict.get("empresa_cliente_fantasia") or "Não identificada",
            "empresa_cliente_razao_social": meta_dict.get("empresa_cliente_razao_social") or "Não citada",
            "atendente": meta_dict.get("atendente") or "Não identificado",
            "cliente": meta_dict.get("cliente") or "Não identificado",
            "protocolo_principal": meta_dict.get("protocolo_principal") or "Não citado",
            "outros_protocolos": meta_dict.get("outros_protocolos") or [],
            "status_resolucao": meta_dict.get("status_resolucao") or "Não especificado",
            "prazo_informado": meta_dict.get("prazo_informado") or "Não informado",
            "motivo_contato": meta_dict.get("motivo_contato") or "Não especificado",
            "resumo_atendimento": meta_dict.get("resumo_atendimento") or "Sem resumo disponível.",
            "sentimento_geral": meta_dict.get("sentimento_geral") or "Neutro",
            "dialogo": dialogo,
            "texto_bruto": texto_transcrito
        }
        
        return resultado_final, modelo_utilizado
        
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        if gemini_file and hasattr(gemini_file, "name"):
            try:
                client.files.delete(name=gemini_file.name)
            except Exception:
                pass

# Botão de Execução
if uploaded_file is not None:
    chave_atual = (st.session_state.get("api_key") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not chave_atual:
        st.warning("🔑 **Atenção Atendente:** Para iniciar a transcrição, digite ou cole sua **Chave API do Gemini** no menu lateral à esquerda.")
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
                    resultado, modelo_usado = processar_audio(uploaded_file, chave_atual, modelo_selecionado, status_box)
                    st.session_state["resultado"] = resultado
                    st.session_state["ultimo_arquivo"] = uploaded_file.name
                    st.session_state["audio_bytes"] = uploaded_file.getvalue()
                    st.session_state["mime_type"] = (
                        "audio/ogg" if uploaded_file.name.endswith(".ogg")
                        else ("audio/mp3" if uploaded_file.name.endswith(".mp3") else "audio/wav")
                    )
                    st.session_state["modelo_usado"] = modelo_usado
                    status_box.empty()
                    
                    if modelo_usado != modelo_selecionado:
                        st.warning(f"O modelo `{modelo_selecionado}` estava com alta demanda momentânea (503). Alternamos automaticamente para `{modelo_usado}` e o atendimento foi concluído!")
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
    ]
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
        st.download_button(
            label="📊 Baixar Dados Estruturados (.json)",
            data=json.dumps(data, indent=2, ensure_ascii=False),
            file_name=f"auditoria_{Path(nome_audio).stem}.json",
            mime="application/json",
            use_container_width=True
        )
