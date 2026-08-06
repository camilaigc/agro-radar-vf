# -*- coding: utf-8 -*-
"""
cepea_render.py — obtem a tabela de indicadores do CEPEA usando um navegador.

Historico do problema, para nao repetir tentativa que ja falhou:
  1. requests direto no widget  -> HTTP 403 (WAF do CEPEA).
  2. Chromium com set_content() numa pagina montada na memoria -> TimeoutError
     esperando <table> (execucao de 05/08/2026, 11:52). Motivo provavel: com
     set_content a pagina fica na origem about:blank, sem Referer, e o widget
     e recusado pelo mesmo WAF do caso 1. O navegador nao resolve nada se a
     requisicao continua chegando sem cabecalho de origem.
  3. Estrategia atual: TRES tentativas, em ordem, e a primeira que trouxer texto
     aproveitavel vence.
       A) navegar direto na URL do widget. O navegador manda cabecalhos completos
          e o conteudo vem como texto JavaScript com document.write(...) dentro.
          O parse_cepea_widget do build.py JA sabe ler document.write, entao esse
          texto serve como esta.
       B) servir uma pagina local em 127.0.0.1 que carrega o widget por <script>.
          Assim existe origem http de verdade e Referer.
       C) navegar na pagina publica de indicadores do CEPEA e ler a tabela dela.

Nenhuma tentativa inventa valor: se as tres falharem, devolve erro com o motivo
de cada uma, e o painel fica sem CEPEA.
"""
import re
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Padrao. A URL de verdade vem do sources.yml (cepea.widget_url), para trocar a
# lista de id_indicador sem mexer em codigo Python.
WIDGET = ("https://cepea.org.br/br/widgetproduto.js.php?fonte=arial&tamanho=10&largura=400px&corfundo=dbd6b2&cortexto=333333&corlinha=ede7bf&id_indicador%5B%5D=2&id_indicador%5B%5D=77&id_indicador%5B%5D=12&id_indicador%5B%5D=178&id_indicador%5B%5D=179&id_indicador%5B%5D=92")

def _pagina_local(url):
    return ("<html><head><meta charset='utf-8'></head><body>"
            f"<script src='{url}'></script></body></html>")

REFERER = "https://www.cepea.esalq.usp.br/br/"
CABECALHOS = {"Referer": REFERER,
              "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"}

# Aproveitavel = tem data dd/mm/aaaa E numero no formato brasileiro. E o mesmo
# par que o parse_cepea_widget exige por linha; sem os dois nao ha o que extrair.
TEM_DATA = re.compile(r"\d{2}/\d{2}/\d{4}")
TEM_VALOR = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")


def _aproveitavel(txt):
    return bool(txt) and bool(TEM_DATA.search(txt)) and bool(TEM_VALOR.search(txt))


def _servidor_local(html, porta=0):
    """Sobe um HTTP server de uma pagina so, em thread. Devolve (url, desligar)."""
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            corpo = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", porta), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_port}/", srv.shutdown


def render_cepea(timeout_ms=30000, url=None):
    """Devolve (html, erro). Nunca levanta excecao para fora.

    url=None usa o WIDGET padrao deste arquivo. Passe cepea.widget_url do
    sources.yml para trocar a lista de indicadores sem editar codigo.
    """
    widget = url or WIDGET
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", "playwright nao instalado (pip install playwright)"

    tentativas = []
    try:
        with sync_playwright() as pw:
            nav = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                ctx = nav.new_context(locale="pt-BR", extra_http_headers=CABECALHOS)
                pg = ctx.new_page()

                # A) direto na URL do widget
                try:
                    resp = pg.goto(widget, wait_until="domcontentloaded", timeout=timeout_ms)
                    status = resp.status if resp else "?"
                    txt = pg.evaluate("document.body ? document.body.innerText : ''")
                    if _aproveitavel(txt):
                        return txt, None
                    tentativas.append(f"A (URL direta): HTTP {status}, "
                                      f"{len(txt or '')} chars sem data+valor")
                except Exception as ex:
                    tentativas.append(f"A (URL direta): {type(ex).__name__}: {str(ex)[:80]}")

                # B) pagina servida de 127.0.0.1, com origem e Referer de verdade
                desligar = None
                try:
                    url_local, desligar = _servidor_local(_pagina_local(widget))
                    pg.goto(url_local, wait_until="networkidle", timeout=timeout_ms)
                    pg.wait_for_timeout(3000)
                    html = pg.content()
                    if _aproveitavel(pg.evaluate("document.body.innerText")):
                        return html, None
                    tentativas.append("B (servidor local): renderizou sem data+valor")
                except Exception as ex:
                    tentativas.append(f"B (servidor local): {type(ex).__name__}: {str(ex)[:80]}")
                finally:
                    if desligar:
                        desligar()

                # C) pagina publica de indicadores
                try:
                    pg.goto("https://www.cepea.esalq.usp.br/br/indicador/soja.aspx",
                            wait_until="networkidle", timeout=timeout_ms)
                    pg.wait_for_timeout(2000)
                    html = pg.content()
                    if _aproveitavel(pg.evaluate("document.body.innerText")):
                        return html, None
                    tentativas.append("C (pagina publica): renderizou sem data+valor")
                except Exception as ex:
                    tentativas.append(f"C (pagina publica): {type(ex).__name__}: {str(ex)[:80]}")
            finally:
                nav.close()
    except Exception as ex:
        tentativas.append(f"navegador: {type(ex).__name__}: {str(ex)[:100]}")

    return "", " | ".join(tentativas)


if __name__ == "__main__":
    html, erro = render_cepea()
    if erro:
        print("FALHA em todas as tentativas:")
        for t in erro.split(" | "):
            print("  -", t)
    else:
        print(f"ok, {len(html)} chars")
        print(html[:600])
        try:
            from build import parse_cepea_widget
            for a in parse_cepea_widget(html):
                print(f"  {a['label']:44} {a['preco_texto']:>12} {a['unidade']:<12} "
                      f"ref {a['data_referencia']}")
        except ImportError:
            pass
