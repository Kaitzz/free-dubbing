from pathlib import Path
from zipfile import ZipFile
import pytest
from scripts.colab_credentials import write_youtube_cookie
from backend.app.remote_archive import listing, pack_files


def test_cookie_readable_by_downloader_but_not_in_checkpoint(tmp_path):
    from http.cookiejar import MozillaCookieJar
    value = '# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsynthetic-secret\n.example.com\tTRUE\t/\tTRUE\t2147483647\tOTHER\tunrelated\n'
    target = write_youtube_cookie(value, tmp_path/'data')
    jar = MozillaCookieJar(str(target))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert [(c.name,c.value) for c in jar] == [('SID','synthetic-secret')]
    session=tmp_path/'session'
    session.mkdir()
    (session/'result.txt').write_text('result')
    archive=tmp_path/'result.zip'
    pack_files(archive, [(name, path) for name, (path, _, _) in listing({'session':session}).items()])
    with ZipFile(archive) as z:
        assert not any('cookie' in n for n in z.namelist())
        assert all(b'synthetic-secret' not in z.read(n) for n in z.namelist())


@pytest.mark.parametrize('value', ['SID=synthetic-secret', '# Netscape HTTP Cookie File\ninvalid-synthetic-secret', '# Netscape HTTP Cookie File\n'])
def test_bad_cookie_format_does_not_echo_values(value,tmp_path):
    with pytest.raises(ValueError) as caught:
        write_youtube_cookie(value,tmp_path)
    assert 'synthetic-secret' not in str(caught.value)


def test_absent_cookie_is_optional(tmp_path):
    assert write_youtube_cookie('',tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_worker_supplies_cookie_without_env_leak_and_cleans_file(tmp_path,monkeypatch):
    import io
    import httpx
    from scripts import colab_worker as worker
    monkeypatch.setattr(worker,'ROOT',tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-api-key')
    monkeypatch.setenv('YOUTUBE_COOKIES','# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsynthetic-secret\n')
    uploads=[]
    def handler(request):
        if request.url.path.endswith('/output'):
            uploads.append(request.content)
            return httpx.Response(200,json={'offset':len(request.content)})
        if request.url.path.endswith('/finish'):
            return httpx.Response(200,json={'status':'failed','files':{}})
        return httpx.Response(200,json={'ok':True})
    cookie_path=tmp_path/'remote-runs/tasks/test/leases'/('a'*32)/'data/cookies/youtube.txt'
    class Process:
        stdout=io.StringIO('')
        def poll(self): return 0
        def wait(self): return 0
    def start(*args,env,**kw):
        assert 'YOUTUBE_COOKIES' not in env
        assert Path(env['YOUDUB_DATA_DIR'])/'cookies/youtube.txt' == cookie_path
        assert 'synthetic-secret' in cookie_path.read_text()
        output=Path(env['WORKFOLDER'])/'session/metadata/asr.json'
        output.parent.mkdir(parents=True); output.write_text('{}')
        return Process()
    monkeypatch.setattr(worker.subprocess,'Popen',start)
    monkeypatch.setattr(worker,'snapshot',lambda folder,task,exitcode=None:
                        {**task,'status':'paused','stages':[{'name':'download','status':'succeeded'}]})
    job={'lease':'a'*32,'transfer_version':worker.TRANSFER_VERSION,'files':{},
         'task':{'id':'test','url':'https://www.youtube.com/watch?v=abcdefghijk','stages':[{'name':'download','status':'pending'}]},
         'settings':{'base_url':'https://example.com/v1','model':'test','translate_concurrency':'2'}}
    with httpx.Client(base_url='https://test.invalid',transport=httpx.MockTransport(handler)) as client:
        worker.run_job(client,job)
    assert not cookie_path.exists()
    with ZipFile(io.BytesIO(b''.join(uploads))) as z:
        assert z.namelist()==['session/metadata/asr.json']
        assert all(b'synthetic-secret' not in z.read(n) for n in z.namelist())
