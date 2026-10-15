class AgentError(Exception):
    """Adapter failure with a client-safe public message."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message
