import os,sys,tempfile,subprocess,socket,time,stat,importlib.util
P=os.path.dirname(__file__)+'/vm_bridge.py'
spec=importlib.util.spec_from_file_location('b',P); b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
with tempfile.TemporaryDirectory(prefix='bridge-edge-') as root:
 os.chmod(root,0o700)
 def run(mode):
  code='import runpy,sys; d=runpy.run_path(sys.argv[1]); d["main"].__globals__["ROOT"]=sys.argv[2]; sys.argv=[sys.argv[1]]+sys.argv[3:]; d["main"]()'
  return subprocess.Popen([sys.executable,'-I','-c',code,P,root]+mode,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 d=root+'/'+b.NAME; os.mkdir(d,0o700)
 os.rmdir(d); os.symlink(root,d)
 p=run(['publish']); p.communicate(timeout=2); assert p.returncode; os.unlink(d)
 print('PASS runtime directory symlink rejection')
 os.mkdir(d,0o700)
 s=socket.socket(socket.AF_UNIX); s.bind(d+'/'+b.SOCKET); os.chmod(d+'/'+b.SOCKET,0o600); s.listen(1)
 p=run(['publish']); p.communicate(timeout=2); assert p.returncode and os.path.exists(d+'/'+b.SOCKET); s.close(); os.unlink(d+'/'+b.SOCKET)
 print('PASS live socket refused and retained without publisher lock')
 v=run(['via',b.LINUX]); time.sleep(.5); p=run(['publish']); assert p.stderr.readline()==b'BRIDGE_READY\n'; assert p.stderr.readline()==b'BRIDGE_CONNECTED\n'
 p.stdin.close(); v.stdin.close(); p.wait(timeout=5); v.wait(timeout=5); assert not p.returncode and not v.returncode
 print('PASS via waits for fresh publisher')
 start=time.monotonic(); v=run(['via',b.LINUX]); v.communicate(timeout=12); elapsed=time.monotonic()-start; assert v.returncode and 9.8<elapsed<11
 print('PASS unavailable publisher timeout',round(elapsed,2))
 # SO_PEERCRED tested on an actual same-UID socket; mock only the rejected UID.
 a,z=socket.socketpair(); b.peer(a); a.close(); z.close()
 class Peer:
  def getsockopt(self,*args): return __import__('struct').pack('3i',123,1001,1001)
 try: b.peer(Peer()); raise AssertionError('accepted foreign peer')
 except RuntimeError: pass
 print('PASS SO_PEERCRED positive and foreign UID rejection unit case')
