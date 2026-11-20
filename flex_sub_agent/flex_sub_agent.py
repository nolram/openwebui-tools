"""
title: Flex Sub Agent
author: nolram
version: 0.1.0
license: MIT
required_open_webui_version: 0.11.0
description: Delega tarefas a subagentes (sequência ou paralelo), com escolha de modelo por chamada.
"""

# Subagentes in-process sobre os helpers do core do Open WebUI.
#   * Roda DENTRO do processo: nenhuma chamada HTTP de volta, nenhum token, nenhuma senha.
#     As ferramentas herdadas já estão autenticadas no request do usuário.
#   * Import resiliente: o fork move símbolos entre módulos; nunca fixamos um único caminho.
#   * Compat por assinatura: o shim de `process_tool_result` inspeciona a assinatura do core.
#   * Anti-recursão incondicional: o subagente nunca herda a própria ferramenta.
#
# NÃO use `from __future__ import annotations`: o loader do Open WebUI pode não registrar o
# módulo em sys.modules, e as anotações viram strings que o pydantic não resolve.

import ast
import asyncio
import copy
import fnmatch
import importlib
import inspect
import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("tool_flex_sub_agent")
LOG_PREFIX = "[FlexSubAgent]"


def _import_first(mod_names: List[str], attr: str) -> Any:
    """Resolve um símbolo tentando módulos candidatos, na ordem dada."""
    last = None
    for name in mod_names:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:  # ImportError e variantes
            last = exc
            continue
        obj = getattr(mod, attr, None)
        if obj is not None:
            return obj
    raise ImportError(f"{attr} não encontrado em {mod_names}: {last}")


def _safe_import(mod_names: List[str], attr: str) -> Any:
    """Como `_import_first`, mas devolve None em vez de explodir no import do módulo."""
    try:
        return _import_first(mod_names, attr)
    except Exception as exc:
        log.debug("%s helper indisponível: %s (%s)", LOG_PREFIX, attr, exc)
        return None


generate_chat_completion = _safe_import(
    ["open_webui.utils.chat", "open_webui.utils.app", "open_webui.utils.middleware"], "generate_chat_completion"
)
get_event_emitter = _safe_import(["open_webui.utils.actions", "open_webui.utils.app"], "get_event_emitter")
get_event_call = _safe_import(["open_webui.utils.actions", "open_webui.utils.app"], "get_event_call")
process_tool_result_core = _safe_import(
    ["open_webui.utils.middleware", "open_webui.utils.actions"], "process_tool_result"
)
get_tools = _safe_import(["open_webui.utils.tools", "open_webui.utils.middleware"], "get_tools")
get_builtin_tools = _safe_import(["open_webui.utils.tools", "open_webui.utils.middleware"], "get_builtin_tools")
get_terminal_tools = _safe_import(["open_webui.utils.tools", "open_webui.utils.middleware"], "get_terminal_tools")
get_updated_tool_function = _safe_import(
    ["open_webui.utils.middleware", "open_webui.utils.tools"], "get_updated_tool_function"
)
get_task_model_id = _safe_import(["open_webui.utils.middleware", "open_webui.utils.app"], "get_task_model_id")
UserModel = _safe_import(["open_webui.models.users"], "UserModel")


def _signature_has(func: Callable, param: str) -> bool:
    try:
        return param in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def _maybe_await(value: Any) -> Any:
    """Normaliza para algo SEMPRE aguardável (o core mistura helpers sync e async)."""
    if hasattr(value, "__await__"):
        return value

    async def _immediate() -> Any:
        return value

    return _immediate()


async def _await(value: Any) -> Any:
    """Aguarda quando necessário; aceita valor síncrono, corrotina ou awaitable."""
    return await _maybe_await(value)


async def process_tool_result_compat(
    request: Any, name: str, result: Any, tool_type: str,
    direct_tool: bool = False, metadata: Optional[dict] = None, user: Any = None,
):
    """Chama o core com os kwargs que ele realmente aceita; devolve sempre uma tupla de 3.

    Aqui mora o bug da ferramenta da comunidade: ela passa `direct_tool=` incondicionalmente e,
    nos forks cujo helper não declara esse parâmetro, todo resultado de ferramenta vira
    "unexpected keyword argument". Inspecionamos a assinatura antes de decidir.
    """
    core = process_tool_result_core
    if core is None:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return text, [], []
    kwargs: Dict[str, Any] = {"metadata": metadata if isinstance(metadata, dict) else {}, "user": user}
    if _signature_has(core, "direct_tool"):
        kwargs["direct_tool"] = bool(direct_tool)
    # Os quatro primeiros parâmetros são posicionais sem anotação: passar por posição.
    out = await _await(core(request, name, result, tool_type, **kwargs))
    if not isinstance(out, tuple) or len(out) != 3:
        out = (out if isinstance(out, str) else str(out), [], [])
    return out


def isolate_request_state(request: Any, metadata: Optional[dict]) -> Any:
    """Cópia do request com `state.metadata` próprio — evita corrida no modo paralelo."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return request
    state = dict(scope.get("state") or {})
    state["metadata"] = copy.deepcopy(metadata) if isinstance(metadata, dict) else metadata
    isolated = copy.copy(request)
    isolated.scope = {**scope, "state": state}
    try:
        from starlette.datastructures import State

        isolated._state = State(state)
    except Exception:  # pragma: no cover - starlette sempre presente em runtime
        pass
    return isolated


def truncate_text(text: str, limit: int, enabled: bool = True) -> str:
    """Corta mantendo cabeça e cauda, com marcador explícito de omissão."""
    if not enabled or limit <= 0 or not isinstance(text, str) or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = max(limit - head, 1)
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text
    return f"{text[:head]}\n... [omitido {omitted} caracteres] ...\n{text[-tail:]}"


def make_status_emitter(emitter: Any, prefix: str = "") -> Any:
    """Embrulha um emitter prefixando a descrição — usado no modo paralelo."""
    if emitter is None:
        return None

    async def wrapped(event: dict) -> Any:
        try:
            if isinstance(event, dict) and isinstance(event.get("data"), dict) and prefix:
                data = dict(event["data"])
                data["description"] = f"{prefix} {data.get('description', '')}".strip()
                event = {**event, "data": data}
            return await _await(emitter(event))
        except Exception:
            return None

    return wrapped


async def emit_status(emitter: Any, description: str, done: bool = False) -> None:
    if emitter is None:
        return
    try:
        await _await(emitter({"type": "status", "data": {"description": description, "done": done}}))
    except Exception:
        pass


# Catálogo de ferramentas nativas por categoria (nomes reais do core).
BUILTIN_TOOL_CATEGORIES: Dict[str, List[str]] = {
    "time": ["get_current_timestamp", "calculate_timestamp"],
    "user_input": ["ask_user"],
    "web": ["search_web", "fetch_url"],
    "image": ["generate_image", "edit_image"],
    "files": ["list_chat_files", "query_chat_files", "grep_chat_files", "view_file", "display_file"],
    "knowledge": [
        "list_knowledge", "list_knowledge_bases", "search_knowledge_bases", "query_knowledge_bases",
        "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files", "kb_exec",
        "view_file", "view_knowledge_file", "display_file",
    ],
    "chat": ["search_chats", "view_chat"],
    "memory": [
        "search_memories", "list_memory_paths", "read_memory_path", "list_memories",
        "update_memory", "add_memory", "replace_memory_content", "delete_memory",
    ],
    "notes": ["search_notes", "view_note", "write_note", "replace_note_content"],
    "channels": ["search_channels", "search_channel_messages", "view_channel_thread", "view_channel_message"],
    "code_interpreter": ["execute_code"],
    "skills": ["view_skill", "read_skill_file", "grep_skill_files"],
    "skills_write": ["update_skill_files", "create_skill"],
    "subagents": ["delegate_task", "timer"],
    "tasks": ["create_tasks", "update_task"],
    "automations": [
        "create_automation", "update_automation", "list_automations", "toggle_automation", "delete_automation",
    ],
    "calendar": [
        "search_calendar_events", "create_calendar_event", "update_calendar_event", "delete_calendar_event",
    ],
    "notifications": ["notify"],
}

VALVE_TO_CATEGORY: Dict[str, str] = {
    "ENABLE_TIME_TOOLS": "time", "ENABLE_USER_INPUT_TOOLS": "user_input", "ENABLE_WEB_TOOLS": "web",
    "ENABLE_IMAGE_TOOLS": "image", "ENABLE_FILE_TOOLS": "files", "ENABLE_KNOWLEDGE_TOOLS": "knowledge",
    "ENABLE_CHAT_TOOLS": "chat", "ENABLE_MEMORY_TOOLS": "memory", "ENABLE_NOTES_TOOLS": "notes",
    "ENABLE_CHANNELS_TOOLS": "channels", "ENABLE_CODE_INTERPRETER_TOOLS": "code_interpreter",
    "ENABLE_SKILLS_TOOLS": "skills", "ENABLE_SKILLS_WRITE_TOOLS": "skills_write",
    "ENABLE_SUBAGENT_TOOLS": "subagents", "ENABLE_TASK_TOOLS": "tasks",
    "ENABLE_AUTOMATION_TOOLS": "automations", "ENABLE_CALENDAR_TOOLS": "calendar",
    "ENABLE_NOTIFICATION_TOOLS": "notifications",
}
CATEGORY_TO_VALVE: Dict[str, str] = {v: k for k, v in VALVE_TO_CATEGORY.items()}

# O core lê as categorias de `model.info.meta.builtinTools`, com nomes que nem sempre coincidem
# com os nomes lógicos acima. Mapeamos os dois para o mesmo booleano.
CATEGORY_CORE_KEYS: Dict[str, tuple] = {
    "web": ("web", "web_search"), "image": ("image", "image_generation"),
    "chat": ("chat", "chats"), "files": ("files",),
}
# Categorias do core sem Valve correspondente: sempre desligadas dentro do subagente.
ALWAYS_DISABLED_CORE_KEYS = ("video_generation", "tools", "visualizations")
# Nomes/ids que um subagente nunca recebe (anti-recursão).
SELF_TOOL_NAMES = ("run_sub_agent", "run_parallel_sub_agents")
SELF_TOOL_IDS = ("flex_sub_agent", "sub_agent")
KNOWN_BUILTIN_NAMES = {n for names in BUILTIN_TOOL_CATEGORIES.values() for n in names}


class SubAgentTaskItem(BaseModel):
    """Uma tarefa delegada (usada por run_parallel_sub_agents)."""

    description: str = Field(description="Rótulo curto da tarefa, usado no relatório final.")
    prompt: str = Field(description="Instrução completa e autocontida para o subagente.")
    model: Optional[str] = Field(
        default=None, description="Modelo apenas desta tarefa. Vazio = modelo padrão do chat/configuração."
    )


class _UserShim:
    """Fallback mínimo quando `UserModel` não está disponível."""

    def __init__(self, data: dict):
        self.id = data.get("id") or ""
        self.email = data.get("email")
        self.name = data.get("name")
        self.role = data.get("role") or "user"

    def model_dump(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role}


class Tools:
    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        DEFAULT_MODEL: str = Field(default="", description="Modelo padrão dos subagentes. Vazio = modelo do chat pai.")
        ALLOWED_MODEL_IDS: str = Field(
            default="", description="Allowlist de modelos. Vazio ou '*' = todos. Aceita vírgula e curingas ('qwen*')."
        )
        MAX_ITERATIONS: int = Field(default=25, ge=0, description="Iterações máximas do loop. 0 = ilimitado.")
        MAX_PARALLEL_AGENTS: int = Field(default=5, ge=1, description="Máximo de subagentes simultâneos.")
        AVAILABLE_TOOL_IDS: str = Field(
            default="", description="Se preenchido, substitui as ferramentas do chat (csv de tool ids)."
        )
        EXCLUDED_TOOL_IDS: str = Field(
            default="", description="Tool ids nunca herdados (csv). A própria ferramenta é sempre excluída."
        )
        ENABLE_TERMINAL_TOOLS: bool = Field(
            default=True, description="Herdar as ferramentas do Open Terminal quando o chat tem terminal_id."
        )
        APPLY_INLET_FILTERS: bool = Field(
            default=False, description="Reservado: filtros inlet/request não são suportados nesta versão."
        )
        MAX_TOOL_RESULT_CHARS: int = Field(
            default=60000, ge=1000, description="Tamanho máximo do resultado de uma ferramenta."
        )
        TRUNCATE_TOOL_RESULTS: bool = Field(
            default=True, description="Truncar resultados grandes preservando cabeça e cauda."
        )
        ENABLE_CONTEXT_COMPACTION: bool = Field(
            default=False, description="Compactação determinística de contexto antigo acima do limiar."
        )
        CONTEXT_COMPACTION_TOKEN_THRESHOLD: int = Field(
            default=80000, ge=1000, description="Limiar de tokens para disparar a compactação."
        )
        COMPACTION_SUMMARY_MODEL: str = Field(
            default="", description="Reservado para sumarização futura; a compactação atual é determinística."
        )
        ITERATION_NOTE_ROLE: Literal["user", "system"] = Field(
            default="user", description="Papel da nota de iteração injetada em cada rodada."
        )
        DEBUG: bool = Field(default=False, description="Log detalhado (nunca inclui dados sensíveis).")

        # Categorias de ferramentas nativas (ver tabela no README da pasta).
        ENABLE_TIME_TOOLS: bool = True
        ENABLE_USER_INPUT_TOOLS: bool = True
        ENABLE_WEB_TOOLS: bool = True
        ENABLE_IMAGE_TOOLS: bool = True
        ENABLE_FILE_TOOLS: bool = True
        ENABLE_KNOWLEDGE_TOOLS: bool = True
        ENABLE_CHAT_TOOLS: bool = True
        ENABLE_MEMORY_TOOLS: bool = True
        ENABLE_NOTES_TOOLS: bool = True
        ENABLE_CHANNELS_TOOLS: bool = True
        ENABLE_CODE_INTERPRETER_TOOLS: bool = True
        ENABLE_SKILLS_TOOLS: bool = True
        ENABLE_SKILLS_WRITE_TOOLS: bool = True
        ENABLE_TASK_TOOLS: bool = True
        ENABLE_CALENDAR_TOOLS: bool = True
        ENABLE_SUBAGENT_TOOLS: bool = False
        ENABLE_AUTOMATION_TOOLS: bool = False
        ENABLE_NOTIFICATION_TOOLS: bool = False

    class UserValves(BaseModel):
        SYSTEM_PROMPT: str = Field(
            default="""\
Você é um subagente operando de forma autônoma para concluir uma tarefa delegada.
REGRAS:
1. Conclua a tarefa por completo, sem pedir confirmação ao usuário.
2. Use as ferramentas disponíveis para reunir informação e agir.
3. Se encontrar um obstáculo, tente abordagens alternativas antes de desistir.
4. Notas [Iteração N/M] indicam limite de iterações: termine antes do limite.
RESPOSTA FINAL: abrangente, com evidências. Se não conseguir concluir, explique o que
tentou, por que falhou e os próximos passos acionáveis.""",
            description="System prompt do subagente.",
        )

    # ---------------------------------------------------------------- métodos públicos
    async def run_sub_agent(
        self, description: str, prompt: str, model: Optional[str] = None,
        max_iterations: Optional[int] = None,
        __user__=None, __request__=None, __model__=None, __metadata__=None, __id__=None,
        __event_emitter__=None, __event_call__=None, __chat_id__=None, __message_id__=None,
        __oauth_token__=None, __messages__=None,
    ) -> str:
        """Executa UM subagente autônomo, com as ferramentas do chat herdadas.

        Bloqueia até terminar e devolve o texto final (mais metadados de execução).
        Use quando a tarefa é isolada e precisa de várias chamadas de ferramenta.

        :param description: Rótulo curto do que o subagente deve fazer.
        :param prompt: Instrução completa e autocontida para o subagente.
        :param model: Modelo do subagente. Vazio = modelo padrão do chat/configuração.
        :param max_iterations: Limite de iterações; 0 = ilimitado; vazio = valor das Valves.
        :return: JSON com description, model, iterações, ferramentas e result.
        """
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        try:
            emitter = __event_emitter__ or await self._emitter_for(metadata)
            ctx = self._agent_context(
                __request__, __user__, __model__, metadata, emitter, __event_call__,
                __chat_id__ or metadata.get("chat_id"), __message_id__, __oauth_token__, __messages__,
            )
            outcome = await self._run_one(ctx, description, prompt, model, max_iterations, __id__)
            if outcome.get("error"):
                return json.dumps(outcome["error"], ensure_ascii=False)
            return json.dumps(outcome, ensure_ascii=False)
        except Exception as exc:
            log.exception("%s run_sub_agent falhou", LOG_PREFIX)
            return json.dumps({"error": "SUBAGENT_FAILED", "detail": str(exc)[:500]}, ensure_ascii=False)

    async def run_parallel_sub_agents(
        self, tasks: List[SubAgentTaskItem], model: Optional[str] = None,
        max_iterations: Optional[int] = None,
        __user__=None, __request__=None, __model__=None, __metadata__=None, __id__=None,
        __event_emitter__=None, __event_call__=None, __chat_id__=None, __message_id__=None,
        __oauth_token__=None, __messages__=None,
    ) -> str:
        """Executa VÁRIOS subagentes em paralelo, cada um podendo usar um modelo diferente.

        Devolve um JSON com "results": lista na MESMA ordem das tarefas enviadas.
        Use quando as subtarefas são independentes (economiza tempo real de execução).

        :param tasks: Lista de tarefas; cada item tem description, prompt e model opcional.
        :param model: Modelo padrão para as tarefas que não definirem o próprio.
        :param max_iterations: Limite de iterações por subagente; 0 = ilimitado.
        :return: JSON com results (ordenado), total, succeeded, failed.
        """
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        if not isinstance(tasks, list) or not tasks:
            return json.dumps({"error": "Nenhuma tarefa informada."}, ensure_ascii=False)
        if len(tasks) > self.valves.MAX_PARALLEL_AGENTS:
            return json.dumps(
                {"error": f"tasks ({len(tasks)}) excede MAX_PARALLEL_AGENTS ({self.valves.MAX_PARALLEL_AGENTS})"},
                ensure_ascii=False,
            )

        try:
            base_emitter = __event_emitter__ or await self._emitter_for(metadata)
        except Exception:
            base_emitter = None
        semaphore = asyncio.Semaphore(self.valves.MAX_PARALLEL_AGENTS)

        async def one(index: int, task: Any) -> dict:
            item = self._coerce_task(task)
            label = item.get("description") or f"tarefa {index}"
            async with semaphore:
                try:
                    ctx = self._agent_context(
                        __request__, __user__, __model__, metadata,
                        make_status_emitter(base_emitter, f"[{index}]"), __event_call__,
                        __chat_id__ or metadata.get("chat_id"), __message_id__,
                        __oauth_token__, __messages__,
                    )
                    outcome = await self._run_one(
                        ctx, label, item.get("prompt") or "", item.get("model") or model,
                        max_iterations, __id__,
                    )
                    return outcome
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("%s tarefa %s falhou: %s", LOG_PREFIX, index, type(exc).__name__)
                    return {"description": label, "error": str(exc)[:500] or type(exc).__name__}

        results = await asyncio.gather(*(one(i, t) for i, t in enumerate(tasks)), return_exceptions=True)
        normalized = []
        for i, res in enumerate(results):
            if isinstance(res, BaseException):
                item = self._coerce_task(tasks[i])
                normalized.append(
                    {"description": item.get("description") or f"tarefa {i}", "error": str(res)[:500]}
                )
            else:
                normalized.append(res)
        return json.dumps(
            {
                "total": len(normalized),
                "succeeded": sum(1 for r in normalized if not r.get("error")),
                "failed": sum(1 for r in normalized if r.get("error")),
                "results": normalized,
            },
            ensure_ascii=False,
        )

    # ---------------------------------------------------------------- infraestrutura
    async def _run_one(self, ctx: dict, description: str, prompt: str, model: Any,
                       max_iterations: Any, own_tool_id: Any) -> dict:
        """Executa um subagente completo dentro de um contexto já isolado."""
        model_id, error = await self._resolve_model(model, ctx["metadata"], ctx["model_dict"], ctx["request"])
        if error:
            return {"description": description, "model": model or "", "error": error}
        tools_dict, terminal_prompt = await self._collect_tools(ctx, own_tool_id)
        messages = self._initial_messages(ctx["system_prompt"], terminal_prompt, prompt)
        iterations = self._iterations(max_iterations)
        result = await self._run_loop(
            ctx, model_id, messages, tools_dict, iterations, label=description or "subagente"
        )
        return {
            "description": description, "model": model_id, "iterations": iterations,
            "tools_available": sorted(tools_dict.keys()), "result": result,
        }

    @staticmethod
    def _coerce_task(task: Any) -> dict:
        if isinstance(task, dict):
            return task
        if isinstance(task, BaseModel):
            return task.model_dump()
        return {}

    def _iterations(self, override: Optional[int]) -> int:
        if isinstance(override, int) and override >= 0:
            return override
        return int(self.valves.MAX_ITERATIONS)

    async def _emitter_for(self, metadata: dict) -> Any:
        if get_event_emitter is None:
            return None
        try:
            info = {
                "chat_id": metadata.get("chat_id"),
                "message_id": metadata.get("message_id"),
                "session_id": metadata.get("session_id"),
            }
            return await _await(get_event_emitter(info, update_db=False))
        except Exception:
            return None

    def _build_user(self, user: Any) -> tuple:
        """Devolve (user_model, user_dict). Nunca loga o conteúdo."""
        if isinstance(user, dict):
            user_dict = user
        elif hasattr(user, "model_dump"):
            user_dict = user.model_dump()
        elif user is None:
            return None, None
        else:
            user_dict = dict(getattr(user, "__dict__", {}) or {})
        user_model = None
        if user_dict:
            if UserModel is not None:
                try:
                    user_model = UserModel(**user_dict)
                except Exception:
                    user_model = _UserShim(user_dict)
            else:
                user_model = _UserShim(user_dict)
        return user_model, user_dict

    def _system_prompt(self) -> str:
        user_valves = getattr(self, "user_valves", None)
        prompt = getattr(user_valves, "SYSTEM_PROMPT", None)
        return str(prompt if prompt else self.UserValves().SYSTEM_PROMPT).strip()

    def _agent_context(self, request, user, model_dict, metadata, emitter, event_call,
                       chat_id, message_id, oauth_token, messages) -> dict:
        """Monta o contexto ISOLADO de um subagente (request, metadata, extra_params)."""
        metadata_iso = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
        request_iso = isolate_request_state(request, metadata_iso)
        user_model, user_dict = self._build_user(user)
        extra_params = {
            "__user__": user_dict, "__request__": request_iso, "__model__": model_dict,
            "__metadata__": metadata_iso, "__event_emitter__": emitter, "__event_call__": event_call,
            "__chat_id__": chat_id, "__message_id__": message_id, "__oauth_token__": oauth_token,
            "__messages__": messages or [], "__files__": metadata_iso.get("files") or [],
        }
        return {
            "request": request_iso, "user": user_model, "user_dict": user_dict,
            "model_dict": model_dict if isinstance(model_dict, dict) else {},
            "metadata": metadata_iso, "emitter": emitter, "extra_params": extra_params,
            "system_prompt": self._system_prompt(),
        }

    @staticmethod
    def _initial_messages(system_prompt: str, terminal_prompt: Optional[str], prompt: str) -> List[dict]:
        system = f"{system_prompt}\n\n{terminal_prompt}".strip() if terminal_prompt else system_prompt
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    # ---------------------------------------------------------------- resolução de modelo
    async def _resolve_model(self, requested, metadata: dict, model_dict, request) -> tuple:
        candidates = []
        if isinstance(requested, str) and requested.strip():
            candidates.append(requested.strip())
        if self.valves.DEFAULT_MODEL.strip():
            candidates.append(self.valves.DEFAULT_MODEL.strip())
        parent = (metadata or {}).get("model") or {}
        if isinstance(parent, dict) and parent.get("id"):
            candidates.append(str(parent["id"]))
        if isinstance(model_dict, dict) and model_dict.get("id"):
            candidates.append(str(model_dict["id"]))
        model_id = next((c for c in candidates if c), "") or self._task_model_fallback(request)
        if not model_id:
            return None, {"error": "MODEL_NOT_RESOLVED", "detail": "Nenhum modelo disponível para o subagente."}
        if not self._model_allowed(model_id):
            return None, {
                "error": "MODEL_NOT_ALLOWED", "requested": model_id, "allowed": self._allowed_patterns(),
            }
        return model_id, None

    def _task_model_fallback(self, request) -> str:
        if get_task_model_id is None:
            return ""
        try:
            state = getattr(getattr(request, "app", None), "state", None)
            models = getattr(state, "MODELS", {}) or {}
            resolved = get_task_model_id(
                "task_model", self.valves.DEFAULT_MODEL, self.valves.COMPACTION_SUMMARY_MODEL, models
            )
            return resolved or ""
        except Exception as exc:
            log.debug("%s fallback de modelo falhou: %s", LOG_PREFIX, type(exc).__name__)
            return ""

    def _allowed_patterns(self) -> List[str]:
        raw = (self.valves.ALLOWED_MODEL_IDS or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _model_allowed(self, model_id: str) -> bool:
        raw = (self.valves.ALLOWED_MODEL_IDS or "").strip()
        if not raw or raw == "*":
            return True
        for pattern in self._allowed_patterns():
            matched = fnmatch.fnmatch(model_id, pattern) if "*" in pattern else pattern == model_id
            if matched:
                return True
        return False

    # ---------------------------------------------------------------- coleta de ferramentas
    def _enabled_builtin_names(self) -> set:
        names = set()
        for category, tools in BUILTIN_TOOL_CATEGORIES.items():
            valve = CATEGORY_TO_VALVE.get(category)
            if valve is None or getattr(self.valves, valve, True):
                names.update(tools)
        return names

    def _builtin_model_override(self, model_dict: dict) -> dict:
        flags: Dict[str, bool] = {}
        for valve, category in VALVE_TO_CATEGORY.items():
            enabled = bool(getattr(self.valves, valve, True))
            for key in CATEGORY_CORE_KEYS.get(category, (category,)):
                flags[key] = enabled
        for key in ALWAYS_DISABLED_CORE_KEYS:
            flags[key] = False
        model = dict(model_dict or {})
        info = dict(model.get("info") or {})
        meta = dict(info.get("meta") or {})
        meta["builtinTools"] = {**(meta.get("builtinTools") or {}), **flags}
        info["meta"] = meta
        model["info"] = info
        return model

    def _visible_tool_ids(self, metadata: dict, own_tool_id: Any) -> List[str]:
        tool_ids = list(metadata.get("tool_ids") or [])
        if self.valves.AVAILABLE_TOOL_IDS.strip():
            tool_ids = [t.strip() for t in self.valves.AVAILABLE_TOOL_IDS.split(",") if t.strip()]
        excluded = {t.strip() for t in (self.valves.EXCLUDED_TOOL_IDS or "").split(",") if t.strip()}
        # ANTI-RECURSÃO: sempre, incondicionalmente, sem depender de Valve.
        excluded |= set(SELF_TOOL_IDS)
        if isinstance(own_tool_id, str) and own_tool_id.strip():
            excluded.add(own_tool_id.strip())
        return [t for t in tool_ids if t and t not in excluded]

    async def _collect_tools(self, ctx: dict, own_tool_id: Any) -> tuple:
        request, user_model = ctx["request"], ctx["user"]
        metadata, extra_params = ctx["metadata"], ctx["extra_params"]
        if request is None:
            raise RuntimeError("__request__ não injetado: impossível herdar ferramentas com segurança.")
        tool_ids = self._visible_tool_ids(metadata, own_tool_id)

        regular: Dict[str, dict] = {}
        if tool_ids and get_tools is not None and user_model is not None:
            try:
                regular = await _await(get_tools(request, tool_ids, user_model, extra_params)) or {}
            except Exception as exc:
                log.warning("%s get_tools falhou (%s)", LOG_PREFIX, type(exc).__name__)

        builtin_allowed = self._enabled_builtin_names()
        builtin: Dict[str, dict] = {}
        if get_builtin_tools is not None:
            kwargs = {
                "request": request, "extra_params": extra_params,
                "features": metadata.get("features") or {},
                "model": self._builtin_model_override(ctx["model_dict"]),
            }
            if _signature_has(get_builtin_tools, "is_note_chat"):
                kwargs["is_note_chat"] = bool(metadata.get("is_note_chat"))
            try:
                for name, spec in (await _await(get_builtin_tools(**kwargs)) or {}).items():
                    if name in SELF_TOOL_NAMES:
                        continue
                    # O core usa default=True por categoria; o filtro explícito é a garantia.
                    if name in KNOWN_BUILTIN_NAMES and name not in builtin_allowed:
                        continue
                    builtin[name] = spec
            except Exception as exc:
                log.warning("%s get_builtin_tools falhou (%s)", LOG_PREFIX, type(exc).__name__)

        terminal: Dict[str, dict] = {}
        terminal_prompt = None
        terminal_id = metadata.get("terminal_id")
        if terminal_id and self.valves.ENABLE_TERMINAL_TOOLS and get_terminal_tools is not None:
            try:
                res = await _await(get_terminal_tools(
                    request=request, terminal_id=terminal_id, user=user_model, extra_params=extra_params
                ))
                # ATENÇÃO: o retorno pode ser tupla (tools, prompt) ou apenas dict.
                if isinstance(res, tuple):
                    terminal, terminal_prompt = res[0] or {}, (res[1] if len(res) > 1 else None)
                else:
                    terminal = res or {}
            except Exception as exc:
                log.warning("%s get_terminal_tools falhou (%s)", LOG_PREFIX, type(exc).__name__)

        merged: Dict[str, dict] = {}
        for source_name, source in (("builtin", builtin), ("regular", regular), ("terminal", terminal)):
            for name, spec in (source or {}).items():
                if not isinstance(spec, dict) or name in SELF_TOOL_NAMES:
                    continue
                if name in merged:
                    log.info("%s colisão de ferramenta '%s' (%s sobrescreve)", LOG_PREFIX, name, source_name)
                merged[name] = spec
        if self.valves.DEBUG:
            log.info("%s ferramentas do subagente: %s", LOG_PREFIX, sorted(merged.keys()))
        return merged, terminal_prompt

    # ---------------------------------------------------------------- execução de ferramenta
    @staticmethod
    def _parse_arguments(raw: Any) -> tuple:
        if isinstance(raw, dict):
            return raw, None
        if not isinstance(raw, str):
            return None, "Erro: argumentos devem ser um objeto JSON."
        text = raw.strip() or "{}"
        try:
            return ast.literal_eval(text), None
        except Exception:
            pass
        try:
            return json.loads(text), None
        except Exception as exc:
            return None, f"Erro ao analisar argumentos: {exc}"

    @staticmethod
    def _normalize_tool_calls(tool_calls: Any) -> List[dict]:
        normalized = []
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments", "{}")
            if not isinstance(args, str):
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = "{}"
            normalized.append({**tc, "function": {**fn, "arguments": args}})
        return normalized

    async def _execute_tool_call(self, tc: dict, tools_dict: dict, extra_params: dict) -> dict:
        tc_id = tc.get("id") or str(uuid.uuid4())
        fn_spec = tc.get("function") or {}
        name = fn_spec.get("name") or ""
        params, error = self._parse_arguments(fn_spec.get("arguments", "{}"))
        if error:
            return {"tool_call_id": tc_id, "content": error}
        if not isinstance(params, dict):
            return {"tool_call_id": tc_id, "content": "Erro: argumentos devem ser um objeto JSON."}
        tool = tools_dict.get(name)
        if tool is None:
            return {"tool_call_id": tc_id, "content": f"Ferramenta desconhecida: {name}"}

        allowed = ((tool.get("spec") or {}).get("parameters") or {}).get("properties") or {}
        params = {k: v for k, v in params.items() if k in allowed}

        # Re-injetar SÓ o contexto por chamada: `get_tools` já pôs UserValves em `__user__`.
        call_params = {
            "__messages__": extra_params.get("__messages__", []),
            "__files__": (extra_params.get("__metadata__") or {}).get("files") or [],
        }
        for key in ("__event_emitter__", "__event_call__"):
            if extra_params.get(key) is not None:
                call_params[key] = extra_params[key]
        if isinstance(extra_params.get("__metadata__"), dict):
            call_params["__metadata__"] = extra_params["__metadata__"]

        try:
            fn = tool.get("callable")
            if fn is None:
                return {"tool_call_id": tc_id, "content": f"Ferramenta sem callable: {name}"}
            if get_updated_tool_function is not None:
                fn = await _await(get_updated_tool_function(function=fn, extra_params=call_params))
            tool_result = await _await(fn(**params))
        except Exception as exc:
            log.warning("%s ferramenta '%s' falhou (%s)", LOG_PREFIX, name, type(exc).__name__)
            return {"tool_call_id": tc_id, "content": f"Error: {str(exc)[:500]}"}

        try:
            payload, _files, _embeds = await process_tool_result_compat(
                extra_params.get("__request__"), name, tool_result, tool.get("type", "") or "",
                direct_tool=False, metadata=extra_params.get("__metadata__"),
                user=extra_params.get("__user__"),
            )
        except Exception as exc:
            log.warning("%s pós-processamento de '%s' falhou (%s)", LOG_PREFIX, name, type(exc).__name__)
            return {"tool_call_id": tc_id, "content": f"Error: {str(exc)[:500]}"}

        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        if self.valves.DEBUG:
            log.info("%s ferramenta '%s' ok (%d chars)", LOG_PREFIX, name, len(text))
        text = truncate_text(text, self.valves.MAX_TOOL_RESULT_CHARS, self.valves.TRUNCATE_TOOL_RESULTS)
        return {"tool_call_id": tc_id, "content": text}

    # ---------------------------------------------------------------- loop do agente
    @staticmethod
    def _form_data(model_id: str, messages: List[dict], tools_param: Optional[list], iteration: int) -> dict:
        form_data = {
            "model": model_id, "messages": messages, "stream": False,
            "metadata": {"task": "flex_sub_agent", "sub_agent_iteration": iteration},
        }
        if tools_param:
            form_data["tools"] = tools_param
        return form_data

    async def _call_model(self, ctx: dict, form_data: dict):
        user_model = ctx["user"] or _UserShim({"id": "", "role": "user"})
        return await _await(generate_chat_completion(
            request=ctx["request"], form_data=form_data, user=user_model, bypass_filter=True
        ))

    async def _run_loop(self, ctx: dict, model_id: str, messages: List[dict], tools_dict: dict,
                        max_iterations: int, label: str = "subagente") -> str:
        if generate_chat_completion is None:
            return "Erro: helper generate_chat_completion indisponível neste core."
        emitter, extra_params = ctx["emitter"], ctx["extra_params"]
        tools_param = [
            {"type": "function", "function": t["spec"]} for t in tools_dict.values()
            if isinstance(t.get("spec"), dict) and t["spec"].get("name")
        ]

        iteration = 0
        while max_iterations == 0 or iteration < max_iterations:
            iteration += 1
            limit_text = "∞" if max_iterations == 0 else str(max_iterations)
            await emit_status(emitter, f"{label}: iteração {iteration}/{limit_text}")

            payload_messages = copy.deepcopy(messages)
            payload_messages.append({
                "role": self.valves.ITERATION_NOTE_ROLE,
                "content": f"[Iteração {iteration}/{limit_text}] Conclua a tarefa; responda em texto "
                "final quando não houver mais ferramentas a chamar.",
            })
            if self.valves.ENABLE_CONTEXT_COMPACTION:
                payload_messages = self._compact_messages(payload_messages, model_id)
            form_data = self._form_data(model_id, payload_messages, tools_param, iteration)
            if self.valves.DEBUG:
                log.info("%s iter=%d model=%s tools=%d", LOG_PREFIX, iteration, model_id, len(tools_param))

            try:
                resp = await self._call_model(ctx, form_data)
            except Exception as exc:
                log.warning("%s chamada ao modelo falhou (%s)", LOG_PREFIX, type(exc).__name__)
                return f"Erro ao chamar o modelo {model_id}: {str(exc)[:500]}"
            if not isinstance(resp, dict):
                return f"Resposta inesperada do modelo: {type(resp).__name__}: {str(resp)[:300]}"
            choices = resp.get("choices") or []
            if not choices:
                detail = resp.get("error") or resp.get("detail") or ""
                return f"Sem resposta do modelo {model_id}. {str(detail)[:300]}".strip()

            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            tool_calls = self._normalize_tool_calls(msg.get("tool_calls"))
            if not tool_calls:
                await emit_status(emitter, f"{label}: concluído em {iteration} iteração(ões)", done=True)
                return content or ""
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            for tc in tool_calls:
                result = await self._execute_tool_call(tc, tools_dict, extra_params)
                messages.append(
                    {"role": "tool", "tool_call_id": result["tool_call_id"], "content": result["content"]}
                )

        # Esgotou as iterações: UMA última chamada sem tools para forçar a resposta final.
        await emit_status(emitter, f"{label}: limite de iterações atingido, sintetizando resposta", done=True)
        forced = copy.deepcopy(messages)
        forced.append({
            "role": "user",
            "content": "Limite de iterações atingido. Não chame mais ferramentas. "
            "Responda agora com o melhor resultado obtido e o que ficou pendente.",
        })
        try:
            resp = await self._call_model(ctx, {"model": model_id, "messages": forced, "stream": False})
        except Exception as exc:
            return f"Limite de iterações atingido e a síntese final falhou: {str(exc)[:300]}"
        if isinstance(resp, dict):
            choices = resp.get("choices") or []
            if choices:
                return (choices[0].get("message") or {}).get("content") or "Limite de iterações atingido."
        return "Limite de iterações atingido."

    # ---------------------------------------------------------------- compactação
    def _compact_messages(self, messages: List[dict], model_id: str) -> List[dict]:
        """Compactação determinística: substitui resultados de ferramenta antigos por stubs."""
        try:
            import tiktoken

            try:
                encoder = tiktoken.encoding_for_model(model_id)
            except Exception:
                encoder = tiktoken.get_encoding("cl100k_base")
            total = sum(len(encoder.encode(str(m.get("content") or ""))) for m in messages)
        except Exception as exc:
            log.debug("%s compactação ignorada (%s)", LOG_PREFIX, type(exc).__name__)
            return messages
        if total <= self.valves.CONTEXT_COMPACTION_TOKEN_THRESHOLD:
            return messages
        compacted = list(messages)
        tool_indices = [i for i, m in enumerate(compacted) if m.get("role") == "tool"]
        for i in tool_indices[:-3]:
            content = str(compacted[i].get("content") or "")
            if len(content) > 400:
                compacted[i] = {
                    **compacted[i], "content": f"[resultado antigo omitido: {len(content)} caracteres]"
                }
        log.info("%s contexto compactado (%d stubs)", LOG_PREFIX, max(len(tool_indices) - 3, 0))
        return compacted