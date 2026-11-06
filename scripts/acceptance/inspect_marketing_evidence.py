"""Read-only inspection of the disposable loop's rows and downloaded export."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import zipfile

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    directory = Path(os.environ['MARKETING_ACCEPTANCE_DIR'])
    url = os.environ['MARKETING_ACCEPTANCE_OWNER_DATABASE_URL']
    parsed = make_url(url)
    # The native acceptance database, or the disposable docker compose stack (docker-compose.yml).
    if (parsed.host, parsed.port) not in {('127.0.0.1', 55432), ('localhost', 5432)} or parsed.database != 'marketai_domain':
        raise ValueError('Disposable local acceptance database required')
    loop = json.loads((directory / 'loop-result.json').read_text())
    platform = json.loads((directory / 'platform.json').read_text())
    workspace = platform['workspaceId']
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.scalar(text('SELECT current_user')) == 'marketai_domain_owner'
        assert connection.scalar(text('SELECT id FROM marketing_rounds WHERE workspace_id=:w AND project_id=:p AND id=:r'),
            {'w': workspace, 'p': loop['projectId'], 'r': loop['roundId']}) == loop['roundId']
        tables = ('generation_runs', 'run_items', 'asset_versions', 'marketing_feedback_drafts', 'marketing_qc_reviews',
            'marketing_evaluations', 'marketing_metric_batches', 'marketing_planning_calls', 'marketing_ai_qc_calls')
        counts = {table: connection.scalar(text(f'SELECT count(*) FROM {table} WHERE workspace_id=:w'), {'w': workspace}) for table in tables}
        jobs = [dict(row._mapping) for row in connection.execute(text(
            'SELECT type,state,attempt,count(*) AS count FROM jobs WHERE workspace_id=:w GROUP BY type,state,attempt ORDER BY type,state,attempt'), {'w': workspace})]
        paid = connection.scalar(text("SELECT count(*) FROM provider_call_attempts WHERE workspace_id=:w AND execution_mode='byok'"), {'w': workspace})
        launches = connection.scalar(text('SELECT count(*) FROM launch_operations WHERE workspace_id=:w'), {'w': workspace})
    engine.dispose()
    assert all(counts[key] == 6 for key in ('generation_runs', 'run_items', 'asset_versions'))
    assert all(job['state'] == 'succeeded' and job['attempt'] == 1 for job in jobs)
    assert paid == 0 and launches == 0
    restart = json.loads((directory / 'restart-result.json').read_text())
    assert restart['passed'] and restart['roundId'] == loop['roundId'] and restart['evaluationHash'] == loop['evaluationHash']
    (directory / 'runtime-evidence.json').write_text(json.dumps({'passed': True, 'counts': counts, 'jobs': jobs,
        'paid_provider_attempts': paid, 'meta_launch_operations': launches, 'restart_result': restart}, indent=2))

    package = directory / 'controlled-meta-export.zip'
    assert hashlib.sha256(package.read_bytes()).hexdigest() == loop['exportSha256']
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['workspace_id'] == workspace and manifest['round_id'] == loop['roundId']
        assert len(manifest['entries']) == 5
        assert loop['originalVersion'] not in [e['asset_version_id'] for e in manifest['entries']]
        correction = next(e for e in manifest['entries'] if e['asset_version_id'] == loop['correctionVersion'])
        assert correction['provenance']['kind'] == 'qc_feedback_revision'
        assert correction['provenance']['source_asset_version_id'] == loop['originalVersion']
        assert not list(csv.DictReader(io.StringIO(archive.read('daily-metrics-template.csv').decode())))
        for entry in manifest['entries']:
            assert hashlib.sha256(archive.read(entry['file'])).hexdigest() == entry['content_sha256']
            assert entry['qc']['quality'] == 'approved' and entry['qc']['ai_qc_id']
            assert entry['qc']['source'] == 'ai_assisted_human_review'
    (directory / 'export-validation.json').write_text(json.dumps({'passed': True, 'media_count': 5,
        'zip_sha256': loop['exportSha256'], 'qc_feedback_provenance': True,
        'ai_assisted_fresh_reviews': True, 'empty_return_template': True}, indent=2))
    print('Runtime, no duplicate attempts, exact media and feedback provenance verified')


if __name__ == '__main__':
    main()
