import posixpath
import re
import shutil
import stat
import secrets


def validar_pasta(caminho, base):
    if not isinstance(caminho, str) or not caminho.startswith('/'):
        raise ValueError('Selecione uma pasta de destino válida.')
    if '..' in caminho.split('/') or '\\' in caminho or any(ord(c) < 32 or ord(c) == 127 for c in caminho):
        raise ValueError('Pasta de destino inválida.')
    caminho = posixpath.normpath(caminho)
    if caminho != base and not caminho.startswith(base + '/'):
        raise ValueError('A pasta deve estar dentro do diretório do servidor.')
    return caminho


def conferir_pasta(sftp, caminho, base):
    caminho = validar_pasta(caminho, base)
    base_real = sftp.normalize(base)
    real = sftp.normalize(caminho)
    if real != base_real and not real.startswith(base_real.rstrip('/') + '/'):
        raise ValueError('A pasta aponta para fora do diretório permitido.')
    if not stat.S_ISDIR(sftp.lstat(caminho).st_mode):
        raise ValueError('Selecione uma pasta real, sem link simbólico.')
    return caminho


def listar_pastas(sftp, caminho, base):
    caminho = conferir_pasta(sftp, caminho, base)
    pastas = [dict(nome=item.filename, caminho=posixpath.join(caminho, item.filename))
              for item in sftp.listdir_attr(caminho)
              if stat.S_ISDIR(item.st_mode) and item.filename not in {'.', '..'}
              and '/' not in item.filename and '\\' not in item.filename]
    return dict(atual=caminho, pai=posixpath.dirname(caminho) if caminho != base else None,
                pastas=sorted(pastas, key=lambda item: item['nome'].casefold()))


def adicionar_banco(sftp, base, alias, modo, pasta, nome_arquivo, arquivo=None):
    if not re.fullmatch(r'[a-z0-9]+', alias) or modo not in {'alias', 'existente'}:
        raise ValueError('Alias ou modo de destino inválido.')
    if len(nome_arquivo) > 150 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.fdb', nome_arquivo, re.I):
        raise ValueError('Informe um nome de arquivo .fdb válido, sem diretórios.')
    if modo == 'alias':
        conferir_pasta(sftp, base, base)
        pasta = posixpath.join(base, alias)
        try:
            sftp.lstat(pasta)
        except FileNotFoundError:
            sftp.mkdir(pasta, mode=0o775)
    pasta = conferir_pasta(sftp, pasta, base)
    caminho = posixpath.join(pasta, nome_arquivo)
    try:
        existente = sftp.lstat(caminho)
    except FileNotFoundError:
        existente = None
    if existente and (arquivo is not None or not stat.S_ISREG(existente.st_mode)):
        raise ValueError('Já existe um arquivo nesse destino. Escolha outro nome; o arquivo existente não será substituído.')
    if arquivo is not None:
        temporario = posixpath.join(pasta, f'.{nome_arquivo}.{secrets.token_hex(12)}.upload')
        criado = False
        try:
            with sftp.open(temporario, 'wx') as destino:
                criado = True
                shutil.copyfileobj(arquivo, destino, length=1024 * 1024)
            sftp.chmod(temporario, 0o664)
            # Standard SFTP rename rejects an existing destination, including concurrent uploads.
            sftp.rename(temporario, caminho)
        except Exception:
            if criado:
                try:
                    sftp.remove(temporario)
                except OSError:
                    pass
            raise
    return caminho
