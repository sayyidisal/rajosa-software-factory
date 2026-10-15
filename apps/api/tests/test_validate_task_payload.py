import pytest


def validate_task_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Input payload must be a dictionary.")

    required_fields = ["title", "instruction"]
    for field in required_fields:
        if field not in payload:
            raise ValueError(f"Missing required field: '{field}'.")
        value = payload[field]
        if not isinstance(value, str):
            raise ValueError(f"Field '{field}' must be a string.")
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"Field '{field}' cannot be empty or whitespace only.")

    # Return new dictionary with stripped values
    return {field: payload[field].strip() for field in required_fields}


def test_valid_payload():
    payload = {"title": "Task Title", "instruction": "Do something important."}
    expected = {"title": "Task Title", "instruction": "Do something important."}
    assert validate_task_payload(payload) == expected


def test_strip_whitespace():
    payload = {"title": "  Task Title  ", "instruction": "\nDo something important.\t"}
    expected = {"title": "Task Title", "instruction": "Do something important."}
    assert validate_task_payload(payload) == expected


def test_missing_field():
    payload = {"title": "Task Title"}
    with pytest.raises(ValueError) as e:
        validate_task_payload(payload)
    assert "Missing required field: 'instruction'." in str(e.value)


def test_empty_string_field():
    payload = {"title": "  ", "instruction": "Do something"}
    with pytest.raises(ValueError) as e:
        validate_task_payload(payload)
    assert "Field 'title' cannot be empty or whitespace only." in str(e.value)


def test_wrong_type_field():
    payload = {"title": "Task Title", "instruction": 123}
    with pytest.raises(ValueError) as e:
        validate_task_payload(payload)
    assert "Field 'instruction' must be a string." in str(e.value)
