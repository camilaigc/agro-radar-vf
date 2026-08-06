# -*- coding: utf-8 -*-
"""
Radar Agro v7 — ingesta bruta COM relatorio de cobertura.

Diferenca em relacao a v6: nada falha em silencio. Cada fonte registra
http, erro exato, itens brutos, itens dentro da janela e itens que
sobreviveram ao dedupe. O relatorio vai para o log do Actions E para dentro
do JSON, em "cobertura". Se der zero, o JSON diz por que deu zero.

Nunca inventa item nem cotacao. Fonte que falha aparece como falha.

Uso: python build.py
"""
import os
import argparse, difflib, hashlib, html as html_lib, json, re, sys, time, unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
import unicodedata
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests, yaml

try:
    import feedparser
except ImportError:
    feedparser = None

# Modulos novos. Import defensivo: arquivo ausente vira nota na cobertura, nao
# quebra a coleta de noticias.
try:
    from cepea_canalrural import fetch_cepea_canalrural
except ImportError:
    fetch_cepea_canalrural = None
try:
    from painel_do_historico import rodar as rodar_painel_hist
except ImportError:
    rodar_painel_hist = None
try:
    from historico_pinksheet import rodar as rodar_pinksheet
except ImportError:
    rodar_pinksheet = None
try:
    from arquivo import atualizar_arquivo
except ImportError:
    atualizar_arquivo = None
try:
    from cotacoes_fertilizantes import fetch_fertilizantes
except ImportError:
    fetch_fertilizantes = None
try:
    from cotacoes_ouro_serie import rodar as rodar_ouro_serie
except ImportError:
    rodar_ouro_serie = None

ROOT = Path(__file__).resolve().parent
DOCS, DATA = ROOT / "docs", ROOT / "data"
SEEN_PATH = DATA / "seen.json"
TZ = ZoneInfo("America/Sao_Paulo")
TIMEOUT, SLEEP = 30, 1.0
# Versao das regras de triagem. Ao mudar termos, exclusoes ou a regra de plural,
# incremente: o acumulado do dia gravado sob regra antiga e descartado em vez de
# sobreviver classificado errado.
TRIAGEM_VERSAO = 4

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}
GN_CFG = {"pt": "hl=pt-BR&gl=BR&ceid=BR:pt-419", "en": "hl=en-US&gl=US&ceid=US:en"}


def now_brt():
    return datetime.now(TZ)


def research_window(now, dias=None):
    if dias is None:
        dias = 3 if now.weekday() == 0 else 1
    return (now - timedelta(days=dias)).replace(hour=0, minute=0, second=0, microsecond=0), now


def norm(t):
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower()).strip()


def title_hash(t):
    return hashlib.sha1(norm(t).encode()).hexdigest()[:16]


def strip_gn_suffix(title):
    m = re.match(r"^(.*)\s+-\s+([^-]{2,60})$", title or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else ((title or "").strip(), None)


def parse_entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc).astimezone(TZ)
            except Exception:
                pass
    return None


# ----------------------------------------------------------------- coleta
def fetch_feed(url):
    """Devolve (entries, meta). Levanta excecao com o motivo real."""
    r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    meta = {"http": r.status_code, "bytes": len(r.content),
            "content_type": (r.headers.get("Content-Type") or "")[:60]}
    r.raise_for_status()
    if feedparser is None:
        raise RuntimeError("feedparser ausente")
    p = feedparser.parse(r.content)
    meta["xml_malformado"] = bool(p.bozo)
    if p.bozo and not p.entries:
        raise ValueError(f"XML ilegivel: {str(getattr(p, 'bozo_exception', ''))[:100]}")
    return p.entries, meta


def gn_url(q, lang, dias=None):
    """URL de busca do Google News, com recorte de data opcional.

    Por que o recorte: medido em 05/08/2026, a camada de consulta ampla devolveu
    1.950 itens brutos e apenas 16 dentro da janela, 0,8% de aproveitamento. O
    endpoint de busca ordena por RELEVANCIA, nao por data, e enche a resposta de
    materia antiga que depois e descartada pelo filtro de janela. O operador
    "when:Nd" corta na origem, e N acompanha a janela do proprio radar: 4 dias na
    segunda-feira, 2 nos outros dias.
    """
    termo = f"{q} when:{int(dias)}d" if dias else q
    return f"https://news.google.com/rss/search?q={quote_plus(termo)}&{GN_CFG.get(lang, GN_CFG['pt'])}"


def sem_acento(txt):
    """Versao sem acento do termo, para retry.

    Motivo concreto: "recuperacao judicial agronegocio" com acento devolveu HTTP 200
    e ZERO itens em 05/08/2026, enquanto outras consultas acentuadas funcionaram.
    Mesmo padrao do coletor do Comex Stat: tenta a variante, registra qual funcionou.
    """
    return "".join(c for c in unicodedata.normalize("NFD", txt)
                   if unicodedata.category(c) != "Mn")


def collect(cfg, window_start, usar_google_news=True):
    items, cobertura = [], []

    def ingest(entries, source_name, layer, trusted):
        """Devolve (brutos, dentro_da_janela)."""
        dentro = 0
        for e in entries:
            raw = (e.get("title") or "").strip()
            if not raw:
                continue
            title, gn_src = strip_gn_suffix(raw) if layer != "feed" else (raw, None)
            when = parse_entry_date(e)
            if when is not None and when < window_start:
                continue
            snippet = html_lib.unescape(re.sub(r"<[^>]+>", " ", e.get("summary", "") or ""))
            snippet = re.sub(r"\s+", " ", snippet).strip()[:400]
            items.append({"title": title, "link": e.get("link") or "",
                          "source": gn_src or source_name, "when": when,
                          "layer": layer, "trusted": trusted, "snippet": snippet})
            dentro += 1
        return len(entries), dentro

    def rodar(kind, label, url, layer, trusted, source_name):
        rec = {"camada": kind, "fonte": label, "url": url}
        try:
            entries, meta = fetch_feed(url)
            rec.update(meta)
            brutos, dentro = ingest(entries, source_name, layer, trusted)
            rec.update({"status": "ok", "itens_brutos": brutos, "na_janela": dentro})
            if brutos and not dentro:
                rec["nota"] = "todos os itens ficaram fora da janela de datas"
            elif not brutos:
                rec["status"] = "falha"
                rec["erro"] = "respondeu HTTP 200 mas 0 itens: nao e um feed valido"
        except requests.HTTPError as ex:
            rec.update({"status": "falha", "itens_brutos": 0, "na_janela": 0,
                        "http": getattr(ex.response, "status_code", None),
                        "erro": f"HTTP {getattr(ex.response, 'status_code', '?')}"})
        except Exception as ex:
            rec.update({"status": "falha", "itens_brutos": 0, "na_janela": 0,
                        "erro": f"{type(ex).__name__}: {ex}"[:160]})
        cobertura.append(rec)
        time.sleep(SLEEP)

    for f in cfg.get("feeds", []) or []:
        rodar("RSS", f["name"], f["url"], "feed", bool(f.get("trusted")), f["name"])

    if usar_google_news:
        for s in cfg.get("site_watch", []) or []:
            rodar("Site via Google News", s["domain"],
                  gn_url(f"site:{s['domain']}", s.get("lang", "pt")),
                  "site_watch", bool(s.get("trusted")), s["domain"])
        # dias do recorte = mesma janela do radar (3 na segunda, 1 nos outros),
        # com 1 dia de folga para fuso e atraso de indexacao do Google
        # dias do recorte = tamanho real da janela + 1 de folga, para fuso e atraso
        # de indexacao do Google. Na segunda a janela e de 3 dias, entao vira when:4d.
        from datetime import datetime as _dt
        _agora = _dt.now(window_start.tzinfo) if window_start.tzinfo else _dt.now()
        dias_janela = max(1, int((_agora - window_start).total_seconds() // 86400) + 1)
        dias_corte = int(cfg.get("dias_recorte_consulta") or (dias_janela + 1))
        for q in cfg.get("queries", []) or []:
            lang = q.get("lang", "pt")
            antes = len(items)
            rodar("Consulta Google News", q["q"],
                  gn_url(q["q"], lang, dias_corte), "query", False, "Google News")
            # retry sem acento quando a consulta acentuada nao devolveu nada
            if len(items) == antes and sem_acento(q["q"]) != q["q"]:
                rodar("Consulta Google News", q["q"] + " [sem acento]",
                      gn_url(sem_acento(q["q"]), lang, dias_corte),
                      "query", False, "Google News")

    return items, cobertura


# ----------------------------------------------------------------- dedupe
def load_seen():
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def marcar_repetidos(items, seen, now, dias_memoria=60):
    """NAO elimina nada. Cada item e mantido e recebe marcas:
        novo         -> primeira vez que este titulo aparece
        visto_em     -> data em que apareceu pela primeira vez
        parecido_com -> titulo quase igual visto na mesma execucao (mesma materia
                        publicada por varios veiculos), apenas para agrupar

    A politica anterior descartava item ja visto e isso apagava noticia do dia.
    Agora a decisao de olhar ou nao fica com quem le, nao com o script."""
    corte = (now - timedelta(days=dias_memoria)).isoformat()
    seen = {h: t for h, t in seen.items() if t >= corte}
    prio = {"feed": 0, "site_watch": 1, "query": 2, "commodity": 3}
    items = sorted(items, key=lambda i: (prio.get(i["layer"], 9),
                                         -(i["when"].timestamp() if i["when"] else 0)))
    novos = repetidos = parecidos = 0
    vistos_agora = []
    for it in items:
        h = title_hash(it["title"])
        anterior = seen.get(h)
        if anterior:
            it["novo"] = False
            it["visto_em"] = anterior[:10]
            repetidos += 1
        else:
            it["novo"] = True
            it["visto_em"] = now.date().isoformat()
            seen[h] = now.isoformat()
            novos += 1
        n = norm(it["title"])
        # limite mais frouxo que antes de proposito: agora isso apenas ROTULA o item
        # como parecido com outro, nao remove nada, entao errar para o lado de agrupar
        # nao custa noticia perdida
        semelhante = next((t for t, k in vistos_agora
                           if difflib.SequenceMatcher(None, n, k).ratio() > 0.85), None)
        it["parecido_com"] = semelhante
        if semelhante:
            parecidos += 1
        else:
            vistos_agora.append((it["title"], n))
    return items, seen, {"novos": novos, "repetidos_de_edicoes_anteriores": repetidos,
                         "agrupados_por_titulo_parecido": parecidos,
                         "descartados": 0}



# ------------------------------------------------------- porta setorial
def compilar_setores(cfg):
    """(termo_normalizado, micro) por termo, ordenado do mais longo para o mais curto,
    para que 'nutricao animal' vença 'racao' quando os dois aparecerem."""
    pares = []
    for micro, termos in (cfg.get("setores") or {}).items():
        for t in termos:
            pares.append((norm(t), micro))
    pares.sort(key=lambda p: -len(p[0]))
    return pares


VOGAIS = "aeiou"


def _regex_termo(termo):
    """Plural do portugues sem criar palavra nova. Termo terminado em vogal aceita
    apenas '+s' (milho -> milhos), NUNCA '+es', senao 'milho' casaria com 'milhoes'
    e qualquer valor em milhoes entraria como milho. Termo terminado em consoante
    aceita '+es' e '+s' (acucar -> acucares, seed -> seeds)."""
    sufixo = r"s?" if termo and termo[-1] in VOGAIS else r"(?:es|s)?"
    return re.compile(r"\b" + re.escape(termo) + sufixo + r"\b")


def _regex_frase(frase):
    """Regex de expressao com mais de uma palavra, tolerante a espaco extra."""
    partes = [re.escape(p) for p in frase.split()]
    return re.compile(r"\b" + r"\s+".join(partes) + r"\b")


def compilar_regex(pares):
    return [(t, m, _regex_termo(t)) for t, m in pares]


def casar_setor(texto, compilados):
    """Devolve (termo, micro) ou (None, None). Limite de palavra para nao casar
    'boi' dentro de 'boiada'."""
    alvo = norm(texto)
    for termo, micro, rx in compilados:
        if rx.search(alvo):
            return termo, micro
    return None, None


def carregar_vigiados(cfg):
    """Le a lista de nomes vigiados de FORA do repositorio.

    Precedencia: env ENTIDADES_VIGIADAS (um nome por linha) > arquivo apontado por
    ENTIDADES_VIGIADAS_ARQUIVO > entidades_vigiadas.local.yml (no .gitignore).
    Devolve (nomes, origem). Lista no sources.yml e aceita apenas como ultimo
    recurso e com aviso, porque este repositorio e publico.
    """
    import os
    bruto = os.environ.get("ENTIDADES_VIGIADAS")
    if bruto and bruto.strip():
        nomes = [l.strip(" -\t") for l in bruto.splitlines() if l.strip(" -\t")]
        return nomes, f"variavel de ambiente ({len(nomes)} nomes)"
    caminho = os.environ.get("ENTIDADES_VIGIADAS_ARQUIVO") or "entidades_vigiadas.local.yml"
    pth = Path(caminho)
    if pth.exists():
        try:
            dados = yaml.safe_load(pth.read_text(encoding="utf-8")) or []
            nomes = dados.get("entidades_vigiadas", []) if isinstance(dados, dict) else list(dados)
            nomes = [str(x).strip() for x in nomes if str(x).strip()]
            return nomes, f"{pth.name} ({len(nomes)} nomes)"
        except Exception as ex:
            return [], f"{pth.name} ilegivel: {type(ex).__name__}"
    do_yml = cfg.get("entidades_vigiadas") or []
    if do_yml:
        return [str(x) for x in do_yml], ("sources.yml (ATENCAO: repositorio publico, "
                                          "mova para um secret)")
    return [], "nenhuma fonte configurada"


def compilar_vigiados(cfg):
    """Compila a lista de nomes vigiados em regex de palavra inteira.

    Nome com menos de `vigiados_min_letras` e descartado de proposito: sigla curta
    vira ruido (por exemplo "ICL" casaria dentro de outras palavras se nao fosse a
    borda \b, e mesmo com borda gera falso positivo demais).
    """
    minimo = int(cfg.get("vigiados_min_letras", 4))
    nomes, origem = carregar_vigiados(cfg)
    saida, ignorados = [], []
    for nome in nomes:
        n = norm(str(nome)).strip()
        if len(n.replace(" ", "")) < minimo:
            ignorados.append(nome)
            continue
        partes = [re.escape(x) for x in n.split()]
        saida.append((str(nome), re.compile(r"\b" + r"\s+".join(partes) + r"\b")))
    return saida, ignorados, origem


def compilar_ambiguas(cfg):
    """Nome vigiado que tambem e palavra comum: exige termo de contexto."""
    saida = []
    for nome, contextos in (cfg.get("entidades_ambiguas") or {}).items():
        n = norm(str(nome)).strip()
        rx = re.compile(r"\b" + r"\s+".join(re.escape(x) for x in n.split()) + r"\b")
        ctx = [_regex_termo(norm(c)) for c in (contextos or [])]
        saida.append((str(nome), rx, ctx))
    return saida


def casar_vigiados(texto_norm, vigiados, ambiguas=()):
    """Nomes vigiados presentes no texto.

    Nome da lista simples entra direto. Nome ambiguo entra so com contexto: "Vale"
    exige minerio, mineradora, ferro, Carajas e afins, senao casaria o verbo valer.
    """
    achados = [nome for nome, rx in vigiados if rx.search(texto_norm)]
    for nome, rx, ctx in ambiguas:
        if rx.search(texto_norm) and any(c.search(texto_norm) for c in ctx):
            achados.append(nome)
    return achados


def limpar_negativas(texto, negativas_rx):
    """Apaga expressoes idiomaticas ANTES de procurar termo do setor.

    Motivo concreto: a materia "Golden Age: Morgan ve era de ouro para energia e
    destaca Petrobras" entrou em Mineracao porque a palavra "ouro" casou. Ela nao
    fala de ouro, fala da Petrobras. Apagando "era de ouro" do texto de analise, o
    termo simplesmente nao existe mais para o classificador. O texto ORIGINAL do
    item nao e alterado: isto vale so para a decisao.
    """
    alvo = texto
    achadas = []
    for expr, rx in negativas_rx:
        if rx.search(alvo):
            achadas.append(expr)
            alvo = rx.sub(" ", alvo)
    return alvo, achadas


def pontuar_setores(texto_norm, compilados, fracos, coocor):
    """Escolhe a categoria por PONTOS, nao pelo primeiro termo que casa.

    Peso: termo inequivoco vale 2, termo ambiguo vale 1 e so pontua se houver, no
    mesmo texto, um termo de confirmacao daquela categoria (coocorrencia). "ouro"
    sozinho nao classifica como Mineracao; "ouro" perto de jazida, lavra, garimpo,
    mina ou producao, sim.

    Devolve (micro, termo_principal, pontos, evidencia) com a evidencia escrita,
    para a classificacao ser auditavel depois.
    """
    placar, provas = {}, {}
    for termo, micro, rx in compilados:
        if not rx.search(texto_norm):
            continue
        fraco = termo in fracos
        if fraco:
            confirmadores = coocor.get(micro, [])
            tem_confirmacao = any(_rx_coocor(c).search(texto_norm) for c in confirmadores)
            if not tem_confirmacao:
                continue                     # ambiguo e sem confirmacao: nao pontua
            peso, marca = 1, f"{termo} (ambiguo, confirmado)"
        else:
            peso, marca = 2, termo
        placar[micro] = placar.get(micro, 0) + peso
        provas.setdefault(micro, []).append(marca)
    if not placar:
        return None, None, 0, ""
    micro = max(placar, key=lambda k: (placar[k], -len(k)))
    ev = provas[micro]
    return micro, ev[0].split(" (")[0], placar[micro], ", ".join(ev[:4])


_CACHE_COOCOR = {}


def _rx_coocor(termo):
    if termo not in _CACHE_COOCOR:
        _CACHE_COOCOR[termo] = _regex_termo(norm(termo))
    return _CACHE_COOCOR[termo]


def classificar_transacao(texto_norm, cfg_tr):
    """Separa transacao de noticia. Tres faixas, e NADA e descartado.

    forte  = linguagem que so aparece em operacao ("assina contrato de compra e
             venda", "ato de concentracao", "assume o controle")
    fraca  = linguagem que aparece em operacao E em texto comum ("compra",
             "aporte", "capta")
    valor  = presenca de cifra em R$ ou US$, que reforca

    Regra: >=1 forte, ou >=2 fracas, ou 1 fraca + valor  -> "transacao"
           1 fraca isolada                               -> "possivel_transacao"
           nada                                          -> "noticia"
    """
    fortes = [t for t in (cfg_tr.get("fortes") or []) if _rx_coocor(t).search(texto_norm)]
    fracas = [t for t in (cfg_tr.get("fracas") or []) if _rx_coocor(t).search(texto_norm)]
    tem_valor = bool(re.search(r"\b(?:r\$|us\$|usd)\s*\d|\b\d+[,.]?\d*\s*(?:bilh|milh)", texto_norm))
    ev = []
    if fortes:
        ev.append("forte: " + ", ".join(fortes[:3]))
    if fracas:
        ev.append("fraca: " + ", ".join(fracas[:3]))
    if tem_valor:
        ev.append("cifra no texto")
    # Regra apertada depois do teste com dados reais: cifra no texto NAO promove
    # sozinha, porque quase toda materia de negocio tem cifra. Promove so termo
    # forte, ou duas expressoes fracas juntas.
    if fortes or len(fracas) >= 2:
        return "transacao", "; ".join(ev)
    if fracas:
        return "possivel_transacao", "; ".join(ev)
    return "noticia", ""


EVENTOS_ESPECIAIS = (
    "recuperacao judicial", "pedido de recuperacao judicial", "falencia",
    "reestruturacao de divida", "leilao judicial", "judicial recovery",
    "bankruptcy", "debt restructuring",
)
EVENTOS_MERCADO_CAPITAIS = (
    "ipo", "follow on", "oferta subsequente", "oferta publica de acoes",
    "cra", "fiagro", "debenture", "bond offering",
)
EVENTOS_CAPTACAO = (
    "rodada de investimento", "series a", "series b", "series c",
    "aporte", "captacao", "capta r", "capta us", "levanta r", "levanta us",
    "funding round", "venture capital",
)


def classificar_tipo_evento(texto_norm, classe):
    """Separa M&A de captação, mercado de capitais e situações especiais.

    A função só organiza a exibição. Ela nunca remove um item nem rebaixa a fila
    de revisão. ``classe`` continua sendo o sinal amplo de operação usado pela
    triagem; ``tipo_evento`` é a taxonomia profissional mostrada no site.
    """
    if classe == "noticia":
        return "noticia"
    if any(_rx_coocor(t).search(texto_norm) for t in EVENTOS_ESPECIAIS):
        return "situacao_especial"
    if any(_rx_coocor(t).search(texto_norm) for t in EVENTOS_MERCADO_CAPITAIS):
        return "mercado_capitais"
    if any(_rx_coocor(t).search(texto_norm) for t in EVENTOS_CAPTACAO):
        return "captacao"
    if classe == "possivel_transacao":
        return "possivel_operacao"
    return "ma"


def triar(items, cfg):
    """Separa dentro e fora do escopo. Nada e descartado: o que fica fora vai para
    lista propria no JSON, sempre com o motivo.

    Mudou em relacao a versao anterior, que classificava pelo PRIMEIRO termo que
    casava e por isso mandava "era de ouro" para Mineracao:
      - expressoes idiomaticas sao apagadas do texto de analise antes de tudo;
      - a categoria e escolhida por pontos, com termo ambiguo exigindo confirmacao;
      - fonte GENERALISTA passou a exigir 2 pontos (um termo inequivoco, ou dois
        ambiguos confirmados). Fonte setorial continua entrando com 1 ponto;
      - cada item recebe "classe" (transacao / possivel_transacao / noticia) e a
        evidencia que levou a isso.
    """
    compilados = compilar_regex(compilar_setores(cfg))
    excl = compilar_regex([(norm(t), "_excluido") for t in (cfg.get("excluir_termos") or [])])
    negativas_rx = [(e, _regex_frase(norm(e))) for e in (cfg.get("negativas") or [])]
    fracos = {norm(t) for t in (cfg.get("termos_fracos") or [])}
    coocor = {k: v for k, v in (cfg.get("coocorrencia") or {}).items()}
    cfg_tr = cfg.get("transacao") or {}
    fontes_fora = {norm(f) for f in (cfg.get("fontes_excluidas") or [])}
    vigiados, vigiados_ignorados, origem_vigiados = compilar_vigiados(cfg)
    print(f"  nomes vigiados: {len(vigiados)} ativos, origem: {origem_vigiados}")
    ambiguas = compilar_ambiguas(cfg)
    minimo_generalista = int(cfg.get("pontos_minimos_generalista", 2))

    dentro, fora = [], []
    for it in items:
        bruto = norm(f"{it['title']} {it.get('snippet','')}")
        limpo, negativas_achadas = limpar_negativas(bruto, negativas_rx)
        micro, termo, pontos, evidencia = pontuar_setores(limpo, compilados, fracos, coocor)

        it["micro_sugerida"] = micro
        it["termo_setorial"] = termo
        it["pontos_setor"] = pontos
        it["evidencia_setor"] = evidencia
        if negativas_achadas:
            it["negativas"] = negativas_achadas
        it["classe"], it["evidencia_transacao"] = classificar_transacao(limpo, cfg_tr)
        it["tipo_evento"] = classificar_tipo_evento(limpo, it["classe"])

        # Rede de segurança: o item pode ficar fora do recorte setorial, mas um
        # sinal corporativo ou qualquer evidência parcial
        # obriga sua presença na fila de revisão da página principal. Assim, o
        # filtro organiza a leitura sem fazer uma notícia potencialmente relevante
        # desaparecer em uma lista pouco consultada.
        it["prioridade_revisao"] = bool(
            it["classe"] != "noticia" or pontos > 0
        )

        # NOMES VIGIADOS: entra no escopo sem passar pela porta setorial. E o
        # mecanismo de recall do sistema. Custo de falso positivo: cinco segundos de
        # leitura. Custo de falso negativo: perder noticia de um nome do processo.
        # Roda ANTES da porta setorial, e depois da lista de fontes excluidas, porque
        # publicidade continua sendo publicidade mesmo citando um nome vigiado.
        achados_vig = casar_vigiados(bruto, vigiados, ambiguas)
        if achados_vig:
            it["vigiados"] = achados_vig

        if norm(it.get("source", "")) in fontes_fora:
            it["motivo"] = "fonte na lista de exclusao"
            fora.append(it)
            continue

        if achados_vig:
            it["motivo"] = ("nome vigiado: " + ", ".join(achados_vig[:3])
                            + (f" | {pontos} ponto(s) de setor: {evidencia}" if pontos else
                               " | sem termo do setor, entrou pelo nome"))
            dentro.append(it)
            continue

        exigido = 1 if it.get("trusted") else minimo_generalista
        # Excecao deliberada: fonte generalista com sinal INEQUIVOCO de operacao e
        # pelo menos 1 ponto de setor entra com 1 ponto. Motivo: e exatamente o item
        # que nao se pode perder. "Grupo do agro entra em recuperacao judicial" vinha
        # de fonte generalista e caia fora pela regra dos 2 pontos.
        if it["classe"] == "transacao" and pontos >= 1:
            exigido = 1
            it["motivo_excecao"] = "entrou com 1 ponto por sinal de transacao"
        if pontos >= exigido:
            it["motivo"] = (f"{'fonte setorial + ' if it.get('trusted') else ''}"
                            f"{pontos} ponto(s): {evidencia}"
                            + (" | " + it["motivo_excecao"] if it.get("motivo_excecao") else ""))
            dentro.append(it)
            continue

        if pontos > 0:                       # casou, mas fraco para fonte generalista
            it["motivo"] = (f"fonte generalista com evidencia fraca ({pontos} ponto, "
                            f"minimo {minimo_generalista}): {evidencia}")
            fora.append(it)
            continue

        termo_x, _ = casar_setor(limpo, excl)
        if termo_x:
            it["motivo"] = f"assunto fora do setor (termo: {termo_x})"
            fora.append(it)
            continue
        if negativas_achadas:
            it["motivo"] = ("termo do setor aparecia so em expressao sem relacao: "
                            + ", ".join(negativas_achadas[:3]))
            fora.append(it)
            continue
        if it.get("trusted"):
            it["motivo"] = "fonte setorial, sem termo do setor no titulo ou trecho"
            dentro.append(it)
        else:
            it["motivo"] = "fonte generalista sem termo do setor no titulo ou trecho"
            fora.append(it)

    if vigiados_ignorados:
        print(f"  aviso: {len(vigiados_ignorados)} nome(s) vigiado(s) ignorado(s) por serem "
              f"curtos demais: {', '.join(map(str, vigiados_ignorados[:8]))}")

    ordem_classe = {"transacao": 0, "possivel_transacao": 1, "noticia": 2}
    dentro.sort(key=lambda x: (ordem_classe.get(x.get("classe"), 3),
                               0 if x.get("termo_setorial") else 1,
                               -(x["when"].timestamp() if x.get("when") else 0)))
    return dentro, fora


# ----------------------------------------------------------------- cotacoes
CEPEA_MAP = [
    (("boi",), "boi_gordo", "Boi gordo CEPEA/ESALQ SP", "R$/@"),
    (("soja", "paranagu"), "soja_paranagua", "Soja fisico CEPEA Paranagua", "R$/sc 60kg"),
    (("soja",), "soja_cepea", "Soja fisico CEPEA", "R$/sc 60kg"),
    (("milho",), "milho_campinas", "Milho fisico ESALQ Campinas-SP", "R$/sc 60kg"),
    (("trigo",), "trigo_parana", "Trigo fisico CEPEA Parana", "R$/t"),
]


# Unidades que o widget do CEPEA escreve na propria linha. Servem para produto
# que nao esta no CEPEA_MAP: em vez de eu chutar a unidade, le-se a da pagina.
CEPEA_UNIDADES = [
    (r"R\$\s*/\s*@", "R$/@"),
    (r"R\$\s*/\s*sc(?:a)?\.?\s*(?:de\s*)?60\s*kg", "R$/sc 60kg"),
    (r"R\$\s*/\s*sc(?:a)?\.?\s*(?:de\s*)?50\s*kg", "R$/sc 50kg"),
    (r"R\$\s*/\s*sc(?:a)?", "R$/sc"),
    (r"R\$\s*/\s*t(?:on(?:elada)?)?\b", "R$/t"),
    (r"R\$\s*/\s*kg", "R$/kg"),
    (r"R\$\s*/\s*(?:litro|l)\b", "R$/litro"),
    (r"R\$\s*/\s*(?:cx|caixa)", "R$/caixa"),
    (r"R\$\s*/\s*(?:dz|duzia)", "R$/duzia"),
    (r"R\$\s*/\s*m3", "R$/m3"),
    (r"US\$\s*/\s*t(?:on)?\b", "US$/t"),
    (r"US\$\s*/\s*lb", "US$/lb"),
]


def _slug_cepea(txt):
    base = norm(txt).replace(" ", "_")
    base = re.sub(r"[^a-z0-9_]", "", base).strip("_")
    return f"cepea_{base[:40]}" if base else "cepea_sem_nome"


def _unidade_da_linha(linha):
    for padrao, rotulo in CEPEA_UNIDADES:
        if re.search(padrao, linha, re.I):
            return rotulo
    return None


def parse_cepea_widget(texto, notas=None):
    """Le TODAS as linhas de indicador do widget, nao apenas as conhecidas.

    Antes: linha cujo produto nao estava no CEPEA_MAP era descartada em silencio.
    Consequencia pratica: acrescentar um id_indicador na URL do widget nao trazia
    nada, e nada avisava. Agora:
      - produto no CEPEA_MAP  -> usa o rotulo e a unidade canonicos de lá;
      - produto desconhecido  -> usa o nome como esta na pagina e a unidade lida
        da propria linha; se a linha nao declarar unidade, grava
        "nao declarada na linha" e registra em notas, para nao passar por
        unidade conhecida o que nao e.
    Assim, mudar a lista de indicadores na URL nao exige mexer no codigo.
    """
    lits = re.findall(r"document\.write\(\s*(['\"])(.*?)\1\s*\)", texto, re.S)
    html = "".join(m[1] for m in lits) if lits else texto
    html = html.replace("\\'", "'").replace('\\"', '"').replace("\\/", "/")
    achados, desconhecidos, sem_unidade = [], [], []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [re.sub(r"<[^>]+>", " ", c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells if c.strip()]
        if not cells:
            continue
        linha = " ".join(cells)
        low = linha.lower()
        mdate = re.search(r"(\d{2}/\d{2}/\d{4})", linha)
        mval = re.search(r"([\d]{1,3}(?:\.\d{3})*,\d{2})", linha)
        if not (mdate and mval):
            continue      # cabecalho, rodape, credito: nao e linha de indicador

        alvo = next(((k, lbl, un) for palavras, k, lbl, un in CEPEA_MAP
                     if all(p in low for p in palavras)), None)
        if alvo:
            key, label, unidade = alvo
            conhecido = True
        else:
            # nome do produto = primeira celula, que e onde o widget escreve
            nome = re.sub(r"\s*\d{2}/\d{2}/\d{4}.*$", "", cells[0]).strip(" -:\u2013")
            nome = nome or "Indicador CEPEA sem nome"
            key, label = _slug_cepea(nome), f"{nome} (CEPEA)"
            unidade = _unidade_da_linha(linha)
            conhecido = False
            desconhecidos.append(nome)
            if not unidade:
                unidade = "nao declarada na linha"
                sem_unidade.append(nome)

        achados.append({
            "key": key, "label": label, "unidade": unidade,
            "preco": float(mval.group(1).replace(".", "").replace(",", ".")),
            "preco_texto": mval.group(1),
            "data_referencia": datetime.strptime(mdate.group(1), "%d/%m/%Y").date().isoformat(),
            "fonte": "CEPEA/ESALQ (widget oficial)",
            "no_mapa": conhecido,
            "linha_original": linha[:160],
        })

    if notas is not None:
        if desconhecidos:
            notas.append("CEPEA: indicadores fora do CEPEA_MAP, publicados com o nome e a "
                         f"unidade lidos da pagina: {', '.join(desconhecidos[:8])}")
        if sem_unidade:
            notas.append("CEPEA: sem unidade declarada na linha, gravados como "
                         f"'nao declarada na linha': {', '.join(sem_unidade[:8])}. "
                         "Confirmar antes de usar em conta.")
    return achados



def fetch_bcb_ptax(cfg, now, notas):
    """USD/BRL PTAX venda, serie 1 do SGS. Endpoint confirmado HTTP 200 em 05/08/2026.
    Descarta cotacao velha: o SGS ja atrasou semanas no passado."""
    c = (cfg.get("bcb_ptax") or {})
    if not c.get("enabled"):
        notas.append("BCB PTAX desativado no sources.yml")
        return []
    try:
        r = requests.get(c["url"], headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        serie = r.json()
        if not serie:
            notas.append("BCB PTAX: serie vazia")
            return []
        ultimo = serie[-1]
        d = datetime.strptime(ultimo["data"], "%d/%m/%Y").date()
        idade = (now.date() - d).days
        limite = int(c.get("max_idade_dias", 7))
        if idade > limite:
            notas.append(f"BCB PTAX descartada: cotacao de {d.isoformat()}, {idade} dias de atraso (limite {limite})")
            return []
        valor = float(str(ultimo["valor"]).replace(",", "."))
        notas.append(f"BCB PTAX ok ({d.isoformat()})")
        return [{
            "key": "usdbrl_ptax", "label": "USD/BRL - PTAX venda",
            "preco": valor, "preco_texto": f"{valor:.4f}".replace(".", ","),
            "unidade": "R$/US$", "data_referencia": d.isoformat(),
            "fonte": "Banco Central do Brasil, SGS serie 1",
        }]
    except Exception as ex:
        notas.append(f"BCB PTAX falha: {type(ex).__name__}: {str(ex)[:90]}")
        return []



# ------------------------------------------------- cotacoes: Trading Economics
# Estrutura confirmada na amostra de 05/08/2026 (docs/diagnostico-fontes.json):
# o texto da pagina traz "Actual 6.6645 Daily Change ...", a unidade em "USD/Lbs"
# ou "USD/MT", a data em "was last updated on August 5 of 2026", e o MESMO valor
# aparece num bloco JSON da pagina como "last": 6.664500000000.
# Publicamos so quando os dois valores batem. Divergencia = nao publica.
MESES_EN = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,
            "august":8,"september":9,"october":10,"november":11,"december":12}

UNIDADE_TE = {"lbs":"US$/lb","lb":"US$/lb","mt":"US$/t","t":"US$/t","bu":"US$/bu",
              "bushel":"US$/bu","toz":"US$/oz t","oz":"US$/oz t","gal":"US$/gal"}


def _texto_limpo(html):
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)


def parse_trading_economics(html, rotulo):
    """Devolve (registro, motivo). registro=None quando nao da para confiar."""
    txt = _texto_limpo(html)
    # O JSON embutido e a fonte primaria de valor: ele existe em todas as paginas.
    js = [float(x) for x in re.findall(r'["\'](?:last)["\']\s*:\s*"?(-?[\d]+(?:\.[\d]+)?)"?', html)]
    m = re.search(r"\bActual\s+([\d]+(?:[.,][\d]+)?)\s", txt)

    if m:
        preco = float(m.group(1).replace(",", ""))
        if not js:
            return None, f"{rotulo}: campo 'last' do JSON ausente, sem como conferir o valor"
        if not any(abs(v - preco) <= max(0.01, abs(preco) * 0.002) for v in js):
            return None, (f"{rotulo}: divergencia entre texto ({preco}) e JSON ({js[:3]}), "
                          f"nao publicado")
        origem_valor = "campo Actual, conferido contra o JSON"
    else:
        # Plano B. A pagina da soja nao traz o bloco "Actual" (execucao de 05/08/2026:
        # "nao achei o campo 'Actual'"), mas traz o valor no texto corrido e no JSON.
        # Exige as DUAS leituras batendo, senao nao publica: o JSON sozinho nao diz
        # a que serie o numero pertence.
        # numero com separador de milhar ("1,166.00") OU numero simples ("1166.00"),
        # seguido da unidade. A primeira versao exigia grupo de milhar e casava lixo.
        mt = re.search(r"\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*US[Dd][dc]?\s*/\s*[A-Za-z]",
                       txt)
        if not (mt and js):
            return None, (f"{rotulo}: sem campo 'Actual' e sem par texto+JSON para conferir "
                          f"(texto={'ok' if mt else 'ausente'}, json={'ok' if js else 'ausente'})")
        preco = float(mt.group(1).replace(",", ""))
        if not any(abs(v - preco) <= max(0.01, abs(preco) * 0.002) for v in js):
            return None, (f"{rotulo}: sem 'Actual', e texto ({preco}) divergiu do JSON "
                          f"({js[:3]}), nao publicado")
        origem_valor = "texto corrido (sem bloco Actual), conferido contra o JSON"

    # Unidade: o TE escreve "USd/Bu" para grao, "USD/T" para metal-base e
    # "USD/T.oz" ou "USD/t oz" para metais preciosos. A versão anterior parava
    # no primeiro "T" de "T.oz" e publicava prata e platina como US$/t. Agora o
    # sufixo inteiro é capturado e normalizado antes do mapeamento.
    mu = re.search(r"US[Dd]\s*/\s*([A-Za-z]{1,6}(?:\.\s*oz|\s+oz)?)\b", txt)
    if mu:
        codigo_unidade = re.sub(r"[^a-z]", "", mu.group(1).lower())
        unidade = UNIDADE_TE.get(codigo_unidade, f"USD/{mu.group(1).strip()}")
    else:
        unidade = "nao identificada"

    md = re.search(r"last updated on ([A-Za-z]+)\s+(\d{1,2})\s+of\s+(\d{4})", txt, re.I)
    if md and md.group(1).lower() in MESES_EN:
        data_ref = datetime(int(md.group(3)), MESES_EN[md.group(1).lower()], int(md.group(2))).date().isoformat()
    else:
        return None, f"{rotulo}: data de referencia nao encontrada na pagina"

    return {
        "key": f"te_{norm(rotulo).replace(' ', '_')}",
        "label": rotulo,
        "preco": preco,
        "preco_texto": f"{preco:,.4f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", "."),
        "unidade": unidade,
        "data_referencia": data_ref,
        "metodologia": ("Trading Economics, referencia de mercado baseada em OTC/CFD que "
                        "acompanha o contrato de referencia, nao e fechamento oficial de "
                        f"bolsa. Valor lido do {origem_valor}."),
        "fonte": "Trading Economics (CFD que acompanha o benchmark)",
    }, f"{rotulo}: ok ({preco} {unidade}, ref {data_ref})"


TE_ALVOS = [
    ("https://tradingeconomics.com/commodity/gold", "Ouro"),
    ("https://tradingeconomics.com/commodity/copper", "Cobre"),
    ("https://tradingeconomics.com/commodity/iron-ore", "Minerio de ferro 62% Fe CFR China"),
]


def _converter_te(reg, alvo, notas):
    """Aplica conversao de unidade declarada no sources.yml, se houver.

    Serve para grao: o Trading Economics cota soja, milho e trigo em US centavos
    por bushel, e o painel precisa de US$/t. Bushel e unidade de PESO padronizada
    (soja e trigo 27,2155 kg; milho 25,4012 kg), entao o fator e constante fisica,
    nao estimativa. O valor original e a unidade original ficam gravados no
    registro para o numero continuar rastreavel.

    A faixa e uma guarda: se o TE trocar de unidade na pagina (de centavos para
    dolares, por exemplo), o valor cai fora da faixa e NADA e convertido nem
    publicado, em vez de sair um preco 100x errado.
    """
    fator = alvo.get("fator_usd_t")
    if not fator:
        return reg, None
    faixa = alvo.get("faixa_original")
    v = reg["preco"]
    if faixa and not (float(faixa[0]) <= v <= float(faixa[1])):
        return None, (f"{reg['label']}: valor {v} fora da faixa esperada "
                      f"{faixa} para a unidade de origem. Provavel troca de unidade "
                      f"na pagina do TE. Nada convertido, nada publicado.")
    convertido = round(v * float(fator), 2)
    reg["preco_original"] = v
    reg["unidade_original"] = reg.get("unidade")
    reg["preco"] = convertido
    reg["preco_texto"] = f"{convertido:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    reg["unidade"] = "US$/t"
    reg["metodologia"] = (f"Convertido de {v} {reg['unidade_original']} por fator {fator} "
                          f"(bushel e unidade de peso padronizada). Fonte do valor: "
                          f"Trading Economics, front-month.")
    return reg, f"{reg['label']}: convertido {v} {reg['unidade_original']} -> {convertido} US$/t"


def fetch_trading_economics(cfg, now, notas):
    c = (cfg.get("trading_economics") or {})
    if not c.get("enabled"):
        notas.append("Trading Economics desativado no sources.yml")
        return []
    alvos = (c.get("alvos") or []) or [{"url": u, "label": l} for u, l in TE_ALVOS]
    saida = []
    for a in alvos:
        url, rotulo = a["url"], a["label"]
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            reg, motivo = parse_trading_economics(r.text, rotulo)
            notas.append(motivo)
            if reg:
                reg, nota_conv = _converter_te(reg, a, notas)
                if nota_conv:
                    notas.append(nota_conv)
                if reg is None:
                    continue
                idade = (now.date() - datetime.fromisoformat(reg["data_referencia"]).date()).days
                limite = int(c.get("max_idade_dias", 5))
                if idade > limite:
                    notas.append(f"{rotulo}: descartado, referencia de {reg['data_referencia']} "
                                 f"com {idade} dias (limite {limite})")
                else:
                    saida.append(reg)
        except Exception as ex:
            notas.append(f"{rotulo}: falha {type(ex).__name__}: {str(ex)[:80]}")
        time.sleep(SLEEP)
    return saida


# ------------------------------------------------- cotacoes: USDA AMS (CBOT)
# Parser portado do skill agro-ma-clipping (quotes.py), que ja roda em producao.
AMS_MAP = {"SOYBEANS": ("soja_cbot", "Soja CBOT"), "CORN": ("milho_cbot", "Milho CBOT"),
           "WHEAT": ("trigo_cbot", "Trigo CBOT (SRW)")}
CBOT_MES = {"F":1,"G":2,"H":3,"J":4,"K":5,"M":6,"N":7,"Q":8,"U":9,"V":10,"X":11,"Z":12}
MES_EN_CURTO = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
# Conversao de unidade apenas (constante fisica), nao e estimativa de preco.
# MULTIPLICADOR de US¢/bushel para US$/tonelada:
#   soja e trigo: bushel = 27,2155 kg  ->  1/27,2155*1000/100 = 0,367437
#   milho:        bushel = 25,4012 kg  ->  1/25,4012*1000/100 = 0,393683
# Conferencia: 1208,50 US¢/bu de soja x 0,367437 = US$ 443,94/t
CENTAVOS_BU_PARA_USD_T = {"soja_cbot": 0.367437, "milho_cbot": 0.393683, "trigo_cbot": 0.367437}
FAIXA_CENTAVOS = {"soja_cbot": (700, 2200), "milho_cbot": (250, 900), "trigo_cbot": (350, 1400)}


def parse_usda_ams(texto, hoje):
    """Le o relatorio diario de graos da USDA AMS. Grao CBOT sem contrato
    identificado na linha e recusado: sem contrato nao se sabe qual vencimento."""
    entradas, notas = [], []
    data_iso = None
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto):
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            cand = datetime(yy, mm, dd).date()
        except ValueError:
            continue
        if cand <= hoje and (data_iso is None or cand.isoformat() > data_iso):
            data_iso = cand.isoformat()
    if data_iso is None:
        return [], ["USDA AMS: data do relatorio nao encontrada (esperado MM/DD/AAAA)"]

    visto = set()
    for linha in texto.split("\n"):
        ln = linha.strip()
        up = ln.upper()
        for rotulo_ams, (key, label) in AMS_MAP.items():
            if not up.startswith(rotulo_ams) or key in visto:
                continue
            cod = re.search(r"\b(Z[SCW])([FGHJKMNQUVXZ])(\d{2})\b", ln)
            contrato = mes_contrato = None
            if cod:
                contrato = cod.group(0)
                mes_contrato = f"{2000 + int(cod.group(3)):04d}-{CBOT_MES[cod.group(2)]:02d}"
            else:
                nomeado = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                                    r"[a-z]*\s*'?(\d{2})\b", ln)
                if nomeado:
                    mes = MES_EN_CURTO.index(nomeado.group(1)) + 1
                    contrato = f"{rotulo_ams[:2].title()}{nomeado.group(1)}{nomeado.group(2)}"
                    mes_contrato = f"{2000 + int(nomeado.group(2)):04d}-{mes:02d}"
            nums = [float(t.replace(",", "")) for t in re.findall(r"[\d,]+\.\d+", ln)]
            if not nums:
                notas.append(f"USDA AMS {label}: linha sem preco decimal legivel")
                break
            if mes_contrato is None:
                notas.append(f"USDA AMS {label}: contrato nao identificado, linha recusada")
                break
            # Nao assume posicao do numero na linha (a linha traz settlement e variacao).
            # Escolhe o unico numero dentro da faixa plausivel do grao. Se nenhum ou
            # mais de um se encaixar, recusa e registra os numeros vistos: melhor linha
            # vazia com motivo do que publicar a variacao como se fosse o fechamento.
            lo, hi = FAIXA_CENTAVOS[key]
            candidatos = [n for n in nums if lo <= n <= hi]
            if not candidatos:
                notas.append(f"USDA AMS {label}: nenhum numero da linha cai na faixa "
                             f"plausivel {lo}-{hi} US¢/bu (vistos: {nums}), descartado")
                break
            if len(candidatos) > 1:
                notas.append(f"USDA AMS {label}: ambiguo, {len(candidatos)} numeros na faixa "
                             f"{lo}-{hi} ({candidatos}), descartado por seguranca")
                break
            centavos = candidatos[0]
            visto.add(key)
            dolar_t = centavos * CENTAVOS_BU_PARA_USD_T[key]
            entradas.append({
                "key": key, "label": f"{label} — {contrato}",
                "preco": round(dolar_t, 2),
                "preco_texto": f"{dolar_t:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                "unidade": "US$/t",
                "data_referencia": data_iso,
                "fonte": (f"USDA AMS, settlement CBOT {contrato}; {centavos} US¢/bu x "
                          f"{CENTAVOS_BU_PARA_USD_T[key]} = US$/t"),
            })
            break
    if not entradas:
        notas.append("USDA AMS: nenhuma linha de grao aproveitavel no texto")
    return entradas, notas


def fetch_usda_ams(cfg, now, notas):
    c = (cfg.get("usda_ams") or {})
    if not c.get("enabled"):
        notas.append("USDA AMS desativado no sources.yml")
        return []
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        notas.append("USDA AMS: pdfminer.six ausente, adicione ao requirements.txt")
        return []
    try:
        import io
        r = requests.get(c["url"], headers=UA, timeout=60)
        r.raise_for_status()
        texto = extract_text(io.BytesIO(r.content))
        entradas, n = parse_usda_ams(texto, now.date())
        notas.extend(n)
        limite = int(c.get("max_idade_dias", 5))
        ok = []
        for e in entradas:
            idade = (now.date() - datetime.fromisoformat(e["data_referencia"]).date()).days
            if idade > limite:
                notas.append(f"USDA AMS {e['label']}: descartado, relatorio de "
                             f"{e['data_referencia']} com {idade} dias (limite {limite})")
            else:
                ok.append(e)
        if ok:
            notas.append(f"USDA AMS ok ({len(ok)} graos, relatorio {ok[0]['data_referencia']})")
        return ok
    except Exception as ex:
        notas.append(f"USDA AMS falha: {type(ex).__name__}: {str(ex)[:90]}")
        return []


def fetch_cotacoes(cfg, now):
    """Monta o painel inteiro de cotacoes.

    Camadas, cada uma com sua propria regra de idade:
      diarias  -> USDA AMS (graos CBOT), Trading Economics (cobre, minerio),
                  LBMA (ouro), BCB PTAX (cambio), CEPEA (fisico em R$/saca)
      mensais  -> IndexMundi / World Bank e Comex Stat (fertilizantes)

    Registro mensal carrega a chave "referencia_mensal". `salvar` usa isso para
    separar as duas tabelas no JSON: serie mensal com atraso de meses nao pode
    aparecer na mesma tabela de cotacao diaria.
    """
    painel, notas = [], []
    painel += fetch_usda_ams(cfg, now, notas)
    painel += fetch_trading_economics(cfg, now, notas)

    # ouro (LBMA) + PTAX por intervalo + gravacao da serie historica
    if rodar_ouro_serie is None:
        notas.append("cotacoes_ouro_serie.py ausente: ouro e PTAX nao coletados. "
                     "Caindo para o fetch_bcb_ptax antigo.")
        painel += fetch_bcb_ptax(cfg, now, notas)
    else:
        p2, n2 = rodar_ouro_serie(cfg, now, caminho_serie=str(DOCS / "serie-cotacoes.json"))
        painel += p2
        notas.extend(n2)

    # A prata do Trading Economics é mantida como fallback de disponibilidade,
    # mas, quando o benchmark oficial da LBMA foi coletado, a referência CFD não
    # aparece como um segundo cartão para a mesma commodity.
    if any(c.get("key") == "prata" for c in painel):
        painel = [c for c in painel if c.get("key") != "te_prata"]
        notas.append("Prata: LBMA disponível; referência CFD do Trading Economics omitida do painel.")

    # fertilizantes (mensal)
    if fetch_fertilizantes is None:
        notas.append("cotacoes_fertilizantes.py ausente: MAP, ureia e KCl nao coletados.")
    else:
        p3, n3 = fetch_fertilizantes(cfg, now)
        painel += p3
        notas.extend(n3)

    # CEPEA em R$/saca e R$/@, por via indireta (Canal Rural). Motivo: o CEPEA
    # bloqueia IP de datacenter, entao nem requests nem Chromium headless passam.
    if fetch_cepea_canalrural is None:
        notas.append("cepea_canalrural.py ausente: nenhum indicador em R$/saca coletado.")
    else:
        try:
            painel += fetch_cepea_canalrural(cfg, now, notas)
        except Exception as ex:
            notas.append(f"CEPEA/Canal Rural: erro {type(ex).__name__}: {str(ex)[:110]}")

    # Tentativa direta no CEPEA. Fica desligada por padrao: em 05/08/2026 as tres
    # estrategias falharam (403 na URL direta dentro do proprio Chromium, 403 no
    # script via servidor local, timeout na pagina publica). Mantida para o caso
    # de o bloqueio mudar; o widget na pagina continua funcionando no navegador
    # do leitor, que nao passa por aqui.
    c = (cfg.get("cepea") or {})
    if not c.get("enabled", False):
        notas.append("CEPEA desativado no sources.yml: soja, milho, trigo e boi aparecem "
                     "ao vivo na pagina pelo widget oficial, mas nao ficam gravados.")
    else:
        try:
            from cepea_render import render_cepea
        except ImportError:
            render_cepea = None
            notas.append("CEPEA: playwright/cepea_render.py ausentes, coleta nao tentada.")
        if render_cepea is not None:
            try:
                html, erro = render_cepea(url=c.get("widget_url"))
                if erro:
                    notas.append(f"CEPEA (navegador) falha: {erro}")
                else:
                    achados = parse_cepea_widget(html, notas)
                    if achados:
                        painel.extend(achados)
                        notas.append(f"CEPEA ok via navegador ({len(achados)} indicadores)")
                    else:
                        notas.append("CEPEA renderizou mas o parser extraiu 0 linhas: "
                                     "conferir se o widget mudou de layout.")
            except Exception as ex:
                notas.append(f"CEPEA (navegador) erro: {type(ex).__name__}: {str(ex)[:110]}")
    return painel, notas


# ----------------------------------------------------------------- saida
def _serializar(it):
    return {
        "titulo": it["title"],
        "trecho": it["snippet"][:300],
        "link": it["link"],
        "fonte": it["source"],
        "quando": it["when"].strftime("%Y-%m-%dT%H:%M:%S") if it.get("when") else None,
        "camada": it["layer"],
        "fonte_confiavel": it["trusted"],
        "motivo": it.get("motivo"),
        # A identidade do item, publicada. Sem isso, consumidor externo precisa
        # recalcular o hash por fora, e se a normalizacao mudar aqui os dois lados
        # divergem em silencio e o item ja publicado volta a ser publicado.
        "id": title_hash(it.get("title") or it.get("titulo") or ""),
        "micro_sugerida": it.get("micro_sugerida"),
        "termo_setorial": it.get("termo_setorial"),
        "pontos_setor": it.get("pontos_setor"),
        "evidencia_setor": it.get("evidencia_setor"),
        "negativas": it.get("negativas"),
        "vigiados": it.get("vigiados"),
        "classe": it.get("classe"),
        "evidencia_transacao": it.get("evidencia_transacao"),
        "tipo_evento": it.get("tipo_evento"),
        "prioridade_revisao": bool(it.get("prioridade_revisao")),
        "novo": it.get("novo", True),
        "visto_em": it.get("visto_em"),
        "parecido_com": it.get("parecido_com"),
    }


def _mesclar(novos, antigos):
    """Acumula: rodar de novo no mesmo dia soma, nao substitui. Chave: hash do titulo."""
    vistos, saida = set(), []
    for lista in (antigos or [], novos):
        for x in lista:
            # item gravado antes da porta setorial existir nao tem "motivo": descartar,
            # senao ruido de execucao antiga sobrevive para sempre no arquivo do dia
            if x.get("motivo") is None:
                continue
            h = title_hash(x.get("titulo", ""))
            if h in vistos:
                continue
            vistos.add(h)
            saida.append(x)
    saida.sort(key=lambda x: (x.get("quando") or ""), reverse=True)
    return saida


def salvar(items, fora_escopo, cobertura, painel, notas_cotacao, dedupe_info, now, janela):
    DOCS.mkdir(parents=True, exist_ok=True)
    ok = [c for c in cobertura if c["status"] == "ok"]
    caminho = DOCS / f"coletas-{now:%Y-%m-%d}.json"
    anterior, nota_reset = {}, None
    if caminho.exists():
        try:
            lido = json.loads(caminho.read_text(encoding="utf-8"))
            if int(lido.get("triagem_versao", 0)) == TRIAGEM_VERSAO:
                anterior = lido
            else:
                nota_reset = (f"acumulado do dia descartado: gravado sob regra de triagem "
                              f"v{lido.get('triagem_versao', 0)}, agora v{TRIAGEM_VERSAO}")
        except Exception:
            anterior = {}
    itens_final = _mesclar([_serializar(i) for i in items], anterior.get("itens"))
    fora_final = _mesclar([_serializar(i) for i in fora_escopo], anterior.get("itens_fora_escopo"))
    revisao_final = [i for i in fora_final if i.get("prioridade_revisao")]
    execucoes = int(anterior.get("execucoes_no_dia", 0)) + 1
    doc = {
        "schema": "radar-agro-ingesta/2",
        "data": now.strftime("%Y-%m-%d"),
        "gerado_em": now.isoformat(),
        "janela": {"de": janela[0].isoformat(), "ate": janela[1].isoformat()},
        "resumo": {
            "fontes_testadas": len(cobertura),
            "fontes_ok": len(ok),
            "itens_brutos": sum(c.get("itens_brutos", 0) for c in cobertura),
            "itens_na_janela": sum(c.get("na_janela", 0) for c in cobertura),
            "novos_nesta_execucao": sum(1 for i in items if i.get("novo")),
            "no_escopo_nesta_execucao": len(items),
            "novos_fora_escopo_nesta_execucao": len(fora_escopo),
            "no_escopo_no_dia": len(itens_final),
            "fora_escopo_no_dia": len(fora_final),
            **dedupe_info,
            "cotacoes_coletadas": len(painel),
        },
        "execucoes_no_dia": execucoes,
        "triagem_versao": TRIAGEM_VERSAO,
        # Duas tabelas separadas de proposito. Cotacao diaria e serie mensal com
        # atraso de meses nao podem dividir a mesma tabela no front.
        "cotacoes": [c for c in painel if not c.get("referencia_mensal")],
        "cotacoes_mensais": [c for c in painel if c.get("referencia_mensal")],
        "notas_cotacoes": notas_cotacao,
        "notas_triagem": ([nota_reset] if nota_reset else []),
        "cobertura": cobertura,
        "itens": itens_final,
        "itens_fora_escopo": fora_final,
        "itens_revisao": revisao_final,
    }
    gravar_visoes(doc, caminho)

    # Arquivo permanente e pesquisavel. Acumula TODO item ja coletado, dentro e
    # fora do escopo, por mes. Item que reaparece nao duplica: mantem a data em
    # que foi visto pela primeira vez e conta +1 em "vezes". Roda depois do
    # snapshot do dia de proposito: se o arquivo falhar, a coleta do dia esta salva.
    # Pink Sheet do World Bank: estende docs/historico.json com os meses novos.
    # Roda depois do snapshot do dia, como o arquivo: se falhar, a coleta esta salva.
    # ---------- A2: manifesto leve ----------
    # Para saber se a coleta e de hoje, o consumidor baixava 380 KB de painel.json.
    # Este arquivo tem ~1 KB e responde "esta fresco?" barato, o que permite tarefa
    # agendada abortar cedo em vez de montar relatorio sobre dado velho.
    # S2: o retry "[sem acento]" e fallback da MESMA consulta, nao fonte nova.
    # Contando em separado, ele inflava numerador e denominador ao mesmo tempo
    # (11 falhas de 77 fontes, quando as fontes distintas com problema eram 7).
    # Aqui as variantes sao consolidadas: uma entrada por fonte, status = melhor
    # resultado entre as tentativas.
    def _base(nome):
        return str(nome).replace(" [sem acento]", "").strip()

    consolidado = {}
    for c in cobertura:
        k = (c["camada"], _base(c["fonte"]))
        alvo = consolidado.setdefault(k, {"camada": c["camada"], "fonte": _base(c["fonte"]),
                                          "status": "falha", "erro": None, "tentativas": [],
                                          "itens_brutos": 0, "na_janela": 0})
        alvo["tentativas"].append("sem acento" if "[sem acento]" in str(c["fonte"]) else "direta")
        alvo["itens_brutos"] += c.get("itens_brutos", 0)
        alvo["na_janela"] += c.get("na_janela", 0)
        if c["status"] == "ok":
            alvo["status"] = "ok"
        elif alvo["erro"] is None:
            alvo["erro"] = str(c.get("erro"))[:120]
    cob_unica = list(consolidado.values())
    falhas = [c for c in cob_unica if c["status"] != "ok"]
    # Saude por fonte. NAO existe remocao automatica de URL: fonte silenciosa nao e
    # fonte morta. Das 32 fontes com 0 item na janela em 05/08/2026, 30 responderam
    # perfeitamente e so nao tinham materia nova, entre elas Brasil Mineral, Portal
    # DBO e fusoesaquisicoes.com, que sao veiculos especializados de baixo volume e
    # justamente os que mais importam. O que merece atencao e OUTRA coisa: acesso
    # falhando. Estes dois grupos ficam separados de proposito.
    sem_acesso = [{"fonte": c["fonte"], "camada": c["camada"], "erro": str(c.get("erro"))[:120],
                   "tentativas": c.get("tentativas")} for c in falhas]
    silenciosas = [{"fonte": c["fonte"], "camada": c["camada"],
                    "itens_brutos": c.get("itens_brutos", 0)}
                   for c in cob_unica if c["status"] == "ok" and not c.get("na_janela")]
    # S7: silencio medido em JANELA ROLANTE, nao no dia.
    # "quem nao publicou hoje" nao detecta degradacao: veiculo especializado publica
    # algumas vezes por semana. "quem nao publica ha N execucoes" separa o ritmo
    # normal do feed que mudou de URL e ninguem notou. Ledger proprio, pequeno.
    # Continua NAO havendo remocao automatica de URL.
    ledger_p = DOCS / "saude-fontes.json"
    ledger = {"schema": "radar-agro-saude/1", "execucoes": [], "fontes": {}}
    if ledger_p.exists():
        try:
            lido = json.loads(ledger_p.read_text(encoding="utf-8"))
            if isinstance(lido.get("fontes"), dict):
                ledger = lido
        except Exception:
            pass
    marca = doc["gerado_em"]
    ledger["execucoes"] = ([e for e in ledger.get("execucoes", []) if e != marca] + [marca])[-40:]
    total_exec = len(ledger["execucoes"])
    for c in cob_unica:
        f = ledger["fontes"].setdefault(c["fonte"], {"camada": c["camada"],
                                                     "execucoes_vistas": 0,
                                                     "ultima_com_item": None,
                                                     "ultimo_acesso_ok": None})
        f["camada"] = c["camada"]
        f["execucoes_vistas"] = int(f.get("execucoes_vistas", 0)) + 1
        if c["status"] == "ok":
            f["ultimo_acesso_ok"] = marca
        if c.get("na_janela"):
            f["ultima_com_item"] = marca
    def _execs_sem_item(f):
        u = f.get("ultima_com_item")
        if u is None:
            return min(int(f.get("execucoes_vistas", 0)), total_exec)
        return max(0, total_exec - 1 - ledger["execucoes"].index(u)) if u in ledger["execucoes"] else total_exec
    for f in ledger["fontes"].values():
        f["execucoes_sem_item"] = _execs_sem_item(f)
    ledger["atualizado_em"] = marca
    ledger_p.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")

    LIMITE_SILENCIO = int(os.environ.get("LIMITE_SILENCIO", 5))
    silenciosas_janela = sorted(
        ({"fonte": k, "camada": v.get("camada"),
          "execucoes_sem_item": v.get("execucoes_sem_item"),
          "ultima_com_item": v.get("ultima_com_item"),
          "acesso_ok_na_ultima": v.get("ultimo_acesso_ok") == marca}
         for k, v in ledger["fontes"].items()
         if v.get("execucoes_sem_item", 0) >= LIMITE_SILENCIO),
        key=lambda x: -(x["execucoes_sem_item"] or 0))

    status = {
        "schema": "radar-agro-status/2",
        "data": doc["data"],
        "gerado_em": doc["gerado_em"],
        "execucoes_no_dia": doc.get("execucoes_no_dia"),
        "resumo": doc.get("resumo"),
        "n_falhas": len(falhas),
        "n_fontes": len(cob_unica),
        "n_entradas_cobertura": len(cobertura),
        "cotacoes": len(doc.get("cotacoes") or []),
        "cotacoes_mensais": len(doc.get("cotacoes_mensais") or []),
        "itens": len(doc.get("itens") or []),
        "itens_revisao": len(doc.get("itens_revisao") or []),
        "itens_fora_escopo": len(doc.get("itens_fora_escopo") or []),
        "saude_fontes": {
            "sem_acesso": sem_acesso,
            "silenciosas_hoje": silenciosas,
            f"silenciosas_{LIMITE_SILENCIO}_execucoes": silenciosas_janela,
            "execucoes_no_ledger": total_exec,
            "nota": ("Fonte silenciosa NAO e fonte morta: veiculo especializado publica "
                     "algumas vezes por semana. Nenhuma URL e removida automaticamente. "
                     "Só 'sem_acesso' pede conferencia de URL."),
        },
    }
    (DOCS / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1),
                                      encoding="utf-8")

    if rodar_pinksheet is None:
        doc["notas_cotacoes"].append("historico_pinksheet.py ausente: historico nao "
                                     "cresce sozinho nesta execucao.")
    else:
        try:
            notas_ps = rodar_pinksheet(str(DOCS / "historico.json"))
            doc["notas_historico"] = notas_ps
            doc["notas_cotacoes"].extend(notas_ps)
        except Exception as ex:
            doc["notas_cotacoes"].append(f"Pink Sheet: falha {type(ex).__name__}: {str(ex)[:120]}")

    # Painel mensal a partir do historico ja atualizado pelo Pink Sheet. Roda DEPOIS
    # dele de proposito: assim o painel cita o mes que acabou de entrar, e nao a
    # copia atrasada do IndexMundi. Isso elimina a contradicao de mostrar dois
    # valores para o mesmo indicador na mesma pagina.
    if rodar_painel_hist is not None:
        try:
            notas_ph = []
            novas = rodar_painel_hist(DOCS, now.date(), notas_ph)
            if novas:
                chaves = {c["key"] for c in novas}
                antigas = [c for c in doc.get("cotacoes_mensais", [])
                           if c.get("key") not in chaves]
                doc["cotacoes_mensais"] = novas + antigas
            doc["notas_cotacoes"].extend(notas_ph)
            caminho.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            (DOCS / "ultima.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
        except Exception as ex:
            doc["notas_cotacoes"].append(f"painel do historico: falha {type(ex).__name__}: "
                                         f"{str(ex)[:110]}")

    if atualizar_arquivo is None:
        doc["notas_cotacoes"].append("arquivo.py ausente: nada foi arquivado nesta execucao.")
    else:
        try:
            notas_arq = []
            atualizar_arquivo(DOCS, doc.get("itens") or [],
                              doc.get("itens_fora_escopo") or [],
                              f"{now:%Y-%m-%d}", notas_arq)
            doc["notas_arquivo"] = notas_arq
            doc["notas_cotacoes"].extend(notas_arq)
            gravar_visoes(doc, caminho)
        except Exception as ex:
            doc["notas_cotacoes"].append(f"arquivo: falha {type(ex).__name__}: {str(ex)[:120]}")
    return caminho, doc


def gravar_visoes(doc, caminho_snapshot=None):
    """Grava a visão completa e as duas cargas leves usadas pelo site.

    ``ultima.json`` continua contendo tudo para auditoria e compatibilidade. A
    página principal carrega ``painel.json``, sem centenas de itens fora do
    escopo. Esses itens ficam em ``fora-escopo.json`` e em uma página própria.
    Nenhum registro é descartado: a separação é somente de carregamento e layout.
    """
    # Compatibilidade com snapshots anteriores à taxonomia de eventos. A
    # migração é determinística e não altera título, link, fonte ou escopo.
    for chave_lista in ("itens", "itens_fora_escopo"):
        for item in doc.get(chave_lista) or []:
            classe = item.get("classe") or "noticia"
            item.setdefault(
                "tipo_evento",
                classificar_tipo_evento(norm(f"{item.get('titulo','')} {item.get('trecho','')}"), classe),
            )
            item["prioridade_revisao"] = bool(
                classe != "noticia" or item.get("pontos_setor", 0) > 0
            )
    doc["itens_revisao"] = [
        i for i in (doc.get("itens_fora_escopo") or []) if i.get("prioridade_revisao")
    ]

    texto_completo = json.dumps(doc, ensure_ascii=False, indent=1)
    if caminho_snapshot is not None:
        Path(caminho_snapshot).write_text(texto_completo, encoding="utf-8")
    (DOCS / "ultima.json").write_text(texto_completo, encoding="utf-8")

    painel = {k: v for k, v in doc.items() if k != "itens_fora_escopo"}
    painel["fora_escopo_disponivel_em"] = "fora-escopo.json"
    (DOCS / "painel.json").write_text(
        json.dumps(painel, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    fora = {
        "schema": "radar-agro-fora-escopo/1",
        "data": doc.get("data"),
        "gerado_em": doc.get("gerado_em"),
        "triagem_versao": doc.get("triagem_versao"),
        "total": len(doc.get("itens_fora_escopo") or []),
        "itens_revisao": doc.get("itens_revisao") or [],
        "itens": doc.get("itens_fora_escopo") or [],
    }
    (DOCS / "fora-escopo.json").write_text(
        json.dumps(fora, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def imprimir_relatorio(doc):
    r = doc["resumo"]
    print("\n=== COBERTURA POR FONTE ===")
    for c in doc["cobertura"]:
        if c["status"] == "ok":
            extra = f"  <- {c['nota']}" if c.get("nota") else ""
            print(f"  OK    {c['fonte'][:34]:34} brutos={c.get('itens_brutos',0):3} "
                  f"janela={c.get('na_janela',0):3}{extra}")
        else:
            print(f"  FALHA {c['fonte'][:34]:34} {c.get('erro','?')[:80]}")
    print("\n=== COTACOES ===")
    for x in doc["cotacoes"]:
        print(f"  {x['label'][:38]:38} {x['preco_texto']:>12} {x['unidade']:<12} ref {x['data_referencia']}")
    if doc.get("cotacoes_mensais"):
        print("  -- mensais (fertilizantes) --")
        for x in doc["cotacoes_mensais"]:
            marca = " [proxy]" if x.get("e_proxy") else ""
            print(f"  {x['label'][:38]:38} {x['preco_texto']:>12} {x['unidade']:<12} "
                  f"ref {x.get('referencia_mensal')} ({x.get('atraso_dias')} d){marca}")
    for n in doc["notas_cotacoes"]:
        print(f"  nota: {n}")
    for n in doc.get("notas_triagem") or []:
        print(f"  triagem: {n}")
    print("\n=== RESUMO ===")
    print(f"  fontes ok: {r['fontes_ok']}/{r['fontes_testadas']}")
    print(f"  itens brutos: {r['itens_brutos']} | na janela: {r['itens_na_janela']}")
    print(f"  novos: {r['novos']} | repetidos de edicoes anteriores: {r['repetidos_de_edicoes_anteriores']}"
          f" | agrupados por titulo parecido: {r['agrupados_por_titulo_parecido']}")
    print(f"  DESCARTADOS POR REPETICAO: {r['descartados']} (politica: nunca descartar)")
    print(f"  nesta execucao: {r['no_escopo_nesta_execucao']} no escopo + {r['novos_fora_escopo_nesta_execucao']} fora")
    print(f"  acumulado do dia ({doc['execucoes_no_dia']} execucao(oes)): "
          f"{r['no_escopo_no_dia']} no escopo | {r['fora_escopo_no_dia']} fora do escopo")
    print(f"  cotacoes: {r['cotacoes_coletadas']}")
    if r["no_escopo_no_dia"] == 0:
        print("\n  ATENCAO: zero itens publicados. Olhe a lista de FALHA acima e as notas "
              "de janela. Nenhum item foi inventado para preencher o vazio.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=None,
                    help="tamanho da janela em dias (padrao: 1, ou 3 na segunda)")
    ap.add_argument("--sem-google-news", action="store_true")
    ap.add_argument("--zerar-ledger", action="store_true",
                    help="ignora o data/seen.json existente nesta execucao")
    args = ap.parse_args()

    if feedparser is None:
        sys.exit("feedparser ausente: pip install -r requirements.txt")

    cfg = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8"))
    now = now_brt()
    janela = research_window(now, args.dias)
    print(f"janela: {janela[0]:%d/%m %H:%M} -> {janela[1]:%d/%m %H:%M} (BRT)")

    items, cobertura = collect(cfg, janela[0], usar_google_news=not args.sem_google_news)
    items, seen, dedupe_info = marcar_repetidos(items, {} if args.zerar_ledger else load_seen(), now)
    items, fora_escopo = triar(items, cfg)
    painel, notas = fetch_cotacoes(cfg, now)

    p, doc = salvar(items, fora_escopo, cobertura, painel, notas, dedupe_info, now, janela)
    DATA.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    imprimir_relatorio(doc)
    print(f"\nescrito: {p.name} e ultima.json")


if __name__ == "__main__":
    main()
