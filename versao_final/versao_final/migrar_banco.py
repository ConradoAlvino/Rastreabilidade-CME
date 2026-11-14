"""Copia de modo seguro um banco anterior para a versão alternativa.
Uso: python migrar_banco.py "C:\\caminho\\anterior\\bandejas_web.db"
Encerre o servidor antes. NÃO sobrescreve arquivos existentes.
"""
from pathlib import Path
import sqlite3
import sys

DESTINO = Path(__file__).resolve().parent / 'bandejas_web.db'

def main():
    if len(sys.argv)!=2:
        print('Uso: python migrar_banco.py "C:\\caminho\\anterior\\bandejas_web.db"')
        return 2
    origem=Path(sys.argv[1]).expanduser().resolve()
    if not origem.is_file():
        print('ERRO: banco anterior não encontrado:', origem)
        return 2
    if DESTINO.exists():
        print('ERRO: banco de destino já existe. Nenhum dado foi alterado:', DESTINO)
        return 2
    try:
        with sqlite3.connect(f'file:{origem.as_posix()}?mode=ro',uri=True) as con:
            tables={r[0] for r in con.execute('SELECT name FROM sqlite_master WHERE type="table"')}
            if not {'usuarios','bandejas','pedidos','contas'} <= tables:
                print('ERRO: arquivo não parece ser um banco TrayTrack Web com usuários.');return 2
            usuarios=con.execute('SELECT COUNT(*) FROM usuarios').fetchone()[0]
            with sqlite3.connect(DESTINO) as target:
                con.backup(target)
        with sqlite3.connect(DESTINO) as check:
            count=check.execute('SELECT COUNT(*) FROM usuarios').fetchone()[0]
        if count!=usuarios:
            DESTINO.unlink(missing_ok=True)
            print('ERRO: validação do backup falhou.');return 1
        print(f'Banco copiado e verificado: {count} usuário(s). Destino: {DESTINO}')
        print('Agora execute: python web_app.py')
        return 0
    except (sqlite3.Error, OSError) as exc:
        DESTINO.unlink(missing_ok=True)
        print('Falha na migração:',exc)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
