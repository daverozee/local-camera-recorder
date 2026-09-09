import copy
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import discovery
import ptz
import recorder


class DiscoveryTests(unittest.TestCase):
    def test_scan_bounds(self):
        self.assertEqual(str(discovery.valid_network('10.0.0.181/24')),'10.0.0.0/24')
        for invalid in ['8.8.8.0/24','10.0.0.0/8','127.0.0.0/24','::/0']:
            with self.assertRaises(ValueError):discovery.valid_network(invalid)

    def test_onvif_announcements_do_not_escape_subnet(self):
        packet=b'''<Envelope><ProbeMatch><Types>dn:NetworkVideoTransmitter</Types>
            <Scopes>onvif://www.onvif.org/name/Porch%20camera</Scopes>
            <XAddrs>http://10.0.0.226:888/onvif/device_service http://8.8.8.8/ http://user:password@10.0.0.7/ file:///secret</XAddrs>
            </ProbeMatch></Envelope>'''
        result=discovery.parse_probe(packet,ipaddress.ip_network('10.0.0.0/24'))
        self.assertEqual(result,[dict(host='10.0.0.226',onvif_port=888,name='Porch camera')])
        self.assertEqual(discovery.parse_probe(b'<broken',ipaddress.ip_network('10.0.0.0/24')),[])

    def test_neighbor_retry_finds_slow_camera_without_ping(self):
        counts={}
        def identify(host,extra=()):
            counts[host]=counts.get(host,0)+1
            if host=='10.0.0.1' and counts[host]>1:
                return dict(host=host,confirmed=True,brand='Foscam')
        with patch.object(discovery,'ws_discover',return_value=[]),patch.object(discovery,'mac_addresses',return_value={'10.0.0.1':'AA-BB-CC'}),patch.object(discovery,'identify',side_effect=identify):
            result=discovery.discover('10.0.0.0/30')
        self.assertEqual(result[0]['host'],'10.0.0.1')
        self.assertEqual(counts['10.0.0.1'],2)

    def test_rediscovery_preserves_credentials_and_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';state.mkdir()
            config={'settings':{'storage_root':str(root/'video'),'segment_seconds':60,'retention_days':7,'cleanup_enabled':False,'min_free_gb':5},
                    'cameras':[dict(id='kept-id',name='My porch',host='10.0.0.67',port=88,stream='videoMain',mode='copy',username='local-user',secret='encrypted-placeholder',audio=False)]}
            (state/'config.json').write_text(json.dumps(config))
            manager=recorder.Manager(state)
            found=[dict(host=ip,port=88,control_port=88,confirmed=True,brand='Foscam',mac=ip,evidence=['RTSP']) for ip in ['10.0.0.67','10.0.0.96','10.0.0.105','10.0.0.142','10.0.0.226']]
            try:
                with patch.object(discovery,'discover',return_value=found):
                    manager.run_discovery('10.0.0.0/24');manager.run_discovery('10.0.0.0/24')
                self.assertEqual(len(manager.config['cameras']),5)
                kept=manager.camera('kept-id')
                self.assertEqual(kept['name'],'My porch');self.assertEqual(kept['secret'],'encrypted-placeholder')
                self.assertFalse(any(c.get('sweep_enabled') for c in manager.config['cameras']))
            finally:manager.close()


class FakeManager:
    def __init__(self,camera):
        self.camera=camera;self.pending=[]
        self.recording=type('Recording',(),{'status':'recording','thread':type('Thread',(),{'is_alive':lambda self:True})()})()
        self.jobs={camera['id']:self.recording}
    def camera_password(self,camera):return 'synthetic-password'
    def pending_sweep(self,cid,value):self.pending.append(value)


class PTZTests(unittest.TestCase):
    def setUp(self):
        self.camera=dict(id='test',host='10.0.0.1',username='synthetic',sweep_map='Horizental')

    def test_reads_actual_track_names_and_rejects_fixed_camera(self):
        with patch.object(ptz,'cgi',return_value={'result':'0','cnt':'2','map0':'Horizental','map1':'Vertical'}):
            self.assertEqual(ptz.cruise_maps(self.camera,'pw'),['Horizental','Vertical'])
        with patch.object(ptz,'cgi',return_value={'result':'0','cnt':'0'}):
            with self.assertRaisesRegex(ptz.CameraError,'No cruise tracks'):ptz.cruise_maps(self.camera,'pw')

    def test_sweep_starts_only_after_video_and_stops_on_cancel(self):
        manager=FakeManager(self.camera);manager.recording.status='connecting';calls=[]
        def cgi(camera,password,command,params=None):
            calls.append(command)
            return {'result':'0','map0':'Horizental'}
        with patch.object(ptz,'cgi',side_effect=cgi):
            job=ptz.SweepJob(manager,self.camera)
            time.sleep(.6);self.assertNotIn('ptzStartCruise',calls)
            manager.recording.status='recording'
            for _ in range(30):
                if 'ptzStartCruise' in calls:break
                time.sleep(.05)
            self.assertIn('ptzStartCruise',calls)
            self.assertEqual(manager.pending[0],True)
            job.stop()
            self.assertIn('ptzStopCruise',calls);self.assertIn('ptzStopRun',calls)
            self.assertEqual(manager.pending[-1],False);self.assertFalse(job.thread.is_alive())

    def test_no_movement_if_capability_probe_fails(self):
        manager=FakeManager(self.camera);calls=[]
        def failure(camera,password,command,params=None):
            calls.append(command);raise ptz.CameraError('Unsupported')
        with patch.object(ptz,'cgi',side_effect=failure):
            job=ptz.SweepJob(manager,self.camera);job.thread.join(timeout=2)
        self.assertEqual(calls,['ptzGetCruiseMapList']);self.assertEqual(job.status,'error')

    def test_recording_pause_stops_active_sweep(self):
        manager=FakeManager(self.camera);calls=[]
        def cgi(camera,password,command,params=None):
            calls.append(command);return {'result':'0','map0':'Horizental'}
        with patch.object(ptz,'cgi',side_effect=cgi):
            job=ptz.SweepJob(manager,self.camera)
            for _ in range(30):
                if 'ptzStartCruise' in calls:break
                time.sleep(.05)
            manager.recording.status='paused'
            for _ in range(30):
                if 'ptzStopRun' in calls:break
                time.sleep(.05)
            self.assertIn('ptzStopRun',calls);self.assertFalse(job.active)
            job.stop()

    def test_timeout_after_start_keeps_persistent_stop_intent(self):
        manager=FakeManager(self.camera);calls=[]
        def fail_move(camera,password,command,params=None):
            calls.append(command)
            if command=='ptzGetCruiseMapList':return {'result':'0','map0':'Horizental'}
            raise ptz.CameraError('No response')
        with patch.object(ptz,'cgi',side_effect=fail_move):
            job=ptz.SweepJob(manager,self.camera);job.thread.join(timeout=4)
        self.assertIn('ptzStartCruise',calls);self.assertIn('ptzStopCruise',calls)
        self.assertEqual(manager.pending,[True]);self.assertEqual(job.status,'error')


if __name__=='__main__':unittest.main()
