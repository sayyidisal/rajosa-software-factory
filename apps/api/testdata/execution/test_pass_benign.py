"""Fixed benign fixture — no network, no filesystem escape, no secrets."""


def test_one():
    assert 1 + 1 == 2


def test_two():
    assert "rajosa".startswith("raj")


def test_three():
    assert [1, 2, 3][-1] == 3
