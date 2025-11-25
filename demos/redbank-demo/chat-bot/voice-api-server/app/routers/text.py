# Copyright 2025 IBM, Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ..services.responses_svc import ResponseService
from ..deps import get_logger
from ..config import settings
from .common import start_session_helper, chat_helper

router = APIRouter(prefix="/api/text", tags=["text"])

_response = None


def _get_response():
    global _response
    if _response is None:
        _response = ResponseService(
            settings.llamastack_url,
            settings.inference_model,
            settings.vector_store_name,
            settings.mcp_url,
        )
    return _response


class TextInput(BaseModel):
    text: str


@router.post("/complete")
async def complete(input_data: TextInput, logger=Depends(get_logger)):
    text = input_data.text
    logger.info(f"Received text input: {len(text)} chars")
    agent_resp = _get_response().invoke(text, settings.model_instructions)
    response_text = (
        agent_resp.get("output") or agent_resp.get("text") or str(agent_resp)
    )
    return JSONResponse(
        {
            "transcript": text,
            "agent_text": response_text,
        }
    )


@router.post("/session/start")
async def start_session(logger=Depends(get_logger)):
    """Start a new agent session"""
    return await start_session_helper(_get_response(), logger)


@router.post("/chat")
async def chat_with_agent(input_data: TextInput, logger=Depends(get_logger)):
    """Chat with the agent using text input"""
    return await chat_helper(input_data.text, _get_response(), logger)


@router.post("/conversation/clear")
async def clear_conversation(logger=Depends(get_logger)):
    """Clear the conversation history"""
    try:
        _get_response().clear_conversation()
        logger.info("Conversation history cleared")
        return {"status": "cleared", "message": "Conversation history has been cleared"}
    except Exception as e:
        logger.error(f"Clear conversation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
