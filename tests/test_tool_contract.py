import inspect

import docstring_parser
import pytest
from yunhu_plugin.main import YunhuPlugin

TOOLS = [
    method for method in vars(YunhuPlugin).values() if hasattr(method, "tool_name")
]


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.tool_name)
def test_registered_tool_documents_every_model_argument(tool):
    # AstrBot 按 docstring 而不是 Python 类型注解生成 schema。
    parameters = dict(inspect.signature(tool).parameters)
    del parameters["self"]
    del parameters["event"]
    doc = docstring_parser.parse(tool.__doc__)
    assert doc.description
    # 框架按入口模块路径绑定工具，业务迁移到子包时仍需在入口声明工具。
    assert tool.__module__ == "yunhu_plugin.main"
    assert {param.arg_name for param in doc.params} == set(parameters)
    for param in doc.params:
        assert param.type_name in {"string", "number", "object", "array[string]"}
        assert param.description
        assert tool.tool_name.startswith("yunhu_")


def test_all_expected_agent_tools_are_exposed():
    assert {tool.tool_name for tool in TOOLS} == {
        "yunhu_context",
        "yunhu_read_attachment",
        "yunhu_list_files",
        "yunhu_manage_file",
        "yunhu_mute_member",
        "yunhu_remove_member",
        "yunhu_message_types",
        "yunhu_list_tags",
        "yunhu_create_tag",
        "yunhu_edit_tag",
        "yunhu_delete_tag",
        "yunhu_user_tag",
        "yunhu_message_history",
        "yunhu_recall_message",
        "yunhu_edit_message",
        "yunhu_set_board",
        "yunhu_dismiss_board",
        "yunhu_send_message",
        "yunhu_send_buttons",
        "yunhu_global_board",
        "yunhu_batch_send",
    }
