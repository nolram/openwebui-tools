# Flex Sub Agent

Ferramenta de **subagentes** para o Open WebUI: delegação em sequência ou em paralelo, com
**escolha de modelo por chamada**.

- Arquivo único: [`flex_sub_agent.py`](flex_sub_agent.py) — copie e cole em
  **Workspace → Tools → +**.
- Sem dependências externas: só stdlib + o que o próprio Open WebUI já traz.
- **Nenhuma credencial** na ferramenta: roda dentro do processo e herda as ferramentas já
  autenticadas no request do usuário.

---

## Por que existe

A tool da comunidade `sub_agent` chama um helper do core passando `direct_tool=`
incondicionalmente. Em forks cujo `process_tool_result` não declara esse parâmetro, todo
resultado de ferramenta vira a string:

```text
Error: process_tool_result() got an unexpected keyword argument 'direct_tool'
```

O subagente então termina sem conseguir usar ferramenta nenhuma. O Flex Sub Agent:

1. **inspeciona a assinatura** do helper e só passa `direct_tool` se ele existir;
2. resolve imports por **cadeia de candidatos** (o fork move símbolos entre módulos);
3. **isola o `request.state`** de cada subagente, para que execuções em paralelo não
   compartilhem estado;
4. remove **sempre** a própria ferramenta do subagente (anti-recursão sem depender de
   configuração).

---

## Interface

```python
run_sub_agent(description: str, prompt: str, model: str | None = None,
              max_iterations: int | None = None) -> str

run_parallel_sub_agents(tasks: list[{description, prompt, model}],
                        model: str | None = None,
                        max_iterations: int | None = None) -> str
```

`run_sub_agent` devolve JSON com `description`, `model`, `iterations`, `tools_available` e
`result`. `run_parallel_sub_agents` devolve JSON com `total`, `succeeded`, `failed` e
`results` **na mesma ordem** das tarefas.

### Resolução do modelo

Nesta ordem, o primeiro que existir vence:

1. `model` explícito da chamada (ou `tasks[i].model`)
2. Valve `DEFAULT_MODEL`
3. modelo do chat pai (`metadata.model.id`)
4. modelo injetado no contexto (`__model__.id`)
5. fallback do core (`get_task_model_id`)

Depois o resultado é validado contra `ALLOWED_MODEL_IDS`. Se for barrado, a ferramenta
devolve `{"error": "MODEL_NOT_ALLOWED", "requested": ..., "allowed": [...]}` — **nunca**
troca de modelo em silêncio.

---

## Valves

### Modelo e execução

| Valve | Padrão | Descrição |
|---|---|---|
| `DEFAULT_MODEL` | vazio | Modelo padrão. Vazio = herda o do chat pai. |
| `ALLOWED_MODEL_IDS` | vazio | Allowlist csv com curingas (`qwen*`). Vazio ou `*` = todos. |
| `MAX_ITERATIONS` | `25` | Iterações por subagente. `0` = ilimitado. |
| `MAX_PARALLEL_AGENTS` | `5` | Subagentes simultâneos e tamanho máximo da lista de tarefas. |

### Ferramentas

| Valve | Padrão | Descrição |
|---|---|---|
| `AVAILABLE_TOOL_IDS` | vazio | Substitui as ferramentas do chat (csv). |
| `EXCLUDED_TOOL_IDS` | vazio | Tool ids nunca herdados. A própria tool é sempre excluída. |
| `ENABLE_TERMINAL_TOOLS` | `true` | Herda ferramentas do Open Terminal quando há `terminal_id`. |
| `ENABLE_*_TOOLS` | ver tabela abaixo | Liga/desliga famílias de ferramentas nativas. |

### Contexto e diagnóstico

| Valve | Padrão | Descrição |
|---|---|---|
| `MAX_TOOL_RESULT_CHARS` | `60000` | Teto do resultado de uma ferramenta. |
| `TRUNCATE_TOOL_RESULTS` | `true` | Corta preservando cabeça e cauda. |
| `ENABLE_CONTEXT_COMPACTION` | `false` | Compactação determinística acima do limiar. |
| `CONTEXT_COMPACTION_TOKEN_THRESHOLD` | `80000` | Limiar em tokens. |
| `COMPACTION_SUMMARY_MODEL` | vazio | Reservado; a compactação atual é determinística. |
| `ITERATION_NOTE_ROLE` | `user` | Papel da nota `[Iteração N/M]`. |
| `APPLY_INLET_FILTERS` | `false` | Reservado; filtros inlet não são suportados. |
| `DEBUG` | `false` | Log detalhado, nunca com dados sensíveis. |

---

## Categorias de ferramentas nativas

| Valve | Padrão | Ferramentas |
|---|---|---|
| `ENABLE_TIME_TOOLS` | `true` | `get_current_timestamp`, `calculate_timestamp` |
| `ENABLE_USER_INPUT_TOOLS` | `true` | `ask_user` |
| `ENABLE_WEB_TOOLS` | `true` | `search_web`, `fetch_url` |
| `ENABLE_IMAGE_TOOLS` | `true` | `generate_image`, `edit_image` |
| `ENABLE_FILE_TOOLS` | `true` | `list_chat_files`, `query_chat_files`, `grep_chat_files`, `view_file`, `display_file` |
| `ENABLE_KNOWLEDGE_TOOLS` | `true` | `list_knowledge*`, `query_knowledge_files`, `grep_knowledge_files`, `kb_exec`, `view_knowledge_file` |
| `ENABLE_CHAT_TOOLS` | `true` | `search_chats`, `view_chat` |
| `ENABLE_MEMORY_TOOLS` | `true` | `search_memories`, `add_memory`, `replace_memory_content`, `delete_memory` |
| `ENABLE_NOTES_TOOLS` | `true` | `search_notes`, `view_note`, `write_note`, `replace_note_content` |
| `ENABLE_CHANNELS_TOOLS` | `true` | `search_channels`, `search_channel_messages`, `view_channel_*` |
| `ENABLE_CODE_INTERPRETER_TOOLS` | `true` | `execute_code` |
| `ENABLE_SKILLS_TOOLS` | `true` | `view_skill`, `read_skill_file`, `grep_skill_files` |
| `ENABLE_SKILLS_WRITE_TOOLS` | `true` | `update_skill_files`, `create_skill` |
| `ENABLE_TASK_TOOLS` | `true` | `create_tasks`, `update_task` |
| `ENABLE_CALENDAR_TOOLS` | `true` | `search_calendar_events`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event` |
| `ENABLE_SUBAGENT_TOOLS` | `false` | `delegate_task`, `timer` |
| `ENABLE_AUTOMATION_TOOLS` | `false` | `create_automation` e afins |
| `ENABLE_NOTIFICATION_TOOLS` | `false` | `notify` |

Desligadas por padrão de propósito: um subagente não deve delegar de novo, agendar coisas
nem notificar pessoas por conta própria.

---

## User Valves

`SYSTEM_PROMPT` — system prompt do subagente, ajustável por usuário.

---

## Segurança

- Nenhuma URL, host, IP, namespace ou credencial aparece no código.
- A ferramenta **não lê** arquivos de credencial e **não** faz chamadas HTTP de volta.
- Logs usam o logger `tool_flex_sub_agent` com prefixo fixo `[FlexSubAgent]` e registram no
  máximo: modelo, iteração, nomes de ferramenta e tamanho do resultado. Nunca o conteúdo de
  `__user__`, `__oauth_token__`, `extra_params`, headers ou `form_data`.
- Erros de ferramenta voltam como **conteúdo** (`role="tool"`), truncados em 500 caracteres,
  para o subagente poder tentar outra abordagem em vez de derrubar o chat.

---

## Testes

```bash
python3 -m unittest discover -s flex_sub_agent -v
```

Cobrem o shim de compatibilidade (com e sem `direct_tool`), normalização de
`tool_calls`/`arguments`, truncamento, resolução de modelo e allowlist, normalização do
retorno de `get_terminal_tools` (tupla e dict), anti-recursão, ordem e limite do modo
paralelo, e o comportamento do loop (parada, execução de ferramenta, limite de iterações).

---

## Limitações (v0.1.0)

- Filtros inlet/request não são aplicados dentro do subagente (`bypass_filter=True`).
- Compactação de contexto é determinística, não sumarizada.
- Servidores MCP e tool servers OpenAPI não são herdados diretamente.
- O modelo escolhido precisa suportar function calling.

---

## Rollback

Esta ferramenta tem id próprio (`flex_sub_agent`) e não substitui nada. Para voltar atrás,
basta desativá-la e reativar a tool anterior — nenhum dado da outra é alterado.

---

## Changelog

### 0.1.0

- Primeira versão: `run_sub_agent`, `run_parallel_sub_agents`, modelo por chamada com
  allowlist, herança de ferramentas (chat + nativas por categoria + terminal),
  anti-recursão incondicional, isolamento de `request.state` no paralelo.

---

## Licença

MIT — veja o [LICENSE](../LICENSE) do repositório.