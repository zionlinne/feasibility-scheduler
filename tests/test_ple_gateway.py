"""Failure diagnosis must distinguish causes without leaking CLI text."""
import importlib.util
from pathlib import Path

PATH=Path(__file__).resolve().parents[1]/'cloud/subscription_gateway.py'
spec=importlib.util.spec_from_file_location('cloud_gateway_fixture',PATH)
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)


def test_subscription_limit_and_configuration_failures_are_distinct():
    assert gateway.failure_category('{"type":"error","message":"You have hit your usage limit"}','')=='usage_limit'
    assert gateway.failure_category('',"error: unexpected argument '--unknown'")=='cli_arguments'
    assert gateway.failure_category('',"Error: Unknown feature: nonexistent")=='cli_configuration'
    assert gateway.failure_category('{"type":"turn.failed","error":{"message":"Invalid schema for response_format"}}','')=='response_schema'


def test_failure_diagnostics_never_return_credentials_or_task_text():
    private='Bearer secret-account-token task material must not be logged'
    assert gateway.failure_category('',private)=='unclassified_cli_error'
    assert gateway.failure_category('',private+' Unauthorized 401')=='authentication'


def test_model_native_tool_restrictions_remain_in_the_invocation(monkeypatch,tmp_path):
    monkeypatch.setattr(gateway,'EXE','/fixture/codex')
    cmd=gateway.cli_command('gpt-6-astra','high',tmp_path/'schema',tmp_path/'reply')
    assert cmd[cmd.index('--sandbox')+1]=='read-only'
    disabled=[cmd[i+1] for i,v in enumerate(cmd) if v=='--disable']
    assert {'shell_tool','unified_exec','multi_agent','apps','plugins','browser_use','computer_use','view_image'}<=set(disabled)
