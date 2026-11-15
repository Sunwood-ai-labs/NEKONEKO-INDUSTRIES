import ast
import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

root = Path(__file__).resolve().parents[1]


def check_dr_gates(status_path):
    """Check evidence and stage gates; an unknown required check never passes."""
    status = json.loads(status_path.read_text(encoding='utf-8-sig'))
    errors = []
    stages = {}
    prior_passed = True
    if status.get('dr_definition_version') != 2 or status.get('gate_policy_version') != 1:
        errors.append('Expected DR definition 2 and gate policy 1')
    for name in ('DR0', 'DR1', 'DR2', 'DR3', 'DR4'):
        stage = status.get('stages', {}).get(name, {})
        checks = stage.get('required_checks', [])
        reasons = []
        if not checks:
            reasons.append('Required checks are missing')
        for check in checks:
            label = check.get('id', '?')
            if check.get('result') != 'PASS':
                reasons.append(f'{label}: {check.get("result", "UNKNOWN")}')
                continue
            if not check.get('criterion') or not check.get('evidence'):
                reasons.append(f'{label}: criterion or evidence missing')
                continue
            for evidence in check['evidence']:
                path = (root / evidence.get('path', '')).resolve()
                if (not path.is_relative_to(root) or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != evidence.get('sha256')):
                    reasons.append(f'{label}: evidence location or SHA-256 mismatch')
        if not prior_passed:
            reasons.append('Preceding stage gate has not passed')
        passed = not reasons
        if stage.get('gate_passed') is not passed:
            errors.append(f'{name}: reported gate differs from evidence')
        stages[name] = {'gate_passed': passed, 'reasons': reasons}
        prior_passed = passed
    overall = all(stage['gate_passed'] for stage in stages.values())
    if status.get('overall_gate_passed') is not overall:
        errors.append('Reported overall gate differs from stage gates')
    if status.get('child_use_approved') is not False:
        errors.append('This data-only checker cannot approve child use')
    return {'record_consistent': not errors, 'overall_gate_passed': overall,
            'stages': stages, 'errors': errors,
            'scope': 'Evidence consistency and stage gates; not toy safety certification'}


if '--dr-gate-status' in sys.argv:
    parser = argparse.ArgumentParser(description='Check a DR v2 stage-gate record.')
    parser.add_argument('--dr-gate-status', required=True, type=Path)
    args = parser.parse_args()
    gate_result = check_dr_gates(args.dr_gate_status)
    print(json.dumps(gate_result, ensure_ascii=False))
    sys.exit(1 if not gate_result['record_consistent'] else
             0 if gate_result['overall_gate_passed'] else 2)

manifest = json.loads((root / 'public-manifest.json').read_text(encoding='utf-8-sig'))
published_paths = {entry['path'] for entry in manifest['files']} | {'public-manifest.json'}
failures = []
link_count = 0
archive_count = 0
for entry in manifest['files']:
    path = (root / entry['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        failures.append({'path': entry['path'], 'check': 'file location'})
        continue
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != entry['sha256']:
        failures.append({'path': entry['path'], 'check': 'SHA-256'})
    if len(content) >= 100 * 1024 * 1024:
        failures.append({'path': entry['path'], 'check': 'file size'})
    if path.suffix.lower() in ('.fcstd', '.3mf'):
        with zipfile.ZipFile(path) as archive:
            names = {name.lower() for name in archive.namelist()}
            required = 'document.xml' if path.suffix.lower() == '.fcstd' else '3d/3dmodel.model'
            if required not in names:
                failures.append({'path': entry['path'], 'check': 'CAD or 3MF structure'})
            archive_count += 1
    if path.suffix.lower() == '.json':
        json.loads(content.decode('utf-8-sig'))
    if path.suffix.lower() == '.py':
        ast.parse(content.decode('utf-8-sig'), filename=entry['path'])
    if path.suffix.lower() != '.md':
        continue
    text = re.sub(r'```.*?```', '', content.decode('utf-8-sig'), flags=re.S)
    for target in re.findall(r'!?\[[^\]]*\]\(([^)\n]+)\)', text):
        target = target.strip().strip('<>')
        parsed = urlsplit(target)
        if parsed.scheme in ('http', 'https', 'mailto') or target.startswith('#'):
            continue
        linked = (path.parent / unquote(parsed.path)).resolve()
        relative = linked.relative_to(root).as_posix() if linked.is_relative_to(root) else ''
        in_snapshot = relative in published_paths or (linked.is_dir() and any(name.startswith(relative + '/') for name in published_paths))
        if parsed.scheme or not linked.is_relative_to(root) or not linked.exists() or not in_snapshot:
            failures.append({'path': entry['path'], 'check': 'local link', 'target': target})
        link_count += 1

statuses = json.loads((root / 'docs/data/bus-review-status.json').read_text(encoding='utf-8'))
for required in ('README.md', 'docs/バスの版一覧.md', 'docs/人間のレビュー記録.md',
                 'docs/NEKONEKO-INDUSTRIES-concept-and-quality.md', 'docs/data/bus-review-status.json',
                 'docs/data/bus-agent-reviews.json', 'docs/data/bus-versions.json'):
    if required not in published_paths:
        failures.append({'path': required, 'check': 'required published file'})
expected_variants = {'v01・大型一体', 'v02・大型3部品', 'v03・60mm組立', 'v04・60mm一体',
                     'v05・底面開口候補', 'v06・40mmシンプル', 'v06・60mm空洞', 'v06・赤タイヤ',
                     'v07・40mm床なし', 'v08・写真型', 'v09・傾斜ボンネット'}
if len(statuses['rows']) != 11 or {row['label'] for row in statuses['rows']} != expected_variants:
    failures.append({'check': 'eleven review variants'})
printed = [row['label'] for row in statuses['rows'] if row['print_completed'] == 'recorded']
if set(printed) != {'v06・赤タイヤ', 'v09・傾斜ボンネット'}:
    failures.append({'check': 'two printed variants'})
agents = json.loads((root / 'docs/data/bus-agent-reviews.json').read_text(encoding='utf-8'))
if agents.get('dr_definition_version') != 1:
    failures.append({'check': 'historical DR definition version'})
if set(agents['rows']) != expected_variants:
    failures.append({'check': 'eleven agent review variants'})
if any(entry['stages']['DR3'] != 'なし' for entry in agents['rows'].values()):
    failures.append({'check': 'legacy DR v1 physical-test evidence state'})
summary = {'ok': not failures, 'files_checked': len(manifest['files']), 'local_links_checked': link_count,
           'cad_archives_checked': archive_count, 'printed_count': len(printed), 'failures': failures}
print(json.dumps(summary, ensure_ascii=True))
sys.exit(0 if summary['ok'] else 1)
