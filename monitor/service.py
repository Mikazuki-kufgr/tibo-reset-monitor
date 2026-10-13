"""Single supervised local worker, without changing any existing bot service."""
from __future__ import annotations
import ctypes,json,os,pathlib,subprocess,sys,time,uuid
from datetime import datetime,timezone
from .model import iso

def atomic(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temp,path)

def process_stamp(pid):
    if os.name!='nt':
        return None
    from ctypes import wintypes
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    k.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=k.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:
        exit_code=wintypes.DWORD()
        if not k.GetExitCodeProcess(handle,ctypes.byref(exit_code)) or exit_code.value!=259:return None
        fields=[wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle,*[ctypes.byref(x) for x in fields]):return None
        return (fields[0].dwHighDateTime<<32)|fields[0].dwLowDateTime
    finally:k.CloseHandle(handle)

def status(runtime):
    path=runtime/'service.json'
    if not path.exists():return {'status':'not_started'}
    value=json.loads(path.read_text('utf-8'))
    if value.get('status')=='running' and os.name=='nt':
        stamp=process_stamp(int(value['pid']))
        if stamp is None or stamp!=value.get('process_stamp'):
            value['status']='stopped_or_stale'
    return value

def stop(runtime):
    value=status(runtime)
    if value.get('status')!='running':return value
    atomic(runtime/'stop.json',{'nonce':value['nonce']})
    return {'status':'stop_requested','pid':value['pid']}

def daemon(config_path,state_path,interval=300,private_path=None,cycle_timeout=240):
    from .__main__ import lock
    runtime=state_path.parent
    project=pathlib.Path(__file__).resolve().parents[1]
    runtime.mkdir(parents=True,exist_ok=True)
    with lock(runtime/'daemon.lock'):
        nonce=str(uuid.uuid4())
        value={'status':'running','pid':os.getpid(),'process_stamp':process_stamp(os.getpid()),'nonce':nonce,'started_at':iso(datetime.now(timezone.utc)),'project':str(project),'python':sys.executable,'interval_seconds':interval,'last_exit_code':None,'last_completed_at':None,'cycle_count':0}
        atomic(runtime/'service.json',value)
        def stopping():
            try:return json.loads((runtime/'stop.json').read_text('utf-8')).get('nonce')==nonce
            except (OSError,ValueError):return False
        try:
            while not stopping():
                started=time.monotonic()
                log=runtime/'worker.log'
                if log.exists() and log.stat().st_size>1_000_000:
                    os.replace(log,runtime/'worker.previous.log')
                cmd=[sys.executable,'-B','-m','monitor','run','--config',str(config_path),'--state',str(state_path),'--send']
                if private_path:cmd.extend(['--private-channels',str(private_path)])
                with log.open('ab') as output:
                    child=subprocess.Popen(cmd,cwd=project,stdout=output,stderr=output)
                    value['child_pid']=child.pid;atomic(runtime/'service.json',value)
                    while child.poll() is None:
                        if stopping() or time.monotonic()-started>cycle_timeout:
                            # Only the Popen object created in this cycle.
                            child.terminate()
                            try:child.wait(timeout=15)
                            except subprocess.TimeoutExpired:child.kill();child.wait()
                            break
                        time.sleep(0.5)
                value.update(last_exit_code=child.returncode,last_completed_at=iso(datetime.now(timezone.utc)),cycle_count=value['cycle_count']+1)
                value.pop('child_pid',None);atomic(runtime/'service.json',value)
                while not stopping() and time.monotonic()-started<interval:time.sleep(0.5)
        finally:
            value['status']='stopped';value['stopped_at']=iso(datetime.now(timezone.utc));atomic(runtime/'service.json',value)
