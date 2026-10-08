"""Run after generating the finance and novel preset graphs."""
from pathlib import Path
import csv
import gzip
import hashlib
import io
import json
import zipfile

import argparse
parser = argparse.ArgumentParser(description="校验并打包预置图对应的原始构图数据源。")
parser.add_argument("--finance-source", required=True, type=Path)
parser.add_argument("--novel-source", required=True, type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / "graph_engine" / "resources"
finance = args.finance_source
tables = []
files = {}
for index, table in enumerate(('institution_main', 'individual_info', 'institution_shareholder_relationship')):
    name = table + '.csv'
    raw = (finance / name).read_bytes()
    files[name] = raw
    columns = next(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    tables.append({'name': table, 'file_name': name, 'entry': f'tables/{index:03d}-{name}',
        'columns': columns, 'primary_key': ['UUID'] if index == 0 else ['IDV_ID'] if index == 1 else [],
        'foreign_keys': [{'columns': ['UUID'], 'ref_table': 'institution_main', 'ref_columns': ['UUID']}] if index == 2 else []})
stream = io.BytesIO()
with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
    archive.writestr(zipfile.ZipInfo('manifest.json', date_time=(2026, 1, 1, 0, 0, 0)), json.dumps({'version': 'csv_bundle.v1', 'tables': tables}, ensure_ascii=False))
    for table in tables:
        archive.writestr(zipfile.ZipInfo(table['entry'], date_time=(2026, 1, 1, 0, 0, 0)), files[table['file_name']])
raw = stream.getvalue()
sources = [
    ('finance', 'finance.csv', raw, {'csv_bundle': {'version': 'csv_bundle.v1', 'source_files': list(files),
       'tables': [{k: t[k] for k in ('name', 'columns', 'primary_key', 'foreign_keys')} for t in tables], 'total_bytes': len(raw)}}),
    ('tianlongbabu', '天龙八部.txt', args.novel_source.read_bytes(), {}),
]
for case_id, filename, content, metadata in sources:
    directory = root / case_id
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    expected = manifest['sources']
    if case_id == 'finance':
        for name, value in files.items():
            assert hashlib.sha256(value).hexdigest() == expected[name]['sha256']
    else:
        assert hashlib.sha256(content).hexdigest() == expected[filename]['sha256']
    asset = 'source.gz'
    (directory / asset).write_bytes(gzip.compress(content, mtime=0))
    manifest['construction_source'] = {'asset': asset, 'filename': filename,
        'sha256': hashlib.sha256(content).hexdigest(), 'metadata': metadata}
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(case_id, filename, len(content), (directory / asset).stat().st_size)
