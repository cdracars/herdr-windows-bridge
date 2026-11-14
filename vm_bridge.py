#!/usr/bin/env python3
"""Single-use private SSH stdio bridge; no execution of caller shell text."""
import os, sys, stat, socket, struct, select, fcntl, time, errno
UID=1000
ROOT='/run/user/1000'
NAME='herdr-windows-bridge'
SOCKET='bridge.sock'
LIMIT=262144
GRACE=3.0
PUBLISHER_WAIT_SECONDS=10.0
PUBLISHER_POLL_SECONDS=0.1
LINUX="sh -c 'p=$(command -v herdr-core) || p=$HOME/.local/bin/herdr-core; [ -x \"$p\" ] || { echo \"herdr-core is not installed here\" >&2; exit 127; }; HC_SESSION=default exec \"$p\" connect'"
WINDOWS='cmd /d /c "if exist .\\AppData\\Local\\Programs\\herdr-core\\herdr-core.exe (.\\AppData\\Local\\Programs\\herdr-core\\herdr-core.exe connect --session default) else (echo herdr-core is not installed here 1>&2 & exit /b 127)"'
ALLOWED=frozenset((LINUX,LINUX.replace('HC_SESSION=default ',''),WINDOWS))
def checked(st,kind,mode):
    if st.st_uid!=UID or not kind(st.st_mode) or stat.S_IMODE(st.st_mode)!=mode:
        raise RuntimeError('unsafe filesystem object')
def runtime():
    if os.getuid()!=UID or os.geteuid()!=UID: raise RuntimeError('UID rejected')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in ROOT.strip('/').split('/'):
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=nxt
        checked(os.fstat(fd),stat.S_ISDIR,0o700)
        try: os.mkdir(NAME,0o700,dir_fd=fd)
        except FileExistsError: pass
        nxt=os.open(NAME,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        try: checked(os.fstat(nxt),stat.S_ISDIR,0o700)
        except BaseException:
            os.close(nxt); raise
        return nxt,ROOT+'/'+NAME+'/'+SOCKET
    finally: os.close(fd)
def peer(s):
    _,uid,_=struct.unpack('3i',s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    if uid!=UID: raise RuntimeError('peer rejected')
def relay(s):
    s.setblocking(False)
    original={fd:os.get_blocking(fd) for fd in (0,1)}
    for fd in original: os.set_blocking(fd,False)
    outgoing,incoming=bytearray(),bytearray()
    read_stdin=read_sock=True
    shut=False; deadline=None
    try:
        while True:
            if not read_stdin and not outgoing and not shut:
                s.shutdown(socket.SHUT_WR); shut=True
            if not read_sock and not read_stdin and not incoming and not outgoing: return
            if deadline is not None and time.monotonic()>=deadline: return
            readers=([0] if read_stdin and len(outgoing)<LIMIT else [])+([s] if read_sock and len(incoming)<LIMIT else [])
            writers=([s] if outgoing else [])+([1] if incoming else [])
            r,w,_=select.select(readers,writers,[],0.2)
            for source,queue in ((0,outgoing),(s,incoming)):
                if source not in r: continue
                try: data=os.read(0,min(65536,LIMIT-len(queue))) if source==0 else s.recv(min(65536,LIMIT-len(queue)))
                except BlockingIOError: continue
                if data: queue.extend(data)
                else:
                    if source==0: read_stdin=False
                    else: read_sock=False
                    if deadline is None: deadline=time.monotonic()+GRACE
            for target,queue in ((s,outgoing),(1,incoming)):
                if target not in w: continue
                try:
                    n=os.write(1,queue) if target==1 else s.send(queue)
                    del queue[:n]
                except BlockingIOError: pass
    finally:
        for fd,blocking in original.items(): os.set_blocking(fd,blocking)
def publish(dfd,path):
    lock=os.open('publisher.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=dfd)
    listener=socket.socket(socket.AF_UNIX); own=None
    try:
        checked(os.fstat(lock),stat.S_ISREG,0o600)
        if os.fstat(lock).st_nlink!=1: raise RuntimeError('lock links rejected')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: st=os.stat(SOCKET,dir_fd=dfd,follow_symlinks=False)
        except FileNotFoundError: st=None
        if st is not None:
            checked(st,stat.S_ISSOCK,0o600)
            probe=socket.socket(socket.AF_UNIX)
            probe.settimeout(0.5)
            try: probe.connect(path)
            except OSError as exc:
                if exc.errno!=errno.ECONNREFUSED: raise
            else: raise RuntimeError('live socket exists')
            finally: probe.close()
            os.unlink(SOCKET,dir_fd=dfd)
        listener.bind(path)
        os.chmod(SOCKET,0o600,dir_fd=dfd,follow_symlinks=False)
        own=os.stat(SOCKET,dir_fd=dfd,follow_symlinks=False)
        listener.listen(1)
        print('BRIDGE_READY',file=sys.stderr,flush=True)
        poller=select.poll()
        poller.register(0,select.POLLHUP|select.POLLERR|select.POLLNVAL)
        poller.register(listener,select.POLLIN)
        while True:
            events=dict(poller.poll(200))
            # HUP is visible even when unread protocol bytes remain in the pipe.
            # Never carry those bytes into a later transport/session.
            if events.get(0,0)&(select.POLLHUP|select.POLLERR|select.POLLNVAL): return
            if events.get(listener.fileno(),0)&select.POLLIN:
                client,_=listener.accept()
                with client:
                    peer(client); listener.close(); os.unlink(SOCKET,dir_fd=dfd)
                    print('BRIDGE_CONNECTED',file=sys.stderr,flush=True)
                    relay(client)
                return
    finally:
        listener.close()
        if own is not None:
            try:
                now=os.stat(SOCKET,dir_fd=dfd,follow_symlinks=False)
                if (now.st_dev,now.st_ino)==(own.st_dev,own.st_ino): os.unlink(SOCKET,dir_fd=dfd)
            except FileNotFoundError: pass
        os.close(lock)
def via(dfd,path):
    until=time.monotonic()+PUBLISHER_WAIT_SECONDS
    while True:
        remaining=until-time.monotonic()
        if remaining<=0:
            raise TimeoutError(
                f'publisher socket {SOCKET!r} did not appear within '
                f'{PUBLISHER_WAIT_SECONDS:g}s; check the session publisher launcher'
            )
        s=socket.socket(socket.AF_UNIX)
        s.settimeout(min(0.25,remaining))
        try:
            checked(os.stat(SOCKET,dir_fd=dfd,follow_symlinks=False),stat.S_ISSOCK,0o600)
            s.connect(path); peer(s); break
        except OSError as exc:
            s.close()
            if not isinstance(exc,TimeoutError) and exc.errno not in (errno.ENOENT,errno.ECONNREFUSED,errno.EAGAIN): raise
            time.sleep(min(PUBLISHER_POLL_SECONDS,max(0,until-time.monotonic())))
        except BaseException:
            s.close(); raise
    with s: relay(s)
def main():
    os.umask(0o077)
    if len(sys.argv)==2 and sys.argv[1]=='publish': mode=publish
    elif len(sys.argv)==3 and sys.argv[1]=='via' and sys.argv[2] in ALLOWED: mode=via
    else: raise RuntimeError('intent rejected')
    dfd,path=runtime()
    try: mode(dfd,path)
    finally: os.close(dfd)
if __name__=='__main__':
    try: main()
    except (OSError,RuntimeError) as exc:
        print('herdr bridge stopped: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(1)
