# -*- coding: utf-8 -*-
"""
cepea_canalrural.py — indicadores CEPEA em R$/saca e R$/@, por via indireta.

POR QUE INDIRETO
A execucao de 05/08/2026 fechou a questao: o widget do CEPEA devolve HTTP 403
para requests E para um Chromium headless de verdade, com cabecalho completo e
Referer, e a pagina publica de indicadores da timeout. Nao e cabecalho: e
bloqueio por IP de datacenter. O GitHub Actions nao alcanca o CEPEA, e navegador
nao resolve isso.

O widget continua na pagina do radar e funciona, porque ali ele roda no navegador
de quem visita. O que nao dava era GRAVAR o valor no historico.

SOLUCAO
O Canal Rural publica os indicadores CEPEA em texto corrido, diariamente, e o RSS
do Canal Rural ja responde HTTP 200 no radar. Entao o valor vem de la.

O QUE ISSO SIGNIFICA METODOLOGICAMENTE, e fica gravado em cada registro:
  - o numero e do CEPEA/ESALQ, mas a LEITURA e de uma materia do Canal Rural;
  - "data_referencia" e a data de PUBLICACAO da materia, nao necessariamente o
    dia do indicador: materia publicada na sexta costuma trazer o indicador da
    sexta, mas materia de sabado ou de segunda pode trazer o de sexta;
  - por isso todo registro sai com e_derivado=True e a ressalva no campo
    "metodologia". Para conta que precise da data exata do indicador, a fonte e o
    CEPEA direto, nao isto aqui.

Padroes reconhecidos, extraidos de materia real do Canal Rural (24/07/2026):
  "o indicador Cepea/Esalq Paranagua fechou a R$ 148,37 por saca de 60 quilos"
  "No Parana, o indicador Cepea/Esalq encerrou o dia em R$ 140,26 por saca"
  "O indicador do milho Esalq/BM&FBovespa fechou ... a R$ 65,74 por saca de 60 quilos"
  "o indicador do boi gordo Cepea/Esalq encerrou o pregao em R$ 344,20 por arroba"
"""
import re
from datetime import datetime, timezone

import requests

TIMEOUT = 40
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}

RSS_PADRAO = "https://www.canalrural.com.br/feed"

# (key, label, unidade, [regex]). A ordem importa: soja Paranagua antes de soja
# generica, senao a generica captura a linha de Paranagua.
NUM = r"R\$\s*([\d]{1,3}(?:\.\d{3})*,\d{2})"
ALVOS = [
    ("soja_paranagua_cr", "Soja CEPEA/ESALQ Paranagua", "R$/sc 60kg", [
        rf"Cepea\s*/\s*Esalq\s+Paranagu[aá][^.]{{0,80}}?{NUM}",
        rf"Paranagu[aá][^.]{{0,60}}?{NUM}\s*por\s+saca",
    ]),
    ("soja_parana_cr", "Soja CEPEA/ESALQ Parana", "R$/sc 60kg", [
        rf"No\s+Paran[aá][^.]{{0,90}}?Cepea\s*/\s*Esalq[^.]{{0,60}}?{NUM}",
    ]),
    ("milho_esalq_cr", "Milho ESALQ/B3 Campinas-SP", "R$/sc 60kg", [
        rf"milho[^.]{{0,90}}?(?:Esalq|Cepea)[^.]{{0,80}}?{NUM}\s*por\s+saca",
        rf"indicador\s+do\s+milho[^.]{{0,110}}?{NUM}",
    ]),
    ("boi_gordo_cr", "Boi gordo CEPEA/ESALQ SP", "R$/@", [
        rf"boi\s+gordo[^.]{{0,110}}?{NUM}\s*por\s+arroba",
        rf"boi\s+gordo[^.]{{0,110}}?{NUM}",
    ]),
]

# Faixas de sanidade. Servem contra captura de numero errado na frase (percentual,
# preco de outro produto, valor de contrato). Fora da faixa, NAO publica.
FAIXAS = {"soja_paranagua_cr": (60, 400), "soja_parana_cr": (60, 400),
          "milho_esalq_cr": (25, 200), "boi_gordo_cr": (150, 700)}


def _texto(html):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&#8217;", "'"), ("&quot;", '"')):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


TEM_VALOR_BR = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}")


def _num(s):
    return float(s.replace(".", "").replace(",", "."))


def abrir_materia(link, notas, titulo):
    """Baixa o texto da materia. Tres tentativas, em ordem de custo.

    Motivo: em 05/08/2026 o RSS do Canal Rural respondeu 200 e achou a materia, mas
    a PAGINA devolveu "403 Client Error: Forbidden". Ou seja, o gargalo mudou de
    lugar: nao e achar, e abrir. Como o workflow ja instala o Chromium para o probe
    do CEPEA, usar o navegador aqui nao custa nada a mais.
    """
    try:
        r = requests.get(link, headers=UA, timeout=TIMEOUT)
        if r.ok:
            return _texto(r.text), "requests"
        motivo = f"HTTP {r.status_code}"
    except Exception as ex:
        motivo = f"{type(ex).__name__}"
    notas.append(f"CEPEA/Canal Rural: requests falhou ({motivo}) em {titulo[:44]}, "
                 f"tentando navegador")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        notas.append("CEPEA/Canal Rural: playwright ausente, sem navegador de reserva")
        return None, None
    try:
        with sync_playwright() as pw:
            nav = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                pg = nav.new_context(locale="pt-BR").new_page()
                pg.goto(link, wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(1200)
                return _texto(pg.content()), "navegador"
            finally:
                nav.close()
    except Exception as ex:
        notas.append(f"CEPEA/Canal Rural: navegador tambem falhou "
                     f"{type(ex).__name__}: {str(ex)[:80]}")
        return None, None


def achar_materias(rss_url, notas, maximo=6):
    """Devolve [(titulo, link, data_iso)] das materias de indicador CEPEA no RSS."""
    try:
        r = requests.get(rss_url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as ex:
        notas.append(f"CEPEA/Canal Rural: RSS falhou {type(ex).__name__}: {str(ex)[:90]}")
        return []
    itens = re.findall(r"<item>(.*?)</item>", r.text, re.S | re.I)
    saida = []
    for it in itens:
        tit = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        lnk = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", it, re.S)
        dat = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not (tit and lnk):
            continue
        titulo = _texto(tit.group(1)).strip()
        desc = _texto(re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>",
                                it, re.S).group(1)) if "<description>" in it else ""
        # BUG corrigido: a versao anterior exigia "Cepea" NO TITULO. Na execucao de
        # 05/08/2026 isso devolveu "nenhuma materia com 'Cepea' no titulo no RSS de
        # hoje": o Canal Rural nem sempre poe a palavra no titulo, e o RSS traz so
        # ~10 itens. Agora: procura no titulo OU no resumo, e aceita tambem materia
        # de mercado de graos e boi mesmo sem citar Cepea. Falso positivo e barato,
        # porque a extracao do corpo exige a frase "indicador Cepea/Esalq ... R$ X"
        # e ainda passa pela guarda de faixa. Falso NEGATIVO custa o indicador do dia.
        alvo_txt = (titulo + " " + desc).lower()
        cita_cepea = "cepea" in alvo_txt or "esalq" in alvo_txt
        fala_de_preco = any(p in alvo_txt for p in
                            ("indicador", "cotac", "fecha", "encerra", "alta", "queda",
                             "avanc", "recua", "sobe", "cai", "preco", "preço", "mercado"))
        fala_de_produto = any(p in alvo_txt for p in
                              ("soja", "milho", "boi", "trigo", "arroba", "saca"))
        if not (cita_cepea or (fala_de_preco and fala_de_produto)):
            continue
        data_iso = None
        if dat:
            try:
                data_iso = datetime.strptime(dat.group(1).strip()[:25],
                                             "%a, %d %b %Y %H:%M:%S").date().isoformat()
            except ValueError:
                data_iso = None
        saida.append((titulo, lnk.group(1).strip(), data_iso, desc))
        if len(saida) >= maximo:
            break
    if not saida:
        notas.append(f"CEPEA/Canal Rural: nenhuma das {len(itens)} materias do RSS parece falar "
                     f"de indicador de preco. Titulos vistos: "
                     + " | ".join(_texto(re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>',
                                   i, re.S).group(1))[:52] for i in itens[:6]
                                   if "<title>" in i))
    return saida


def extrair(texto, data_ref, titulo, link, notas):
    """Extrai indicadores do texto de UMA materia. Devolve lista de registros."""
    achados, vistos = [], set()
    for key, label, unidade, padroes in ALVOS:
        if key in vistos:
            continue
        for pad in padroes:
            m = re.search(pad, texto, re.I)
            if not m:
                continue
            v = _num(m.group(1))
            lo, hi = FAIXAS[key]
            if not (lo <= v <= hi):
                notas.append(f"CEPEA/Canal Rural: {label} capturou {v}, fora da faixa "
                             f"{lo}-{hi} {unidade}. Descartado, provavel numero errado na frase.")
                continue
            achados.append({
                "key": key, "label": label, "unidade": unidade,
                "preco": v,
                "preco_texto": m.group(1),
                "data_referencia": data_ref,
                "fonte": "CEPEA/ESALQ via Canal Rural",
                "e_derivado": True,
                "metodologia": ("Valor do indicador CEPEA/ESALQ lido de materia do Canal "
                                "Rural, porque o CEPEA bloqueia acesso automatizado de "
                                "datacenter. A data e a de PUBLICACAO da materia e pode ser "
                                "um dia util depois do dia do indicador. Para data exata do "
                                "indicador, consultar o CEPEA direto."),
                "materia": titulo[:160],
                "link": link,
                "trecho_origem": texto[max(0, m.start() - 60):m.end() + 40].strip()[:220],
            })
            vistos.add(key)
            break
    return achados


def fetch_cepea_canalrural(cfg, now, notas):
    c = (cfg.get("cepea_canalrural") or {})
    if not c.get("enabled"):
        notas.append("CEPEA via Canal Rural desativado no sources.yml")
        return []
    rss = c.get("rss", RSS_PADRAO)
    limite = int(c.get("max_idade_dias", 5))
    materias = achar_materias(rss, notas, int(c.get("max_materias", 3)))
    coletados, por_key = [], set()
    for titulo, link, data_iso, desc in materias:
        data_ref = data_iso or now.date().isoformat()
        idade = (now.date() - datetime.fromisoformat(data_ref).date()).days
        if idade > limite:
            notas.append(f"CEPEA/Canal Rural: materia de {data_ref} com {idade} dias "
                         f"(limite {limite}), ignorada")
            continue
        # 1) o proprio resumo do RSS as vezes ja tem o indicador, e sai de graca
        corpo, via = (_texto(desc), "resumo do RSS") if desc and TEM_VALOR_BR.search(desc) else (None, None)
        # 2) senao, abre a materia (requests, e navegador como reserva)
        if corpo is None:
            corpo, via = abrir_materia(link, notas, titulo)
        if not corpo:
            continue
        for reg in extrair(corpo, data_ref, titulo, link, notas):
            reg["via"] = via
            if reg["key"] in por_key:
                continue
            por_key.add(reg["key"])
            coletados.append(reg)
    if coletados:
        notas.append("CEPEA/Canal Rural: " + ", ".join(
            f"{r['label'].split(' CEPEA')[0].split(' ESALQ')[0]}={r['preco_texto']}"
            for r in coletados))
    else:
        notas.append("CEPEA/Canal Rural: nenhum indicador extraido nesta execucao")
    return coletados


# ------------------------------------------------------------------ testes
# Texto REAL de materia do Canal Rural (capturado em 05/08/2026).
REAL = ("No mercado da soja, o indicador Cepea/Esalq Paranagua fechou a R$ 148,37 por saca "
        "de 60 quilos, alta de 0,61% em relacao a quinta-feira (23). No Parana, o indicador "
        "Cepea/Esalq encerrou o dia em R$ 140,26 por saca, com ganho diario de 0,70%. Na "
        "comparacao com a segunda-feira, quando o preco era de R$ 135,73, a valorizacao foi "
        "de 3,34%. Em julho, o indicador acumula alta de 10,07%. O indicador do milho "
        "Esalq/BM&FBovespa fechou a sexta-feira cotado a R$ 65,74 por saca de 60 quilos, "
        "avanco de 0,29% frente ao dia anterior. Ja o indicador do boi gordo Cepea/Esalq "
        "encerrou o pregao em R$ 344,20 por arroba, valorizacao diaria de 0,13%.")


def _testes():
    notas = []
    regs = extrair(REAL, "2026-07-24", "Cepea: soja, milho e boi gordo", "https://x", notas)
    got = {r["key"]: r["preco"] for r in regs}
    esperado = {"soja_paranagua_cr": 148.37, "soja_parana_cr": 140.26,
                "milho_esalq_cr": 65.74, "boi_gordo_cr": 344.20}
    for k, v in esperado.items():
        assert got.get(k) == v, f"{k}: esperado {v}, veio {got.get(k)}"
    for r in regs:
        print(f"ok  {r['label']:34} {r['preco_texto']:>9} {r['unidade']:<11} "
              f"derivado={r['e_derivado']}")

    # faixa: numero absurdo na frase nao publica
    ruim = REAL.replace("R$ 344,20 por arroba", "R$ 3.442,00 por arroba")
    n2 = []
    regs2 = extrair(ruim, "2026-07-24", "t", "l", n2)
    assert not any(r["key"] == "boi_gordo_cr" for r in regs2), "publicou fora da faixa"
    assert any("fora da faixa" in x for x in n2), n2
    print("ok  guarda de faixa:", [x for x in n2 if "fora da faixa" in x][0][:88])

    # texto sem indicador nenhum
    assert extrair("Materia sobre clima, sem indicador.", "2026-08-05", "t", "l", []) == []
    print("ok  materia sem indicador devolve vazio")
    print("\ntodos os testes passaram")


if __name__ == "__main__":
    _testes()
