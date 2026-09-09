# LocalCam

A local recorder for your Foscam cameras, with network discovery and optional pan/tilt sweeps. Python supervises FFmpeg and maintains a SQLite recording index; a dependency-free Node.js server provides the controls and browser review interface.

## Open and connect

1. Double-click **Start LocalCam.cmd**, or run `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -OpenBrowser` from this folder.
2. Open **http://127.0.0.1:8765** on this computer.
3. Click **Connect camera**, enter the camera's own local username and password, and save. This may differ from your Foscam cloud account. Repeat for the other cameras.
4. Click **Start recording** on each camera. Once video arrives the status changes to Recording. Completed clips appear after approximately one minute.
5. Filter the library by camera/date, then **Review** or **Download clip**.

Five camera addresses were confirmed: 10.0.0.67, 10.0.0.96, 10.0.0.105, 10.0.0.142 and 10.0.0.226. RTSP port 88. Main stream `/videoMain`, optional smaller `/videoSub`. Reserve these IP addresses in your router so camera addresses remain stable.

## Discovery and camera sweeps

Click **Discover cameras → Scan network**. The default home subnet is 10.0.0.0/24; change it if your router uses another private subnet. Discovery listens for ONVIF WS-Discovery announcements, tests every address on camera-related TCP ports including 88, 554 and 888, and retries unidentified neighbor devices. It does not require ping responses. Allow about two minutes. Confirmed Foscam RTSP endpoints are added automatically; generic ONVIF/RTSP candidates are identified separately. Existing names and credentials are preserved. Repeat scans deduplicate by IP and, when available, MAC address. Add camera also supports a manually entered private IP and custom ports. The app's native stream/control paths target Foscam HD cameras, not arbitrary ONVIF brands.

After saving the camera's **local camera account**, click **Check support** under Pan & tilt. This reads the camera's own cruise-track list without moving it. If it reports tracks, choose one (often Horizontal/Horizental or Vertical), then check **Sweep while recording**. No movement is requested until recording is receiving video. Each pass requests the camera's native cruise for up to 20 seconds, sends stop commands, and pauses for 10 seconds before another pass. The camera's existing limits, speed and cruise settings remain unchanged; a camera may finish its configured track before the pass ends. A custom track configured in Foscam can combine left/right and up/down movements.

Unchecking the option, stopping recording, a detected stream interruption, low disk pause or clean app shutdown sends cruise/tilt stop commands. Failed or uncertain movement requests persist a pending-stop flag. LocalCam retries unconfirmed stops every 30 seconds and after restart. **If the camera/network becomes unreachable or the PC crashes, an immediate stop cannot be guaranteed; use the Foscam app or camera controls to stop it.** The UI reports unconfirmed stops instead of claiming movement stopped.

Fixed cameras or cameras/accounts that do not report native Foscam cruise tracks cannot enable this sweep feature. A read-only account may detect tracks but lack permission to start movement; use an operator-capable local camera account. HTTPS controls require a trusted camera certificate. HTTP control port 88 is the default on these cameras; HTTP control requests carry camera credentials on the local network, so use only on your trusted LAN. The control port is separate from the RTSP port and can be edited.

Protocol reference: [Foscam cruise setup](https://www.foscam.com/faqs/view.html?id=121) and the [Foscam CGI user guide](https://manualzz.com/doc/23657609/foscam-cgi-ip-camera-user-guide).

Recordings go to **C:\CameraRecordings\LocalCam\camera-id\UTC-date\session-id\clip_000000.mp4**. The app displays clip times in your computer's local timezone. Folder dates use UTC. A SQLite index keeps each file's start time, duration and size. Use the browser download button to export a clip. Filenames alone do not contain exact wall-clock times; retain the app's data directory with your archive.

## Recording behavior

- **Original** mode copies the camera video without re-encoding, keeping CPU use low. MP4 playback depends on the camera codec and browser. H.264 works most broadly; H.265 may need VLC or a compatible browser.
- **Compatible** mode re-encodes to H.264 for browser playback and uses more CPU. This affects future clips only.
- Audio is off by default. Enable it per camera to include audio, converted to AAC.
- Recording uses continuous RTSP over TCP and fragmented MP4 segments. Camera keyframe intervals determine exact clip boundaries in Original mode. Timestamps approximate the local connection-start time, not forensic-grade synchronized camera timestamps.
- Stream disconnects reconnect with backoff. Stalled streams restart after about 45 seconds. Authentication errors stop retries until credentials are corrected and recording is restarted.
- Stops cleanly finalize clips. An abrupt crash can leave the current fragment unindexed; the app only lists completed manifest entries. Existing completed manifests are recovered on app startup.
- Recording pauses below **5 GB free** and checks for recovery. This is a reserve, not a strict storage quota; temporary overshoot is possible with concurrent cameras.
- Automatic deletion is **off** initially. The settings form offers seven-day retention. Enabling it authorizes deletion of LocalCam's indexed expired clips, including files in earlier recording folders. Cleanup never recursively deletes folders or unrelated files. Empty session folders/manifests remain.
- Recording continues in the background while this app process runs. Closing the browser does not stop recording. Use Stop recording or **Stop LocalCam.cmd** for a clean stop.
- The computer must remain awake and connected. This build does not install a Windows service, change power settings, start at boot, or resume camera recording automatically after an app/PC restart.

## Privacy and local security

No cloud storage or external web assets. The web interface binds exclusively to 127.0.0.1 and checks Host, Origin and a per-run token for changes. It is intended for the current computer, not LAN/Internet hosting.

Camera passwords are stored in `data/config.json` as Windows DPAPI ciphertext bound to the current Windows user. APIs do not return passwords or credential-bearing URLs. Camera RTSP credentials necessarily appear in the local FFmpeg process arguments while recording; Windows administrators and sufficiently privileged local software can inspect those. RTSP over TCP itself does not encrypt footage on the LAN. Disk video is not encrypted by this app; use Windows disk encryption and appropriate account access if needed.

## Components and installation

- Python 3.11+ (run `setup.ps1` to create a local `.venv`; it is not committed).
- Node.js 20+ (the launcher can use the installed Codex Node runtime on this computer).
- `imageio-ffmpeg==0.6.0`, which supplies an FFmpeg binary. No npm packages are needed.

On a fresh installation run `powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1`, then double-click **Start LocalCam.cmd**. Credentials, the recording index, logs, video and Python environment are excluded from Git. Set `LOCALCAM_FFMPEG` to use another FFmpeg executable. For direct server invocation, `LOCALCAM_PYTHON` selects Python, `LOCALCAM_STATE` selects the config/index folder, and `LOCALCAM_PORT` defaults to 8765. Launch scripts use default port 8765.

Core files: `recorder.py`, `server.js`, and `public/`. Run checks with `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.

References: [Foscam RTSP endpoints](https://download.foscam.com/faqs/view.html?id=81), [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment_002c-stream_005fsegment_002c-ssegment), [FFmpeg RTSP transport](https://ffmpeg.org/ffmpeg-protocols.html#rtsp).
