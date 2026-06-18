"""Parse Azure Function apps (Python or TypeScript)."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class FunctionBinding:
    name: str
    binding_type: str    # httpTrigger, timerTrigger, blobTrigger, serviceBusTrigger, ...
    direction: str       # in / out / inout
    extra: dict = field(default_factory=dict)


@dataclass
class FunctionInfo:
    path: str
    function_name: str
    language: str        # python / typescript
    trigger_type: str
    trigger_schedule: str
    bindings: list[FunctionBinding] = field(default_factory=list)
    http_methods: list[str] = field(default_factory=list)
    route: str = ""
    auth_level: str = ""
    source_code: str = ""
    description: str = ""


def parse_function_dir(directory: str) -> list[FunctionInfo]:
    """Parse all functions found under a directory."""
    results = []
    root = Path(directory)

    fa = root / "function_app.py"
    if fa.exists():
        results.extend(_parse_v2_python(str(fa)))
        return results

    for func_json in root.rglob("function.json"):
        info = _parse_v1_function(func_json.parent)
        if info:
            results.append(info)

    return results


def parse_file(path: str) -> Optional[FunctionInfo]:
    """Parse a single function.json or function_app.py."""
    p = Path(path)
    if p.name == "function.json":
        return _parse_v1_function(p.parent)
    if p.name == "function_app.py":
        funcs = _parse_v2_python(path)
        return funcs[0] if funcs else None
    return None


def _parse_v1_function(func_dir: Path) -> Optional[FunctionInfo]:
    func_json_path = func_dir / "function.json"
    if not func_json_path.exists():
        return None

    try:
        meta = json.loads(func_json_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    bindings_raw = meta.get("bindings", [])
    bindings = [_parse_binding(b) for b in bindings_raw]
    trigger = next((b for b in bindings if "trigger" in b.binding_type.lower()), None)

    py_files = list(func_dir.glob("*.py"))
    ts_files = list(func_dir.glob("*.ts")) + list(func_dir.glob("index.ts"))
    language = "python" if py_files else ("typescript" if ts_files else "unknown")

    source_files = py_files if language == "python" else ts_files
    source_code = ""
    description = ""
    for sf in source_files[:1]:
        source_code = sf.read_text(encoding="utf-8", errors="replace")
        description = _extract_docstring(source_code, language)

    http_methods = []
    route = ""
    auth_level = ""
    if trigger and trigger.binding_type.lower() == "httptrigger":
        http_methods = trigger.extra.get("methods", ["GET", "POST"])
        route = trigger.extra.get("route", "")
        auth_level = trigger.extra.get("authLevel", "")

    return FunctionInfo(
        path=str(func_dir),
        function_name=func_dir.name,
        language=language,
        trigger_type=trigger.binding_type if trigger else "unknown",
        trigger_schedule=trigger.extra.get("schedule", "") if trigger else "",
        bindings=bindings,
        http_methods=http_methods,
        route=route,
        auth_level=auth_level,
        source_code=source_code[:3000],
        description=description,
    )


def _parse_v2_python(path: str) -> list[FunctionInfo]:
    """Parse Azure Functions v2 Python SDK (decorator-based)."""
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    results = []

    pattern = re.compile(
        r'@app\.(route|timer_trigger|blob_trigger|service_bus_queue_trigger|event_hub_message_trigger|queue_trigger)'
        r'\(([^)]*)\)\s*\ndef\s+(\w+)',
        re.MULTILINE | re.DOTALL,
    )
    for m in pattern.finditer(source):
        trigger_kind = m.group(1)
        params_raw = m.group(2)
        func_name = m.group(3)

        trigger_map = {
            "route": "httpTrigger",
            "timer_trigger": "timerTrigger",
            "blob_trigger": "blobTrigger",
            "service_bus_queue_trigger": "serviceBusTrigger",
            "event_hub_message_trigger": "eventHubTrigger",
            "queue_trigger": "queueTrigger",
        }
        trigger_type = trigger_map.get(trigger_kind, trigger_kind)

        methods = re.findall(r'methods=\[([^\]]+)\]', params_raw)
        http_methods = [x.strip().strip('"\'') for x in methods[0].split(",")] if methods else []
        route = _extract_kwarg(params_raw, "route")
        schedule = _extract_kwarg(params_raw, "schedule")

        func_body_start = m.end()
        func_body = source[func_body_start:func_body_start + 500]
        description = _extract_docstring(func_body, "python")

        results.append(FunctionInfo(
            path=path,
            function_name=func_name,
            language="python",
            trigger_type=trigger_type,
            trigger_schedule=schedule,
            http_methods=http_methods,
            route=route,
            source_code=source[:3000],
            description=description,
        ))

    return results


def _parse_binding(b: dict) -> FunctionBinding:
    return FunctionBinding(
        name=b.get("name", ""),
        binding_type=b.get("type", ""),
        direction=b.get("direction", "in"),
        extra={k: v for k, v in b.items() if k not in ("name", "type", "direction")},
    )


def _extract_docstring(source: str, language: str) -> str:
    if language == "python":
        m = re.search(r'"""(.*?)"""', source, re.DOTALL)
        if m:
            return m.group(1).strip()[:300]
    elif language == "typescript":
        m = re.search(r'/\*\*(.*?)\*/', source, re.DOTALL)
        if m:
            return re.sub(r'\s*\*\s?', ' ', m.group(1)).strip()[:300]
    return ""


def _extract_kwarg(params_raw: str, key: str) -> str:
    m = re.search(rf'{key}\s*=\s*["\']([^"\']+)["\']', params_raw)
    return m.group(1) if m else ""
