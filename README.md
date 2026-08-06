# Radar Agro & Natural Resources

Robô no GitHub Actions que coleta notícias e transações de M&A do agro em fontes
abertas, mais as cotações do CEPEA, e publica no GitHub Pages. Sem IA nesta etapa:
a coleta é bruta e o filtro editorial fica no clipping curado.

## Princípio

Nada é preenchido por estimativa. Fonte que falha aparece como falha, com o erro
exato. Cotação sem coleta validada não aparece. Se a execução der zero item, o
relatório diz por quê.

## Arquivos

- `probe.py` — diagnóstico. Testa várias URLs candidatas por veículo e imprime
  quais estão vivas, mais um bloco `feeds:` pronto para colar no `sources.yml`.
- `build.py` — coleta diária. Grava `docs/coletas-AAAA-MM-DD.json` e `docs/ultima.json`,
  ambos com a seção `cobertura` (status por fonte).
- `sources.yml` — fontes. Corrigir com o resultado do `probe.py`.
- `docs/index.html` — painel principal, com destaques, eventos, commodities e notícias no escopo.
- `docs/fora-escopo.html` — conferência integral dos itens fora do recorte, com busca e filtros.
- `docs/painel.json` — carga leve da página principal, sem a lista completa fora do escopo.
- `docs/fora-escopo.json` — carga da página de conferência. Nada é descartado.
- `validate_data.py` — valida preço, unidade, data e coerência com as séries antes da publicação.

## Ordem de uso

1. Actions → **testar-fontes** → Run workflow. Leia o log.
2. Substitua o bloco `feeds:` do `sources.yml` pelo que o log imprimiu.
3. Actions → **radar-diario** → Run workflow.
4. Abra `https://SEU-USUARIO.github.io/NOME-DO-REPO/`.

Depois disso o radar roda sozinho às 09h15 BRT, de segunda a sexta.

## O que é atualizado automaticamente

- Notícias, cobertura das fontes e cotações atuais: sim, a cada execução agendada.
- Arquivo diário de notícias: sim, sem apagar itens anteriores.
- `docs/serie-cotacoes.json` de LBMA e PTAX: sim, acumula novos pontos automaticamente.
- `docs/historico.json` usado nos gráficos mensais: ainda não. Essa base permanece estática
  até existir um coletor mensal validado para cada benchmark. O site não mistura uma cotação
  diária de fonte diferente no histórico apenas para preencher a série.

O GitHub Actions executa `validate_data.py` depois da coleta. Um erro estrutural de unidade,
preço ou data bloqueia a publicação; diferenças legítimas de benchmark ou frequência são
registradas como aviso e não recebem uma linha de comparação enganosa no gráfico.

## Fora do escopo por licença

Mergermarket, TTR Data, DealReporter, Debtwire e Valor atrás de paywall são dados
licenciados e não entram num site público. Seguem no clipping curado.
