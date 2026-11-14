"""Interface web local (biblioteca padrão Python) sobre o motor de negócios v4.
Executar: python web_app.py ; abrir http://127.0.0.1:8000
Protótipo; não publicar diretamente na internet.
"""
from __future__ import annotations
import hashlib
import hmac
import html
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from urllib.parse import parse_qs, urlencode, urlparse

from bandejas import SistemaBandejas, RegraNegocioError, PAPEIS, ean13_valido, LIMITE_EMPRESTIMO_HORAS, SETORES_ATENDIMENTO, setor_canonico

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('BANDEJAS_DB', ROOT / 'bandejas_web.db')).resolve()
HOST = '127.0.0.1'
PORT = int(os.environ.get('BANDEJAS_PORT', '8000'))
ENGINE = SistemaBandejas(DB)
SESSIONS = {}
LOCK = RLock()


def db():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_auth():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS fotos_conferencia (
            id INTEGER PRIMARY KEY, bandeja_id INTEGER NOT NULL REFERENCES bandejas(id),
            autor_id INTEGER NOT NULL REFERENCES usuarios(id), acao TEXT NOT NULL,
            nome TEXT NOT NULL, mime TEXT NOT NULL, imagem BLOB NOT NULL,
            criado_em TEXT NOT NULL)""")
        con.execute('''CREATE TABLE IF NOT EXISTS contas (
            usuario_id INTEGER PRIMARY KEY REFERENCES usuarios(id),
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            senha_salt TEXT NOT NULL,
            senha_hash TEXT NOT NULL,
            iteracoes INTEGER NOT NULL DEFAULT 260000,
            aprovado INTEGER NOT NULL DEFAULT 0 CHECK(aprovado IN (0,1)),
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def hash_password(password, salt=None, iterations=260000):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return salt.hex(), digest.hex(), iterations


def authenticate(email, password):
    with db() as con:
        row = con.execute('''SELECT c.*,u.nome,u.papel,u.ativo,u.setor FROM contas c
                JOIN usuarios u ON u.id=c.usuario_id WHERE c.email=?''', (email.strip(),)).fetchone()
    if not row:
        return None
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(row['senha_salt']), row['iteracoes']).hex()
    if not hmac.compare_digest(digest, row['senha_hash']) or not row['ativo'] or not row['aprovado']:
        return None
    return dict(row)


def register(name, email, password, sector, role):
    if not 2 <= len(name.strip()) <= 100 or (role != 'gerente_estoque' and not sector.strip()) or role not in PAPEIS:
        raise ValueError('Informe nome, setor e perfil válidos.')
    if role in ('medico','enfermeiro'):
        sector=setor_canonico(sector)
        if sector is None: raise ValueError('Selecione Enfermaria, CTI, Pronto Atendimento ou Bloco Cirúrgico.')
    if len(password) < 8 or len(password) > 256:
        raise ValueError('A senha deve ter entre 8 e 256 caracteres.')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email.strip()) or len(email) > 200:
        raise ValueError('Informe um e-mail válido.')
    salt, digest, rounds = hash_password(password)
    with LOCK, db() as con:
        con.execute('BEGIN IMMEDIATE')
        first = con.execute('SELECT COUNT(*) FROM contas').fetchone()[0] == 0
        if first and role != 'gerente_estoque':
            raise ValueError('O primeiro cadastro deve ser de um gerente de estoque.')
        user_id = con.execute('INSERT INTO usuarios(nome,setor,papel) VALUES (?,?,?)',
                              (name.strip(), sector.strip(), role)).lastrowid
        try:
            con.execute('''INSERT INTO contas(usuario_id,email,senha_salt,senha_hash,iteracoes,aprovado)
                        VALUES (?,?,?,?,?,?)''', (user_id,email.strip().lower(),salt,digest,rounds,int(first)))
        except sqlite3.IntegrityError as exc:
            raise ValueError('E-mail já cadastrado.') from exc
        return first


def esc(value):
    return html.escape(str(value if value is not None else ''), quote=True)


def tag(text, cls=''):
    return f'<span class="tag {esc(cls)}">{esc(text)}</span>'


ROLE_LABEL = {'medico':'Médico','enfermeiro':'Técnico de enfermagem','gerente_estoque':'Gerente de estoque'}
STATUS_LABEL = {'estoque':'No estoque','separada':'Separada','enfermagem':'Com técnico de enfermagem','emprestada':'Emprestada',
                'retorno_estoque':'Retorno ao estoque','aberto':'Aberto','separado':'Separado',
                'em_atendimento':'Em atendimento','finalizado':'Finalizado'}
COLOR_HEX = {'azul':'#4776dc','verde':'#36a878','vermelho':'#e56368','amarelo':'#f3c65e', 'laranja':'#f28c28','roxo':'#9056c6','branco':'#ffffff','preto':'#20252d'}


def status_badge(st, blocked=False):
    """Localização física e impedimento de uso são informações independentes.

    Uma bandeja recebida pelo gerente permanece 'No estoque' mesmo quando
    possui faltas ou incidentes de lacre que impedem novo empréstimo.
    """
    kind = 'ok' if st in ('estoque','finalizado') else 'warning' if st in ('separada','retorno_estoque','aberto') else 'info'
    state = tag(STATUS_LABEL.get(st, st), kind)
    return state + (' ' + tag('Bloqueada • pendência', 'danger') if blocked else '')


def barcode_svg(code):
    """EAN-13 legítimo e legível por leitores ópticos; desenho sem bibliotecas externas."""
    code=str(code)
    if not ean13_valido(code): return f'<b class="mono">{esc(code)}</b>'
    L=('0001101','0011001','0010011','0111101','0100011','0110001','0101111','0111011','0110111','0001011')
    G=('0100111','0110011','0011011','0100001','0011101','0111001','0000101','0010001','0001001','0010111')
    R=('1110010','1100110','1101100','1000010','1011100','1001110','1010000','1000100','1001000','1110100')
    parity=('LLLLLL','LLGLGG','LLGGLG','LLGGGL','LGLLGG','LGGLLG','LGGGLL','LGLGLG','LGLGGL','LGGLGL')
    bars='101'+''.join((L if pa=='L' else G)[int(d)] for pa,d in zip(parity[int(code[0])],code[1:7]))+'01010'+''.join(R[int(d)] for d in code[7:])+'101'
    rects=''.join(f'<rect x="{10+i*2}" y="2" width="2" height="{59 if i<3 or 45<=i<50 or i>=92 else 52}"/>' for i,c in enumerate(bars) if c=='1')
    return (f'<svg role="img" aria-label="Código de barras {esc(code)}" viewBox="0 0 210 82" class="barcode" '
            f'xmlns="http://www.w3.org/2000/svg"><rect width="210" height="82" fill="white"/>'
            f'<g fill="black">{rects}</g><text x="105" y="77" fill="black" font-family="monospace" '
            f'font-size="13" text-anchor="middle">{esc(code)}</text></svg>')


def color_chips(a,b):
    return ''.join(f'<span class="color-chip" style="background:{COLOR_HEX.get(x,"#aaa")}" title="{esc(x)}"></span>' for x in (a,b))


def field(label, name, *, typ='text', required=True, value='', placeholder='', extra='', rows=0):
    elem = (f'<textarea name="{esc(name)}" placeholder="{esc(placeholder)}" {"required" if required else ""} {extra}>{esc(value)}</textarea>'
            if rows else f'<input type="{esc(typ)}" name="{esc(name)}" value="{esc(value)}" placeholder="{esc(placeholder)}" {"required" if required else ""} {extra}>')
    return f'<label class="field"><span>{esc(label)}</span>{elem}</label>'


def select_field(label, name, options, selected=None, required=True):
    options_html = ''.join(f'<option value="{esc(v)}" {"selected" if str(v)==str(selected) else ""}>{esc(text)}</option>' for v,text in options)
    return f'<label class="field"><span>{esc(label)}</span><select name="{esc(name)}" {"required" if required else ""}>{options_html}</select></label>'


def form(action, csrf, content, button='Confirmar', *, class_name='', tone='primary', confirm=None):
    conf = f' onsubmit="return confirm(\'{esc(confirm)}\')"' if confirm else ''
    return (f'<form method="post" action="/acao" enctype="multipart/form-data" class="{esc(class_name)}"{conf}>'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="acao" value="{esc(action)}">'
            f'{content}<button type="submit" class="btn {esc(tone)}">{esc(button)}</button></form>')


def card(title, body, subtitle='', extra=''):
    return f'<section class="card {esc(extra)}"><div class="card-head"><div><h3>{title}</h3>{f"<p>{subtitle}</p>" if subtitle else ""}</div></div>{body}</section>'


def empty(msg):
    return f'<div class="empty"><span>◎</span><p>{esc(msg)}</p></div>'


def fmt_datetime(value):
    if not value: return '—'
    try:
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone().strftime('%d/%m/%Y %H:%M')
    except ValueError:
        return str(value)


def layout(title, body, user=None, flash=None, current=''):
    nav = [('inicio','⌂','Visão geral'),('pedidos','▤','Portal de solicitações'),('leitura','▥','Bipar / conferir'),('bandejas','▣','Bandejas'),('movimentacoes','⇄','Movimentações'),('pendencias','⚑','Pendências')]
    if user and user['papel'] == 'medico':
        nav = [item for item in nav if item[0] in ('inicio','pedidos')]
    if user and user['papel'] == 'enfermeiro':
        nav.append(('pacientes','♙','Pacientes do setor'))
        nav = [i for i in nav if i[0] not in ('movimentacoes','pendencias')]
    if user and user['papel'] == 'gerente_estoque':
        nav.append(('inventario_rotativo','▦','Inventário rotativo'))
        nav.append(('usuarios','♙','Equipe e acessos'))
    if user:
        links = ''.join(f'<a href="/{k}" class="nav-link {"active" if current==k else ""}"><span>{ico}</span>{text}</a>' for k,ico,text in nav)
        sidebar = f'''<aside class="sidebar"><a href="/inicio" class="logo"><span class="logo-icon">✳</span><span>tray<span class="logo-light">track</span><small>GESTÃO HOSPITALAR</small></span></a>
            <div class="nav-caption">WORKSPACE</div><nav>{links}</nav>
            <div class="sidebar-end"><div class="user-avatar">{esc(user['nome'][0].upper())}</div><div class="user-text"><b>{esc(user['nome'])}</b><small>{esc(ROLE_LABEL[user['papel']])}</small></div><a class="logout" title="Sair" href="/sair">⇥</a></div></aside>'''
        top = f'<header class="topbar"><div><small>GESTÃO DE INSTRUMENTAIS / {esc(ROLE_LABEL[user["papel"]].upper())}</small><h1>{esc(title)}</h1></div><div class="top-right"><span class="online-dot"></span> Ambiente local <span class="divider-v"></span> {esc(user["setor"])}</div></header>'
    else:
        sidebar=''; top=''
    alert = f'<div class="flash {esc(flash[0])}">{esc(flash[1])}</div>' if flash else ''
    with open(ROOT/'static'/'style.css',encoding='utf-8') as fp:
        css=fp.read()
    refresh = '<meta http-equiv="refresh" content="60">' if user and user['papel']=='medico' else ''
    return f'''<!DOCTYPE html><html lang="pt-BR"><head>{refresh}<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · traytrack</title><style>{css}</style></head>
        <body><div class="app {"auth-layout" if not user else ""}">{sidebar}<main class="main">{top}<div class="content">{alert}{body}</div><footer>traytrack · Protótipo de rastreabilidade administrativa — não valida esterilização ou liberação clínica.</footer></main></div></body></html>'''


def overview(user, csrf):
    trays=ENGINE.listar_bandejas(); orders=ENGINE.listar_pedidos(); alerts=ENGINE.alertas(); issues=ENGINE.pendencias()
    if user['papel']=='medico': orders=[p for p in orders if p['medico_id']==user['usuario_id']]
    if user['papel']=='enfermeiro': orders=[p for p in orders if p['setor_solicitante']==user['setor']]
    total_available=sum(b['etapa']=='estoque' and not b['bloqueada'] for b in trays)
    if user['papel']=='medico':
        counts=estoque_resumo()
        own_type=next((t for t in ENGINE.listar_tipos() if setor_canonico(t['nome'])==setor_canonico(user['setor'])),None)
        request_card=(card('Nova solicitação médica',form('solicitar',csrf,
            '<input type="hidden" name="tipo" value="'+esc(own_type['nome'])+'">'+campos_pedido_por_cor(user['setor']),
            'Gerar código de solicitação'), 'Selecione o padrão de itens pelas duas cores. Bandejas do Arsenal compartilhado, com destino ao seu setor.')
            if own_type else card('Nova solicitação médica',empty('Setor do médico não configurado. Solicite correção ao gerente.')))
        summary_stock=card('Disponibilidade no Arsenal',stock_table(counts),
                           'Quantidade atualizada de bandejas completas, prontas administrativamente para separação')
        low=''.join(f'<div class="alert-row">{esc(r["tipo"])}: {r["disponiveis"]} disponíveis</div>' for r in counts if r['disponiveis']<=3) or empty('Sem alertas de estoque baixo.')
        recent=''.join(f'<tr><td>{esc(p["codigo"])}</td><td>{esc(p["tipo"])}</td><td>{status_badge(p["status"])}</td></tr>' for p in reversed(orders[-6:]))
        return request_card+summary_stock+catalogo_medico(user["setor"])+card('Alertas de estoque',low)+painel_emprestimos(user)+painel_transferencias(user,csrf)+card('Meus pedidos recentes',
            '<div class="table-wrap"><table><tr><th>Pedido</th><th>Tipo</th><th>Situação</th></tr>'+recent+'</table></div>')
    if user['papel']=='enfermeiro':
        total_available=0
        trays=[b for b in trays if b['responsavel_id']==user['usuario_id'] and b['etapa'] in ('enfermagem','emprestada')]
        issues=[i for i in issues if i['bandeja'] in {b['codigo'] for b in trays}]
    stats=[('Bandejas sob controle' if user['papel']=='enfermeiro' else 'Bandejas cadastradas',len(trays),'▣'),('Disponíveis' if user['papel']!='enfermeiro' else 'Somente minhas bandejas',total_available if user['papel']!='enfermeiro' else len(trays),'✓'),
           ('Solicitações abertas',sum(p['status']!='finalizado' for p in orders),'▤'),('Pendências',len(issues),'⚑')]
    summary='<div class="stat-grid">'+''.join(f'<div class="stat"><span class="stat-icon">{ico}</span><p>{esc(label)}</p><strong>{val}</strong></div>' for label,val,ico in stats)+'</div>'
    low = (''.join(f'<div class="alert-row"><span class="alert-symbol">!</span><div><b>{esc(a["tipo"])}</b><small>{a["disponiveis"]} bandeja(s) disponíveis · limite: 3</small></div>{tag("Estoque baixo","warning")}</div>' for a in alerts) or empty('Estoque acima do limite mínimo por composição.'))
    recent = ''.join(f'<tr><td><b>{esc(p["codigo"])}</b></td><td>{esc(p["tipo"])}</td><td>{status_badge(p["status"])}</td></tr>' for p in reversed(orders[-6:]))
    recent = f'<div class="table-wrap"><table><thead><tr><th>Solicitação</th><th>Tipo</th><th>Situação</th></tr></thead><tbody>{recent}</tbody></table></div>' if recent else empty('Ainda não há solicitações.')
    quick = '<a href="/pedidos" class="btn primary">Abrir solicitações →</a>' if user['papel']=='medico' else '<a href="/leitura" class="btn primary">Conferir por código de barras →</a>' if user['papel']=='enfermeiro' else '<a href="/bandejas" class="btn primary">Consultar bandejas →</a>'
    return ('<div class="eyebrow">PAINEL OPERACIONAL</div><h2 class="hero">Controle inteligente de bandejas<br><span>e rastreabilidade de materiais.</span></h2>' +
            (solicitacoes_abertas_do_setor(user) if user['papel']=='enfermeiro' else '')+summary+(alertas_faltas_gerente()+catalogo_medico() if user['papel']=='gerente_estoque' else '')+painel_emprestimos(user)+'<div class="grid-two">'+card('Alertas de estoque', low, 'Avisos em tempo real por tipo de bandeja')+card('Solicitações recentes',recent,'Acompanhe o andamento dos pedidos')+'</div>'+quick)


def estoque_resumo():
    with db() as con:
        return [dict(x) for x in con.execute("""SELECT 'Arsenal (compartilhado)' AS tipo,
            COUNT(CASE WHEN etapa='estoque' AND localizacao_atual='Arsenal' AND bloqueada=0 THEN 1 END) AS disponiveis,
            COUNT(*) AS cadastradas FROM bandejas""")]


def stock_table(rows):
    body=''.join(f'<tr><td>{esc(r["tipo"])}</td><td><b>{r["disponiveis"]}</b></td>'
                 f'<td>{tag("Estoque baixo", "warning") if r["disponiveis"]<=3 else tag("Disponível", "ok")}</td></tr>' for r in rows)
    return f'<div class="table-wrap"><table><thead><tr><th>Setor / tipo</th><th>Disponíveis</th><th>Alerta</th></tr></thead><tbody>{body}</tbody></table></div>'


def catalogo_medico(setor=None):
    """Padrões agrupados por setor e cores; não mostra EANs individuais ao médico."""
    grupos=ENGINE.resumo_padroes_por_setor()
    if setor:
        grupos=[g for g in grupos if setor_canonico(g['setor'])==setor_canonico(setor)]
    else:
        grupos=list({g['padrao_id']:dict(g,setor='Todos os setores') for g in grupos}.values())
    if not grupos: return card('Padrões de bandejas',empty('Nenhum padrão cadastrado para este setor.'))
    rows=''
    for g in grupos:
        itens=''.join(f'<li>{int(i["quantidade"])}× {esc(i["nome"])}</li>' for i in g['itens'])
        rows+=(f'<tr><td><b>{esc(g["setor"])}</b></td>'
               f'<td>{color_chips(g["cor_1"],g["cor_2"])} {esc(g["cor_1"])} / {esc(g["cor_2"])}</td>'
               f'<td>{g["total_itens"]} unidade(s)</td><td><details><summary>Ver materiais</summary><ul class="item-summary">{itens}</ul></details></td>'
               f'<td>{g["disponiveis"]}</td><td>{g["no_estoque"]}</td><td>{g["total_cadastradas"]}</td></tr>')
    return card('Padrões compartilhados disponíveis no Arsenal',
        '<div class="table-wrap"><table><thead><tr><th>Setor</th><th>Cores / padrão</th><th>Itens por bandeja</th>'
        '<th>Composição</th><th>Disponíveis</th><th>No estoque (inclusive bloqueadas)</th><th>Cadastradas</th>'
        '</tr></thead><tbody>'+rows+'</tbody></table></div>',
        'As quantidades disponíveis são do Arsenal compartilhado, não de estoques exclusivos por setor.')


def campos_pedido_por_cor(setor):
    """Uma opção por composição existente no setor, sem revelar códigos das bandejas."""
    grupos=[g for g in ENGINE.resumo_padroes_por_setor()
            if setor_canonico(g['setor'])==setor_canonico(setor)]
    if not grupos: return empty('Não existem padrões de bandeja cadastrados para o seu setor.')
    options=[]
    for g in grupos:
        options.append((str(g['padrao_id']),f'{g["cor_1"]} + {g["cor_2"]} · '
            f'{g["total_itens"]} unidades · {g["disponiveis"]} disponíveis'))
    return (f'<p><b>Setor de destino:</b> {esc(setor)}</p>'
        +select_field('Escolha o padrão pelas cores e materiais','padrao_cores_id',options)
        +'<p class="hint">O pedido terá esta composição específica; o estoque separará apenas uma bandeja do padrão escolhido.</p>')


def alertas_faltas_gerente():
    faltas=ENGINE.pendencias()
    if not faltas: return card('Avisos de materiais faltantes',empty('Não existem faltas de materiais em aberto.'))
    lines=''.join(f'<div class="alert-row"><span class="alert-symbol">!</span>'
        f'<div><b>{esc(f["item"])} — {f["faltante"]} unidade(s) faltante(s)</b>'
        f'<small>Bandeja {esc(f["bandeja"])} · {esc(f["tipo"])}</small></div>'
        f'{tag("Reposição pendente","warning")}</div>' for f in faltas)
    return card('Avisos de materiais faltantes',lines+'<p><a href="/pendencias" class="btn secondary">Consultar e regularizar pendências →</a></p>',
                'Avisos internos ao sistema: materiais não localizados durante as conferências, com identificação da bandeja.')


def inventario_materiais_gerente():
    dados=ENGINE.inventario_materiais_estoque()
    rows=''.join(f'<tr><td>{esc(i["nome"])}</td><td><b>{i["previsto"]}</b></td><td>{i["faltante"]}</td><td><b>{i["estimado_presente"]}</b></td></tr>' for i in dados['itens'])
    rows+=f'<tr><th>Total</th><th>{dados["total_previsto"]}</th><th>{dados["total_faltante"]}</th><th>{dados["total_estimado_presente"]}</th></tr>' if rows else ''
    summary=(f'<p><b>{dados["bandejas_no_estoque"]}</b> bandejas fisicamente no estoque · '
             f'<b>{dados["bandejas_bloqueadas"]}</b> bloqueada(s) · '
             f'<b>{dados["total_estimado_presente"]}</b> unidades estimadas presentes</p>')
    return card('Materiais totais nas bandejas do estoque',summary+(
        '<div class="table-wrap"><table><thead><tr><th>Material</th><th>Previsto nas bandejas</th><th>Faltas em aberto</th><th>Estimado presente</th></tr></thead><tbody>'+rows+'</tbody></table></div>' if rows else empty('Nenhuma bandeja no estoque.')),
        'Inclui bandejas bloqueadas que já retornaram fisicamente. Não inclui bandejas separadas, com técnico de enfermagem ou emprestadas. O estimado presente desconta pendências abertas; exige conferência física.')



def solicitacoes_abertas_do_setor(user):
    """Exibe somente os pedidos pendentes do setor do técnico, sem EAN individual."""
    if user['papel'] != 'enfermeiro':
        return ''
    setor = setor_canonico(user['setor'])
    pedidos = [p for p in ENGINE.listar_pedidos()
               if setor_canonico(p['setor_solicitante']) == setor
               and p['status'] in ('aberto', 'separado')]
    padroes = {g['padrao_id']: g for g in ENGINE.resumo_padroes_por_setor()
               if setor_canonico(g['setor']) == setor}
    linhas=[]
    for p in reversed(pedidos):
        padrao=padroes.get(p.get('padrao_cores_id'))
        if padrao:
            composicao='<details><summary>Ver itens pedidos</summary><ul class="item-summary">'+''.join(
                f'<li>{int(i["quantidade"])}× {esc(i["nome"])}</li>' for i in padrao['itens'])+'</ul></details>'
            cores=color_chips(padrao['cor_1'],padrao['cor_2'])+' '+esc(padrao['cor_1'])+' / '+esc(padrao['cor_2'])
            total=f'{padrao["total_itens"]} unidades'
        else:
            composicao='<span class="hint">Composição não especificada no pedido antigo; consulte o gerente.</span>'
            cores=esc((p.get('cor_solicitada_1') or 'Não definido')+' / '+(p.get('cor_solicitada_2') or 'Não definido'))
            total='—'
        acao=(f'<a class="btn secondary" href="/leitura?pedido={esc(p["codigo"])}">Conferir pedido →</a>'
              if p['status']=='separado' else '<span class="hint">Aguardando separação pelo estoque</span>')
        linhas.append('<tr>'+
            f'<td class="mono"><b>{esc(p["codigo"])}</b></td>'+
            f'<td>{esc(p["medico"])}</td><td>{esc(p["tipo"])}</td>'+
            f'<td>{cores}</td><td>{total}<div>{composicao}</div></td>'+
            f'<td>{fmt_datetime(p["criado_em"])}</td><td>{status_badge(p["status"])}</td>'+
            f'<td>{acao}</td></tr>')
    tabela=('<div class="table-wrap"><table><thead><tr><th>Solicitação</th><th>Médico solicitante</th>'
            '<th>Setor / bandeja</th><th>Cores</th><th>Composição solicitada</th><th>Data</th>'
            '<th>Situação</th><th>Próximo passo</th></tr></thead><tbody>'+''.join(linhas)+'</tbody></table></div>') if linhas else empty('Nenhuma solicitação aberta ou separada no seu setor.')
    return card('Solicitações abertas do meu setor',tabela,
                f'{len(pedidos)} pedido(s) aguardando separação ou check-in · {esc(user["setor"])}')


def pedidos_page(user,csrf):
    orders=ENGINE.listar_pedidos(); types=[t for t in ENGINE.listar_tipos() if setor_canonico(t['nome'])]
    if user['papel']=='medico': orders=[p for p in orders if p['medico_id']==user['usuario_id']]
    if user['papel']=='enfermeiro': orders=[p for p in orders if p['setor_solicitante']==user['setor']]
    blocks=[]
    if user['papel']=='medico':
        own_types=[t for t in types if setor_canonico(t['nome'])==setor_canonico(user['setor'])]
        if own_types:
            blocks.append(card('Nova solicitação médica',form('solicitar',csrf,
                '<input type="hidden" name="tipo" value="'+esc(own_types[0]['nome'])+'">'+campos_pedido_por_cor(user['setor']),
                'Gerar código de solicitação'),'Escolha pelas cores e pelos materiais; o código SOL-… é gerado automaticamente.'))
        else:
            blocks.append(card('Nova solicitação médica',empty('Seu setor não está configurado corretamente. Solicite ajuste ao gerente.')))
        blocks.append(card('Disponibilidade antes de solicitar',stock_table(estoque_resumo()),
                           'Bandejas do Arsenal compartilhadas por todos os setores'))
        blocks.append(catalogo_medico(user["setor"]))
    elif user['papel']=='gerente_estoque':
        pending=[p for p in orders if p['status']=='aberto']
        available=[b for b in ENGINE.listar_bandejas() if b['etapa']=='estoque' and not b['bloqueada']]
        if pending:
            separacao=[]
            for order in pending:
                candidatos=[b for b in available if b['localizacao_atual']=='Arsenal' and
                    (not order.get('cor_solicitada_1') or
                     (b['cor_1']==order['cor_solicitada_1'] and b['cor_2']==order['cor_solicitada_2']))]
                label=(order['cor_solicitada_1']+' + '+order['cor_solicitada_2']) if order.get('cor_solicitada_1') else 'padrão legado livre'
                if candidatos:
                    f=(f'<input type="hidden" name="pedido" value="{esc(order["codigo"])}">'
                       +select_field('Bandeja compatível com o pedido','bandeja',
                          [(b['codigo'],b['codigo']+' · '+b['cor_1']+'/'+b['cor_2']) for b in candidatos]))
                    separacao.append(card('Separar · '+order['codigo'],
                        f'<p><b>{esc(order["tipo"])}</b> · Cores solicitadas: {esc(label)} · {len(candidatos)} disponível(is)</p>'
                        +form('separar',csrf,f,'Registrar saída do estoque')))
                else:
                    separacao.append(card('Pedido aguardando reposição · '+order['codigo'],
                        empty(f'Não há bandeja disponível com o padrão {label} no Arsenal.')))
            blocks.extend(separacao)
        else: blocks.append(card('Separar bandeja',empty('Sem pedidos abertos.')))
    elif user['papel']=='enfermeiro':
        blocks.append(solicitacoes_abertas_do_setor(user))
        blocks.append(card('Receber bandeja vinculada a um pedido',
            '<p>Use o leitor de códigos de barras no painel de conferência. O número da solicitação médica é obrigatório.</p>'
            '<a class="btn primary" href="/leitura">Abrir leitura e conferência →</a>'))
    rows=''.join(f'<tr><td class="mono"><b>{esc(p["codigo"])}</b></td><td>{esc(p["tipo"])}</td>'
        f'<td>{esc(p["medico"])}</td><td>{esc(p["setor_solicitante"] or "—")}</td>'
        f'<td>{esc(p["paciente_codigo"] or "A definir no check-in")}</td>'
        f'<td>{fmt_datetime(p["criado_em"])}</td><td>{status_badge(p["status"])}</td></tr>' for p in reversed(orders))
    blocks.append(card('Portal de solicitações',f'<div class="table-wrap"><table><thead><tr>'
        '<th>Código</th><th>Tipo / destino</th><th>Solicitante</th><th>Setor de origem</th><th>Paciente vinculado</th><th>Data e hora</th><th>Status</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>' if rows else empty('Nenhum pedido registrado.'),
        f'{len(orders)} solicitações · médicos visualizam apenas suas próprias solicitações'))
    if user['papel'] in ('medico','gerente_estoque'):
        blocks.insert(1 if user['papel']=='medico' else 0,painel_emprestimos(user)+(painel_transferencias(user,csrf) if user['papel']=='medico' else ''))
    return ''.join(blocks)


def painel_emprestimos(user):
    loans=ENGINE.emprestimos_ativos(user['usuario_id'] if user['papel']=='medico' else None)
    if not loans:
        return card('Acompanhamento desde o check-in do técnico de enfermagem',empty('Nenhuma bandeja com check-in concluído sob sua posse atual.'),
                    f'Limite configurado: {LIMITE_EMPRESTIMO_HORAS} horas')
    atrasadas=sum(1 for r in loans if r['atrasado'])
    aviso=(f'<div class="flash error"><b>ATENÇÃO: {atrasadas} bandeja(s) excederam {LIMITE_EMPRESTIMO_HORAS} horas desde o check-in.</b> '
           'O alerta acompanha o médico portador atual; o gerente acompanha os casos de escalonamento e investigação no mesmo painel.</div>') if atrasadas else ''
    preventivas=sum(1 for r in loans if r['nivel_alerta']=='preventivo')
    aviso+= (f'<div class="flash aviso-laranja"><b>AVISO: {preventivas} bandeja(s) chegaram a 80% do prazo de empréstimo.</b> '
             'Prepare a devolução ou verifique a necessidade de prorrogação.</div>') if preventivas else ''
    rows=''.join('<tr><td>'+esc(r['pedido'])+'</td><td>'+esc(r['tipo'])+'</td>'+
                 ('<td>'+esc(r['codigo'])+'</td>' if user['papel']=='gerente_estoque' else '')+
                 '<td>'+esc(r['medico'])+'</td><td>'+esc(r['portador'] or '—')+'</td>'+
                 '<td>'+status_badge(r['etapa'])+'</td><td>'+fmt_datetime(r['retirado_em'])+'</td><td>'+f'{r["horas_emprestimo"]*60:.1f} min'+'</td><td>'+
                 ({'normal':tag('No prazo','ok'), 'preventivo':tag('80% do prazo · Atenção','warning'),
                    'atrasado':tag('ATRASADA','danger'), 'escalonamento':tag('Escalonar','danger'),
                    'investigacao':tag('Investigar localização','danger')}[r['nivel_alerta']])+'</td></tr>' for r in loans)
    return card('Tempo desde o check-in · atualização a cada 60 s',aviso+
        '<div class="table-wrap"><table><thead><tr><th>Solicitação</th><th>Tipo</th>'+
        ('<th>Bandeja</th>' if user['papel']=='gerente_estoque' else '')+
        '<th>Solicitante original</th><th>Portador atual</th><th>Situação</th><th>Check-in do técnico</th><th>Tempo</th><th>Alerta</th></tr></thead><tbody>'+rows+'</tbody></table></div>',
        f'Alerta laranja aos 80% e vermelho aos 100% de {LIMITE_EMPRESTIMO_HORAS} h desde o check-in; direcionado ao portador atual')


def painel_transferencias(user,csrf):
    if user['papel']!='medico': return ''
    uid=user['usuario_id']
    atuais=[r for r in ENGINE.emprestimos_ativos(uid) if r['etapa']=='emprestada']
    pendentes=ENGINE.transferencias(uid)
    with db() as con:
        destinos=[dict(r) for r in con.execute("""SELECT u.id,u.nome,u.setor FROM usuarios u
            JOIN contas c ON c.usuario_id=u.id
            WHERE u.papel='medico' AND u.ativo=1 AND c.aprovado=1 AND u.id<>?
            ORDER BY u.nome""",(uid,))]
    sections=[]
    em_processo={r['bandeja'] for r in pendentes if r['origem_id']==uid}
    disponiveis=[r for r in atuais if r['codigo'] not in em_processo]
    if disponiveis and destinos:
        f=select_field('Bandeja em sua posse','bandeja',[(r['codigo'],r['tipo']+' · '+r['codigo']) for r in disponiveis])
        f+=select_field('Médico destinatário','medico_destino_id',[(str(u['id']),u['nome']+' · '+u['setor']) for u in destinos])
        sections.append(card('Solicitar transferência médico → médico',form('transferir_medico',csrf,f,'Enviar pedido de transferência'),
            'A custódia permanece com você até o destinatário bipar a bandeja e aceitar. O temporizador não reinicia.'))
    else:
        sections.append(card('Solicitar transferência médico → médico',empty('Sem bandejas disponíveis para transferir ou sem médicos destinatários aprovados.')))
    para_mim=[r for r in pendentes if r['destino_id']==uid]
    enviadas=[r for r in pendentes if r['origem_id']==uid]
    blocks=''
    for tr in para_mim:
        prefix=('<p><b>'+esc(tr['tipo'])+'</b> · Solicitação '+esc(tr['pedido'])+' · Remetente '+esc(tr['origem'])+'</p>'
                +'<p class="hint">Leia o código EAN-13 diretamente da bandeja que está recebendo. Confira a integridade antes de aceitar.</p>')
        fields=(f'<input type="hidden" name="transferencia_id" value="{tr["id"]}">'
                +field('Código EAN-13 bipado','bandeja',placeholder='Bipe a etiqueta ou digite os 13 dígitos',extra='inputmode="numeric" pattern="[0-9]{13}"')
                +field('Observação / divergências','observacoes',required=False,rows=2))
        blocks+=card('Recebimento pendente · '+esc(tr['bandeja']),prefix+
            form('aceitar_transferencia',csrf,fields,'Confirmar recebimento')+
            form('recusar_transferencia',csrf,fields,'Recusar recebimento',tone='secondary'))
    if not blocks: blocks=card('Transferências recebidas',empty('Você não possui transferências aguardando aceite.'))
    if enviadas:
        for tr in enviadas:
            blocks+=card('Aguardando aceite do destinatário',f'<p>{esc(tr["bandeja"])} · {esc(tr["destino"])} · {fmt_datetime(tr["solicitado_em"])}</p>'+
                form('cancelar_transferencia',csrf,f'<input type="hidden" name="transferencia_id" value="{tr["id"]}">','Cancelar transferência',tone='secondary'))
    return ''.join(sections)+blocks


def motivos_conferencia(val,bandeja):
    motivos={}
    bandeja=next((t for t in ENGINE.listar_bandejas() if t['codigo']==bandeja or t.get('codigo_legado')==bandeja),None)
    if not bandeja: return motivos
    for item in bandeja['itens']:
        motivo=val('motivo_'+str(item['id']),'em_apuracao')
        if motivo not in ('consumido','descartado','nao_localizado','em_apuracao'):
            raise ValueError('Motivo inválido.')
        motivos[item['nome']]=motivo
    return motivos


def checklist_fields(b):
    ret = ('<p class="hint">Todas as unidades estão pré-marcadas. '
           'Desmarque somente as unidades ausentes e descreva eventuais '
           'divergências nas observações.</p>')
    ret += '<input type="hidden" name="conferencia_checkbox" value="1">'
    ret += '<fieldset class="check-items"><legend>Conferência de materiais</legend>'
    for item in b['itens']:
        for numero in range(1, int(item['quantidade']) + 1):
            ret += (f'<label class="check-row"><input type="checkbox" '
                    f'name="presente_{item["id"]}_{numero}" value="1" checked> '
                    f'{esc(item["nome"])} Unidade {numero} presente</label>')
    ret += '</fieldset>'
    ret += '<label class="check-row seal"><input type="checkbox" name="lacre_violado" value="1"> Lacre violado</label>'
    ret += field('Observações da conferência','observacoes',required=False,rows=3,
                 placeholder='Ex.: 1 gaze faltante, embalagem danificada...')
    ret += '<p class="hint">O registro de itens faltantes é automático a partir das caixas desmarcadas. '
    ret += 'O gerente poderá consultar a quantidade e o item específico no painel de pendências.</p>'
    ret += foto_field()
    return ret


def foto_field():
    return ('<label class="field"><span>Foto da bandeja / lacre / materiais (opcional)</span>'
            '<input type="file" name="foto" accept="image/jpeg,image/png,image/webp">'
            '<small class="hint">PNG, JPEG ou WebP, até 2 MB. Evite fotografar pacientes ou dados pessoais.</small></label>')


def leitura_page(user,csrf,query):
    if user['papel']=='medico': return empty('Médicos não podem conferir bandejas.')
    codigo=(query.get('codigo',[''])[0]).strip().upper()
    pedido=(query.get('pedido',[''])[0]).strip().upper()
    title=card('Leitor de código de barras',
        '<form method="get" action="/leitura" class="scan-form">'+
        (field('Código da solicitação médica (obrigatório no check-in)','pedido',required=False,
               value=pedido,placeholder='SOL-XXXXXXXXXXXX') if user['papel']=='enfermeiro' else '')+
        field('Código de barras da bandeja (13 dígitos)','codigo',value=codigo,
              placeholder='Bipe a etiqueta; o leitor deve enviar Enter',extra='autofocus inputmode="numeric" pattern="[0-9]{13}"')+
        '<button type="submit" class="btn primary">Abrir conferência</button></form>',
        'No check-in: informe primeiro o código SOL-… e depois bipe o código de barras. O leitor deve enviar Enter.')
    if not codigo: return title
    # Evita expor inventário: enfermeiro só lê bandeja separada com pedido válido
    # ou bandeja já sob sua responsabilidade. Não mostra bandejas em estoque.
    trays=ENGINE.listar_bandejas()
    b=next((t for t in trays if t['codigo']==codigo or t.get('codigo_legado')==codigo),None)
    if not b: return title+card('Resultado',empty('Código não encontrado.'))
    if user['papel']=='enfermeiro':
        own=b['responsavel_id']==user['usuario_id'] and b['etapa'] in ('enfermagem','emprestada')
        allowed_checkin=False
        if pedido and b['etapa']=='separada':
            with db() as con:
                exists=con.execute("""SELECT 1 FROM pedidos p WHERE p.id=? AND p.codigo=? AND p.status='separado'""",
                                   (b['pedido_id'],pedido)).fetchone()
                allowed_checkin=bool(exists)
        if not (own or allowed_checkin):
            return title+card('Acesso restrito',empty('Informe um código de pedido válido associado a uma bandeja separada, ou leia uma bandeja sob sua responsabilidade.'))
    description=f'<div class="barcode-large">{barcode_svg(b["codigo"])}</div><p><b>Destino:</b> {esc(b["tipo"])} &nbsp; '
    description+=f'<b>Padrão de materiais:</b> {color_chips(b["cor_1"],b["cor_2"])} {esc(b["cor_1"])} / {esc(b["cor_2"])}</p>'
    description+=f'<p><b>Estado:</b> {status_badge(b["etapa"],b["bloqueada"])}</p>'
    h=f'<input type="hidden" name="bandeja" value="{esc(b["codigo"])}">'
    if user['papel']=='enfermeiro':
        if b['etapa']=='separada':
            h+=f'<input type="hidden" name="pedido" value="{esc(pedido)}">'
            try:
                pacientes=ENGINE.pacientes_do_pedido(user['usuario_id'],pedido)
                paciente_input=select_field('Paciente atendido neste setor (obrigatório)','paciente_id',
                    [('', 'Selecione um paciente')]+[(str(p['id']),p['codigo']+' · '+p['nome']) for p in pacientes])
                description+=form('checkin',csrf,h+paciente_input+
                    '<p class="hint">Antes do uso, verifique a liberação da bandeja pelo CME. O vínculo ao paciente não comprova esterilidade.</p>'+
                    checklist_fields(b),'Conferir e registrar check-in')
            except RegraNegocioError as exc:
                description+=empty(str(exc))
        elif b['etapa']=='enfermagem':
            if not b['bloqueada']:
                description+=form('checkout',csrf,h+'<p>Entrega inicial ao médico solicitante. Transferências posteriores são feitas pelo painel médico.</p>'+foto_field(), 'Check-out: entregar ao médico solicitante')
            else: description+='<p class="hint">Bandeja bloqueada: encaminhe ao estoque para regularização.</p>'
            description+=form('enviar_estoque',csrf,h,'Enviar ao estoque',tone='secondary')
        elif b['etapa']=='emprestada':
            description+=form('receber_emprestimo',csrf,h+checklist_fields(b),'Confirmar devolução e conferência')
    elif b['etapa']=='retorno_estoque':
        description+=form('receber_estoque',csrf,h+checklist_fields(b),'Confirmar recebimento no estoque')
    description+='<p><a href="/historico?codigo='+esc(b['codigo'])+'">Consultar histórico desta bandeja →</a></p>'
    return title+card('Bandeja identificada — conferência',description)


def inventario_rotativo_page(user,csrf):
    if user['papel']!='gerente_estoque': return empty('Área restrita ao gerente.')
    locais=['Arsenal']+sorted({u['setor'] for u in ENGINE.listar_usuarios() if u['setor']})
    sections=[card('Iniciar inventário rotativo',form('iniciar_inventario',csrf,
        select_field('Local físico a conferir','localizacao',[(a,a) for a in locais]),'Criar inventário'),
        'Conferência por código de barras das bandejas esperadas no local selecionado. Não altera automaticamente a localização registrada.')]
    for inv in ENGINE.listar_inventarios(user['usuario_id']):
        res=ENGINE.consultar_inventario(user['usuario_id'],inv['id'])
        descricao=(f'<p>Local: <b>{esc(inv["localizacao"])}</b> · Criado em {fmt_datetime(inv["iniciado_em"])} · '
                   f'{tag("Encerrado","ok") if res["encerrado"] else tag("Em andamento","warning")}</p>'
                   f'<p><b>{res["esperados"]}</b> esperadas · <b>{res["lidos"]}</b> encontradas · '
                   f'<b>{len(res["nao_localizados"])}</b> não localizadas · <b>{len(res["nao_previstos"])}</b> não previstas</p>')
        if res['nao_localizados']:
            descricao+='<p class="flash error">Não localizadas: '+esc(', '.join(res['nao_localizados']))+'</p>'
        if res['nao_previstos']:
            descricao+='<p class="flash error">Fora do local esperado: '+esc(', '.join(res['nao_previstos']))+'</p>'
        if not res['encerrado']:
            h=f'<input type="hidden" name="inventario_id" value="{inv["id"]}">'
            descricao+=form('bipar_inventario',csrf,h+field('Código EAN-13','bandeja',extra='autofocus inputmode="numeric" pattern="[0-9]{13}"'),
                             'Registrar leitura')
            descricao+=form('encerrar_inventario',csrf,h,'Encerrar e guardar resultado',tone='secondary')
        sections.append(card('Inventário #'+str(inv['id']),descricao))
    return ''.join(sections)


def historico_page(user,csrf,query):
    if user['papel']=='medico': return empty('Acesso restrito.')
    code=query.get('codigo',[''])[0].strip().upper()
    f='<form method="get" action="/historico">'+field('Código de barras','codigo',value=code,placeholder='789...')+'<button class="btn primary">Consultar</button></form>'
    if not code: return card('Histórico individual por bandeja',f)
    trays=ENGINE.listar_bandejas()
    b=next((t for t in trays if code in (t['codigo'],t.get('codigo_legado'))),None)
    if not b: return card('Histórico individual por bandeja',f+empty('Bandeja não encontrada.'))
    if user['papel']=='enfermeiro' and not (b['responsavel_id']==user['usuario_id'] and b['etapa'] in ('enfermagem','emprestada')):
        return card('Histórico individual por bandeja',f+empty('Você só pode visualizar o histórico de bandejas sob sua responsabilidade.'))
    history=ENGINE.historico(b['codigo'])
    with db() as con:
        confs=[dict(r) for r in con.execute("""SELECT c.*,u.nome ator FROM conferencias c JOIN usuarios u ON u.id=c.ator_id
            WHERE c.bandeja_id=? ORDER BY c.id DESC""",(b['id'],))]
    with db() as con:
        fotos=[dict(r) for r in con.execute("""SELECT f.id,f.nome,f.acao,f.criado_em,u.nome autor
             FROM fotos_conferencia f JOIN usuarios u ON u.id=f.autor_id
             WHERE f.bandeja_id=? ORDER BY f.id DESC""",(b['id'],))]
    with db() as con:
        exposicoes=[dict(r) for r in con.execute("""SELECT DISTINCT p.codigo pedido, pa.codigo paciente,
            p.setor_atendimento setor, u.nome medico, m.criado_em checkin_em
            FROM movimentos m JOIN pedidos p ON p.id=m.pedido_id
            JOIN pacientes pa ON pa.id=p.paciente_id
            JOIN usuarios u ON u.id=p.medico_id
            WHERE m.bandeja_id=? AND m.acao='checkin_pedido' ORDER BY m.criado_em DESC""",(b['id'],))]
    expos_rows=''.join('<tr><td>'+esc(x['pedido'])+'</td><td>'+esc(x['paciente'])+'</td><td>'+esc(x['setor'])+
        '</td><td>'+esc(x['medico'])+'</td><td>'+fmt_datetime(x['checkin_em'])+'</td></tr>' for x in exposicoes)
    expos_html=('<div class="table-wrap"><table><tr><th>Solicitação</th><th>Paciente (código)</th><th>Setor</th><th>Médico solicitante</th><th>Check-in</th></tr>'+
        expos_rows+'</table></div>') if expos_rows else empty('Nenhum paciente vinculado a esta bandeja.')
    fotos_html=''.join('<p>📷 '+fmt_datetime(x['criado_em'])+' · '+esc(x['acao'])+' · '+esc(x['autor'])+
        ' — <a href="/foto?id='+str(x['id'])+'" target="_blank" rel="noopener">Ver imagem anexada</a></p>' for x in fotos)
    rows=''.join(f'<tr><td>{fmt_datetime(m["criado_em"])}</td><td>{esc(m["ator"])}</td>'
                 f'<td>{esc(m["acao"])}</td><td>{esc(m["origem"])} → {esc(m["destino"])}</td>'
                 f'<td>{esc(m["observacao"])}</td></tr>' for m in reversed(history))
    checks=''.join(f'<tr><td>{fmt_datetime(c["criado_em"])}</td><td>{esc(c["ator"])}</td>'
                   f'<td>{esc(c["fase"])}</td><td>{esc(", ".join(x["nome"]+": "+str(x["faltante"])+" faltante(s)" for x in __import__("json").loads(c["itens_json"])))}</td>' +
                   f'<td>{esc("Não íntegro" if c["lacre_integro"] == 0 else ("Íntegro" if c["lacre_integro"] == 1 else "Não registrado"))}</td><td>{esc(c["observacoes"])}</td></tr>' for c in confs)
    return card('Histórico de '+esc(b['codigo']), barcode_svg(b['codigo'])+'<p>Setor de destino: '+esc(b['tipo'])+'</p>'+f+(
        '<div class="table-wrap"><table><tr><th>Data</th><th>Profissional</th><th>Ação</th><th>Movimento</th><th>Observações</th></tr>'+rows+'</table></div>' if rows else empty('Sem movimentos.')))+card('Conferências de composição',
        '<div class="table-wrap"><table><tr><th>Data</th><th>Profissional</th><th>Etapa</th><th>Itens</th><th>Lacre</th><th>Observações</th></tr>'+checks+'</table></div>' if checks else empty('Sem conferências.'))+card('Pacientes vinculados à bandeja',expos_html,'Registro para rastreabilidade; não certifica processamento ou esterilidade.')+card('Imagens anexadas',fotos_html or empty('Nenhuma foto anexada.'))

def trays_page(user,csrf):
    if user['papel']=='medico': return empty('Inventário completo não disponível para médicos.')
    trays=ENGINE.listar_bandejas(); types=[t for t in ENGINE.listar_tipos() if setor_canonico(t['nome'])]; blocks=[]
    if user['papel']=='enfermeiro':
        total_available=0
        trays=[b for b in trays if b['responsavel_id']==user['usuario_id'] and b['etapa'] in ('enfermagem','emprestada')]
        blocks.append(card('Bandejas sob sua responsabilidade',
            '<p>Somente bandejas recebidas por você. Para conferir uma entrega, utilize o código da solicitação médica e biped o código de barras.</p><a href="/leitura" class="btn primary">Abrir leitor →</a>'))
    else:
        f='<input type="hidden" name="tipo" value="Arsenal">'
        f+=field('Itens (Nome:quantidade por linha)','itens',rows=4,placeholder='Gaze (pacote):2\nCompressa (pacote):2\nPinça (exemplo):1\n... (total 10 a 40 unidades)')
        blocks.append(card('Cadastrar bandeja',form('nova_bandeja',csrf,f,'Cadastrar e gerar EAN-13'),
            'As novas bandejas entram no Arsenal, sem setor de destino. O pedido médico definirá o destino. Cores e EAN-13 são atribuídos automaticamente.'))
        f=field('Nome do novo setor de destino','nome')+field('Descrição','descricao',required=False)
        # Os setores são fixos nesta edição; cadastros extras foram desativados.
        rows=[]
        for padrao in ENGINE.listar_padroes():
            items=__import__('json').loads(padrao['itens_json'])
            rows.append(f'<tr><td>{color_chips(padrao["cor_1"],padrao["cor_2"])} '
                        f'{esc(padrao["cor_1"])} / {esc(padrao["cor_2"])}</td><td>{esc(", ".join(str(q)+"× "+n for n,q in items))}</td></tr>')
        blocks.append(alertas_faltas_gerente())
        blocks.append(catalogo_medico())
        blocks.append(inventario_materiais_gerente())
        blocks.append(card('Legenda de cores · padrão de itens','<div class="table-wrap"><table><tr><th>Cores</th><th>Composição</th></tr>'+''.join(rows)+'</table></div>',
                'Todas as bandejas entram no Arsenal e atendem qualquer setor; as cores definem o conteúdo do kit.'))
    rows=''
    for b in trays:
        itemtext=', '.join(f'{i["quantidade"]}× {i["nome"]}' for i in b['itens'])
        rows+=f'<tr><td>{barcode_svg(b["codigo"])}<small class="subline">Legado: {esc(b.get("codigo_legado") or "—")}</small></td>'
        rows+=f'<td>{esc(b["localizacao_atual"] or "Arsenal")}</td><td>{color_chips(b["cor_1"],b["cor_2"])} {esc(b["cor_1"])} / {esc(b["cor_2"])}</td>'
        rows+=f'<td>{status_badge(b["etapa"],b["bloqueada"])}</td><td>{esc(b["responsavel"] or "—")}</td>'
        rows+=f'<td><details><summary>Composição e ações</summary><p>{esc(itemtext)}</p>'
        rows+=f'<a href="/historico?codigo={esc(b["codigo"])}">Histórico →</a> &nbsp; <a href="/leitura?codigo={esc(b["codigo"])}">Conferir →</a></details></td></tr>'
    blocks.append(card('Inventário' if user['papel']=='gerente_estoque' else 'Minhas bandejas',
        '<div class="table-wrap"><table><thead><tr><th>Código de barras</th><th>Localização atual</th><th>Padrão / cores</th><th>Estado</th><th>Responsável</th><th>Detalhes</th></tr></thead><tbody>'+
        rows+'</tbody></table></div>' if rows else empty('Nenhuma bandeja sob sua responsabilidade.'),f'{len(trays)} bandejas visíveis'))
    return ''.join(blocks)

def issues_page(user,csrf):
    issues=ENGINE.pendencias(); rows=''
    for issue in issues:
        ops=''
        if user['papel']=='gerente_estoque':
            inp=f'<input type="hidden" name="bandeja" value="{esc(issue["bandeja"])}">'
            inp+=f'<input type="hidden" name="item" value="{esc(issue["item"])}">'
            inp+=f'<input type="hidden" name="quantidade" value="{issue["faltante"]}">'
            ops=form('regularizar',csrf,inp,'Confirmar reposição',tone='secondary',confirm='Confirmar reposição física do material?')
        rows+=f'<tr><td><b>{esc(issue["bandeja"])}</b><small class="subline">{esc(issue["tipo"])}</small></td><td>{esc(issue["item"])}</td><td>{issue["previsto"]}</td><td>{issue["devolvido"]}</td><td><b class="red-text">{issue["faltante"]} faltante(s)</b></td><td>{fmt_datetime(issue["reportado_em"])}</td><td>{ops}</td></tr>'
    with db() as con:
        seals=[dict(x) for x in con.execute('''SELECT i.*,b.codigo,u.nome ator FROM incidentes_lacre i
            JOIN bandejas b ON b.id=i.bandeja_id
            JOIN conferencias c ON c.id=i.conferencia_id JOIN usuarios u ON u.id=c.ator_id
            WHERE i.resolvido_em IS NULL ORDER BY i.id DESC''')]
    seal_rows=''
    for it in seals:
        fields=f'<input type="hidden" name="bandeja" value="{esc(it["codigo"])}">'+field(
            'Avaliação / providências para o lacre','observacoes',rows=2)
        seal_rows+=f'<div class="alert-row"><b>{esc(it["codigo"])}</b> · {esc(it["ator"])} · {fmt_datetime(it["registrado_em"])}'+(
            form('resolver_lacre',csrf,fields,'Encerrar ocorrência de lacre') if user['papel']=='gerente_estoque' else '')+'</div>'
    extras=''
    if user['papel']=='gerente_estoque':
        ds=ENGINE.divergencias(user['usuario_id'])
        if ds:
            records=''.join(f'<tr><td>{fmt_datetime(d["registrado_em"])}</td><td>{esc(d["bandeja"])}</td><td>{esc(d["item"])}</td><td>{d["quantidade"]}</td><td>{esc(d["motivo"]).replace("_"," ")}</td></tr>' for d in ds)
            extras=card('Classificação de divergências', '<div class="table-wrap"><table><thead><tr><th>Data</th><th>Bandeja</th><th>Material</th><th>Qtd.</th><th>Motivo informado</th></tr></thead><tbody>'+records+'</tbody></table></div>',
                'A classificação é declaratória; não constitui comprovação de furto ou perda.')
    seal_card=card('Ocorrências de lacre não íntegro',seal_rows if seal_rows else empty('Nenhuma ocorrência de lacre pendente.'),
                   'Resolução administrativa não equivale à liberação sanitária.')
    return extras+seal_card+card('Materiais faltantes',f'<div class="table-wrap"><table><thead><tr><th>Bandeja</th><th>Item</th><th>Esperado</th><th>Devolvido</th><th>Faltante</th><th>Data</th><th>Regularização</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows else empty('Nenhuma pendência em aberto.'), 'Bandejas com pendências ficam bloqueadas até a reposição.')


def movement_page(user,csrf):
    requested=[]
    with db() as con:
        requested=[dict(r) for r in con.execute('''SELECT m.*,b.codigo bandeja,u.nome ator,p.codigo pedido
        FROM movimentos m JOIN bandejas b ON b.id=m.bandeja_id JOIN usuarios u ON u.id=m.ator_id
        LEFT JOIN pedidos p ON p.id=m.pedido_id ORDER BY m.id DESC LIMIT 200''')]
    rows=''.join(f'<tr><td>{fmt_datetime(m["criado_em"])}</td><td><b>{esc(m["bandeja"])}</b></td><td>{esc(m["acao"].replace("_"," ").title())}</td><td>{esc(m["origem"])} → {esc(m["destino"])}</td><td>{esc(m["ator"])}</td><td>{esc(m["pedido"] or "—")}</td><td>{esc(m["observacao"])}</td></tr>' for m in requested)
    return card('Histórico de movimentações', '<div class="table-wrap"><table><thead><tr><th>Data e hora</th><th>Bandeja</th><th>Ação</th><th>Movimento</th><th>Responsável</th><th>Pedido</th><th>Observação</th></tr></thead><tbody>'+rows+'</tbody></table></div>' if rows else empty('Nenhuma movimentação registrada.'),'Últimas 200 operações')


def users_page(user,csrf):
    with db() as con:
        users=[dict(r) for r in con.execute('''SELECT u.*,c.email,c.aprovado FROM usuarios u LEFT JOIN contas c ON c.usuario_id=u.id ORDER BY u.id''')]
    rows=''
    for u in users:
        state=tag('Aprovado','ok') if u['aprovado'] else tag('Aguardando aprovação','warning') if u['email'] else tag('Sem login','danger')
        actions=''
        if u['email'] and not u['aprovado'] and u['ativo']:
            actions=form('aprovar',csrf,f'<input type="hidden" name="usuario_id" value="{u["id"]}">','Aprovar acesso',tone='secondary')
        if u['papel'] in ('medico','enfermeiro'):
            choose='<input type="hidden" name="usuario_id" value="'+str(u['id'])+'">'
            choose+=select_field('Setor de '+u['nome'],'setor',[(x,x) for x in SETORES_ATENDIMENTO],selected=u['setor'])
            actions+=form('alterar_setor',csrf,choose,'Atualizar setor',tone='secondary')
        rows+=f'<tr><td><b>{esc(u["nome"])}</b></td><td>{esc(u["email"] or "—")}</td><td>{esc(ROLE_LABEL[u["papel"]])}</td><td>{esc(u["setor"])}</td><td>{state}</td><td>{actions}</td></tr>'
    return card('Usuários e permissões','<p class="hint">O primeiro gerente é ativado automaticamente. Novos cadastros precisam ser aprovados por um gerente antes do primeiro login.</p><div class="table-wrap"><table><thead><tr><th>Profissional</th><th>E-mail</th><th>Perfil</th><th>Setor</th><th>Situação</th><th>Ação</th></tr></thead><tbody>'+rows+'</tbody></table></div>',f'{len(users)} profissionais cadastrados')


def pacientes_page(user,csrf):
    if user['papel']!='enfermeiro': return empty('Somente técnicos de enfermagem podem cadastrar pacientes.')
    pacientes=ENGINE.listar_pacientes(user['usuario_id'])
    new=card('Cadastrar paciente no setor '+esc(user['setor']),
        form('novo_paciente',csrf,field('Identificação do paciente','nome',
            placeholder='Ex.: Paciente de demonstração 11'), 'Cadastrar paciente'),
        'O setor é preenchido automaticamente pela conta do técnico de enfermagem. Use apenas dados fictícios no protótipo.')
    rows=''.join('<tr><td>'+esc(p['codigo'])+'</td><td>'+esc(p['nome'])+'</td><td>'+esc(p['setor'])+'</td><td>'+('Sim' if p['ficticio'] else 'Não')+'</td></tr>' for p in pacientes)
    return new+card('Pacientes do meu setor',
        '<div class="table-wrap"><table><thead><tr><th>Código</th><th>Identificação</th><th>Setor</th><th>Fictício</th></tr></thead><tbody>'+rows+'</tbody></table></div>' if rows else empty('Nenhum paciente cadastrado.'),
        'A listagem de pacientes de outros setores não é acessível pelo técnico de enfermagem.')


def auth_page(csrf, login=True, flash=None):
    if login:
        inner=field('E-mail institucional','email',typ='email',placeholder='nome@hospital.com')+field('Senha','senha',typ='password')
        inner=form('login',csrf,inner,'Entrar no sistema')
        note='<p class="auth-bottom">Ainda não possui conta? <a href="/cadastro">Criar cadastro</a></p>'
        title='Acesse sua conta'; subtitle='Gerencie pedidos, bandejas e movimentações em um único lugar.'
    else:
        with db() as con: first=con.execute('SELECT COUNT(*) FROM contas').fetchone()[0]==0
        inner=field('Nome completo','nome',placeholder='Nome do profissional')+field('E-mail','email',typ='email',placeholder='nome@hospital.com')
        inner+=select_field('Setor de atendimento (gerente: selecione Sem setor)','setor',
            [('', 'Sem setor / gerente de estoque')]+[(x,x) for x in SETORES_ATENDIMENTO],required=False)
        roles=[(r,ROLE_LABEL[r]) for r in PAPEIS if not first or r=='gerente_estoque']
        inner+=select_field('Perfil profissional','papel',roles)
        inner+=field('Senha (mínimo 8 caracteres)','senha',typ='password',extra='minlength="8"')
        inner=form('cadastro',csrf,inner,'Cadastrar usuário')
        note='<p class="auth-bottom">Já tem uma conta? <a href="/login">Entrar</a></p>'
        title='Crie seu acesso'; subtitle='O primeiro cadastro deve ser de um gerente. Os seguintes precisam de aprovação.'
    content=f'''<div class="auth-wrap"><div class="auth-brand"><div class="logo big"><span class="logo-icon">✳</span>tray<span class="logo-light">track</span></div><div class="auth-illustration"><span>▣</span><h2>Materiais certos.<br>Nas mãos certas.</h2><p>Rastreabilidade, organização e transparência em todas as etapas.</p></div></div><div class="auth-form-wrap"><div class="auth-card"><div class="eyebrow">CONTROLE HOSPITALAR</div><h1>{title}</h1><p class="lead">{subtitle}</p>{inner}{note}</div></div></div>'''
    return layout(title,content,flash=flash)


def read_missing(codigo, val):
    tray=next((b for b in ENGINE.listar_bandejas() if codigo.upper() in (b['codigo'],b.get('codigo_legado'))),None)
    if not tray: raise ValueError('Bandeja não localizada.')
    missing=[]
    rapid = val('conferencia_rapida')=='1'
    use_checkboxes = val('conferencia_checkbox')=='1'
    for item in tray['itens']:
        if rapid:
            raw=val('falta_id_'+str(item['id']),'0') or '0'
            try: n=int(raw)
            except ValueError: raise ValueError('Quantidades faltantes devem ser inteiras.')
        elif use_checkboxes:
            n=sum(1 for index in range(1,item['quantidade']+1)
                  if val('presente_'+str(item['id'])+'_'+str(index))!='1')
        else:
            raw=val('falta_id_'+str(item['id']),'0') or '0'
            try: n=int(raw)
            except ValueError: raise ValueError('Quantidades faltantes devem ser inteiras.')
        if n<0 or n>item['quantidade']: raise ValueError('Quantidade faltante excede o previsto.')
        if n: missing.append((item['nome'],n))
    if rapid:
        if val('kit_completo')=='1' and missing:
            raise ValueError('Há faltas informadas, mas a caixa Kit completo está marcada. Desmarque-a para registrar as faltas.')
        if val('kit_completo')!='1' and not missing:
            raise ValueError('Kit completo desmarcado: informe a quantidade faltante de pelo menos um material.')
    return missing

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'{self.address_string()} [{self.log_date_time_string()}] {fmt % args}')

    def session(self):
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: return None,None
        sid=cookie.get('tt_session')
        token=sid.value if sid else None
        with LOCK: return token, SESSIONS.get(token)

    def new_session(self):
        token=secrets.token_urlsafe(32)
        with LOCK: SESSIONS[token]={'csrf':secrets.token_urlsafe(24),'user_id':None,'flash':None}
        return token,SESSIONS[token]

    def user(self, session):
        if not session or not session.get('user_id'): return None
        with db() as con:
            row=con.execute('''SELECT c.usuario_id,u.nome,u.papel,u.setor FROM contas c JOIN usuarios u ON u.id=c.usuario_id
                   WHERE c.usuario_id=? AND c.aprovado=1 AND u.ativo=1''',(session['user_id'],)).fetchone()
        return dict(row) if row else None

    def send_html(self, body, cookie=None, status=200):
        encoded=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(encoded)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        if cookie:
            self.send_header('Set-Cookie',f'tt_session={cookie}; HttpOnly; SameSite=Lax; Path=/')
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, path, cookie=None):
        self.send_response(303)
        self.send_header('Location',path)
        if cookie: self.send_header('Set-Cookie',f'tt_session={cookie}; HttpOnly; SameSite=Lax; Path=/')
        self.send_header('Content-Length','0')
        self.end_headers()

    def send_photo(self, query, user):
        try: photo_id=int(query.get('id',['0'])[0])
        except ValueError: self.send_error(400); return
        with db() as con:
            foto=con.execute("""SELECT f.*,b.responsavel_id,b.etapa FROM fotos_conferencia f
                JOIN bandejas b ON b.id=f.bandeja_id WHERE f.id=?""",(photo_id,)).fetchone()
            if not foto: self.send_error(404); return
            # Técnicos de enfermagem veem anexos próprios ou da bandeja atualmente sob sua guarda.
            if user['papel']=='medico' or (user['papel']=='enfermeiro' and not (
                foto['autor_id']==user['usuario_id'] or (foto['responsavel_id']==user['usuario_id'] and foto['etapa'] in ('enfermagem','emprestada')))):
                self.send_error(403);return
            photo=bytes(foto['imagem'])
        self.send_response(200)
        self.send_header('Content-Type',foto['mime'])
        self.send_header('Content-Length',str(len(photo)))
        self.send_header('Content-Disposition','inline')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(photo)

    def do_GET(self):
        route=urlparse(self.path).path
        if route=='/favicon.ico': self.send_response(204); self.end_headers(); return
        token,session=self.session()
        if session is None: token,session=self.new_session(); new_cookie=token
        else: new_cookie=None
        if route=='/sair':
            with LOCK:
                SESSIONS.pop(token,None)
            self.redirect('/login',cookie='deleted; Max-Age=0'); return
        user=self.user(session)
        if route=='/foto':
            if not user: self.send_error(403);return
            self.send_photo(parse_qs(urlparse(self.path).query),user);return
        flash=session.pop('flash',None)
        if route in ('/login','/cadastro'):
            if user: self.redirect('/inicio',cookie=new_cookie);return
            self.send_html(auth_page(session['csrf'],route=='/login',flash),cookie=new_cookie);return
        if not user:
            self.redirect('/login',cookie=new_cookie);return
        route=route.strip('/') or 'inicio'
        query=parse_qs(urlparse(self.path).query)
        if (route in ('usuarios','inventario_rotativo') and user['papel']!='gerente_estoque') or (route=='pacientes' and user['papel']!='enfermeiro') or (user['papel']=='medico' and route not in ('inicio','pedidos')) or (user['papel']=='enfermeiro' and route in ('movimentacoes','pendencias')):
            self.send_html(layout('Acesso negado',empty('Essa página não está disponível para seu perfil.'),user,flash),cookie=new_cookie,status=403);return
        if route in ('leitura','historico'):
            page=leitura_page(user,session['csrf'],query) if route=='leitura' else historico_page(user,session['csrf'],query)
            self.send_html(layout('Leitura e conferência' if route=='leitura' else 'Histórico da bandeja',page,user,flash,route),cookie=new_cookie);return
        pages={'inicio':('Visão geral',overview),'pedidos':('Solicitações',pedidos_page),'bandejas':('Inventário de bandejas',trays_page),
               'movimentacoes':('Movimentações',movement_page),'pendencias':('Pendências de materiais',issues_page),'usuarios':('Equipe e acessos',users_page),'inventario_rotativo':('Inventário rotativo',inventario_rotativo_page),'pacientes':('Pacientes do setor',pacientes_page)}
        if route not in pages:
            self.send_html(layout('Página não encontrada',empty('Página não encontrada.'),user,flash),cookie=new_cookie,status=404);return
        title,func=pages[route]
        self.send_html(layout(title,func(user,session['csrf']),user,flash,route),cookie=new_cookie)

    def do_POST(self):
        if urlparse(self.path).path!='/acao': self.send_error(404);return
        length=int(self.headers.get('Content-Length','0'))
        if length>3_000_000 or length<0: self.send_error(413);return
        raw=self.rfile.read(length)
        photo=None
        if self.headers.get('Content-Type','').lower().startswith('multipart/form-data'):
            payload=(b'MIME-Version: 1.0\r\nContent-Type: '+self.headers['Content-Type'].encode('latin-1')+
                     b'\r\n\r\n'+raw)
            parsed=BytesParser(policy=EMAIL_POLICY).parsebytes(payload)
            if not parsed.is_multipart(): self.send_error(400);return
            data={}
            for part in parsed.iter_parts():
                name=part.get_param('name',header='content-disposition')
                if not name: continue
                content=part.get_payload(decode=True) or b''
                if name=='foto':
                    if part.get_filename() and content:
                        if len(content)>2_000_000:
                            self.send_error(413,'A imagem deve ter no máximo 2 MB.');return
                        if content.startswith(b'\x89PNG\r\n\x1a\n'): mime='image/png'
                        elif content.startswith(b'\xff\xd8\xff'): mime='image/jpeg'
                        elif content[:4]==b'RIFF' and content[8:12]==b'WEBP': mime='image/webp'
                        else: self.send_error(400,'Arquivo inválido: use PNG, JPEG ou WebP.');return
                        photo=(part.get_filename()[:120],mime,content)
                else:
                    if len(content)>10000: self.send_error(413);return
                    data[name]=[content.decode('utf-8')]
        else:
            if length>30000: self.send_error(413);return
            data=parse_qs(raw.decode('utf-8'),keep_blank_values=True)
        val=lambda k,default='': data.get(k,[default])[0].strip()
        token,session=self.session()
        if not session or not hmac.compare_digest(val('csrf'),session['csrf']):
            self.send_error(403,'Token de formulário inválido. Atualize a página.');return
        action=val('acao'); user=self.user(session)
        landing={'login':'/login','cadastro':'/cadastro','solicitar':'/pedidos','separar':'/pedidos','checkin':'/pedidos',
                 'nova_bandeja':'/bandejas','novo_tipo':'/bandejas','transferir_medico':'/pedidos','aceitar_transferencia':'/pedidos','recusar_transferencia':'/pedidos','cancelar_transferencia':'/pedidos','iniciar_inventario':'/inventario_rotativo','bipar_inventario':'/inventario_rotativo','encerrar_inventario':'/inventario_rotativo','checkout':'/bandejas','receber_emprestimo':'/bandejas',
                 'enviar_estoque':'/bandejas','receber_estoque':'/bandejas','regularizar':'/pendencias','resolver_lacre':'/pendencias','aprovar':'/usuarios','novo_paciente':'/pacientes','alterar_setor':'/usuarios'}.get(action,'/inicio')
        try:
            if action=='login':
                credential=authenticate(val('email'),val('senha'))
                if not credential: raise ValueError('E-mail ou senha incorretos, ou cadastro aguardando aprovação.')
                session['user_id']=credential['usuario_id']
                session['csrf']=secrets.token_urlsafe(24)
                session['flash']=('success','Login realizado com sucesso.')
                self.redirect('/inicio');return
            elif action=='cadastro':
                initial=register(val('nome'),val('email'),val('senha'),val('setor'),val('papel'))
                session['flash']=('success','Primeiro gerente criado. Faça login.' if initial else 'Cadastro recebido. Aguarde aprovação do gerente para acessar.')
                self.redirect('/login');return
            if user is None: raise ValueError('Faça login para continuar.')
            role=user['papel']; uid=user['usuario_id']
            requires={'solicitar':'medico','separar':'gerente_estoque','checkin':'enfermeiro','nova_bandeja':'gerente_estoque',
                      'novo_tipo':'gerente_estoque','transferir_medico':'medico','aceitar_transferencia':'medico','recusar_transferencia':'medico','cancelar_transferencia':'medico','iniciar_inventario':'gerente_estoque','bipar_inventario':'gerente_estoque','encerrar_inventario':'gerente_estoque','checkout':'enfermeiro','receber_emprestimo':'enfermeiro',
                      'enviar_estoque':'enfermeiro','receber_estoque':'gerente_estoque','regularizar':'gerente_estoque','resolver_lacre':'gerente_estoque','aprovar':'gerente_estoque','novo_paciente':'enfermeiro','alterar_setor':'gerente_estoque'}
            if action not in requires or role!=requires[action]: raise ValueError('Operação não permitida para seu perfil.')
            result=None
            if action=='solicitar':
                result=ENGINE.solicitar(uid,val('tipo'),val('padrao_cores_id'))
                message=f'Solicitação criada: {result["codigo_solicitacao"]}. Copie este código para o check-in.'
            elif action=='alterar_setor':
                ENGINE.atualizar_setor_usuario(uid,int(val('usuario_id')),val('setor'))
                message='Setor profissional atualizado. Pedidos antigos preservam o setor registrado na solicitação.'
            elif action=='novo_paciente':
                result=ENGINE.cadastrar_paciente(uid,val('nome'))
                message='Paciente cadastrado no seu setor: '+result['codigo']+'.'
            elif action=='separar':
                result=ENGINE.separar_estoque(uid,val('pedido'),val('bandeja'))
                message=f'Bandeja {result["bandeja"]} separada para {result["pedido"]}.'
            elif action=='checkin':
                result=ENGINE.checkin_pedido(uid,val('pedido'),val('bandeja'),read_missing(val('bandeja'),val),val('lacre_violado')!='1' if (val('conferencia_checkbox')=='1' or val('conferencia_rapida')=='1') else True,val('observacoes'),{},paciente_id=int(val('paciente_id')) if val('paciente_id').isdigit() else None)
                message=f'Check-in registrado: {result["bandeja"]} / {result["pedido"]}. Paciente vinculado: {result["paciente"]}.'
                if result['faltantes'] or not result['lacre_integro']: message+=' ATENÇÃO: falta ou lacre não íntegro; bandeja bloqueada para empréstimo.'
            elif action=='nova_bandeja':
                items=[]
                for line in val('itens').splitlines():
                    if not line.strip(): continue
                    if ':' not in line: raise ValueError(f'Item precisa do formato Nome:quantidade: {line}')
                    name,qty=line.rsplit(':',1)
                    items.append((name.strip(),int(qty.strip())))
                out=ENGINE.cadastrar_bandeja(uid,'',val('tipo'),itens=items)
                message=f'Bandeja {out["codigo"]} cadastrada com padrão {out["cores"][0]} / {out["cores"][1]}.'
            elif action=='novo_tipo':
                ENGINE.cadastrar_tipo(uid,val('nome'),val('descricao'))
                message='Novo tipo cadastrado.'
            elif action=='checkout':
                ENGINE.checkout(uid,val('bandeja'),'')
                message=f'Check-out realizado: entrega inicial ao médico solicitante registrada para {val("bandeja")}.'
            elif action=='transferir_medico':
                medico_destino_id=int(val('medico_destino_id'))
                with db() as con:
                    autorizado=con.execute('''SELECT 1 FROM usuarios u JOIN contas c ON c.usuario_id=u.id
                        WHERE u.id=? AND u.papel='medico' AND u.ativo=1 AND c.aprovado=1''',
                        (medico_destino_id,)).fetchone()
                if not autorizado: raise ValueError('O médico destinatário não está cadastrado e aprovado.')
                result=ENGINE.transferir_medico(uid,val('bandeja'),medico_destino_id)
                message=f'Transferência solicitada: {result["origem"]} → {result["destino"]}. A custódia somente mudará após o aceite.'
            elif action in ('aceitar_transferencia','recusar_transferencia'):
                result=ENGINE.decidir_transferencia(uid,int(val('transferencia_id')),
                    action=='aceitar_transferencia',val('bandeja'),val('observacoes'))
                message='Transferência '+('aceita; custódia e alertas transferidos.' if result['status']=='aceita' else 'recusada; custódia mantida com o remetente.')
            elif action=='cancelar_transferencia':
                ENGINE.cancelar_transferencia(uid,int(val('transferencia_id')))
                message='Transferência cancelada; custódia mantida.'
            elif action=='iniciar_inventario':
                result=ENGINE.iniciar_inventario(uid,val('localizacao'))
                message='Inventário #'+str(result['inventario_id'])+' iniciado para '+result['localizacao']+'.'
            elif action=='bipar_inventario':
                result=ENGINE.bipar_inventario(uid,int(val('inventario_id')),val('bandeja'))
                message='Leitura registrada. '+('Localização compatível.' if result['conforme'] else 'ATENÇÃO: bandeja encontrada fora do local esperado.')
            elif action=='encerrar_inventario':
                result=ENGINE.encerrar_inventario(uid,int(val('inventario_id')))
                message=f'Inventário encerrado: {len(result["nao_localizados"])} não localizadas e {len(result["nao_previstos"])} não previstas.' 
            elif action=='receber_emprestimo':
                issues=read_missing(val('bandeja'),val)
                result=ENGINE.receber_emprestimo(uid,val('bandeja'),issues,val('lacre_violado')!='1' if (val('conferencia_checkbox')=='1' or val('conferencia_rapida')=='1') else True,val('observacoes'),{})
                message='Devolução conferida. '+(result['observacao'] if issues else 'Todos os itens retornaram.')
            elif action=='enviar_estoque':
                ENGINE.enviar_estoque(uid,val('bandeja'))
                message='Bandeja enviada para recebimento pelo estoque.'
            elif action=='receber_estoque':
                result=ENGINE.receber_estoque(uid,val('bandeja'),read_missing(val('bandeja'),val),val('lacre_violado')!='1' if (val('conferencia_checkbox')=='1' or val('conferencia_rapida')=='1') else True,val('observacoes'),{})
                message='Recebimento confirmado: status atualizado para No estoque.' + (' Bandeja bloqueada por pendências; indisponível para novos empréstimos.' if result['pendencias'] else ' Bandeja disponível administrativamente.')
            elif action=='regularizar':
                ENGINE.regularizar_item(uid,val('bandeja'),val('item'),int(val('quantidade')))
                message='Reposição registrada. Essa operação não comprova esterilização.'
            elif action=='resolver_lacre':
                ENGINE.resolver_lacre(uid,val('bandeja'),val('observacoes'))
                message='Ocorrência do lacre encerrada; verificar demais pendências e liberação sanitária.'
            elif action=='aprovar':
                target=int(val('usuario_id'))
                with db() as con:
                    r=con.execute('''SELECT c.usuario_id FROM contas c JOIN usuarios u ON u.id=c.usuario_id
                                      WHERE c.usuario_id=? AND u.ativo=1''',(target,)).fetchone()
                    if not r: raise ValueError('Cadastro não encontrado.')
                    con.execute('UPDATE contas SET aprovado=1 WHERE usuario_id=?',(target,))
                message='Cadastro aprovado. O usuário já pode entrar com sua senha.'
            if photo:
                if action not in ('checkin','checkout','receber_emprestimo','receber_estoque'):
                    raise ValueError('Foto não permitida nessa operação.')
                with db() as con:
                    tray=con.execute('SELECT id FROM bandejas WHERE codigo=? OR codigo_legado=?',
                                     (val('bandeja'),val('bandeja'))).fetchone()
                    if not tray: raise ValueError('Bandeja para anexação não encontrada.')
                    con.execute("""INSERT INTO fotos_conferencia
                        (bandeja_id,autor_id,acao,nome,mime,imagem,criado_em)
                        VALUES (?,?,?,?,?,?,?)""",(tray['id'],uid,action,photo[0],photo[1],photo[2],
                        datetime.now(timezone.utc).isoformat(timespec='seconds')))
                message+=' Foto anexada ao histórico.'
            if result and result.get('alerta_estoque'):
                message+=' '+result['alerta_estoque']['mensagem']
            session['flash']=('success',message)
        except (RegraNegocioError,ValueError,sqlite3.Error) as exc:
            session['flash']=('error',str(exc))
        self.redirect(landing)


def main():
    init_auth()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    print(f'\n  traytrack · interface disponível em http://{HOST}:{PORT}\n  Banco de dados: {DB}\n  Pressione Ctrl+C para encerrar.\n')
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nServidor encerrado.')
    finally: server.server_close()


if __name__=='__main__':
    main()
