"""Run the spec's twelve case mappings and retain bounded acceptance evidence.

Core regression alone is not cross-service sign-off. A controlled full-loop
result, restart result and PostgreSQL JUnit report are additionally required.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--postgres-junit', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / 'scripts/acceptance/marketing_cases.json').read_text())
    selected = sorted({test for case in cases for test in case['tests']})
    report = args.output / 'cases.junit.xml'
    with (args.output / 'cases.log').open('w') as log:
        command = subprocess.run([sys.executable, '-m', 'pytest', '-q', *selected,
            '--junitxml', str(report.resolve())], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    results = {}
    if report.exists():
        for item in ET.parse(report).iter('testcase'):
            status = 'failed' if item.find('failure') is not None or item.find('error') is not None else \
                'skipped' if item.find('skipped') is not None else 'passed'
            results[(item.get('classname'), item.get('name'))] = status
    for case in cases:
        checks = []
        for selector in case['tests']:
            file, cls, name = selector.split('::')
            key = (file.removesuffix('.py').replace('/', '.') + '.' + cls, name)
            checks.append({'test': selector, 'status': results.get(key, 'not_run')})
        case['checks'] = checks
        case['status'] = 'passed_automated' if all(c['status'] == 'passed' for c in checks) else 'incomplete'
    postgres = {'status': 'not_supplied'}
    if args.postgres_junit and args.postgres_junit.exists():
        rows = list(ET.parse(args.postgres_junit).iter('testcase'))
        failed = [r for r in rows if any(r.find(tag) is not None for tag in ('error', 'failure', 'skipped'))]
        postgres = {'status': 'passed' if rows and not failed else 'incomplete', 'checks': len(rows),
            'sha256': hashlib.sha256(args.postgres_junit.read_bytes()).hexdigest()}
    evidence = {}
    for name in ('loop-result.json', 'restart-result.json', 'runtime-evidence.json', 'export-validation.json'):
        path = args.output / name
        evidence[name] = json.loads(path.read_text()) if path.exists() else {'passed': False}
    loop, restart = evidence['loop-result.json'], evidence['restart-result.json']
    consistent = bool(loop.get('roundId') and loop.get('roundId') == restart.get('roundId')
        and loop.get('evaluationHash') == restart.get('evaluationHash'))
    complete = all(case['status'] == 'passed_automated' for case in cases) and postgres['status'] == 'passed' \
        and all(r.get('passed') for r in evidence.values()) and consistent
    source_files = sorted([*ROOT.glob('app/marketing/*.py'), *ROOT.glob('migrations/versions/[vw]1*.py'),
        *ROOT.glob('web/components/workspace/Marketing*.tsx'),
        *ROOT.glob('app/generation/*.py'), ROOT / 'web/lib/domain-client/gateway.ts',
        ROOT / 'web/components/layouts/WorkspaceLayout.tsx', ROOT / 'deploy/postgres/domain-tenant-roles.sql',
        ROOT / 'contracts/openapi/marketai-domain-v1.json'])
    payload = {'schema_version': 1, 'status': 'passed_controlled_acceptance' if complete else 'incomplete',
        'scope': 'Local PostgreSQL + real credentials login/BFF/API/Worker + private versioned S3-compatible storage; external AI/Meta controlled',
        'does_not_prove': ['production deployment', 'real Meta delivery or economic success', 'real result-explanation provider quality'],
        'cases': cases, 'postgres': postgres, 'cross_service': evidence,
        'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}}
    (args.output / 'acceptance.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'status': payload['status'], 'cases': {case['id']: case['status'] for case in cases}}, ensure_ascii=False))
    return command.returncode or (0 if complete else 1)


if __name__ == '__main__':
    raise SystemExit(main())
