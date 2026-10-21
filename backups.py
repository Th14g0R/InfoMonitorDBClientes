import os
import re
import ssl
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from zoneinfo import ZoneInfo


FUSO_LOCAL = ZoneInfo('America/Fortaleza')
LOGGER = logging.getLogger('backups')
EMAIL_DESTINATARIO_PADRAO = 'suporte@infobrasilsistemas.com.br'
ASSUNTO_ALERTA_PADRAO = 'Alerta de backup em atraso'


class TextoRelatorio(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes = []

    def handle_starttag(self, tag, attrs):
        if tag in {'br', 'p', 'div', 'pre', 'tr'}:
            self.partes.append('\n')

    def handle_endtag(self, tag):
        if tag in {'p', 'div', 'pre', 'tr'}:
            self.partes.append('\n')

    def handle_data(self, data):
        self.partes.append(data)


def interpretar_status_backups(texto):
    parser = TextoRelatorio()
    parser.feed(texto)
    texto = ''.join(parser.partes)
    resultado = dict(erro=None, clientes=[], total_ok=0, total_erro=0,
                     total_atrasado=0, ultima_atualizacao=None)
    padrao_cliente = re.compile(
        r'^O cliente (.+?) possui o arquivo criado (?:a|há) (.*?)\s*-\s*status\s*'
        r'\[\s*(OK|ERRO|ATRASADO)\s*\]\s*(.*?)\.?$', re.I)
    padrao_atrasado = re.compile(
        r'^O cliente (.+?) est[aá] (.*?) sem bkp\s*-\s*status\s*'
        r'(?:\[\s*)?(?:ERRO|ATRASADO)(?:\s*\])?\s*(.*?)\.?$', re.I)
    padrao_sem_arquivo = re.compile(r'^O caminho (.*?) N[ÃA]O possui arquivo bkp compactado', re.I)
    for linha in texto.splitlines():
        linha = linha.strip()
        cliente = padrao_cliente.match(linha)
        atrasado_sem_bkp = padrao_atrasado.match(linha)
        sem_arquivo = padrao_sem_arquivo.match(linha)
        if cliente:
            alias, idade, status, timestamp = cliente.groups()
            dias = re.search(r'\b(\d+)\s*dias?\b', idade, re.I)
            atrasado = (bool(dias and int(dias[1]) >= 1)
                        and not re.search(r'menos\s+de\s+1\s+dia', idade, re.I))
            resultado['clientes'].append(dict(
                alias=alias.strip(), status='ATRASADO' if atrasado else status.upper(),
                idade=idade.strip(), timestamp=timestamp.strip(), caminho=None))
        elif atrasado_sem_bkp:
            alias, idade, timestamp = atrasado_sem_bkp.groups()
            resultado['clientes'].append(dict(
                alias=alias.strip(), status='ATRASADO', idade=idade.strip(),
                timestamp=timestamp.strip(), caminho=None))
        elif sem_arquivo:
            caminho = sem_arquivo[1].strip()
            resultado['clientes'].append(dict(
                alias=caminho.rstrip('/').split('/')[-1], status='SEM_ARQUIVO',
                idade=None, timestamp=None, caminho=caminho))

    for cliente in resultado['clientes']:
        total = {'OK': 'total_ok', 'ATRASADO': 'total_atrasado'}.get(cliente['status'], 'total_erro')
        resultado[total] += 1
    atualizado = re.search(r'Hor[aá]rio de ultima atualiza[^\n]*\n\s*([^\n]+)', texto, re.I)
    if atualizado:
        resultado['ultima_atualizacao'] = atualizado[1].strip()
    return resultado


def extrair_dias_atraso(cliente, data_referencia=None):
    """
    Calcula a quantidade de dias em atraso para um determinado cliente.
    Clientes com status 'OK' possuem atraso 0.
    Retorna um inteiro >= 0 (ou 999 para SEM_ARQUIVO).
    """
    if data_referencia is None:
        data_referencia = datetime.now(FUSO_LOCAL).date()
    elif isinstance(data_referencia, datetime):
        data_referencia = data_referencia.date()

    status = (cliente.get('status') or '').upper()
    idade = cliente.get('idade') or ''
    ts = cliente.get('timestamp') or ''

    if status == 'OK':
        return 0

    if status == 'SEM_ARQUIVO':
        return 999

    match_data = re.search(r'(\d{4}-\d{2}-\d{2})', ts)
    if match_data:
        try:
            data_bkp = datetime.strptime(match_data.group(1), '%Y-%m-%d').date()
            diff = (data_referencia - data_bkp).days
            return max(1, diff)
        except ValueError:
            pass

    if ts.strip().lower() == 'hoje' or re.search(r'menos\s+de\s+1\s+dia', idade, re.I):
        return 0

    if ts.strip().lower() == 'ontem':
        return 1

    match_dias = re.search(r'\b(\d+)\s*dias?\b', idade, re.I)
    if match_dias:
        return max(1, int(match_dias.group(1)))

    match_horas = re.search(r'\b(\d+)\s*horas?\b', idade, re.I)
    if match_horas:
        return max(1, int(match_horas.group(1)) // 24)

    if status in ('ATRASADO', 'ERRO'):
        return 1

    return 0


def analisar_status_para_alerta(clientes, data_referencia=None, dias_corte=3):
    """
    Analisa os clientes do status de backups e os enriquece com dias de atraso e criticidade.
    Ordena:
      1. Clientes OK (ordem alfabética por alias)
      2. Clientes Atrasados (do maior atraso para o menor, desempate por alias)
    """
    if data_referencia is None:
        data_referencia = datetime.now(FUSO_LOCAL).date()
    elif isinstance(data_referencia, datetime):
        data_referencia = data_referencia.date()

    clientes_ok = []
    clientes_atrasados = []
    clientes_criticos = []

    for item in clientes:
        c = dict(item)
        dias = extrair_dias_atraso(c, data_referencia)
        c['dias_atraso'] = dias
        c['critico'] = bool(c['status'] != 'OK' and dias >= dias_corte)
        if c['status'] == 'OK':
            clientes_ok.append(c)
        else:
            clientes_atrasados.append(c)
            if c['critico']:
                clientes_criticos.append(c)

    clientes_ok.sort(key=lambda x: x['alias'].lower())
    clientes_atrasados.sort(key=lambda x: (-x['dias_atraso'], x['alias'].lower()))
    clientes_criticos.sort(key=lambda x: (-x['dias_atraso'], x['alias'].lower()))

    # Clientes atrasados primeiro (informação principal), seguidos pelos clientes em dia
    todos_ordenados = clientes_atrasados + clientes_ok

    return {
        'total_clientes': len(clientes),
        'total_ok': len(clientes_ok),
        'total_atrasado': len(clientes_atrasados),
        'total_critico': len(clientes_criticos),
        'tem_alerta': len(clientes_criticos) > 0,
        'clientes_ok': clientes_ok,
        'clientes_atrasados': clientes_atrasados,
        'clientes_criticos': clientes_criticos,
        'todos_ordenados': todos_ordenados,
        'data_referencia': data_referencia.isoformat()
    }


def gerar_relatorio_email_backups(dados_analise, ultima_atualizacao=None, data_envio=None):
    """
    Gera as versões HTML e texto simples do e-mail de alerta de backup em atraso.
    Os clientes atrasados são apresentados primeiro (com os mais atrasados grifados),
    seguidos pelos clientes com backup em dia.
    """
    if not data_envio:
        data_envio = datetime.now(FUSO_LOCAL).strftime('%d/%m/%Y %H:%M')

    total_clientes = dados_analise.get('total_clientes', 0)
    total_ok = dados_analise.get('total_ok', 0)
    total_atrasado = dados_analise.get('total_atrasado', 0)
    total_critico = dados_analise.get('total_critico', 0)
    clientes_ok = dados_analise.get('clientes_ok', [])
    clientes_atrasados = dados_analise.get('clientes_atrasados', [])
    clientes_criticos = dados_analise.get('clientes_criticos', [])

    linhas_txt = []
    linhas_txt.append("================================================================================")
    linhas_txt.append("                      ALERTA DE BACKUP EM ATRASO")
    linhas_txt.append("                     InfoMonitor - Infobrasil Sistemas")
    linhas_txt.append("================================================================================")
    linhas_txt.append(f"Data do Envio: {data_envio}")
    if ultima_atualizacao:
        linhas_txt.append(f"Última Atualização no Servidor: {ultima_atualizacao}")
    linhas_txt.append("")
    linhas_txt.append("RESUMO GERAL:")
    linhas_txt.append(f"  - Total de Clientes Monitorados: {total_clientes}")
    linhas_txt.append(f"  - Backups em Atraso: {total_atrasado}")
    linhas_txt.append(f"  - Clientes com Atraso Crítico (>= 3 dias): {total_critico}")
    linhas_txt.append(f"  - Backups em Dia (OK): {total_ok}")
    linhas_txt.append("")

    if total_critico > 0:
        linhas_txt.append("ATENÇÃO: Existem clientes com 3 ou mais dias de atraso na geração de backups!")
        linhas_txt.append("Estes clientes estão GRIFADOS e destacados abaixo:")
        for c in clientes_criticos:
            dias_str = f"{c['dias_atraso']} dias" if c['dias_atraso'] < 999 else "Sem arquivo"
            linhas_txt.append(f"  * [GRIFADO - CRÍTICO] {c['alias']} -> Atraso: {dias_str} | Último: {c.get('timestamp') or '—'}")
        linhas_txt.append("")

    linhas_txt.append("--------------------------------------------------------------------------------")
    linhas_txt.append(f"1. CLIENTES COM BACKUP EM ATRASO [{len(clientes_atrasados)}] - MAIS ATRASADOS GRIFADOS")
    linhas_txt.append("--------------------------------------------------------------------------------")
    if clientes_atrasados:
        for c in clientes_atrasados:
            dias_str = f"{c['dias_atraso']} dia(s)" if c['dias_atraso'] < 999 else "Sem arquivo"
            if c.get('critico'):
                tag = f"[*** GRIFADO - CRÍTICO: {dias_str} DE ATRASO ***]"
            else:
                tag = f"[ATRASADO: {dias_str}]"
            linhas_txt.append(f"  {tag} {c['alias']} | Último bkp: {c.get('timestamp') or '—'}")
    else:
        linhas_txt.append("  Nenhum cliente em atraso.")
    linhas_txt.append("")

    linhas_txt.append("--------------------------------------------------------------------------------")
    linhas_txt.append(f"2. CLIENTES COM BACKUP EM DIA (OK) [{len(clientes_ok)}]")
    linhas_txt.append("--------------------------------------------------------------------------------")
    if clientes_ok:
        for c in clientes_ok:
            linhas_txt.append(f"  [OK] {c['alias']} | Último bkp: {c.get('timestamp') or '—'}")
    else:
        linhas_txt.append("  Nenhum cliente em dia.")
    linhas_txt.append("")
    linhas_txt.append("Mensagem automática do InfoMonitor. Destinatário: suporte@infobrasilsistemas.com.br")
    linhas_txt.append("================================================================================")
    corpo_texto = "\n".join(linhas_txt)

    # Construção do HTML
    # Linhas de Clientes Atrasados
    linhas_html_atrasados = []
    for c in clientes_atrasados:
        ts = c.get('timestamp') or '—'
        dias = c.get('dias_atraso', 0)
        dias_str = f"{dias} dias" if dias < 999 else "Sem arquivo"

        if c.get('critico'):
            # Grifado com destaque visual intenso (amarelo com borda vermelha e badge crítico)
            # Sem ícone antes do alias para evitar quebras de linha indesejadas
            linhas_html_atrasados.append(f"""
            <tr style="background-color: #fef3c7; border-top: 1px solid #fde68a; border-bottom: 1px solid #f59e0b; border-left: 6px solid #dc2626;">
                <td style="padding: 10px 14px; white-space: nowrap; font-size: 13px;">
                    <mark style="background-color: #fde047; padding: 2px 8px; border-radius: 4px; color: #78350f; font-weight: 700; white-space: nowrap; display: inline-block;">{c['alias']}</mark>
                </td>
                <td style="padding: 10px 14px; white-space: nowrap;">
                    <span style="background-color: #fee2e2; color: #991b1b; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 800; border: 1px solid #fca5a5; white-space: nowrap; display: inline-block;">🚨 CRÍTICO (≥ 3 DIAS)</span>
                </td>
                <td style="padding: 10px 14px; color: #b91c1c; font-size: 13px; white-space: nowrap;">
                    <strong>{dias_str}</strong>
                </td>
                <td style="padding: 10px 14px; color: #78350f; font-size: 13px; white-space: nowrap;">{ts}</td>
            </tr>
            """)
        else:
            linhas_html_atrasados.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0; background-color: #ffffff;">
                <td style="padding: 10px 14px; font-weight: 600; color: #1e293b; white-space: nowrap; font-size: 13px;">{c['alias']}</td>
                <td style="padding: 10px 14px; white-space: nowrap;">
                    <span style="background-color: #fef3c7; color: #92400e; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 700; border: 1px solid #fde68a; white-space: nowrap; display: inline-block;">🕒 Atrasado</span>
                </td>
                <td style="padding: 10px 14px; color: #d97706; font-size: 13px; white-space: nowrap;">{dias_str}</td>
                <td style="padding: 10px 14px; color: #334155; font-size: 13px; white-space: nowrap;">{ts}</td>
            </tr>
            """)

    # Linhas de Clientes em Dia (OK)
    linhas_html_ok = []
    for c in clientes_ok:
        ts = c.get('timestamp') or '—'
        linhas_html_ok.append(f"""
        <tr style="border-bottom: 1px solid #e2e8f0; background-color: #ffffff;">
            <td style="padding: 10px 14px; font-weight: 600; color: #1e293b; white-space: nowrap; font-size: 13px;">{c['alias']}</td>
            <td style="padding: 10px 14px; white-space: nowrap;">
                <span style="background-color: #dcfce7; color: #15803d; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 700; border: 1px solid #bbf7d0; white-space: nowrap; display: inline-block;">✅ Em dia</span>
            </td>
            <td style="padding: 10px 14px; color: #64748b; font-size: 13px; white-space: nowrap;">0 dias</td>
            <td style="padding: 10px 14px; color: #334155; font-size: 13px; white-space: nowrap;">{ts}</td>
        </tr>
        """)

    alerta_banner = ""
    if total_critico > 0:
        alerta_banner = f"""
        <div style="background-color: #fef2f2; border: 1px solid #fecaca; border-left: 6px solid #dc2626; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px;">
            <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 24px;">🚨</span>
                <div>
                    <h3 style="margin: 0; color: #991b1b; font-size: 16px; font-weight: 700;">Atenção: {total_critico} cliente(s) com 3 ou mais dias de atraso no backup!</h3>
                    <p style="margin: 4px 0 0 0; color: #7f1d1d; font-size: 13px;">
                        Identificamos backups que não foram gerados há 3 dias ou mais. Os clientes mais atrasados estão <strong>grifados em amarelo com destaque</strong> na listagem abaixo.
                    </p>
                </div>
            </div>
        </div>
        """

    corpo_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Alerta de Backup em Atraso</title>
</head>
<body style="margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #1e293b;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width: 860px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        <!-- Cabeçalho -->
        <tr>
            <td style="background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); padding: 24px 30px; color: #ffffff;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                    <tr>
                        <td>
                            <h1 style="margin: 0; font-size: 22px; font-weight: 700; color: #ffffff;">💽 Alerta de Backup em Atraso</h1>
                            <p style="margin: 6px 0 0 0; font-size: 13px; color: #cbd5e1;">InfoMonitor DB Clientes • Infobrasil Sistemas</p>
                        </td>
                        <td align="right" style="font-size: 12px; color: #e2e8f0; white-space: nowrap;">
                            <div><strong>Envio:</strong> {data_envio}</div>
                            {f'<div><strong>Atualização:</strong> {ultima_atualizacao}</div>' if ultima_atualizacao else ''}
                        </td>
                    </tr>
                </table>
            </td>
        </tr>

        <!-- Conteúdo -->
        <tr>
            <td style="padding: 26px 30px;">
                {alerta_banner}

                <!-- Cards de Resumo -->
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-bottom: 24px;">
                    <tr>
                        <td width="24%" style="background-color: #f1f5f9; border-radius: 8px; padding: 14px 18px; border: 1px solid #e2e8f0;">
                            <div style="font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase;">Total de Clientes</div>
                            <div style="font-size: 24px; font-weight: 800; color: #1e293b; margin-top: 4px;">{total_clientes}</div>
                        </td>
                        <td width="3%"></td>
                        <td width="24%" style="background-color: #fffbeb; border-radius: 8px; padding: 14px 18px; border: 1px solid #fde68a;">
                            <div style="font-size: 11px; font-weight: 700; color: #92400e; text-transform: uppercase;">Atrasados (Geral)</div>
                            <div style="font-size: 24px; font-weight: 800; color: #d97706; margin-top: 4px;">{total_atrasado}</div>
                        </td>
                        <td width="3%"></td>
                        <td width="24%" style="background-color: #fef2f2; border-radius: 8px; padding: 14px 18px; border: 1px solid #fecaca;">
                            <div style="font-size: 11px; font-weight: 700; color: #991b1b; text-transform: uppercase;">Críticos (≥ 3 dias)</div>
                            <div style="font-size: 24px; font-weight: 800; color: #dc2626; margin-top: 4px;">{total_critico}</div>
                        </td>
                        <td width="3%"></td>
                        <td width="24%" style="background-color: #f0fdf4; border-radius: 8px; padding: 14px 18px; border: 1px solid #bbf7d0;">
                            <div style="font-size: 11px; font-weight: 700; color: #166534; text-transform: uppercase;">Backups em Dia (OK)</div>
                            <div style="font-size: 24px; font-weight: 800; color: #16a34a; margin-top: 4px;">{total_ok}</div>
                        </td>
                    </tr>
                </table>

                <!-- Seção 1: Backups em Atraso (com grifados) -->
                <div style="margin-bottom: 26px;">
                    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; border-bottom: 2px solid #ea580c; padding-bottom: 6px;">
                        <h3 style="margin: 0; font-size: 15px; color: #c2410c; font-weight: 700;">⚠️ Clientes com Backup em Atraso — {len(clientes_atrasados)} (Mais atrasados grifados)</h3>
                    </div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse: collapse; font-size: 13px; text-align: left; border: 1px solid #e2e8f0; border-radius: 6px; overflow: hidden; table-layout: auto;">
                        <thead>
                            <tr style="background-color: #f8fafc; border-bottom: 2px solid #cbd5e1; color: #475569; font-size: 12px; text-transform: uppercase;">
                                <th style="padding: 10px 14px; white-space: nowrap;">Cliente (Alias)</th>
                                <th style="padding: 10px 14px; white-space: nowrap;">Situação</th>
                                <th style="padding: 10px 14px; white-space: nowrap;">Dias de Atraso</th>
                                <th style="padding: 10px 14px; white-space: nowrap;">Último Backup</th>
                            </tr>
                        </thead>
                        <tbody>
                            {''.join(linhas_html_atrasados) if linhas_html_atrasados else '<tr><td colspan="4" style="padding: 14px; text-align: center; color: #94a3b8;">Nenhum cliente em atraso.</td></tr>'}
                        </tbody>
                    </table>
                </div>

                <!-- Seção 2: Backups em Dia (OK) -->
                <div style="margin-bottom: 16px;">
                    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; border-bottom: 2px solid #22c55e; padding-bottom: 6px;">
                        <h3 style="margin: 0; font-size: 15px; color: #15803d; font-weight: 700;">✅ Clientes com Backup em Dia (OK) — {len(clientes_ok)}</h3>
                    </div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse: collapse; font-size: 13px; text-align: left; border: 1px solid #e2e8f0; border-radius: 6px; overflow: hidden; table-layout: auto;">
                        <thead>
                            <tr style="background-color: #f8fafc; border-bottom: 2px solid #cbd5e1; color: #475569; font-size: 12px; text-transform: uppercase;">
                                <th style="padding: 10px 14px; white-space: nowrap;">Cliente (Alias)</th>
                                <th style="padding: 10px 14px; white-space: nowrap;">Situação</th>
                                <th style="padding: 10px 14px; white-space: nowrap;">Dias de Atraso</th>
                                <th style="padding: 10px 14px; white-space: nowrap;">Último Backup</th>
                            </tr>
                        </thead>
                        <tbody>
                            {''.join(linhas_html_ok) if linhas_html_ok else '<tr><td colspan="4" style="padding: 14px; text-align: center; color: #94a3b8;">Nenhum cliente em dia.</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </td>
        </tr>

        <!-- Rodapé -->
        <tr>
            <td style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 18px 30px; font-size: 12px; color: #64748b; text-align: center;">
                Este relatório foi gerado automaticamente pelo <strong>InfoMonitor</strong> e enviado para <code>suporte@infobrasilsistemas.com.br</code>.<br>
                Configuração: disparo diário às 09:00 quando houver clientes com 3 ou mais dias de atraso (máximo 1 e-mail por dia).
            </td>
        </tr>
    </table>
</body>
</html>"""

    return corpo_html, corpo_texto


def enviar_email_alerta_backups(
    destinatario=EMAIL_DESTINATARIO_PADRAO,
    assunto=ASSUNTO_ALERTA_PADRAO,
    corpo_html=None,
    corpo_texto=None,
    servidor_smtp=None,
    porta_smtp=None,
    usuario_smtp=None,
    senha_smtp=None
):
    """
    Envia o e-mail de alerta de backups em atraso via SMTP configurado no projeto.
    Suporta servidores SMTP convencionais com STARTTLS (ex: porta 587) e SMTP_SSL (porta 465).
    """
    usuario = usuario_smtp or os.getenv('SMTP_USER')
    senha = senha_smtp or os.getenv('SMTP_PASS')

    if not (usuario and senha):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except (ImportError, OSError):
            pass
        usuario = usuario or os.getenv('SMTP_USER')
        senha = senha or os.getenv('SMTP_PASS')

    servidor = servidor_smtp or os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    porta = int(porta_smtp or os.getenv('SMTP_PORT', 587))

    if not usuario or not senha:
        raise RuntimeError('Configuração de SMTP incompleta: SMTP_USER e SMTP_PASS devem estar definidos no ambiente.')

    msg = MIMEMultipart('alternative')
    msg['From'] = usuario
    msg['To'] = destinatario
    msg['Subject'] = assunto

    if corpo_texto:
        msg.attach(MIMEText(corpo_texto, 'plain', 'utf-8'))
    if corpo_html:
        msg.attach(MIMEText(corpo_html, 'html', 'utf-8'))

    contexto = ssl.create_default_context()
    if porta == 465:
        server = smtplib.SMTP_SSL(servidor, porta, context=contexto, timeout=20)
    else:
        server = smtplib.SMTP(servidor, porta, timeout=20)
        try:
            server.starttls(context=contexto)
        except (smtplib.SMTPException, ssl.SSLError) as tls_err:
            LOGGER.warning('Falha ao iniciar STARTTLS no envio de e-mail de backup: %s', tls_err)

    try:
        server.login(usuario, senha)
        server.sendmail(usuario, [destinatario], msg.as_string())
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass

    return True, "E-mail de alerta enviado com sucesso."

