# -*- coding: utf-8 -*-
"""
alerta_frescor.py — avisa quando o radar para de rodar.

PROBLEMA: se o cron quebrar, ou o GitHub desabilitar o workflow por inatividade
do repositorio (o que ele faz depois de 60 dias sem commit humano), o site continua
mostrando o ultimo dado e ninguem fica sabendo. O status.json foi criado para
permitir esta checagem e nada o consumia.

COMO AVISA: saindo com codigo 1. O GitHub Actions manda e-mail de workflow com
falha para o dono do repositorio por padrao, sem configurar nada.

Nao avisa por avisar: dia sem execucao no fim de semana e esperado, entao o limite
padrao e de 4 dias corridos, que cobre feriado emendado.
"""
import json
import os
import sys
from datetime import date, datetime

LIMITE = int(os.environ.get("LIMITE_DIAS", 4))
CAMINHO = os.environ.get("STATUS", "docs/status.json")


def verificar(caminho=CAMINHO, limite=LIMITE, hoje=None):
    """Devolve (ok, mensagem)."""
    hoje = hoje or date.today()
    try:
        with open(caminho, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        return False, f"{caminho} nao existe. O radar nunca publicou ou o arquivo sumiu."
    except Exception as ex:
        return False, f"{caminho} ilegivel: {type(ex).__name__}: {str(ex)[:90]}"

    bruto = str(s.get("data") or "")[:10]
    try:
        d = datetime.strptime(bruto, "%Y-%m-%d").date()
    except ValueError:
        return False, f"campo 'data' invalido no status.json: {bruto!r}"

    idade = (hoje - d).days
    resumo = (f"ultima coleta {d.isoformat()} ({idade} dia(s)), "
              f"{s.get('itens', '?')} itens, {s.get('cotacoes', '?')} cotacoes, "
              f"{s.get('n_falhas', '?')} de {s.get('n_fontes', '?')} fontes com falha")
    if idade > limite:
        return False, f"RADAR PARADO: {resumo}. Limite: {limite} dias."
    if not s.get("itens") and not s.get("cotacoes"):
        return False, f"RADAR RODOU VAZIO: {resumo}."
    return True, f"ok: {resumo}"


if __name__ == "__main__":
    ok, msg = verificar()
    print(msg)
    sys.exit(0 if ok else 1)
