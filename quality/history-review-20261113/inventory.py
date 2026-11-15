"""Inventory NEKOMO session metadata and message counts without exporting chats."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--sessions', type=Path, default=Path.home() / '.codex/sessions')
parser.add_argument('--workspace', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--before', default='2026-11-13T06:12:06.874Z')
parser.add_argument('--output', type=Path, default=Path(__file__).with_name('source-inventory.json'))
args = parser.parse_args()
if not args.sessions.is_dir():
    parser.error(f'Session directory does not exist: {args.sessions}')
if not args.workspace.is_dir():
    parser.error(f'Workspace directory does not exist: {args.workspace}')
workspace = str(args.workspace.resolve()).replace('\\', '/').lower().rstrip('/')
roots = []
children = Counter()
errors = []
for path in sorted(args.sessions.rglob('rollout-*.jsonl')):
    with path.open(encoding='utf-8') as stream:
        try:
            meta = json.loads(stream.readline()).get('payload', {})
        except (ValueError, UnicodeError):
            errors.append({'file': path.name, 'error': 'invalid session metadata'})
            continue
        cwd = str(meta.get('cwd', '')).replace('\\', '/').lower().rstrip('/')
        if cwd != workspace or meta.get('timestamp', '') >= args.before:
            continue
        source = meta.get('source')
        if isinstance(source, dict):
            parent = source.get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
            children[parent or 'unknown'] += 1
            continue
        counts = Counter()
        first = None
        last = None
        for number, line in enumerate(stream, 2):
            try:
                item = json.loads(line)
            except ValueError:
                errors.append({'file': path.name, 'line': number, 'error': 'invalid JSON'})
                continue
            if item.get('timestamp', '') >= args.before:
                continue
            payload = item.get('payload', {})
            if item.get('type') != 'response_item' or payload.get('type') != 'message':
                continue
            role = payload.get('role')
            if role not in ('user', 'assistant'):
                continue
            counts[role] += 1
            first = first or item.get('timestamp')
            last = item.get('timestamp')
        roots.append({'thread_id': meta['id'], 'session_file': path.name,
                      'started_at_utc': meta.get('timestamp'),
                      'first_message_utc': first, 'last_message_utc': last,
                      'message_counts': dict(counts),
                      'session_sha256_at_inventory': hashlib.sha256(path.read_bytes()).hexdigest()})
result = {'schema_version': 1, 'workspace': 'C:/Prj/NEKOMO',
          'messages_before_utc': args.before,
          'note': 'Counts include injected user context. Discovery and counting do not prove human review. Session hashes cover whole files at execution time; active files can change.',
          'root_session_count': len(roots), 'child_session_count': sum(children.values()),
          'child_sessions_by_parent': dict(sorted(children.items())),
          'root_sessions': roots, 'parse_errors': errors}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'roots': len(roots), 'children': sum(children.values()),
                  'errors': len(errors), 'output': str(args.output)}, ensure_ascii=False))
