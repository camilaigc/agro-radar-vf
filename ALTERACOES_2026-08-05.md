# Alterações de 05/08/2026

## Cobertura de notícias

- Nenhuma notícia é apagada pela triagem, pela deduplicação ou pelos filtros da interface.
- Itens fora do recorte foram movidos para `docs/fora-escopo.html`, com busca, filtros e carregamento progressivo.
- Possíveis perdas de classificação ficam na fila de revisão da página principal.
- Eventos corporativos agora são separados em M&A, captação, mercado de capitais, situações especiais e possíveis operações.
- Notícias semelhantes podem ser apresentadas como um único evento com fontes adicionais recolhidas.

## Commodities

- “Físico Brasil — CEPEA” foi incorporado ao painel de commodities.
- O botão “Todos” foi corrigido.
- Prata e platina foram corrigidas para US$/oz troy no coletor do Trading Economics.
- A prata oficial da LBMA tem prioridade; o Trading Economics funciona apenas como alternativa.
- O vínculo do cobre com o histórico foi corrigido.
- Proxy de importação, unidade incompatível ou cotação mais antiga que o histórico não gera comparação automática.
- `validate_data.py` verifica preço, texto exibido, unidade, data e coerência histórica antes da publicação.

## Interface

- Notas das cotações e do arquivo ficam recolhidas por padrão.
- Foram adicionados destaques do dia, navegação fixa, busca e filtros de notícias.
- As categorias carregam os cards somente quando abertas.
- Ortografia, capitalização e rótulos foram revisados.

## Publicação

Substitua os arquivos do repositório pelos arquivos desta pasta, preserve a configuração do GitHub Pages apontando para `docs/` e faça o commit. Na execução seguinte, o workflow recriará as cargas leves `docs/painel.json` e `docs/fora-escopo.json`.
