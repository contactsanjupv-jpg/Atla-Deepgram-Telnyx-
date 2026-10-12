from pydantic import BaseModel


class TestCallResponse(BaseModel):
    call_id: str
    access_token: str  # handed to the frontend's WebRTC client to join the call
    max_seconds: int  # the browser ends the call after this long (server-configured)
    is_free: bool = False  # True when this was the one promised free test call


class ActivateRequest(BaseModel):
    phone_number_id: str
    agent_id: str


class ActivateResponse(BaseModel):
    phone_number_id: str
    agent_id: str
    status: str
