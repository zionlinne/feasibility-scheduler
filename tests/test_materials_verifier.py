import json
from pathlib import Path

import pytest

from feasibility_scheduler.adapters.external_verifier import node_verifier
from feasibility_scheduler.adapters.legacy_doc import discover_reference_materials


def test_discover_materials_excludes_private_and_generated_sources(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "public").mkdir()
    (tmp_path / "public/expected.json").write_text('{"ok":true}')
    (tmp_path / "private").mkdir()
    (tmp_path / "private/verify.mjs").write_text("private")
    (tmp_path / "solution.mjs").write_text("generated")
    result = discover_reference_materials(tmp_path, {})
    assert set(result) == {"package.json", "public/expected.json"}
    with pytest.raises(ValueError, match="conflicts"):
        discover_reference_materials(tmp_path, {"package.json": "different"})


def test_verifier_report_must_be_for_this_solution_and_not_selfcheck(tmp_path):
    script = tmp_path / "verify.mjs"
    script.write_text("""import fs from 'node:fs';
const [solution, report]=process.argv.slice(2);
fs.writeFileSync(report,JSON.stringify({solution,selfCheckOnly:false,summary:{passed:1,failed:0,error:0},cases:[]}));
""")
    checker = node_verifier(script, tmp_path / "reports")
    assert checker(tmp_path)["status"] == "passed"
    script.write_text(script.read_text().replace("selfCheckOnly:false", "selfCheckOnly:true"))
    assert checker(tmp_path)["status"] == "unverified"
    assert len(list((tmp_path / "reports").glob("*.json"))) == 2


def test_missing_acceptance_script_fails_before_model_call(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        node_verifier(tmp_path / "missing.mjs", tmp_path)
