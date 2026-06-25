"""Parse Azure Data Factory JSON artefacts: pipelines, datasets, linked services."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ADFActivity:
    name: str
    activity_type: str
    depends_on: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    description: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ADFPipelineInfo:
    path: str
    name: str
    description: str
    activities: list[ADFActivity] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)

    @property
    def activity_flow(self) -> list[tuple[str, str]]:
        """Return (source_activity, target_activity) edges from dependsOn."""
        edges = []
        for act in self.activities:
            for dep in act.depends_on:
                edges.append((dep, act.name))
        return edges


@dataclass
class ADFDatasetInfo:
    path: str
    name: str
    linked_service: str
    dataset_type: str
    description: str = ""
    schema_defined: bool = False


@dataclass
class ADFLinkedServiceInfo:
    path: str
    name: str
    service_type: str
    description: str = ""


def parse_file(path: str) -> Optional[ADFPipelineInfo | ADFDatasetInfo | ADFLinkedServiceInfo]:
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return _classify_and_parse(data, path)


def parse_content(content: str, path: str) -> Optional[ADFPipelineInfo | ADFDatasetInfo | ADFLinkedServiceInfo]:
    """Parse ADF JSON from an in-memory string using *path* for classification hints."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    return _classify_and_parse(data, path)


def _classify_and_parse(data: dict, path: str) -> Optional[ADFPipelineInfo | ADFDatasetInfo | ADFLinkedServiceInfo]:
    # 1. ADF resource type field — most reliable
    adf_type = data.get("type", "").lower()
    if "pipelines" in adf_type:
        return _parse_pipeline(path, data)
    if "datasets" in adf_type:
        return _parse_dataset(path, data)
    if "linkedservices" in adf_type:
        return _parse_linked_service(path, data)
    if "triggers" in adf_type:
        return None  # triggers are not documented

    # 2. Path-based classification
    lower_path = path.lower().replace("\\", "/")
    if "pipeline/" in lower_path:
        return _parse_pipeline(path, data)
    if "dataset/" in lower_path:
        return _parse_dataset(path, data)
    if "linkedservice/" in lower_path:
        return _parse_linked_service(path, data)

    # 3. Content-based fallback
    props = data.get("properties", {})
    if "activities" in props:
        return _parse_pipeline(path, data)
    if "linkedServiceName" in props:
        return _parse_dataset(path, data)
    if "typeProperties" in props:
        return _parse_linked_service(path, data)
    return None


def _parse_pipeline(path: str, data: dict) -> ADFPipelineInfo:
    props = data.get("properties", {})
    activities_raw = props.get("activities", [])

    activities = []
    for a in activities_raw:
        depends = [d.get("activity", "") for d in a.get("dependsOn", [])]
        inputs  = [i.get("referenceName", "") for i in a.get("inputs", [])]
        outputs = [o.get("referenceName", "") for o in a.get("outputs", [])]

        extra = {}
        tp = a.get("typeProperties", {})
        if "source" in tp:
            extra["source_type"] = tp["source"].get("type", "")
        if "sink" in tp:
            extra["sink_type"] = tp["sink"].get("type", "")
        if "pipeline" in tp:
            extra["executes_pipeline"] = tp["pipeline"].get("referenceName", "")

        activities.append(ADFActivity(
            name=a.get("name", ""),
            activity_type=a.get("type", ""),
            depends_on=depends,
            inputs=inputs,
            outputs=outputs,
            description=a.get("description", ""),
            extra=extra,
        ))

    return ADFPipelineInfo(
        path=path,
        name=data.get("name", Path(path).stem),
        description=props.get("description", ""),
        activities=activities,
        parameters=list(props.get("parameters", {}).keys()),
        variables=list(props.get("variables", {}).keys()),
        annotations=props.get("annotations", []),
    )


def _parse_dataset(path: str, data: dict) -> ADFDatasetInfo:
    props = data.get("properties", {})
    ls = props.get("linkedServiceName", {})
    linked_service = ls.get("referenceName", "") if isinstance(ls, dict) else ""

    return ADFDatasetInfo(
        path=path,
        name=data.get("name", Path(path).stem),
        linked_service=linked_service,
        dataset_type=props.get("type", ""),
        description=props.get("description", ""),
        schema_defined=bool(props.get("schema", [])),
    )


def _parse_linked_service(path: str, data: dict) -> ADFLinkedServiceInfo:
    props = data.get("properties", {})
    return ADFLinkedServiceInfo(
        path=path,
        name=data.get("name", Path(path).stem),
        service_type=props.get("type", ""),
        description=props.get("description", ""),
    )
