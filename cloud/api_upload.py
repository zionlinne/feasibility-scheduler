"""Receive a per-run RSA-encrypted API credential without repository secrets.

The operator uploads only ciphertext on runtime/ple-api-<run id>. The private
key and decrypted model env file exist only on this runner and are not exported.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


def openssl(*args,input=None):
    result=subprocess.run(['openssl',*args],input=input,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    if result.returncode: raise ValueError('Credential encryption operation failed')
    return result.stdout


def private_write(path,data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream: stream.write(data)


def prepare(root,run_id):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    private_write(root/'upload-private.pem',openssl('genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072'))
    public=openssl('pkey','-in',str(root/'upload-private.pem'),'-pubout')
    request={'run_id':str(run_id),'public_key_pem':public.decode(),
             'public_key_sha256':hashlib.sha256(public).hexdigest(),
             'upload_branch':'runtime/ple-api-'+str(run_id)}
    (root/'api-request.json').write_text(json.dumps(request),encoding='utf-8')
    return request


def decrypt(root,payload):
    root=Path(root)
    request=json.loads((root/'api-request.json').read_text())
    if str(payload.get('run_id'))!=request['run_id'] or payload.get('public_key_sha256')!=request['public_key_sha256']:
        raise ValueError('Upload belongs to a different run')
    ciphertext=base64.b64decode(payload['ciphertext'],validate=True)
    if len(ciphertext)!=384: raise ValueError('Invalid ciphertext size')
    raw=openssl('pkeyutl','-decrypt','-inkey',str(root/'upload-private.pem'),
                '-pkeyopt','rsa_padding_mode:oaep','-pkeyopt','rsa_oaep_md:sha256',input=ciphertext)
    key=raw.decode('utf-8')
    if not key or len(raw)>318 or any(c.isspace() or not c.isprintable() for c in key):
        raise ValueError('Invalid model credential')
    private_write(root/'model.env',b'MODEL_API_KEY='+raw+b'\n')
    (root/'upload-private.pem').unlink()


def receive(root,repo,token):
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',repo): raise ValueError('Invalid repository')
    request=json.loads((Path(root)/'api-request.json').read_text())
    ref=urllib.parse.quote(request['upload_branch'],safe='')
    url=f'https://api.github.com/repos/{repo}/contents/api-upload.json?ref={ref}'
    print('Waiting for encrypted API upload for run '+request['run_id'],flush=True)
    while True:
        req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,
            'Accept':'application/vnd.github+json','User-Agent':'fbs-api-upload'})
        try:
            with urllib.request.urlopen(req,timeout=30) as response: obj=json.load(response)
            payload=json.loads(base64.b64decode(obj['content'],validate=False))
            decrypt(root,payload)
            print('API upload received; private decryption key removed.',flush=True)
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in (404,429,500,502,503,504): raise
        except (TimeoutError,urllib.error.URLError):
            pass
        time.sleep(10)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','receive'])
    parser.add_argument('root',type=Path)
    args=parser.parse_args()
    try:
        if args.action=='prepare':
            prepare(args.root,os.environ['GITHUB_RUN_ID'])
            print('One-run public upload request ready.',flush=True)
        else: receive(args.root,os.environ['GITHUB_REPOSITORY'],os.environ['GITHUB_TOKEN'])
    except Exception as exc:
        print(json.dumps({'ok':False,'error_type':type(exc).__name__}),flush=True)
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
