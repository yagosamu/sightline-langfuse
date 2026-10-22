# sightline-langfuse

[English](README.md) · **Português**

Observabilidade e avaliação para um agente de IA que responde perguntas a partir de um grafo de
conhecimento temporal. Os traços do agente, os prompts, o custo, os experimentos contra o ground truth,
os juízes online, a revisão humana e os alertas rodam num Langfuse v4 self-hosted, com OpenTelemetry.

A **Orbisa** é uma empresa de SaaS de logística cujo conhecimento está espalhado por sete sistemas. O
**Helix** transforma esses sistemas num grafo de conhecimento bitemporal (Graphiti sobre Neo4j), servido
via MCP. O **Ask Orbisa** é um agente feito com o Claude Agent SDK que responde perguntas dos funcionários
"na data de" um dia específico. Antes deste projeto, o engenheiro de plantão não conseguia saber se uma
resposta estava certa, nem por quê.

> Orbisa, Brisklane, Helix e todas as pessoas e números deste repositório são fictícios.

![Arquitetura](docs/architecture.png)

## Resultados

Todos os números foram medidos no Langfuse. O relatório completo para o CTO está em
[`decisions/03-readout.md`](decisions/03-readout.md) (em inglês).

| Critério | Meta | Medido |
|---|---|---|
| Cada resposta tem um traço completo (agente → chamadas ao modelo → ferramentas do Helix → fatos e fontes) | 100% | 21/22. O CLI perdeu um span raiz, uma limitação conhecida da versão beta. |
| Correção nas perguntas temporais, de conflito e de premissa falsa | ≥ 0.8 | 1.0 em 2 rodadas. É um teto, não uma prova: o juiz é leniente. |
| Recall de fontes | ≥ 0.8 | 0.98 |
| Custo e latência por pergunta e por etapa | visíveis | US$ 0.088 por pergunta, p95 de 36.8 s |
| O juiz LLM concorda com revisores humanos | ≥ 5/6 | 5/6 |

O achado mais útil foi um defeito no próprio Helix. Ele vazava fatos registrados depois da data
perguntada por três caminhos: `list_sources`, o `valid_until` de um fato e os resumos de entidades. Uma
correção na branch `fix/helix-as-of` eliminou todos os vazamentos reais nos experimentos. Ela não foi
mergeada, porque a regra de lançamento não conseguiu separar as quedas de recall do ruído.

## O que foi construído

| Incremento | O que faz | Código |
|---|---|---|
| Tracing | Um traço por resposta, atravessando três processos (agente, Claude Code CLI e servidor MCP do Helix) e ligado pelo `traceparent` do W3C. Cada traço leva usuário, sessão e tags, custo por modelo e mascaramento de e-mails. | `src/observability/tracing/` |
| Gestão de prompts | O system prompt e as descrições das ferramentas MCP são versionados no Langfuse, com labels e fallback local, e cada geração fica ligada à versão do prompt que a produziu. | `src/observability/prompts/` |
| Ground truth | Um dataset de 22 perguntas, avaliadores em código (recall de fontes, vazamento de datas), um juiz Claude Haiku calibrado, experimentos em Docker e uma regra de lançamento. | `datasets/`, `evaluators/`, `experiments/` |
| Monitoramento ao vivo | Um juiz online de faithfulness (Claude Sonnet), uma fila de anotação para revisão humana, um dashboard e um alerta de qualidade enviado para um webhook local assinado. | `src/observability/monitoring/` |
| Acesso agêntico | Um investigador somente leitura que lê as rodadas pelo servidor MCP do Langfuse e classifica cada falha como temporal, de recuperação ou de raciocínio. | `monitoring/investigate.py` |

## Principais trade-offs

- **Langfuse self-hosted, e não na nuvem.** Os traços contêm contratos e tickets de clientes, e nenhum
  dado de cliente pode sair da máquina. O custo é rodar Postgres, ClickHouse, Redis e MinIO localmente.
- **O OpenTelemetry do próprio CLI, em vez de instrumentar o SDK.** O Claude Code CLI já emite spans para
  cada chamada ao modelo e a ferramentas. Nós propagamos o `traceparent` para ele em vez de instrumentar
  tudo de novo. Isso ainda é beta: cada pergunta precisa de um `CLAUDE_CONFIG_DIR` novo, senão os spans
  param depois da primeira.
- **Um juiz barato offline e um forte online.** O Haiku avalia a correção comparando com as referências,
  e foi calibrado em 22/22 antes de ser usado. A faithfulness não tem referência, então roda no Sonnet,
  com os documentos que o agente leu. Com entradas longas, o Haiku devolvia justificativas vazias.
- **Uma regra de lançamento fixada antes de a candidata rodar.** Uma mudança só vai para produção se
  passar em cinco checagens:
  - o recall de fontes sobe além do ruído entre rodadas;
  - nenhum item perde correção;
  - todos os traços estão completos;
  - os vazamentos de data não pioram;
  - o custo sobe no máximo 10%.

  Mesmo assim, a regra ainda exige revisão humana. Ela rejeitou um prompt que parecia melhor, mas
  amplificava um vazamento do Helix.
- **Resumos nos traços, não os dados brutos.** As saídas das ferramentas são cortadas: os documentos
  guardam uma prévia de 3.000 caracteres. Isso mantém os traços legíveis e o armazenamento pequeno. O
  preço é que um juiz que lê só o traço pode perder o texto que fica além da prévia.
- **Corrigir a medição antes de corrigir o modelo.** Várias "falhas" eram nossas, não do agente:
  - referências que pediam fatos posteriores à data da pergunta;
  - um detector de vazamentos com falsos positivos;
  - um resumo de experimento que escondia itens que falharam.

  Cada caso está registrado em [`decisions/`](decisions/).

## Como rodar

Requisitos: Docker Desktop (cerca de 8 GB de RAM livre), [uv](https://docs.astral.sh/uv/) e uma chave da
API da Anthropic.

```bash
cp .env.example .env     # coloque ANTHROPIC_API_KEY no .env, nunca no .env.example
make up                  # gera os segredos, constrói o Helix e sobe Neo4j, Helix MCP e Langfuse v4
make ingest              # carrega o corpus da Orbisa no grafo (Claude Haiku, cerca de 8 minutos)
make ask Q=Q01           # faz uma pergunta do ground truth ao agente
make dataset             # carrega as 22 perguntas no Langfuse
make experiment ARGS="--name my-run"   # roda o dataset e pontua as respostas
```

| Serviço | URL |
|---|---|
| Langfuse v4 | http://localhost:3000. O usuário e a senha são as variáveis `LANGFUSE_INIT_USER_*` do `.env`. |
| Servidor MCP do Helix | http://localhost:8765/mcp |
| Neo4j Browser | http://localhost:7474 |
| Receptor de alertas | http://localhost:8787. Rode `make alerts` para ver o que ele recebeu. |

Outros comandos: `make ps`, `make logs`, `make rebuild` (depois de mudar o `src/`), `make test` e
`make down`.

Para rodar os testes e o agente fora do Docker:

```bash
uv sync --python 3.12
uv run pytest -q
uv run python -m clients.agent "Who approves refunds?" --as-of 2026-10-15
```

## Repositório

| Caminho | Conteúdo |
|---|---|
| [`decisions/`](decisions/) | A definição do problema, o desenho da solução e o readout, com cada decisão e seu motivo (em inglês) |
| `docs/evidence/` | Prints, resumos de experimentos e relatórios de investigação que sustentam cada afirmação |
| `data/orbisa/` | Exportações de Slack, Confluence, Notion, Drive, HubSpot, Zendesk e Gong, mais o ground truth |
| `src/context/` | Helix: ingestão, Graphiti sobre Neo4j, recuperação e o servidor MCP |
| `src/clients/agent/` | Ask Orbisa, o agente feito com o Claude Agent SDK |
| `src/observability/` | A camada de observabilidade descrita acima |
| `infra/docker-compose.yml` | A stack inteira, incluindo o Langfuse self-hosted |
