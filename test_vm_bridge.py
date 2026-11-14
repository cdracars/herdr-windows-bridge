import importlib.util, os, sys, tempfile, subprocess, socket, stat, time, threading, hashlib
P=os.path.dirname(__file__)+'/vm_bridge.py'
spec=importlib.util.spec_from_file_location('b',P); b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
count=0
def check(v,label):
 global count
 assert v,label
 count+=1; print('PASS',label,flush=True)
with tempfile.TemporaryDirectory(prefix='bridge-test-') as root:
 os.chmod(root,0o700)
 def run(args):
  wrapper='import runpy,sys; d=runpy.run_path(sys.argv[1]); d["main"].__globals__["ROOT"]=sys.argv[2]; sys.argv=[sys.argv[1]]+sys.argv[3:]; d["main"]()'
  return subprocess.Popen([sys.executable,'-I','-c',wrapper,P,root]+args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 def pub():
  p=run(['publish']); assert p.stderr.readline()==b'BRIDGE_READY\n'; return p
 path=root+'/'+b.NAME+'/'+b.SOCKET
 p=pub()
 check(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)==0o700,'private runtime')
 check(stat.S_IMODE(os.stat(path).st_mode)==0o600,'private socket')
 d=run(['publish']); d.communicate(timeout=3); check(d.returncode!=0,'duplicate refuses')
 v=run(['via',b.LINUX]); check(p.stderr.readline()==b'BRIDGE_CONNECTED\n','one consumer accepted')
 a=bytes(range(256))*1024; z=a[::-1]
 results={}
 def transfer(proc,data,key): results[key]=proc.communicate(data,timeout=8)[0]
 t=threading.Thread(target=transfer,args=(p,a,'p')); t.start()
 transfer(v,z,'v'); t.join()
 check(results=={'p':z,'v':a},'binary full duplex 256 KiB each and EOF')
 check(p.returncode==0 and v.returncode==0,'clean disconnect')
 for command in ['id',b.LINUX+'; id',b.LINUX.replace(' connect',' install'),b.WINDOWS.replace('default','other')]:
  r=run(['via',command]); r.communicate(timeout=3); check(r.returncode!=0,'reject shell intent')
 # Stale socket left by a killed publisher.
 p=pub(); p.kill(); p.communicate(); check(os.path.exists(path),'killed publisher leaves stale socket')
 p=pub(); p.stdin.close(); p.wait(timeout=3); check(not os.path.exists(path),'stale cleanup and idle EOF')
 os.symlink('/dev/null',path)
 p=run(['publish']); p.communicate(timeout=3); check(p.returncode!=0 and os.path.islink(path),'socket symlink rejected'); os.unlink(path)
 lock=os.path.dirname(path)+'/publisher.lock'; os.unlink(lock); os.symlink('/dev/null',lock)
 p=run(['publish']); p.communicate(timeout=3); check(p.returncode!=0 and os.path.islink(lock),'lock symlink rejected'); os.unlink(lock)
 os.chmod(os.path.dirname(path),0o755)
 p=run(['publish']); p.communicate(timeout=3); check(p.returncode!=0,'directory mode rejected'); os.chmod(os.path.dirname(path),0o700)
 # Socket half-close: consumer ends writes, publisher can still deliver final response.
 p=pub(); s=socket.socket(socket.AF_UNIX); s.connect(path); assert p.stderr.readline()==b'BRIDGE_CONNECTED\n'
 s.sendall(b'request'); s.shutdown(socket.SHUT_WR)
 check(os.read(p.stdout.fileno(),7)==b'request','half-close request delivered')
 p.stdin.write(b'response'); p.stdin.close(); check(s.recv(8)==b'response','half-close final response delivered'); s.close(); p.wait(timeout=4)
 # Backpressure: never reading stdout cannot grow user-space queues unbounded.
 p=pub(); s=socket.socket(socket.AF_UNIX); s.connect(path); p.stderr.readline(); s.setblocking(False)
 total=0
 for _ in range(2000):
  try: total+=s.send(b'x'*65536)
  except BlockingIOError: break
 check(total<16*1024*1024,'bounded pipe/socket backpressure')
 s.close(); p.stdin.close(); p.wait(timeout=5); check(p.poll() is not None,'blocked-output disconnect bounded')
 # UID guard and component symlink checks via in-process isolated override only.
 old=b.UID; b.UID=1001
 try:
  try: b.runtime(); ok=False
  except RuntimeError: ok=True
  check(ok,'UID guard')
 finally: b.UID=old
 b.ROOT=root
 os.rmdir(os.path.dirname(path)) if not os.listdir(os.path.dirname(path)) else None
 # checked rejects incorrect owner without requiring privileged ownership changes.
 class Fake: st_uid=1001; st_mode=stat.S_IFSOCK|0o600
 try: b.checked(Fake(),stat.S_ISSOCK,0o600); ok=False
 except RuntimeError: ok=True
 check(ok,'foreign owner rejected')
 check(b.LINUX in b.ALLOWED and b.WINDOWS in b.ALLOWED and len(b.ALLOWED)==3,'exact allowlist only')
print('TOTAL',count)
print('SHA256',hashlib.sha256(open(P,'rb').read()).hexdigest())
