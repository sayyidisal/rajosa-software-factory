def sanitize_max_chars(value: int, *, default: int) -> int:
    """Return a positive size limit; invalid values fall back to default."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return default
    return value
