from server.hub.mock import MockHub
from server.setup_groups import apply_changes, plan_hub

PATTERN = "{hub}-p{port:02d}"


async def test_plan_on_factory_hub_and_apply(tmp_path):
    hub = MockHub("hub-a", backup_dir=tmp_path)
    changes = await plan_hub(hub, "hub-a", 8, PATTERN)
    assert {c.kind for c in changes} == {"move", "describe"}
    assert len([c for c in changes if c.kind == "move"]) == 7  # 端口 1 本来就在 group01
    await apply_changes(hub, "hub-a", changes, PATTERN)
    assert await plan_hub(hub, "hub-a", 8, PATTERN) == []  # 幂等：再规划无变更
    groups = {g.name: (g.ports, g.description) for g in await hub.list_groups()}
    assert groups["group06"] == ([6], "hub-a-p06")


async def test_plan_flags_missing_group(tmp_path):
    hub = MockHub("hub-a", ports=8, backup_dir=tmp_path)
    changes = await plan_hub(hub, "hub-a", 10, PATTERN)  # 声明 10 口，Hub 只有 8 个分组
    assert [c.kind for c in changes if c.port > 8] == ["missing", "missing"]
