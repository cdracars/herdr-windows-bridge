import os,sys,tempfile,subprocess,socket,time,importlib.util,hashlib
P=os.path.dirname(__file__)+'/vm_bridge.py'
spec=importlib.util.spec_from_file_location('b',P); b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
with tempfile.TemporaryDirectory(prefix='bridge-regression-') as root:
 os.chmod(root,0o700)
 def run(args):
  code='import runpy,sys; d=runpy.run_path(sys.argv[1]); d["main"].__globals__["ROOT"]=sys.argv[2]; sys.argv=[sys.argv[1]]+sys.argv[3:]; d["main"]()'
  return subprocess.Popen([sys.executable,'-I','-c',code,P,root]+args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 path=root+'/'+b.NAME+'/'+b.SOCKET
 p=run(['publish']); assert p.stderr.readline()==b'BRIDGE_READY\n'
 p.stdin.write(b'buffered protocol bytes\x00\xff'); p.stdin.close(); start=time.monotonic(); p.wait(timeout=2)
 assert p.returncode==0 and not os.path.exists(path)
 print('PASS buffered EOF before consumer exits and removes socket',round(time.monotonic()-start,3),flush=True)
 p=run(['publish']); assert p.stderr.readline()==b'BRIDGE_READY\n'; p.stdin.close(); p.wait(timeout=2); assert not p.returncode
 print('PASS new publisher takes released lock after buffered EOF',flush=True)
 s=socket.socket(socket.AF_UNIX); s.bind(path); os.chmod(path,0o600); s.listen(0)
 queued=socket.socket(socket.AF_UNIX); queued.connect(path)
 probe=socket.socket(socket.AF_UNIX); probe.setblocking(False)
 try:
  probe.connect(path); raise AssertionError('backlog did not fill')
 except BlockingIOError: pass
 finally: probe.close()
 print('PASS test listener backlog demonstrably saturated',flush=True)
 start=time.monotonic(); p=run(['publish']); p.communicate(timeout=2); elapsed=time.monotonic()-start
 assert p.returncode and elapsed<1.5 and os.path.exists(path)
 print('PASS saturated stale-probe bounded and live socket preserved',round(elapsed,3),flush=True)
 start=time.monotonic(); v=run(['via',b.LINUX]); out,err=v.communicate(timeout=11); elapsed=time.monotonic()-start
 assert v.returncode and 9.8<elapsed<10.5 and not out
 print('PASS saturated via connect total deadline',round(elapsed,3),flush=True)
 queued.close(); s.close()
print('SHA256',hashlib.sha256(open(P,'rb').read()).hexdigest())
