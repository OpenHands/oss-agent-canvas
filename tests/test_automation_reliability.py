import ast
import json
import types
import unittest
import urllib.request
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
REVIEWERS = sorted(
    path for path in ROOT.glob('**/agent_conversation.py')
    if 'github-pr-reviewer' in str(path)
)
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


class ReviewerReliabilityTests(unittest.TestCase):
    def test_archived_subject_rotates_without_losing_dedupe_key(self):
        for path in REVIEWERS:
            with self.subTest(path=path):
                store = {}
                created = []
                steps = []
                initial = uuid5(NAMESPACE_URL, 'automation:repo:pr:1')
                key = f'agent-conversation-{initial}'
                store[key] = {'conversation_id': str(initial), 'delivery': 'sha1', 'head': 'sha1'}

                class Conversation:
                    state = types.SimpleNamespace(execution_status='running')

                    def __init__(self, id):
                        self.id = id

                    def update_secrets(self, secrets):
                        pass

                    def send_message(self, prompt):
                        steps.append(('message', prompt))

                    def run(self, *, blocking):
                        steps.append(('run', blocking))

                    def close(self):
                        pass

                class HTTPStatusError(Exception):
                    def __init__(self, status_code):
                        self.response = types.SimpleNamespace(status_code=status_code)

                class Remote:
                    @staticmethod
                    def attach(workspace, id, visualizer):
                        if id != initial and not created:
                            raise HTTPStatusError(404)
                        return Conversation(id)

                    @staticmethod
                    def create(workspace, request, visualizer):
                        created.append(request)
                        return Conversation(request.conversation_id)

                def archived(url, key, id):
                    return id == initial

                namespace = {
                    'uuid5': uuid5, 'UUID': UUID, 'NAMESPACE_URL': NAMESPACE_URL, 'Path': Path,
                    '_CONVERSATION_KEY_PREFIX': 'agent-conversation-',
                    '_kv_request': lambda key, method, value=None: (
                        store.get(key) if method == 'GET' else store.__setitem__(key, value)
                    ),
                    '_runtime_archived': archived, 'RemoteConversation': Remote,
                    'httpx': types.SimpleNamespace(HTTPStatusError=HTTPStatusError),
                    'ConversationExecutionStatus': types.SimpleNamespace(RUNNING='running', IDLE='idle', PAUSED='paused', ERROR='error'),
                    'LocalWorkspace': lambda **kwargs: kwargs,
                    'StartConversationRequest': lambda **kwargs: types.SimpleNamespace(**kwargs),
                    'SendMessageRequest': lambda **kwargs: kwargs,
                    'TextContent': lambda **kwargs: kwargs,
                    'automation_conversation_kwargs': lambda **kwargs: {},
                }
                deliver = load_function(path, 'deliver', namespace)
                dispatcher = types.SimpleNamespace(automation_id='automation', _workspace=object(),
                    agent_url='http://agent', api_key='key', profile_id=UUID(int=1), _secrets={})
                dispatcher._deliver_once = types.MethodType(load_function(path, '_deliver_once', namespace), dispatcher)
                path_kwargs = {}
                if 'github-pr-reviewer-events' in path.parts:
                    destination = Path('/workspace/staging/publish.py')
                    path_kwargs['publisher_path'] = destination
                    dispatcher._upload_publisher = lambda id, dest: steps.append(('upload', id, dest))
                result = deliver(dispatcher, 'repo:pr:1', 'sha1', 'review', head='sha1', **path_kwargs)
                replacement = uuid5(NAMESPACE_URL, f'{initial}:replacement')
                self.assertEqual(result['conversation_id'], str(replacement))
                self.assertEqual(result['disposition'], 'created')
                self.assertEqual(store[key]['conversation_id'], str(replacement))
                self.assertFalse(hasattr(created[0], "max_iterations"))
                if path_kwargs:
                    self.assertFalse(hasattr(created[0], 'initial_message'))
                    self.assertEqual(steps, [
                        ('upload', replacement, destination), ('message', 'review'), ('run', False)
                    ])
                again = deliver(dispatcher, 'repo:pr:1', 'sha1', 'review', head='sha1', **path_kwargs)
                self.assertEqual(again['disposition'], 'in_progress')
                self.assertEqual(len(created), 1)
                self.assertEqual(len(steps), 3 if path_kwargs else 0)

    def test_event_review_upload_failure_leaves_turn_unsent(self):
        path = ROOT / 'plugins/github-pr-reviewer-events/scripts/agent_conversation.py'
        steps = []

        def refuse_upload(id, destination):
            steps.append('upload')
            raise RuntimeError('upload rejected')

        class HTTPStatusError(Exception):
            response = types.SimpleNamespace(status_code=404)

        def not_found(*args, **kwargs):
            raise HTTPStatusError()

        conversation = types.SimpleNamespace(
            send_message=lambda prompt: steps.append('message'),
            run=lambda **kwargs: steps.append('run'),
            close=lambda: steps.append('close'),
            state=types.SimpleNamespace(execution_status='idle'),
            update_secrets=lambda secrets: steps.append('secrets'),
        )
        remote = types.SimpleNamespace(
            attach=not_found,
            create=lambda *args, **kwargs: conversation,
        )
        deliver = load_function(path, '_deliver_once', {
            'UUID': UUID, 'Path': Path, 'RemoteConversation': remote,
            'httpx': types.SimpleNamespace(HTTPStatusError=HTTPStatusError),
            'LocalWorkspace': lambda **kwargs: kwargs,
            'StartConversationRequest': lambda **kwargs: kwargs,
            'automation_conversation_kwargs': lambda **kwargs: {},
            'ConversationExecutionStatus': types.SimpleNamespace(RUNNING='running'),
        })
        dispatcher = types.SimpleNamespace(
            _workspace=object(), profile_id=UUID(int=1), _secrets={},
            _upload_publisher=refuse_upload,
        )
        for attach in (not_found, lambda *args, **kwargs: conversation):
            remote.attach = attach
            steps.clear()
            with self.assertRaisesRegex(RuntimeError, 'upload rejected'):
                deliver(dispatcher, UUID(int=2), 'review', None, False, False, Path('/workspace/publish.py'))
            self.assertEqual(steps, ['upload', 'close'])

    def test_410_during_secret_update_rotates_runtime(self):
        path = ROOT / 'plugins/github-pr-reviewer/scripts/agent_conversation.py'
        initial = uuid5(NAMESPACE_URL, 'automation:repo:pr:1')
        store = {f'agent-conversation-{initial}': {
            'conversation_id': str(initial), 'delivery': 'sha1', 'head': 'sha1',
        }}

        class HTTPStatusError(Exception):
            def __init__(self, code):
                self.response = types.SimpleNamespace(status_code=code)

        class Conversation:
            state = types.SimpleNamespace(execution_status='running')

            def __init__(self, id):
                self.id = id

            def update_secrets(self, secrets):
                if self.id == initial:
                    raise HTTPStatusError(410)

            def close(self):
                pass

        class Remote:
            @staticmethod
            def attach(workspace, id, visualizer):
                if id != initial:
                    raise HTTPStatusError(404)
                return Conversation(id)

            @staticmethod
            def create(workspace, request, visualizer):
                return Conversation(request.conversation_id)

        namespace = {
            'uuid5': uuid5, 'UUID': UUID, 'NAMESPACE_URL': NAMESPACE_URL,
            '_CONVERSATION_KEY_PREFIX': 'agent-conversation-',
            '_kv_request': lambda key, method, value=None: (
                store.get(key) if method == 'GET' else store.__setitem__(key, value)
            ),
            '_runtime_archived': lambda *args: False, 'RemoteConversation': Remote,
            'httpx': types.SimpleNamespace(HTTPStatusError=HTTPStatusError),
            'ConversationExecutionStatus': types.SimpleNamespace(RUNNING='running', IDLE='idle', PAUSED='paused', ERROR='error'),
            'LocalWorkspace': lambda **kwargs: kwargs,
            'StartConversationRequest': lambda **kwargs: types.SimpleNamespace(**kwargs),
            'SendMessageRequest': lambda **kwargs: kwargs,
            'TextContent': lambda **kwargs: kwargs,
            'automation_conversation_kwargs': lambda **kwargs: {},
        }
        deliver = load_function(path, 'deliver', namespace)
        dispatcher = types.SimpleNamespace(automation_id='automation', _workspace=object(),
            agent_url='http://agent', api_key='key', profile_id=UUID(int=1), _secrets={})
        dispatcher._deliver_once = types.MethodType(load_function(path, '_deliver_once', namespace), dispatcher)
        result = deliver(dispatcher, 'repo:pr:1', 'sha1', 'review', head='sha1')
        self.assertEqual(result['disposition'], 'created')
        self.assertEqual(result['conversation_id'], str(uuid5(NAMESPACE_URL, f'{initial}:replacement')))


    def test_runtime_preflight_recognizes_archived_history(self):
        for path in REVIEWERS:
            with self.subTest(path=path):
                class Url:
                    def __init__(self, can_resume):
                        self.can_resume = can_resume

                    def __call__(self, request, timeout):
                        self.assert_timeout(timeout)
                        return Response({'can_resume': self.can_resume})

                    @staticmethod
                    def assert_timeout(timeout):
                        assert timeout == 15

                for can_resume in (True, False):
                    namespace = {'Request': urllib.request.Request, 'urlopen': Url(can_resume),
                        'json': json, 'HTTPError': urllib.error.HTTPError, 'UUID': UUID}
                    check = load_function(path, '_runtime_archived', namespace)
                    self.assertEqual(check('http://agent', 'key', UUID(int=1)), not can_resume)


if __name__ == '__main__':
    unittest.main()
