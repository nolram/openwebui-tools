"""Testes unitários offline do Flex Sub Agent.

Rodar com:
    python3 -m unittest discover -s flex_sub_agent -v

Não requer Open WebUI: os helpers do core são substituídos por mocks.
Nenhum literal de ambiente (host, namespace, credencial) aparece aqui — a varredura de
segredos do repositório varre esta pasta também.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).with_name("flex_sub_agent.py")
_spec = importlib.util.spec_from_file_location("flex_sub_agent_under_test", MODULE_PATH)
fsa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fsa)


def run(coro):
    """Executa uma corrotina em um event loop novo (isolado por teste)."""
    return asyncio.run(coro)


async def _async_value(value):
    return value


class _FakeRequest:
    """Request mínimo: `scope` dict + `app` com `state.MODELS`."""

    def __init__(self, models=None):
        self.scope = {"state": {}}
        self.state = mock.Mock()
        self.app = mock.Mock()
        self.app.state.MODELS = models or {}


def _make_tool(name, func, properties=None, tool_type=""):
    return {
        "spec": {
            "name": name,
            "description": f"ferramenta {name}",
            "parameters": {"type": "object", "properties": properties or {}},
        },
        "callable": func,
        "type": tool_type,
    }


class ProcessToolResultCompatTests(unittest.TestCase):
    """Item 1: o bug original do fork — `direct_tool` passado incondicionalmente."""

    def test_core_without_direct_tool_does_not_receive_kwarg(self):
        calls = {}

        async def core(request, name, result, tool_type, metadata=None, user=None):
            calls["args"] = (name, result, tool_type, metadata, user)
            return {"ok": True}

        with mock.patch.object(fsa, "process_tool_result_core", core):
            out = run(fsa.process_tool_result_compat(None, "t", {"a": 1}, "builtin"))

        self.assertEqual(len(out), 3)
        self.assertIn("ok", out[0])
        self.assertEqual(calls["args"][0], "t")
        self.assertEqual(out[1:], ([], []))

    def test_core_with_direct_tool_receives_kwarg(self):
        received = {}

        # Assinatura COM direct_tool — o caso do upstream.
        async def core(request, name, result, tool_type, metadata=None, user=None, direct_tool=False):
            received["direct_tool"] = direct_tool
            return ("payload", [], [])

        with mock.patch.object(fsa, "process_tool_result_core", core):
            out = run(fsa.process_tool_result_compat(None, "t", "r", "builtin", direct_tool=True))

        self.assertTrue(received["direct_tool"])
        self.assertEqual(out, ("payload", [], []))

    def test_sync_core_is_awaited_correctly(self):
        def core(request, name, result, tool_type, metadata=None, user=None):
            return ("sync", [], [])

        with mock.patch.object(fsa, "process_tool_result_core", core):
            out = run(fsa.process_tool_result_compat(None, "t", "r", "builtin"))
        self.assertEqual(out, ("sync", [], []))

    def test_non_tuple_return_is_normalized(self):
        async def core(request, name, result, tool_type, metadata=None, user=None):
            return 12345

        with mock.patch.object(fsa, "process_tool_result_core", core):
            out = run(fsa.process_tool_result_compat(None, "t", "r", "builtin"))
        self.assertEqual(out, ("12345", [], []))

    def test_missing_core_falls_back_to_string(self):
        with mock.patch.object(fsa, "process_tool_result_core", None):
            payload, files, embeds = run(
                fsa.process_tool_result_compat(None, "t", {"x": 1}, "builtin")
            )
        self.assertIn('"x"', payload)
        self.assertEqual((files, embeds), ([], []))


class AwaitUtilTests(unittest.TestCase):
    """Item 2: `_maybe_await`/`_await` com valor síncrono e assíncrono."""

    def test_maybe_await_sync_value(self):
        """Valor síncrono sai como aguardável que devolve o próprio valor."""
        awaited = fsa._maybe_await(7)
        self.assertTrue(hasattr(awaited, "__await__"))

        async def scenario():
            self.assertEqual(await awaited, 7)

        run(scenario())

    def test_maybe_await_async_value(self):
        async def scenario():
            awaited = fsa._maybe_await(_async_value("ok"))
            self.assertTrue(hasattr(awaited, "__await__"))
            self.assertEqual(await awaited, "ok")

        run(scenario())

    def test_await_helper_on_both(self):
        async def scenario():
            self.assertEqual(await fsa._await(5), 5)
            self.assertEqual(await fsa._await(_async_value("z")), "z")

        run(scenario())


class ExecuteToolCallTests(unittest.TestCase):
    """Item 3: argumentos válidos/inválidos, poda, ferramenta inexistente, exceção."""

    def setUp(self):
        self.tools = fsa.Tools()
        self.extra = {"__messages__": [], "__metadata__": {"files": []}, "__user__": {"id": "u"}}

    def _call(self, tc, tools):
        return run(self.tools._execute_tool_call(tc, tools, self.extra))

    def test_valid_arguments(self):
        def soma(a, b):
            return {"total": a + b}

        tools = {"soma": _make_tool("soma", soma, {"a": {}, "b": {}})}
        tc = {"id": "1", "function": {"name": "soma", "arguments": '{"a": 2, "b": 3}'}}
        out = self._call(tc, tools)
        self.assertEqual(out["tool_call_id"], "1")
        self.assertIn("5", out["content"])

    def test_invalid_non_json_arguments(self):
        tools = {"soma": _make_tool("soma", lambda: None, {"a": {}})}
        tc = {"id": "2", "function": {"name": "soma", "arguments": "{isto nao e json"}}
        out = self._call(tc, tools)
        self.assertIn("Erro ao analisar argumentos", out["content"])

    def test_arguments_not_object(self):
        tools = {"soma": _make_tool("soma", lambda: None, {"a": {}})}
        tc = {"id": "3", "function": {"name": "soma", "arguments": "[1, 2]"}}
        out = self._call(tc, tools)
        self.assertIn("objeto JSON", out["content"])

    def test_unknown_tool(self):
        tc = {"id": "4", "function": {"name": "inexistente", "arguments": "{}"}}
        out = self._call(tc, {})
        self.assertIn("Ferramenta desconhecida", out["content"])

    def test_extra_keys_are_pruned_by_schema(self):
        seen = {}

        def recebe(a):
            seen.update({"got": a})
            return "ok"

        tools = {"recebe": _make_tool("recebe", recebe, {"a": {}})}
        tc = {"id": "5", "function": {"name": "recebe", "arguments": '{"a": 1, "injetado": "x"}'}}
        out = self._call(tc, tools)
        self.assertEqual(seen["got"], 1)
        self.assertEqual(out["content"], "ok")

    def test_tool_raising_exception_returns_content(self):
        def explode():
            raise ValueError("falha proposital")

        tools = {"explode": _make_tool("explode", explode, {})}
        tc = {"id": "6", "function": {"name": "explode", "arguments": "{}"}}
        out = self._call(tc, tools)
        self.assertIn("Error:", out["content"])
        self.assertIn("falha proposital", out["content"])

    def test_result_is_truncated(self):
        self.tools.valves.MAX_TOOL_RESULT_CHARS = 1000

        def grande():
            return "A" * 5000 + "Z" * 100

        tools = {"grande": _make_tool("grande", grande, {})}
        tc = {"id": "7", "function": {"name": "grande", "arguments": "{}"}}
        out = self._call(tc, tools)
        self.assertIn("omitido", out["content"])
        self.assertLess(len(out["content"]), 1200)

    def test_non_dict_arguments_are_accepted(self):
        def soma(a):
            return a + 1

        tools = {"soma": _make_tool("soma", soma, {"a": {}})}
        tc = {"id": "8", "function": {"name": "soma", "arguments": {"a": 1}}}
        out = self._call(tc, tools)
        self.assertEqual(out["content"], "2")


class ModelResolutionTests(unittest.TestCase):
    """Item 4: precedência do modelo e allowlist."""

    def setUp(self):
        self.tools = fsa.Tools()

    def _resolve(self, requested=None, metadata=None, model_dict=None):
        return run(self.tools._resolve_model(requested, metadata or {}, model_dict, _FakeRequest()))

    def test_explicit_model_wins(self):
        model_id, err = self._resolve(
            "explicito", {"model": {"id": "chat-pai"}}, {"id": "dunder-model"}
        )
        self.assertIsNone(err)
        self.assertEqual(model_id, "explicito")

    def test_default_valve_wins_over_parent_chat(self):
        self.tools.valves.DEFAULT_MODEL = "valve-model"
        model_id, err = self._resolve(None, {"model": {"id": "chat-pai"}}, {"id": "dunder"})
        self.assertIsNone(err)
        self.assertEqual(model_id, "valve-model")

    def test_parent_chat_model_used_when_nothing_else(self):
        model_id, err = self._resolve(None, {"model": {"id": "chat-pai"}}, {"id": "dunder"})
        self.assertIsNone(err)
        self.assertEqual(model_id, "chat-pai")

    def test_dunder_model_is_used_last(self):
        model_id, err = self._resolve(None, {}, {"id": "dunder"})
        self.assertIsNone(err)
        self.assertEqual(model_id, "dunder")

    def test_no_model_available(self):
        model_id, err = self._resolve(None, {}, None)
        self.assertIsNone(model_id)
        self.assertEqual(err["error"], "MODEL_NOT_RESOLVED")

    def test_allowlist_wildcard(self):
        self.tools.valves.ALLOWED_MODEL_IDS = "qwen*"
        self.assertTrue(self.tools._model_allowed("qwen3.8:27b-agent"))
        self.assertFalse(self.tools._model_allowed("glm-5.3"))

    def test_allowlist_star_allows_everything(self):
        self.tools.valves.ALLOWED_MODEL_IDS = "*"
        self.assertTrue(self.tools._model_allowed("qualquer-modelo"))

    def test_allowlist_empty_allows_everything(self):
        self.tools.valves.ALLOWED_MODEL_IDS = ""
        self.assertTrue(self.tools._model_allowed("qualquer-modelo"))

    def test_allowlist_exact_list(self):
        self.tools.valves.ALLOWED_MODEL_IDS = "glm-5.3, qwen3.7-plus"
        self.assertTrue(self.tools._model_allowed("glm-5.3"))
        self.assertTrue(self.tools._model_allowed("qwen3.7-plus"))
        self.assertFalse(self.tools._model_allowed("qwen3.8:27b-agent"))

    def test_blocked_model_returns_error_not_silent_switch(self):
        self.tools.valves.ALLOWED_MODEL_IDS = "qwen*"
        model_id, err = self._resolve("glm-5.3", {}, None)
        self.assertIsNone(model_id)
        self.assertEqual(err["error"], "MODEL_NOT_ALLOWED")
        self.assertEqual(err["requested"], "glm-5.3")
        self.assertEqual(err["allowed"], ["qwen*"])


class ToolCallsNormalizationTests(unittest.TestCase):
    """Item 5: `arguments` como dict; entradas não-dict são descartadas."""

    def test_dict_arguments_are_serialized(self):
        raw = [
            {"id": "a", "function": {"name": "f", "arguments": {"x": 1}}},
            "lixo",
            {"id": "b"},
            {"id": "c", "function": {"name": "g", "arguments": "{}"}},
        ]
        out = fsa.Tools._normalize_tool_calls(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(json.loads(out[0]["function"]["arguments"]), {"x": 1})
        self.assertEqual(out[1]["function"]["arguments"], "{}")

    def test_none_returns_empty(self):
        self.assertEqual(fsa.Tools._normalize_tool_calls(None), [])


class CollectToolsTests(unittest.TestCase):
    """Itens 6 e 7: normalização do terminal e anti-recursão."""

    def setUp(self):
        self.tools = fsa.Tools()

    def _ctx(self, metadata):
        return self.tools._agent_context(
            _FakeRequest(), {"id": "u1", "role": "user"}, {"id": "m"}, metadata,
            None, None, "c1", "msg1", None, [],
        )

    def test_visible_tool_ids_removes_self_unconditionally(self):
        visible = self.tools._visible_tool_ids(
            {"tool_ids": ["flex_sub_agent", "sub_agent", "outra_tool", "mais_uma"]}, "flex_sub_agent"
        )
        self.assertEqual(visible, ["outra_tool", "mais_uma"])

    def test_visible_tool_ids_ignores_valve(self):
        self.tools.valves.EXCLUDED_TOOL_IDS = ""
        self.assertEqual(self.tools._visible_tool_ids({"tool_ids": ["sub_agent"]}, None), [])

    def test_visible_tool_ids_removes_dynamic_own_id(self):
        visible = self.tools._visible_tool_ids({"tool_ids": ["id_dinamico", "ok"]}, "id_dinamico")
        self.assertEqual(visible, ["ok"])

    def test_collect_tools_merges_and_drops_self_names(self):
        async def fake_get_tools(request, tool_ids, user, extra):
            return {"minha_tool": _make_tool("minha_tool", lambda: "ok")}

        async def fake_get_builtin_tools(**kwargs):
            return {
                "get_current_timestamp": _make_tool("get_current_timestamp", lambda: 1),
                "run_sub_agent": _make_tool("run_sub_agent", lambda: "recursao"),
                "run_parallel_sub_agents": _make_tool("run_parallel_sub_agents", lambda: 1),
                "notify": _make_tool("notify", lambda: 1),
            }

        with mock.patch.object(fsa, "get_tools", fake_get_tools), mock.patch.object(
            fsa, "get_builtin_tools", fake_get_builtin_tools
        ):
            merged, prompt = run(
                self.tools._collect_tools(self._ctx({"tool_ids": ["minha_tool"]}), "flex_sub_agent")
            )

        self.assertIn("minha_tool", merged)
        self.assertIn("get_current_timestamp", merged)
        self.assertNotIn("run_sub_agent", merged)
        self.assertNotIn("run_parallel_sub_agents", merged)
        self.assertNotIn("notify", merged)  # categoria desligada por padrão
        self.assertIsNone(prompt)

    def test_terminal_tools_tuple_is_unpacked(self):
        async def fake_get_terminal_tools(request, terminal_id, user, extra_params):
            return (
                {"term_exec": _make_tool("term_exec", lambda: "ls")},
                "Use o terminal com cuidado.",
            )

        async def no_builtin(**kwargs):
            return {}

        with mock.patch.object(fsa, "get_terminal_tools", fake_get_terminal_tools), mock.patch.object(
            fsa, "get_builtin_tools", no_builtin
        ), mock.patch.object(fsa, "get_tools", mock.AsyncMock(return_value={})):
            merged, prompt = run(
                self.tools._collect_tools(
                    self._ctx({"terminal_id": "t1", "tool_ids": []}), "flex_sub_agent"
                )
            )

        self.assertIn("term_exec", merged)
        self.assertEqual(prompt, "Use o terminal com cuidado.")

    def test_terminal_tools_plain_dict_is_accepted(self):
        async def fake_get_terminal_tools(request, terminal_id, user, extra_params):
            return {"term_exec": _make_tool("term_exec", lambda: "ls")}

        async def no_builtin(**kwargs):
            return {}

        with mock.patch.object(fsa, "get_terminal_tools", fake_get_terminal_tools), mock.patch.object(
            fsa, "get_builtin_tools", no_builtin
        ), mock.patch.object(fsa, "get_tools", mock.AsyncMock(return_value={})):
            merged, prompt = run(
                self.tools._collect_tools(
                    self._ctx({"terminal_id": "t1", "tool_ids": []}), "flex_sub_agent"
                )
            )

        self.assertIn("term_exec", merged)
        self.assertIsNone(prompt)

    def test_missing_request_raises_clear_error(self):
        ctx = self._ctx({})
        ctx["request"] = None
        with self.assertRaises(RuntimeError):
            run(self.tools._collect_tools(ctx, None))


class ParallelTests(unittest.TestCase):
    """Item 8: limite de MAX_PARALLEL_AGENTS e ordem dos resultados."""

    def setUp(self):
        self.tools = fsa.Tools()
        self.tools.valves.MAX_PARALLEL_AGENTS = 3
        self.tools.valves.DEFAULT_MODEL = "modelo-padrao"

    def test_limit_exceeded_returns_error_json(self):
        tasks = [fsa.SubAgentTaskItem(description=f"t{i}", prompt="p") for i in range(4)]
        out = json.loads(run(self.tools.run_parallel_sub_agents(tasks=tasks)))
        self.assertIn("MAX_PARALLEL_AGENTS", out["error"])

    def test_empty_tasks_returns_error(self):
        out = json.loads(run(self.tools.run_parallel_sub_agents(tasks=[])))
        self.assertIn("error", out)

    def test_results_preserve_order_and_per_task_model(self):
        delays = {"t0": 0.05, "t1": 0.01, "t2": 0.02}
        seen_models = []

        async def fake_run_one(ctx, description, prompt, model, max_iterations, own_tool_id):
            # Mesma cadeia do código real: explícito -> DEFAULT_MODEL -> chat pai.
            resolved, _err = await self.tools._resolve_model(
                model, ctx["metadata"], {"id": "modelo-do-chat"}, _FakeRequest()
            )
            seen_models.append((description, resolved))
            await asyncio.sleep(delays[description])
            return {"description": description, "model": resolved, "result": f"r-{description}"}

        tasks = [
            fsa.SubAgentTaskItem(description="t0", prompt="p0", model="modelo-a"),
            fsa.SubAgentTaskItem(description="t1", prompt="p1"),
            fsa.SubAgentTaskItem(description="t2", prompt="p2", model="modelo-b"),
        ]
        with mock.patch.object(self.tools, "_run_one", fake_run_one):
            out = json.loads(run(self.tools.run_parallel_sub_agents(tasks=tasks)))

        self.assertEqual(out["total"], 3)
        self.assertEqual([r["description"] for r in out["results"]], ["t0", "t1", "t2"])
        self.assertEqual(out["results"][2]["result"], "r-t2")
        models = dict(seen_models)
        self.assertEqual(models["t0"], "modelo-a")
        self.assertEqual(models["t1"], "modelo-padrao")
        self.assertEqual(models["t2"], "modelo-b")

    def test_tasks_accept_plain_dicts(self):
        async def fake_run_one(ctx, description, prompt, model, max_iterations, own_tool_id):
            return {"description": description, "model": model, "result": "ok"}

        tasks = [{"description": "dict", "prompt": "p"}]
        with mock.patch.object(self.tools, "_run_one", fake_run_one):
            out = json.loads(run(self.tools.run_parallel_sub_agents(tasks=tasks)))
        self.assertEqual(out["results"][0]["description"], "dict")

    def test_task_exception_is_isolated(self):
        async def fake_run_one(ctx, description, prompt, model, max_iterations, own_tool_id):
            if description == "boom":
                raise RuntimeError("estourou")
            return {"description": description, "model": model, "result": "ok"}

        tasks = [
            fsa.SubAgentTaskItem(description="boom", prompt="p"),
            fsa.SubAgentTaskItem(description="fina", prompt="p"),
        ]
        with mock.patch.object(self.tools, "_run_one", fake_run_one):
            out = json.loads(run(self.tools.run_parallel_sub_agents(tasks=tasks)))

        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["succeeded"], 1)
        self.assertIn("estourou", out["results"][0]["error"])
        self.assertEqual(out["results"][1]["result"], "ok")

    def test_allowlist_error_surfaces_per_task(self):
        self.tools.valves.ALLOWED_MODEL_IDS = "qwen*"

        async def fake_run_one(ctx, description, prompt, model, max_iterations, own_tool_id):
            return await self.tools._run_one(ctx, description, prompt, model, max_iterations, own_tool_id)

        async def no_collect(*args, **kwargs):
            raise AssertionError("não deveria coletar ferramentas quando o modelo é barrado")

        tasks = [fsa.SubAgentTaskItem(description="x", prompt="p", model="glm-5.3")]
        with mock.patch.object(self.tools, "_collect_tools", no_collect):
            out = json.loads(run(self.tools.run_parallel_sub_agents(tasks=tasks)))

        self.assertEqual(out["results"][0]["error"]["error"], "MODEL_NOT_ALLOWED")


class TruncationTests(unittest.TestCase):
    """Item 9: truncamento preserva cabeça e cauda."""

    def test_head_and_tail_preserved(self):
        text = "INICIO" + ("x" * 5000) + "FIM"
        out = fsa.truncate_text(text, 1000, True)
        self.assertTrue(out.startswith("INICIO"))
        self.assertTrue(out.endswith("FIM"))
        self.assertIn("omitido", out)
        self.assertLess(len(out), 1200)

    def test_short_text_untouched(self):
        self.assertEqual(fsa.truncate_text("curto", 1000, True), "curto")

    def test_disabled_returns_original(self):
        text = "y" * 5000
        self.assertEqual(fsa.truncate_text(text, 100, False), text)


class LoopTests(unittest.TestCase):
    """Cobertura extra do loop: parada, execução de ferramenta e limite de iterações."""

    def setUp(self):
        self.tools = fsa.Tools()

    def _ctx(self):
        return self.tools._agent_context(
            _FakeRequest(), {"id": "u1", "role": "user"}, {"id": "m"}, {}, None, None,
            None, None, None, [],
        )

    def _patch_helpers(self):
        return mock.patch.object(fsa, "generate_chat_completion", lambda **kwargs: None)

    def test_loop_stops_when_no_tool_calls(self):
        responses = [{"choices": [{"message": {"content": "pronto"}}]}]

        async def fake_call(ctx, form_data):
            return responses.pop(0)

        with self._patch_helpers(), mock.patch.object(self.tools, "_call_model", fake_call):
            out = run(self.tools._run_loop(self._ctx(), "m", [{"role": "user", "content": "x"}], {}, 5))
        self.assertEqual(out, "pronto")

    def test_loop_executes_tool_then_finishes(self):
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "ping", "arguments": "{}"}}
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "feito"}}]},
        ]

        async def fake_call(ctx, form_data):
            return responses.pop(0)

        async def ping():
            return "pong"

        tools = {"ping": _make_tool("ping", ping, {})}
        with self._patch_helpers(), mock.patch.object(self.tools, "_call_model", fake_call):
            out = run(
                self.tools._run_loop(self._ctx(), "m", [{"role": "user", "content": "x"}], tools, 5)
            )
        self.assertEqual(out, "feito")

    def test_iteration_limit_forces_final_call_without_tools(self):
        calls = []

        async def fake_call(ctx, form_data):
            calls.append(form_data)
            if "tools" in form_data:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {"id": "c", "function": {"name": "ping", "arguments": "{}"}}
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "sintese final"}}]}

        async def ping():
            return "pong"

        tools = {"ping": _make_tool("ping", ping, {})}
        with self._patch_helpers(), mock.patch.object(self.tools, "_call_model", fake_call):
            out = run(
                self.tools._run_loop(self._ctx(), "m", [{"role": "user", "content": "x"}], tools, 2)
            )

        self.assertEqual(out, "sintese final")
        self.assertEqual(len(calls), 3)  # 2 iterações com tools + 1 síntese sem tools
        self.assertNotIn("tools", calls[-1])

    def test_malformed_response_is_reported_as_text(self):
        async def fake_call(ctx, form_data):
            return "não é dict"

        with self._patch_helpers(), mock.patch.object(self.tools, "_call_model", fake_call):
            out = run(
                self.tools._run_loop(self._ctx(), "m", [{"role": "user", "content": "x"}], {}, 1)
            )
        self.assertIn("Resposta inesperada", out)

    def test_missing_helper_reports_clean_error(self):
        with mock.patch.object(fsa, "generate_chat_completion", None):
            out = run(
                self.tools._run_loop(self._ctx(), "m", [{"role": "user", "content": "x"}], {}, 1)
            )
        self.assertIn("generate_chat_completion", out)


class SafetyTests(unittest.TestCase):
    """R3: nenhum literal sensível no código da ferramenta.

    Os padrões são montados por concatenação para não aparecerem literalmente neste arquivo
    (a varredura de segredos do repositório varre a pasta inteira).
    """

    FORBIDDEN = (
        "svc" + ".cluster.local",
        "gh" + "cr.io",
        "ai" + "-tools",
        "token" + ".txt",
        "Bear" + "er ",
        "BEGIN" + " RSA PRIVATE KEY",
        "OPENAI" + "_API_KEY",
    )

    def test_source_has_no_environment_secrets(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        hits = [token for token in self.FORBIDDEN if token in source]
        self.assertEqual(hits, [], f"literais sensíveis encontrados: {len(hits)}")

    def test_source_is_under_line_limit_and_not_generated(self):
        lines = MODULE_PATH.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 900)
        self.assertFalse(any("GENERATED FILE" in line for line in lines))


if __name__ == "__main__":
    unittest.main()