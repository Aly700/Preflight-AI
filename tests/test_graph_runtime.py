"""Graph topology and request configuration; no live model, MCP, or tracing service."""

from dataclasses import replace
import importlib
import json
import unittest
from unittest.mock import patch

from test_agent_runtime import SETTINGS, FakeModel, FakeTools, claims

from backend import runtime
from backend.agent_runtime import Loop
from backend.model_client import ToolCall, Turn


class GraphRuntime(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.graph_runtime'), 'graph runtime must exist')
        self.runtime = importlib.import_module('backend.graph_runtime')

    def test_compiled_graph_nodes_and_edges(self):
        drawing = self.runtime.graph.get_graph()
        self.assertEqual(
            set(drawing.nodes),
            {
                '__start__',
                'start',
                'model',
                'tools',
                'wrap_up',
                'finish',
                'claims',
                '__end__',
            },
        )
        self.assertEqual(
            {(e.source, e.target, e.conditional) for e in drawing.edges},
            {
                ('__start__', 'start', False),
                ('start', 'model', False),
                ('model', 'tools', True),
                ('model', 'finish', True),
                ('tools', 'wrap_up', True),
                ('tools', 'model', True),
                ('wrap_up', 'finish', False),
                ('finish', 'claims', True),
                ('finish', '__end__', True),
                ('claims', '__end__', False),
            },
        )
        self.assertEqual(self.runtime.mermaid(), drawing.draw_mermaid())

    async def test_recursion_limit_scales_with_rounds_and_loop_stays_out_of_state(self):
        graph = self.runtime.graph
        invoke = graph.ainvoke
        limits = []

        async def inspect_invoke(state, config):
            json.dumps(state)
            self.assertNotIn('loop', state)
            loop = config['configurable']['loop']
            self.assertIsInstance(loop, Loop)
            limits.append(config['recursion_limit'])
            self.assertGreaterEqual(config['recursion_limit'], 2 * loop.settings.max_tool_rounds + 4)
            result = await invoke(state, config)
            json.dumps(result)
            self.assertNotIn('loop', result)
            return result

        with patch.object(graph, 'ainvoke', side_effect=inspect_invoke):
            for rounds in (1, 6, 20):
                with self.subTest(rounds=rounds):
                    model = FakeModel(
                        [
                            *[Turn('', [ToolCall(f't{i}', 'list_open_issues', {'limit': 1})]) for i in range(rounds)],
                            Turn('The records include ISSUE-0001.'),
                            claims(['ISSUE-0001']),
                        ]
                    )
                    events = [
                        event
                        async for event in self.runtime.stream_answer(
                            'Summary',
                            replace(SETTINGS, runtime='langgraph', max_tool_rounds=rounds, max_tool_calls=20),
                            model_client=model,
                            tools=FakeTools(),
                        )
                    ]
                    self.assertEqual(events[-1]['event'], 'done')
                    self.assertEqual(events[-1]['data']['rounds'], rounds)
                    self.assertTrue(events[-1]['data']['verified'])
                    self.assertIs(self.runtime.graph, graph)
        self.assertLess(limits[0], limits[1])
        self.assertLess(limits[1], limits[2])

    async def test_runtime_dispatches_to_langgraph(self):
        settings = replace(SETTINGS, runtime='langgraph')
        expected = [{'event': 'delta', 'data': {'text': 'Answer.'}}, {'event': 'done', 'data': {}}]

        async def answer(question, actual_settings):
            self.assertEqual(question, 'Question')
            self.assertIs(actual_settings, settings)
            for event in expected:
                yield event

        with (
            patch.object(self.runtime, 'stream_answer', side_effect=answer) as selected,
            patch('backend.copilot_chat.stream_answer', side_effect=AssertionError('wrong runtime')),
            patch('backend.agent_runtime.stream_answer', side_effect=AssertionError('wrong runtime')),
        ):
            self.assertEqual([event async for event in runtime.stream_answer('Question', settings)], expected)
            selected.assert_called_once_with('Question', settings)

    async def test_graph_invocation_explicitly_disables_tracing(self):
        from langsmith.run_helpers import get_tracing_context

        invoke = self.runtime.graph.ainvoke

        async def inspect_invoke(state, config):
            self.assertIs(get_tracing_context()['enabled'], False)
            return await invoke(state, config)

        with patch.object(self.runtime.graph, 'ainvoke', side_effect=inspect_invoke):
            events = [
                event
                async for event in self.runtime.stream_answer(
                    'Summary', SETTINGS, model_client=FakeModel([Turn('Answer.')]), tools=FakeTools()
                )
            ]
        self.assertEqual(events[-1]['event'], 'done')


if __name__ == '__main__':
    unittest.main()
