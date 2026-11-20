# openwebui-tools

Ferramentas (Tools) para o [Open WebUI](https://github.com/open-webui/open-webui), em português.

| Ferramenta | Pasta | O que faz |
|---|---|---|
| **Flex Sub Agent** | [`flex_sub_agent/`](flex_sub_agent/) | Delega tarefas a subagentes em **sequência** ou em **paralelo**, com **escolha de modelo por chamada** e allowlist configurável. |
| **Whisper Transcription** | [`whisper_transcription.py`](whisper_transcription.py) | Transcreve áudio com um endpoint Whisper compatível com a API da OpenAI. |

---

## Instalação

### Pela interface (recomendado)

1. Abra o Open WebUI.
2. Vá em **Workspace → Tools → +** (nova ferramenta).
3. Copie o conteúdo de `flex_sub_agent/flex_sub_agent.py` e cole no editor.
4. Salve. A ferramenta aparece como **Flex Sub Agent**.
5. Ative-a no chat, em **Ferramentas**, e/ou associe-a a um modelo.
6. Se quiser, ajuste as **Valves** da ferramenta antes de usar (veja abaixo).

### Por API

```bash
python3 install_tool.py     # script local, NÃO versionado (contém a URL do seu servidor)
```

O script faz `POST /api/v1/tools/create` com o conteúdo do arquivo. Para reaplicar uma
edição, use `POST /api/v1/tools/id/flex_sub_agent/update`.

---

## Dois conceitos que evitam confusão

**Valves** são as configurações do administrador (valem para todos os usuários).
**User Valves** são configurações por usuário — no Flex Sub Agent, apenas o system prompt
do subagente.

---

## Flex Sub Agent

### O que ele resolve

- **Modelo por chamada**: o agente principal pode pedir explicitamente
  `model="algum-modelo"` — e cada tarefa paralela pode usar o seu próprio modelo. Uma
  allowlist (`ALLOWED_MODEL_IDS`) restringe o que pode ser pedido.
- **Paralelismo real**: `run_parallel_sub_agents` executa tarefas independentes ao mesmo
  tempo, com emissores de status isolados e ordem de resultado preservada.
- **Sem credenciais**: a ferramenta roda dentro do processo do Open WebUI e herda as
  ferramentas que já estão autenticadas no request do usuário. Não há chamada HTTP de
  volta, nem token, nem senha na ferramenta.
- **Anti-recursão**: o subagente **nunca** recebe `run_sub_agent` /
  `run_parallel_sub_agents` nem a própria ferramenta. Isso é incondicional, não depende de
  configuração.

### Como o agente principal usa

```text
run_sub_agent(description, prompt, model=None, max_iterations=None)
run_parallel_sub_agents(tasks=[{description, prompt, model}], model=None, max_iterations=None)
```

Exemplo de pedido ao modelo principal:

> Use `run_parallel_sub_agents` com três tarefas. A primeira deve usar o modelo
> `modelo-rapido`, as outras o padrão. Cada tarefa responde uma cor primária.

O retorno é um JSON com `results` **na mesma ordem** das tarefas enviadas.

### Valves principais

| Valve | Padrão | Para que serve |
|---|---|---|
| `DEFAULT_MODEL` | vazio | Modelo padrão do subagente. Vazio = herda o modelo do chat pai. |
| `ALLOWED_MODEL_IDS` | vazio | Allowlist. Vazio ou `*` = todos. Aceita vírgula e curingas: `qwen*`, `gpt-4o,glm-*`. |
| `MAX_ITERATIONS` | `25` | Iterações por subagente. `0` = ilimitado (use com cautela). |
| `MAX_PARALLEL_AGENTS` | `5` | Subagentes simultâneos. Também limita o tamanho da lista de tarefas. |
| `AVAILABLE_TOOL_IDS` | vazio | Se preenchido, **substitui** as ferramentas do chat (csv de tool ids). |
| `EXCLUDED_TOOL_IDS` | vazio | Tool ids nunca herdados. A própria ferramenta é sempre excluída. |
| `ENABLE_TERMINAL_TOOLS` | `true` | Herda as ferramentas do Open Terminal quando o chat tem `terminal_id`. |
| `MAX_TOOL_RESULT_CHARS` | `60000` | Teto do resultado de uma ferramenta. |
| `TRUNCATE_TOOL_RESULTS` | `true` | Corta resultados grandes preservando cabeça e cauda, com marcador de omissão. |
| `ENABLE_CONTEXT_COMPACTION` | `false` | Compactação determinística de resultados antigos acima do limiar. |
| `CONTEXT_COMPACTION_TOKEN_THRESHOLD` | `80000` | Limiar de tokens que dispara a compactação. |
| `ITERATION_NOTE_ROLE` | `user` | Papel da nota `[Iteração N/M]` injetada em cada rodada. |
| `DEBUG` | `false` | Log detalhado (modelo, iteração, nomes de ferramenta, tamanho). Nunca loga dados sensíveis. |

### Categorias de ferramentas nativas

Cada Valve abaixo liga ou desliga uma família de ferramentas nativas dentro do subagente.

| Valve | Categoria |
|---|---|
| `ENABLE_TIME_TOOLS` | hora e cálculos de data |
| `ENABLE_USER_INPUT_TOOLS` | `ask_user` |
| `ENABLE_WEB_TOOLS` | busca e leitura de páginas |
| `ENABLE_IMAGE_TOOLS` | geração e edição de imagem |
| `ENABLE_FILE_TOOLS` | arquivos do chat |
| `ENABLE_KNOWLEDGE_TOOLS` | bases de conhecimento |
| `ENABLE_CHAT_TOOLS` | histórico de chats |
| `ENABLE_MEMORY_TOOLS` | memórias |
| `ENABLE_NOTES_TOOLS` | notas |
| `ENABLE_CHANNELS_TOOLS` | canais |
| `ENABLE_CODE_INTERPRETER_TOOLS` | execução de código |
| `ENABLE_SKILLS_TOOLS` / `ENABLE_SKILLS_WRITE_TOOLS` | leitura e escrita de skills |
| `ENABLE_TASK_TOOLS` | checklist de tarefas |
| `ENABLE_CALENDAR_TOOLS` | calendário |
| `ENABLE_SUBAGENT_TOOLS` | `delegate_task`, `timer` — **desligado** por padrão |
| `ENABLE_AUTOMATION_TOOLS` | automações — **desligado** por padrão |
| `ENABLE_NOTIFICATION_TOOLS` | notificações — **desligado** por padrão |

As três últimas vêm desligadas de propósito: um subagente não deve escalar privilégios nem
agendar coisas sozinho. Ligue só se precisar.

### User Valves

`SYSTEM_PROMPT` — o system prompt do subagente. Cada usuário pode ajustar o seu. O texto
padrão já orienta o subagente a concluir a tarefa sozinho e a explicar o que ficou pendente
quando não conseguir.

### Limites conhecidos (v0.1.0)

- **Filtros inlet/request não são aplicados** dentro do subagente. A chamada usa
  `bypass_filter=True`; a Valve `APPLY_INLET_FILTERS` existe apenas como reserva.
- **Compactação de contexto é determinística** (substitui resultados antigos por stubs).
  Não há sumarização por modelo.
- **Servidores MCP e tool servers OpenAPI** não são herdados diretamente; use
  `AVAILABLE_TOOL_IDS` se eles também estiverem registrados como Tools.
- O subagente herda as ferramentas **do chat**, não as do turno: ferramentas ligadas só
  para uma mensagem podem não aparecer.
- O modelo escolhido precisa aceitar `tools` (function calling). Modelos sem suporte
  respondem em texto e o loop termina na primeira iteração.

### Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `MODEL_NOT_ALLOWED` | O modelo pedido não casa com `ALLOWED_MODEL_IDS`. | Ajuste a allowlist ou peça outro modelo. |
| `MODEL_NOT_RESOLVED` | Nenhum modelo pôde ser determinado. | Preencha `DEFAULT_MODEL` ou use a ferramenta em um chat com modelo definido. |
| Subagente não usa ferramenta nenhuma | O modelo não suporta function calling, ou todas as categorias estão desligadas. | Teste o modelo com `tools` e revise os `ENABLE_*_TOOLS`. |
| `tasks (N) excede MAX_PARALLEL_AGENTS` | Lista maior que o limite. | Reduza as tarefas ou aumente a Valve. |
| Subagente responde que não pode delegar | Anti-recursão funcionando. | Comportamento esperado. |

---

## Whisper Transcription

Transcreve arquivos de áudio usando um endpoint compatível com `/v1/audio/transcriptions`.
Configure `WHISPER_BASE_URL`, `WHISPER_API_KEY` e `WHISPER_MODEL` nas Valves da ferramenta —
os padrões são genéricos e não apontam para nenhum servidor específico.

---

## Desenvolvimento

```bash
python3 -m unittest discover -s flex_sub_agent -v
```

Os testes rodam offline: os helpers do core do Open WebUI são substituídos por mocks, então
não é preciso subir a aplicação.

Estrutura de `flex_sub_agent/`:

| Arquivo | Papel |
|---|---|
| `flex_sub_agent.py` | a ferramenta (copiar e colar no Open WebUI) |
| `test_flex_sub_agent.py` | testes unitários offline |
| `README.md` | documentação detalhada e changelog |

---

## Licença

MIT — veja [LICENSE](LICENSE).