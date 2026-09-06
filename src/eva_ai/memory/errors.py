class MemoryError(Exception):
    """Base class for content-free memory failures."""


class MemoryNotFoundError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory was not found")


class MemoryScopeError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory scope is invalid")


class MemoryConflictError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory changed concurrently")


class UnsafeMemoryError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory content is not eligible for storage")


class MemoryEmbeddingError(MemoryError):
    def __init__(self) -> None:
        super().__init__("Memory embedding failed")
