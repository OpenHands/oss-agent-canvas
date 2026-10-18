"""Launch a bounded SDK parity audit with only GitHub and scoped KV access."""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def config():
    host = os.environ.get('AGENT_SERVER_URL', '').rstrip('/')
    key = os.environ.get('OH_SESSION_API_KEYS_0') or os.environ.get('SESSION_API_KEY')
    kv_url = os.environ.get('AUTOMATION_API_URL', '').rstrip('/') + '/v1/kv'
    kv_token = os.environ.get('AUTOMATION_KV_TOKEN')
    if not host or not key or not kv_url or not kv_token:
        raise RuntimeError('Required agent-server and scoped KV configuration missing')
    for url in (host, kv_url):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
            raise RuntimeError('Invalid service URL')
    if '/api/automation/v1/kv' not in urlsplit(kv_url).path:
        raise RuntimeError('Expected scoped automation KV URL')
    return host, key, kv_url, kv_token


def fire_callback(status, error=None):
    url = os.environ.get('AUTOMATION_CALLBACK_URL')
    if not url:
        return
    body = {'status': status, 'run_id': os.environ.get('AUTOMATION_RUN_ID', '')}
    if error:
        body['error'] = error
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + os.environ.get('AUTOMATION_CALLBACK_API_KEY', ''),
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def preflight():
    from openhands.sdk import Conversation
    from openhands.sdk.workspace.remote.base import RemoteWorkspace
    from openhands.tools.preset.default import get_default_agent
    import inspect
    assert 'names' in inspect.signature(RemoteWorkspace.get_secrets).parameters
    assert callable(Conversation) and callable(get_default_agent)
    prompt = Path(__file__).with_name('prompt.txt').read_text()
    assert 'UNKNOWN' in prompt and 'software-agent-sdk' in prompt and 'oss-ohe-parity' in prompt
    return prompt


def run():
    prompt = preflight()
    host, key, kv_url, kv_token = config()
    # Verify durable bootstrap is readable before any LLM or GitHub write.
    request = urllib.request.Request(kv_url + '/policy_v1', headers={'Authorization': 'Bearer ' + kv_token})
    with urllib.request.urlopen(request, timeout=30) as response:
        policy = json.load(response)['value']
    if policy.get('kv_absence_means') != 'unknown' or not policy.get('always_search_live_github_before_creation'):
        raise RuntimeError('Required positive-evidence policy not seeded')
    from openhands.sdk import Conversation
    from openhands.sdk.workspace.remote.base import RemoteWorkspace
    from openhands.tools.preset.default import get_default_agent
    base = os.path.expanduser(os.environ.get('WORKSPACE_BASE', '/workspace'))
    with RemoteWorkspace(host=host, api_key=key, working_dir=base) as workspace:
        llm = workspace.get_llm(profile_name=os.environ.get('AUTOMATION_MODEL') or None)
        secrets = workspace.get_secrets(names=['GITHUB_TOKEN'])
        if set(secrets) != {'GITHUB_TOKEN'}:
            raise RuntimeError('Explicit GitHub secret unavailable; refusing broad fallback')
        secrets.update({'AUTOMATION_KV_TOKEN': kv_token, 'AUTOMATION_KV_URL': kv_url})
        agent = get_default_agent(llm=llm, cli_mode=True)
        with Conversation(agent=agent, workspace=workspace, secrets=secrets,
                          delete_on_close=False, max_iteration_per_run=100,
                          tags={'automation': 'oss-ohe-parity'},
                          user_id=os.environ.get('AUTOMATION_USER_ID')) as conversation:
            print('Parity audit conversation:', conversation.id)
            conversation.send_message(prompt)
            conversation.run()
            status = str(conversation.state.execution_status).lower()
            if status.rsplit('.', 1)[-1] not in ('finished', 'idle'):
                raise RuntimeError('Audit conversation did not finish successfully: ' + status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    if args.preflight:
        preflight()
        print('SDK imports, explicit secret-filter signature and packaged policy verified; no run or writes')
        return 0
    try:
        run()
    except Exception as error:
        # Never propagate provider/network exception bodies into callback logs.
        message = 'Parity audit failed: ' + type(error).__name__
        print(message, file=sys.stderr)
        fire_callback('FAILED', message)
        return 1
    fire_callback('COMPLETED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
