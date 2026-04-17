from pydantic import BaseModel


class ExtractionResponse(BaseModel):
    filename: str
    file_type: str | None = None
    plain_text: str | None = None
    error: str | None = None
