import os
import stat

from server.config import (
    load_config, password_env_name, remove_hub_in_data, set_env_var, update_config_file, upsert_hub_in_data,
)

SRC = """# 顶部注释：不能丢
server:
  port: 8443            # 行尾注释：不能丢

hubs:
  - name: hub-a
    address: 10.70.1.208
    ports: 8
    password_env: HUB_PASSWORD_HUB_A
    tls_verify: false   # 自签证书
  # 后续 Hub 用向导添加
ukeys:                  # 端口 → 名称
  - {hub: hub-a, port: 6, label: "演示 U 盘（端口 6）"}
"""


def setup(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(SRC, encoding="utf-8")
    return p, tmp_path / "backups"


def test_add_hub_preserves_comments_and_backs_up(tmp_path):
    p, bdir = setup(tmp_path)
    new = {"name": "hub-b", "address": "10.70.1.209", "model": "AW24", "serial": "AW24-1", "ports": 24,
           "admin_user": "admin", "password_env": password_env_name("hub-b"), "tls_verify": False}
    bak = update_config_file(p, bdir, lambda d: upsert_hub_in_data(d, new, {1: "key one", 2: ""}))
    assert bak.read_text(encoding="utf-8") == SRC and bak.parent == bdir     # 备份是旧文件
    text = p.read_text(encoding="utf-8")
    for keep in ("# 顶部注释：不能丢", "# 行尾注释：不能丢", "# 自签证书", "# 后续 Hub 用向导添加", "# 端口 → 名称"):
        assert keep in text, keep
    cfg = load_config(p)
    assert [h.name for h in cfg.hubs] == ["hub-a", "hub-b"] and cfg.hub("hub-b").ports == 24
    keys = {(u["hub"], u["port"]): u["label"] for u in cfg.model_extra["ukeys"]}
    assert keys == {("hub-a", 6): "演示 U 盘（端口 6）", ("hub-b", 1): "key one"}   # 空标签不写


def test_edit_existing_hub_updates_labels_only_for_that_hub(tmp_path):
    p, bdir = setup(tmp_path)
    update_config_file(p, bdir, lambda d: upsert_hub_in_data(d, {"name": "hub-a", "tls_verify": True}, {3: "xbee"}))
    cfg = load_config(p)
    assert cfg.hub("hub-a").tls_verify is True and len(cfg.hubs) == 1
    assert {(u["port"], u["label"]) for u in cfg.model_extra["ukeys"]} == {(3, "xbee")}   # hub-a 的旧标签被替换


def test_remove_hub(tmp_path):
    p, bdir = setup(tmp_path)
    update_config_file(p, bdir, lambda d: upsert_hub_in_data(d, {"name": "hub-b", "address": "x", "ports": 4,
                                                                 "password_env": "X"}, {1: "k"}))
    update_config_file(p, bdir, lambda d: remove_hub_in_data(d, "hub-b"))
    cfg = load_config(p)
    assert [h.name for h in cfg.hubs] == ["hub-a"] and all(u["hub"] != "hub-b" for u in cfg.model_extra["ukeys"])
    assert len(list(bdir.glob("config-*.yaml"))) == 2


def test_set_env_var_creates_updates_and_locks_down(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n# comment\nHUB_PASSWORD_HUB_B=old\n")
    os.chmod(env, 0o644)
    set_env_var(env, "HUB_PASSWORD_HUB_B", "new pass")
    set_env_var(env, "HUB_PASSWORD_HUB_C", "c")
    assert env.read_text() == "A=1\n# comment\nHUB_PASSWORD_HUB_B=new pass\nHUB_PASSWORD_HUB_C=c\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    try:
        set_env_var(env, "X", "a\nb")
        raise AssertionError("应拒绝换行")
    except ValueError:
        pass


def test_password_env_name():
    assert password_env_name("hub-b") == "HUB_PASSWORD_HUB_B" and password_env_name("hub-a") == "HUB_PASSWORD_HUB_A"
