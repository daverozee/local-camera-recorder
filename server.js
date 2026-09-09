'use strict';
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const readline = require('node:readline');
const {spawn} = require('node:child_process');

const root = __dirname;
const port = Number(process.env.LOCALCAM_PORT || 8765);
const python = process.env.LOCALCAM_PYTHON || path.join(root,'.venv','Scripts','python.exe');
const worker = spawn(python,['-u',path.join(root,'recorder.py')],{cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe']});
let alive = true, nextId = 1;
const pending = new Map();
const token = crypto.randomBytes(32).toString('hex');
const host = `127.0.0.1:${port}`;
function failPending() {
  alive=false;
  for (const item of pending.values()) {clearTimeout(item.timer);item.reject(new Error('Recorder stopped. Restart LocalCam.'));}
  pending.clear();
}
worker.on('error',failPending);
worker.on('exit',failPending);
worker.stderr.on('data',()=>{ /* Never forward camera URLs or raw FFmpeg logs to the UI. */ });
readline.createInterface({input:worker.stdout}).on('line',line=>{
  try {
    const msg=JSON.parse(line), item=pending.get(msg.id);
    if (!item) return;
    clearTimeout(item.timer);pending.delete(msg.id);
    msg.error ? item.reject(new Error(msg.error)) : item.resolve(msg.result);
  } catch {}
});
function rpc(method,data={}) {
  return new Promise((resolve,reject)=>{
    if (!alive) return reject(new Error('Recorder unavailable. Run setup.ps1, then restart.'));
    const id=nextId++;
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error('Recorder request timed out.'));},90000);
    pending.set(id,{resolve,reject,timer});
    worker.stdin.write(JSON.stringify({id,method,data})+'\n',err=>{
      if(err){clearTimeout(timer);pending.delete(id);reject(new Error('Recorder unavailable.'));}
    });
  });
}
function headers(res) {
  res.setHeader('X-Content-Type-Options','nosniff');
  res.setHeader('X-Frame-Options','DENY');
  res.setHeader('Referrer-Policy','no-referrer');
  res.setHeader('Cache-Control','no-store');
  res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
}
function json(res,status,value) {
  res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(value));
}
async function body(req) {
  if(!String(req.headers['content-type']).startsWith('application/json')) throw new Error('Expected JSON.');
  let data='';
  for await (const chunk of req) {data+=chunk;if(data.length>16384) throw new Error('Request too large.');}
  return JSON.parse(data||'{}');
}
function parseRange(value,size) {
  const match=/^bytes=(\d*)-(\d*)$/.exec(value||'');
  if(!match || (!match[1]&&!match[2])) return null;
  let start,end;
  if(!match[1]) {const suffix=Number(match[2]);if(suffix<=0)return null;start=Math.max(0,size-suffix);end=size-1;}
  else {start=Number(match[1]);end=match[2]?Math.min(Number(match[2]),size-1):size-1;}
  return Number.isSafeInteger(start)&&Number.isSafeInteger(end)&&start>=0&&start<size&&end>=start ? {start,end} : null;
}
const server=http.createServer(async(req,res)=>{
  headers(res);
  if(req.headers.host!==host || (req.headers.origin && req.headers.origin!==`http://${host}`) || req.headers['sec-fetch-site']==='cross-site') {
    return json(res,403,{error:'Local access only. Open the 127.0.0.1 address.'});
  }
  try {
    const url=new URL(req.url,`http://${host}`);
    if(req.method==='GET'&&url.pathname==='/api/state') return json(res,200,{...await rpc('status'),token});
    if(req.method==='GET'&&url.pathname==='/api/clips') return json(res,200,await rpc('clips',Object.fromEntries(url.searchParams)));
    if(req.method==='POST'&&url.pathname.startsWith('/api/')) {
      if(req.headers['x-localcam-token']!==token) return json(res,403,{error:'Refresh this page before making changes.'});
      if(url.pathname==='/api/shutdown') {json(res,200,{ok:true});setImmediate(shutdown);return;}
      const method={'/api/camera':'save_camera','/api/settings':'settings','/api/start':'start','/api/stop':'stop',
                    '/api/discover':'discover','/api/ptz-probe':'ptz_probe','/api/sweep':'sweep'}[url.pathname];
      if(!method) return json(res,404,{error:'Not found.'});
      return json(res,200,await rpc(method,await body(req)));
    }
    if((req.method==='GET'||req.method==='HEAD')&&url.pathname.startsWith('/media/')) {
      const id=decodeURIComponent(url.pathname.slice(7));
      if(!/^[a-f0-9]{32}-clip_\d{6}$/.test(id)) return json(res,404,{error:'Recording not found.'});
      const clip=await rpc('clip',{id});
      let start=0,end=clip.size-1,status=200;
      if(req.headers.range) {
        const range=parseRange(req.headers.range,clip.size);
        if(!range){res.writeHead(416,{'Content-Range':`bytes */${clip.size}`});return res.end();}
        ({start,end}=range);status=206;res.setHeader('Content-Range',`bytes ${start}-${end}/${clip.size}`);
      }
      res.setHeader('Content-Type','video/mp4');res.setHeader('Accept-Ranges','bytes');
      res.setHeader('Content-Length',end-start+1);
      if(url.searchParams.has('download'))res.setHeader('Content-Disposition',`attachment; filename="${id}.mp4"`);
      const stream=fs.createReadStream(clip.path,{start,end});
      stream.on('error',()=>{if(!res.headersSent)json(res,404,{error:'Recording unavailable.'});else res.destroy();});
      if(req.method==='HEAD'){stream.destroy();res.writeHead(status);return res.end();}
      stream.on('open',()=>{res.writeHead(status);stream.pipe(res);});
      res.on('close',()=>stream.destroy());
      return;
    }
    const assets={'/':['index.html','text/html'],'/app.js':['app.js','text/javascript'],'/style.css':['style.css','text/css']};
    if(req.method==='GET'&&assets[url.pathname]) {
      const [file,type]=assets[url.pathname];res.writeHead(200,{'Content-Type':type+'; charset=utf-8'});
      return res.end(fs.readFileSync(path.join(root,'public',file)));
    }
    json(res,404,{error:'Not found.'});
  } catch(e) {if(!res.headersSent)json(res,400,{error:e.message});else res.destroy();}
});
server.requestTimeout=100000;
server.listen(port,'127.0.0.1',()=>console.log(`LocalCam ready: http://${host}`));
server.on('error',()=>{worker.stdin.end();process.exitCode=1;});
let closing=false;
function shutdown(){if(closing)return;closing=true;server.close();worker.stdin.end();setTimeout(()=>{worker.kill();process.exit();},70000).unref();}
process.on('SIGINT',shutdown);process.on('SIGTERM',shutdown);
