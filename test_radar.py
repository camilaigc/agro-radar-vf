# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

import build


class RadarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = yaml.safe_load((Path(__file__).parent / "sources.yml").read_text(encoding="utf-8"))

    def test_te_metal_pre_work_usa_onca_troy(self):
        html = """
        <div>Actual 1749.20 USD/T.oz was last updated on August 5 of 2026</div>
        <script>{"last":1749.20}</script>
        """
        reg, motivo = build.parse_trading_economics(html, "Platina")
        self.assertIsNotNone(reg, motivo)
        self.assertEqual(reg["unidade"], "US$/oz t")
        self.assertEqual(reg["preco"], 1749.20)

    def test_te_metal_base_nao_captura_palavra_seguinte_na_unidade(self):
        html = """
        <div>Actual 3249.55 USD/T was last updated on August 5 of 2026</div>
        <script>{"last":3249.55}</script>
        """
        reg, motivo = build.parse_trading_economics(html, "Alumínio")
        self.assertIsNotNone(reg, motivo)
        self.assertEqual(reg["unidade"], "US$/t")

    def test_triagem_nunca_descarta_item(self):
        agora = datetime.now(build.TZ)
        itens = [
            {"title":"Empresa adquire a concorrente", "snippet":"Operação anunciada",
             "link":"https://example.com/1", "source":"Fonte geral", "when":agora,
             "layer":"query", "trusted":False},
            {"title":"Produção de soja cresce", "snippet":"Safra e produtor rural",
             "link":"https://example.com/2", "source":"AgFeed", "when":agora,
             "layer":"feed", "trusted":True},
            {"title":"Notícia macroeconômica", "snippet":"Assunto geral",
             "link":"https://example.com/3", "source":"Fonte geral", "when":agora,
             "layer":"feed", "trusted":False},
        ]
        dentro, fora = build.triar(itens, self.cfg)
        self.assertEqual(len(dentro) + len(fora), len(itens))
        operacao = next(i for i in dentro + fora if "adquire" in i["title"])
        self.assertTrue(operacao["prioridade_revisao"])
        self.assertEqual(operacao["tipo_evento"], "ma")

    def test_situacao_especial_nao_vira_ma(self):
        classe, _ = build.classificar_transacao("grupo pede recuperacao judicial", self.cfg["transacao"])
        self.assertEqual(classe, "transacao")
        self.assertEqual(
            build.classificar_tipo_evento("grupo pede recuperacao judicial", classe),
            "situacao_especial",
        )

    def test_visoes_separam_fora_sem_apagar(self):
        doc = {
            "schema":"teste", "data":"2026-08-05", "gerado_em":"2026-08-05T12:00:00-03:00",
            "resumo":{"fora_escopo_no_dia":2}, "itens":[],
            "itens_fora_escopo":[
                {"titulo":"A", "prioridade_revisao":True},
                {"titulo":"B", "prioridade_revisao":False},
            ],
            "itens_revisao":[{"titulo":"A", "prioridade_revisao":True}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)
            with patch.object(build, "DOCS", pasta):
                build.gravar_visoes(doc)
            painel = json.loads((pasta / "painel.json").read_text(encoding="utf-8"))
            fora = json.loads((pasta / "fora-escopo.json").read_text(encoding="utf-8"))
            completo = json.loads((pasta / "ultima.json").read_text(encoding="utf-8"))
            self.assertNotIn("itens_fora_escopo", painel)
            self.assertEqual(fora["total"], 2)
            self.assertEqual(len(completo["itens_fora_escopo"]), 2)


if __name__ == "__main__":
    unittest.main()
