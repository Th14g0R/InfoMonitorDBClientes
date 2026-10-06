"""Monitoramento TLS com consulta pública e alterações autenticadas."""
import ipaddress
import logging
import math
import os
import re
import smtplib
import socket
import ssl
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from cryptography import x509
from flask import flash, redirect, render_template, request, url_for

LOGGER = logging.getLogger(__name__)
ALERTA_DIAS = 35
DOMINIOS = (
    'info-api', 'api-tray', 'api-oto', 'api-anotai',
    'infoapiretaguarda', 'infocob', 'api-scanntech', 'guia',
)
SUFIXO = '.infobrasilsistemas.com.br'


def agora():
    return datetime.now(timezone.utc)


def validar_dominio(valor):
    dominio = str(valor or '').strip().lower().rstrip('.')
    if len(dominio) > 253 or not dominio.endswith(SUFIXO):
        raise ValueError('Informe um subdomínio de infobrasilsistemas.com.br, sem protocolo, porta ou caminho.')
    if not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', parte) for parte in dominio.split('.')):
        raise ValueError('Subdomínio inválido.')
    return dominio


def consultar_certificado(dominio):
    dominio = validar_dominio(dominio)
    # Conecta ao IP já validado, impedindo DNS rebinding e acesso à rede privada.
    enderecos = socket.getaddrinfo(dominio, 443, type=socket.SOCK_STREAM)
    if not enderecos or any(not ipaddress.ip_address(item[4][0]).is_global for item in enderecos):
        raise ValueError('Destino não permitido.')
    familia, tipo, protocolo, _, destino = enderecos[0]

    def conectar(contexto):
        with socket.socket(familia, tipo, protocolo) as sock:
            sock.settimeout(5)
            sock.connect(destino)
            with contexto.wrap_socket(sock, server_hostname=dominio) as tls:
                return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))

    confiavel = True
    try:
        cert = conectar(ssl.create_default_context())
    except ssl.SSLCertVerificationError:
        # Somente inspeção do certificado: não transmite credenciais ou dados.
        contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
        cert = conectar(contexto)
        confiavel = False
    expira = cert.not_valid_after_utc
    dias = math.ceil((expira - agora()).total_seconds() / 86400)
    status = 'Expirado' if expira <= agora() else 'TLS inválido' if not confiavel else 'Vencendo' if dias <= ALERTA_DIAS else 'Válido'
    return expira.isoformat(), status


def enviar_email(itens, *, teste=False):
    usuario, senha = os.getenv('SMTP_USER'), os.getenv('SMTP_PASS')
    if not usuario or not senha:
        raise RuntimeError('SMTP não configurado.')
    mensagem = EmailMessage()
    mensagem['From'] = usuario
    mensagem['To'] = 'suporte@infobrasilsistemas.com.br' if teste else 'atendimento@nossatelecom.com.br'
    mensagem['Subject'] = 'Certificado vencendo'
    linhas = [f"- {item['dominio']}: {formatar_data(item['expira_em'])}" for item in itens]
    mensagem.set_content(('TESTE — Datas fictícias para demonstração. Não é uma solicitação real de renovação.\n\n' if teste else '') + 'Prezados,\n\nOs certificados abaixo estão vencendo ou já expiraram:\n\n' + '\n'.join(linhas) +
        '\n\nSolicitamos a renovação para evitar falhas nos sites por motivo de certificado expirado.\n\nSuporte Infobrasil\nMensagem automática.')
    servidor = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    porta = int(os.getenv('SMTP_PORT', '587'))
    contexto = ssl.create_default_context()
    classe = smtplib.SMTP_SSL if porta == 465 else smtplib.SMTP
    kwargs = {'context': contexto} if porta == 465 else {}
    with classe(servidor, porta, timeout=20, **kwargs) as smtp:
        if porta != 465:
            smtp.starttls(context=contexto)
        smtp.login(usuario, senha)
        if smtp.send_message(mensagem):
            raise RuntimeError('Entrega recusada.')


def formatar_data(valor):
    return datetime.fromisoformat(valor).strftime('%d/%m/%Y %H:%M UTC') if valor else '—'


class MonitorCertificados:
    def __init__(self, conectar):
        self.conectar = conectar
        with closing(conectar()) as conn, conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS certificados (
                    dominio TEXT PRIMARY KEY, expira_em TEXT, status TEXT NOT NULL DEFAULT 'Aguardando',
                    verificado_em TEXT, email_em TEXT, email_expiracao TEXT, email_status TEXT
                );
                CREATE TABLE IF NOT EXISTS certificados_rotina (
                    id INTEGER PRIMARY KEY CHECK(id=1), mes TEXT, bloqueio_ate TEXT
                );
                INSERT OR IGNORE INTO certificados_rotina(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS certificados_emails (
                    id INTEGER PRIMARY KEY, criado_em TEXT NOT NULL, dominios TEXT NOT NULL,
                    resultado TEXT NOT NULL
                );
            ''')
            conn.executemany('INSERT OR IGNORE INTO certificados(dominio) VALUES (?)', [(d + SUFIXO,) for d in DOMINIOS])

    def verificar(self, manual=False):
        instante = agora()
        with closing(self.conectar()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            rotina = conn.execute('SELECT * FROM certificados_rotina WHERE id=1').fetchone()
            if rotina['bloqueio_ate'] and rotina['bloqueio_ate'] > instante.isoformat():
                return 'Uma verificação já está em andamento ou foi executada recentemente.'
            if not manual and rotina['mes'] == instante.strftime('%Y-%m'):
                return 'Verificação mensal já concluída.'
            conn.execute('UPDATE certificados_rotina SET bloqueio_ate=? WHERE id=1', ((instante + timedelta(minutes=30)).isoformat(),))
            dominios = conn.execute('SELECT dominio FROM certificados ORDER BY dominio').fetchall()
        concluida = False
        consultas_ok = True
        try:
            for item in dominios:
                expira = None
                try:
                    expira, status = consultar_certificado(item['dominio'])
                except Exception:
                    consultas_ok = False
                    status = 'Falha na verificação'
                    LOGGER.warning('Falha ao consultar certificado de %s', item['dominio'])
                with closing(self.conectar()) as conn, conn:
                    conn.execute('UPDATE certificados SET expira_em=COALESCE(?, expira_em), status=?, verificado_em=? WHERE dominio=?',
                                 (expira, status, agora().isoformat(), item['dominio']))
            with closing(self.conectar()) as conn:
                pendentes = conn.execute('''SELECT * FROM certificados WHERE expira_em IS NOT NULL
                    AND expira_em <= ? AND (email_expiracao IS NULL OR email_expiracao != expira_em)''',
                    ((agora() + timedelta(days=ALERTA_DIAS)).isoformat(),)).fetchall()
            resultado = 'Verificação concluída.'
            if pendentes:
                enviado = False
                try:
                    enviar_email(pendentes)
                    enviado = True
                except Exception:
                    LOGGER.exception('Falha ao enviar alerta de certificados')
                estado = 'Enviado' if enviado else 'Falha no envio; nova tentativa automática'
                with closing(self.conectar()) as conn, conn:
                    conn.execute('INSERT INTO certificados_emails(criado_em, dominios, resultado) VALUES (?, ?, ?)',
                                 (agora().isoformat(), ', '.join(p['dominio'] for p in pendentes), estado))
                    for item in pendentes:
                        conn.execute('UPDATE certificados SET email_status=? WHERE dominio=?', (estado, item['dominio']))
                        if enviado:
                            conn.execute('UPDATE certificados SET email_em=?, email_expiracao=? WHERE dominio=?',
                                         (agora().isoformat(), item['expira_em'], item['dominio']))
                resultado += ' ' + estado + '.'
                if not enviado:
                    return resultado
            concluida = consultas_ok
            return resultado
        finally:
            with closing(self.conectar()) as conn, conn:
                conn.execute('UPDATE certificados_rotina SET mes=CASE WHEN ? THEN ? ELSE mes END, bloqueio_ate=? WHERE id=1',
                             (concluida, instante.strftime('%Y-%m'), (agora() + timedelta(minutes=1)).isoformat()))


def configurar_certificados(bp, conectar, tem_permissao, scheduler):
    monitor = MonitorCertificados(conectar)

    @bp.route('/certificados', endpoint='certificados')
    def listar():
        with closing(conectar()) as conn:
            itens = [dict(item) for item in conn.execute('SELECT * FROM certificados ORDER BY dominio')]
            historico = conn.execute('SELECT * FROM certificados_emails ORDER BY id DESC LIMIT 100').fetchall()
        for item in itens:
            item['dias'] = math.ceil((datetime.fromisoformat(item['expira_em']) - agora()).total_seconds() / 86400) if item['expira_em'] else None
            if item['dias'] is not None and item['dias'] <= 0 and item['status'] != 'Falha na verificação':
                item['status'] = 'Expirado'
        colunas = {'dominio': 'Subdomínio', 'status': 'Situação TLS', 'expira_em': 'Validade',
                   'dias': 'Dias restantes', 'verificado_em': 'Última verificação',
                   'email_em': 'Último e-mail enviado', 'email_status': 'Solicitação de renovação'}
        ordem = request.args.get('ordem', 'dominio')
        if ordem not in colunas:
            ordem = 'dominio'
        direcao = 'desc' if request.args.get('direcao') == 'desc' else 'asc'
        presentes = [item for item in itens if item[ordem] is not None]
        ausentes = [item for item in itens if item[ordem] is None]
        itens = sorted(presentes, key=lambda item: item[ordem], reverse=direcao == 'desc') + ausentes
        return render_template('certificados.html', itens=itens, historico=historico, formatar_data=formatar_data,
                               colunas=colunas, ordem=ordem, direcao=direcao,
                               pode_editar=tem_permissao('perm_certificados'), alerta_dias=ALERTA_DIAS)

    @bp.route('/admin/certificados/adicionar', methods=['POST'])
    def adicionar_certificado():
        if not tem_permissao('perm_certificados'):
            return 'Acesso negado.', 403
        try:
            dominio = validar_dominio(request.form.get('dominio'))
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for('bancos.certificados'))
        with closing(conectar()) as conn, conn:
            if conn.execute('SELECT COUNT(*) FROM certificados').fetchone()[0] >= 100:
                return 'Limite de 100 subdomínios atingido.', 400
            inserido = conn.execute('INSERT OR IGNORE INTO certificados(dominio) VALUES (?)', (dominio,)).rowcount
            if inserido:
                conn.execute('UPDATE certificados_rotina SET mes=NULL WHERE id=1')
        flash('Subdomínio adicionado.' if inserido else 'Subdomínio já cadastrado.')
        return redirect(url_for('bancos.certificados'))

    @bp.route('/admin/certificados/verificar', methods=['POST'])
    def verificar_certificados():
        if not tem_permissao('perm_certificados'):
            return 'Acesso negado.', 403
        flash(monitor.verificar(manual=True))
        return redirect(url_for('bancos.certificados'))

    scheduler.add_job(monitor.verificar, 'cron', hour=5, minute=0, timezone='America/Fortaleza',
                      id='certificados_mensal', replace_existing=True, max_instances=1, coalesce=True)
