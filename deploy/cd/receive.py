#!/usr/bin/env python3
"""Forced SSH command: accept a Git archive and deploy one validated commit."""
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def receive():
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '') or ' '.join(sys.argv[1:])
    if command == 'check':
        print('iGraph deployment receiver ready')
        return
    match = re.fullmatch(r'deploy ([0-9a-f]{40})', command)
    if not match:
        raise SystemExit('Only check or deploy <commit SHA> is allowed')
    sha = match.group(1)
    root = Path.home() / 'apps' / 'igraph-deploy'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Lock upload + deployment together, including reconnects and manual retries.
    import fcntl
    with (root / 'deployment.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        staging = root / ('incoming-' + sha)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        archive = root / ('incoming-' + sha + '.tar.gz')
        try:
            total = 0
            with archive.open('wb') as out:
                while True:
                    chunk = sys.stdin.buffer.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 300 * 1024 * 1024:
                        raise ValueError('Source archive exceeds 300 MiB')
                    out.write(chunk)
            with tarfile.open(archive, 'r:gz') as bundle:
                members = bundle.getmembers()
                if sum(m.size for m in members) > 1024 * 1024 * 1024:
                    raise ValueError('Expanded archive exceeds 1 GiB')
                for member in members:
                    path = Path(member.name)
                    if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                        raise ValueError('Unsafe archive entry')
                    if path.parts and path.parts[0] in {'.env', '.git', 'host-config'}:
                        raise ValueError('Archive must not contain host configuration')
                bundle.extractall(staging, members=members)
            # Git executable bits are retained, but restrictive uploader umasks
            # must not make static assets unreadable by the container's nginx.
            for path in staging.rglob('*'):
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
            script = staging / 'deploy/cd/deploy.sh'
            if not script.is_file():
                raise ValueError('Missing deployment script')
            subprocess.run(['bash', str(script), sha, str(staging)], check=True)
        finally:
            archive.unlink(missing_ok=True)


if __name__ == '__main__':
    os.umask(0o077)
    receive()
