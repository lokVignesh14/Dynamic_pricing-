from pydantic import BaseModel


# Autopricing-only models.
# Currently the FastAPI endpoints in main.py use query parameters and
# return plain JSON, so these are placeholders for future request bodies
# if you decide to send autopricing data in the POST body instead.


class AutoPricingPreviewRequest(BaseModel):
    hotel_id: str


class AutoPricingApplyRequest(BaseModel):
    hotel_id: str