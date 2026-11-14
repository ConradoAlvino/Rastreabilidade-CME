"""Protótipo v4: rastreabilidade administrativa de bandejas.
Python >=3.10, SQLite; somente demonstração, sem autenticação nem validação de esterilidade.
"""
from __future__ import annotations
import argparse
import json
import hashlib
import re
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
from uuid import uuid4

CORES_GRUPO_1 = ('azul', 'verde', 'vermelho', 'amarelo')
CORES_GRUPO_2 = ('laranja', 'roxo', 'branco', 'preto')
CORES = CORES_GRUPO_1 + CORES_GRUPO_2
PAPEIS = ('medico', 'enfermeiro', 'gerente_estoque')
SETORES_ATENDIMENTO = ('Enfermaria', 'CTI', 'Pronto Atendimento', 'Bloco Cirúrgico')

def setor_canonico(valor):
    return next((x for x in SETORES_ATENDIMENTO if normalizar(x) == normalizar(valor or '')), None)

ETAPAS = ('estoque', 'separada', 'enfermagem', 'emprestada', 'retorno_estoque')
LIMITE_ALERTA = 3
LIMITE_EMPRESTIMO_HORAS = max(1/60, float(os.getenv("TRAYTRACK_LIMITE_EMPRESTIMO_HORAS", "24")))
# As cores identificam um PADRÃO DE COMPOSIÇÃO, independentemente do setor.
PARES_CORES = [(a,b) for a in CORES_GRUPO_1 for b in CORES_GRUPO_2]

def validar_par_cores(cor_1, cor_2):
    """Cada bloco deve ter uma cor permitida, diferente da outra."""
    if cor_1 not in CORES_GRUPO_1 or cor_2 not in CORES_GRUPO_2:
        raise RegraNegocioError('Bloco 1: azul, verde, vermelho ou amarelo; bloco 2: laranja, roxo, branco ou preto.')
    return cor_1, cor_2


def codigo_ean13(numero):
    # 20 + 10 dígitos sequenciais (prefixo de circulação restrita), mais dígito de controle.
    base = f'20{numero:010d}'
    soma = sum(int(c) * (1 if i % 2 == 0 else 3) for i,c in enumerate(base))
    return base + str((-soma) % 10)

def ean13_valido(codigo):
    return bool(re.fullmatch(r'\d{13}', str(codigo))) and codigo_ean13(int(codigo[2:12])) == codigo and codigo.startswith('20')

def assinatura_itens(itens):
    return json.dumps(sorted([(normalizar(n), int(q)) for n,q in itens]), ensure_ascii=False)


# Dados fictícios para demonstração. Não são listas de instrumentais clínicos validadas.
TIPOS_PADRAO = [
    ('Enfermaria', 'Kits demonstrativos para enfermaria'),
    ('CTI', 'Kits demonstrativos para centro de terapia intensiva'),
    ('Pronto Atendimento', 'Kits demonstrativos para pronto atendimento'),
    ('Bloco Cirúrgico', 'Kits demonstrativos para bloco cirúrgico'),
]
# Composições apenas para TESTES: não correspondem a kits clinicamente aprovados.
# Quatro composições diferentes, uma por setor, reutilizadas em dez unidades físicas
# de cada setor. A mesma composição sempre terá o mesmo par de cores.
BASE_ENFERMARIA = [
    ('Gaze (pacote)', 2), ('Compressa (pacote)', 2), ('Pinça anatômica (exemplo)', 1),
    ('Tesoura (exemplo)', 1), ('Cuba (exemplo)', 1), ('Campo (exemplo)', 2),
    ('Seringa (exemplo)', 1), ('Curativo (exemplo)', 1), ('Pinça auxiliar (exemplo)', 1),
    ('Bandeja auxiliar (exemplo)', 1),
]
BASE_CTI = [
    ('Gaze (pacote)', 2), ('Compressa (pacote)', 2), ('Pinça hemostática (exemplo)', 1),
    ('Tesoura para curativo (exemplo)', 1), ('Cuba (exemplo)', 1), ('Campo (exemplo)', 2),
    ('Conector (exemplo)', 1), ('Seringa (exemplo)', 1), ('Pinça auxiliar (exemplo)', 1),
    ('Suporte de materiais (exemplo)', 1),
]
BASE_PRONTO_ATENDIMENTO = [
    ('Gaze (pacote)', 2), ('Compressa (pacote)', 2), ('Tesoura (exemplo)', 1),
    ('Pinça de curativo (exemplo)', 1), ('Cuba (exemplo)', 1), ('Campo (exemplo)', 2),
    ('Seringa (exemplo)', 1), ('Curativo (exemplo)', 1), ('Pinça auxiliar (exemplo)', 1),
    ('Bandeja de procedimento (exemplo)', 1),
]
BASE_BLOCO_CIRURGICO = [
    ('Gaze (pacote)', 2), ('Compressa (pacote)', 2), ('Pinça hemostática (exemplo)', 1),
    ('Pinça anatômica (exemplo)', 1), ('Bisturi (exemplo)', 1), ('Tesoura cirúrgica (exemplo)', 1),
    ('Porta-agulhas (exemplo)', 1), ('Afastador (exemplo)', 1),
    ('Campo cirúrgico (exemplo)', 2), ('Cuba de instrumentais (exemplo)', 1),
]
# Um pool único de 30 kits compartilhados. Nenhum kit nasce vinculado a um setor.
# As 3 composições fictícias são identificadas por cores, 10 cópias de cada.
TIPO_COMPARTILHADO = 'Arsenal'
LOCAL_ESTOQUE = 'Arsenal'
BANDEJAS_PADRAO = [
    (f'KIT-ARS-{j:03d}', TIPO_COMPARTILHADO, composicao)
    for inicio, composicao in ((1,BASE_ENFERMARIA),(11,BASE_CTI),(21,BASE_BLOCO_CIRURGICO))
    for j in range(inicio,inicio+10)
]
# Versão anterior: utilizada somente para reconhecer kits demonstrativos antigos
# e migrá-los quando não houver qualquer movimentação ou conferência.
BANDEJAS_LEGADAS = {
    'BDJ-001':[('Gaze (pacote)',2),('Bisturi (exemplo)',1)],
    'BDJ-002':[('Gaze (pacote)',2),('Pinça (exemplo)',1)],
    'BDJ-003':[('Gaze (pacote)',3),('Bisturi (exemplo)',1)],
    'BDJ-004':[('Gaze (pacote)',2),('Tesoura (exemplo)',1)],
    'BDJ-005':[('Pinça ortopédica (exemplo)',1),('Compressa (pacote)',2)],
    'BDJ-006':[('Pinça ortopédica (exemplo)',2),('Gaze (pacote)',2)],
    'BDJ-007':[('Afastador (exemplo)',1),('Compressa (pacote)',2)],
    'BDJ-008':[('Pinça delicada (exemplo)',1),('Gaze (pacote)',2)],
    'BDJ-009':[('Tesoura delicada (exemplo)',1),('Gaze (pacote)',2)],
    'BDJ-010':[('Pinça delicada (exemplo)',2),('Compressa (pacote)',1)],
}

class RegraNegocioError(ValueError):
    pass

def agora():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def normalizar(texto):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', texto.casefold())
                            if not unicodedata.combining(c)).split())

class SistemaBandejas:
    def __init__(self, banco: str | Path = 'bandejas_v4.db'):
        self.banco = str(banco)
        self._criar_tabelas()
        self._migrar_cores_oito()
        self._seed_inicial()
        self._proteger_cores_no_banco()

    def _migrar_cores_oito(self):
        """Migra pares antigos sem mudar IDs, pedidos, itens ou histórico.

        A tabela bandejas da versão anterior contém CHECK que não aceita as cores
        do grupo 2; é reconstruída transacionalmente com FKs desligadas apenas
        nessa conexão local. A associação composição->padrão permanece intacta.
        """
        con = sqlite3.connect(self.banco, timeout=20)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys=OFF')
            con.execute('BEGIN IMMEDIATE')
            pares_padrao = con.execute('SELECT id,cor_1,cor_2 FROM padroes_cores ORDER BY id').fetchall()
            pares_bandeja = con.execute('SELECT DISTINCT cor_1,cor_2 FROM bandejas ORDER BY cor_1,cor_2').fetchall()
            pares = list(dict.fromkeys(
                [(r['cor_1'],r['cor_2']) for r in pares_padrao]
                + [(r['cor_1'],r['cor_2']) for r in pares_bandeja]))
            if len(pares) > len(PARES_CORES):
                raise RegraNegocioError('Há mais de 16 padrões antigos distintos; migração requer revisão manual.')
            # Se já são todas cores válidas, não muda identificações numa segunda inicialização.
            validos = {p for p in PARES_CORES}
            usados = {p for p in pares if p in validos}
            livres = [p for p in PARES_CORES if p not in usados]
            mapa = {p: (p if p in validos else livres.pop(0)) for p in pares}
            old_ddl = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='bandejas'").fetchone()[0]
            velho = "CHECK(cor_2 IN ('azul','verde','vermelho','amarelo'))"
            if velho in old_ddl:
                # Mantém exatamente os campos acrescentados por versões anteriores.
                nova_ddl = old_ddl.replace('CREATE TABLE bandejas (', 'CREATE TABLE bandejas_nova (', 1)
                nova_ddl = nova_ddl.replace(velho, "CHECK(cor_2 IN ('laranja','roxo','branco','preto'))")
                assert nova_ddl != old_ddl and 'CREATE TABLE bandejas_nova (' in nova_ddl
                con.execute(nova_ddl)
                campos = [r['name'] for r in con.execute('PRAGMA table_info(bandejas)')]
                nomes = ','.join('"'+c+'"' for c in campos)
                for linha in con.execute('SELECT * FROM bandejas').fetchall():
                    valores = dict(linha)
                    novo = mapa[(valores['cor_1'], valores['cor_2'])]
                    valores['cor_1'], valores['cor_2'] = novo
                    con.execute(f"INSERT INTO bandejas_nova ({nomes}) VALUES ({','.join('?' for _ in campos)})",
                                [valores[c] for c in campos])
                con.execute('DROP TABLE bandejas')
                con.execute('ALTER TABLE bandejas_nova RENAME TO bandejas')
                con.execute('CREATE INDEX IF NOT EXISTS idx_bandejas_tipo_etapa ON bandejas(tipo_id,etapa)')
            elif "CHECK(cor_2 IN ('laranja','roxo','branco','preto'))" not in old_ddl:
                raise RegraNegocioError('Esquema de cores desconhecido; migração cancelada.')
            # Retira gatilhos legados antes de converter as cores dos padrões.
            for tabela in ('bandejas','padroes_cores'):
                for acao in ('insert','update'):
                    con.execute(f'DROP TRIGGER IF EXISTS validar_cores_{tabela}_{acao}')
            # Padrões têm UNIQUE(cor_1,cor_2); os destinos são todos distintos.
            for antigo, novo in mapa.items():
                if antigo == novo:
                    continue
                con.execute('UPDATE padroes_cores SET cor_1=?,cor_2=? WHERE cor_1=? AND cor_2=?',
                            (*novo,*antigo))
                if velho not in old_ddl:
                    con.execute('UPDATE bandejas SET cor_1=?,cor_2=? WHERE cor_1=? AND cor_2=?',
                                (*novo,*antigo))
            violations = con.execute('PRAGMA foreign_key_check').fetchall()
            if violations:
                raise RegraNegocioError('Falha de integridade durante migração: '+str([tuple(v) for v in violations[:3]]))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _proteger_cores_no_banco(self):
        # TRIGGERs também protegem bancos antigos cujo CREATE TABLE não tinha CHECK.
        with self._conexao() as con:
            for tabela in ('bandejas', 'padroes_cores'):
                for acao in ('INSERT', 'UPDATE'):
                    nome = f'validar_cores_{tabela}_{acao.lower()}'
                    con.execute(f'DROP TRIGGER IF EXISTS {nome}')
                    con.execute(f'''CREATE TRIGGER {nome}
                        BEFORE {acao} ON {tabela}
                        WHEN NEW.cor_1 NOT IN ('azul','verde','vermelho','amarelo')
                          OR NEW.cor_2 NOT IN ('laranja','roxo','branco','preto')
                        BEGIN SELECT RAISE(ABORT, 'As cores devem seguir os grupos exclusivos'); END''')

    @contextmanager
    def _conexao(self):
        con = sqlite3.connect(self.banco, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _criar_tabelas(self):
        with self._conexao() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY, nome TEXT NOT NULL, setor TEXT NOT NULL,
                papel TEXT NOT NULL CHECK(papel IN ('medico','enfermeiro','gerente_estoque')),
                ativo INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS tipos (
                id INTEGER PRIMARY KEY, nome TEXT NOT NULL UNIQUE, descricao TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY, codigo TEXT NOT NULL UNIQUE,
                medico_id INTEGER NOT NULL REFERENCES usuarios(id),
                tipo_id INTEGER NOT NULL REFERENCES tipos(id),
                status TEXT NOT NULL DEFAULT 'aberto' CHECK(status IN ('aberto','separado','em_atendimento','finalizado')),
                criado_em TEXT NOT NULL, finalizado_em TEXT
            );
            CREATE TABLE IF NOT EXISTS bandejas (
                id INTEGER PRIMARY KEY, codigo TEXT NOT NULL UNIQUE,
                tipo_id INTEGER NOT NULL REFERENCES tipos(id),
                cor_1 TEXT NOT NULL CHECK(cor_1 IN ('azul','verde','vermelho','amarelo')),
                cor_2 TEXT NOT NULL CHECK(cor_2 IN ('laranja','roxo','branco','preto')),
                etapa TEXT NOT NULL DEFAULT 'estoque' CHECK(etapa IN ('estoque','separada','enfermagem','emprestada','retorno_estoque')),
                pedido_id INTEGER REFERENCES pedidos(id),
                responsavel_id INTEGER REFERENCES usuarios(id),
                CHECK(cor_1 != cor_2)
            );
            CREATE TABLE IF NOT EXISTS itens_bandeja (
                id INTEGER PRIMARY KEY, bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                nome TEXT NOT NULL, quantidade INTEGER NOT NULL CHECK(quantidade>0),
                UNIQUE(bandeja_id,nome)
            );
            CREATE TABLE IF NOT EXISTS movimentos (
                id INTEGER PRIMARY KEY,
                bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                pedido_id INTEGER REFERENCES pedidos(id),
                ator_id INTEGER NOT NULL REFERENCES usuarios(id),
                acao TEXT NOT NULL, origem TEXT NOT NULL, destino TEXT NOT NULL,
                responsavel_anterior_id INTEGER REFERENCES usuarios(id),
                responsavel_novo_id INTEGER REFERENCES usuarios(id),
                observacao TEXT NOT NULL DEFAULT '', criado_em TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS faltas (
                id INTEGER PRIMARY KEY, bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                pedido_id INTEGER REFERENCES pedidos(id), item_id INTEGER NOT NULL REFERENCES itens_bandeja(id),
                quantidade INTEGER NOT NULL CHECK(quantidade > 0),
                reportado_por INTEGER NOT NULL REFERENCES usuarios(id), reportado_em TEXT NOT NULL,
                regularizado_por INTEGER REFERENCES usuarios(id), regularizado_em TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_faltas_abertas ON faltas(bandeja_id,regularizado_em);
            CREATE INDEX IF NOT EXISTS idx_bandejas_tipo_etapa ON bandejas(tipo_id,etapa);
            CREATE INDEX IF NOT EXISTS idx_movimentos_bandeja ON movimentos(bandeja_id,id);
            CREATE INDEX IF NOT EXISTS idx_pedidos_status ON pedidos(status);
            ''')
            # Padrão de composição escolhido pelo médico (opcional em pedidos legados).
            col_pedidos = {r['name'] for r in con.execute('PRAGMA table_info(pedidos)')}
            if 'padrao_cores_id' not in col_pedidos:
                con.execute('ALTER TABLE pedidos ADD COLUMN padrao_cores_id INTEGER REFERENCES padroes_cores(id)')
            # Migração não destrutiva de bancos anteriores.
            campos_tipos = {r['name'] for r in con.execute('PRAGMA table_info(tipos)')}
            if 'cor_1' not in campos_tipos:
                con.execute('ALTER TABLE tipos ADD COLUMN cor_1 TEXT')
            if 'cor_2' not in campos_tipos:
                con.execute('ALTER TABLE tipos ADD COLUMN cor_2 TEXT')
            campos_bandejas = {r['name'] for r in con.execute('PRAGMA table_info(bandejas)')}
            if 'bloqueada' not in campos_bandejas:
                con.execute('ALTER TABLE bandejas ADD COLUMN bloqueada INTEGER NOT NULL DEFAULT 0')
            # Padrões: as duas cores representam a composição, não o setor.
            con.execute('''CREATE TABLE IF NOT EXISTS padroes_cores (
                id INTEGER PRIMARY KEY, cor_1 TEXT NOT NULL, cor_2 TEXT NOT NULL,
                assinatura TEXT NOT NULL UNIQUE, itens_json TEXT NOT NULL,
                UNIQUE(cor_1,cor_2), CHECK(cor_1<>cor_2))''')
            con.execute('''CREATE TABLE IF NOT EXISTS conferencias (
                id INTEGER PRIMARY KEY, bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                pedido_id INTEGER REFERENCES pedidos(id), ator_id INTEGER NOT NULL REFERENCES usuarios(id),
                fase TEXT NOT NULL, itens_json TEXT NOT NULL, criado_em TEXT NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS incidentes_lacre (
                id INTEGER PRIMARY KEY, bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                conferencia_id INTEGER NOT NULL REFERENCES conferencias(id),
                registrado_em TEXT NOT NULL, resolvido_por INTEGER REFERENCES usuarios(id),
                resolvido_em TEXT, observacoes TEXT NOT NULL DEFAULT '')''')
            campos_conferencias = {r['name'] for r in con.execute('PRAGMA table_info(conferencias)')}
            if 'lacre_integro' not in campos_conferencias:
                con.execute('ALTER TABLE conferencias ADD COLUMN lacre_integro INTEGER')
            if 'observacoes' not in campos_conferencias:
                con.execute("ALTER TABLE conferencias ADD COLUMN observacoes TEXT NOT NULL DEFAULT ''")
            campos_faltas = {r['name'] for r in con.execute('PRAGMA table_info(faltas)')}
            if 'fase' not in campos_faltas:
                con.execute("ALTER TABLE faltas ADD COLUMN fase TEXT NOT NULL DEFAULT 'devolucao_emprestimo'")
            if 'codigo_legado' not in campos_bandejas:
                con.execute('ALTER TABLE bandejas ADD COLUMN codigo_legado TEXT')
            # Extensões da alternativa: transferências bilaterais, divergências e inventários.
            con.executescript("""
            CREATE TABLE IF NOT EXISTS transferencias_pendentes (
                id INTEGER PRIMARY KEY, bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                pedido_id INTEGER NOT NULL REFERENCES pedidos(id),
                origem_id INTEGER NOT NULL REFERENCES usuarios(id),
                destino_id INTEGER NOT NULL REFERENCES usuarios(id),
                status TEXT NOT NULL DEFAULT 'pendente' CHECK(status IN ('pendente','aceita','recusada','cancelada')),
                solicitado_em TEXT NOT NULL, concluido_em TEXT, observacao TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unica_transferencia_pendente
              ON transferencias_pendentes(bandeja_id) WHERE status='pendente';
            CREATE TABLE IF NOT EXISTS inventarios_rotativos (
                id INTEGER PRIMARY KEY, localizacao TEXT NOT NULL, iniciado_por INTEGER NOT NULL REFERENCES usuarios(id),
                iniciado_em TEXT NOT NULL, encerrado_em TEXT, encerrado_por INTEGER REFERENCES usuarios(id)
            );
            CREATE TABLE IF NOT EXISTS inventario_leituras (
                id INTEGER PRIMARY KEY, inventario_id INTEGER NOT NULL REFERENCES inventarios_rotativos(id),
                bandeja_id INTEGER NOT NULL REFERENCES bandejas(id), ator_id INTEGER NOT NULL REFERENCES usuarios(id),
                lido_em TEXT NOT NULL, UNIQUE(inventario_id,bandeja_id)
            );
            CREATE TABLE IF NOT EXISTS inventario_esperados (
                inventario_id INTEGER NOT NULL REFERENCES inventarios_rotativos(id),
                bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
                PRIMARY KEY (inventario_id,bandeja_id)
            );
            CREATE TABLE IF NOT EXISTS divergencias_classificadas (
                id INTEGER PRIMARY KEY, conferencia_id INTEGER NOT NULL REFERENCES conferencias(id),
                bandeja_id INTEGER NOT NULL REFERENCES bandejas(id), item_id INTEGER NOT NULL REFERENCES itens_bandeja(id),
                quantidade INTEGER NOT NULL, motivo TEXT NOT NULL CHECK(motivo IN ('consumido','descartado','nao_localizado','em_apuracao')),
                registrado_por INTEGER NOT NULL REFERENCES usuarios(id), registrado_em TEXT NOT NULL
            );
            """)
            if 'localizacao_atual' not in campos_bandejas:
                con.execute("ALTER TABLE bandejas ADD COLUMN localizacao_atual TEXT NOT NULL DEFAULT 'Arsenal'")
                con.execute("""UPDATE bandejas SET localizacao_atual = CASE
                    WHEN etapa='estoque' THEN 'Arsenal'
                    WHEN etapa='separada' THEN 'Em transporte'
                    WHEN etapa='retorno_estoque' THEN 'Em devolução ao estoque'
                    ELSE COALESCE((SELECT NULLIF(u.setor,'') FROM usuarios u WHERE u.id=bandejas.responsavel_id),'Localização não confirmada') END""")
            if 'categoria' not in campos_faltas:
                con.execute("ALTER TABLE faltas ADD COLUMN categoria TEXT NOT NULL DEFAULT 'em_apuracao'")
            # A solicitação permanece vinculada ao médico original. A posse é separada.
            if 'portador_medico_id' not in campos_bandejas:
                con.execute('ALTER TABLE bandejas ADD COLUMN portador_medico_id INTEGER REFERENCES usuarios(id)')
            if 'saida_estoque_em' not in campos_bandejas:
                con.execute('ALTER TABLE bandejas ADD COLUMN saida_estoque_em TEXT')
            # O relógio de uso passa a iniciar no primeiro check-in confirmado.
            # Preserva a saída do estoque somente para rastreabilidade logística.
            if 'checkin_em' not in campos_bandejas:
                con.execute('ALTER TABLE bandejas ADD COLUMN checkin_em TEXT')
            # Migração idempotente: reconstrói o check-in de ciclos antigos pelo histórico.
            con.execute("""UPDATE bandejas SET checkin_em=(
                SELECT m.criado_em FROM movimentos m
                WHERE m.bandeja_id=bandejas.id AND m.acao='checkin_pedido'
                ORDER BY m.id DESC LIMIT 1)
                WHERE checkin_em IS NULL AND etapa IN ('enfermagem','emprestada','retorno_estoque')""")
            # Migração de bandejas que já estavam fora do estoque: recupera a saída.
            con.execute("""UPDATE bandejas SET portador_medico_id=(
                SELECT p.medico_id FROM pedidos p WHERE p.id=bandejas.pedido_id)
                WHERE portador_medico_id IS NULL AND pedido_id IS NOT NULL AND etapa!='estoque'""")
            con.execute("""UPDATE bandejas SET saida_estoque_em=(
                SELECT m.criado_em FROM movimentos m
                WHERE m.bandeja_id=bandejas.id AND m.acao='saida_estoque'
                ORDER BY m.id DESC LIMIT 1)
                WHERE saida_estoque_em IS NULL AND etapa!='estoque'""")

            # Versão setorial: histórico do setor requisitante + vínculo paciente-pedido.
            con.execute("""CREATE TABLE IF NOT EXISTS pacientes (
                id INTEGER PRIMARY KEY, codigo TEXT NOT NULL UNIQUE,
                nome TEXT NOT NULL, setor TEXT NOT NULL,
                ativo INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0,1)),
                ficticio INTEGER NOT NULL DEFAULT 0,
                cadastrado_por INTEGER REFERENCES usuarios(id),
                criado_em TEXT NOT NULL)""")
            campos_pedidos={r['name'] for r in con.execute('PRAGMA table_info(pedidos)')}
            if 'setor_atendimento' not in campos_pedidos:
                con.execute('ALTER TABLE pedidos ADD COLUMN setor_atendimento TEXT')
            if 'paciente_id' not in campos_pedidos:
                con.execute('ALTER TABLE pedidos ADD COLUMN paciente_id INTEGER REFERENCES pacientes(id)')
            con.execute("""UPDATE pedidos SET setor_atendimento=(
                SELECT u.setor FROM usuarios u WHERE u.id=pedidos.medico_id)
                WHERE setor_atendimento IS NULL""")
            con.execute('CREATE INDEX IF NOT EXISTS idx_pacientes_setor ON pacientes(setor,ativo)')
            # Exemplos totalmente fictícios, idempotentes e sem dados sensíveis.
            nomes_ficticios = ('Aurora Freire', 'Breno Monteiro', 'Clara Tavares',
                'Davi Siqueira', 'Elisa Nogueira', 'Fábio Ribeiro', 'Giovana Paes',
                'Heitor Martins', 'Isadora Castro', 'Jonas Almeida')
            for indice in range(1, 11):
                setor=SETORES_ATENDIMENTO[(indice-1) % len(SETORES_ATENDIMENTO)]
                con.execute("""INSERT OR IGNORE INTO pacientes(codigo,nome,setor,ficticio,criado_em)
                    VALUES (?,?,?,?,?)""",
                    (f'PAC-NOVO-{indice:03d}',f'Paciente fictício {indice:02d} — {nomes_ficticios[indice-1]}',setor,1,agora()))
            # Novos pacientes fictícios são acrescentados sem alterar os históricos anteriores.

    def _seed_inicial(self):
        """Base nova: 30 kits no Arsenal. Bancos migrados: preserva bandejas existentes.

        Nenhum código ou composição histórica é substituído. Bandejas em estoque
        são desvinculadas do setor antigo, sem alterar solicitações anteriores.
        """
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            for nome, descricao in TIPOS_PADRAO:
                con.execute('INSERT OR IGNORE INTO tipos(nome,descricao) VALUES (?,?)',(nome,descricao))
            con.execute('INSERT OR IGNORE INTO tipos(nome,descricao) VALUES (?,?)',
                        (TIPO_COMPARTILHADO,'Conjunto de bandejas compartilhado pelos quatro setores'))
            arsenal_id=con.execute('SELECT id FROM tipos WHERE nome=?',(TIPO_COMPARTILHADO,)).fetchone()['id']
            existentes=con.execute('SELECT COUNT(*) FROM bandejas').fetchone()[0]
            if existentes==0:
                for alias, _, itens in BANDEJAS_PADRAO:
                    padrao=self._garantir_padrao(con,itens)
                    bid=con.execute("""INSERT INTO bandejas(codigo,codigo_legado,tipo_id,cor_1,cor_2,localizacao_atual)
                        VALUES (?,?,?,?,?,?)""",('TEMP-'+uuid4().hex,alias,arsenal_id,
                        padrao['cor_1'],padrao['cor_2'],LOCAL_ESTOQUE)).lastrowid
                    con.execute('UPDATE bandejas SET codigo=? WHERE id=?',(codigo_ean13(bid),bid))
                    con.executemany('INSERT INTO itens_bandeja(bandeja_id,nome,quantidade) VALUES (?,?,?)',
                                    [(bid,n,q) for n,q in itens])
            else:
                # Migração não destrutiva: mudar apenas bandejas já disponíveis no estoque.
                con.execute("""UPDATE bandejas SET tipo_id=?, localizacao_atual=?
                    WHERE etapa='estoque' """,(arsenal_id,LOCAL_ESTOQUE))
                # Nas bandejas em circulação, tipo_id legado não decide o setor de destino.
            for row in con.execute('SELECT id,codigo FROM bandejas').fetchall():
                if not ean13_valido(row['codigo']):
                    con.execute('UPDATE bandejas SET codigo=?,codigo_legado=COALESCE(codigo_legado,?) WHERE id=?',
                                (codigo_ean13(row['id']),row['codigo'],row['id']))
            con.execute('UPDATE tipos SET cor_1=NULL,cor_2=NULL')

    def _garantir_padrao(self, con, itens):
        signature=assinatura_itens(itens)
        padrao=con.execute('SELECT * FROM padroes_cores WHERE assinatura=?',(signature,)).fetchone()
        if padrao:
            validar_par_cores(padrao['cor_1'],padrao['cor_2'])
            return padrao
        usados={(r['cor_1'],r['cor_2']) for r in con.execute('SELECT cor_1,cor_2 FROM padroes_cores')}
        livres=[c for c in PARES_CORES if c not in usados]
        if not livres:
            raise RegraNegocioError('Os 16 padrões de duas cores estão ocupados. Utilize uma composição existente ou amplie o sistema de identificação.')
        c1,c2=validar_par_cores(*livres[0])
        con.execute('INSERT INTO padroes_cores(cor_1,cor_2,assinatura,itens_json) VALUES (?,?,?,?)',
                    (c1,c2,signature,json.dumps(itens,ensure_ascii=False)))
        return con.execute('SELECT * FROM padroes_cores WHERE assinatura=?',(signature,)).fetchone()

    def listar_padroes(self):
        with self._conexao() as con:
            return self._linhas(con.execute('SELECT * FROM padroes_cores ORDER BY id'))

    def proximo_codigo(self):
        with self._conexao() as con:
            n=con.execute('SELECT COALESCE(MAX(id),0)+1 FROM bandejas').fetchone()[0]
            return codigo_ean13(n)

    @staticmethod
    def _linhas(cursor):
        return [dict(r) for r in cursor.fetchall()]

    def _ator(self, con, ator_id, papel):
        r = con.execute('SELECT * FROM usuarios WHERE id=? AND ativo=1', (ator_id,)).fetchone()
        if r is None or r['papel'] != papel:
            raise RegraNegocioError(f'Operação exclusiva de {papel} ativo (ID válido obrigatório).')
        return r

    def _bandeja(self, con, codigo):
        b = con.execute('SELECT * FROM bandejas WHERE codigo=? OR codigo_legado=?', (codigo.strip().upper(),codigo.strip().upper())).fetchone()
        if not b:
            raise RegraNegocioError('Bandeja não encontrada.')
        return b

    def _pedido_codigo(self, con, codigo):
        p = con.execute('SELECT * FROM pedidos WHERE codigo=?', (codigo.strip().upper(),)).fetchone()
        if not p:
            raise RegraNegocioError('Código de solicitação inválido ou inexistente.')
        return p

    def _mov(self, con, b, pedido_id, ator_id, acao, destino, responsavel_id=None, observacao=''):
        con.execute('''INSERT INTO movimentos(bandeja_id,pedido_id,ator_id,acao,origem,destino,
            responsavel_anterior_id,responsavel_novo_id,observacao,criado_em)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (b['id'], pedido_id, ator_id, acao, b['etapa'], destino,
             b['responsavel_id'], responsavel_id, observacao, agora()))
        con.execute('UPDATE bandejas SET etapa=?,pedido_id=?,responsavel_id=? WHERE id=?',
                    (destino,pedido_id,responsavel_id,b['id']))

    def cadastrar_usuario(self, nome, setor, papel, gerente_id=None):
        if not nome.strip() or (papel != 'gerente_estoque' and not setor.strip()) or papel not in PAPEIS:
            raise RegraNegocioError('Nome, setor e papel válido obrigatórios.')
        if papel in ('medico','enfermeiro'):
            setor = setor_canonico(setor)
            if setor is None: raise RegraNegocioError('O setor deve ser Enfermaria, CTI, Pronto Atendimento ou Bloco Cirúrgico.')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT COUNT(*) FROM usuarios').fetchone()[0] == 0:
                if papel != 'gerente_estoque':
                    raise RegraNegocioError('Primeiro usuário deve ser gerente_estoque.')
            else:
                self._ator(con,gerente_id,'gerente_estoque')
            return con.execute('INSERT INTO usuarios(nome,setor,papel) VALUES (?,?,?)',
                               (nome.strip(),setor.strip(),papel)).lastrowid

    def cadastrar_tipo(self, gerente_id, nome, descricao='', cor_1=None, cor_2=None):
        if not setor_canonico(nome):
            raise RegraNegocioError('Só são permitidos os quatro setores padrão.')
        nome = setor_canonico(nome)
        with self._conexao() as con:
            self._ator(con,gerente_id,'gerente_estoque')
            try:
                return con.execute('INSERT INTO tipos(nome,descricao) VALUES (?,?)',
                                   (nome.strip(),descricao.strip())).lastrowid
            except sqlite3.IntegrityError as e:
                raise RegraNegocioError('Setor já cadastrado.') from e

    def cadastrar_bandeja(self, gerente_id, codigo, tipo_nome, cor_1=None, cor_2=None, itens=None):
        """Código EAN-13 sequencial. Cores decorrem da composição (não do setor)."""
        if normalizar(tipo_nome) not in [normalizar(TIPO_COMPARTILHADO)] + [normalizar(x) for x in SETORES_ATENDIMENTO]:
            raise RegraNegocioError('Bandejas novas devem ser destinadas ao Arsenal (pool compartilhado).')
        if not itens or any(not n.strip() or not isinstance(q,int) or q <= 0 for n,q in itens):
            raise RegraNegocioError('Informe itens e quantidades inteiras positivas.')
        if len(set(normalizar(n) for n,_ in itens))!=len(itens):
            raise RegraNegocioError('Itens duplicados na bandeja.')
        if not 10 <= sum(q for _,q in itens) <= 40:
            raise RegraNegocioError('A bandeja deve conter entre 10 e 40 unidades de materiais (soma das quantidades).')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            tipo=self._resolver_tipo(con,TIPO_COMPARTILHADO)
            padrao=self._garantir_padrao(con,itens)
            n=con.execute('SELECT COALESCE(MAX(id),0)+1 FROM bandejas').fetchone()[0]
            codigo=codigo_ean13(n)
            bid=con.execute('INSERT INTO bandejas(codigo,tipo_id,cor_1,cor_2,localizacao_atual) VALUES (?,?,?,?,?)',
                            (codigo,tipo['id'],padrao['cor_1'],padrao['cor_2'],LOCAL_ESTOQUE)).lastrowid
            con.executemany('INSERT INTO itens_bandeja(bandeja_id,nome,quantidade) VALUES (?,?,?)',
                            [(bid,n.strip(),q) for n,q in itens])
            return {'id':bid,'codigo':codigo,'cores':(padrao['cor_1'],padrao['cor_2'])}

    def _resolver_tipo(self, con, entrada):
        nome = re.sub(r'^solicitar\s+bandeja\s+', '', entrada.strip(), flags=re.IGNORECASE)
        nome = normalizar(nome)
        tipos = self._linhas(con.execute('SELECT * FROM tipos'))
        achados = [t for t in tipos if normalizar(t['nome']) == nome]
        if not achados:
            raise RegraNegocioError('Tipo não encontrado. Consulte o comando tipos.')
        return achados[0]

    def solicitar(self, medico_id, tipo_ou_frase, padrao_cores_id=None):
        """Aceita 'cirurgia geral' ou 'Solicitar bandeja cirurgia geral'."""
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            medico=self._ator(con,medico_id,'medico')
            setor=setor_canonico(medico['setor'])
            if not setor: raise RegraNegocioError('Médico precisa estar associado a um setor padrão.')
            tipo = self._resolver_tipo(con,tipo_ou_frase)
            if normalizar(tipo['nome']) != normalizar(setor):
                raise RegraNegocioError(
                    f'Médicos do setor {setor} só podem solicitar bandejas de {setor}.')
            if padrao_cores_id is not None:
                try: padrao_cores_id = int(padrao_cores_id)
                except (ValueError, TypeError): raise RegraNegocioError('Selecione um padrão de cores válido.')
                valido=con.execute('''SELECT 1 FROM bandejas b
                    JOIN padroes_cores pc ON pc.cor_1=b.cor_1 AND pc.cor_2=b.cor_2
                    WHERE pc.id=? LIMIT 1''',(padrao_cores_id,)).fetchone()
                if not valido: raise RegraNegocioError('Padrão de cores não disponível para seu setor.')
            if padrao_cores_id is None:
                raise RegraNegocioError('Selecione a composição e as cores da bandeja.')
            codigo = 'SOL-' + uuid4().hex[:12].upper()
            pid = con.execute('''INSERT INTO pedidos(codigo,medico_id,tipo_id,criado_em,setor_atendimento,padrao_cores_id)
                                 VALUES (?,?,?,?,?,?)''',(codigo,medico_id,tipo['id'],agora(),setor,padrao_cores_id)).lastrowid
            return {'pedido_id':pid,'codigo_solicitacao':codigo,'tipo':tipo['nome'],
                    'setor_atendimento':setor,'padrao_cores_id':padrao_cores_id,'status':'aberto'}

    def separar_estoque(self, gerente_id, codigo_pedido, codigo_bandeja):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            p = self._pedido_codigo(con,codigo_pedido)
            b = self._bandeja(con,codigo_bandeja)
            if p['status']!='aberto' or b['etapa']!='estoque' or b['bloqueada'] or b['localizacao_atual']!=LOCAL_ESTOQUE:
                raise RegraNegocioError('Pedido não aberto ou bandeja indisponível no Arsenal.')
            if p['padrao_cores_id'] is not None:
                padrao=con.execute('SELECT cor_1,cor_2 FROM padroes_cores WHERE id=?',
                                   (p['padrao_cores_id'],)).fetchone()
                if not padrao or (b['cor_1'],b['cor_2']) != (padrao['cor_1'],padrao['cor_2']):
                    raise RegraNegocioError('A bandeja não corresponde às cores/composição solicitadas pelo médico.')
            self._mov(con,b,p['id'],gerente_id,'saida_estoque','separada',observacao='Destino solicitado: '+(p['setor_atendimento'] or 'não informado'))
            con.execute("UPDATE bandejas SET localizacao_atual='Em transporte' WHERE id=?",(b['id'],))
            con.execute('UPDATE bandejas SET portador_medico_id=?,saida_estoque_em=? WHERE id=?',
                        (p['medico_id'],agora(),b['id']))
            con.execute("UPDATE pedidos SET status='separado' WHERE id=?",(p['id'],))
            alerta = self._alerta_tipo(con,b['tipo_id'])
            return {'pedido':p['codigo'],'bandeja':b['codigo'],'status':'separado','alerta_estoque':alerta}

    def checkin_pedido(self, enfermeiro_id, codigo_pedido, codigo_bandeja, faltantes=None, lacre_integro=True, observacoes="", classificacoes=None, paciente_id=None):
        if not codigo_pedido or not codigo_pedido.strip():
            raise RegraNegocioError('Código da solicitação médica obrigatório.')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            enfermeiro=self._ator(con,enfermeiro_id,'enfermeiro')
            p = self._pedido_codigo(con,codigo_pedido)
            if not paciente_id: raise RegraNegocioError('Selecione o paciente atendido antes do check-in.')
            if normalizar(enfermeiro['setor']) != normalizar(p['setor_atendimento']):
                raise RegraNegocioError('Técnicos de enfermagem só podem fazer check-in de pedidos do próprio setor.')
            paciente=con.execute('SELECT * FROM pacientes WHERE id=? AND ativo=1',(paciente_id,)).fetchone()
            if not paciente or normalizar(paciente['setor']) != normalizar(p['setor_atendimento']):
                raise RegraNegocioError('Paciente não encontrado ou pertencente a outro setor.')
            b = self._bandeja(con,codigo_bandeja)
            if p['status']!='separado' or b['pedido_id']!=p['id'] or b['etapa']!='separada':
                raise RegraNegocioError('Bandeja não liberada para essa solicitação médica.')
            con.execute('UPDATE pedidos SET paciente_id=? WHERE id=?',(paciente['id'],p['id']))
            achados=self._registrar_conferencia(con,b,enfermeiro_id,'checkin_retirada',faltantes or [],lacre_integro,observacoes,classificacoes)
            self._mov(con,b,p['id'],enfermeiro_id,'checkin_pedido','enfermagem',enfermeiro_id,
                      ('Itens faltantes no recebimento: '+str(achados) if achados else 'Itens conferidos sem faltas') + ('; LACRE NÃO ÍNTEGRO' if not lacre_integro else '') + ('; Observações: '+observacoes.strip() if observacoes.strip() else ''))
            setor=con.execute('SELECT setor FROM usuarios WHERE id=?',(enfermeiro_id,)).fetchone()['setor']
            con.execute('UPDATE bandejas SET localizacao_atual=?,checkin_em=? WHERE id=?',
                        (setor,agora(),b['id']))
            con.execute("UPDATE pedidos SET status='em_atendimento' WHERE id=?",(p['id'],))
            return {'pedido':p['codigo'],'bandeja':b['codigo'],'paciente':paciente['codigo'],'status':'em_atendimento','faltantes':achados,'lacre_integro':lacre_integro}

    def checkout(self, enfermeiro_id, codigo_bandeja, destinatario):
        # O destinatário jurídico do empréstimo é sempre um médico cadastrado;
        # o check-out inicial destina a bandeja ao médico solicitante.
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,enfermeiro_id,'enfermeiro')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='enfermagem' or b['responsavel_id']!=enfermeiro_id or b['bloqueada']:
                raise RegraNegocioError('Bandeja indisponível, bloqueada ou sob outra responsabilidade.')
            p=con.execute('SELECT medico_id FROM pedidos WHERE id=?',(b['pedido_id'],)).fetchone()
            con.execute('UPDATE bandejas SET portador_medico_id=? WHERE id=?',(p['medico_id'],b['id']))
            self._mov(con,b,b['pedido_id'],enfermeiro_id,'checkout_emprestimo','emprestada',enfermeiro_id,
                      f'Entregue ao médico solicitante (ID {p["medico_id"]})')
            setor=con.execute('SELECT setor FROM usuarios WHERE id=?',(p['medico_id'],)).fetchone()['setor']
            con.execute('UPDATE bandejas SET localizacao_atual=? WHERE id=?',(setor,b['id']))
            return {'bandeja':b['codigo'],'status':'emprestada','medico_portador_id':p['medico_id']}

    def _registrar_conferencia(self, con, b, ator_id, fase, faltantes, lacre_integro=True, observacoes="", classificacoes=None):
        itens=self._linhas(con.execute('SELECT id,nome,quantidade FROM itens_bandeja WHERE bandeja_id=?',(b['id'],)))
        mapa={normalizar(i['nome']):i for i in itens}
        vistos=set(); faltas=[]; declarado={}
        for nome,qtd in faltantes:
            key=normalizar(nome)
            if key in vistos or key not in mapa: raise RegraNegocioError(f'Item inválido ou repetido: {nome}')
            vistos.add(key)
            item=mapa[key]
            if not isinstance(qtd,int) or qtd<1 or qtd>item['quantidade']:
                raise RegraNegocioError(f'Quantidade faltante inválida: {nome}')
            declarado[item['id']]=qtd
            faltas.append({'item':item['nome'],'faltante':qtd,'esperado':item['quantidade']})
        classificacoes = classificacoes or {}
        categorias_validas = {'consumido','descartado','nao_localizado','em_apuracao'}
        for item in itens:
            qtd=declarado.get(item['id'],0)
            categoria=classificacoes.get(item['nome'], 'em_apuracao')
            if qtd and categoria not in categorias_validas:
                raise RegraNegocioError('Motivo de diferença inválido.')
            abertas=con.execute('SELECT COALESCE(SUM(quantidade),0) FROM faltas WHERE bandeja_id=? AND item_id=? AND regularizado_em IS NULL',
                               (b['id'],item['id'])).fetchone()[0]
            if qtd>abertas:
                con.execute('''INSERT INTO faltas(bandeja_id,pedido_id,item_id,quantidade,reportado_por,reportado_em,fase)
                               VALUES (?,?,?,?,?,?,?)''', (b['id'],b['pedido_id'],item['id'],qtd-abertas,ator_id,agora(),fase))
        if not isinstance(lacre_integro, bool):
            raise RegraNegocioError('Estado do lacre inválido.')
        if len(observacoes) > 2000:
            raise RegraNegocioError('Observações não podem exceder 2000 caracteres.')
        con.execute('''INSERT INTO conferencias(bandeja_id,pedido_id,ator_id,fase,itens_json,criado_em,lacre_integro,observacoes)
                       VALUES (?,?,?,?,?,?,?,?)''',(b['id'],b['pedido_id'],ator_id,fase,
                           json.dumps([{'nome':i['nome'],'previsto':i['quantidade'],'faltante':declarado.get(i['id'],0)} for i in itens],ensure_ascii=False),agora(),int(lacre_integro),observacoes.strip()))
        cid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        for item in itens:
            qtd=declarado.get(item['id'],0)
            if qtd:
                motivo=classificacoes.get(item['nome'],'em_apuracao')
                con.execute('''INSERT INTO divergencias_classificadas(conferencia_id,bandeja_id,item_id,quantidade,motivo,registrado_por,registrado_em) VALUES(?,?,?,?,?,?,?)''',
                    (cid,b['id'],item['id'],qtd,motivo,ator_id,agora()))
                con.execute('''UPDATE faltas SET categoria=? WHERE bandeja_id=? AND item_id=? AND regularizado_em IS NULL''',
                    (motivo,b['id'],item['id']))
        if not lacre_integro:
            con.execute('INSERT INTO incidentes_lacre(bandeja_id,conferencia_id,registrado_em,observacoes) VALUES (?,?,?,?)',
                        (b['id'],cid,agora(),observacoes.strip()))
        if not lacre_integro or faltas or con.execute('SELECT 1 FROM faltas WHERE bandeja_id=? AND regularizado_em IS NULL LIMIT 1',(b['id'],)).fetchone():
            con.execute('UPDATE bandejas SET bloqueada=1 WHERE id=?',(b['id'],))
        return faltas

    def receber_emprestimo(self, enfermeiro_id, codigo_bandeja, faltantes=None, lacre_integro=True, observacoes="", classificacoes=None):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,enfermeiro_id,'enfermeiro')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='emprestada' or b['responsavel_id']!=enfermeiro_id:
                raise RegraNegocioError('Bandeja não emprestada por esse técnico de enfermagem.')
            if con.execute("SELECT 1 FROM transferencias_pendentes WHERE bandeja_id=? AND status='pendente'",(b['id'],)).fetchone():
                raise RegraNegocioError('Cancele ou conclua a transferência médica pendente antes da devolução.')
            faltas=self._registrar_conferencia(con,b,enfermeiro_id,'retorno_emprestimo',faltantes or [],lacre_integro,observacoes,classificacoes)
            resumo=', '.join(f'{f["faltante"]} x {f["item"]} faltante(s)' for f in faltas) or 'Composição conferida sem faltas informadas'
            resumo += ('; LACRE NÃO ÍNTEGRO' if not lacre_integro else '') + ('; Observações: '+observacoes.strip() if observacoes.strip() else '')
            self._mov(con,b,b['pedido_id'],enfermeiro_id,'recebimento_emprestimo','enfermagem',enfermeiro_id,resumo)
            setor=con.execute('SELECT setor FROM usuarios WHERE id=?',(enfermeiro_id,)).fetchone()['setor']
            con.execute('UPDATE bandejas SET localizacao_atual=? WHERE id=?',(setor,b['id']))
            return {'bandeja':b['codigo'],'status':'enfermagem','itens_faltantes':faltas,'observacao':resumo}

    def enviar_estoque(self, enfermeiro_id, codigo_bandeja):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,enfermeiro_id,'enfermeiro')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='enfermagem' or b['responsavel_id']!=enfermeiro_id:
                raise RegraNegocioError('Bandeja não está sob responsabilidade desse técnico de enfermagem.')
            self._mov(con,b,b['pedido_id'],enfermeiro_id,'envio_estoque','retorno_estoque')
            con.execute("UPDATE bandejas SET localizacao_atual='Em devolução ao estoque' WHERE id=?",(b['id'],))
            return {'bandeja':b['codigo'],'status':'retorno_estoque'}

    def receber_estoque(self, gerente_id, codigo_bandeja, faltantes=None, lacre_integro=True, observacoes="", classificacoes=None):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='retorno_estoque':
                raise RegraNegocioError('Bandeja não aguardando devolução ao estoque.')
            achados=self._registrar_conferencia(con,b,gerente_id,'recebimento_estoque',faltantes or [],lacre_integro,observacoes,classificacoes)
            pedido_id=b['pedido_id']
            abertas = con.execute('SELECT COUNT(*) FROM faltas WHERE bandeja_id=? AND regularizado_em IS NULL',(b['id'],)).fetchone()[0]
            lacres_abertos = con.execute('SELECT COUNT(*) FROM incidentes_lacre WHERE bandeja_id=? AND resolvido_em IS NULL',(b['id'],)).fetchone()[0]
            self._mov(con,b,pedido_id,gerente_id,'entrada_estoque','estoque',observacao=('QUARENTENA: pendência(s) em aberto' if abertas or lacres_abertos else 'Conferida') + ('; LACRE NÃO ÍNTEGRO' if not lacre_integro else '') + ('; Observações: '+observacoes.strip() if observacoes.strip() else ''))
            con.execute("UPDATE bandejas SET portador_medico_id=NULL,saida_estoque_em=NULL,checkin_em=NULL,localizacao_atual='Arsenal',tipo_id=(SELECT id FROM tipos WHERE nome='Arsenal') WHERE id=?",(b['id'],))
            con.execute("UPDATE pedidos SET status='finalizado',finalizado_em=? WHERE id=?",
                        (agora(),pedido_id))
            alerta=self._alerta_tipo(con,b['tipo_id'])
            return {'bandeja':b['codigo'],'status':'estoque_bloqueado' if abertas or lacres_abertos else 'estoque',
                    'pendencias':abertas+lacres_abertos,'faltantes':achados,'alerta_estoque':alerta}

    def pendencias(self, codigo_bandeja=None):
        with self._conexao() as con:
            sql = '''SELECT f.id AS falta_id,b.codigo AS bandeja,t.nome AS tipo,i.nome AS item,
                i.quantidade AS previsto,f.quantidade AS faltante,
                i.quantidade-f.quantidade AS devolvido,f.reportado_em
                FROM faltas f JOIN bandejas b ON b.id=f.bandeja_id
                JOIN tipos t ON t.id=b.tipo_id JOIN itens_bandeja i ON i.id=f.item_id
                WHERE f.regularizado_em IS NULL'''
            params=[]
            if codigo_bandeja:
                sql += ' AND b.codigo=?'
                params.append(codigo_bandeja.strip().upper())
            return self._linhas(con.execute(sql+' ORDER BY f.id',params))

    def regularizar_item(self, gerente_id, codigo_bandeja, item_nome, quantidade):
        # Confirma recomposição física; não comprova esterilização.
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='estoque' or not b['bloqueada']:
                raise RegraNegocioError('Bandeja deve estar no estoque e bloqueada por pendência.')
            falta = con.execute('''SELECT f.*,i.nome FROM faltas f JOIN itens_bandeja i ON i.id=f.item_id
                    WHERE f.bandeja_id=? AND f.regularizado_em IS NULL''',(b['id'],)).fetchall()
            correspondentes = [f for f in falta if normalizar(f['nome'])==normalizar(item_nome)]
            if len(correspondentes)!=1 or quantidade!=correspondentes[0]['quantidade']:
                raise RegraNegocioError('Item não tem falta aberta ou quantidade difere da pendência.')
            f=correspondentes[0]
            con.execute('UPDATE faltas SET regularizado_por=?, regularizado_em=? WHERE id=?',(gerente_id,agora(),f['id']))
            restantes=con.execute('SELECT COUNT(*) FROM faltas WHERE bandeja_id=? AND regularizado_em IS NULL',(b['id'],)).fetchone()[0]
            lacre_pendente=con.execute('SELECT 1 FROM incidentes_lacre WHERE bandeja_id=? AND resolvido_em IS NULL LIMIT 1',(b['id'],)).fetchone()
            if restantes==0 and not lacre_pendente:
                con.execute('UPDATE bandejas SET bloqueada=0 WHERE id=?',(b['id'],))
            self._mov(con,b,None,gerente_id,'regularizacao_item','estoque',observacao=f'Reposição confirmada: {quantidade} x {f["nome"]}. Não equivale a liberação sanitária.')
            return {'bandeja':b['codigo'],'item':f['nome'],'quantidade_regularizada':quantidade,
                    'pendencias_restantes':restantes,'bloqueada':bool(restantes),'alerta_estoque':self._alerta_tipo(con,b['tipo_id'])}

    def resolver_lacre(self, gerente_id, codigo_bandeja, observacoes):
        if not observacoes.strip():
            raise RegraNegocioError('Informe a avaliação e providências tomadas antes de encerrar a ocorrência do lacre.')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='estoque':
                raise RegraNegocioError('A bandeja precisa estar no estoque para resolver pendências do lacre.')
            abertas=con.execute('SELECT COUNT(*) FROM incidentes_lacre WHERE bandeja_id=? AND resolvido_em IS NULL',(b['id'],)).fetchone()[0]
            if not abertas: raise RegraNegocioError('Não há ocorrência de lacre pendente.')
            con.execute('UPDATE incidentes_lacre SET resolvido_em=?, resolvido_por=?, observacoes=observacoes||? WHERE bandeja_id=? AND resolvido_em IS NULL',
                        (agora(),gerente_id,' | Resolução: '+observacoes.strip(),b['id']))
            faltas=con.execute('SELECT COUNT(*) FROM faltas WHERE bandeja_id=? AND regularizado_em IS NULL',(b['id'],)).fetchone()[0]
            if not faltas: con.execute('UPDATE bandejas SET bloqueada=0 WHERE id=?',(b['id'],))
            self._mov(con,b,None,gerente_id,'resolucao_lacre','estoque',observacao=observacoes.strip()+'; requer liberação sanitária independente')
            return {'bandeja':b['codigo'],'faltas_restantes':faltas}

    def transferir_medico(self, medico_origem_id, codigo_bandeja, medico_destino_id):
        # Solicita transferência e aguarda aceite bilateral.
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            origem=self._ator(con,medico_origem_id,'medico')
            destino=self._ator(con,int(medico_destino_id),'medico')
            if origem['id']==destino['id']:
                raise RegraNegocioError('Informe outro médico.')
            b=self._bandeja(con,codigo_bandeja)
            if b['etapa']!='emprestada' or b['bloqueada'] or b['portador_medico_id']!=origem['id']:
                raise RegraNegocioError('Apenas o portador atual pode solicitar transferência de bandeja emprestada e regular.')
            try:
                tid=con.execute('''INSERT INTO transferencias_pendentes
                    (bandeja_id,pedido_id,origem_id,destino_id,solicitado_em)
                    VALUES(?,?,?,?,?)''',(b['id'],b['pedido_id'],origem['id'],destino['id'],agora())).lastrowid
            except sqlite3.IntegrityError as exc:
                raise RegraNegocioError('Já existe uma transferência aguardando aceite para esta bandeja.') from exc
            con.execute('''INSERT INTO movimentos(bandeja_id,pedido_id,ator_id,acao,origem,destino,
                responsavel_anterior_id,responsavel_novo_id,observacao,criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',(b['id'],b['pedido_id'],origem['id'],'transferencia_solicitada',
                b['etapa'],b['etapa'],origem['id'],origem['id'],f'Convite #{tid} enviado a {destino["nome"]}; custódia mantida',agora()))
            return {'transferencia_id':tid,'bandeja':b['codigo'],'origem':origem['nome'],
                    'destino':destino['nome'],'status':'pendente','portador_atual':origem['nome']}

    def transferencias(self, medico_id):
        with self._conexao() as con:
            self._ator(con,medico_id,'medico')
            return self._linhas(con.execute('''SELECT x.*, b.codigo AS bandeja, t.nome AS tipo,
                o.nome AS origem, d.nome AS destino, p.codigo AS pedido
                FROM transferencias_pendentes x JOIN bandejas b ON b.id=x.bandeja_id
                JOIN tipos t ON t.id=b.tipo_id JOIN usuarios o ON o.id=x.origem_id
                JOIN usuarios d ON d.id=x.destino_id JOIN pedidos p ON p.id=x.pedido_id
                WHERE (x.origem_id=? OR x.destino_id=?) AND x.status='pendente'
                ORDER BY x.id DESC''',(medico_id,medico_id)))

    def decidir_transferencia(self, medico_id, transferencia_id, aceitar, codigo_bandeja, observacao=''):
        if len(observacao)>1000: raise RegraNegocioError('Observação muito longa.')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            medico=self._ator(con,medico_id,'medico')
            x=con.execute('SELECT * FROM transferencias_pendentes WHERE id=?',(int(transferencia_id),)).fetchone()
            if not x or x['status']!='pendente' or x['destino_id']!=medico['id']:
                raise RegraNegocioError('Transferência não encontrada, já finalizada ou destinada a outro médico.')
            b=self._bandeja(con,codigo_bandeja)
            if b['id']!=x['bandeja_id'] or b['etapa']!='emprestada' or b['portador_medico_id']!=x['origem_id']:
                raise RegraNegocioError('Leia o código da bandeja correta. Posse atual inválida.')
            if aceitar and b['bloqueada']:
                raise RegraNegocioError('Bandeja bloqueada. Recuse e informe a divergência para conferência.')
            novo='aceita' if aceitar else 'recusada'
            con.execute('UPDATE transferencias_pendentes SET status=?,concluido_em=?,observacao=? WHERE id=?',
                (novo,agora(),observacao.strip(),x['id']))
            if aceitar:
                con.execute('UPDATE bandejas SET portador_medico_id=?,localizacao_atual=? WHERE id=?',
                    (medico_id,medico['setor'] or 'Médico portador',b['id']))
            con.execute('''INSERT INTO movimentos(bandeja_id,pedido_id,ator_id,acao,origem,destino,
                responsavel_anterior_id,responsavel_novo_id,observacao,criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',(b['id'],x['pedido_id'],medico_id,
                'transferencia_aceita' if aceitar else 'transferencia_recusada',b['etapa'],b['etapa'],x['origem_id'],
                medico_id if aceitar else x['origem_id'],f'Local: {medico["setor"]}; '+observacao.strip(),agora()))
            return {'status':novo,'bandeja':b['codigo'],'portador_atual_id':medico_id if aceitar else x['origem_id']}

    def cancelar_transferencia(self, medico_id, transferencia_id):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,medico_id,'medico')
            x=con.execute('SELECT * FROM transferencias_pendentes WHERE id=?',(int(transferencia_id),)).fetchone()
            if not x or x['origem_id']!=medico_id or x['status']!='pendente':
                raise RegraNegocioError('Apenas o remetente pode cancelar transferência pendente.')
            con.execute("UPDATE transferencias_pendentes SET status='cancelada',concluido_em=? WHERE id=?",(agora(),x['id']))
            b=con.execute('SELECT etapa FROM bandejas WHERE id=?',(x['bandeja_id'],)).fetchone()
            con.execute('''INSERT INTO movimentos(bandeja_id,pedido_id,ator_id,acao,origem,destino,
                responsavel_anterior_id,responsavel_novo_id,observacao,criado_em)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',(x['bandeja_id'],x['pedido_id'],medico_id,
                'transferencia_cancelada',b['etapa'],b['etapa'],medico_id,medico_id,
                'Solicitação de transferência cancelada pelo remetente',agora()))
            return {'status':'cancelada'}

    def iniciar_inventario(self, gerente_id, localizacao):
        localizacao=localizacao.strip()
        if not localizacao or len(localizacao)>120: raise RegraNegocioError('Informe um local válido.')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            iid=con.execute('INSERT INTO inventarios_rotativos(localizacao,iniciado_por,iniciado_em) VALUES(?,?,?)',
                (localizacao,gerente_id,agora())).lastrowid
            con.execute('INSERT INTO inventario_esperados(inventario_id,bandeja_id) SELECT ?,id FROM bandejas WHERE localizacao_atual=?',(iid,localizacao))
            return {'inventario_id':iid,'localizacao':localizacao}

    def bipar_inventario(self, gerente_id, inventario_id, codigo):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            inv=con.execute('SELECT * FROM inventarios_rotativos WHERE id=?',(int(inventario_id),)).fetchone()
            if not inv or inv['encerrado_em']: raise RegraNegocioError('Inventário não encontrado ou já encerrado.')
            b=self._bandeja(con,codigo)
            try:
                con.execute('INSERT INTO inventario_leituras(inventario_id,bandeja_id,ator_id,lido_em) VALUES(?,?,?,?)',
                    (inv['id'],b['id'],gerente_id,agora()))
            except sqlite3.IntegrityError as exc:
                raise RegraNegocioError('Bandeja já registrada neste inventário.') from exc
            return {'bandeja':b['codigo'],'localizacao_registrada':b['localizacao_atual'],
                    'conforme':b['localizacao_atual']==inv['localizacao']}

    def consultar_inventario(self, gerente_id, inventario_id):
        with self._conexao() as con:
            self._ator(con,gerente_id,'gerente_estoque')
            inv=con.execute('SELECT * FROM inventarios_rotativos WHERE id=?',(int(inventario_id),)).fetchone()
            if not inv: raise RegraNegocioError('Inventário inexistente.')
            base='''SELECT b.codigo FROM inventario_esperados e JOIN bandejas b ON b.id=e.bandeja_id
                WHERE e.inventario_id=?'''
            expected={r['codigo'] for r in con.execute(base,(inv['id'],))}
            found={r['codigo'] for r in con.execute('''SELECT b.codigo FROM inventario_leituras l
                JOIN bandejas b ON b.id=l.bandeja_id WHERE l.inventario_id=?''',(inv['id'],))}
            return {'id':inv['id'],'localizacao':inv['localizacao'],'encerrado':bool(inv['encerrado_em']),
                    'esperados':len(expected),'lidos':len(found),'conciliados':sorted(expected & found),
                    'nao_localizados':sorted(expected-found),'nao_previstos':sorted(found-expected)}

    def encerrar_inventario(self, gerente_id, inventario_id):
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            row=con.execute('SELECT * FROM inventarios_rotativos WHERE id=?',(int(inventario_id),)).fetchone()
            if not row or row['encerrado_em']: raise RegraNegocioError('Inventário já encerrado ou inexistente.')
            con.execute('UPDATE inventarios_rotativos SET encerrado_em=?,encerrado_por=? WHERE id=?',
                (agora(),gerente_id,int(inventario_id)))
        return self.consultar_inventario(gerente_id,inventario_id)

    def listar_inventarios(self, gerente_id):
        with self._conexao() as con:
            self._ator(con,gerente_id,'gerente_estoque')
            return self._linhas(con.execute('SELECT * FROM inventarios_rotativos ORDER BY id DESC LIMIT 30'))

    def divergencias(self, gerente_id):
        with self._conexao() as con:
            self._ator(con,gerente_id,'gerente_estoque')
            return self._linhas(con.execute('''SELECT d.*,b.codigo AS bandeja,i.nome AS item,u.nome AS registrado_por_nome
                FROM divergencias_classificadas d JOIN bandejas b ON b.id=d.bandeja_id
                JOIN itens_bandeja i ON i.id=d.item_id JOIN usuarios u ON u.id=d.registrado_por
                ORDER BY d.id DESC LIMIT 200'''))

    def emprestimos_ativos(self, medico_id=None):
        """Tempo de uso desde o check-in até o retorno físico ao estoque.

        A separação não inicia o relógio. O filtro médico é o portador atual.
        """
        with self._conexao() as con:
            parametros=[]
            filtro=''
            if medico_id is not None:
                self._ator(con,medico_id,'medico')
                filtro=' AND b.portador_medico_id=?'
                parametros.append(medico_id)
            rows=self._linhas(con.execute("""SELECT b.codigo,b.id AS bandeja_id,b.etapa,t.nome AS tipo,
                p.codigo AS pedido,p.medico_id, u.nome AS medico,
                b.portador_medico_id, port.nome AS portador,
                b.checkin_em AS retirado_em, b.saida_estoque_em, b.responsavel_id
                FROM bandejas b JOIN pedidos p ON p.id=b.pedido_id
                JOIN tipos t ON t.id=b.tipo_id JOIN usuarios u ON u.id=p.medico_id
                LEFT JOIN usuarios port ON port.id=b.portador_medico_id
                WHERE b.etapa IN ('separada','enfermagem','emprestada','retorno_estoque')
                AND b.checkin_em IS NOT NULL """+filtro+
                ' ORDER BY b.checkin_em',parametros))
            instante=datetime.now(timezone.utc)
            for r in rows:
                t0=datetime.fromisoformat(r['retirado_em'].replace('Z','+00:00'))
                r['horas_emprestimo']=round(max(0,(instante-t0).total_seconds()/3600),4)
                r['limite_horas']=LIMITE_EMPRESTIMO_HORAS
                r['atrasado']=r['horas_emprestimo'] >= LIMITE_EMPRESTIMO_HORAS
                proporcao=r['horas_emprestimo']/LIMITE_EMPRESTIMO_HORAS
                r['nivel_alerta']=('investigacao' if proporcao>=2 else
                    'escalonamento' if proporcao>=1.25 else
                    'atrasado' if proporcao>=1 else
                    'preventivo' if proporcao>=.80 else 'normal')
            return rows

    def _alerta_tipo(self, con, tipo_id):
        # Compatibilidade com chamadas antigas; alertas são por padrão, não por setor.
        return None

    def alertas(self):
        with self._conexao() as con:
            rows=con.execute("""SELECT pc.cor_1,pc.cor_2,
                COUNT(CASE WHEN b.etapa='estoque' AND b.localizacao_atual=? AND b.bloqueada=0 THEN 1 END) qtd
                FROM padroes_cores pc LEFT JOIN bandejas b
                  ON b.cor_1=pc.cor_1 AND b.cor_2=pc.cor_2
                WHERE EXISTS (SELECT 1 FROM bandejas bx WHERE bx.cor_1=pc.cor_1 AND bx.cor_2=pc.cor_2)
                GROUP BY pc.id""",(LOCAL_ESTOQUE,)).fetchall()
            return [{'tipo':r['cor_1']+' + '+r['cor_2'],'disponiveis':r['qtd'],
                     'mensagem':'Estoque baixo do padrão '+r['cor_1']+' + '+r['cor_2']}
                     for r in rows if r['qtd']<=LIMITE_ALERTA]

    def catalogo_composicoes(self):
        """Apenas padrões e quantidades por tipo, sem expor identificadores individuais."""
        with self._conexao() as con:
            linhas=con.execute("""SELECT b.id,b.tipo_id,t.nome AS tipo,b.cor_1,b.cor_2,b.etapa,b.bloqueada,
                               i.nome AS item,i.quantidade AS quantidade
                               FROM bandejas b JOIN tipos t ON t.id=b.tipo_id
                               JOIN itens_bandeja i ON i.bandeja_id=b.id
                               ORDER BY t.nome,b.id,i.nome""").fetchall()
            grupos={}
            for r in linhas:
                chave=(r['tipo_id'],r['cor_1'],r['cor_2'])
                g=grupos.setdefault(chave,{'tipo':r['tipo'],'cor_1':r['cor_1'],'cor_2':r['cor_2'],
                    'bandejas_cadastradas':set(),'bandejas_disponiveis':set(),'itens':{},'total_itens':0})
                g['bandejas_cadastradas'].add(r['id'])
                if r['etapa']=='estoque' and not r['bloqueada']:
                    g['bandejas_disponiveis'].add(r['id'])
                # Uma composição é a mesma em todas as bandejas do padrão.
                g['itens'][r['item']]=r['quantidade']
            resultado=[]
            for g in grupos.values():
                g['bandejas_cadastradas']=len(g['bandejas_cadastradas'])
                g['bandejas_disponiveis']=len(g['bandejas_disponiveis'])
                g['itens']=[{'nome':n,'quantidade':q} for n,q in sorted(g['itens'].items())]
                g['total_itens']=sum(x['quantidade'] for x in g['itens'])
                resultado.append(g)
            return resultado

    def resumo_padroes_por_setor(self):
        """Catálogo do Arsenal reutilizável em cada setor; contagens são globais.

        Cada setor pode solicitar qualquer composição disponível, sem reserva fixa.
        """
        with self._conexao() as con:
            rows=con.execute("""SELECT pc.id padrao_id,pc.cor_1,pc.cor_2,pc.itens_json,
                  COUNT(b.id) total_cadastradas,
                  SUM(CASE WHEN b.etapa='estoque' AND b.localizacao_atual=? THEN 1 ELSE 0 END) no_estoque,
                  SUM(CASE WHEN b.etapa='estoque' AND b.localizacao_atual=? AND b.bloqueada=0 THEN 1 ELSE 0 END) disponiveis,
                  SUM(CASE WHEN b.bloqueada=1 THEN 1 ELSE 0 END) bloqueadas
                FROM bandejas b JOIN padroes_cores pc ON pc.cor_1=b.cor_1 AND pc.cor_2=b.cor_2
                GROUP BY pc.id ORDER BY pc.id""",(LOCAL_ESTOQUE,LOCAL_ESTOQUE)).fetchall()
            resultado=[]
            for r in rows:
                base=dict(r)
                base['itens']=[{'nome':n,'quantidade':int(q)} for n,q in json.loads(base.pop('itens_json'))]
                base['total_itens']=sum(i['quantidade'] for i in base['itens'])
                for setor in SETORES_ATENDIMENTO:
                    resultado.append(dict(base,setor=setor))
            return resultado

    def inventario_materiais_estoque(self):
        """Agrega somente bandejas fisicamente no estoque; faltas abertas reduzem contagem estimada."""
        with self._conexao() as con:
            linhas=con.execute("""SELECT i.nome,i.quantidade AS previsto,b.id AS bandeja_id,b.bloqueada,
                             COALESCE((SELECT SUM(f.quantidade) FROM faltas f
                                 WHERE f.item_id=i.id AND f.regularizado_em IS NULL),0) AS falta_aberta
                             FROM bandejas b JOIN itens_bandeja i ON i.bandeja_id=b.id
                             WHERE b.etapa='estoque' ORDER BY i.nome,b.id""").fetchall()
            resumo={}
            bandejas=set()
            bloqueadas=set()
            for r in linhas:
                bandejas.add(r['bandeja_id'])
                if r['bloqueada']: bloqueadas.add(r['bandeja_id'])
                chave=normalizar(r['nome'])
                reg=resumo.setdefault(chave,{'nome':r['nome'],'previsto':0,'faltante':0,'estimado_presente':0})
                reg['previsto']+=r['previsto']
                falta=min(r['previsto'],r['falta_aberta'])
                reg['faltante']+=falta
                reg['estimado_presente']+=r['previsto']-falta
            itens=sorted(resumo.values(),key=lambda x: normalizar(x['nome']))
            return {'bandejas_no_estoque':len(bandejas),'bandejas_bloqueadas':len(bloqueadas),
                    'itens':itens,'total_previsto':sum(i['previsto'] for i in itens),
                    'total_faltante':sum(i['faltante'] for i in itens),
                    'total_estimado_presente':sum(i['estimado_presente'] for i in itens)}

    def atualizar_setor_usuario(self, gerente_id, usuario_id, novo_setor):
        """Correção explícita do setor de contas migradas; não altera setor histórico dos pedidos."""
        novo_setor=setor_canonico(novo_setor)
        if not novo_setor: raise RegraNegocioError('Selecione um dos quatro setores de atendimento.')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            self._ator(con,gerente_id,'gerente_estoque')
            u=con.execute('SELECT * FROM usuarios WHERE id=? AND ativo=1',(usuario_id,)).fetchone()
            if not u or u['papel'] not in ('medico','enfermeiro'):
                raise RegraNegocioError('Profissional médico/técnico de enfermagem ativo não encontrado.')
            con.execute('UPDATE usuarios SET setor=? WHERE id=?',(novo_setor,usuario_id))
            return {'usuario_id':usuario_id,'setor':novo_setor}

    def cadastrar_paciente(self, enfermeiro_id, nome):
        """Cadastro mínimo por setor; evitar CPF, prontuário e outros dados reais no piloto."""
        nome=nome.strip()
        if len(nome)<2 or len(nome)>120:
            raise RegraNegocioError('Informe a identificação do paciente (2 a 120 caracteres).')
        with self._conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            enfermeiro=self._ator(con,enfermeiro_id,'enfermeiro')
            setor=setor_canonico(enfermeiro['setor'])
            if not setor: raise RegraNegocioError('Técnico de enfermagem não possui setor válido.')
            codigo='PAC-'+uuid4().hex[:10].upper()
            pid=con.execute("""INSERT INTO pacientes(codigo,nome,setor,ficticio,cadastrado_por,criado_em)
                VALUES (?,?,?,0,?,?)""",(codigo,nome,setor,enfermeiro_id,agora())).lastrowid
            return {'id':pid,'codigo':codigo,'setor':setor}

    def listar_pacientes(self, enfermeiro_id):
        with self._conexao() as con:
            u=self._ator(con,enfermeiro_id,'enfermeiro')
            setor=setor_canonico(u['setor'])
            return self._linhas(con.execute("""SELECT id,codigo,nome,setor,ficticio FROM pacientes
                WHERE ativo=1 AND setor=? ORDER BY id""",(setor,)))

    def pacientes_do_pedido(self, enfermeiro_id, codigo_pedido):
        with self._conexao() as con:
            u=self._ator(con,enfermeiro_id,'enfermeiro')
            p=self._pedido_codigo(con,codigo_pedido)
            if normalizar(u['setor'])!=normalizar(p['setor_atendimento']):
                raise RegraNegocioError('A solicitação pertence a outro setor.')
            return self._linhas(con.execute("""SELECT id,codigo,nome,setor,ficticio FROM pacientes
                WHERE ativo=1 AND setor=? ORDER BY id""",(p['setor_atendimento'],)))

    def listar_tipos(self):
        with self._conexao() as con:
            return self._linhas(con.execute('SELECT * FROM tipos ORDER BY id'))

    def listar_usuarios(self):
        with self._conexao() as con:
            return self._linhas(con.execute('SELECT * FROM usuarios ORDER BY id'))

    def listar_pedidos(self):
        with self._conexao() as con:
            return self._linhas(con.execute('''SELECT p.*,t.nome AS tipo,u.nome AS medico,
                COALESCE(p.setor_atendimento,u.setor) AS setor_solicitante,
                pa.codigo AS paciente_codigo, pa.nome AS paciente_nome,
                pc.cor_1 AS cor_solicitada_1, pc.cor_2 AS cor_solicitada_2
                FROM pedidos p JOIN tipos t ON t.id=p.tipo_id
                JOIN usuarios u ON u.id=p.medico_id
                LEFT JOIN pacientes pa ON pa.id=p.paciente_id
                LEFT JOIN padroes_cores pc ON pc.id=p.padrao_cores_id ORDER BY p.id'''))

    def listar_bandejas(self):
        with self._conexao() as con:
            bandejas=self._linhas(con.execute('''SELECT b.*,t.nome AS tipo,u.nome AS responsavel
                FROM bandejas b JOIN tipos t ON t.id=b.tipo_id
                LEFT JOIN usuarios u ON u.id=b.responsavel_id ORDER BY b.id'''))
            for b in bandejas:
                b['itens']=self._linhas(con.execute('SELECT id,nome,quantidade FROM itens_bandeja WHERE bandeja_id=? ORDER BY id',(b['id'],)))
            return bandejas

    def historico(self, codigo_bandeja):
        with self._conexao() as con:
            b=self._bandeja(con,codigo_bandeja)
            return self._linhas(con.execute('''SELECT m.*,u.nome AS ator FROM movimentos m
                JOIN usuarios u ON u.id=m.ator_id WHERE m.bandeja_id=? ORDER BY m.id''',(b['id'],)))


def main(argv=None):
    parser=argparse.ArgumentParser(description='Rastreabilidade de bandejas hospitalares v4 (protótipo)')
    parser.add_argument('--db',default=str(Path(__file__).resolve().parent / 'bandejas_v4.db'),
                        help='Caminho do banco SQLite (padrão: pasta de bandejas.py)')
    sub=parser.add_subparsers(dest='comando',required=True)
    def cmd(nome,*campos):
        p=sub.add_parser(nome)
        for campo in campos:
            p.add_argument(campo,type=int if campo.endswith('_id') else str)
        return p
    u=cmd('novo-usuario','nome','setor','papel');u.add_argument('--gerente-id',type=int)
    t=cmd('novo-tipo','gerente_id','nome');t.add_argument('--descricao',default='');t.add_argument('--cor-1');t.add_argument('--cor-2')
    b=cmd('nova-bandeja','gerente_id','codigo','tipo');b.add_argument('cor_1',nargs='?');b.add_argument('cor_2',nargs='?')
    b.add_argument('--item',action='append',required=True,metavar='NOME:QUANTIDADE',
                   help='Repetir para cada item, ex.: --item "Gaze:2" --item "Bisturi:1"')
    cmd('solicitar','medico_id','tipo_ou_frase')
    cmd('separar','gerente_id','codigo_pedido','codigo_bandeja')
    cmd('checkin-pedido','enfermeiro_id','codigo_pedido','codigo_bandeja')
    cmd('checkout','enfermeiro_id','codigo_bandeja','destinatario')
    r=cmd('receber-emprestimo','enfermeiro_id','codigo_bandeja');r.add_argument('--faltante',action='append',default=[],metavar='ITEM:QUANTIDADE')
    cmd('enviar-estoque','enfermeiro_id','codigo_bandeja')
    cmd('receber-estoque','gerente_id','codigo_bandeja')
    reg=cmd('regularizar','gerente_id','codigo_bandeja');reg.add_argument('--item',required=True,metavar='NOME:QUANTIDADE')
    pen=sub.add_parser('pendencias');pen.add_argument('codigo_bandeja',nargs='?')
    for nome in ('usuarios','tipos','pedidos','bandejas','alertas'): sub.add_parser(nome)
    cmd('historico','codigo_bandeja')
    args=parser.parse_args(argv)
    app=SistemaBandejas(args.db)
    a=args
    try:
        def ler_itens(textos):
            itens=[]
            for item in textos:
                if ':' not in item:
                    raise RegraNegocioError('Use "Nome:quantidade".')
                nome,qtd=item.rsplit(':',1)
                try: qtd=int(qtd)
                except ValueError as e: raise RegraNegocioError('Quantidade precisa ser inteira.') from e
                itens.append((nome,qtd))
            return itens
        if a.comando=='nova-bandeja':
            itens=ler_itens(a.item)
            resultado=app.cadastrar_bandeja(a.gerente_id,a.codigo,a.tipo,a.cor_1,a.cor_2,itens)
        elif a.comando=='regularizar':
            itens=ler_itens([a.item])
            resultado=app.regularizar_item(a.gerente_id,a.codigo_bandeja,*itens[0])
        else:
            operacoes={
                'novo-usuario':lambda:app.cadastrar_usuario(a.nome,a.setor,a.papel,a.gerente_id),
                'novo-tipo':lambda:app.cadastrar_tipo(a.gerente_id,a.nome,a.descricao,a.cor_1,a.cor_2),
                'solicitar':lambda:app.solicitar(a.medico_id,a.tipo_ou_frase),
                'separar':lambda:app.separar_estoque(a.gerente_id,a.codigo_pedido,a.codigo_bandeja),
                'checkin-pedido':lambda:app.checkin_pedido(a.enfermeiro_id,a.codigo_pedido,a.codigo_bandeja),
                'checkout':lambda:app.checkout(a.enfermeiro_id,a.codigo_bandeja,a.destinatario),
                'receber-emprestimo':lambda:app.receber_emprestimo(a.enfermeiro_id,a.codigo_bandeja,ler_itens(a.faltante)),
                'enviar-estoque':lambda:app.enviar_estoque(a.enfermeiro_id,a.codigo_bandeja),
                'receber-estoque':lambda:app.receber_estoque(a.gerente_id,a.codigo_bandeja),
                'usuarios':app.listar_usuarios,'tipos':app.listar_tipos,'pedidos':app.listar_pedidos,
                'bandejas':app.listar_bandejas,'alertas':app.alertas,
                'historico':lambda:app.historico(a.codigo_bandeja),
                'pendencias':lambda:app.pendencias(a.codigo_bandeja),
            }
            resultado=operacoes[a.comando]()
        print(json.dumps(resultado,ensure_ascii=False,indent=2))
        if a.comando in ('separar','receber-estoque','regularizar') and isinstance(resultado,dict) and resultado.get('alerta_estoque'):
            print('\n' + resultado['alerta_estoque']['mensagem'],file=sys.stderr)
    except (RegraNegocioError,sqlite3.IntegrityError) as e:
        parser.exit(1,f'Erro: {e}\n')

if __name__=='__main__':
    main()
