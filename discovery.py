"""Bounded LAN camera discovery: WS-Discovery plus TCP/RTSP/HTTP fingerprints."""
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, unquote
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, HTTPSHandler
from urllib.error import HTTPError

PORTS = (88,554,80,443,888,8080,8088,8554,8899,10088)
PRIVATE = [ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]


def valid_network(value):
    try:
        net=ipaddress.ip_network(value,strict=False)
    except ValueError:
        raise ValueError('Enter a private IPv4 subnet, for example 10.0.0.0/24.')
    if net.version!=4 or net.num_addresses>1024 or not any(net.subnet_of(p) for p in PRIVATE):
        raise ValueError('Discovery supports private IPv4 subnets of at most 1,024 addresses.')
    return net


def default_network():
    if os.name=='nt':
        cmd="Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway} | ForEach-Object {$_.IPv4Address | Select-Object IPAddress,PrefixLength} | ConvertTo-Json -Compress"
        try:
            raw=subprocess.check_output(['powershell.exe','-NoProfile','-Command',cmd],timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            values=json.loads(raw.decode('utf-8-sig'))
            if isinstance(values,dict):values=[values]
            for item in values:
                try:
                    return str(valid_network(f"{item['IPAddress']}/{item['PrefixLength']}"))
                except ValueError:pass
        except (OSError,ValueError,subprocess.SubprocessError):pass
    return '10.0.0.0/24'


def parse_probe(data, network):
    """Ignore off-subnet, malformed and non-HTTP discovery addresses."""
    results=[]
    try:
        root=ET.fromstring(data)
    except ET.ParseError:return results
    for match in root.iter():
        if match.tag.rsplit('}',1)[-1]!='ProbeMatch':continue
        scopes=' '.join((x.text or '') for x in match if x.tag.rsplit('}',1)[-1]=='Scopes')
        types=' '.join((x.text or '') for x in match if x.tag.rsplit('}',1)[-1]=='Types')
        if 'onvif' not in scopes.lower() and 'networkvideotransmitter' not in types.lower():continue
        name=''
        for scope in scopes.split():
            if '/name/' in scope:name=unquote(scope.split('/name/',1)[1])[:80]
        for node in match:
            if node.tag.rsplit('}',1)[-1]!='XAddrs':continue
            for value in (node.text or '').split():
                try:
                    u=urlsplit(value);ip=ipaddress.ip_address(u.hostname or '')
                    if ip not in network or u.scheme not in ('http','https') or u.username or u.password:continue
                    results.append(dict(host=str(ip),onvif_port=u.port or (443 if u.scheme=='https' else 80),name=name))
                except ValueError:continue
    return results


def ws_discover(network):
    found=[]
    probe=('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
           'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header><a:MessageID>uuid:'+str(uuid.uuid4())+
           '</a:MessageID><a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
           '<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action></s:Header>'
           '<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>').encode()
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
            s.settimeout(.5)
            # Route selection follows the requested local subnet.
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as route:
                route.connect((str(next(network.hosts())),3702));local=route.getsockname()[0]
            s.bind((local,0));s.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_IF,socket.inet_aton(local))
            for _ in range(2):s.sendto(probe,('239.255.255.250',3702))
            end=time.monotonic()+4
            while time.monotonic()<end:
                try:
                    raw,_=s.recvfrom(65535);found.extend(parse_probe(raw,network))
                except socket.timeout:continue
    except OSError:pass
    return found


def rtsp_fingerprint(host,port):
    try:
        with socket.create_connection((host,port),timeout=1.5) as s:
            s.settimeout(1.5)
            s.sendall(f'DESCRIBE rtsp://{host}:{port}/videoMain RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n'.encode())
            text=s.recv(8192).decode('utf-8','replace')
            return text.startswith('RTSP/'), 'foscam' in text.lower()
    except OSError:return False,False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def http_fingerprint(host,port):
    scheme='https' if port==443 else 'http'
    # No credentials are used during identification; do not follow advertised redirects.
    opener=build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=ssl._create_unverified_context()))
    try:
        with opener.open(f'{scheme}://{host}:{port}/',timeout=1.5) as response:
            body=response.read(24000).decode('utf-8','replace')
            return bool(re.search(r'<title[^>]*>\s*Foscam\s*</title>',body,re.I) or 'Foscam IPCam' in body)
    except (OSError,HTTPError):return False


def identify(host, extra_ports=()):
    opened=[]
    for port in dict.fromkeys((*PORTS,*extra_ports)):
        try:
            with socket.create_connection((host,port),timeout=1):opened.append(port)
        except OSError:pass
    if not opened:return None
    rtsp_port=None;foscam=False;evidence=[]
    for port in opened:
        if port in (443,888,8899):continue
        rtsp,branded=rtsp_fingerprint(host,port)
        if rtsp and rtsp_port is None:rtsp_port=port
        if branded:
            foscam=True;rtsp_port=port;evidence.append(f'Foscam RTSP response on {port}');break
    control=None
    if foscam:
        control=88 if 88 in opened else (rtsp_port if rtsp_port not in (554,8554) else next((p for p in (80,8080,8088) if p in opened),None))
    else:
        for port in opened:
            if port in (554,8554,888,8899):continue
            if http_fingerprint(host,port):
                foscam=True;control=port;evidence.append(f'Foscam web interface on {port}');break
    if not foscam and not rtsp_port:return None
    return dict(host=host,port=rtsp_port or 88,control_port=control or 88,
                control_https=control==443,brand='Foscam' if foscam else 'Unidentified RTSP',
                confirmed=foscam and bool(rtsp_port),evidence=evidence or [f'RTSP service on {rtsp_port}'],open_ports=opened)


def mac_addresses():
    if os.name!='nt':return {}
    try:
        raw=subprocess.check_output(['powershell.exe','-NoProfile','-Command',
            "Get-NetNeighbor -AddressFamily IPv4 | Where-Object {$_.LinkLayerAddress -ne '00-00-00-00-00-00'} | Select-Object IPAddress,LinkLayerAddress | ConvertTo-Json -Compress"],
            timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        rows=json.loads(raw.decode('utf-8-sig'))
        if isinstance(rows,dict):rows=[rows]
        return {x['IPAddress']:x['LinkLayerAddress'] for x in rows}
    except (OSError,ValueError,subprocess.SubprocessError):return {}


def discover(subnet,progress=lambda done,total:None):
    network=valid_network(subnet)
    announced=ws_discover(network)
    extras={x['host']:[x['onvif_port']] for x in announced}
    targets=list(map(str,network.hosts()))
    results={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
        futures={pool.submit(identify,host,extras.get(host,())):host for host in targets}
        for count,future in enumerate(concurrent.futures.as_completed(futures),1):
            try:
                item=future.result()
                if item:results[item['host']]=item
            except Exception:pass
            progress(count,len(targets))
    # Retry neighbor/ONVIF hosts that did not identify; do not require ping replies.
    macs=mac_addresses()
    retry=[h for h in set(macs)|set(extras) if h not in results and ipaddress.ip_address(h) in network]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for item in pool.map(lambda h:identify(h,extras.get(h,())),retry):
            if item:results[item['host']]=item
    for entry in announced:
        if entry['host'] not in results:
            results[entry['host']]=dict(host=entry['host'],port=554,control_port=entry['onvif_port'],
                brand='ONVIF candidate',confirmed=False,evidence=['ONVIF announcement; stream not confirmed'])
    for host,item in results.items():
        item['mac']=macs.get(host,'')
    return sorted(results.values(),key=lambda item:ipaddress.ip_address(item['host']))


if __name__=='__main__':
    print(json.dumps(discover(default_network()),indent=2))
