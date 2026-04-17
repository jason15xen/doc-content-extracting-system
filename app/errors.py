class ExtractionError(Exception):
    pass


class UnsupportedFormatError(ExtractionError):
    pass


class ConversionError(ExtractionError):
    pass


class DuplicateHashError(Exception):
    pass


class EmbeddingError(Exception):
    pass


class SearchIndexError(Exception):
    pass


class PipelineError(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message
