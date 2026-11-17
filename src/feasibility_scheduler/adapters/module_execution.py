"""Host-collected V8 execution evidence; never accept a model's usage declaration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


PRELOAD = r"""const fs=require('node:fs'),path=require('node:path'),v8=require('node:v8'),http=require('node:http');
const dir=process.env.NODE_V8_COVERAGE;
const flush=()=>{try{v8.takeCoverage()}catch{}};
if(dir){
  fs.writeFileSync(path.join(dir,'entry-'+process.pid+'.json'),JSON.stringify({entry:process.argv[1]?path.resolve(process.argv[1]):null}));
  const emit=http.Server.prototype.emit;
  http.Server.prototype.emit=function(event,...args){
    if(event==='request'&&args[1]){
      const end=args[1].end;
      args[1].end=function(...values){flush();return end.apply(this,values)};
    }
    return emit.call(this,event,...args);
  };
  process.once('SIGTERM',()=>{flush();if(process.listenerCount('SIGTERM')===0)process.kill(process.pid,'SIGTERM')});
}
"""


def coverage_environment(directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=False)
    preload = directory / "execution-preload.cjs"
    preload.write_text(PRELOAD, encoding="utf-8")
    environment = dict(os.environ)
    environment["NODE_V8_COVERAGE"] = str(directory.resolve())
    environment["NODE_OPTIONS"] = (environment.get("NODE_OPTIONS", "") + " --require "
                                   + json.dumps(preload.resolve().as_posix())).strip()
    return environment


def collect_execution(directory: Path, workspace: Path, required_files) -> dict:
    root = workspace.resolve()
    required = {p: (root / p).resolve() for p in required_files}
    for target in required.values():
        target.relative_to(root)
    by_path = {os.path.normcase(str(target)): name for name, target in required.items()}
    evidence = {p: {"loaded": False, "function_called": False, "has_functions": False, "entrypoint": False}
                for p in required}
    for entry in directory.glob("entry-*.json"):
        name = json.loads(entry.read_text(encoding="utf-8")).get("entry")
        if name is not None and os.path.normcase(str(Path(name).resolve())) in by_path:
            evidence[by_path[os.path.normcase(str(Path(name).resolve()))]]["entrypoint"] = True
    coverage_files = list(directory.glob("coverage-*.json"))
    for coverage in coverage_files:
        data = json.loads(coverage.read_text(encoding="utf-8"))
        for script in data["result"]:
            url = urlsplit(script.get("url", ""))
            if url.scheme != "file":
                continue
            target = os.path.normcase(str(Path(url2pathname(url.path)).resolve()))
            if target not in by_path:
                continue
            row = evidence[by_path[target]]
            functions = script.get("functions", [])
            if functions:
                row["loaded"] |= any(r.get("count", 0) > 0 for r in functions[0].get("ranges", []))
                row["has_functions"] |= len(functions) > 1
                row["function_called"] |= any(f.get("ranges", [{}])[0].get("count", 0) > 0 for f in functions[1:])
    missing = [p for p, row in evidence.items() if not row["loaded"]]
    unused = [p for p, row in evidence.items() if row["loaded"] and row["has_functions"]
              and not row["entrypoint"] and not row["function_called"]]
    return {"status": "passed" if coverage_files and not missing and not unused else "failed",
            "required_files": sorted(required), "files": evidence, "not_loaded": missing,
            "no_function_calls": unused, "coverage_file_count": len(coverage_files)}
