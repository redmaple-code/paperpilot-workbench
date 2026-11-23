"""OA transport boundaries, redirects and bounded streaming."""
import hashlib,ipaddress,socket,ssl,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse
import pytest
from paperpilot.services import imports as service


@pytest.fixture(autouse=True)
def direct_transport(monkeypatch):
    monkeypatch.setenv('PAPERPILOT_OA_PROXY_MODE','direct')


def addresses(*ips):
    result=[]
    for ip in ips:
        family=socket.AF_INET6 if ':' in ip else socket.AF_INET
        address=(ip,443,0,0) if family==socket.AF_INET6 else (ip,443)
        result.append((family,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',address))
    return result


@pytest.mark.parametrize('ips',[('127.0.0.1',),('10.1.2.3',),('169.254.169.254',),('100.64.0.1',),('::1',),('::ffff:127.0.0.1',),('224.0.0.1',),('8.8.8.8','192.168.0.1')])
def test_private_special_or_mixed_dns_is_denied_before_connect(monkeypatch,ips):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:addresses(*ips))
    monkeypatch.setattr(service,'_PinnedConnection',lambda *a,**k:pytest.fail('must not connect'))
    with pytest.raises(service.ImportError) as error:service.fetch_public('https://paper.example/file')
    assert error.value.code=='non_public_address'


@pytest.mark.parametrize('url',['http://127.0.0.1/a','https://u:p@paper.example/a','https://paper.example/a#fragment','http://[broken','https://paper.example/a\nInjected: x'])
def test_untrusted_urls_are_rejected(monkeypatch,url):
    if url.startswith('http://127.'):
        monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:addresses('127.0.0.1'))
    with pytest.raises(service.ImportError) as error:service.fetch_public(url)
    assert error.value.code in {'invalid_url','non_public_address'}


def test_redirect_rechecks_dns_and_blocks_private_target(monkeypatch):
    def resolve(host,*args,**kwargs):return addresses('8.8.8.8' if host=='public.example' else '127.0.0.1')
    monkeypatch.setattr(socket,'getaddrinfo',resolve);made=[]
    class Response:
        status=302;fp=None
        def getheader(self,name):return 'http://private.example/secret' if name=='Location' else None
        def close(self):pass
    class Connection:
        sock=None
        def __init__(self,*args):made.append(args)
        def request(self,*args,**kwargs):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(service,'_PinnedConnection',Connection)
    with pytest.raises(service.ImportError) as error:service.fetch_public('http://public.example/paper')
    assert error.value.code=='non_public_address' and len(made)==1


def test_connection_pins_resolved_socket_and_tls_hostname(monkeypatch):
    events=[]
    class FakeSocket:
        def settimeout(self,value):pass
        def connect(self,address):events.append(('connect',address))
        def close(self):pass
    class FakeTLS:
        def wrap_socket(self,sock,server_hostname):events.append(('tls',server_hostname));return sock
    monkeypatch.setattr(socket,'socket',lambda *args:FakeSocket())
    monkeypatch.setattr(ssl,'create_default_context',lambda:FakeTLS())
    connection=service._PinnedConnection('paper.example',443,(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,('8.8.8.8',443)),2,True);connection.connect()
    assert events==[('connect',('8.8.8.8',443)),('tls','paper.example')]


@pytest.fixture
def local_transport(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            if self.path=='/declared':
                self.send_response(200);self.send_header('Content-Length','999');self.send_header('Content-Type','text/html');self.end_headers();return
            if self.path=='/truncated':
                self.send_response(200);self.send_header('Content-Length','20');self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'short');return
            if self.path=='/slow':
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
                for _ in range(40):
                    try:self.wfile.write(b'x');self.wfile.flush();time.sleep(.05)
                    except OSError:break
                return
            data=b'x'*40 if self.path=='/stream' else b'<html>public test</html>'
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def local(url,deadline):
        parsed=urlparse(url);return parsed,'fixture.example',server.server_port,[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,('127.0.0.1',server.server_port))]
    monkeypatch.setattr(service,'_public_target',local)
    try:yield
    finally:server.shutdown();server.server_close();thread.join(3)


def test_real_streaming_size_and_truncation(local_transport):
    data,kind,sha=service.fetch_public('http://fixture.example/small');assert data==b'<html>public test</html>' and sha==hashlib.sha256(data).hexdigest()
    for path in ('/declared','/stream'):
        with pytest.raises(service.ImportError) as error:service._public_request('http://fixture.example'+path,timeout=2,max_bytes=32,accept='text/html')
        assert error.value.code=='file_too_large'
    with pytest.raises(service.ImportError) as error:service._public_request('http://fixture.example/truncated',timeout=2,max_bytes=32,accept='text/html')
    assert error.value.code=='fetch_incomplete'


def test_real_slow_drip_obeys_total_deadline(local_transport):
    start=time.monotonic()
    with pytest.raises(service.ImportError) as error:service._public_request('http://fixture.example/slow',timeout=.25,max_bytes=1000,accept='text/html')
    assert error.value.code=='fetch_timeout' and time.monotonic()-start<1.5


def test_metadata_identifier_is_validated_and_quoted(monkeypatch):
    urls=[]
    def request(url,**kwargs):urls.append(url);return b'{"id":"safe"}','application/json'
    monkeypatch.setattr(service,'_public_request',request)
    assert service.resolve_openalex('10.1000/a:b')['id']=='safe' and urls[0].endswith('10.1000/a%3Ab')
    with pytest.raises(service.ImportError) as error:service.resolve_openalex('10.1000/a?bad=1')
    assert error.value.code=='invalid_input' and len(urls)==1


def test_public_redirect_is_revalidated_and_pins_each_answer(monkeypatch):
    lookups=[];connections=[]
    def resolve(host,*args,**kwargs):
        lookups.append(host);return addresses('8.8.8.8' if host=='one.example' else '1.1.1.1')
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    class Response:
        fp=None
        def __init__(self,redirect):self.status=302 if redirect else 200;self.body=b'' if redirect else b'public text';self.redirect=redirect
        def getheader(self,name):
            if self.redirect and name=='Location':return 'https://two.example/file'
            if name=='Content-Type':return 'text/html'
            return None
        def read1(self,size):part=self.body[:size];self.body=self.body[size:];return part
        def close(self):pass
    class Connection:
        sock=None
        def __init__(self,host,port,address,timeout,secure):self.host=host;connections.append((host,address[3][0],secure))
        def request(self,*args,**kwargs):pass
        def getresponse(self):return Response(self.host=='one.example')
        def close(self):pass
    monkeypatch.setattr(service,'_PinnedConnection',Connection)
    data,_,_=service.fetch_public('https://one.example/start')
    assert data==b'public text' and lookups==['one.example','two.example'] and connections==[('one.example','8.8.8.8',True),('two.example','1.1.1.1',True)]


def test_dns_timeout_is_bounded_and_slot_eventually_released(monkeypatch):
    release=threading.Event();started=threading.Event()
    def slow(*args,**kwargs):started.set();release.wait(2);return addresses('8.8.8.8')
    monkeypatch.setattr(socket,'getaddrinfo',slow);start=time.monotonic()
    try:
        with pytest.raises(service.ImportError) as error:service.fetch_public('https://slow.example/paper',timeout=.15)
        assert started.is_set() and error.value.code=='fetch_timeout' and time.monotonic()-start<1
    finally:release.set()


def test_unicode_url_path_is_encoded_without_credentials(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:addresses('8.8.8.8'))
    requests=[]
    class Response:
        status=200;fp=None
        def __init__(self):self.body=b'ok'
        def getheader(self,name):return 'text/html' if name=='Content-Type' else None
        def read1(self,size):data=self.body;self.body=b'';return data
        def close(self):pass
    class Connection:
        sock=None
        def __init__(self,*args):pass
        def request(self,method,target,headers):requests.append((method,target,headers))
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(service,'_PinnedConnection',Connection)
    assert service.fetch_public('https://paper.example/论文?q=材料')[0]==b'ok'
    assert requests[0][1]=='/%E8%AE%BA%E6%96%87?q=%E6%9D%90%E6%96%99'
    assert 'Authorization' not in requests[0][2] and 'Cookie' not in requests[0][2]
