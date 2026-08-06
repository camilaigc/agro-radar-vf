# -*- coding: utf-8 -*-
"""
arquivo.py — memoria permanente e pesquisavel do Radar.

O que existia antes deste modulo:
  data/seen.json          -> so hash + data. Impede repetir, nao permite buscar.
  docs/coletas-DIA.json   -> snapshot do dia, completo, mas ninguem consolida
                             e o front nunca abre.

O que este modulo cria:
  docs/arquivo/AAAA-MM-DD.json -> registro compacto de todo item coletado no dia.
  docs/arquivo/dias.json       -> indice dos dias existentes, com contagem.

Por que um arquivo por DIA, e nao um unico nem um por mes: sao ~440 itens por
dia, ~200 KB. Num arquivo unico daria mais de 100 mil itens no ano, que nenhum
navegador de celular abre. Por mes seria pior para o git: o commit diario
reescreveria um arquivo que chega a 5 MB no fim do mes, ou seja ~100 MB de
objetos novos por mes no historico. Por dia, cada commit APENAS ACRESCENTA um
arquivo pequeno e nunca reescreve os antigos, e o front baixa so o periodo pedido.

Regra de ouro do arquivo: NADA e sobrescrito e NADA e apagado. Item que reaparece
mantem a data em que foi visto pela PRIMEIRA vez e ganha +1 em "vezes". Assim
"desde quando esse nome aparece" continua respondivel meses depois.
"""
import hashlib
import json
import unicodedata
import re
from datetime import datetime
from pathlib import Path

# Campos guardados. Deliberadamente enxuto: sem isso o arquivo do mes fica grande
# demais para o navegador. O texto completo continua no snapshot do dia.
LIMITE_TRECHO = 300


def _mes(iso):
    return (iso or "")[:7]


def _norm(t):
    """Mesma normalizacao do build.py. Copiada de proposito para o modulo nao
    depender de importar build (que faz coleta ao ser importado em alguns fluxos)."""
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower()).strip()


def _chave(item):
    """Identidade do item: sha1 do titulo normalizado, 16 hex.

    CORRECAO de 05/08/2026. Antes era blake2s do LINK, e o build.py publica
    id = sha1(titulo normalizado). Duas identidades para a mesma coisa, ambas com
    16 caracteres, o que escondia o problema: medido no repositorio, a intersecao
    entre as chaves do arquivo e os ids do painel era ZERO em 1.193 itens.
    Nada quebrava na tela; o erro so aparecia ao tentar cruzar arquivo com painel.
    Agora a identidade e uma so, e o campo id e gravado no registro.
    """
    base = item.get("id")
    if base:
        return str(base)
    titulo = item.get("titulo") or item.get("title") or ""
    return hashlib.sha1(_norm(titulo).encode()).hexdigest()[:16] if titulo.strip() else ""


# Campos que MUDAM quando o item reaparece: a classificacao pode ter melhorado
# desde a primeira vez (foi assim que 747 de 1.193 itens ficaram sem classe: eram
# anteriores a existencia do campo e o reaparecimento so somava "vezes").
MUTAVEIS = ("classe", "tipo_evento", "micro", "termo", "escopo", "motivo",
            "prioridade_revisao", "evidencia_setor", "evidencia_transacao",
            "pontos_setor", "vigiados")


def _compacto(item, visto_em, escopo):
    return {
        "id": _chave(item),
        "titulo": (item.get("titulo") or "")[:300],
        "trecho": (item.get("trecho") or "")[:LIMITE_TRECHO],
        "link": item.get("link") or "",
        "fonte": item.get("fonte") or "",
        "confiavel": bool(item.get("fonte_confiavel")),
        "camada": item.get("camada") or "",
        "micro": item.get("micro_sugerida") or "",
        "termo": item.get("termo_setorial") or "",
        "classe": item.get("classe") or "",
        "tipo_evento": item.get("tipo_evento") or "",
        "motivo": (item.get("motivo") or "")[:180],
        "prioridade_revisao": bool(item.get("prioridade_revisao")),
        "evidencia_setor": (item.get("evidencia_setor") or "")[:120],
        "evidencia_transacao": (item.get("evidencia_transacao") or "")[:120],
        "pontos_setor": item.get("pontos_setor"),
        "vigiados": item.get("vigiados") or None,
        "classe": item.get("classe") or "noticia",
        "tipo_evento": item.get("tipo_evento") or "noticia",
        "motivo": item.get("motivo") or "",
        "prioridade_revisao": bool(item.get("prioridade_revisao")),
        "quando": (item.get("quando") or "")[:19],
        "visto_em": visto_em,
        "escopo": escopo,          # "dentro" ou "fora"
        "vezes": 1,
    }


def atualizar_arquivo(docs_dir, itens, itens_fora, hoje_iso, notas):
    """Acrescenta a coleta do dia ao arquivo do mes. Idempotente.

    Rodar duas vezes no mesmo dia nao duplica: item ja arquivado so incrementa
    "vezes" e preserva "visto_em" original.
    """
    pasta = Path(docs_dir) / "arquivo"
    pasta.mkdir(parents=True, exist_ok=True)
    dia = (hoje_iso or "")[:10]
    alvo = pasta / f"{dia}.json"

    doc = {"schema": "radar-agro-arquivo/2", "dia": dia, "itens": {}}
    if alvo.exists():
        try:
            lido = json.loads(alvo.read_text(encoding="utf-8"))
            if isinstance(lido.get("itens"), dict):
                doc = lido
        except Exception as ex:
            notas.append(f"arquivo {dia}: ilegivel ({type(ex).__name__}), NAO sobrescrito. "
                         f"Corrija o arquivo antes de confiar na busca desse dia.")
            return None

    registro = doc["itens"]
    novos = revistos = 0
    for lista, escopo in ((itens or [], "dentro"), (itens_fora or [], "fora")):
        for it in lista:
            k = _chave(it)
            if not k:
                continue
            if k in registro:
                # NAO usar o nome "alvo" aqui: ele ja e o Path do arquivo do dia,
                # logo acima. A primeira versao deste patch sombreou a variavel e
                # quebrou a gravacao com AttributeError.
                reg_existente = registro[k]
                reg_existente["vezes"] = int(reg_existente.get("vezes", 1)) + 1
                # reescreve o que pode ter mudado desde a primeira vez; visto_em e
                # vezes ficam intactos, porque "desde quando aparece" e o que da
                # valor ao arquivo
                novo = _compacto(it, reg_existente.get("visto_em") or dia, escopo)
                for campo in MUTAVEIS:
                    if novo.get(campo) not in (None, "", False) or not reg_existente.get(campo):
                        reg_existente[campo] = novo.get(campo)
                reg_existente.setdefault("id", k)
                revistos += 1
            else:
                registro[k] = _compacto(it, dia, escopo)
                novos += 1

    doc["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    doc["total"] = len(registro)
    alvo.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")

    # indice de dias, para o front saber o que existe sem adivinhar caminho
    dias = []
    for f in sorted(pasta.glob("20*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            dias.append({"dia": d.get("dia") or f.stem, "total": d.get("total", 0)})
        except Exception:
            dias.append({"dia": f.stem, "total": None, "erro": "ilegivel"})
    (pasta / "dias.json").write_text(
        json.dumps({"schema": "radar-agro-arquivo-indice/2",
                    "atualizado_em": doc["atualizado_em"],
                    "dias": dias,
                    "total_geral": sum(d["total"] or 0 for d in dias)},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    notas.append(f"arquivo {dia}: +{novos} novos, {revistos} reaparecimentos, "
                 f"{doc['total']} no dia, {sum(d['total'] or 0 for d in dias)} no total")
    return doc


def backfill(docs_dir, notas):
    """Reconstroi o arquivo a partir de todos os docs/coletas-AAAA-MM-DD.json.

    Serve para nao perder o que ja foi coletado antes do arquivo existir. Roda
    quantas vezes quiser: e idempotente pela mesma regra de chave.
    """
    pasta = Path(docs_dir)
    achados = sorted(pasta.glob("coletas-*.json"))
    if not achados:
        notas.append("backfill: nenhum coletas-*.json encontrado")
        return 0
    lidos = 0
    for f in achados:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as ex:
            notas.append(f"backfill: {f.name} ilegivel ({type(ex).__name__}), pulado")
            continue
        quando = (d.get("data") or f.stem.replace("coletas-", ""))[:10]
        atualizar_arquivo(docs_dir, d.get("itens") or [],
                          d.get("itens_fora_escopo") or [], quando, notas)
        lidos += 1
    notas.append(f"backfill: {lidos} snapshots processados")
    return lidos


# ------------------------------------------------------------------ testes
def _testes():
    import tempfile
    notas = []
    with tempfile.TemporaryDirectory() as tmp:
        it1 = [{"titulo": "Boa Safra compra fabrica", "link": "https://x.com/a",
                "fonte": "AgFeed", "fonte_confiavel": True, "camada": "RSS",
                "micro_sugerida": "Sementes Graos", "termo_setorial": "semente",
                "trecho": "A Boa Safra anunciou...", "quando": "2026-08-05T09:00:00"}]
        fora = [{"titulo": "Copom mantem Selic", "link": "https://x.com/b",
                 "fonte": "InfoMoney", "camada": "RSS", "trecho": "..."}]
        atualizar_arquivo(tmp, it1, fora, "2026-08-05", notas)
        d = json.loads((Path(tmp) / "arquivo" / "2026-08-05.json").read_text())
        ka, kb = _chave(it1[0]), _chave(fora[0])
        assert d["total"] == 2, d["total"]
        assert d["itens"][ka]["escopo"] == "dentro"
        assert d["itens"][kb]["escopo"] == "fora"
        assert len(ka) == 16, ka
        print("ok  gravou 2 itens, escopo separado, chave de 16 hex")

        # mesmo item de novo: nao duplica, incrementa vezes, preserva visto_em
        atualizar_arquivo(tmp, it1, [], "2026-08-05", notas)
        d = json.loads((Path(tmp) / "arquivo" / "2026-08-05.json").read_text())
        assert d["total"] == 2, d["total"]
        assert d["itens"][ka]["vezes"] == 2
        assert d["itens"][ka]["visto_em"] == "2026-08-05"
        print("ok  reaparecimento no mesmo dia: vezes=2, visto_em preservado")

        # mes novo vira arquivo novo
        atualizar_arquivo(tmp, [{"titulo": "Outro dia", "link": "https://x.com/c",
                                 "fonte": "AgFeed"}], [], "2026-09-01", notas)
        idx = json.loads((Path(tmp) / "arquivo" / "dias.json").read_text())
        assert [d["dia"] for d in idx["dias"]] == ["2026-08-05", "2026-09-01"], idx["dias"]
        assert idx["total_geral"] == 3, idx["total_geral"]
        print("ok  indice de dias:", [d["dia"] for d in idx["dias"]], "total", idx["total_geral"])

        # item sem link cai no titulo normalizado
        atualizar_arquivo(tmp, [{"titulo": "Sem Link  Aqui!", "fonte": "X"}],
                          [], "2026-09-02", notas)
        d = json.loads((Path(tmp) / "arquivo" / "2026-09-02.json").read_text())
        esperada = _chave({"titulo": "Sem Link  Aqui!"})
        assert esperada in d["itens"], list(d["itens"])
        print("ok  item sem link identificado pelo titulo normalizado")

        # backfill a partir de snapshot diario
        snap = {"data": "2026-07-20", "itens": it1, "itens_fora_escopo": []}
        (Path(tmp) / "coletas-2026-07-20.json").write_text(json.dumps(snap))
        backfill(tmp, notas)
        idx = json.loads((Path(tmp) / "arquivo" / "dias.json").read_text())
        assert "2026-07-20" in [d["dia"] for d in idx["dias"]]
        print("ok  backfill criou 2026-07-20 a partir do snapshot")
    print("\ntodos os testes passaram")


if __name__ == "__main__":
    _testes()
