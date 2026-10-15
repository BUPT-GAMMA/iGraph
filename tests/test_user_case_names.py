"""Default example name upgrades preserve identity and custom labels."""
from pathlib import Path
from backend.projects import ProjectDirectory
from backend.project_datasets import ProjectDatasetDirectory
from backend.user_cases import USER_CASES, normalize_user_case_names


def test_science_name_migration_preserves_custom_names_and_snapshot(tmp_path):
    projects=ProjectDirectory(tmp_path/'projects.json')
    datasets=ProjectDatasetDirectory(tmp_path/'datasets.json',tmp_path/'files')
    previous_defaults = {'finance_risk': '金融风控', 'borylation': '化学分析性质分析', 'tianlongbabu': '天龙八部'}
    current_projects = {case['id']: projects.ensure_user_case('existing', case['id'], previous_defaults[case['id']], '')
                        for case in USER_CASES}
    old='分子硼化反应示例'
    default=projects.ensure_user_case('u','borylation',old,'')
    custom=projects.ensure_user_case('v','borylation','我的化学课题','')
    unrelated=projects.create('u',old)
    novel=projects.ensure_user_case('u','tianlongbabu','天龙八部人物关系示例','')
    snapshot=tmp_path/'borylation-story.npz';snapshot.write_bytes(b'unchanged graph snapshot')
    source=datasets.create_reference(project_id=None,user_id='u',name=old,
        original_filename=snapshot.name,storage_path=snapshot,source_id='science-source',
        metadata={'user_case_id':'borylation','source_relative_path':snapshot.name})[0]
    imported=datasets.create_reference(project_id=default.project_id,user_id='u',name='Molecular borylation example',
        original_filename=snapshot.name,storage_path=snapshot,source_id='science-source',
        metadata={'source_relative_path':snapshot.name})[0]
    renamed=datasets.create_reference(project_id=custom.project_id,user_id='v',name='我的化学数据',
        original_filename=snapshot.name,storage_path=snapshot,source_id='science-source',
        metadata={'source_relative_path':snapshot.name})[0]
    unrelated_dataset=datasets.create_reference(project_id=None,user_id='u',name=old,
        original_filename='unrelated.npz',storage_path=snapshot,source_id='unrelated',metadata={})[0]
    normalize_user_case_names(projects,datasets)
    for case in USER_CASES:
        assert projects.get(current_projects[case['id']].project_id, 'existing').name == case['name']
    assert projects.get(default.project_id,'u').name=='化学分子性质分析'
    assert projects.get(novel.project_id,'u').name=='文学人物关系网络'
    assert projects.get(custom.project_id,'v').name=='我的化学课题'
    assert projects.get(unrelated.project_id,'u').name==old
    records=datasets._load()
    assert records[source.dataset_id].name==records[imported.dataset_id].name=='化学分子性质分析'
    assert records[renamed.dataset_id].name=='我的化学数据'
    assert records[unrelated_dataset.dataset_id].name==old
    assert snapshot.read_bytes()==b'unchanged graph snapshot'
    assert records[source.dataset_id].metadata==source.metadata
    assert records[imported.dataset_id].storage_path==imported.storage_path
    before=(projects.path.read_bytes(),datasets.metadata_path.read_bytes())
    normalize_user_case_names(projects,datasets)
    assert before==(projects.path.read_bytes(),datasets.metadata_path.read_bytes())
