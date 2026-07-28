import os
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agents.state import WorkflowState
from app.llm import get_llm, invoke_with_retry
from app.tools.document_tools import store_and_classify_document_tool

DOCUMENT_SYSTEM_PROMPT = (
    "You are the Document Agent for AgentCare, an administrative healthcare "
    "workflow assistant. A patient has attached one file to their request; "
    "its filename is given below (the server already knows the real file "
    "location — you never see or need the full path). Call "
    "store_and_classify_document with your best-fit document_type, chosen "
    "from this fixed list: ecg, lab_report, prescription_old, insurance, "
    "id_proof, other. Base your choice on the filename and the patient's "
    "own request text, used only as a note — never open, read, or interpret "
    "the file's actual contents, and never diagnose or interpret what a "
    "document means medically; you are only filing paperwork, not "
    "reviewing it. Once the tool returns a result, reply with a short "
    "confirmation sentence and do not call any more tools."
)

document_tools = [store_and_classify_document_tool]
document_tools_node = ToolNode(document_tools)


class DocumentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    patient_id: str
    file_path: str
    document_result: dict | None


def document_llm_node(state: DocumentState, config):
    model = get_llm().bind_tools(document_tools)
    messages = [SystemMessage(DOCUMENT_SYSTEM_PROMPT), *state["messages"]]
    ai_message = invoke_with_retry(model, messages)
    return {"messages": [ai_message]}


def document_capture_node(state: DocumentState, config):
    last = state["messages"][-1]
    if isinstance(last, ToolMessage) and last.name == "store_and_classify_document_tool":
        return {"document_result": last.artifact}
    return {}


def route_after_document_llm(state: DocumentState) -> Literal["document_tools", "__end__"]:
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "document_tools"
    return "__end__"


def build_document_subgraph():
    graph = StateGraph(DocumentState)
    graph.add_node("document_llm", document_llm_node)
    graph.add_node("document_tools", document_tools_node)
    graph.add_node("document_capture", document_capture_node)
    graph.set_entry_point("document_llm")
    graph.add_conditional_edges(
        "document_llm",
        route_after_document_llm,
        {"document_tools": "document_tools", "__end__": END},
    )
    graph.add_edge("document_tools", "document_capture")
    graph.add_edge("document_capture", "document_llm")
    return graph.compile()


_document_subgraph = build_document_subgraph()


def document_agent_node(state: WorkflowState, config) -> dict:
    """Parent-graph node (registered as "document_agent" in app/graph.py).
    If no file was attached to this request, returns {} immediately - a
    true no-op, no LLM call, for the overwhelmingly common case. Otherwise
    invokes the private Document subgraph once per attached file (today,
    always exactly one) and collects each successful result's real id."""
    uploaded_files = state.get("uploaded_files") or []
    if not uploaded_files:
        return {}

    document_ids = []
    missing_document_types: list[str] = []
    for file_path in uploaded_files:
        result = _document_subgraph.invoke(
            {
                "messages": [
                    HumanMessage(
                        f"filename: {os.path.basename(file_path)}\nrequest: {state['request_text']}"
                    )
                ],
                "patient_id": state["patient_id"],
                "file_path": file_path,
                "document_result": None,
            },
            config=config,
        )
        document_result = result.get("document_result") or {}
        document_id = document_result.get("id")
        if document_id:
            document_ids.append(document_id)
        missing_document_types = document_result.get("missing_document_types") or []

    return {"document_ids": document_ids, "missing_document_types": missing_document_types}
