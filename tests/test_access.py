import pytest

import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


def test_whitelist_and_owner_protection():
    db.add_user(1, "Owner", "owner")
    db.add_user(2, "Guest")
    assert db.is_owner(1) and not db.is_owner(2)
    assert db.is_allowed(2) and not db.is_allowed(3)
    db.add_user(1, "", "user")          # повторное добавление не снимает роль владельца
    assert db.is_owner(1)
    assert not db.remove_user(1)        # владельца убрать нельзя
    assert db.remove_user(2) and not db.is_allowed(2)


def test_invite_is_single_use():
    token = db.create_invite(created_by=1)
    assert db.use_invite(token, 10) == 1
    assert db.use_invite(token, 11) is None
    assert db.use_invite("wrong-token", 12) is None


def test_expired_invite_rejected():
    token = db.create_invite(created_by=1, hours=-1)
    assert db.use_invite(token, 10) is None


def test_only_whitelisted_chats_get_notifications():
    db.add_chat(100)
    db.add_chat(200)
    db.add_user(100, "Me", "owner")
    assert [c["chat_id"] for c in db.get_chats()] == [100]
