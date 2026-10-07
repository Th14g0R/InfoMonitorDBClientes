import io
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    """Adiciona paginação automática 'Página X de Y' no rodapé do documento."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#718096"))
        page_text = f"Página {self._pageNumber} de {page_count}"
        self.drawRightString(A4[0] - 36, 20, page_text)
        self.drawString(36, 20, "Infobrasil Sistemas — Documento de Auditoria e Qualidade de Atendimento")
        self.restoreState()


def gerar_relatorio_pdf(dados: dict, nome_arquivo_audio: str = "gravação") -> bytes:
    """Gera um PDF formatado profissionalmente para impressão e documentação de ligações."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=40
    )

    styles = getSampleStyleSheet()

    # Estilos customizados
    titulo_style = ParagraphStyle(
        'DocTitulo',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#1A365D"),
        spaceAfter=2
    )

    subtitulo_style = ParagraphStyle(
        'DocSubTitulo',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#4A5568"),
        spaceAfter=8
    )

    secao_style = ParagraphStyle(
        'DocSecao',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#2B6CB0"),
        spaceBefore=8,
        spaceAfter=4
    )

    meta_label = ParagraphStyle(
        'MetaLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#2D3748")
    )

    meta_val = ParagraphStyle(
        'MetaVal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#1A202C")
    )

    corpo_resumo = ParagraphStyle(
        'CorpoResumo',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#1A202C")
    )

    fala_tempo = ParagraphStyle(
        'FalaTempo',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#4A5568")
    )

    fala_atendente = ParagraphStyle(
        'FalaAtendente',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#2B6CB0")
    )

    fala_cliente = ParagraphStyle(
        'FalaCliente',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#C53030")
    )

    fala_outro = ParagraphStyle(
        'FalaOutro',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#4A5568")
    )

    fala_texto = ParagraphStyle(
        'FalaTexto',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#2D3748")
    )

    story = []

    # 1. Cabeçalho Principal
    story.append(Paragraph("INFOBRASIL SISTEMAS — AUDITORIA DE ATENDIMENTO", titulo_style))
    agora_str = datetime.now().strftime("%d/%m/%Y às %H:%M")
    story.append(Paragraph(f"Arquivo: <b>{nome_arquivo_audio}</b> | Emitido em: {agora_str}", subtitulo_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#2B6CB0"), spaceAfter=8))

    # 2. Tabela de Metadados Estruturados
    story.append(Paragraph("DADOS PRINCIPAIS DO ATENDIMENTO", secao_style))

    empresa_fantasia = dados.get("empresa_cliente_fantasia") or dados.get("empresa_atendimento") or "Não identificada"
    empresa_razao = dados.get("empresa_cliente_razao_social") or "Não citada"
    atendente = dados.get("atendente", "Não identificado")
    cliente = dados.get("cliente", "Não identificado")
    protocolo_principal = dados.get("protocolo_principal") or dados.get("protocolo") or "Não citado"
    outros_protos = dados.get("outros_protocolos", [])
    outros_protos_str = ", ".join(outros_protos) if outros_protos else "Nenhum"
    status_resolucao = dados.get("status_resolucao", "Não especificado")
    prazo = dados.get("prazo_informado") or "Não informado"
    motivo = dados.get("motivo_contato", "Suporte geral")
    sentimento = dados.get("sentimento_geral", "Neutro")

    tabela_meta_data = [
        [
            Paragraph("Empresa do Cliente (Fantasia):", meta_label),
            Paragraph(f"<b>{empresa_fantasia}</b>", meta_val),
            Paragraph("Protocolo Principal:", meta_label),
            Paragraph(f"<b>{protocolo_principal}</b>", meta_val),
        ],
        [
            Paragraph("Razão Social do Cliente:", meta_label),
            Paragraph(empresa_razao, meta_val),
            Paragraph("Outros Protocolos Citados:", meta_label),
            Paragraph(outros_protos_str, meta_val),
        ],
        [
            Paragraph("Cliente (Solicitante):", meta_label),
            Paragraph(cliente, meta_val),
            Paragraph("Status da Resolução:", meta_label),
            Paragraph(f"<b>{status_resolucao}</b>", meta_val),
        ],
        [
            Paragraph("Atendente (Infobrasil):", meta_label),
            Paragraph(atendente, meta_val),
            Paragraph("Prazo Informado:", meta_label),
            Paragraph(prazo, meta_val),
        ],
        [
            Paragraph("Motivo do Contato:", meta_label),
            Paragraph(motivo, meta_val),
            Paragraph("Sentimento do Cliente:", meta_label),
            Paragraph(sentimento, meta_val),
        ],
    ]

    tabela_meta = Table(tabela_meta_data, colWidths=[130, 135, 125, 130])
    tabela_meta.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F7FAFC")),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor("#CBD5E0")),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(tabela_meta)
    story.append(Spacer(1, 8))

    # 3. Resumo Executivo
    resumo_texto = dados.get("resumo_atendimento") or "Sem resumo disponível."
    tabela_resumo = Table([
        [Paragraph("<b>RESUMO EXECUTIVO DO ATENDIMENTO:</b>", meta_label)],
        [Paragraph(resumo_texto, corpo_resumo)]
    ], colWidths=[520])
    tabela_resumo.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#EDF2F7")),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor("#CBD5E0")),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(tabela_resumo)
    story.append(Spacer(1, 10))

    # 4. Diálogo Completo com Timestamps
    story.append(Paragraph("TRANSCRIÇÃO CRONOLÓGICA DAS FALAS", secao_style))

    dialogo = dados.get("dialogo", [])
    if not dialogo:
        story.append(Paragraph("Nenhuma fala registrada na transcrição.", corpo_resumo))
    else:
        tabela_dialogo_data = [
            [
                Paragraph("<b>Tempo</b>", meta_label),
                Paragraph("<b>Interlocutor</b>", meta_label),
                Paragraph("<b>Transcrição Literal da Fala</b>", meta_label)
            ]
        ]

        for i, item in enumerate(dialogo):
            tempo = item.get("timestamp", "00:00")
            locutor = item.get("locutor", "Interlocutor")
            papel = item.get("papel", "")
            texto = item.get("texto", "")

            # Formatação do papel/locutor
            if "Atendente" in papel or "Atendente" in locutor:
                estilo_locutor = fala_atendente
            elif "Cliente" in papel or "Cliente" in locutor:
                estilo_locutor = fala_cliente
            else:
                estilo_locutor = fala_outro

            tabela_dialogo_data.append([
                Paragraph(tempo, fala_tempo),
                Paragraph(locutor, estilo_locutor),
                Paragraph(texto, fala_texto)
            ])

        tabela_dialogo = Table(tabela_dialogo_data, colWidths=[45, 120, 355], repeatRows=1)
        
        # Alternância de cores nas linhas
        table_styles = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
            ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor("#CBD5E0")),
            ('INNERGRID', (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]
        for row_idx in range(1, len(tabela_dialogo_data)):
            if row_idx % 2 == 0:
                table_styles.append(('BACKGROUND', (0, row_idx), (-1, row_idx), colors.HexColor("#F7FAFC")))

        tabela_dialogo.setStyle(TableStyle(table_styles))
        story.append(tabela_dialogo)

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()
