import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class PreprocessCfg(BaseModel):
    min_chat_length_chars: int
    min_messages_after_filter: int
    drop_phrases_exact: list[str]
    drop_if_shorter_than_chars: int
    keep_if_contains_code_block: bool


class RuleDef(BaseModel):
    id: str
    name: str
    weight: float = Field(ge=0.0, le=5.0)
    description: str
    positive_signals: list[str]
    negative_signals: list[str]


class ChangelogChange(BaseModel):
    rule_id: str
    old_weight: float
    new_weight: float
    driving_comment_ids: list[str]


class ChangelogEntry(BaseModel):
    version: int
    at: str
    reason: str
    changes: list[ChangelogChange]


class RulesConfig(BaseModel):
    version: int
    threshold: int = Field(ge=10, le=100)
    preprocess: PreprocessCfg
    rules: list[RuleDef]
    changelog: list[ChangelogEntry]


def load_rules(path: Path) -> RulesConfig:
    return RulesConfig.model_validate(yaml.safe_load(path.read_text()))


def save_rules(path: Path, rc: RulesConfig) -> None:
    data = yaml.safe_dump(rc.model_dump(), sort_keys=False)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=".rules_", suffix=".yaml", delete=False,
    ) as tf:
        tf.write(data)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_name = tf.name
    os.replace(tmp_name, path)


def clamp_weight(w: float, *, weight_min: float, weight_max: float) -> float:
    return max(weight_min, min(weight_max, w))


def apply_weight_deltas(
    rc: RulesConfig,
    deltas: dict[str, float],
    *,
    driving_comment_ids: dict[str, list[str]],
    reason: str,
    weight_min: float,
    weight_max: float,
) -> RulesConfig:
    new_rules = []
    changes: list[ChangelogChange] = []
    for r in rc.rules:
        if r.id in deltas:
            new_w = clamp_weight(
                r.weight + deltas[r.id], weight_min=weight_min, weight_max=weight_max
            )
            if abs(new_w - r.weight) > 1e-9:
                changes.append(
                    ChangelogChange(
                        rule_id=r.id,
                        old_weight=r.weight,
                        new_weight=new_w,
                        driving_comment_ids=driving_comment_ids.get(r.id, []),
                    )
                )
                new_rules.append(r.model_copy(update={"weight": new_w}))
            else:
                new_rules.append(r)
        else:
            new_rules.append(r)

    if not changes:
        return rc  # no-op

    new_version = rc.version + 1
    entry = ChangelogEntry(
        version=new_version,
        at=datetime.now(timezone.utc).isoformat(),
        reason=reason,
        changes=changes,
    )
    return rc.model_copy(
        update={
            "version": new_version,
            "rules": new_rules,
            "changelog": [entry] + rc.changelog,
        }
    )


def git_commit(repo_path: Path, *, message: str) -> None:
    import subprocess
    subprocess.run(
        ["git", "add", "config/rules_config.yaml"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_path, check=True, capture_output=True,
    )
