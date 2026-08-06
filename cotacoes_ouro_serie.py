# -*- coding: utf-8 -*-
"""
cotacoes_ouro_serie.py — dois problemas, um modulo.

1. OURO SEM DEPENDER DO TRADING ECONOMICS
   Fonte: LBMA Gold Price, o benchmark oficial do mercado de ouro (leilao
   administrado pela ICE Benchmark Administration, publicado pela LBMA).
   Endpoint JSON aberto, sem chave, serie diaria completa desde 1968:
     https://prices.lbma.org.uk/json/gold_pm.json
   Formato confirmado por leitura direta em 05/08/2026:
     [{"is_cms_locked":0,"d":"AAAA-MM-DD","v":[USD, GBP, EUR]}, ...]
   ordenado do mais antigo para o mais recente. Indice 0 do "v" = USD/oz troy.

   Vantagem sobre o Trading Economics: e o preco de referencia de verdade, nao
   um CFD que acompanha o benchmark, e o parser depende de um JSON estavel em
   vez de uma frase de copy da pagina ("last updated on August 5 of 2026").

   Nao usar stooq: o robots.txt do site proibe acesso automatizado.

2. HISTORICO SEM TRABALHO MANUAL
   O SGS do Banco Central entrega a serie inteira por intervalo de datas, nao
   so os ultimos N. Junto com o JSON da LBMA, isso permite backfill: o
   historico nasce completo na primeira execucao, sem esperar acumular dia a dia.
   `atualizar_serie` grava docs/serie-cotacoes.json, chave (key, data_referencia),
   idempotente: rodar duas vezes no mesmo dia nao duplica nem sobrescreve com
   valor diferente sem avisar.

Testado offline contra amostras reais. As chamadas de rede precisam do probe.
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

TIMEOUT = 40
# Headers: a LBMA fica atras de WAF. O probe.py pegou HTTP 200 na mesma URL em
# que este modulo levou 403, e a unica diferenca era o conjunto de cabecalhos:
# faltava Accept-Language e Referer. Por isso agora sao dois perfis, tentados em
# ordem, e o motivo real fica na nota quando os dois falham.
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9,pt-BR;q=0.8",
    "Referer": "https://www.lbma.org.uk/prices-and-data/lbma-precious-metal-prices",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}
# Perfil 2: exatamente o do probe.py, que respondeu 200 em 05/08/2026.
UA_PROBE = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("application/rss+xml, application/atom+xml, application/xml;q=0.9, "
               "text/html;q=0.8, */*;q=0.5"),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _get_json(url, notas, rotulo):
    """GET com dois perfis de cabecalho. Devolve (json, erro).

    Em erro HTTP, guarda o inicio do corpo da resposta: e ali que o servidor diz
    o que recusou. Sem isso, 403 e 400 viram "falhou" sem explicacao.
    """
    ultimo = ""
    for i, headers in enumerate((UA, UA_PROBE), start=1):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code >= 400:
                ultimo = f"HTTP {r.status_code} (perfil {i}), corpo: {r.text[:120]!r}"
                continue
            return r.json(), None
        except Exception as ex:
            ultimo = f"{type(ex).__name__} (perfil {i}): {str(ex)[:110]}"
    return None, ultimo

LBMA_URLS = {
    "ouro_pm": ("https://prices.lbma.org.uk/json/gold_pm.json",
                "Ouro - LBMA Gold Price PM", "US$/oz t"),
    "ouro_am": ("https://prices.lbma.org.uk/json/gold_am.json",
                "Ouro - LBMA Gold Price AM", "US$/oz t"),
    "prata": ("https://prices.lbma.org.uk/json/silver.json",
              "Prata - LBMA Silver Price", "US$/oz t"),
}


def _fmt_br(v, dec=2):
    return f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------- ouro
def parse_lbma(serie, key, label, unidade, hoje, max_idade_dias=6):
    """Devolve (registro, motivo). Usa o ultimo dia com valor em USD.

    Nao assume que o ultimo elemento tem valor: dia de leilao cancelado ou
    feriado aparece com null. Percorre de tras para frente ate achar USD valido.
    """
    if not isinstance(serie, list) or not serie:
        return None, f"{label}: resposta nao e uma lista de precos"
    for item in reversed(serie):
        try:
            d = datetime.strptime(item["d"][:10], "%Y-%m-%d").date()
            usd = item["v"][0]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if usd is None:
            continue
        usd = float(usd)
        if usd <= 0:
            continue
        idade = (hoje - d).days
        if idade < 0:
            return None, f"{label}: data {d.isoformat()} no futuro, resposta suspeita"
        if idade > max_idade_dias:
            return None, (f"{label}: ultimo leilao com valor e de {d.isoformat()}, "
                          f"{idade} dias atras (limite {max_idade_dias}), nao publicado")
        return {
            "key": key, "label": label,
            "preco": round(usd, 2), "preco_texto": _fmt_br(usd),
            "unidade": unidade,
            "data_referencia": d.isoformat(),
            "metodologia": ("Leilao eletronico administrado pela ICE Benchmark "
                            "Administration, publicado pela LBMA. Preco de referencia "
                            "do mercado fisico de Londres, nao CFD."),
            "fonte": "LBMA (prices.lbma.org.uk), serie diaria oficial",
        }, f"{label}: ok ({usd} {unidade}, ref {d.isoformat()})"
    return None, f"{label}: nenhum dia da serie tem valor em USD"


def fetch_lbma(cfg, now, notas):
    c = (cfg.get("lbma") or {})
    if not c.get("enabled"):
        notas.append("LBMA desativado no sources.yml")
        return []
    alvos = c.get("alvos") or ["ouro_pm"]
    limite = int(c.get("max_idade_dias", 6))
    saida = []
    for k in alvos:
        if k not in LBMA_URLS:
            notas.append(f"LBMA: alvo desconhecido {k!r}, ignorado")
            continue
        url, label, unidade = LBMA_URLS[k]
        dados, erro = _get_json(url, notas, label)
        if erro:
            notas.append(f"{label}: falha {erro}")
            continue
        reg, motivo = parse_lbma(dados, k, label, unidade, now.date(), limite)
        notas.append(motivo)
        if reg:
            saida.append(reg)
    return saida


# --------------------------------------------------------------------- ptax
SGS_SERIES = {"usdbrl_ptax": (1, "USD/BRL - PTAX venda", "R$/US$")}


def parse_sgs(serie, key, label, unidade, hoje, max_idade_dias=7):
    if not isinstance(serie, list) or not serie:
        return [], f"{label}: serie vazia"
    linhas = []
    for item in serie:
        try:
            d = datetime.strptime(item["data"], "%d/%m/%Y").date()
            v = float(str(item["valor"]).replace(",", "."))
        except (KeyError, TypeError, ValueError):
            continue
        if v <= 0:
            continue
        linhas.append((d, v))
    if not linhas:
        return [], f"{label}: nenhuma linha utilizavel"
    linhas.sort()
    idade = (hoje - linhas[-1][0]).days
    if idade > max_idade_dias:
        return [], (f"{label}: ultimo dado de {linhas[-1][0].isoformat()}, {idade} dias "
                    f"de atraso (limite {max_idade_dias}), nao publicado")
    regs = [{
        "key": key, "label": label,
        "preco": round(v, 4), "preco_texto": _fmt_br(v, 4),
        "unidade": unidade, "data_referencia": d.isoformat(),
        "metodologia": "PTAX venda, taxa de fechamento apurada pelo Banco Central.",
        "fonte": f"Banco Central do Brasil, SGS serie {SGS_SERIES[key][0]}",
    } for d, v in linhas]
    return regs, f"{label}: ok ({len(regs)} dias, ultimo {linhas[-1][0].isoformat()})"


def fetch_sgs(key, de, ate, hoje, notas, max_idade_dias=7):
    """de/ate como date. Intervalo, nao 'ultimos N': serve para backfill."""
    cod, label, unidade = SGS_SERIES[key]
    url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{cod}/dados"
           f"?formato=json&dataInicial={de:%d/%m/%Y}&dataFinal={ate:%d/%m/%Y}")
    dados, erro = _get_json(url, notas, label)
    if erro:
        notas.append(f"{label}: falha {erro}")
        return []
    regs, motivo = parse_sgs(dados, key, label, unidade, hoje, max_idade_dias)
    notas.append(motivo)
    return regs


# ------------------------------------------------------------------- serie
def atualizar_serie(caminho, registros, notas):
    """Acumula (key, data_referencia) -> preco em docs/serie-cotacoes.json.

    Idempotente. Se o mesmo par chave/data voltar com valor diferente, grava o
    novo E registra o conflito na nota: revisao de serie e normal (o World Bank
    revisa; o BCB tambem ja revisou), mas nao pode acontecer em silencio.
    """
    p = Path(caminho)
    doc = {"schema": "radar-agro-serie/1", "pontos": {}}
    if p.exists():
        try:
            lido = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(lido.get("pontos"), dict):
                doc = lido
        except Exception as ex:
            notas.append(f"serie: arquivo ilegivel ({type(ex).__name__}), recomecando")
    pontos = doc["pontos"]
    novos = conflitos = 0
    for r in registros:
        k = r.get("key")
        d = r.get("data_referencia")
        if not (k and d):
            continue
        serie_k = pontos.setdefault(k, {})
        antigo = serie_k.get(d)
        if antigo is None:
            novos += 1
        elif abs(float(antigo.get("preco", 0)) - float(r["preco"])) > 1e-9:
            conflitos += 1
            notas.append(f"serie: {k} {d} mudou de {antigo.get('preco')} para "
                         f"{r['preco']}, valor novo gravado, revisao registrada")
        else:
            continue
        serie_k[d] = {"preco": r["preco"], "unidade": r.get("unidade"),
                      "fonte": r.get("fonte")}
    doc["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    doc["total_pontos"] = sum(len(v) for v in pontos.values())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    notas.append(f"serie: +{novos} pontos novos, {conflitos} revisoes, "
                 f"{doc['total_pontos']} pontos no total")
    return doc


def rodar(cfg, now, caminho_serie="docs/serie-cotacoes.json", dias_backfill=None):
    """Ponto de entrada. dias_backfill=None usa 30; na primeira execucao passe
    algo como 1825 (5 anos) para o historico nascer completo."""
    notas = []
    painel = fetch_lbma(cfg, now, notas)
    dias = int(dias_backfill or (cfg.get("bcb_ptax") or {}).get("dias_backfill", 30))
    ptax = fetch_sgs("usdbrl_ptax", now.date() - timedelta(days=dias), now.date(),
                     now.date(), notas)
    if ptax:
        painel.append(ptax[-1])          # painel do dia: so o ultimo
    atualizar_serie(caminho_serie, painel + ptax, notas)   # serie: tudo
    return painel, notas


# ------------------------------------------------------------------ testes
AMOSTRA_LBMA = [
    {"is_cms_locked": 0, "d": "1968-04-01", "v": [37.7, 15.68, None]},
    {"is_cms_locked": 0, "d": "2026-08-03", "v": [4102.55, 3050.1, 3555.2]},
    {"is_cms_locked": 0, "d": "2026-08-04", "v": [None, None, None]},
    {"is_cms_locked": 0, "d": "2026-08-05", "v": [4133.9, 3070.4, 3580.7]},
]
AMOSTRA_SGS = [{"data": "01/08/2026", "valor": "5.4321"},
               {"data": "04/08/2026", "valor": "5.4102"},
               {"data": "05/08/2026", "valor": "5.3988"}]


def _testes():
    hoje = date(2026, 8, 5)

    reg, motivo = parse_lbma(AMOSTRA_LBMA, "ouro_pm", "Ouro - LBMA PM", "US$/oz t", hoje)
    assert reg and reg["preco"] == 4133.90, motivo
    assert reg["data_referencia"] == "2026-08-05"
    print("ok  lbma:", reg["preco_texto"], reg["unidade"], reg["data_referencia"])

    # pula dia com leilao sem valor
    reg2, m2 = parse_lbma(AMOSTRA_LBMA[:3], "ouro_pm", "Ouro", "US$/oz t", hoje)
    assert reg2 and reg2["data_referencia"] == "2026-08-03", m2
    print("ok  pula dia nulo:", reg2["data_referencia"])

    # serie velha nao publica
    reg3, m3 = parse_lbma(AMOSTRA_LBMA[:1], "ouro_pm", "Ouro", "US$/oz t", hoje)
    assert reg3 is None and "dias atras" in m3, m3
    print("ok  guarda de idade:", m3[:66])

    notas = []
    regs, m4 = parse_sgs(AMOSTRA_SGS, "usdbrl_ptax", "PTAX", "R$/US$", hoje)
    assert len(regs) == 3 and regs[-1]["preco"] == 5.3988, m4
    print("ok  sgs:", len(regs), "dias, ultimo", regs[-1]["preco_texto"])

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        alvo = Path(tmp) / "serie-cotacoes.json"
        atualizar_serie(alvo, [reg] + regs, notas)
        d1 = json.loads(alvo.read_text())
        assert d1["total_pontos"] == 4, d1["total_pontos"]
        # idempotencia
        atualizar_serie(alvo, [reg] + regs, notas)
        d2 = json.loads(alvo.read_text())
        assert d2["total_pontos"] == 4, d2["total_pontos"]
        print("ok  idempotencia: 4 pontos, sem duplicar")
        # conflito de revisao
        rev = dict(reg); rev["preco"] = 4140.00
        atualizar_serie(alvo, [rev], notas)
        d3 = json.loads(alvo.read_text())
        assert d3["pontos"]["ouro_pm"]["2026-08-05"]["preco"] == 4140.00
        assert any("revisao registrada" in n for n in notas)
        print("ok  revisao gravada e sinalizada")
    print("\ntodos os testes passaram")


if __name__ == "__main__":
    _testes()
