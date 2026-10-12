import json
from agno.models.message import Message
from agent_runtime.finance_review import finance_evidence,review_request


def messages(success=True):
    return [Message(role="system",content="Answer in English."),
            Message(role="user",content="Check target, no control conclusion."),
            Message(role="tool",tool_name="finance_inspect_ownership",content=json.dumps({
                "success":success,"operation":"snapshot","result":{"direct_ownership_summaries":[{"direct_shareholder_count":5}]},
                "quality":{"coverage_gap_institutions":4987}}))]


def test_review_preserves_actual_tool_data_and_language():
    request,count=review_request(messages(),"No shareholder records.")
    assert count==1 and request[0].content=="Answer in English."
    data=json.loads(request[-1].content)
    assert data["ledger"][0]["evidence"]["quality"]["coverage_gap_institutions"]==4987
    assert data["draft"]=="No shareholder records."


def test_review_does_not_use_prior_question_or_failed_data():
    assert finance_evidence(messages()+[Message(role="user",content="Other graph")])==[]
    assert review_request(messages(False),"Draft") is None


def test_strict_formats_and_empty_drafts_skip_review():
    assert review_request(messages(),"Draft",{"type":"json_object"}) is None
    assert review_request(messages(),"") is None
