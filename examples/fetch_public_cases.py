"""Fetch exact public original HTMLs for local reproduction, with SHA256 checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', choices=['2903', '2873', '2876', '2726'], required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('public-cases.json').read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for case_id in dict.fromkeys(args.case):
        case = next(row for row in manifest if row['case_id'] == case_id)
        target = args.output_dir / ('issue' + case_id + '.html')
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != case['sha256']:
                raise SystemExit('Existing file differs from public original: ' + str(target))
            results.append({'case': case_id, 'path': str(target), 'sha256': case['sha256'], 'cached': True})
            continue
        with urllib.request.urlopen(case['source_url'], timeout=30) as response:
            data = response.read(4 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != case['sha256']:
            raise SystemExit('Public attachment hash changed; review source before updating manifest: ' + case_id)
        with tempfile.NamedTemporaryFile(dir=args.output_dir, delete=False) as temp:
            temp.write(data)
            temp_path = Path(temp.name)
        try:
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
        results.append({'case': case_id, 'path': str(target), 'sha256': case['sha256'], 'cached': False})
    print(json.dumps({'public_originals': results}, indent=2))

if __name__ == '__main__':
    main()
