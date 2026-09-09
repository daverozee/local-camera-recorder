import copy
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recorder


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='localcam-test-')
        self.root = Path(self.temp.name)
        self.state = self.root/'state'
        self.state.mkdir()
        self.config = {'settings':{'storage_root':str(self.root/'video'),'segment_seconds':60,
                      'retention_days':7,'cleanup_enabled':False,'min_free_gb':5},
                      'cameras':[dict(id='foscam-1',name='Test camera',host='10.0.0.67',port=88,
                                      stream='videoMain',mode='copy',username='',secret='',audio=False)]}
        (self.state/'config.json').write_text(json.dumps(self.config))
        self.manager = recorder.Manager(self.state)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_credentials_encrypted_and_not_returned(self):
        if os.name!='nt':self.skipTest('DPAPI requires Windows')
        camera={**self.config['cameras'][0],'username':'test-user','password':'a:@# 密碼'}
        self.manager.command('save_camera',camera)
        disk=(self.state/'config.json').read_text('utf-8')
        self.assertNotIn(camera['password'],disk)
        self.assertEqual(recorder.protect(self.manager.camera('foscam-1')['secret'],True),camera['password'])
        state=json.dumps(self.manager.status())
        self.assertNotIn('secret',state)
        self.assertNotIn(camera['password'],state)
        camera['password']=''
        self.manager.command('save_camera',camera)
        self.assertTrue(self.manager.status()['cameras'][0]['has_password'])

    def test_private_targets_and_encoded_credentials(self):
        for host in ['127.0.0.1','8.8.8.8','example.com','10.0.0.1/path']:
            with self.assertRaises(ValueError):recorder.validate_camera({**self.config['cameras'][0],'host':host})
        c={**self.config['cameras'][0],'username':'a@b'}
        self.assertIn('a%40b:p%3A%40%2F@10.0.0.67',recorder.stream_url(c,'p:@/'))

    def test_start_requires_credentials(self):
        with self.assertRaisesRegex(ValueError,'username and password'):
            self.manager.command('start',{'id':'foscam-1'})

    def test_sweep_disable_survives_failed_capability_refresh(self):
        camera=self.manager.camera('foscam-1')
        camera.update(sweep_enabled=True,sweep_map='Horizental',ptz_maps=[])
        sweep=Mock()
        self.manager.sweeps['foscam-1']=sweep
        self.manager.command('sweep',{'id':'foscam-1','enabled':False,'map':'Horizental'})
        sweep.stop.assert_called_once()
        self.assertFalse(camera['sweep_enabled'])

    def test_real_ffmpeg_authentication_failure_stops_retries(self):
        if os.name!='nt':self.skipTest('DPAPI requires Windows')
        class Unauthorized(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    while True:
                        request=self.request.recv(4096)
                        if not request:return
                        cseq=next((line.split(b':',1)[1].strip() for line in request.split(b'\r\n') if line.lower().startswith(b'cseq:')),b'1')
                        self.request.sendall(b'RTSP/1.0 401 Unauthorized\r\nCSeq: '+cseq+b'\r\nWWW-Authenticate: Digest realm="LocalCam test", nonce="1234"\r\nContent-Length: 0\r\n\r\n')
                except OSError:pass
        with socketserver.ThreadingTCPServer(('127.0.0.1',0),Unauthorized) as server:
            server.daemon_threads=True
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            cam=self.manager.camera('foscam-1')
            cam.update(host='127.0.0.1',port=server.server_address[1],username='synthetic-test',secret=recorder.protect('not-a-real-password'))
            self.manager.command('start',{'id':'foscam-1'})
            job=self.manager.jobs['foscam-1'];job.thread.join(timeout=15)
            self.assertFalse(job.thread.is_alive())
            self.assertEqual(job.status,'error')
            self.assertIn('rejected credentials',job.message)
            self.assertEqual(self.manager.command('clips',{})['total'],0)
            server.shutdown();thread.join(timeout=3)

    def test_disk_reserve_pauses_before_camera_connection(self):
        if os.name!='nt':self.skipTest('DPAPI requires Windows')
        self.manager.config['settings']['min_free_gb']=10**9
        self.manager.camera('foscam-1').update(username='test',secret=recorder.protect('synthetic'))
        self.manager.command('start',{'id':'foscam-1'})
        job=self.manager.jobs['foscam-1']
        for _ in range(30):
            if job.status=='paused':break
            time.sleep(.05)
        self.assertEqual(job.status,'paused')
        self.assertIsNone(job.proc)
        with self.assertRaisesRegex(ValueError,'Stop recording'):
            self.manager.command('settings',self.config['settings'])
        self.manager.command('stop',{'id':'foscam-1'})
        self.assertFalse(job.thread.is_alive())

    def test_manifest_index_recovery_and_retention_scope(self):
        start=time.time()-10*86400
        folder=self.root/'video'/'LocalCam'/'foscam-1'/'2026-01-01'/('a'*32)
        folder.mkdir(parents=True)
        complete=folder/'clip_000000.mp4';complete.write_bytes(b'closed video')
        partial=folder/'clip_000001.mp4';partial.write_bytes(b'active video')
        unrelated=folder/'my-important-file.mp4';unrelated.write_bytes(b'keep')
        (folder/'segments.csv').write_text('clip_000000.mp4,0,60\nclip_000001.mp4,60,\n')
        self.manager.new_session('foscam-1',folder,start)
        self.manager.index_session(folder.name,folder,start,'foscam-1')
        self.manager.index_session(folder.name,folder,start,'foscam-1')
        self.assertEqual(self.manager.command('clips',{})['total'],1)
        self.assertEqual(self.manager.cleanup(),0)
        self.manager.config['settings']['cleanup_enabled']=True
        self.assertEqual(self.manager.cleanup(),1)
        self.assertFalse(complete.exists())
        self.assertTrue(partial.exists());self.assertTrue(unrelated.exists())
        self.assertEqual(self.manager.command('clips',{})['total'],0)

    def make_video(self):
        source=self.root/'source.mp4'
        subprocess.run([self.manager.ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',
                        'testsrc2=size=320x180:rate=15','-t','6','-c:v','libx264','-g','30',
                        '-pix_fmt','yuv420p',str(source)],check=True,creationflags=recorder.HIDDEN)
        folder=self.root/'video'/'LocalCam'/'foscam-1'/'2026-09-08'/('b'*32)
        folder.mkdir(parents=True)
        subprocess.run([self.manager.ffmpeg,'-hide_banner','-loglevel','error','-i',str(source),
                        *recorder.output_args(self.config['cameras'][0],2,folder)],check=True,creationflags=recorder.HIDDEN)
        sid=self.manager.new_session('foscam-1',folder,time.time())
        self.manager.index_session(sid,folder,time.time(),'foscam-1')
        return folder

    def test_real_video_segments_decode_and_index(self):
        self.make_video()
        clips=self.manager.command('clips',{})['items']
        self.assertEqual(len(clips),3)
        # Two reordered frames at 15 fps can shift the initial muxer timestamp.
        self.assertAlmostEqual(sum(c['duration'] for c in clips),6,delta=2/15+.01)
        for clip in clips:
            file=self.manager.command('clip',{'id':clip['id']})['path']
            result=subprocess.run([self.manager.ffmpeg,'-v','error','-i',file,'-f','null','-'],
                                  capture_output=True,creationflags=recorder.HIDDEN)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertGreater(Path(file).stat().st_size,1000)

    def test_node_api_ranges_and_local_access(self):
        self.make_video()
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        env={**os.environ,'LOCALCAM_PORT':str(port),'LOCALCAM_STATE':str(self.state),'LOCALCAM_PYTHON':sys.executable}
        node=subprocess.Popen(['node',str(recorder.APP/'server.js')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=recorder.HIDDEN)
        base=f'http://127.0.0.1:{port}'
        op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(url,headers=None,data=None):
            req=urllib.request.Request(base+url,headers=headers or {},data=data)
            return op.open(req,timeout=10)
        try:
            for _ in range(60):
                try:
                    with request('/api/state') as r:status=json.load(r)
                    break
                except (urllib.error.URLError,TimeoutError):time.sleep(.1)
            else:self.fail('Node server failed to start')
            self.assertNotIn('secret',json.dumps(status))
            with request('/api/clips') as r:clip=json.load(r)['items'][0]
            with request('/media/'+clip['id'],{'Range':'bytes=0-31'}) as r:
                self.assertEqual(r.status,206);self.assertEqual(len(r.read()),32)
                self.assertTrue(r.headers['Content-Range'].startswith('bytes 0-31/'))
            with request('/media/'+clip['id'],{'Range':'bytes=-16'}) as r:self.assertEqual(len(r.read()),16)
            for uri,headers,data,expected in [
                ('/api/state',{'Host':'evil.example'},None,403),
                ('/api/state',{'Origin':'https://evil.example'},None,403),
                ('/api/start',{'Content-Type':'application/json'},b'{"id":"foscam-1"}',403),
                ('/media/'+clip['id'],{'Range':'bytes=999999999-'},None,416),
                ('/media/..%2f..%2fconfig.json',{},None,404),
                ('/data/config.json',{},None,404)]:
                with self.assertRaises(urllib.error.HTTPError) as cm:request(uri,headers,data)
                self.assertEqual(cm.exception.code,expected)
            with request('/api/shutdown',{'X-LocalCam-Token':status['token'],'Content-Type':'application/json'},b'{}') as r:
                self.assertTrue(json.load(r)['ok'])
            node.wait(timeout=15)
        finally:
            if node.poll() is None:node.kill();node.wait(timeout=5)
            node.stdout.close();node.stderr.close()

    def test_graceful_ffmpeg_stop_finalizes_current_clip(self):
        folder=self.root/'stop-test';folder.mkdir()
        camera={**self.config['cameras'][0],'mode':'compatible'}
        proc=subprocess.Popen([self.manager.ffmpeg,'-hide_banner','-loglevel','error','-re','-f','lavfi',
                               '-i','testsrc2=size=160x90:rate=10',*recorder.output_args(camera,60,folder)],
                               stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=recorder.HIDDEN)
        time.sleep(2)
        recorder.stop_process(proc)
        self.assertEqual(proc.returncode,0,proc.stderr.read().decode())
        self.assertTrue((folder/'segments.csv').read_text().strip())
        proc.stdin.close();proc.stderr.close()


if __name__=='__main__':unittest.main()
