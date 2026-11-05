import ast
import json
import types
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = [
    path for path in [
        *ROOT.glob('plugins/*/scripts/main.py'),
        *ROOT.glob('automations/*/tarball/main.py'),
    ] if '_assert_conversation_succeeded' in path.read_text()
]


def load_function(path, name, namespace):
    tree = ast.parse(path.read_text())
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(functions) == 1, (path, name)
    module = ast.fix_missing_locations(ast.Module(body=[functions[0]], type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace[name]


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.body).encode()


class PromptCompletionTests(unittest.TestCase):
    def test_errors_cannot_report_completed(self):
        for path in PROMPTS:
            with self.subTest(path=path):
                events = []

                def urlopen(request, timeout):
                    self.assertEqual(timeout, 15)
                    self.assertIn('kind=ConversationErrorEvent', request.full_url)
                    return Response({'items': events})

                namespace = {'IS_LOCAL_MODE': True, 'agent_server_url': 'http://agent',
                    'session_key': 'key', 'json': json,
                    'urllib': types.SimpleNamespace(request=types.SimpleNamespace(
                        Request=urllib.request.Request, urlopen=urlopen))}
                check = load_function(path, '_assert_conversation_succeeded', namespace)
                conversation = types.SimpleNamespace(id='test', state=types.SimpleNamespace(execution_status='finished'))
                check(conversation)
                conversation.state.execution_status = 'stuck'
                with self.assertRaisesRegex(RuntimeError, 'stuck'):
                    check(conversation)
                conversation.state.execution_status = 'error'
                with self.assertRaisesRegex(RuntimeError, 'error'):
                    check(conversation)
                conversation.state.execution_status = 'finished'
                events.append({'kind': 'ConversationErrorEvent', 'code': 'fatal', 'detail': 'failed'})
                with self.assertRaisesRegex(RuntimeError, 'fatal'):
                    check(conversation)


if __name__ == '__main__':
    unittest.main()
