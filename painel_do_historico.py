# -*- coding: utf-8 -*-
"""
painel_do_historico.py — o painel passa a citar a fonte mais recente.

PROBLEMA QUE RESOLVE: o site mostrava DOIS valores para o mesmo indicador. A serie
historica do KCl ia ate 07/2026 pelo Pink Sheet, e o painel de cotacoes mostrava
o KCl do IndexMundi com 157 dias de atraso, porque o IndexMundi espelha a mesma
serie do World Bank com meses de defasagem. Mesmo numero, duas idades, na mesma
pagina. Era a unica contradicao aberta do site.

O QUE FAZ: le o ultimo ponto das series do Pink Sheet ja gravadas em
historico.json e emite as linhas mensais do painel a partir dali. Nao ha coleta
nova: o dado ja foi baixado, validado por unidade e gravado pelo coletor mensal.

USD/BRL: media mensal calculada a partir do PTAX diario que o radar ja acumula em
serie-cotacoes.json. Mes com poucas observacoes NAO entra, e o que entra guarda
quantos dias foram usados. Media de 6 dias nao e media mensal.
"""
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# serie do historico -> como aparece no painel
DO_PINK_SHEET = [
    ("KCl", "kcl_ps", "Cloreto de potassio (KCl), CFR Brasil"),
    ("Ureia (World Bank)", "ureia_ps", "Ureia"),
    ("DAP", "dap_ps", "DAP"),
    ("TSP", "tsp_ps", "TSP"),
    ("Rocha fosfatica", "rocha_ps", "Rocha fosfatica"),
]
MIN_DIAS_MES = 15          # abaixo disso a media do mes nao e publicada


def _fmt(v, dec=2):
    return f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _idade(mes, hoje):
    a, m = int(mes[:4]), int(mes[5:7])
    fim = date(a + (m == 12), 1 if m == 12 else m + 1, 1)
    return (hoje - fim).days + 1


def do_pink_sheet(caminho_hist, hoje, notas, limite_dias=120):
    p = Path(caminho_hist)
    if not p.exists():
        notas.append(f"painel: {caminho_hist} nao existe, nada do Pink Sheet")
        return []
    hist = json.loads(p.read_text(encoding="utf-8")).get("series") or {}
    saida = []
    for nome, key, rotulo in DO_PINK_SHEET:
        s = hist.get(nome)
        if not s or not s.get("pontos"):
            notas.append(f"painel: serie {nome!r} ausente ou vazia no historico")
            continue
        mes, valor = s["pontos"][-1]
        idade = _idade(mes, hoje)
        if idade > limite_dias:
            notas.append(f"painel: {rotulo} descartado, referencia {mes} com {idade} dias")
            continue
        anterior = s["pontos"][-2][1] if len(s["pontos"]) > 1 else None
        saida.append({
            "key": key, "label": rotulo,
            "preco": valor, "preco_texto": _fmt(valor),
            "unidade": s.get("unidade", "US$/t"),
            "data_referencia": f"{mes}-01",
            "referencia_mensal": mes,
            "variacao_mes_pct": round((valor / anterior - 1) * 100, 2) if anterior else None,
            "atraso_dias": idade,
            "e_proxy": False,
            "fonte": "World Bank Pink Sheet",
            "metodologia": (f"{s.get('benchmark', '')}. Media mensal publicada pelo World "
                            f"Bank, lida do arquivo CMO-Historical-Data-Monthly."),
        })
        notas.append(f"painel: {rotulo} = {valor} {s.get('unidade')} ref {mes} ({idade}d)")
    return saida


def usdbrl_mensal(caminho_serie, caminho_hist, hoje, notas):
    """Media mensal do PTAX, a partir do diario que o radar ja acumula."""
    ps, ph = Path(caminho_serie), Path(caminho_hist)
    if not (ps.exists() and ph.exists()):
        notas.append("USD/BRL mensal: serie-cotacoes.json ou historico.json ausente")
        return
    pontos = (json.loads(ps.read_text(encoding="utf-8")).get("pontos") or {}).get("usdbrl_ptax") or {}
    if not pontos:
        notas.append("USD/BRL mensal: nenhum PTAX diario acumulado ainda")
        return
    por_mes = defaultdict(list)
    for dia, reg in pontos.items():
        v = reg.get("preco") if isinstance(reg, dict) else reg
        if isinstance(v, (int, float)) and v > 0:
            por_mes[dia[:7]].append(float(v))

    hist = json.loads(ph.read_text(encoding="utf-8"))
    serie = hist["series"].get("USD/BRL")
    if not serie:
        notas.append("USD/BRL mensal: serie nao existe no historico")
        return
    existentes = {m: v for m, v in (serie.get("pontos") or [])}
    dias_por_mes = dict(serie.get("dias_por_mes") or {})
    novos = 0
    for mes, vals in sorted(por_mes.items()):
        if len(vals) < MIN_DIAS_MES:
            notas.append(f"USD/BRL {mes}: so {len(vals)} dia(s) coletado(s), minimo "
                         f"{MIN_DIAS_MES}. Nao publicado: media de poucos dias nao e "
                         f"media mensal.")
            continue
        media = round(sum(vals) / len(vals), 4)
        if existentes.get(mes) == media:
            continue
        existentes[mes] = media
        dias_por_mes[mes] = len(vals)
        novos += 1
    if not novos:
        notas.append("USD/BRL mensal: nenhum mes novo com dias suficientes")
        return
    serie["pontos"] = sorted([[m, v] for m, v in existentes.items()])
    serie["de"], serie["ate"], serie["n"] = serie["pontos"][0][0], serie["pontos"][-1][0], len(serie["pontos"])
    serie["dias_por_mes"] = dias_por_mes
    serie["nota"] = ("Meses recentes calculados pelo radar como media dos PTAX diarios "
                     "coletados; dias_por_mes registra quantos entraram em cada um. "
                     "Meses antigos vem da base original.")
    ph.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    notas.append(f"USD/BRL mensal: +{novos} mes(es), serie vai ate {serie['ate']}")


def rodar(docs, hoje, notas):
    usdbrl_mensal(Path(docs) / "serie-cotacoes.json", Path(docs) / "historico.json", hoje, notas)
    return do_pink_sheet(Path(docs) / "historico.json", hoje, notas)


if __name__ == "__main__":
    import tempfile, os
    n = []
    d = tempfile.mkdtemp()
    hist = {"series": {
        "KCl": {"benchmark": "World Bank Potassium chloride", "unidade": "US$/t",
                "pontos": [["2026-06", 350.0], ["2026-07", 352.5]]},
        "Ureia (World Bank)": {"benchmark": "World Bank Urea", "unidade": "US$/t",
                "pontos": [["2026-07", 441.0]]},
        "USD/BRL": {"benchmark": "BCB PTAX", "unidade": "R$/US$",
                "pontos": [["2026-06", 5.40]], "de": "2026-06", "ate": "2026-06", "n": 1},
    }}
    json.dump(hist, open(os.path.join(d, "historico.json"), "w"))
    dias = {f"2026-07-{i:02d}": {"preco": 5.4 + i / 1000} for i in range(1, 21)}
    dias.update({f"2026-08-{i:02d}": {"preco": 5.5} for i in range(1, 4)})
    json.dump({"pontos": {"usdbrl_ptax": dias}}, open(os.path.join(d, "serie-cotacoes.json"), "w"))
    painel = rodar(d, date(2026, 8, 6), n)
    for x in painel:
        print(f"  {x['label'][:38]:40} {x['preco_texto']:>9} {x['unidade']} ref {x['referencia_mensal']} ({x['atraso_dias']}d)")
    print()
    for x in n:
        print("  nota:", x)
    u = json.load(open(os.path.join(d, "historico.json")))["series"]["USD/BRL"]
    print("\n  USD/BRL:", u["pontos"], "| dias:", u.get("dias_por_mes"))
