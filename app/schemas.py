from pydantic import BaseModel


class ExtractionResponse(BaseModel):
    filename: str
    file_type: str
    plain_text: str
