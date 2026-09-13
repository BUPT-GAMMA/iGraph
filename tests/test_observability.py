import json
import logging
from backend.observability import log_event, redact

def test_redact_secrets_recursively():
    assert redact({'api_key':'secret','nested':{'authorization':'Bearer x'},'value':'ok'}) == {'api_key':'<REDACTED>','nested':{'authorization':'<REDACTED>'},'value':'ok'}

def test_log_event_has_correlation_and_iso_timestamp(caplog):
    logger=logging.getLogger('observability-test')
    with caplog.at_level(logging.INFO, logger='observability-test'):
        log_event(logger, 'agent.tool.completed', session_id='s', run_id='r', tool_name='get_graph_info')
    row=json.loads(caplog.records[-1].message)
    assert row['event']=='agent.tool.completed'
    assert row['session_id']=='s' and row['run_id']=='r'
    assert row['ts'].endswith('+00:00')
