import http.client,json,sys,time,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest import mock
sys.path[:0]=['poc']
import online_source_client as source_module
import online_workspace as workspace_module
from online_material_format import MaterialError

state={'cancel':False,'cancel_at':None}
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Length','10');self.end_headers()
  for i in range(10):
   time.sleep(.03)
   self.wfile.write(b'x');self.wfile.flush()
   if i==0:state['cancel']=True;state['cancel_at']=time.monotonic()
 def log_message(self,*args):pass
class Opener:
 def open(self,request,timeout):
  self.timeout=timeout;conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=timeout)
  conn.request('GET','/neutral');response=conn.getresponse()
  class Wrapped:
   status=response.status;headers=response.headers
   def geturl(self):return request.full_url
   def read1(self,count):return response.read1(count)
   def read(self,count):return response.read(count)
   def __enter__(self):return self
   def __exit__(self,*args):response.close();conn.close()
  return Wrapped()
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
 report={}
 opener=Opener()
 with mock.patch.object(source_module,'REQUEST_TIMEOUT_SECONDS',.05):
  client=source_module.PinnedSourceClient('neutral','source','a'*40,opener=opener)
  start=time.monotonic();body=client.read_file('entry.cbl',max_bytes=10);elapsed=time.monotonic()-start
 report['timeout']={'configured_timeout':opener.timeout,'elapsed_seconds':elapsed,'bytes':len(body),'returned_success':body==b'x'*10,'real_network':'localhost_http_connection_injected_into_https_client'}
 state.update(cancel=False,cancel_at=None);checks=[]
 def check_cancel():
  checks.append({'after':time.monotonic()-start,'requested':state['cancel']})
  if state['cancel']:raise MaterialError('ONLINE_CANCELLED')
 workbench=object.__new__(workspace_module.OnlineWorkbench);workbench.source_factory=None
 original=source_module.PinnedSourceClient;opener=Opener()
 def client_factory(*args,**options):return original(*args,opener=opener,**options)
 with mock.patch.object(source_module,'REQUEST_TIMEOUT_SECONDS',.05),mock.patch.object(workspace_module,'PinnedSourceClient',side_effect=client_factory):
  read=workbench._source({'identity':'neutral/source','commit':'a'*40,'subdirectory':''},{},check_cancel)
  start=time.monotonic()
  try:read('entry.cbl',max_bytes=10);outcome='returned'
  except MaterialError as e:outcome=e.code
  end=time.monotonic()
 report['cancel']={'configured_timeout':.05,'elapsed_seconds':end-start,'cancel_requested_after':state['cancel_at']-start,'cancel_observed_after':checks[-1]['after'],'post_cancel_read_seconds':end-state['cancel_at'],'outcome':outcome,'checks':checks}
 print(json.dumps(report));Path('/private/tmp/cobol-online-termination-audit-20261111/slow-drip-before.json').write_text(json.dumps(report,indent=2)+'\n')
finally:server.shutdown();server.server_close()
