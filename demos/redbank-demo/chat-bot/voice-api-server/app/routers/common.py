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

from fastapi import HTTPException
from ..services.responses_svc import ResponseService
from ..config import settings


def get_response_service(response_instance: ResponseService) -> ResponseService:
    """Get or initialize response service instance"""
    return response_instance


async def start_session_helper(response_service: ResponseService, logger):
    """Helper function to start a new agent session"""
    try:
        session_id = response_service.create_session()
        if session_id:
            logger.info(f"Created new agent session: {session_id}")
            return {"session_id": session_id, "status": "created"}
        else:
            raise HTTPException(
                status_code=500, detail="Failed to create agent session"
            )
    except Exception as e:
        logger.error(f"Session creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def chat_helper(text: str, response_service: ResponseService, logger):
    """Helper function to chat with the agent"""
    try:
        tools_ctx = {"vdb_url": settings.vdb_url}
        agent_resp = response_service.invoke(text, tools_ctx)
        agent_text = (
            agent_resp.get("output") or agent_resp.get("text") or str(agent_resp)
        )

        logger.info(f"User: {text}")
        logger.info(f"Agent: {agent_text}")

        return {
            "user_input": text,
            "agent_response": agent_text,
            "conversation_length": len(response_service.conversation_history),
        }
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def clear_conversation_helper(response_service: ResponseService, logger):
    """Helper function to clear conversation history"""
    try:
        response_service.clear_conversation()
        logger.info("Conversation history cleared")
        return {"status": "cleared", "message": "Conversation history has been cleared"}
    except Exception as e:
        logger.error(f"Clear conversation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
