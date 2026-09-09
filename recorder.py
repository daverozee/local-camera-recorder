"""LocalCam: stdlib Python recorder, controlled through JSON-lines on stdin/stdout."""
import base64
import csv
import ctypes
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import quote
import discovery
import ptz

APP = Path(__file__).resolve().parent
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def protect(password, decrypt=False):
    """Windows DPAPI binds stored camera credentials to the current Windows user."""
    if os.name != 'nt':
        raise ValueError('Saved credentials require Windows DPAPI on this build.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(password) if decrypt else password.encode('utf-8')
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_int
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('Windows could not unlock/save this credential.')
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def validate_camera(data):
    name = str(data.get('name', '')).strip()
    if not 1 <= len(name) <= 80:
        raise ValueError('Give the camera a name (1–80 characters).')
    host = str(data.get('host', ''))
    try:
        ip = ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError('Enter a private IPv4 camera address.')
    if not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
        raise ValueError('Only private home-network camera addresses are supported.')
    port = int(data.get('port', 88))
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    stream = data.get('stream', 'videoMain')
    mode = data.get('mode', 'copy')
    if stream not in ('videoMain', 'videoSub') or mode not in ('copy', 'compatible'):
        raise ValueError('Invalid stream or recording mode.')
    user = str(data.get('username', '')).strip()
    if len(user) > 128 or any(ord(c) < 32 for c in user):
        raise ValueError('Invalid camera username.')
    control_port=int(data.get('control_port',88))
    if not 1<=control_port<=65535:raise ValueError('Invalid camera control port.')
    return dict(name=name, host=host, port=port, stream=stream, mode=mode,
                username=user, audio=bool(data.get('audio', False)),
                control_port=control_port,control_https=bool(data.get('control_https',False)))


def stream_url(camera, password):
    return (f"rtsp://{quote(camera['username'], safe='')}:{quote(password, safe='')}@"
            f"{camera['host']}:{camera['port']}/{camera['stream']}")


def output_args(camera, seconds, folder):
    codec = ['-c:v', 'copy'] if camera['mode'] == 'copy' else [
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
        '-force_key_frames', f'expr:gte(t,n_forced*{seconds})']
    audio = ['-map','0:a:0?','-c:a','aac','-b:a','96k'] if camera['audio'] else ['-an']
    return ['-map','0:v:0', *audio, *codec, '-f','segment', '-segment_time',str(seconds),
            '-reset_timestamps','1','-segment_format','mp4',
            '-segment_format_options','movflags=+frag_keyframe+empty_moov+default_base_moof',
            '-segment_list',str(folder/'segments.csv'), '-segment_list_type','csv',
            str(folder/'clip_%06d.mp4')]


def stop_process(proc):
    if proc.poll() is not None:
        return
    try:
        proc.stdin.write(b'q\n')
        proc.stdin.flush()
        proc.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait(timeout=5)


class CameraJob:
    def __init__(self, manager, camera):
        self.manager = manager
        self.camera = dict(camera)
        self.cancel = threading.Event()
        self.status = 'connecting'
        self.message = 'Opening camera stream…'
        self.session = None
        self.proc = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        failures = 0
        while not self.cancel.is_set():
            try:
                settings = self.manager.config['settings'].copy()
                root = Path(settings['storage_root'])
                root.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(root).free < settings['min_free_gb'] * 1024**3:
                    self.status, self.message = 'paused', 'Low disk space. Recording will resume when space is available.'
                    self.cancel.wait(15)
                    continue
                folder = root/'LocalCam'/self.camera['id']/dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')/uuid.uuid4().hex
                folder.mkdir(parents=True)
                start = time.time()
                self.session = self.manager.new_session(self.camera['id'], folder, start)
                url = stream_url(self.camera, protect(self.camera['secret'], decrypt=True))
                args = [self.manager.ffmpeg,'-hide_banner','-loglevel','warning',
                        '-rtsp_transport','tcp','-timeout','10000000','-i',url,
                        *output_args(self.camera, settings['segment_seconds'], folder)]
                self.status, self.message = 'connecting', 'Waiting for video…'
                errors = queue.Queue(maxsize=50)
                self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.PIPE, creationflags=HIDDEN)
                def read_errors(proc):
                    for line in iter(proc.stderr.readline, b''):
                        text = line.decode('utf-8','replace').lower()
                        category = ('auth' if '401' in text or 'unauthorized' in text else
                                    'codec' if 'codec' in text or 'could not find' in text else 'connection')
                        try:
                            errors.put_nowait(category)
                        except queue.Full:
                            pass
                error_reader = threading.Thread(target=read_errors,args=(self.proc,),daemon=True)
                error_reader.start()
                last_size, last_growth = 0, time.monotonic()
                while self.proc.poll() is None and not self.cancel.wait(1):
                    self.manager.index_session(self.session, folder, start, self.camera['id'])
                    size = sum(p.stat().st_size for p in folder.glob('clip_*.mp4') if p.is_file())
                    if size > last_size:
                        last_size, last_growth = size, time.monotonic()
                        self.status, self.message = 'recording', 'Saving video locally'
                        failures = 0
                    if time.monotonic() - last_growth > 45:
                        self.message = 'Stream stalled. Reconnecting…'
                        break
                    if shutil.disk_usage(root).free < settings['min_free_gb'] * 1024**3:
                        break
                stop_process(self.proc)
                error_reader.join(timeout=2)
                self.manager.index_session(self.session, folder, start, self.camera['id'])
                categories = []
                while not errors.empty():
                    categories.append(errors.get_nowait())
                if self.cancel.is_set():
                    break
                if 'auth' in categories:
                    self.status, self.message = 'error', 'Camera rejected credentials. Edit credentials, then start again.'
                    return
                failures += 1
                self.status = 'reconnecting'
                self.message = ('Video format unavailable; try Compatible mode.' if 'codec' in categories
                                else 'Camera unavailable. Retrying automatically…')
            except Exception:
                self.status, self.message = 'error', 'Recorder could not access the stream or storage. Check credentials, disk and settings.'
                failures += 1
            finally:
                if self.proc is not None:
                    stop_process(self.proc)
                    self.proc.stdin.close()
                    self.proc.stderr.close()
                    self.proc = None
            self.cancel.wait(min(60, 5 * 2**min(failures, 4)))
        self.status, self.message = 'stopped', 'Recording stopped'

    def stop(self):
        self.cancel.set()
        self.thread.join(timeout=25)
        if self.thread.is_alive():
            raise ValueError('Recorder is still stopping; retry shortly.')


class Manager:
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir or os.environ.get('LOCALCAM_STATE', APP/'data'))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.state_dir/'config.json'
        self.lock = threading.RLock()
        self.jobs = {}
        self.sweeps = {}
        self.discovery_status = {'running':False,'done':0,'total':0,'subnet':'10.0.0.0/24',
                                 'results':[],'message':'Discover cameras on your LAN. Expected: 5.'}
        cache=self.state_dir/'discovery.json'
        if cache.exists():
            try:
                previous=json.loads(cache.read_text('utf-8-sig'))
                if isinstance(previous,dict) and not previous.get('running'):
                    self.discovery_status.update(previous,running=False)
                    self.discovery_status['message']='Last scan: '+str(previous.get('message',''))
            except (OSError,ValueError):pass
        self.discovery_thread = None
        self.closed = threading.Event()
        self.maintenance_error = None
        self.db = sqlite3.connect(self.state_dir/'recordings.sqlite',check_same_thread=False)
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,camera TEXT,folder TEXT,start REAL);
            CREATE TABLE IF NOT EXISTS clips(id TEXT PRIMARY KEY,camera TEXT,path TEXT UNIQUE,start REAL,duration REAL,size INTEGER);
            CREATE INDEX IF NOT EXISTS clips_start ON clips(start);
        ''')
        if self.config_file.exists():
            self.config = json.loads(self.config_file.read_text('utf-8'))
        else:
            self.config = {'settings':{'storage_root':str(Path('C:/CameraRecordings') if os.name=='nt' else APP/'recordings'),
                          'segment_seconds':60,'retention_days':7,'cleanup_enabled':False,'min_free_gb':5},
                          'cameras':[dict(id=f'foscam-{n}',name=f'Foscam {n}',host=ip,port=88,stream='videoMain',
                                          mode='copy',username='',secret='',audio=False)
                                     for n,ip in enumerate(['10.0.0.67','10.0.0.96','10.0.0.142','10.0.0.105','10.0.0.226'],1)]}
            self.save()
        for cam in self.config['cameras']:
            for key,value in dict(control_port=88,control_https=False,sweep_enabled=False,sweep_map='',
                                  ptz_maps=[],ptz_message='Check pan/tilt support after saving credentials',sweep_pending=False).items():
                cam.setdefault(key,value)
        self.save()
        Path(self.config['settings']['storage_root']).mkdir(parents=True,exist_ok=True)
        try:
            import imageio_ffmpeg
            self.ffmpeg = os.environ.get('LOCALCAM_FFMPEG') or shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            self.ffmpeg = os.environ.get('LOCALCAM_FFMPEG') or shutil.which('ffmpeg')
        with self.lock:
            for sid,cam,folder,start in self.db.execute('SELECT id,camera,folder,start FROM sessions').fetchall():
                self.index_session(sid,Path(folder),start,cam)
        self.maintenance = threading.Thread(target=self.maintain,daemon=True)
        self.maintenance.start()

    def save(self):
        temp = self.config_file.with_suffix('.tmp')
        temp.write_text(json.dumps(self.config,indent=2),encoding='utf-8')
        os.replace(temp,self.config_file)

    def new_session(self, camera, folder, start):
        sid = folder.name
        with self.lock:
            self.db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(sid,camera,str(folder),start))
            self.db.commit()
        return sid

    def index_session(self, sid, folder, start, camera):
        manifest = folder/'segments.csv'
        if not manifest.exists():
            return
        try:
            lines = manifest.read_text('utf-8').splitlines()
        except OSError:
            return
        with self.lock:
            for row in csv.reader(lines):
                if len(row) != 3:
                    continue
                try:
                    filename = Path(row[0]).name
                    if not re.fullmatch(r'clip_\d{6}\.mp4',filename):
                        continue
                    path = (folder/filename).resolve()
                    if path.parent != folder.resolve() or not path.is_file():
                        continue
                    offset, end = float(row[1]), float(row[2])
                    if end <= offset:
                        continue
                    self.db.execute('INSERT OR IGNORE INTO clips VALUES (?,?,?,?,?,?)',
                                    (sid+'-'+filename[:-4],camera,str(path),start+offset,end-offset,path.stat().st_size))
                except (ValueError,OSError):
                    continue
            self.db.commit()

    def cleanup(self):
        settings = self.config['settings']
        if not settings['cleanup_enabled']:
            return 0
        cutoff = time.time()-settings['retention_days']*86400
        removed = 0
        with self.lock:
            rows = self.db.execute('SELECT id,path FROM clips WHERE start+duration < ?',(cutoff,)).fetchall()
            for cid, value in rows:
                path = Path(value)
                # Delete only indexed files with our exact generated session/camera layout.
                if (not re.fullmatch(r'clip_\d{6}\.mp4',path.name) or
                    not re.fullmatch(r'[a-f0-9]{32}',path.parent.name) or
                    path.parents[3].name != 'LocalCam' or path.is_symlink() or
                    any(p.is_symlink() or (getattr(p.lstat(),'st_file_attributes',0) & 0x400)
                        for p in list(path.parents)[:4])):
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                self.db.execute('DELETE FROM clips WHERE id=?',(cid,))
                removed += 1
            self.db.commit()
        return removed

    def maintain(self):
        while not self.closed.wait(30):
            try:
                self.cleanup()
                self.retry_sweep_stops()
                self.maintenance_error = None
            except Exception:
                self.maintenance_error = 'Automatic cleanup could not run. Check storage access.'

    def status(self):
        with self.lock:
            cameras = []
            for c in self.config['cameras']:
                job = self.jobs.get(c['id'])
                item = {k:v for k,v in c.items() if k not in ('secret',)}
                sweep=self.sweeps.get(c['id'])
                item.update(has_password=bool(c['secret']), status=job.status if job else 'stopped',
                            message=job.message if job else 'Add credentials and start recording',
                            running=bool(job and job.thread.is_alive()),
                            sweep_status=sweep.status if sweep else ('error' if c.get('sweep_pending') else 'stopped'),
                            sweep_message=sweep.message if sweep else ('Movement stop pending; check the camera.' if c.get('sweep_pending') else c.get('ptz_message','')))
                cameras.append(item)
            count,size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM clips').fetchone()
            disk = shutil.disk_usage(self.config['settings']['storage_root'])
            return dict(cameras=cameras,settings=self.config['settings'],ffmpeg_available=bool(self.ffmpeg),
                        clips=count,bytes=size,free_bytes=disk.free,total_bytes=disk.total,
                        maintenance_error=self.maintenance_error,discovery=self.discovery_status)

    def camera_password(self,camera):
        return protect(camera['secret'],decrypt=True)

    def pending_sweep(self,cid,pending):
        with self.lock:
            self.camera(cid)['sweep_pending']=pending
            self.save()

    def retry_sweep_stops(self):
        for camera in list(self.config['cameras']):
            sweep=self.sweeps.get(camera['id'])
            if not camera.get('sweep_pending') or (sweep and sweep.thread.is_alive()):continue
            try:
                password=self.camera_password(camera)
                outcomes=[]
                for command in ('ptzStopCruise','ptzStopRun'):
                    try:ptz.cgi(camera,password,command);outcomes.append(True)
                    except ValueError:outcomes.append(False)
                if all(outcomes):
                    self.pending_sweep(camera['id'],False)
                    if sweep:sweep.status='stopped';sweep.message='Sweep stopped'
            except ValueError:pass

    def check_ptz(self,camera):
        if not camera['username'] or not camera['secret']:raise ValueError('Save this camera’s local username and password first.')
        try:
            maps=ptz.cruise_maps(camera,self.camera_password(camera))
        except ValueError as e:
            with self.lock:
                camera['ptz_maps']=[];camera['ptz_message']=str(e);self.save()
            raise
        with self.lock:
            camera['ptz_maps']=maps;camera['ptz_message']='Cruise tracks detected: '+', '.join(maps)
            if camera.get('sweep_map') not in maps:camera['sweep_map']=maps[0]
            self.save()
        return {'maps':maps,'message':camera['ptz_message']}

    def run_discovery(self,subnet):
        try:
            def progress(done,total):
                with self.lock:self.discovery_status.update(done=done,total=total,message=(f'Checking {done} of {total} addresses…' if done<total else 'Retrying slow and nearby devices…'))
            results=discovery.discover(subnet,progress)
            added=0
            with self.lock:
                for result in results:
                    if not result['confirmed']:continue
                    camera=next((c for c in self.config['cameras'] if c['host']==result['host']),None)
                    if camera is None and result.get('mac'):
                        camera=next((c for c in self.config['cameras'] if c.get('mac')==result['mac']),None)
                    if camera is None:
                        camera=dict(id='foscam-'+uuid.uuid4().hex[:10],name='Foscam '+str(len(self.config['cameras'])+1),
                            host=result['host'],port=result['port'],stream='videoMain',mode='copy',username='',secret='',audio=False,
                            sweep_enabled=False,sweep_map='',ptz_maps=[],ptz_message='Check pan/tilt support after saving credentials',sweep_pending=False)
                        self.config['cameras'].append(camera);added+=1
                    job=self.jobs.get(camera['id'])
                    # Preserve credentials, names, user-configured ports and running jobs.
                    if (not job or not job.thread.is_alive()) and not camera.get('sweep_pending'):
                        if camera['host']!=result['host']:
                            camera['host']=result['host'];camera['ptz_maps']=[];camera['sweep_enabled']=False
                    camera.setdefault('control_port',result['control_port'])
                    camera.setdefault('control_https',result.get('control_https',False))
                    camera['mac']=result.get('mac','');camera['last_seen']=time.time()
                self.save()
                confirmed=sum(bool(x['confirmed']) for x in results)
                self.discovery_status.update(running=False,results=results,finished=time.time(),
                    message=f'Found {confirmed} Foscam cameras; added {added}. Expected: 5.'+(' Check power, Wi-Fi or subnet for missing cameras.' if confirmed<5 else ''))
                (self.state_dir/'discovery.json').write_text(json.dumps(self.discovery_status,indent=2),'utf-8')
        except Exception:
            with self.lock:self.discovery_status.update(running=False,message='Discovery failed. Check the subnet and network connection.')

    def camera(self,cid):
        for cam in self.config['cameras']:
            if cam['id']==cid:
                return cam
        raise ValueError('Camera not found.')

    def command(self, method, data):
        if method == 'status':
            return self.status()
        if method == 'save_camera':
            existing=data.get('id')
            cam = self.camera(existing) if existing else dict(id='foscam-'+uuid.uuid4().hex[:10],secret='',ptz_maps=[],
                sweep_enabled=False,sweep_map='',sweep_pending=False,ptz_message='Check pan/tilt support after saving credentials')
            job = self.jobs.get(cam['id'])
            if job and job.thread.is_alive():
                raise ValueError('Stop this camera before editing its settings.')
            if cam.get('sweep_pending'):raise ValueError('Stop camera movement before changing its connection settings.')
            clean = validate_camera(data)
            if any(c['host']==clean['host'] and c['id']!=cam['id'] for c in self.config['cameras']):
                raise ValueError('This camera address is already in your list.')
            secret = cam['secret']
            if data.get('password'):
                if len(data['password'])>1024:
                    raise ValueError('Password is too long.')
                secret = protect(data['password'])
            with self.lock:
                cam.update(ptz_maps=[],sweep_enabled=False,sweep_map='',ptz_message='Check pan/tilt support after saving credentials')
                cam.update(clean,secret=secret)
                if not existing:self.config['cameras'].append(cam)
                self.save()
            return {'saved':True}
        if method == 'settings':
            if any(j.thread.is_alive() for j in self.jobs.values()):
                raise ValueError('Stop recording before changing storage settings.')
            root = Path(str(data.get('storage_root','')))
            if not root.is_absolute():
                raise ValueError('Use an absolute folder path, such as C:\\CameraRecordings.')
            seconds,days,free = int(data.get('segment_seconds',60)),int(data.get('retention_days',7)),float(data.get('min_free_gb',5))
            if seconds not in (30,60,120,300) or not 1 <= days <= 3650 or not 1 <= free <= 10000:
                raise ValueError('Invalid clip length, retention or disk reserve.')
            root.mkdir(parents=True,exist_ok=True)
            with self.lock:
                self.config['settings'] = dict(storage_root=str(root.resolve()),segment_seconds=seconds,
                    retention_days=days,min_free_gb=free,cleanup_enabled=bool(data.get('cleanup_enabled',False)))
                self.save()
            return {'saved':True}
        if method in ('start','stop'):
            cam = self.camera(data.get('id'))
            job = self.jobs.get(cam['id'])
            if method=='stop':
                sweep=self.sweeps.get(cam['id'])
                if sweep:sweep.cancel.set()
                if job:
                    job.stop()
                    self.jobs.pop(cam['id'],None)
                if sweep:sweep.stop()
            elif not job or not job.thread.is_alive():
                if not self.ffmpeg:
                    raise ValueError('FFmpeg is missing. Run setup.ps1.')
                if not cam['username'] or not cam['secret']:
                    raise ValueError('Save the camera username and password first.')
                if cam.get('sweep_pending'):raise ValueError('Previous movement stop is unconfirmed. Check the camera before restarting.')
                old_sweep=self.sweeps.get(cam['id'])
                if old_sweep and old_sweep.thread.is_alive():old_sweep.stop()
                self.jobs[cam['id']] = CameraJob(self,cam)
                if cam.get('sweep_enabled'):self.sweeps[cam['id']]=ptz.SweepJob(self,cam)
            return {'ok':True}
        if method=='discover':
            if self.discovery_status['running']:return {'started':False,'message':'Discovery is already running.'}
            subnet=str(discovery.valid_network(data.get('subnet') or discovery.default_network()))
            with self.lock:self.discovery_status.update(running=True,done=0,total=0,subnet=subnet,message='Searching ONVIF announcements…')
            self.discovery_thread=threading.Thread(target=self.run_discovery,args=(subnet,),daemon=True)
            self.discovery_thread.start()
            return {'started':True}
        if method=='ptz_probe':return self.check_ptz(self.camera(data.get('id')))
        if method=='sweep':
            camera=self.camera(data.get('id'));enabled=bool(data.get('enabled',False));sweep=self.sweeps.get(camera['id'])
            if enabled:
                if camera.get('sweep_pending') and not (sweep and sweep.thread.is_alive()):
                    raise ValueError('Previous movement stop is unconfirmed. Stop movement in Foscam first.')
                result=self.check_ptz(camera)
                selected=str(data.get('map',camera.get('sweep_map','')))
                if selected not in result['maps']:raise ValueError('Choose an available cruise track.')
                if sweep:sweep.stop()
                if camera.get('sweep_pending'):raise ValueError('Movement stop could not be confirmed.')
                with self.lock:camera.update(sweep_enabled=True,sweep_map=selected);self.save()
                job=self.jobs.get(camera['id'])
                if job and job.thread.is_alive():self.sweeps[camera['id']]=ptz.SweepJob(self,camera)
            else:
                selected=str(data.get('map',camera.get('sweep_map','')))
                # Disabling must always work, even after a capability refresh fails.
                if selected not in camera.get('ptz_maps',[]):selected=camera.get('sweep_map','')
                with self.lock:camera['sweep_enabled']=False;camera['sweep_map']=selected;self.save()
                if sweep:sweep.stop()
                self.retry_sweep_stops()
                if camera.get('sweep_pending'):raise ValueError('Stop could not be confirmed. Use Foscam to stop movement; automatic stop retries continue.')
            return {'ok':True}
        if method=='clips':
            camera = str(data.get('camera',''))
            date = str(data.get('date',''))
            clauses, params = [], []
            if camera:
                clauses.append('camera=?'); params.append(camera)
            if date:
                # UI supplies local calendar dates; use the recorder host's timezone.
                day = dt.datetime.strptime(date,'%Y-%m-%d')
                clauses.append('start>=? AND start<?')
                params.extend([day.timestamp(),(day+dt.timedelta(days=1)).timestamp()])
            where = ' WHERE '+' AND '.join(clauses) if clauses else ''
            offset = max(0,int(data.get('offset',0)))
            with self.lock:
                total = self.db.execute('SELECT COUNT(*) FROM clips'+where,params).fetchone()[0]
                rows = self.db.execute('SELECT id,camera,start,duration,size FROM clips'+where+' ORDER BY start DESC LIMIT 100 OFFSET ?',params+[offset]).fetchall()
            return {'total':total,'items':[dict(zip(('id','camera','start','duration','size'),r)) for r in rows]}
        if method=='clip':
            with self.lock:
                row = self.db.execute('SELECT path,size FROM clips WHERE id=?',(str(data.get('id','')),)).fetchone()
            if not row or not Path(row[0]).is_file():
                raise ValueError('Recording not found or already removed.')
            return {'path':row[0],'size':Path(row[0]).stat().st_size}
        raise ValueError('Unknown command.')

    def close(self):
        self.closed.set()
        for sweep in list(self.sweeps.values()):sweep.cancel.set()
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel.set()
        for job in jobs:
            try:job.stop()
            except ValueError:pass
        for sweep in list(self.sweeps.values()):
            try:sweep.stop()
            except ValueError:pass
        self.maintenance.join(timeout=5)
        self.db.close()


def main():
    manager = Manager()
    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                result = manager.command(request['method'],request.get('data',{}))
                reply = {'id':request['id'],'result':result}
            except ValueError as e:
                reply = {'id':request.get('id'),'error':str(e)}
            except Exception:
                reply = {'id':request.get('id'),'error':'Operation failed. Check local storage and configuration.'}
            print(json.dumps(reply),flush=True)
    finally:
        manager.close()


if __name__=='__main__':
    main()
