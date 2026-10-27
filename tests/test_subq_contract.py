import io
import json
from unittest.mock import patch

import pytest

from contextweft.repository import Candidate
from contextweft.subq import SubQError, judge


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def fake_urlopen(req, timeout):
    body = json.loads(req.data)
    assert req.full_url == 'https://api.subq.ai/v1/systemone'
    assert body['model'] == 'subq-snap-1.1'
    assert len(body['questions']) == 2
    assert 'excerpt' not in body['state']['candidate_files'][0]
    answers = {'relevance_0': {'type':'noul','noul':0.81},
               'review_risk': {'type':'noul','noul':0.93}}
    return Response(json.dumps({'answers':answers,'usage':{'input_tokens':133}}).encode())


def test_subq_contract_and_multiplex():
    with patch('contextweft.subq.urllib.request.urlopen', fake_urlopen):
        r = judge('fix auth', [Candidate('src/auth.py',1, 'secret excerpt')], api_key='fake')
    assert r.probabilities == {'src/auth.py':0.81}
    assert r.risk_probability == .93
    assert r.input_tokens == 133


def test_subq_missing_usage_is_unknown_not_zero():
    body = json.dumps({'answers': {'relevance_0': {'type': 'noul', 'noul': 0.5}}}).encode()
    with patch('contextweft.subq.urllib.request.urlopen', lambda req, timeout: Response(body)):
        r = judge('fix', [Candidate('a.py', 1)], api_key='fake')
    assert r.input_tokens is None and r.risk_probability is None


def test_subq_missing_key_and_http_error(monkeypatch):
    import urllib.error
    monkeypatch.delenv('SUBQ_API_KEY', raising=False)
    with pytest.raises(SubQError, match='not set'):
        judge('fix', [Candidate('a.py', 1)])

    def deny(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 402, 'secret body', {}, None)
    with patch('contextweft.subq.urllib.request.urlopen', deny):
        with pytest.raises(SubQError, match='insufficient credits'):
            judge('fix', [Candidate('a.py', 1)], api_key='fake')


def test_scout_falls_back_without_key(tmp_path, monkeypatch):
    from contextweft.scout import scout
    monkeypatch.delenv('SUBQ_API_KEY', raising=False)
    (tmp_path / 'auth.py').write_text('def login(): pass\n')
    r = scout('fix login auth', tmp_path)
    assert r.status == 'fallback' and r.input_tokens == 0 and r.files[0].path == 'auth.py'


def test_subq_invalid_response_fails():
    with patch('contextweft.subq.urllib.request.urlopen', lambda req,timeout:Response(b'{"answers": {}}')):
        with pytest.raises(SubQError):
            judge('fix auth', [Candidate('src/auth.py',1)], api_key='fake')
