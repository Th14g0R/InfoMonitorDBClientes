import re
from html.parser import HTMLParser


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
