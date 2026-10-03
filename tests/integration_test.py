#!/usr/bin/env python3
"""Integration harness for the local HTML library; only generated fixtures."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

APP = Path(__file__).resolve().parents[1]
PORT, CONTENT_PORT = 18775, 18776
BASE = f'http://127.0.0.1:{PORT}'
CONTENT = f'http://127.0.0.1:{CONTENT_PORT}'
HTTP = build_opener(ProxyHandler({}))
RESULTS = []
TOKEN = None


def request(path, data=None, method=None, headers=None, origin=True):
    url = path if path.startswith('http') else BASE + path
    h = {'Accept': 'application/json'}
    if origin:
        h['Origin'] = BASE
    if data is not None:
        h['Content-Type'] = 'application/json'
    if TOKEN:
        h['X-Library-Token'] = TOKEN
    h.update(headers or {})
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  method=method or ('POST' if data is not None else 'GET'), headers=h)
    try:
        with HTTP.open(req, timeout=4) as response:
            payload = response.read()
            return response.status, response.headers, payload
    except HTTPError as error:
        return error.code, error.headers, error.read()


def jget(path):
    code, _, body = request(path)
    assert code == 200, (path, code, body[:300])
    return json.loads(body)


def post(path, body):
    code, _, payload = request(path, body)
    assert 200 <= code < 300, (path, code, payload[:300])
    return json.loads(payload) if payload else {}


def wait_for(predicate, desc, seconds=12):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, AssertionError, ValueError, URLError) as error:
            last = repr(error)
        time.sleep(.2)
    raise AssertionError(f'timed out: {desc}; last={last!r}')


def check(name, callback):
    try:
        detail = callback()
        RESULTS.append({'test': name, 'result': 'PASS', 'detail': str(detail or '')})
        print('PASS', name, flush=True)
        return detail
    except Exception as error:
        RESULTS.append({'test': name, 'result': 'FAIL', 'detail': repr(error)})
        print('FAIL', name, repr(error), flush=True)
        return None


def launch(root, data, log):
    process = subprocess.Popen([
        sys.executable, '-u', str(APP / 'server.py'),
        '--port', str(PORT), '--content-port', str(CONTENT_PORT),
        '--data-dir', str(data), '--root', str(root), '--interval', '1',
    ], stdout=log, stderr=subprocess.STDOUT, cwd=APP)
    wait_for(lambda: jget('/api/state'), 'server readiness')
    return process


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)


def make_fixture(base):
    root = base / 'source 中文目录'
    nested = root / 'project alpha'
    nested.mkdir(parents=True)
    (nested / 'assets').mkdir()
    (nested / 'assets/style.css').write_text('body { color: rgb(10,20,30); }')
    (nested / 'assets/app.js').write_text('window.qaRelativeResource=true;')
    (nested / '说明.txt').write_text('SAFE RESOURCE')
    (nested / '研究 面板.html').write_text('''<!doctype html><meta charset="utf-8">
<title>季度研究面板</title><link rel="stylesheet" href="assets/style.css">
<h1>季度研究面板</h1><p>雪山咖啡指标 搜索正文独有词 一月趋势分析。</p>
<script src="assets/app.js"></script>''')
    outside = base / 'outside'
    outside.mkdir()
    (outside / 'private.html').write_text('<title>DO NOT EXPOSE</title>OUTSIDE SECRET')
    (outside / 'secret.txt').write_text('OUTSIDE SECRET')
    os.symlink(outside, nested / 'escape')
    os.symlink(outside / 'private.html', nested / 'escape.html')
    return root, nested, outside


def main():
    global TOKEN
    log_path = Path(__file__).parent / 'server-test.log'
    report_path = Path(__file__).parent / 'integration-results.json'
    with tempfile.TemporaryDirectory(prefix='html-library-qa-') as folder, log_path.open('w') as log:
        base = Path(folder)
        root, nested, outside = make_fixture(base)
        process = None
        try:
            process = launch(root, base / 'index-data', log)
            TOKEN = jget('/api/state')['csrf_token']
            def items(): return jget('/api/state')['items']
            def search(query): return jget('/api/search?' + urlencode({'q':query}))['items']
            def mutate(path, payload, method='PATCH'):
                code, _, body = request(path, payload, method=method)
                assert code == 200, (code, body)
                return json.loads(body)
            def initial():
                state = wait_for(lambda: items(), 'first scan')
                assert len(state)==1, state
                assert state[0]['title']=='季度研究面板', state
                return state[0]
            item = check('initial HTML title extraction and symlink skip', initial)
            if item is None: raise RuntimeError('cannot continue without initial item')
            item_id = item['id']
            def chinese_search():
                assert [i['id'] for i in search('搜索正文独有词')] == [item_id]
                assert [i['id'] for i in search('研究 面板')] == [item_id]
                assert len(search('雪山咖啡 趋势')) == 1
                assert len(search('雪山咖啡 nonexistent')) == 0
            check('Chinese body, filename and AND keyword search', chinese_search)
            original = nested / '研究 面板.html'
            original_bytes = original.read_bytes()
            def add_modify_delete():
                added = nested / 'new-page.htm'
                added.write_text('<title>自动发现</title><p>自动新增独有词</p>')
                added_item = wait_for(lambda: search('自动新增独有词'), 'automatic add')[0]
                added.write_text('<title>自动更新标题</title><p>正文更新独有词</p>')
                wait_for(lambda: search('正文更新独有词'), 'automatic update')
                assert not search('自动新增独有词')
                added.unlink()
                wait_for(lambda: not search('正文更新独有词'), 'automatic delete')
                assert all(i['id'] != added_item['id'] for i in items())
            check('automatic add, modify and delete without manual scan', add_modify_delete)
            def save_metadata():
                mutate('/api/items/' + item_id, {'favorite':True, 'title':'珍藏研究面板'})
                opened = post('/api/items/' + item_id + '/open', {})
                now = items()[0]
                assert now['favorite'] is True and now['last_opened'] and now['title']=='珍藏研究面板'
                assert original.read_bytes() == original_bytes
                return opened['url']
            content_url = check('favorite, title and recent metadata preserve source', save_metadata)
            def resources():
                code, headers, body = request(content_url)
                assert code == 200 and '季度研究面板'.encode() in body, (code, body)
                prefix = content_url.rsplit('/',1)[0] + '/'
                for relative, expected in [('assets/style.css',b'rgb(10,20,30)'), ('assets/app.js',b'qaRelativeResource'), ('说明.txt',b'SAFE RESOURCE')]:
                    code, _, data = request(prefix + quote(relative, safe='/'))
                    assert code == 200 and expected in data, (relative,code,data)
                assert headers.get('Referrer-Policy')=='no-referrer'
            check('original page and CSS JS Unicode relative resources', resources)
            def security_requests():
                for path, body, method, headers, origin in [
                    ('/api/scan',{},'POST',{'Origin':'https://evil.example'},True),
                    ('/api/scan',{},'POST',{'Origin':CONTENT},True),
                    ('/api/scan',{},'POST',{},False),
                    ('/api/scan',{},'POST',{'X-Library-Token':'wrong'},True),
                    ('/api/state',None,'GET',{'Origin':CONTENT},True),
                    ('/api/state',None,'GET',{'Sec-Fetch-Site':'same-site'},True),
                    ('/api/state',None,'GET',{'Host':'evil.example:'+str(PORT)},True),
                ]:
                    code, _, data = request(path,body,method,headers,origin)
                    assert code==403, (path,method,headers,code,data)
                assert request(CONTENT + '/api/state')[0]==404
            check('cross Origin writes, CSRF, same-site reads and DNS rebinding rejected', security_requests)
            def traversal():
                prefix = content_url.rsplit('/',1)[0] + '/'
                cap_prefix = content_url.split('/files/',1)[0]+'/files/'+content_url.split('/files/',1)[1].split('/',1)[0]+'/'
                for url in [prefix+'escape/secret.txt', prefix+'escape.html', prefix+'../escape/secret.txt', cap_prefix+'%2e%2e/outside/secret.txt', cap_prefix+'%2fetc/passwd', cap_prefix+'%00.html', cap_prefix+'..%5coutside%5csecret.txt']:
                    code, _, body = request(url)
                    assert code in (400,403,404) and b'OUTSIDE SECRET' not in body, (url,code,body)
            check('encoded traversal and symlink escapes denied', traversal)
            def invalid_inputs():
                for path,payload,method in [('/api/roots', {'path':'relative'},'POST'),('/api/roots', {'path':'/'},'POST'),('/api/roots', {'path':str(base/'missing')},'POST'),('/api/items/'+item_id,{'favorite':'yes'},'PATCH')]:
                    code,_,body = request(path,payload,method)
                    assert code==400, (path,code,body)
            check('invalid roots and invalid favorite return useful 400', invalid_inputs)
            def preview_check():
                attack = nested / 'preview-attack.html'
                attack.write_text('''<title>预览边界</title><style>body{color:red;background:url(https://evil.example/t)} @import "https://evil.example/s";</style>
<script>window.__qaExecuted=true;fetch('/api/state')</script><img src="https://evil.example/i" onerror="alert(1)">
<iframe src="https://evil.example/"></iframe><form action="https://evil.example"><input></form>
<a href="javascript:alert(1)" onclick="alert(1)">Click</a><svg onload="alert(1)"></svg><meta http-equiv="refresh" content="0;url=https://evil.example">''')
                record = wait_for(lambda: search('预览边界'), 'attack fixture index')[0]
                code,headers,body = request('/api/items/'+record['id']+'/preview')
                rendered = body.decode()
                assert code==200
                csp = headers.get('Content-Security-Policy','')
                assert "script-src 'none'" in csp and '; sandbox;' in csp and "connect-src 'none'" in csp, csp
                for unsafe in ['<script','<iframe','<form','<svg','onclick=','onerror=','javascript:','https://evil.example','http-equiv']:
                    assert unsafe not in rendered, (unsafe, rendered)
                attack.unlink()
                wait_for(lambda: not search('预览边界'), 'attack fixture cleanup')
            check('preview sanitizer plus sandbox CSP blocks active content', preview_check)
            def build_resources():
                dist = nested / 'dist'
                (dist/'assets').mkdir(parents=True)
                script = dist/'assets/main.js'
                script.write_text('window.qaBuiltScript=true;')
                css = dist/'assets/main.css'
                css.write_text('body{background-image:url("/assets/pixel.png")}')
                (dist/'assets/pixel.png').write_bytes(b'QA-PNG-RESOURCE')
                built = dist/'index.html'
                built.write_text('<title>构建产物资源测试</title><link rel="stylesheet" href="/assets/main.css"><script src="/assets/main.js"></script><p>构建测试独有词</p>')
                before = built.read_bytes()
                record = wait_for(lambda:search('构建测试独有词'),'dist inclusion')[0]
                url = jget('/api/items/'+record['id']+'/url')['url']
                code, _, data = request(url)
                assert code==200
                text=data.decode()
                root_prefix=url.rsplit('/',1)[0]+'/'
                expected_path=root_prefix.removeprefix(CONTENT)+'assets/'
                assert 'src="'+expected_path+'main.js"' in text, text
                assert 'href="'+expected_path+'main.css"' in text, text
                code, _, data=request(root_prefix+'assets/main.css')
                assert code==200 and (expected_path+'pixel.png').encode() in data, data
                assert request(root_prefix+'assets/main.js')[0]==200
                assert built.read_bytes()==before
                built.unlink()
                wait_for(lambda:not search('构建测试独有词'),'build fixture cleanup')
            check('dist indexed and root-relative HTML CSS assets rewritten without source edits', build_resources)
            def hidden_alias():
                hidden = root/'.secret'
                hidden.mkdir()
                (hidden/'private.txt').write_text('HIDDEN SECRET')
                alias = nested/'visible-alias'
                os.symlink(hidden,alias)
                prefix = content_url.rsplit('/',1)[0]+'/'
                for suffix in ['visible-alias/private.txt','../.secret/private.txt']:
                    code, _, data=request(prefix+suffix)
                    assert code in (403,404) and b'HIDDEN SECRET' not in data, (code,data)
            check('visible symlink cannot expose hidden files', hidden_alias)
            stop(process)
            process = launch(root, base/'index-data', log)
            TOKEN = jget('/api/state')['csrf_token']
            def persisted():
                record=next(i for i in items() if i['id']==item_id)
                assert record['favorite'] and record['title']=='珍藏研究面板' and record['last_opened']
                assert jget('/api/items/'+item_id+'/url')['url']==content_url
            check('favorite title recent and content link persist after restart', persisted)
            def add_remove_root():
                second = base/'second root'
                second.mkdir()
                source = second/'second.html'
                source.write_text('<title>第二目录</title>第二目录测试标记')
                before=source.read_bytes()
                added=post('/api/roots', {'path':str(second)})
                record=wait_for(lambda: search('第二目录测试标记'), 'second root indexing')[0]
                url=jget('/api/items/'+record['id']+'/url')['url']
                mutate('/api/roots/'+added['id'], {}, method='DELETE')
                assert not search('第二目录测试标记')
                assert request(url)[0] in (403,404)
                assert source.read_bytes()==before
            check('register and remove source revokes access and preserves files', add_remove_root)
            def deleted_reappears():
                original.unlink()
                wait_for(lambda:not search('珍藏研究面板'),'remove favorite source')
                original.write_bytes(original_bytes)
                record=wait_for(lambda: search('珍藏研究面板'), 'restore same source')[0]
                assert record['favorite'] and record['last_opened']
            check('delete then restore same file retains metadata', deleted_reappears)
            def root_symlink_swap():
                saved=root.with_name('saved-source')
                root.rename(saved)
                replacement=outside/'project alpha'
                replacement.mkdir()
                (replacement/'研究 面板.html').write_text('OUTSIDE SECRET')
                os.symlink(outside,root)
                try:
                    code,_,body=request(content_url)
                    assert code in (403,404) and b'OUTSIDE SECRET' not in body, (code,body)
                finally:
                    root.unlink()
                    saved.rename(root)
            check('registered root replaced by symlink cannot change trust boundary', root_symlink_swap)
        finally:
            stop(process)
            report_path.write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2))
    failed=[r for r in RESULTS if r['result']=='FAIL']
    print(f'{len(RESULTS)-len(failed)}/{len(RESULTS)} passed; report={report_path}')
    raise SystemExit(bool(failed))

if __name__ == '__main__':
    main()
