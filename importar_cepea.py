# -*- coding: utf-8 -*-
"""
importar_cepea.py — carrega series do CEPEA no docs/historico.json.

DE ONDE VEM O DADO
Do proprio site do CEPEA, em Consultas ao Banco de Dados, botao GERAR EXCEL:
    https://www.cepea.org.br/br/consultas-ao-banco-de-dados-do-site.aspx
Baixado pelo navegador da usuaria, nao pelo robo. Isso e proposital: o CEPEA
bloqueia acesso vindo de IP de datacenter, entao o GitHub Actions leva HTTP 403,
com requests e tambem com Chromium headless. Foi verificado tres vezes. O mesmo
vale para o Canal Rural, que era o caminho indireto.

Ou seja: esta carga e MANUAL por natureza, nao por preguica de automatizar. O
caminho para o dado diario entrar sozinho e outro (boletim por e-mail numa caixa
autenticada), e nao passa por raspagem.

FORMATO DO ARQUIVO (verificado em 05/08/2026)
  linha 1: "Milho | INDICADOR DO MILHO ESALQ/BM&FBOVESPA"
  linha 2: "Nota"  + metodologia
  linha 3: "Fonte" + "Cepea"
  linha 4: "Data" | "A vista R$" | "A vista US$"     (ou "Data" | "Valor")
  linha 5+: "01/2010" | "19,66" | "11,04"
Os .xls do CEPEA sao gravados com um defeito de estrutura que faz o xlrd recusar;
por isso abre-se com ignore_workbook_corruption=True.

O QUE ELE GRAVA
Serie propria por arquivo, com benchmark declarado ("CEPEA/ESALQ - PARANAGUA").
NAO substitui nem funde com serie de outra fonte: a serie "Boi gordo" que veio do
clipping continua existindo, e a nova entra ao lado, com o nome do indicador.
Assim ninguem compara Paranagua com Parana achando que e a mesma coisa.

USO
    python importar_cepea.py arquivo1.xls arquivo2.xls ...
    python importar_cepea.py --pasta ./downloads
"""
import json
import re
import sys
from pathlib import Path

HISTORICO = "docs/historico.json"

# Unidade deduzida da nota do proprio arquivo, nunca chutada.
REGRAS_UNIDADE = [
    (r"saca de 60\s*kg", "R$/sc 60kg"),
    (r"por arroba", "R$/@"),
    (r"por tonelada", "R$/t"),
    (r"por quilo|por kg", "R$/kg"),
    (r"por litro", "R$/litro"),
    (r"por d[uú]zia", "R$/duzia"),
    (r"por caixa", "R$/caixa"),
]


def _unidade(nota):
    n = nota.lower()
    for padrao, un in REGRAS_UNIDADE:
        if re.search(padrao, n):
            return un
    return None


def _mes(txt):
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{4})\s*", str(txt or ""))
    if not m:
        return None
    mes = int(m.group(1))
    return f"{m.group(2)}-{mes:02d}" if 1 <= mes <= 12 else None


def _num(txt):
    s = str(txt or "").strip().replace(".", "").replace(",", ".")
    if not s or s in {"-", "…", "n/d"}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def ler(caminho, notas, irmas=None):
    """Devolve (nome_serie, registro) ou (None, None)."""
    try:
        import xlrd
    except ImportError:
        notas.append("xlrd ausente: pip install xlrd")
        return None, None
    try:
        ws = xlrd.open_workbook(str(caminho),
                                ignore_workbook_corruption=True).sheet_by_index(0)
    except Exception as ex:
        notas.append(f"{Path(caminho).name}: ilegivel ({type(ex).__name__}: {str(ex)[:60]})")
        return None, None

    titulo = str(ws.cell_value(0, 0)).strip()
    nota = str(ws.cell_value(1, 1)).strip() if ws.nrows > 1 else ""
    fonte_dec = str(ws.cell_value(2, 1)).strip() if ws.nrows > 2 else "Cepea"
    if "|" not in titulo:
        notas.append(f"{Path(caminho).name}: linha 1 sem o formato 'Produto | INDICADOR'")
        return None, None
    produto, indicador = [x.strip() for x in titulo.split("|", 1)]

    # A unidade sai da nota. Quando a nota traz outro assunto (o arquivo de Soja
    # Paranagua traz um aviso sobre datas de novembro), varre-se o resto do
    # cabecalho antes de desistir; e, em ultimo caso, herda-se a unidade de outra
    # serie JA IMPORTADA do mesmo produto, marcando unidade_inferida=True. Nunca
    # se adivinha a partir da ordem de grandeza do valor.
    un = _unidade(nota)
    inferida = None
    if not un:
        for r in range(0, min(4, ws.nrows)):
            for c in range(ws.ncols):
                un = _unidade(str(ws.cell_value(r, c)))
                if un:
                    inferida = f"lida de outra celula do cabecalho (linha {r+1})"
                    break
            if un:
                break
    if not un and irmas:
        for nome_irma, un_irma in irmas.items():
            if nome_irma.lower().startswith(produto.lower()):
                un, inferida = un_irma, (f"herdada de {nome_irma!r}, mesmo produto. "
                                         f"CONFIRMAR no site do CEPEA.")
                break
    if not un:
        notas.append(f"{Path(caminho).name}: unidade nao identificada na nota. "
                     f"Nada importado. Nota lida: {nota[:70]!r}")
        return None, None

    pontos, ignoradas = [], 0
    for r in range(4, ws.nrows):
        mes = _mes(ws.cell_value(r, 0))
        val = _num(ws.cell_value(r, 1))
        if mes and val is not None:
            pontos.append([mes, val])
        elif str(ws.cell_value(r, 0)).strip():
            ignoradas += 1
    if not pontos:
        notas.append(f"{Path(caminho).name}: nenhuma linha de dado utilizavel")
        return None, None
    pontos.sort()

    nome = f"{produto} CEPEA {indicador.split('-')[-1].strip().title()}" \
        if "-" in indicador else f"{produto} CEPEA/ESALQ"
    reg = {
        "benchmark": f"CEPEA/ESALQ {indicador}",
        "unidade": un,
        "unidade_inferida": inferida,
        "fonte": f"{fonte_dec} (planilha oficial, carga manual)",
        "pontos": pontos,
        "de": pontos[0][0], "ate": pontos[-1][0], "n": len(pontos),
        "nota": (f"{nota[:260]} | Importado de {Path(caminho).name} em carga manual: "
                 f"o CEPEA bloqueia acesso automatizado de datacenter."),
    }
    if inferida:
        notas.append(f"{nome}: unidade {un} {inferida}")
    notas.append(f"{nome}: {len(pontos)} meses, {pontos[0][0]} a {pontos[-1][0]}, {un}"
                 + (f", {ignoradas} linha(s) ignorada(s)" if ignoradas else ""))
    return nome, reg


def importar(arquivos, caminho_hist=HISTORICO):
    notas = []
    p = Path(caminho_hist)
    if not p.exists():
        return [f"{caminho_hist} nao existe"]
    doc = json.loads(p.read_text(encoding="utf-8"))
    novas = atualizadas = 0
    irmas = {k: v["unidade"] for k, v in doc["series"].items() if "CEPEA" in str(v.get("benchmark","")).upper()}
    for arq in arquivos:
        nome, reg = ler(arq, notas, irmas)
        if nome:
            irmas[nome] = reg["unidade"]
        if not nome:
            continue
        if nome in doc["series"]:
            antigos = {m: v for m, v in (doc["series"][nome].get("pontos") or [])}
            for m, v in reg["pontos"]:
                antigos[m] = v
            reg["pontos"] = sorted([[m, v] for m, v in antigos.items()])
            reg["de"], reg["ate"], reg["n"] = reg["pontos"][0][0], reg["pontos"][-1][0], len(reg["pontos"])
            atualizadas += 1
        else:
            novas += 1
        doc["series"][nome] = reg
    doc["cepea_importado_em"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    notas.append(f"historico.json: {novas} serie(s) nova(s), {atualizadas} atualizada(s), "
                 f"{len(doc['series'])} no total")
    return notas


def importar_navegador(caminho_json, caminho_hist=HISTORICO):
    """Carrega o arquivo exportado pelo botao do site (acumulo no navegador).

    O acumulo e DIARIO e o historico e MENSAL, entao aqui calcula-se a media do
    mes. ATENCAO: media dos dias capturados NAO e a media mensal oficial do CEPEA.
    Se a pagina foi aberta em 6 dias do mes, a media e de 6 dias. Por isso cada
    ponto guarda dias_capturados e a serie sai com media_parcial=True. Para a
    media oficial, a fonte e a planilha do site do CEPEA.
    """
    import collections
    notas = []
    doc = json.loads(Path(caminho_json).read_text(encoding="utf-8"))
    regs = doc.get("registros") or []
    if not regs:
        return ["arquivo do navegador sem registros"]
    por = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in regs:
        d = str(r.get("data") or "")
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", d)
        v = _num(r.get("valor"))
        if not (m and v is not None):
            continue
        por[str(r.get("nome") or "?").strip()][f"{m.group(3)}-{m.group(2)}"].append(v)

    p = Path(caminho_hist)
    hist = json.loads(p.read_text(encoding="utf-8"))
    for nome, meses in por.items():
        chave = f"{nome.title()} CEPEA (acumulado no navegador)"
        pontos, dias = [], {}
        for mes, vals in sorted(meses.items()):
            pontos.append([mes, round(sum(vals) / len(vals), 4)])
            dias[mes] = len(vals)
        hist["series"][chave] = {
            "benchmark": f"CEPEA/ESALQ {nome} (leitura do widget)",
            "unidade": next((r.get("unidade") for r in regs
                             if str(r.get("nome")).strip() == nome and r.get("unidade")), None),
            "fonte": "CEPEA/ESALQ via widget, acumulado no navegador",
            "pontos": pontos, "de": pontos[0][0], "ate": pontos[-1][0], "n": len(pontos),
            "media_parcial": True,
            "dias_capturados_por_mes": dias,
            "nota": ("Media dos dias em que a pagina foi aberta, NAO a media mensal "
                     "oficial do CEPEA. Use para acompanhamento; para serie oficial, "
                     "baixe a planilha em Consultas ao Banco de Dados do CEPEA."),
        }
        notas.append(f"{chave}: {len(pontos)} mes(es), dias por mes: {dias}")
    p.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    notas.append(f"historico.json: {len(por)} serie(s) do navegador, {len(hist['series'])} no total")
    return notas


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--navegador":
        for n in importar_navegador(args[1]):
            print(" ", n)
        sys.exit(0)
    if args and args[0] == "--pasta":
        arquivos = sorted(Path(args[1]).glob("*.xls"))
    else:
        arquivos = [Path(a) for a in args] or sorted(Path(".").glob("cepea-consulta-*.xls"))
    if not arquivos:
        print("uso: python importar_cepea.py arquivo.xls [...]  |  --pasta ./downloads")
        sys.exit(1)
    for n in importar(arquivos):
        print(" ", n)
