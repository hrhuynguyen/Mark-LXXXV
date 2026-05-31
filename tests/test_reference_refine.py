from __future__ import annotations

from server.tools.reference_images import ReferenceImage
from server.tools.reference_refine import (
    build_part_addition_prompt,
    build_part_replacement_prompt,
    build_reference_refine_prompt,
    create_restore_point,
    prepare_part_replacement,
    restore_from_restore_point,
)


class _FakeBlender:
    def __init__(self, result: str | list[str]) -> None:
        self.results = result if isinstance(result, list) else [result]
        self.calls: list[tuple[str, dict | None]] = []

    def send_command(self, command_type: str, params: dict | None = None) -> dict:
        self.calls.append((command_type, params))
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return {"result": self.results[index]}


def test_create_restore_point_sends_targeted_blender_code():
    fake = _FakeBlender('noise\n{"ok": true, "target": "landing_leg_1", "backups": []}\n')

    result = create_restore_point(fake, target_name="landing_leg_1", scope="part")

    assert result["ok"] is True
    assert result["target"] == "landing_leg_1"
    command, params = fake.calls[0]
    assert command == "execute_code"
    assert 'target_name = "landing_leg_1"' in params["code"]
    assert 'scope = "part"' in params["code"]


def test_restore_from_restore_point_sends_restore_payload():
    fake = _FakeBlender('{"ok": true, "restored": ["landing_leg_1"]}\n')

    result = restore_from_restore_point(
        fake,
        {
            "ok": True,
            "target": "landing_leg_1",
            "backups": [{"original": "landing_leg_1", "backup": "ForgeBackup_landing_leg_1"}],
        },
    )

    assert result == {"ok": True, "restored": ["landing_leg_1"]}
    command, params = fake.calls[0]
    assert command == "execute_code"
    assert '"original": "landing_leg_1"' in params["code"]


def test_prepare_part_replacement_backs_up_and_hides_target():
    fake = _FakeBlender(
        [
            'noise\n{"ok": true, "target": "landing_leg_1", "backups": []}\n',
            '{"ok": true, "hidden_original": "landing_leg_1"}\n',
        ]
    )

    result = prepare_part_replacement(fake, target_name="landing_leg_1")

    assert result["ok"] is True
    assert result["replacement"]["hidden_original"] == "landing_leg_1"
    assert len(fake.calls) == 2
    assert 'target_name = "landing_leg_1"' in fake.calls[0][1]["code"]
    assert 'target_name = "landing_leg_1"' in fake.calls[1][1]["code"]
    assert "hide_viewport = True" in fake.calls[1][1]["code"]


def test_build_reference_refine_prompt_limits_scope_and_carries_reference_context():
    prompt = build_reference_refine_prompt(
        snapshot={"name": "landing_leg_1"},
        reference=ReferenceImage(
            title="Falcon 9 landing leg",
            image_url="https://example.com/leg.jpg",
            context_url="https://example.com/source",
        ),
        reference_description="White telescoping strut with dark hinge hardware.",
        scope="part",
    )

    assert "Improve only the selected part" in prompt
    assert "Target Blender object name: landing_leg_1" in prompt
    assert "Falcon 9 landing leg" in prompt
    assert "https://example.com/leg.jpg" in prompt
    assert "White telescoping strut" in prompt
    assert "Do not modify unrelated objects" in prompt


def test_build_part_replacement_prompt_keeps_original_and_limits_scope():
    prompt = build_part_replacement_prompt(
        snapshot={
            "name": "landing_leg_1",
            "parent": "Falcon_9",
            "dimensions": [0.3, 0.4, 3.0],
            "location": [1.0, 2.0, 3.0],
            "rotation": [0.0, 0.0, 0.2],
            "scale": [1.0, 1.0, 1.0],
            "materials": ["white_metal"],
        },
        replacement_request="sleeker carbon-fiber landing leg",
    )

    assert "Original selected part name: landing_leg_1" in prompt
    assert "Parent object/group: Falcon_9" in prompt
    assert "sleeker carbon-fiber landing leg" in prompt
    assert "original selected part has already been backed up and hidden" in prompt
    assert "Create only the new replacement part" in prompt
    assert "Do not modify unrelated objects" in prompt


def test_build_part_addition_prompt_keeps_original_visible_and_limits_scope():
    prompt = build_part_addition_prompt(
        snapshot={
            "name": "rocket_body",
            "parent": "Falcon_9",
            "dimensions": [1.2, 1.2, 8.0],
            "location": [0.0, 0.0, 4.0],
            "rotation": [0.0, 0.0, 0.0],
            "scale": [1.0, 1.0, 1.0],
            "materials": ["white_metal"],
        },
        addition_request="small communication antenna",
    )

    assert "Selected anchor part name: rocket_body" in prompt
    assert "Parent object/group: Falcon_9" in prompt
    assert "small communication antenna" in prompt
    assert "Keep the original selected part and original object visible and unchanged" in prompt
    assert "Create only the new additional part" in prompt
    assert "Do not modify unrelated objects" in prompt
