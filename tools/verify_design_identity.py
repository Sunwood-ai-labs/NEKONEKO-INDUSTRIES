"""Check the readable current name before reporting a new vehicle identity."""

import argparse
import json
from pathlib import Path
import re
import sys


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('identity', type=Path, help='Current identity.json; do not rewrite historical CAD metadata.')
args = parser.parse_args()
try:
    data = json.loads(args.identity.read_text(encoding='utf-8-sig'))
except (OSError, ValueError) as error:
    print(json.dumps({'status': 'FAIL', 'issues': [str(error)]}, ensure_ascii=False))
    sys.exit(1)

issues = []
if not isinstance(data, dict):
    issues.append('identity.json must contain an object.')
    data = {}
for field in ['model', 'codename_ja', 'codename_en', 'display_name']:
    if not isinstance(data.get(field), str) or not data[field].strip():
        issues.append(f'{field}: supply a nonempty string.')

reading = data.get('codename_ja', '')
english = data.get('codename_en', '')
display = data.get('display_name', '')
if isinstance(reading, str) and reading:
    if not re.fullmatch(r'[ァ-ヶー・]+', reading) or not re.search(r'[ァ-ヶ]', reading):
        issues.append('codename_ja: write the name in readable full-width katakana, not Roman letters.')
    if isinstance(display, str) and not display.startswith(reading):
        issues.append('display_name: begin with codename_ja; put the English name and model after it.')
if isinstance(english, str) and english and not re.fullmatch(r'[A-Z][A-Z0-9-]*', english):
    issues.append('codename_en: use the uppercase English code, separately from its Japanese reading.')

if data.get('status') not in {'current_agent_selected_name', 'user_approved_name'}:
    issues.append('status: name is not selected; keep under_review names out of adopted-name reports.')
rationale = data.get('naming_rationale')
if not isinstance(rationale, dict):
    rationale = {}
for field in ['origin', 'design_connection', 'callability', 'alternatives', 'selection_reason']:
    if not isinstance(rationale.get(field), str) or not rationale[field].strip():
        issues.append(f'naming_rationale.{field}: document the naming review before adoption.')

print(json.dumps({'status': 'FAIL' if issues else 'PASS', 'issues': issues,
                  'display_name': display,
                  'scope': 'Checks reading, display, review-field presence and adoption status only. Meaning, relevance and callability require human judgment.'},
                 ensure_ascii=False, indent=2))
sys.exit(1 if issues else 0)
