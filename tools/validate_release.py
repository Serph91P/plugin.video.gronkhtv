import json
from pathlib import Path


def write_validation_evidence(path: Path, evidence: dict[str, object]) -> None:
    Path(path).write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_validation_evidence(path: Path) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
