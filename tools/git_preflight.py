#!/usr/bin/env python3
"""Audit tracked/staged files or an untracked clean export before GitHub/GitLab push.

Exits 1 when blocked extensions, risky files, or high-confidence secrets appear.
"""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys

BAD_EXT={'.xlsx','.xls','.xlsm','.xlsb','.ods','.csv','.tsv','.parquet','.arrow','.feather',
         '.pdf','.doc','.docx','.ppt','.pptx','.mp4','.mov','.avi','.las','.laz','.shp','.shx',
         '.dbf','.geojson','.gpkg','.sqlite','.sqlite3','.db','.bak','.backup','.pt','.pth','.ckpt',
         '.safetensors','.onnx','.npy','.npz','.pkl','.pickle','.zip','.7z','.rar','.tar','.gz',
         '.jpg','.jpeg','.webp','.tiff','.tif','.bmp','.gif'}
BAD_COMPONENTS={'data','datasets','models','weights','thumb_cache','outputs','exports','logs','results',
                '.venv','venv','__pycache__','node_modules','backups','secrets','demo_out'}
ALLOWED_IMAGES={'dashboard/assets/dsn_group_logo.png'}
KEY_PATTERNS=[
 ('private key',re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')),
 ('github credential',re.compile(rb'\bgh[pousr]_[A-Za-z0-9_]{20,}\b')),
 ('gitlab credential',re.compile(rb'\bglpat-[A-Za-z0-9_-]{15,}\b')),
 ('aws access key',re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
 ('openai credential',re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b')),
 ('literal password',re.compile(rb'(?im)^[ \t]*(?:PG_PASSWORD|MSSQL_PASSWORD|DB60_ZPAY_PASSWORD|MEDIA_GATEWAY_TOKEN)[ \t]*=[ \t]*(?!$|GANTI_|YOUR_|CHANGE_ME|<)[^\r\n#]{8,}')),
]
INTERNAL_IP=re.compile(rb'(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)')
MAX_BYTES=5*1024*1024

def git_paths(root:Path, mode:str):
    cmd=['git','-C',str(root),'ls-files','-z']
    if mode=='staged':cmd.append('--cached')
    elif mode=='tracked':cmd.append('--cached')
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True)
        return [root/Path(x.decode('utf-8','surrogateescape')) for x in p.stdout.split(b'\0') if x]
    except (OSError,subprocess.CalledProcessError):
        return None

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--path',default='.')
    ap.add_argument('--all-files',action='store_true',help='Scan output folder before git init')
    ap.add_argument('--allow-internal-ips',action='store_true',help='Suppress internal IP warnings only, not secrets')
    args=ap.parse_args()
    root=Path(args.path).resolve()
    if not root.is_dir():ap.error('Folder not found')
    paths=None if args.all_files else git_paths(root,'staged')
    if paths is None:
        paths=[p for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts]
    problems=[]; warnings=[]; extensions=Counter(); total_bytes=0
    for p in sorted(paths):
        if not p.exists() or not p.is_file():continue
        rel=p.relative_to(root).as_posix(); lower=rel.lower()
        parts=set(lower.split('/'))
        ext=p.suffix.lower(); extensions[ext]+=1
        size=p.stat().st_size;total_bytes+=size
        if size>MAX_BYTES:problems.append((rel,f'file larger than 5 MiB ({size:,} bytes)'))
        if (ext in BAD_EXT or ext=='.png') and lower not in ALLOWED_IMAGES:
            problems.append((rel,f'data/binary file type {ext}'))
        if parts & BAD_COMPONENTS or any(c.startswith(('demo_','render_','transport_demo','transport_render')) for c in parts):
            problems.append((rel,'runtime/data/demo folder'))
        if p.name.startswith('.env') and lower not in {'.env.example'}:
            problems.append((rel,'local environment secrets'))
        if any(s in p.name.lower() for s in ['.pem','.key','.p12','.pfx','id_rsa','credentials.json']):
            problems.append((rel,'credentials/key file'))
        if size>MAX_BYTES or ext in BAD_EXT:continue
        try:content=p.read_bytes()
        except OSError:continue
        if b'\x00' in content[:10000]:continue
        for tag,pat in KEY_PATTERNS:
            if pat.search(content):problems.append((rel,tag))
        if not args.allow_internal_ips and INTERNAL_IP.search(content):
            warnings.append((rel,'private network address, review before public release'))
    print(f"Checked {len(paths)} files; {total_bytes/1024/1024:.2f} MiB")
    print('Top file types:',dict(extensions.most_common(12)))
    for rel,desc in problems[:60]:print(f'BLOCK {rel}: {desc}')
    if len(problems)>60:print(f'... {len(problems)-60} additional blocks')
    for rel,desc in warnings[:30]:print(f'REVIEW {rel}: {desc}')
    if len(warnings)>30:print(f'... {len(warnings)-30} additional warnings')
    if problems:
        print(f'FAIL: {len(problems)} unsafe file findings. DO NOT PUSH.')
        return 1
    print('PASS (file type / common secrets). Manual review still required for confidential code and data.')
    return 0

if __name__=='__main__':raise SystemExit(main())
