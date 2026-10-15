"""Fixed benign failing fixture — deterministic assert failure only."""


def test_one():
    assert True


def test_two():
    assert False, "intentional benign failure for Phase 3C"


def test_three():
    assert True
