"""Check tracked source paths, credential-shaped values and pinned patch integrity."""
from pathlib import Path
import ast, hashlib, json, re, subprocess, sys
ROOT=Path(__file__).resolve().parents[1]
SKIP={'.git','.verify','runtime','dist-exe','data','__pycache__','.pytest_cache','.ruff_cache'}
FORBIDDEN={'.pem','.key','.p12','.keystore','.pass','.token','.db','.sqlite3','.exe','.apk','.zip','.log'}
def main():
    result=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True)
    names=result.stdout.decode().split('\0') if result.returncode==0 else []
    files=[ROOT/n for n in names if n] if names else [p for p in ROOT.rglob('*') if p.is_file() and not any(x in SKIP for x in p.relative_to(ROOT).parts)]
    failures=[];python_files=0
    for p in files:
        rel=p.relative_to(ROOT)
        if any(x in SKIP for x in rel.parts) or p.suffix in FORBIDDEN or p.name in {'secrets.json','host.json','local.properties'} or p.name.startswith('.env'):
            failures.append((rel.as_posix(),'runtime or credential file'));continue
        if p.suffix=='.png':continue
        try:s=p.read_text('utf8')
        except UnicodeError:
            failures.append((rel.as_posix(),'unexpected binary'));continue
        if p.suffix=='.py':
            try:ast.parse(s);python_files+=1
            except SyntaxError:failures.append((rel.as_posix(),'invalid Python syntax'))
        if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',s):
            failures.append((rel.as_posix(),'GitHub token shape'))
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',s):
            failures.append((rel.as_posix(),'private key'))
    metadata=json.loads((ROOT/'UPSTREAM.json').read_text())
    if hashlib.sha256((ROOT/'patches/astrbot.patch').read_bytes()).hexdigest()!=metadata['astrbot']['patch_sha256']:
        failures.append(('patches/astrbot.patch','digest mismatch'))
    for name in metadata['plugins']:
        if not (ROOT/'plugin-sources'/name/'main.py').is_file():failures.append((name,'missing plugin'))
    for name in ['LICENSE','NOTICE','THIRD_PARTY_NOTICES.md','MODIFICATIONS.md']:
        if not (ROOT/name).is_file():failures.append((name,'missing legal notice'))
    print(json.dumps({'files_checked':len(files),'python_files':python_files,'failures':failures},ensure_ascii=False,indent=2))
    return bool(failures)
if __name__=='__main__':sys.exit(main())
