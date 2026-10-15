"""Free exact-request cancellation with real sockets, no native provider or account."""
from __future__ import annotations
import http.server, json, queue, socket, sys, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis.v2 import bridge, mcp_session as M


def eventually(predicate, timeout=3):
    end=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>=end:raise AssertionError('fixture deadline')
        threading.Event().wait(.01)


def request(identity, name='hold'):
    return {'jsonrpc':'2.0','id':identity,'method':'tools/call','params':{'name':name,'arguments':{}}}


def cancel(identity):
    return {'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':identity}}


class Lines:
    def __init__(self):self.q=queue.Queue()
    def put(self,message):self.q.put(json.dumps(message)+'\n')
    def close(self):self.q.put(None)
    def __iter__(self):return self
    def __next__(self):
        line=self.q.get(timeout=10)
        if line is None:raise StopIteration
        return line


class Sink:
    def __init__(self):self.messages=[];self.lock=threading.Lock()
    def write(self,line):
        with self.lock:self.messages.append(json.loads(line))
    def flush(self):pass
    def has(self,identity):
        with self.lock:return any(x.get('id')==identity for x in self.messages)


class SocketLifetime(unittest.TestCase):
    def test_peek_never_consumes_and_closed_socket_denies(self):
        a,b=socket.socketpair()
        try:
            alive=bridge._request_alive(a);self.assertTrue(alive())
            b.sendall(b'x');self.assertTrue(alive());self.assertTrue(alive())
            self.assertEqual(a.recv(1),b'x')
            b.shutdown(socket.SHUT_RDWR);self.assertFalse(alive())
            a.close();self.assertFalse(alive())
        finally:a.close();b.close()

    def test_shutdown_before_bind_and_after_unbind_cannot_touch_successor(self):
        a,b=socket.socketpair();c,d=socket.socketpair()
        try:
            old=M.CallLifetime();old.cancel();self.assertFalse(old.bind(a))
            self.assertEqual(b.recv(1),b'')
            old.unbind(a);new=M.CallLifetime();self.assertTrue(new.bind(c))
            old.cancel();d.sendall(b'x');self.assertEqual(c.recv(1),b'x')
            new.cancel();self.assertEqual(d.recv(1),b'')
        finally:
            for sock in (a,b,c,d):sock.close()

    def test_cancel_before_during_and_after_connect_sends_no_request_or_reconnect(self):
        for stage in ('before','connecting','bound'):
            with self.subTest(stage=stage):
                entered=threading.Event();release=threading.Event();instances=[]
                class Connection:
                    def __init__(self,*args,**kwargs):
                        self.sock=None;self.peer=None;self.connects=0;self.requests=0;instances.append(self)
                    def connect(self):
                        self.connects+=1
                        if stage=='connecting':entered.set();release.wait(2)
                        self.sock,self.peer=socket.socketpair()
                    def request(self,*args,**kwargs):self.requests+=1;raise AssertionError('request after cancellation')
                    def close(self):
                        if self.sock:self.sock.close()
                        if self.peer:self.peer.close()
                life=M.CallLifetime()
                if stage=='before':life.cancel()
                if stage=='bound':
                    original=life.bind
                    def bind(sock):
                        got=original(sock);entered.set();release.wait(2);return got
                    life.bind=bind
                out=[]
                with patch.object(M.http.client,'HTTPConnection',Connection):
                    worker=threading.Thread(target=lambda:out.append(M.Relay('dummy',1).call_tool('hold',{},lifetime=life)))
                    worker.start()
                    if stage!='before':self.assertTrue(entered.wait(2));life.cancel();release.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive());self.assertTrue(out[0]['isError'])
                self.assertEqual(instances[0].connects,0 if stage=='before' else 1)
                self.assertEqual(instances[0].requests,0)
                self.assertIs(instances[0].auto_open,False)


class RealRelaySockets(unittest.TestCase):
    def setUp(self):
        self.entered={name:threading.Event() for name in ('hold','survivor')}
        self.stopped=threading.Event();self.release=threading.Event();self.received=[]
        outer=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                name=body['name'];outer.received.append(name);outer.entered[name].set()
                alive=bridge._request_alive(self.connection)
                deadline=time.monotonic()+5
                while not outer.release.is_set():
                    if not alive():outer.stopped.set();return
                    if time.monotonic()>deadline:return
                    outer.release.wait(.01)
                raw=json.dumps({'text':'Acted: fixture','isError':False}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
                try:self.wfile.write(raw)
                except OSError:pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.source=Lines();self.sink=Sink()
        self.worker=threading.Thread(target=M.serve,args=(M.Relay('dummy',self.server.server_port),self.source,self.sink),daemon=True)
        self.worker.start()
    def tearDown(self):
        self.release.set();self.source.close();self.worker.join(3)
        self.server.shutdown();self.server.server_close();self.server_thread.join(2)
        self.assertFalse(self.worker.is_alive())
    def test_cancel_exact_request_preserves_parallel_survivor(self):
        self.source.put(request(1));self.source.put(request(2,'survivor'))
        self.assertTrue(self.entered['hold'].wait(2));self.assertTrue(self.entered['survivor'].wait(2))
        self.source.put(cancel('unknown'));self.source.put(cancel(True))
        self.source.put({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':1,'reason':7}})
        self.source.put({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':1,'unexpected':True}})
        self.source.put({'jsonrpc':'2.0','id':90,'method':'ping','params':{}})
        eventually(lambda:self.sink.has(90));self.assertFalse(self.stopped.is_set())
        self.source.put(cancel(1));self.assertTrue(self.stopped.wait(2))
        self.assertFalse(self.sink.has(2));self.release.set();eventually(lambda:self.sink.has(2))
        self.assertFalse(self.sink.has(1));self.assertEqual(sorted(self.received),['hold','survivor'])
    def test_eof_shutdown_is_observed_before_server_work_can_continue(self):
        self.source.put(request(1));self.assertTrue(self.entered['hold'].wait(2))
        self.source.close();self.worker.join(3)
        self.assertFalse(self.worker.is_alive());self.assertTrue(self.stopped.wait(2))
        self.assertFalse(self.sink.has(1))


class BoundedProtocol(unittest.TestCase):
    def test_active_calls_workers_duplicate_ids_and_reuse(self):
        entered=[];stopped=[];lock=threading.Lock()
        class Relay:
            def call_tool(self,name,args,*,codex_context=None,lifetime=None):
                with lock:entered.append((args['tag'],lifetime))
                lifetime.cancelled.wait(5)
                with lock:stopped.append(args['tag'])
                return M._result('stopped')
        source=Lines();sink=Sink();worker=threading.Thread(target=M.serve,args=(Relay(),source,sink),daemon=True)
        def send(identity,tag):
            message=request(identity);message['params']['arguments']={'tag':tag};source.put(message)
        with patch.object(M,'MAX_ACTIVE_CALLS',2):
            worker.start();send(1,'first');send(1,'duplicate');send(2,'second');send(3,'overflow')
            eventually(lambda:len(entered)==2 and sink.has(3))
            self.assertTrue(sink.has(1));self.assertEqual(len(entered),2)
            first=entered[0][1];source.put(cancel(1));eventually(lambda:'first' in stopped)
            # Wait for exact worker cleanup by pinging same ID until admitted.
            eventually(lambda:not first.worker.is_alive())
            send(1,'reused');eventually(lambda:len(entered)==3)
            first.cancel();self.assertFalse(entered[-1][1].cancelled.is_set())
            source.close();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(sorted(stopped),['first','reused','second'])


if __name__=='__main__':unittest.main(verbosity=2)
