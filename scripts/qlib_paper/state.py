"""Isolated GitHub branch storage, optimistic concurrency, never reset on failure."""
import base64
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BRANCH = 'qlib-paper-state'


def api(path, method='GET', payload=None):
    repo = os.environ['GITHUB_REPOSITORY']
    request = Request(f'https://api.github.com/repos/{repo}/{path}', method=method,
                      headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                               'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'},
                      data=json.dumps(payload).encode() if payload is not None else None)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def load():
    record = api(f'contents/paper-state.json?ref={BRANCH}')
    return json.loads(base64.b64decode(record['content'])), record['sha']


def save(state, sha):
    payload = {'message': 'Update simulated Qlib ledger', 'branch': BRANCH,
               'content': base64.b64encode(json.dumps(state, indent=2, allow_nan=False).encode()).decode()}
    if sha:
        payload['sha'] = sha
    return api('contents/paper-state.json', 'PUT', payload)['content']['sha']


def initialize():
    from paper import initial_state
    try:
        api(f'git/ref/heads/{BRANCH}')
    except HTTPError as exc:
        if exc.code != 404:
            raise
        head = api('git/ref/heads/main')['object']['sha']
        api('git/refs', 'POST', {'ref': f'refs/heads/{BRANCH}', 'sha': head})
    try:
        load()
    except HTTPError as exc:
        if exc.code != 404:
            raise
        save(initial_state(), None)
        return
    raise ValueError('Paper state already exists; initialization refused')
