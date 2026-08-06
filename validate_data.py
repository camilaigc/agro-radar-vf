# -*- coding: utf-8 -*-
"""Validação local dos dados publicados pelo Radar Agro.

Erros de integridade encerram a execução com código 1. Diferenças legítimas de
benchmark, frequência ou defasagem viram avisos: o site continua publicando a
cotação, mas não deve desenhá-la como se fosse diretamente comparável ao
histórico.
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"

LIGACOES = {
    "te_soja_cbot": ("Soja", "indicativa"),
    "te_milho_cbot": ("Milho", "indicativa"),
    "te_trigo_cbot__srw": ("Trigo", "indicativa"),
    "ouro_pm": ("Ouro", "direta"),
    "prata": ("Prata", "direta"),
    "te_prata": ("Prata", "fallback"),
    "te_platina": ("Platina", "indicativa"),
    "te_aluminio": ("Alumínio", "indicativa"),
    "te_chumbo": ("Chumbo", "indicativa"),
    "te_estanho": ("Estanho", "indicativa"),
    "te_niquel": ("Níquel", "indicativa"),
    "te_zinco": ("Zinco", "indicativa"),
    "te_cobre": ("Cobre", "indicativa"),
    "usdbrl_ptax": ("USD/BRL", "direta"),
    "kcl_wb": ("KCl", "direta"),
}


def unidade_normalizada(valor):
    return (
        str(valor or "")
        .lower()
        .replace(" ", "")
        .replace("us$", "usd")
        .replace("r$", "brl")
        .replace("oztroy", "oz")
        .replace("ozt", "oz")
        .replace("/tonelada", "/t")
        .replace("/ton", "/t")
        .replace("/mt", "/t")
    )


def numero_br(texto, esperado=None):
    s = str(texto or "").strip().replace(" ", "")
    if not s:
        raise ValueError("texto vazio")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
        return float(s)
    candidatos = [float(s)]
    if "." in s:
        candidatos.append(float(s.replace(".", "")))
    if esperado is None:
        return candidatos[0]
    return min(candidatos, key=lambda x: abs(x - float(esperado)))


def mes_referencia(cotacao):
    return (cotacao.get("referencia_mensal") or cotacao.get("data_referencia") or "")[:7]


def main():
    painel_path = DOCS / "painel.json"
    if not painel_path.exists():
        painel_path = DOCS / "ultima.json"
    painel = json.loads(painel_path.read_text(encoding="utf-8"))
    historico = json.loads((DOCS / "historico.json").read_text(encoding="utf-8"))

    erros, avisos = [], []
    cotacoes = (painel.get("cotacoes") or []) + (painel.get("cotacoes_mensais") or [])
    por_chave = {}
    for c in cotacoes:
        chave = c.get("key")
        if not chave:
            erros.append("cotação sem key")
            continue
        if chave in por_chave:
            erros.append(f"chave de cotação duplicada: {chave}")
        por_chave[chave] = c

        preco = c.get("preco")
        if not isinstance(preco, (int, float)) or not math.isfinite(preco) or preco <= 0:
            erros.append(f"{chave}: preço inválido {preco!r}")
            continue
        try:
            exibido = numero_br(c.get("preco_texto"), preco)
            tolerancia = max(0.005, abs(preco) * 0.0001)
            if abs(exibido - preco) > tolerancia:
                erros.append(f"{chave}: preço numérico {preco} diverge do texto {c.get('preco_texto')}")
        except Exception as ex:
            erros.append(f"{chave}: preço_texto ilegível ({ex})")

        ref = c.get("data_referencia")
        if ref:
            try:
                d = date.fromisoformat(ref[:10])
                if d > date.today():
                    erros.append(f"{chave}: data de referência no futuro ({ref})")
            except ValueError:
                erros.append(f"{chave}: data de referência inválida ({ref})")

        if chave in {"te_prata", "te_platina"} and unidade_normalizada(c.get("unidade")) != "usd/oz":
            erros.append(f"{chave}: metal precioso deve estar em US$/oz t, veio {c.get('unidade')}")

    series = historico.get("series") or {}
    for nome, serie in series.items():
        pontos = serie.get("pontos") or []
        meses = [p[0] for p in pontos]
        if meses != sorted(meses):
            erros.append(f"{nome}: pontos fora de ordem cronológica")
        if len(meses) != len(set(meses)):
            erros.append(f"{nome}: meses duplicados")
        if any(not isinstance(p[1], (int, float)) or p[1] <= 0 for p in pontos):
            erros.append(f"{nome}: série contém valor não positivo ou não numérico")

    for chave, (nome, nivel) in LIGACOES.items():
        c, s = por_chave.get(chave), series.get(nome)
        if not (c and s and s.get("pontos")):
            continue
        if unidade_normalizada(c.get("unidade")) != unidade_normalizada(s.get("unidade")):
            avisos.append(
                f"{chave} → {nome}: unidades diferentes ({c.get('unidade')} x {s.get('unidade')}); sem linha atual"
            )
            continue
        ref_atual = mes_referencia(c)
        ref_serie = s["pontos"][-1][0]
        if ref_atual and ref_atual < ref_serie:
            avisos.append(
                f"{chave} → {nome}: cotação de {ref_atual} é anterior ao último histórico {ref_serie}; sem linha atual"
            )
            continue
        ultimo = float(s["pontos"][-1][1])
        razao = float(c["preco"]) / ultimo if ultimo else math.inf
        if not 0.25 <= razao <= 4:
            avisos.append(f"{chave} → {nome}: razão atual/último histórico suspeita ({razao:.2f}x)")
        if nivel == "indicativa":
            avisos.append(f"{chave} → {nome}: comparação apenas indicativa; fonte/frequência diferem")

    for aviso in avisos:
        print("AVISO:", aviso)
    if erros:
        for erro in erros:
            print("ERRO:", erro)
        print(f"\nValidação reprovada: {len(erros)} erro(s), {len(avisos)} aviso(s).")
        return 1
    print(f"Validação aprovada: {len(cotacoes)} cotações, {len(series)} séries, {len(avisos)} aviso(s) documentado(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
