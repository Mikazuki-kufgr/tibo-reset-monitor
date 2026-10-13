"""GitHub Actions state worktree helper. Only explicitly selected DB files enter Git."""
from __future__ import annotations
import argparse, pathlib, subprocess, sys

ROOT = pathlib.Path.cwd().resolve()
RUNTIME = ROOT / 'runtime'
BRANCH = 'monitor-state'

def git(*args, where=ROOT, ok=(0,)):
    result = subprocess.run(['git', '-C', str(where), *args], capture_output=True, text=True)
    if result.returncode not in ok:
        raise RuntimeError('git_state_operation_failed')
    return result

def prepare(bootstrap):
    if RUNTIME.exists():
        raise RuntimeError('runtime_directory_already_exists')
    remote = git('ls-remote', '--exit-code', '--heads', 'origin', BRANCH, ok=(0,2))
    if remote.returncode == 2:
        if not bootstrap:
            raise RuntimeError('state_branch_missing_manual_bootstrap_required')
        git('worktree','add','--detach',str(RUNTIME),'HEAD')
        git('checkout','--orphan',BRANCH,where=RUNTIME)
        # Remove only index entries; keep checkout files untouched. Only the DB
        # and marker are subsequently added to this orphan state branch.
        git('rm','-r','--cached','--','.',where=RUNTIME)
    else:
        git('fetch','origin',BRANCH)
        git('worktree','add','--detach',str(RUNTIME),'FETCH_HEAD')
        if not (RUNTIME/'state.sqlite3').is_file() or not (RUNTIME/'.initialized').is_file():
            raise RuntimeError('state_branch_incomplete_restore_required')

def persist():
    if not RUNTIME.resolve().is_relative_to(ROOT) or not (RUNTIME/'.git').is_file():
        raise RuntimeError('invalid_runtime_worktree')
    if not (RUNTIME/'state.sqlite3').is_file() or not (RUNTIME/'.initialized').is_file():
        raise RuntimeError('state_missing')
    git('add','-f','--','state.sqlite3','.initialized',where=RUNTIME)
    staged = git('diff','--cached','--name-only',where=RUNTIME).stdout.splitlines()
    if set(staged) - {'state.sqlite3','.initialized'}:
        raise RuntimeError('unexpected_staged_file')
    if not staged:
        return
    git('config','user.name','tibo-reset-monitor[bot]',where=RUNTIME)
    git('config','user.email','tibo-reset-monitor@users.noreply.github.com',where=RUNTIME)
    git('commit','-m','Persist reset monitor checkpoint',where=RUNTIME)
    # A concurrent writer causes a safe non-fast-forward failure, not a reset
    # or binary database merge. Restore the recovery artifact before resuming.
    git('push','origin','HEAD:refs/heads/'+BRANCH,where=RUNTIME)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','persist'])
    parser.add_argument('--bootstrap',action='store_true')
    args=parser.parse_args()
    try:
        prepare(args.bootstrap) if args.command=='prepare' else persist()
    except Exception as e:
        print('cloud_state_failed:',str(e) if isinstance(e,RuntimeError) else type(e).__name__,file=sys.stderr)
        sys.exit(1)
