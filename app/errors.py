class ExtractionError(Exception):
    pass


class UnsupportedFormatError(ExtractionError):
    pass


class ConversionError(ExtractionError):
    pass
