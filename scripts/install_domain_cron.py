#!/usr/bin/env python3
"""Install/remove the bounded six-hour domain collector, preserving other jobs."""
import argparse
from pathlib import Path
import shlex
import subprocess
import sys

TAG = '# nekaise-corpus SSD domain collection'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    old=subprocess.run(['crontab','-l'],capture_output=True,text=True)
    if old.returncode and 'no crontab' not in old.stderr.lower():
        raise RuntimeError(old.stderr)
    lines=[line for line in old.stdout.splitlines() if TAG not in line]
    if not args.remove:
        import domain_storage
        domain_storage.require_space(root)
        line=f'47 1,7,13,19 * * * cd {shlex.quote(str(root))} && /usr/bin/timeout --signal=TERM --kill-after=60s 10800 {shlex.quote(sys.executable)} scripts/domain_acquire.py --repositories 6 --websites 6 >/dev/null 2>&1  {TAG}'
        lines.append(line)
    backup=root/'workspace/crontab-before-domain.txt'
    if not backup.exists():backup.write_text(old.stdout)
    subprocess.run(['crontab','-'],input='\n'.join(lines)+'\n',text=True,check=True)
    print('Removed domain schedule' if args.remove else line)


if __name__=='__main__':main()
