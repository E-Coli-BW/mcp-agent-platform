from app.context.request_context import get_request_context, set_agent_contract
from app.registry.config_loader import load_agent_config


def test_customer_qa_pack_declares_isolated_knowledge_collection():
    config = load_agent_config("agents/customer-qa-agent.yaml")
    assert config.id == "customer-qa-agent"
    assert config.knowledge == {"collection": "customer-faq"}


def test_agent_contract_context_is_request_scoped():
    set_agent_contract("customer-qa-agent", "customer-faq")
    context = get_request_context()
    assert context.agent_id == "customer-qa-agent"
    assert context.knowledge_collection == "customer-faq"
