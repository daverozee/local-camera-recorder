"""Foscam native cruise control. No calibration, preset or camera setting changes."""
import re
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode,unquote
from urllib.request import build_opener,ProxyHandler,HTTPSHandler
from discovery import NoRedirect


class CameraError(ValueError):pass


def cgi(camera,password,command,params=None):
    # Only the four explicit, bounded commands in this module are accepted.
    if command not in ('ptzGetCruiseMapList','ptzStartCruise','ptzStopCruise','ptzStopRun'):
        raise CameraError('Unsupported pan/tilt command.')
    scheme='https' if camera.get('control_https',False) else 'http'
    port=camera.get('control_port',88)
    query=urlencode(dict(cmd=command,usr=camera['username'],pwd=password,**(params or {})))
    url=f"{scheme}://{camera['host']}:{port}/cgi-bin/CGIProxy.fcgi?{query}"
    # For HTTPS controls require a trusted certificate; never fall back to HTTP silently.
    opener=build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(url,timeout=4) as response:
            raw=response.read(65536)
        root=ET.fromstring(raw)
        result=root.findtext('result')
        if result=='-2':raise CameraError('Camera rejected credentials or this account cannot control movement.')
        if result=='-3':raise CameraError('This camera/account does not support the Foscam cruise command.')
        if result!='0':raise CameraError('Camera did not accept the pan/tilt command.')
        return {x.tag:x.text or '' for x in root}
    except CameraError:raise
    except Exception:
        # URLs contain credentials: never propagate raw network exceptions.
        raise CameraError('Could not reach the camera control service. Check its control port and credentials.') from None


def cruise_maps(camera,password):
    result=cgi(camera,password,'ptzGetCruiseMapList')
    maps=[unquote(v) for k,v in result.items() if re.fullmatch(r'map\d+',k) and v and len(v)<=128]
    if not maps:raise CameraError('No cruise tracks reported. This may be a fixed camera; configure a track in Foscam if it supports pan/tilt.')
    return list(dict.fromkeys(maps))[:8]


class SweepJob:
    """One native cruise at a time, stopped on cancellation or recorder interruption."""
    def __init__(self,manager,camera):
        self.manager=manager;self.camera=dict(camera);self.cancel=threading.Event()
        self.message='Sweep armed; waiting for recording';self.status='waiting';self.active=False
        self.started_at=0
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def stop_motion(self,password):
        success=False
        for _ in range(3):
            outcomes=[]
            for command in ('ptzStopCruise','ptzStopRun'):
                try:
                    cgi(self.camera,password,command);outcomes.append(True)
                except CameraError:outcomes.append(False)
            if all(outcomes):success=True;break
            time.sleep(.3)
        if success:
            self.manager.pending_sweep(self.camera['id'],False)
            self.active=False
        else:
            self.status='error';self.message='Stop could not be confirmed. Use the Foscam app to stop movement; LocalCam will retry.'
        return success

    def run(self):
        password=''
        try:
            password=self.manager.camera_password(self.camera)
            maps=cruise_maps(self.camera,password)
            selected=self.camera.get('sweep_map','')
            if selected not in maps:raise CameraError('Select one of this camera’s reported cruise tracks.')
            while not self.cancel.wait(.5):
                recording=self.manager.jobs.get(self.camera['id'])
                if not recording or not recording.thread.is_alive():break
                if recording.status!='recording':
                    if self.active and not self.stop_motion(password):return
                    self.status='waiting';self.message='Sweep paused until recording resumes'
                    continue
                if not self.active:
                    # Persist stop intent BEFORE sending a movement request, including timeout cases.
                    self.manager.pending_sweep(self.camera['id'],True)
                    self.active=True
                    cgi(self.camera,password,'ptzStartCruise',{'mapName':selected})
                    self.started_at=time.monotonic()
                    self.status='sweeping';self.message=f'Cruise requested: {selected}'
                elif time.monotonic()-self.started_at>=20:
                    if not self.stop_motion(password):return
                    self.status='resting';self.message='Sweep pause · next pass in 10 seconds'
                    if self.cancel.wait(10):break
                # Native camera track controls travel limits/speed/duration; do not overwrite them.
        except CameraError as e:
            self.status='error';self.message=str(e)
        except Exception:
            self.status='error';self.message='Sweep could not start. Check camera settings.'
        finally:
            if self.active:self.stop_motion(password)
            if self.status!='error':self.status='stopped';self.message='Sweep stopped'

    def stop(self):
        self.cancel.set();self.thread.join(timeout=32)
        if self.thread.is_alive():raise CameraError('Sweep is still stopping. Check the camera in Foscam.')
