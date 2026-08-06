# -*- coding: utf-8 -*-
"""
cotacoes_fertilizantes.py — modulo novo para o Radar Agro.

Cobre a lacuna do painel atual: MAP, ureia e KCl.

Duas camadas independentes, nunca misturadas na mesma linha:

  A) BENCHMARK INTERNACIONAL (mensal)
     Serie World Bank / Fertilizer Week espelhada no IndexMundi, que serve a
     tabela mes-a-mes em HTML. Traz Ureia, KCl (cloreto de potassio), DAP,
     TSP e rocha fosfatica. NAO traz MAP.
     A base metodologica (incoterm e praca) NAO e escrita por nos: e lida do
     campo "Description:" da propria pagina e gravada em "metodologia".
     Motivo: a definicao da serie de ureia ja mudou de praca no passado
     (Europa do Leste / Mar Negro / Oriente Medio). Fixar isso no codigo seria
     rotular o numero com uma base que pode nao ser a dele.

  B) VALOR UNITARIO DE IMPORTACAO BRASIL (mensal, API oficial)
     Comex Stat / MDIC. Cobre MAP, que nao tem fonte aberta de preco de mercado.
     ATENCAO: valor unitario de importacao NAO e cotacao de mercado. E
     US$ CIF ou FOB dividido por tonelada desembarcada, media do mes, com mix
     de origem e de contrato dentro. Entra no painel com
     "e_proxy": True e rotulo dizendo o que e. Nunca comparar contra
     benchmark spot sem dizer que sao metodologias diferentes.

Nada aqui inventa valor. Fonte que falha volta lista vazia e nota com o motivo.

NAO TESTADO CONTRA A REDE neste ambiente (sandbox sem saida para
indexmundi.com nem api-comexstat.mdic.gov.br). O parser da camada A foi
testado contra amostra real capturada em 05/08/2026 (ver testes no final).
Rodar probe.py antes de ligar em producao.
"""
import json
import re
import time
from datetime import date, datetime

import requests

TIMEOUT = 40
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
}

MESES_ABREV = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MESES_CHEIO = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
               "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
               "november": 11, "december": 12}


def _texto(html):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", " \n ", t, flags=re.I)
    t = re.sub(r"</t[dhr]>", " | ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"[ \t]+", " ", t)


def _fmt_br(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------- camada A
def parse_indexmundi(html, label, key, unidade_esperada="US Dollars per Metric Ton"):
    """Devolve (registro, motivo). registro=None quando nao da para confiar.

    Exige tres coisas para publicar:
      1. cabecalho "Data as of <Mes> <Ano>";
      2. a ultima linha da tabela mes-a-mes ser exatamente esse mes;
      3. a unidade declarada ser US$/t.
    Se a tabela e o cabecalho discordarem, nao publica: e sinal de que a
    pagina mudou de layout ou de que a serie foi revisada no meio.
    """
    txt = _texto(html)

    mu = re.search(r"Unit:\s*([A-Za-z$ ]+?)\s*(?:Currency|\|)", txt)
    unidade_lida = (mu.group(1).strip() if mu else "")
    if unidade_esperada.lower() not in unidade_lida.lower():
        return None, f"{label}: unidade da pagina e {unidade_lida!r}, esperado {unidade_esperada!r}"

    mh = re.search(r"Data as of\s*\**\s*([A-Za-z]+)\s+(\d{4})", txt)
    if not mh or mh.group(1).lower() not in MESES_CHEIO:
        return None, f"{label}: cabecalho 'Data as of <mes> <ano>' nao encontrado"
    mes_cab, ano_cab = MESES_CHEIO[mh.group(1).lower()], int(mh.group(2))

    linhas = re.findall(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\|?\s*"
        r"(\d{4})\s*\|?\s*((?:\d{1,3},)*\d+\.\d{2,3})\b", txt)
    if not linhas:
        return None, f"{label}: tabela mes-a-mes nao encontrada no texto da pagina"

    serie = []
    for abrev, ano, valor in linhas:
        serie.append((int(ano), MESES_ABREV[abrev.lower()],
                      float(valor.replace(",", ""))))
    serie.sort(key=lambda x: (x[0], x[1]))
    ano_u, mes_u, preco = serie[-1]

    if (ano_u, mes_u) != (ano_cab, mes_cab):
        return None, (f"{label}: cabecalho diz {ano_cab}-{mes_cab:02d} mas a ultima linha da "
                      f"tabela e {ano_u}-{mes_u:02d}, divergencia nao publicada")

    md = re.search(r"Description:\s*(.+?)\s*(?:\||Unit:)", txt, re.S)
    metodologia = re.sub(r"\s+", " ", md.group(1)).strip()[:300] if md else ""
    if not metodologia:
        return None, f"{label}: campo 'Description' ausente, sem base metodologica para rotular"

    mf = re.search(r"Source:\s*(.+?)\s*(?:\||See also)", txt, re.S)
    fonte_serie = re.sub(r"\s+", " ", mf.group(1)).strip()[:120] if mf else "nao declarada"

    anterior = serie[-2][2] if len(serie) > 1 else None
    var = round((preco / anterior - 1) * 100, 2) if anterior else None

    return {
        "key": key,
        "label": label,
        "preco": preco,
        "preco_texto": _fmt_br(preco),
        "unidade": "US$/t",
        "data_referencia": date(ano_u, mes_u, 1).isoformat(),
        "referencia_mensal": f"{ano_u}-{mes_u:02d}",
        "variacao_mes_pct": var,
        "metodologia": metodologia,
        "e_proxy": False,
        "fonte": f"IndexMundi (serie {fonte_serie})",
        "serie": [{"mes": f"{a}-{m:02d}", "preco": p} for a, m, p in serie[-24:]],
    }, f"{label}: ok ({preco} US$/t, ref {ano_u}-{mes_u:02d})"


IM_ALVOS = [
    ("urea", "ureia_wb", "Ureia"),
    ("potassium-chloride", "kcl_wb", "Cloreto de potassio (KCl)"),
    ("dap-fertilizer", "dap_wb", "DAP"),
    ("triple-superphosphate", "tsp_wb", "TSP"),
    ("rock-phosphate", "rocha_fosfatica_wb", "Rocha fosfatica"),
]


def fetch_indexmundi(cfg, hoje, notas):
    c = (cfg.get("indexmundi") or {})
    if not c.get("enabled"):
        notas.append("IndexMundi (fertilizantes) desativado no sources.yml")
        return []
    base = c.get("base_url", "https://www.indexmundi.com/commodities/?commodity=")
    meses_url = int(c.get("meses_historico", 60))
    # Serie mensal do World Bank sai com atraso. Limite proprio, separado do
    # limite diario das outras fontes: 100 dias cobre atraso de ate ~3 meses.
    limite = int(c.get("max_idade_dias", 100))
    alvos = c.get("alvos") or [{"slug": s, "key": k, "label": l} for s, k, l in IM_ALVOS]
    saida = []
    for a in alvos:
        url = f"{base}{a['slug']}&months={meses_url}"
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            reg, motivo = parse_indexmundi(r.text, a["label"], a["key"])
            notas.append(motivo)
            if not reg:
                continue
            idade = (hoje - datetime.fromisoformat(reg["data_referencia"]).date()).days
            reg["atraso_dias"] = idade
            if idade > limite:
                notas.append(f"{a['label']}: descartado, referencia {reg['referencia_mensal']} "
                             f"com {idade} dias de atraso (limite {limite})")
                continue
            saida.append(reg)
        except Exception as ex:
            notas.append(f"{a['label']}: falha {type(ex).__name__}: {str(ex)[:90]}")
    return saida


# --------------------------------------------------------------- camada B
CS_URL = "https://api-comexstat.mdic.gov.br/general"
CS_ALVOS = [
    ("310540", "map_import_br", "MAP e misturas com DAP, valor unitario de importacao"),
    ("310210", "ureia_import_br", "Ureia, valor unitario de importacao"),
    ("310420", "kcl_import_br", "KCl, valor unitario de importacao"),
]


def montar_payload_comexstat(hs6, de, ate, metrica_valor="metricFOB"):
    """de/ate no formato AAAA-MM. Estrutura conforme api-comexstat.mdic.gov.br/docs."""
    return {
        "flow": "import",
        "monthDetail": True,
        "period": {"from": de, "to": ate},
        "filters": [{"filter": "sh6", "values": [int(hs6)]}],
        "details": ["sh6"],
        "metrics": [metrica_valor, "metricKG"],
    }


def parse_comexstat(payload_resp, hs6, key, label, metrica_valor="metricFOB"):
    """Devolve (registro, motivo). Calcula US$/t = valor / (kg/1000)."""
    try:
        lista = payload_resp["data"]["list"]
    except (KeyError, TypeError):
        return None, f"{label}: resposta sem data.list, formato inesperado"
    if not lista:
        return None, f"{label}: Comex Stat devolveu lista vazia para HS6 {hs6}"

    linhas = []
    for row in lista:
        ano = str(row.get("year") or row.get("coYear") or "").strip()
        mes = str(row.get("monthNumber") or row.get("month") or "").strip()
        if not (ano.isdigit() and mes.strip("0").isdigit()):
            continue
        try:
            valor = float(row[metrica_valor])
            kg = float(row["metricKG"])
        except (KeyError, TypeError, ValueError):
            continue
        if kg <= 0:
            continue
        linhas.append((int(ano), int(mes), valor / (kg / 1000.0)))
    if not linhas:
        return None, f"{label}: nenhuma linha com {metrica_valor} e metricKG utilizaveis"

    linhas.sort(key=lambda x: (x[0], x[1]))
    ano_u, mes_u, usd_t = linhas[-1]
    if not (50 <= usd_t <= 3000):
        return None, (f"{label}: valor unitario {usd_t:.2f} US$/t fora da faixa plausivel "
                      f"50-3000, descartado")
    anterior = linhas[-2][2] if len(linhas) > 1 else None
    var = round((usd_t / anterior - 1) * 100, 2) if anterior else None
    incoterm = "FOB" if metrica_valor == "metricFOB" else "CIF"
    return {
        "key": key,
        "label": label,
        "preco": round(usd_t, 2),
        "preco_texto": _fmt_br(usd_t),
        "unidade": "US$/t",
        "data_referencia": date(ano_u, mes_u, 1).isoformat(),
        "referencia_mensal": f"{ano_u}-{mes_u:02d}",
        "variacao_mes_pct": var,
        "e_proxy": True,
        "metodologia": (f"Valor unitario de importacao ({incoterm}) = {metrica_valor} / "
                        f"tonelada desembarcada, media do mes, HS6 {hs6}, todas as origens. "
                        f"NAO e cotacao spot de mercado."),
        "fonte": "Comex Stat / MDIC, API general",
        "serie": [{"mes": f"{a}-{m:02d}", "preco": round(p, 2)} for a, m, p in linhas[-24:]],
    }, f"{label}: ok ({usd_t:.2f} US$/t {incoterm}, ref {ano_u}-{mes_u:02d})"


PAUSA_CS, PAUSA_429, MAX_429 = 2.0, 25.0, 4


def _variantes_payload(alvo, de, ate, metrica):
    """Ordem de tentativa do payload do Comex Stat.

    O HTTP 400 de 05/08/2026 diz que a estrutura foi recusada, mas nao qual campo.
    Em vez de chutar uma unica correcao, o modulo tenta as hipoteses plausiveis em
    ordem, registra o corpo do erro de cada uma e memoriza a primeira que funciona.
    """
    # A execucao de 05/08/2026 respondeu: o filtro "sh6" e recusado com
    # {"error":{"code":400,"message":"Filtro invalido"}}, e a variante que funciona
    # e "ncm8 string", filtro "ncm" com o NCM de 8 digitos como texto. Ela vem
    # primeiro agora: antes eram 5 chamadas desperdicadas por alvo, o que estourava
    # o limite da API e gerava HTTP 429.
    v = []
    if alvo.get("ncm8"):
        v += [
            {"nome": "ncm8 string",
             "payload": lambda a, d, t, m: {**montar_payload_comexstat(a["hs6"], d, t, m),
                                            "filters": [{"filter": "ncm",
                                                         "values": [str(a["ncm8"])]}],
                                            "details": ["ncm"]}},
            {"nome": "ncm8 int",
             "payload": lambda a, d, t, m: {**montar_payload_comexstat(a["hs6"], d, t, m),
                                            "filters": [{"filter": "ncm",
                                                         "values": [int(a["ncm8"])]}],
                                            "details": ["ncm"]}},
        ]
    v += [
        {"nome": "sh6 int",
         "payload": lambda a, d, t, m: montar_payload_comexstat(a["hs6"], d, t, m)},
        {"nome": "sh6 string",
         "payload": lambda a, d, t, m: {**montar_payload_comexstat(a["hs6"], d, t, m),
                                        "filters": [{"filter": "sh6", "values": [str(a["hs6"])]}]}},
        {"nome": "sh6 int, sem details",
         "payload": lambda a, d, t, m: {k: x for k, x in
                                        montar_payload_comexstat(a["hs6"], d, t, m).items()
                                        if k != "details"}},
        {"nome": "sh6 int + language pt",
         "payload": lambda a, d, t, m: {**montar_payload_comexstat(a["hs6"], d, t, m),
                                        "language": "pt"}},
        {"nome": "sh6 int, monthDetail false",
         "payload": lambda a, d, t, m: {**montar_payload_comexstat(a["hs6"], d, t, m),
                                        "monthDetail": False}},
    ]
    return v


def fetch_comexstat(cfg, hoje, notas):
    c = (cfg.get("comexstat") or {})
    if not c.get("enabled"):
        notas.append("Comex Stat desativado no sources.yml")
        return []
    metrica = c.get("metrica_valor", "metricFOB")
    limite = int(c.get("max_idade_dias", 100))
    meses = int(c.get("meses_historico", 24))
    # aritmetica de mes com base 0, senao dezembro vira mes 13
    total = (hoje.year * 12 + hoje.month - 1) - meses
    de = f"{total // 12}-{total % 12 + 1:02d}"
    ate = f"{hoje.year}-{hoje.month:02d}"
    alvos = c.get("alvos") or [{"hs6": h, "key": k, "label": l} for h, k, l in CS_ALVOS]
    saida = []
    variante_boa = None       # descoberta uma vez, reusada nos demais alvos
    for a in alvos:
        variantes = ([variante_boa] if variante_boa
                     else _variantes_payload(a, de, ate, metrica))
        reg = None
        tentativas_429 = 0
        fila = list(variantes)
        while fila:
            v = fila[0]
            payload = v["payload"](a, de, ate, metrica) if callable(v.get("payload")) else v["payload"]
            try:
                r = requests.post(CS_URL, json=payload,
                                  headers={**UA, "Content-Type": "application/json"},
                                  timeout=TIMEOUT)
            except Exception as ex:
                notas.append(f"{a['label']} [{v['nome']}]: {type(ex).__name__}: {str(ex)[:90]}")
                time.sleep(PAUSA_CS)
                fila.pop(0)
                continue
            if r.status_code == 429:
                # BUG corrigido: antes o 429 fazia "continue", ou seja, GASTAVA a
                # variante. Na execucao de 05/08/2026 o MAP levou 429 nas cinco
                # primeiras tentativas, e quando o limite liberou so restavam as
                # variantes sh6, que sao invalidas. Resultado: MAP e KCl ficaram de
                # fora, e a ureia entrou so porque o limite ja tinha liberado nela.
                # 429 nao e recusa de payload: e "espere". Agora espera e repete a
                # MESMA variante.
                if tentativas_429 >= MAX_429:
                    notas.append(f"{a['label']} [{v['nome']}]: HTTP 429 ainda depois de "
                                 f"{MAX_429} esperas. Alvo abandonado nesta execucao.")
                    break
                tentativas_429 += 1
                notas.append(f"{a['label']} [{v['nome']}]: HTTP 429, espera "
                             f"{PAUSA_429:.0f}s e repete a MESMA variante "
                             f"({tentativas_429}/{MAX_429}).")
                time.sleep(PAUSA_429)
                continue          # NAO faz fila.pop: repete esta mesma variante
            if r.status_code >= 400:
                # o corpo do erro e onde a API diz o que recusou. Sem isso, nao da
                # para consertar o payload sem chutar.
                notas.append(f"{a['label']} [{v['nome']}]: HTTP {r.status_code}, "
                             f"corpo: {r.text[:200]!r}")
                time.sleep(PAUSA_CS)
                fila.pop(0)
                if r.status_code == 400:
                    continue      # payload recusado: vale tentar a variante seguinte
                # 401, 403, 500, 502: problema de acesso ou do servidor, nao do
                # payload. Testar as outras variantes so gasta chamada e ajuda a
                # estourar o limite da API. Para aqui e reporta.
                notas.append(f"{a['label']}: HTTP {r.status_code} nao e recusa de payload. "
                             f"Variantes restantes nao testadas.")
                break
            try:
                corpo = r.json()
            except Exception:
                notas.append(f"{a['label']} [{v['nome']}]: resposta nao e JSON: {r.text[:120]!r}")
                time.sleep(PAUSA_CS)
                fila.pop(0)
                continue
            reg, motivo = parse_comexstat(corpo, a["hs6"], a["key"], a["label"], metrica)
            notas.append(f"{a['label']} [{v['nome']}]: {motivo}")
            time.sleep(PAUSA_CS)
            if reg:
                variante_boa = v
                notas.append(f"Comex Stat: variante que funcionou = {v['nome']}")
                break
            fila.pop(0)
        if not reg:
            continue
        idade = (hoje - datetime.fromisoformat(reg["data_referencia"]).date()).days
        reg["atraso_dias"] = idade
        if idade > limite:
            notas.append(f"{a['label']}: descartado, referencia {reg['referencia_mensal']} "
                         f"com {idade} dias (limite {limite})")
            continue
        saida.append(reg)
    return saida


def fetch_fertilizantes(cfg, now):
    """Ponto unico de entrada. Chamar de fetch_cotacoes() no build.py."""
    notas = []
    painel = fetch_indexmundi(cfg, now.date(), notas)
    painel += fetch_comexstat(cfg, now.date(), notas)
    if not painel:
        notas.append("Painel de fertilizantes vazio nesta execucao: nenhuma fonte publicavel.")
    return painel, notas


# --------------------------------------------------------------- testes
AMOSTRA_IM = """
<html><body>
<b>Description:</b> Urea, (Black Sea), bulk, spot, f.o.b. Black Sea (primarily Yuzhnyy)
beginning July 1991; for 1985-91 (June) f.o.b. Eastern Europe
<br><b>Unit:</b> US Dollars per Metric Ton  Currency: US Dollar
<br><b>Source:</b> Fertilizer Week; Fertilizer International; World Bank.<br>See also: x
<table><tr><td>Urea Monthly Price - US Dollars per Metric Ton Data as of <b>March 2026</b></td></tr>
<tr><td>Month</td><td>Price</td><td>Change</td></tr>
<tr><td>Nov 2025</td><td>409.25</td><td>3.77%</td></tr>
<tr><td>Dec 2025</td><td>392.50</td><td>-4.09%</td></tr>
<tr><td>Jan 2026</td><td>415.40</td><td>5.83%</td></tr>
<tr><td>Feb 2026</td><td>472.00</td><td>13.63%</td></tr>
<tr><td>Mar 2026</td><td>725.63</td><td>53.74%</td></tr>
</table></body></html>
"""

RESP_CS = {"data": {"list": [
    {"year": "2026", "monthNumber": "04", "sh6": "310540", "metricFOB": "10000000", "metricKG": "20000000"},
    {"year": "2026", "monthNumber": "05", "sh6": "310540", "metricFOB": "12000000", "metricKG": "20000000"},
]}}


def _testes():
    reg, motivo = parse_indexmundi(AMOSTRA_IM, "Ureia", "ureia_wb")
    assert reg is not None, motivo
    assert reg["preco"] == 725.63, reg["preco"]
    assert reg["referencia_mensal"] == "2026-03", reg["referencia_mensal"]
    assert reg["variacao_mes_pct"] == 53.74, reg["variacao_mes_pct"]
    assert "f.o.b. Black Sea" in reg["metodologia"], reg["metodologia"]
    assert reg["e_proxy"] is False
    assert len(reg["serie"]) == 5
    print("ok  indexmundi:", reg["preco_texto"], reg["unidade"], reg["referencia_mensal"])

    # cabecalho divergente da tabela nao publica
    ruim = AMOSTRA_IM.replace("<b>March 2026</b>", "<b>April 2026</b>")
    reg2, motivo2 = parse_indexmundi(ruim, "Ureia", "ureia_wb")
    assert reg2 is None and "divergencia" in motivo2, motivo2
    print("ok  guarda de divergencia:", motivo2[:70])

    # unidade diferente nao publica
    ruim2 = AMOSTRA_IM.replace("US Dollars per Metric Ton  Currency", "Euro per Metric Ton  Currency")
    reg3, motivo3 = parse_indexmundi(ruim2, "Ureia", "ureia_wb")
    assert reg3 is None and "unidade" in motivo3, motivo3
    print("ok  guarda de unidade:", motivo3[:70])

    p = montar_payload_comexstat("310540", "2024-06", "2026-05")
    assert p["filters"][0]["values"] == [310540] and p["flow"] == "import"
    assert p["metrics"] == ["metricFOB", "metricKG"]
    print("ok  payload comexstat:", json.dumps(p, ensure_ascii=False))

    reg4, motivo4 = parse_comexstat(RESP_CS, "310540", "map_import_br", "MAP importacao")
    assert reg4 is not None, motivo4
    assert reg4["preco"] == 600.00, reg4["preco"]   # 12.000.000 / 20.000 t
    assert reg4["referencia_mensal"] == "2026-05"
    assert reg4["variacao_mes_pct"] == 20.0, reg4["variacao_mes_pct"]
    assert reg4["e_proxy"] is True
    print("ok  comexstat:", reg4["preco_texto"], reg4["unidade"], reg4["referencia_mensal"])

    fora = {"data": {"list": [{"year": "2026", "monthNumber": "05",
                              "metricFOB": "1", "metricKG": "20000000"}]}}
    reg5, motivo5 = parse_comexstat(fora, "310540", "x", "MAP importacao")
    assert reg5 is None and "faixa plausivel" in motivo5, motivo5
    print("ok  guarda de faixa:", motivo5[:70])
    print("\ntodos os testes passaram")


if __name__ == "__main__":
    _testes()
